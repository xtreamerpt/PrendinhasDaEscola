#!/usr/bin/env python3
"""
test_email.py — testa SÓ o envio de email, sem tocar no site das vacantes.

Usa exatamente as mesmas variáveis de ambiente que vacantes_monitor.py usa
(EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO, e opcionalmente SMTP_SERVER/SMTP_PORT),
por isso se este script funcionar, sabes que os Secrets estão corretos e
o script principal também vai conseguir enviar.
"""

import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.mime.text import MIMEText

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")


def main():
    missing = [name for name, val in [
        ("EMAIL_FROM", EMAIL_FROM),
        ("EMAIL_PASSWORD", EMAIL_PASSWORD),
        ("EMAIL_TO", EMAIL_TO),
    ] if not val]

    if missing:
        print(f"ERRO: faltam estas variáveis de ambiente: {', '.join(missing)}")
        sys.exit(1)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recipients = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]

    msg = MIMEText(
        f"Este é um email de teste enviado em {ts}.\n\n"
        "Se recebeste isto, as credenciais SMTP (EMAIL_FROM / EMAIL_PASSWORD) "
        "e o destinatário (EMAIL_TO) estão configurados corretamente.",
        "plain",
        "utf-8",
    )
    msg["Subject"] = "Teste — vacantes_monitor"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    print(f"A ligar a {SMTP_SERVER}:{SMTP_PORT} como {EMAIL_FROM}...")
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
        print(f"OK — email de teste enviado para: {', '.join(recipients)}")
    except smtplib.SMTPAuthenticationError as exc:
        print(f"ERRO DE AUTENTICAÇÃO: {exc}")
        print("Verifica: 1) EMAIL_PASSWORD é a App Password de 16 carateres "
              "(não a password normal); 2) EMAIL_FROM está correto.")
        sys.exit(1)
    except Exception as exc:
        print(f"ERRO: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
