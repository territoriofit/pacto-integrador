# -*- coding: utf-8 -*-
"""
Monitor de Atendimento: vigia continuo de TODOS os atendimentos do CRM
(consultoras + Clara IA + respostas das automacoes de disparo).

Roda a cada ~30min no horario comercial (GitHub Actions) e verifica:

  1. ATENDIMENTO PARADO — lead mandou mensagem (individual, instancias de
     atendimento) ha mais de LIMITE_MIN minutos e NINGUEM respondeu
     (nem consultora nem Clara).
  2. RESPOSTA A AUTOMACAO SEM ATENDIMENTO — lead que recebeu um disparo
     (win-back, aniversariantes, regua de cobranca) nos ultimos 7 dias
     respondeu e esta sem resposta humana; o alerta ja vai com o
     HISTORICO: qual automacao recebeu, quando, e as ultimas mensagens.
  3. TRANSFERENCIA DA CLARA NAO ASSUMIDA — conversa status=transferred ha
     mais de LIMITE_MIN minutos sem mensagem humana depois (mesma logica
     do banner do CRM).
  4. CLARA COM ERRO — eventos 'error' em ai_agent_chat_events na ultima
     1h30 (ex.: creditos Anthropic esgotados, timeout).

Cada alerta e deduplicado em agent_activity (agent_slug
'monitor-atendimento', metadata.alerta_key) — 1 alerta por caso a cada
ALERTA_COOLDOWN_H horas (padrao 4h) — e aparece na aba Equipe Digital.
O resumo do run vai por WhatsApp pro Andre (instancia "Ceo Territorio
Digital" -> numero pessoal).

Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO.
Opcional: ALERTA_PARA=5516... (padrao 5516992290338) | LIMITE_MIN=n
(padrao 30) | ALERTA_COOLDOWN_H=n (padrao 4) | DRY_RUN=1 (nao envia
WhatsApp nem grava dedup).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"

TZ_SP = timezone(timedelta(hours=-3))

INSTANCIAS_FORA = ("professores", "ceo")  # nao sao atendimento a aluno/lead


def _sb(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _get(sb: dict, tabela: str, params: dict) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabela}", params=params,
                     headers=sb, timeout=60)
    r.raise_for_status()
    return r.json()


def _mins(iso: str) -> int:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    except (ValueError, AttributeError):
        return 0


def _fmt_tempo(m: int) -> str:
    return f"{m}min" if m < 60 else f"{m // 60}h{m % 60:02d}"


def _trecho(txt: str, n: int = 70) -> str:
    t = " ".join((txt or "").split())
    return t[:n] + ("…" if len(t) > n else "")


_FECHAMENTOS = {
    "ok", "okay", "okk", "obrigado", "obrigada", "ok obrigado",
    "ok obrigada", "obg", "valeu", "vlw", "blz", "beleza", "imagina",
    "de nada", "denada", "ta bom", "tá bom", "ta bem", "tudo bem",
    "certo", "show", "top", "perfeito", "combinado", "entendi", "sim",
    "boa", "otimo", "ótimo", "legal", "maravilha", "aham", "uhum",
}


def _fecha_conversa(content: str) -> bool:
    """Msg de encerramento (ok/obrigada/figurinha/so emoji) nao pede
    resposta — nao vira alerta de atendimento parado."""
    t = (content or "").strip().lower()
    if "[sticker]" in t or t.startswith("🏷️"):
        return True
    limpo = "".join(c for c in t if c.isalnum() or c.isspace()).strip()
    return not limpo or limpo in _FECHAMENTOS


def _eh_humana(msg: dict) -> bool:
    sb_ = (msg.get("metadata") or {}).get("sent_by")
    return msg.get("is_from_me") and sb_ != "ai_agent"


def _respondida(msgs_lead: list[dict], desde_iso: str) -> bool:
    """Alguem (humano ou Clara) mandou msg depois de desde_iso?"""
    return any(m["is_from_me"] and m["sent_at"] > desde_iso
               for m in msgs_lead)


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").strip()
    zap = os.environ.get("UAZAPI_TOKEN_CEO", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    destino = os.environ.get("ALERTA_PARA", "5516992290338").strip()
    limite_min = int(os.environ.get("LIMITE_MIN", "30"))
    cooldown_h = int(os.environ.get("ALERTA_COOLDOWN_H", "4"))
    if not key or (not zap and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_CEO")
        return 1

    agora = datetime.now(TZ_SP)
    if not (8 <= agora.hour < 20) and not dry:
        print(f"[janela] {agora:%H:%M} BRT — fora do horario, nada a fazer.")
        return 0

    sb = _sb(key)
    utc_agora = datetime.now(timezone.utc)
    h48 = (utc_agora - timedelta(hours=48)).isoformat()
    d7 = (utc_agora - timedelta(days=7)).isoformat()
    cooldown_iso = (utc_agora - timedelta(hours=cooldown_h)).isoformat()

    # -- instancias de atendimento ---------------------------------------
    insts = _get(sb, "whatsapp_instances", {"select": "id,name"})
    inst_atend = {i["id"] for i in insts
                  if not any(x in (i.get("name") or "").lower()
                             for x in INSTANCIAS_FORA)}

    # -- mensagens individuais das ultimas 48h ---------------------------
    msgs = _get(sb, "whatsapp_messages", {
        "select": "lead_id,instance_id,is_from_me,sent_at,content,metadata",
        "group_id": "is.null", "sent_at": f"gte.{h48}",
        "order": "sent_at.asc", "limit": "8000"})
    por_lead: dict[str, list[dict]] = {}
    for m in msgs:
        if m.get("lead_id") and (m.get("instance_id") in inst_atend
                                 or m.get("instance_id") is None):
            por_lead.setdefault(m["lead_id"], []).append(m)

    # -- alertas ja emitidos (cooldown) ----------------------------------
    emitidos = {(r.get("metadata") or {}).get("alerta_key")
                for r in _get(sb, "agent_activity", {
                    "select": "metadata",
                    "agent_slug": "eq.monitor-atendimento",
                    "created_at": f"gte.{cooldown_iso}",
                    "limit": "1000"})}

    # -- disparos de automacao dos ultimos 7d ----------------------------
    disparos: dict[str, dict] = {}  # lead_id -> ultimo disparo
    for r in _get(sb, "agent_activity", {
            "select": "title,created_at,metadata",
            "created_at": f"gte.{d7}", "limit": "3000"}):
        meta = r.get("metadata") or {}
        lid = meta.get("lead_id")
        if lid and meta.get("disparo_key"):
            atual = disparos.get(lid)
            if not atual or r["created_at"] > atual["created_at"]:
                disparos[lid] = {"title": r["title"],
                                 "created_at": r["created_at"],
                                 "campanha": meta.get("campanha", "")}

    alertas: list[dict] = []  # {key, tipo, texto}

    # ---- 2) resposta a automacao sem atendimento (prioridade) ----------
    leads_automacao = set()
    for lid, disp in disparos.items():
        hist = por_lead.get(lid, [])
        inbound = [m for m in hist if not m["is_from_me"]
                   and m["sent_at"] > disp["created_at"]]
        if not inbound:
            continue
        ultima = inbound[-1]
        espera = _mins(ultima["sent_at"])
        if espera < limite_min or _respondida(hist, ultima["sent_at"]):
            continue
        if _fecha_conversa(ultima["content"]):
            continue
        leads_automacao.add(lid)
        dia = agora.date().isoformat()
        contexto = " | ".join(
            f"{'>' if m['is_from_me'] else '<'} {_trecho(m['content'], 40)}"
            for m in hist[-3:])
        alertas.append({
            "key": f"mon-auto-{lid}-{dia}",
            "tipo": "resposta_automacao",
            "lead_id": lid,
            "texto": (f"respondeu a automacao *{disp['campanha'] or disp['title']}* "
                      f"(disparo {_fmt_tempo(_mins(disp['created_at']))} atras) "
                      f"e esta ha *{_fmt_tempo(espera)}* sem atendimento.\n"
                      f"   Ultima msg: \"{_trecho(ultima['content'])}\"\n"
                      f"   Historico: {contexto}")})

    # ---- 1) atendimento parado (consultoras + Clara) -------------------
    for lid, hist in por_lead.items():
        if lid in leads_automacao:
            continue
        inbound = [m for m in hist if not m["is_from_me"]]
        if not inbound:
            continue
        ultima = inbound[-1]
        espera = _mins(ultima["sent_at"])
        if espera < limite_min or espera > 48 * 60:
            continue
        if _respondida(hist, ultima["sent_at"]):
            continue
        if _fecha_conversa(ultima["content"]):
            continue
        dia = agora.date().isoformat()
        alertas.append({
            "key": f"mon-parado-{lid}-{dia}",
            "tipo": "atendimento_parado",
            "lead_id": lid,
            "texto": (f"esta ha *{_fmt_tempo(espera)}* sem resposta "
                      f"(nem consultora nem Clara).\n"
                      f"   Ultima msg: \"{_trecho(ultima['content'])}\"")})

    # ---- 3) transferencia da Clara nao assumida ------------------------
    for c in _get(sb, "ai_agent_conversations", {
            "select": "id,lead_id,updated_at,metadata",
            "status": "eq.transferred",
            "updated_at": f"gte.{d7}", "limit": "500"}):
        meta = c.get("metadata") or {}
        if meta.get("handoff_resolved_at"):
            continue
        t_at = meta.get("transferred_at") or c["updated_at"]
        espera = _mins(t_at)
        if espera < limite_min:
            continue
        hist = por_lead.get(c["lead_id"], [])
        if any(_eh_humana(m) and m["sent_at"] > t_at for m in hist):
            continue
        alertas.append({
            "key": f"mon-transf-{c['id']}",
            "tipo": "transferencia_pendente",
            "lead_id": c["lead_id"],
            "texto": (f"foi transferido pela Clara ha "
                      f"*{_fmt_tempo(espera)}* e ninguem assumiu.\n"
                      f"   Motivo: {meta.get('transfer_reason', '—')}")})

    # ---- 4) Clara com erro ---------------------------------------------
    h90m = (utc_agora - timedelta(minutes=90)).isoformat()
    erros = _get(sb, "ai_agent_chat_events", {
        "select": "id,created_at,event_type",
        "event_type": "eq.error",
        "created_at": f"gte.{h90m}", "limit": "50"})
    if erros:
        hora_bucket = agora.strftime("%Y-%m-%d-%H")
        alertas.append({
            "key": f"mon-clara-erro-{hora_bucket}",
            "tipo": "clara_erro",
            "lead_id": None,
            "texto": (f"⚙️ Clara registrou *{len(erros)} erro(s)* nos "
                      "ultimos 90min — conferir creditos Anthropic / fila "
                      "(ai_agent_chat_events).")})

    # -- nomes dos leads -------------------------------------------------
    lids = list({a["lead_id"] for a in alertas if a["lead_id"]})
    nomes: dict[str, str] = {}
    for i in range(0, len(lids), 80):
        for row in _get(sb, "leads", {
                "select": "id,name",
                "id": f"in.({','.join(lids[i:i + 80])})"}):
            nomes[row["id"]] = row.get("name") or "Lead"

    novos = [a for a in alertas if a["key"] not in emitidos]
    print(f"[monitor] {len(alertas)} caso(s) aberto(s), "
          f"{len(novos)} novo(s) (cooldown {cooldown_h}h)")
    for a in alertas:
        flag = "NOVO " if a in novos else "visto"
        print(f"  [{flag}] {a['tipo']}: "
              f"{nomes.get(a['lead_id'], '')} — {a['texto'][:80]}")

    if not novos:
        return 0
    if dry:
        print("[DRY] nada enviado/gravado.")
        return 0

    # -- WhatsApp pro Andre ----------------------------------------------
    linhas = [f"🔎 *Monitor de Atendimento* — {agora:%H:%M}",
              f"{len(novos)} caso(s) precisando de atencao:", ""]
    for i, a in enumerate(novos, 1):
        nome = nomes.get(a["lead_id"], "").title() if a["lead_id"] else ""
        prefixo = f"{i}) {nome} " if nome else f"{i}) "
        linhas.append(prefixo + a["texto"])
        linhas.append("")
    resp = requests.post(
        f"{UAZAPI_URL}/send/text",
        headers={"token": zap, "Content-Type": "application/json"},
        json={"number": destino, "text": "\n".join(linhas).strip()},
        timeout=120)
    print(f"[whats] alerta -> ...{destino[-4:]} HTTP {resp.status_code}")

    # -- log + dedup na Equipe Digital -----------------------------------
    for a in novos:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "monitor-atendimento",
                  "title": f"Alerta: {a['tipo'].replace('_', ' ')}",
                  "detail": (f"{nomes.get(a['lead_id'], '').title()} — "
                             if a["lead_id"] else "") +
                            a["texto"].replace("*", ""),
                  "status": "concluido",
                  "metadata": {"alerta_key": a["key"],
                               "tipo": a["tipo"],
                               "lead_id": a["lead_id"]}},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
