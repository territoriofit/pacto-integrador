# -*- coding: utf-8 -*-
"""
Regua de faltosos: alunos ATIVOS que pararam de vir recebem ate 3 mensagens
de acolhimento — aos 5, 10 e 15 dias sem acesso na catraca. Depois de 15 dias
a regua NAO manda mais nada (decisao Andre 28/09/2026).

Fluxo diario (GitHub Actions, depois do sync-diario):
  1. Le os alunos ativos do CRM (leads status cliente/inadimplente) e usa
     metadata.ultimo_acesso (sync_ultimo_acesso, evento 'Chegou' da catraca).
     So vale quem foi sincronizado HOJE (ultimo_acesso_synced_at) — cadastro
     com dado velho fica de fora pra nao chamar de faltoso quem veio ontem.
  2. Passo pela faixa de dias sem vir (tolerancia de 2 dias cobre domingo e
     run que falhou): f5 = 5 a 7 dias, f10 = 10 a 12, f15 = 15 a 17.
  3. Estado em agent_activity (campanha "faltosos-regua", disparo_key
     "faltosos-<lead_id>-<ultimo_acesso>-f<passo>"): cada passo sai 1 vez por
     ciclo de ausencia; se o aluno volta e some de novo, e outro ciclo.
  3b. CONFERENCIA AO VIVO: antes de cada envio consulta a linha-tempo do aluno
     no Pacto e recalcula o ultimo acesso em horario de Brasilia. Motivos
     (validacao de 28/09): o sync roda de manha, entao quem veio depois dele
     ainda aparece como ausente; e o sync grava a data em UTC, o que adianta
     1 dia os acessos depois das 21h. Se o Pacto nao responder, NAO envia.
  3c. DIAS SEM REGISTRO (DIAS_SEM_REGISTRO): dias em que a catraca ficou sem
     internet e nao gravou acesso (26 e 27/09/2026, informado pelo Andre). Se
     existe um dia desses depois do ultimo acesso gravado, o aluno ganha o
     beneficio da duvida: a contagem parte do ultimo dia sem registro. Quem
     ja esta ha mais de 17 dias sem acesso GRAVADO fica fora de qualquer jeito.
  4. TRAVAS: fora quem tem parcela atrasada (ja esta na regua de cobranca),
     quem tem tag de opt-out, quem recebeu o MESMO passo nos ultimos
     COOLDOWN_DIAS (padrao 30 — aluno de 1x/semana nao recebe toda semana) e
     quem ja esta em conversa (mensagem do aluno ou de humano da equipe depois
     do ultimo acesso).
  5. Prioridade quando passa do limite do run: mais dias sem vir primeiro
     (f15 > f10 > f5). Envio DIVIDIDO entre os numeros de INSTANCIAS (padrao
     91, Professores e 2000 — decisao Andre 28/09); so entram os que estiverem
     conectados no CRM, cada um com limite proprio (MAX_POR_INSTANCIA) e
     90-150s entre os seus envios. O aluno recebe os 3 passos do mesmo numero.
     Texto puro, jitter de inicio, seg a sab, janela ate 19:30 BRT.

Quem responder cai no inbox do numero que enviou; a nota no context do lead
avisa a Clara de que e aluno ativo em acolhimento (nao e venda).

Env: SUPABASE_KEY e as PACTO_* do sync (conferencia ao vivo). O token de cada
numero vem do CRM (whatsapp_instances.api_key).
Opcional: DRY_RUN=1 (so lista) | TEST_TO=5516... (envia os 3 textos de
exemplo so pra esse numero, sem gravar nada) | INSTANCIAS=numeros separados
por virgula | MAX_POR_INSTANCIA=n (padrao 30) | MAX_POR_RUN=n (padrao: soma
dos limites) | HORA_INICIO=h (padrao 14) | COOLDOWN_DIAS=n (padrao 30) |
DIAS_SEM_REGISTRO=AAAA-MM-DD,... | JITTER_MAX_MIN=n.
"""

import os
import random
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"
TZ_SP = timezone(timedelta(hours=-3))
CAMPANHA = "faltosos-regua"
MARCA = "[Régua de faltosos]"
NOTA = ("\n" + MARCA + " Aluno ATIVO que recebeu mensagem automática de "
        "acolhimento por estar sem vir à academia. Não é venda: acolher, "
        "entender o motivo e oferecer ajuda pra retomar (ajuste de treino com "
        "professor, aulas coletivas). Não falar de cancelamento, valores ou "
        "desconto — se o aluno trouxer esses assuntos, encaminhar pra consultora.")

# passo -> (dias minimo, dias maximo)
FAIXAS = {15: (15, 17), 10: (10, 12), 5: (5, 7)}
LIMITE_DIAS = 17  # acima disso, pelo acesso gravado, a regua nao fala mais

# numeros de envio, em ordem: 91, Professores, 2000 (so os conectados entram)
INSTANCIAS_PADRAO = "5516992143091,5516992580287,5516988772000"
# catraca sem internet, acessos nao gravados (informado pelo Andre 28/09/2026)
DIAS_SEM_REGISTRO_PADRAO = "2026-09-26,2026-09-27"

# Textos do Andre (28/09/2026)
MSGS = {
    5: ("Oi, {nome}! Tudo bem? 😊\n\n"
        "Passando por aqui porque sentimos sua falta nos últimos dias na "
        "Território Fit.\n\n"
        "Tá tudo certo por aí? Se pudermos te ajudar de alguma forma pra "
        "voltar à rotina de treinos, conta com a gente! 💪\n\n"
        "Se estiver querendo dar uma variada, também podemos te ajudar com "
        "uma mudança no treino ou indicar alguma das nossas aulas coletivas "
        "pra você experimentar.\n\n"
        "Estamos por aqui! 🧡"),
    10: ("Oi, {nome}! Tudo bem?\n\n"
         "Percebemos que já faz alguns dias que você não aparece por aqui e "
         "resolvemos passar pra saber como você está. 😊\n\n"
         "Às vezes a rotina aperta, o treino fica repetitivo ou bate aquela "
         "desmotivação mesmo. Se for o seu caso, podemos te ajudar!\n\n"
         "Podemos conversar com um professor pra dar uma renovada no seu "
         "treino ou te indicar alguma aula coletiva diferente pra você "
         "experimentar.\n\n"
         "O importante é não deixar a rotina parar de vez. Conta com a "
         "Território! 💪🧡"),
    15: ("Oi, {nome}! Como você está?\n\n"
         "Já faz um tempinho que não vemos você treinando por aqui e sentimos "
         "sua falta. 🧡\n\n"
         "Queremos saber se aconteceu alguma coisa ou se podemos fazer algo "
         "pra te ajudar a retomar os treinos.\n\n"
         "Se você enjoou da rotina, podemos verificar uma mudança no seu "
         "treino. Se quiser algo diferente e mais dinâmico, temos várias "
         "aulas coletivas que você pode experimentar também.\n\n"
         "Me conta: tem alguma coisa que está dificultando sua volta pra "
         "academia? Talvez a gente consiga te ajudar. 😊"),
}

OPT_OUT = {"nao contatar", "nao_contatar", "opt_out", "opt-out", "bloqueado"}


def _sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _norm(v) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(v or "").lower())
                   if unicodedata.category(c) != "Mn")


def _primeiro_nome(nome: str) -> str:
    p = (nome or "").replace("﻿", "").strip().split()
    return p[0].title() if p else "tudo bem"


def _fone_55(phone: str) -> str | None:
    d = "".join(c for c in (phone or "") if c.isdigit())
    if len(d) in (10, 11):
        return "55" + d
    if len(d) in (12, 13) and d.startswith("55"):
        return d
    return None


def aguardar_janela_comercial(hora_inicio: int) -> bool:
    agora = datetime.now(TZ_SP)
    if agora.weekday() == 6:
        print("[janela] domingo — regua nao envia.")
        return False
    if agora.hour >= 20 or (agora.hour == 19 and agora.minute > 30):
        print(f"[janela] {agora:%H:%M} BRT — tarde demais, abortando.")
        return False
    while agora.hour < hora_inicio:
        espera = min(1800,
                     (hora_inicio - agora.hour) * 3600 - agora.minute * 60)
        print(f"[janela] {agora:%H:%M} BRT — aguardando {espera//60} min...")
        time.sleep(espera)
        agora = datetime.now(TZ_SP)
    return True


def faixa(dias: int) -> int | None:
    return next((p for p, (lo, hi) in FAIXAS.items() if lo <= dias <= hi), None)


def dias_sem_registro() -> list[date]:
    bruto = os.environ.get("DIAS_SEM_REGISTRO") or DIAS_SEM_REGISTRO_PADRAO
    out = []
    for p in bruto.split(","):
        try:
            out.append(date.fromisoformat(p.strip()))
        except ValueError:
            pass
    return out


def referencia(ultimo: date, hoje: date, apagoes: list[date]) -> date:
    """Data de onde a ausencia e contada: o ultimo acesso gravado ou, se
    houver, o ultimo dia sem registro depois dele (beneficio da duvida)."""
    depois = [d for d in apagoes if ultimo < d <= hoje]
    return max([ultimo] + depois)


def instancias(sb: dict) -> list[dict]:
    """Numeros configurados que estao conectados no CRM, na ordem de INSTANCIAS."""
    numeros = [n.strip() for n in
               (os.environ.get("INSTANCIAS") or INSTANCIAS_PADRAO).split(",")
               if n.strip()]
    r = requests.get(f"{SUPABASE_URL}/rest/v1/whatsapp_instances",
                     params={"select": "name,phone_number,status,api_key"},
                     headers=sb, timeout=30)
    r.raise_for_status()
    por_fone = {i.get("phone_number"): i for i in r.json()}
    ativas = []
    for n in numeros:
        i = por_fone.get(n)
        if i and i.get("status") == "connected" and i.get("api_key"):
            ativas.append({"fone": n, "nome": i["name"], "token": i["api_key"]})
        else:
            print(f"[numero] ...{n[-4:]} fora: "
                  f"{(i or {}).get('status') or 'nao cadastrado no CRM'}")
    return ativas


def acesso_real(pacto, matricula: str) -> date | None:
    """Ultimo acesso na catraca direto do Pacto, em horario de Brasilia.
    Usa o evento mais recente (a linha-tempo nao vem estritamente em ordem).
    Levanta excecao se o Pacto nao responder."""
    eventos = pacto.linha_tempo_aluno(int(matricula))
    datas = [
        datetime.fromtimestamp(int(ev["data"]) / 1000, tz=TZ_SP).date()
        for ev in eventos
        if ev.get("data") and (
            "chegou" in (ev.get("descricao") or "").lower()
            or ev.get("evento") in ("CHECKIN", "ACESSOU"))
    ]
    return max(datas) if datas else None


def alunos_ativos(sb: dict) -> list[dict]:
    campos = ("id,name,phone,status,tags,context,"
              "mat:metadata->>pacto_matricula,"
              "ua:metadata->>ultimo_acesso,"
              "us:metadata->>ultimo_acesso_synced_at")
    linhas: list[dict] = []
    for ini in range(0, 10000, 1000):
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/leads",
            params={"select": campos, "status": "in.(cliente,inadimplente)",
                    "order": "id"},
            headers={**sb, "Range": f"{ini}-{ini + 999}"}, timeout=90)
        r.raise_for_status()
        lote = r.json()
        linhas += lote
        if len(lote) < 1000:
            break
    return linhas


def em_cobranca(sb: dict) -> set[str]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/parcelas_atrasadas",
                     params={"select": "lead_id", "limit": "2000"},
                     headers=sb, timeout=60)
    r.raise_for_status()
    return {p["lead_id"] for p in r.json() if p.get("lead_id")}


def estado_regua(sb: dict, desde: str) -> tuple[set[str], dict, dict]:
    """(disparo_keys ja feitos, {(lead_id, passo): data do ultimo envio},
    {lead_id: numero que enviou por ultimo})."""
    feitos: set[str] = set()
    ultimo: dict = {}
    numero: dict = {}
    for ini in range(0, 20000, 1000):
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            params={"select": "created_at,metadata",
                    "metadata->>campanha": f"eq.{CAMPANHA}",
                    "created_at": f"gte.{desde}",
                    "order": "created_at.asc"},
            headers={**sb, "Range": f"{ini}-{ini + 999}"}, timeout=60)
        r.raise_for_status()
        lote = r.json()
        for row in lote:
            m = row.get("metadata") or {}
            if m.get("disparo_key"):
                feitos.add(m["disparo_key"])
            if m.get("lead_id") and m.get("passo") is not None:
                ultimo[(m["lead_id"], int(m["passo"]))] = row["created_at"][:10]
                if m.get("instancia"):
                    numero[m["lead_id"]] = m["instancia"]
        if len(lote) < 1000:
            break
    return feitos, ultimo, numero


def em_conversa(sb: dict, lead_id: str, desde_iso: str) -> bool:
    """Aluno escreveu, ou humano da equipe falou com ele, depois do ultimo
    acesso (CRM grava sent_by 'human'; celular fica sem sent_by; Clara =
    'ai_agent'). Disparos por API nao ficam gravados."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/whatsapp_messages",
        params={"select": "id",
                "lead_id": f"eq.{lead_id}",
                "group_id": "is.null",
                "sent_at": f"gte.{desde_iso}",
                "or": "(is_from_me.eq.false,metadata->>sent_by.is.null,"
                      "metadata->>sent_by.neq.ai_agent)",
                "limit": "1"},
        headers=sb, timeout=30)
    r.raise_for_status()
    return bool(r.json())


def selecionar(sb: dict, hoje: date, cooldown: int, apagoes: list[date]):
    """Candidatos por faixa, ja sem os bloqueios baratos (dado velho, cobranca,
    opt-out). Conferencia ao vivo no Pacto, passo ja enviado, cooldown e trava
    de conversa sao checados na hora do envio, aluno a aluno."""
    alunos = alunos_ativos(sb)
    cobranca = em_cobranca(sb)
    feitos, ultimo, numero = estado_regua(
        sb, (hoje - timedelta(days=max(cooldown, 30) + 5)).isoformat())
    fora = {"dado de acesso desatualizado": 0, "sem registro de acesso": 0,
            "em cobrança": 0, "opt-out": 0, "fora das faixas": 0}
    frescos = 0
    cand: list[dict] = []
    for a in alunos:
        if (a.get("us") or "")[:10] != hoje.isoformat():
            fora["dado de acesso desatualizado"] += 1
            continue
        frescos += 1
        if not a.get("ua"):
            fora["sem registro de acesso"] += 1
            continue
        try:
            ua = date.fromisoformat(a["ua"][:10])
        except ValueError:
            fora["sem registro de acesso"] += 1
            continue
        dias = (hoje - referencia(ua, hoje, apagoes)).days
        # o sync pode adiantar 1 dia (UTC): olha 1 dia a mais pra nao perder
        # quem esta na borda; a conferencia ao vivo decide o passo de verdade
        passo = faixa(dias) or faixa(dias + 1)
        if passo is None or (hoje - ua).days > LIMITE_DIAS + 1:
            fora["fora das faixas"] += 1
            continue
        if a["id"] in cobranca:
            fora["em cobrança"] += 1
            continue
        if {_norm(t) for t in (a.get("tags") or [])} & OPT_OUT:
            fora["opt-out"] += 1
            continue
        cand.append({**a, "dias": dias, "passo": passo, "ua_data": ua})
    cand.sort(key=lambda c: -c["dias"])  # f15 > f10 > f5; ultimo dia da faixa antes
    print(f"[crm] {len(alunos)} cadastro(s) ativo(s), {frescos} com acesso "
          f"sincronizado hoje, {len(cand)} candidato(s) nas faixas")
    return cand, fora, feitos, ultimo, numero


def escolher_numero(lead_id: str, anterior: str | None, ativas: list[dict],
                    usados: dict, limite: int) -> dict | None:
    """Mesmo numero do passo anterior quando possivel; senao divisao estavel
    pelo id do aluno, pulando numero que ja bateu o limite do dia."""
    livres = [i for i in ativas if usados.get(i["fone"], 0) < limite]
    if not livres:
        return None
    for i in livres:
        if i["fone"] == anterior:
            return i
    base = int(lead_id.replace("-", "")[:8], 16) % len(ativas)
    for k in range(len(ativas)):
        i = ativas[(base + k) % len(ativas)]
        if i in livres:
            return i
    return None


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    test_to = os.environ.get("TEST_TO", "").replace("﻿", "").strip()
    max_por_inst = int(os.environ.get("MAX_POR_INSTANCIA") or "30")
    hora_inicio = int(os.environ.get("HORA_INICIO") or "14")
    cooldown = int(os.environ.get("COOLDOWN_DIAS") or "30")
    jitter_max = int(os.environ.get("JITTER_MAX_MIN") or "40")
    if not key:
        print("Falta env SUPABASE_KEY")
        return 1

    print("[campanha] regua de faltosos (5, 10 e 15 dias sem vir)")
    sb = _sb_headers(key)
    ativas = instancias(sb)
    if not ativas:
        print("Nenhum numero conectado — nada a fazer.")
        return 1
    print("[numeros] " + ", ".join(f"{i['nome']} (...{i['fone'][-4:]})"
                                   for i in ativas)
          + f" | limite {max_por_inst} por numero")
    max_por_run = int(os.environ.get("MAX_POR_RUN")
                      or str(max_por_inst * len(ativas)))

    if test_to:
        for passo in (5, 10, 15):
            resp = requests.post(
                f"{UAZAPI_URL}/send/text",
                headers={"token": ativas[0]["token"],
                         "Content-Type": "application/json"},
                json={"number": test_to,
                      "text": MSGS[passo].format(nome="André")}, timeout=120)
            print(f"[teste] f{passo} por {ativas[0]['nome']} -> "
                  f"...{test_to[-4:]} HTTP {resp.status_code}")
            time.sleep(8)
        return 0

    if not dry and not aguardar_janela_comercial(hora_inicio):
        return 0
    if not dry and jitter_max > 0:
        atraso = random.uniform(0, jitter_max * 60)
        print(f"[jitter] variando horario do disparo: +{int(atraso // 60)}min")
        time.sleep(atraso)

    hoje = datetime.now(TZ_SP).date()
    apagoes = dias_sem_registro()
    if any(hoje - timedelta(days=LIMITE_DIAS) <= d <= hoje for d in apagoes):
        print("[sem registro] dias sem acesso gravado na janela: "
              + ", ".join(f"{d:%d/%m}" for d in apagoes))
    cand, fora, feitos, ultimo, numero = selecionar(sb, hoje, cooldown, apagoes)
    por_passo = {p: sum(1 for c in cand if c["passo"] == p) for p in (5, 10, 15)}
    print(f"[faixas pelo CRM] f5={por_passo[5]} f10={por_passo[10]} "
          f"f15={por_passo[15]} | fora: "
          + ", ".join(f"{k}={v}" for k, v in fora.items() if v))
    if not cand:
        print("Nenhum alvo hoje.")
        return 0

    # conferencia ao vivo no Pacto (mesmo cliente do sync)
    import logging
    logging.disable(logging.CRITICAL)
    from agente_integrador_pacto import PactoClient
    pacto = PactoClient()

    enviados, conversa, sem_fone, falhas = 0, 0, 0, 0
    voltou, sem_pacto, repetido, corrigidos = 0, 0, 0, 0
    env_passo = {5: 0, 10: 0, 15: 0}
    usados: dict = {}
    ultimo_envio: dict = {}
    for c in cand:
        if enviados >= max_por_run:
            print(f"[lote] limite de {max_por_run} atingido.")
            break
        nome = c.get("name") or ""
        fone = _fone_55(c.get("phone") or "")
        if not fone:
            sem_fone += 1
            continue

        # 1) o Pacto confirma a ausencia agora?
        try:
            real = acesso_real(pacto, c["mat"]) if c.get("mat") else None
        except Exception as e:
            print(f"[pacto] {_primeiro_nome(nome)} — sem resposta ({str(e)[:60]}), "
                  "nao envia.")
            sem_pacto += 1
            continue
        if real is None:
            sem_pacto += 1
            continue
        if real != c["ua_data"]:
            corrigidos += 1
        dias = (hoje - referencia(real, hoje, apagoes)).days
        passo = faixa(dias) if (hoje - real).days <= LIMITE_DIAS else None
        if passo is None:
            voltou += 1
            print(f"[fora] {_primeiro_nome(nome)} — Pacto mostra ultimo acesso em "
                  f"{real:%d/%m} ({dias}d contados), fora das faixas.")
            continue
        c.update(dias=dias, passo=passo, ua_data=real,
                 key=f"faltosos-{c['id']}-{real.isoformat()}-f{passo}")

        # 2) passo ja enviado neste ciclo, ou mesmo passo ha pouco tempo
        ant = ultimo.get((c["id"], passo))
        if c["key"] in feitos or (
                ant and (hoje - date.fromisoformat(ant)).days < cooldown):
            repetido += 1
            continue

        # 3) ja esta em conversa com a equipe
        desde = (real + timedelta(days=1)).isoformat() + "T00:00:00-03:00"
        if em_conversa(sb, c["id"], desde):
            conversa += 1
            print(f"[pausa] f{passo} {_primeiro_nome(nome)} — ja em conversa "
                  "depois do ultimo acesso.")
            continue
        inst = escolher_numero(c["id"], numero.get(c["id"]), ativas, usados,
                               max_por_inst)
        if inst is None:
            print("[lote] todos os numeros bateram o limite do dia.")
            break
        texto = MSGS[passo].format(nome=_primeiro_nome(nome))
        if dry:
            print(f"[DRY] f{passo} — {_primeiro_nome(nome)} ...{fone[-4:]} "
                  f"{c['dias']}d sem vir (ultimo acesso {c['ua_data']:%d/%m}) "
                  f"via {inst['nome']}")
            enviados += 1
            env_passo[passo] += 1
            usados[inst["fone"]] = usados.get(inst["fone"], 0) + 1
            continue

        # anti-bloqueio: 90-150s entre envios do MESMO numero
        if inst["fone"] in ultimo_envio:
            espera = (90 + random.uniform(0, 60)
                      - (time.monotonic() - ultimo_envio[inst["fone"]]))
            if espera > 0:
                time.sleep(espera)
        resp = requests.post(
            f"{UAZAPI_URL}/send/text",
            headers={"token": inst["token"], "Content-Type": "application/json"},
            json={"number": fone, "text": texto}, timeout=120)
        ultimo_envio[inst["fone"]] = time.monotonic()
        ok = resp.status_code == 200
        print(f"[send] f{passo} {_primeiro_nome(nome)} -> ...{fone[-4:]} "
              f"HTTP {resp.status_code}")
        if not ok:
            print("       resp:", resp.text[:200])
            falhas += 1
            # numero inexistente: grava pra nao tentar de novo neste ciclo
            if "not on WhatsApp" in resp.text:
                requests.post(
                    f"{SUPABASE_URL}/rest/v1/agent_activity",
                    headers={**sb, "Prefer": "return=minimal"},
                    json={"agent_slug": "crm-relacionamento",
                          "title": f"Faltosos f{passo}: numero fora do WhatsApp",
                          "detail": f"{nome.title()} — envio nao realizado",
                          "status": "erro",
                          "metadata": {"disparo_key": c["key"],
                                       "campanha": CAMPANHA,
                                       "lead_id": c["id"]}},
                    timeout=30)
            continue

        enviados += 1
        env_passo[passo] += 1
        usados[inst["fone"]] = usados.get(inst["fone"], 0) + 1
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": f"Faltosos: mensagem de {passo} dias enviada",
                  "detail": f"{nome.title()} — {c['dias']} dias sem vir "
                            f"(ultimo acesso {c['ua_data']:%d/%m}) "
                            f"pelo {inst['nome']}",
                  "status": "concluido",
                  "metadata": {"disparo_key": c["key"], "campanha": CAMPANHA,
                               "passo": passo, "dias_sem_vir": c["dias"],
                               "ultimo_acesso": c["ua_data"].isoformat(),
                               "instancia": inst["fone"],
                               "lead_id": c["id"]}},
            timeout=30)
        ctx = c.get("context") or ""
        if MARCA not in ctx:
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/leads",
                params={"id": f"eq.{c['id']}"},
                headers={**sb, "Prefer": "return=minimal"},
                json={"context": ctx + NOTA}, timeout=30)

    print("[numeros] envios por numero: "
          + ", ".join(f"{i['nome']}={usados.get(i['fone'], 0)}" for i in ativas))
    print(f"\nResumo: {enviados} {'selecionado(s)' if dry else 'enviado(s)'} "
          f"(f5={env_passo[5]}, f10={env_passo[10]}, f15={env_passo[15]}), "
          f"{conversa} ja em conversa, {voltou} fora da faixa pelo Pacto ao vivo, "
          f"{repetido} ja receberam, {sem_pacto} sem confirmacao do Pacto, "
          f"{sem_fone} sem telefone, {falhas} falha(s) | "
          f"{corrigidos} com data do CRM diferente do Pacto")

    if not dry and enviados:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/agent_activity",
            headers={**sb, "Prefer": "return=minimal"},
            json={"agent_slug": "crm-relacionamento",
                  "title": "Regua de faltosos (run diario)",
                  "detail": f"{enviados} msg(s) enviadas (5d: {env_passo[5]}, "
                            f"10d: {env_passo[10]}, 15d: {env_passo[15]}), "
                            f"{conversa} ja em conversa.",
                  "status": "concluido",
                  "metadata": {"campanha": CAMPANHA}},
            timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
