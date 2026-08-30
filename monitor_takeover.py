# -*- coding: utf-8 -*-
"""
Monitor do TAKEOVER da Clara (ela assume atendimentos parados e faz follow-up).
Le as ultimas HORAS (padrao 24) e manda um relatorio no WhatsApp do Andre
(instancia Ceo): quantas conversas ela assumiu, follow-ups, suprimidos,
quantos leads responderam, agendamentos que sairam, quantas a consultora
retomou, erros, e 3 exemplos reais do que ela mandou.
Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO. Opcional: HORAS=n | ALERTA_PARA=5516... | DRY_RUN=1
"""
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"
TZ_SP = timezone(timedelta(hours=-3))


def _sb(key):
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "Range": "0-9999"}


def _get(sb, tabela, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabela}", params=params, headers=sb, timeout=90)
    r.raise_for_status()
    return r.json()


def _hora(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ_SP).strftime("%d/%m %H:%M")


def _primeiro_nome(nome):
    n = (nome or "").replace("Feff", "").strip()
    if not n or n.startswith("(") or n[:2].isdigit() or n.startswith("55"):
        return "lead sem nome"
    return n.split()[0][:14]


def main():
    key = os.environ.get("SUPABASE_KEY", "").replace("Feff", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_CEO", "").replace("Feff", "").strip()
    horas = int(os.environ.get("HORAS", "24"))
    destino = os.environ.get("ALERTA_PARA", "5516992290338").replace("Feff", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_CEO")
        return 1
    sb = _sb(key)
    agora = datetime.now(timezone.utc)
    desde = (agora - timedelta(hours=horas)).strftime("%Y-%m-%dT%H:%M:%SZ")

    ev = _get(sb, "ai_agent_chat_events", {
        "select": "lead_id,event_type,reason,created_at",
        "created_at": f"gte.{desde}", "reason": "like.takeover*", "order": "created_at"})
    stale = {e["lead_id"] for e in ev if e["reason"] == "takeover_stale"}
    follow = {e["lead_id"] for e in ev if e["reason"] == "takeover_followup"}
    supr = {e["lead_id"] for e in ev if e["reason"] == "takeover_followup_suppressed"}
    primeiro_ev = {}
    for e in ev:
        primeiro_ev.setdefault(e["lead_id"], e["created_at"])
    leads_ids = set(primeiro_ev)

    erros = _get(sb, "ai_agent_chat_events", {
        "select": "lead_id,reason,message,created_at", "created_at": f"gte.{desde}",
        "event_type": "eq.error", "order": "created_at.desc", "limit": "5"})

    nomes, enviadas, respostas, retomadas, agend = {}, {}, set(), set(), set()
    if leads_ids:
        ids = ",".join(sorted(leads_ids))
        for l in _get(sb, "leads", {"select": "id,name", "id": f"in.({ids})"}):
            nomes[l["id"]] = l["name"]
        msgs = _get(sb, "whatsapp_messages", {
            "select": "lead_id,content,is_from_me,sent_at,metadata",
            "lead_id": f"in.({ids})", "sent_at": f"gte.{desde}", "group_id": "is.null",
            "order": "sent_at"})
        por_lead = defaultdict(list)
        for m in msgs:
            por_lead[m["lead_id"]].append(m)
        for lid, hist in por_lead.items():
            t0 = primeiro_ev.get(lid)
            depois = [m for m in hist if t0 and m["sent_at"] >= t0]
            clara = [m for m in depois if m["is_from_me"] and (m.get("metadata") or {}).get("sent_by") == "ai_agent"]
            if clara:
                enviadas[lid] = clara[0]
                t_clara = clara[0]["sent_at"]
                if any((not m["is_from_me"]) and m["sent_at"] > t_clara for m in depois):
                    respostas.add(lid)
                if any(m["is_from_me"] and (m.get("metadata") or {}).get("sent_by") != "ai_agent"
                       and m["sent_at"] > t_clara for m in depois):
                    retomadas.add(lid)
        for a in _get(sb, "agendamentos", {"select": "lead_id,created_at", "lead_id": f"in.({ids})",
                                          "created_at": f"gte.{desde}"}):
            agend.add(a["lead_id"])

    pausas = _get(sb, "ai_agent_chat_events", {
        "select": "lead_id", "created_at": f"gte.{desde}", "reason": "eq.human_active_cooldown"})
    pausadas_pos = {p["lead_id"] for p in pausas} & leads_ids

    linhas = [f"🤖 *Clara takeover — últimas {horas}h* ({agora.astimezone(TZ_SP):%d/%m %H:%M})", ""]
    linhas.append(f"• Assumiu {len(stale)} atendimento(s) parado(s)")
    linhas.append(f"• Follow-ups enviados: {len(follow - supr)} · descartados por ela (não cabia): {len(supr)}")
    linhas.append(f"• Mensagens realmente enviadas: {len(enviadas)}")
    linhas.append(f"• Leads que responderam depois: {len(respostas)}")
    linhas.append(f"• Agendamentos que saíram desses leads: {len(agend)}")
    linhas.append(f"• Consultora retomou a conversa: {len(retomadas | pausadas_pos)}")
    if erros:
        linhas.append(f"• ⚠️ Erros da Clara: {len(erros)} (último: {(erros[0].get('message') or erros[0].get('reason') or '')[:80]})")
    else:
        linhas.append("• Erros da Clara: 0")
    if enviadas:
        linhas += ["", "*Exemplos do que ela mandou:*"]
        for lid, m in list(sorted(enviadas.items(), key=lambda x: x[1]["sent_at"], reverse=True))[:3]:
            tipo = "parado" if lid in stale else "follow-up"
            texto = (m["content"] or "").replace("\n", " ")[:160]
            marca = " ✅ respondeu" if lid in respostas else ""
            linhas.append(f"— {_primeiro_nome(nomes.get(lid))} ({tipo}, {_hora(m['sent_at'])}){marca}: \"{texto}\"")
    if not leads_ids:
        linhas += ["", "Nenhuma conversa assumida no período — ou tudo respondido a tempo, ou o cron parou (conferir)."]
    linhas += ["", "Pra desligar: \"desliga o takeover\".", "", "— Leonardo · Território Fit Digital"]
    texto = "\n".join(linhas)
    print(texto)
    if dry:
        return 0
    r = requests.post(f"{UAZAPI_URL}/send/text", headers={"token": zap, "Content-Type": "application/json"},
                      json={"number": destino, "text": texto}, timeout=120)
    print(f"[whats] -> ...{destino[-4:]} HTTP {r.status_code}")
    requests.post(f"{SUPABASE_URL}/rest/v1/agent_activity", headers={**sb, "Prefer": "return=minimal"},
                  json={"agent_slug": "monitor-atendimento", "title": f"Relatório takeover da Clara ({horas}h)",
                        "detail": texto[:900], "status": "concluido",
                        "started_at": agora.isoformat(), "finished_at": agora.isoformat(),
                        "metadata": {"takeover_report": True, "stale": len(stale), "followup": len(follow - supr),
                                     "respostas": len(respostas), "agendamentos": len(agend)}}, timeout=60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
