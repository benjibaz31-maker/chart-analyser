#!/usr/bin/env python3
"""
ChartAnalyzer Scanner Autonome v2
Multi-paires + M15+H1+H4 + Anti-spam + Graphiques annotés SL/TP
"""

import os, sys, json, base64, smtplib, time, traceback, re
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from io import BytesIO

import requests
import numpy as np
import performance_engine as perf_engine

import pandas as pd
import yfinance as yf

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import Rectangle

PAIRS_RAW = os.getenv("PAIRS", "XAU/EUR,XAU/USD")
PAIRS     = [p.strip() for p in PAIRS_RAW.split(",") if p.strip()]
GROQ_KEY  = os.getenv("GROQ_KEY",   "")
EMAIL_FROM= os.getenv("EMAIL_FROM", "")
EMAIL_PASS= os.getenv("EMAIL_PASS", "")
EMAIL_TO  = os.getenv("EMAIL_TO",   "")
MIN_SCORE = int(os.getenv("MIN_SCORE", "55"))
BALANCE   = float(os.getenv("BALANCE","1000"))
GH_TOKEN  = os.getenv("GITHUB_TOKEN","")
GH_REPO   = os.getenv("GITHUB_REPOSITORY","")
SMTP_LOGIN= os.getenv("SMTP_LOGIN", "")
TEST_EMAIL= os.getenv("TEST_EMAIL","false").lower() == "true"
MODEL     = "meta-llama/llama-4-scout-17b-16e-instruct"
TIMEFRAMES= ["15m","1h","4h"]
TF_LABELS = {"15m":"M15","1h":"H1","4h":"H4"}
CANDLES   = {"15m":80,"1h":70,"4h":55}
BG="#0b0b12";GRID="#1a1a2e";GREEN="#26a69a";RED="#ef5350"
MA_COL="#ef4444";MACD_G="#26a69a";MACD_R="#ef5350";RSI_COL="#60a5fa"
TEXT="#e2e8f0";TEXT2="#94a3b8"

STATE_FILE="signals_state.json"

# ──────────────────────────────────────────────────────────────
#  ÉTAT / GITHUB
# ──────────────────────────────────────────────────────────────
def read_state():
    if not GH_TOKEN or not GH_REPO:
        return {},None
    url=f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"
    hdrs={"Authorization":f"token {GH_TOKEN}","Accept":"application/vnd.github.v3+json"}
    try:
        r=requests.get(url,headers=hdrs,timeout=10)
        if r.status_code==200:
            d=r.json()
            return json.loads(base64.b64decode(d["content"]).decode()),d["sha"]
        return {},None
    except Exception as e:
        print(f"  Lecture etat: {e}");return {},None

def write_state(state,sha):
    if not GH_TOKEN or not GH_REPO:return
    url=f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"
    hdrs={"Authorization":f"token {GH_TOKEN}","Accept":"application/vnd.github.v3+json"}
    content=base64.b64encode(json.dumps(state,indent=2).encode()).decode()
    body={"message":f"[bot] signals state {datetime.now(timezone.utc).strftime('%H:%M')}","content":content}
    if sha:body["sha"]=sha
    try:requests.put(url,headers=hdrs,json=body,timeout=10)
    except Exception as e:print(f"  Ecriture etat: {e}")

def write_signal_json(consensus, pair):
    if not GH_TOKEN or not GH_REPO:
        print("  ⚠ GITHUB_TOKEN manquant — signal.json non écrit")
        return
    sig   = consensus["signal"]
    r1    = consensus["r1"]
    sltp  = r1.get("sltp", {})
    now   = datetime.now(timezone.utc)
    pair_mt5 = pair.replace("/", "")
    signal = {
        "id":         now.strftime("%Y%m%d_%H%M%S") + "_" + pair_mt5 + "_" + sig,
        "pair":       pair_mt5,
        "action":     sig,
        "entry":      sltp.get("entree",    "0"),
        "sl":         sltp.get("sl",        "0"),
        "tp":         sltp.get("tp",        "0"),
        "sl_pips":    sltp.get("sl_pips",   "0"),
        "tp_pips":    sltp.get("tp_pips",   "0"),
        "lot":        float(sltp.get("lot_micro", "0.01") or "0.01"),
        "rr":         sltp.get("rr",        "1:2"),
        "score_h1":   consensus["score_h1"],
        "score_h4":   consensus["score_h4"],
        "m15_ok":     consensus["m15_ok"],
        "partial":    consensus["partial"],
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status":     "pending"
    }
    url     = f"https://api.github.com/repos/{GH_REPO}/contents/signal.json"
    headers = {"Authorization":f"token {GH_TOKEN}","Accept":"application/vnd.github.v3+json"}
    sha = None
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    content = base64.b64encode(json.dumps(signal, indent=2).encode()).decode()
    body    = {"message": f"[signal] {sig} {pair} {now.strftime('%H:%M')} UTC","content": content}
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=body, timeout=10)
        if r.status_code in (200, 201):
            print(f"  ✅ signal.json écrit dans GitHub ({sig} {pair})")
        else:
            print(f"  ❌ Erreur écriture signal.json : {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"  ❌ Exception signal.json : {e}")

# ──────────────────────────────────────────────────────────────
#  ANTI-SPAM
# ──────────────────────────────────────────────────────────────
def already_signaled(state,pair,signal):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    k=f"{pair}_{today}"
    last=state.get(k,{})
    if last.get("signal")==signal:
        print(f"  Anti-spam: {pair} {signal} deja envoye aujourd hui a {last.get('sent_at','')}");return True
    return False

def mark_sent(state,pair,signal):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    k=f"{pair}_{today}"
    state[k]={"signal":signal,"sent_at":datetime.now(timezone.utc).strftime("%H:%M UTC")}
    cutoff=(datetime.now(timezone.utc)-timedelta(days=14)).strftime("%Y-%m-%d")
    return {k:v for k,v in state.items() if k.split("_")[-1]>=cutoff}

# ──────────────────────────────────────────────────────────────
#  MARCHÉ OUVERT
# ──────────────────────────────────────────────────────────────
def is_market_open():
    now=datetime.now(timezone.utc);wd=now.weekday();h=now.hour
    if wd==5:return False
    if wd==6:return h>=22
    if wd==4:return h<21
    return True

# ──────────────────────────────────────────────────────────────
#  DONNÉES OHLCV
# ──────────────────────────────────────────────────────────────
def _dl_yf(ticker,period,interval="1h"):
    data=yf.download(ticker,period=period,interval=interval,auto_adjust=True,progress=False)
    if data.empty:raise ValueError(f"Pas de donnees pour {ticker}")
    if isinstance(data.columns,pd.MultiIndex):data.columns=data.columns.get_level_values(0)
    data=data.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close"})
    data.index.name="date";data=data.reset_index()
    data["date"]=pd.to_datetime(data["date"],utc=True)
    return data[["date","open","high","low","close"]].dropna()

def fetch_ohlcv(pair,interval,n):
    yf_iv="15m" if interval=="15m" else "1h"
    period={"15m":"5d","1h":"8d","4h":"30d"}[interval]
    if pair.startswith("XAU"):
        quote=pair.split("/")[1]
        print(f"    GC=F{' / '+quote+'USD=X' if quote!='USD' else ''} [{yf_iv} {period}]")
        xau=_dl_yf("GC=F",period,yf_iv).set_index("date").sort_index()
        if quote=="USD":
            data=xau.reset_index()
        else:
            fx=_dl_yf(f"{quote}USD=X",period,yf_iv)[["date","close"]].rename(columns={"close":"fx"}).set_index("date").sort_index()
            merged=xau.join(fx,how="inner")
            if merged.empty:raise ValueError(f"Alignement impossible GC=F/{quote}USD")
            for c in ["open","high","low","close"]:merged[c]/=merged["fx"]
            data=merged[["open","high","low","close"]].reset_index()
    else:
        tmap={"EUR/USD":"EURUSD=X","GBP/USD":"GBPUSD=X","USD/JPY":"USDJPY=X","AUD/USD":"AUDUSD=X"}
        ticker=tmap.get(pair,pair.replace("/","")+"=X")
        print(f"    Yahoo: {ticker} [{yf_iv} {period}]")
        data=_dl_yf(ticker,period,yf_iv)
    if interval=="4h":
        data=data.set_index("date").resample("4h").agg({"open":"first","high":"max","low":"min","close":"last"}).dropna().reset_index()
    data=data.sort_values("date").reset_index(drop=True).tail(n).reset_index(drop=True)
    if data.empty:raise ValueError(f"DataFrame vide {pair} {interval}")
    print(f"    {len(data)} bougies — Close={data['close'].iloc[-1]:.2f}")
    return data

# ──────────────────────────────────────────────────────────────
#  INDICATEURS
# ──────────────────────────────────────────────────────────────
def compute_indicators(df):
    c=df["close"];d=c.diff();g=d.clip(lower=0);l=(-d).clip(lower=0)
    ag=g.ewm(com=13,min_periods=14).mean();al=l.ewm(com=13,min_periods=14).mean()
    df["rsi"]=100-(100/(1+ag/al.replace(0,float("nan"))))
    e12=c.ewm(span=12,adjust=False).mean();e26=c.ewm(span=26,adjust=False).mean()
    df["macd"]=e12-e26;df["macd_signal"]=df["macd"].ewm(span=9,adjust=False).mean()
    df["macd_hist"]=df["macd"]-df["macd_signal"];df["ma50"]=c.rolling(50).mean()
    df["ema200"]=c.ewm(span=200,adjust=False).mean()
    return df

# ──────────────────────────────────────────────────────────────
#  GRAPHIQUE ANNOTÉ SL/TP  ← FONCTION MANQUANTE RÉINTÉGRÉE
# ──────────────────────────────────────────────────────────────
def generate_chart(df, pair, tf, sltp=None, signal=None):
    """
    Génère un graphique candlestick annoté avec SL/TP.
    Retourne les bytes PNG.
    """
    n = min(60, len(df))
    df_plot = df.tail(n).reset_index(drop=True)

    fig = plt.figure(figsize=(12, 7), facecolor=BG)
    gs  = gridspec.GridSpec(3, 1, height_ratios=[3, 1, 1], hspace=0.04)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)

    for ax in [ax1, ax2, ax3]:
        ax.set_facecolor(BG)
        ax.tick_params(colors=TEXT2, labelsize=7)
        ax.spines[:].set_color(GRID)
        ax.grid(color=GRID, linewidth=0.5, alpha=0.6)

    # ── Bougies ──
    for i, row in df_plot.iterrows():
        color = GREEN if row["close"] >= row["open"] else RED
        ax1.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.8)
        rect = Rectangle((i - 0.3, min(row["open"], row["close"])),
                          0.6, abs(row["close"] - row["open"]),
                          facecolor=color, edgecolor=color, linewidth=0)
        ax1.add_patch(rect)

    # ── MA50 & EMA200 ──
    if "ma50" in df_plot.columns:
        ax1.plot(df_plot.index, df_plot["ma50"], color=MA_COL,
                 linewidth=1.2, label="MA50", alpha=0.85)
    if "ema200" in df_plot.columns:
        ax1.plot(df_plot.index, df_plot["ema200"], color="#f59e0b",
                 linewidth=1.0, label="EMA200", alpha=0.75, linestyle="--")

    # ── Lignes SL/TP ──
    if sltp and signal in ("BUY", "SELL"):
        try:
            sl_val = float(sltp.get("sl", 0))
            tp_val = float(sltp.get("tp", 0))
            entry  = float(sltp.get("entree", 0))
            if sl_val > 0:
                ax1.axhline(sl_val, color="#ef5350", linewidth=1.2,
                            linestyle="--", alpha=0.9, label=f"SL {sl_val}")
                ax1.text(n - 1, sl_val, f" SL {sl_val}", color="#ef5350",
                         fontsize=7, va="center")
            if tp_val > 0:
                ax1.axhline(tp_val, color="#26a69a", linewidth=1.2,
                            linestyle="--", alpha=0.9, label=f"TP {tp_val}")
                ax1.text(n - 1, tp_val, f" TP {tp_val}", color="#26a69a",
                         fontsize=7, va="center")
            if entry > 0:
                ax1.axhline(entry, color="#60a5fa", linewidth=1.0,
                            linestyle=":", alpha=0.8, label=f"Entrée {entry}")
        except Exception:
            pass

    # ── Titre ax1 ──
    sig_label = f" — {signal}" if signal else ""
    ax1.set_title(f"{pair} {TF_LABELS[tf]}{sig_label}",
                  color=TEXT, fontsize=10, pad=6, loc="left")
    ax1.legend(fontsize=6, loc="upper left",
               facecolor=BG, edgecolor=GRID, labelcolor=TEXT2)
    ax1.set_xlim(-1, n + 1)

    # ── MACD ──
    if "macd_hist" in df_plot.columns:
        colors_macd = [MACD_G if v >= 0 else MACD_R
                       for v in df_plot["macd_hist"]]
        ax2.bar(df_plot.index, df_plot["macd_hist"],
                color=colors_macd, width=0.7, alpha=0.85)
        if "macd" in df_plot.columns:
            ax2.plot(df_plot.index, df_plot["macd"],
                     color="#60a5fa", linewidth=0.9)
        if "macd_signal" in df_plot.columns:
            ax2.plot(df_plot.index, df_plot["macd_signal"],
                     color="#f59e0b", linewidth=0.9)
    ax2.set_ylabel("MACD", color=TEXT2, fontsize=7)
    ax2.axhline(0, color=GRID, linewidth=0.6)

    # ── RSI ──
    if "rsi" in df_plot.columns:
        ax3.plot(df_plot.index, df_plot["rsi"],
                 color=RSI_COL, linewidth=1.0)
        ax3.axhline(70, color=RED,   linewidth=0.6, linestyle="--", alpha=0.6)
        ax3.axhline(30, color=GREEN, linewidth=0.6, linestyle="--", alpha=0.6)
        ax3.fill_between(df_plot.index, df_plot["rsi"], 70,
                         where=(df_plot["rsi"] >= 70),
                         color=RED, alpha=0.12)
        ax3.fill_between(df_plot.index, df_plot["rsi"], 30,
                         where=(df_plot["rsi"] <= 30),
                         color=GREEN, alpha=0.12)
        ax3.set_ylim(0, 100)
    ax3.set_ylabel("RSI", color=TEXT2, fontsize=7)

    # ── Labels X (dates) ──
    if "date" in df_plot.columns:
        step = max(1, n // 8)
        ticks = range(0, n, step)
        labels = [df_plot["date"].iloc[i].strftime("%d/%m %H:%M")
                  for i in ticks]
        ax3.set_xticks(list(ticks))
        ax3.set_xticklabels(labels, rotation=25, ha="right",
                            fontsize=6, color=TEXT2)
    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)

    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=120,
                bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ──────────────────────────────────────────────────────────────
#  SCORING PYTHON
# ──────────────────────────────────────────────────────────────
def compute_score(df, pair, tf):
    last   = df.iloc[-1]
    prev   = df.iloc[-2]
    rsi    = df["rsi"].iloc[-1]
    rsi_p  = df["rsi"].iloc[-2]
    macd_h = df["macd_hist"].iloc[-1]
    macd_h2= df["macd_hist"].iloc[-2]
    macd_h3= df["macd_hist"].iloc[-3] if len(df)>2 else 0
    close  = last["close"]
    ma50   = last["ma50"]
    ema200 = last["ema200"] if not pd.isna(last["ema200"]) else ma50
    is_xau = pair.startswith("XAU")
    risk   = BALANCE * 0.01

    buy_pts = 0; buy_reasons = []
    if rsi < 50 and rsi > rsi_p:
        buy_pts += 15; buy_reasons.append(f"RSI={rsi:.1f}<50 montant")
    elif rsi < 45:
        buy_pts += 10; buy_reasons.append(f"RSI={rsi:.1f}<45")
    if macd_h > 0 and macd_h2 <= 0:
        buy_pts += 20; buy_reasons.append("MACD croisement haussier")
    elif macd_h > 0 and macd_h > macd_h2:
        buy_pts += 15; buy_reasons.append("MACD hist haussier croissant")
    elif macd_h > macd_h2 > macd_h3:
        buy_pts += 10; buy_reasons.append("MACD hist accelere haussier")
    if close > ma50:
        buy_pts += 20; buy_reasons.append(f"Close>{ma50:.2f}(MA50)")
    if close > ema200:
        buy_pts += 15; buy_reasons.append(f"Close>{ema200:.2f}(EMA200)")
    last3 = df.tail(3)
    if all(last3["close"].values > last3["open"].values):
        buy_pts += 10; buy_reasons.append("3 bougies vertes")
    if rsi_p < 30 and rsi > 30:
        buy_pts += 10; buy_reasons.append("RSI sort survente")

    sell_pts = 0; sell_reasons = []
    if rsi > 50 and rsi < rsi_p:
        sell_pts += 15; sell_reasons.append(f"RSI={rsi:.1f}>50 descendant")
    elif rsi > 55:
        sell_pts += 10; sell_reasons.append(f"RSI={rsi:.1f}>55")
    if macd_h < 0 and macd_h2 >= 0:
        sell_pts += 20; sell_reasons.append("MACD croisement baissier")
    elif macd_h < 0 and macd_h < macd_h2:
        sell_pts += 15; sell_reasons.append("MACD hist baissier croissant")
    elif macd_h < macd_h2 < macd_h3:
        sell_pts += 10; sell_reasons.append("MACD hist accelere baissier")
    if close < ma50:
        sell_pts += 20; sell_reasons.append(f"Close<{ma50:.2f}(MA50)")
    if close < ema200:
        sell_pts += 15; sell_reasons.append(f"Close<{ema200:.2f}(EMA200)")
    if all(last3["close"].values < last3["open"].values):
        sell_pts += 10; sell_reasons.append("3 bougies rouges")
    if rsi_p > 70 and rsi < 70:
        sell_pts += 10; sell_reasons.append("RSI sort surachat")

    if buy_pts > sell_pts and buy_pts >= 35:
        signal = "BUY"; score = min(100, buy_pts); reasons = buy_reasons
    elif sell_pts > buy_pts and sell_pts >= 35:
        signal = "SELL"; score = min(100, sell_pts); reasons = sell_reasons
    else:
        signal = "WAIT"
        score  = max(buy_pts, sell_pts)
        reasons = buy_reasons if buy_pts > sell_pts else sell_reasons

    print(f"    Score Python: {signal} {score}/100 | {' | '.join(reasons) if reasons else 'aucune condition'}")

    if is_xau:
        sl_dist = {"15m": 8, "1h": 15, "4h": 30}.get(tf, 15)
    else:
        sl_dist = {"15m": 20, "1h": 40, "4h": 80}.get(tf, 40)
    tp_dist = sl_dist * 2
    digits  = 2 if is_xau else 5

    if signal == "BUY":
        sl_price = round(close - sl_dist, digits)
        tp_price = round(close + tp_dist, digits)
    elif signal == "SELL":
        sl_price = round(close + sl_dist, digits)
        tp_price = round(close - tp_dist, digits)
    else:
        sl_price = round(close - sl_dist, digits)
        tp_price = round(close + tp_dist, digits)

    lot = max(0.01, round(risk / (sl_dist * (100 if is_xau else 10)), 2))

    rsi_zone  = "survente" if rsi < 30 else "surachat" if rsi > 70 else "neutre"
    rsi_trend = "montant" if rsi > rsi_p else "descendant" if rsi < rsi_p else "stable"
    macd_etat = "haussier" if macd_h > 0 else "baissier" if macd_h < 0 else "neutre"
    ma50_pos  = "au-dessus" if close > ma50 else "en-dessous"
    tendance  = "haussiere" if close > ema200 else "baissiere"

    return {
        "signal":  signal,
        "score":   score,
        "confiance": {"niveau": "eleve" if score >= 70 else "moyen" if score >= 50 else "faible",
                      "raison": " | ".join(reasons) if reasons else "aucune condition claire"},
        "tendance": {"direction": tendance, "force": "forte" if score >= 70 else "moderee",
                     "description": f"Prix {'au-dessus' if close>ema200 else 'en-dessous'} EMA200"},
        "rsi":    {"valeur": round(rsi, 2), "zone": rsi_zone, "tendance": rsi_trend},
        "macd":   {"etat": macd_etat, "bougies_depuis": 1},
        "ma50":   {"position": ma50_pos, "condition": close > ma50 if signal=="BUY" else close < ma50},
        "supports_resistances": {
            "resistances": [f"R1: {round(close*1.005,2)}", f"R2: {round(close*1.010,2)}"],
            "supports":    [f"S1: {round(close*0.995,2)}", f"S2: {round(close*0.990,2)}"]
        },
        "sltp": {
            "entree":    str(round(close, digits)),
            "sl":        str(sl_price),
            "sl_pips":   str(sl_dist),
            "tp":        str(tp_price),
            "tp_pips":   str(tp_dist),
            "rr":        "1:2",
            "lot_micro": str(lot)
        },
        "forces":   " | ".join(reasons[:3]) if reasons else "—",
        "faiblesses": "Marché sans tendance forte" if score < 55 else "Signal technique",
        "analyse":  f"{pair} {TF_LABELS[tf]}: {signal} score={score}/100. RSI={rsi:.1f} {rsi_zone}. MACD {macd_etat}. Prix {ma50_pos} MA50.",
        "scenario_alternatif": f"Invalidation si prix {'passe sous' if signal=='BUY' else 'passe au-dessus'} {sl_price}",
        "probabilite_signal":  f"{score}% — basé sur {len(reasons)} condition(s) technique(s)"
    }

# ──────────────────────────────────────────────────────────────
#  CALL GROQ (enrichissement optionnel)
# ──────────────────────────────────────────────────────────────
def call_groq(img_bytes, df, pair, tf):
    base_result = compute_score(df, pair, tf)
    signal = base_result["signal"]
    score  = base_result["score"]
    if not GROQ_KEY:
        return base_result

    last  = df.iloc[-1]
    rsi   = df["rsi"].iloc[-1]
    ma50  = last["ma50"]
    close = last["close"]
    macd_col = "vert" if df["macd_hist"].iloc[-1] >= 0 else "rouge"
    macd_dir = "haussier" if df["macd_hist"].iloc[-1] > df["macd_hist"].iloc[-2] else "baissier"
    above  = close > ma50
    is_xau = pair.startswith("XAU")
    risk   = BALANCE * 0.01
    b64    = base64.b64encode(img_bytes).decode()
    sltp_base = base_result["sltp"]

    xau_r = (f"\nREGLES XAU: sl_pips/tp_pips=DOLLARS. SL {TF_LABELS[tf]} selon ATR.\n"
             f"Prix 2 decimales. INTERDIT prix<100. lot_micro=({risk:.2f}/(sl_pips*100))"
             if is_xau else
             f"\nREGLES FOREX: sl_pips/tp_pips=PIPS. lot_micro=({risk:.2f}/(sl_pips*10))")

    prompt = (f"Analyste technique expert. Paire {pair} {TF_LABELS[tf]}.\n"
              f"Signal déjà calculé: {signal} (score={score}/100)\n"
              f"Close={close:.2f} RSI={rsi:.2f} MACD={macd_col} {macd_dir} "
              f"MA50={ma50:.2f} Prix={'AU-DESSUS' if above else 'EN-DESSOUS'} MA50\n"
              f"{xau_r}\n\n"
              f"Complète UNIQUEMENT les champs textuels et affine les niveaux SL/TP si nécessaire.\n"
              f'Garde signal="{signal}" et score proche de {score}.\n'
              f"JSON UNIQUEMENT sans markdown:\n"
              f'{{"signal":"{signal}","score":{score},"confiance":{{"niveau":"faible|moyen|eleve","raison":"..."}},'
              f'"tendance":{{"direction":"haussiere|baissiere|laterale","force":"faible|moderee|forte","description":"..."}},'
              f'"rsi":{{"valeur":{rsi:.2f},"zone":"survente|neutre|surachat","tendance":"montant|descendant|stable"}},'
              f'"macd":{{"etat":"haussier|baissier|neutre","bougies_depuis":1}},'
              f'"ma50":{{"position":"au-dessus|en-dessous|proche","condition":true}},'
              f'"supports_resistances":{{"resistances":["R1: {sltp_base["tp"]}","R2: niveau"],"supports":["S1: {sltp_base["sl"]}","S2: niveau"]}},'
              f'"sltp":{{"entree":"{sltp_base["entree"]}","sl":"{sltp_base["sl"]}","sl_pips":"{sltp_base["sl_pips"]}",'
              f'"tp":"{sltp_base["tp"]}","tp_pips":"{sltp_base["tp_pips"]}","rr":"1:2","lot_micro":"{sltp_base["lot_micro"]}"}},'
              f'"forces":"...","faiblesses":"...","analyse":"3 phrases max",'
              f'"scenario_alternatif":"niveau invalidation","probabilite_signal":"{score}% justification"}}')

    payload = {"model": MODEL, "max_tokens": 800,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
               ]}]}
    hdrs = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}

    for attempt in range(2):
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers=hdrs, json=payload, timeout=45)
            r.raise_for_status()
            raw   = r.json()["choices"][0]["message"]["content"]
            clean = raw.replace("```json","").replace("```","").strip()
            m     = re.search(r'\{[\s\S]*\}', clean)
            groq_result = json.loads(m.group(0) if m else clean)
            groq_result["signal"] = signal
            groq_result["score"]  = score
            if not groq_result.get("sltp",{}).get("entree"):
                groq_result["sltp"] = sltp_base
            print(f"    Groq enrichissement OK")
            return groq_result
        except Exception as e:
            print(f"  Groq tentative {attempt+1}/2: {e}")
            if attempt < 1:
                time.sleep(3)

    print(f"    Groq indisponible — résultat Python utilisé")
    return base_result

# ──────────────────────────────────────────────────────────────
#  CONSENSUS
# ──────────────────────────────────────────────────────────────
def evaluate_consensus(results):
    r15=results.get("15m",{});r1=results.get("1h",{});r4=results.get("4h",{})
    s15=r15.get("signal","WAIT");s1=r1.get("signal","WAIT");s4=r4.get("signal","WAIT")
    sc1=int(r1.get("score",0));sc4=int(r4.get("score",0))
    print(f"  M15={s15} | H1={s1}({sc1}) | H4={s4}({sc4}) | MIN_SCORE={MIN_SCORE}")

    if s1==s4 and s1 in ("BUY","SELL") and sc1>=MIN_SCORE and sc4>=MIN_SCORE:
        print(f"  ✅ Signal FORT : H1={sc1} H4={sc4} >= {MIN_SCORE}")
        return {"signal":s1,"score_h1":sc1,"score_h4":sc4,
                "m15_ok":s15==s1,"partial":False,"r15":r15,"r1":r1,"r4":r4}

    if s1 in ("BUY","SELL") and s4 in ("WAIT",s1) and sc1>=MIN_SCORE+10:
        print(f"  ⚠️ Signal PARTIEL : H1={sc1}>={MIN_SCORE+10} H4={sc4} en WAIT")
        return {"signal":s1,"score_h1":sc1,"score_h4":sc4,
                "m15_ok":s15==s1,"partial":True,"r15":r15,"r1":r1,"r4":r4}

    if s1 not in ("BUY","SELL"):
        print(f"  ❌ Pas de signal : H1={s1} — pas de direction claire")
    elif s1!=s4:
        print(f"  ❌ Pas de signal : H1={s1} vs H4={s4} — directions opposées")
    elif sc1<MIN_SCORE:
        print(f"  ❌ Pas de signal : H1 score={sc1} < {MIN_SCORE}")
    elif sc4<MIN_SCORE:
        print(f"  ❌ Pas de signal : H4 score={sc4} < {MIN_SCORE}")
    return None

# ──────────────────────────────────────────────────────────────
#  EMAIL
# ──────────────────────────────────────────────────────────────
def build_email(consensus,charts,pair):
    sig=consensus["signal"];r1=consensus["r1"];r4=consensus["r4"];r15=consensus["r15"]
    sc1=consensus["score_h1"];sc4=consensus["score_h4"]
    partial=consensus["partial"];m15_ok=consensus["m15_ok"]
    sltp=r1.get("sltp",{});now_str=datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    is_buy=sig=="BUY";col=("#2563EB" if is_buy else "#DC2626")
    ico="🟢" if is_buy else "🔴";arrow="▲" if is_buy else "▼"
    unit="$" if pair.startswith("XAU") else "p"
    entree=sltp.get("entree","N/A");sl=sltp.get("sl","N/A");tp=sltp.get("tp","N/A")
    rr=sltp.get("rr","1:2");lot=sltp.get("lot_micro","N/A")
    sl_u=sltp.get("sl_pips","N/A");tp_u=sltp.get("tp_pips","N/A")
    prob=r1.get("probabilite_signal","N/A");inv=r1.get("scenario_alternatif","N/A")
    analyse=r1.get("analyse","");forces=r1.get("forces","");risques=r1.get("faiblesses","")
    res_list=r1.get("supports_resistances",{}).get("resistances",[])
    sup_list=r1.get("supports_resistances",{}).get("supports",[])
    tend1=r1.get("tendance",{}).get("direction","—");tend4=r4.get("tendance",{}).get("direction","—")
    def tb(r,lbl):
        s=r.get("signal","WAIT");sc=r.get("score",0)
        c="tf-buy" if s=="BUY" else "tf-sell" if s=="SELL" else "tf-wait"
        return f'<span class="tf-badge {c}">{lbl}: {s} {sc}/100</span>'
    html=f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<style>
body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
.wrap{{max-width:660px;margin:20px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10);}}
.header{{background:{col};padding:26px 28px 18px;color:#fff;}}
.h-sig{{font-size:2.1rem;font-weight:900;}}.h-sub{{font-size:.88rem;opacity:.88;margin-top:4px;}}
.h-date{{font-size:.72rem;opacity:.65;margin-top:5px;}}.body{{padding:22px 28px;}}
.kpi-row{{display:flex;gap:10px;margin-bottom:16px;}}
.kpi{{flex:1;background:#f8fafc;border-radius:10px;padding:12px;border:1.5px solid #e2e8f0;text-align:center;}}
.kpi-lbl{{font-size:.62rem;color:#64748b;text-transform:uppercase;margin-bottom:3px;}}
.kpi-val{{font-size:1.1rem;font-weight:800;color:#1e293b;}}.kpi-sub{{font-size:.62rem;color:#94a3b8;margin-top:2px;}}
.k-sl .kpi-val{{color:#DC2626;}}.k-tp .kpi-val{{color:#16a34a;}}.k-rr .kpi-val{{color:#7c3aed;}}
.sec{{font-size:.68rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.1em;margin:14px 0 7px;border-bottom:1px solid #e2e8f0;padding-bottom:4px;}}
.sbar{{display:flex;align-items:center;gap:8px;margin-bottom:5px;}}.slbl{{font-size:.68rem;color:#64748b;width:32px;}}
.bbg{{flex:1;height:7px;background:#f1f5f9;border-radius:4px;overflow:hidden;}}.bf{{height:100%;border-radius:4px;}}
.snum{{font-size:.68rem;font-weight:700;color:#1e293b;width:42px;text-align:right;}}
.al{{padding:11px 13px;border-radius:8px;font-size:.76rem;line-height:1.6;margin-bottom:9px;}}
.ai{{background:#eff6ff;border:1px solid #93c5fd;color:#1e40af;}}
.aw{{background:#fffbeb;border:1px solid #fbbf24;color:#92400e;}}
.ad{{background:#fef2f2;border:1px solid #fca5a5;color:#991b1b;}}
.ao{{background:#f0fdf4;border:1px solid #86efac;color:#166534;}}
.sr-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.sr-r{{font-size:.7rem;padding:4px 8px;border-radius:5px;margin-bottom:3px;background:#fef2f2;color:#dc2626;border-left:3px solid #dc2626;}}
.sr-s{{font-size:.7rem;padding:4px 8px;border-radius:5px;margin-bottom:3px;background:#f0fdf4;color:#16a34a;border-left:3px solid #16a34a;}}
.boxes{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;}}
.fb{{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:11px;}}
.rb{{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:11px;}}
.bt{{font-size:.62rem;font-weight:700;text-transform:uppercase;margin-bottom:5px;}}
.bx{{font-size:.69rem;line-height:1.7;white-space:pre-line;}}
.ci{{width:100%;border-radius:8px;margin-bottom:8px;display:block;border:1px solid #e2e8f0;}}
.footer{{background:#f8fafc;padding:12px 28px;font-size:.62rem;color:#94a3b8;text-align:center;border-top:1px solid #e2e8f0;}}
.tfr{{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px;}}
.tf-badge{{padding:4px 11px;border-radius:20px;font-size:.7rem;font-weight:700;}}
.tf-buy{{background:#dbeafe;color:#1d4ed8;}}.tf-sell{{background:#fee2e2;color:#b91c1c;}}.tf-wait{{background:#f3f4f6;color:#6b7280;}}
.pw{{background:#fffbeb;border:1px solid #f59e0b;border-radius:8px;padding:9px 13px;font-size:.71rem;color:#92400e;margin-bottom:12px;}}
.m15b{{display:inline-block;padding:3px 9px;border-radius:12px;font-size:.65rem;font-weight:700;margin-left:8px;}}
.m15ok{{background:#dcfce7;color:#166534;}}.m15no{{background:#fef9c3;color:#854d0e;}}
</style></head>
<body><div class="wrap">
<div class="header">
<div class="h-sig">{ico} {sig} {arrow} &nbsp; {sc1}/100</div>
<div class="h-sub">📊 {pair} · Triple confirmation M15+H1+H4 <span class="m15b {'m15ok' if m15_ok else 'm15no'}">{'✅ M15 confirme' if m15_ok else '⚠ M15 neutre'}</span></div>
<div class="h-date">🕐 {now_str}{'  ·  ⚠️ Signal partiel' if partial else '  ·  ✅ H1+H4 confirmés'}</div>
</div>
<div class="body">
{'<div class="pw">⚠️ <b>Signal partiel</b> : H4 en WAIT. Réduire position de 50% et attendre confirmation H4.</div>' if partial else ''}
<div class="sec">Scores triple timeframe</div>
<div class="tfr">{tb(r15,"M15")} {tb(r1,"H1")} {tb(r4,"H4")}</div>
<div class="sbar"><span class="slbl">M15</span><div class="bbg"><div class="bf" style="width:{r15.get('score',0)}%;background:#f59e0b"></div></div><span class="snum">{r15.get('score',0)}/100</span></div>
<div class="sbar"><span class="slbl">H1</span><div class="bbg"><div class="bf" style="width:{sc1}%;background:{col}"></div></div><span class="snum">{sc1}/100</span></div>
<div class="sbar"><span class="slbl">H4</span><div class="bbg"><div class="bf" style="width:{sc4}%;background:#7c3aed"></div></div><span class="snum">{sc4}/100</span></div>
<div class="sec">Niveaux de trade</div>
<div class="kpi-row">
<div class="kpi"><div class="kpi-lbl">Entrée</div><div class="kpi-val">{entree}</div><div class="kpi-sub">Prix actuel</div></div>
<div class="kpi k-sl"><div class="kpi-lbl">⛔ Stop Loss</div><div class="kpi-val">{sl}</div><div class="kpi-sub">−{sl_u}{unit}</div></div>
<div class="kpi k-tp"><div class="kpi-lbl">🎯 Take Profit</div><div class="kpi-val">{tp}</div><div class="kpi-sub">+{tp_u}{unit}</div></div>
<div class="kpi k-rr"><div class="kpi-lbl">R:R</div><div class="kpi-val">{rr}</div><div class="kpi-sub">Lot: {lot}</div></div>
</div>
<div class="sec">Indicateurs H1</div>
<div class="al ai"><b>RSI(14)</b>: {r1.get('rsi',{}).get('valeur','—')} — {r1.get('rsi',{}).get('zone','—')} — {r1.get('rsi',{}).get('tendance','—')}<br>
<b>MACD</b>: {r1.get('macd',{}).get('etat','—')} depuis {r1.get('macd',{}).get('bougies_depuis',0)} bougie(s)<br>
<b>MA50</b>: {r1.get('ma50',{}).get('position','—')} — Tendance H1: {tend1} | H4: {tend4}</div>
<div class="sec">Supports &amp; Résistances</div>
<div class="sr-grid">
<div><div style="font-size:.65rem;font-weight:700;color:#dc2626;margin-bottom:5px;">▼ Résistances</div>
{''.join(f'<div class="sr-r">{l}</div>' for l in res_list) or '<span style="font-size:.7rem;color:#94a3b8">—</span>'}</div>
<div><div style="font-size:.65rem;font-weight:700;color:#16a34a;margin-bottom:5px;">▲ Supports</div>
{''.join(f'<div class="sr-s">{l}</div>' for l in sup_list) or '<span style="font-size:.7rem;color:#94a3b8">—</span>'}</div>
</div>
<div class="sec">Analyse</div>
<div class="al ai">{analyse}</div>
<div class="al {'ao' if any(x in str(prob)[:3] for x in ['6','7','8']) else 'aw'}"><b>📊 Probabilité:</b> {prob}</div>
<div class="al ad"><b>⚠️ Invalidation:</b> {inv}</div>
<div class="boxes">
<div class="fb"><div class="bt" style="color:#16a34a">✅ Points forts</div><div class="bx" style="color:#166534">{forces}</div></div>
<div class="rb"><div class="bt" style="color:#d97706">⚠️ Risques</div><div class="bx" style="color:#92400e">{risques}</div></div>
</div>
<div class="sec">Graphiques annotés SL/TP</div>
<img src="cid:chart_m15" class="ci" alt="M15">
<img src="cid:chart_h1" class="ci" alt="H1">
<img src="cid:chart_h4" class="ci" alt="H4">
<div class="al aw">⚠️ <b>Toujours vérifier avant d'entrer.</b> Ne jamais risquer plus de 1%. Lot suggéré: <b>{lot}</b>.</div>
</div>
<div class="footer">ChartAnalyzer Scanner v2 · {pair} · {now_str} · M15={r15.get('signal','?')} H1={sig}({sc1}) H4={r4.get('signal','?')}({sc4}) · Seuil={MIN_SCORE}/100</div>
</div></body></html>"""
    msg=MIMEMultipart("related")
    msg["Subject"]=f"{ico} [{sig}] {pair} — {sc1}/100 · {'M15+H1+H4' if m15_ok else 'H1+H4'} · {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
    msg["From"]=EMAIL_FROM;msg["To"]=EMAIL_TO
    alt=MIMEMultipart("alternative");alt.attach(MIMEText(html,"html","utf-8"));msg.attach(alt)
    for tf,cid in [("15m","chart_m15"),("1h","chart_h1"),("4h","chart_h4")]:
        if tf in charts:
            img=MIMEImage(charts[tf],_subtype="png")
            img.add_header("Content-ID",f"<{cid}>")
            img.add_header("Content-Disposition","inline",filename=f"chart_{tf}.png")
            msg.attach(img)
    return msg

def send_email(msg):
    print(f"  Envoi email a {EMAIL_TO}...")
    login = SMTP_LOGIN if SMTP_LOGIN else EMAIL_FROM
    with smtplib.SMTP("smtp-relay.brevo.com", 587) as s:
        s.starttls()
        s.login(login, EMAIL_PASS)
        s.send_message(msg)
    print("  Email envoye!")

# ──────────────────────────────────────────────────────────────
#  ANALYSE PAIRE
# ──────────────────────────────────────────────────────────────
def analyze_pair(pair,state):
    print(f"\n{'='*55}\n  PAIRE: {pair}\n{'='*55}")
    results={};sltp_h1={}
    for tf in TIMEFRAMES:
        print(f"\n  -- {TF_LABELS[tf]} --")
        try:df=fetch_ohlcv(pair,tf,CANDLES[tf])
        except Exception as e:print(f"  Erreur donnees {TF_LABELS[tf]}: {e}");continue
        df=compute_indicators(df)
        print(f"  Analyse {TF_LABELS[tf]}...")
        try:
            img=generate_chart(df,pair,tf)
            result=call_groq(img,df,pair,tf)
            results[tf]=result
            print(f"  -> {result.get('signal','?')} ({result.get('score',0)}/100)")
            if tf=="1h":sltp_h1=result.get("sltp",{})
        except Exception as e:print(f"  Erreur {TF_LABELS[tf]}: {e}");traceback.print_exc()
        time.sleep(1)
    print(f"\n  -- CONSENSUS --")
    if len(results)<2:print("  Donnees insuffisantes");return state,False
    consensus=evaluate_consensus(results)
    if consensus is None:print("  Pas de signal");return state,False
    sig=consensus["signal"]
    print(f"  SIGNAL: {sig} H1={consensus['score_h1']} H4={consensus['score_h4']}")
    if already_signaled(state,pair,sig):return state,False
    try:
        hist_bt, _ = perf_engine._read_history(GH_TOKEN, GH_REPO)
        wr_bt, nb_bt, reco_bt = perf_engine.mini_backtest(
            pair.replace("/",""), sig, consensus["score_h1"], hist_bt)
        if reco_bt == "SKIP":
            print(f"  [perf] ❌ Backtest défavorable (WR={wr_bt}% sur {nb_bt} trades) — signal bloqué")
            return state, False
        elif reco_bt == "CAUTION":
            print(f"  [perf] ⚠️ Backtest moyen (WR={wr_bt}% sur {nb_bt} trades) — lot réduit 50%")
            consensus["partial"] = True
        elif reco_bt == "GO":
            print(f"  [perf] ✅ Backtest favorable (WR={wr_bt}% sur {nb_bt} trades)")
    except Exception as ep:
        print(f"  [perf] mini_backtest: {ep}")
    print(f"\n  Graphiques annotes SL/TP...")
    charts={}
    for tf in TIMEFRAMES:
        try:
            df=fetch_ohlcv(pair,tf,CANDLES[tf]);df=compute_indicators(df)
            charts[tf]=generate_chart(df,pair,tf,sltp=sltp_h1,signal=sig)
        except Exception as e:print(f"  Graphique {tf}: {e}")
    try:
        msg=build_email(consensus,charts,pair);send_email(msg)
        state=mark_sent(state,pair,sig)
        write_signal_json(consensus,pair)
        try:
            perf_engine.save_signal(consensus, pair, GH_TOKEN, GH_REPO)
        except Exception as ep:
            print(f"  [perf] save_signal: {ep}")
    except Exception as e:print(f"  Erreur email: {e}");traceback.print_exc()
    return state,True

# ──────────────────────────────────────────────────────────────
#  EMAIL DE TEST
# ──────────────────────────────────────────────────────────────
def send_test_email():
    print("\n  MODE TEST EMAIL — envoi d un email de test...")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "✅ [TEST] ChartAnalyzer Scanner — Email OK"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    html = ("""<!DOCTYPE html><html><body style="font-family:Arial;padding:20px;background:#f1f5f9">
<div style="max-width:500px;margin:auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 12px rgba(0,0,0,.1)">
<h2 style="color:#16a34a">✅ Email de test reçu !</h2>
<p>Ton scanner ChartAnalyzer est correctement configuré.</p>
<p>Les prochains emails de signaux BUY/SELL seront envoyés ici.</p>
<hr style="border:1px solid #e2e8f0;margin:16px 0">
<p style="color:#64748b;font-size:12px">ChartAnalyzer Scanner v2 — Test envoyé le """
             + datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
             + """</p></div></body></html>""")
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        send_email(msg)
        print("  ✅ Email de test envoyé avec succès !")
    except Exception as e:
        print(f"  ❌ Erreur email : {e}")
        traceback.print_exc()

# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*55}\n  ChartAnalyzer Scanner v2\n  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n{'='*55}")
    missing=[k for k,v in [("GROQ_KEY",GROQ_KEY),("EMAIL_FROM",EMAIL_FROM),
                             ("EMAIL_PASS",EMAIL_PASS),("EMAIL_TO",EMAIL_TO)] if not v]
    if missing:print(f"Variables manquantes: {', '.join(missing)}");sys.exit(1)
    if TEST_EMAIL:
        send_test_email();sys.exit(0)
    if not is_market_open():print("Marche ferme");sys.exit(0)
    print(f"Paires: {', '.join(PAIRS)} | Score min: {MIN_SCORE} | Capital: {BALANCE}EUR")
    print(f"Anti-spam: {'actif' if GH_TOKEN else 'inactif'}")
    state,sha=read_state()
    sent_total=0
    for pair in PAIRS:
        try:state,sent=analyze_pair(pair,state);sent_total+=int(sent)
        except Exception as e:print(f"Erreur {pair}: {e}");traceback.print_exc()
        time.sleep(3)
    if GH_TOKEN:write_state(state,sha)
    try:
        perf_engine.run(GH_TOKEN, GH_REPO, MIN_SCORE)
    except Exception as ep:
        print(f"  [perf] run: {ep}")
    print(f"\n{'='*55}\n  Scan termine — {sent_total} signal(s) sur {len(PAIRS)} paire(s)\n{'='*55}\n")

if __name__=="__main__":
    main()
