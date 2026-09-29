# -*- coding: utf-8 -*-
"""
Ciclo do agente de Inteligencia Comercial — roda inteiro na nuvem.

Pedido do Andre em 29/09/2026: o ciclo de analise do agente de inteligencia
comercial precisa rodar sem depender do PC dele e chegar em PDF no WhatsApp,
pelo numero "Ceo Territorio Digital".

O que faz:
  1. monta o universo de leads do periodo (mesmo criterio do relatorio de
     vendas perdidas) e confirma o resultado de cada um na Pacto, pelas
     tabelas que o CRM ja sincroniza (visitantes_bv, pacto_alunos_ativos,
     vendas_pacto). Vinculo pelo telefone (8 ultimos digitos);
  2. le TODAS as conversas, de quem matriculou e de quem nao matriculou, com
     o mesmo roteiro e sem contar ao leitor qual foi o desfecho;
  3. compara os comportamentos de atendimento pelo resultado real (visita e
     matricula confirmada), com nivel de evidencia calculado pelo tamanho da
     amostra, nunca pela opiniao do leitor;
  4. escreve o relatorio, manda o PDF e guarda os numeros no CRM
     (agent_activity) e a leitura por lead em arquivo privado (Storage).

Somente leitura no CRM e na Pacto. Nao envia mensagem a cliente e nao altera a
Clara: as recomendacoes saem como proposta para o Andre aprovar.

Usa a API paga da Anthropic (chave na config do CRM). Repo publico: o log so
tem contagens.

Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO. Opcional: DATA_INI, DATA_FIM (AAAA-MM-DD,
fim exclusivo) | RELATORIO_PARA | MODELO | TETO_USD (padrao 20) |
MAX_CONVERSAS (teste) | DRY_RUN=1 (gera o PDF local, nao envia nem guarda) |
SAIDA_PDF.
"""

import io
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import requests

import relatorio_vendas_perdidas as base

CAMPANHA = "ciclo-inteligencia-comercial"
BALDE = "inteligencia-comercial"
TZ_SP = base.TZ_SP
ORIGENS = base.ORIGENS

# ------------------------------------------------------------------ leitura
SIS_LEITURA = """Você analisa conversas de WhatsApp entre uma academia (Território Fit, São Carlos/SP) e pessoas interessadas em treinar. Seu trabalho é descrever, com base só no que está escrito, COMO o atendimento foi conduzido. Você não sabe e não deve tentar adivinhar se a pessoa acabou se matriculando: descreva o comportamento, não o resultado.

Como ler a conversa: cada linha começa com data e hora (horário de Brasília) e o remetente. LEAD é o cliente. CLARA é a assistente automática da academia (também envia lembretes automáticos). "CONSULTORA <nome>" é uma atendente humana escrevendo pelo sistema. EQUIPE é uma atendente humana escrevendo pelo celular. Linhas que começam com "🔧" são registros de ação da Clara.

Contexto do negócio: a sequência de venda é visita, aula experimental grátis e só então a consultora fecha a matrícula.

Regras de julgamento:
- Não invente. Na dúvida, escolha a opção mais conservadora.
- "tipo" é "nao_e_venda" quando a pessoa já era aluna tratando de rotina, cobrança ou cancelamento, candidato a emprego, fornecedor, parceiro, engano ou spam.
- "primeiro_contato": "ja_tinha_visitado_ou_recepcao" quando a conversa mostra que a pessoa já tinha visitado a academia ou sido atendida no balcão antes da primeira mensagem; "iniciado_pela_equipe" quando a academia mandou a primeira mensagem sem contato presencial anterior visível; "digital" quando o lead começou a conversa.
- "primeiro_pedido": o que o lead pediu na primeira ou segunda mensagem dele. Mensagem pronta de anúncio que cita plano ou preço conta como "preco"; pedido de agendar visita ou aula conta como "visita_ou_experimental".
- "abertura_tipo": o que a academia fez na primeira resposta.
- "perguntas_seguidas": verdadeiro quando a academia fez três ou mais perguntas em sequência, como um formulário, sem reagir ao que o lead respondeu.
- "lead_contou_contexto": verdadeiro quando o lead contou objetivo, rotina, histórico de treino, onde mora ou trabalha, ou alguma dificuldade.
- "preco_informado": a academia informou valor de plano, mensalidade ou diária.
- "preco_pedido_pelo_lead": antes do primeiro preço, o lead perguntou por valores, planos ou mensalidade.
- "convite_antes_do_preco": antes do primeiro preço (ou na mesma mensagem ou no mesmo bloco de mensagens seguidas), a academia ou a Clara já tinha convidado para visita ou aula experimental, perguntado dia ou horário para vir, ou aceitado o pedido de agendamento avançando para marcar. Perguntar só "você já conhece a academia?" não é convite. Se nenhum preço foi informado, use falso.
- "preco_formato": "valor_de_entrada" quando informou um valor inicial ("a partir de") sem listar os planos; "tabela_completa" quando listou dois ou mais planos com valores; "nao_informou" quando não houve preço.
- "cta_tipo": o convite mais forte que a academia fez em toda a conversa. "dia_e_hora" quando propôs um dia e um horário; "duas_opcoes" quando deu duas alternativas para o lead escolher; "aberto" quando convidou sem data ("quando quer vir?", "bora conhecer?"); "nenhum" quando não convidou.
- "objecao_principal": a principal resistência dita pelo lead. "objecao_tratamento": "ignorada" quando a academia não respondeu à objeção; "respondida_sem_avanco" quando respondeu mas não propôs próximo passo; "respondida_com_avanco" quando respondeu e propôs próximo passo; "nao_houve" quando não houve objeção.
- "flexibilizou_pagamento": verdadeiro quando a academia ofereceu mudar data de pagamento, forma de pagamento ou plano sem cartão para resolver uma dificuldade do lead.
- "followups": número de mensagens de retomada que a academia mandou depois que o lead parou de responder. "followup_tipo": o tipo predominante. "cita_contexto" quando retoma algo que o lead disse ou combinou; "convite_com_data" quando propõe dia e hora; "tabela_ou_promocao" quando manda valores ou condição especial; "generico" quando só pergunta se viu, se desistiu ou cumprimenta.
- "lead_voltou_apos_followup": verdadeiro quando o lead respondeu depois de uma retomada.
- "visita_combinada": verdadeiro quando ficou combinada visita, aula experimental ou início com dia definido.
- "mensagens_longas": verdadeiro quando a academia mandou blocos grandes de texto, com muita informação de uma vez.
- "consultora": primeiro nome da atendente humana principal, ou texto vazio.
- "atendentes_humanas": quantas atendentes humanas diferentes escreveram.
- "aprendizado": uma frase curta, sem nome de pessoa, dizendo o que neste atendimento ajudou ou atrapalhou o lead a avançar.
- "evidencia": trecho curto e literal da conversa, de até 15 palavras, que sustenta o aprendizado. Não inclua nome nem telefone."""

ESQ_LEITURA = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["venda", "nao_e_venda"]},
        "primeiro_contato": {"type": "string", "enum": [
            "digital", "iniciado_pela_equipe", "ja_tinha_visitado_ou_recepcao"]},
        "primeiro_pedido": {"type": "string", "enum": [
            "preco", "visita_ou_experimental", "informacoes", "modalidade_ou_horario",
            "convenio", "cumprimento", "outro"]},
        "intencao": {"type": "string", "enum": ["alta", "media", "baixa"]},
        "primeira_resposta_de": {"type": "string", "enum": ["clara", "consultora", "ninguem"]},
        "abertura_tipo": {"type": "string", "enum": [
            "pergunta_se_conhece", "pergunta_objetivo", "convite_direto",
            "apresenta_academia", "preco_direto", "so_saudacao", "outro"]},
        "perguntas_seguidas": {"type": "boolean"},
        "lead_contou_contexto": {"type": "boolean"},
        "preco_informado": {"type": "boolean"},
        "preco_pedido_pelo_lead": {"type": "boolean"},
        "convite_antes_do_preco": {"type": "boolean"},
        "preco_formato": {"type": "string", "enum": [
            "valor_de_entrada", "tabela_completa", "nao_informou"]},
        "cta_tipo": {"type": "string", "enum": ["nenhum", "aberto", "duas_opcoes", "dia_e_hora"]},
        "objecao_principal": {"type": "string", "enum": [
            "nenhuma", "preco", "forma_de_pagamento", "fidelidade_ou_multa", "distancia",
            "horario_ou_tempo", "concorrente", "vou_pensar", "consultar_familia",
            "comecar_depois", "convenio", "outra"]},
        "objecao_tratamento": {"type": "string", "enum": [
            "nao_houve", "ignorada", "respondida_sem_avanco", "respondida_com_avanco"]},
        "flexibilizou_pagamento": {"type": "boolean"},
        "followups": {"type": "integer"},
        "followup_tipo": {"type": "string", "enum": [
            "nenhum", "generico", "cita_contexto", "convite_com_data", "tabela_ou_promocao"]},
        "lead_voltou_apos_followup": {"type": "boolean"},
        "visita_combinada": {"type": "boolean"},
        "mensagens_longas": {"type": "boolean"},
        "consultora": {"type": "string"},
        "atendentes_humanas": {"type": "integer"},
        "aprendizado": {"type": "string"},
        "evidencia": {"type": "string"},
    },
    "additionalProperties": False,
}
ESQ_LEITURA["required"] = list(ESQ_LEITURA["properties"])

# ------------------------------------------------------------------ sintese
HIPOTESES = [
    ("H1", "Enviar o preço antes de qualquer convite para visita reduz o avanço do lead",
     "convite_antes_do_preco"),
    ("H2", "Mensagem de tabela ou condição especial para quem está em silêncio quase nunca traz o lead de volta",
     "followup_tabela"),
    ("H3", "Retomada que cita o que a pessoa disse ou combinou funciona melhor que retomada genérica",
     "followup_contexto"),
    ("H4", "Convite com dia e hora definidos gera mais visita que convite aberto", "cta"),
    ("H5", "Flexibilizar a forma ou a data de pagamento resolve a objeção de cartão",
     "flexibilizou_pagamento"),
    ("H6", "Contato iniciado pela equipe converte mais que lead de anúncio", "origem"),
    ("H7", "Primeira mensagem entre 12h e 14h avança menos", "horario"),
    ("H8", "Quando a Clara dá a primeira resposta, o lead avança mais", "primeira_resposta"),
    ("H9", "Velocidade de resposta não é o gargalo", "tempo_resposta"),
]

SIS_SINTESE = """Você é o Agente de Inteligência Comercial da Território Fit (academia em São Carlos/SP) e escreve para o André, dono da academia. Ele vai ler no celular. Escreva em português do Brasil, em frases completas e diretas, sem jargão, sem siglas e sem inglês. Não use nomes de clientes nem telefones. Não faça ranking de consultoras nem julgue pessoas: descreva comportamentos.

Você recebe: o funil do período com a confirmação na Pacto; comparações entre grupos de conversas, cada uma com tamanho de amostra, taxa de visita, taxa de matrícula confirmada e um NÍVEL DE EVIDÊNCIA JÁ CALCULADO; a lista de hipóteses em teste com a comparação correspondente; e uma amostra de aprendizados anotados conversa a conversa, separados entre quem matriculou e quem não matriculou.

Princípios obrigatórios:
- O resultado que importa é matrícula confirmada na Pacto. Resposta e conversa são só intermediários.
- Respeite o nível de evidência recebido. Nunca promova: "Hipótese" não vira "Sinal", "Sinal" não vira "Padrão". "Padrão validado" só vale quando o nível recebido disser isso, porque exige repetição em outro período.
- Correlação não é causa. Quando uma diferença puder ser explicada pela origem do lead, pela intenção ou por quem já tinha visitado a academia, diga isso.
- Não invente número. Use só os números recebidos e cite o tamanho da amostra junto.
- Quando a amostra for pequena, diga que é pequena e não conclua.
- Regras da casa que toda recomendação respeita: a sequência é visita, aula experimental e só então a consultora fecha; quando perguntam preço, informar o valor de entrada com convite para visita na mesma mensagem; não oferecer sete dias grátis por iniciativa da academia; aula experimental só em dia útil; não citar dados de gestão a cliente; não propor desconto nem promoção que não exista; sem urgência falsa.

O que escrever:
- "resumo": 4 a 6 frases, começando pela conclusão mais importante do ciclo.
- "descobertas": de 3 a 6 descobertas, em ordem de relevância para matrícula. Em cada uma: o que foi observado, os números com amostra, o nível de evidência recebido e a ressalva necessária.
- "hipoteses": uma entrada para cada hipótese recebida, na mesma ordem, com a situação ("confirma", "enfraquece" ou "sem dados suficientes"), o nível de evidência (copie o da comparação) e uma justificativa de uma ou duas frases com os números.
- "melhor_desempenho" e "pior_desempenho": abordagens associadas a mais e a menos matrícula, com números.
- "objecoes": o que os dados mostram sobre as objeções mais frequentes e o tratamento dado.
- "rapport", "followup", "cta": um parágrafo cada, com números.
- "recomendacoes_clara": SOMENTE para comparações com nível "Padrão" ou "Padrão validado". Se nenhuma tiver esse nível, devolva lista vazia. Cada recomendação traz aprendizado, quando utilizar, como aplicar, exemplo curto de mensagem, o que evitar, evidência, resultado e confiança.
- "testes": de 2 a 4 testes recomendados para as diferenças com nível "Sinal" ou "Hipótese" que valem ser confirmadas, cada um com hipótese, segmento, estratégia A, estratégia B, métrica e quantidade mínima de casos.
- "rever": aprendizados anteriores que os dados deste ciclo mandam revisar, ou texto dizendo que nenhum.
- "limitacoes": de 3 a 6 frases curtas sobre o que limita a leitura deste ciclo."""

_S = {"type": "string"}
ESQ_SINTESE = {
    "type": "object",
    "properties": {
        "resumo": _S,
        "descobertas": {"type": "array", "items": {"type": "object", "properties": {
            "titulo": _S, "observado": _S, "evidencia": _S, "ressalva": _S},
            "required": ["titulo", "observado", "evidencia", "ressalva"],
            "additionalProperties": False}},
        "hipoteses": {"type": "array", "items": {"type": "object", "properties": {
            "codigo": _S, "situacao": {"type": "string", "enum": [
                "confirma", "enfraquece", "sem dados suficientes"]},
            "nivel": _S, "justificativa": _S},
            "required": ["codigo", "situacao", "nivel", "justificativa"],
            "additionalProperties": False}},
        "melhor_desempenho": {"type": "array", "items": _S},
        "pior_desempenho": {"type": "array", "items": _S},
        "objecoes": _S, "rapport": _S, "followup": _S, "cta": _S,
        "recomendacoes_clara": {"type": "array", "items": {"type": "object", "properties": {
            "aprendizado": _S, "quando_utilizar": _S, "como_aplicar": _S, "exemplo": _S,
            "evitar": _S, "evidencia": _S, "resultado": _S, "confianca": _S},
            "required": ["aprendizado", "quando_utilizar", "como_aplicar", "exemplo",
                         "evitar", "evidencia", "resultado", "confianca"],
            "additionalProperties": False}},
        "testes": {"type": "array", "items": {"type": "object", "properties": {
            "hipotese": _S, "segmento": _S, "estrategia_a": _S, "estrategia_b": _S,
            "metrica": _S, "minimo_de_casos": _S},
            "required": ["hipotese", "segmento", "estrategia_a", "estrategia_b",
                         "metrica", "minimo_de_casos"],
            "additionalProperties": False}},
        "rever": _S,
        "limitacoes": {"type": "array", "items": _S},
    },
    "additionalProperties": False,
}
ESQ_SINTESE["required"] = list(ESQ_SINTESE["properties"])


# ------------------------------------------------------------------ Pacto
def p8(s):
    return re.sub(r"\D", "", s or "")[-8:]


def dia(s):
    """Data (date) a partir de 'AAAA-MM-DD' ou de um timestamp."""
    if not s:
        return None
    return date.fromisoformat(s[:10]) if len(s) <= 10 else base.hora(s).date()


def carregar_pacto(key, ini):
    desde = (date.fromisoformat(ini) - timedelta(days=2)).isoformat()
    bv = base.sb_todos(key, "visitantes_bv", {
        "select": "telefone,data_visita,situacao,convertido_em,codigo_cliente,tipo_bv"})
    at = base.sb_todos(key, "pacto_alunos_ativos", {"select": "phone8,codigo_cliente"}, 10000)
    vp = base.sb_todos(key, "vendas_pacto", {
        "select": "codigo_cliente,data_venda,tipo,descricao_plano,valor,duracao_meses",
        "data_venda": f"gte.{desde}"})
    por_fone, ativos, vendas = defaultdict(list), defaultdict(set), defaultdict(list)
    for v in bv:
        if len(p8(v["telefone"])) == 8:
            por_fone[p8(v["telefone"])].append(v)
    for a in at:
        if a.get("phone8"):
            ativos[a["phone8"]].add(str(a.get("codigo_cliente") or ""))
    for v in vp:
        if v.get("codigo_cliente") is not None:
            vendas[str(v["codigo_cliente"])].append(v)
    qual = {
        "visitantes": len(bv),
        "visitantes_sem_telefone": sum(1 for v in bv if len(p8(v["telefone"])) != 8),
        "vendas_no_periodo": len(vp),
        "vendas_sem_codigo_cliente": sum(1 for v in vp if v.get("codigo_cliente") is None),
    }
    print(f"[pacto] visitantes {len(bv)} | ativos {len(at)} | vendas {len(vp)}")
    return por_fone, ativos, vendas, qual


def resultado_pacto(lead, px):
    """Cruza um lead com a Pacto. Devolve o que os sistemas confirmam."""
    por_fone, ativos, vendas, _ = px
    f = p8(lead["phone"])
    criado = base.hora(lead["created_at"]).date()
    out = {"vinculo": "sem_cadastro", "visita_data": None, "visitou_antes": False,
           "matricula": False, "matricula_data": None, "matricula_por": "",
           "plano": "", "valor": None, "tipo_contrato": "", "so_brinde": False}
    if len(f) != 8:
        out["vinculo"] = "sem_telefone"
        return out
    bvs = por_fone.get(f, [])
    codigos = {str(v["codigo_cliente"]) for v in bvs if v.get("codigo_cliente")} | \
        {c for c in ativos.get(f, set()) if c}
    if not bvs and f not in ativos:
        return out
    if len(codigos) > 1:
        out["vinculo"] = "nao_confirmado"
        return out
    out["vinculo"] = "confirmado"
    visitas = sorted(d for d in (dia(v["data_visita"]) for v in bvs) if d)
    depois = [d for d in visitas if d >= criado - timedelta(days=1)]
    out["visitou_antes"] = any(d < criado - timedelta(days=1) for d in visitas)
    out["visita_data"] = depois[0] if depois else None
    contratos = [c for cod in codigos for c in vendas.get(cod, [])
                 if c["tipo"] in ("MA", "RE") and dia(c["data_venda"]) >= criado - timedelta(days=1)]
    pagos = sorted((c for c in contratos if (c.get("valor") or 0) > 0
                    and "BRINDE" not in (c.get("descricao_plano") or "").upper()),
                   key=lambda c: c["data_venda"])
    if pagos:
        c = pagos[0]
        out.update(matricula=True, matricula_data=dia(c["data_venda"]), matricula_por="contrato",
                   plano=(c.get("descricao_plano") or "").strip(), valor=c.get("valor"),
                   tipo_contrato=c["tipo"])
        return out
    if contratos:
        out["so_brinde"] = True
        return out
    conv = sorted(d for d in (dia(v.get("convertido_em")) for v in bvs
                              if v.get("situacao") == "Ativo") if d and d >= criado - timedelta(days=1))
    if conv:
        out.update(matricula=True, matricula_data=conv[0], matricula_por="cadastro_ativo")
    elif f in ativos and not out["visitou_antes"]:
        out.update(matricula=True, matricula_por="cadastro_ativo")
    return out


# ------------------------------------------------------------------ universo
def montar(key, ini, fim):
    leads, msgs, ag, ativos = base.extrair(key, ini, fim)
    univ, fora = base.montar_universo(leads, msgs, ag, ativos)
    por_id = {l["id"][:8]: l for l in leads}
    por_msgs = defaultdict(list)
    for m in msgs:
        por_msgs[m["lead_id"]].append(m)
    px = carregar_pacto(key, ini)
    linhas = []
    for u in univ:
        l = por_id[u["id"]]
        r = resultado_pacto(l, px)
        ms = por_msgs.get(l["id"], [])
        corte = r["matricula_data"] + timedelta(days=1) if r["matricula_data"] else None
        conv = []
        for m in ms:
            if corte and base.hora(m["created_at"]).date() > corte:
                break
            c = re.sub(r"\s+", " ", (m.get("content") or "").strip()) or \
                "[%s]" % (m.get("message_type") or "midia")
            conv.append("[%s] %s: %s" % (base.hora(m["created_at"]).strftime("%d/%m %H:%M"),
                                         base.quem(m), c[:160 if c.startswith("🔧") else 380]))
        if len(conv) > 120:
            conv = conv[:80] + ["[... %d mensagens omitidas ...]" % (len(conv) - 120)] + conv[-40:]
        rec = [m for m in ms if not m["is_from_me"]]
        respondeu = any(m["is_from_me"] and m["created_at"] >= rec[0]["created_at"] for m in ms)
        agend = bool(u["agend"])
        veio = any(a.get("veio") is True for a in u["agend"]) or bool(r["visita_data"])
        crm_fechou = (l["status"] in ("cliente", "inadimplente")
                      or any(a.get("fechou") is True for a in u["agend"]))
        criado = base.hora(l["created_at"]).date()
        linhas.append({
            "id": u["id"], "lead_id": l["id"], "origem": u["origem"], "criado": criado,
            "hora_1a": u["hora_1a"], "min_humano": u["min_humano"],
            "n_lead": len(rec), "n_equipe": len(ms) - len(rec), "respondido": respondeu,
            "agendou": agend, "visitou": veio, "matriculou": r["matricula"],
            "crm_fechou": crm_fechou, "pacto": r, "conversa": conv,
            "dias_ate_matricula": ((r["matricula_data"] - criado).days
                                   if r["matricula_data"] else None),
            "dias_ate_visita": ((r["visita_data"] - criado).days if r["visita_data"] else None),
        })
    return linhas, fora, px[3]


def ler(client, linhas, teto):
    res = {}

    def uma(u):
        if base.custo_usd() > teto:
            return u["id"], None
        cab = "CONVERSA | lead criado em %s | origem %s\n" % (
            u["criado"].strftime("%d/%m"), ORIGENS.get(u["origem"], u["origem"]))
        for _ in range(3):
            r = base.chamar(client, SIS_LEITURA, cab + "\n".join(u["conversa"]), ESQ_LEITURA,
                            max_tokens=3000)
            if r:
                return u["id"], r
        return u["id"], None

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(uma, u) for u in linhas]
        for n, f in enumerate(as_completed(futs), 1):
            i, r = f.result()
            if r:
                res[i] = r
            if n % 50 == 0:
                print(f"[claude] {n}/{len(linhas)} lidas | custo estimado "
                      f"US$ {base.custo_usd():.2f}")
    return res


# ------------------------------------------------------------------ numeros
def taxa(sel, campo):
    return round(base.pct(sum(1 for r in sel if r[campo]), len(sel)), 1) if sel else None


def nivel(a, b):
    """Nivel de evidencia pela amostra e pela diferenca na matricula."""
    if not a or not b:
        return "Hipótese"
    menor = min(len(a), len(b))
    dif = abs(taxa(a, "matriculou") - taxa(b, "matriculou"))
    dif_v = abs(taxa(a, "visitou") - taxa(b, "visitou"))
    if menor >= 30 and (dif >= 8 or dif_v >= 12):
        return "Padrão"
    if menor >= 15 and (dif >= 5 or dif_v >= 8):
        return "Sinal"
    return "Hipótese"


def grupo(rotulo, sel):
    return {"rotulo": rotulo, "conversas": len(sel), "agendou_pct": taxa(sel, "agendou"),
            "visitou_pct": taxa(sel, "visitou"), "matriculou_pct": taxa(sel, "matriculou"),
            "matriculas": sum(1 for r in sel if r["matriculou"])}


def comparar(chave, titulo, base_txt, ra, a, rb, b):
    return {"chave": chave, "titulo": titulo, "base": base_txt, "a": grupo(ra, a),
            "b": grupo(rb, b), "nivel": nivel(a, b)}


def faixa_resposta(m):
    if m is None:
        return "sem resposta humana"
    for lim, rot in ((1, "até 1 min"), (5, "1 a 5 min"), (15, "5 a 15 min"), (30, "15 a 30 min"),
                     (60, "30 a 60 min"), (180, "1 a 3 horas")):
        if m <= lim:
            return rot
    return "acima de 3 horas"


def faixa_dias(d):
    if d is None:
        return None
    for lim, rot in ((0, "mesmo dia"), (1, "até 24 horas"), (3, "até 3 dias"), (7, "até 7 dias"),
                     (15, "até 15 dias"), (30, "até 30 dias")):
        if d <= lim:
            return rot
    return "mais de 30 dias"


def calcular(linhas, res, qual, fim):
    R = [{**u, **res[u["id"]]} for u in linhas if u["id"] in res]
    V = [r for r in R if r["tipo"] == "venda"]
    dig = [r for r in V if r["primeiro_contato"] == "digital"]
    ultimo = date.fromisoformat(fim) - timedelta(days=1)
    hoje = datetime.now(TZ_SP).date()

    funil = [
        ("Leads recebidos com conversa", len(V)),
        ("Receberam resposta da academia", sum(1 for r in V if r["respondido"])),
        ("Conversa iniciada (lead mandou 2 ou mais mensagens)", sum(1 for r in V if r["n_lead"] >= 2)),
        ("Lead qualificado (contou contexto ou intenção média ou alta)",
         sum(1 for r in V if r["lead_contou_contexto"] or r["intencao"] != "baixa")),
        ("Visita agendada (agenda do CRM ou combinada na conversa)",
         sum(1 for r in V if r["agendou"] or r["visita_combinada"])),
        ("Visita realizada (visitante cadastrado na Pacto ou presença na agenda)",
         sum(1 for r in V if r["visitou"])),
        ("Matrícula confirmada na Pacto", sum(1 for r in V if r["matriculou"])),
    ]
    mat = [r for r in V if r["matriculou"]]
    C = []
    # H1
    cp = [r for r in dig if r["preco_informado"]]
    C.append(comparar("convite_antes_do_preco", "Convite antes do preço",
                      "primeiro contato digital, conversas com preço informado",
                      "convidou antes do preço", [r for r in cp if r["convite_antes_do_preco"]],
                      "preço antes de qualquer convite", [r for r in cp if not r["convite_antes_do_preco"]]))
    C.append(comparar("preco_sem_pedir", "Preço enviado sem o lead pedir",
                      "primeiro contato digital, conversas com preço informado",
                      "lead pediu o preço", [r for r in cp if r["preco_pedido_pelo_lead"]],
                      "preço enviado sem pedido", [r for r in cp if not r["preco_pedido_pelo_lead"]]))
    C.append(comparar("preco_formato", "Valor de entrada ou tabela completa",
                      "primeiro contato digital, conversas com preço informado",
                      "valor de entrada", [r for r in cp if r["preco_formato"] == "valor_de_entrada"],
                      "tabela completa", [r for r in cp if r["preco_formato"] == "tabela_completa"]))
    pp = [r for r in dig if r["primeiro_pedido"] == "preco" and r["preco_informado"]]
    C.append(comparar("convite_antes_do_preco_pediu_preco", "Convite antes do preço, só em quem abriu pedindo preço",
                      "primeiro contato digital, primeiro pedido foi preço",
                      "convidou antes do preço", [r for r in pp if r["convite_antes_do_preco"]],
                      "preço antes de qualquer convite", [r for r in pp if not r["convite_antes_do_preco"]]))
    # H4
    C.append(comparar("cta", "Tipo de convite",
                      "primeiro contato digital, conversas em que houve convite",
                      "com dia e hora ou duas opções",
                      [r for r in dig if r["cta_tipo"] in ("dia_e_hora", "duas_opcoes")],
                      "convite aberto", [r for r in dig if r["cta_tipo"] == "aberto"]))
    C.append(comparar("cta_nenhum", "Houve convite ou não",
                      "primeiro contato digital",
                      "algum convite", [r for r in dig if r["cta_tipo"] != "nenhum"],
                      "nenhum convite", [r for r in dig if r["cta_tipo"] == "nenhum"]))
    # H2 / H3
    fu = [r for r in dig if r["followups"] > 0]
    C.append(comparar("followup_contexto", "Tipo de retomada",
                      "primeiro contato digital, conversas com retomada",
                      "cita o contexto do lead ou convida com data",
                      [r for r in fu if r["followup_tipo"] in ("cita_contexto", "convite_com_data")],
                      "genérica", [r for r in fu if r["followup_tipo"] == "generico"]))
    C.append(comparar("followup_tabela", "Retomada com tabela ou condição especial",
                      "primeiro contato digital, conversas com retomada",
                      "tabela ou condição especial",
                      [r for r in fu if r["followup_tipo"] == "tabela_ou_promocao"],
                      "outros tipos de retomada",
                      [r for r in fu if r["followup_tipo"] != "tabela_ou_promocao"]))
    # H5
    op = [r for r in V if r["objecao_principal"] in ("forma_de_pagamento", "preco", "fidelidade_ou_multa")]
    C.append(comparar("flexibilizou_pagamento", "Flexibilizar pagamento diante de objeção de preço ou pagamento",
                      "conversas com objeção de preço, pagamento ou fidelidade",
                      "flexibilizou", [r for r in op if r["flexibilizou_pagamento"]],
                      "não flexibilizou", [r for r in op if not r["flexibilizou_pagamento"]]))
    # H6
    C.append(comparar("origem", "Contato iniciado pela equipe ou lead de anúncio",
                      "todas as conversas de venda",
                      "contato iniciado pela equipe", [r for r in V if r["origem"] == "whatsapp_manual"],
                      "anúncio no Instagram ou Facebook",
                      [r for r in V if r["origem"].startswith("anuncio")]))
    # H7
    C.append(comparar("horario", "Horário da primeira mensagem",
                      "primeiro contato digital",
                      "das 8h às 12h", [r for r in dig if 8 <= r["hora_1a"] < 12],
                      "das 12h às 14h", [r for r in dig if 12 <= r["hora_1a"] < 14]))
    # H8
    C.append(comparar("primeira_resposta", "Quem deu a primeira resposta",
                      "primeiro contato digital",
                      "Clara", [r for r in dig if r["primeira_resposta_de"] == "clara"],
                      "consultora", [r for r in dig if r["primeira_resposta_de"] == "consultora"]))
    # H9
    C.append(comparar("tempo_resposta", "Tempo até a primeira resposta humana",
                      "primeiro contato digital",
                      "até 5 minutos", [r for r in dig if r["min_humano"] is not None and r["min_humano"] <= 5],
                      "mais de 30 minutos", [r for r in dig if r["min_humano"] is not None and r["min_humano"] > 30]))
    C.append(comparar("rapport", "Lead contou o contexto dele",
                      "primeiro contato digital",
                      "contou objetivo, rotina ou dificuldade", [r for r in dig if r["lead_contou_contexto"]],
                      "não contou", [r for r in dig if not r["lead_contou_contexto"]]))
    C.append(comparar("perguntas_seguidas", "Perguntas em sequência, como formulário",
                      "primeiro contato digital",
                      "sem sequência de perguntas", [r for r in dig if not r["perguntas_seguidas"]],
                      "três ou mais perguntas seguidas", [r for r in dig if r["perguntas_seguidas"]]))
    C.append(comparar("mensagens_longas", "Blocos grandes de texto",
                      "primeiro contato digital",
                      "mensagens curtas", [r for r in dig if not r["mensagens_longas"]],
                      "blocos grandes", [r for r in dig if r["mensagens_longas"]]))

    def quebra(sel, campo, rot=None, minimo=8):
        g = defaultdict(list)
        for r in sel:
            g[r[campo] if rot is None else rot(r)].append(r)
        return [grupo(str(k), v) for k, v in sorted(g.items(), key=lambda x: -len(x[1]))
                if len(v) >= minimo]

    cons = defaultdict(list)
    for r in V:
        n = (r["consultora"] or "").strip().title()
        if n and r["primeira_resposta_de"] != "ninguem":
            cons[n].append(r)
    tempo = Counter(faixa_dias(r["dias_ate_matricula"]) for r in mat
                    if r["dias_ate_matricula"] is not None)
    crm_sem_pacto = sum(1 for r in V if r["crm_fechou"] and not r["matriculou"])
    pacto_sem_crm = sum(1 for r in V if r["matriculou"] and not r["crm_fechou"])
    vinc = Counter(r["pacto"]["vinculo"] for r in V)
    deviam = [r for r in V if r["agendou"] and any(a.get("veio") for a in [r])] or V
    return {
        "leads": len(V), "nao_venda": len(R) - len(V), "lidas": len(R),
        "sem_leitura": len(linhas) - len(R),
        "primeiro_contato": dict(Counter(r["primeiro_contato"] for r in V)),
        "funil": funil,
        "kpi": {
            "lead_matricula": taxa(V, "matriculou"),
            "lead_visita": taxa(V, "visitou"),
            "lead_agendamento": taxa(V, "agendou"),
            "lead_resposta": taxa(V, "respondido"),
            "agendamento_comparecimento": taxa([r for r in V if r["agendou"]], "visitou"),
            "visita_matricula": taxa([r for r in V if r["visitou"]], "matriculou"),
            "lead_matricula_digital": taxa(dig, "matriculou"),
        },
        "matriculas": {
            "total": len(mat),
            "por_contrato": sum(1 for r in mat if r["pacto"]["matricula_por"] == "contrato"),
            "por_cadastro_ativo": sum(1 for r in mat if r["pacto"]["matricula_por"] == "cadastro_ativo"),
            "rematricula": sum(1 for r in mat if r["pacto"]["tipo_contrato"] == "RE"),
            "so_brinde": sum(1 for r in V if r["pacto"]["so_brinde"]),
            "digitais": sum(1 for r in mat if r["primeiro_contato"] == "digital"),
            "planos": Counter(r["pacto"]["plano"] for r in mat if r["pacto"]["plano"]).most_common(8),
            "receita_contratos": round(sum(r["pacto"]["valor"] or 0 for r in mat), 2),
            "tempo": [(k, tempo[k]) for k in ("mesmo dia", "até 24 horas", "até 3 dias", "até 7 dias",
                                             "até 15 dias", "até 30 dias", "mais de 30 dias") if tempo[k]],
        },
        "integracao": {
            **qual,
            "leads_vinculo_confirmado": vinc["confirmado"],
            "leads_sem_cadastro_na_pacto": vinc["sem_cadastro"],
            "leads_vinculo_nao_confirmado": vinc["nao_confirmado"],
            "leads_sem_telefone": vinc["sem_telefone"],
            "visitou_sem_cadastro_de_visitante": sum(
                1 for r in V if r["visitou"] and not r["pacto"]["visita_data"]),
            "match_pct": round(base.pct(
                sum(1 for r in V if r["visitou"] and r["pacto"]["vinculo"] == "confirmado"),
                sum(1 for r in V if r["visitou"])), 1),
            "crm_diz_matriculou_pacto_nao_confirma": crm_sem_pacto,
            "pacto_confirma_crm_nao_marcou": pacto_sem_crm,
        },
        "janela": {"ultimo_lead": ultimo.isoformat(), "analise": hoje.isoformat(),
                   "dias_minimos": (hoje - ultimo).days},
        "comparacoes": C,
        "por_origem": quebra(V, "origem", lambda r: ORIGENS.get(r["origem"], r["origem"])),
        "por_primeiro_pedido": quebra(dig, "primeiro_pedido"),
        "por_intencao": quebra(dig, "intencao"),
        "por_abertura": quebra(dig, "abertura_tipo"),
        "por_cta": quebra(dig, "cta_tipo"),
        "por_tempo_resposta": quebra(dig, "min_humano", lambda r: faixa_resposta(r["min_humano"])),
        "por_objecao": quebra(V, "objecao_principal", minimo=5),
        "por_tratamento_objecao": quebra([r for r in V if r["objecao_principal"] != "nenhuma"],
                                         "objecao_tratamento", minimo=5),
        "por_consultora": [grupo(n, s) for n, s in sorted(cons.items(), key=lambda x: -len(x[1]))
                           if len(s) >= 10],
        "assistidas": sum(1 for r in mat if r["atendentes_humanas"] >= 2),
        "_R": R, "_V": V,
    }


def publico(c):
    return {k: v for k, v in c.items() if not k.startswith("_")}


def sintetizar(client, c):
    rnd = random.Random(7)

    def amostra(sel, n):
        sel = [r for r in sel if len(r.get("aprendizado") or "") > 15]
        rnd.shuffle(sel)
        return [{"aprendizado": r["aprendizado"][:200],
                 "evidencia": (r.get("evidencia") or "")[:120],
                 "primeiro_pedido": r["primeiro_pedido"]} for r in sel[:n]]

    V = c["_V"]
    dados = {
        "periodo": c["janela"], "numeros": {k: v for k, v in publico(c).items()
                                            if k not in ("por_consultora", "comparacoes")},
        "comparacoes": c["comparacoes"],
        "hipoteses_em_teste": [{"codigo": h, "texto": t, "comparacao": k} for h, t, k in HIPOTESES],
        "aprendizados_de_quem_matriculou": amostra(
            [r for r in V if r["matriculou"] and r["primeiro_contato"] == "digital"], 45),
        "aprendizados_de_quem_nao_matriculou": amostra(
            [r for r in V if not r["matriculou"] and r["primeiro_contato"] == "digital"], 45),
    }
    for _ in range(3):
        r = base.chamar(client, SIS_SINTESE, json.dumps(dados, ensure_ascii=False, default=str),
                        ESQ_SINTESE, max_tokens=14000)
        if r:
            niveis = {x["chave"]: x["nivel"] for x in c["comparacoes"]}
            mapa = {h: niveis.get(k, "Hipótese") for h, _, k in HIPOTESES}
            for h in r["hipoteses"]:
                if h["codigo"] in mapa:
                    h["nivel"] = mapa[h["codigo"]]
            if not any(v.startswith("Padrão") for v in niveis.values()):
                r["recomendacoes_clara"] = []
            return r
    return None


# ------------------------------------------------------------------ PDF
def gerar_pdf(c, s, ini, fim):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
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
    H3 = ParagraphStyle("h3", fontName=negr, fontSize=10.5, leading=14, spaceBefore=6,
                        spaceAfter=2)
    P = ParagraphStyle("p", fontName=fonte, fontSize=10, leading=14.5, spaceAfter=4)
    B = ParagraphStyle("b", parent=P, leftIndent=12, bulletIndent=2, spaceAfter=2)
    C = ParagraphStyle("c", fontName=fonte, fontSize=8.5, leading=11.5)
    CB = ParagraphStyle("cb", fontName=negr, fontSize=8.5, leading=11.5, textColor=colors.white)

    def esc(t):
        t = "".join(ch for ch in str(t if t is not None else "")
                    if ord(ch) < 0x2000 or ch in "–—‘’“”…")
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def tabela(cab, linhas, larg):
        dados = [[Paragraph(esc(x), CB) for x in cab]] + \
                [[Paragraph(esc(x), C) for x in l] for l in linhas]
        t = Table(dados, colWidths=[w * mm for w in larg], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), azul), ("GRID", (0, 0), (-1, -1), 0.4, borda),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, cinza]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5)]))
        return t

    def p0(v):
        return "—" if v is None else "%.0f%%" % v

    def grupos(titulo, lista, primeira="Grupo"):
        if not lista:
            return []
        return [KeepTogether([
            Paragraph(titulo, H3),
            tabela([primeira, "Conversas", "Agendou", "Visitou", "Matriculou", "Matrículas"],
                   [[g["rotulo"].replace("_", " "), g["conversas"], p0(g["agendou_pct"]),
                     p0(g["visitou_pct"]), p0(g["matriculou_pct"]), g["matriculas"]]
                    for g in lista], [70, 22, 22, 22, 24, 20])]), Spacer(1, 4)]

    k, m, q = c["kpi"], c["matriculas"], c["integracao"]
    fim_d = date.fromisoformat(fim) - timedelta(days=1)
    e = [Paragraph("Inteligência comercial: o que leva à matrícula", T),
         Paragraph("Território Fit · leads novos de %s a %s · resultado conferido na Pacto "
                   "até %s" % (date.fromisoformat(ini).strftime("%d/%m"),
                               fim_d.strftime("%d/%m/%Y"),
                               datetime.now(TZ_SP).strftime("%d/%m/%Y")), S),
         Spacer(1, 8)]
    if s:
        e += [Paragraph("Resumo", H), Paragraph(esc(s["resumo"]), P)]

    e.append(Paragraph("Período, conversas e conversões", H))
    e.append(tabela(["Indicador", "Resultado"], [
        ["Conversas lidas", c["lidas"]],
        ["Leads de venda", c["leads"]],
        ["Contatos que não eram venda (ficaram de fora)", c["nao_venda"]],
        ["Matrículas confirmadas na Pacto", m["total"]],
        ["Lead para matrícula confirmada (indicador principal)", p0(k["lead_matricula"])],
        ["Lead para matrícula, só primeiro contato digital", p0(k["lead_matricula_digital"])],
        ["Visita realizada para matrícula", p0(k["visita_matricula"])],
        ["Lead para visita realizada", p0(k["lead_visita"])],
        ["Agendamento para comparecimento", p0(k["agendamento_comparecimento"])],
        ["Lead para agendamento", p0(k["lead_agendamento"])],
        ["Lead que recebeu resposta", p0(k["lead_resposta"])],
    ], [130, 50]))
    e.append(Spacer(1, 4))
    e.append(Paragraph(
        "Das %d matrículas, %d têm contrato pago localizado na Pacto e %d aparecem como aluno "
        "ativo sem contrato localizado. %d são rematrícula de ex-aluno. %d leads só têm plano "
        "de brinde e não contam como matrícula. Em %d matrículas mais de uma atendente "
        "participou da conversa." % (m["total"], m["por_contrato"], m["por_cadastro_ativo"],
                                     m["rematricula"], m["so_brinde"], c["assistidas"]), P))

    total = c["funil"][0][1] or 1
    linhas, ant = [], None
    for rot, n in c["funil"]:
        linhas.append([rot, n, p0(100.0 * n / total),
                       "—" if ant in (None, 0) else p0(100.0 * n / ant)])
        ant = n
    e.append(KeepTogether([Paragraph("Funil comercial", H),
                           tabela(["Etapa", "Leads", "Do total", "Da etapa anterior"],
                                  linhas, [104, 22, 24, 30])]))
    e.append(Spacer(1, 4))
    e.append(Paragraph(
        "O cadastro de visitante é feito na recepção quando a pessoa chega, então visitante "
        "cadastrado e visita realizada são medidos pelo mesmo registro. Quem veio e não foi "
        "cadastrado aparece como se não tivesse visitado. Por isso, e porque existe fechamento "
        "pelo WhatsApp, pode haver mais matrículas do que visitas registradas.", P))
    if m["tempo"]:
        e.append(KeepTogether([
            Paragraph("Tempo do primeiro contato até a matrícula", H3),
            tabela(["Faixa", "Matrículas"], [[a, b] for a, b in m["tempo"]], [90, 40])]))
    e.append(Paragraph(
        "O último lead do período entrou em %s e a conferência foi feita %d dias depois. "
        "Quem entrou no fim do período teve pouco tempo para matricular e ainda pode "
        "converter." % (fim_d.strftime("%d/%m"), c["janela"]["dias_minimos"]), P))

    if s:
        e.append(Paragraph("Principais descobertas", H))
        for i, d in enumerate(s["descobertas"], 1):
            e.append(KeepTogether([
                Paragraph("%d. %s" % (i, esc(d["titulo"])), H3),
                Paragraph(esc(d["observado"]), P),
                Paragraph("<b>Evidência:</b> " + esc(d["evidencia"]), P),
                Paragraph("<b>Ressalva:</b> " + esc(d["ressalva"]), P)]))

    e.append(Paragraph("Comparações entre abordagens", H))
    e.append(Paragraph(
        "Cada linha compara dois grupos de conversas pelo resultado real. O nível de evidência "
        "é calculado pelo tamanho dos grupos e pela diferença: Padrão exige pelo menos 30 "
        "conversas em cada grupo; Sinal, pelo menos 15; abaixo disso é Hipótese. Padrão "
        "validado só existe depois que o resultado se repetir em outro período.", P))
    lc = []
    for x in c["comparacoes"]:
        for g in (x["a"], x["b"]):
            lc.append([x["titulo"] if g is x["a"] else "", g["rotulo"], g["conversas"],
                       p0(g["visitou_pct"]), p0(g["matriculou_pct"]),
                       x["nivel"] if g is x["a"] else ""])
    e.append(tabela(["Comparação", "Grupo", "Conversas", "Visitou", "Matriculou", "Evidência"],
                    lc, [46, 52, 20, 18, 22, 22]))

    padroes = [x for x in c["comparacoes"] if x["nivel"] == "Padrão"]
    validados = [x for x in c["comparacoes"] if x["nivel"] == "Padrão validado"]
    e.append(Paragraph("Padrões validados", H))
    e.append(Paragraph(
        "%s Um padrão só é considerado validado quando se repete em leads de outro mês. %s" % (
            "Validados: %s." % "; ".join(x["titulo"].lower() for x in validados) if validados
            else "Nenhum até agora.",
            "As comparações que chegaram ao nível Padrão foram: %s." % "; ".join(
                x["titulo"].lower() for x in padroes) if padroes
            else "Nenhuma outra comparação chegou ao nível Padrão."), P))

    if s:
        e.append(Paragraph("Hipóteses", H))
        txt = {h: t for h, t, _ in HIPOTESES}
        e.append(tabela(["Hipótese", "Situação", "Evidência", "Justificativa"],
                        [[txt.get(h["codigo"], h["codigo"]), h["situacao"], h["nivel"],
                          h["justificativa"]] for h in s["hipoteses"]], [52, 26, 20, 82]))
        for tit, campo in (("Abordagens com melhor desempenho", "melhor_desempenho"),
                           ("Abordagens com pior desempenho", "pior_desempenho")):
            e.append(Paragraph(tit, H))
            e += [Paragraph(esc(x), B, bulletText="•") for x in s[campo]] or \
                 [Paragraph("Nada a destacar neste ciclo.", P)]
        e.append(Paragraph("Objeções mais frequentes", H))
        e.append(Paragraph(esc(s["objecoes"]), P))
    e += grupos("Objeção principal", c["por_objecao"], "Objeção")
    e += grupos("Como a objeção foi tratada", c["por_tratamento_objecao"], "Tratamento")
    if s:
        for tit, campo in (("Novos aprendizados sobre rapport", "rapport"),
                           ("Novos aprendizados sobre follow-up", "followup"),
                           ("Novos aprendizados sobre convite (CTA)", "cta")):
            e.append(Paragraph(tit, H))
            e.append(Paragraph(esc(s[campo]), P))
    e += grupos("Tipo de convite, primeiro contato digital", c["por_cta"], "Convite")
    e += grupos("Primeira resposta da academia, primeiro contato digital", c["por_abertura"],
                "Abertura")

    if s:
        e.append(Paragraph("Recomendações para a Clara", H))
        if s["recomendacoes_clara"]:
            e.append(Paragraph("Propostas para sua aprovação. Nada foi alterado na Clara.", P))
            for i, r in enumerate(s["recomendacoes_clara"], 1):
                e.append(KeepTogether([
                    Paragraph("%d. %s" % (i, esc(r["aprendizado"])), H3),
                    Paragraph("<b>Quando utilizar:</b> " + esc(r["quando_utilizar"]), P),
                    Paragraph("<b>Como aplicar:</b> " + esc(r["como_aplicar"]), P),
                    Paragraph("<b>Exemplo:</b> " + esc(r["exemplo"]), P),
                    Paragraph("<b>Evitar:</b> " + esc(r["evitar"]), P),
                    Paragraph("<b>Evidência:</b> " + esc(r["evidencia"]), P),
                    Paragraph("<b>Resultado:</b> " + esc(r["resultado"]), P),
                    Paragraph("<b>Confiança:</b> " + esc(r["confianca"]), P)]))
        else:
            e.append(Paragraph(
                "Nenhuma recomendação neste ciclo. Nenhuma comparação atingiu o nível de "
                "evidência exigido para virar regra da Clara, e nada foi alterado nela.", P))
        e.append(Paragraph("Testes recomendados", H))
        for i, t in enumerate(s["testes"], 1):
            e.append(KeepTogether([
                Paragraph("%d. %s" % (i, esc(t["hipotese"])), H3),
                Paragraph("<b>Segmento:</b> " + esc(t["segmento"]), P),
                Paragraph("<b>Estratégia A:</b> " + esc(t["estrategia_a"]), P),
                Paragraph("<b>Estratégia B:</b> " + esc(t["estrategia_b"]), P),
                Paragraph("<b>Métrica:</b> %s · <b>Mínimo de casos:</b> %s" % (
                    esc(t["metrica"]), esc(t["minimo_de_casos"])), P)]))
        e.append(Paragraph("Aprendizados que devem ser removidos ou revisados", H))
        e.append(Paragraph(esc(s["rever"]), P))

    e.append(Paragraph("Resultado por origem e por perfil", H))
    e += grupos("Origem do lead", c["por_origem"], "Origem")
    e += grupos("Primeiro pedido do lead, primeiro contato digital", c["por_primeiro_pedido"],
                "Primeiro pedido")
    e += grupos("Intenção do lead, primeiro contato digital", c["por_intencao"], "Intenção")
    e += grupos("Tempo até a primeira resposta humana, primeiro contato digital",
                c["por_tempo_resposta"], "Tempo de resposta")
    if m["planos"]:
        e.append(KeepTogether([
            Paragraph("Planos contratados", H3),
            tabela(["Plano", "Matrículas"], [[a, b] for a, b in m["planos"]], [110, 30]),
            Spacer(1, 3),
            Paragraph("Valor total dos contratos localizados: R$ %s." % (
                "{:,.2f}".format(m["receita_contratos"]).replace(",", "X").replace(
                    ".", ",").replace("X", ".")), P)]))

    e.append(Paragraph("Qualidade da integração entre CRM e Pacto", H))
    e.append(tabela(["Verificação", "Resultado"], [
        ["Leads com cadastro localizado na Pacto pelo telefone", q["leads_vinculo_confirmado"]],
        ["Leads sem cadastro na Pacto", q["leads_sem_cadastro_na_pacto"]],
        ["Leads com vínculo não confirmado (telefone em mais de um cadastro)",
         q["leads_vinculo_nao_confirmado"]],
        ["Visitas com presença na agenda e sem cadastro de visitante",
         q["visitou_sem_cadastro_de_visitante"]],
        ["Taxa de vínculo das visitas realizadas", p0(q["match_pct"])],
        ["CRM indica matrícula e a Pacto não confirma", q["crm_diz_matriculou_pacto_nao_confirma"]],
        ["Pacto confirma matrícula e o CRM não marcou", q["pacto_confirma_crm_nao_marcou"]],
        ["Visitantes da Pacto sem telefone (toda a base)",
         "%d de %d" % (q["visitantes_sem_telefone"], q["visitantes"])],
        ["Vendas do período sem código de cliente",
         "%d de %d" % (q["vendas_sem_codigo_cliente"], q["vendas_no_periodo"])],
    ], [130, 50]))
    e.append(Spacer(1, 4))
    e.append(Paragraph("Esses são problemas de cadastro e de integração, não de atendimento. "
                       "Matrícula feita com outro telefone aparece aqui como não convertida.", P))

    if s:
        e.append(Paragraph("Limites desta análise", H))
        e += [Paragraph(esc(x), B, bulletText="•") for x in s["limitacoes"]]
    e.append(Paragraph(
        "As conversas foram lidas por inteligência artificial, todas com o mesmo roteiro e sem "
        "informar ao leitor se a pessoa matriculou. Conversas muito longas tiveram o meio "
        "resumido. %d conversas ficaram sem leitura." % c["sem_leitura"], P))

    if c["por_consultora"]:
        e.append(PageBreak())
        e.append(Paragraph("Somente para o André: resultado por consultora", H))
        e.append(Paragraph(
            "A consultora é a atendente principal de cada conversa. As origens e a intenção "
            "dos leads variam entre elas, então a diferença de taxa não mede sozinha a "
            "qualidade do atendimento. Não é um ranking.", P))
        e += grupos("Conversas de venda", c["por_consultora"], "Consultora")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title="Inteligência comercial: o que leva à matrícula",
                            author="Território Fit")

    def rodape(cv, d):
        cv.saveState()
        cv.setFont(fonte, 8)
        cv.setFillColor(colors.HexColor("#777777"))
        cv.drawString(15 * mm, 8 * mm, "Território Fit · inteligência comercial · relatório interno")
        cv.drawRightString(195 * mm, 8 * mm, "Página %d" % d.page)
        cv.restoreState()

    doc.build(e, onFirstPage=rodape, onLaterPages=rodape)
    return buf.getvalue()


# ------------------------------------------------------------------ guarda
def guardar_leitura(key, c, ini, fim):
    """Leitura por lead em arquivo privado (sem nome, telefone nem texto da conversa)."""
    h = base._h(key)
    requests.post(f"{base.SUPABASE_URL}/storage/v1/bucket", headers={**h, "Content-Type": "application/json"},
                  json={"id": BALDE, "name": BALDE, "public": False}, timeout=30)
    fora = ("conversa", "aprendizado", "evidencia", "pacto", "id", "criado")
    linhas = [{**{k: v for k, v in r.items() if k not in fora},
               "criado": r["criado"].isoformat(),
               "matricula_por": r["pacto"]["matricula_por"], "plano": r["pacto"]["plano"],
               "vinculo": r["pacto"]["vinculo"]} for r in c["_R"]]
    nome = "ciclos/%s_a_%s.json" % (ini, fim)
    r = requests.post(f"{base.SUPABASE_URL}/storage/v1/object/{BALDE}/{nome}",
                      headers={**h, "Content-Type": "application/json", "x-upsert": "true"},
                      data=json.dumps({"periodo": [ini, fim], "leituras": linhas},
                                      ensure_ascii=False, default=str).encode("utf-8"), timeout=60)
    print(f"[guarda] leitura por lead HTTP {r.status_code}")
    return nome if r.status_code in (200, 201) else None


def main():
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    token = os.environ.get("UAZAPI_TOKEN_CEO", "").replace("﻿", "").strip()
    destino = (os.environ.get("RELATORIO_PARA") or "5516992290338").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    ini = (os.environ.get("DATA_INI") or "2026-08-25").strip()
    fim = (os.environ.get("DATA_FIM") or "2026-09-26").strip()
    teto = float(os.environ.get("TETO_USD") or "20")
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

    linhas, fora, qual = montar(key, ini, fim)
    print(f"[universo] {len(linhas)} leads com conversa | matrícula confirmada "
          f"{sum(1 for u in linhas if u['matriculou'])} | fora: {dict(fora)}")
    alvo = linhas
    if maxc:
        sim = [u for u in linhas if u["matriculou"]][:maxc // 2]
        alvo = sim + [u for u in linhas if not u["matriculou"]][:maxc - len(sim)]
    res = ler(client, alvo, teto)
    print(f"[leitura] {len(res)} de {len(alvo)} | custo estimado US$ {base.custo_usd():.2f}")
    if len(res) < 0.6 * len(alvo):
        print("[erro] leitura insuficiente")
        if not dry:
            base.enviar_texto(token, destino,
                              "Não consegui terminar o ciclo de inteligência comercial: a "
                              "leitura das conversas falhou. Veja o saldo da API da Anthropic "
                              "e me peça pra rodar de novo.")
        return 1
    c = calcular(alvo, res, qual, fim)
    s = sintetizar(client, c)
    print(f"[sintese] {'ok' if s else 'falhou — sai só com os números'} | custo estimado "
          f"US$ {base.custo_usd():.2f} | chamadas {base.USO['chamadas']} | "
          f"comparações: {dict(Counter(x['nivel'] for x in c['comparacoes']))}")
    pdf = gerar_pdf(c, s, ini, fim)
    print(f"[pdf] {len(pdf) // 1024} KB")
    if dry:
        with open(os.environ.get("SAIDA_PDF") or "ciclo-inteligencia-comercial.pdf", "wb") as fh:
            fh.write(pdf)
        print("[DRY] PDF gravado localmente, nada enviado nem guardado.")
        return 0

    arquivo = guardar_leitura(key, c, ini, fim)
    fim_d = date.fromisoformat(fim) - timedelta(days=1)
    requests.post(f"{base.SUPABASE_URL}/rest/v1/agent_activity",
                  headers={**base._h(key), "Content-Type": "application/json",
                           "Prefer": "return=minimal"},
                  json={"agent_slug": "inteligencia-comercial",
                        "title": "Ciclo de inteligência comercial",
                        "detail": "Leads de %s a %s: %d conversas de venda, %d matrículas "
                                  "confirmadas na Pacto (%s)." % (
                                      date.fromisoformat(ini).strftime("%d/%m"),
                                      fim_d.strftime("%d/%m"), c["leads"],
                                      c["matriculas"]["total"],
                                      "%.0f%%" % (c["kpi"]["lead_matricula"] or 0)),
                        "status": "concluido",
                        "metadata": {"campanha": CAMPANHA, "data_ini": ini, "data_fim": fim,
                                     "numeros": json.loads(json.dumps(
                                         {k: v for k, v in publico(c).items()
                                          if k != "por_consultora"}, default=str)),
                                     "sintese": s, "arquivo_leitura": arquivo,
                                     "custo_usd": round(base.custo_usd(), 2)}},
                  timeout=30)

    padroes = sum(1 for x in c["comparacoes"] if x["nivel"].startswith("Padrão"))
    leg = ["*Inteligência comercial: o que leva à matrícula*",
           "Leads de %s a %s." % (date.fromisoformat(ini).strftime("%d/%m"),
                                  fim_d.strftime("%d/%m")),
           "%d conversas de venda, %d matrículas confirmadas na Pacto (%.0f%%)." % (
               c["leads"], c["matriculas"]["total"], c["kpi"]["lead_matricula"] or 0),
           "Quem visitou e matriculou: %s." % ("%.0f%%" % c["kpi"]["visita_matricula"]
                                               if c["kpi"]["visita_matricula"] is not None
                                               else "sem dados"),
           "%d comparações com evidência forte. Recomendações para a Clara: %d, para sua "
           "aprovação." % (padroes, len(s["recomendacoes_clara"]) if s else 0),
           "Detalhes no PDF."]
    nome = "inteligencia-comercial-%s.pdf" % fim_d.strftime("%d-%m")
    if not base.enviar_pdf(token, destino, pdf, nome, "\n".join(leg)):
        base.enviar_texto(token, destino, "\n".join(leg[:-1])
                          + "\nNão consegui anexar o PDF deste ciclo.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
