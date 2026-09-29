# -*- coding: utf-8 -*-
"""
Leitura diaria do agente de Inteligencia Comercial — continua de onde parou.

Pedido do Andre em 29/09/2026: depois do primeiro ciclo, o agente deve seguir
lendo todo dia o que chega no CRM, SEM reler tudo: so as mensagens novas de
cada conversa.

Como funciona:
  - para cada lead fica guardado ate que mensagem a conversa ja foi lida e a
    leitura feita ate ali (arquivo privado no Storage do CRM);
  - todo dia a rotina procura as conversas que receberam mensagem depois da
    ultima execucao. Conversa nova e lida inteira; conversa ja lida recebe so
    as mensagens novas, junto com a leitura anterior, e a leitura e atualizada;
  - o resultado de cada lead na Pacto (visita, matricula) e conferido de novo
    todo dia, sem custo de leitura;
  - as comparacoes entre abordagens sao recalculadas com a base acumulada.
    Quando a mesma diferenca se repete em leads de dois meses, o nivel sobe
    para "Padrao validado".

Nao manda mensagem de rotina. O PDF so sai no dia 1 de cada mes, quando uma
comparacao sobe para Padrao ou Padrao validado, ou quando pedido (ENVIAR=1).

Somente leitura no CRM e na Pacto. Nao altera a Clara.

Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO. Opcional: INICIO_BASE (AAAA-MM-DD, padrao
2026-08-25) | TETO_USD (padrao 3 por dia) | ENVIAR=1 | DRY_RUN=1 (nao grava
nem envia) | MAX_CONVERSAS (teste) | TESTE_INCREMENTAL=1 (teste: le metade da
conversa e depois so o resto) | SAIDA_PDF | RELATORIO_PARA | MODELO.
"""

import copy
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import requests

import ciclo_inteligencia_comercial as ciclo
import relatorio_vendas_perdidas as base

CAMPANHA = "leitura-diaria-inteligencia"
BALDE = ciclo.BALDE
ESTADO, CONTROLE = "estado/leituras.json", "estado/controle.json"
TZ_SP = base.TZ_SP

ESQ = copy.deepcopy(ciclo.ESQ_LEITURA)
ESQ["properties"]["resumo"] = {"type": "string"}
ESQ["required"] = list(ESQ["properties"])
CAMPOS = list(ESQ["properties"])

REGRA_RESUMO = """
- "resumo": até 60 palavras, sem nome nem telefone, contando em ordem o que aconteceu na conversa até a última mensagem lida: o que o lead pediu, o que a academia respondeu, o que ficou combinado e como a conversa está agora. Esse resumo será usado para continuar a leitura quando chegarem mensagens novas."""

SIS_COMPLETA = ciclo.SIS_LEITURA + REGRA_RESUMO

SIS_INCREMENTAL = ciclo.SIS_LEITURA + REGRA_RESUMO + """

ATENÇÃO: esta conversa já foi lida antes. Você recebe a LEITURA ANTERIOR, que registra tudo o que aconteceu até a última mensagem lida, e em seguida só as MENSAGENS NOVAS. Você não vai receber as mensagens antigas. Devolva a leitura ATUALIZADA da conversa inteira:
- Mantenha como estavam os campos que descrevem o começo da conversa: "tipo" (a não ser que as mensagens novas mostrem que não era venda), "primeiro_contato", "primeiro_pedido", "primeira_resposta_de" e "abertura_tipo".
- Um campo verdadeiro continua verdadeiro. Um campo falso passa a verdadeiro se as mensagens novas mostrarem o comportamento.
- "convite_antes_do_preco" e "preco_pedido_pelo_lead" só mudam se o primeiro preço da conversa aparecer nas mensagens novas.
- "cta_tipo" fica com o convite mais forte entre o anterior e o das mensagens novas (dia_e_hora é mais forte que duas_opcoes, que é mais forte que aberto).
- "followups" é o total: some as retomadas novas às anteriores. "followup_tipo" é o tipo predominante considerando tudo.
- "atendentes_humanas" é o total de atendentes diferentes, somando quem apareceu agora.
- "objecao_principal" e "objecao_tratamento" são atualizados se surgir objeção nova ou se a anterior for tratada.
- "aprendizado", "evidencia" e "resumo" devem refletir a conversa inteira, incluindo o que aconteceu agora."""


# ------------------------------------------------------------------ Storage
def st_get(key, caminho):
    r = requests.get(f"{base.SUPABASE_URL}/storage/v1/object/{BALDE}/{caminho}",
                     headers=base._h(key), timeout=120)
    return r.json() if r.status_code == 200 else None


def st_put(key, caminho, obj):
    h = base._h(key)
    requests.post(f"{base.SUPABASE_URL}/storage/v1/bucket",
                  headers={**h, "Content-Type": "application/json"},
                  json={"id": BALDE, "name": BALDE, "public": False}, timeout=30)
    r = requests.post(f"{base.SUPABASE_URL}/storage/v1/object/{BALDE}/{caminho}",
                      headers={**h, "Content-Type": "application/json", "x-upsert": "true"},
                      data=json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                      timeout=120)
    print(f"[guarda] {caminho.split('/')[-1]} HTTP {r.status_code}")
    return r.status_code in (200, 201)


def st_list(key, prefixo):
    r = requests.post(f"{base.SUPABASE_URL}/storage/v1/object/list/{BALDE}",
                      headers={**base._h(key), "Content-Type": "application/json"},
                      json={"prefix": prefixo, "limit": 200}, timeout=60)
    return r.json() if r.status_code == 200 else []


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def carregar_estado(key):
    """Estado guardado; na primeira vez, parte da leitura do primeiro ciclo."""
    estado, ctrl = st_get(key, ESTADO), st_get(key, CONTROLE)
    if estado is not None and ctrl is not None:
        return estado, ctrl
    estado, marca = {}, None
    for it in st_list(key, "ciclos/"):
        arq = st_get(key, "ciclos/" + it["name"])
        if not arq:
            continue
        quando = it.get("updated_at") or it.get("created_at")
        marca = max(marca, quando) if marca else quando
        for r in arq["leituras"]:
            estado[r["lead_id"]] = {
                "lida_ate": quando, "criado": r["criado"], "origem": r["origem"],
                "hora_1a": r["hora_1a"], "min_humano": r["min_humano"],
                "n_lead": r["n_lead"], "n_equipe": r["n_equipe"],
                "respondido": r["respondido"],
                "leitura": {k: r[k] for k in CAMPOS if k in r}}
    print(f"[estado] primeira execução: {len(estado)} leituras herdadas do primeiro ciclo")
    return estado, {"ultima_execucao": marca, "pendentes": [], "niveis": {}, "execucoes": 0}


# ------------------------------------------------------------------ CRM
def buscar(key, inicio, ctrl, estado):
    leads = base.sb_todos(key, "leads", {
        "select": "id,name,phone,status,source,tags,created_at,"
                  "camp:metadata->origem_whatsapp->>campanha_nome",
        "created_at": f"gte.{inicio}T03:00:00Z", "order": "created_at"})
    por_id = {l["id"]: l for l in leads}
    if ctrl.get("ultima_execucao"):
        marca = ts(ctrl["ultima_execucao"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        novas = base.sb_todos(key, "whatsapp_messages", {
            "select": "lead_id", "created_at": f"gt.{marca}", "group_id": "is.null",
            "lead_id": "not.is.null"}, teto=100000)
        cand = {m["lead_id"] for m in novas} & set(por_id)
        n_msgs = len(novas)
    else:
        cand, n_msgs = set(), 0
    cand |= set(ctrl.get("pendentes") or []) & set(por_id)
    cand |= {i for i in por_id if i not in estado}  # nunca avaliados
    ids = sorted(cand)
    msgs = []
    for i in range(0, len(ids), 40):
        msgs += base.sb_todos(key, "whatsapp_messages", {
            "select": "lead_id,is_from_me,content,message_type,created_at,"
                      "sb:metadata->>sent_by",
            "lead_id": "in.(%s)" % ",".join(ids[i:i + 40]),
            "group_id": "is.null", "order": "created_at.asc"}, teto=50000)
    ag = base.sb_todos(key, "agendamentos", {
        "select": "lead_id,aula,data_agendamento,horario,consultor,veio,fechou",
        "lead_id": "not.is.null"}, teto=20000)
    ativos = {re.sub(r"\D", "", x["phone8"] or "")[-8:]
              for x in base.sb_todos(key, "pacto_alunos_ativos", {"select": "phone8"}, 10000)}
    print(f"[crm] leads na base {len(leads)} | mensagens novas {n_msgs} | conversas a "
          f"avaliar {len(ids)} | agendamentos {len(ag)}")
    return leads, por_id, ids, msgs, ag, ativos


def linha(m):
    c = re.sub(r"\s+", " ", (m.get("content") or "").strip()) or \
        "[%s]" % (m.get("message_type") or "midia")
    return "[%s] %s: %s" % (base.hora(m["created_at"]).strftime("%d/%m %H:%M"), base.quem(m),
                            c[:160 if c.startswith("🔧") else 380])


def encurta(linhas, teto=120):
    if len(linhas) <= teto:
        return linhas
    return linhas[:80] + ["[... %d mensagens omitidas ...]" % (len(linhas) - teto)] + linhas[-40:]


def ler(client, tarefas, teto):
    """tarefas: (lead_id, sistema, texto). Devolve {lead_id: leitura}."""
    res = {}

    def uma(t):
        if base.custo_usd() > teto:
            return t[0], None
        for _ in range(3):
            r = base.chamar(client, t[1], t[2], ESQ, max_tokens=3000)
            if r:
                return t[0], r
        return t[0], None

    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(uma, t) for t in tarefas]):
            i, r = f.result()
            if r:
                res[i] = r
    return res


# ------------------------------------------------------------------ niveis
def validar_entre_meses(linhas, res, qual, fim, c):
    """Sobe para 'Padrão validado' o que se repete em leads de dois meses."""
    meses = defaultdict(list)
    for u in linhas:
        meses[u["criado"].strftime("%Y-%m")].append(u)
    por_mes = {}
    for mes, sel in meses.items():
        if len(sel) >= 60:
            cm = ciclo.calcular(sel, res, qual, fim)
            por_mes[mes] = {x["chave"]: x for x in cm["comparacoes"]}
    for x in c["comparacoes"]:
        if x["nivel"] != "Padrão":
            continue
        sinal = (x["a"]["matriculou_pct"] or 0) - (x["b"]["matriculou_pct"] or 0)
        ok = 0
        for mes, comp in por_mes.items():
            y = comp.get(x["chave"])
            if not y or min(y["a"]["conversas"], y["b"]["conversas"]) < 15:
                continue
            d = (y["a"]["matriculou_pct"] or 0) - (y["b"]["matriculou_pct"] or 0)
            if abs(d) >= 5 and (d > 0) == (sinal > 0):
                ok += 1
        if ok >= 2:
            x["nivel"] = "Padrão validado"
    return sorted(por_mes)


def main():
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    token = os.environ.get("UAZAPI_TOKEN_CEO", "").replace("﻿", "").strip()
    destino = (os.environ.get("RELATORIO_PARA") or "5516992290338").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    forcar = os.environ.get("ENVIAR", "") == "1"
    inicio = (os.environ.get("INICIO_BASE") or "2026-08-25").strip()
    teto = float(os.environ.get("TETO_USD") or "3")
    maxc = int(os.environ.get("MAX_CONVERSAS") or "0")
    teste_inc = os.environ.get("TESTE_INCREMENTAL", "") == "1"
    if not key or (not token and not dry):
        print("Faltam envs SUPABASE_KEY / UAZAPI_TOKEN_CEO")
        return 1
    import anthropic
    akey = base._anthropic_key(key)
    if not akey:
        print("ANTHROPIC_API_KEY ausente na config do CRM")
        return 1
    client = anthropic.Anthropic(api_key=akey, max_retries=5, timeout=300.0)

    agora = datetime.now(timezone.utc)
    hoje = agora.astimezone(TZ_SP).date()
    estado, ctrl = carregar_estado(key)
    leads, por_id, ids, msgs, ag, ativos = buscar(key, inicio, ctrl, estado)
    px = ciclo.carregar_pacto(key, inicio)

    por_msgs = defaultdict(list)
    for m in msgs:
        por_msgs[m["lead_id"]].append(m)
    cand = [por_id[i] for i in ids]
    univ, fora = base.montar_universo(cand, msgs, ag, ativos)
    dentro = {u["id"]: u for u in univ}
    marca_nova = agora.isoformat()

    # quem fica de fora do universo: so registra, sem leitura
    for l in cand:
        if l["id"][:8] not in dentro and not (estado.get(l["id"]) or {}).get("leitura"):
            estado[l["id"]] = {"ignorado": True, "lida_ate": marca_nova}

    tarefas, meta, sem_novidade = [], {}, 0
    for l in cand:
        u = dentro.get(l["id"][:8])
        if not u:
            continue
        ms = por_msgs.get(l["id"], [])
        r = ciclo.resultado_pacto(l, px)
        corte = r["matricula_data"] + timedelta(days=1) if r["matricula_data"] else None
        validas = [m for m in ms if not corte or base.hora(m["created_at"]).date() <= corte]
        rec = [m for m in ms if not m["is_from_me"]]
        meta[l["id"]] = {
            "lida_ate": ms[-1]["created_at"], "criado": base.hora(l["created_at"]).date().isoformat(),
            "origem": u["origem"], "hora_1a": u["hora_1a"], "min_humano": u["min_humano"],
            "n_lead": len(rec), "n_equipe": len(ms) - len(rec),
            "respondido": any(m["is_from_me"] and m["created_at"] >= rec[0]["created_at"]
                              for m in ms)}
        ant = estado.get(l["id"]) or {}
        cab = "CONVERSA | lead criado em %s | origem %s\n" % (
            base.hora(l["created_at"]).strftime("%d/%m"),
            ciclo.ORIGENS.get(u["origem"], u["origem"]))
        if ant.get("leitura"):
            novas = [m for m in validas if ts(m["created_at"]) > ts(ant["lida_ate"])]
            if not novas:
                sem_novidade += 1
                estado[l["id"]] = {**ant, **meta[l["id"]]}
                continue
            texto = (cab + "LEITURA ANTERIOR (até a última mensagem lida, em %s):\n%s\n\n"
                     "MENSAGENS NOVAS:\n%s" % (
                         base.hora(ant["lida_ate"]).strftime("%d/%m %H:%M"),
                         json.dumps(ant["leitura"], ensure_ascii=False),
                         "\n".join(encurta([linha(m) for m in novas]))))
            tarefas.append((l["id"], SIS_INCREMENTAL, texto, "incremental", len(novas)))
        else:
            tarefas.append((l["id"], SIS_COMPLETA,
                            cab + "\n".join(encurta([linha(m) for m in validas])),
                            "completa", len(validas)))
    # conversas com resposta do lead primeiro; o que passar do teto fica para amanha
    tarefas.sort(key=lambda t: (t[3] != "completa", -t[4]))
    if maxc:
        tarefas = tarefas[:maxc]
    print(f"[leitura] a ler: {len(tarefas)} ({dict(Counter(t[3] for t in tarefas))}) | "
          f"mensagens a ler: {sum(t[4] for t in tarefas)} | sem novidade: {sem_novidade} | "
          f"fora do universo: {dict(fora)}")

    if teste_inc:  # teste: le so a primeira metade e depois continua com o resto
        t1, guarda = [], {}
        for t in [x for x in tarefas if x[3] == "completa" and x[4] >= 8][:6]:
            ln = t[2].split("\n")
            meio = 1 + (len(ln) - 1) // 2
            t1.append((t[0], SIS_COMPLETA, "\n".join(ln[:meio]), "completa", meio))
            guarda[t[0]] = (ln[0], ln[meio:])
        r1 = ler(client, t1, teto)
        t2 = [(i, SIS_INCREMENTAL, "%s\nLEITURA ANTERIOR (até a última mensagem lida):\n%s\n\n"
               "MENSAGENS NOVAS:\n%s" % (guarda[i][0], json.dumps(r, ensure_ascii=False),
                                         "\n".join(guarda[i][1])), "incremental", len(guarda[i][1]))
              for i, r in r1.items()]
        r2 = ler(client, t2, teto)
        rc = ler(client, [(t[0], t[1], t[2], t[3], t[4]) for t in tarefas if t[0] in r2], teto)
        campos = [k for k in CAMPOS if k not in ("aprendizado", "evidencia", "resumo", "consultora")]
        iguais = sum(1 for i in r2 if i in rc for k in campos if r2[i][k] == rc[i][k])
        total = sum(1 for i in r2 if i in rc for _ in campos)
        dif = Counter(k for i in r2 if i in rc for k in campos if r2[i][k] != rc[i][k])
        print(f"[teste incremental] {len(r2)} conversas | campos iguais à leitura completa: "
              f"{iguais} de {total} | diferenças por campo: {dict(dif)} | custo "
              f"US$ {base.custo_usd():.2f}")
        return 0

    res = ler(client, tarefas, teto)
    pend = [t[0] for t in tarefas if t[0] not in res]
    for i, r in res.items():
        estado[i] = {**meta[i], "leitura": r}
    print(f"[leitura] lidas {len(res)} | ficaram para amanhã {len(pend)} | custo estimado "
          f"US$ {base.custo_usd():.2f} | chamadas {base.USO['chamadas']}")

    # base acumulada com o resultado de hoje na Pacto
    ags = defaultdict(list)
    for a in ag:
        if (a.get("aula") or "") != "Currículo":
            ags[a["lead_id"]].append(a)
    linhas, leit = [], {}
    for lid, e in estado.items():
        l = por_id.get(lid)
        if not l or not e.get("leitura"):
            continue
        r = ciclo.resultado_pacto(l, px)
        criado = date.fromisoformat(e["criado"])
        a_lead = ags.get(lid, [])
        linhas.append({
            "id": lid, "lead_id": lid, "origem": e["origem"], "criado": criado,
            "hora_1a": e["hora_1a"], "min_humano": e["min_humano"], "n_lead": e["n_lead"],
            "n_equipe": e["n_equipe"], "respondido": e["respondido"],
            "agendou": bool(a_lead),
            "visitou": any(a.get("veio") is True for a in a_lead) or bool(r["visita_data"]),
            "matriculou": r["matricula"],
            "crm_fechou": (l["status"] in ("cliente", "inadimplente")
                           or any(a.get("fechou") is True for a in a_lead)),
            "pacto": r, "conversa": [],
            "dias_ate_matricula": ((r["matricula_data"] - criado).days
                                   if r["matricula_data"] else None),
            "dias_ate_visita": ((r["visita_data"] - criado).days if r["visita_data"] else None)})
        leit[lid] = {"aprendizado": "", "evidencia": "", **e["leitura"]}
    if len(linhas) < 30:
        print("[base] ainda pequena demais para comparar; nada a calcular")
        if not dry:
            st_put(key, ESTADO, estado)
            st_put(key, CONTROLE, {**ctrl, "ultima_execucao": marca_nova, "pendentes": pend,
                                   "execucoes": ctrl.get("execucoes", 0) + 1})
        return 0
    fim = (hoje + timedelta(days=1)).isoformat()
    c = ciclo.calcular(linhas, leit, px[3], fim)
    meses = validar_entre_meses(linhas, leit, px[3], fim, c)
    niveis = {x["chave"]: x["nivel"] for x in c["comparacoes"]}
    antes = ctrl.get("niveis") or {}
    ordem = {"Hipótese": 0, "Sinal": 1, "Padrão": 2, "Padrão validado": 3}
    subiu = [x["titulo"] for x in c["comparacoes"]
             if ordem[x["nivel"]] >= 2 and ordem[x["nivel"]] > ordem.get(antes.get(x["chave"], "Hipótese"), 0)]
    primeira = not antes
    print(f"[base] {c['leads']} leads de venda | matrículas confirmadas {c['matriculas']['total']} "
          f"| meses comparáveis {len(meses)} | níveis {dict(Counter(niveis.values()))} | "
          f"subiram hoje {len(subiu)}")

    enviar = forcar or hoje.day == 1 or (bool(subiu) and not primeira)
    motivo = ("pedido" if forcar else "fechamento do mês" if hoje.day == 1
              else "evidência nova" if enviar else "")
    pdf = s = None
    if enviar or dry:
        s = ciclo.sintetizar(client, c)
        pdf = ciclo.gerar_pdf(c, s, inicio, fim)
        print(f"[pdf] {len(pdf) // 1024} KB | custo estimado US$ {base.custo_usd():.2f}")
    if dry:
        with open(os.environ.get("SAIDA_PDF") or "leitura-diaria-inteligencia.pdf", "wb") as fh:
            fh.write(pdf)
        print("[DRY] nada gravado nem enviado.")
        return 0

    st_put(key, ESTADO, estado)
    st_put(key, CONTROLE, {"ultima_execucao": marca_nova, "pendentes": pend, "niveis": niveis,
                           "execucoes": ctrl.get("execucoes", 0) + 1,
                           "ultimo_envio": hoje.isoformat() if enviar else ctrl.get("ultimo_envio")})
    requests.post(f"{base.SUPABASE_URL}/rest/v1/agent_activity",
                  headers={**base._h(key), "Content-Type": "application/json",
                           "Prefer": "return=minimal"},
                  json={"agent_slug": "inteligencia-comercial",
                        "title": "Leitura diária das conversas",
                        "detail": "%d conversas atualizadas hoje (%d novas, %d continuadas). "
                                  "Base: %d leads de venda, %d matrículas confirmadas na Pacto "
                                  "(%.0f%%).%s" % (
                                      len(res),
                                      sum(1 for t in tarefas if t[3] == "completa" and t[0] in res),
                                      sum(1 for t in tarefas if t[3] == "incremental" and t[0] in res),
                                      c["leads"], c["matriculas"]["total"],
                                      c["kpi"]["lead_matricula"] or 0,
                                      " Evidência nova: %s." % "; ".join(subiu) if subiu else ""),
                        "status": "concluido",
                        "metadata": {"campanha": CAMPANHA, "data": hoje.isoformat(),
                                     "lidas": len(res), "pendentes": len(pend),
                                     "kpi": c["kpi"], "niveis": niveis, "subiram": subiu,
                                     "custo_usd": round(base.custo_usd(), 2)}},
                  timeout=30)
    if not enviar:
        print("[envio] nada a enviar hoje (sem fechamento de mês nem evidência nova)")
        return 0
    leg = ["*Inteligência comercial: o que leva à matrícula*",
           "Motivo do envio: %s." % motivo,
           "Base acumulada desde %s: %d leads de venda, %d matrículas confirmadas na Pacto "
           "(%.0f%%)." % (date.fromisoformat(inicio).strftime("%d/%m"), c["leads"],
                          c["matriculas"]["total"], c["kpi"]["lead_matricula"] or 0)]
    if subiu:
        leg.append("Evidência nova: %s." % "; ".join(subiu))
    leg += ["Recomendações para a Clara: %d, para sua aprovação." % (
                len(s["recomendacoes_clara"]) if s else 0), "Detalhes no PDF."]
    nome = "inteligencia-comercial-%s.pdf" % hoje.strftime("%d-%m")
    if not base.enviar_pdf(token, destino, pdf, nome, "\n".join(leg)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
