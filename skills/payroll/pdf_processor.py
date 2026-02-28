"""
PDF processing: text extraction, employee matching, amount extraction, page splitting.

Libraries:
  pdfplumber  – text extraction from complex layouts
  pypdf       – page splitting and writing
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

import pdfplumber
from pypdf import PdfReader, PdfWriter

from skills.payroll.csv_parser import Employee
from utils.logger import logger

# ------------------------------------------------------------------
# Amount extraction patterns (German payroll conventions)
# ------------------------------------------------------------------
_SEP = r"\s*[:\-=]?\s*"
_AMT = r"(\d{1,3}(?:\.\d{3})*,\d{2})"
_EUR = r"(?:(?:EUR|€)\s*)?"

_AMOUNT_PATTERNS: List[re.Pattern] = [  # type: ignore[type-arg]
    # Column-header format: "Auszahlungsbetrag" as table header, value on next line
    re.compile(rf"(?i)auszahlungsbetrag\s*\n[^\n]*(?<!\d)(?<!\.){_AMT}"),
    re.compile(rf"(?i)auszahlungsbetrag{_SEP}{_EUR}{_AMT}"),
    re.compile(rf"(?i)nettolohn{_SEP}{_EUR}{_AMT}"),
    re.compile(rf"(?i)nettogehalt{_SEP}{_EUR}{_AMT}"),
    re.compile(rf"(?i)netto{_SEP}{_EUR}{_AMT}"),
    re.compile(rf"(?i)überweisung\w*{_SEP}{_EUR}{_AMT}"),
    re.compile(rf"(?i)zahlbetrag{_SEP}{_EUR}{_AMT}"),
    re.compile(rf"(?i)betrag{_SEP}{_EUR}{_AMT}"),
    re.compile(rf"{_AMT}\s*(?:EUR|€)"),
]


@dataclass
class ProcessingResult:
    saved_files: List[Path] = field(default_factory=list)
    unmatched_pages: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _german_amount_to_cents(amount_str: str) -> int:
    return round(float(amount_str.replace(".", "").replace(",", ".")) * 100)


def _make_filename(name: str, period: str) -> str:
    """
    YYYYMM_<1st of first name><1st of last name><2nd of last name>
    Example: 'Michael Richter' / '2026-02' → '202602_MRI'
    """
    period_compact = period.replace("-", "")
    parts = name.strip().split()
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else first
    return f"{period_compact}_{(first[:1] + last[:2]).upper()}"


def _extract_text(pdf_path: Path, page_index: int) -> str:
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if page_index >= len(pdf.pages):
                return ""
            return pdf.pages[page_index].extract_text(x_tolerance=3, y_tolerance=3) or ""
    except Exception as exc:
        logger.warning("Textextraktion Seite %d fehlgeschlagen: %s", page_index + 1, exc)
        return ""


def _extract_amount(text: str) -> int:
    for pattern in _AMOUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                cents = _german_amount_to_cents(match.group(1))
                if cents > 0:
                    return cents
            except (ValueError, OverflowError):
                continue
    return 0


def _score_name_match(text: str, name: str) -> float:
    text_lower = text.lower()
    name_lower = name.lower().strip()
    if name_lower in text_lower:
        return 1.0
    parts = name_lower.split()
    if len(parts) < 2:
        return 1.0 if parts[0] in text_lower else 0.0
    if all(part in text_lower for part in parts):
        return 0.7
    if parts[-1] in text_lower:
        return 0.4
    if parts[0] in text_lower:
        return 0.2
    return 0.0


def process_pdf(
    pdf_path: Path,
    employees: List[Employee],
    period: Optional[str] = None,
    skip_save: Optional[Set[str]] = None,
) -> ProcessingResult:
    """
    Extract all pages, match to employees, save individual PDFs.

    Args:
        pdf_path:  Path to the multi-page payroll PDF.
        employees: Validated employee list from CSV.
        period:    'YYYY-MM' (defaults to current month).
        skip_save: Employee names whose PDF should not be saved.
    """
    if period is None:
        period = datetime.now().strftime("%Y-%m")
    if skip_save is None:
        skip_save = set()

    result = ProcessingResult()

    try:
        reader = PdfReader(str(pdf_path))
        num_pages = len(reader.pages)
    except Exception as exc:
        result.errors.append(f"PDF konnte nicht geoeffnet werden: {exc}")
        return result

    for idx in range(num_pages):
        text = _extract_text(pdf_path, idx)
        amount = _extract_amount(text)

        # Find best matching employee
        best_score = 0.0
        best_emp: Optional[Employee] = None
        for emp in employees:
            score = _score_name_match(text, emp.name)
            if score > best_score:
                best_score = score
                best_emp = emp

        if best_emp is None or best_score < 0.4:
            result.unmatched_pages.append(idx + 1)
            logger.warning("Seite %d: kein Mitarbeiter gefunden (Score %.2f).", idx + 1, best_score)
            continue

        best_emp.page_index = idx
        best_emp.amount_cents = amount
        logger.info("Seite %d -> %s (Score %.2f, %d Cent)", idx + 1, best_emp.name, best_score, amount)

        if best_emp.name in skip_save:
            logger.info("PDF-Speicherung uebersprungen: %s", best_emp.name)
            continue

        # Save page
        output_path = best_emp.target_dir / f"{_make_filename(best_emp.name, period)}.pdf"
        try:
            writer = PdfWriter()
            writer.add_page(reader.pages[idx])
            with open(output_path, "wb") as fh:
                writer.write(fh)
            result.saved_files.append(output_path)
            best_emp.pdf_saved = True
            logger.info("PDF gespeichert: %s", output_path)
        except OSError as exc:
            result.errors.append(f"Seite {idx + 1} ({best_emp.name}): {exc}")

    return result
