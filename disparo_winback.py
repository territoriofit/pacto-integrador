# -*- coding: utf-8 -*-
"""
Campanha win-back: ex-alunos que NAO renovaram o plano no mes de referencia
(env CAMPANHA_MES, formato YYYY-MM; padrao 2026-04 = campanha de abril).

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
Opcional: CAMPANHA_MES=YYYY-MM (padrao 2026-04) | HORA_INICIO=h (inicio da
janela BRT, padrao 9) | DRY_RUN=1 (so lista) | TEST_TO=5516... (envia 1
exemplo pro numero, sem gravar dedup) | MAX_POR_RUN=n (padrao 35).
"""

import calendar
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

MESES_PT = {1: "janeiro", 2: "fevereiro", 3: "marco", 4: "abril",
            5: "maio", 6: "junho", 7: "julho", 8: "agosto",
            9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"}


def _campanha():
    """Mes de referencia via env CAMPANHA_MES (YYYY-MM, padrao 2026-04).

    Retorna (tag, label, ini_ms, fim_ms). tag "abril26" mantem compat com
    os dedup keys ja gravados da 1a campanha.
    """
    ref = os.environ.get("CAMPANHA_MES", "2026-04").strip()
    ano, mes = int(ref[:4]), int(ref[5:7])
    nome = MESES_PT[mes]
    tag = f"{nome}{ano % 100}"
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    ini = int(datetime(ano, mes, 1, tzinfo=timezone.utc).timestamp() * 1000)
    fim = int(datetime(ano, mes, ultimo_dia, 23, 59, 59,
                       tzinfo=timezone.utc).timestamp() * 1000)
    return tag, f"{nome}/{ano}", ini, fim

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


def buscar_nao_renovados(label: str, ini_ms: int, fim_ms: int) -> list[dict]:
    """Contratos do mes nao renovados cujo cliente segue inativo."""
    import json as _json
    body = {"empresa": 1, "dataInicial": ini_ms, "dataFinal": fim_ms,
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
    print(f"[pacto] {label}: {len(lista)} nao renovados, "
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
    hora_inicio = int(os.environ.get("HORA_INICIO", "9"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1
    if not os.environ.get("PACTO_TOKEN") or not os.environ.get("PACTO_CHAVE"):
        print("Faltam envs PACTO_TOKEN / PACTO_CHAVE")
        return 1

    tag, label, ini_ms, fim_ms = _campanha()
    print(f"[campanha] winback-{tag} ({label})")

    if not dry and not aguardar_janela_comercial(hora_inicio):
        return 0

    sb = _sb_headers(key)
    alvos = buscar_nao_renovados(label, ini_ms, fim_ms)
    enviados, pulados, sem_fone = 0, 0, []

    for c in alvos:
        if enviados >= max_por_run:
            print(f"[lote] limite de {max_por_run} atingido — "
                  "o resto vai no proximo run.")
            break

        cod = c["codigoCliente"]
        nome = c.get("nomeCliente") or ""
        disparo_key = f"winback-{tag}-{cod}"

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
                  "title": f"Win-back {label} enviado no WhatsApp",
                  "detail": f"{nome.title()} — campanha ex-alunos nao "
                            f"renovados de {label} (garrafa + super "
                            "desconto) pelo Whats 2000",
                  "status": "concluido",
                  "metadata": {"disparo_key": disparo_key,
                               "codigo_cliente": cod,
                               "campanha": f"winback-{tag}"}},
            timeout=30)

        # anti-bloqueio: 90-150s entre mensagens (2 campanhas no mesmo
        # numero a partir de 31/07 — espacamento maior protege a instancia)
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
                  "title": f"Campanha win-back {label} (run diario)",
                  "detail": f"{enviados} arte(s) enviada(s) no WhatsApp, "
                            f"{pulados} ja tinham recebido, "
                            f"{len(sem_fone)} sem telefone.",
                  "status": "concluido"},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
