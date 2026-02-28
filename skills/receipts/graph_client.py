"""
Microsoft Graph API client – Email and attachment access.

Endpoints used:
  GET /me/messages          – list messages with filter
  GET /me/messages/{id}/attachments – list attachments of a message
  GET /me/messages/{id}/attachments/{aid}/$value – download attachment bytes
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from skills.receipts.ms_oauth import MsAuthError, get_valid_token
from utils.logger import logger

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TIMEOUT = 30


class GraphClient:
    def __init__(self, client_id: str, tenant_id: str) -> None:
        self._client_id = client_id
        self._tenant_id = tenant_id

    def _headers(self) -> Dict[str, str]:
        token = get_valid_token(self._client_id, self._tenant_id)
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{_GRAPH_BASE}/{path.lstrip('/')}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=_TIMEOUT)
        if resp.status_code == 401:
            raise MsAuthError("MS Graph: Autorisierung abgelaufen. Bitte 'receipts_authorize' aufrufen.")
        resp.raise_for_status()
        return resp.json()

    def _get_bytes(self, path: str) -> bytes:
        url = f"{_GRAPH_BASE}/{path.lstrip('/')}"
        resp = requests.get(url, headers=self._headers(), timeout=_TIMEOUT)
        if resp.status_code == 401:
            raise MsAuthError("MS Graph: Autorisierung abgelaufen.")
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------------

    def get_messages_with_attachments(
        self,
        since: datetime,
        until: datetime,
        max_results: int = 100,
    ) -> List[dict]:
        """
        Fetch emails with attachments received between since and until.

        Returns list of message dicts with keys:
          id, subject, from_address, from_name, received_at, body_preview
        """
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        until_iso = until.strftime("%Y-%m-%dT%H:%M:%SZ")

        filter_str = (
            f"hasAttachments eq true "
            f"and receivedDateTime ge {since_iso} "
            f"and receivedDateTime le {until_iso}"
        )

        messages = []
        params = {
            "$filter": filter_str,
            "$select": "id,subject,from,receivedDateTime,bodyPreview",
            "$top": min(max_results, 100),
            "$orderby": "receivedDateTime desc",
        }

        while len(messages) < max_results:
            data = self._get("/me/messages", params=params)
            for msg in data.get("value", []):
                sender = msg.get("from", {}).get("emailAddress", {})
                messages.append({
                    "id": msg["id"],
                    "subject": msg.get("subject", ""),
                    "from_address": sender.get("address", ""),
                    "from_name": sender.get("name", ""),
                    "received_at": msg.get("receivedDateTime", ""),
                    "body_preview": msg.get("bodyPreview", ""),
                })
            next_link = data.get("@odata.nextLink")
            if not next_link or len(messages) >= max_results:
                break
            # next_link is a full URL with params already included
            resp = requests.get(next_link, headers=self._headers(), timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            params = {}  # params already encoded in next_link

        logger.info("MS Graph: %d Mails mit Anhaengen gefunden.", len(messages))
        return messages[:max_results]

    def list_attachments(self, message_id: str) -> List[dict]:
        """
        List all non-inline attachments of a message.

        Returns list of dicts: id, name, content_type, size_bytes
        """
        data = self._get(
            f"/me/messages/{message_id}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
        )
        result = []
        for att in data.get("value", []):
            if att.get("isInline", False):
                continue  # skip inline images
            result.append({
                "id": att["id"],
                "name": att.get("name", "attachment"),
                "content_type": att.get("contentType", "application/octet-stream"),
                "size_bytes": att.get("size", 0),
            })
        return result

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download the raw bytes of an attachment."""
        # Prefer $value endpoint (raw content); fall back to base64 in JSON
        try:
            return self._get_bytes(
                f"/me/messages/{message_id}/attachments/{attachment_id}/$value"
            )
        except requests.HTTPError:
            # Fallback: get base64-encoded contentBytes from JSON
            data = self._get(
                f"/me/messages/{message_id}/attachments/{attachment_id}",
                params={"$select": "contentBytes"},
            )
            encoded = data.get("contentBytes", "")
            return base64.b64decode(encoded)
