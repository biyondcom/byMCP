"""
Qonto API client for SEPA transfers.

Required environment variables:
  QONTO_LOGIN          – Organisation login slug
  QONTO_SECRET_KEY     – API secret key (read-only endpoints)
  QONTO_DEBIT_IBAN     – IBAN of the company account to debit
  QONTO_CLIENT_ID      – OAuth2 client ID (payment endpoints)
  QONTO_CLIENT_SECRET  – OAuth2 client secret

Optional:
  QONTO_API_BASE_URL   – Override base URL (default: https://thirdparty.qonto.com/v2)

Transfer flow:
  1. Resolve bank_account_id via GET /organizations/me
  2. POST /sepa/verify_payee  → vop_proof_token   (OAuth2)
  3. POST /sepa/transfers     → 201 or 428 SCA    (OAuth2 + paired-device)
  4. On 428: poll GET /sca_sessions/{token} until user approves on smartphone
  5. Retry POST /sepa/transfers with X-Qonto-Sca-Session-Token
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from skills.payroll.qonto_oauth import QontoOAuthError, get_valid_token
from utils.logger import logger

_DEFAULT_BASE = "https://thirdparty.qonto.com/v2"
_TIMEOUT = 30


@dataclass
class TransferResult:
    success: bool
    transfer_id: Optional[str] = None
    error: str = ""
    status_code: Optional[int] = None


class QontoConfigError(Exception):
    """Raised when required environment variables are missing."""


class QontoClient:
    """Thread-safe Qonto API client."""

    def __init__(self) -> None:
        self._login = os.getenv("QONTO_LOGIN", "").strip()
        self._secret = os.getenv("QONTO_SECRET_KEY", "").strip()
        self.debit_iban = os.getenv("QONTO_DEBIT_IBAN", "").strip()
        self._client_id = os.getenv("QONTO_CLIENT_ID", "").strip()
        self._client_secret = os.getenv("QONTO_CLIENT_SECRET", "").strip()
        self.base_url = os.getenv("QONTO_API_BASE_URL", _DEFAULT_BASE).rstrip("/")

        missing = [
            name
            for name, val in [
                ("QONTO_LOGIN", self._login),
                ("QONTO_SECRET_KEY", self._secret),
                ("QONTO_DEBIT_IBAN", self.debit_iban),
                ("QONTO_CLIENT_ID", self._client_id),
                ("QONTO_CLIENT_SECRET", self._client_secret),
            ]
            if not val
        ]
        if missing:
            raise QontoConfigError(f"Umgebungsvariablen fehlen: {', '.join(missing)}")

        self._bank_account_id: str = self._resolve_bank_account_id()

    # ------------------------------------------------------------------

    def _base_headers(self, idempotency_key: Optional[str] = None) -> dict:
        headers = {
            "Authorization": f"{self._login}:{self._secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["X-Qonto-Idempotency-Key"] = idempotency_key
        return headers

    def _oauth_headers(self, idempotency_key: Optional[str] = None) -> dict:
        token = get_valid_token(self._client_id, self._client_secret)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["X-Qonto-Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        use_oauth: bool = False,
        extra_headers: Optional[dict] = None,
        retries: int = 3,
    ) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._oauth_headers(idempotency_key) if use_oauth else self._base_headers(idempotency_key)
        if extra_headers:
            headers.update(extra_headers)

        for attempt in range(1, retries + 1):
            try:
                resp = requests.request(method, url, json=json, headers=headers, timeout=_TIMEOUT)
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning("Qonto %s %s – HTTP %d. Retry %d/%d in %ds.", method, path, resp.status_code, attempt, retries, wait)
                    if attempt < retries:
                        time.sleep(wait)
                        continue
                return resp
            except requests.Timeout:
                logger.warning("Qonto timeout (attempt %d/%d).", attempt, retries)
                if attempt < retries:
                    time.sleep(2 ** attempt)
            except requests.ConnectionError as exc:
                logger.warning("Qonto connection error (attempt %d/%d): %s", attempt, retries, exc)
                if attempt < retries:
                    time.sleep(2 ** attempt)

        raise requests.ConnectionError("Qonto API nicht erreichbar nach mehreren Versuchen.")

    def _resolve_bank_account_id(self) -> str:
        try:
            resp = self._request("GET", "/organizations/me")
        except requests.ConnectionError as exc:
            raise QontoConfigError(f"Qonto-Verbindung fehlgeschlagen: {exc}") from exc

        if resp.status_code != 200:
            raise QontoConfigError(f"Konto-ID nicht abrufbar: HTTP {resp.status_code}")

        org = resp.json().get("organization", {})
        for account in org.get("bank_accounts", []):
            if account.get("iban") == self.debit_iban:
                account_id = account.get("id", "")
                logger.info("Qonto-Konto: %s (ID: %s)", org.get("legal_name", "-"), account_id)
                return account_id

        raise QontoConfigError(
            f"Kein Bankkonto mit IBAN {self.debit_iban} gefunden. "
            f"Verfuegbar: {[a.get('iban') for a in org.get('bank_accounts', [])]}"
        )

    def _verify_payee(self, iban: str, name: str) -> str:
        """POST /sepa/verify_payee → vop_proof_token (EU mandate since Oct 2025)."""
        try:
            resp = self._request(
                "POST", "/sepa/verify_payee",
                json={"iban": iban, "beneficiary_name": name},
                use_oauth=True,
            )
        except requests.ConnectionError as exc:
            raise ValueError(f"VOP-Verbindungsfehler: {exc}") from exc

        if resp.status_code != 200:
            raise ValueError(f"Payee-Verifizierung fehlgeschlagen: HTTP {resp.status_code} – {resp.text[:200]}")

        data = resp.json()
        match = data.get("match_result", "")
        if match == "MATCH_RESULT_MATCH":
            logger.info("VOP: Namensübereinstimmung fuer %s", name)
        elif match in ("MATCH_RESULT_CLOSE_MATCH", "MATCH_RESULT_NO_MATCH", "MATCH_RESULT_NOT_POSSIBLE"):
            logger.warning("VOP: %s fuer %s – Ueberweisung wird fortgesetzt.", match, name)

        return data["proof_token"]["token"]

    def _poll_sca_session(
        self,
        sca_session_token: str,
        log_callback: Optional[Callable[[str, str], None]] = None,
        timeout_seconds: int = 300,
    ) -> bool:
        """
        Poll GET /sca_sessions/{token} every 2s until approved or denied.
        Qonto returns {"result": "waiting"|"allow"|"deny", "canceled_at": ""}
        """
        deadline = time.time() + timeout_seconds
        last_log_at = 0.0

        while time.time() < deadline:
            now = time.time()
            if now - last_log_at >= 10:
                remaining = int(deadline - now)
                msg = f"Warte auf Genehmigung in Qonto-App … ({remaining}s verbleibend)"
                if log_callback:
                    log_callback("INFO", msg)
                logger.info(msg)
                last_log_at = now

            try:
                resp = self._request("GET", f"/sca_sessions/{sca_session_token}", use_oauth=True)
            except requests.RequestException as exc:
                logger.warning("SCA-Poll Verbindungsfehler: %s", exc)
                time.sleep(2)
                continue

            if resp.status_code == 200:
                body = resp.json()
                session_data = body.get("sca_session", body)
                result = session_data.get("result") or session_data.get("status", "")
                if result == "allow":
                    return True
                if result in ("deny", "denied", "rejected", "cancel"):
                    if log_callback:
                        log_callback("WARNING", "SCA-Freigabe abgelehnt.")
                    return False
            elif resp.status_code not in (404, 425):
                logger.warning("SCA-Poll HTTP %d: %s", resp.status_code, resp.text[:200])

            time.sleep(2)

        if log_callback:
            log_callback("WARNING", "SCA-Timeout: Genehmigung nicht erhalten (5 Min.).")
        return False

    # ------------------------------------------------------------------

    def create_transfer(
        self,
        *,
        credit_name: str,
        credit_iban: str,
        amount_cents: int,
        period: str,
        idempotency_key: str,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ) -> TransferResult:
        """
        Create a SEPA credit transfer with SCA smartphone approval.

        Flow:
          1. POST /sepa/verify_payee  → vop_proof_token
          2. POST /sepa/transfers     → 201 success or 428 SCA challenge
          3. On 428: poll /sca_sessions/{token}, wait for smartphone approval
          4. Retry POST /sepa/transfers with X-Qonto-Sca-Session-Token
        """
        logger.info("Ueberweisung fuer %s (%s, %d Cent) …", credit_name, period, amount_cents)

        try:
            vop_token = self._verify_payee(credit_iban, credit_name)
        except Exception as exc:
            logger.error("VOP fehlgeschlagen fuer %s: %s", credit_name, exc)
            return TransferResult(success=False, error=f"VOP fehlgeschlagen: {exc}")

        payload = {
            "vop_proof_token": vop_token,
            "transfer": {
                "bank_account_id": self._bank_account_id,
                "beneficiary": {"name": credit_name, "iban": credit_iban},
                "reference": f"Gehalt {period}",
                "amount": f"{amount_cents / 100:.2f}",
                "note": f"Gehalt {period}",
            },
        }
        sca_headers = {"X-Qonto-2fa-Preference": "paired-device"}

        try:
            resp = self._request(
                "POST", "/sepa/transfers",
                json=payload,
                idempotency_key=idempotency_key,
                use_oauth=True,
                extra_headers=sca_headers,
            )
        except requests.ConnectionError as exc:
            return TransferResult(success=False, error=str(exc))

        if resp.status_code == 428:
            try:
                sca_token = resp.json().get("sca_session_token", "")
            except Exception:
                sca_token = ""
            if not sca_token:
                return TransferResult(success=False, error="SCA erforderlich, aber kein sca_session_token.", status_code=428)

            msg = f"Bitte Ueberweisung fuer {credit_name} in Qonto-App bestaetigen …"
            logger.info(msg)
            if log_callback:
                log_callback("INFO", msg)

            if not self._poll_sca_session(sca_token, log_callback=log_callback):
                return TransferResult(success=False, error="SCA-Genehmigung verweigert oder Timeout.", status_code=428)

            try:
                resp = self._request(
                    "POST", "/sepa/transfers",
                    json=payload,
                    idempotency_key=idempotency_key,
                    use_oauth=True,
                    extra_headers={**sca_headers, "X-Qonto-Sca-Session-Token": sca_token},
                )
            except requests.ConnectionError as exc:
                return TransferResult(success=False, error=str(exc))

        return self._parse_transfer_response(resp, credit_name)

    def _parse_transfer_response(self, resp: requests.Response, name: str) -> TransferResult:
        if resp.status_code in (200, 201):
            body = resp.json()
            t = body.get("transfer", body)
            tid = str(t.get("id") or t.get("uuid", ""))
            logger.info("Ueberweisung OK: %s → ID %s", name, tid)
            return TransferResult(success=True, transfer_id=tid, status_code=resp.status_code)

        if resp.status_code == 422:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            if "idempotency" in str(detail).lower() or "already" in str(detail).lower():
                logger.info("Ueberweisung bereits vorhanden (Idempotenz): %s", name)
                return TransferResult(success=True, error="Bereits verarbeitet (Idempotenz)", status_code=422)
            error_msg = _extract_error(detail)
            logger.error("Validierungsfehler fuer %s: %s", name, error_msg)
            return TransferResult(success=False, error=error_msg, status_code=422)

        try:
            error_body = resp.json()
        except Exception:
            error_body = resp.text
        error_msg = _extract_error(error_body)
        logger.error("Qonto Fehler %d fuer %s: %s", resp.status_code, name, error_msg)
        return TransferResult(success=False, error=error_msg, status_code=resp.status_code)


def _extract_error(body: object) -> str:
    if isinstance(body, dict):
        errors = body.get("errors", [])
        if errors and isinstance(errors, list):
            return "; ".join(e.get("message", str(e)) for e in errors if isinstance(e, dict))
        return body.get("message", str(body))
    return str(body)
