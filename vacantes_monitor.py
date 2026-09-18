#!/usr/bin/env python3
"""
vacantes_monitor.py — versão GitHub Actions

Verifica https://www.edu.xunta.gal/substitutoslistas/VacantesPendentes.do
e envia um email sempre que há listagens novas ou removidas.
"""

import hashlib
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------
# CONFIG — ajusta consoante o que queres vigiar
# --------------------------------------------------------------
BASE_URL = "https://www.edu.xunta.gal/substitutoslistas/VacantesPendentes.do"

SEARCH_PARAMS = {
    "corpo": "597",
    "especialidade": "32",
    "provincia": "",
    "centro": "",
    "dataIni": "",
    "dataFin": "",
    "sort": "listado.dataAlta",
    "dir": "desc",
}

SEARCH_EVENT_FIELD = "DIALOG-EVENT-vacantesPendentes"
SEARCH_EVENT_VALUE = "Buscar"

STATE_FILE = Path("state.json")
DEBUG_HTML_FILE = Path("debug_last_response.html")
LISTINGS_TXT_FILE = Path("current_listings.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "gl,es;q=0.9,en;q=0.8",
}

# --------------------------------------------------------------
# CONFIG DE EMAIL — lido de variáveis de ambiente / GitHub Secrets
# --------------------------------------------------------------
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")


def fetch_form_and_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(BASE_URL, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form")
    hidden_fields = {}
    if form:
        for inp in form.find_all("input", type="hidden"):
            name = inp.get("name")
            if name:
                hidden_fields[name] = inp.get("value", "")
    return session, hidden_fields


def submit_search(session, hidden_fields):
    payload = dict(hidden_fields)
    payload.update(SEARCH_PARAMS)
    payload[SEARCH_EVENT_FIELD] = SEARCH_EVENT_VALUE

    resp = session.post(
        BASE_URL,
        data=payload,
        timeout=20,
        headers={**HEADERS, "Referer": BASE_URL,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    DEBUG_HTML_FILE.write_text(resp.text, encoding="utf-8")

    if "anomal" in resp.text.lower() or "xss.detect" in resp.text.lower():
        raise RuntimeError(
            "O filtro anti-abuso do site bloqueou o pedido — "
            "verifica debug_last_response.html."
        )
    return resp.text


def parse_listings(html):
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return {}

    results_table = max(tables, key=lambda t: len(t.find_all("tr")))
    listings = {}
    for row in results_table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if not cells or not any(cells):
            continue
        row_text = " | ".join(cells)
        row_id = hashlib.sha1(row_text.encode("utf-8")).hexdigest()[:16]
        listings[row_id] = {"text": cells, "raw": row_text}
    return listings


def write_listings_txt(listings):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Vacantes pendentes — snapshot em {ts}",
        f"Filtro: corpo={SEARCH_PARAMS['corpo']} "
        f"especialidade={SEARCH_PARAMS['especialidade']}",
        f"Total de listagens: {len(listings)}",
        "=" * 60,
        "",
    ]
    if not listings:
        lines.append("(nenhuma listagem encontrada)")
    else:
        for i, item in enumerate(listings.values(), start=1):
            lines.append(f"{i}. {item['raw']}")
    LISTINGS_TXT_FILE.write_text("\n".join(lines), encoding="utf-8")


def load_previous_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(listings):
    STATE_FILE.write_text(json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8")


def diff_listings(old, new):
    old_ids, new_ids = set(old.keys()), set(new.keys())
    added = [new[i] for i in (new_ids - old_ids)]
    removed = [old[i] for i in (old_ids - new_ids)]
    return added, removed


def send_email(added, removed):
    if not (EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO):
        print("Email não configurado (faltam variáveis de ambiente) — a saltar envio.")
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"Alterações detetadas em {ts}", ""]

    if added:
        lines.append(f"NOVAS listagens ({len(added)}):")
        for a in added:
            lines.append(f"  + {a['raw']}")
        lines.append("")

    if removed:
        lines.append(f"Listagens REMOVIDAS ({len(removed)}):")
        for r in removed:
            lines.append(f"  - {r['raw']}")
        lines.append("")

    body = "\n".join(lines)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Vacantes pendentes: {len(added)} nova(s), {len(removed)} removida(s)"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    recipients = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())

    print(f"Email enviado para: {', '.join(recipients)}")


def main():
    try:
        session, hidden_fields = fetch_form_and_session()
        html = submit_search(session, hidden_fields)
        current = parse_listings(html)
        previous = load_previous_state()

        added, removed = diff_listings(previous, current)

        if added or removed:
            print(f"{len(added)} nova(s), {len(removed)} removida(s).")
            send_email(added, removed)
        else:
            print("Sem alterações.")

        write_listings_txt(current)
        save_state(current)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
