# -*- coding: utf-8 -*-
"""
Registro automatico de agendamento — leva pra aba Agendamentos do CRM as
visitas e aulas experimentais combinadas na conversa que ninguem lancou.

Plano de acao das vendas perdidas (Andre, 28/09/2026). A confirmacao de
visita (confirmacao_visita.py) so enxerga o que esta na aba Agendamentos;
o que fica so na conversa nao recebe lembrete e nao entra na conta de falta.

Fluxo:
  1. Conversas com mensagem do lead nas ultimas JANELA_HORAS (padrao 30).
  2. Fora: aluno ativo, candidato a vaga (tag CV), contato interno, opt-out.
  3. Filtro barato: so segue a conversa que cita dia/hora E visita/aula.
  4. Claude le a conversa (ultimos 7 dias) e devolve o combinado que esta
     valendo: dia, horario, tipo, quem combinou e grau de certeza.
  5. So grava com certeza ALTA, aceite claro do lead e data de hoje em diante:
       - sem agendamento futuro no CRM  -> cria a linha;
       - com agendamento futuro diferente -> corrige dia/horario (remarcacao);
       - igual ao que ja esta no CRM     -> nao mexe.
     Toda gravacao gera evento na linha do agendamento ("Leitura da conversa").

Repo publico: o log so tem contagens. DETALHE=1 (uso local) mostra nomes.

Env: SUPABASE_KEY. Opcional: DRY_RUN (padrao 1 = so simula; 0 grava) |
JANELA_HORAS | MAX_CONVERSAS | TETO_USD (padrao 3) | MODELO | DETALHE=1.
"""

import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
TZ_SP = timezone(timedelta(hours=-3))
CAMPANHA = "registro-agendamento"
MODELO = os.environ.get("MODELO") or "claude-opus-5"
PRECO = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
         "claude-haiku-4-5-20251001": (1.0, 5.0)}
USO = {"in": 0, "cache": 0, "out": 0, "chamadas": 0}
OPT_OUT = {"nao contatar", "nao_contatar", "opt_out", "opt-out", "bloqueado"}
# finais de telefone da propria equipe (dono e numeros da academia)
INTERNOS = {"92290338", "88772000", "92143091", "92580287"}
DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado",
               "domingo"]

RE_DIA = re.compile(
    r"\b(amanha|hoje|segunda|terca|quarta|quinta|sexta|sabado|domingo|"
    r"dia \d{1,2}|\d{1,2}/\d{1,2})\b")
RE_VISITA = re.compile(
    r"visita|conhecer|experimental|agend|marcad|marcar|te espero|te aguardo|"
    r"aguardamos|esperamos|passar ai|passo ai|vou ai|\b\d{1,2}\s?(h|hs|hrs|horas)\b|"
    r"\b\d{1,2}:\d{2}\b")

SISTEMA = """Você audita conversas de WhatsApp entre uma academia (Território Fit, São Carlos/SP) e pessoas interessadas. Sua tarefa: dizer se existe uma VISITA ou AULA EXPERIMENTAL combinada que esteja valendo agora, com dia definido.

Cada linha da conversa traz data, dia da semana e hora (horário de Brasília) e quem falou: LEAD, CLARA (assistente da academia), CONSULTORA <nome> ou EQUIPE.

Regras:
- "combinado" só é verdadeiro quando os DOIS lados fecharam um dia específico: a academia propôs e o lead aceitou de forma clara, ou o lead disse o dia em que vem e a academia confirmou. Convite sem resposta, "vou ver", "qualquer dia eu passo", "semana que vem eu vou" sem dia, ou intenção vaga NÃO são combinado.
- Resolva datas relativas ("amanhã", "sexta") a partir da data da mensagem em que foram ditas.
- Se houve remarcação, vale o último combinado. Se o lead cancelou ou desistiu depois, não há combinado.
- Matrícula, pagamento, renovação, reposição de aula de aluno, entrega de currículo e entrevista de emprego NÃO são visita nem aula experimental.
- horario: "HH:MM" quando houver hora; "manhã", "tarde" ou "noite" quando só houver período; vazio quando não houver.
- tipo "aula_experimental" quando o combinado é treinar ou fazer uma aula; "visita" quando é conhecer a academia.
- certeza "alta" só quando dia e aceite estão explícitos no texto. Na dúvida, use "media" ou "baixa".
- consultora: primeiro nome de quem combinou pela academia, se aparecer; "Clara" se foi a CLARA; vazio se não der pra saber."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "combinado": {"type": "boolean"},
        "data": {"type": "string", "description": "AAAA-MM-DD ou vazio"},
        "horario": {"type": "string"},
        "tipo": {"type": "string", "enum": ["visita", "aula_experimental", "nenhum"]},
        "modalidade": {"type": "string", "enum": [
            "musculacao", "fitdance", "bike", "pilates", "funcional", "outra",
            "nenhuma"]},
        "consultora": {"type": "string"},
        "certeza": {"type": "string", "enum": ["alta", "media", "baixa"]},
    },
    "required": ["combinado", "data", "horario", "tipo", "modalidade",
                 "consultora", "certeza"],
    "additionalProperties": False,
}


def _h(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _norm(v) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(v or "").lower())
                   if unicodedata.category(c) != "Mn").strip()


def _get(sb: dict, tabela: str, params: dict, limite: int = 20000) -> list[dict]:
    linhas: list[dict] = []
    for ini in range(0, limite, 1000):
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabela}", params=params,
                         headers={**sb, "Range": f"{ini}-{ini + 999}"},
                         timeout=90)
        r.raise_for_status()
        lote = r.json()
        linhas += lote
        if len(lote) < 1000:
            break
    return linhas


def _hora(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ_SP)


def quem(m: dict) -> str:
    if not m["is_from_me"]:
        return "LEAD"
    if m.get("sb") == "ai_agent":
        return "CLARA"
    mm = re.match(r"^\*([A-Za-zÀ-ú]+):\*", m.get("content") or "")
    return "CONSULTORA " + mm.group(1) if mm else "EQUIPE"


def custo() -> float:
    pi, po = PRECO.get(MODELO, (5.0, 25.0))
    return (USO["in"] * pi + USO["cache"] * pi * 0.1 + USO["out"] * po) / 1e6


def ler(client, cabecalho: str, conversa: list[str]) -> dict | None:
    import anthropic
    try:
        resp = client.messages.create(
            model=MODELO, max_tokens=1500,
            system=[{"type": "text", "text": SISTEMA,
                     "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "medium", "format": {
                "type": "json_schema", "schema": ESQUEMA}},
            messages=[{"role": "user",
                       "content": cabecalho + "\n" + "\n".join(conversa)}])
    except anthropic.APIError as e:
        print(f"[claude] erro {type(e).__name__}")
        return None
    u = resp.usage
    USO["in"] += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    USO["cache"] += getattr(u, "cache_read_input_tokens", 0) or 0
    USO["out"] += u.output_tokens or 0
    USO["chamadas"] += 1
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None
    try:
        return json.loads(next((b.text for b in resp.content if b.type == "text"), ""))
    except json.JSONDecodeError:
        return None


def aula_crm(r: dict) -> str:
    if r["tipo"] != "aula_experimental":
        return "Visita"
    mod = r["modalidade"]
    if mod in ("musculacao", "nenhuma", "outra"):
        m = re.match(r"(\d{1,2}):", r["horario"] or "")
        h = int(m.group(1)) if m else {"manha": 8, "tarde": 14, "noite": 19}.get(
            _norm(r["horario"]), -1)
        if h < 0:
            return "Musculação"
        return "Musc. Manhã" if h < 12 else "Musc. Tarde" if h < 18 else "Musc. Noite"
    return {"fitdance": "FitDance", "bike": "Bike", "pilates": "Pilates",
            "funcional": "Funcional"}[mod]


def consultor_crm(nome: str) -> str | None:
    n = _norm(nome)
    for canon in ("Raiane", "Nathalia", "Kellyta", "Lyandra"):
        if n.startswith(_norm(canon)[:5]):
            return canon
    return None


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    dry = os.environ.get("DRY_RUN", "1") != "0"
    detalhe = os.environ.get("DETALHE", "") == "1"
    janela = int(os.environ.get("JANELA_HORAS") or "30")
    max_conv = int(os.environ.get("MAX_CONVERSAS") or "80")
    teto = float(os.environ.get("TETO_USD") or "3")
    if not key:
        print("Falta env SUPABASE_KEY")
        return 1
    sb = _h(key)
    agora = datetime.now(TZ_SP)
    hoje = agora.date()
    print("[rotina] registro automático de agendamento"
          + (" — SIMULAÇÃO" if dry else "") + f" | janela {janela}h")

    desde = (agora - timedelta(hours=janela)).astimezone(timezone.utc).isoformat()
    recentes = _get(sb, "whatsapp_messages", {
        "select": "lead_id", "group_id": "is.null", "is_from_me": "eq.false",
        "lead_id": "not.is.null", "sent_at": f"gte.{desde}"})
    ids = sorted({m["lead_id"] for m in recentes})
    print(f"[conversas] {len(ids)} com mensagem do lead na janela")
    if not ids:
        return 0

    ativos = {re.sub(r"\D", "", a["phone8"])[-8:] for a in
              _get(sb, "pacto_alunos_ativos", {"select": "phone8"})
              if a.get("phone8")}
    leads: dict = {}
    for i in range(0, len(ids), 80):
        for l in _get(sb, "leads", {
                "select": "id,tenant_id,name,phone,status,tags,is_internal_contact,"
                          "cv:metadata->cv",
                "id": f"in.({','.join(ids[i:i + 80])})"}):
            leads[l["id"]] = l
    ags: dict = {}
    for i in range(0, len(ids), 80):
        for a in _get(sb, "agendamentos", {
                "select": "id,lead_id,aula,data_agendamento,horario,origem,"
                          "mes_referencia,veio,fechou",
                "lead_id": f"in.({','.join(ids[i:i + 80])})",
                "order": "created_at.asc"}):
            ags.setdefault(a["lead_id"], []).append(a)

    fora = {"aluno ativo": 0, "candidato a vaga": 0, "contato interno": 0,
            "opt-out": 0, "sem sinal de agendamento": 0}
    cand = []
    for lid in ids:
        l = leads.get(lid)
        if not l:
            continue
        fone = re.sub(r"\D", "", l.get("phone") or "")
        tags = {_norm(t) for t in (l.get("tags") or [])}
        if l.get("status") in ("cliente", "inadimplente") or (
                len(fone) >= 8 and fone[-8:] in ativos):
            fora["aluno ativo"] += 1
        elif l.get("cv") or any(t == "cv" or t.startswith("cv ") for t in tags) or any(
                _norm(a.get("aula")) == "curriculo" for a in ags.get(lid, [])):
            fora["candidato a vaga"] += 1
        elif l.get("is_internal_contact") or fone[-8:] in INTERNOS or (
                "academia" in _norm(l.get("name"))):
            fora["contato interno"] += 1
        elif tags & OPT_OUT:
            fora["opt-out"] += 1
        else:
            cand.append(l)

    import anthropic
    cfg = requests.get(f"{SUPABASE_URL}/rest/v1/config",
                       params={"select": "value", "key": "eq.ANTHROPIC_API_KEY",
                               "limit": "1"}, headers=sb, timeout=30).json()
    client = anthropic.Anthropic(
        api_key=(cfg[0].get("value") or "").replace("﻿", "").strip())

    criados = corrigidos = iguais = sem_combinado = pouca_certeza = 0
    passado = lidas = falhas = 0
    semana_atras = (agora - timedelta(days=7)).astimezone(timezone.utc).isoformat()
    for l in cand:
        if lidas >= max_conv or custo() > teto:
            print("[limite] teto de conversas ou de custo atingido.")
            break
        msgs = _get(sb, "whatsapp_messages", {
            "select": "is_from_me,content,message_type,sent_at,"
                      "sb:metadata->>sent_by,auto:metadata->>automacao",
            "lead_id": f"eq.{l['id']}", "group_id": "is.null",
            "sent_at": f"gte.{semana_atras}", "order": "sent_at.asc"}, 1000)
        msgs = [m for m in msgs if m.get("message_type") != "ai_tool_call"
                and (m.get("content") or "").strip()][-60:]
        texto = _norm(" ".join(m["content"] for m in msgs))
        if not (RE_DIA.search(texto) and RE_VISITA.search(texto)):
            fora["sem sinal de agendamento"] += 1
            continue

        futuros = [a for a in ags.get(l["id"], [])
                   if a.get("data_agendamento")
                   and a["data_agendamento"] >= hoje.isoformat()
                   and _norm(a.get("aula")) != "curriculo"]
        atual = futuros[-1] if futuros else None
        cab = (f"HOJE: {hoje.isoformat()} ({DIAS_SEMANA[hoje.weekday()]}). "
               + (f"Agendamento já registrado no CRM: {atual['data_agendamento']} "
                  f"{atual.get('horario') or 'sem horário'}."
                  if atual else "Nenhum agendamento futuro registrado no CRM.")
               + "\nCONVERSA:")
        linhas = []
        for m in msgs:
            t = _hora(m["sent_at"])
            linhas.append(f"[{t:%Y-%m-%d} {DIAS_SEMANA[t.weekday()]} {t:%H:%M}] "
                          f"{quem(m)}: {m['content'][:500]}")
        lidas += 1
        r = ler(client, cab, linhas)
        if r is None:
            falhas += 1
            continue
        if not r["combinado"] or r["tipo"] == "nenhum":
            sem_combinado += 1
            continue
        if r["certeza"] != "alta" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["data"]):
            pouca_certeza += 1
            continue
        try:
            dia = date.fromisoformat(r["data"])
        except ValueError:
            pouca_certeza += 1
            continue
        if dia < hoje or dia > hoje + timedelta(days=45):
            passado += 1
            continue
        hr = (r["horario"] or "").strip() or None
        if atual and atual["data_agendamento"] == r["data"] and (
                not hr or _norm(atual.get("horario")).replace(";", ":").lstrip("0")
                == _norm(hr).lstrip("0")):
            iguais += 1
            continue
        # mesmo dia e o CRM ja tem horario lancado por alguem: nao sobrescreve
        if atual and atual["data_agendamento"] == r["data"] and (
                atual.get("horario") or "").strip():
            iguais += 1
            continue

        aula = aula_crm(r)
        consultor = consultor_crm(r["consultora"])
        if detalhe:
            primeiro = (l.get("name") or "?").split()[0] if l.get("name") else "?"
            rotulo = f"{primeiro} ...{re.sub(r'[^0-9]', '', l.get('phone') or '')[-4:]}"
        else:
            rotulo = f"lead {l['id'][:8]}"
        acao = "corrigir" if atual else "criar"
        print(f"[{'SIMULAÇÃO' if dry else 'grava'}] {acao} — {rotulo} | {aula} "
              f"{r['data']} {hr or 'sem horário'} | por "
              f"{consultor or r['consultora'] or 'não identificado'}"
              + (f" | no CRM estava {atual['data_agendamento']} "
                 f"{atual.get('horario') or 'sem horário'}" if atual else ""))
        if atual:
            corrigidos += 1
        else:
            criados += 1
        if dry:
            continue

        semana = min((dia.day + 6) // 7, 5)
        ag_id = None
        if atual:
            requests.patch(f"{SUPABASE_URL}/rest/v1/agendamentos",
                           params={"id": f"eq.{atual['id']}"},
                           headers={**sb, "Prefer": "return=minimal"},
                           json={"data_agendamento": r["data"], "horario": hr,
                                 "mes_referencia": r["data"][:7],
                                 "semana": semana, "confirmacao": None},
                           timeout=30).raise_for_status()
            ag_id = atual["id"]
            descricao = (f"Remarcado pela leitura da conversa: {aula} {r['data']}"
                         f"{' às ' + hr if hr else ''} (antes "
                         f"{atual['data_agendamento']} "
                         f"{atual.get('horario') or 'sem horário'}).")
        else:
            registro = {
                "tenant_id": l["tenant_id"], "lead_id": l["id"],
                "nome": (l.get("name") or "").strip() or "Sem nome",
                "telefone": re.sub(r"\D", "", l.get("phone") or "") or None,
                "fonte": "WhatsApp", "aula": aula, "consultor": consultor,
                "mes_referencia": r["data"][:7], "semana": semana,
                "data_contato": hoje.isoformat(), "data_agendamento": r["data"],
                "horario": hr, "origem": "whatsapp_auto",
                "observacao": "Registrado automaticamente pela leitura da "
                              "conversa do WhatsApp."}
            resp = requests.post(f"{SUPABASE_URL}/rest/v1/agendamentos",
                                 headers={**sb, "Prefer": "return=representation"},
                                 json=registro, timeout=30)
            if resp.status_code == 409:
                # ja existe linha automatica do lead no mes: completa a linha
                resp = requests.patch(
                    f"{SUPABASE_URL}/rest/v1/agendamentos",
                    params={"lead_id": f"eq.{l['id']}", "origem": "eq.whatsapp_auto",
                            "mes_referencia": f"eq.{r['data'][:7]}"},
                    headers={**sb, "Prefer": "return=representation"},
                    json={"data_agendamento": r["data"], "horario": hr,
                          "aula": aula, "semana": semana,
                          **({"consultor": consultor} if consultor else {})},
                    timeout=30)
            resp.raise_for_status()
            linhas_ag = resp.json()
            ag_id = linhas_ag[0]["id"] if linhas_ag else None
            descricao = (f"Agendamento registrado pela leitura da conversa: "
                         f"{aula} {r['data']}{' às ' + hr if hr else ''}.")
        if ag_id:
            requests.post(f"{SUPABASE_URL}/rest/v1/agendamento_eventos",
                          headers={**sb, "Prefer": "return=minimal"},
                          json={"tenant_id": l["tenant_id"],
                                "agendamento_id": ag_id, "tipo": "auto",
                                "descricao": descricao,
                                "registrado_por": "Leitura da conversa"},
                          timeout=30)

    print("[fora] " + ", ".join(f"{k}={v}" for k, v in fora.items() if v))
    print(f"Resumo: {lidas} conversa(s) lida(s) | {criados} agendamento(s) "
          f"{'a criar' if dry else 'criado(s)'}, {corrigidos} "
          f"{'a corrigir' if dry else 'corrigido(s)'}, {iguais} já certos no CRM, "
          f"{sem_combinado} sem combinado, {pouca_certeza} com pouca certeza, "
          f"{passado} com data passada, {falhas} falha(s) de leitura | custo "
          f"estimado US$ {custo():.2f}")
    if not dry and (criados or corrigidos):
        requests.post(f"{SUPABASE_URL}/rest/v1/agent_activity",
                      headers={**sb, "Prefer": "return=minimal"},
                      json={"agent_slug": "comercial-vendas",
                            "title": "Registro automático de agendamento",
                            "detail": f"{criados} criado(s) e {corrigidos} "
                                      f"corrigido(s) pela leitura de {lidas} "
                                      "conversa(s).",
                            "status": "concluido",
                            "metadata": {"campanha": CAMPANHA,
                                         "criados": criados,
                                         "corrigidos": corrigidos,
                                         "custo_usd": round(custo(), 2)}},
                      timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
