"""One-off cloud campaign. Private audience/text live only in CRM."""
import os, sys, time, random, re, json, unicodedata
from datetime import datetime, timezone, timedelta
import requests

BASE = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
TZ = timezone(timedelta(hours=-3))
TENANT = "4eeff494-6528-4f49-8a5c-2742eabb8c2c"
CAMPAIGN_ID = "6b1499ea-8520-491a-8e54-1480a937ae62"
KEY = os.environ.get("SUPABASE_KEY", "")
HEADERS = {"apikey": KEY, "Authorization": "Bearer " + KEY,
           "Content-Type": "application/json", "Prefer": "return=representation"}

def api(table, params=None, method="GET", body=None):
    r = requests.request(method, BASE + "/rest/v1/" + table,
                         headers=HEADERS, params=params, json=body, timeout=45)
    if not r.ok:
        raise RuntimeError("CRM HTTP " + str(r.status_code) + " on " + table)
    return r.json() if r.content else []

def all_rows(table, params):
    out = []
    while True:
        rows = api(table, dict(params, offset=len(out), limit=1000))
        out.extend(rows)
        if len(rows) < 1000:
            return out

def digits(value):
    return re.sub(r"\D", "", value or "")

def norm(value):
    return "".join(c for c in unicodedata.normalize("NFD", str(value).lower())
                   if unicodedata.category(c) != "Mn")

def active_base():
    rows = all_rows("pacto_alunos_ativos", {"select": "phone8,synced_at", "order": "phone8"})
    if not rows or any(datetime.fromisoformat(r["synced_at"].replace("Z", "+00:00")).astimezone(TZ).date()
                       != datetime.now(TZ).date() for r in rows):
        raise RuntimeError("Active student cache not fully refreshed today")
    return {digits(r["phone8"])[-8:] for r in rows}

def blocked_lead(lead):
    metadata = lead.get("metadata") or {}
    status = norm(lead.get("status"))
    pacto = norm(metadata.get("pacto_situacao"))
    tags = {norm(t) for t in (lead.get("tags") or [])}
    if status in {"aluno", "aluno ativo", "aluno_ativo", "active", "ativo", "customer", "cliente"} or pacto in {"ativo", "aluno", "matriculado"}:
        return "aluno"
    if tags.intersection({"aluno", "aluno ativo", "nao contatar", "nao_contatar", "opt_out", "opt-out", "bloqueado"}):
        return "exclusao_cadastro"
    if any(metadata.get(k) is True for k in ["whatsapp_opted_out", "opt_out", "do_not_contact", "blocked"]):
        return "nao_contatar"
    return None

def eligible(contact, active, leads):
    if digits(contact["phone"])[-8:] in active:
        return None, "aluno_ativo"
    matches = [l for l in leads if digits(l.get("phone"))[-8:] == digits(contact["phone"])[-8:]]
    if len(matches) > 1:
        return None, "cadastro_ambiguo"
    lead = matches[0] if matches else None
    if lead:
        reason = blocked_lead(lead)
        if reason:
            return lead, reason
        history = api("whatsapp_messages", {"select": "content",
            "lead_id": "eq." + lead["id"], "tenant_id": "eq." + TENANT,
            "is_from_me": "eq.false", "group_id": "is.null",
            "order": "sent_at.desc", "limit": 100})
        for message in history:
            text = norm(message.get("content", ""))
            if any(p in text for p in ("nao me mande", "nao me envie", "nao quero receber",
                                      "pare de enviar", "remova meu", "retire meu", "nao tenho interesse")):
                return lead, "recusa_no_historico"
    return lead, None

def main():
    dry = os.environ.get("DRY_RUN", "1") != "0"
    row = api("agent_activity", {"id": "eq." + CAMPAIGN_ID})[0]
    m = row["metadata"]
    contacts = m["contacts"]
    assert len(contacts) == 19 and len({c["phone"] for c in contacts}) == 19
    assert m["date"] == "2026-09-09" and m["delayMinSeconds"] == 30 and m["delayMaxSeconds"] == 60
    assert all(re.fullmatch(r"5516\d{9}", c["phone"]) for c in contacts)
    if not dry:
        while datetime.now(TZ).date().isoformat() == m["date"] and datetime.now(TZ).hour < 9:
            time.sleep(30)
        now = datetime.now(TZ)
        if now.date().isoformat() != m["date"] or not 9 <= now.hour < 12:
            print("Outside authorized date/window; no sends.")
            return
        if m.get("state") != "scheduled":
            print("Campaign already claimed or finished; no sends.")
            return
    active = active_base()
    leads = all_rows("leads", {"select": "id,name,phone,status,tags,metadata,context",
                              "tenant_id": "eq." + TENANT, "order": "id"})
    instance = api("whatsapp_instances", {"id": "eq." + m["instanceId"],
        "tenant_id": "eq." + TENANT, "select": "id,status,phone_number,api_url,api_key"})[0]
    assert digits(instance["phone_number"]) == "5516988772000"
    assert instance["status"] == "connected"
    url = instance["api_url"].rstrip("/")
    assert url == "https://territoriofit.uazapi.com"
    if dry:
        reasons = {}
        for contact in contacts:
            _, reason = eligible(contact, active, leads)
            reasons[reason or "elegivel"] = reasons.get(reason or "elegivel", 0) + 1
        print(json.dumps({"dry_run": True, "audience": len(contacts), "checks": reasons,
                          "date": m["date"], "start_local": "09:00", "spacing_seconds": [30, 60]}))
        return
    m["state"] = "running"
    m["results"] = []
    claim = api("agent_activity", {"id": "eq." + CAMPAIGN_ID, "metadata->>state": "eq.scheduled"},
                "PATCH", {"metadata": m, "status": "em_andamento", "started_at": datetime.now(timezone.utc).isoformat()})
    if not claim:
        print("Campaign claimed elsewhere; no sends.")
        return
    def save():
        api("agent_activity", {"id": "eq." + CAMPAIGN_ID}, "PATCH",
            {"metadata": m, "status": "erro" if m["state"] == "failed" else
             ("concluido" if m["state"] == "completed" else "em_andamento"),
             "finished_at": datetime.now(timezone.utc).isoformat() if m["state"] in {"failed", "completed"} else None})
    last_finished = None
    try:
        for index, contact in enumerate(contacts):
            if datetime.now(TZ).date().isoformat() != m["date"] or datetime.now(TZ).hour >= 12:
                raise RuntimeError("Authorized morning window ended")
            # Refresh membership just before each send.
            active = active_base()
            lead, reason = eligible(contact, active, leads)
            result = {"index": index, "phone": contact["phone"], "state": "skipped" if reason else "preparing",
                      "reason": reason}
            m["results"].append(result)
            save()
            if reason:
                continue
            if lead:
                lead = api("leads", {"id": "eq." + lead["id"], "tenant_id": "eq." + TENANT})[0]
                reason = blocked_lead(lead)
                if reason:
                    result.update(state="skipped", reason=reason)
                    save()
                    continue
            else:
                lead = api("leads", method="POST", body={"tenant_id": TENANT,
                    "name": contact["name"], "phone": contact["phone"], "status": "lead",
                    "source": "parceria", "metadata": {"campanha": m["campaign"]}})[0]
                leads.append(lead)
            first = "José Neto" if contact["name"].startswith("José Neto") else contact["name"].split()[0]
            text = m["message"].replace("[nome]", first)
            note = "\nCampanha autorizada Time Marquesfit 390 da Faber: convite de 15 dias grátis; desconto especial e camiseta exclusiva no fechamento da matrícula. Não inventar percentual, prazo ou condições. Texto do convite: " + text
            api("leads", {"id": "eq." + lead["id"], "tenant_id": "eq." + TENANT}, "PATCH",
                {"context": (lead.get("context") or "") + note})
            if last_finished is not None:
                wait = random.uniform(30, 60) - (time.monotonic() - last_finished)
                if wait > 0:
                    time.sleep(wait)
            if datetime.now(TZ).hour >= 12:
                raise RuntimeError("Authorized morning window ended")
            result.update(state="sending", lead_id=lead["id"], started_at=datetime.now(timezone.utc).isoformat())
            save()  # Durable before sending; never auto-retry an uncertain send.
            response = requests.post(url + "/send/text",
                headers={"token": instance["api_key"], "Content-Type": "application/json"},
                json={"number": contact["phone"], "text": text}, timeout=45)
            last_finished = time.monotonic()
            if not response.ok:
                result.update(state="failed", http_status=response.status_code)
                raise RuntimeError("Provider rejected send; campaign stopped")
            data = response.json()
            mid = data.get("id") or data.get("messageid") or data.get("messageID") or (data.get("message") or {}).get("id")
            if not mid:
                result.update(state="uncertain")
                raise RuntimeError("No provider message ID; manual verification required")
            result.update(state="sent", message_id=mid, sent_at=datetime.now(timezone.utc).isoformat())
            save()
            print("Recipient", index + 1, "accepted by provider.")
        m["state"] = "completed"
        save()
        print(json.dumps({"sent": sum(r["state"] == "sent" for r in m["results"]),
                          "skipped": sum(r["state"] == "skipped" for r in m["results"])}))
    except Exception:
        if m["results"] and m["results"][-1]["state"] == "sending":
            m["results"][-1]["state"] = "uncertain"
        m["state"] = "failed"
        save()
        raise RuntimeError("Campaign paused. Inspect private CRM audit; do not resend automatically.") from None

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Campaign check/run failed:", type(error).__name__)
        sys.exit(1)
