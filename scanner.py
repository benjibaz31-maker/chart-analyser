#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║   ChartAnalyzer Scanner Autonome — XAU/EUR H1 + H4          ║
║   Toutes les 15 min via GitHub Actions                       ║
║   Signal BUY/SELL → Email automatique                        ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, sys, json, base64, smtplib, time, traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from io import BytesIO

import requests
import numpy as np
import pandas as pd
import yfinance as yf

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import Rectangle
import matplotlib.dates as mdates

# ─────────────────────────────────────────────────────────────
#  CONFIG — variables d'environnement (GitHub Secrets)
# ─────────────────────────────────────────────────────────────
PAIR        = os.getenv("PAIR",        "XAU/EUR")
GROQ_KEY    = os.getenv("GROQ_KEY",   "")
EMAIL_FROM  = os.getenv("EMAIL_FROM", "")
EMAIL_PASS  = os.getenv("EMAIL_PASS", "")
EMAIL_TO    = os.getenv("EMAIL_TO",   "")
MIN_SCORE   = int(os.getenv("MIN_SCORE",  "65"))
BALANCE     = float(os.getenv("BALANCE", "1000"))

# Mapping paire → ticker Yahoo Finance
YAHOO_SYMBOLS = {
    "XAU/EUR": "XAUEUR=X",
    "XAU/USD": "XAUUSD=X",
    "XAU/GBP": "XAUGBP=X",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
}

MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

TIMEFRAMES = ["1h", "4h"]
TF_LABELS  = {"1h": "H1", "4h": "H4"}
CANDLES    = {"1h": 70, "4h": 55}

# ─────────────────────────────────────────────────────────────
#  1. VÉRIFICATION HEURES DE MARCHÉ
# ─────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    """XAU/EUR : ouvert Dim 22h UTC → Ven 21h UTC"""
    now = datetime.now(timezone.utc)
    wd  = now.weekday()   # 0=Lun … 6=Dim
    h   = now.hour

    # Samedi : fermé toute la journée
    if wd == 5:
        return False
    # Dimanche : ouvert après 22h UTC
    if wd == 6:
        return h >= 22
    # Vendredi : fermé après 21h UTC
    if wd == 4:
        return h < 21
    # Lun-Jeu : toujours ouvert
    return True

# ─────────────────────────────────────────────────────────────
#  2. RÉCUPÉRATION DES DONNÉES — Twelve Data
# ─────────────────────────────────────────────────────────────
def fetch_ohlcv(pair: str, interval: str, n: int) -> pd.DataFrame:
    """
    Télécharge les bougies OHLCV via yfinance (Yahoo Finance, 100% gratuit, sans clé API).
    interval : "1h" ou "4h"
    """
    import yfinance as yf

    ticker = YAHOO_SYMBOLS.get(pair, pair.replace("/", "") + "=X")

    # Pour H1 : on prend 8 jours (>70 bougies H1)
    # Pour H4 : on prend des bougies 1h sur 30j puis on resamplons en 4h (~55 bougies)
    period     = "8d"  if interval == "1h" else "30d"
    yf_interval = "1h"  # toujours 1h, on resample pour H4

    print(f"    → Yahoo Finance : {ticker} {yf_interval} {period}")
    data = yf.download(ticker, period=period, interval=yf_interval,
                       auto_adjust=True, progress=False)

    if data.empty:
        raise ValueError(f"Aucune donnée Yahoo Finance pour {ticker}")

    # Aplatir les colonnes si MultiIndex
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    data = data.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume"
    })
    data.index.name = "date"
    data = data.reset_index()
    data["date"] = pd.to_datetime(data["date"], utc=True)
    data = data[["date", "open", "high", "low", "close"]].dropna()

    # Pour H4 : resample H1 → H4
    if interval == "4h":
        data = data.set_index("date")
        data = data.resample("4h").agg({
            "open":  "first",
            "high":  "max",
            "low":   "min",
            "close": "last",
        }).dropna().reset_index()

    data = data.sort_values("date").reset_index(drop=True)
    data = data.tail(n).reset_index(drop=True)
    return data

# ─────────────────────────────────────────────────────────────
#  3. CALCUL DES INDICATEURS
# ─────────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """RSI(14), MACD(12,26,9), MA50."""
    close = df["close"]

    # RSI
    delta  = close.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=13, min_periods=14).mean()
    avg_l  = loss.ewm(com=13, min_periods=14).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    # MA50
    df["ma50"] = close.rolling(50).mean()

    return df

# ─────────────────────────────────────────────────────────────
#  4. GÉNÉRATION DU GRAPHIQUE (style MT5 sombre)
# ─────────────────────────────────────────────────────────────
BG      = "#0b0b12"
GRID    = "#1a1a2e"
GREEN   = "#26a69a"
RED     = "#ef5350"
MA_COL  = "#ef4444"
MACD_G  = "#26a69a"
MACD_R  = "#ef5350"
RSI_COL = "#60a5fa"
TEXT    = "#e2e8f0"
TEXT2   = "#94a3b8"

def draw_candles(ax, df):
    for i, row in df.iterrows():
        col = GREEN if row["close"] >= row["open"] else RED
        ax.plot([i, i], [row["low"], row["high"]], color=col, linewidth=0.7, zorder=2)
        bot = min(row["open"], row["close"])
        h   = max(abs(row["close"] - row["open"]), row["close"] * 0.0001)
        rect = Rectangle((i - 0.4, bot), 0.8, h,
                          facecolor=col, edgecolor=col, linewidth=0, zorder=3)
        ax.add_patch(rect)

def style_ax(ax, label="", ylabel="", show_x=False):
    ax.set_facecolor(BG)
    ax.tick_params(colors=TEXT2, labelsize=7)
    ax.spines[:].set_color(GRID)
    ax.yaxis.set_label_position("right")
    ax.yaxis.tick_right()
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)
    ax.grid(True, color=GRID, linewidth=0.4, linestyle="--", alpha=0.6)
    if label:
        ax.text(0.01, 0.97, label, transform=ax.transAxes,
                color=TEXT2, fontsize=7, va="top", ha="left")
    if ylabel:
        ax.set_ylabel(ylabel, color=TEXT2, fontsize=7, rotation=0, labelpad=28, va="center")
    if not show_x:
        ax.tick_params(labelbottom=False)

def generate_chart(df: pd.DataFrame, pair: str, tf: str) -> bytes:
    n   = len(df)
    idx = np.arange(n)

    fig = plt.figure(figsize=(14, 7), facecolor=BG)
    gs  = gridspec.GridSpec(3, 1, figure=fig,
                            height_ratios=[3, 1, 1],
                            hspace=0.04,
                            left=0.02, right=0.88,
                            top=0.93, bottom=0.07)

    ax1 = fig.add_subplot(gs[0])   # Prix + MA50
    ax2 = fig.add_subplot(gs[1])   # MACD
    ax3 = fig.add_subplot(gs[2])   # RSI

    # ── Panneau prix ──
    draw_candles(ax1, df)
    ax1.plot(idx, df["ma50"], color=MA_COL, linewidth=1.2, label="MA50", zorder=4)
    style_ax(ax1, label=f"{pair}  {TF_LABELS[tf]}")
    ax1.set_xlim(-1, n + 1)
    ax1.autoscale(axis="y")

    # Légende MA50
    last_ma = df["ma50"].iloc[-1]
    ax1.annotate(f"MA50 {last_ma:.2f}",
                 xy=(n - 1, last_ma), xytext=(n + 0.5, last_ma),
                 color=MA_COL, fontsize=6.5, va="center",
                 arrowprops=dict(arrowstyle="-", color=MA_COL, lw=0.5))

    # Titre
    ax1.set_title(f"  {pair}  ·  {TF_LABELS[tf]}  ·  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
                  color=TEXT, fontsize=8.5, loc="left", pad=6)

    # ── Panneau MACD ──
    hist   = df["macd_hist"].values
    colors = [MACD_G if v >= 0 else MACD_R for v in hist]
    ax2.bar(idx, hist, color=colors, width=0.7, zorder=3)
    ax2.plot(idx, df["macd"],        color="#818cf8", linewidth=0.9, zorder=4)
    ax2.plot(idx, df["macd_signal"], color="#f59e0b", linewidth=0.9, zorder=4)
    ax2.axhline(0, color=GRID, linewidth=0.6)
    last_h = hist[-1]; last_sig = df["macd_signal"].iloc[-1]
    style_ax(ax2, label=f"MACD(12,26,9)  {last_h:.3f}  {last_sig:.3f}")
    ax2.set_xlim(-1, n + 1)

    # ── Panneau RSI ──
    ax3.plot(idx, df["rsi"], color=RSI_COL, linewidth=1.0, zorder=4)
    ax3.axhline(70, color=RED,   linewidth=0.5, linestyle="--", alpha=0.6)
    ax3.axhline(30, color=GREEN, linewidth=0.5, linestyle="--", alpha=0.6)
    ax3.axhline(50, color=GRID,  linewidth=0.4, alpha=0.5)
    ax3.fill_between(idx, df["rsi"], 70, where=df["rsi"] >= 70,
                     alpha=0.15, color=RED)
    ax3.fill_between(idx, df["rsi"], 30, where=df["rsi"] <= 30,
                     alpha=0.15, color=GREEN)
    ax3.set_ylim(0, 100)
    last_rsi = df["rsi"].iloc[-1]
    style_ax(ax3, label=f"RSI(14)  {last_rsi:.2f}", show_x=True)
    ax3.set_xlim(-1, n + 1)

    # Axe X — dates
    step    = max(1, n // 10)
    xticks  = idx[::step]
    xlabels = [df["date"].iloc[i].strftime("%d/%m %Hh") for i in xticks]
    ax3.set_xticks(xticks)
    ax3.set_xticklabels(xlabels, rotation=30, ha="right", fontsize=6, color=TEXT2)

    # Sauvegarde en mémoire
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ─────────────────────────────────────────────────────────────
#  5. APPEL API GROQ (vision)
# ─────────────────────────────────────────────────────────────
def call_groq(img_bytes: bytes, df: pd.DataFrame, pair: str, tf: str) -> dict:
    """Analyse le graphique via Groq Llama 4 Vision."""

    last    = df.iloc[-1]
    rsi_val = df["rsi"].iloc[-1]
    macd_h  = df["macd_hist"].iloc[-5:]
    macd_dir = "haussier" if macd_h.iloc[-1] > macd_h.iloc[-2] else "baissier"
    macd_color = "vert" if df["macd_hist"].iloc[-1] >= 0 else "rouge"
    ma50_val  = df["ma50"].iloc[-1]
    above_ma  = last["close"] > ma50_val

    risk_eur = BALANCE * 0.01
    b64 = base64.b64encode(img_bytes).decode()

    sys_prompt = f"""Tu es un analyste technique senior spécialisé RSI+MACD+MA50. Analyse ce graphique {pair} {TF_LABELS[tf]} avec précision chirurgicale.

═══ STRATÉGIE RSI+MACD+MA50 ═══

🟢 BUY : RSI < 50 ET montant + MACD histogramme passe rouge→vert (1-3 dernières bougies) + prix AU-DESSUS MA50
🔴 SELL : RSI > 50 ET descendant + MACD histogramme passe vert→rouge (1-3 dernières bougies) + prix EN-DESSOUS MA50
⏸ WAIT : moins de 2 conditions remplies

═══ DONNÉES CALCULÉES (Python) ═══
• Paire : {pair}  Timeframe : {TF_LABELS[tf]}
• Prix actuel (close) : {last['close']:.2f}
• RSI(14) : {rsi_val:.2f}
• MACD histogramme dernière bougie : {df['macd_hist'].iloc[-1]:.4f} ({macd_color})
• MACD direction : {macd_dir}
• MA50 : {ma50_val:.2f}
• Prix {'AU-DESSUS' if above_ma else 'EN-DESSOUS'} de la MA50
• Solde : {BALANCE}€  Risque 1% : {risk_eur:.2f}€

═══ RÈGLES XAU OBLIGATOIRES ═══
• sl_pips et tp_pips = DOLLARS (ex: "12" = 12$ de SL)
• SL typique H1: 10-20$  H4: 20-40$
• Prix entree/sl/tp avec 2 décimales (ex: 4412.30)
• lot_micro = ({risk_eur:.2f} / (sl_pips × 100)) arrondi à 0.01

RÉPONDS UNIQUEMENT EN JSON VALIDE SANS MARKDOWN :
{{"signal":"BUY|SELL|WAIT","score":0-100,"confiance":{{"niveau":"faible|moyen|élevé","raison":"..."}},"tendance":{{"direction":"haussière|baissière|latérale","force":"faible|modérée|forte","description":"2 phrases max"}},"rsi":{{"valeur":{rsi_val:.2f},"zone":"survente|neutre|surachat","tendance":"montant|descendant|stable"}},"macd":{{"etat":"haussier|baissier|neutre","bougies_depuis":0,"divergence":false}},"ma50":{{"position":"au-dessus|en-dessous|proche","condition":true}},"supports_resistances":{{"resistances":["R1: niveau","R2: niveau"],"supports":["S1: niveau","S2: niveau"],"zone_cle":"..."}},"sltp":{{"entree":"valeur","sl":"valeur","sl_pips":"valeur","tp":"valeur","tp_pips":"valeur","rr":"1:2","lot_micro":"valeur"}},"forces":"• Force 1\\n• Force 2","faiblesses":"• Risque 1\\n• Risque 2","analyse":"3 phrases max","scenario_alternatif":"niveau d'invalidation précis","probabilite_signal":"XX% - justification courte"}}"""

    payload = {
        "model": MODEL,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Analyse ce graphique {pair} {TF_LABELS[tf]} avec précision. "
                         f"RSI={rsi_val:.2f}, MACD={'vert' if df['macd_hist'].iloc[-1]>=0 else 'rouge'}, "
                         f"Prix {'>' if above_ma else '<'} MA50({ma50_val:.2f}). Donne BUY/SELL/WAIT en JSON."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]}
        ]
    }

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type":  "application/json"
    }

    for attempt in range(3):
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"]
            clean = raw.replace("```json", "").replace("```", "").strip()
            import re
            m = re.search(r'\{[\s\S]*\}', clean)
            return json.loads(m.group(0) if m else clean)
        except Exception as e:
            print(f"  ⚠ Groq tentative {attempt+1}/3 : {e}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return {}

# ─────────────────────────────────────────────────────────────
#  6. LOGIQUE DE CONSENSUS H1 + H4
# ─────────────────────────────────────────────────────────────
def evaluate_consensus(results: dict) -> dict | None:
    """
    Retourne un signal consolidé si :
    - H1 et H4 sont d'accord (BUY/BUY ou SELL/SELL)
    - Score H1 >= MIN_SCORE
    - Signal n'est pas WAIT
    """
    r1 = results.get("1h", {})
    r4 = results.get("4h", {})

    sig1 = r1.get("signal", "WAIT")
    sig4 = r4.get("signal", "WAIT")
    sc1  = int(r1.get("score", 0))
    sc4  = int(r4.get("score", 0))

    print(f"\n  H1 → {sig1} ({sc1}/100)  |  H4 → {sig4} ({sc4}/100)")

    # Consensus strict : même direction, score H1 suffisant
    if sig1 == sig4 and sig1 in ("BUY", "SELL") and sc1 >= MIN_SCORE:
        return {
            "signal":   sig1,
            "score_h1": sc1,
            "score_h4": sc4,
            "r_h1":     r1,
            "r_h4":     r4
        }

    # Consensus partiel : H4 neutre mais H1 très fort
    if sig1 in ("BUY", "SELL") and sig4 in ("WAIT", sig1) and sc1 >= MIN_SCORE + 10:
        return {
            "signal":   sig1,
            "score_h1": sc1,
            "score_h4": sc4,
            "partial":  True,
            "r_h1":     r1,
            "r_h4":     r4
        }

    return None

# ─────────────────────────────────────────────────────────────
#  7. EMAIL HTML
# ─────────────────────────────────────────────────────────────
def build_email(consensus: dict, charts: dict, pair: str) -> MIMEMultipart:
    sig     = consensus["signal"]
    r1      = consensus["r_h1"]
    r4      = consensus["r_h4"]
    sc1     = consensus["score_h1"]
    sc4     = consensus["score_h4"]
    partial = consensus.get("partial", False)
    sltp    = r1.get("sltp", {})
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    # Couleurs
    is_buy  = sig == "BUY"
    col_sig = "#2563EB" if is_buy else "#DC2626"
    col_bg  = "#EFF6FF" if is_buy else "#FEF2F2"
    ico_sig = "🟢" if is_buy else "🔴"
    arrow   = "▲" if is_buy else "▼"

    # Valeurs SLTP
    entree = sltp.get("entree", "N/A")
    sl     = sltp.get("sl",     "N/A")
    tp     = sltp.get("tp",     "N/A")
    rr     = sltp.get("rr",     "1:2")
    lot    = sltp.get("lot_micro", "N/A")
    sl_pips_val = sltp.get("sl_pips", "N/A")
    tp_pips_val = sltp.get("tp_pips", "N/A")
    prob   = r1.get("probabilite_signal", "N/A")
    inv    = r1.get("scenario_alternatif", "N/A")
    analyse= r1.get("analyse", "")
    forces = r1.get("forces",  "")
    risques= r1.get("faiblesses", "")

    # SR
    res_list = r1.get("supports_resistances", {}).get("resistances", [])
    sup_list = r1.get("supports_resistances", {}).get("supports", [])
    zone_cle = r1.get("supports_resistances", {}).get("zone_cle", "")

    # Tendances
    tend1 = r1.get("tendance", {}).get("direction", "—")
    tend4 = r4.get("tendance", {}).get("direction", "—")

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal {sig} — {pair}</title>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .wrap{{max-width:640px;margin:20px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10);}}
  .header{{background:{col_sig};padding:28px 30px 20px;color:#fff;}}
  .h-sig{{font-size:2.2rem;font-weight:900;letter-spacing:.04em;}}
  .h-pair{{font-size:1rem;opacity:.88;margin-top:2px;}}
  .h-date{{font-size:.78rem;opacity:.65;margin-top:6px;}}
  .body{{padding:24px 30px;}}
  .kpi-row{{display:flex;gap:10px;margin-bottom:18px;}}
  .kpi{{flex:1;background:#f8fafc;border-radius:10px;padding:12px 14px;border:1.5px solid #e2e8f0;text-align:center;}}
  .kpi-lbl{{font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;}}
  .kpi-val{{font-size:1.15rem;font-weight:800;color:#1e293b;}}
  .kpi-sub{{font-size:.65rem;color:#94a3b8;margin-top:2px;}}
  .kpi.k-sl .kpi-val{{color:#DC2626;}}
  .kpi.k-tp .kpi-val{{color:#16a34a;}}
  .kpi.k-rr .kpi-val{{color:#7c3aed;}}
  .section{{margin-bottom:18px;}}
  .sec-title{{font-size:.72rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px;border-bottom:1px solid #e2e8f0;padding-bottom:5px;}}
  .score-bar{{display:flex;align-items:center;gap:10px;margin-bottom:6px;}}
  .score-lbl{{font-size:.72rem;color:#64748b;width:36px;}}
  .bar-bg{{flex:1;height:8px;background:#f1f5f9;border-radius:4px;overflow:hidden;}}
  .bar-fill{{height:100%;border-radius:4px;transition:width .3s;}}
  .score-num{{font-size:.72rem;font-weight:700;color:#1e293b;width:44px;text-align:right;}}
  .alert{{padding:12px 14px;border-radius:8px;font-size:.78rem;line-height:1.6;margin-bottom:10px;}}
  .alert-warn{{background:#fffbeb;border:1px solid #fbbf24;color:#92400e;}}
  .alert-info{{background:#eff6ff;border:1px solid #93c5fd;color:#1e40af;}}
  .alert-danger{{background:#fef2f2;border:1px solid #fca5a5;color:#991b1b;}}
  .sr-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
  .sr-col-title{{font-size:.65rem;font-weight:700;margin-bottom:6px;}}
  .sr-item{{font-size:.72rem;padding:5px 8px;border-radius:5px;margin-bottom:3px;}}
  .sr-r{{background:#fef2f2;color:#dc2626;border-left:3px solid #dc2626;}}
  .sr-s{{background:#f0fdf4;color:#16a34a;border-left:3px solid #16a34a;}}
  .2col{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
  .force-box{{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px;}}
  .risk-box{{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px;}}
  .box-title{{font-size:.65rem;font-weight:700;text-transform:uppercase;margin-bottom:6px;}}
  .box-text{{font-size:.72rem;line-height:1.7;white-space:pre-line;}}
  .chart-img{{width:100%;border-radius:10px;margin-bottom:10px;display:block;border:1px solid #e2e8f0;}}
  .footer{{background:#f8fafc;padding:14px 30px;font-size:.65rem;color:#94a3b8;text-align:center;border-top:1px solid #e2e8f0;}}
  .partial-warn{{background:#fffbeb;border:1px solid #f59e0b;border-radius:8px;padding:10px 14px;font-size:.73rem;color:#92400e;margin-bottom:14px;}}
  .tf-row{{display:flex;gap:8px;margin-bottom:14px;}}
  .tf-badge{{padding:5px 12px;border-radius:20px;font-size:.72rem;font-weight:700;}}
  .tf-buy{{background:#dbeafe;color:#1d4ed8;}}
  .tf-sell{{background:#fee2e2;color:#b91c1c;}}
  .tf-wait{{background:#f3f4f6;color:#6b7280;}}
</style>
</head>
<body>
<div class="wrap">

  <!-- HEADER -->
  <div class="header">
    <div class="h-sig">{ico_sig} {sig} {arrow} &nbsp; {sc1}/100</div>
    <div class="h-pair">📊 {pair} · Analyse H1 + H4 · Score confirmé</div>
    <div class="h-date">🕐 {now_str}{'  ·  ⚠️ H4 non confirmé (signal H1 fort)' if partial else '  ·  ✅ Consensus H1 + H4'}</div>
  </div>

  <div class="body">

    {'<div class="partial-warn">⚠️ <b>Signal partiel</b> : H4 en WAIT. Signal basé sur H1 fort uniquement. Réduire la taille de position de 50%.</div>' if partial else ''}

    <!-- SCORES TF -->
    <div class="section">
      <div class="sec-title">Scores par timeframe</div>
      <div class="tf-row">
        <span class="tf-badge {'tf-buy' if r1.get('signal')=='BUY' else 'tf-sell' if r1.get('signal')=='SELL' else 'tf-wait'}">H1 : {sig} {sc1}/100</span>
        <span class="tf-badge {'tf-buy' if r4.get('signal')=='BUY' else 'tf-sell' if r4.get('signal')=='SELL' else 'tf-wait'}">H4 : {r4.get('signal','WAIT')} {sc4}/100</span>
      </div>
      <div class="score-bar">
        <span class="score-lbl">H1</span>
        <div class="bar-bg"><div class="bar-fill" style="width:{sc1}%;background:{col_sig}"></div></div>
        <span class="score-num">{sc1}/100</span>
      </div>
      <div class="score-bar">
        <span class="score-lbl">H4</span>
        <div class="bar-bg"><div class="bar-fill" style="width:{sc4}%;background:{'#7c3aed'}"></div></div>
        <span class="score-num">{sc4}/100</span>
      </div>
    </div>

    <!-- KPI SLTP -->
    <div class="section">
      <div class="sec-title">Niveaux de trade</div>
      <div class="kpi-row">
        <div class="kpi">
          <div class="kpi-lbl">Entrée</div>
          <div class="kpi-val">{entree}€</div>
          <div class="kpi-sub">Prix actuel</div>
        </div>
        <div class="kpi k-sl">
          <div class="kpi-lbl">⛔ Stop Loss</div>
          <div class="kpi-val">{sl}€</div>
          <div class="kpi-sub">−{sl_pips_val}$</div>
        </div>
        <div class="kpi k-tp">
          <div class="kpi-lbl">🎯 Take Profit</div>
          <div class="kpi-val">{tp}€</div>
          <div class="kpi-sub">+{tp_pips_val}$</div>
        </div>
        <div class="kpi k-rr">
          <div class="kpi-lbl">R:R</div>
          <div class="kpi-val">{rr}</div>
          <div class="kpi-sub">Lot: {lot}</div>
        </div>
      </div>
    </div>

    <!-- INDICATEURS -->
    <div class="section">
      <div class="sec-title">Indicateurs H1</div>
      <div class="alert alert-info">
        <b>RSI(14)</b> : {r1.get('rsi',{{}}).get('valeur','—')} — {r1.get('rsi',{{}}).get('zone','—')} — {r1.get('rsi',{{}}).get('tendance','—')}<br>
        <b>MACD</b> : {r1.get('macd',{{}}).get('etat','—')} depuis {r1.get('macd',{{}}).get('bougies_depuis',0)} bougie(s)<br>
        <b>MA50</b> : Prix {r1.get('ma50',{{}}).get('position','—')} — condition {'✅' if r1.get('ma50',{{}}).get('condition') else '❌'}<br>
        <b>Tendance H1</b> : {tend1} &nbsp;|&nbsp; <b>Tendance H4</b> : {tend4}
      </div>
    </div>

    <!-- S/R -->
    <div class="section">
      <div class="sec-title">Supports &amp; Résistances</div>
      {'<div style="font-size:.72rem;color:#64748b;margin-bottom:8px;">' + zone_cle + '</div>' if zone_cle else ''}
      <div class="sr-grid">
        <div>
          <div class="sr-col-title" style="color:#dc2626">▼ Résistances</div>
          {''.join(f'<div class="sr-item sr-r">{l}</div>' for l in res_list) or '<div style="font-size:.7rem;color:#94a3b8">—</div>'}
        </div>
        <div>
          <div class="sr-col-title" style="color:#16a34a">▲ Supports</div>
          {''.join(f'<div class="sr-item sr-s">{l}</div>' for l in sup_list) or '<div style="font-size:.7rem;color:#94a3b8">—</div>'}
        </div>
      </div>
    </div>

    <!-- ANALYSE -->
    <div class="section">
      <div class="sec-title">Analyse H1</div>
      <div class="alert alert-info">{analyse}</div>
    </div>

    <!-- PROBABILITE + INVALIDATION -->
    <div class="section">
      <div class="alert {'alert-info' if int((prob or '0').split('%')[0] or 0) >= 60 else 'alert-warn'}">
        <b>📊 Probabilité signal :</b> {prob}
      </div>
      <div class="alert alert-danger">
        <b>⚠️ Scénario d'invalidation :</b> {inv}
      </div>
    </div>

    <!-- FORCES / RISQUES -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:18px;">
      <div class="force-box">
        <div class="box-title" style="color:#16a34a">✅ Points forts</div>
        <div class="box-text" style="color:#166534;font-size:.70rem;">{forces}</div>
      </div>
      <div class="risk-box">
        <div class="box-title" style="color:#d97706">⚠️ Risques</div>
        <div class="box-text" style="color:#92400e;font-size:.70rem;">{risques}</div>
      </div>
    </div>

    <!-- GRAPHIQUES -->
    <div class="section">
      <div class="sec-title">Graphiques</div>
      <img src="cid:chart_h1" class="chart-img" alt="Graphique H1">
      <img src="cid:chart_h4" class="chart-img" alt="Graphique H4">
    </div>

    <!-- RAPPEL RISQUE -->
    <div class="alert alert-warn">
      ⚠️ <b>Toujours vérifier avant d'entrer en position.</b> Ce signal est automatisé et peut contenir des erreurs.
      Ne jamais risquer plus de 1% du capital par trade. Lot suggéré : <b>{lot}</b>.
    </div>

  </div><!-- /body -->

  <div class="footer">
    ChartAnalyzer Scanner Autonome · {pair} · {now_str}<br>
    Score H1={sc1}/100 · Score H4={sc4}/100 · Seuil déclenchement={MIN_SCORE}/100
  </div>
</div>
</body>
</html>"""

    msg = MIMEMultipart("related")
    msg["Subject"] = f"{ico_sig} [{sig}] {pair} — Score {sc1}/100 · H1+H4 · {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    # Attacher les images inline
    for tf, cid in [("1h", "chart_h1"), ("4h", "chart_h4")]:
        if tf in charts:
            img = MIMEImage(charts[tf], _subtype="png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline",
                           filename=f"chart_{tf}.png")
            msg.attach(img)

    return msg

# ─────────────────────────────────────────────────────────────
#  8. ENVOI EMAIL (Gmail SMTP)
# ─────────────────────────────────────────────────────────────
def send_email(msg: MIMEMultipart):
    print(f"  📧 Envoi email à {EMAIL_TO}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_PASS)
        server.send_message(msg)
    print("  ✅ Email envoyé !")

# ─────────────────────────────────────────────────────────────
#  9. EMAIL DE TEST / MARCHÉ FERMÉ
# ─────────────────────────────────────────────────────────────
def send_status_email(subject: str, body: str):
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.send_message(msg)
    except Exception as e:
        print(f"  ⚠ Impossible d'envoyer l'email de statut : {e}")

# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*60)
    print(f"  ChartAnalyzer Scanner Autonome")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("═"*60)

    # Vérification config
    missing = [k for k,v in [
        ("GROQ_KEY",   GROQ_KEY),
        ("EMAIL_FROM", EMAIL_FROM),
        ("EMAIL_PASS", EMAIL_PASS),
        ("EMAIL_TO",   EMAIL_TO)
    ] if not v]
    if missing:
        print(f"❌ Variables manquantes : {', '.join(missing)}")
        sys.exit(1)

    # Marché ouvert ?
    if not is_market_open():
        print("📴 Marché fermé — pas d'analyse")
        sys.exit(0)

    print(f"\n📡 Paire : {PAIR}  |  Score min : {MIN_SCORE}/100  |  Capital : {BALANCE}€")

    results = {}
    charts  = {}

    for tf in TIMEFRAMES:
        tf_label = TF_LABELS[tf]
        print(f"\n── {tf_label} ──────────────────────────────────────")

        # Données
        print(f"  📥 Téléchargement {PAIR} {tf_label}...")
        try:
            df = fetch_ohlcv(PAIR, tf, CANDLES[tf])
        except Exception as e:
            print(f"  ❌ Erreur Twelve Data : {e}")
            continue

        df = compute_indicators(df)
        print(f"  ✅ {len(df)} bougies — Close={df['close'].iloc[-1]:.2f} RSI={df['rsi'].iloc[-1]:.2f}")

        # Graphique
        print(f"  🎨 Génération graphique {tf_label}...")
        try:
            img_bytes = generate_chart(df, PAIR, tf)
            charts[tf] = img_bytes
        except Exception as e:
            print(f"  ⚠ Erreur graphique : {e}")
            img_bytes = b""

        # Analyse Groq
        print(f"  🤖 Analyse Groq {tf_label}...")
        try:
            result = call_groq(img_bytes, df, PAIR, tf)
            results[tf] = result
            sig = result.get("signal", "?")
            sc  = result.get("score",  0)
            print(f"  → Signal : {sig} ({sc}/100)")
        except Exception as e:
            print(f"  ❌ Erreur Groq : {e}")
            traceback.print_exc()

        # Pause entre les calls API
        time.sleep(2)

    # Consensus
    print("\n── CONSENSUS ─────────────────────────────────────────")
    if len(results) < 2:
        print("  ❌ Données insuffisantes pour le consensus")
        sys.exit(0)

    consensus = evaluate_consensus(results)

    if consensus is None:
        sig1 = results.get("1h", {}).get("signal", "WAIT")
        sig4 = results.get("4h", {}).get("signal", "WAIT")
        sc1  = results.get("1h", {}).get("score", 0)
        print(f"  ⏸ Pas de signal — H1={sig1}({sc1}) H4={sig4} → En attente")
        sys.exit(0)

    print(f"\n  ✅ SIGNAL CONFIRMÉ : {consensus['signal']} "
          f"(H1={consensus['score_h1']}/100 · H4={consensus['score_h4']}/100)")

    # Construction et envoi email
    print("\n── EMAIL ─────────────────────────────────────────────")
    try:
        msg = build_email(consensus, charts, PAIR)
        send_email(msg)
    except Exception as e:
        print(f"  ❌ Erreur email : {e}")
        traceback.print_exc()
        sys.exit(1)

    print("\n" + "═"*60)
    print("  ✅ Scan terminé avec succès")
    print("═"*60 + "\n")

if __name__ == "__main__":
    main()
