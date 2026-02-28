"""
Microsoft OAuth2 – Device Code Flow for Microsoft Graph API.

Setup (einmalig):
  1. Azure Portal → App-Registrierungen → Neue Registrierung
     - Kontotyp: "Konten in einem Organisationsverzeichnis und persönliche Microsoft-Konten"
     - Kein Redirect URI nötig (Device Code Flow)
  2. Unter "API-Berechtigungen" hinzufügen:
     - Microsoft Graph → Delegierte Berechtigungen: Mail.Read, offline_access
  3. Unter "Authentifizierung" → "Öffentliche Clientflows zulassen" → Ja
  4. Client-ID und Tenant-ID aus der Übersichtsseite in .env eintragen.

Token-Cache: ~/.byMCP/ms_tokens.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import requests

_TOKEN_FILE = Path.home() / ".byMCP" / "ms_tokens.json"
_FLOW_FILE = Path.home() / ".byMCP" / "ms_device_flow.json"
_GRAPH_SCOPE = "https://graph.microsoft.com/Mail.Read Sites.ReadWrite.All offline_access"
_TIMEOUT = 30


class MsAuthError(Exception):
    """Raised when Microsoft authentication fails."""


# ------------------------------------------------------------------
# Token persistence
# ------------------------------------------------------------------

def _load_tokens() -> dict:
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_tokens(tokens: dict) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def _store(data: dict) -> dict:
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + data.get("expires_in", 3600),
    }
    _save_tokens(tokens)
    return tokens


# ------------------------------------------------------------------
# Token refresh
# ------------------------------------------------------------------

def _refresh(client_id: str, tenant_id: str, refresh_token: str) -> Optional[dict]:
    try:
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": _GRAPH_SCOPE,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return _store(resp.json())
    except requests.RequestException:
        pass
    return None


# ------------------------------------------------------------------
# Device Code Flow
# ------------------------------------------------------------------

def initiate_device_code_flow(client_id: str, tenant_id: str) -> dict:
    """
    Start the device code flow. Returns the full response dict containing:
      user_code, verification_uri, device_code, expires_in, interval, message
    """
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/devicecode",
        data={"client_id": client_id, "scope": _GRAPH_SCOPE},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise MsAuthError(
            f"Device Code Flow fehlgeschlagen (HTTP {resp.status_code}): {resp.text[:300]}"
        )
    return resp.json()


def poll_device_code(
    client_id: str,
    tenant_id: str,
    device_code: str,
    interval: int = 5,
    expires_in: int = 900,
) -> dict:
    """
    Poll the token endpoint until the user completes authorization or it expires.
    Returns the token dict on success, raises MsAuthError on failure.
    """
    deadline = time.time() + expires_in
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    while time.time() < deadline:
        time.sleep(interval)
        try:
            resp = requests.post(
                token_url,
                data={
                    "client_id": client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MsAuthError(f"Verbindungsfehler beim Token-Polling: {exc}") from exc

        data = resp.json()
        error = data.get("error", "")

        if resp.status_code == 200:
            return _store(data)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "authorization_declined":
            raise MsAuthError("Autorisierung abgelehnt.")
        if error == "expired_token":
            raise MsAuthError("Device Code abgelaufen. Bitte erneut versuchen.")
        raise MsAuthError(f"Token-Fehler: {error} – {data.get('error_description', '')}")

    raise MsAuthError("Autorisierung abgelaufen. Bitte erneut versuchen.")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def get_valid_token(client_id: str, tenant_id: str) -> str:
    """Return a valid MS Graph access token (cached or refreshed)."""
    tokens = _load_tokens()
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 60:
        return tokens["access_token"]
    if tokens.get("refresh_token"):
        refreshed = _refresh(client_id, tenant_id, tokens["refresh_token"])
        if refreshed:
            return refreshed["access_token"]
    raise MsAuthError(
        "Kein gueltiges Token vorhanden. Bitte zuerst 'receipts_authorize' aufrufen."
    )


def needs_authorization(client_id: str, tenant_id: str) -> bool:
    tokens = _load_tokens()
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 60:
        return False
    if tokens.get("refresh_token"):
        return _refresh(client_id, tenant_id, tokens["refresh_token"]) is None
    return True


def clear_tokens() -> None:
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()


# ------------------------------------------------------------------
# Pending device flow persistence
# ------------------------------------------------------------------

def save_pending_flow(flow: dict) -> None:
    """Persist a started device code flow so it can be polled later."""
    _FLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    _FLOW_FILE.write_text(json.dumps(flow, indent=2), encoding="utf-8")


def load_pending_flow() -> Optional[dict]:
    """Load a previously started device code flow, or None if none exists."""
    if _FLOW_FILE.exists():
        try:
            return json.loads(_FLOW_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def clear_pending_flow() -> None:
    if _FLOW_FILE.exists():
        _FLOW_FILE.unlink()
