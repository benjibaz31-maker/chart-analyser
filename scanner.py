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
MIN_SCORE = int(os.getenv("MIN_SCORE", "65"))
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

def is_market_open():
    now=datetime.now(timezone.utc);wd=now.weekday();h=now.hour
    if wd==5:return False
    if wd==6:return h>=22
    if wd==4:return h<21
    return True

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

def compute_indicators(df):
    c=df["close"];d=c.diff();g=d.clip(lower=0);l=(-d).clip(lower=0)
    ag=g.ewm(com=13,min_periods=14).mean();al=l.ewm(com=13,min_periods=14).mean()
    df["rsi"]=100-(100/(1+ag/al.replace(0,float("nan"))))
    e12=c.ewm(span=12,adjust=False).mean();e26=c.ewm(span=26,adjust=False).mean()
    df["macd"]=e12-e26;df["macd_signal"]=df["macd"].ewm(span=9,adjust=False).mean()
    df["macd_hist"]=df["macd"]-df["macd_signal"];df["ma50"]=c.rolling(50).mean()
    return df

def _parse_price(s):
    if not s or s=="N/A":return None
    m=re.search(r"\d{1,6}\.\d{1,4}",str(s))
    return float(m.group()) if m else None

def draw_candles(ax,df):
    for i,row in df.iterrows():
        col=GREEN if row["close"]>=row["open"] else RED
        ax.plot([i,i],[row["low"],row["high"]],color=col,linewidth=0.7,zorder=2)
        bot=min(row["open"],row["close"])
        h=max(abs(row["close"]-row["open"]),row["close"]*0.0001)
        ax.add_patch(Rectangle((i-0.4,bot),0.8,h,facecolor=col,edgecolor=col,linewidth=0,zorder=3))

def style_ax(ax,label="",show_x=False):
    ax.set_facecolor(BG);ax.tick_params(colors=TEXT2,labelsize=7)
    ax.spines[:].set_color(GRID);ax.yaxis.set_label_position("right");ax.yaxis.tick_right()
    for sp in ax.spines.values():sp.set_linewidth(0.5)
    ax.grid(True,color=GRID,linewidth=0.4,linestyle="--",alpha=0.6)
    if label:ax.text(0.01,0.97,label,transform=ax.transAxes,color=TEXT2,fontsize=7,va="top",ha="left")
    if not show_x:ax.tick_params(labelbottom=False)

def generate_chart(df,pair,tf,sltp=None,signal=""):
    n=len(df);idx=np.arange(n)
    fig=plt.figure(figsize=(14,7),facecolor=BG)
    gs=gridspec.GridSpec(3,1,figure=fig,height_ratios=[3,1,1],hspace=0.04,left=0.02,right=0.88,top=0.93,bottom=0.07)
    ax1=fig.add_subplot(gs[0]);ax2=fig.add_subplot(gs[1]);ax3=fig.add_subplot(gs[2])
    draw_candles(ax1,df)
    ax1.plot(idx,df["ma50"],color=MA_COL,linewidth=1.2,zorder=4)
    style_ax(ax1,label=f"{pair}  {TF_LABELS[tf]}")
    ax1.set_xlim(-1,n+1);ax1.autoscale(axis="y")
    # Lignes SL/TP annotees
    if sltp:
        entry=_parse_price(sltp.get("entree"));sl_p=_parse_price(sltp.get("sl"));tp_p=_parse_price(sltp.get("tp"))
        pmin=df["low"].min();pmax=df["high"].max()
        def hline(price,color,label,ls="--",lw=1.5):
            if price and pmin*0.95<price<pmax*1.05:
                ax1.axhline(price,color=color,linewidth=lw,linestyle=ls,alpha=0.9,zorder=5)
                ax1.text(n+0.3,price,f" {label}\n {price:.2f}",color=color,fontsize=7,va="center",fontweight="bold")
        hline(entry,"#60a5fa","ENTREE","-",1.8)
        hline(sl_p,"#ef4444","SL")
        hline(tp_p,"#22c55e","TP")
        if entry and sl_p and tp_p:
            ax1.axhspan(min(entry,tp_p),max(entry,tp_p),alpha=0.06,color="#22c55e",zorder=1)
            ax1.axhspan(min(entry,sl_p),max(entry,sl_p),alpha=0.06,color="#ef4444",zorder=1)
    ico="🟢" if signal=="BUY" else "🔴" if signal=="SELL" else "⏸"
    ax1.set_title(f"  {ico} {pair}  ·  {TF_LABELS[tf]}  ·  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",color=TEXT,fontsize=8.5,loc="left",pad=6)
    hist=df["macd_hist"].values;colors=[MACD_G if v>=0 else MACD_R for v in hist]
    ax2.bar(idx,hist,color=colors,width=0.7,zorder=3)
    ax2.plot(idx,df["macd"],color="#818cf8",linewidth=0.9,zorder=4)
    ax2.plot(idx,df["macd_signal"],color="#f59e0b",linewidth=0.9,zorder=4)
    ax2.axhline(0,color=GRID,linewidth=0.6)
    style_ax(ax2,label=f"MACD(12,26,9)  {hist[-1]:.3f}");ax2.set_xlim(-1,n+1)
    ax3.plot(idx,df["rsi"],color=RSI_COL,linewidth=1.0,zorder=4)
    ax3.axhline(70,color=RED,linewidth=0.5,linestyle="--",alpha=0.6)
    ax3.axhline(30,color=GREEN,linewidth=0.5,linestyle="--",alpha=0.6)
    ax3.fill_between(idx,df["rsi"],70,where=df["rsi"]>=70,alpha=0.15,color=RED)
    ax3.fill_between(idx,df["rsi"],30,where=df["rsi"]<=30,alpha=0.15,color=GREEN)
    ax3.set_ylim(0,100);style_ax(ax3,label=f"RSI(14)  {df['rsi'].iloc[-1]:.2f}",show_x=True);ax3.set_xlim(-1,n+1)
    step=max(1,n//10);xticks=idx[::step]
    ax3.set_xticks(xticks);ax3.set_xticklabels([df["date"].iloc[i].strftime("%d/%m %Hh") for i in xticks],rotation=30,ha="right",fontsize=6,color=TEXT2)
    buf=BytesIO();fig.savefig(buf,format="png",dpi=130,bbox_inches="tight",facecolor=BG,edgecolor="none");plt.close(fig);buf.seek(0)
    return buf.read()

def call_groq(img_bytes,df,pair,tf):
    last=df.iloc[-1];rsi=df["rsi"].iloc[-1]
    macd_col="vert" if df["macd_hist"].iloc[-1]>=0 else "rouge"
    macd_dir="haussier" if df["macd_hist"].iloc[-1]>df["macd_hist"].iloc[-2] else "baissier"
    ma50=df["ma50"].iloc[-1];above=last["close"]>ma50
    is_xau=pair.startswith("XAU");risk=BALANCE*0.01;b64=base64.b64encode(img_bytes).decode()
    xau_r=f"""
REGLES XAU: sl_pips/tp_pips=DOLLARS. SL M15:5-12$ H1:10-20$ H4:20-40$.
Prix 2 decimales (ex:4412.30). INTERDIT prix<100. lot_micro=({risk:.2f}/(sl_pips*100))""" if is_xau else f"""
REGLES FOREX: sl_pips/tp_pips=PIPS. SL M15:15-25p H1:30-50p H4:50-100p.
lot_micro=({risk:.2f}/(sl_pips*10))"""
    prompt=f"""Analyste technique RSI+MACD+MA50. Paire {pair} {TF_LABELS[tf]}.
BUY: RSI<50 montant + MACD rouge->vert + prix>MA50
SELL: RSI>50 descendant + MACD vert->rouge + prix<MA50
WAIT: <2 conditions
Donnees: Close={last['close']:.2f} RSI={rsi:.2f} MACD={macd_col} {macd_dir} MA50={ma50:.2f} Prix={'AU-DESSUS' if above else 'EN-DESSOUS'}{xau_r}
JSON UNIQUEMENT sans markdown:
{{"signal":"BUY|SELL|WAIT","score":0-100,"confiance":{{"niveau":"faible|moyen|eleve","raison":"..."}},"tendance":{{"direction":"haussiere|baissiere|laterale","force":"faible|moderee|forte","description":"..."}},"rsi":{{"valeur":{rsi:.2f},"zone":"survente|neutre|surachat","tendance":"montant|descendant|stable"}},"macd":{{"etat":"haussier|baissier|neutre","bougies_depuis":0}},"ma50":{{"position":"au-dessus|en-dessous|proche","condition":true}},"supports_resistances":{{"resistances":["R1: niveau","R2: niveau"],"supports":["S1: niveau","S2: niveau"]}},"sltp":{{"entree":"valeur","sl":"valeur","sl_pips":"valeur","tp":"valeur","tp_pips":"valeur","rr":"1:2","lot_micro":"valeur"}},"forces":"Force1\\nForce2","faiblesses":"Risque1\\nRisque2","analyse":"3 phrases","scenario_alternatif":"niveau invalidation","probabilite_signal":"XX% justification"}}"""
    payload={"model":MODEL,"max_tokens":1100,"messages":[{"role":"user","content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}}]}]}
    hdrs={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"}
    for attempt in range(3):
        try:
            r=requests.post("https://api.groq.com/openai/v1/chat/completions",headers=hdrs,json=payload,timeout=60)
            r.raise_for_status();raw=r.json()["choices"][0]["message"]["content"]
            clean=raw.replace("```json","").replace("```","").strip()
            m=re.search(r'\{[\s\S]*\}',clean)
            return json.loads(m.group(0) if m else clean)
        except Exception as e:
            print(f"  Groq tentative {attempt+1}/3: {e}")
            if attempt<2:time.sleep(5*(attempt+1))
    return {}

def evaluate_consensus(results):
    r15=results.get("15m",{});r1=results.get("1h",{});r4=results.get("4h",{})
    s15=r15.get("signal","WAIT");s1=r1.get("signal","WAIT");s4=r4.get("signal","WAIT")
    sc1=int(r1.get("score",0));sc4=int(r4.get("score",0))
    print(f"  M15={s15} | H1={s1}({sc1}) | H4={s4}({sc4})")
    if s1==s4 and s1 in ("BUY","SELL") and sc1>=MIN_SCORE:
        return {"signal":s1,"score_h1":sc1,"score_h4":sc4,"m15_ok":s15==s1,"partial":False,"r15":r15,"r1":r1,"r4":r4}
    if s1 in ("BUY","SELL") and s4 in ("WAIT",s1) and sc1>=MIN_SCORE+10:
        return {"signal":s1,"score_h1":sc1,"score_h4":sc4,"m15_ok":s15==s1,"partial":True,"r15":r15,"r1":r1,"r4":r4}
    return None

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
.h-sig{{font-size:2.1rem;font-weight:900;}}
.h-sub{{font-size:.88rem;opacity:.88;margin-top:4px;}}
.h-date{{font-size:.72rem;opacity:.65;margin-top:5px;}}
.body{{padding:22px 28px;}}
.kpi-row{{display:flex;gap:10px;margin-bottom:16px;}}
.kpi{{flex:1;background:#f8fafc;border-radius:10px;padding:12px;border:1.5px solid #e2e8f0;text-align:center;}}
.kpi-lbl{{font-size:.62rem;color:#64748b;text-transform:uppercase;margin-bottom:3px;}}
.kpi-val{{font-size:1.1rem;font-weight:800;color:#1e293b;}}
.kpi-sub{{font-size:.62rem;color:#94a3b8;margin-top:2px;}}
.k-sl .kpi-val{{color:#DC2626;}}.k-tp .kpi-val{{color:#16a34a;}}.k-rr .kpi-val{{color:#7c3aed;}}
.sec{{font-size:.68rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.1em;margin:14px 0 7px;border-bottom:1px solid #e2e8f0;padding-bottom:4px;}}
.sbar{{display:flex;align-items:center;gap:8px;margin-bottom:5px;}}
.slbl{{font-size:.68rem;color:#64748b;width:32px;}}
.bbg{{flex:1;height:7px;background:#f1f5f9;border-radius:4px;overflow:hidden;}}
.bf{{height:100%;border-radius:4px;}}
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

def analyze_pair(pair,state):
    print(f"\n{'='*55}\n  PAIRE: {pair}\n{'='*55}")
    results={};sltp_h1={}
    for tf in TIMEFRAMES:
        print(f"\n  -- {TF_LABELS[tf]} --")
        try:df=fetch_ohlcv(pair,tf,CANDLES[tf])
        except Exception as e:print(f"  Erreur donnees {TF_LABELS[tf]}: {e}");continue
        df=compute_indicators(df)
        print(f"  Analyse Groq {TF_LABELS[tf]}...")
        try:
            img=generate_chart(df,pair,tf)
            result=call_groq(img,df,pair,tf)
            results[tf]=result
            print(f"  -> {result.get('signal','?')} ({result.get('score',0)}/100)")
            if tf=="1h":sltp_h1=result.get("sltp",{})
        except Exception as e:print(f"  Erreur Groq {TF_LABELS[tf]}: {e}");traceback.print_exc()
        time.sleep(2)
    print(f"\n  -- CONSENSUS --")
    if len(results)<2:print("  Donnees insuffisantes");return state,False
    consensus=evaluate_consensus(results)
    if consensus is None:print("  Pas de signal");return state,False
    sig=consensus["signal"]
    print(f"  SIGNAL: {sig} H1={consensus['score_h1']} H4={consensus['score_h4']}")
    if already_signaled(state,pair,sig):return state,False
    # Mini backtest : vérifier l'historique similaire
    try:
        hist_bt, _ = perf_engine._read_history(GH_TOKEN, GH_REPO)
        wr_bt, nb_bt, reco_bt = perf_engine.mini_backtest(
            pair.replace("/",""), sig, consensus["score_h1"], hist_bt
        )
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
        # Enregistrer dans l'historique de performance
        try:
            perf_engine.save_signal(consensus, pair, GH_TOKEN, GH_REPO)
        except Exception as ep:
            print(f"  [perf] save_signal: {ep}")
    except Exception as e:print(f"  Erreur email: {e}");traceback.print_exc()
    return state,True

def send_test_email():
    """Envoie un email de test pour vérifier la configuration SMTP."""
    print("\n  MODE TEST EMAIL — envoi d un email de test...")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "✅ [TEST] ChartAnalyzer Scanner — Email OK"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    html = """<!DOCTYPE html><html><body style="font-family:Arial;padding:20px;background:#f1f5f9">
<div style="max-width:500px;margin:auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 12px rgba(0,0,0,.1)">
<h2 style="color:#16a34a">✅ Email de test reçu !</h2>
<p>Ton scanner ChartAnalyzer est correctement configuré.</p>
<p>Les prochains emails de signaux BUY/SELL seront envoyés ici.</p>
<hr style="border:1px solid #e2e8f0;margin:16px 0">
<p style="color:#64748b;font-size:12px">ChartAnalyzer Scanner v2 — Test envoyé le """ + datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC") + """</p>
</div></body></html>"""
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        send_email(msg)
        print("  ✅ Email de test envoyé avec succès !")
    except Exception as e:
        print(f"  ❌ Erreur email : {e}")
        traceback.print_exc()

def main():
    print(f"\n{'='*55}\n  ChartAnalyzer Scanner v2\n  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n{'='*55}")
    missing=[k for k,v in [("GROQ_KEY",GROQ_KEY),("EMAIL_FROM",EMAIL_FROM),("EMAIL_PASS",EMAIL_PASS),("EMAIL_TO",EMAIL_TO)] if not v]
    if missing:print(f"Variables manquantes: {', '.join(missing)}");sys.exit(1)
    if TEST_EMAIL:
        send_test_email()
        sys.exit(0)
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
    # Moteur de performance : mise à jour résultats + analyse
    try:
        perf_engine.run(GH_TOKEN, GH_REPO, MIN_SCORE)
    except Exception as ep:
        print(f"  [perf] run: {ep}")
    print(f"\n{'='*55}\n  Scan termine — {sent_total} signal(s) sur {len(PAIRS)} paire(s)\n{'='*55}\n")

if __name__=="__main__":
    main()


# ─────────────────────────────────────────────────────────────
#  ÉCRITURE DU SIGNAL DANS GITHUB (pour l'EA MT5)
# ─────────────────────────────────────────────────────────────
def write_signal_json(consensus: dict, pair: str):
    """Écrit signal.json dans le repo GitHub pour que l'EA MT5 puisse le lire."""
    if not GH_TOKEN or not GH_REPO:
        print("  ⚠ GITHUB_TOKEN manquant — signal.json non écrit")
        return

    sig   = consensus["signal"]
    r1    = consensus["r1"]
    sltp  = r1.get("sltp", {})
    now   = datetime.now(timezone.utc)

    # Nettoyer le nom de paire pour MT5 (XAU/EUR → XAUEUR)
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
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept":        "application/vnd.github.v3+json"
    }

    # Récupérer le SHA existant si le fichier existe déjà
    sha = None
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    content = base64.b64encode(json.dumps(signal, indent=2).encode()).decode()
    body    = {
        "message": f"[signal] {sig} {pair} {now.strftime('%H:%M')} UTC",
        "content": content
    }
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
