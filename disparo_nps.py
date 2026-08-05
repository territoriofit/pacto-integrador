# -*- coding: utf-8 -*-
"""
Disparo do NPS próprio da Território Fit por WhatsApp (substitui o NPS
diário por e-mail da WeHelp, que convertia só ~2,8%).

Fluxo diário (GitHub Actions):
  1. Base: leads status cliente/inadimplente com acesso à catraca nos últimos
     ACESSO_DIAS dias (metadata.ultimo_acesso, sincronizado diariamente pelo
     sync_ultimo_acesso) — equivalente ao gatilho ACESSO_CATRACA da WeHelp.
  2. Exclusões (cooldowns, tudo via nps_envios — fonte da verdade):
       - recebeu QUALQUER pesquisa nossa nos últimos COOLDOWN_QUALQUER dias
         (protege alunos novos que acabaram de receber a 3d/10d);
       - recebeu NPS nos últimos COOLDOWN_NPS dias;
       - respondeu NPS nos últimos COOLDOWN_RESPONDEU dias.
  3. Cria 1 linha em nps_envios por aluno (token único → link personalizado
     https://crm.territoriofit.com.br/pesquisa/<token>) e envia pela
     instância UazAPI "Whats TF 2000". A resposta entra IDENTIFICADA no
     dashboard da aba Pesquisa (edge function nps-pesquisa).
  4. Máximo NPS_MAX_DIA envios/dia, jitter de início + 90-150s entre
     mensagens (padrão anti-bloqueio das campanhas).

Env: SUPABASE_KEY (service role), UAZAPI_TOKEN_2000.
Opcional: DRY_RUN=1 | TEST_TO=5516... (envia só pra esse número, envio criado
com canal='teste') | NPS_MAX_DIA (padrão 25) | ACESSO_DIAS (padrão 7).
"""

import os
import random
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
TENANT_ID = "4eeff494-6528-4f49-8a5c-2742eabb8c2c"
UAZAPI_URL = "https://territoriofit.uazapi.com"
PESQUISA_BASE_URL = "https://crm.territoriofit.com.br/pesquisa/"

TZ_SP = timezone(timedelta(hours=-3))

COOLDOWN_QUALQUER = 45   # dias sem receber nenhuma pesquisa nossa
COOLDOWN_NPS = 90        # dias sem receber o NPS
COOLDOWN_RESPONDEU = 180 # dias sem ser incomodado depois de responder

MSG_NPS = (
    "Oi, {nome}! Aqui é da Território Fit 💛\n\n"
    "A gente quer muito saber: como está sendo sua experiência na "
    "Território?\n\n"
    "Responde essa pesquisinha rápida (leva menos de 1 minuto, prometo 😄) — "
    "sua opinião vale ouro pra gente melhorar sempre:\n\n"
    "👉 {link}\n\nBons treinos! 💪"
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


def criar_envio(sb: dict, nome: str, fone: str, lead_id: str | None,
                canal: str) -> tuple[str, str] | None:
    """Cria a linha em nps_envios; retorna (envio_id, link) ou None."""
    token = secrets.token_urlsafe(8)
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/nps_envios",
        headers={**sb, "Prefer": "return=representation"},
        json={"token": token, "person_name": nome, "phone": fone,
              "lead_id": lead_id, "pesquisa": "nps", "canal": canal},
        timeout=30)
    if r.status_code not in (200, 201):
        print(f"[envio] falha ao criar envio de {nome}: "
              f"HTTP {r.status_code} {r.text[:200]}")
        return None
    return r.json()[0]["id"], PESQUISA_BASE_URL + token


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_2000", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    test_to = os.environ.get("TEST_TO", "").strip()
    max_dia = int(os.environ.get("NPS_MAX_DIA", "25"))
    acesso_dias = int(os.environ.get("ACESSO_DIAS", "7"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1

    if not dry and not aguardar_janela_comercial():
        return 0

    jitter_max = int(os.environ.get("JITTER_MAX_MIN", "40"))
    if not dry and not test_to and jitter_max > 0:
        atraso = random.uniform(0, jitter_max * 60)
        print(f"[jitter] variando horario do disparo: +{int(atraso // 60)}min")
        time.sleep(atraso)

    sb = _sb_headers(key)
    hoje = datetime.now(TZ_SP).date()

    # 1) base: alunos ativos com acesso recente à catraca
    # (paginado — o PostgREST corta em 1000 linhas por request)
    corte_acesso = (hoje - timedelta(days=acesso_dias)).isoformat()
    base, offset = [], 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/leads",
            params={"select": "id,name,phone,metadata->>ultimo_acesso",
                    "tenant_id": f"eq.{TENANT_ID}",
                    "status": "in.(cliente,inadimplente)",
                    "metadata->>ultimo_acesso": f"gte.{corte_acesso}",
                    "order": "id",
                    "limit": "1000", "offset": str(offset)},
            headers=sb, timeout=60)
        r.raise_for_status()
        pagina = r.json()
        base.extend(pagina)
        if len(pagina) < 1000:
            break
        offset += 1000
    print(f"[base] {len(base)} aluno(s) com acesso desde {corte_acesso}")

    # 2) cooldowns via nps_envios (janela mais longa cobre todas as regras)
    corte_hist = (hoje - timedelta(days=COOLDOWN_RESPONDEU)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/nps_envios",
        params={"select": "lead_id,phone,pesquisa,enviado_em,respondido_em",
                "enviado_em": f"gte.{corte_hist}",
                "limit": "10000"},
        headers=sb, timeout=60)
    r.raise_for_status()
    historico = r.json()

    def _dias(iso: str) -> int:
        return (hoje - datetime.fromisoformat(iso[:10]).date()).days

    excluidos: set[str] = set()
    for e in historico:
        chaves = {c for c in (e.get("lead_id"), e.get("phone")) if c}
        d_envio = _dias(e["enviado_em"])
        if d_envio <= COOLDOWN_QUALQUER:
            excluidos |= chaves
        if e["pesquisa"] == "nps" and d_envio <= COOLDOWN_NPS:
            excluidos |= chaves
        if e["pesquisa"] == "nps" and e.get("respondido_em") \
                and _dias(e["respondido_em"]) <= COOLDOWN_RESPONDEU:
            excluidos |= chaves

    candidatos = []
    fones_vistos: set[str] = set()
    for lead in base:
        fone = _fone_55(lead.get("phone"))
        if not fone or fone in fones_vistos:
            continue
        if lead["id"] in excluidos or fone in excluidos:
            continue
        fones_vistos.add(fone)
        candidatos.append({**lead, "fone": fone})

    random.shuffle(candidatos)  # justiça: sem viés de ordem alfabética
    selecao = candidatos[:max_dia]
    print(f"[selecao] {len(candidatos)} elegíveis após cooldowns; "
          f"enviando {len(selecao)} (máx {max_dia}/dia)")

    enviados = 0
    for lead in selecao:
        nome = lead.get("name") or ""
        if dry:
            print(f"[DRY] NPS -> {nome} ({lead['fone']})")
            enviados += 1
            continue

        canal = "teste" if test_to else "whatsapp"
        criado = criar_envio(sb, nome, lead["fone"], lead["id"], canal)
        if not criado:
            continue
        envio_id, link = criado

        destino = test_to or lead["fone"]
        texto = MSG_NPS.format(nome=_primeiro_nome(nome), link=link)
        resp = requests.post(
            f"{UAZAPI_URL}/send/text",
            headers={"token": zap, "Content-Type": "application/json"},
            json={"number": destino, "text": texto}, timeout=60)
        print(f"[send] NPS {nome} -> {destino[-4:]} HTTP {resp.status_code}")
        if resp.status_code != 200:
            print("       resp:", resp.text[:200])
            # envio não saiu — remove a linha pra não travar cooldown à toa
            requests.delete(f"{SUPABASE_URL}/rest/v1/nps_envios",
                            params={"id": f"eq.{envio_id}"},
                            headers=sb, timeout=30)
            continue

        enviados += 1
        if not test_to:
            requests.post(
                f"{SUPABASE_URL}/rest/v1/agent_activity",
                headers={**sb, "Prefer": "return=minimal"},
                json={"agent_slug": "crm-relacionamento",
                      "title": "Pesquisa NPS enviada no WhatsApp",
                      "detail": f"{nome.title()} — NPS próprio com link "
                                "personalizado pelo Whats 2000",
                      "status": "concluido",
                      "metadata": {"envio_id": envio_id, "pesquisa": "nps"}},
                timeout=30)

        time.sleep(90 + random.uniform(0, 60))

    print(f"\nResumo: {enviados} enviado(s) de {len(selecao)} selecionado(s)")
    if not dry and not test_to and enviados:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": "NPS próprio (run diário)",
                  "detail": f"{enviados} pesquisa(s) NPS enviada(s) no "
                            f"WhatsApp com link personalizado "
                            f"({len(candidatos)} elegíveis na base).",
                  "status": "concluido"},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
