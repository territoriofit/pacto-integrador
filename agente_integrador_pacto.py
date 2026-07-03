"""
Agente Integrador Pacto -- Territorio Fit
Modulos: Alunos, Frequencia (ZW), Contratos, Notificacoes
"""

import os
import json
import time
import uuid
import logging
import requests
from datetime import datetime, timedelta, date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env.integracao")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

EXPORT_DIR = Path(r"G:\Meu Drive\André Obsidian\André Obsidian\09 - Dados Pacto")
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

PACTO_BASE_URL = os.getenv("PACTO_BASE_URL", "https://zw819.pactosolucoes.com.br/TreinoWeb/prest")
PACTO_TOKEN    = os.getenv("PACTO_TOKEN")
PACTO_CHAVE    = os.getenv("PACTO_CHAVE")
PACTO_EMP_ID   = os.getenv("PACTO_EMPRESA_ID")
PACTO_CTX      = os.getenv("PACTO_CTX")
PACTO_USER     = os.getenv("PACTO_USER")
PACTO_PWD      = os.getenv("PACTO_PWD")


def _ts_to_date(ts) -> datetime | None:
    """Converte timestamp Unix em ms para datetime. Retorna None se invalido."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000)
    except Exception:
        return None


class PactoClient:
    """Cliente HTTP para a API TreinoWeb da Pacto Solucoes -- Territorio Fit."""

    def __init__(self):
        self.base        = PACTO_BASE_URL
        self.ctx         = PACTO_CTX
        self.jwt_token   = None
        self._authed     = False
        self.session     = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {PACTO_TOKEN}",
            "chave":         PACTO_CHAVE,
            "empresaId":     PACTO_EMP_ID,
            "Content-Type":  "application/json",
        })
        # autenticacao automatica na inicializacao
        if not self.login_jwt():
            log.warning("Nao foi possivel autenticar automaticamente -- usando token estatico")

    # -- Requisicao com retry + re-auth automatico ----------------------------

    def _req(self, method: str, path: str, _retry_auth: bool = True, **kwargs):
        url = f"{self.base}{path}"
        for tentativa in range(3):
            try:
                r = self.session.request(method, url, timeout=30, **kwargs)
                if r.status_code in (200, 201):
                    try:
                        return r.json()
                    except Exception:
                        return {"raw": r.text}
                elif r.status_code == 401:
                    if _retry_auth:
                        log.info("Token expirado -- renovando autenticacao...")
                        if self.login_jwt():
                            return self._req(method, path, _retry_auth=False, **kwargs)
                    raise Exception("Token invalido ou expirado apos renovacao")
                elif r.status_code == 403:
                    raise Exception(f"Sem permissao: {path}")
                elif r.status_code >= 500:
                    log.warning(f"Pacto 500 em {path} -- tentativa {tentativa+1}/3")
                    time.sleep(5)
                else:
                    log.error(f"Pacto {r.status_code} em {path}: {r.text[:200]}")
                    return {"erro": r.status_code, "mensagem": r.text[:200]}
            except requests.exceptions.ConnectionError:
                log.warning(f"Falha de conexao -- tentativa {tentativa+1}/3")
                time.sleep(3)
        raise Exception(f"Maximo de tentativas atingido: {path}")

    # -- Autenticacao JWT ------------------------------------------------------

    def login_jwt(self) -> bool:
        """
        Obtem token JWT e reconfigura session com empresaId=1.
        O empresaId '1' e o indice da Territorio Fit no payload do JWT.
        """
        try:
            r = requests.post(
                f"{self.base}/login",
                json={"username": PACTO_USER, "senha": PACTO_PWD, "chave": PACTO_CHAVE},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                self.jwt_token = (
                    data.get("token") or data.get("access_token")
                    or data.get("jwt") or data.get("content")
                )
                if self.jwt_token:
                    self.session.headers.update({
                        "Authorization": f"Bearer {self.jwt_token}",
                        "empresaId": "1",
                    })
                    log.info("Login JWT realizado com sucesso")
                    return True
            log.error(f"Falha no login JWT: {r.status_code} -- {r.text[:200]}")
        except Exception as e:
            log.error(f"Erro no login JWT: {e}")
        return False

    # -- Descoberta de endpoints -----------------------------------------------

    def listar_empresas(self) -> dict:
        return self._req("GET", "/EndpointControl/empresas")

    def listar_endpoints(self) -> dict:
        return self._req("GET", "/EndpointControl/show")

    def views_bi(self) -> dict:
        return self._req("GET", "/psec/grafico-bi/views/all")

    # -- ALUNOS ----------------------------------------------------------------

    def todos_alunos(self, situacao: str = None) -> list:
        """
        Retorna alunos via /psec/alunos (requer JWT + empresaId=1).
        situacao: 'ATIVO', 'INATIVO', 'VISITANTE' ou None para todos.

        Campos principais:
          id, matriculaZW, nome, situacaoAluno, dataNascimento, sexo,
          emails, fones, planoZW, contratoZW {vencimento (timestamp ms), tipo},
          codigoCliente, codigoPessoa, programas, parq_status.
        """
        todos       = []
        page        = 0
        size        = 500
        total_pages = None
        while True:
            r = self._req("GET", "/psec/alunos", params={"size": size, "page": page})
            content = r.get("content", []) if isinstance(r, dict) else (r or [])
            if not content:
                break
            if total_pages is None:
                total_pages = r.get("totalPages", 1)
            if situacao:
                content = [a for a in content if a.get("situacaoAluno") == situacao]
            todos.extend(content)
            page += 1
            if page >= total_pages:
                break
        return todos

    def alunos_ativos(self) -> list:
        resultado = self.todos_alunos(situacao="ATIVO")
        log.info(f"Alunos ativos: {len(resultado)}")
        return resultado

    def alunos_inativos(self) -> list:
        resultado = self.todos_alunos(situacao="INATIVO")
        log.info(f"Alunos inativos (ex-alunos): {len(resultado)}")
        return resultado

    def checkins_recentes(self) -> list:
        """Ultimos check-ins agregados da academia (todos os alunos)."""
        r = self._req("GET", "/psec/alunos/ultimos-checkins-agregados")
        return r.get("content", []) if isinstance(r, dict) else (r or [])

    def lista_acessos_recentes(self, tipo: int = 1, limite: int = 50) -> list:
        """
        Lista rapida dos ultimos acessos fisicos registrados na academia.
        tipo=1 lista todos; tipo=2 pode filtrar por tipo especifico (enum da API).
        Retorna campos: codigoCliente, matricula, hora, nome, plano, situacao, tipoCheckin.
        """
        r = self._req("GET", "/psec/alunos/lista-rapida-acessos",
                      params={"tipo": tipo, "limite": limite})
        content = r.get("content", {}) if isinstance(r, dict) else {}
        return content.get("lista", []) if isinstance(content, dict) else []

    def historico_presenca(self, matricula: int) -> dict:
        """
        Resumo de presenca do aluno em aulas/turmas agendadas:
          totalAulasRealizadas, aulasMesAtual, semanasConsecutivas.
        Nota: conta apenas aulas agendadas pelo sistema -- nao conta treino livre na catraca.
        Requer param empresa=1.
        """
        r = self._req("GET", f"/cliente/{self.ctx}/historico-presenca",
                      params={"matricula": matricula, "empresa": 1, "atualizaCache": True})
        return r.get("content", {}) if isinstance(r, dict) else {}

    def distribuicao_acesso_semanal(self, cod_aluno: int,
                                    data_ini: date = None, data_fim: date = None) -> dict:
        """
        Distribuicao de acessos fisicos do aluno por dia da semana no periodo.
        Usa dados de catraca/check-in fisico (nao TreinoWeb app).
        Retorna dict com chaves SEG, TER, QUA, QUI, SEX, SAB, DOM.
        data_ini/data_fim default: ultimos 6 meses.
        """
        if data_fim is None:
            data_fim = date.today()
        if data_ini is None:
            data_ini = date(data_fim.year - (1 if data_fim.month <= 6 else 0),
                            (data_fim.month - 6) % 12 or 12, 1)
        ts_ini = int(datetime(data_ini.year, data_ini.month, data_ini.day).timestamp() * 1000)
        ts_fim = int(datetime(data_fim.year, data_fim.month, data_fim.day).timestamp() * 1000)
        r = self._req("GET",
                      f"/cliente/{self.ctx}/app/consultarQuantidadeAcessosClientesAgrupadosDia",
                      params={"codigoAluno": cod_aluno, "username": "",
                              "dataInicial": ts_ini, "dataFinal": ts_fim})
        if isinstance(r, dict) and "sucesso" in r:
            return r["sucesso"]
        return {}

    def linha_tempo_aluno(self, matricula: int) -> list:
        """
        Historico de eventos do aluno no sistema ZW:
        ACABOU_TREINO, MONTOU_TREINO, REVISOU_TREINO, checkins, etc.
        O campo 'data' e timestamp Unix em ms.
        """
        r = self._req("GET", f"/psec/alunos/linha-tempo/{matricula}")
        return r.get("content", []) if isinstance(r, dict) else (r or [])

    def ultimo_treino(self, matricula: int) -> datetime | None:
        """Retorna a data do ultimo treino concluido (ACABOU_TREINO) do aluno."""
        eventos = self.linha_tempo_aluno(matricula)
        for ev in eventos:
            if ev.get("evento") in ("ACABOU_TREINO", "CHECKIN", "ACESSOU"):
                return _ts_to_date(ev.get("data"))
        return None

    def ultimo_acesso_catraca(self, matricula: int) -> datetime | None:
        """
        Data do ultimo acesso fisico (catraca) do aluno, via linha-tempo.
        O acesso na catraca gera evento NOTIFICACAO cuja descricao varia:
        'Notificacao: Chegou' ou 'Notificacao: Aluno chegou e possui Indice
        de Risco' (grupo de risco) — por isso o match e case-insensitive em
        'chegou'. Cobre tambem check-ins Gympass/TotalPass.
        """
        eventos = self.linha_tempo_aluno(matricula)
        for ev in eventos:
            desc = (ev.get("descricao") or "").lower()
            if "chegou" in desc or ev.get("evento") in ("CHECKIN", "ACESSOU"):
                return _ts_to_date(ev.get("data"))
        return None

    def alunos_faltosos(self, dias: int = 7) -> list:
        """
        Retorna alunos ATIVOS sem treino registrado nos ultimos `dias` dias.
        Usa linha-tempo por aluno (evento ACABOU_TREINO) -- sistema ZW musculacao.
        """
        ativos   = self.alunos_ativos()
        corte    = datetime.now() - timedelta(days=dias)
        faltosos = []

        log.info(f"Verificando {len(ativos)} alunos ativos (ultimos {dias} dias)...")

        for aluno in ativos:
            matricula = aluno.get("matriculaZW") or aluno.get("id")
            if not matricula:
                continue
            try:
                ultimo = self.ultimo_treino(matricula)
                if ultimo is None or ultimo < corte:
                    dias_aus = (datetime.now() - ultimo).days if ultimo else 999
                    faltosos.append({
                        **aluno,
                        "ultimo_treino": ultimo.strftime("%d/%m/%Y") if ultimo else None,
                        "dias_ausente":  dias_aus,
                    })
            except Exception as e:
                log.debug(f"Erro linha-tempo aluno {matricula}: {e}")

        faltosos.sort(key=lambda x: x.get("dias_ausente") or 999, reverse=True)
        log.info(f"Alunos faltosos (>{dias} dias): {len(faltosos)}")
        return faltosos

    # -- CONTRATOS / FINANCEIRO ------------------------------------------------

    def inadimplentes(self) -> list:
        """
        Retorna alunos ATIVOS com contrato vencido.
        O campo contratoZW.vencimento e timestamp Unix em ms.
        """
        ativos = self.alunos_ativos()
        hoje   = datetime.now()
        inadimpl = []
        for aluno in ativos:
            ts = (aluno.get("contratoZW") or {}).get("vencimento")
            venc = _ts_to_date(ts)
            if venc and venc < hoje:
                inadimpl.append({
                    **aluno,
                    "vencimento_fmt": venc.strftime("%d/%m/%Y"),
                    "dias_atraso":    (hoje - venc).days,
                })
        inadimpl.sort(key=lambda x: x.get("dias_atraso", 0), reverse=True)
        log.info(f"Inadimplentes (contrato vencido): {len(inadimpl)}")
        return inadimpl

    def cancelamentos(self, dias: int = 30) -> list:
        """
        Retorna alunos INATIVO como representacao de cancelamentos.
        Filtra pelos que tem contratoZW.vencimento nos ultimos `dias` dias.
        """
        inativos = self.alunos_inativos()
        corte    = datetime.now() - timedelta(days=dias)
        recentes = []
        for aluno in inativos:
            ts   = (aluno.get("contratoZW") or {}).get("vencimento")
            venc = _ts_to_date(ts)
            if venc and venc >= corte:
                recentes.append({**aluno, "data_cancelamento": venc.strftime("%d/%m/%Y")})
        log.info(f"Cancelamentos (ultimos {dias} dias): {len(recentes)}")
        return recentes

    def renovacoes(self, dias: int = 30) -> list:
        """
        Retorna alunos ATIVOS com contratoZW.tipo == RENOVACAO cujo vencimento
        e nos proximos `dias` dias (contratos recem-renovados com vencimento futuro).
        """
        ativos  = self.alunos_ativos()
        corte   = datetime.now() - timedelta(days=dias)
        hoje    = datetime.now()
        renovados = []
        for aluno in ativos:
            contrato = aluno.get("contratoZW") or {}
            if contrato.get("tipo") in ("RENOVACAO", "REMATRICULA"):
                ts   = contrato.get("vencimento")
                venc = _ts_to_date(ts)
                if venc and venc > hoje:
                    renovados.append({**aluno, "vencimento_fmt": venc.strftime("%d/%m/%Y")})
        log.info(f"Renovacoes ativas: {len(renovados)}")
        return renovados

    def matriculas_novas(self, dias: int = 30) -> list:
        """
        Retorna alunos ATIVOS com contratoZW.tipo == MATRICULA com vencimento
        nos proximos `dias` * 12 dias (matriculas recentes ainda vigentes).
        """
        ativos = self.alunos_ativos()
        corte  = datetime.now() - timedelta(days=dias)
        hoje   = datetime.now()
        novas  = []
        for aluno in ativos:
            contrato = aluno.get("contratoZW") or {}
            if contrato.get("tipo") == "MATRICULA":
                ts   = contrato.get("vencimento")
                venc = _ts_to_date(ts)
                if venc and venc > hoje:
                    novas.append({**aluno, "vencimento_fmt": venc.strftime("%d/%m/%Y")})
        log.info(f"Matriculas ativas (tipo MATRICULA): {len(novas)}")
        return novas

    def contratos_a_vencer(self, dias: int = 30) -> list:
        """Alunos ATIVOS com contrato vencendo nos proximos `dias` dias."""
        ativos   = self.alunos_ativos()
        hoje     = datetime.now()
        limite   = hoje + timedelta(days=dias)
        a_vencer = []
        for aluno in ativos:
            ts   = (aluno.get("contratoZW") or {}).get("vencimento")
            venc = _ts_to_date(ts)
            if venc and hoje <= venc <= limite:
                a_vencer.append({
                    **aluno,
                    "vencimento_fmt": venc.strftime("%d/%m/%Y"),
                    "dias_restantes": (venc - hoje).days,
                })
        a_vencer.sort(key=lambda x: x.get("dias_restantes", 0))
        log.info(f"Contratos a vencer em {dias} dias: {len(a_vencer)}")
        return a_vencer

    # -- AGENDAMENTOS ----------------------------------------------------------

    def historico_agendamentos(self, codigo_cliente: int, max_results: int = 10, index: int = 0) -> dict:
        return self._req("POST", f"/agendamento/{self.ctx}/historico",
                         params={"codigoCliente": codigo_cliente,
                                 "maxResults": max_results, "index": index})

    def confirmar_agendamento(self, username: str, id_agendamento: str, matricula: int) -> dict:
        return self._req("POST", f"/agendamento/{self.ctx}/confirma",
                         params={"username": username, "idAgendamento": id_agendamento,
                                 "matricula": matricula})

    def cancelar_agendamento(self, username: str, id_agendamento: str, matricula: int) -> dict:
        return self._req("POST", f"/agendamento/{self.ctx}/cancelar",
                         params={"username": username, "idAgendamento": id_agendamento,
                                 "matricula": matricula})

    # -- NOTIFICACOES ----------------------------------------------------------

    def push_lembrar_compromisso(self, data_atual: str) -> dict:
        return self._req("POST", f"/EndpointControl/{self.ctx}/pushLembrarCompromisso",
                         params={"dataAtual": data_atual})

    def push_proximos_agendados(self, data_atual: str, tipo_lembrete: str = "QUINZE_MINUTOS") -> dict:
        return self._req("POST", f"/EndpointControl/{self.ctx}/pushProximosAgendados",
                         params={"dataAtual": data_atual, "tipoLembrete": tipo_lembrete})

    # -- WHATSAPP (stub -- conectar ao novo CRM) --------------------------------

    def notificar_whatsapp(self, telefone: str, mensagem: str, crm_client=None) -> bool:
        """
        Envia mensagem WhatsApp via CRM.
        Passar `crm_client` com metodo .enviar_whatsapp(tel, msg) quando CRM estiver definido.
        """
        if crm_client is None:
            log.info(f"[WhatsApp STUB] Para: {telefone} | Msg: {mensagem[:60]}...")
            return True
        return crm_client.enviar_whatsapp(telefone, mensagem)

    # -- USUÁRIO / ADM ---------------------------------------------------------

    def obter_usuario(self) -> dict:
        """
        Dados do usuário autenticado: nome, perfil, recursos e funcionalidades.
        Usa /psec/validateToken (TreinoWeb) que retorna JSON em claro.
        O /adm/obter-usuario (API Gateway) retorna AES-encriptado -- chave desconhecida.
        """
        r = self._req("GET", "/psec/validateToken")
        return r.get("content", r) if isinstance(r, dict) else r

    # -- DASHBOARD CONSOLIDADO -------------------------------------------------

    def dashboard(self) -> dict:
        """Gera snapshot consolidado da operacao."""
        log.info("Gerando dashboard...")
        agora = datetime.now()

        ativos      = self.alunos_ativos()
        inadimpl    = self.inadimplentes()
        a_vencer    = self.contratos_a_vencer(dias=30)
        cancelados  = self.cancelamentos(dias=30)
        faltosos_7  = self.alunos_faltosos(dias=7)
        faltosos_14 = self.alunos_faltosos(dias=14)

        return {
            "gerado_em": agora.strftime("%d/%m/%Y %H:%M"),
            "alunos": {
                "ativos":           len(ativos),
                "faltosos_7_dias":  len(faltosos_7),
                "faltosos_14_dias": len(faltosos_14),
            },
            "contratos": {
                "inadimplentes":      len(inadimpl),
                "a_vencer_30_dias":   len(a_vencer),
                "cancelamentos_30d":  len(cancelados),
            },
            "detalhes": {
                "faltosos_7_dias": faltosos_7[:20],
                "inadimplentes":   inadimpl[:20],
                "a_vencer":        a_vencer[:20],
                "cancelamentos":   cancelados[:10],
            },
        }

    # -- EXPORTACAO ------------------------------------------------------------

    def exportar(self, nome: str, dados) -> Path:
        arquivo = EXPORT_DIR / f"{nome}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        with open(arquivo, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        log.info(f"Exportado: {arquivo}")
        return arquivo

    def exportar_dashboard_md(self, painel: dict) -> Path:
        a = painel["alunos"]
        c = painel["contratos"]

        linhas_faltosos = ""
        for al in painel["detalhes"]["faltosos_7_dias"]:
            fones = al.get("fones") or []
            tel   = fones[0].get("numero") if fones else "—"
            linhas_faltosos += (
                f"- **{al.get('nome', '—')}** | Tel: {tel} "
                f"| Ausente: {al.get('dias_ausente', '—')} dias "
                f"| Ultimo treino: {al.get('ultimo_treino') or 'nenhum'}\n"
            )

        linhas_inadimpl = ""
        for al in painel["detalhes"]["inadimplentes"]:
            linhas_inadimpl += (
                f"- **{al.get('nome', '—')}** "
                f"| Venceu: {al.get('vencimento_fmt', '—')} "
                f"| Atraso: {al.get('dias_atraso', '—')} dias\n"
            )

        md = f"""# Dashboard Operacional — {painel['gerado_em']}

## Alunos
| Metrica | Valor |
|---|---|
| Ativos | {a['ativos']} |
| Faltosos +7 dias | {a['faltosos_7_dias']} |
| Faltosos +14 dias | {a['faltosos_14_dias']} |

## Contratos
| Metrica | Valor |
|---|---|
| Inadimplentes | {c['inadimplentes']} |
| A vencer em 30 dias | {c['a_vencer_30_dias']} |
| Cancelamentos (30d) | {c['cancelamentos_30d']} |

## Alunos faltosos +7 dias
{linhas_faltosos}
## Inadimplentes
{linhas_inadimpl}
"""
        arquivo = EXPORT_DIR / f"dashboard_{datetime.now().strftime('%Y%m%d')}.md"
        with open(arquivo, "w", encoding="utf-8") as f:
            f.write(md)
        log.info(f"Dashboard MD: {arquivo}")
        return arquivo


# -- MÓDULO ADM / CRM (API Gateway) ------------------------------------------

SUPABASE_URL   = os.getenv("SUPABASE_URL", "https://bmnyhaxvlifmwkcuglfh.supabase.co")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
CRM_TENANT_ID  = os.getenv("CRM_TENANT_ID")

PACTO_GW_URL = "https://apigw.pactosolucoes.com.br"


class PactoADMClient:
    """
    Acesso ao módulo ADM/CRM via API Gateway (apigw.pactosolucoes.com.br).
    Autentica com PACTO_TOKEN (hex) -- header Authorization sem prefixo Bearer.
    """

    def __init__(self):
        self.base    = PACTO_GW_URL
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": PACTO_TOKEN,
            "chave":         PACTO_CHAVE,
            "empresaId":     "1",
            "Content-Type":  "application/json",
        })

    def _gw(self, method: str, path: str, **kwargs):
        r = self.session.request(method, f"{self.base}{path}", timeout=20, **kwargs)
        if r.status_code in (200, 201):
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}
        log.error(f"ADM GW {r.status_code} em {path}: {r.text[:200]}")
        return {"erro": r.status_code, "mensagem": r.text[:200]}

    def _epoch_ms(self, d: date) -> int:
        from datetime import timezone
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)

    def meta_crm(self, data_inicio: date, data_fim: date) -> list:
        """Retorna metas CRM por fase para o período informado."""
        r = self._gw("GET", "/meta-crm", params={
            "dataInicio": self._epoch_ms(data_inicio),
            "dataFim":    self._epoch_ms(data_fim),
        })
        return r.get("content", []) if isinstance(r, dict) else []

    def meta_crm_detalhada(self, codigos: list[int], page: int = 0, size: int = 100) -> dict:
        """Retorna clientes detalhados para os códigos de fechamento de meta."""
        if not codigos:
            return {"content": [], "totalElements": 0}
        params = [("codigosFecharMeta", ",".join(str(c) for c in codigos)),
                  ("page", page), ("size", size)]
        return self._gw("GET", "/meta-crm/detalhada", params=params)

    def visitantes_24h(self, d: date) -> dict:
        """
        Retorna visitantes cadastrados para contato na data informada.
        Combina meta-crm (fase HO) + meta-crm/detalhada para obter nomes e responsáveis.

        Retorna dict com:
          total      -- total_elements da fase HO
          visitantes -- lista com nome, matricula, situacao, dataMeta, consultora
        """
        fases = self.meta_crm(d, d + timedelta(days=1))
        ho_codes = []
        ho_meta = ho_realizado = 0
        for fase in fases:
            for tipo in fase.get("tiposMeta", []):
                if tipo.get("faseEnum") == "HO":
                    ho_codes      = tipo.get("codigosFecharMeta", [])
                    ho_meta       = tipo.get("totalMeta", 0)
                    ho_realizado  = tipo.get("totalMetaRealizada", 0)

        if not ho_codes:
            return {"total": 0, "meta": 0, "realizado": 0, "visitantes": []}

        detalhado = self.meta_crm_detalhada(ho_codes)
        visitantes = [
            {
                "nome":       v.get("nome"),
                "matricula":  v.get("matricula"),
                "situacao":   v.get("situacao"),
                "dataMeta":   v.get("dataMeta"),
                "consultora": v.get("nomeColaborador"),
                "telefone":   v.get("telefone"),
                "email":      v.get("email"),
            }
            for v in detalhado.get("content", [])
        ]
        return {
            "total":      detalhado.get("totalElements", len(visitantes)),
            "meta":       ho_meta,
            "realizado":  ho_realizado,
            "visitantes": visitantes,
        }

    def historico_contato(self, page: int = 0, size: int = 100) -> dict:
        """Histórico de contatos CRM (todas as entradas, sem filtro de data)."""
        return self._gw("GET", "/historico-contato", params={"page": page, "size": size})

    def meta_crm_abertura(self) -> dict:
        """
        Verifica se a meta CRM foi aberta hoje e se pode ser aberta.
        Campos: abriuMetaHoje (bool), metaPodeSerAberta (bool), motivo, mensagemBloqueio.
        """
        r = self._gw("GET", "/meta-crm/abertura")
        return r.get("content", r) if isinstance(r, dict) else r

    def fases_crm(self) -> list:
        """
        Fases do funil CRM configuradas.
        Campos por fase: name (enum), descricao, cor, ordem.
        Exemplos: AGENDAMENTO, NEGOCIACAO, MATRICULA, etc.
        """
        r = self._gw("GET", "/v1/configuracao/fases")
        return r.get("result", []) if isinstance(r, dict) else (r or [])

    def fases_crm_ai(self) -> list:
        """Fases do CRM com categorização por IA (NOVOS_LEADS, RETENCAO, etc.)."""
        r = self._gw("GET", "/v1/configuracao/fases-ai")
        return r.get("result", []) if isinstance(r, dict) else (r or [])

    def objecoes(self) -> list:
        """
        Objeções cadastradas para uso no CRM (motivos de perda de oportunidade).
        Campos: codigo, descricao, ativo.
        """
        r = self._gw("GET", "/v1/comum/objecao")
        content = r.get("content", r) if isinstance(r, dict) else r
        return content if isinstance(content, list) else []

    def meta_diaria(self, data_inicio: date, data_fim: date) -> list:
        """
        Meta diária do CRM por colaborador para o período.
        Parâmetros: dataInicio e dataFim no formato ISO 'yyyy-MM-dd'.
        Campos por entrada: colaborador, metaDiaria, realizadoDiario, etc.
        """
        r = self._gw("GET", "/meta-diaria", params={
            "dataInicio": data_inicio.isoformat(),
            "dataFim":    data_fim.isoformat(),
        })
        return r.get("content", []) if isinstance(r, dict) else (r or [])

    # -- ADM (ADMINISTRAÇÃO) --------------------------------------------------

    @staticmethod
    def _decrypt_aes(data: str) -> dict:
        """
        Decripta resposta AES-128 retornada pelos endpoints /adm/*.
        Tenta ECB primeiro, depois CBC com IV zero.
        Chave: PACTO_CHAVE decodificada de hex (16 bytes).
        Requer: pip install pycryptodome
        """
        import base64
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad
        except ImportError:
            log.warning(
                "pycryptodome nao instalado -- retornando dado encriptado bruto. "
                "Instale com: pip install pycryptodome"
            )
            return {"raw_encrypted": data}

        key = bytes.fromhex(PACTO_CHAVE)
        try:
            raw = base64.b64decode(data)
        except Exception:
            return {"raw_encrypted": data}

        for mode, kwargs in [
            (AES.MODE_ECB, {}),
            (AES.MODE_CBC, {"iv": b"\x00" * 16}),
        ]:
            try:
                cipher = AES.new(key, mode, **kwargs)
                dec = unpad(cipher.decrypt(raw), AES.block_size)
                return json.loads(dec.decode("utf-8"))
            except Exception:
                continue

        log.warning("Nao foi possivel decriptar a resposta AES -- retornando bruto")
        return {"raw_encrypted": data}

    def obter_usuario(self) -> dict:
        """
        Dados do usuario autenticado via /psec/validateToken (TreinoWeb, JSON em claro).
        O /adm/obter-usuario (API Gateway) retorna AES-encriptado com chave desconhecida;
        _decrypt_aes() esta disponivel para quando a chave for descoberta.
        """
        zw = PactoClient()
        return zw.obter_usuario()

    # -- FINANCEIRO -----------------------------------------------------------

    def _paginar(self, path: str, params: dict, max_pages: int = 50) -> list:
        """Pagina automaticamente um endpoint com content[] até esgotar."""
        todos, page = [], 0
        while page < max_pages:
            r = self._gw("GET", path, params={**params, "page": page, "size": 500})
            content = r.get("content", []) if isinstance(r, dict) else []
            if not content:
                break
            todos.extend(content)
            total_pages = r.get("totalPages")
            page += 1
            if total_pages is not None and page >= total_pages:
                break
        return todos

    # Mapeamento confirmado por varredura completa dos 3.918 lançamentos (2026-06-20):
    # 1=Despesa  3=Recebivel Debito  4=Transferencia  5=Estorno
    # 7=Conciliacao  8=Recebivel Cartao/Cheque  10=Retirada de lote
    # NOTA: tipo=2 (receita manual) não existe nesta academia —
    #       receitas de mensalidades vêm de /parcelas/{codPessoa}
    TIPOS_MOVCONTA = {
        1:  "Despesa",
        3:  "Recebivel Debito",
        4:  "Transferencia",
        5:  "Estorno",
        7:  "Conciliacao de Saldo",
        8:  "Recebivel Cartao/Cheque",
        10: "Retirada de Lote",
    }

    def movcontas(self, page: int = 0, size: int = 200) -> dict:
        """
        Lançamentos financeiros (contas a pagar e receber).
        Campos: codigo, descricao, pessoa, valor, dataLancamento,
                dataVencimento, dataCompetencia, tipoOperacao, nrParcela, conta.
        Ver TIPOS_MOVCONTA para o mapeamento completo de tipoOperacao.
        O filtro tipoOperacao via query param é ignorado pelo servidor — filtrar no cliente.
        """
        return self._gw("GET", "/v1/movconta", params={"page": page, "size": size})

    def receitas(self) -> list:
        """
        Recebíveis em movcontas: tipos 3 (débito) e 8 (cartão/cheque).
        Para receitas de mensalidades, use parcelas_cliente() com situacao=PG.
        """
        movs = self._paginar("/v1/movconta", {})
        return [m for m in movs if m.get("tipoOperacao") in (3, 8)]

    def despesas(self) -> list:
        """Lançamentos de despesa (tipoOperacao=1 — contas a pagar)."""
        movs = self._paginar("/v1/movconta", {})
        return [m for m in movs if m.get("tipoOperacao") == 1]

    def estornos(self) -> list:
        """Estornos registrados em movcontas (tipoOperacao=5)."""
        movs = self._paginar("/v1/movconta", {})
        return [m for m in movs if m.get("tipoOperacao") == 5]

    def planos(self, apenas_ativos: bool = False) -> list:
        """
        Planos de matrícula cadastrados.
        Campos: codigo, descricao, tipoPlano, vigenciaDe, vigenciaAte,
                ingressoAte, permitirAcessoSomenteNaEmpresaVende, ...
        """
        params = {"page": 0, "size": 500}
        if apenas_ativos:
            params["ativo"] = True
        return self._paginar("/planos", params)

    def contas(self) -> list:
        """Contas bancárias e caixas cadastrados."""
        return self._paginar("/v1/conta", {})

    def bancos(self) -> list:
        """Bancos cadastrados."""
        return self._paginar("/v1/banco", {})

    def centros_custo(self) -> list:
        """Centros de custo."""
        return self._paginar("/v1/centrocusto", {})

    def plano_contas(self) -> list:
        """Plano de contas (categorias financeiras)."""
        return self._paginar("/v1/planoconta", {})

    def meta_financeira(self) -> dict:
        """
        Meta financeira do período atual.
        Retorna faturamentoRecebido, faturamentoDevolvido, faturamentoLiquido,
        metas (lista por consultor/meta), produtosFaturamento.
        """
        r = self._gw("POST", "/v2-meta-financeira", json={})
        content = r.get("content", {}) if isinstance(r, dict) else {}
        jd = content.get("jsonDados", "{}")
        try:
            return json.loads(jd) if isinstance(jd, str) else jd
        except Exception:
            return content

    def resumo_financeiro(self) -> dict:
        """
        Resumo financeiro consolidado a partir de movcontas.
        Receitas aqui = recebíveis (tipos 3+8); despesas = tipo 1.
        Para receita de mensalidades, use parcelas_cliente() com situacao=PG.
        """
        log.info("Gerando resumo financeiro...")
        movs = self._paginar("/v1/movconta", {})

        receitas = [m for m in movs if m.get("tipoOperacao") in (3, 8)]
        despesas = [m for m in movs if m.get("tipoOperacao") == 1]
        estornos = [m for m in movs if m.get("tipoOperacao") == 5]

        total_receitas = sum(m.get("valor", 0) for m in receitas)
        total_despesas = sum(m.get("valor", 0) for m in despesas)
        total_estornos = sum(m.get("valor", 0) for m in estornos)

        por_tipo = {}
        for m in movs:
            t = m.get("tipoOperacao")
            por_tipo[t] = por_tipo.get(t, 0) + 1

        log.info(
            f"Financeiro: {len(movs)} lancamentos | "
            f"Recebiveis: R$ {total_receitas:,.2f} | Despesas: R$ {total_despesas:,.2f}"
        )

        return {
            "total_lancamentos": len(movs),
            "recebiveis_total":  round(total_receitas, 2),
            "despesas_total":    round(total_despesas, 2),
            "estornos_total":    round(total_estornos, 2),
            "saldo":             round(total_receitas - total_despesas, 2),
            "por_tipo":          por_tipo,
            "recebiveis":        receitas,
            "despesas":          despesas,
        }

    def grupo_risco(self) -> list:
        """
        Clientes em grupo de risco (potencial churn) gerado pelo BI.
        Campos: cliente (codigo), colaborador, codigo, empresa, nomeCliente.
        """
        r = self._gw("POST", "/v2-grupo-risco", json={})
        if not isinstance(r, dict) or r.get("erro"):
            return []
        content = r.get("content", {})
        jd = content.get("jsonDados", "{}")
        try:
            data = json.loads(jd) if isinstance(jd, str) else jd
            return data.get("listaClientes", [])
        except Exception:
            return []

    # -- CLIENTES ADM ---------------------------------------------------------

    def cliente(self, codigo_cliente: int) -> dict:
        """
        Retorna dados completos de um cliente pelo codigoCliente (ADM).
        Inclui: nome, CPF, telefones, endereço, email, foto, vínculos, avisos.
        O campo pessoa.codigo é o codPessoa usado em /parcelas/{codPessoa}.
        """
        r = self._gw("GET", f"/v1/cliente/{codigo_cliente}")
        return r.get("content", r) if isinstance(r, dict) else r

    def pessoa_de_cliente(self, codigo_cliente: int) -> int | None:
        """Retorna o codPessoa de um cliente (para uso em /parcelas/)."""
        c = self.cliente(codigo_cliente)
        return (c.get("pessoa") or {}).get("codigo")

    def parcelas_cliente(self, codigo_cliente: int, size: int = 100) -> list:
        """Parcelas de um cliente pelo codigoCliente (resolve codPessoa internamente)."""
        cod_pessoa = self.pessoa_de_cliente(codigo_cliente)
        if not cod_pessoa:
            return []
        r = self._gw("GET", f"/parcelas/{cod_pessoa}", params={"size": size})
        return r.get("content", []) if isinstance(r, dict) else []

    def faturamento_dia(self, d: date, max_clientes: int = 20000) -> dict:
        """
        Faturamento do dia somando parcelas pagas (situacao=PG) com dataPagamento = d.
        Itera /v1/cliente/{id} para obter codPessoa, depois /parcelas/{codPessoa}.
        max_clientes limita o range de codigoCliente varrido (padrão: 20000).
        """
        from datetime import timezone
        d_start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
        d_end   = int(datetime(d.year, d.month, d.day + 1, tzinfo=timezone.utc).timestamp() * 1000)

        total = 0.0
        pagamentos = []
        log.info(f"Varrendo faturamento de {d}...")

        for cod in range(1, max_clientes + 1):
            cod_pessoa = self.pessoa_de_cliente(cod)
            if not cod_pessoa:
                continue
            parcelas = self._gw("GET", f"/parcelas/{cod_pessoa}", params={"size": 200})
            for p in (parcelas.get("content", []) if isinstance(parcelas, dict) else []):
                dt_pag = p.get("dataPagamento") or 0
                if d_start <= dt_pag < d_end and p.get("situacao") == "PG":
                    val = float(p.get("valor", 0) or 0)
                    total += val
                    pagamentos.append({
                        "codigoCliente": cod,
                        "codPessoa":     cod_pessoa,
                        "descricao":     p.get("descricao"),
                        "valor":         round(val, 2),
                        "dataPagamento": datetime.fromtimestamp(dt_pag / 1000,
                                         tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    })

        return {
            "data":         d.isoformat(),
            "total":        round(total, 2),
            "n_pagamentos": len(pagamentos),
            "pagamentos":   pagamentos,
        }

    def reconstruir_peso(self, cod_aluno: int,
                         dias_avencer_config: int = 10,
                         data_ref: date = None) -> dict:
        """
        Reconstrói o peso de risco de churn de um cliente usando a fórmula oficial do Pacto.

        Fórmula: peso = peso_vencimento + peso_faltas + peso_presenca  (range 3-8)

          peso_vencimento — proximidade do vencimento do contrato:
            2 se vence em <= dias_avencer_config dias (config empresa, padrão 10)
            1 caso contrário

          peso_faltas — faltas sequenciais (dias corridos sem acesso físico via catraca):
            1 se ausente 0-1 dias
            2 se ausente 2-3 dias
            3 se ausente 4+ dias
            (threshold configurável na empresa)

          peso_presenca — presença média nas últimas 4 semanas (janelas de 7 dias corridos):
            1 se >= 4 dias/semana
            2 se == 3 dias/semana
            3 se <= 2 dias/semana

        Fonte de acessos: consultarQuantidadeAcessosClientesAgrupadosDia (acesso físico / catraca).
        NÃO usa linha-tempo TreinoWeb (TREINOU = home workout, não conta).

        Args:
            cod_aluno: codigoCliente (campo 'cliente' em grupo_risco())
            dias_avencer_config: threshold "Ativo a Vencer" configurado na empresa (padrão 10)
            data_ref: data de referência (padrão: hoje)

        Returns:
            dict com peso_total, componentes individuais, vencimento, presença média e detalhes.
        """
        zw = PactoClient()
        ctx = zw.ctx

        hoje = data_ref or date.today()

        def ts(d: date) -> int:
            return int(datetime(d.year, d.month, d.day).timestamp() * 1000)

        def acessos_janela(ini: date, fim: date) -> int:
            r = zw._req("GET",
                f"/cliente/{ctx}/app/consultarQuantidadeAcessosClientesAgrupadosDia",
                params={"codigoAluno": cod_aluno, "username": "",
                        "dataInicial": ts(ini), "dataFinal": ts(fim)})
            if isinstance(r, dict) and "sucesso" in r:
                return sum(r["sucesso"].values())
            return 0

        # ── 1. peso_presenca: média nas últimas 4 semanas ──────────────
        total_4sem = acessos_janela(hoje - timedelta(weeks=4), hoje)
        media_semanal = total_4sem / 4.0
        if media_semanal >= 4:
            peso_presenca = 1
        elif media_semanal >= 3:
            peso_presenca = 2
        else:
            peso_presenca = 3

        # ── 2. peso_faltas: dias corridos sem acesso físico ────────────
        # Aproximação: verifica janelas curtas para determinar a faixa
        if acessos_janela(hoje - timedelta(days=2), hoje) > 0:
            peso_faltas = 1
            dias_ausente_aprox = "0-1"
        elif acessos_janela(hoje - timedelta(days=4), hoje) > 0:
            peso_faltas = 2
            dias_ausente_aprox = "2-3"
        else:
            peso_faltas = 3
            # estima há quantos dias não vai
            dias_ausente_aprox = "4+"
            for semanas in range(1, 27):
                ini = hoje - timedelta(weeks=semanas + 1)
                fim = hoje - timedelta(weeks=semanas)
                if acessos_janela(ini, fim) > 0:
                    dias_ausente_aprox = f"~{semanas * 7}d"
                    break

        # ── 3. peso_vencimento: próximo do vencimento? ─────────────────
        # Fonte primária: clienteSintetico.situacaocontrato.codigo == "AV" (ADM)
        # Fonte secundária: situacaoContratoZW == "A_VENCER" + contratoZW.vencimento (TreinoWeb)
        peso_vencimento = 1
        vencimento_str = "desconhecido"
        dias_para_vencer = None
        try:
            cli = self.cliente(cod_aluno)
            mat_zw = cli.get("matricula") if isinstance(cli, dict) else None

            # método 1: situacaocontrato no clienteSintetico (ADM)
            cs = (cli.get("clienteSintetico") or {}) if isinstance(cli, dict) else {}
            sit = (cs.get("situacaocontrato") or {})
            if (sit.get("codigo") == "AV") or (sit.get("descricao", "").lower() == "a vencer"):
                peso_vencimento = 2
                vencimento_str = "A Vencer (ADM)"

            # método 2: contratoZW.vencimento via TreinoWeb (mais preciso)
            if mat_zw:
                r_zw = zw._req("GET",
                    f"/psec/alunos/obter-aluno-completo-por-matricula/{mat_zw}")
                content = (r_zw.get("content", r_zw) if isinstance(r_zw, dict) else r_zw) or {}
                sit_zw = content.get("situacaoContratoZW", "")
                contrato_zw = content.get("contratoZW") or {}
                venc_ts = contrato_zw.get("vencimento")
                if venc_ts:
                    venc_date = datetime.fromtimestamp(venc_ts / 1000).date()
                    dias_para_vencer = (venc_date - hoje).days
                    vencimento_str = venc_date.strftime("%d/%m/%Y")
                    if 0 <= dias_para_vencer <= dias_avencer_config:
                        peso_vencimento = 2
                elif sit_zw in ("A_VENCER",):
                    peso_vencimento = 2
                    vencimento_str = "A Vencer (ZW)"
        except Exception as e:
            vencimento_str = f"erro: {e}"

        peso_total = peso_vencimento + peso_faltas + peso_presenca
        # garante range 3-8
        peso_total = max(3, min(8, peso_total))

        detalhes = (f"venc={peso_vencimento}({vencimento_str}"
                    f"{f', {dias_para_vencer}d' if dias_para_vencer is not None else ''})"
                    f" + faltas={peso_faltas}({dias_ausente_aprox})"
                    f" + pres={peso_presenca}({media_semanal:.1f}/sem)")

        return {
            "cod_aluno":           cod_aluno,
            "peso_calculado":      peso_total,
            "peso_vencimento":     peso_vencimento,
            "peso_faltas":         peso_faltas,
            "peso_presenca":       peso_presenca,
            "vencimento":          vencimento_str,
            "dias_para_vencer":    dias_para_vencer,
            "media_presenca_4sem": round(media_semanal, 2),
            "dias_ausente_aprox":  dias_ausente_aprox,
            "detalhes":            detalhes,
        }


# -- CRM SUPABASE (crm.territoriofit.com.br) ----------------------------------

# Namespace fixo para gerar UUIDs determinísticos a partir do código Pacto.
# Garante que o mesmo aluno sempre mapeia para o mesmo id no CRM, permitindo
# upsert seguro por primary key sem precisar de constraints extras.
_PACTO_NS = uuid.UUID("d7e8a9f0-cafe-5678-abcd-ef0123456789")


def _pacto_lead_id(pacto_codigo: int, tenant_id: str) -> str:
    return str(uuid.uuid5(_PACTO_NS, f"{tenant_id}:{pacto_codigo}"))


class CRMClient:
    """
    Integração Pacto → CRM Território Fit (Supabase).
    Requer: pip install supabase
    """

    def __init__(self):
        try:
            from supabase import create_client
        except ImportError:
            raise RuntimeError(
                "Pacote 'supabase' não instalado. Execute: pip install supabase"
            )
        if not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_KEY não definida no .env.integracao")
        self.sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        self.tenant_id = CRM_TENANT_ID or self._bootstrap_tenant()

    # -- Setup -----------------------------------------------------------------

    def _bootstrap_tenant(self) -> str:
        """Cria ou recupera o tenant 'territorio-fit'. Salva o id no .env."""
        r = self.sb.table("tenants").select("id").eq("slug", "territorio-fit").execute()
        if r.data:
            tid = r.data[0]["id"]
        else:
            r = self.sb.table("tenants").insert({
                "name": "Território Fit",
                "slug": "territorio-fit",
                "is_active": True,
            }).execute()
            tid = r.data[0]["id"]
            log.info(f"Tenant criado: {tid}")

        # persiste no .env para não recriar nas próximas execuções
        env_path = Path(__file__).parent / ".env.integracao"
        content = env_path.read_text(encoding="utf-8")
        content = content.replace("CRM_TENANT_ID=", f"CRM_TENANT_ID={tid}")
        env_path.write_text(content, encoding="utf-8")
        log.info(f"Tenant: {tid}")
        return tid

    # -- Helpers ---------------------------------------------------------------

    @staticmethod
    def _tel(fones) -> str | None:
        if not fones:
            return None
        raw = fones[0].get("numero", "") if isinstance(fones[0], dict) else str(fones[0])
        digits = "".join(c for c in raw if c.isdigit())
        return digits or None

    @staticmethod
    def _email_addr(emails) -> str | None:
        if not emails:
            return None
        e = emails[0]
        return (e.get("email") if isinstance(e, dict) else str(e)) or None

    def _lead_row(self, pacto_codigo: int, aluno: dict, source: str = "pacto_aluno") -> dict:
        """Monta um dict pronto para upsert na tabela leads."""
        contrato = aluno.get("contratoZW") or {}
        venc_ts  = contrato.get("vencimento")
        venc_str = _ts_to_date(venc_ts).strftime("%Y-%m-%d") if venc_ts else None

        return {
            "id":        _pacto_lead_id(pacto_codigo, self.tenant_id),
            "name":      aluno.get("nome") or aluno.get("name"),
            "phone":     self._tel(aluno.get("fones") or []),
            "email":     self._email_addr(aluno.get("emails") or []),
            "source":    source,
            "status":    "cliente" if aluno.get("situacaoAluno") == "ATIVO" else "inativo",
            "tenant_id": self.tenant_id,
            "metadata":  {
                "pacto_codigo":    pacto_codigo,
                "pacto_matricula": aluno.get("matriculaZW") or aluno.get("id"),
                "pacto_plano":     (aluno.get("planoZW") or {}).get("descricao"),
                "pacto_situacao":  aluno.get("situacaoAluno"),
                "vencimento":      venc_str,
                "tipo_contrato":   contrato.get("tipo"),
                "synced_at":       datetime.now().isoformat(),
            },
        }

    def _upsert_batch(self, rows: list[dict]) -> int:
        """Upsert em lote de no máximo 200 linhas por chamada."""
        if not rows:
            return 0
        total = 0
        BATCH = 200
        for i in range(0, len(rows), BATCH):
            r = self.sb.table("leads").upsert(rows[i:i + BATCH]).execute()
            total += len(r.data or [])
        return total

    # -- Syncs -----------------------------------------------------------------

    def sync_alunos_ativos(self, pacto: "PactoClient") -> int:
        """Upsert de todos os alunos ativos do Pacto como leads/clientes no CRM."""
        log.info("CRM sync: alunos ativos...")
        alunos = pacto.alunos_ativos()
        rows = []
        for a in alunos:
            cod = a.get("codigoCliente")
            if cod:
                rows.append(self._lead_row(cod, a, source="pacto_aluno"))
        n = self._upsert_batch(rows)
        log.info(f"sync_alunos_ativos: {n}/{len(alunos)} ok")
        return n

    def sync_alunos_inativos(self, pacto: "PactoClient") -> int:
        """Upsert de todos os alunos inativos (ex-alunos) do Pacto como leads no CRM."""
        log.info("CRM sync: alunos inativos...")
        alunos = pacto.alunos_inativos()
        rows = []
        for a in alunos:
            cod = a.get("codigoCliente")
            if cod:
                rows.append(self._lead_row(cod, a, source="pacto_aluno"))
        n = self._upsert_batch(rows)
        log.info(f"sync_alunos_inativos: {n}/{len(alunos)} ok")
        return n

    def _stage_id_by_description(self, description: str, pipeline_id: str | None = None) -> str | None:
        """Busca o id da fase (sales_pipeline_stages) pelo slug (description)."""
        pipeline_id = pipeline_id or self._ensure_pipeline()
        r = self.sb.table("sales_pipeline_stages").select("id").eq(
            "pipeline_id", pipeline_id
        ).eq("description", description).limit(1).execute()
        return r.data[0]["id"] if r.data else None

    def sync_visitantes(self, adm: "PactoADMClient", data: date = None) -> int:
        """Upsert de visitantes 24h do Pacto como leads no CRM."""
        data = data or date.today()
        log.info(f"CRM sync: visitantes {data}...")
        stage_id = self._stage_id_by_description("VINTE_QUATRO_HORAS")
        r = adm.visitantes_24h(data)
        rows = []
        for v in r.get("visitantes", []):
            mat = v.get("matricula")
            if not mat:
                continue
            tel = "".join(c for c in (v.get("telefone") or "") if c.isdigit()) or None
            rows.append({
                "id":                _pacto_lead_id(mat, self.tenant_id),
                "name":              v.get("nome"),
                "phone":             tel,
                "email":             v.get("email"),
                "source":            "pacto_visitante",
                "status":            "lead",
                "sales_stage":       v.get("situacao"),
                "pipeline_stage_id": stage_id,
                "tenant_id":         self.tenant_id,
                "metadata": {
                    "pacto_codigo":     mat,
                    "pacto_situacao":   v.get("situacao"),
                    "pacto_consultora": v.get("consultora"),
                    "pacto_data_meta":  str(v.get("dataMeta", "")),
                    "synced_at":        datetime.now().isoformat(),
                },
            })
        n = self._upsert_batch(rows)
        log.info(f"sync_visitantes: {n}/{len(rows)} ok")
        return n

    def sync_grupo_risco(self, adm: "PactoADMClient") -> int:
        """Atualiza campo metadata.grupo_risco nos leads com dados de churn do Pacto."""
        log.info("CRM sync: grupo de risco...")
        clientes = adm.grupo_risco()
        if not clientes:
            log.info("Grupo de risco vazio.")
            return 0

        # busca leads em lotes para evitar URL too long (limite ~8KB)
        BATCH = 80
        existentes: dict = {}  # lid -> {metadata, name, status}
        pares = [(c["cliente"], _pacto_lead_id(c["cliente"], self.tenant_id))
                 for c in clientes if c.get("cliente")]
        ids = [lid for _, lid in pares]
        for i in range(0, len(ids), BATCH):
            r = self.sb.table("leads").select("id,name,status,metadata").in_(
                "id", ids[i:i+BATCH]
            ).execute()
            for row in (r.data or []):
                existentes[row["id"]] = row

        rows = []
        for c in clientes:
            cod = c.get("cliente")
            if not cod:
                continue
            lid = _pacto_lead_id(cod, self.tenant_id)
            if lid not in existentes:
                continue  # lead ainda não sincronizado; sync_alunos_ativos o criará
            lead = existentes[lid]
            meta = dict(lead.get("metadata") or {})
            meta["grupo_risco"] = {
                "peso":        c.get("peso"),
                "colaborador": c.get("colaborador"),
                "synced_at":   datetime.now().isoformat(),
            }
            rows.append({
                "id":        lid,
                "name":      lead.get("name"),
                "status":    lead.get("status"),
                "metadata":  meta,
                "tenant_id": self.tenant_id,
            })

        n = self._upsert_batch(rows)
        log.info(f"sync_grupo_risco: {n}/{len(clientes)} ok ({len(clientes)-n} nao encontrados no CRM)")
        return n

    def sync_ultimo_acesso(self, pacto: "PactoClient") -> int:
        """
        Atualiza metadata.ultimo_acesso (data do ultimo acesso fisico na catraca)
        de todos os alunos ativos. Fonte: linha-tempo por aluno (evento 'Chegou'),
        1 request por aluno (~0.4s cada; ~15min na base de ~2000 ativos).
        Quem nunca acessou fica com ultimo_acesso=null (frontend mostra 'sem acesso').
        """
        log.info("CRM sync: ultimo acesso catraca...")
        alunos = pacto.alunos_ativos()

        # busca metadata atual dos leads em lotes (merge, padrao sync_grupo_risco)
        BATCH = 80
        existentes: dict = {}
        pares = [(a, _pacto_lead_id(a["codigoCliente"], self.tenant_id))
                 for a in alunos if a.get("codigoCliente")]
        ids = [lid for _, lid in pares]
        for i in range(0, len(ids), BATCH):
            r = self.sb.table("leads").select("id,name,status,metadata").in_(
                "id", ids[i:i+BATCH]
            ).execute()
            for row in (r.data or []):
                existentes[row["id"]] = row

        rows, erros = [], 0
        for a, lid in pares:
            if lid not in existentes:
                continue  # lead ainda nao sincronizado; sync_alunos_ativos o criara
            mat = a.get("matriculaZW") or a.get("id")
            if not mat:
                continue
            ultimo, falhou = None, False
            for tentativa in (1, 2):
                try:
                    ultimo = pacto.ultimo_acesso_catraca(mat)
                    falhou = False
                    break
                except Exception as e:
                    falhou = True
                    if tentativa == 2:
                        erros += 1
                        log.warning(f"ultimo_acesso mat={mat}: {e}")
            if falhou:
                continue  # nao sobrescreve dado anterior com falha de rede
            lead = existentes[lid]
            meta = dict(lead.get("metadata") or {})
            meta["ultimo_acesso"] = ultimo.strftime("%Y-%m-%d") if ultimo else None
            meta["ultimo_acesso_synced_at"] = datetime.now().isoformat()
            rows.append({
                "id":        lid,
                "name":      lead.get("name"),
                "status":    lead.get("status"),
                "metadata":  meta,
                "tenant_id": self.tenant_id,
            })

        n = self._upsert_batch(rows)
        log.info(f"sync_ultimo_acesso: {n}/{len(pares)} ok ({erros} falhas de consulta)")
        return n

    def sync_aniversariantes(self, pacto: "PactoClient") -> int:
        """
        Marca metadata.aniversariante_mes_atual (+ data_nascimento) nos alunos
        ATIVOS e INATIVOS que fazem aniversario no mes corrente e desmarca quem
        saiu do mes. Fonte: dataNascimento da listagem /psec/alunos.
        Para aniversariante sem ultimo_acesso no metadata (inativos ficam fora
        do sync_ultimo_acesso) busca tambem o ultimo acesso na catraca.
        Alimenta a coluna Aniversariantes do Kanban de Alunos.
        """
        log.info("CRM sync: aniversariantes do mes (ativos + inativos)...")
        alunos = pacto.todos_alunos()
        mes = date.today().month

        aniversariantes = []
        for a in alunos:
            if a.get("situacaoAluno") not in ("ATIVO", "INATIVO"):
                continue
            cod = a.get("codigoCliente")
            nasc = _ts_to_date(a.get("dataNascimento")) if a.get("dataNascimento") else None
            if cod and nasc and nasc.month == mes:
                aniversariantes.append((a, nasc, _pacto_lead_id(cod, self.tenant_id)))
        ids_novos = {lid for _, _, lid in aniversariantes}

        rows = []

        # desmarca quem esta com a flag mas nao faz aniversario neste mes
        r = self.sb.table("leads").select("id,name,status,metadata").eq(
            "source", "pacto_aluno"
        ).eq("metadata->>aniversariante_mes_atual", "true").limit(5000).execute()
        for row in (r.data or []):
            if row["id"] in ids_novos:
                continue
            meta = dict(row.get("metadata") or {})
            meta["aniversariante_mes_atual"] = False
            rows.append({
                "id":        row["id"],
                "name":      row.get("name"),
                "status":    row.get("status"),
                "metadata":  meta,
                "tenant_id": self.tenant_id,
            })

        # metadata atual dos aniversariantes em lotes (merge, padrao sync_grupo_risco)
        BATCH = 80
        existentes: dict = {}
        ids = [lid for _, _, lid in aniversariantes]
        for i in range(0, len(ids), BATCH):
            r = self.sb.table("leads").select("id,name,status,metadata").in_(
                "id", ids[i:i+BATCH]
            ).execute()
            for row in (r.data or []):
                existentes[row["id"]] = row

        buscou_acesso = 0
        for a, nasc, lid in aniversariantes:
            lead = existentes.get(lid)
            if lead is None:
                # ex-aluno ainda nao sincronizado como lead: cria a linha completa
                row = self._lead_row(a["codigoCliente"], a)
                meta = row["metadata"]
            else:
                meta = dict(lead.get("metadata") or {})
                row = {
                    "id":        lid,
                    "name":      lead.get("name"),
                    "status":    lead.get("status"),
                    "metadata":  meta,
                    "tenant_id": self.tenant_id,
                }
            meta["aniversariante_mes_atual"] = True
            meta["data_nascimento"] = nasc.strftime("%Y-%m-%d")
            # ultimo acesso na catraca p/ quem nunca recebeu (1 request por aluno)
            if "ultimo_acesso_synced_at" not in meta:
                mat = a.get("matriculaZW") or a.get("id")
                if mat:
                    try:
                        ua = pacto.ultimo_acesso_catraca(mat)
                        meta["ultimo_acesso"] = ua.strftime("%Y-%m-%d") if ua else None
                        meta["ultimo_acesso_synced_at"] = datetime.now().isoformat()
                        buscou_acesso += 1
                    except Exception as e:
                        log.warning(f"ultimo_acesso aniversariante mat={mat}: {e}")
            rows.append(row)

        n = self._upsert_batch(rows)
        log.info(f"sync_aniversariantes: {len(aniversariantes)} aniversariantes no mes "
                 f"(ativos+inativos), {n} leads atualizados, {buscou_acesso} acessos buscados")
        return n

    def sync_contratos_a_vencer(self, pacto: "PactoClient", dias: int = 30) -> int:
        """Upsert de clientes com contrato a vencer, marcando alerta no metadata."""
        log.info(f"CRM sync: contratos a vencer ({dias}d)...")
        a_vencer = pacto.contratos_a_vencer(dias=dias)
        rows = []
        for a in a_vencer:
            cod = a.get("codigoCliente")
            if not cod:
                continue
            row = self._lead_row(cod, a, source="pacto_aluno")
            row["metadata"]["alerta"]          = "contrato_a_vencer"
            row["metadata"]["dias_restantes"]  = a.get("dias_restantes")
            rows.append(row)
        n = self._upsert_batch(rows)
        log.info(f"sync_contratos_a_vencer: {n}/{len(a_vencer)} ok")
        return n

    @staticmethod
    def _month_bounds(ref: date | None = None) -> tuple[datetime, datetime]:
        """Retorna (inicio, fim) do mes calendario de `ref` (padrao: hoje)."""
        ref = ref or date.today()
        inicio = datetime(ref.year, ref.month, 1)
        if ref.month == 12:
            fim = datetime(ref.year + 1, 1, 1) - timedelta(seconds=1)
        else:
            fim = datetime(ref.year, ref.month + 1, 1) - timedelta(seconds=1)
        return inicio, fim

    def sync_renovacao_mes_atual(self, pacto: "PactoClient") -> int:
        """
        Marca na etapa 'Renovacao' os alunos ATIVOS cujo contrato vence dentro
        do mes calendario atual. Quem sai da janela do mes e removido da etapa.
        """
        log.info("CRM sync: renovacao (vencimento no mes atual)...")
        stage_id = self._stage_id_by_description("RENOVACAO")
        if not stage_id:
            log.warning("Etapa RENOVACAO nao encontrada no pipeline.")
            return 0

        inicio, fim = self._month_bounds()
        alunos = pacto.alunos_ativos()
        rows, ids_no_mes = [], []
        for a in alunos:
            cod = a.get("codigoCliente")
            if not cod:
                continue
            ts   = (a.get("contratoZW") or {}).get("vencimento")
            venc = _ts_to_date(ts)
            if venc and inicio <= venc <= fim:
                row = self._lead_row(cod, a, source="pacto_aluno")
                row["pipeline_stage_id"] = stage_id
                row["metadata"]["vencimento_mes_atual"] = venc.strftime("%Y-%m-%d")
                rows.append(row)
                ids_no_mes.append(row["id"])

        n = self._upsert_batch(rows)

        # Tira da etapa quem nao vence mais neste mes (evita ficar preso pra sempre)
        query = self.sb.table("leads").update({"pipeline_stage_id": None}).eq(
            "pipeline_stage_id", stage_id
        ).eq("source", "pacto_aluno")
        if ids_no_mes:
            query = query.not_.in_("id", ids_no_mes)
        query.execute()

        log.info(f"sync_renovacao_mes_atual: {n}/{len(rows)} vencendo em {inicio:%m/%Y}")
        return n

    def sync_inadimplentes(self, pacto: "PactoClient", adm: "PactoADMClient") -> int:
        """
        Upsert de inadimplentes (contrato vencido) com alerta no metadata +
        dados da parcela atrasada mais antiga (codigo, vencimento, numero de
        tentativas de cobranca), buscados via /parcelas/{codigoPessoa}.

        NOTA (2026-07-02): contrato vencido != parcela em atraso no Pacto.
        Quem tem parcela realmente atrasada (situacao EA + vencimento no
        passado) e coberto por sync_parcelas_atrasadas(), que escaneia o
        grupo de risco (peso >= 5) + inadimplentes aluno a aluno.
        """
        log.info("CRM sync: inadimplentes...")
        inadimpl = pacto.inadimplentes()
        rows = []
        for a in inadimpl:
            cod = a.get("codigoCliente")
            if not cod:
                continue
            row = self._lead_row(cod, a, source="pacto_aluno")
            row["status"]                    = "inadimplente"
            row["metadata"]["alerta"]        = "inadimplente"
            row["metadata"]["dias_atraso"]   = a.get("dias_atraso")

            # Parcela em aberto e vencida (situacao=EA "Em aberto" + dataVencimento no
            # passado) mais antiga: codigo, vencimento, tentativas de cobranca.
            # IMPORTANTE:
            #  - O codigo correto e "EA", nao "AB" (confirmado via situacaoDescricao
            #    == "Em aberto" em dados reais).
            #  - Usa codigoPessoa do proprio TreinoWeb -- pessoa_de_cliente()/ADM
            #    retorna sempre codigo=1 (registro generico da empresa, nao da
            #    pessoa) neste token, entao NAO usar adm.parcelas_cliente()/
            #    pessoa_de_cliente() aqui.
            #  - "EA" sozinho inclui parcelas futuras do plano de pagamento (ainda
            #    nao venceram) -- filtra por dataVencimento < hoje pra pegar só
            #    quem esta de fato atrasado.
            cod_pessoa = a.get("codigoPessoa")
            try:
                if not cod_pessoa:
                    raise ValueError("sem codigoPessoa")
                hoje_ms = int(datetime.now().timestamp() * 1000)
                parcelas = adm._gw("GET", f"/parcelas/{cod_pessoa}", params={"size": 100})
                parcelas = parcelas.get("content", []) if isinstance(parcelas, dict) else []
                atrasadas = [
                    p for p in parcelas
                    if p.get("situacao") == "EA" and (p.get("dataVencimento") or 0) < hoje_ms
                ]
                atrasadas.sort(key=lambda p: p.get("dataVencimento") or 0)
                if atrasadas:
                    p = atrasadas[0]
                    venc_ts = p.get("dataVencimento")
                    venc_date = _ts_to_date(venc_ts) if venc_ts else None
                    row["metadata"]["parcela_atrasada"] = {
                        "codigo":               p.get("codigo"),
                        "vencimento":           venc_date.strftime("%Y-%m-%d") if venc_date else None,
                        "nr_tentativas":        p.get("nrTentativas"),
                        "valor":                p.get("valor"),
                        "total_parcelas_abertas": len(atrasadas),
                    }
            except Exception as e:
                log.warning(f"parcelas em aberto falharam pro cliente {cod}: {e}")

            rows.append(row)
        n = self._upsert_batch(rows)
        log.info(f"sync_inadimplentes: {n}/{len(inadimpl)} ok")
        return n

    # -- Parcelas atrasadas (situacao EA + vencimento no passado) -------------

    _PARCELA_NS = uuid.UUID("c0b4a5d6-cafe-5678-abcd-9876543210ab")

    @staticmethod
    def _motivo_falha_parcela(p: dict) -> str:
        """
        Motivo (derivado) pelo qual a parcela nao foi cobrada.

        A API do Pacto NAO expoe o retorno da operadora (investigado em
        2026-07-02): o objeto parcela so traz nrTentativas; os BIs
        /v2-pendencias e /v2-inadimplencia retornam 500 neste token; e o
        gateway nao roteia nenhum endpoint de historico de cobranca
        (/cobranca/*, /transacoes, /parcelas/{id}/tentativas -- todos 404).
        Derivamos o melhor motivo possivel do que existe na parcela.
        """
        nt = p.get("nrTentativas") or 0
        desc = (p.get("descricao") or "").upper()
        if nt > 0:
            plural = "s" if nt != 1 else ""
            return f"Cobranca automatica sem sucesso ({nt} tentativa{plural})"
        if "RENEGOCIA" in desc:
            return "Parcela renegociada nao paga"
        return "Sem tentativa de cobranca registrada (boleto/pix/manual nao pago)"

    def scan_parcelas_atrasadas(self, pacto: "PactoClient", adm: "PactoADMClient",
                                peso_minimo: int = 5,
                                todos_ativos: bool = False) -> list[dict]:
        """
        Escaneia aluno a aluno as parcelas EA ("Em aberto") com dataVencimento
        no passado -- as que o sistema tentou cobrar e nao conseguiu.

        Base do scan (nao existe endpoint em lote; /v1/movconta ignora filtros
        server-side, entao o filtro e todo client-side):
          - padrao: grupo de risco com peso >= peso_minimo (~724 com peso>=5)
            + inadimplentes de contrato vencido (~46) -- uniao ~740 alunos.
          - todos_ativos=True: escaneia a base ativa inteira (~2000 alunos,
            ~2x mais lento; preparado pra rodar de madrugada).

        Retorna [{"aluno": <aluno TreinoWeb>, "parcelas": [parcela EA vencida...]}]
        com as parcelas ordenadas da mais antiga pra mais recente.
        """
        ativos  = pacto.alunos_ativos()
        por_cod = {a.get("codigoCliente"): a for a in ativos if a.get("codigoCliente")}
        hoje    = datetime.now()
        hoje_ms = int(hoje.timestamp() * 1000)

        # inadimplentes (contrato vencido) calculados localmente pra nao
        # refazer o fetch de alunos_ativos()
        inad_cods = set()
        for cod, a in por_cod.items():
            venc = _ts_to_date((a.get("contratoZW") or {}).get("vencimento"))
            if venc and venc < hoje:
                inad_cods.add(cod)

        if todos_ativos:
            alvo = set(por_cod)
        else:
            risco = adm.grupo_risco()
            alvo  = {c["cliente"] for c in risco
                     if c.get("cliente") and (c.get("peso") or 0) >= peso_minimo}
            alvo |= inad_cods
            alvo &= set(por_cod)

        log.info(f"scan_parcelas_atrasadas: escaneando {len(alvo)} alunos "
                 f"({'todos ativos' if todos_ativos else f'risco peso>={peso_minimo} + inadimplentes'})...")

        achados = []
        for i, cod in enumerate(sorted(alvo)):
            aluno      = por_cod[cod]
            cod_pessoa = aluno.get("codigoPessoa")
            if not cod_pessoa:
                continue
            # 1 retry: na base completa ~3/2000 clientes dao read timeout (20s)
            for tentativa in (1, 2):
                try:
                    r = adm._gw("GET", f"/parcelas/{cod_pessoa}", params={"size": 100})
                    parcelas = r.get("content", []) if isinstance(r, dict) else []
                    atrasadas = [
                        p for p in parcelas
                        if p.get("situacao") == "EA" and (p.get("dataVencimento") or 0) < hoje_ms
                    ]
                    if atrasadas:
                        atrasadas.sort(key=lambda p: p.get("dataVencimento") or 0)
                        achados.append({"aluno": aluno, "parcelas": atrasadas})
                    break
                except Exception as e:
                    if tentativa == 2:
                        log.warning(f"scan parcelas: erro no cliente {cod}: {e}")
            if (i + 1) % 100 == 0:
                log.info(f"scan parcelas: {i + 1}/{len(alvo)} alunos, "
                         f"{len(achados)} com parcela atrasada")

        total_parcelas = sum(len(x["parcelas"]) for x in achados)
        log.info(f"scan_parcelas_atrasadas: {len(achados)} alunos com "
                 f"{total_parcelas} parcelas atrasadas (de {len(alvo)} escaneados)")
        return achados

    def sync_parcelas_atrasadas(self, pacto: "PactoClient", adm: "PactoADMClient",
                                peso_minimo: int = 5,
                                todos_ativos: bool = False) -> dict:
        """
        Scan + gravacao das parcelas atrasadas no Supabase:
          1. tabela parcelas_atrasadas: 1 linha por parcela (nome, matricula,
             codigo da parcela, vencimento, motivo da falha, tentativas);
             parcelas que sairam do atraso (pagas/renegociadas) sao removidas.
          2. leads: metadata.parcela_atrasada + alerta/status inadimplente
             (merge com metadata existente pra nao perder grupo_risco etc);
             quem nao tem mais parcela atrasada tem a flag limpa.
        """
        achados    = self.scan_parcelas_atrasadas(pacto, adm, peso_minimo, todos_ativos)
        agora      = datetime.now()
        scan_start = agora.isoformat()

        # -- 1) tabela parcelas_atrasadas -----------------------------------
        rows_parc = []
        por_lead  = {}  # lead_id -> (aluno, parcelas)
        for item in achados:
            a    = item["aluno"]
            cod  = a.get("codigoCliente")
            lid  = _pacto_lead_id(cod, self.tenant_id)
            por_lead[lid] = item
            for p in item["parcelas"]:
                venc = _ts_to_date(p.get("dataVencimento"))
                rows_parc.append({
                    "id":             str(uuid.uuid5(self._PARCELA_NS, f"{self.tenant_id}:{p.get('codigo')}")),
                    "tenant_id":      self.tenant_id,
                    "lead_id":        lid,
                    "nome_aluno":     a.get("nome"),
                    "matricula":      str(a.get("matriculaZW") or a.get("id") or ""),
                    "codigo_cliente": cod,
                    "codigo_pessoa":  a.get("codigoPessoa"),
                    "parcela_codigo": p.get("codigo"),
                    "contrato":       p.get("contrato"),
                    "descricao":      p.get("descricao"),
                    "valor":          p.get("valor"),
                    "data_vencimento": venc.strftime("%Y-%m-%d") if venc else None,
                    "dias_atraso":    (agora - venc).days if venc else None,
                    "nr_tentativas":  p.get("nrTentativas") or 0,
                    "motivo_falha":   self._motivo_falha_parcela(p),
                    "synced_at":      scan_start,
                })
        for i in range(0, len(rows_parc), 200):
            self.sb.table("parcelas_atrasadas").upsert(rows_parc[i:i + 200]).execute()
        # remove parcelas que nao apareceram neste scan (pagas/renegociadas)
        self.sb.table("parcelas_atrasadas").delete().eq(
            "tenant_id", self.tenant_id
        ).lt("synced_at", scan_start).execute()

        # -- 2) leads: flag + resumo no metadata (merge, padrao sync_grupo_risco)
        BATCH = 80
        ids = list(por_lead.keys())
        existentes: dict = {}
        for i in range(0, len(ids), BATCH):
            r = self.sb.table("leads").select("id,name,status,metadata").in_(
                "id", ids[i:i + BATCH]
            ).execute()
            for row in (r.data or []):
                existentes[row["id"]] = row

        rows_leads = []
        for lid, item in por_lead.items():
            if lid not in existentes:
                continue  # lead ainda nao sincronizado; sync_alunos_ativos o criara
            a, parcelas = item["aluno"], item["parcelas"]
            p    = parcelas[0]  # mais antiga
            venc = _ts_to_date(p.get("dataVencimento"))
            lead = existentes[lid]
            meta = dict(lead.get("metadata") or {})
            meta["alerta"] = "inadimplente"
            meta["parcela_atrasada"] = {
                "codigo":                 p.get("codigo"),
                "vencimento":             venc.strftime("%Y-%m-%d") if venc else None,
                "dias_atraso":            (agora - venc).days if venc else None,
                "nr_tentativas":          p.get("nrTentativas") or 0,
                "valor":                  p.get("valor"),
                "motivo_falha":           self._motivo_falha_parcela(p),
                "matricula":              str(a.get("matriculaZW") or a.get("id") or ""),
                "total_parcelas_abertas": len(parcelas),
            }
            rows_leads.append({
                "id":        lid,
                "name":      lead.get("name"),
                "status":    "inadimplente",
                "metadata":  meta,
                "tenant_id": self.tenant_id,
            })
        n_leads = self._upsert_batch(rows_leads)

        # -- 3) limpa a flag de quem regularizou -----------------------------
        r = self.sb.table("leads").select("id,name,status,metadata").eq(
            "tenant_id", self.tenant_id
        ).eq("source", "pacto_aluno").not_.is_(
            "metadata->parcela_atrasada", "null"
        ).execute()
        limpos = []
        for lead in (r.data or []):
            if lead["id"] in por_lead:
                continue
            meta = dict(lead.get("metadata") or {})
            meta.pop("parcela_atrasada", None)
            if meta.get("alerta") == "inadimplente":
                meta.pop("alerta", None)
            limpos.append({
                "id":        lead["id"],
                "name":      lead.get("name"),
                "status":    "cliente" if lead.get("status") == "inadimplente" else lead.get("status"),
                "metadata":  meta,
                "tenant_id": self.tenant_id,
            })
        self._upsert_batch(limpos)

        resultado = {
            "alunos_escaneados_com_parcela": len(achados),
            "parcelas_atrasadas":            len(rows_parc),
            "leads_marcados":                n_leads,
            "leads_limpos":                  len(limpos),
        }
        log.info(f"sync_parcelas_atrasadas: {resultado}")
        return resultado

    # -- Prioridade 3: Fases CRM Pacto → sales_pipeline_stages ---------------

    _PIPELINE_NS = uuid.UUID("f1e2d3c4-cafe-5678-abcd-123456789abc")

    def _ensure_pipeline(self, name: str = "CRM Pacto") -> str:
        """Cria ou recupera o pipeline principal. Retorna pipeline_id."""
        pid = str(uuid.uuid5(self._PIPELINE_NS, f"{self.tenant_id}:{name}"))
        self.sb.table("sales_pipelines").upsert({
            "id":         pid,
            "name":       name,
            "description": "Pipeline importado do Pacto Soluções",
            "position":   1,
            "is_default": True,
            "is_active":  True,
            "tenant_id":  self.tenant_id,
        }).execute()
        return pid

    def sync_pipeline_stages_from_pacto(self, adm: "PactoADMClient") -> int:
        """Sincroniza as fases do CRM Pacto para sales_pipeline_stages."""
        log.info("CRM sync: fases do pipeline...")
        fases = adm.fases_crm()
        if not fases:
            log.warning("Nenhuma fase retornada pelo Pacto.")
            return 0

        pipeline_id = self._ensure_pipeline()
        _STAGE_NS   = uuid.UUID("a1b2c3d4-cafe-5678-abcd-fedcba987654")
        cores_padrao = ["#6366f1", "#8b5cf6", "#ec4899", "#f59e0b",
                        "#10b981", "#3b82f6", "#ef4444", "#14b8a6"]

        rows = []
        for i, fase in enumerate(fases):
            name = fase.get("descricao") or fase.get("name") or f"Fase {i+1}"
            slug = fase.get("name", f"fase_{i}")
            sid  = str(uuid.uuid5(_STAGE_NS, f"{pipeline_id}:{slug}"))
            rows.append({
                "id":          sid,
                "name":        name,
                "position":    fase.get("ordem", i + 1),
                "color":       fase.get("cor") or cores_padrao[i % len(cores_padrao)],
                "description": fase.get("name"),
                "is_won":      slug in ("MATRICULA", "RENOVACAO", "REMATRICULA"),
                "is_lost":     slug in ("PERDA", "NAO_CONVERTEU", "OBJECAO"),
                "pipeline_id": pipeline_id,
                "tenant_id":   self.tenant_id,
            })

        for i in range(0, len(rows), 50):
            self.sb.table("sales_pipeline_stages").upsert(rows[i:i+50]).execute()

        log.info(f"sync_pipeline_stages: {len(rows)} fases sincronizadas")
        return len(rows)

    # -- Prioridade 1: Leads de anúncios (Meta Lead Ads) ----------------------

    def sync_meta_lead_ads(self) -> int:
        """
        Migra leads capturados pelo webhook Meta Lead Ads (meta_lead_ads_logs)
        para a tabela leads com utm_source='meta_ads'.
        """
        log.info("CRM sync: Meta Lead Ads → leads...")
        r = self.sb.table("meta_lead_ads_logs").select("*").eq(
            "tenant_id", self.tenant_id
        ).is_("lead_id", "null").execute()
        logs = r.data or []
        if not logs:
            log.info("Nenhum lead Meta Ads pendente de migração.")
            return 0

        rows = []
        for entry in logs:
            lid = entry.get("id")  # usa o próprio id do log como id do lead
            rows.append({
                "id":          lid,
                "name":        entry.get("lead_name"),
                "phone":       "".join(c for c in (entry.get("lead_phone") or "") if c.isdigit()) or None,
                "email":       entry.get("lead_email"),
                "source":      "meta_lead_ads",
                "status":      "lead",
                "utm_source":  "meta_ads",
                "utm_campaign": entry.get("form_name"),
                "tenant_id":   self.tenant_id,
                "metadata": {
                    "meta_page_id":   entry.get("page_id"),
                    "meta_form_id":   entry.get("form_id"),
                    "meta_form_name": entry.get("form_name"),
                    "leadgen_id":     entry.get("leadgen_id"),
                    "raw":            entry.get("raw_data"),
                    "synced_at":      datetime.now().isoformat(),
                },
            })

        n = self._upsert_batch(rows)

        # atualiza lead_id nos logs para não reprocessar
        ids_migrados = [r["id"] for r in rows]
        for i in range(0, len(ids_migrados), 50):
            batch_ids = ids_migrados[i:i+50]
            for log_id in batch_ids:
                self.sb.table("meta_lead_ads_logs").update(
                    {"lead_id": log_id}
                ).eq("id", log_id).execute()

        log.info(f"sync_meta_lead_ads: {n} leads migrados")
        return n

    # -- Prioridade 2: Financeiro → deals --------------------------------------

    _DEAL_NS = uuid.UUID("c0ffee00-cafe-5678-abcd-deadbeef1234")

    def _deal_id(self, pacto_codigo: int, tipo: str) -> str:
        return str(uuid.uuid5(self._DEAL_NS, f"{self.tenant_id}:{pacto_codigo}:{tipo}"))

    def sync_deals_financeiro(self, pacto: "PactoClient", adm: "PactoADMClient") -> dict:
        """
        Cria/atualiza deals para:
          - inadimplentes (contratos vencidos)
          - contratos a vencer em 30 dias
        Usa a fase correta do pipeline se disponível.
        """
        log.info("CRM sync: deals financeiro...")
        pipeline_id = self._ensure_pipeline()

        # tenta mapear stage ids para as fases financeiras
        stages_r = self.sb.table("sales_pipeline_stages").select("id,description").eq(
            "pipeline_id", pipeline_id
        ).execute()
        stage_por_slug = {s["description"]: s["id"] for s in (stages_r.data or [])}
        stage_inadimpl  = stage_por_slug.get("NEGATIVADO") or stage_por_slug.get("PERDA")
        stage_a_vencer  = stage_por_slug.get("RETENCAO")   or stage_por_slug.get("RENOVACAO")

        rows_deals   = []
        rows_leads   = []

        # inadimplentes
        inadimpl = pacto.inadimplentes()
        for a in inadimpl:
            cod  = a.get("codigoCliente")
            if not cod:
                continue
            lead_id = _pacto_lead_id(cod, self.tenant_id)
            rows_leads.append(self._lead_row(cod, a))
            rows_deals.append({
                "id":               self._deal_id(cod, "inadimplente"),
                "lead_id":          lead_id,
                "pipeline_id":      pipeline_id,
                "pipeline_stage_id": stage_inadimpl,
                "title":            f"Inadimplente — {a.get('nome', '')}",
                "original_price":   0.0,
                "status":           "open",
                "payment_status":   "overdue",
                "expected_close_date": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
                "tenant_id":        self.tenant_id,
                "metadata": {
                    "pacto_codigo":  cod,
                    "dias_atraso":   a.get("dias_atraso"),
                    "vencimento":    a.get("vencimento_fmt"),
                    "synced_at":     datetime.now().isoformat(),
                },
            })

        # contratos a vencer em 30 dias
        a_vencer = pacto.contratos_a_vencer(dias=30)
        codigos_inadimpl = {a.get("codigoCliente") for a in inadimpl}
        for a in a_vencer:
            cod = a.get("codigoCliente")
            if not cod or cod in codigos_inadimpl:
                continue  # já processado como inadimplente
            lead_id = _pacto_lead_id(cod, self.tenant_id)
            rows_leads.append(self._lead_row(cod, a))
            venc_date = (a.get("vencimento_fmt") and
                         datetime.strptime(a["vencimento_fmt"], "%d/%m/%Y").strftime("%Y-%m-%d"))
            rows_deals.append({
                "id":               self._deal_id(cod, "renovacao"),
                "lead_id":          lead_id,
                "pipeline_id":      pipeline_id,
                "pipeline_stage_id": stage_a_vencer,
                "title":            f"Renovação — {a.get('nome', '')}",
                "original_price":   0.0,
                "status":           "open",
                "payment_status":   "pending",
                "expected_close_date": venc_date,
                "tenant_id":        self.tenant_id,
                "metadata": {
                    "pacto_codigo":    cod,
                    "dias_restantes":  a.get("dias_restantes"),
                    "vencimento":      a.get("vencimento_fmt"),
                    "synced_at":       datetime.now().isoformat(),
                },
            })

        n_leads = self._upsert_batch(rows_leads)
        n_deals = 0
        for i in range(0, len(rows_deals), 100):
            r = self.sb.table("deals").upsert(rows_deals[i:i+100]).execute()
            n_deals += len(r.data or [])

        log.info(f"sync_deals_financeiro: {n_leads} leads, {n_deals} deals")
        return {"leads": n_leads, "deals": n_deals}

    # -- Orquestradores por frequência -----------------------------------------

    def sincronizar_leads(self, pacto: "PactoClient", adm: "PactoADMClient") -> dict:
        """Sync rápido de leads — rodar a cada hora."""
        resultado = {}
        for nome, fn in [
            ("visitantes_hoje", lambda: self.sync_visitantes(adm)),
            ("meta_lead_ads",   lambda: self.sync_meta_lead_ads()),
        ]:
            try:
                resultado[nome] = fn()
            except Exception as e:
                log.error(f"{nome}: {e}")
                resultado[nome] = f"erro: {e}"
        return resultado

    # -- Merge webhook ↔ Pacto ------------------------------------------------

    def merge_leads_webhook_pacto(self) -> int:
        """
        Funde leads criados pelo webhook do WhatsApp (source NULL) com o
        cadastro Pacto da mesma pessoa (mesmos últimos 8 dígitos do fone —
        o mesmo critério de matching que o webhook usa). O lead Pacto
        sobrevive (UUID determinístico, referenciado por deals/parcelas);
        mensagens e métricas de inbox são reapontadas antes de deletar o
        duplicado. Entre múltiplos candidatos Pacto (famílias compartilham
        telefone), prioriza aluno ativo/inadimplente e depois o mais antigo.
        """
        webhook = (self.sb.table("leads")
                   .select("id,name,phone,photo_url")
                   .is_("source", "null").execute().data or [])

        def last8(p) -> str | None:
            d = "".join(c for c in (p or "") if c.isdigit())
            return d[-8:] if len(d) >= 10 else None

        if not any(last8(w.get("phone")) for w in webhook):
            return 0

        # Indexa a base Pacto por últimos 8 dígitos (paginado)
        by8: dict = {}
        page = 0
        while True:
            r = (self.sb.table("leads")
                 .select("id,name,phone,status,photo_url,metadata,created_at")
                 .not_.is_("source", "null")
                 .range(page * 1000, page * 1000 + 999).execute())
            rows = r.data or []
            for p in rows:
                k = last8(p.get("phone"))
                if k:
                    by8.setdefault(k, []).append(p)
            if len(rows) < 1000:
                break
            page += 1

        merged = 0
        for w in webhook:
            k = last8(w.get("phone"))
            candidatos = by8.get(k or "", [])
            if not candidatos:
                continue
            prioridade = {"cliente": 0, "inadimplente": 0}
            keep = sorted(candidatos,
                          key=lambda c: (prioridade.get(c.get("status"), 1),
                                         c.get("created_at") or ""))[0]
            try:
                self.sb.table("whatsapp_messages").update(
                    {"lead_id": keep["id"]}).eq("lead_id", w["id"]).execute()
                # métricas de inbox: reaponta, ou descarta se o Pacto já tem
                tem = (self.sb.table("cs_inbox_metrics").select("id")
                       .eq("lead_id", keep["id"]).limit(1).execute().data or [])
                if tem:
                    self.sb.table("cs_inbox_metrics").delete().eq(
                        "lead_id", w["id"]).execute()
                else:
                    self.sb.table("cs_inbox_metrics").update(
                        {"lead_id": keep["id"]}).eq("lead_id", w["id"]).execute()
                updates = {"metadata": {**(keep.get("metadata") or {}),
                                        "whatsapp_pushname": w.get("name"),
                                        "merged_from_lead": w["id"]}}
                if w.get("photo_url") and not keep.get("photo_url"):
                    updates["photo_url"] = w["photo_url"]
                self.sb.table("leads").update(updates).eq(
                    "id", keep["id"]).execute()
                self.sb.table("leads").delete().eq("id", w["id"]).execute()
                merged += 1
                log.info(f"Merge: '{w.get('name')}' ({w.get('phone')}) → "
                         f"'{keep.get('name')}' [{keep.get('status')}]")
            except Exception as e:
                log.warning(f"Merge falhou {w['id']} → {keep['id']}: {e}")
        log.info(f"Merge webhook↔Pacto: {merged} lead(s) fundido(s)")
        return merged

    def rotacionar_kanban_leads(self) -> int:
        """
        Move leads do kanban 'Leads Hoje' criados antes de hoje (horário de
        Brasília) para 'Leads Acumuladas'. Os leads entram em 'Leads Hoje'
        pelo webhook do WhatsApp (com origem: anúncio IG/FB, link ou
        espontâneo) e acumulam no dia seguinte se não forem convertidos.
        """
        from datetime import timezone
        stages = (self.sb.table("sales_pipeline_stages").select("id,name")
                  .in_("name", ["Leads Hoje", "Leads Acumuladas"])
                  .execute().data or [])
        por_nome = {s["name"]: s["id"] for s in stages}
        hoje_id = por_nome.get("Leads Hoje")
        acum_id = por_nome.get("Leads Acumuladas")
        if not hoje_id or not acum_id:
            log.warning("Etapas 'Leads Hoje'/'Leads Acumuladas' não encontradas")
            return 0
        tz_sp = timezone(timedelta(hours=-3))
        corte = (datetime.now(tz_sp)
                 .replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(timezone.utc).isoformat())
        r = (self.sb.table("leads")
             .update({"pipeline_stage_id": acum_id})
             .eq("pipeline_stage_id", hoje_id)
             .lt("created_at", corte).execute())
        n = len(r.data or [])
        log.info(f"Kanban leads: {n} movido(s) 'Leads Hoje' → 'Leads Acumuladas'")
        return n

    def sincronizar_diario(self, pacto: "PactoClient", adm: "PactoADMClient") -> dict:
        """Sync completo — rodar uma vez ao dia."""
        resultado = {}
        for nome, fn in [
            ("alunos_ativos",      lambda: self.sync_alunos_ativos(pacto)),
            # logo após alunos_ativos: quem conversou no WhatsApp antes de
            # virar aluno ganha lead Pacto hoje — funde o lead do webhook nele
            ("merge_webhook_pacto", lambda: self.merge_leads_webhook_pacto()),
            # leads de ontem que não converteram saem de "Leads Hoje"
            ("rotacao_kanban_leads", lambda: self.rotacionar_kanban_leads()),
            # depois de alunos_ativos (que reescreve o metadata) pra re-marcar a flag
            ("aniversariantes",    lambda: self.sync_aniversariantes(pacto)),
            ("grupo_risco",        lambda: self.sync_grupo_risco(adm)),
            ("pipeline_stages",    lambda: self.sync_pipeline_stages_from_pacto(adm)),
            ("deals_financeiro",   lambda: self.sync_deals_financeiro(pacto, adm)),
            ("renovacao_mes_atual", lambda: self.sync_renovacao_mes_atual(pacto)),
            ("inadimplentes",      lambda: self.sync_inadimplentes(pacto, adm)),
            # base completa (~2000, ~45min): roda de madrugada junto do diario;
            # achou 22% mais parcelas que o subset de risco (medido 2026-07-02)
            ("parcelas_atrasadas", lambda: self.sync_parcelas_atrasadas(pacto, adm, todos_ativos=True)),
            # 1 request/aluno (~15min na base completa) — alimenta o card do
            # Kanban de Alunos ("Xd sem vir")
            ("ultimo_acesso",      lambda: self.sync_ultimo_acesso(pacto)),
        ]:
            try:
                resultado[nome] = fn()
            except Exception as e:
                log.error(f"{nome}: {e}")
                resultado[nome] = f"erro: {e}"
        return resultado

    def sincronizar_tudo(self, pacto: "PactoClient", adm: "PactoADMClient") -> dict:
        """Executa todos os syncs (leads + diário) e retorna resumo consolidado."""
        log.info("=== Sincronização completa Pacto → CRM ===")
        r1 = self.sincronizar_leads(pacto, adm)
        r2 = self.sincronizar_diario(pacto, adm)
        resultado = {**r1, **r2}
        log.info(f"=== Resultado: {resultado} ===")
        return resultado


def crm_run_scheduler(crm: "CRMClient", pacto: PactoClient,
                      adm: PactoADMClient) -> None:
    """
    Inicia o scheduler de sincronização contínua:
      - a cada hora: visitantes + Meta Lead Ads
      - todos os dias às 07:00: alunos, grupo de risco, fases, deals financeiro
    Mantém o processo rodando até Ctrl+C.
    """
    try:
        import schedule
    except ImportError:
        raise RuntimeError("Pacote 'schedule' não instalado. Execute: pip install schedule")

    log.info("Scheduler CRM iniciado (Ctrl+C para parar).")

    schedule.every().hour.do(crm.sincronizar_leads, pacto, adm)
    schedule.every().day.at("07:00").do(crm.sincronizar_diario, pacto, adm)

    # executa imediatamente na primeira rodada
    crm.sincronizar_tudo(pacto, adm)

    while True:
        schedule.run_pending()
        time.sleep(60)


# -- CLI interativo -----------------------------------------------------------

if __name__ == "__main__":
    import sys

    pacto = PactoClient()
    adm   = PactoADMClient()
    crm   = None  # inicializado sob demanda (requer supabase instalado)

    # Modos nao-interativos (GitHub Actions / Task Scheduler):
    #   python agente_integrador_pacto.py --scan-parcelas-completo  (so parcelas, base inteira)
    #   python agente_integrador_pacto.py --sync-diario             (sincronizar_tudo)
    if "--scan-parcelas-completo" in sys.argv:
        r = CRMClient().sync_parcelas_atrasadas(pacto, adm, todos_ativos=True)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if "--sync-diario" in sys.argv:
        r = CRMClient().sincronizar_tudo(pacto, adm)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        # falha o job se algum sync deu erro (visivel no historico do Actions)
        raise SystemExit(1 if any(isinstance(v, str) and v.startswith("erro") for v in r.values()) else 0)

    def _crm() -> CRMClient:
        global crm
        if crm is None:
            crm = CRMClient()
        return crm

    MENU = """
+------------------------------------------+
|  Agente Integrador Pacto - Territorio Fit |
+------------------------------------------+
 --- TreinoWeb ---
  1 - Alunos ativos
  2 - Alunos faltosos (7 dias)
  3 - Alunos faltosos (14 dias)
  4 - Inadimplentes (contrato vencido)
  5 - Contratos a vencer (30 dias)
  6 - Cancelamentos (30 dias)
  7 - Renovacoes ativas
  8 - Matriculas ativas
  9 - Alunos inativos
 10 - Checkins recentes
 11 - Dashboard completo + exportar MD
 12 - Mapear endpoints
 13 - Usuario autenticado
 --- CRM Pacto (ADM Gateway) ---
 14 - Meta de hoje (abertura + visitantes 24h)
 15 - Fases do funil CRM
 16 - Objecoes cadastradas
 17 - Meta diaria (ultimos 7 dias)
 --- Financeiro (ADM Gateway) ---
 18 - Resumo financeiro (movcontas)
 19 - Recebiveis (tipos 3+8)
 20 - Despesas (tipo 1)
 21 - Grupo de risco (churn)
 22 - Planos cadastrados
 --- Presenca / Acesso Fisico ---
 23 - Lista acessos recentes (academia, hoje)
 24 - Historico presenca aluno (por matricula)
 25 - Distribuicao semanal de acesso fisico (por cod_aluno)
 26 - Reconstruir peso de risco (por cod_aluno)
 --- CRM Supabase (crm.territoriofit.com.br) ---
 27 - Sync: alunos ativos -> leads CRM
 28 - Sync: visitantes hoje -> leads CRM
 29 - Sync: grupo de risco -> leads CRM (metadata)
 30 - Sync: fases Pacto -> sales_pipeline_stages
 31 - Sync: Meta Lead Ads -> leads CRM
 32 - Sync: inadimplentes + a vencer -> deals CRM
 33 - Sync completo (todos os grupos)
 34 - Iniciar scheduler (horario + diario, loop continuo)
 35 - Sync: alunos inativos -> leads CRM
 36 - Sync: renovacao mes atual -> etapa Renovacao CRM
 37 - Sync: inadimplentes (parcelas em aberto) -> etapa Inadimplente CRM
 38 - Sync: parcelas atrasadas EA (risco peso>=5 + inadimplentes, ~740)
 39 - Sync: parcelas atrasadas EA (TODOS os ativos, ~2000 -- lento)
 40 - Sync: ultimo acesso catraca -> leads CRM (metadata, ~15min)
 41 - Sync: aniversariantes do mes -> leads CRM (metadata)
 42 - Merge: leads do webhook WhatsApp -> cadastro Pacto (mesmo fone)
 43 - Kanban: mover 'Leads Hoje' de ontem -> 'Leads Acumuladas'
  0 - Sair
"""

    while True:
        print(MENU)
        op = input("Escolha: ").strip()

        if op == "0":
            break
        elif op == "1":
            r = pacto.alunos_ativos()
            print(f"\nAtivos: {len(r)}")
            pacto.exportar("alunos_ativos", r)
        elif op == "2":
            r = pacto.alunos_faltosos(dias=7)
            print(f"\nFaltosos +7d: {len(r)}")
            for a in r[:10]:
                print(f"  {a.get('nome')[:35]:35} | {a.get('dias_ausente')}d | {a.get('ultimo_treino') or 'nenhum'}")
            pacto.exportar("faltosos_7d", r)
        elif op == "3":
            r = pacto.alunos_faltosos(dias=14)
            print(f"\nFaltosos +14d: {len(r)}")
            for a in r[:10]:
                print(f"  {a.get('nome')[:35]:35} | {a.get('dias_ausente')}d")
            pacto.exportar("faltosos_14d", r)
        elif op == "4":
            r = pacto.inadimplentes()
            print(f"\nInadimplentes: {len(r)}")
            for a in r[:10]:
                print(f"  {a.get('nome')[:35]:35} | venc: {a.get('vencimento_fmt')} | {a.get('dias_atraso')}d atraso")
            pacto.exportar("inadimplentes", r)
        elif op == "5":
            r = pacto.contratos_a_vencer()
            print(f"\nA vencer em 30d: {len(r)}")
            for a in r[:10]:
                print(f"  {a.get('nome')[:35]:35} | vence: {a.get('vencimento_fmt')} | {a.get('dias_restantes')}d")
            pacto.exportar("a_vencer", r)
        elif op == "6":
            r = pacto.cancelamentos()
            print(f"\nCancelamentos (30d): {len(r)}")
            pacto.exportar("cancelamentos", r)
        elif op == "7":
            r = pacto.renovacoes()
            print(f"\nRenovacoes ativas: {len(r)}")
            pacto.exportar("renovacoes", r)
        elif op == "8":
            r = pacto.matriculas_novas()
            print(f"\nMatriculas ativas: {len(r)}")
            pacto.exportar("matriculas", r)
        elif op == "9":
            r = pacto.alunos_inativos()
            print(f"\nInativos: {len(r)}")
            pacto.exportar("inativos", r)
        elif op == "10":
            r = pacto.checkins_recentes()
            print(f"\nCheckins recentes: {len(r)}")
            for c in r[:10]:
                print(f"  {c.get('nome')[:30]:30} | {c.get('dataHora')} | {c.get('tipoCheckin')}")
        elif op == "11":
            painel = pacto.dashboard()
            pacto.exportar("dashboard", painel)
            md = pacto.exportar_dashboard_md(painel)
            print(f"\nDashboard:")
            print(f"   Ativos:          {painel['alunos']['ativos']}")
            print(f"   Faltosos +7d:    {painel['alunos']['faltosos_7_dias']}")
            print(f"   Faltosos +14d:   {painel['alunos']['faltosos_14_dias']}")
            print(f"   Inadimplentes:   {painel['contratos']['inadimplentes']}")
            print(f"   A vencer 30d:    {painel['contratos']['a_vencer_30_dias']}")
            print(f"   Cancelamentos:   {painel['contratos']['cancelamentos_30d']}")
            print(f"\n   MD: {md}")
        elif op == "12":
            r = pacto.listar_endpoints()
            print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
            pacto.exportar("endpoints", r)
        elif op == "13":
            r = pacto.obter_usuario()
            u = r.get("user", {})
            print(f"\nUsuario: {u.get('nome')} (@{u.get('username')})")
            print(f"Perfil:  {', '.join(u.get('perfis', []))}")
            emp = r.get("unidadesEmpresa", [{}])[0]
            print(f"Empresa: {emp.get('nome')} (id={emp.get('id')})")
            recursos = r.get("perfilUsuario", {}).get("recursos", [])
            print(f"Recursos: {len(recursos)} permissoes")
            funcs = [f for f in r.get("perfilUsuario", {}).get("funcionalidades", []) if f.get("possuiFuncionalidade")]
            print(f"Funcionalidades ativas: {len(funcs)}")
            pacto.exportar("usuario", r)
        elif op == "14":
            ab = adm.meta_crm_abertura()
            print(f"\nMeta hoje: abriu={ab.get('abriuMetaHoje')} | pode abrir={ab.get('metaPodeSerAberta')}")
            if ab.get("mensagemBloqueio"):
                print(f"Bloqueio: {ab.get('mensagemBloqueio')}")
            v = adm.visitantes_24h(date.today())
            print(f"Visitantes 24h: {v.get('total')} total | meta={v.get('meta')} | realizado={v.get('realizado')}")
            for vis in v.get("visitantes", [])[:10]:
                print(f"  {(vis.get('nome') or '?')[:35]:35} | {vis.get('situacao')} | {vis.get('consultora') or '—'}")
            pacto.exportar("visitantes_24h", v)
        elif op == "15":
            fases = adm.fases_crm()
            print(f"\nFases CRM ({len(fases)} total):")
            for f in fases:
                print(f"  {f.get('name','?'):35} {f.get('descricao','')[:40]}")
        elif op == "16":
            obj = adm.objecoes()
            print(f"\nObjecoes ({len(obj)}):")
            for o in obj:
                ativo = "ativo" if o.get("ativo") else "inativo"
                print(f"  [{o.get('codigo')}] {o.get('descricao','?')} ({ativo})")
        elif op == "17":
            hoje = date.today()
            inicio = hoje - timedelta(days=7)
            r = adm.meta_diaria(inicio, hoje)
            print(f"\nMeta diaria ({inicio} a {hoje}): {len(r)} registros")
            for m in r[:10]:
                print(f"  {json.dumps(m, ensure_ascii=False)[:100]}")
            if r:
                pacto.exportar("meta_diaria", r)
        elif op == "18":
            r = adm.resumo_financeiro()
            print(f"\nResumo financeiro:")
            print(f"  Lancamentos: {r.get('total_lancamentos')}")
            print(f"  Recebiveis:  R$ {r.get('recebiveis_total'):,.2f}")
            print(f"  Despesas:    R$ {r.get('despesas_total'):,.2f}")
            print(f"  Estornos:    R$ {r.get('estornos_total'):,.2f}")
            print(f"  Saldo:       R$ {r.get('saldo'):,.2f}")
            print(f"  Por tipo:    {r.get('por_tipo')}")
            pacto.exportar("resumo_financeiro", r)
        elif op == "19":
            r = adm.receitas()
            print(f"\nRecebiveis (tipos 3+8): {len(r)}")
            for m in r[:10]:
                print(f"  {m.get('descricao','?')[:45]:45} | R$ {m.get('valor')} | {m.get('dataLancamento','')[:10]}")
            pacto.exportar("recebiveis", r)
        elif op == "20":
            r = adm.despesas()
            print(f"\nDespesas (tipo 1): {len(r)}")
            for m in r[:10]:
                print(f"  {m.get('descricao','?')[:45]:45} | R$ {m.get('valor')} | {m.get('dataLancamento','')[:10]}")
            pacto.exportar("despesas", r)
        elif op == "21":
            r = adm.grupo_risco()
            print(f"\nGrupo de risco (churn): {len(r)} clientes")
            for c in r[:10]:
                print(f"  [{c.get('cliente')}] {c.get('nomeCliente','?')[:40]}")
            pacto.exportar("grupo_risco", r)
        elif op == "22":
            r = adm.planos()
            print(f"\nPlanos ({len(r)}):")
            for p in r[:15]:
                print(f"  [{p.get('codigo')}] {p.get('descricao','?')[:50]}")
            pacto.exportar("planos", r)
        elif op == "23":
            r = pacto.lista_acessos_recentes(tipo=1, limite=50)
            print(f"\nAcessos fisicos recentes: {len(r)}")
            for a in r[:15]:
                print(f"  [{a.get('matricula')}] {a.get('nome','?')[:35]:35} | {a.get('hora','')} | {a.get('plano','')[:25]}")
        elif op == "24":
            mat = int(input("Matricula ZW do aluno: ").strip())
            r = pacto.historico_presenca(mat)
            print(f"\nHistorico de presenca (mat={mat}):")
            print(f"  Total aulas realizadas : {r.get('totalAulasRealizadas','?')}")
            print(f"  Aulas no mes atual     : {r.get('aulasMesAtual','?')}")
            print(f"  Semanas consecutivas   : {r.get('semanasConsecutivas','?')}")
        elif op == "25":
            cod = int(input("Codigo do aluno (codigoCliente): ").strip())
            r = pacto.distribuicao_acesso_semanal(cod)
            total = sum(r.values()) if r else 0
            print(f"\nDistribuicao de acesso fisico semanal (cod={cod}, ultimos 6 meses):")
            for dia, cnt in r.items():
                bar = "#" * cnt
                print(f"  {dia}: {cnt:3}  {bar}")
            print(f"  Total: {total} acessos")
        elif op == "26":
            cod = int(input("Codigo do aluno (codigoCliente): ").strip())
            r = adm.reconstruir_peso(cod)
            print(f"\nPeso de risco reconstruido (cod={cod}):")
            print(f"  Peso calculado : {r['peso_calculado']}")
            print(f"  Vencimento     : {r['peso_vencimento']} ({r['vencimento']}"
                  + (f", {r['dias_para_vencer']}d" if r['dias_para_vencer'] is not None else "") + ")")
            print(f"  Faltas         : {r['peso_faltas']} ({r['dias_ausente_aprox']} sem acesso)")
            print(f"  Presenca 4 sem : {r['peso_presenca']} ({r['media_presenca_4sem']:.1f} dias/sem)")
            print(f"  Formula        : {r['detalhes']}")
        elif op == "27":
            n = _crm().sync_alunos_ativos(pacto)
            print(f"\nSync alunos ativos → CRM: {n} leads upserted")
        elif op == "28":
            n = _crm().sync_visitantes(adm)
            print(f"\nSync visitantes hoje → CRM: {n} leads upserted")
        elif op == "29":
            n = _crm().sync_grupo_risco(adm)
            print(f"\nSync grupo de risco → CRM: {n} leads atualizados")
        elif op == "30":
            n = _crm().sync_pipeline_stages_from_pacto(adm)
            print(f"\nSync fases CRM → sales_pipeline_stages: {n} fases")
        elif op == "31":
            n = _crm().sync_meta_lead_ads()
            print(f"\nSync Meta Lead Ads → leads CRM: {n} leads migrados")
        elif op == "32":
            r = _crm().sync_deals_financeiro(pacto, adm)
            print(f"\nSync financeiro → deals CRM: {r['leads']} leads, {r['deals']} deals")
        elif op == "33":
            r = _crm().sincronizar_tudo(pacto, adm)
            print("\nSync completo concluido:")
            for k, v in r.items():
                print(f"  {k:25}: {v}")
        elif op == "34":
            print("\nIniciando scheduler (Ctrl+C para parar)...")
            crm_run_scheduler(_crm(), pacto, adm)
        elif op == "35":
            n = _crm().sync_alunos_inativos(pacto)
            print(f"\nSync alunos inativos → CRM: {n} leads upserted")
        elif op == "36":
            n = _crm().sync_renovacao_mes_atual(pacto)
            print(f"\nSync renovação mês atual → CRM: {n} leads na etapa Renovação")
        elif op == "37":
            n = _crm().sync_inadimplentes(pacto, adm)
            print(f"\nSync inadimplentes → CRM: {n} leads (com dados de parcela em aberto)")
        elif op == "38":
            r = _crm().sync_parcelas_atrasadas(pacto, adm)
            print(f"\nSync parcelas atrasadas → CRM:")
            for k, v in r.items():
                print(f"  {k:32}: {v}")
        elif op == "39":
            r = _crm().sync_parcelas_atrasadas(pacto, adm, todos_ativos=True)
            print(f"\nSync parcelas atrasadas (base completa) → CRM:")
            for k, v in r.items():
                print(f"  {k:32}: {v}")
        elif op == "40":
            n = _crm().sync_ultimo_acesso(pacto)
            print(f"\nSync ultimo acesso catraca → CRM: {n} leads atualizados")
        elif op == "41":
            n = _crm().sync_aniversariantes(pacto)
            print(f"\nSync aniversariantes do mes → CRM: {n} leads atualizados")
        elif op == "42":
            n = _crm().merge_leads_webhook_pacto()
            print(f"\nMerge leads webhook ↔ Pacto: {n} lead(s) fundido(s)")
        elif op == "43":
            n = _crm().rotacionar_kanban_leads()
            print(f"\nKanban: {n} lead(s) movido(s) para 'Leads Acumuladas'")
        else:
            print("Opcao invalida.")
