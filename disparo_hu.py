"""Disparo unico — leads captados na acao no HU (Hospital Universitario) em 25/09/2026.

Audiencia, mensagem e nota ficam SOMENTE no CRM (agent_activity, metadata.kind=audiencia,
metadata.campanha=hu-20260925) — o repo e publico, nada de PII aqui. Para cada contato:
  1. localiza lead no CRM pelos ultimos 8 digitos (ou cria um novo, source acao_hu)
  2. etiqueta com "H.U" (leads.tags) + nota no context pra Clara
  3. envia a mensagem pela instancia 2000 (UazAPI) com 90-150s entre envios
  4. loga em agent_activity (metadata.campanha, dedup por disparo_key = campanha-<8 digitos>)
Roda local (.env.integracao) ou na nuvem (secrets SUPABASE_KEY). DRY_RUN=1 (padrao) so simula.
ONLY=<8 ultimos digitos> restringe a um contato. Janela: na data da campanha, ate 20h (BRT).
"""
import os, re, sys, time, random, unicodedata
from datetime import datetime, timezone, timedelta
import requests

ENV_FILE = "C:/Users/Acer/pacto-integrador/.env.integracao"
BASE = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZ = "https://territoriofit.uazapi.com"
TZ = timezone(timedelta(hours=-3))
CAMPANHA = "hu-20260925"
TENANT_DEFAULT = "4eeff494-6528-4f49-8a5c-2742eabb8c2c"
INSTANCE_PHONE = "5516988772000"  # Academia Territorio Fit 2000
MARCA = "[Ação HU 25/09/2026]"

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
        "status": status, "metadata": {"campanha": CAMPANHA, **(meta or {})}})


def main():
    dry = os.environ.get("DRY_RUN", "1") != "0"
    only = os.environ.get("ONLY")
    aud = api("agent_activity", {"select": "metadata", "metadata->>campanha": "eq." + CAMPANHA,
                                 "metadata->>kind": "eq.audiencia", "limit": "1"})[0]["metadata"]
    contacts = [(c["turno"], c["name"], c["phone"]) for c in aud["contacts"]]
    msg, nota_tpl, tag, data = aud["message"], aud["nota"], aud["tag"], aud["date"]
    assert len({p for _, _, p in contacts}) == len(contacts)
    assert all(re.fullmatch(r"[1-9]\d9\d{8}", p) for _, _, p in contacts)
    assert MARCA in nota_tpl and "[nome]" in msg
    inst = api("whatsapp_instances", {"id": "eq." + aud["instanceId"], "select": "status,phone_number,api_key"})[0]
    assert inst["status"] == "connected" and inst["phone_number"] == INSTANCE_PHONE, {k: inst[k] for k in ("status", "phone_number")}
    token = inst["api_key"]
    ativos = {re.sub(r"\D", "", a["phone8"])[-8:] for a in api("pacto_alunos_ativos", {"select": "phone8", "limit": "5000"})}
    done = {(a["metadata"] or {}).get("disparo_key") for a in api("agent_activity",
            {"select": "metadata", "metadata->>campanha": "eq." + CAMPANHA, "status": "in.(concluido,erro)", "limit": "500"})}
    enviados = pulados = falhas = seguidas = 0
    last = None
    for turno, nome, ph in contacts:
        l8 = ph[-8:]
        if only and l8 != only:
            continue
        key = f"{CAMPANHA}-{l8}"
        if key in done:
            print(f"[skip] {nome} ja recebeu"); pulados += 1; continue
        if l8 in ativos:
            print(f"[skip] {nome} aluno ativo"); pulados += 1; continue
        leads = api("leads", {"select": "id,name,phone,status,tags,metadata,context,created_at",
                              "tenant_id": "eq." + TENANT, "phone": "like.*" + l8, "order": "created_at"})
        lead = leads[0] if leads else None
        if lead:
            tags = {norm(t) for t in (lead.get("tags") or [])}
            if tags & {"nao contatar", "nao_contatar", "opt_out", "opt-out", "bloqueado"} or lead.get("status") in ("aluno", "ativo"):
                print(f"[skip] {nome} bloqueado/aluno no CRM"); pulados += 1; continue
        destino = re.sub(r"\D", "", (lead or {}).get("phone") or "")
        destino = destino if len(destino) >= 12 else "55" + (destino if len(destino) == 11 else ph)
        text = msg.replace("[nome]", nome)
        if dry:
            print(f"[DRY] {turno} {nome} -> ...{destino[-4:]} ({'lead existente' if lead else 'NOVO'})"); enviados += 1; continue
        # 1) etiqueta + nota pra Clara
        nota = nota_tpl.format(turno=turno)
        if lead:
            tags = list(lead.get("tags") or [])
            if tag not in tags:
                tags.append(tag)
            meta = dict(lead.get("metadata") or {})
            meta.setdefault("campanha", CAMPANHA)
            meta["acao_hu"] = {"data": data, "turno": turno}
            ctx = lead.get("context") or ""
            api("leads", {"id": "eq." + lead["id"], "tenant_id": "eq." + TENANT}, "PATCH",
                {"tags": tags, "metadata": meta, "context": ctx + ("" if MARCA in ctx else nota)})
        else:
            lead = api("leads", method="POST", body={"tenant_id": TENANT, "name": nome, "phone": destino, "status": "lead",
                "source": "acao_hu", "tags": [tag], "metadata": {"campanha": CAMPANHA, "acao_hu": {"data": data, "turno": turno}},
                "context": nota.strip()})[0]
        # 2) espacamento anti-bloqueio 90-150s
        if last is not None:
            wait = 90 + random.uniform(0, 60) - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        if datetime.now(TZ).date().isoformat() != data or datetime.now(TZ).hour >= 20:
            print("Fora da janela (data da campanha ate 20h) — parando."); break
        # 3) envio
        r = requests.post(UAZ + "/send/text", headers={"token": token, "Content-Type": "application/json"},
                          json={"number": destino, "text": text}, timeout=60)
        last = time.monotonic()
        rdata = r.json() if r.ok else {}
        ok = bool(rdata.get("id") or rdata.get("messageid") or rdata.get("messageID"))
        print(f"[send] {datetime.now(TZ):%H:%M} {turno} {nome} -> ...{destino[-4:]} HTTP {r.status_code} {'ok' if ok else r.text[:160]}", flush=True)
        if not ok:
            falhas += 1; seguidas += 1
            log(f"Ação HU: falha ao enviar pra {nome}", f"HTTP {r.status_code}: {r.text[:200]}", "erro",
                {"disparo_key": key, "lead_id": lead["id"], "turno": turno})
            if seguidas >= 3:
                print("3 falhas seguidas — parando pra proteger a instancia."); break
            continue
        seguidas = 0; enviados += 1
        log(f"Ação HU: convite enviado pra {nome}",
            f"{nome} (turno {turno}) — voucher 30 dias pelo Whats 2000; etiqueta {tag} aplicada",
            "concluido", {"disparo_key": key, "lead_id": lead["id"], "turno": turno})
    print(f"\nResumo: {enviados} enviado(s), {pulados} pulado(s), {falhas} falha(s)", flush=True)
    if not dry and not only:
        log("Campanha Ação HU 25/09 — disparo concluído",
            f"{enviados} convites enviados nesta rodada, {pulados} pulados (já enviados/inelegíveis), {falhas} falhas. "
            f"{len(contacts)} contatos da lista (turnos 7h e 13h) etiquetados como {tag}.",
            "erro" if falhas and not enviados else "concluido")
    return 0


if __name__ == "__main__":
    sys.exit(main())
