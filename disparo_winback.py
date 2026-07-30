# -*- coding: utf-8 -*-
"""
Campanha win-back: ex-alunos que NAO renovaram o plano em abril/2026.

Fluxo diario (GitHub Actions, ate esgotar a lista):
  1. Busca no BI do Pacto (/v2-indice-renovacao, reloadFull) os contratos com
     previsao de renovacao em abril/2026 que NAO renovaram (com tolerancia) e
     cujo cliente segue INATIVO hoje — quem voltou por outro caminho sai da
     lista automaticamente a cada run.
  2. Resolve o telefone pelo cadastro ADM (/v1/cliente/{codigo}).
  3. Dedup por linha em agent_activity (metadata.disparo_key =
     "winback-abril26-<codigoCliente>") — 1 envio por pessoa, pra sempre.
  4. Envia a ARTE da campanha (repo publico, raw.githubusercontent) com a
     copy como legenda pela instancia UazAPI "Whats TF 2000", em lotes de
     ate MAX_POR_RUN por dia (padrao 35), com 60s + jitter entre envios.

Quem responder "VOLTEI" cai na consultora (o desconto e passado por ela;
nao vai valor na mensagem). Ex-alunos ja existem como lead no CRM, entao o
fallback da Clara (so contato NOVO) nao intercepta.

Janela de envio: 09:00-19:30 BRT.

Env: SUPABASE_KEY, UAZAPI_TOKEN_2000, PACTO_TOKEN, PACTO_CHAVE.
Opcional: DRY_RUN=1 (so lista) | TEST_TO=5516... (envia 1 exemplo pro numero,
sem gravar dedup) | MAX_POR_RUN=n (padrao 35).
"""

import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"
PACTO_GW = "https://apigw.pactosolucoes.com.br"

TZ_SP = timezone(timedelta(hours=-3))

# abril/2026 em epoch ms UTC (mesmo filtro validado na sessao 2026-07-30)
ABRIL_INI = 1775001600000   # 2026-04-01 00:00:00 UTC
ABRIL_FIM = 1777507199000   # 2026-04-30 23:59:59 UTC

ARTE_URL = ("https://raw.githubusercontent.com/territoriofit/"
            "pacto-integrador/main/assets/winback_exalunos_abril.png")

COPY = (
    "Oi, {nome}! Aqui é da Território Fit 💛\n\n"
    "Seu lugar aqui continua guardado — e a gente preparou uma condição "
    "especial só pra quem já fez parte do nosso território:\n\n"
    "🎁 Voltando a treinar, você ganha *NO ATO* a garrafa exclusiva "
    "Território Fit\n"
    "💥 E ainda leva um *SUPER DESCONTO* no seu plano de volta\n\n"
    "Responde *VOLTEI* que nossa consultora te conta tudo 😉"
)


def _sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _gw_headers() -> dict:
    return {"Authorization": os.environ["PACTO_TOKEN"].strip(),
            "chave": os.environ["PACTO_CHAVE"].strip(),
            "empresaId": "1", "Content-Type": "application/json",
            "Accept-Language": "pt-BR"}


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


def aguardar_janela_comercial() -> bool:
    agora = datetime.now(TZ_SP)
    if agora.hour >= 20 or (agora.hour == 19 and agora.minute > 30):
        print(f"[janela] {agora:%H:%M} BRT — tarde demais, abortando.")
        return False
    while agora.hour < 9:
        espera = min(1800, (9 - agora.hour) * 3600 - agora.minute * 60)
        print(f"[janela] {agora:%H:%M} BRT — aguardando {espera//60} min...")
        time.sleep(espera)
        agora = datetime.now(TZ_SP)
    return True


def buscar_nao_renovados() -> list[dict]:
    """Contratos de abril/2026 nao renovados cujo cliente segue inativo."""
    import json as _json
    body = {"empresa": 1, "dataInicial": ABRIL_INI, "dataFinal": ABRIL_FIM,
            "retornarContratos": True,
            "desconsiderarContratosRenovaveis": False,
            "considerarMudancaDePlano": False}
    r = requests.post(f"{PACTO_GW}/v2-indice-renovacao",
                      params={"reloadFull": "true"},
                      headers=_gw_headers(), json=body, timeout=180)
    r.raise_for_status()
    content = r.json().get("content", {})
    jd = content.get("jsonDados", "{}")
    data = _json.loads(jd) if isinstance(jd, str) else jd
    lista = data.get("contratosNaoRenovadosToleranciaPrevisaoMes", [])
    inativos = [c for c in lista if c.get("situacaoCliente") == "IN"]
    print(f"[pacto] abril/26: {len(lista)} nao renovados, "
          f"{len(inativos)} ainda inativos")
    return inativos


def telefone_cliente(codigo: int) -> str | None:
    try:
        r = requests.get(f"{PACTO_GW}/v1/cliente/{codigo}",
                         headers=_gw_headers(), timeout=30)
        if r.status_code != 200:
            return None
        cli = r.json().get("content", {})
        pessoa = cli.get("pessoa") or {}
        for t in (pessoa.get("telefones") or cli.get("telefones") or []):
            fone = _fone_55(str(t.get("numero") or t.get("telefone") or ""))
            if fone:
                return fone
    except Exception as e:
        print(f"  [tel] erro cliente {codigo}: {e}")
    return None


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_2000", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    test_to = os.environ.get("TEST_TO", "").strip()
    max_por_run = int(os.environ.get("MAX_POR_RUN", "35"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1
    if not os.environ.get("PACTO_TOKEN") or not os.environ.get("PACTO_CHAVE"):
        print("Faltam envs PACTO_TOKEN / PACTO_CHAVE")
        return 1

    if not dry and not aguardar_janela_comercial():
        return 0

    sb = _sb_headers(key)
    alvos = buscar_nao_renovados()
    enviados, pulados, sem_fone = 0, 0, []

    for c in alvos:
        if enviados >= max_por_run:
            print(f"[lote] limite de {max_por_run} atingido — "
                  "o resto vai no proximo run.")
            break

        cod = c["codigoCliente"]
        nome = c.get("nomeCliente") or ""
        disparo_key = f"winback-abril26-{cod}"

        rj = requests.get(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            params={"select": "id",
                    "metadata->>disparo_key": f"eq.{disparo_key}",
                    "limit": "1"},
            headers=sb, timeout=30).json()
        if rj:
            pulados += 1
            continue

        fone = telefone_cliente(cod)
        if not fone:
            sem_fone.append(nome)
            continue

        destino = test_to or fone
        legenda = COPY.format(nome=_primeiro_nome(nome))
        if dry:
            print(f"[DRY] {nome} ({fone})")
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
                  "title": "Win-back abril enviado no WhatsApp",
                  "detail": f"{nome.title()} — campanha ex-alunos nao "
                            "renovados de abril (garrafa + super desconto) "
                            "pelo Whats 2000",
                  "status": "concluido",
                  "metadata": {"disparo_key": disparo_key,
                               "codigo_cliente": cod,
                               "campanha": "winback-abril26"}},
            timeout=30)

        time.sleep(60 + random.uniform(0, 30))

    print(f"\nResumo: {enviados} enviado(s), {pulados} ja enviados antes, "
          f"{len(sem_fone)} sem telefone")
    if sem_fone:
        print("Sem telefone:", "; ".join(sem_fone))

    if not dry and not test_to and (enviados or sem_fone):
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": "Campanha win-back abril (run diario)",
                  "detail": f"{enviados} arte(s) enviada(s) no WhatsApp, "
                            f"{pulados} ja tinham recebido, "
                            f"{len(sem_fone)} sem telefone.",
                  "status": "concluido"},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
