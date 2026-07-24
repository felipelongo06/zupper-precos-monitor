# -*- coding: utf-8 -*-
"""
Monitor de precos Skyscanner - Zupper (versao GitHub Actions, sem GCP) - v2
Roda 4x/dia via cron do Actions, pesquisa as top rotas (embarque sempre D+30)
e grava snapshots em data/precos.json (historico rolante de 45 dias).
O dashboard le o JSON via cdn.jsdelivr.net.

v2: stealth + fecha banner de cookies + espera real + dump de debug + sempre grava.
"""
import re
import json
import os
import datetime as dt
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(__file__)
ARQ = os.path.join(BASE, "data", "precos.json")
ARQ_DEBUG = os.path.join(BASE, "data", "debug_ultima_pagina.txt")
ANTECEDENCIA_DIAS = 30            # embarque = hoje + 30 (regra fixa de comparabilidade)
RETENCAO_DIAS = 45                # historico mantido no JSON
TZ = ZoneInfo("America/Sao_Paulo")
TIMEOUT_MS = 60000
ANOMALIA_PCT = 60

ROTAS = [
    ("SSA", "CGH"), ("CGH", "SSA"),
    ("GRU", "SSA"), ("SSA", "GRU"),
    ("GIG", "SSA"), ("SSA", "GIG"),
    ("REC", "CGH"), ("CGH", "REC"),
    ("GRU", "JDO"), ("FOR", "GRU"),
]

# Script de stealth: esconde sinais obvios de automacao antes de qualquer script da pagina rodar
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = { runtime: {} };
const _q = window.navigator.permissions && window.navigator.permissions.query;
if (_q) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _q(p)
  );
}
"""

COOKIE_SELETORES = [
    "#onetrust-accept-btn-handler",
    "button:has-text('Aceitar todos')",
    "button:has-text('Aceitar')",
    "button:has-text('Accept all')",
    "button:has-text('Accept')",
    "button:has-text('Concordo')",
    "[data-testid='cookie-banner'] button",
]


def fechar_cookies(page):
    for sel in COOKIE_SELETORES:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
                return sel
        except Exception:
            continue
    return None


def texto_pagina(page):
    """Extrai texto da area principal; cai pro body se nao houver <main>."""
    for sel in ["main", "body"]:
        try:
            t = page.inner_text(sel)
            if t and len(t.strip()) > 0:
                return t
        except Exception:
            continue
    return ""


def parse_page_text(text):
    out = {"mais_barato": None, "direto": None, "por_companhia": {}, "vendedores": {}, "zupper_vitrine": None}
    m = re.search(r"Mais barato\s*\n?\s*R\$\s?([\d.]+)", text)
    if m:
        out["mais_barato"] = int(m.group(1).replace(".", ""))
    m = re.search(r"Direto\s*\n?\s*a partir de R\$\s?([\d.]+)", text)
    if m:
        out["direto"] = int(m.group(1).replace(".", ""))
    # aceita nome com ou sem acento (\S no lugar da vogal acentuada)
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


def coleta_rota(page, ori, dst, data_embarque, debug_sink=None):
    url = (f"https://www.skyscanner.com.br/transporte/voos/{ori.lower()}/{dst.lower()}/"
           f"{data_embarque.strftime('%y%m%d')}/?adultsv2=1&cabinclass=economy&rtn=0")
    page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
    fechar_cookies(page)
    # espera a pagina hidratar; tenta rede ociosa, senao espera fixa
    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        page.wait_for_timeout(15000)
    try:
        page.wait_for_selector("text=Mais barato", timeout=20000)
    except Exception:
        pass

    text = texto_pagina(page)
    dados = parse_page_text(text)
    dados["url"] = url
    dados["url_final"] = page.url
    low = text.lower()
    titulo = ""
    try:
        titulo = page.title()
    except Exception:
        pass
    dados["bloqueado"] = (
        "captcha" in low or "are you a person" in low or "verifique" in low
        or "unusual traffic" in low or dados["mais_barato"] is None
    )

    # dump de debug: guarda o que a primeira rota devolveu, para diagnostico
    if debug_sink is not None and not debug_sink.get("gravado"):
        debug_sink["gravado"] = True
        debug_sink["conteudo"] = (
            f"=== DEBUG COLETA ===\n"
            f"rota: {ori}-{dst}\n"
            f"url_pedida: {url}\n"
            f"url_final:  {page.url}\n"
            f"titulo: {titulo}\n"
            f"tamanho_texto: {len(text)} chars\n"
            f"bloqueado (heuristica): {dados['bloqueado']}\n"
            f"parse: mais_barato={dados['mais_barato']} direto={dados['direto']} "
            f"companhias={list(dados['por_companhia'].keys())} "
            f"vendedores={list(dados['vendedores'].keys())}\n"
            f"=== PRIMEIROS 8000 CHARS DO TEXTO DA PAGINA ===\n"
            f"{text[:8000]}\n"
        )
    return dados


def main():
    agora = dt.datetime.now(TZ)
    embarque = (agora + dt.timedelta(days=ANTECEDENCIA_DIAS)).date()

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
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        for ori, dst in ROTAS:
            rota, status, dados, flag = f"{ori}-{dst}", "ok", None, None
            try:
                dados = coleta_rota(page, ori, dst, embarque, debug_sink)
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
                "url_final": (dados or {}).get("url_final"),
                "status": status, "flag_auditoria": flag,
            })
            page.wait_for_timeout(4000)
        browser.close()

    # junta, aplica retencao e salva
    corte = (agora - dt.timedelta(days=RETENCAO_DIAS)).isoformat()
    todas = [c for c in hist.get("coletas", []) if c["coletado_em"] >= corte] + novas
    os.makedirs(os.path.dirname(ARQ), exist_ok=True)
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump({
            "atualizado_em": agora.isoformat(),
            "regra": f"embarque sempre D+{ANTECEDENCIA_DIAS}; retencao {RETENCAO_DIAS}d",
            "coletas": todas,
        }, f, ensure_ascii=False, indent=1)

    # grava o dump de debug (sempre), para diagnostico do que o site devolveu
    with open(ARQ_DEBUG, "w", encoding="utf-8") as f:
        f.write(debug_sink.get("conteudo") or "sem conteudo de debug capturado")

    ok = sum(1 for c in novas if c["status"] == "ok")
    print(f"Coleta {agora.isoformat()}: {ok}/{len(novas)} rotas ok. "
          f"Embarque: {embarque}. Total historico: {len(todas)} linhas.")
    # v2: NAO sai com erro quando bloqueia - grava tudo (JSON + debug) para auditoria.
    # O status por rota ('bloqueado_ou_vazio') sinaliza a saude no proprio JSON.


if __name__ == "__main__":
    main()
