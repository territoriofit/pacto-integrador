"""
Importa CANCELAMENTO.xlsx para a tabela cancelamentos do CRM.

Uso:
    python import_cancelamentos.py "C:\\caminho\\CANCELAMENTO.xlsx"

Idempotente: pula linhas que já existem (mesma matrícula + data do pedido).
Vincula lead pelo metadata.pacto_matricula (com e sem zeros à esquerda).
"""
import sys
from datetime import datetime, date

import openpyxl

from agente_integrador_pacto import CRMClient

CANON = {"kelytta": "Kellyta", "kellyta": "Kellyta", "kelly": "Kellyta",
         "nathalia": "Nathalia", "nathy": "Nathalia", "ntahy": "Nathalia",
         "raiane": "Raiane", "rai": "Raiane",
         "ly": "Lyandra", "lyandra": "Lyandra"}


def _num(v):
    if v is None or v == "" or (isinstance(v, str) and v.strip() in ("-", "")):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _data(v):
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return None


def main(path: str):
    crm = CRMClient()
    ws = openpyxl.load_workbook(path, data_only=True).active

    existentes = set()
    r = crm.sb.table("cancelamentos").select("matricula,data_pedido").execute()
    for x in r.data or []:
        existentes.add((str(x.get("matricula") or ""), x.get("data_pedido") or ""))

    rows, pulados, vinculados = [], 0, 0
    for row in ws.iter_rows(min_row=5):
        matricula = row[0].value
        nome = row[1].value
        if not nome or not str(nome).strip():
            continue
        matricula = str(int(matricula)) if isinstance(matricula, (int, float)) else (str(matricula).strip() or None)
        data_pedido = _data(row[7].value)
        if (str(matricula or ""), data_pedido or "") in existentes:
            pulados += 1
            continue

        dur = None
        if row[4].value:
            digitos = "".join(ch for ch in str(row[4].value) if ch.isdigit())
            dur = int(digitos) if digitos else None

        consultora = CANON.get(str(row[3].value or "").strip().lower())

        lead_id = None
        if matricula:
            rl = crm.sb.table("leads").select("id").or_(
                f"metadata->>pacto_matricula.eq.{matricula},"
                f"metadata->>pacto_matricula.eq.{matricula.zfill(6)}"
            ).limit(1).execute()
            if rl.data:
                lead_id = rl.data[0]["id"]
                vinculados += 1

        rows.append({
            "tenant_id": crm.tenant_id,
            "lead_id": lead_id,
            "matricula": matricula,
            "nome": str(nome).strip().title(),
            "professor": (str(row[2].value).strip().title() or None) if row[2].value else None,
            "consultora": consultora,
            "duracao_meses": dur,
            "forma_pagamento": (str(row[5].value).strip().lower() or None) if row[5].value else None,
            "data_compra": _data(row[6].value),
            "data_pedido": data_pedido,
            "mes_referencia": (data_pedido or "")[:7] or datetime.now().strftime("%Y-%m"),
            "valor_plano": _num(row[8].value),
            "total_pago": _num(row[9].value),
            "media_mensal": _num(row[10].value),
            "meses_cobrados": int(_num(row[11].value) or 0) or None,
            "total_cobrado": _num(row[12].value),
            "saldo_restante": _num(row[13].value),
            "multa_pct": _num(row[14].value),
            "valor_multa": _num(row[15].value),
            "retido": _num(row[16].value),
            "devolucao": _num(row[17].value),
            "autorizacao": (str(row[18].value).strip() or None) if row[18].value else None,
            "adquirente": (str(row[19].value).strip().lower() or None) if row[19].value else None,
            "motivo": (str(row[20].value).strip() or None) if row[20].value else None,
            "origem": "planilha",
        })

    for i in range(0, len(rows), 100):
        crm.sb.table("cancelamentos").insert(rows[i:i + 100]).execute()
    print(f"Importados: {len(rows)} | pulados (já existiam): {pulados} | com lead: {vinculados}")


if __name__ == "__main__":
    main(sys.argv[1])
