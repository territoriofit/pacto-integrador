# -*- coding: utf-8 -*-
"""
Disparo das pesquisas de follow-up de alunos novos (3 e 10 dias) por WhatsApp.

Fluxo diário (GitHub Actions, horários alternados por dia da semana):
  1. Busca em vendas_pacto os contratos tipo MA/RE (matrícula/rematrícula)
     com data_venda na janela de 3-5 dias atrás (pesquisa 3d) e 10-12 dias
     atrás (pesquisa 10d). A janela cobre runs perdidos; o dedup garante 1
     envio por contrato.
  2. Resolve o telefone do aluno pela tabela leads (match exato de nome,
     case-insensitive). Telefone do Pacto vem sem 55 — prefixa.
  3. Dedup por linha em agent_activity (metadata.disparo_key = "<contrato>-d3").
  4. Cria 1 linha em nps_envios (token único → link personalizado do NPS
     PRÓPRIO em crm.territoriofit.com.br/pesquisa/<token>; desde 05/08/2026
     substitui os links fixos da WeHelp — resposta entra IDENTIFICADA) e
     envia pela instância UazAPI "Whats TF 2000" com intervalo de 60s +
     jitter (boas práticas anti-bloqueio), registrando cada envio no feed
     da Equipe Digital do CRM.

Janela de envio: 09:00-19:30 BRT (o scheduler do Actions atrasa; antes das
9h o job espera, depois das 19h30 aborta e a janela de datas cobre no dia
seguinte).

Env: SUPABASE_KEY (service role), UAZAPI_TOKEN_2000.
Opcional: DRY_RUN=1 (não envia, só lista) | TEST_TO=5516... (envia tudo só
para esse número, sem gravar dedup).
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

CAMPANHAS = [
    {
        "dia": 3,
        "janela_dias": (3, 5),      # data_venda entre hoje-5 e hoje-3
        "pesquisa": "3d",
        "msg": (
            "Oi, {nome}! Aqui é da Território Fit 💛\n\n"
            "Você acabou de completar seus primeiros dias com a gente e "
            "queremos saber: como está sendo até agora?\n\n"
            "Responde essa pesquisinha rápida (leva menos de 1 minuto, "
            "prometo 😄) — sua opinião ajuda a gente a cuidar ainda melhor "
            "de você:\n\n👉 {link}\n\nBons treinos! 💪"
        ),
    },
    {
        "dia": 10,
        "janela_dias": (10, 12),    # data_venda entre hoje-12 e hoje-10
        "pesquisa": "10d",
        "msg": (
            "Oi, {nome}! Território Fit por aqui 💛\n\n"
            "Já são quase 2 semanas de treino com a gente — que demais! 🎉\n\n"
            "Conta pra gente como está sendo encaixar os treinos na rotina? "
            "É rapidinho, menos de 1 minuto:\n\n👉 {link}\n\n"
            "Qualquer coisa que precisar, é só chamar por aqui. "
            "Bons treinos! 💪"
        ),
    },
]


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


def criar_envio(sb: dict, pesquisa: str, nome: str, fone: str,
                lead_id: str | None, canal: str, contrato) -> tuple[str, str] | None:
    """Cria a linha em nps_envios; retorna (envio_id, link) ou None."""
    token = secrets.token_urlsafe(8)
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/nps_envios",
        headers={**sb, "Prefer": "return=representation"},
        json={"token": token, "person_name": nome, "phone": fone,
              "lead_id": lead_id, "pesquisa": pesquisa, "canal": canal,
              "metadata": {"codigo_contrato": contrato}},
        timeout=30)
    if r.status_code not in (200, 201):
        print(f"[envio] falha ao criar envio de {nome}: "
              f"HTTP {r.status_code} {r.text[:200]}")
        return None
    return r.json()[0]["id"], PESQUISA_BASE_URL + token


def aguardar_janela_comercial() -> bool:
    """True se pode enviar agora (espera até 9h se cedo; False se tarde)."""
    agora = datetime.now(TZ_SP)
    if agora.hour >= 20 or (agora.hour == 19 and agora.minute > 30):
        print(f"[janela] {agora:%H:%M} BRT — tarde demais, abortando "
              "(a janela de datas cobre amanhã).")
        return False
    while agora.hour < 9:
        espera = min(1800, (9 - agora.hour) * 3600 - agora.minute * 60)
        print(f"[janela] {agora:%H:%M} BRT — aguardando {espera//60} min...")
        time.sleep(espera)
        agora = datetime.now(TZ_SP)
    return True


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_2000", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    test_to = os.environ.get("TEST_TO", "").strip()
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_2000")
        return 1

    if not dry and not aguardar_janela_comercial():
        return 0

    # horario alternado (pedido Andre 04/08): atraso aleatorio de ate
    # JITTER_MAX_MIN (padrao 40min) pra nao disparar sempre no mesmo
    # horario todo dia — padrao anti-bloqueio do WhatsApp
    jitter_max = int(os.environ.get("JITTER_MAX_MIN", "40"))
    if not dry and not test_to and jitter_max > 0:
        atraso = random.uniform(0, jitter_max * 60)
        print(f"[jitter] variando horario do disparo: +{int(atraso // 60)}min")
        time.sleep(atraso)

    sb = _sb_headers(key)
    hoje = datetime.now(TZ_SP).date()
    enviados, pulados, sem_fone = 0, 0, []

    for camp in CAMPANHAS:
        d_min, d_max = camp["janela_dias"]
        de = (hoje - timedelta(days=d_max)).isoformat()
        ate = (hoje - timedelta(days=d_min)).isoformat()
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/vendas_pacto",
            params=[("select", "codigo_contrato,nome_cliente,data_venda,tipo"),
                    ("tipo", "in.(MA,RE)"),
                    ("data_venda", f"gte.{de}"),
                    ("data_venda", f"lte.{ate}"),
                    ("order", "data_venda")],
            headers=sb, timeout=30)
        r.raise_for_status()
        vendas = r.json()
        print(f"[d{camp['dia']}] janela {de}..{ate}: {len(vendas)} contrato(s)")

        for v in vendas:
            contrato = v["codigo_contrato"]
            nome = v.get("nome_cliente") or ""
            disparo_key = f"{contrato}-d{camp['dia']}"

            # dedup
            rj = requests.get(
                f"{SUPABASE_URL}/rest/v1/agent_activity",
                params={"select": "id",
                        "metadata->>disparo_key": f"eq.{disparo_key}",
                        "limit": "1"},
                headers=sb, timeout=30).json()
            if rj:
                pulados += 1
                continue

            # telefone via leads (match exato case-insensitive)
            rl = requests.get(
                f"{SUPABASE_URL}/rest/v1/leads",
                params={"select": "id,phone,name",
                        "tenant_id": f"eq.{TENANT_ID}",
                        "name": f"ilike.{nome}",
                        "limit": "2"},
                headers=sb, timeout=30).json()
            fone = _fone_55(rl[0].get("phone")) if len(rl) >= 1 else None
            if not fone:
                sem_fone.append(f"{nome} ({v['data_venda']})")
                continue
            lead_id = rl[0].get("id")

            if dry:
                print(f"[DRY] d{camp['dia']} -> {nome} ({fone})")
                enviados += 1
                continue

            # link próprio personalizado (nps_envios; substitui a WeHelp)
            canal = "teste" if test_to else "whatsapp"
            criado = criar_envio(sb, camp["pesquisa"], nome, fone,
                                 lead_id, canal, contrato)
            if not criado:
                continue
            envio_id, link = criado

            destino = test_to or fone
            texto = camp["msg"].format(nome=_primeiro_nome(nome), link=link)
            resp = requests.post(
                f"{UAZAPI_URL}/send/text",
                headers={"token": zap, "Content-Type": "application/json"},
                json={"number": destino, "text": texto}, timeout=60)
            ok = resp.status_code == 200
            print(f"[send] d{camp['dia']} {nome} -> {destino[-4:]} "
                  f"HTTP {resp.status_code}")
            if not ok:
                print("       resp:", resp.text[:200])
                # envio não saiu — remove a linha pra não poluir métricas
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
                          "title": f"Pesquisa {camp['dia']}d enviada no "
                                   "WhatsApp",
                          "detail": f"{nome.title()} — matrícula "
                                    f"{v['data_venda']} — link da pesquisa "
                                    f"de {camp['dia']} dias pelo Whats 2000",
                          "status": "concluido",
                          "metadata": {"disparo_key": disparo_key,
                                       "codigo_contrato": contrato,
                                       "dia": camp["dia"]}},
                    timeout=30)

            # boas práticas: 60s + jitter entre mensagens
            time.sleep(90 + random.uniform(0, 60))

    print(f"\nResumo: {enviados} enviado(s), {pulados} já enviados antes, "
          f"{len(sem_fone)} sem telefone")
    if sem_fone:
        print("Sem telefone:", "; ".join(sem_fone))

    # resumo do run no feed (só quando algo aconteceu de verdade)
    if not dry and not test_to and (enviados or sem_fone):
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": "Follow-up pesquisas alunos novos (run diário)",
                  "detail": f"{enviados} pesquisa(s) enviada(s) no WhatsApp "
                            f"(3d/10d), {pulados} deduplicada(s), "
                            f"{len(sem_fone)} sem telefone.",
                  "status": "concluido"},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
