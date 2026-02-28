"""IBAN validation using MOD-97 algorithm (ISO 13616)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

# Country code → expected total IBAN length
IBAN_LENGTHS: Dict[str, int] = {
    "AL": 28, "AD": 24, "AT": 20, "AZ": 28, "BH": 22,
    "BE": 16, "BA": 20, "BR": 29, "BG": 22, "CR": 22,
    "HR": 21, "CY": 28, "CZ": 24, "DK": 18, "DO": 28,
    "EE": 20, "EG": 29, "FO": 18, "FI": 18, "FR": 27,
    "GE": 22, "DE": 22, "GI": 23, "GR": 27, "GL": 18,
    "GT": 28, "HU": 28, "IS": 26, "IQ": 23, "IE": 22,
    "IL": 23, "IT": 27, "JO": 30, "KZ": 20, "XK": 20,
    "KW": 30, "LV": 21, "LB": 28, "LI": 21, "LT": 20,
    "LU": 20, "MT": 31, "MR": 27, "MU": 30, "MD": 24,
    "MC": 27, "ME": 22, "NL": 18, "MK": 19, "NO": 15,
    "PK": 24, "PS": 29, "PL": 28, "PT": 25, "QA": 29,
    "RO": 24, "LC": 32, "SM": 27, "SA": 24, "RS": 22,
    "SK": 24, "SI": 19, "ES": 24, "SE": 24, "CH": 21,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "AE": 23,
    "GB": 22, "VA": 22, "VG": 24, "YE": 30,
}

_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]+$")


@dataclass
class ValidationResult:
    valid: bool
    masked: str
    error: str = ""


def _iban_to_int(iban: str) -> int:
    rearranged = iban[4:] + iban[:4]
    digits = "".join(
        str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged
    )
    return int(digits)


def _mask_iban(iban: str) -> str:
    if len(iban) < 8:
        return iban
    return iban[:4] + "*" * (len(iban) - 8) + iban[-4:]


def validate_iban(raw: str) -> ValidationResult:
    """Validate an IBAN string via MOD-97. Returns ValidationResult."""
    iban = raw.strip().replace(" ", "").upper()
    if len(iban) < 5:
        return ValidationResult(False, _mask_iban(iban), "IBAN ist zu kurz.")
    country = iban[:2]
    if not country.isalpha():
        return ValidationResult(False, _mask_iban(iban), "Ungultiger Laendercode.")
    expected_len = IBAN_LENGTHS.get(country)
    if expected_len is None:
        return ValidationResult(False, _mask_iban(iban), f"Unbekannter Laendercode: {country}")
    if len(iban) != expected_len:
        return ValidationResult(
            False,
            _mask_iban(iban),
            f"Falsche IBAN-Laenge fuer {country}: erwartet {expected_len}, erhalten {len(iban)}.",
        )
    if not _IBAN_RE.match(iban):
        return ValidationResult(False, _mask_iban(iban), "IBAN enthaelt ungueltige Zeichen.")
    if _iban_to_int(iban) % 97 != 1:
        return ValidationResult(False, _mask_iban(iban), "IBAN-Pruefsumme (MOD-97) ungueltig.")
    return ValidationResult(True, _mask_iban(iban))
