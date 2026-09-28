# -*- coding: utf-8 -*-
"""
Analise unica pro Andre (pedido 28/09/2026): 1 semana depois das mudancas no
Google Ads de 28/09 (anuncios voltaram pra home, grupo de marca antigo
reativado, campanha de marca nova pausada), mandar um resumo no WhatsApp
pessoal dele pelo numero "Ceo Territorio Digital".

Fonte: website_contact_clicks do CRM (cliques de WhatsApp no site com
attribution.gclid/gbraid = veio de anuncio do Google, e landing_path = pagina
de entrada). NAO tem gasto nem custo por conversao: isso so existe no painel
do Google Ads, que nao tem API ligada aqui.

Env: SUPABASE_KEY, UAZAPI_TOKEN_CEO. Opcional: RELATORIO_PARA (padrao numero
pessoal do Andre) | DRY_RUN=1 (so imprime o texto) | FORCAR=1 (ignora a trava
de data, pra teste).
"""

import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests

SUPABASE_URL = "https://bmnyhaxvlifmwkcuglfh.supabase.co"
UAZAPI_URL = "https://territoriofit.uazapi.com"
TZ_SP = timezone(timedelta(hours=-3))

DIA_DO_ENVIO = date(2026, 10, 5)
ANTES = (date(2026, 9, 22), date(2026, 9, 28))   # anuncios na pagina dedicada
DEPOIS_INICIO = date(2026, 9, 29)                # 1o dia cheio apos a troca

PAGINAS = [
    ("/academia-sao-carlos", "Página dedicada"),
    ("/pilates-sao-carlos", "Página de pilates"),
    ("/escolher-academia-sao-carlos", "Página de comparação"),
]


def pagina(path: str) -> str:
    p = (path or "/").split("?")[0].split("#")[0].rstrip("/") or "/"
    for prefixo, nome in PAGINAS:
        if p.startswith(prefixo):
            return nome
    return "Home e demais páginas do site"


def buscar(key: str) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/website_contact_clicks",
        params={"select": "created_at,placement,attribution,conversation_at",
                "created_at": "gte.2026-09-22T03:00:00Z",
                "order": "created_at.asc", "limit": "5000"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=60)
    r.raise_for_status()
    out = []
    for row in r.json():
        a = row.get("attribution") or {}
        if not isinstance(a, dict):
            continue
        dia = datetime.fromisoformat(
            row["created_at"].replace("Z", "+00:00")).astimezone(TZ_SP).date()
        out.append({"dia": dia,
                    "google_ads": bool(a.get("gclid") or a.get("gbraid")
                                       or a.get("wbraid")),
                    "pagina": pagina(a.get("landing_path")),
                    "conversa": bool(row.get("conversation_at"))})
    return out


def resumo(linhas: list[dict], ini: date, fim: date) -> dict:
    sel = [l for l in linhas if ini <= l["dia"] <= fim and l["google_ads"]]
    dias = (fim - ini).days + 1
    return {"dias": dias, "cliques": len(sel),
            "por_dia": len(sel) / dias if dias else 0,
            "conversas": sum(1 for l in sel if l["conversa"]),
            "paginas": Counter(l["pagina"] for l in sel),
            "todos": sum(1 for l in linhas if ini <= l["dia"] <= fim)}


def num(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


def montar(a: dict, d: dict, fim_depois: date) -> str:
    linhas = ["*Resumo dos anúncios, uma semana depois das mudanças de 28/09*",
              "", "*Google Ads*"]
    linhas.append("Cliques no WhatsApp vindos de anúncio do Google, "
                  "pelo rastreio do nosso site:")
    linhas.append(f"• Antes (22 a 28/09, {a['dias']} dias): {a['cliques']} "
                  f"cliques, {num(a['por_dia'])} por dia, "
                  f"{a['conversas']} viraram conversa")
    linhas.append(f"• Depois (29/09 a {fim_depois:%d/%m}, {d['dias']} dias): "
                  f"{d['cliques']} cliques, {num(d['por_dia'])} por dia, "
                  f"{d['conversas']} viraram conversa")
    linhas.append("")
    if a["por_dia"] > 0:
        var = (d["por_dia"] / a["por_dia"] - 1) * 100
        if abs(var) < 10:
            leitura = "ficou praticamente igual"
        elif var > 0:
            leitura = f"subiu {abs(var):.0f}%"
        else:
            leitura = f"caiu {abs(var):.0f}%"
        linhas.append(f"Na média por dia, o volume {leitura}.")
    else:
        linhas.append("Não havia cliques no período anterior pra comparar.")
    if d["cliques"] < 15:
        linhas.append("O volume é pequeno, então a diferença pode ser acaso.")
    linhas.append("")
    linhas.append("Por página de entrada, depois da mudança:")
    if d["paginas"]:
        for nome, n in d["paginas"].most_common():
            linhas.append(f"• {nome}: {n}")
    else:
        linhas.append("• nenhum clique registrado")
    linhas.append("")
    linhas.append(f"Para comparação, o site teve {d['todos']} cliques no "
                  "WhatsApp somando todas as origens nesse período.")
    linhas.append("")
    linhas.append("*O que este resumo não tem:* gasto, custo por conversão, "
                  "resultado do grupo de marca e aprovação dos anúncios. "
                  "Esses números só saem do painel do Google Ads.")
    linhas.append("")
    linhas.append("Para a análise completa, abra o Claude no PC com o Chrome "
                  "logado na conta da academia e peça a performance do "
                  "Google Ads.")
    return "\n".join(linhas)


# ---------------------------------------------------------------- Meta Ads
# Pedido do Andre 28/09: alem do Google, acompanhar 2 anuncios em observacao
# no Meta (reel Aulas Coletivas no Semelhante 1% e imagem "Chega de segunda"
# no frio). So leitura: token META_ADS_TOKEN da tabela config do CRM.

META_CONTA = "act_571830319971175"
GRAPH = "https://graph.facebook.com/v21.0/"
CONVERSA = "onsite_conversion.messaging_conversation_started_7d"
SEMANA_BASE = {"conta": 13.80, "frio": 22.17, "remarketing": 8.84,
               "semelhante": 14.76}  # custo por conversa de 21 a 27/09


def _meta_token(key: str) -> str:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/config",
                     params={"select": "value", "key": "eq.META_ADS_TOKEN"},
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=30)
    r.raise_for_status()
    return (r.json()[0]["value"] or "").strip()


def _insights(tok: str, ini: date, fim: date, nivel: str) -> list[dict]:
    import json
    r = requests.get(
        GRAPH + META_CONTA + "/insights",
        params={"fields": "campaign_name,adset_name,ad_name,spend,actions",
                "level": nivel, "limit": "300",
                "time_range": json.dumps({"since": ini.isoformat(),
                                          "until": fim.isoformat()}),
                "access_token": tok}, timeout=60)
    r.raise_for_status()
    out = []
    for row in r.json().get("data", []):
        conv = 0
        for a in row.get("actions") or []:
            if a["action_type"] == CONVERSA:
                conv = int(float(a["value"]))
        out.append({"campanha": row.get("campaign_name") or "",
                    "conjunto": row.get("adset_name") or "",
                    "anuncio": row.get("ad_name") or "",
                    "gasto": float(row.get("spend") or 0), "conv": conv})
    return out


def _reais(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _resultado(gasto: float, conv: int) -> str:
    """Ex.: '4 conversas, R$ 23,48 por conversa' ou 'nenhuma conversa'."""
    if not conv:
        return "nenhuma conversa"
    nome = "1 conversa" if conv == 1 else f"{conv} conversas"
    return f"{nome}, {_reais(gasto / conv)} por conversa"


def _frase(texto: str) -> str:
    return texto[:1].upper() + texto[1:]


def secao_meta(key: str, fim: date) -> list[str]:
    tok = _meta_token(key)
    ini = date(2026, 9, 28)
    camp = _insights(tok, ini, fim, "campaign")
    vendas = [c for c in camp if "vagas" not in c["campanha"].lower()]
    g = sum(c["gasto"] for c in vendas)
    n = sum(c["conv"] for c in vendas)
    linhas = ["", f"*Meta Ads, de {ini:%d/%m} a {fim:%d/%m}*",
              f"• Conta (sem Vagas): {_reais(g)}, {_resultado(g, n)}. "
              f"Na semana anterior era {_reais(SEMANA_BASE['conta'])}."]
    for chave, nome, ident in (("frio", "Público frio", "vendas"),
                               ("remarketing", "Remarketing", "remarketing"),
                               ("semelhante", "Semelhante 1%", "semelhante")):
        sel = [c for c in camp if ident in c["campanha"].lower()]
        gg = sum(c["gasto"] for c in sel)
        nn = sum(c["conv"] for c in sel)
        linhas.append(f"• {nome}: {_reais(gg)}, {_resultado(gg, nn)} "
                      f"(era {_reais(SEMANA_BASE[chave])})")

    linhas += ["", "Anúncios que ficaram em observação:"]
    ads_total = _insights(tok, date(2026, 9, 23), fim, "ad")
    reel = [a for a in ads_total if a["anuncio"].startswith("[LAL-COLETIVAS-REEL]")]
    gr = sum(a["gasto"] for a in reel)
    nr = sum(a["conv"] for a in reel)
    if gr < 100:
        dec = "ainda abaixo de R$ 100 de gasto, manter em observação"
    elif nr and gr / nr <= 15:
        dec = "dentro da régua, sugestão: manter"
    else:
        dec = "acima da régua, sugestão: pausar"
    linhas.append(f"• Reel Aulas Coletivas no Semelhante: {_reais(gr)} desde "
                  f"23/09, {_resultado(gr, nr)}. {_frase(dec)}.")

    ads_14 = _insights(tok, date(2026, 9, 21), fim, "ad")
    img = [a for a in ads_14 if a["anuncio"].startswith("[AE-A]")]
    gi = sum(a["gasto"] for a in img)
    ni = sum(a["conv"] for a in img)
    if ni == 0 and gi >= 60:
        dec = "sugestão: pausar"
    elif ni and gi / ni <= 15:
        dec = "voltou a converter, sugestão: manter"
    elif ni:
        dec = "convertendo caro, sugestão: observar mais uma semana"
    else:
        dec = "pouco gasto pra decidir, manter em observação"
    linhas.append(f"• Imagem \"Chega de segunda eu começo\" no frio: {_reais(gi)} "
                  f"desde 21/09, {_resultado(gi, ni)}. {_frase(dec)}.")
    linhas += ["", "Nada foi alterado automaticamente no Meta. Se quiser "
                   "aplicar alguma sugestão, é só me pedir no Claude."]
    return linhas


def main() -> int:
    key = os.environ.get("SUPABASE_KEY", "").replace("﻿", "").strip()
    token = os.environ.get("UAZAPI_TOKEN_CEO", "").replace("﻿", "").strip()
    destino = (os.environ.get("RELATORIO_PARA") or "5516992290338").strip()
    dry = os.environ.get("DRY_RUN", "") == "1"
    forcar = os.environ.get("FORCAR", "") == "1"
    hoje = datetime.now(TZ_SP).date()
    if not key:
        print("Falta env SUPABASE_KEY")
        return 1
    if hoje != DIA_DO_ENVIO and not (dry or forcar):
        print(f"Hoje é {hoje:%d/%m/%Y}; o envio é só em "
              f"{DIA_DO_ENVIO:%d/%m/%Y}. Nada enviado.")
        return 0

    linhas = buscar(key)
    fim_depois = min(hoje - timedelta(days=1), date(2026, 10, 4))
    if fim_depois < DEPOIS_INICIO:
        fim_depois = DEPOIS_INICIO
    texto = montar(resumo(linhas, *ANTES),
                   resumo(linhas, DEPOIS_INICIO, fim_depois), fim_depois)
    try:
        texto += "\n" + "\n".join(secao_meta(key, fim_depois))
    except Exception as e:  # o resumo do Google sai mesmo se o Meta falhar
        print(f"[meta] falhou: {e}")
        texto += ("\n\n*Meta Ads:* não consegui ler os números do Meta "
                  "nesta execução.")
    print(texto)
    if dry:
        print("\n[DRY] nada enviado.")
        return 0
    if not token:
        print("Falta env UAZAPI_TOKEN_CEO")
        return 1
    resp = requests.post(
        f"{UAZAPI_URL}/send/text",
        headers={"token": token, "Content-Type": "application/json"},
        json={"number": destino, "text": texto}, timeout=120)
    print(f"\n[envio] -> ...{destino[-4:]} HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
