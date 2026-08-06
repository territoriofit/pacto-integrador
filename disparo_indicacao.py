# -*- coding: utf-8 -*-
"""
Campanha Indica Território → alunos novos matriculados em julho/2026.

Convida os matriculados do mês (vendas_pacto tipo MA) a indicar amigos:
1 mês grátis pra quem indica (acumulativo, brinde aos 5) e 1 mês pro
indicado; asterisco de campanha por tempo limitado (planos FIT6/FIT12).
Link: crm.territoriofit.com.br/indica.html

Fluxo diário (GitHub Actions, até esgotar a lista — pedido do André 06/08):
  1. Busca em vendas_pacto os contratos tipo MA com data_venda em julho/26.
  2. Resolve telefone + status pela tabela leads (match exato de nome);
     só envia pra quem segue aluno ativo (cliente/inadimplente).
  3. Dedup por pessoa em agent_activity (metadata.disparo_key =
     "indicacao-julho26-<contrato>") + dedup de telefone dentro do run.
  4. Envia texto pela instância UazAPI "Whats TF 2000", máx MAX_POR_RUN
     por dia (padrão 20), com jitter de início (até 40min) e 90-150s
     entre mensagens (padrão anti-bloqueio da casa).
  5. Quando a lista ESGOTAR: avisa o André no WhatsApp pessoal (instância
     CEO; 1 aviso só, dedup "indicacao-julho26-CONCLUIDA") e os runs
     seguintes encerram cedo.

Janela de envio: 09:00-19:30 BRT.

Env: SUPABASE_KEY, UAZAPI_TOKEN_2000. Opcional: UAZAPI_TOKEN_CEO (aviso
final; fallback 2000) | ALERTA_PARA=5516... (padrão 5516992290338) |
HORA_INICIO=h (padrão 9) | JITTER_MAX_MIN=n (padrão 40) | MAX_POR_RUN=n
(padrão 20) | DRY_RUN=1 | TEST_TO=5516... (1 exemplo, sem dedup).
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

CAMPANHA = "indicacao-julho26"
JANELA = ("2026-07-01", "2026-07-31")

COPY = (
    "Oi, {nome}! Aqui é da Território Fit 🧡\n\n"
    "Que bom ter você treinando com a gente! Bora trazer um amigo pro seu "
    "território? 😎\n\n"
    "No *Indica Território*, cada amigo que se matricular com o seu código "
    "te dá *1 MÊS GRÁTIS* — e o seu amigo também ganha 1 mês! Acumulou 5 "
    "amigos, você ainda escolhe um brinde: camiseta ou garrafinha "
    "personalizada da Território.\n\n"
    "Pega seu código em 30 segundos aqui 👉 "
    "https://crm.territoriofit.com.br/indica.html\n\n"
    "_*Campanha promocional por tempo limitado, válida na aquisição dos "
    "planos promocionais FIT6 e FIT12._"
)


def _sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _primeiro_nome(nome: str) -> str:
    p = (nome or "").strip().split()
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


def _log_activity(sb: dict, title: str, detail: str, metadata: dict) -> None:
    requests.post(
        f"{SUPABASE_URL}/rest/v1/agent_activity",
        headers={**sb, "Prefer": "return=minimal"},
        json={"agent_slug": "crm-relacionamento", "title": title,
              "detail": detail, "status": "concluido",
              "metadata": metadata},
        timeout=30)


def avisar_andre(texto: str) -> bool:
    token = (os.environ.get("UAZAPI_TOKEN_CEO", "").strip()
             or os.environ.get("UAZAPI_TOKEN_2000", "").strip())
    para = os.environ.get("ALERTA_PARA", "5516992290338").strip()
    if not token:
        return False
    r = requests.post(
        f"{UAZAPI_URL}/send/text",
        headers={"token": token, "Content-Type": "application/json"},
        json={"number": para, "text": texto}, timeout=60)
    print(f"[aviso] André ...{para[-4:]} HTTP {r.status_code}")
    return r.status_code == 200


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_2000", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    test_to = os.environ.get("TEST_TO", "").strip()
    max_por_run = int(os.environ.get("MAX_POR_RUN", "20"))
    hora_inicio = int(os.environ.get("HORA_INICIO", "9"))
    jitter_max = int(os.environ.get("JITTER_MAX_MIN", "40"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1

    sb = _sb_headers(key)

    # campanha já concluída? encerra cedo (aviso único já foi)
    if _dedup_existe(sb, f"{CAMPANHA}-CONCLUIDA"):
        print("[fim] campanha já concluída — nada a fazer.")
        return 0

    if not dry and not aguardar_janela_comercial(hora_inicio):
        return 0

    # anti-bloqueio: horário alternado por dia (jitter de início)
    if not dry and not test_to and jitter_max > 0:
        atraso = random.uniform(0, jitter_max * 60)
        print(f"[jitter] variando horário do disparo: +{int(atraso // 60)}min")
        time.sleep(atraso)

    # matrículas (MA) de julho/2026
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/vendas_pacto",
        params=[("select", "codigo_contrato,nome_cliente,data_venda,tipo"),
                ("tipo", "eq.MA"),
                ("data_venda", f"gte.{JANELA[0]}"),
                ("data_venda", f"lte.{JANELA[1]}"),
                ("order", "data_venda")],
        headers=sb, timeout=30)
    r.raise_for_status()
    vendas = r.json()
    print(f"[lista] matrículas MA em julho/26: {len(vendas)} contrato(s)")

    enviados, pulados, sem_fone, inativos, restantes = 0, 0, [], 0, 0
    fones_do_run: set[str] = set()

    for v in vendas:
        contrato = v["codigo_contrato"]
        nome = v.get("nome_cliente") or ""
        disparo_key = f"{CAMPANHA}-{contrato}"

        if _dedup_existe(sb, disparo_key):
            pulados += 1
            continue

        if enviados >= max_por_run:
            restantes += 1
            continue

        # telefone + status via leads (match exato case-insensitive)
        rl = requests.get(
            f"{SUPABASE_URL}/rest/v1/leads",
            params={"select": "id,phone,name,status",
                    "tenant_id": f"eq.{TENANT_ID}",
                    "name": f"ilike.{nome}",
                    "limit": "2"},
            headers=sb, timeout=30).json()
        fone = _fone_55(rl[0].get("phone")) if len(rl) >= 1 else None
        if not fone:
            sem_fone.append(f"{nome} ({v['data_venda']})")
            continue
        status = (rl[0].get("status") or "").lower()
        if status not in ("cliente", "inadimplente"):
            # matriculou em julho mas já não é aluno ativo — não convida
            inativos += 1
            continue
        if fone in fones_do_run:
            pulados += 1
            continue

        destino = test_to or fone
        texto = COPY.format(nome=_primeiro_nome(nome))
        if dry:
            print(f"[DRY] {nome} ({fone})")
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
            sb, "Indica Território enviado no WhatsApp",
            f"{nome.title()} — convite do programa de indicação (aluno novo "
            "de julho/26, 1 mês grátis por amigo) pelo Whats 2000",
            {"disparo_key": disparo_key, "codigo_contrato": contrato,
             "campanha": CAMPANHA})

        # anti-bloqueio: 90-150s entre mensagens
        time.sleep(90 + random.uniform(0, 60))

    print(f"\nResumo: {enviados} enviado(s), {pulados} já enviados/dup, "
          f"{len(sem_fone)} sem telefone, {inativos} não-ativos, "
          f"{restantes} ficam pro próximo run")
    if sem_fone:
        print("Sem telefone:", "; ".join(sem_fone))

    if not dry and not test_to and enviados:
        _log_activity(
            sb, "Campanha Indica Território julho/26 (run diário)",
            f"{enviados} convite(s) de indicação enviados no WhatsApp, "
            f"{pulados} já tinham recebido, {len(sem_fone)} sem telefone, "
            f"{inativos} não-ativos, {restantes} restando.",
            {"campanha": CAMPANHA})

    # lista esgotou? avisa o André UMA vez e marca a campanha como concluída
    if not dry and not test_to and restantes == 0:
        total = pulados + enviados
        aviso = (
            "✅ *Campanha Indica Território — alunos novos de julho/26 "
            "CONCLUÍDA!*\n\n"
            f"Todos os envios foram feitos: {total} aluno(s) convidados a "
            "indicar amigos (20/dia, com espaçamento anti-bloqueio).\n"
            f"Sem telefone: {len(sem_fone)} · Não-ativos pulados: {inativos}\n\n"
            "Acompanhe os códigos gerados e indicações em "
            "crm.territoriofit.com.br/gestao/indicacoes 🧡")
        if avisar_andre(aviso):
            _log_activity(
                sb, "Campanha Indica Território julho/26 CONCLUÍDA",
                f"Lista esgotada: {total} convidados no total. André avisado "
                "no WhatsApp.",
                {"disparo_key": f"{CAMPANHA}-CONCLUIDA",
                 "campanha": CAMPANHA})
    return 0


if __name__ == "__main__":
    sys.exit(main())
