#!/usr/bin/env python3
"""Script de test SMTP standalone — diagnostic email"""
import os, smtplib, sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
EMAIL_TO   = os.getenv("EMAIL_TO",   "")

print(f"EMAIL_FROM : {EMAIL_FROM}")
print(f"EMAIL_PASS : {'*' * len(EMAIL_PASS) if EMAIL_PASS else 'MANQUANT'}")
print(f"EMAIL_TO   : {EMAIL_TO}")

if not all([EMAIL_FROM, EMAIL_PASS, EMAIL_TO]):
    print("❌ Variables manquantes !")
    sys.exit(1)

msg = MIMEMultipart("alternative")
msg["Subject"] = "✅ TEST ChartAnalyzer — Email OK !"
msg["From"]    = EMAIL_FROM
msg["To"]      = EMAIL_TO
msg.attach(MIMEText("<h2>✅ Email de test reçu !</h2><p>Brevo fonctionne.</p>", "html"))

print("\nConnexion SMTP Brevo (smtp-relay.brevo.com:587)...")
try:
    with smtplib.SMTP("smtp-relay.brevo.com", 587) as s:
        print("  → connexion OK")
        s.starttls()
        print("  → TLS OK")
        s.login(EMAIL_FROM, EMAIL_PASS)
        print("  → login OK")
        s.send_message(msg)
        print("✅ EMAIL ENVOYÉ avec succès !")
except smtplib.SMTPAuthenticationError as e:
    print(f"❌ Erreur authentification : {e}")
    print("   → Vérifie EMAIL_FROM et EMAIL_PASS (clé SMTP Brevo)")
except smtplib.SMTPConnectError as e:
    print(f"❌ Erreur connexion : {e}")
except Exception as e:
    print(f"❌ Erreur : {type(e).__name__}: {e}")
