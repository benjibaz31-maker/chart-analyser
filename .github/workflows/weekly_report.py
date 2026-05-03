
#!/usr/bin/env python3
"""
ChartAnalyzer — Journal PDF Hebdomadaire + Scoring Paire du Jour
Genere un PDF avec tous les trades de la semaine
et envoie un email de scoring chaque matin
"""

import os, sys, json, base64, smtplib, traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from io import BytesIO

import requests

# ─── Config ──────────────────────────────────────────────────────
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
EMAIL_TO   = os.getenv("EMAIL_TO",   "")
SMTP_LOGIN = os.getenv("SMTP_LOGIN", "")
GH_TOKEN   = os.getenv("GITHUB_TOKEN", "")
GH_REPO    = os.getenv("GITHUB_REPOSITORY", "")
GROQ_KEY   = os.getenv("GROQ_KEY", "")
PAIRS      = [p.strip() for p in os.getenv("PAIRS", "XAU/EUR,XAU/USD").split(",")]
MODE       = os.getenv("REPORT_MODE", "weekly")  # weekly ou morning

# ─── Lecture historique GitHub ────────────────────────────────────
def read_history():
    if not GH_TOKEN or not GH_REPO:
        return []
    url = f"https://api.github.com/repos/{GH_REPO}/contents/signals_history.json"
    hdrs = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        r = requests.get(url, headers=hdrs, timeout=10)
        if r.status_code == 200:
            return json.loads(base64.b64decode(r.json()["content"]).decode())
    except Exception as e:
        print(f"Erreur lecture historique: {e}")
    return []

# ─── Envoi email ──────────────────────────────────────────────────
def send_email(msg):
    login = SMTP_LOGIN if SMTP_LOGIN else EMAIL_FROM
    with smtplib.SMTP("smtp-relay.brevo.com", 587) as s:
        s.starttls()
        s.login(login, EMAIL_PASS)
        s.send_message(msg)
    print("Email envoyé !")

# ─────────────────────────────────────────────────────────────────
#  JOURNAL PDF HEBDOMADAIRE
# ─────────────────────────────────────────────────────────────────
def generate_pdf_report(history, week_start, week_end):
    """Génère un PDF du journal de trading hebdomadaire."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor, white, black
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                         Table, TableStyle, HRFlowable)
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    except ImportError:
        print("reportlab non installé — pip install reportlab")
        return None

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)

    # Couleurs
    DARK    = HexColor("#0f172a")
    BLUE    = HexColor("#2563eb")
    GREEN   = HexColor("#16a34a")
    RED     = HexColor("#dc2626")
    GOLD    = HexColor("#d97706")
    LIGHT   = HexColor("#f8fafc")
    GRAY    = HexColor("#64748b")
    BGCARD  = HexColor("#1e293b")

    styles = getSampleStyleSheet()
    story  = []

    # ── Style titre ──
    title_style = ParagraphStyle("title", parent=styles["Normal"],
                                  fontSize=24, fontName="Helvetica-Bold",
                                  textColor=DARK, alignment=TA_CENTER,
                                  spaceAfter=4)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"],
                                fontSize=11, fontName="Helvetica",
                                textColor=GRAY, alignment=TA_CENTER,
                                spaceAfter=16)
    h2_style = ParagraphStyle("h2", parent=styles["Normal"],
                               fontSize=14, fontName="Helvetica-Bold",
                               textColor=DARK, spaceBefore=12, spaceAfter=6)
    normal = ParagraphStyle("norm", parent=styles["Normal"],
                             fontSize=9, fontName="Helvetica",
                             textColor=DARK, spaceAfter=2)
    small = ParagraphStyle("small", parent=styles["Normal"],
                            fontSize=8, fontName="Helvetica",
                            textColor=GRAY)

    # ── En-tête ──
    story.append(Paragraph("📊 ChartAnalyzer — Journal de Trading", title_style))
    story.append(Paragraph(
        f"Semaine du {week_start.strftime('%d/%m/%Y')} au {week_end.strftime('%d/%m/%Y')}",
        sub_style))
    story.append(HRFlowable(width="100%", thickness=1, color=BLUE, spaceAfter=16))

    # ── Filtrer la semaine ──
    ws_str = week_start.strftime("%Y-%m-%d")
    we_str = week_end.strftime("%Y-%m-%d")
    week_trades = [s for s in history
                   if ws_str <= (s.get("date") or "") <= we_str]

    # ── KPIs ──
    total  = len(week_trades)
    tps    = sum(1 for t in week_trades if t.get("result") == "TP")
    sls    = sum(1 for t in week_trades if t.get("result") == "SL")
    pend   = total - tps - sls
    wr     = round(tps / (tps+sls) * 100) if (tps+sls) > 0 else 0
    buys   = sum(1 for t in week_trades if t.get("signal") == "BUY")
    sells  = total - buys
    # PnL simulé (RR 1:2, risque 1%)
    pnl_sim = tps * 20 - sls * 10

    kpi_data = [
        ["Signaux", "Gagnants (TP)", "Perdants (SL)", "En attente", "Win Rate", "PnL simulé"],
        [str(total), str(tps), str(sls), str(pend),
         f"{wr}%", f"{'+' if pnl_sim>=0 else ''}{pnl_sim}€"]
    ]
    kpi_table = Table(kpi_data, colWidths=[28*mm]*6)
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), DARK),
        ("TEXTCOLOR",  (0,0), (-1,0), LIGHT),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,0), 8),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("ROWHEIGHT",  (0,0), (-1,-1), 10*mm),
        ("BACKGROUND", (0,1), (-1,1), BGCARD),
        ("TEXTCOLOR",  (0,1), (0,1), LIGHT),
        ("TEXTCOLOR",  (1,1), (1,1), GREEN),
        ("TEXTCOLOR",  (2,1), (2,1), RED),
        ("TEXTCOLOR",  (3,1), (3,1), GOLD),
        ("TEXTCOLOR",  (4,1), (4,1), GREEN if wr>=50 else RED),
        ("TEXTCOLOR",  (5,1), (5,1), GREEN if pnl_sim>=0 else RED),
        ("FONTNAME",   (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE",   (0,1), (-1,1), 12),
        ("GRID",       (0,0), (-1,-1), 0.5, HexColor("#334155")),
        ("ROUNDEDCORNERS", [4]),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 12))

    # ── Tableau des trades ──
    story.append(Paragraph("📋 Détail des trades", h2_style))

    if week_trades:
        headers = ["Date", "Paire", "Signal", "Entrée", "SL", "TP",
                   "Score H1", "Score H4", "Résultat"]
        rows = [headers]
        for t in week_trades:
            result = t.get("result", "pending")
            result_str = "✅ TP" if result=="TP" else "❌ SL" if result=="SL" else "⏳ En cours"
            rows.append([
                f"{t.get('date','')} {t.get('time','')}",
                t.get("pair", "—"),
                t.get("signal", "—"),
                t.get("entry", "—"),
                t.get("sl", "—"),
                t.get("tp", "—"),
                f"{t.get('score_h1','—')}/100",
                f"{t.get('score_h4','—')}/100",
                result_str
            ])

        col_w = [32*mm, 18*mm, 14*mm, 20*mm, 20*mm, 20*mm, 18*mm, 18*mm, 18*mm]
        trade_table = Table(rows, colWidths=col_w, repeatRows=1)
        ts = TableStyle([
            ("BACKGROUND",  (0,0), (-1,0), DARK),
            ("TEXTCOLOR",   (0,0), (-1,0), LIGHT),
            ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",    (0,0), (-1,-1), 7.5),
            ("ALIGN",       (0,0), (-1,-1), "CENTER"),
            ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
            ("ROWHEIGHT",   (0,0), (-1,-1), 8*mm),
            ("GRID",        (0,0), (-1,-1), 0.3, HexColor("#334155")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT, HexColor("#f1f5f9")]),
        ])
        # Coloriser les résultats
        for i, t in enumerate(week_trades, start=1):
            result = t.get("result", "pending")
            col = GREEN if result=="TP" else RED if result=="SL" else GOLD
            ts.add("TEXTCOLOR", (8,i), (8,i), col)
            ts.add("FONTNAME",  (8,i), (8,i), "Helvetica-Bold")
            # Coloriser BUY/SELL
            sig_col = BLUE if t.get("signal")=="BUY" else RED
            ts.add("TEXTCOLOR", (2,i), (2,i), sig_col)
            ts.add("FONTNAME",  (2,i), (2,i), "Helvetica-Bold")
        trade_table.setStyle(ts)
        story.append(trade_table)
    else:
        story.append(Paragraph("Aucun trade cette semaine.", normal))

    story.append(Spacer(1, 12))

    # ── Stats par paire ──
    story.append(Paragraph("🌍 Stats par paire", h2_style))
    pair_map = {}
    for t in week_trades:
        p = t.get("pair", "?")
        if p not in pair_map:
            pair_map[p] = {"buy":0, "sell":0, "tp":0, "sl":0}
        if t.get("signal")=="BUY":  pair_map[p]["buy"]+=1
        else:                        pair_map[p]["sell"]+=1
        if t.get("result")=="TP":   pair_map[p]["tp"]+=1
        if t.get("result")=="SL":   pair_map[p]["sl"]+=1

    if pair_map:
        p_headers = ["Paire", "BUY", "SELL", "Total", "TP", "SL", "Win Rate"]
        p_rows = [p_headers]
        for pair, d in pair_map.items():
            tot = d["buy"]+d["sell"]
            wr_p = round(d["tp"]/(d["tp"]+d["sl"])*100) if (d["tp"]+d["sl"])>0 else 0
            p_rows.append([pair, str(d["buy"]), str(d["sell"]), str(tot),
                           str(d["tp"]), str(d["sl"]), f"{wr_p}%"])
        p_table = Table(p_rows, colWidths=[35*mm,20*mm,20*mm,20*mm,20*mm,20*mm,25*mm])
        p_table.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,0), DARK),
            ("TEXTCOLOR",   (0,0), (-1,0), LIGHT),
            ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",    (0,0), (-1,-1), 9),
            ("ALIGN",       (0,0), (-1,-1), "CENTER"),
            ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
            ("ROWHEIGHT",   (0,0), (-1,-1), 8*mm),
            ("GRID",        (0,0), (-1,-1), 0.3, HexColor("#334155")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT, HexColor("#f1f5f9")]),
        ]))
        story.append(p_table)

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"ChartAnalyzer Scanner v2 — Généré le {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC — Capital: 1000€ — Risque: 1%/trade",
        small))

    doc.build(story)
    buf.seek(0)
    return buf.read()

def send_weekly_pdf():
    """Envoie le journal PDF par email."""
    print("Génération du rapport PDF hebdomadaire...")
    history = read_history()

    now = datetime.now(timezone.utc)
    # Semaine du lundi au vendredi
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end   = week_start + timedelta(days=6)

    pdf_bytes = generate_pdf_report(history, week_start, week_end)

    ws = week_start.strftime("%d/%m")
    we = week_end.strftime("%d/%m/%Y")

    # Stats pour le sujet
    ws_str = week_start.strftime("%Y-%m-%d")
    we_str = week_end.strftime("%Y-%m-%d")
    week_trades = [s for s in history if ws_str <= (s.get("date") or "") <= we_str]
    tps  = sum(1 for t in week_trades if t.get("result")=="TP")
    sls  = sum(1 for t in week_trades if t.get("result")=="SL")
    pnl  = tps*20 - sls*10

    msg = MIMEMultipart()
    msg["Subject"] = f"📊 Journal Trading {ws}-{we} — {len(week_trades)} signaux — PnL: {'+' if pnl>=0 else ''}{pnl}€"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO

    html = f"""<!DOCTYPE html><html><body style="font-family:Arial;padding:16px;background:#f1f5f9">
<div style="max-width:500px;margin:auto;background:#1e293b;border-radius:16px;padding:24px;color:#fff;text-align:center">
  <div style="font-size:1.8rem;font-weight:900">📊 Journal de Trading</div>
  <div style="font-size:.9rem;opacity:.7;margin-top:6px">Semaine du {ws} au {we}</div>
</div>
<div style="max-width:500px;margin:12px auto;background:#fff;border-radius:16px;padding:20px;box-shadow:0 2px 12px rgba(0,0,0,.1)">
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px">
    <div style="background:#eff6ff;border-radius:10px;padding:14px;text-align:center">
      <div style="font-size:1.8rem;font-weight:900;color:#2563eb">{len(week_trades)}</div>
      <div style="font-size:.72rem;color:#64748b">SIGNAUX</div>
    </div>
    <div style="background:#f0fdf4;border-radius:10px;padding:14px;text-align:center">
      <div style="font-size:1.8rem;font-weight:900;color:#16a34a">{tps}</div>
      <div style="font-size:.72rem;color:#64748b">TP ✅</div>
    </div>
    <div style="background:#fef2f2;border-radius:10px;padding:14px;text-align:center">
      <div style="font-size:1.8rem;font-weight:900;color:#dc2626">{sls}</div>
      <div style="font-size:.72rem;color:#64748b">SL ❌</div>
    </div>
  </div>
  <div style="background:{'#f0fdf4' if pnl>=0 else '#fef2f2'};border-radius:10px;padding:16px;text-align:center;margin-bottom:12px">
    <div style="font-size:2rem;font-weight:900;color:{'#16a34a' if pnl>=0 else '#dc2626'}">
      {'+' if pnl>=0 else ''}{pnl}€
    </div>
    <div style="font-size:.75rem;color:#64748b">PnL simulé de la semaine</div>
  </div>
  <div style="background:#fffbeb;border-radius:10px;padding:12px;font-size:.8rem;color:#92400e;text-align:center">
    📎 Le journal complet est en pièce jointe (PDF)
  </div>
</div>
</body></html>"""

    msg.attach(MIMEText(html, "html", "utf-8"))

    if pdf_bytes:
        pdf_attach = MIMEApplication(pdf_bytes, _subtype="pdf")
        filename = f"journal_trading_{week_start.strftime('%Y_%m_%d')}.pdf"
        pdf_attach.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(pdf_attach)
        print(f"PDF généré: {len(pdf_bytes)} bytes")

    send_email(msg)
    print("Journal PDF envoyé !")

# ─────────────────────────────────────────────────────────────────
#  SCORING PAIRE DU JOUR (email matinal 7h UTC)
# ─────────────────────────────────────────────────────────────────
def send_morning_scoring():
    """Envoie le scoring des paires chaque matin."""
    print("Génération du scoring matinal...")
    history = read_history()
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    # Calculer le score de chaque paire sur les 7 derniers jours
    pair_scores = {}
    for pair in ["XAU/EUR", "XAU/USD", "XAG/USD"]:
        pair_id = pair.replace("/","")
        recent = [t for t in history
                  if t.get("pair","").replace("/","") == pair_id
                  and (t.get("date","")) >= week_ago]
        tps  = sum(1 for t in recent if t.get("result")=="TP")
        sls  = sum(1 for t in recent if t.get("result")=="SL")
        total = len(recent)
        wr   = round(tps/(tps+sls)*100) if (tps+sls)>0 else 50
        avg_score = round(sum(int(t.get("score_h1",65)) for t in recent)/total) if total>0 else 65
        # Score composite: win rate (50%) + score moyen (50%)
        composite = round(wr*0.5 + avg_score*0.5)
        pair_scores[pair] = {
            "wr": wr, "avg_score": avg_score,
            "composite": composite, "total": total,
            "tps": tps, "sls": sls
        }

    # Trier par score composite
    ranked = sorted(pair_scores.items(), key=lambda x: x[1]["composite"], reverse=True)

    def medal(i):
        return ["🥇","🥈","🥉"][i] if i < 3 else "  "

    rows_html = ""
    for i, (pair, s) in enumerate(ranked):
        color = "#16a34a" if s["composite"]>=65 else "#f59e0b" if s["composite"]>=50 else "#dc2626"
        rows_html += f"""
        <tr style="border-bottom:1px solid #f1f5f9">
          <td style="padding:10px;font-size:1.2rem">{medal(i)}</td>
          <td style="padding:10px;font-weight:800;font-size:.9rem">{pair}</td>
          <td style="padding:10px;text-align:center;font-weight:700;color:{color};font-size:1.1rem">{s['composite']}/100</td>
          <td style="padding:10px;text-align:center;font-size:.85rem;color:#16a34a">{s['wr']}%</td>
          <td style="padding:10px;text-align:center;font-size:.85rem">{s['total']} signaux</td>
        </tr>"""

    best_pair, best_data = ranked[0] if ranked else ("XAU/USD", {"composite":65})

    html = f"""<!DOCTYPE html><html><body style="font-family:Arial;padding:16px;background:#f1f5f9">
<div style="max-width:500px;margin:auto;background:linear-gradient(135deg,#1e293b,#0f172a);border-radius:16px;padding:24px;color:#fff;text-align:center">
  <div style="font-size:1.6rem;font-weight:900">🌅 Scoring du Jour</div>
  <div style="font-size:.85rem;opacity:.7;margin-top:4px">{now.strftime('%A %d/%m/%Y')} — {now.strftime('%H:%M')} UTC</div>
</div>
<div style="max-width:500px;margin:12px auto;background:#fff;border-radius:16px;padding:20px;box-shadow:0 2px 12px rgba(0,0,0,.1)">
  <div style="background:#eff6ff;border-radius:12px;padding:14px;margin-bottom:16px;text-align:center">
    <div style="font-size:.72rem;color:#64748b;margin-bottom:4px">⭐ MEILLEURE PAIRE DU JOUR</div>
    <div style="font-size:1.5rem;font-weight:900;color:#2563eb">{best_pair}</div>
    <div style="font-size:.8rem;color:#64748b">Score composite: {best_data['composite']}/100</div>
  </div>
  <table style="width:100%;border-collapse:collapse">
    <tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0">
      <th style="padding:8px;font-size:.72rem;color:#64748b;text-align:left">#</th>
      <th style="padding:8px;font-size:.72rem;color:#64748b;text-align:left">Paire</th>
      <th style="padding:8px;font-size:.72rem;color:#64748b">Score</th>
      <th style="padding:8px;font-size:.72rem;color:#64748b">Win Rate</th>
      <th style="padding:8px;font-size:.72rem;color:#64748b">Activité</th>
    </tr>
    {rows_html}
  </table>
  <div style="margin-top:14px;padding:12px;background:#f0fdf4;border-radius:10px;font-size:.78rem;color:#166534">
    💡 Concentre-toi sur <b>{best_pair}</b> aujourd'hui — meilleur historique cette semaine.
  </div>
  <div style="margin-top:8px;padding:10px;background:#fffbeb;border-radius:10px;font-size:.72rem;color:#92400e;text-align:center">
    ⚠️ Score basé sur l'historique des 7 derniers jours. Pas un conseil d'investissement.
  </div>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🌅 Scoring du {now.strftime('%d/%m')} — Meilleure paire: {best_pair} ({best_data['composite']}/100)"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    send_email(msg)
    print("Scoring matinal envoyé !")

# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    print(f"\nWeekly Report / Morning Scoring — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    missing = [k for k,v in [("EMAIL_FROM",EMAIL_FROM),("EMAIL_PASS",EMAIL_PASS),("EMAIL_TO",EMAIL_TO)] if not v]
    if missing:
        print(f"Variables manquantes: {', '.join(missing)}")
        sys.exit(1)

    if MODE == "morning":
        send_morning_scoring()
    else:
        send_weekly_pdf()

if __name__ == "__main__":
    main()
