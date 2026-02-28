"""CSV parser for employee payroll data."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Union

from utils.iban_validator import validate_iban
from utils.logger import logger

REQUIRED_COLUMNS = {"name", "iban", "zielordner"}


@dataclass
class Employee:
    name: str
    iban: str
    iban_masked: str
    target_dir: Path

    # Filled during PDF processing
    page_index: int = -1
    amount_cents: int = 0
    pdf_saved: bool = False

    def __str__(self) -> str:
        return f"{self.name} ({self.iban_masked})"


@dataclass
class ParseResult:
    employees: List[Employee] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0 and len(self.employees) > 0


def parse_csv(path: Union[str, Path]) -> ParseResult:
    """
    Parse a payroll CSV and return validated employees.

    Expected format (UTF-8 or latin-1, comma or semicolon separated):
        name,iban,zielordner
        Max Mustermann,DE12500105170648489890,/pfad/zum/ordner
    """
    result = ParseResult()
    path = Path(path)

    if not path.exists():
        result.errors.append(f"CSV-Datei nicht gefunden: {path}")
        return result

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            result.errors.append("CSV-Datei konnte nicht gelesen werden (Encoding-Fehler).")
            return result

    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

    if reader.fieldnames is None:
        result.errors.append("CSV hat keine Kopfzeile.")
        return result

    fieldnames_lower = {f.strip().lower(): f for f in reader.fieldnames}
    missing = REQUIRED_COLUMNS - set(fieldnames_lower.keys())
    if missing:
        result.errors.append(
            f"CSV fehlen Spalten: {', '.join(sorted(missing))}. "
            f"Erwartet: {', '.join(sorted(REQUIRED_COLUMNS))}."
        )
        return result

    name_col = fieldnames_lower["name"]
    iban_col = fieldnames_lower["iban"]
    dir_col = fieldnames_lower["zielordner"]

    for row_num, row in enumerate(reader, start=2):
        name = (row.get(name_col) or "").strip()
        raw_iban = (row.get(iban_col) or "").strip().replace(" ", "").upper()
        raw_dir = (row.get(dir_col) or "").strip()

        if not name:
            result.errors.append(f"Zeile {row_num}: Kein Name angegeben.")
            continue

        iban_result = validate_iban(raw_iban)
        if not iban_result.valid:
            result.errors.append(f"Zeile {row_num} ({name}): IBAN ungueltig – {iban_result.error}")
            continue

        if not raw_dir:
            result.errors.append(f"Zeile {row_num} ({name}): Kein Zielordner angegeben.")
            continue

        target_dir = Path(raw_dir).expanduser().resolve()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.errors.append(
                f"Zeile {row_num} ({name}): Zielordner konnte nicht erstellt werden: {exc}"
            )
            continue

        result.employees.append(
            Employee(name=name, iban=raw_iban, iban_masked=iban_result.masked, target_dir=target_dir)
        )
        logger.debug("Mitarbeiter geladen: %s -> %s", name, target_dir)

    if not result.employees and not result.errors:
        result.errors.append("CSV enthaelt keine gueltigen Eintraege.")

    return result
