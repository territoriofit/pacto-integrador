# -*- coding: utf-8 -*-
"""
Monitor de Atendimento: vigia continuo de TODOS os atendimentos do CRM
(consultoras + Clara IA + respostas das automacoes de disparo).

Roda a cada ~30min no horario comercial (GitHub Actions) e verifica:

  1. ATENDIMENTO PARADO — lead mandou mensagem (individual, instancias de
     atendimento) ha mais de LIMITE_MIN minutos e NINGUEM respondeu
     (nem consultora nem Clara). Numeros da propria equipe (team_members)
     ficam de fora. Antes de alertar, uma IA (claude-haiku-4-5, chave da
     tabela config do CRM) le as ultimas mensagens e descarta conversas ja
     finalizadas (despedida, assunto resolvido, msg que nao pede resposta);
     se a IA falhar, o alerta e mantido (fail-open).
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


def _get_all(sb: dict, tabela: str, params: dict,
             max_rows: int = 20000) -> list[dict]:
    """PostgREST corta silenciosamente em 1000 linhas por request (um
    limit=8000 volta 1000 sem erro) — pagina via header Range ate vir
    pagina incompleta. Sem isso o monitor enxergava so as msgs mais
    antigas da janela e alertava conversa ja respondida."""
    pagina = 1000
    out: list[dict] = []
    params = {k: v for k, v in params.items() if k != "limit"}
    while len(out) < max_rows:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{tabela}", params=params,
            headers={**sb, "Range-Unit": "items",
                     "Range": f"{len(out)}-{len(out) + pagina - 1}"},
            timeout=60)
        r.raise_for_status()
        chunk = r.json()
        out.extend(chunk)
        if len(chunk) < pagina:
            break
    return out


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
    "ok obrigada", "obg", "valeu", "vlw", "blz", "beleza", "belezinha",
    "imagina",
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


def _last8(phone: str) -> str:
    d = "".join(c for c in (phone or "") if c.isdigit())
    return d[-8:]


def _academia_aberta(sb: dict, agora) -> bool:
    """Janela = horario de FUNCIONAMENTO da academia, lido do
    settings.business_hours do agente (mesma fonte da Clara):
    {"0": ["08:00","13:00"], ...} com 0=domingo, horarios em BRT.
    Sem config (ou erro), cai no fallback 08-20 BRT (janela antiga)."""
    try:
        rows = _get(sb, "ai_sales_agents", {
            "select": "settings", "is_active": "eq.true", "limit": "1"})
        hours = (rows[0].get("settings") or {}).get("business_hours") if rows else None
        if isinstance(hours, dict):
            dow_js = (agora.weekday() + 1) % 7  # python seg=0 -> js dom=0
            faixa = hours.get(str(dow_js))
            if not (isinstance(faixa, list) and len(faixa) >= 2):
                return False  # dia sem faixa = fechado
            hhmm = f"{agora.hour:02d}:{agora.minute:02d}"
            return faixa[0] <= hhmm <= faixa[1]
    except Exception as e:
        print(f"[janela] erro lendo business_hours ({e}) — fallback 08-20")
    return 8 <= agora.hour < 20


def _anthropic_key(sb: dict) -> str:
    """Le a ANTHROPIC_API_KEY da tabela config do CRM (mesma da Clara)."""
    try:
        rows = _get(sb, "config", {"select": "value",
                                   "key": "eq.ANTHROPIC_API_KEY", "limit": "1"})
        return (rows[0].get("value") or "").strip() if rows else ""
    except Exception as e:
        print(f"[ia] erro lendo ANTHROPIC_API_KEY: {e}")
        return ""


def conversa_precisa_resposta(anthropic_key: str, hist: list[dict]) -> bool:
    """IA le o contexto e decide se a conversa ainda espera resposta da
    academia. Fail-open: qualquer erro mantem o alerta."""
    if not anthropic_key:
        return True
    transcript = "\n".join(
        f"[{'Academia' if m['is_from_me'] else 'Cliente'}]: "
        f"{_trecho(m.get('content'), 200)}"
        for m in hist[-6:] if (m.get("content") or "").strip())
    if not transcript:
        return True
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=10,
            system=(
                "Você analisa conversas de WhatsApp de uma academia "
                "(Território Fit). Diga se a ÚLTIMA mensagem do cliente "
                "ainda espera uma resposta da equipe. Conversa encerrada, "
                "despedida, agradecimento, assunto já resolvido ou "
                "mensagem que não pede resposta = NAO. Pergunta, pedido, "
                "interesse, problema não resolvido ou cliente esperando "
                "retorno = SIM. Responda APENAS a palavra SIM ou NAO."),
            messages=[{"role": "user", "content": transcript}],
        )
        veredito = (msg.content[0].text or "").strip().upper()
        return not veredito.startswith("NAO")
    except Exception as e:
        print(f"[ia] erro na análise ({e}) — mantendo alerta")
        return True


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

    sb = _sb(key)
    agora = datetime.now(TZ_SP)
    if not _academia_aberta(sb, agora) and not dry:
        print(f"[janela] {agora:%H:%M} BRT — academia fechada, nada a fazer.")
        return 0
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
    msgs = _get_all(sb, "whatsapp_messages", {
        "select": "lead_id,instance_id,is_from_me,sent_at,content,metadata",
        "group_id": "is.null", "sent_at": f"gte.{h48}",
        "order": "sent_at.asc"})
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
    for r in _get_all(sb, "agent_activity", {
            "select": "title,created_at,metadata",
            "created_at": f"gte.{d7}"}):
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

    # -- nomes e fones dos leads -----------------------------------------
    lids = list({a["lead_id"] for a in alertas if a["lead_id"]})
    nomes: dict[str, str] = {}
    fones_lead: dict[str, str] = {}
    for i in range(0, len(lids), 80):
        for row in _get(sb, "leads", {
                "select": "id,name,phone",
                "id": f"in.({','.join(lids[i:i + 80])})"}):
            nomes[row["id"]] = row.get("name") or "Lead"
            fones_lead[row["id"]] = row.get("phone") or ""

    # -- exclui numeros da propria equipe (consultoras etc.) -------------
    equipe = {_last8(t.get("phone"))
              for t in _get(sb, "team_members", {"select": "name,phone"})
              if _last8(t.get("phone"))}
    antes = len(alertas)
    alertas = [a for a in alertas
               if not (a["lead_id"]
                       and _last8(fones_lead.get(a["lead_id"], "")) in equipe)]
    if antes != len(alertas):
        print(f"[equipe] {antes - len(alertas)} caso(s) descartado(s): "
              "numero e da propria equipe")

    novos = [a for a in alertas if a["key"] not in emitidos]

    # -- IA analisa o contexto antes de alertar (so casos novos) ---------
    if novos:
        akey = _anthropic_key(sb)
        mantidos = []
        for a in novos:
            if a["tipo"] in ("atendimento_parado", "resposta_automacao"):
                hist = por_lead.get(a["lead_id"], [])
                if not conversa_precisa_resposta(akey, hist):
                    print(f"[ia] {nomes.get(a['lead_id'], '')} — conversa "
                          "finalizada segundo o contexto, sem alerta")
                    continue
            mantidos.append(a)
        novos = mantidos
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
