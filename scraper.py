# -*- coding: utf-8 -*-
"""
Monitor de precos Skyscanner - Zupper (GitHub Actions, sem GCP) - v3 (Bright Data)
Busca as top rotas via Bright Data Web Unlocker (passa pelo anti-bot/captcha),
embarque sempre D+30, e grava snapshots em data/precos.json (rolante 45 dias).
O dashboard le o JSON via cdn.jsdelivr.net.

Credenciais (GitHub Secrets, nunca no codigo):
  BRIGHTDATA_TOKEN  -> token de API da conta Bright Data
  BRIGHTDATA_ZONE   -> nome da zona Web Unlocker (ex.: web_unlocker1)
"""
import re
import os
import json
import time
import datetime as dt
from zoneinfo import ZoneInfo

import requests

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

BASE = os.path.dirname(__file__)
ARQ = os.path.join(BASE, "data", "precos.json")
ARQ_DEBUG = os.path.join(BASE, "data", "debug_ultima_pagina.txt")
ANTECEDENCIA_DIAS = 30
RETENCAO_DIAS = 45
TZ = ZoneInfo("America/Sao_Paulo")
ANOMALIA_PCT = 60

BRD_TOKEN = os.environ.get("BRIGHTDATA_TOKEN", "").strip()
BRD_ZONE = os.environ.get("BRIGHTDATA_ZONE", "web_unlocker1").strip()
BRD_API = "https://api.brightdata.com/request"
BRD_TIMEOUT = 120

ROTAS = [
    ("SSA", "CGH"), ("CGH", "SSA"),
    ("GRU", "SSA"), ("SSA", "GRU"),
    ("GIG", "SSA"), ("SSA", "GIG"),
    ("REC", "CGH"), ("CGH", "REC"),
    ("GRU", "JDO"), ("FOR", "GRU"),
]


def buscar_html(url):
    """Pede a pagina ao Web Unlocker da Bright Data e devolve o HTML ja destravado."""
    resp = requests.post(
        BRD_API,
        headers={"Authorization": f"Bearer {BRD_TOKEN}", "Content-Type": "application/json"},
        json={"zone": BRD_ZONE, "url": url, "format": "raw", "country": "br"},
        timeout=BRD_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def html_para_texto(html):
    """Converte HTML em texto visivel, aproximando o inner_text do navegador."""
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.extract()
        return soup.get_text("\n")
    txt = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    return re.sub(r"\n\s*\n+", "\n", txt)


def parse_page_text(text):
    out = {"mais_barato": None, "direto": None, "por_companhia": {}, "vendedores": {}, "zupper_vitrine": None}
    m = re.search(r"Mais barato\s*\n?\s*R\$\s?([\d.]+)", text)
    if m:
        out["mais_barato"] = int(m.group(1).replace(".", ""))
    m = re.search(r"Direto\s*\n?\s*a partir de R\$\s?([\d.]+)", text)
    if m:
        out["direto"] = int(m.group(1).replace(".", ""))
    companhias = {
        "GOL": r"GOL Linhas A\S?reas",
        "LATAM": r"LATAM Airlines",
        "Azul": r"Azul",
        "Voepass": r"Voepass",
        "Aerolineas": r"Aerol\S?neas Argentinas",
    }
    for nome, padrao in companhias.items():
        m = re.search(padrao + r"\s*\n?\s*a partir de R\$\s?([\d.]+)", text)
        if m:
            out["por_companhia"][nome] = int(m.group(1).replace(".", ""))
    for m in re.finditer(r"Reserve com a ([^\n]+?) a partir de\s*\n?\s*R\$\s?([\d.]+)", text):
        vend, preco = m.group(1).strip(), int(m.group(2).replace(".", ""))
        prev = out["vendedores"].get(vend)
        out["vendedores"][vend] = min(prev, preco) if prev else preco
    m = re.search(r"Zupper[^\n]*?\s*R\$\s?([\d.]+)", text)
    if m:
        out["zupper_vitrine"] = int(m.group(1).replace(".", ""))
    return out


def coleta_rota(ori, dst, data_embarque, debug_sink=None):
    url = (f"https://www.skyscanner.com.br/transporte/voos/{ori.lower()}/{dst.lower()}/"
           f"{data_embarque.strftime('%y%m%d')}/?adultsv2=1&cabinclass=economy&rtn=0")
    html = buscar_html(url)
    text = html_para_texto(html)
    dados = parse_page_text(text)
    dados["url"] = url
    low = text.lower()
    dados["bloqueado"] = (
        "captcha" in low or "are you a person" in low or "verifique" in low
        or "unusual traffic" in low or dados["mais_barato"] is None
    )
    if debug_sink is not None and not debug_sink.get("gravado"):
        debug_sink["gravado"] = True
        debug_sink["conteudo"] = (
            f"=== DEBUG COLETA (Bright Data Web Unlocker) ===\n"
            f"rota: {ori}-{dst}\n"
            f"url: {url}\n"
            f"zona: {BRD_ZONE}\n"
            f"tamanho_html: {len(html)} chars | tamanho_texto: {len(text)} chars\n"
            f"bloqueado (heuristica): {dados['bloqueado']}\n"
            f"parse: mais_barato={dados['mais_barato']} direto={dados['direto']} "
            f"companhias={list(dados['por_companhia'].keys())} "
            f"vendedores={list(dados['vendedores'].keys())}\n"
            f"=== PRIMEIROS 10000 CHARS DO TEXTO DA PAGINA ===\n"
            f"{text[:10000]}\n"
        )
    return dados


def main():
    agora = dt.datetime.now(TZ)
    embarque = (agora + dt.timedelta(days=ANTECEDENCIA_DIAS)).date()

    if not BRD_TOKEN:
        print("ERRO: BRIGHTDATA_TOKEN nao configurado (GitHub Secret ausente).")

    hist = {"coletas": []}
    if os.path.exists(ARQ):
        try:
            with open(ARQ, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = {"coletas": []}

    ultimo = {}
    for c in hist.get("coletas", []):
        if c.get("preco_mais_barato"):
            ultimo[c["rota"]] = c["preco_mais_barato"]

    debug_sink = {"gravado": False, "conteudo": ""}
    novas = []
    for ori, dst in ROTAS:
        rota, status, dados, flag = f"{ori}-{dst}", "ok", None, None
        try:
            dados = coleta_rota(ori, dst, embarque, debug_sink)
            if dados["bloqueado"]:
                status = "bloqueado_ou_vazio"
            elif ultimo.get(rota) and dados["mais_barato"]:
                desvio = abs(dados["mais_barato"] / ultimo[rota] - 1) * 100
                if desvio >= ANOMALIA_PCT:
                    flag = f"verificar: desvio {desvio:.0f}% vs anterior (R$ {ultimo[rota]})"
        except Exception as e:
            status = f"erro: {str(e)[:200]}"
        novas.append({
            "coletado_em": agora.isoformat(),
            "rota": rota, "origem": ori, "destino": dst,
            "data_embarque": embarque.isoformat(),
            "antecedencia_dias": ANTECEDENCIA_DIAS,
            "preco_mais_barato": (dados or {}).get("mais_barato"),
            "preco_direto": (dados or {}).get("direto"),
            "por_companhia": (dados or {}).get("por_companhia", {}),
            "vendedores": (dados or {}).get("vendedores", {}),
            "zupper_vitrine": (dados or {}).get("zupper_vitrine"),
            "url_busca": (dados or {}).get("url"),
            "status": status, "flag_auditoria": flag,
        })
        time.sleep(2)

    corte = (agora - dt.timedelta(days=RETENCAO_DIAS)).isoformat()
    todas = [c for c in hist.get("coletas", []) if c["coletado_em"] >= corte] + novas
    os.makedirs(os.path.dirname(ARQ), exist_ok=True)
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump({
            "atualizado_em": agora.isoformat(),
            "regra": f"embarque sempre D+{ANTECEDENCIA_DIAS}; retencao {RETENCAO_DIAS}d; via Bright Data",
            "coletas": todas,
        }, f, ensure_ascii=False, indent=1)

    with open(ARQ_DEBUG, "w", encoding="utf-8") as f:
        f.write(debug_sink.get("conteudo") or "sem conteudo de debug capturado")

    ok = sum(1 for c in novas if c["status"] == "ok")
    print(f"Coleta {agora.isoformat()}: {ok}/{len(novas)} rotas ok. "
          f"Embarque: {embarque}. Total historico: {len(todas)} linhas.")


if __name__ == "__main__":
    main()
