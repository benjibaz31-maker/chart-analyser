#!/usr/bin/env python3
"""Script de test SMTP standalone — diagnostic email"""
import os, smtplib, sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

EMAIL_FROM = os.getenv("EMAIL_FROM",  "")
SMTP_LOGIN = os.getenv("SMTP_LOGIN",  "")
EMAIL_PASS = os.getenv("EMAIL_PASS",  "")
EMAIL_TO   = os.getenv("EMAIL_TO",    "")

if not SMTP_LOGIN:
    SMTP_LOGIN = EMAIL_FROM

print("EMAIL_FROM  : " + EMAIL_FROM)
print("SMTP_LOGIN  : " + SMTP_LOGIN)
print("EMAIL_PASS  : " + ("*" * len(EMAIL_PASS) if EMAIL_PASS else "MANQUANT"))
print("EMAIL_TO    : " + EMAIL_TO)

if not all([EMAIL_FROM, EMAIL_PASS, EMAIL_TO]):
    print("ERREUR: Variables manquantes !")
    sys.exit(1)

msg = MIMEMultipart("alternative")
msg["Subject"] = "TEST ChartAnalyzer - Email OK !"
msg["From"]    = EMAIL_FROM
msg["To"]      = EMAIL_TO
msg.attach(MIMEText("<h2>Email de test recu !</h2><p>Brevo fonctionne.</p>", "html"))

print("")
print("Connexion SMTP Brevo (smtp-relay.brevo.com:587)...")
try:
    with smtplib.SMTP("smtp-relay.brevo.com", 587) as s:
        print("  connexion OK")
        s.starttls()
        print("  TLS OK")
        s.login(SMTP_LOGIN, EMAIL_PASS)
        print("  login OK")
        s.send_message(msg)
        print("OK - EMAIL ENVOYE avec succes !")
except smtplib.SMTPAuthenticationError as e:
    print("ERREUR authentification : " + str(e))
    print("  Verifier SMTP_LOGIN=" + SMTP_LOGIN + " et EMAIL_PASS")
    sys.exit(1)
except Exception as e:
    print("ERREUR : " + str(type(e).__name__) + " : " + str(e))
    sys.exit(1)
