"""Follow-up unico — leads da acao no HU (25/09/2026) que NAO responderam ao convite.

Audiencia vem do CRM (agent_activity, metadata.kind=audiencia, metadata.campanha=hu-20260925);
o texto do follow-up vem de arquivo local (FOLLOWUP_MSG_FILE) — o repo e publico, nada de PII aqui.
Elegivel: recebeu o convite (log concluido), nao mandou nenhuma mensagem desde a campanha, nao tem
agendamento, nao e aluno ativo nem opt-out, e ainda nao recebeu este follow-up.
Envio pela instancia 2000 (UazAPI): jitter de inicio ate 40min + 90-150s entre envios.
DRY_RUN=1 (padrao) so lista. ONLY=<8 ultimos digitos> restringe a um contato.
Janela: segunda a sabado, das 9h as 20h (BRT).
"""
import os, re, sys, time, random, unicodedata
from datetime import datetime, timezone, timedelta
import requests

ENV_FILE = "C:/Users/Acer/pacto-integrador/.env.integracao"
BASE = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZ = "https://territoriofit.uazapi.com"
TZ = timezone(timedelta(hours=-3))
CAMPANHA = "hu-20260925"
ETAPA = "fu1"
TENANT_DEFAULT = "4eeff494-6528-4f49-8a5c-2742eabb8c2c"
INSTANCE_PHONE = "5516988772000"  # Academia Territorio Fit 2000

env = dict(os.environ)
if os.path.exists(ENV_FILE):  # local; na nuvem (GH Actions) vem dos secrets
    for line in open(ENV_FILE, encoding="utf-8-sig"):
        if "=" in line and not line.startswith("#"):
            k, v = line.rstrip("\n").split("=", 1)
            env[k.strip()] = v.strip()
KEY = env["SUPABASE_KEY"]
TENANT = env.get("CRM_TENANT_ID") or TENANT_DEFAULT
H = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json", "Prefer": "return=representation"}


def api(table, params=None, method="GET", body=None):
    r = requests.request(method, BASE + "/rest/v1/" + table, headers=H, params=params, json=body, timeout=45)
    if not r.ok:
        raise RuntimeError(f"CRM HTTP {r.status_code} on {table}: {r.text[:200]}")
    return r.json() if r.content else []


def norm(v):
    return "".join(c for c in unicodedata.normalize("NFD", str(v or "").lower()) if unicodedata.category(c) != "Mn")


def log(title, detail, status="concluido", meta=None):
    api("agent_activity", method="POST", body={"agent_slug": "comercial-vendas", "title": title, "detail": detail,
        "status": status, "metadata": {"campanha": CAMPANHA, "etapa": ETAPA, **(meta or {})}})


def na_janela():
    agora = datetime.now(TZ)
    return agora.weekday() < 6 and 9 <= agora.hour < 20


def main():
    dry = os.environ.get("DRY_RUN", "1") != "0"
    only = os.environ.get("ONLY")
    msg = open(os.environ["FOLLOWUP_MSG_FILE"], encoding="utf-8").read().strip()
    assert "[nome]" in msg and len(msg) > 40
    aud = api("agent_activity", {"select": "metadata", "metadata->>campanha": "eq." + CAMPANHA,
                                 "metadata->>kind": "eq.audiencia", "limit": "1"})[0]["metadata"]
    contacts = [(c["turno"], c["name"], c["phone"]) for c in aud["contacts"]]
    data = aud["date"]
    inst = api("whatsapp_instances", {"id": "eq." + aud["instanceId"], "select": "status,phone_number,api_key"})[0]
    assert inst["status"] == "connected" and inst["phone_number"] == INSTANCE_PHONE, {k: inst[k] for k in ("status", "phone_number")}
    token = inst["api_key"]
    ativos = {re.sub(r"\D", "", a["phone8"])[-8:] for a in api("pacto_alunos_ativos", {"select": "phone8", "limit": "5000"})}
    acts = api("agent_activity", {"select": "status,metadata", "metadata->>campanha": "eq." + CAMPANHA, "limit": "1000"})
    convidados = {(a["metadata"] or {}).get("disparo_key") for a in acts if a["status"] == "concluido"}
    feitos = {(a["metadata"] or {}).get("disparo_key") for a in acts if a["status"] in ("concluido", "erro")}
    enviados = pulados = falhas = seguidas = 0
    last = None
    if not dry:
        if not na_janela():
            print("Fora da janela (seg a sab, 9h as 20h) — nada enviado."); return 0
        espera = random.uniform(0, 40 * 60)
        print(f"Jitter de inicio: {espera / 60:.0f} min", flush=True)
        time.sleep(espera)
    for turno, nome, ph in contacts:
        l8 = ph[-8:]
        if only and l8 != only:
            continue
        key = f"{CAMPANHA}-{ETAPA}-{l8}"
        if f"{CAMPANHA}-{l8}" not in convidados:
            print(f"[skip] {nome} nao recebeu o convite"); pulados += 1; continue
        if key in feitos:
            print(f"[skip] {nome} ja recebeu este follow-up"); pulados += 1; continue
        if l8 in ativos:
            print(f"[skip] {nome} aluno ativo"); pulados += 1; continue
        leads = api("leads", {"select": "id,name,phone,status,tags", "tenant_id": "eq." + TENANT,
                              "phone": "like.*" + l8, "order": "created_at"})
        if not leads:
            print(f"[skip] {nome} sem lead no CRM"); pulados += 1; continue
        lead = leads[0]
        tags = {norm(t) for t in (lead.get("tags") or [])}
        if tags & {"nao contatar", "nao_contatar", "opt_out", "opt-out", "bloqueado"} or lead.get("status") in ("aluno", "ativo", "cliente"):
            print(f"[skip] {nome} bloqueado/aluno no CRM"); pulados += 1; continue
        ids = "in.(" + ",".join(l["id"] for l in leads) + ")"
        if api("whatsapp_messages", {"select": "id", "lead_id": ids, "is_from_me": "eq.false",
                                     "created_at": "gte." + data + "T00:00:00-03:00", "limit": "1"}):
            print(f"[skip] {nome} ja respondeu"); pulados += 1; continue
        if api("agendamentos", {"select": "id", "lead_id": ids, "created_at": "gte." + data + "T00:00:00-03:00", "limit": "1"}):
            print(f"[skip] {nome} ja tem agendamento"); pulados += 1; continue
        destino = re.sub(r"\D", "", lead.get("phone") or "")
        destino = destino if len(destino) >= 12 else "55" + (destino if len(destino) == 11 else ph)
        text = msg.replace("[nome]", nome)
        if dry:
            print(f"[DRY] {turno} {nome} -> ...{destino[-4:]}"); enviados += 1; continue
        if last is not None:
            wait = 90 + random.uniform(0, 60) - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        if not na_janela():
            print("Fora da janela (seg a sab, 9h as 20h) — parando."); break
        r = requests.post(UAZ + "/send/text", headers={"token": token, "Content-Type": "application/json"},
                          json={"number": destino, "text": text}, timeout=60)
        last = time.monotonic()
        rdata = r.json() if r.ok else {}
        ok = bool(rdata.get("id") or rdata.get("messageid") or rdata.get("messageID"))
        print(f"[send] {datetime.now(TZ):%H:%M} {turno} {nome} -> ...{destino[-4:]} HTTP {r.status_code} {'ok' if ok else r.text[:160]}", flush=True)
        if not ok:
            falhas += 1; seguidas += 1
            log(f"Ação HU: falha no follow-up pra {nome}", f"HTTP {r.status_code}: {r.text[:200]}", "erro",
                {"disparo_key": key, "lead_id": lead["id"], "turno": turno})
            if seguidas >= 3:
                print("3 falhas seguidas — parando pra proteger a instancia."); break
            continue
        seguidas = 0; enviados += 1
        log(f"Ação HU: follow-up enviado pra {nome}",
            f"{nome} (turno {turno}) — lembrete do voucher 30 dias pelo Whats 2000 (sem resposta ao convite)",
            "concluido", {"disparo_key": key, "lead_id": lead["id"], "turno": turno})
    print(f"\nResumo: {enviados} {'elegivel(is)' if dry else 'enviado(s)'}, {pulados} pulado(s), {falhas} falha(s)", flush=True)
    if not dry and not only:
        log("Campanha Ação HU 25/09 — follow-up concluído",
            f"{enviados} lembretes enviados nesta rodada, {pulados} pulados (responderam/agendaram/inelegíveis), {falhas} falhas.",
            "erro" if falhas and not enviados else "concluido")
    return 0


if __name__ == "__main__":
    sys.exit(main())
