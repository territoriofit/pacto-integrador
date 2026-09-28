# -*- coding: utf-8 -*-
"""
Analise semanal do atendimento — toda sexta no WhatsApp do Andre.

Plano de acao das vendas perdidas (Andre, 28/09/2026): "toda sexta-feira fazer
uma analise da semana e enviar no meu whats pelo Ceo Territorio mostrando onde
podemos melhorar".

O que sai:
  - 4 indicadores fixos, por consultora e no total, comparados com a semana
    anterior: preco enviado sem o lead pedir, convite com dia e hora, falta
    sem recontato e conversa sem retomada depois do silencio;
  - agenda da semana: agendados, vieram, faltaram;
  - motivos das perdas da semana e o que fazer diferente (sintese).

Semana = 7 dias fechados ate ontem (sexta a quinta quando roda na sexta).
Base: leads NOVOS da semana com conversa, sem candidato a vaga (inclusive os
do anuncio de vagas), sem quem ja era aluno e sem o que a leitura marcar como
"nao e venda". As conversas de quem nao matriculou sao lidas pelo Claude com o
mesmo criterio do relatorio mensal (relatorio_vendas_perdidas.py).

Os indicadores de cada semana ficam guardados no CRM (agent_activity,
campanha "relatorio-semanal-atendimento"); a comparacao usa o que foi guardado
na sexta anterior. Na primeira vez, le tambem a semana anterior.

Repo publico: o log so tem contagens.

Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO. Opcional: DATA_FIM (AAAA-MM-DD,
exclusivo; padrao hoje) | RELATORIO_PARA | MODELO | TETO_USD (padrao 8) |
MAX_CONVERSAS (teste) | DRY_RUN=1 (gera o PDF local, nao envia nem guarda) |
SAIDA_PDF.
"""

import io
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import requests

import relatorio_vendas_perdidas as base

CAMPANHA = "relatorio-semanal-atendimento"
TZ_SP = base.TZ_SP
MOTIVOS = base.MOTIVOS

ESQ_SINTESE = {
    "type": "object",
    "properties": {
        "resumo": {"type": "string"},
        "melhorias": {"type": "array", "items": {"type": "object", "properties": {
            "o_que": {"type": "string"}, "por_que": {"type": "string"},
            "como_fazer": {"type": "string"}},
            "required": ["o_que", "por_que", "como_fazer"],
            "additionalProperties": False}},
        "consultoras": {"type": "array", "items": {"type": "object", "properties": {
            "nome": {"type": "string"}, "ponto_forte": {"type": "string"},
            "ponto_a_corrigir": {"type": "string"}},
            "required": ["nome", "ponto_forte", "ponto_a_corrigir"],
            "additionalProperties": False}},
    },
    "required": ["resumo", "melhorias", "consultoras"],
    "additionalProperties": False,
}

SIS_SINTESE = """Você é analista comercial e escreve para o dono de uma academia (Território Fit, São Carlos/SP). Ele vai ler no celular, numa sexta-feira, para saber onde o atendimento pelo WhatsApp pode melhorar na semana seguinte. Escreva em português do Brasil, em frases completas e diretas, sem jargão, sem siglas e sem inglês. Não use nomes de clientes nem telefones.

Você recebe os indicadores da semana, os da semana anterior quando existem, e uma amostra de evidências e oportunidades apontadas conversa a conversa. Com base só nisso:
- "resumo": 3 a 4 frases. Comece pelo que mais pesou na semana e diga o que melhorou e o que piorou em relação à semana anterior, com os números.
- "melhorias": exatamente 3 pontos para a equipe trabalhar na semana seguinte, em ordem de impacto. Em cada um: o que mudar, por que (com o número da semana) e como fazer na prática, em uma ou duas frases.
- "consultoras": para cada consultora listada, um ponto forte e um ponto a corrigir sustentados pelos números dela. Se a amostra dela for pequena, diga isso.

Regras da casa: a sequência de venda é visita, aula experimental e só então a consultora fecha; o convite deve vir com dia e hora; preço não é enviado por iniciativa da academia; não proponha desconto nem promoção nova. Não invente número. Não repita a mesma ideia em dois lugares."""


def periodo() -> tuple[date, date]:
    fim_env = (os.environ.get("DATA_FIM") or "").strip()
    fim = date.fromisoformat(fim_env) if fim_env else datetime.now(TZ_SP).date()
    return fim - timedelta(days=7), fim


def agenda(key: str, ini: date, fim: date) -> dict:
    """Agendamentos com data dentro da semana: vieram, faltaram, recontato."""
    ags = base.sb_todos(key, "agendamentos", {
        "select": "id,lead_id,aula,consultor,data_agendamento,horario,veio,fechou",
        "data_agendamento": f"gte.{ini.isoformat()}",
        "and": f"(data_agendamento.lt.{fim.isoformat()})"})
    ags = [a for a in ags if (a.get("aula") or "") not in ("Currículo", "Ligação")]
    faltas = [a for a in ags if not a.get("veio") and not a.get("fechou")]
    sem_recontato = 0
    for a in faltas:
        if not a.get("lead_id"):
            sem_recontato += 1
            continue
        m = re.match(r"(\d{1,2})\D?(\d{2})?", a.get("horario") or "")
        h = min(int(m.group(1)), 23) if m else 12
        marco = datetime.fromisoformat(a["data_agendamento"]).replace(
            hour=h, tzinfo=TZ_SP)
        depois = base.sb_get(key, "whatsapp_messages", {
            "select": "id", "lead_id": f"eq.{a['lead_id']}", "group_id": "is.null",
            "is_from_me": "eq.true", "message_type": "neq.ai_tool_call",
            "sent_at": f"gte.{marco.isoformat()}", "limit": "1"})
        if not depois:
            sem_recontato += 1
    por = defaultdict(lambda: [0, 0])
    for a in ags:
        nome = (a.get("consultor") or "Sem consultora").strip()
        por[nome][0] += 1
        por[nome][1] += int(bool(a.get("veio") or a.get("fechou")))
    return {"agendados": len(ags), "vieram": len(ags) - len(faltas),
            "faltaram": len(faltas), "faltas_sem_recontato": sem_recontato,
            "por_consultora": {k: v for k, v in por.items()}}


def indicadores(univ: list, res: dict, ag: dict) -> dict:
    lidas = [{**res[u["id"]], "crm": u["desfecho"]} for u in univ if u["id"] in res]
    nao_venda = {u["id"] for u in univ
                 if u["id"] in res and res[u["id"]]["tipo"] != "venda"}
    venda = [u for u in univ if u["id"] not in nao_venda]
    V = [r for r in lidas if r["tipo"] == "venda"
         and r["desfecho_real"] != "matriculou_pela_conversa"]
    perdidas = [r for r in V if r["desfecho_real"] != "em_andamento"]
    silencio = [r for r in perdidas if r["ultima_mensagem_de"] == "equipe"]

    def sinais(sel: list) -> dict:
        n = len(sel)
        sil = [r for r in sel if r["ultima_mensagem_de"] == "equipe"
               and r["desfecho_real"] != "em_andamento"]
        return {
            "conversas": n,
            "preco_sem_pedir": round(base.pct(sum(
                r["preco_informado"] and not r["preco_pedido_pelo_lead"]
                for r in sel), n), 1),
            "convite_dia_hora": round(base.pct(sum(
                r["convite_com_dia_e_hora"] for r in sel), n), 1),
            "sem_retomada": round(base.pct(sum(
                r["followups_apos_silencio"] <= 0 for r in sil), len(sil)), 1),
            "silencio": len(sil),
        }

    cons = defaultdict(list)
    for r in V:
        nome = (r["consultora"] or "").strip().title()
        if nome and r["quem_atendeu"] != "clara":
            cons[nome].append(r)
    funil = Counter(u["desfecho"] for u in venda)
    return {
        "leads": len(venda), "nao_venda": len(nao_venda),
        "agendou_pct": round(base.pct(len(venda) - funil["NAO_AGENDOU"], len(venda)), 1),
        "matriculou_pct": round(base.pct(funil["MATRICULOU"], len(venda)), 1),
        "lidas": len(V), **sinais(V),
        "sem_resposta_da_equipe": round(base.pct(sum(
            r["ultima_mensagem_de"] == "lead" for r in perdidas), len(perdidas)), 1),
        "falta_pct": round(base.pct(ag["faltaram"], ag["agendados"]), 1),
        "falta_sem_recontato_pct": round(base.pct(
            ag["faltas_sem_recontato"], ag["faltaram"]), 1),
        "agenda": {k: ag[k] for k in ("agendados", "vieram", "faltaram",
                                      "faltas_sem_recontato")},
        "por_consultora": [{"nome": n, **sinais(s)} for n, s in
                           sorted(cons.items(), key=lambda x: -len(x[1]))
                           if len(s) >= 5],
        "motivos": [(k, v) for k, v in Counter(
            r["motivo_principal"] for r in perdidas).most_common(6)],
        "_V": V, "_perdidas": perdidas, "_silencio": len(silencio),
    }


def analisar(key: str, client, ini: date, fim: date, teto: float, maxc: int):
    leads, msgs, ag, ativos = base.extrair(key, ini.isoformat(), fim.isoformat())
    univ, fora = base.montar_universo(leads, msgs, ag, ativos)
    alvo = [u for u in univ if u["desfecho"] != "MATRICULOU"]
    if maxc:
        alvo = alvo[:maxc]
    print(f"[semana {ini:%d/%m} a {fim - timedelta(days=1):%d/%m}] "
          f"{len(univ)} leads de venda | fora: {dict(fora)} | lendo {len(alvo)}")
    res = base.classificar(client, alvo, teto)
    if alvo and len(res) < 0.5 * len(alvo):
        return None
    return indicadores(univ, res, agenda(key, ini, fim))


def guardado(key: str, ini: date) -> dict | None:
    linhas = base.sb_get(key, "agent_activity", {
        "select": "metadata", "metadata->>campanha": f"eq.{CAMPANHA}",
        "metadata->>semana_ini": f"eq.{ini.isoformat()}",
        "order": "created_at.desc", "limit": "1"})
    return (linhas[0]["metadata"] or {}).get("indicadores") if linhas else None


def publico(ind: dict) -> dict:
    return {k: v for k, v in ind.items() if not k.startswith("_")}


def sintetizar(client, ind: dict, ant: dict | None) -> dict | None:
    import json
    amostra = []
    for k, _ in ind["motivos"][:5]:
        sel = [r for r in ind["_perdidas"] if r["motivo_principal"] == k][:8]
        amostra.append({"motivo": MOTIVOS[k],
                        "evidencias": [r["evidencia"][:140] for r in sel],
                        "oportunidades": [r["oportunidade"][:160] for r in sel]})
    dados = {"semana": {**publico(ind), "motivos": [
                 {"motivo": MOTIVOS[k], "conversas": v} for k, v in ind["motivos"]]},
             "semana_anterior": ({k: v for k, v in ant.items() if k != "motivos"}
                                 if ant else "sem dados"),
             "amostra_por_motivo": amostra}
    for _ in range(3):
        r = base.chamar(client, SIS_SINTESE, json.dumps(dados, ensure_ascii=False),
                        ESQ_SINTESE, max_tokens=6000)
        if r:
            return r
    return None


LINHAS = [  # (chave, rotulo, quanto menor melhor)
    ("preco_sem_pedir", "Preço enviado sem o lead pedir", True),
    ("convite_dia_hora", "Convite com dia e hora definidos", False),
    ("falta_sem_recontato_pct", "Faltas sem nenhum recontato", True),
    ("sem_retomada", "Conversas sem retomada depois do silêncio", True),
    ("sem_resposta_da_equipe", "Última mensagem do lead ficou sem resposta", True),
    ("falta_pct", "Agendados que não vieram", True),
    ("agendou_pct", "Leads que agendaram", False),
    ("matriculou_pct", "Leads que matricularam", False),
]


def variacao(atual: float, antes, menor_melhor: bool) -> str:
    if antes is None:
        return "—"
    d = atual - antes
    if abs(d) < 1:
        return "igual"
    bom = (d < 0) == menor_melhor
    return "%s %.0f pontos (%s)" % ("caiu" if d < 0 else "subiu", abs(d),
                                    "melhorou" if bom else "piorou")


def gerar_pdf(ind: dict, ant: dict | None, s: dict | None,
              ini: date, fim: date) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    fonte, negr = "Helvetica", "Helvetica-Bold"
    for reg, bold in (("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                      (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf")):
        if os.path.exists(reg) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("Base", reg))
            pdfmetrics.registerFont(TTFont("Base-Bold", bold))
            pdfmetrics.registerFontFamily("Base", normal="Base", bold="Base-Bold")
            fonte, negr = "Base", "Base-Bold"
            break
    azul, cinza, borda = (colors.HexColor("#1F3A5F"), colors.HexColor("#F2F4F7"),
                          colors.HexColor("#C9CED6"))
    T = ParagraphStyle("t", fontName=negr, fontSize=19, leading=23, textColor=azul)
    S = ParagraphStyle("s", fontName=fonte, fontSize=9.5, leading=13,
                       textColor=colors.HexColor("#555555"))
    H = ParagraphStyle("h", fontName=negr, fontSize=13.5, leading=17, textColor=azul,
                       spaceBefore=12, spaceAfter=5)
    H3 = ParagraphStyle("h3", fontName=negr, fontSize=10.5, leading=14,
                        spaceBefore=6, spaceAfter=2)
    P = ParagraphStyle("p", fontName=fonte, fontSize=10, leading=14.5, spaceAfter=4)
    C = ParagraphStyle("c", fontName=fonte, fontSize=9, leading=12)
    CB = ParagraphStyle("cb", fontName=negr, fontSize=9, leading=12,
                        textColor=colors.white)

    def esc(t):
        return str(t if t is not None else "").replace("&", "&amp;").replace(
            "<", "&lt;").replace(">", "&gt;")

    def tabela(cab, linhas, larg):
        dados = [[Paragraph(esc(x), CB) for x in cab]] + \
                [[Paragraph(esc(x), C) for x in l] for l in linhas]
        t = Table(dados, colWidths=[w * mm for w in larg], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), azul),
            ("GRID", (0, 0), (-1, -1), 0.4, borda),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, cinza]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        return t

    def p0(v):
        return "—" if v is None else "%.0f%%" % v

    e = [Paragraph("Atendimento da semana", T),
         Paragraph("Território Fit · leads novos de %s a %s · gerado em %s" % (
             ini.strftime("%d/%m"), (fim - timedelta(days=1)).strftime("%d/%m/%Y"),
             datetime.now(TZ_SP).strftime("%d/%m/%Y")), S),
         Spacer(1, 8)]
    if s:
        e += [Paragraph("Resumo", H), Paragraph(esc(s["resumo"]), P)]
        for i, m in enumerate(s["melhorias"][:3], 1):
            cab = [Paragraph("Onde melhorar na próxima semana", H)] if i == 1 else []
            e.append(KeepTogether(cab + [
                Paragraph("%d. %s" % (i, esc(m["o_que"])), H3),
                Paragraph("<b>Por quê:</b> " + esc(m["por_que"]), P),
                Paragraph("<b>Como fazer:</b> " + esc(m["como_fazer"]), P)]))

    e += [KeepTogether([
              Paragraph("Indicadores da semana", H),
              tabela(["Indicador", "Semana", "Semana anterior", "Variação"],
                     [[rot, p0(ind[k]), p0((ant or {}).get(k)),
                       variacao(ind[k], (ant or {}).get(k), menor)]
                      for k, rot, menor in LINHAS], [80, 25, 30, 45])]),
          Spacer(1, 4),
          Paragraph("Base da semana: %d leads de venda com conversa; %d conversas de "
                    "quem não matriculou foram lidas; %d contatos não eram venda e "
                    "ficaram de fora." % (ind["leads"], ind["lidas"],
                                          ind["nao_venda"]), P)]

    a = ind["agenda"]
    e.append(KeepTogether([
        Paragraph("Agenda da semana", H),
        tabela(["Agendados", "Vieram", "Não vieram", "Faltas sem recontato"],
               [[a["agendados"], a["vieram"], a["faltaram"],
                 a["faltas_sem_recontato"]]], [45, 45, 45, 45]),
        Spacer(1, 4),
        Paragraph("A presença vem do cadastro de visitantes feito na recepção. "
                  "Quem veio e não foi cadastrado aparece como falta.", P)]))

    if ind["por_consultora"]:
        e.append(KeepTogether([
            Paragraph("Por consultora", H),
            tabela(["Consultora", "Conversas", "Preço sem pedir",
                    "Convite com dia e hora", "Sem retomada"],
                   [[x["nome"], x["conversas"], p0(x["preco_sem_pedir"]),
                     p0(x["convite_dia_hora"]),
                     p0(x["sem_retomada"]) if x["silencio"] else "—"]
                    for x in ind["por_consultora"]], [45, 30, 35, 40, 30])]))
        if s:
            for x in s["consultoras"]:
                e.append(KeepTogether([
                    Paragraph(esc(x["nome"]), H3),
                    Paragraph("<b>Ponto forte:</b> " + esc(x["ponto_forte"]), P),
                    Paragraph("<b>A corrigir:</b> " + esc(x["ponto_a_corrigir"]), P)]))

    if ind["motivos"]:
        n = len(ind["_perdidas"])
        e.append(KeepTogether([
            Paragraph("Motivos das perdas da semana (%d conversas)" % n, H),
            tabela(["Motivo", "Conversas", "Parcela"],
                   [[MOTIVOS[k], v, p0(base.pct(v, n))] for k, v in ind["motivos"]],
                   [110, 35, 35])]))
    exemplos = []
    for k, _ in ind["motivos"][:4]:
        ev = [r["evidencia"] for r in ind["_perdidas"]
              if r["motivo_principal"] == k and 12 < len(r["evidencia"]) < 140][:2]
        if ev:
            exemplos.append([MOTIVOS[k], " · ".join("“%s”" % x for x in ev)])
    if exemplos:
        e += [Paragraph("Trechos das conversas", H),
              tabela(["Motivo", "O que apareceu"], exemplos, [55, 125])]

    e += [Paragraph("Como ler", H),
          Paragraph("Os percentuais de preço, convite e retomada consideram as "
                    "conversas da semana de quem não matriculou, lidas por "
                    "inteligência artificial. Como a semana é curta, parte das "
                    "conversas ainda está em andamento e os números de uma única "
                    "consultora podem variar bastante de uma semana pra outra. "
                    "Vale mais a tendência de várias semanas do que um número "
                    "isolado.", P)]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title="Atendimento da semana", author="Território Fit")

    def rodape(cv, d):
        cv.saveState()
        cv.setFont(fonte, 8)
        cv.setFillColor(colors.HexColor("#777777"))
        cv.drawString(15 * mm, 8 * mm, "Território Fit · relatório interno")
        cv.drawRightString(195 * mm, 8 * mm, "Página %d" % d.page)
        cv.restoreState()

    doc.build(e, onFirstPage=rodape, onLaterPages=rodape)
    return buf.getvalue()


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    token = os.environ.get("UAZAPI_TOKEN_CEO", "").replace("﻿", "").strip()
    destino = (os.environ.get("RELATORIO_PARA") or "5516992290338").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    teto = float(os.environ.get("TETO_USD") or "8")
    maxc = int(os.environ.get("MAX_CONVERSAS") or "0")
    if not key or (not token and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_CEO")
        return 1
    import anthropic
    akey = base._anthropic_key(key)
    if not akey:
        print("ANTHROPIC_API_KEY ausente na config do CRM")
        return 1
    client = anthropic.Anthropic(api_key=akey, max_retries=5, timeout=300.0)

    ini, fim = periodo()
    ind = analisar(key, client, ini, fim, teto, maxc)
    if ind is None:
        print("[erro] leitura insuficiente")
        if not dry:
            base.enviar_texto(token, destino,
                              "Não consegui terminar a análise semanal do "
                              "atendimento: a leitura das conversas falhou. Veja o "
                              "saldo da API da Anthropic e me peça pra rodar de novo.")
        return 1
    ant = guardado(key, ini - timedelta(days=7))
    if ant is None and os.environ.get("SEM_COMPARACAO", "") != "1":
        print("[comparacao] semana anterior sem indicadores guardados — lendo agora")
        lido = analisar(key, client, ini - timedelta(days=7), ini, teto, maxc)
        ant = publico(lido) if lido else None
    s = sintetizar(client, ind, ant)
    print(f"[sintese] {'ok' if s else 'falhou — sai só com os números'} | custo "
          f"estimado US$ {base.custo_usd():.2f} | chamadas {base.USO['chamadas']}")
    pdf = gerar_pdf(ind, ant, s, ini, fim)
    print(f"[pdf] {len(pdf) // 1024} KB")
    if dry:
        with open(os.environ.get("SAIDA_PDF") or "atendimento-da-semana.pdf", "wb") as fh:
            fh.write(pdf)
        print("[DRY] PDF gravado localmente, nada enviado nem guardado.")
        return 0

    requests.post(f"{base.SUPABASE_URL}/rest/v1/agent_activity",
                  headers={**base._h(key), "Content-Type": "application/json",
                           "Prefer": "return=minimal"},
                  json={"agent_slug": "comercial-vendas",
                        "title": "Análise semanal do atendimento",
                        "detail": "Semana de %s a %s: %d leads, %d conversas lidas." % (
                            ini.strftime("%d/%m"),
                            (fim - timedelta(days=1)).strftime("%d/%m"),
                            ind["leads"], ind["lidas"]),
                        "status": "concluido",
                        "metadata": {"campanha": CAMPANHA,
                                     "semana_ini": ini.isoformat(),
                                     "semana_fim": fim.isoformat(),
                                     "indicadores": publico(ind),
                                     "custo_usd": round(base.custo_usd(), 2)}},
                  timeout=30)

    leg = ["*Atendimento da semana (%s a %s)*" % (
               ini.strftime("%d/%m"), (fim - timedelta(days=1)).strftime("%d/%m")),
           "%d leads de venda, %.0f%% agendaram." % (ind["leads"], ind["agendou_pct"]),
           "Preço sem o lead pedir: %.0f%%. Convite com dia e hora: %.0f%%." % (
               ind["preco_sem_pedir"], ind["convite_dia_hora"]),
           "Agenda: %d marcados, %d não vieram." % (
               ind["agenda"]["agendados"], ind["agenda"]["faltaram"]),
           "Onde melhorar e números por consultora no PDF."]
    nome = "atendimento-semana-%s.pdf" % (fim - timedelta(days=1)).strftime("%d-%m")
    if not base.enviar_pdf(token, destino, pdf, nome, "\n".join(leg)):
        base.enviar_texto(token, destino, "\n".join(leg[:-1])
                          + "\nNão consegui anexar o PDF desta semana.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
