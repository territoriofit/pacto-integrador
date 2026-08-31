# -*- coding: utf-8 -*-
"""
Indica Território pós-NPS → promotores do NPS próprio (nota 9-10).

Quem respondeu o NPS com nota alta recebe NO DIA SEGUINTE o convite pro
programa de indicação (1 mês grátis por amigo matriculado + 1 pro amigo,
brinde aos 5; *FIT6/FIT12 por tempo limitado). Campanha CONTÍNUA — roda
todo dia útil, cada aluno recebe UMA única vez na vida.

Fluxo diário (GitHub Actions):
  1. Busca em nps_respostas os promotores (score>=9, source=proprio) que
     responderam ONTEM (janela de LOOKBACK_DIAS dias pra trás, resiliente
     a runs que falharem; nunca pega resposta de hoje — D+1 sempre).
  2. Pula quem relatou problema na pesquisa (metadata.problema.teve),
     mesmo com nota alta — caso é da consultora, não de pedir indicação.
  3. Resolve o lead: só envia pra status "cliente" (inadimplente fica
     fora — não misturar com a régua de cobrança) e telefone válido.
  4. Dedup permanente por lead em agent_activity (metadata.disparo_key =
     "indicacao-nps-<lead_id>"); também pula quem já recebeu o convite da
     campanha julho/26 e quem já tem código gerado (indicacao_codigos,
     por lead_id ou últimos 8 dígitos do telefone).
  5. Envia pela instância "Whats TF 2000", máx MAX_POR_RUN por dia
     (padrão 15), jitter de início (até 40min) + 90-150s entre mensagens
     (padrão anti-bloqueio da casa). Janela 09:00-19:30 BRT.
  6. Log em agent_activity com lead_id (monitor de atendimento vigia
     resposta sem atendimento).

Env: SUPABASE_KEY, UAZAPI_TOKEN_2000. Opcional: LOOKBACK_DIAS=n (padrão
3) | HORA_INICIO=h (padrão 12) | JITTER_MAX_MIN=n (padrão 40) |
MAX_POR_RUN=n (padrão 15) | DRY_RUN=1 | TEST_TO=5516... (1 exemplo, sem
dedup).
"""

import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
TENANT_ID = "4eeff494-6528-4f49-8a5c-2742eabb8c2c"
UAZAPI_URL = "https://territoriofit.uazapi.com"

TZ_SP = timezone(timedelta(hours=-3))

CAMPANHA = "indicacao-nps"

COPY = (
    "Oi, {nome}! Aqui é da Território Fit 🧡\n\n"
    "Vimos que você tá curtindo treinar com a gente — isso deixa a equipe "
    "toda feliz demais! 🙌\n\n"
    "Então bora trazer um amigo pro seu território? No *Indica Território*, "
    "cada amigo que se matricular com o seu código te dá *1 MÊS GRÁTIS* — "
    "e o seu amigo também ganha 1 mês! Acumulou 5 amigos, você ainda "
    "escolhe um brinde: camiseta ou garrafinha personalizada da "
    "Território.\n\n"
    "Pega seu código em 30 segundos aqui 👉 "
    "https://crm.territoriofit.com.br/indica.html\n\n"
    "_*Campanha promocional por tempo limitado, válida na aquisição dos "
    "planos promocionais FIT6 e FIT12._"
)


def _env(nome: str, padrao: str = "") -> str:
    return os.environ.get(nome, padrao).replace("﻿", "").strip()


def _sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _primeiro_nome(nome: str) -> str:
    p = (nome or "").replace("﻿", "").strip().split()
    return p[0].title() if p else "tudo bem"


def _fone_55(phone: str) -> str | None:
    d = "".join(c for c in (phone or "") if c.isdigit())
    if len(d) in (10, 11):
        return "55" + d
    if len(d) in (12, 13) and d.startswith("55"):
        return d
    return None


def aguardar_janela_comercial(hora_inicio: int) -> bool:
    agora = datetime.now(TZ_SP)
    if agora.hour >= 20 or (agora.hour == 19 and agora.minute > 30):
        print(f"[janela] {agora:%H:%M} BRT — tarde demais, abortando.")
        return False
    while agora.hour < hora_inicio:
        espera = min(1800,
                     (hora_inicio - agora.hour) * 3600 - agora.minute * 60)
        print(f"[janela] {agora:%H:%M} BRT — aguardando {espera//60} min...")
        time.sleep(espera)
        agora = datetime.now(TZ_SP)
    return True


def _dedup_existe(sb: dict, key: str) -> bool:
    rj = requests.get(
        f"{SUPABASE_URL}/rest/v1/agent_activity",
        params={"select": "id", "metadata->>disparo_key": f"eq.{key}",
                "limit": "1"},
        headers=sb, timeout=30).json()
    return bool(rj)


def _recebeu_campanha_julho(sb: dict, lead_id: str) -> bool:
    rj = requests.get(
        f"{SUPABASE_URL}/rest/v1/agent_activity",
        params={"select": "id",
                "metadata->>campanha": "eq.indicacao-julho26",
                "metadata->>lead_id": f"eq.{lead_id}",
                "limit": "1"},
        headers=sb, timeout=30).json()
    return bool(rj)


def _ja_tem_codigo(sb: dict, lead_id: str, fone: str) -> bool:
    rj = requests.get(
        f"{SUPABASE_URL}/rest/v1/indicacao_codigos",
        params={"select": "id", "aluno_lead_id": f"eq.{lead_id}",
                "limit": "1"},
        headers=sb, timeout=30).json()
    if rj:
        return True
    rj = requests.get(
        f"{SUPABASE_URL}/rest/v1/indicacao_codigos",
        params={"select": "id", "aluno_telefone": f"ilike.*{fone[-8:]}",
                "limit": "1"},
        headers=sb, timeout=30).json()
    return bool(rj)


def _log_activity(sb: dict, title: str, detail: str, metadata: dict) -> None:
    requests.post(
        f"{SUPABASE_URL}/rest/v1/agent_activity",
        headers={**sb, "Prefer": "return=minimal"},
        json={"agent_slug": "crm-relacionamento", "title": title,
              "detail": detail, "status": "concluido",
              "metadata": metadata},
        timeout=30)


def main() -> int:
    key = _env("SUPABASE_KEY")
    zap = _env("UAZAPI_TOKEN_2000")
    dry = _env("DRY_RUN") == "1"
    test_to = _env("TEST_TO")
    max_por_run = int(_env("MAX_POR_RUN", "15"))
    hora_inicio = int(_env("HORA_INICIO", "12"))
    jitter_max = int(_env("JITTER_MAX_MIN", "40"))
    lookback = int(_env("LOOKBACK_DIAS", "3"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1

    sb = _sb_headers(key)

    if not dry and not aguardar_janela_comercial(hora_inicio):
        return 0

    if not dry and not test_to and jitter_max > 0:
        atraso = random.uniform(0, jitter_max * 60)
        print(f"[jitter] variando horário do disparo: +{int(atraso // 60)}min")
        time.sleep(atraso)

    # janela D+1: respostas de ontem pra trás (lookback), nunca de hoje
    hoje_brt = datetime.now(TZ_SP).replace(hour=0, minute=0, second=0,
                                           microsecond=0)
    ini = (hoje_brt - timedelta(days=lookback)).astimezone(timezone.utc)
    fim = hoje_brt.astimezone(timezone.utc)

    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/nps_respostas",
        params=[("select", "id,person_name,score,metadata,lead_id,"
                           "answered_at"),
                ("score", "gte.9"),
                ("source", "eq.proprio"),
                ("lead_id", "not.is.null"),
                ("answered_at", f"gte.{ini:%Y-%m-%dT%H:%M:%S}Z"),
                ("answered_at", f"lt.{fim:%Y-%m-%dT%H:%M:%S}Z"),
                ("order", "answered_at")],
        headers=sb, timeout=30)
    r.raise_for_status()
    respostas = r.json()
    print(f"[lista] promotores (9-10) entre {ini:%d/%m} e {fim:%d/%m}: "
          f"{len(respostas)} resposta(s)")

    enviados, pulados, sem_fone, fora_regra = 0, 0, 0, 0
    fones_do_run: set[str] = set()

    for resp_nps in respostas:
        lead_id = resp_nps["lead_id"]
        nome = resp_nps.get("person_name") or ""
        disparo_key = f"{CAMPANHA}-{lead_id}"

        if _dedup_existe(sb, disparo_key):
            pulados += 1
            continue

        if enviados >= max_por_run:
            print("[limite] MAX_POR_RUN atingido — resto fica pro próximo run.")
            break

        # relatou problema na pesquisa? consultora resolve primeiro
        problema = ((resp_nps.get("metadata") or {}).get("problema") or {})
        if problema.get("teve"):
            fora_regra += 1
            continue

        rl = requests.get(
            f"{SUPABASE_URL}/rest/v1/leads",
            params={"select": "id,phone,name,status",
                    "id": f"eq.{lead_id}", "limit": "1"},
            headers=sb, timeout=30).json()
        if not rl:
            fora_regra += 1
            continue
        fone = _fone_55(rl[0].get("phone"))
        if not fone:
            sem_fone += 1
            continue
        status = (rl[0].get("status") or "").lower()
        if status != "cliente":
            # inadimplente fica fora (régua de cobrança) e inativo também
            fora_regra += 1
            continue
        if fone in fones_do_run:
            pulados += 1
            continue
        if _recebeu_campanha_julho(sb, lead_id) or \
                _ja_tem_codigo(sb, lead_id, fone):
            pulados += 1
            continue

        destino = test_to or fone
        texto = COPY.format(nome=_primeiro_nome(nome))
        if dry:
            print(f"[DRY] {nome} (nota {resp_nps.get('score')}, {fone})")
            enviados += 1
            continue

        resp = requests.post(
            f"{UAZAPI_URL}/send/text",
            headers={"token": zap, "Content-Type": "application/json"},
            json={"number": destino, "text": texto}, timeout=120)
        ok = resp.status_code == 200
        print(f"[send] {nome} -> ...{destino[-4:]} HTTP {resp.status_code}")
        if not ok:
            print("       resp:", resp.text[:200])
            continue

        enviados += 1
        fones_do_run.add(fone)
        if test_to:
            print("[teste] enviado 1 exemplo pro TEST_TO — encerrando.")
            return 0

        _log_activity(
            sb, "Indica Território pós-NPS enviado no WhatsApp",
            f"{nome.title()} — deu nota {resp_nps.get('score')} no NPS e "
            "recebeu o convite do programa de indicação (1 mês grátis por "
            "amigo) pelo Whats 2000",
            {"disparo_key": disparo_key, "campanha": CAMPANHA,
             "lead_id": lead_id, "resposta_id": resp_nps["id"],
             "score": resp_nps.get("score")})

        time.sleep(90 + random.uniform(0, 60))

    print(f"\nResumo: {enviados} enviado(s), {pulados} já convidados/dup, "
          f"{sem_fone} sem telefone, {fora_regra} fora da régua "
          "(problema relatado / não-cliente)")

    if not dry and not test_to and enviados:
        _log_activity(
            sb, "Indica Território pós-NPS (run diário)",
            f"{enviados} promotor(es) do NPS convidados a indicar amigos, "
            f"{pulados} já convidados, {sem_fone} sem telefone, "
            f"{fora_regra} fora da régua.",
            {"campanha": CAMPANHA})
    return 0


if __name__ == "__main__":
    sys.exit(main())
