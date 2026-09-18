#!/usr/bin/env python3
"""
vacantes_monitor_adjudicadas.py

Vixía as vacantes pendentes en:
  https://www.edu.xunta.gal/substitutoslistas/VacantesPendentes.do

Cando se elimina unha listaxe, consulta as adxudicacións recentes
para intentar descubrir o nome do substituto ao que se lle adxudicou.

O informe do correo vai en galego.
"""

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------
# CONFIG — axusta o que queres vixiar
# --------------------------------------------------------------
VACANTES_URL = "https://www.edu.xunta.gal/substitutoslistas/VacantesPendentes.do"
ADJUDICADAS_URL = "https://www.edu.xunta.gal/substitutoslistas/SubstitucionsAdxudicadas.do"

SEARCH_PARAMS = {
    "corpo": "597",          # Mestres
    "especialidade": "32",   # Lingua estranxeira: Inglés
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
# CONFIG DE EMAIL — variables de contorno / GitHub Secrets
# --------------------------------------------------------------
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")


# --------------------------------------------------------------
# Utilidades de sesión e CSRF
# --------------------------------------------------------------
def get_csrf_token(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=20)
    r.raise_for_status()
    match = re.search(
        r'name="OWASP_CSRFTOKEN"\s+value=[\'"]([^\'"]+)[\'"]', r.text
    )
    if not match:
        raise RuntimeError(f"Non se puido obter o token CSRF de {url}")
    return match.group(1)


def fetch_form_and_session(url: str):
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(url, timeout=20)
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


# --------------------------------------------------------------
# Vacantes pendentes
# --------------------------------------------------------------
def submit_vacantes_search(session, hidden_fields):
    payload = dict(hidden_fields)
    payload.update(SEARCH_PARAMS)
    payload[SEARCH_EVENT_FIELD] = SEARCH_EVENT_VALUE

    resp = session.post(
        VACANTES_URL,
        data=payload,
        timeout=20,
        headers={
            **HEADERS,
            "Referer": VACANTES_URL,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp.raise_for_status()
    DEBUG_HTML_FILE.write_text(resp.text, encoding="utf-8")

    if "anomal" in resp.text.lower() or "xss.detect" in resp.text.lower():
        raise RuntimeError(
            "O filtro anti-abuso do sitio bloqueou o pedido — "
            "revisa debug_last_response.html."
        )
    return resp.text


def is_header_row(cells: list[str]) -> bool:
    """Detecta se a fila é a cabeceira da táboa (non unha vacante real)."""
    if not cells:
        return True
    joined = " ".join(cells).lower()
    # Palabras típicas da cabeceira
    header_keywords = (
        "data alta", "teléfono", "especialidade", "modalidade",
        "lingua da praza", "xornada", "data de inicio", "d.prev",
        "motivo", "observacións", "observacions",
    )
    hits = sum(1 for kw in header_keywords if kw in joined)
    # Se aparece o código de centro (8 díxitos) é case seguro unha fila real
    has_centro_code = any(re.search(r"\b\d{8}\b", c) for c in cells)
    if has_centro_code:
        return False
    return hits >= 2 or cells[0].lower().startswith("data alta")


def parse_listings(html: str) -> dict:
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
        if is_header_row(cells):
            continue
        row_text = " | ".join(cells)
        row_id = hashlib.sha1(row_text.encode("utf-8")).hexdigest()[:16]
        listings[row_id] = {"text": cells, "raw": row_text}
    return listings


def build_report(
    added: list,
    removed: list,
    current: dict,
    adjudications_cache: list[dict] | None = None,
) -> str:
    """Constrúe o texto completo do informe (usado no correo e no .txt)."""
    if adjudications_cache is None:
        adjudications_cache = []

    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    lines = [
        f"Informe de vacantes pendentes — {ts}",
        f"Filtro: corpo={SEARCH_PARAMS['corpo']} | especialidade={SEARCH_PARAMS['especialidade']}",
        "",
        "Resumo dos cambios dende a última consulta:",
        "",
    ]

    # --- Entradas engadidas ---
    if added:
        lines.append(f"➕ NOVAS listaxes ({len(added)}):")
        lines.append("-" * 50)
        for a in added:
            lines.append(f"  + {a['raw']}")
        lines.append("")
    else:
        lines.append("➕ Ningunha listaxe nova.")
        lines.append("")

    # --- Entradas eliminadas (con substituto se se atopa) ---
    if removed:
        lines.append(f"➖ Listaxes ELIMINADAS ({len(removed)}):")
        lines.append("-" * 50)
        for r in removed:
            substituto = find_substituto_for_removed(r, adjudications_cache)
            if substituto:
                lines.append(f"  - {r['raw']}")
                lines.append(f"    → Adxudicada a: {substituto}")
            else:
                lines.append(f"  - {r['raw']}")
                lines.append(
                    "    → Non se puido determinar o substituto "
                    "(aínda non aparece nas adxudicacións recentes)."
                )
        lines.append("")
    else:
        lines.append("➖ Ningunha listaxe eliminada.")
        lines.append("")

    # --- Listaxe actual completa ---
    lines.append("=" * 50)
    lines.append(f"📋 LISTAXE ACTUAL COMPLETA ({len(current)} listaxes)")
    lines.append("=" * 50)
    lines.append("")

    if not current:
        lines.append("(ningunha listaxe atopada)")
    else:
        for i, item in enumerate(current.values(), start=1):
            lines.append(f"{i}. {item['raw']}")

    return "\n".join(lines)


def write_listings_txt(report_text: str):
    """Escribe no ficheiro a mesma información que vai no correo."""
    LISTINGS_TXT_FILE.write_text(report_text, encoding="utf-8")


def load_previous_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(listings: dict):
    STATE_FILE.write_text(
        json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def diff_listings(old: dict, new: dict):
    old_ids, new_ids = set(old.keys()), set(new.keys())
    added = [new[i] for i in (new_ids - old_ids)]
    removed = [old[i] for i in (old_ids - new_ids)]
    return added, removed


# --------------------------------------------------------------
# Adxudicacións (para descubrir o substituto)
# --------------------------------------------------------------
def extract_centro_code(raw_text: str) -> str | None:
    """Intenta extraer o código numérico do centro (8 díxitos)."""
    match = re.search(r"\b(\d{8})\b", raw_text)
    return match.group(1) if match else None


def extract_centro_name(raw_text: str) -> str | None:
    """Intenta extraer o nome do centro despois do código."""
    match = re.search(r"\b\d{8}\s*[-–]\s*(.+?)(?:\s*\||$)", raw_text)
    if match:
        return match.group(1).strip()
    return None


def search_adjudications(
    corpo: str = "597",
    especialidade: str = "32",
    days_back: int = 3,
) -> list[dict]:
    """
    Busca adxudicacións recentes (últimos `days_back` días ata mañá).
    Devolve unha lista de dicionarios coa información de cada fila.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    csrf = get_csrf_token(session, ADJUDICADAS_URL)

    today = date.today()
    data_ini = (today - timedelta(days=days_back)).strftime("%d/%m/%Y")
    data_fin = (today + timedelta(days=1)).strftime("%d/%m/%Y")

    payload = {
        "OWASP_CSRFTOKEN": csrf,
        "corpo": corpo,
        "especialidade": especialidade,
        "provincia": "",
        "centro": "-1",
        "dataIni": data_ini,
        "dataFin": data_fin,
        "idDoc": "1",
        "nif": "",
        "DIALOG-EVENT-substitucionsAdxudicadas": "Buscar",
    }

    resp = session.post(
        ADJUDICADAS_URL,
        data=payload,
        timeout=25,
        headers={
            **HEADERS,
            "Referer": ADJUDICADAS_URL,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="fila")
    if not table:
        return []

    results = []
    for row in table.find_all("tr")[1:]:  # saltar cabeceira
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) >= 8:
            results.append(
                {
                    "data_alta": cols[0],
                    "data_adxudicacion": cols[1],
                    "telefono": cols[2],
                    "centro": cols[3],
                    "especialidade": cols[4],
                    "lingua": cols[5],
                    "xornada": cols[6],
                    "substituto": cols[7],
                }
            )
    return results


def find_substituto_for_removed(removed_item: dict, adjudications: list[dict]) -> str | None:
    """
    Tenta emparellar a vacante eliminada cunha adxudicación
    polo código ou nome do centro.
    """
    raw = removed_item.get("raw", "")
    code = extract_centro_code(raw)
    name = extract_centro_name(raw)

    for adj in adjudications:
        centro_adj = adj.get("centro", "")
        if code and code in centro_adj:
            return adj["substituto"]
        if name and name.lower() in centro_adj.lower():
            return adj["substituto"]
    return None


# --------------------------------------------------------------
# Correo electrónico (en galego)
# --------------------------------------------------------------
def send_email(report_text: str, added: list, removed: list):
    if not (EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO):
        print("Correo non configurado (faltan variables de contorno) — sáltase o envío.")
        return

    # Asunto
    subject_parts = []
    if added:
        subject_parts.append(f"{len(added)} nova(s)")
    if removed:
        subject_parts.append(f"{len(removed)} eliminada(s)")
    subject = f"Vacantes pendentes: {', '.join(subject_parts) if subject_parts else 'sen cambios'}"

    msg = MIMEText(report_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    recipients = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())
    print(f"Correo enviado a: {', '.join(recipients)}")


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------
def main():
    try:
        # 1. Consultar vacantes pendentes
        session, hidden_fields = fetch_form_and_session(VACANTES_URL)
        html = submit_vacantes_search(session, hidden_fields)
        current = parse_listings(html)
        previous = load_previous_state()

        added, removed = diff_listings(previous, current)

        # 2. Se hai eliminadas, buscar nas adxudicacións recentes
        adjudications = []
        if removed:
            print(f"Detectáronse {len(removed)} eliminada(s). Consultando adxudicacións…")
            adjudications = search_adjudications(
                corpo=SEARCH_PARAMS["corpo"],
                especialidade=SEARCH_PARAMS["especialidade"],
                days_back=3,
            )
            print(f"Atopáronse {len(adjudications)} adxudicación(s) recentes.")

        # 3. Construir o informe (igual para correo e ficheiro .txt)
        report_text = build_report(added, removed, current, adjudications)

        # 4. Sempre gardar o informe no ficheiro
        write_listings_txt(report_text)

        # 5. Enviar correo só se hai cambios
        if added or removed:
            print(f"{len(added)} nova(s), {len(removed)} eliminada(s).")
            send_email(report_text, added, removed)
        else:
            print("Sen cambios.")

        save_state(current)
        return 0

    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
