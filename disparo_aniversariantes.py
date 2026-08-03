# -*- coding: utf-8 -*-
"""
Campanha aniversariantes inativos: ex-alunos que fazem aniversario no mes
corrente ganham 2 MESES GRATIS no ato da rematricula (gancho "no mes do seu
aniversario o presente e seu").

Fluxo diario (GitHub Actions, roda o mes inteiro):
  1. Busca no CRM (Supabase) os leads com status=inativo e
     metadata.aniversariante_mes_atual=true — a flag e mantida pelo
     sync_aniversariantes do sincronizar_diario (ativos+inativos, com
     ultimo_acesso da catraca preenchido).
  2. Filtra quem treinou nos ultimos MESES_ULTIMO_ACESSO meses (padrao 24);
     sem ultimo_acesso registrado = fora (numeros muito antigos).
  3. Dedup por agent_activity (metadata.disparo_key =
     "aniver-<YYYYMM>-<lead_id>") — 1 envio por pessoa por mes de campanha.
  4. Envia a ARTE da campanha com a copy como legenda pela instancia UazAPI
     "Whats TF 2000", ate MAX_POR_RUN por dia (padrao 25), 90-150s entre
     envios, priorizando quem faz aniversario mais cedo no mes.

Quem responder "PRESENTE" cai na consultora. Ex-alunos ja existem como lead
no CRM, entao o fallback da Clara (so contato NOVO) nao intercepta.

Janela de envio: 09:00-19:30 BRT.

Env: SUPABASE_KEY, UAZAPI_TOKEN_2000.
Opcional: MESES_ULTIMO_ACESSO=n (padrao 24) | HORA_INICIO=h (padrao 9) |
DRY_RUN=1 (so lista) | TEST_TO=5516... (envia 1 exemplo, sem gravar dedup) |
MAX_POR_RUN=n (padrao 25).
"""

import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"

TZ_SP = timezone(timedelta(hours=-3))

ARTE_URL = ("https://raw.githubusercontent.com/territoriofit/"
            "pacto-integrador/main/assets/aniversariantes_inativos.png")

COPY = (
    "Oi, {nome}! 🎁 Esse mês é especial — é o mês do SEU aniversário!\n\n"
    "E aqui NA Território Fit, no seu mês quem ganha o presente é VOCÊ:\n\n"
    "🎁 *2 MESES GRÁTIS* voltando a treinar com a gente, "
    "no ato da sua rematrícula\n\n"
    "Vale só durante o mês do seu aniversário, hein? 😉\n"
    "Responde *PRESENTE* que nossa consultora garante o seu!"
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


def buscar_aniversariantes_inativos(sb: dict, meses_max: int) -> list[dict]:
    """Leads inativos com aniversario no mes e ultimo acesso recente."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/leads",
        params={"select": "id,name,phone,metadata",
                "source": "eq.pacto_aluno",
                "status": "eq.inativo",
                "metadata->>aniversariante_mes_atual": "eq.true",
                "limit": "2000"},
        headers=sb, timeout=60)
    r.raise_for_status()
    todos = r.json()

    corte = (datetime.now(TZ_SP) - timedelta(days=meses_max * 30)).date()
    alvos, sem_acesso, antigos = [], 0, 0
    for ld in todos:
        meta = ld.get("metadata") or {}
        ua = meta.get("ultimo_acesso")
        if not ua:
            sem_acesso += 1
            continue
        try:
            if datetime.strptime(ua[:10], "%Y-%m-%d").date() < corte:
                antigos += 1
                continue
        except ValueError:
            sem_acesso += 1
            continue
        alvos.append(ld)

    # quem faz aniversario mais cedo no mes vai primeiro
    def _dia(ld):
        nasc = (ld.get("metadata") or {}).get("data_nascimento") or ""
        try:
            return int(nasc[8:10])
        except ValueError:
            return 32
    alvos.sort(key=_dia)

    print(f"[crm] {len(todos)} aniversariantes inativos no mes; "
          f"{len(alvos)} com treino nos ultimos {meses_max} meses "
          f"({sem_acesso} sem acesso registrado, {antigos} muito antigos)")
    return alvos


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_2000", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    test_to = os.environ.get("TEST_TO", "").strip()
    max_por_run = int(os.environ.get("MAX_POR_RUN", "25"))
    hora_inicio = int(os.environ.get("HORA_INICIO", "9"))
    meses_max = int(os.environ.get("MESES_ULTIMO_ACESSO", "24"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1

    tag = datetime.now(TZ_SP).strftime("%Y%m")
    print(f"[campanha] aniversariantes inativos {tag}")

    if not dry and not aguardar_janela_comercial(hora_inicio):
        return 0

    sb = _sb_headers(key)
    alvos = buscar_aniversariantes_inativos(sb, meses_max)
    enviados, pulados, sem_fone = 0, 0, []

    for ld in alvos:
        if enviados >= max_por_run:
            print(f"[lote] limite de {max_por_run} atingido — "
                  "o resto vai no proximo run.")
            break

        nome = ld.get("name") or ""
        disparo_key = f"aniver-{tag}-{ld['id']}"

        rj = requests.get(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            params={"select": "id",
                    "metadata->>disparo_key": f"eq.{disparo_key}",
                    "limit": "1"},
            headers=sb, timeout=30).json()
        if rj:
            pulados += 1
            continue

        fone = _fone_55(ld.get("phone") or "")
        if not fone:
            sem_fone.append(nome)
            continue

        destino = test_to or fone
        legenda = COPY.format(nome=_primeiro_nome(nome))
        if dry:
            dia = ((ld.get("metadata") or {}).get("data_nascimento") or "")[8:10]
            print(f"[DRY] dia {dia} — {nome} ({fone})")
            enviados += 1
            continue

        resp = requests.post(
            f"{UAZAPI_URL}/send/media",
            headers={"token": zap, "Content-Type": "application/json"},
            json={"number": destino, "type": "image",
                  "file": ARTE_URL, "text": legenda}, timeout=120)
        ok = resp.status_code == 200
        print(f"[send] {nome} -> ...{destino[-4:]} HTTP {resp.status_code}")
        if not ok:
            print("       resp:", resp.text[:200])
            continue

        enviados += 1
        if test_to:
            print("[teste] enviado 1 exemplo pro TEST_TO — encerrando.")
            return 0

        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": "Aniversariante inativo — presente enviado",
                  "detail": f"{nome.title()} — 2 meses gratis no ato da "
                            "rematricula (mes do aniversario) pelo Whats 2000",
                  "status": "concluido",
                  "metadata": {"disparo_key": disparo_key,
                               "lead_id": ld["id"],
                               "campanha": f"aniver-{tag}"}},
            timeout=30)

        # anti-bloqueio: 90-150s entre mensagens (instancia compartilhada
        # com as campanhas win-back)
        time.sleep(90 + random.uniform(0, 60))

    print(f"\nResumo: {enviados} enviado(s), {pulados} ja enviados antes, "
          f"{len(sem_fone)} sem telefone")
    if sem_fone:
        print("Sem telefone:", "; ".join(sem_fone))

    if not dry and not test_to and (enviados or sem_fone):
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": f"Campanha aniversariantes inativos {tag} "
                           "(run diario)",
                  "detail": f"{enviados} arte(s) enviada(s) no WhatsApp, "
                            f"{pulados} ja tinham recebido, "
                            f"{len(sem_fone)} sem telefone.",
                  "status": "concluido"},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
