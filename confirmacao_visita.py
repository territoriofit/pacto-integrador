# -*- coding: utf-8 -*-
"""
Confirmacao de visita / aula experimental — reduz o no-show dos agendamentos.

Plano de acao das vendas perdidas (Andre, 28/09/2026): entre quem agendou e
nao matriculou, a falta e o motivo numero 1. Esta rotina fala com quem tem
visita ou aula experimental marcada na aba Agendamentos do CRM:

  vespera — D-1, das 17h as 19h45: lembra o dia e a hora e pede confirmacao.
  dia     — D0, das 9h as 17h e pelo menos 2h antes do horario: "estamos te
            esperando". Visita antes do meio-dia so recebe se a vespera nao
            saiu (pra nao mandar 2 mensagens em menos de 18h).
  faltou  — D+1 de manha (seg a sab): quem nao teve presenca registrada
            recebe convite pra remarcar.

De onde vem a presenca: o sync do Pacto marca agendamentos.veio quando o
visitante e cadastrado na recepcao (relatorio BV, cruzado por telefone). O
passo "faltou" so roda se esse cruzamento acabou de ser refeito neste job
(--sync-presenca) e ainda confere o primeiro nome no BV do dia.

TRAVAS (todas por agendamento):
  - fora curriculo/ligacao, quem ja veio, quem ja fechou, aluno ativo no
    Pacto, opt-out e telefone invalido;
  - conversa viva: lead escreveu ha menos de 2h -> nao interrompe;
  - agendamento criado hoje nao recebe o "dia" (acabou de combinar);
  - depois da vespera, se o lead respondeu, a resposta e lida (Claude): so
    recebe o "dia" quem confirmou; quem avisou que nao vem fica fora do "dia"
    e do "faltou", e a coluna Confirmacao do CRM e preenchida;
  - "faltou" nao sai se ja houve conversa depois do horario marcado;
  - cada passo sai 1 vez por agendamento+data (agent_activity, disparo_key
    "confirmacao-<agendamento_id>-<data>-<passo>"); remarcou, conta de novo.

Envio pelo numero da ultima conversa do lead (se conectado); sem conversa,
pelo numero padrao. 90-150s entre envios do mesmo numero, jitter de inicio.
A mensagem e gravada no inbox do CRM (whatsapp_messages) como enviada pela
Clara, porque envio por API nao aparece la sozinho. Nota no context do lead
orienta a Clara se ele responder.

Repo publico: o log so tem contagens e o comeco do id do agendamento.
DETALHE=1 (uso local) mostra primeiro nome e final do telefone.

Env: SUPABASE_KEY (+ PACTO_* quando usa --sync-presenca).
Opcional: DRY_RUN (padrao 1 = so simula; 0 envia) | MODO=manha|tarde|auto |
PASSOS=vespera,dia,faltou | TEST_TO=5516... (manda os 3 textos de exemplo so
pra esse numero) | INSTANCIA_PADRAO | MAX_POR_RUN (padrao 40) |
JITTER_MAX_MIN (padrao 10) | AGORA=AAAA-MM-DDTHH:MM (simulacao) | DETALHE=1.
"""

import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"
TZ_SP = timezone(timedelta(hours=-3))
CAMPANHA = "confirmacao-visita"
MARCA = "[Confirmação de visita]"
NOTA = ("\n" + MARCA + " Lead com visita ou aula experimental agendada que "
        "recebeu lembrete automático. Se confirmar, agradecer e reforçar dia e "
        "horário. Se não puder vir, oferecer duas opções de novo dia e horário "
        "e registrar o novo agendamento. Não passar valores por iniciativa "
        "própria.")
MODELO = "claude-opus-5"
INSTANCIA_PADRAO = "5516988772000"
FORA_AULA = {"curriculo", "ligacao"}
OPT_OUT = {"nao contatar", "nao_contatar", "opt_out", "opt-out", "bloqueado"}
DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
               "sexta-feira", "sábado", "domingo"]

# Textos (aprovacao do Andre pendente em 28/09/2026)
MSGS = {
    "vespera": ("Oi, {nome}! Tudo bem? 😊\n\n"
                "Passando pra lembrar {atividade} aqui na Território Fit "
                "{quando}.\n\n"
                "Posso confirmar sua presença?\n\n"
                "Se precisar trocar o dia ou o horário, é só me avisar por "
                "aqui que a gente remarca. 🧡"),
    "dia": ("{saudacao}, {nome}! 😊\n\n"
            "Hoje é o dia {atividade} aqui na Território Fit{hora}. Estamos "
            "te esperando!{dica}\n\n"
            "Se acontecer algum imprevisto, me avisa por aqui que a gente "
            "encontra outro horário. 💪"),
    "faltou": ("Oi, {nome}! Tudo bem?\n\n"
               "{quando} era o dia {atividade} aqui na Território Fit e a "
               "gente ficou te esperando. 😊\n\n"
               "Aconteceu algum imprevisto?\n\n"
               "Se quiser, me fala o dia e o horário que ficam melhores pra "
               "você e eu já deixo reservado. 🧡"),
}
DICA_AULA = "\n\nVenha com roupa de treino e traga uma garrafinha de água."

SISTEMA_RESPOSTA = (
    "Você lê a resposta de um lead de academia a um lembrete de visita ou aula "
    "experimental agendada e classifica a intenção dele. Considere só o que o "
    "lead escreveu depois do lembrete.\n"
    "- confirmou: disse que vem, confirmou presença, agradeceu confirmando.\n"
    "- nao_vem: avisou que não vem, desistiu ou pediu pra cancelar.\n"
    "- quer_remarcar: pediu outro dia ou outro horário.\n"
    "- indefinido: qualquer outra coisa (dúvida, pergunta, áudio sem "
    "transcrição, resposta ambígua).")
ESQUEMA_RESPOSTA = {
    "type": "object",
    "properties": {"intencao": {"type": "string", "enum": [
        "confirmou", "nao_vem", "quer_remarcar", "indefinido"]}},
    "required": ["intencao"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- utilitarios

def _sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _norm(v) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(v or "").lower())
                   if unicodedata.category(c) != "Mn").strip()


def _primeiro_nome(nome: str) -> str:
    p = re.sub(r"[^\w\sÀ-ÿ]", " ", (nome or "").replace("﻿", "")).split()
    if not p or len(p[0]) < 2 or any(ch.isdigit() for ch in p[0]):
        return ""
    return p[0].title()


def _fone_55(phone: str) -> str | None:
    d = "".join(c for c in (phone or "") if c.isdigit())
    if len(d) in (10, 11):
        return "55" + d
    if len(d) in (12, 13) and d.startswith("55"):
        return d
    return None


def _get(sb: dict, tabela: str, params: dict, limite: int = 5000) -> list[dict]:
    linhas: list[dict] = []
    for ini in range(0, limite, 1000):
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabela}", params=params,
                         headers={**sb, "Range": f"{ini}-{ini + 999}"},
                         timeout=60)
        r.raise_for_status()
        lote = r.json()
        linhas += lote
        if len(lote) < 1000:
            break
    return linhas


def agora_sp() -> datetime:
    fixo = os.environ.get("AGORA", "").strip()
    if fixo:
        return datetime.fromisoformat(fixo).replace(tzinfo=TZ_SP)
    return datetime.now(TZ_SP)


def horario(txt) -> tuple[int, int] | str | None:
    """'18:00', '18;30', '1830', '9:00' -> (h, min); 'tarde' -> 'tarde'."""
    t = _norm(txt)
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2})\s*[:;h.]\s*(\d{2})?\s*(?:h|hs|horas)?", t)
    if not m:
        m = re.fullmatch(r"(\d{1,2})(\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if 5 <= h <= 23 and mi < 60:
            return h, mi
        return None
    for periodo in ("manha", "tarde", "noite"):
        if periodo in t:
            return periodo
    return None


def hora_texto(hr) -> str:
    if isinstance(hr, tuple):
        return f"às {hr[0]}h" + (f"{hr[1]:02d}" if hr[1] else "")
    return {"manha": "de manhã", "tarde": "à tarde", "noite": "à noite"}.get(hr, "")


def hora_minima(hr) -> tuple[int, int]:
    """Horario usado nas contas de antecedencia quando so ha periodo."""
    if isinstance(hr, tuple):
        return hr
    return {"manha": (8, 0), "tarde": (14, 0), "noite": (18, 0)}.get(hr, (14, 0))


def atividade(aula: str) -> tuple[str, bool]:
    """(texto 'da sua ...', e_aula)."""
    a = _norm(aula)
    if not a or "visita" in a:
        return "da sua visita", False
    if "musc" in a:
        return "da sua aula experimental de musculação", True
    nomes = {"fitdance": "FitDance", "fit dance": "FitDance", "bike": "bike",
             "pilates": "pilates", "funcional": "funcional"}
    for chave, nome in nomes.items():
        if chave in a:
            return f"da sua aula experimental de {nome}", True
    return "da sua aula experimental", True


def montar(passo: str, ag: dict, hoje: date) -> str:
    nome = _primeiro_nome(ag["nome"])
    ativ, e_aula = atividade(ag.get("aula"))
    hr = horario(ag.get("horario"))
    dia = date.fromisoformat(ag["data_agendamento"])
    ht = hora_texto(hr)
    if passo == "vespera":
        quando = f"amanhã, {DIAS_SEMANA[dia.weekday()]} ({dia:%d/%m})"
        if ht:
            quando += f", {ht}"
        txt = MSGS["vespera"].format(nome=nome, atividade=ativ, quando=quando)
    elif passo == "dia":
        h = agora_sp().hour
        txt = MSGS["dia"].format(
            saudacao="Bom dia" if h < 12 else "Boa tarde", nome=nome,
            atividade=ativ, hora=f", {ht}" if ht else "",
            dica=DICA_AULA if e_aula else "")
    else:
        atras = (hoje - dia).days
        quando = "Ontem" if atras == 1 else DIAS_SEMANA[dia.weekday()].capitalize()
        txt = MSGS["faltou"].format(nome=nome, atividade=ativ, quando=quando)
    if not nome:   # sem nome utilizavel: "Oi! Tudo bem?"
        txt = txt.replace(", !", "!", 1)
    return txt


# ------------------------------------------------------------------ dados CRM

def instancias(sb: dict) -> dict:
    """{id: instancia} dos numeros de WhatsApp conectados (UazAPI)."""
    r = requests.get(f"{SUPABASE_URL}/rest/v1/whatsapp_instances",
                     params={"select": "id,name,phone_number,status,api_key"},
                     headers=sb, timeout=30)
    r.raise_for_status()
    return {i["id"]: i for i in r.json()
            if i.get("status") == "connected" and i.get("api_key")
            and i.get("phone_number")}


def agendamentos(sb: dict, ini: date, fim: date) -> list[dict]:
    return _get(sb, "agendamentos", {
        "select": "id,tenant_id,lead_id,nome,telefone,aula,consultor,"
                  "data_agendamento,horario,confirmacao,veio,fechou,created_at",
        "data_agendamento": f"gte.{ini.isoformat()}",
        "and": f"(data_agendamento.lte.{fim.isoformat()})",
        "order": "data_agendamento.asc,horario.asc"})


def enviados(sb: dict, desde: date) -> dict:
    """{disparo_key: created_at} dos passos ja enviados."""
    linhas = _get(sb, "agent_activity", {
        "select": "created_at,metadata",
        "metadata->>campanha": f"eq.{CAMPANHA}",
        "created_at": f"gte.{desde.isoformat()}",
        "order": "created_at.asc"})
    return {(l.get("metadata") or {}).get("disparo_key"): l["created_at"]
            for l in linhas if (l.get("metadata") or {}).get("disparo_key")}


def mensagens(sb: dict, lead_id: str, desde: datetime) -> list[dict]:
    """Mensagens da conversa desde `desde`, sem as desta automacao."""
    linhas = _get(sb, "whatsapp_messages", {
        "select": "sent_at,is_from_me,content,message_type,instance_id,metadata",
        "lead_id": f"eq.{lead_id}", "group_id": "is.null",
        "sent_at": f"gte.{desde.astimezone(timezone.utc).isoformat()}",
        "order": "sent_at.asc"}, limite=1000)
    return [m for m in linhas
            if (m.get("metadata") or {}).get("automacao") != CAMPANHA
            and m.get("message_type") != "ai_tool_call"]


def ultima_instancia(sb: dict, lead_id: str) -> str | None:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/whatsapp_messages",
                     params={"select": "instance_id", "lead_id": f"eq.{lead_id}",
                             "group_id": "is.null", "order": "sent_at.desc",
                             "limit": "1"}, headers=sb, timeout=30)
    r.raise_for_status()
    d = r.json()
    return d[0]["instance_id"] if d else None


def ler_resposta(sb: dict, textos: list[str]) -> str:
    """Intencao do lead depois do lembrete. 'indefinido' em qualquer falha."""
    try:
        import anthropic
        r = requests.get(f"{SUPABASE_URL}/rest/v1/config",
                         params={"select": "value",
                                 "key": "eq.ANTHROPIC_API_KEY", "limit": "1"},
                         headers=sb, timeout=30)
        chave = (r.json()[0].get("value") or "").replace("﻿", "").strip()
        resp = anthropic.Anthropic(api_key=chave).messages.create(
            model=MODELO, max_tokens=300, system=SISTEMA_RESPOSTA,
            output_config={"effort": "low", "format": {
                "type": "json_schema", "schema": ESQUEMA_RESPOSTA}},
            messages=[{"role": "user", "content":
                       "Mensagens do lead depois do lembrete:\n"
                       + "\n".join(f"- {t[:400]}" for t in textos[-8:])}])
        if resp.stop_reason in ("refusal", "max_tokens"):
            return "indefinido"
        texto = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(texto)["intencao"]
    except Exception as e:
        print(f"[resposta] leitura falhou ({type(e).__name__}) — tratado como "
              "indefinido")
        return "indefinido"


def registrar_confirmacao(sb: dict, ag: dict, intencao: str, dry: bool) -> None:
    valor = {"confirmou": True, "nao_vem": False, "quer_remarcar": False}.get(intencao)
    if valor is None or ag.get("confirmacao") is valor or dry:
        return
    requests.patch(f"{SUPABASE_URL}/rest/v1/agendamentos",
                   params={"id": f"eq.{ag['id']}"},
                   headers={**sb, "Prefer": "return=minimal"},
                   json={"confirmacao": valor}, timeout=30)
    requests.post(f"{SUPABASE_URL}/rest/v1/agendamento_eventos",
                  headers={**sb, "Prefer": "return=minimal"},
                  json={"tenant_id": ag["tenant_id"], "agendamento_id": ag["id"],
                        "tipo": "auto", "registrado_por": "Confirmação automática",
                        "descricao": {
                            "confirmou": "Lead confirmou presença ao responder o lembrete.",
                            "nao_vem": "Lead avisou que não vem ao responder o lembrete.",
                            "quer_remarcar": "Lead pediu pra remarcar ao responder o lembrete.",
                        }[intencao]}, timeout=30)


# -------------------------------------------------------------------- presenca

def sync_presenca() -> bool:
    """Refaz o cruzamento Pacto (BV) x agendamentos. False se nao deu."""
    try:
        import logging
        logging.disable(logging.CRITICAL)
        from agente_integrador_pacto import CRMClient, PactoADMClient
        crm, adm = CRMClient(), PactoADMClient()
        crm.sync_visitantes_bv(adm, dias=4)
        crm.sync_agendamentos_status(adm)
        return True
    except Exception as e:
        print(f"[presenca] sync do Pacto falhou ({type(e).__name__}) — passo "
              "'faltou' fica pra proxima.")
        return False


def nomes_no_bv(sb: dict, dia: date) -> set[str]:
    linhas = _get(sb, "visitantes_bv", {
        "select": "nome", "data_visita": f"eq.{dia.isoformat()}"})
    return {_norm(_primeiro_nome(l.get("nome") or "")) for l in linhas} - {""}


# ------------------------------------------------------------------- selecao

def selecionar(sb: dict, passos: list[str], agora: datetime,
               presenca_ok: bool, dry: bool):
    hoje = agora.date()
    ags = agendamentos(sb, hoje - timedelta(days=2), hoje + timedelta(days=1))
    feitos = enviados(sb, hoje - timedelta(days=10))
    ativos = {re.sub(r"\D", "", a["phone8"])[-8:] for a in
              _get(sb, "pacto_alunos_ativos", {"select": "phone8"}, 10000)
              if a.get("phone8")}
    ids = sorted({a["lead_id"] for a in ags if a.get("lead_id")})
    leads: dict = {}
    for i in range(0, len(ids), 80):
        for l in _get(sb, "leads", {"select": "id,status,tags,context,phone",
                                    "id": f"in.({','.join(ids[i:i + 80])})"}):
            leads[l["id"]] = l

    fora: dict = {}
    alvo: list[dict] = []
    vistos: set = set()
    bv_cache: dict = {}

    def pula(motivo: str):
        fora[motivo] = fora.get(motivo, 0) + 1

    for ag in ags:
        dia = date.fromisoformat(ag["data_agendamento"])
        if dia == hoje + timedelta(days=1):
            passo = "vespera"
        elif dia == hoje:
            passo = "dia"
        else:
            passo = "faltou"
        if passo not in passos:
            continue
        if _norm(ag.get("aula")) in FORA_AULA:
            continue
        lead = leads.get(ag.get("lead_id")) or {}
        fone = _fone_55(ag.get("telefone") or "") or _fone_55(lead.get("phone") or "")
        if not fone:
            pula("sem telefone válido")
            continue
        if ag.get("veio") or ag.get("fechou"):
            pula("já veio ou já fechou")
            continue
        if fone[-8:] in ativos or lead.get("status") in ("cliente", "inadimplente"):
            pula("já é aluno")
            continue
        if {_norm(t) for t in (lead.get("tags") or [])} & OPT_OUT:
            pula("opt-out")
            continue
        chave = f"confirmacao-{ag['id']}-{ag['data_agendamento']}-{passo}"
        if chave in feitos or (fone, ag["data_agendamento"], passo) in vistos:
            pula("já recebeu este passo")
            continue

        hr = horario(ag.get("horario"))
        h, mi = hora_minima(hr)
        marcado = datetime(dia.year, dia.month, dia.day, h, mi, tzinfo=TZ_SP)
        k_vesp = f"confirmacao-{ag['id']}-{ag['data_agendamento']}-vespera"
        k_dia = f"confirmacao-{ag['id']}-{ag['data_agendamento']}-dia"
        criado = datetime.fromisoformat(
            ag["created_at"].replace("Z", "+00:00")).astimezone(TZ_SP)

        if passo == "dia":
            if criado.date() == hoje:
                pula("agendado hoje pra hoje")
                continue
            if marcado - agora < timedelta(hours=2):
                pula("menos de 2h pro horário")
                continue
            if isinstance(hr, tuple) and hr[0] < 12 and k_vesp in feitos:
                pula("visita de manhã já lembrada na véspera")
                continue
        if passo == "faltou":
            if not presenca_ok:
                pula("presença não conferida no Pacto")
                continue
            if dia not in bv_cache:
                bv_cache[dia] = nomes_no_bv(sb, dia)
            if _norm(_primeiro_nome(ag["nome"])) in bv_cache[dia]:
                pula("mesmo nome no cadastro de visitantes do dia")
                continue

        # conversa: o que aconteceu desde o ultimo lembrete / desde o horario
        msgs = []
        if ag.get("lead_id"):
            base = agora - timedelta(days=3)
            msgs = mensagens(sb, ag["lead_id"], base)
        do_lead = [m for m in msgs if not m["is_from_me"]]

        def depois(ts: datetime, lista: list) -> list:
            return [m for m in lista if datetime.fromisoformat(
                m["sent_at"].replace("Z", "+00:00")) > ts]

        if passo in ("vespera", "dia") and depois(agora - timedelta(hours=2), do_lead):
            pula("conversa em andamento agora")
            continue

        ultimo_lembrete = feitos.get(k_dia) or feitos.get(k_vesp)
        if ultimo_lembrete and passo in ("dia", "faltou"):
            ts = datetime.fromisoformat(ultimo_lembrete.replace("Z", "+00:00"))
            resp = [m.get("content") or f"[{m.get('message_type')}]"
                    for m in depois(ts, do_lead)]
            if resp:
                intencao = ler_resposta(sb, resp)
                registrar_confirmacao(sb, ag, intencao, dry)
                if intencao != "confirmou":
                    pula(f"respondeu ao lembrete ({intencao.replace('_', ' ')})")
                    continue
        if passo == "faltou":
            humanas = [m for m in depois(marcado, msgs)
                       if not m["is_from_me"]
                       or (m.get("metadata") or {}).get("sent_by") != "ai_agent"]
            if humanas:
                pula("já houve conversa depois do horário marcado")
                continue

        vistos.add((fone, ag["data_agendamento"], passo))
        alvo.append({"ag": ag, "passo": passo, "fone": fone, "chave": chave,
                     "lead": lead, "marcado": marcado})

    ordem = {"dia": 0, "vespera": 1, "faltou": 2}
    alvo.sort(key=lambda a: (ordem[a["passo"]], a["marcado"]))
    return alvo, fora


# --------------------------------------------------------------------- envio

def enviar(sb: dict, inst: dict, a: dict, texto: str) -> bool:
    ag = a["ag"]
    resp = requests.post(f"{UAZAPI_URL}/send/text",
                         headers={"token": inst["api_key"],
                                  "Content-Type": "application/json"},
                         json={"number": a["fone"], "text": texto}, timeout=120)
    if resp.status_code != 200:
        print(f"[send] {a['passo']} ag {ag['id'][:8]} HTTP {resp.status_code}")
        if "not on WhatsApp" in resp.text:
            requests.post(f"{SUPABASE_URL}/rest/v1/agent_activity",
                          headers={**sb, "Prefer": "return=minimal"},
                          json={"agent_slug": "comercial-vendas",
                                "title": "Confirmação de visita: número fora do WhatsApp",
                                "detail": f"{ag['nome']} — lembrete não enviado",
                                "status": "erro",
                                "metadata": {"disparo_key": a["chave"],
                                             "campanha": CAMPANHA,
                                             "agendamento_id": ag["id"]}},
                          timeout=30)
        return False
    try:
        corpo = resp.json()
    except ValueError:
        corpo = {}
    msg_id = (corpo.get("id") or (corpo.get("key") or {}).get("id")
              or corpo.get("messageid") or f"confirmacao_{int(time.time() * 1000)}")
    titulos = {"vespera": "lembrete da véspera enviado",
               "dia": "lembrete do dia enviado",
               "faltou": "convite pra remarcar enviado (não veio)"}
    requests.post(f"{SUPABASE_URL}/rest/v1/agent_activity",
                  headers={**sb, "Prefer": "return=minimal"},
                  json={"agent_slug": "comercial-vendas",
                        "title": f"Confirmação de visita: {titulos[a['passo']]}",
                        "detail": f"{ag['nome']} — {ag.get('aula') or 'Visita'} "
                                  f"{ag['data_agendamento']} {ag.get('horario') or ''} "
                                  f"pelo {inst['name']}".replace("  ", " "),
                        "status": "concluido",
                        "metadata": {"disparo_key": a["chave"], "campanha": CAMPANHA,
                                     "passo": a["passo"],
                                     "agendamento_id": ag["id"],
                                     "lead_id": ag.get("lead_id"),
                                     "instancia": inst["phone_number"]}},
                  timeout=30)
    if ag.get("lead_id"):
        # inbox do CRM: envio por API nao aparece sozinho
        requests.post(f"{SUPABASE_URL}/rest/v1/whatsapp_messages",
                      headers={**sb, "Prefer": "return=minimal"},
                      json={"tenant_id": ag["tenant_id"], "instance_id": inst["id"],
                            "remote_jid": f"{a['fone']}@s.whatsapp.net",
                            "message_id": msg_id, "message_type": "Conversation",
                            "content": texto, "is_from_me": True,
                            "sender_phone": "", "lead_id": ag["lead_id"],
                            "sent_at": datetime.now(timezone.utc).isoformat(),
                            "metadata": {"sent_by": "ai_agent",
                                         "automacao": CAMPANHA,
                                         "passo": a["passo"]}},
                      timeout=30)
        ctx = a["lead"].get("context") or ""
        if MARCA not in ctx:
            requests.patch(f"{SUPABASE_URL}/rest/v1/leads",
                           params={"id": f"eq.{ag['lead_id']}"},
                           headers={**sb, "Prefer": "return=minimal"},
                           json={"context": ctx + NOTA}, timeout=30)
    requests.post(f"{SUPABASE_URL}/rest/v1/agendamento_eventos",
                  headers={**sb, "Prefer": "return=minimal"},
                  json={"tenant_id": ag["tenant_id"], "agendamento_id": ag["id"],
                        "tipo": "auto", "registrado_por": "Confirmação automática",
                        "descricao": f"{titulos[a['passo']].capitalize()} "
                                     f"pelo WhatsApp ({inst['name']})."},
                  timeout=30)
    return True


def rodada(sb: dict, passos: list[str], presenca_ok: bool, dry: bool,
           detalhe: bool, max_run: int, ultimo_envio: dict) -> int:
    agora = agora_sp()
    insts = instancias(sb)
    padrao = next((i for i in insts.values() if i["phone_number"] == (
        os.environ.get("INSTANCIA_PADRAO") or INSTANCIA_PADRAO)), None)
    alvo, fora = selecionar(sb, passos, agora, presenca_ok, dry)
    print(f"[{agora:%d/%m %H:%M}] passos {', '.join(passos)}: {len(alvo)} "
          f"alvo(s) | fora: "
          + (", ".join(f"{k}={v}" for k, v in sorted(fora.items())) or "ninguém"))
    feitos_run = {"vespera": 0, "dia": 0, "faltou": 0}
    sem_numero = falhas = 0
    for a in alvo[:max_run]:
        ag = a["ag"]
        inst = None
        if ag.get("lead_id"):
            inst = insts.get(ultima_instancia(sb, ag["lead_id"]))
        inst = inst or padrao
        if inst is None:
            sem_numero += 1
            continue
        texto = montar(a["passo"], ag, agora.date())
        if dry:
            quem = (f"{_primeiro_nome(ag['nome']) or '(sem nome)'} "
                    f"...{a['fone'][-4:]}" if detalhe else f"ag {ag['id'][:8]}")
            print(f"[SIMULAÇÃO] {a['passo']} — {quem} | "
                  f"{ag.get('aula') or 'Visita'} {ag['data_agendamento']} "
                  f"{ag.get('horario') or 'sem horário'} | via {inst['name']}")
            if detalhe:
                print("    " + texto.replace("\n", "\n    "))
            feitos_run[a["passo"]] += 1
            continue
        fone_inst = inst["phone_number"]
        if fone_inst in ultimo_envio:   # anti-bloqueio: 90-150s por numero
            espera = (90 + random.uniform(0, 60)
                      - (time.monotonic() - ultimo_envio[fone_inst]))
            if espera > 0:
                time.sleep(espera)
        ok = enviar(sb, inst, a, texto)
        ultimo_envio[fone_inst] = time.monotonic()
        if ok:
            feitos_run[a["passo"]] += 1
            print(f"[send] {a['passo']} ag {ag['id'][:8]} ok")
        else:
            falhas += 1
    total = sum(feitos_run.values())
    print(f"Resumo: {total} {'simulado(s)' if dry else 'enviado(s)'} "
          f"(véspera={feitos_run['vespera']}, dia={feitos_run['dia']}, "
          f"faltou={feitos_run['faltou']}), {sem_numero} sem número conectado, "
          f"{falhas} falha(s)")
    if not dry and total:
        requests.post(f"{SUPABASE_URL}/rest/v1/agent_activity",
                      headers={**sb, "Prefer": "return=minimal"},
                      json={"agent_slug": "comercial-vendas",
                            "title": "Confirmação de visita (rodada)",
                            "detail": f"{total} mensagem(ns): véspera "
                                      f"{feitos_run['vespera']}, dia "
                                      f"{feitos_run['dia']}, não veio "
                                      f"{feitos_run['faltou']}.",
                            "status": "concluido",
                            "metadata": {"campanha": CAMPANHA}}, timeout=30)
    return total


def esperar_ate(hora: int, minuto: int = 0) -> None:
    if os.environ.get("AGORA"):
        return
    while True:
        agora = agora_sp()
        falta = (hora * 60 + minuto) - (agora.hour * 60 + agora.minute)
        if falta <= 0:
            return
        print(f"[janela] {agora:%H:%M} BRT — aguardando {falta} min...")
        time.sleep(min(falta * 60, 1800))


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    dry = os.environ.get("DRY_RUN", "1") != "0"
    detalhe = os.environ.get("DETALHE", "") == "1"
    test_to = os.environ.get("TEST_TO", "").replace("﻿", "").strip()
    if test_to.lower() == "andre":   # mesmo destino dos relatorios
        test_to = "5516992290338"
    max_run = int(os.environ.get("MAX_POR_RUN") or "40")
    jitter_max = int(os.environ.get("JITTER_MAX_MIN") or "10")
    so_passos = [p.strip() for p in (os.environ.get("PASSOS") or "").split(",")
                 if p.strip() in MSGS]
    if not key:
        print("Falta env SUPABASE_KEY")
        return 1
    sb = _sb_headers(key)
    print("[rotina] confirmação de visita e aula experimental"
          + (" — SIMULAÇÃO" if dry else ""))

    if test_to:
        insts = instancias(sb)
        inst = next((i for i in insts.values() if i["phone_number"] == (
            os.environ.get("INSTANCIA_PADRAO") or INSTANCIA_PADRAO)), None)
        if inst is None:
            print("Número padrão desconectado.")
            return 1
        hoje = agora_sp().date()
        exemplos = [
            ("vespera", {"nome": "André", "aula": "Visita", "horario": "18:00",
                         "data_agendamento": (hoje + timedelta(days=1)).isoformat()}),
            ("dia", {"nome": "André", "aula": "Musc. Noite", "horario": "19:30",
                     "data_agendamento": hoje.isoformat()}),
            ("faltou", {"nome": "André", "aula": "Visita", "horario": "18:00",
                        "data_agendamento": (hoje - timedelta(days=1)).isoformat()}),
        ]
        for passo, ag in exemplos:
            r = requests.post(f"{UAZAPI_URL}/send/text",
                              headers={"token": inst["api_key"],
                                       "Content-Type": "application/json"},
                              json={"number": test_to,
                                    "text": montar(passo, ag, hoje)}, timeout=120)
            print(f"[teste] {passo} -> ...{test_to[-4:]} HTTP {r.status_code}")
            time.sleep(8)
        return 0

    agora = agora_sp()
    modo = (os.environ.get("MODO") or "auto").strip().lower()
    if modo == "auto":
        modo = "manha" if agora.hour < 13 else "tarde"
    if not dry and not os.environ.get("AGORA") and jitter_max > 0:
        atraso = random.uniform(0, jitter_max * 60)
        print(f"[jitter] variando o horário de início: +{int(atraso // 60)} min")
        time.sleep(atraso)

    ultimo_envio: dict = {}
    if modo == "manha":
        if agora_sp().hour >= 17:
            print("[janela] tarde demais pra rodada da manhã.")
            return 0
        if not dry:
            esperar_ate(9)
        passos = ["dia"]
        presenca_ok = False
        if agora_sp().weekday() != 6:
            if "--sync-presenca" in sys.argv:
                presenca_ok = sync_presenca()
            elif dry:   # simulacao local: usa a presenca que ja esta no CRM
                presenca_ok = True
            passos.append("faltou")
        passos = [p for p in passos if not so_passos or p in so_passos]
        rodada(sb, passos, presenca_ok, dry, detalhe, max_run, ultimo_envio)
        return 0

    # tarde: pega o "dia" que ficou pra tras e depois manda a vespera
    a = agora_sp()
    if a.hour >= 20 or (a.hour == 19 and a.minute > 45):
        print(f"[janela] {a:%H:%M} BRT — tarde demais, a rodada da manhã cobre.")
        return 0
    if a.hour < 17 and (not so_passos or "dia" in so_passos):
        rodada(sb, ["dia"], False, dry, detalhe, max_run, ultimo_envio)
    if not so_passos or "vespera" in so_passos:
        if not dry:
            esperar_ate(17)
        rodada(sb, ["vespera"], False, dry, detalhe, max_run, ultimo_envio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
