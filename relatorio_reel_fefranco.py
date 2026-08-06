# -*- coding: utf-8 -*-
"""Relatório one-shot (07/08/2026): entrega do reel do Fê Franco (evento
14/08) nos 2 anúncios criados 06/08 à noite — manda no WhatsApp do André.

GUARDA DE DATA: só roda em 07/08/2026 BRT (o cron do Actions é anual;
fora da data o script sai limpo — pegadinha do quiz-activation de julho).

Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO. Opcional: ALERTA_PARA, FORCE_DATE=1.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

SB = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"
TZ_SP = timezone(timedelta(hours=-3))

ADS = {
    "120246860700380117": "TRINCA26 (frio 3km)",
    "120246860704140117": "Remarketing 3km",
}


def main() -> int:
    hoje = datetime.now(TZ_SP).date().isoformat()
    if hoje != "2026-08-07" and os.environ.get("FORCE_DATE") != "1":
        print(f"[guarda] hoje={hoje}, relatório é só de 2026-08-07 — saindo.")
        return 0

    key = os.environ["SUPABASE_KEY"].strip()
    sb = {"apikey": key, "Authorization": f"Bearer {key}"}
    raw = requests.get(
        f"{SB}/rest/v1/config",
        params={"select": "value", "key": "eq.META_ADS_TOKEN_WRITE"},
        headers=sb, timeout=30).json()[0]["value"]
    token = (json.loads(raw)["value"]
             if raw.lstrip().startswith("{") else raw.strip())

    linhas = []
    total_conv = 0
    for ad_id, rotulo in ADS.items():
        st = requests.get(
            f"https://graph.facebook.com/v21.0/{ad_id}",
            params={"fields": "effective_status",
                    "access_token": token}, timeout=30).json()
        ins = requests.get(
            f"https://graph.facebook.com/v21.0/{ad_id}/insights",
            params={"fields": "spend,impressions,reach,cpm,actions,"
                              "video_thruplay_watched_actions",
                    "date_preset": "maximum",
                    "access_token": token}, timeout=30).json()
        data = (ins.get("data") or [{}])[0]
        gasto = float(data.get("spend") or 0)
        imp = int(data.get("impressions") or 0)
        alcance = int(data.get("reach") or 0)
        cpm = float(data.get("cpm") or 0)
        conv = 0
        for a in data.get("actions") or []:
            if a.get("action_type") == "onsite_conversion.messaging_conversation_started_7d":
                conv = int(a.get("value") or 0)
        thru = 0
        for a in data.get("video_thruplay_watched_actions") or []:
            thru = int(a.get("value") or 0)
        total_conv += conv
        custo = f"R$ {gasto / conv:.2f}".replace(".", ",") if conv else "—"
        linhas.append(
            f"*{rotulo}* [{st.get('effective_status', '?')}]\n"
            f"  Gasto R$ {gasto:.2f} · {imp} impressões · {alcance} alcance\n"
            f"  CPM R$ {cpm:.2f} · {thru} thruplays\n"
            f"  💬 {conv} conversa(s) — custo/conversa: {custo}"
            .replace(".", ",", 1))

    msg = ("📊 *Entrega do reel Fê Franco (evento 14/08)* — desde ontem à "
           "noite:\n\n" + "\n\n".join(linhas) +
           f"\n\n💬 Total: {total_conv} conversa(s) iniciada(s)."
           "\n\nEvento é dia 14/08 — 7 dias de veiculação pela frente. "
           "Qualquer ajuste (verba, criativo), é só chamar o agente de "
           "tráfego. 🧡")

    tok = os.environ["UAZAPI_TOKEN_CEO"].strip()
    para = os.environ.get("ALERTA_PARA", "5516992290338").strip()
    r = requests.post(
        f"{UAZAPI_URL}/send/text",
        headers={"token": tok, "Content-Type": "application/json"},
        json={"number": para, "text": msg}, timeout=60)
    print(f"[whats] André ...{para[-4:]} HTTP {r.status_code}")

    requests.post(
        f"{SB}/rest/v1/agent_activity",
        headers={**sb, "Content-Type": "application/json",
                 "Prefer": "return=minimal"},
        json={"agent_slug": "marketing-trafego",
              "title": "Relatório do reel Fê Franco enviado ao André",
              "detail": f"Entrega dos 2 ads do evento 14/08: {total_conv} "
                        "conversa(s). Enviado no WhatsApp pessoal.",
              "status": "concluido"},
        timeout=30)
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
