# -*- coding: utf-8 -*-
"""
Relatorio "por que perdemos vendas e agendamentos" — roda inteiro na nuvem.

Pedido do Andre (28/09/2026): analisar as conversas do inbox do CRM, apontar o
principal motivo das vendas perdidas e da perda de agendamento dos leads, e
mandar o relatorio em PDF no WhatsApp dele pelo numero "Ceo Territorio Digital".

Etapas:
  1. Le do CRM os leads NOVOS do periodo (created_at entre DATA_INI e DATA_FIM)
     com pelo menos 1 mensagem recebida, tirando candidato a vaga e quem ja era
     aluno. Desfecho: matriculou / agendou sem matricular / nao agendou.
  2. Cada conversa perdida e lida inteira pelo Claude (modelo escolhido pelo
     Andre: claude-opus-5) e classificada com saida JSON estruturada.
  3. Consolida os numeros e pede uma sintese (mesmo modelo).
  4. Monta o PDF (reportlab) e envia pelo UazAPI (instancia Ceo).

O repo e PUBLICO: o log so mostra contagens. Nada de nome, telefone ou trecho
de conversa no log, e nenhum arquivo e publicado como artefato.

Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO. Opcional: RELATORIO_PARA | DATA_INI |
DATA_FIM (AAAA-MM-DD) | MODELO | MAX_CONVERSAS (teste) | TETO_USD (padrao 25) |
SO_TESTE_ENVIO=1 (manda um PDF de teste e sai) | DRY_RUN=1 (nao envia).
"""

import base64
import io
import json
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"
TZ_SP = timezone(timedelta(hours=-3))
MODELO = os.environ.get("MODELO") or "claude-opus-5"
PRECO = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
         "claude-haiku-4-5": (1.0, 5.0)}  # US$ por milhao de tokens (entrada, saida)

MOTIVOS = {
    "sumiu_apos_preco": "Recebeu o preço e parou de responder",
    "sumiu_apos_primeira_resposta": "Parou logo após a primeira resposta",
    "sumiu_apos_convite": "Foi convidado a visitar e não respondeu mais",
    "sem_resposta_da_equipe": "Ficou sem resposta ou a resposta demorou",
    "vou_pensar_sem_recontato": "Disse que ia pensar e ninguém retomou",
    "preco_caro": "Achou caro",
    "forma_de_pagamento": "Forma de pagamento (cartão, pix, fidelidade)",
    "distancia": "Mora longe",
    "horario_rotina": "Horário ou rotina não encaixa",
    "modalidade_nao_oferecida": "Queria algo que a academia não oferece",
    "convenio_agregador": "Wellhub, Gympass ou TotalPass",
    "concorrente": "Fechou com outra academia",
    "adiou": "Deixou para começar depois",
    "diaria_ou_curto_prazo": "Queria diária ou período curto",
    "no_show": "Agendou e não apareceu",
    "visitou_nao_fechou": "Veio e não fechou",
    "so_curiosidade": "Só curiosidade, sem intenção clara",
    "outro": "Outro motivo",
}
ORIGENS = {
    "anuncio_instagram": "Anúncio no Instagram", "anuncio_facebook": "Anúncio no Facebook",
    "anuncio_meta": "Anúncio Meta", "link_whatsapp": "Link do WhatsApp",
    "whatsapp_espontaneo": "WhatsApp espontâneo", "whatsapp_manual": "Contato iniciado pela equipe",
    "link_site": "Site", "pacto_visitante": "Visitante cadastrado na recepção",
    "instagram_direct": "Direct do Instagram", "facebook_messenger": "Messenger",
    "acao_hu": "Ação no HU", "parceria": "Parceria",
}

_lock = threading.Lock()
USO = {"in": 0, "out": 0, "cache": 0, "chamadas": 0, "falhas": 0}


# ------------------------------------------------------------------ CRM
def _h(key):
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def sb_get(key, tabela, params, faixa=None):
    h = _h(key)
    if faixa:
        h["Range"] = faixa
    for tent in range(4):
        try:
            r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabela}", params=params,
                             headers=h, timeout=120)
            r.raise_for_status()
            return r.json()
        except Exception:
            if tent == 3:
                raise
            time.sleep(3 * (tent + 1))


def sb_todos(key, tabela, params, teto=200000):
    out = []
    for ini in range(0, teto, 1000):
        lote = sb_get(key, tabela, params, f"{ini}-{ini + 999}")
        out += lote
        if len(lote) < 1000:
            break
    return out


def hora(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ_SP)


def quem(m):
    if not m["is_from_me"]:
        return "LEAD"
    if m.get("sb") == "ai_agent":
        return "CLARA"
    mm = re.match(r"^\*([A-Za-zÀ-ú]+):\*", m.get("content") or "")
    return "CONSULTORA " + mm.group(1) if mm else "EQUIPE"


def extrair(key, ini, fim):
    leads = sb_todos(key, "leads", {
        "select": "id,name,phone,status,source,tags,created_at,"
                  "camp:metadata->origem_whatsapp->>campanha_nome",
        "created_at": f"gte.{ini}T03:00:00Z",
        "and": f"(created_at.lt.{fim}T03:00:00Z)", "order": "created_at"})
    ids = [l["id"] for l in leads]
    msgs = []
    for i in range(0, len(ids), 40):
        msgs += sb_todos(key, "whatsapp_messages", {
            "select": "lead_id,is_from_me,content,message_type,created_at,"
                      "sb:metadata->>sent_by",
            "lead_id": "in.(%s)" % ",".join(ids[i:i + 40]),
            "group_id": "is.null", "order": "created_at.asc"}, teto=50000)
    ag = []
    for i in range(0, len(ids), 80):
        ag += sb_get(key, "agendamentos", {
            "select": "lead_id,aula,data_agendamento,horario,consultor,veio,fechou",
            "lead_id": "in.(%s)" % ",".join(ids[i:i + 80])})
    ativos = {re.sub(r"\D", "", x["phone8"] or "")[-8:]
              for x in sb_todos(key, "pacto_alunos_ativos", {"select": "phone8"}, 10000)}
    print(f"[crm] leads {len(leads)} | mensagens {len(msgs)} | agendamentos {len(ag)}")
    return leads, msgs, ag, ativos


def montar_universo(leads, msgs, ag, ativos):
    por, ags = defaultdict(list), defaultdict(list)
    for m in msgs:
        por[m["lead_id"]].append(m)
    for a in ag:
        ags[a["lead_id"]].append(a)
    univ, fora = [], Counter()
    for l in leads:
        ms = por.get(l["id"], [])
        rec = [m for m in ms if not m["is_from_me"]]
        if not rec:
            fora["sem mensagem do lead"] += 1
            continue
        txt = " ".join((m.get("content") or "") for m in rec).lower()
        if (any(t.startswith("CV") for t in (l["tags"] or []))
                or any((a.get("aula") or "") == "Currículo" for a in ags.get(l["id"], []))
                # anuncio de vagas de emprego: o lead so diz "quero mais informacoes"
                or "vaga" in (l.get("camp") or "").lower()
                or re.search(r"curr[ií]culo|vaga de|vaga para|vagas de emprego|est[aá]gio", txt)):
            fora["candidato a vaga"] += 1
            continue
        if l["source"] in ("pacto_aluno", "teste_interno"):
            fora["já era aluno"] += 1
            continue
        a_lead = [a for a in ags.get(l["id"], []) if (a.get("aula") or "") != "Currículo"]
        l8 = re.sub(r"\D", "", l["phone"] or "")[-8:]
        virou = (l["status"] in ("cliente", "inadimplente") or l8 in ativos
                 or any(a.get("fechou") is True for a in a_lead))
        desf = "MATRICULOU" if virou else ("AGENDOU_SEM_MATRICULA" if a_lead else "NAO_AGENDOU")
        linhas = []
        for m in ms:
            c = re.sub(r"\s+", " ", (m.get("content") or "").strip()) or \
                "[%s]" % (m.get("message_type") or "midia")
            if c.startswith("🔧"):
                c = c[:160]
            linhas.append("[%s] %s: %s" % (hora(m["created_at"]).strftime("%d/%m %H:%M"),
                                           quem(m), c[:380]))
        if len(linhas) > 70:
            linhas = linhas[:45] + ["[... %d mensagens omitidas ...]" % (len(linhas) - 65)] \
                + linhas[-20:]
        pri = rec[0]
        hum = next((m for m in ms if m["is_from_me"] and hora(m["created_at"]) >= hora(pri["created_at"])
                    and m.get("sb") != "ai_agent"
                    and not (m.get("content") or "").startswith("🔧")), None)
        univ.append({
            "id": l["id"][:8], "origem": l["source"] or "sem origem", "desfecho": desf,
            "criado": hora(l["created_at"]).strftime("%d/%m"),
            "agend": [{"data": a["data_agendamento"], "hora": a["horario"], "aula": a["aula"],
                       "veio": a["veio"], "fechou": a["fechou"]} for a in a_lead],
            "hora_1a": hora(pri["created_at"]).hour,
            "min_humano": ((hora(hum["created_at"]) - hora(pri["created_at"])).total_seconds() / 60
                           if hum else None),
            "conversa": linhas})
    return univ, fora


# ------------------------------------------------------------------ Claude
SISTEMA = """Você analisa conversas de WhatsApp entre uma academia (Território Fit, São Carlos/SP) e pessoas interessadas que NÃO se matricularam. Seu trabalho é dizer, com base só no que está escrito, por que aquela venda ou aquele agendamento se perdeu.

Como ler a conversa: cada linha começa com data e hora (horário de Brasília) e o remetente. LEAD é o cliente. CLARA é a assistente automática da academia. "CONSULTORA <nome>" é uma atendente humana escrevendo pelo sistema. EQUIPE é uma atendente humana escrevendo pelo celular. Linhas que começam com "🔧" são registros de ação da Clara (agendou visita, transferiu para humano).

Contexto do negócio: a sequência de venda é visita → aula experimental grátis → consultora fecha a matrícula. A academia funciona segunda a quinta das 5h às 23h, sexta das 5h às 22h, sábado das 8h às 17h e domingo das 8h às 13h.

Regras de julgamento:
- Não invente. Se a conversa não permite saber o motivo, use "outro" e diga isso na evidência.
- "sumiu_apos_preco" só vale se a academia de fato informou valores antes de o lead parar de responder.
- "sem_resposta_da_equipe" vale quando a última mensagem relevante é do lead e ficou sem resposta, ou quando a resposta demorou mais de 3 horas dentro do horário de funcionamento e o lead esfriou.
- "vou_pensar_sem_recontato" vale quando o lead disse que ia pensar, ver ou falar com alguém e a academia não retomou de forma pessoal.
- Se o cabeçalho informa agendamento mas a conversa não mostra se a pessoa veio, use desfecho "agendou_sem_informacao"; use motivo "no_show" só se houver indício de falta.
- "tipo" é "nao_e_venda" quando a pessoa era aluno atual, ex-aluno tratando de cobrança ou cancelamento, candidato a emprego, fornecedor, parceiro, engano ou spam.
- "em_andamento" vale quando a conversa ainda está viva nos 3 dias finais do período analisado.
- "matriculou_pela_conversa" vale quando a conversa mostra que a pessoa acabou se matriculando.
- "followups_apos_silencio" é o número de mensagens de retomada que a academia mandou depois que o lead parou de responder.
- "convite_antes_do_preco" é verdadeiro quando, antes do primeiro preço (ou na mesma mensagem ou no mesmo bloco de mensagens seguidas), a academia ou a Clara já tinha tentado agendar: convidou para visita ou aula experimental, perguntou dia ou horário para a pessoa vir, ou aceitou o pedido de agendamento do lead avançando para marcar. Perguntar só "você já conhece a academia?" não é convite. Também é verdadeiro quando a conversa mostra que o lead já tinha visitado a academia ou sido atendido presencialmente antes de receber o preço. Se nenhum preço foi informado, use falso.
- "evidencia": trecho curto e literal da conversa, de até 15 palavras, que sustenta o motivo. Prefira fala do lead. Não inclua nome nem telefone.
- "oportunidade": uma frase curta dizendo o que poderia ter salvado a conversa, ou "nenhuma" se a perda era inevitável.
- "consultora": primeiro nome da atendente humana principal, ou texto vazio se não houve."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["venda", "nao_e_venda"]},
        "desfecho_real": {"type": "string", "enum": [
            "nao_agendou", "agendou_nao_veio", "veio_nao_fechou", "agendou_sem_informacao",
            "matriculou_pela_conversa", "em_andamento"]},
        "motivo_principal": {"type": "string", "enum": list(MOTIVOS)},
        "motivo_secundario": {"type": "string", "enum": list(MOTIVOS) + ["nenhum"]},
        "quem_atendeu": {"type": "string", "enum": ["clara", "consultora", "ambos", "ninguem"]},
        "consultora": {"type": "string"},
        "preco_informado": {"type": "boolean"},
        "preco_pedido_pelo_lead": {"type": "boolean"},
        "convite_com_dia_e_hora": {"type": "boolean"},
        "convite_antes_do_preco": {"type": "boolean"},
        "followups_apos_silencio": {"type": "integer"},
        "ultima_mensagem_de": {"type": "string", "enum": ["lead", "equipe"]},
        "evidencia": {"type": "string"},
        "oportunidade": {"type": "string"},
    },
    "required": ["tipo", "desfecho_real", "motivo_principal", "motivo_secundario",
                 "quem_atendeu", "consultora", "preco_informado", "preco_pedido_pelo_lead",
                 "convite_com_dia_e_hora", "convite_antes_do_preco",
                 "followups_apos_silencio", "ultima_mensagem_de",
                 "evidencia", "oportunidade"],
    "additionalProperties": False,
}


def _anthropic_key(key):
    rows = sb_get(key, "config", {"select": "value", "key": "eq.ANTHROPIC_API_KEY", "limit": "1"})
    return (rows[0].get("value") or "").replace("﻿", "").strip() if rows else ""


def custo_usd():
    pi, po = PRECO.get(MODELO, (5.0, 25.0))
    return (USO["in"] * pi + USO["cache"] * pi * 0.1 + USO["out"] * po) / 1e6


def _conta(resp):
    with _lock:
        u = resp.usage
        USO["in"] += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        USO["cache"] += getattr(u, "cache_read_input_tokens", 0) or 0
        USO["out"] += u.output_tokens or 0
        USO["chamadas"] += 1


def chamar(client, sistema, usuario, esquema, max_tokens=4000):
    """Uma chamada com saida JSON estruturada. Devolve dict ou None."""
    import anthropic
    try:
        resp = client.messages.create(
            model=MODELO, max_tokens=max_tokens,
            system=[{"type": "text", "text": sistema,
                     "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": esquema}},
            messages=[{"role": "user", "content": usuario}])
    except anthropic.RateLimitError:
        time.sleep(30)
        return None
    except anthropic.APIStatusError as e:
        print(f"[claude] erro {e.status_code}: {str(e.message)[:120]}")
        if e.status_code >= 500:  # sobrecarga passageira: espera antes da nova tentativa
            time.sleep(15)
        return None
    except anthropic.APIConnectionError:
        return None
    _conta(resp)
    if resp.stop_reason in ("refusal", "max_tokens"):
        print(f"[claude] parou por {resp.stop_reason}")
        return None
    texto = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return None


def classificar(client, perdidas, teto_usd):
    res, parou = {}, False

    def uma(u):
        if custo_usd() > teto_usd:
            return u["id"], None
        cab = ("CONVERSA | lead criado %s | origem %s | desfecho no CRM %s | agendamentos: %s\n"
               % (u["criado"], u["origem"], u["desfecho"],
                  json.dumps(u["agend"], ensure_ascii=False) if u["agend"] else "nenhum"))
        for _ in range(3):
            r = chamar(client, SISTEMA, cab + "\n".join(u["conversa"]), ESQUEMA)
            if r:
                return u["id"], r
        return u["id"], None

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(uma, u) for u in perdidas]
        for n, f in enumerate(as_completed(futs), 1):
            i, r = f.result()
            if r:
                res[i] = r
            else:
                USO["falhas"] += 1
            if n % 50 == 0:
                print(f"[claude] {n}/{len(perdidas)} lidas | custo estimado US$ {custo_usd():.2f}")
            if custo_usd() > teto_usd and not parou:
                parou = True
                print(f"[claude] teto de US$ {teto_usd:.0f} atingido — parando as leituras.")
    return res


# ------------------------------------------------------------------ numeros
def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def consolidar(univ, res):
    R = []
    for u in univ:
        if u["id"] in res:
            R.append({**res[u["id"]], "id": u["id"], "origem": u["origem"], "crm": u["desfecho"]})
    V = [r for r in R if r["tipo"] == "venda"
         and r["desfecho_real"] not in ("matriculou_pela_conversa", "em_andamento")]
    NA = [r for r in V if r["crm"] == "NAO_AGENDOU"]
    AG = [r for r in V if r["crm"] == "AGENDOU_SEM_MATRICULA"]

    def motivos(sel):
        c = Counter(r["motivo_principal"] for r in sel)
        return [(k, v, pct(v, len(sel))) for k, v in c.most_common()]

    def sinais(sel):
        n = len(sel)
        return {
            "n": n,
            "preco_informado": pct(sum(r["preco_informado"] for r in sel), n),
            "preco_sem_pedir": pct(sum(r["preco_informado"] and not r["preco_pedido_pelo_lead"]
                                       for r in sel), n),
            "convite_dia_hora": pct(sum(r["convite_com_dia_e_hora"] for r in sel), n),
            "sem_followup": pct(sum(r["followups_apos_silencio"] <= 0 for r in sel), n),
            "ultima_do_lead": pct(sum(r["ultima_mensagem_de"] == "lead" for r in sel), n),
        }

    cons = defaultdict(list)
    for r in V:
        nome = (r["consultora"] or "").strip().title()
        if nome and r["quem_atendeu"] != "clara":
            cons[nome].append(r)
    por_cons = []
    for nome, sel in sorted(cons.items(), key=lambda x: -len(x[1])):
        if len(sel) >= 10:
            por_cons.append({"nome": nome, **sinais(sel),
                             "motivos": motivos(sel)[:3]})

    # funil, origem e horario so com quem era venda: o que a leitura marcou como
    # "nao e venda" (aluno, fornecedor, candidato que escapou do filtro) sai
    nao_venda = {r["id"] for r in R if r["tipo"] != "venda"}
    univ = [u for u in univ if u["id"] not in nao_venda]

    orig = defaultdict(lambda: Counter())
    for u in univ:
        orig[u["origem"]][u["desfecho"]] += 1
    por_origem = []
    for o, c in sorted(orig.items(), key=lambda x: -sum(x[1].values())):
        n = sum(c.values())
        if n >= 10:
            por_origem.append({"origem": ORIGENS.get(o, o), "n": n,
                               "agendou": pct(n - c["NAO_AGENDOU"], n),
                               "matriculou": pct(c["MATRICULOU"], n)})

    horas = []
    for nome, a, b in (("5h às 8h", 5, 8), ("8h às 12h", 8, 12), ("12h às 14h", 12, 14),
                       ("14h às 18h", 14, 18), ("18h às 21h", 18, 21), ("21h às 24h", 21, 24)):
        sel = [u for u in univ if a <= u["hora_1a"] < b]
        mh = [u["min_humano"] for u in sel if u["min_humano"] is not None]
        if sel:
            horas.append({"faixa": nome, "n": len(sel),
                          "agendou": pct(sum(u["desfecho"] != "NAO_AGENDOU" for u in sel), len(sel)),
                          "mediana_min": statistics.median(mh) if mh else None})
    mh_all = [u["min_humano"] for u in univ if u["min_humano"] is not None]
    return {
        "R": R, "V": V, "NA": NA, "AG": AG,
        "funil": Counter(u["desfecho"] for u in univ), "total": len(univ),
        "nao_venda": sum(r["tipo"] != "venda" for r in R),
        "matriculou_conversa": sum(r["desfecho_real"] == "matriculou_pela_conversa" for r in R),
        "em_andamento": sum(r["desfecho_real"] == "em_andamento" for r in R),
        "motivos_na": motivos(NA), "motivos_ag": motivos(AG), "motivos_todos": motivos(V),
        "desfecho_ag": Counter(r["desfecho_real"] for r in AG),
        "sinais": sinais(V), "sinais_na": sinais(NA),
        "por_cons": por_cons, "por_origem": por_origem, "horas": horas,
        "mediana_humano": statistics.median(mh_all) if mh_all else None,
        "acima_2h": sum(1 for x in mh_all if x > 120), "com_resposta": len(mh_all),
    }


# ------------------------------------------------------------------ sintese
ESQ_SINTESE = {
    "type": "object",
    "properties": {
        "resumo": {"type": "string"},
        "motivo_agendamento": {"type": "object", "properties": {
            "titulo": {"type": "string"}, "explicacao": {"type": "string"}},
            "required": ["titulo", "explicacao"], "additionalProperties": False},
        "motivo_venda": {"type": "object", "properties": {
            "titulo": {"type": "string"}, "explicacao": {"type": "string"}},
            "required": ["titulo", "explicacao"], "additionalProperties": False},
        "achados": {"type": "array", "items": {"type": "object", "properties": {
            "titulo": {"type": "string"}, "explicacao": {"type": "string"}},
            "required": ["titulo", "explicacao"], "additionalProperties": False}},
        "recomendacoes": {"type": "array", "items": {"type": "object", "properties": {
            "acao": {"type": "string"}, "motivo": {"type": "string"},
            "como_medir": {"type": "string"}},
            "required": ["acao", "motivo", "como_medir"], "additionalProperties": False}},
        "consultoras": {"type": "array", "items": {"type": "object", "properties": {
            "nome": {"type": "string"}, "ponto_forte": {"type": "string"},
            "ponto_a_corrigir": {"type": "string"}},
            "required": ["nome", "ponto_forte", "ponto_a_corrigir"],
            "additionalProperties": False}},
    },
    "required": ["resumo", "motivo_agendamento", "motivo_venda", "achados",
                 "recomendacoes", "consultoras"],
    "additionalProperties": False,
}

SIS_SINTESE = """Você é analista comercial e escreve para o dono de uma academia (Território Fit, São Carlos/SP). Ele vai ler no celular, sem ter acompanhado a análise. Escreva em português do Brasil, em frases completas e diretas, sem jargão, sem siglas e sem inglês. Não use nomes de clientes nem telefones.

Você recebe os números consolidados da leitura de conversas perdidas de WhatsApp e uma amostra de evidências e oportunidades apontadas conversa a conversa. Com base só nisso:
- "resumo": 3 a 5 frases com o que ele mais precisa saber, começando pelo principal motivo de perda.
- "motivo_agendamento": o principal motivo pelo qual o lead não chega a agendar. Título curto e explicação de 2 a 4 frases com os números.
- "motivo_venda": o principal motivo pelo qual quem agendou não matriculou. Mesmo formato. Se os dados de comparecimento forem fracos, diga isso.
- "achados": de 3 a 6 achados adicionais que os números sustentam.
- "recomendacoes": de 4 a 7 ações concretas, em ordem de impacto, cada uma com o motivo e como medir se funcionou. Respeite as regras da casa: a sequência é visita, aula experimental e só então a consultora fecha; conteúdo para cliente não cita dados internos; não proponha desconto nem promoção nova.
- "consultoras": para cada consultora listada nos números, um ponto forte e um ponto a corrigir, sustentados pelos dados dela. Se os dados não sustentam uma afirmação, escreva que a amostra é pequena.

Não invente número. Quando a amostra for pequena, diga. Não repita a mesma ideia em dois lugares."""


def sintetizar(client, c):
    def lst(m):
        return [{"motivo": MOTIVOS[k], "conversas": v, "percentual": round(p, 1)} for k, v, p in m]
    amostra = []
    for k, _, _ in c["motivos_todos"][:8]:
        sel = [r for r in c["V"] if r["motivo_principal"] == k][:14]
        amostra.append({"motivo": MOTIVOS[k],
                        "evidencias": [r["evidencia"][:140] for r in sel],
                        "oportunidades": [r["oportunidade"][:160] for r in sel]})
    dados = {
        "leads_analisados": c["total"], "funil": dict(c["funil"]),
        "conversas_perdidas_lidas": len(c["R"]), "nao_eram_venda": c["nao_venda"],
        "vendas_perdidas_de_fato": len(c["V"]),
        "motivos_de_quem_nao_agendou": lst(c["motivos_na"]),
        "motivos_de_quem_agendou_e_nao_matriculou": lst(c["motivos_ag"]),
        "desfecho_de_quem_agendou": dict(c["desfecho_ag"]),
        "sinais_nas_perdidas_percentual": c["sinais"],
        "sinais_em_quem_nao_agendou_percentual": c["sinais_na"],
        "por_consultora": [{"nome": x["nome"], "conversas_perdidas": x["n"],
                            "preco_sem_o_lead_pedir_pct": round(x["preco_sem_pedir"], 1),
                            "convite_com_dia_e_hora_pct": round(x["convite_dia_hora"], 1),
                            "sem_retomada_apos_silencio_pct": round(x["sem_followup"], 1),
                            "principais_motivos": lst(x["motivos"])} for x in c["por_cons"]],
        "por_origem": c["por_origem"], "por_horario_da_primeira_mensagem": c["horas"],
        "mediana_minutos_ate_resposta_humana": c["mediana_humano"],
        "amostra_por_motivo": amostra,
    }
    for _ in range(3):
        r = chamar(client, SIS_SINTESE, json.dumps(dados, ensure_ascii=False),
                   ESQ_SINTESE, max_tokens=12000)
        if r:
            return r
    return None


# ------------------------------------------------------------------ PDF
def gerar_pdf(c, s, ini, fim, lidas, esperadas):
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

    azul, cinza, borda = colors.HexColor("#1F3A5F"), colors.HexColor("#F2F4F7"), colors.HexColor("#C9CED6")
    T = ParagraphStyle("t", fontName=negr, fontSize=19, leading=23, textColor=azul)
    S = ParagraphStyle("s", fontName=fonte, fontSize=9.5, leading=13, textColor=colors.HexColor("#555555"))
    H = ParagraphStyle("h", fontName=negr, fontSize=13.5, leading=17, textColor=azul,
                       spaceBefore=12, spaceAfter=5)
    H3 = ParagraphStyle("h3", fontName=negr, fontSize=10.5, leading=14, spaceBefore=6, spaceAfter=2)
    P = ParagraphStyle("p", fontName=fonte, fontSize=10, leading=14.5, spaceAfter=4)
    C = ParagraphStyle("c", fontName=fonte, fontSize=9, leading=12)
    CB = ParagraphStyle("cb", fontName=negr, fontSize=9, leading=12, textColor=colors.white)

    def esc(t):
        return (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def tabela(cab, linhas, larg):
        dados = [[Paragraph(esc(x), CB) for x in cab]] + \
                [[Paragraph(esc(x), C) for x in l] for l in linhas]
        t = Table(dados, colWidths=[w * mm for w in larg], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), azul), ("GRID", (0, 0), (-1, -1), 0.4, borda),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, cinza]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        return t

    def p1(v):
        return ("%.0f%%" % v)

    def destaque(titulo, texto):
        t = Table([[Paragraph("<b>%s</b><br/>%s" % (esc(titulo), esc(texto)), P)]],
                  colWidths=[180 * mm])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E8F0FA")),
                               ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#9DB7DA")),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 6),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        return t

    f = c["funil"]
    e = [Paragraph("Por que perdemos vendas e agendamentos", T),
         Paragraph("Território Fit · conversas de WhatsApp de leads novos de %s a %s · "
                   "gerado em %s" % (ini, fim, datetime.now(TZ_SP).strftime("%d/%m/%Y")), S),
         Spacer(1, 8)]
    if s:
        e += [Paragraph("Resumo", H), Paragraph(esc(s["resumo"]), P),
              Spacer(1, 4),
              destaque("Principal motivo de não agendar: " + s["motivo_agendamento"]["titulo"],
                       s["motivo_agendamento"]["explicacao"]),
              Spacer(1, 6),
              destaque("Principal motivo de agendar e não matricular: " + s["motivo_venda"]["titulo"],
                       s["motivo_venda"]["explicacao"])]

    e += [Paragraph("O que foi analisado", H),
          tabela(["Desfecho", "Leads", "Parcela"], [
              ["Não agendou", f["NAO_AGENDOU"], p1(pct(f["NAO_AGENDOU"], c["total"]))],
              ["Agendou e não matriculou", f["AGENDOU_SEM_MATRICULA"],
               p1(pct(f["AGENDOU_SEM_MATRICULA"], c["total"]))],
              ["Matriculou", f["MATRICULOU"], p1(pct(f["MATRICULOU"], c["total"]))],
              ["Total de leads de venda com conversa", c["total"], "100%"]], [90, 45, 45]),
          Spacer(1, 4),
          Paragraph("Das %d conversas perdidas, %d foram lidas por inteiro. %d não eram venda "
                    "(aluno, fornecedor, engano) e já estão fora da tabela acima, %d "
                    "mostravam que a pessoa acabou se "
                    "matriculando e %d ainda estavam em andamento. Restaram %d vendas "
                    "perdidas de fato, que são a base dos motivos abaixo."
                    % (esperadas, lidas, c["nao_venda"], c["matriculou_conversa"],
                       c["em_andamento"], len(c["V"])), P)]

    def bloco_motivos(titulo, lista, n):
        if not lista:
            return []
        return [Paragraph("%s (%d conversas)" % (titulo, n), H),
                tabela(["Motivo", "Conversas", "Parcela"],
                       [[MOTIVOS[k], v, p1(p)] for k, v, p in lista if v >= 1][:12],
                       [110, 35, 35])]

    e += bloco_motivos("Por que o lead não agenda", c["motivos_na"], len(c["NA"]))
    e += bloco_motivos("Por que quem agendou não matriculou", c["motivos_ag"], len(c["AG"]))
    if c["AG"]:
        d = c["desfecho_ag"]
        e.append(Paragraph(
            "Entre os que agendaram, não vieram: %d; vieram e não fecharam: %d; "
            "a conversa não mostra se a pessoa veio: %d." % (
                d["agendou_nao_veio"], d["veio_nao_fechou"], d["agendou_sem_informacao"]), P))

    sn = c["sinais"]
    e += [Paragraph("Como foi o atendimento nas conversas perdidas", H),
          tabela(["Sinal", "Parcela das perdidas"], [
              ["A academia informou preço", p1(sn["preco_informado"])],
              ["Informou preço sem o lead ter pedido", p1(sn["preco_sem_pedir"])],
              ["Convidou com dia e horário definidos", p1(sn["convite_dia_hora"])],
              ["Não houve retomada depois que o lead sumiu", p1(sn["sem_followup"])],
              ["A última mensagem foi do lead, sem resposta", p1(sn["ultima_do_lead"])]],
              [125, 55])]
    if c["mediana_humano"] is not None:
        e.append(Paragraph(
            "A primeira resposta humana saiu em %.0f minutos na mediana. Em %d de %d "
            "conversas ela demorou mais de 2 horas."
            % (c["mediana_humano"], c["acima_2h"], c["com_resposta"]), P))

    if c["por_origem"]:
        e += [Paragraph("Resultado por origem do lead", H),
              tabela(["Origem", "Leads", "Agendou ou matriculou", "Matriculou"],
                     [[x["origem"], x["n"], p1(x["agendou"]), p1(x["matriculou"])]
                      for x in c["por_origem"]], [75, 25, 45, 35])]
    if c["horas"]:
        e += [Paragraph("Resultado por horário da primeira mensagem", H),
              tabela(["Horário", "Leads", "Agendou ou matriculou", "Resposta humana (mediana)"],
                     [[x["faixa"], x["n"], p1(x["agendou"]),
                       ("%.0f min" % x["mediana_min"]) if x["mediana_min"] is not None else "—"]
                      for x in c["horas"]], [45, 25, 50, 60])]

    if c["por_cons"]:
        e += [Paragraph("Por consultora, nas conversas perdidas", H),
              tabela(["Consultora", "Perdidas", "Preço sem pedir", "Convite com dia e hora",
                      "Sem retomada", "Motivo mais comum"],
                     [[x["nome"], x["n"], p1(x["preco_sem_pedir"]), p1(x["convite_dia_hora"]),
                       p1(x["sem_followup"]),
                       MOTIVOS[x["motivos"][0][0]] if x["motivos"] else "—"]
                      for x in c["por_cons"]], [28, 20, 27, 32, 25, 48])]
        if s:
            for x in s["consultoras"]:
                e.append(KeepTogether([
                    Paragraph(esc(x["nome"]), H3),
                    Paragraph("<b>Ponto forte:</b> " + esc(x["ponto_forte"]), P),
                    Paragraph("<b>A corrigir:</b> " + esc(x["ponto_a_corrigir"]), P)]))

    if s:
        for n, a in enumerate(s["achados"]):
            cab = [Paragraph("Outros achados", H)] if n == 0 else []
            e.append(KeepTogether(cab + [Paragraph(esc(a["titulo"]), H3),
                                         Paragraph(esc(a["explicacao"]), P)]))
        for i, r in enumerate(s["recomendacoes"], 1):
            cab = [Paragraph("O que fazer, em ordem de impacto", H)] if i == 1 else []
            e.append(KeepTogether(cab + [
                Paragraph("%d. %s" % (i, esc(r["acao"])), H3),
                Paragraph("<b>Por quê:</b> " + esc(r["motivo"]), P),
                Paragraph("<b>Como medir:</b> " + esc(r["como_medir"]), P)]))

    exemplos = []
    for k, _, _ in c["motivos_todos"][:5]:
        ev = [r["evidencia"] for r in c["V"] if r["motivo_principal"] == k
              and 12 < len(r["evidencia"]) < 140][:3]
        if ev:
            exemplos.append([MOTIVOS[k], " · ".join("“%s”" % x for x in ev)])
    if exemplos:
        e += [Paragraph("Trechos das conversas, por motivo", H),
              tabela(["Motivo", "O que apareceu nas conversas"], exemplos, [55, 125])]

    e += [Paragraph("Como a análise foi feita e seus limites", H),
          Paragraph(
              "Entraram os leads criados no período que mandaram pelo menos uma mensagem, "
              "tirando candidatos a vaga e quem já era aluno. Cada conversa perdida foi lida "
              "inteira por inteligência artificial e classificada em um motivo principal. "
              "Conversas muito longas tiveram o meio resumido, mantendo o começo e o fim.", P),
          Paragraph(
              "Limites: a matrícula é identificada pelo cadastro no CRM e no Pacto, então "
              "quem matriculou com outro telefone aparece como perdido. O comparecimento só "
              "é conhecido quando a consultora marcou na agenda ou quando a conversa mostra. "
              "A classificação é uma leitura, não uma medição exata: os percentuais indicam "
              "ordem de grandeza.", P)]
    if lidas < esperadas:
        e.append(Paragraph("Atenção: %d das %d conversas perdidas não puderam ser lidas "
                           "nesta execução." % (esperadas - lidas, esperadas), P))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title="Por que perdemos vendas e agendamentos",
                            author="Território Fit")

    def rodape(cv, d):
        cv.saveState()
        cv.setFont(fonte, 8)
        cv.setFillColor(colors.HexColor("#777777"))
        cv.drawString(15 * mm, 8 * mm, "Território Fit · relatório interno")
        cv.drawRightString(195 * mm, 8 * mm, "Página %d" % d.page)
        cv.restoreState()

    doc.build(e, onFirstPage=rodape, onLaterPages=rodape)
    return buf.getvalue()


# ------------------------------------------------------------------ envio
def enviar_pdf(token, destino, pdf, nome, legenda):
    b64 = base64.b64encode(pdf).decode("ascii")
    for arquivo in (b64, "data:application/pdf;base64," + b64):
        r = requests.post(f"{UAZAPI_URL}/send/media",
                          headers={"token": token, "Content-Type": "application/json"},
                          json={"number": destino, "type": "document", "file": arquivo,
                                "docName": nome, "text": legenda}, timeout=180)
        print(f"[envio] PDF -> ...{destino[-4:]} HTTP {r.status_code}")
        if r.status_code == 200:
            return True
        print("        " + r.text[:160])
    return False


def enviar_texto(token, destino, texto):
    r = requests.post(f"{UAZAPI_URL}/send/text",
                      headers={"token": token, "Content-Type": "application/json"},
                      json={"number": destino, "text": texto}, timeout=120)
    print(f"[envio] texto -> ...{destino[-4:]} HTTP {r.status_code}")
    return r.status_code == 200


def main():
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    token = os.environ.get("UAZAPI_TOKEN_CEO", "").replace("﻿", "").strip()
    destino = (os.environ.get("RELATORIO_PARA") or "5516992290338").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    ini = os.environ.get("DATA_INI") or "2026-08-25"
    fim = os.environ.get("DATA_FIM") or "2026-09-26"
    teto = float(os.environ.get("TETO_USD") or "25")
    maxc = int(os.environ.get("MAX_CONVERSAS") or "0")
    if not key or (not token and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_CEO")
        return 1

    if os.environ.get("SO_TESTE_ENVIO", "") == "1":
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        cv = canvas.Canvas(buf, pagesize=A4)
        cv.setFont("Helvetica", 14)
        cv.drawString(60, 780, "Teste de envio de PDF - Territorio Fit")
        cv.save()
        ok = enviar_pdf(token, destino, buf.getvalue(), "teste-envio.pdf",
                        "Teste de envio: o relatório de vendas perdidas chega por aqui "
                        "quando a análise terminar.")
        return 0 if ok else 1

    import anthropic
    akey = _anthropic_key(key)
    if not akey:
        print("ANTHROPIC_API_KEY ausente na config do CRM")
        return 1
    client = anthropic.Anthropic(api_key=akey, max_retries=5, timeout=300.0)

    leads, msgs, ag, ativos = extrair(key, ini, fim)
    univ, fora = montar_universo(leads, msgs, ag, ativos)
    perd = [u for u in univ if u["desfecho"] != "MATRICULOU"]
    print(f"[universo] {len(univ)} leads de venda | fora: {dict(fora)} | "
          f"funil: {dict(Counter(u['desfecho'] for u in univ))}")
    alvo = perd[:maxc] if maxc else perd
    print(f"[claude] modelo {MODELO} | lendo {len(alvo)} conversas perdidas")

    res = classificar(client, alvo, teto)
    print(f"[claude] classificadas {len(res)} de {len(alvo)} | falhas {USO['falhas']} | "
          f"custo estimado US$ {custo_usd():.2f}")
    if len(res) < 0.5 * len(alvo):
        msg = ("Não consegui terminar o relatório de vendas perdidas: só %d de %d conversas "
               "foram lidas. Veja o saldo e os limites da API da Anthropic e me peça para "
               "rodar de novo." % (len(res), len(alvo)))
        print("[erro] leitura insuficiente")
        if not dry:
            enviar_texto(token, destino, msg)
        return 1

    c = consolidar(univ, res)
    s = sintetizar(client, c)
    print(f"[sintese] {'ok' if s else 'falhou — relatório sai só com os números'} | "
          f"custo total estimado US$ {custo_usd():.2f} | chamadas {USO['chamadas']}")
    di = datetime.strptime(ini, "%Y-%m-%d").strftime("%d/%m")
    df = (datetime.strptime(fim, "%Y-%m-%d") - timedelta(days=1)).strftime("%d/%m/%Y")
    pdf = gerar_pdf(c, s, di, df, len(res), len(alvo))
    print(f"[pdf] {len(pdf) // 1024} KB")
    if dry:
        with open(os.environ.get("SAIDA_PDF") or "relatorio-vendas-perdidas.pdf", "wb") as fh:
            fh.write(pdf)
        print("[DRY] PDF gravado localmente, nada enviado.")
        return 0

    top_na = c["motivos_na"][0] if c["motivos_na"] else None
    top_ag = c["motivos_ag"][0] if c["motivos_ag"] else None
    leg = ["*Relatório: por que perdemos vendas e agendamentos*",
           "Leads novos de %s a %s: %d analisados." % (di, df, c["total"])]
    if top_na:
        leg.append("Principal motivo de não agendar: %s (%.0f%%)." % (MOTIVOS[top_na[0]].lower(), top_na[2]))
    if top_ag:
        leg.append("Principal motivo de agendar e não matricular: %s (%.0f%%)."
                   % (MOTIVOS[top_ag[0]].lower(), top_ag[2]))
    leg.append("Detalhes e recomendações no PDF.")
    ok = enviar_pdf(token, destino, pdf, "relatorio-vendas-perdidas.pdf", "\n".join(leg))
    if not ok:
        enviar_texto(token, destino, "\n".join(leg[:-1]) + "\nNão consegui anexar o PDF. "
                     "Me peça no Claude que eu gero de novo.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
