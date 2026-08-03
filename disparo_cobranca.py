# -*- coding: utf-8 -*-
"""
Regua de cobranca: alunos com parcela recusada na 2a tentativa de cobranca
no cartao (parcelas_atrasadas.nr_tentativas >= 2) recebem ate 4 mensagens
amigaveis, 1 por dia, em dias consecutivos.

Fluxo diario (GitHub Actions):
  1. Busca em parcelas_atrasadas (sync diario do Pacto) as parcelas com
     nr_tentativas >= 2 e dias_atraso <= MAX_DIAS_ATRASO (padrao 30) —
     1 regua por aluno, ancorada na parcela mais antiga em aberto.
  2. Estado da regua em agent_activity (metadata.disparo_key =
     "cobranca-<parcela_codigo>-p1..p4"): envia o proximo passo se o
     anterior foi em dia anterior (cadencia diaria).
  3. TRAVA DA CONSULTORA: a partir do p2, se houver mensagem HUMANA enviada
     ao aluno depois do p1 (consultora falou com ele — CRM grava sent_by
     'human'; celular fica sem sent_by; Clara e disparos = 'ai_agent'),
     a regua PAUSA e nao envia mais nada pra esse aluno.
  4. Pagou = parcela some de parcelas_atrasadas no sync seguinte e a regua
     morre sozinha. Envio pela instancia UazAPI "Whats TF 2000", texto puro,
     90-150s entre envios, janela 09:00-19:30 BRT.

Quem responder cai na consultora no inbox (aluno ja e lead; o fallback da
Clara so pega contato novo).

Env: SUPABASE_KEY, UAZAPI_TOKEN_2000.
Opcional: MAX_DIAS_ATRASO=n (padrao 30) | HORA_INICIO=h (padrao 9) |
DRY_RUN=1 (so lista) | TEST_TO=5516... (envia 1 exemplo do p1, sem gravar
dedup) | MAX_POR_RUN=n (padrao 30).
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

MSGS = {
    1: ("Oi, {nome}, tudo bem? Aqui é da Território Fit 💛\n\n"
        "Tentamos processar a sua mensalidade de *R$ {valor}* no cartão e a "
        "operadora não aprovou — deve ter sido só um probleminha na cobrança "
        "(limite do dia, cartão novo, essas coisas, acontece! 😅)\n\n"
        "Me responde aqui que a gente resolve rapidinho, tá?"),
    2: ("Oi, {nome}! Passando só pra lembrar: a operadora do cartão ainda "
        "não aprovou a sua mensalidade de *R$ {valor}* 😕\n\n"
        "Se for mais fácil, dá pra acertar por *PIX* ou atualizar o cartão "
        "— me responde aqui que nossa consultora te ajuda na hora 💛"),
    3: ("{nome}, tudo certo por aí? 😊\n\n"
        "Sua mensalidade segue pendente e a gente não quer que isso "
        "atrapalhe seus treinos. Responde essa mensagem que encontramos "
        "juntos a melhor forma de acertar, combinado?"),
    4: ("Oi, {nome}! 💛 Pra sua mensalidade não acumular com a próxima e "
        "seu acesso continuar liberado na catraca, bora acertar hoje?\n\n"
        "É rapidinho: responde aqui que nossa consultora resolve com você "
        "em poucos minutos 😉"),
}


def _sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _primeiro_nome(nome: str) -> str:
    p = (nome or "").strip().split()
    return p[0].title() if p else "tudo bem"


def _valor_br(v) -> str:
    try:
        return f"{float(v):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return "--"


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


def buscar_alvos(sb: dict, max_dias: int) -> list[dict]:
    """1 regua por aluno: parcela mais antiga com nr_tentativas>=2."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/parcelas_atrasadas",
        params={"select": "lead_id,nome_aluno,parcela_codigo,valor,"
                          "data_vencimento,dias_atraso,nr_tentativas",
                "nr_tentativas": "gte.2",
                "dias_atraso": f"lte.{max_dias}",
                "order": "data_vencimento.asc",
                "limit": "1000"},
        headers=sb, timeout=60)
    r.raise_for_status()
    por_lead: dict[str, dict] = {}
    for p in r.json():
        por_lead.setdefault(p["lead_id"], p)  # 1a = mais antiga (order asc)
    print(f"[crm] {len(por_lead)} aluno(s) com parcela recusada 2x "
          f"(atraso ate {max_dias}d)")
    return list(por_lead.values())


def telefones(sb: dict, lead_ids: list[str]) -> dict[str, str]:
    fones: dict[str, str] = {}
    for i in range(0, len(lead_ids), 80):
        lote = ",".join(lead_ids[i:i + 80])
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/leads",
            params={"select": "id,phone", "id": f"in.({lote})"},
            headers=sb, timeout=30)
        for row in r.json():
            fones[row["id"]] = row.get("phone") or ""
    return fones


def estado_regua(sb: dict) -> dict[str, dict[int, str]]:
    """parcela_codigo -> {passo: created_at} a partir do agent_activity."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/agent_activity",
        params={"select": "created_at,metadata",
                "metadata->>campanha": "eq.cobranca-regua",
                "order": "created_at.desc", "limit": "2000"},
        headers=sb, timeout=30)
    estado: dict[str, dict[int, str]] = {}
    for row in r.json():
        m = row.get("metadata") or {}
        cod, passo = m.get("parcela_codigo"), m.get("passo")
        if cod is None or passo is None:
            continue
        estado.setdefault(str(cod), {})[int(passo)] = row["created_at"]
    return estado


def consultora_assumiu(sb: dict, lead_id: str, desde_iso: str) -> bool:
    """Msg humana (CRM sent_by='human' ou celular sem sent_by) apos o p1."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/whatsapp_messages",
        params={"select": "id",
                "lead_id": f"eq.{lead_id}",
                "is_from_me": "eq.true",
                "group_id": "is.null",
                "sent_at": f"gte.{desde_iso}",
                "or": "(metadata->>sent_by.is.null,"
                      "metadata->>sent_by.neq.ai_agent)",
                "limit": "1"},
        headers=sb, timeout=30)
    return bool(r.json())


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_2000", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    test_to = os.environ.get("TEST_TO", "").strip()
    max_por_run = int(os.environ.get("MAX_POR_RUN", "30"))
    hora_inicio = int(os.environ.get("HORA_INICIO", "9"))
    max_dias = int(os.environ.get("MAX_DIAS_ATRASO", "30"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1

    print("[campanha] regua de cobranca (parcelas recusadas 2x)")
    if not dry and not test_to and not aguardar_janela_comercial(hora_inicio):
        return 0

    sb = _sb_headers(key)
    alvos = buscar_alvos(sb, max_dias)
    if not alvos:
        print("Nenhum alvo hoje.")
        return 0
    fones = telefones(sb, [a["lead_id"] for a in alvos])
    estado = estado_regua(sb)
    hoje = datetime.now(TZ_SP).date().isoformat()

    enviados, pausados, em_dia, concluidos, sem_fone = 0, 0, 0, 0, []

    for a in alvos:
        if enviados >= max_por_run:
            print(f"[lote] limite de {max_por_run} atingido.")
            break

        cod = str(a["parcela_codigo"])
        nome = a.get("nome_aluno") or ""
        passos = estado.get(cod, {})
        ultimo = max(passos) if passos else 0

        if ultimo >= 4:
            concluidos += 1
            continue
        if ultimo and passos[ultimo][:10] >= hoje:
            em_dia += 1  # passo de hoje ja foi
            continue

        # trava da consultora: humano falou com o aluno depois do p1?
        if ultimo >= 1 and consultora_assumiu(sb, a["lead_id"], passos[1]):
            print(f"[pausa] {nome} — consultora ja falou com o aluno, "
                  "regua pausada.")
            pausados += 1
            continue

        fone = _fone_55(fones.get(a["lead_id"], ""))
        if not fone:
            sem_fone.append(nome)
            continue

        passo = ultimo + 1
        texto = MSGS[passo].format(nome=_primeiro_nome(nome),
                                   valor=_valor_br(a.get("valor")))
        if dry:
            print(f"[DRY] p{passo} — {nome} ({fone}) "
                  f"R$ {_valor_br(a.get('valor'))} "
                  f"venc {a.get('data_vencimento')} "
                  f"{a.get('dias_atraso')}d {a.get('nr_tentativas')}x")
            enviados += 1
            continue

        destino = test_to or fone
        resp = requests.post(
            f"{UAZAPI_URL}/send/text",
            headers={"token": zap, "Content-Type": "application/json"},
            json={"number": destino, "text": texto}, timeout=120)
        ok = resp.status_code == 200
        print(f"[send] p{passo} {nome} -> ...{destino[-4:]} "
              f"HTTP {resp.status_code}")
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
                  "title": f"Cobranca amigavel p{passo}/4 enviada",
                  "detail": f"{nome.title()} — parcela R$ "
                            f"{_valor_br(a.get('valor'))} recusada "
                            f"{a.get('nr_tentativas')}x no cartao "
                            f"(venc. {a.get('data_vencimento')}) "
                            "pelo Whats 2000",
                  "status": "concluido",
                  "metadata": {"disparo_key": f"cobranca-{cod}-p{passo}",
                               "campanha": "cobranca-regua",
                               "parcela_codigo": cod,
                               "passo": passo,
                               "lead_id": a["lead_id"]}},
            timeout=30)

        # anti-bloqueio: 90-150s (instancia compartilhada com campanhas)
        time.sleep(90 + random.uniform(0, 60))

    print(f"\nResumo: {enviados} enviado(s), {em_dia} ja em dia, "
          f"{pausados} pausado(s) pela consultora, {concluidos} regua "
          f"concluida, {len(sem_fone)} sem telefone")
    if sem_fone:
        print("Sem telefone:", "; ".join(sem_fone))

    if not dry and not test_to and enviados:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": "Regua de cobranca (run diario)",
                  "detail": f"{enviados} msg(s) enviadas, {pausados} "
                            f"pausadas pela consultora, {concluidos} "
                            "reguas concluidas.",
                  "status": "concluido"},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
