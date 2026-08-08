# -*- coding: utf-8 -*-
"""
Aviso WhatsApp dos stories de grade programados no Business Suite.

Roda nos horarios dos posts (dom 18h / seg-qui 21h BRT, via cron UTC do
workflow). Manda no WhatsApp do Andre qual story foi ao ar (com base no
calendario da leva programada — nao verifica na Meta). Perto do fim da
leva, avisa que precisa programar o mes seguinte; depois do fim, cobra.

Ao programar leva nova: atualizar a lista CAL abaixo e dar push.

Env: UAZAPI_TOKEN_CEO. Opcional: ALERTA_PARA (padrao Andre), DRY_RUN=1.
"""
import os
import sys
import datetime as dt

import requests

UAZAPI_URL = "https://territoriofit.uazapi.com"
DESTINO = os.environ.get("ALERTA_PARA", "5516992290338").strip()
DRY = os.environ.get("DRY_RUN", "").strip() == "1"

# (data do post AAAA-MM-DD, grade de qual dia, fundo, hora)
CAL = [
    ("2026-08-09", "segunda", "fachada vista de drone", "18:00"),
    ("2026-08-10", "terça",   "fachada vista de drone", "21:00"),
    ("2026-08-11", "quarta",  "fachada vista de drone", "21:00"),
    ("2026-08-12", "quinta",  "fachada vista de drone", "21:00"),
    ("2026-08-13", "sexta",   "fachada vista de drone", "21:00"),
    ("2026-08-16", "segunda", "musculação", "18:00"),
    ("2026-08-17", "terça",   "musculação", "21:00"),
    ("2026-08-18", "quarta",  "musculação", "21:00"),
    ("2026-08-19", "quinta",  "musculação", "21:00"),
    ("2026-08-20", "sexta",   "musculação", "21:00"),
    ("2026-08-23", "segunda", "bike indoor", "18:00"),
    ("2026-08-24", "terça",   "bike indoor", "21:00"),
    ("2026-08-25", "quarta",  "bike indoor", "21:00"),
    ("2026-08-26", "quinta",  "bike indoor", "21:00"),
    ("2026-08-27", "sexta",   "bike indoor", "21:00"),
    ("2026-08-30", "segunda", "dança", "18:00"),
    ("2026-08-31", "terça",   "dança", "21:00"),
    ("2026-09-01", "quarta",  "dança", "21:00"),
    ("2026-09-02", "quinta",  "dança", "21:00"),
    ("2026-09-03", "sexta",   "dança", "21:00"),
]


def envia(texto):
    if DRY:
        safe = texto.encode(sys.stdout.encoding or "utf-8", "replace").decode(
            sys.stdout.encoding or "utf-8", "replace")
        print("[dry-run] nao enviou:\n" + safe)
        return
    token = os.environ.get("UAZAPI_TOKEN_CEO", "").strip()
    if not token:
        print("Falta env UAZAPI_TOKEN_CEO")
        sys.exit(1)
    resp = requests.post(
        f"{UAZAPI_URL}/send/text",
        headers={"token": token, "Content-Type": "application/json"},
        json={"number": DESTINO, "text": texto},
        timeout=120)
    print(f"[whats] -> ...{DESTINO[-4:]} HTTP {resp.status_code}")


def main():
    agora = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=3)  # BRT
    hoje = agora.date()
    cal = {dt.date.fromisoformat(d): (dia, fundo, hora) for d, dia, fundo, hora in CAL}
    ultimo = max(cal)

    if hoje in cal:
        dia, fundo, hora = cal[hoje]
        linhas = [
            "📱 *Story da grade no ar!*",
            f"Grade de *{dia}* acabou de ser publicada ({hora}, FB + IG).",
            f"Fundo desta semana: {fundo}.",
        ]
        faltam = (ultimo - hoje).days
        if faltam == 0:
            linhas += ["", "🚨 Esse era o *ÚLTIMO story da leva*! Me chama no Claude "
                           "pra programar o próximo mês (leva de setembro)."]
        elif faltam <= 3:
            linhas += ["", f"⚠️ A leva programada acaba em {ultimo:%d/%m}. "
                           "Bom já me chamar no Claude pra programar o próximo mês."]
        envia("\n".join(linhas))
        return

    # sem story programado hoje
    if hoje > ultimo and agora.weekday() in (6, 0, 1, 2, 3):   # dom-qui
        envia("🚨 *Sem story de grade programado pra hoje!* A leva acabou em "
              f"{ultimo:%d/%m} e a nova ainda não foi programada. "
              "Me chama no Claude Code: \"programa os stories do mês\".")
    else:
        print("Nada a avisar hoje.")


if __name__ == "__main__":
    main()
