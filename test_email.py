#!/usr/bin/env python3
"""
Test SMTP Brevo — essaie automatiquement les 2 logins possibles
"""
import os, smtplib, sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
EMAIL_TO   = os.getenv("EMAIL_TO",   "")

print("=== DIAGNOSTIC SMTP BREVO ===")
print("EMAIL_FROM : " + EMAIL_FROM)
print("EMAIL_PASS : " + str(len(EMAIL_PASS)) + " caracteres")
print("EMAIL_TO   : " + EMAIL_TO)
print("")

if not all([EMAIL_FROM, EMAIL_PASS, EMAIL_TO]):
    print("ERREUR: Variables EMAIL_FROM / EMAIL_PASS / EMAIL_TO manquantes")
    sys.exit(1)

def make_msg(login_used):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "ChartAnalyzer - Test email OK (login=" + login_used + ")"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    html = "<h2>Email de test recu !</h2><p>Login utilise : " + login_used + "</p>"
    msg.attach(MIMEText(html, "html"))
    return msg

def try_login(login):
    print("--- Tentative avec login: " + login + " ---")
    try:
        with smtplib.SMTP("smtp-relay.brevo.com", 587) as s:
            s.starttls()
            s.login(login, EMAIL_PASS)
            s.send_message(make_msg(login))
            print("SUCCES ! Login qui fonctionne : " + login)
            print("")
            print("==> Mets a jour le secret SMTP_LOGIN avec : " + login)
            return True
    except smtplib.SMTPAuthenticationError as e:
        print("Echec : " + str(e))
        return False
    except Exception as e:
        print("Erreur : " + str(e))
        return False

# Tentative 1 : email Gmail comme login
if try_login(EMAIL_FROM):
    sys.exit(0)

print("")

# Tentative 2 : extraire le login smtp-brevo depuis EMAIL_FROM
# Format typique Brevo : xxxxx@smtp-brevo.com
import re
# Essayer de deviner le login Brevo depuis le format du compte
smtp_login_guess = os.getenv("SMTP_LOGIN", "")
if smtp_login_guess:
    if try_login(smtp_login_guess):
        sys.exit(0)

print("")
print("=== ECHEC DES 2 TENTATIVES ===")
print("")
print("Solutions possibles:")
print("1. La cle SMTP EMAIL_PASS est peut-etre mal copiee")
print("   -> Aller sur app.brevo.com/settings/keys/smtp")
print("   -> Cliquer 'Generer une nouvelle cle SMTP'")
print("   -> Copier TOUTE la cle (sans espaces) dans EMAIL_PASS")
print("")
print("2. Verifier la page Brevo SMTP:")
print("   -> Serveur: smtp-relay.brevo.com")
print("   -> Port: 587")
print("   -> Connexion: c est cette valeur qui doit etre dans SMTP_LOGIN")
print("")
sys.exit(1)
