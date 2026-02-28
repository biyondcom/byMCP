"""
PDF-Extraktion für Rechnungen.

Extrahiert Kopfdaten (Rechnungsnummer, Datum, Beträge, …) und
Positionsdaten (Tabellen) aus Rechnungs-PDFs.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional

import pdfplumber

from utils.logger import logger

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class InvoicePosition:
    position: Optional[int] = None
    beschreibung: Optional[str] = None
    menge: Optional[str] = None
    einheit: Optional[str] = None
    einzelpreis: Optional[str] = None
    mwst_satz: Optional[str] = None
    gesamtpreis: Optional[str] = None


@dataclass
class InvoiceData:
    rechnungsnummer: Optional[str] = None
    rechnungsdatum: Optional[str] = None       # normalisiert: YYYY-MM-DD
    lieferant: Optional[str] = None
    lieferant_ust_id_nr: Optional[str] = None
    nettobetrag: Optional[str] = None
    mwst_betrag: Optional[str] = None
    bruttobetrag: Optional[str] = None
    zahlungsziel: Optional[str] = None
    bestellnummer: Optional[str] = None
    positionen: List[InvoicePosition] = field(default_factory=list)
    source_filename: Optional[str] = None
    extracted_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["positionen"] = [asdict(p) for p in self.positionen]
        return d

    @staticmethod
    def from_dict(d: dict) -> "InvoiceData":
        positionen = [InvoicePosition(**p) for p in d.pop("positionen", [])]
        return InvoiceData(positionen=positionen, **d)


# ---------------------------------------------------------------------------
# Regex patterns for header fields
# ---------------------------------------------------------------------------

_SEP = r"\s*[:\-=]?\s*"
_AMT = r"(\d{1,3}(?:\.\d{3})*,\d{2})"   # deutsches Dezimalformat
_DATE = r"(\d{1,2}[./]\d{1,2}[./]\d{2,4})"

PATTERNS: dict[str, str] = {
    "rechnungsnummer":     r"(?i)rechnungs(?:nummer|nr\.?|no\.?)" + _SEP + r"([A-Z0-9\-/]+)",
    "rechnungsdatum":      r"(?i)rechnungs(?:datum|date)" + _SEP + _DATE,
    "lieferant_ust_id_nr": r"(?i)ust\.?[-\s]?id\.?[-\s]?nr\.?" + _SEP + r"(DE\d{9})",
    "nettobetrag":         r"(?i)netto(?:betrag|summe)?" + _SEP + _AMT,
    "mwst_betrag":         r"(?i)(?:mwst|ust|mehrwertsteuer)\.?\s*\d*\s*%?" + _SEP + _AMT,
    "bruttobetrag":        r"(?i)(?:brutto|gesamtbetrag|zu\s+zahlen|rechnungsbetrag)" + _SEP + _AMT,
    "zahlungsziel":        r"(?i)(?:zahlungsziel|faellig|fällig|bis\s+zum)" + _SEP + _DATE,
    "bestellnummer":       r"(?i)(?:bestell(?:nr|nummer)|auftrag(?:snr|snummer)?)" + _SEP + r"([A-Z0-9\-/]+)",
}

# ---------------------------------------------------------------------------
# Column alias sets for position table detection
# ---------------------------------------------------------------------------

_COL_ALIASES: dict[str, set[str]] = {
    "position":     {"pos", "position", "nr", "#"},
    "beschreibung": {"beschreibung", "bezeichnung", "artikel", "leistung", "text"},
    "menge":        {"menge", "anz", "anzahl", "qty"},
    "einheit":      {"einheit", "einh", "unit"},
    "einzelpreis":  {"einzelpreis", "ep", "stückpreis", "stuckpreis"},
    "mwst_satz":    {"mwst", "ust", "steuer", "tax"},
    "gesamtpreis":  {"gesamtpreis", "gesamt", "summe", "total"},
}


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

def _normalize_date(raw: str) -> str:
    """Try to parse a German-style date (D.M.YY or D/M/YYYY) to YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw  # return as-is if unparseable


# ---------------------------------------------------------------------------
# Supplier heuristic
# ---------------------------------------------------------------------------

def _guess_lieferant(text: str) -> Optional[str]:
    """Return the first non-trivial text block from the document as supplier name."""
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 3 and not re.match(r"(?i)rechnung|invoice|seite|page|\d+", line):
            return line
    return None


# ---------------------------------------------------------------------------
# Position extraction from tables
# ---------------------------------------------------------------------------

def _match_header_col(header_cell: str) -> Optional[str]:
    """Map a table column header to our internal field name, or None."""
    if not header_cell:
        return None
    cell = header_cell.strip().lower()
    for field_name, aliases in _COL_ALIASES.items():
        if cell in aliases:
            return field_name
    return None


def _extract_positions(pdf_bytes: bytes) -> tuple[List[InvoicePosition], List[str]]:
    """
    Scan all pages for invoice position tables.

    Returns (positions, warnings).
    """
    warnings: List[str] = []
    positions: List[InvoicePosition] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Try to find a header row
                header_row = table[0]
                if not any(header_row):
                    continue

                col_map: dict[int, str] = {}  # col_index → field_name
                for i, cell in enumerate(header_row):
                    field_name = _match_header_col(str(cell or ""))
                    if field_name:
                        col_map[i] = field_name

                if "beschreibung" not in col_map.values() and "gesamtpreis" not in col_map.values():
                    continue  # probably not an invoice position table

                for row in table[1:]:
                    if not any(row):
                        continue
                    kwargs: dict = {}
                    for i, field_name in col_map.items():
                        val = str(row[i] or "").strip() if i < len(row) else ""
                        if not val:
                            continue
                        if field_name == "position":
                            try:
                                kwargs["position"] = int(val)
                            except ValueError:
                                kwargs["position"] = None
                        else:
                            kwargs[field_name] = val
                    if kwargs:
                        positions.append(InvoicePosition(**kwargs))

    if not positions:
        warnings.append("Keine Positionstabelle erkannt – manuelle Prüfung empfohlen.")

    return positions, warnings


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_invoice(pdf_bytes: bytes, source_filename: Optional[str] = None) -> tuple[InvoiceData, List[str]]:
    """
    Extract invoice header and positions from PDF bytes.

    Returns (InvoiceData, warnings).
    """
    warnings: List[str] = []

    # --- Extract full text ---
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(
            page.extract_text() or "" for page in pdf.pages
        )

    data = InvoiceData(
        source_filename=source_filename,
        extracted_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    # --- Apply regex patterns ---
    for field_name, pattern in PATTERNS.items():
        m = re.search(pattern, full_text)
        if m:
            value = m.group(1).strip()
            if field_name in ("rechnungsdatum", "zahlungsziel"):
                value = _normalize_date(value)
            setattr(data, field_name, value)
        else:
            warnings.append(f"Feld '{field_name}' nicht gefunden.")

    # --- Supplier heuristic ---
    data.lieferant = _guess_lieferant(full_text)
    if not data.lieferant:
        warnings.append("Lieferant konnte nicht automatisch ermittelt werden.")
    else:
        warnings.append(f"Lieferant (Heuristik, bitte prüfen): '{data.lieferant}'")

    # --- Positions ---
    positions, pos_warnings = _extract_positions(pdf_bytes)
    data.positionen = positions
    warnings.extend(pos_warnings)

    logger.info(
        "invoice extracted: %s | %d positions | %d warnings",
        source_filename,
        len(positions),
        len(warnings),
    )
    return data, warnings
