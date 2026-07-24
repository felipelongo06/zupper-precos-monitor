# -*- coding: utf-8 -*-
"""
Monitor de preços Skyscanner — Zupper (versão GitHub Actions, sem GCP)
Roda 4x/dia via cron do Actions, pesquisa as top rotas (embarque sempre D+30)
e grava snapshots em data/precos.json (histórico rolante de 45 dias).
O dashboard lê o JSON via cdn.jsdelivr.net.
"""
import re
import json
import os
import datetime as dt
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ARQ = os.path.join(os.path.dirname(__file__), "data", "precos.json")
ANTECEDENCIA_DIAS = 30            # embarque pesquisado = hoje + 30 (regra fixa de comparabilidade)
RETENCAO_DIAS = 45                # histórico mantido no JSON
TZ = ZoneInfo("America/Sao_Paulo")
TIMEOUT_MS = 45000
ANOMALIA_PCT = 60

ROTAS = [
    ("SSA", "CGH"), ("CGH", "SSA"),
    ("GRU", "SSA"), ("SSA", "GRU"),
    ("GIG", "SSA"), ("SSA", "GIG"),
    ("REC", "CGH"), ("CGH", "REC"),
    ("GRU", "JDO"), ("FOR", "GRU"),
]

PRECO_RE = re.compile(r"R\$\s?([\d.]+)")


def parse_page_text(text):
    out = {"mais_barato": None, "direto": None, "por_companhia": {}, "vendedores": {}, "zupper_vitrine": None}
    m = re.search(r"Mais barato\s*\n?\s*R\$\s?([\d.]+)", text)
    if m: out["mais_barato"] = int(m.group(1).replace(".", ""))
    m = re.search(r"Direto\s*\n?\s*a partir de R\$\s?([\d.]+)", text)
    if m: out["direto"] = int(m.group(1).replace(".", ""))
    for cia in ["GOL Linhas Aéreas", "LATAM Airlines", "Azul", "Voepass", "Aerolíneas Argentinas"]:
        m = re.search(re.escape(cia) + r"\s*\n?\s*a partir de R\$\s?([\d.]+)", text)
        if m: out["por_companhia"][cia] = int(m.group(1).replace(".", ""))
    for m in re.finditer(r"Reserve com a ([^\n]+?) a partir de\s*\n?\s*R\$\s?([\d.]+)", text):
        vend, preco = m.group(1).strip(), int(m.group(2).replace(".", ""))
        prev = out["vendedores"].get(vend)
        out["vendedores"][vend] = min(prev, preco) if prev else preco
    m = re.search(r"Zupper[^\n]*?\s*R\$\s?([\d.]+)", text)
    if m: out["zupper_vitrine"] = int(m.group(1).replace(".", ""))
    return out


def coleta_rota(page, ori, dst, data_embarque):
    url = (f"https://www.skyscanner.com.br/transporte/voos/{ori.lower()}/{dst.lower()}/"
           f"{data_embarque.strftime('%y%m%d')}/?adultsv2=1&cabinclass=economy&rtn=0")
    page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
    page.wait_for_timeout(12000)
    try:
        page.wait_for_selector("text=Mais barato", timeout=15000)
    except Exception:
        pass
    text = page.inner_text("main")
    dados = parse_page_text(text)
    dados["url"] = url
    low = text.lower()
    dados["bloqueado"] = ("captcha" in low or "are you a person" in low or dados["mais_barato"] is None)
    return dados


def main():
    agora = dt.datetime.now(TZ)
    embarque = (agora + dt.timedelta(days=ANTECEDENCIA_DIAS)).date()

    # carrega histórico existente
    hist = {"coletas": []}
    if os.path.exists(ARQ):
        with open(ARQ, encoding="utf-8") as f:
            hist = json.load(f)

    # última coleta por rota (para flag de anomalia)
    ultimo = {}
    for c in hist["coletas"]:
        if c.get("preco_mais_barato"):
            ultimo[c["rota"]] = c["preco_mais_barato"]

    novas = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="pt-BR",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        for ori, dst in ROTAS:
            rota, status, dados, flag = f"{ori}-{dst}", "ok", None, None
            try:
                dados = coleta_rota(page, ori, dst, embarque)
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
            page.wait_for_timeout(4000)
        browser.close()

    # junta, aplica retenção e salva
    corte = (agora - dt.timedelta(days=RETENCAO_DIAS)).isoformat()
    todas = [c for c in hist["coletas"] if c["coletado_em"] >= corte] + novas
    os.makedirs(os.path.dirname(ARQ), exist_ok=True)
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump({
            "atualizado_em": agora.isoformat(),
            "regra": f"embarque sempre D+{ANTECEDENCIA_DIAS}; retencao {RETENCAO_DIAS}d",
            "coletas": todas,
        }, f, ensure_ascii=False, indent=1)

    ok = sum(1 for c in novas if c["status"] == "ok")
    print(f"Coleta {agora.isoformat()}: {ok}/{len(novas)} rotas ok. Embarque: {embarque}. Total histórico: {len(todas)} linhas.")
    if ok == 0:
        raise SystemExit(1)  # falha visível no Actions quando nada foi capturado


if __name__ == "__main__":
    main()
