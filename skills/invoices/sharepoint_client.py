"""
SharePoint List client via Microsoft Graph API.

Supports:
  - Site ID resolution
  - Column discovery (with Lookup column detection)
  - Item creation in SharePoint lists
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlparse

import requests

from skills.receipts.ms_oauth import MsAuthError, get_valid_token
from utils.logger import logger

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Field maps: InvoiceData field → possible SharePoint column names
# ---------------------------------------------------------------------------

_INVOICE_FIELD_MAP: dict[str, list[str]] = {
    "rechnungsnummer":     ["Rechnungsnummer", "Invoice Number", "Title"],
    "rechnungsdatum":      ["Rechnungsdatum", "Invoice Date", "Datum"],
    "lieferant":           ["Lieferant", "Supplier", "Title", "Vendor"],
    "lieferant_ust_id_nr": ["USt-IdNr", "Lieferant USt-IdNr", "VAT ID"],
    "nettobetrag":         ["Nettobetrag", "Net Amount", "Netto"],
    "mwst_betrag":         ["MwSt Betrag", "VAT Amount", "MwSt"],
    "bruttobetrag":        ["Bruttobetrag", "Gross Amount", "Gesamtbetrag"],
    "zahlungsziel":        ["Zahlungsziel", "Due Date", "Fälligkeitsdatum"],
    "bestellnummer":       ["Bestellnummer", "Order Number", "PO Number"],
}

_POSITION_FIELD_MAP: dict[str, list[str]] = {
    "position":     ["Position", "Pos", "Nr", "Title"],
    "beschreibung": ["Beschreibung", "Bezeichnung", "Artikel", "Title"],
    "menge":        ["Menge", "Quantity", "Anzahl"],
    "einheit":      ["Einheit", "Unit"],
    "einzelpreis":  ["Einzelpreis", "Unit Price", "EP"],
    "mwst_satz":    ["MwSt Satz", "VAT Rate", "Steuersatz"],
    "gesamtpreis":  ["Gesamtpreis", "Total", "Summe"],
}


# ---------------------------------------------------------------------------

class SharePointError(Exception):
    """Raised when a SharePoint Graph API call fails."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SharePointClient:
    def __init__(self, site_url: str, client_id: str, tenant_id: str) -> None:
        self._site_url = site_url.rstrip("/")
        self._client_id = client_id
        self._tenant_id = tenant_id
        self._site_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        token = get_valid_token(self._client_id, self._tenant_id)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict:
        url = f"{_GRAPH_BASE}/{path.lstrip('/')}"
        resp = requests.get(url, headers=self._headers(), timeout=_TIMEOUT)
        if resp.status_code == 401:
            raise MsAuthError(
                "MS Graph: Autorisierung abgelaufen. Bitte 'receipts_authorize' aufrufen."
            )
        if not resp.ok:
            raise SharePointError(
                f"GET {url} → HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        import json
        url = f"{_GRAPH_BASE}/{path.lstrip('/')}"
        resp = requests.post(
            url,
            headers=self._headers(),
            data=json.dumps(body),
            timeout=_TIMEOUT,
        )
        if resp.status_code == 401:
            raise MsAuthError(
                "MS Graph: Autorisierung abgelaufen. Bitte 'receipts_authorize' aufrufen."
            )
        if not resp.ok:
            raise SharePointError(
                f"POST {url} → HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Site resolution
    # ------------------------------------------------------------------

    def _resolve_site_id(self) -> str:
        if self._site_id:
            return self._site_id

        parsed = urlparse(self._site_url)
        hostname = parsed.netloc
        path = parsed.path.rstrip("/")

        data = self._get(f"/sites/{hostname}:{path}")
        self._site_id = data["id"]
        logger.info("SharePoint site resolved: %s → %s", self._site_url, self._site_id)
        return self._site_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_list_columns(self, list_name: str) -> dict[str, str]:
        """
        Discover writable columns of a SharePoint list.

        Returns dict of {display_name_lower → internal_name}.
        Lookup columns are prefixed with "lookup:" in the value.
        """
        site_id = self._resolve_site_id()
        data = self._get(f"/sites/{site_id}/lists/{list_name}/columns")

        columns: dict[str, str] = {}
        for col in data.get("value", []):
            if col.get("hidden") or col.get("readOnly"):
                continue
            display = col.get("displayName", "")
            internal = col.get("name", display)
            is_lookup = col.get("lookup") is not None
            value = f"lookup:{internal}" if is_lookup else internal
            columns[display.lower()] = value

        logger.info(
            "SharePoint list '%s': %d writable columns discovered.", list_name, len(columns)
        )
        return columns

    def create_item(self, list_name: str, fields: dict) -> int:
        """
        Create a new item in a SharePoint list.

        Returns the integer item ID.
        """
        site_id = self._resolve_site_id()
        body = {"fields": fields}
        result = self._post(f"/sites/{site_id}/lists/{list_name}/items", body)
        item_id: int = result.get("id") or result.get("fields", {}).get("id", 0)
        # Graph returns id as string in some versions
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            item_id = 0
        logger.info("SharePoint list '%s': item created with ID %s.", list_name, item_id)
        return item_id


# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------

def map_invoice_fields(
    invoice_dict: dict,
    columns: dict[str, str],
) -> tuple[dict, list[str]]:
    """
    Map InvoiceData header fields to SharePoint column internal names.

    Returns (fields_dict, warnings).
    """
    fields: dict = {}
    warnings: list[str] = []

    for data_key, aliases in _INVOICE_FIELD_MAP.items():
        value = invoice_dict.get(data_key)
        if value is None:
            continue
        internal = _find_column(aliases, columns)
        if internal is None:
            warnings.append(f"Kein SharePoint-Feld für '{data_key}' gefunden – übersprungen.")
            continue
        fields[internal] = value

    return fields, warnings


def map_position_fields(
    pos_dict: dict,
    columns: dict[str, str],
    lookup_col_internal: str,
    header_item_id: int,
) -> tuple[dict, list[str]]:
    """
    Map InvoicePosition fields to SharePoint column internal names.
    Sets the lookup field pointing to the header item.

    Returns (fields_dict, warnings).
    """
    fields: dict = {}
    warnings: list[str] = []

    for data_key, aliases in _POSITION_FIELD_MAP.items():
        value = pos_dict.get(data_key)
        if value is None:
            continue
        internal = _find_column(aliases, columns)
        if internal is None:
            warnings.append(f"Kein SharePoint-Feld für Position '{data_key}' gefunden – übersprungen.")
            continue
        fields[internal] = value

    # Set lookup to header
    if lookup_col_internal:
        fields[f"{lookup_col_internal}LookupId"] = header_item_id

    return fields, warnings


def _find_column(aliases: list[str], columns: dict[str, str]) -> Optional[str]:
    """
    Find the first matching column internal name for a list of aliases.

    columns keys are lowercase display names; values may be "lookup:<internal>" or plain internal name.
    Returns the bare internal name (without "lookup:" prefix), or None.
    """
    for alias in aliases:
        val = columns.get(alias.lower())
        if val is not None:
            if val.startswith("lookup:"):
                return val[len("lookup:"):]
            return val
    return None


def find_lookup_column(columns: dict[str, str]) -> Optional[str]:
    """
    Find the first lookup column in the position list columns dict.

    Returns the bare internal name of the lookup column, or None.
    """
    for _display, internal in columns.items():
        if internal.startswith("lookup:"):
            return internal[len("lookup:"):]
    return None
