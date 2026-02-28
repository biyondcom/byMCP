"""
OAuth2 token management for Qonto payment endpoints.

Flow:
  1. On first run: browser opens for user authorization.
  2. Tokens cached at ~/.byMCP/qonto_tokens.json
     (access token: 1h, refresh token: 90 days).

Setup:
  Register an OAuth2 app in Qonto (Einstellungen → Integrationen → API → OAuth2).
  Redirect URI: http://localhost:7777/callback
  Scopes: payment.write organization.read offline_access
  Add QONTO_CLIENT_ID and QONTO_CLIENT_SECRET to .env.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import requests

_AUTH_URL = "https://oauth.qonto.com/oauth2/auth"
_TOKEN_URL = "https://oauth.qonto.com/oauth2/token"
_SCOPES = "payment.write organization.read offline_access"
_REDIRECT_URI = "http://localhost:7777/callback"
_TOKEN_FILE = Path.home() / ".byMCP" / "qonto_tokens.json"
_TIMEOUT = 30


class QontoOAuthError(Exception):
    """Raised when OAuth2 authorization fails."""


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


def _store_response(data: dict) -> dict:
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

def _refresh(client_id: str, client_secret: str, refresh_token: str) -> Optional[dict]:
    try:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return _store_response(resp.json())
    except requests.RequestException:
        pass
    return None


# ------------------------------------------------------------------
# Authorization code flow
# ------------------------------------------------------------------

def _authorize(client_id: str, client_secret: str) -> dict:
    state = secrets.token_urlsafe(16)
    auth_url = _AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": _SCOPES,
            "state": state,
        }
    )

    captured: dict = {}
    server_ready = threading.Event()
    server_done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = (params.get("code") or [None])[0]
            captured["state"] = (params.get("state") or [None])[0]
            captured["error"] = (params.get("error") or [None])[0]
            body = (
                b"<h2>Qonto-Autorisierung erfolgreich!</h2>"
                b"<p>Du kannst dieses Fenster schliessen.</p>"
                if captured.get("code")
                else b"<h2>Autorisierung fehlgeschlagen.</h2>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            server_done.set()

        def log_message(self, *_) -> None:  # type: ignore[override]
            pass

    with socketserver.TCPServer(("localhost", 7777), _Handler) as srv:
        srv.timeout = 1

        def _serve() -> None:
            server_ready.set()
            while not server_done.is_set():
                srv.handle_request()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        server_ready.wait()
        webbrowser.open(auth_url)
        if not server_done.wait(timeout=180):
            raise QontoOAuthError("Autorisierung abgelaufen (3 Minuten). Bitte erneut versuchen.")

    if captured.get("error"):
        raise QontoOAuthError(f"Autorisierung abgelehnt: {captured['error']}")
    if not captured.get("code"):
        raise QontoOAuthError("Kein Autorisierungscode erhalten.")
    if captured.get("state") != state:
        raise QontoOAuthError("OAuth2 State-Mismatch – Sicherheitsfehler.")

    resp = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": captured["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _REDIRECT_URI,
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise QontoOAuthError(
            f"Token-Austausch fehlgeschlagen (HTTP {resp.status_code}): {resp.text[:300]}"
        )
    return _store_response(resp.json())


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def get_valid_token(client_id: str, client_secret: str) -> str:
    """Return a valid OAuth2 access token (stored, refreshed, or new browser flow)."""
    tokens = _load_tokens()
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 60:
        return tokens["access_token"]
    if tokens.get("refresh_token"):
        refreshed = _refresh(client_id, client_secret, tokens["refresh_token"])
        if refreshed:
            return refreshed["access_token"]
    return _authorize(client_id, client_secret)["access_token"]


def needs_authorization(client_id: str, client_secret: str) -> bool:
    """Return True if browser-based authorization is required."""
    tokens = _load_tokens()
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 60:
        return False
    if tokens.get("refresh_token"):
        return _refresh(client_id, client_secret, tokens["refresh_token"]) is None
    return True


def clear_tokens() -> None:
    """Remove stored tokens (forces re-authorization on next call)."""
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()
