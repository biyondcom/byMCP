"""
Qonto transaction listing and receipt attachment.

Endpoints:
  GET  /transactions           – list transactions with filters
  POST /transactions/{id}/attachments – upload receipt file (multipart)
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests

from skills.payroll.qonto_oauth import get_valid_token
from utils.logger import logger

_DEFAULT_BASE = "https://thirdparty.qonto.com/v2"
_TIMEOUT = 30


class QontoTransactionError(Exception):
    pass


class QontoTransactionClient:
    def __init__(self) -> None:
        self._login = os.getenv("QONTO_LOGIN", "").strip()
        self._secret = os.getenv("QONTO_SECRET_KEY", "").strip()
        self._client_id = os.getenv("QONTO_CLIENT_ID", "").strip()
        self._client_secret = os.getenv("QONTO_CLIENT_SECRET", "").strip()
        self.debit_iban = os.getenv("QONTO_DEBIT_IBAN", "").strip()
        self.base_url = os.getenv("QONTO_API_BASE_URL", _DEFAULT_BASE).rstrip("/")

        missing = [
            name for name, val in [
                ("QONTO_LOGIN", self._login),
                ("QONTO_SECRET_KEY", self._secret),
                ("QONTO_DEBIT_IBAN", self.debit_iban),
            ] if not val
        ]
        if missing:
            raise QontoTransactionError(f"Umgebungsvariablen fehlen: {', '.join(missing)}")

        self._bank_account_id: str = self._resolve_account_id()

    def _api_key_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"{self._login}:{self._secret}",
            "Accept": "application/json",
        }

    def _oauth_headers(self) -> Dict[str, str]:
        token = get_valid_token(self._client_id, self._client_secret)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(1, 4):
            resp = requests.get(url, headers=self._api_key_headers(), params=params, timeout=_TIMEOUT)
            if resp.status_code in (429,) or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return resp
        return resp  # return last response even on repeated failure

    def _resolve_account_id(self) -> str:
        resp = self._get("/organizations/me")
        if resp.status_code != 200:
            raise QontoTransactionError(f"Konto-ID nicht abrufbar: HTTP {resp.status_code}")
        org = resp.json().get("organization", {})
        for account in org.get("bank_accounts", []):
            if account.get("iban") == self.debit_iban:
                return account["id"]
        raise QontoTransactionError(
            f"Kein Konto mit IBAN {self.debit_iban} gefunden."
        )

    def get_transactions_without_receipts(
        self,
        emitted_at_from: str,
        emitted_at_to: str,
        side: str = "debit",
    ) -> List[dict]:
        """
        Fetch completed debit transactions in the given date range that have no attachments.

        Args:
            emitted_at_from: ISO date string, e.g. '2026-01-01'
            emitted_at_to:   ISO date string, e.g. '2026-02-28'
            side:            'debit' or 'credit'

        Returns list of transaction dicts:
            id, label, amount, amount_cents, currency, emitted_at,
            reference, attachment_ids
        """
        params = {
            "bank_account_id": self._bank_account_id,
            "status[]": "completed",
            "side": side,
            "emitted_at_from": emitted_at_from,
            "emitted_at_to": emitted_at_to,
            "per_page": 100,
        }

        transactions = []
        page = 1
        while True:
            params["current_page"] = page
            resp = self._get("/transactions", params=params)
            if resp.status_code != 200:
                logger.error("Qonto Transaktionen: HTTP %d – %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            batch = data.get("transactions", [])
            for tx in batch:
                # Only include transactions without attachments
                if not tx.get("attachment_ids"):
                    transactions.append({
                        "id": tx.get("id", ""),
                        "label": tx.get("label", ""),
                        "amount": tx.get("amount", 0.0),
                        "amount_cents": round(tx.get("amount", 0.0) * 100),
                        "currency": tx.get("currency", "EUR"),
                        "emitted_at": tx.get("emitted_at", ""),
                        "reference": tx.get("reference", ""),
                        "attachment_ids": tx.get("attachment_ids", []),
                    })
            meta = data.get("meta", {})
            total_pages = meta.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        logger.info(
            "Qonto: %d Transaktionen ohne Beleg (%s – %s).",
            len(transactions), emitted_at_from, emitted_at_to,
        )
        return transactions

    def attach_receipt(
        self,
        transaction_id: str,
        file_bytes: bytes,
        filename: str,
        content_type: str = "application/pdf",
    ) -> bool:
        """
        Upload a receipt file and link it to the given Qonto transaction.

        Uses OAuth2 (payment scope) since attachment upload requires elevated permissions.
        Returns True on success.
        """
        url = f"{self.base_url}/transactions/{transaction_id}/attachments"
        headers = self._oauth_headers()
        # Do NOT set Content-Type here – requests sets it automatically for multipart
        del headers["Accept"]

        files = {"file": (filename, file_bytes, content_type)}

        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    timeout=60,
                )
            except requests.RequestException as exc:
                logger.warning("Beleg-Upload Verbindungsfehler (attempt %d): %s", attempt, exc)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code in (200, 201):
                logger.info(
                    "Beleg erfolgreich angehaengt: Transaction %s, Datei '%s'.",
                    transaction_id, filename,
                )
                return True

            if resp.status_code == 422:
                # Already attached or validation error
                logger.warning("Beleg-Upload 422: %s", resp.text[:300])
                return False

            if resp.status_code in (429,) or resp.status_code >= 500:
                logger.warning("Beleg-Upload HTTP %d, Retry %d/3.", resp.status_code, attempt)
                time.sleep(2 ** attempt)
                continue

            logger.error(
                "Beleg-Upload fehlgeschlagen: HTTP %d – %s",
                resp.status_code, resp.text[:300],
            )
            return False

        return False
