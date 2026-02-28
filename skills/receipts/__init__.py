"""
Receipts skill – automatische Belegzuordnung.

Workflow:
  1. receipts_authorize        – einmalige MS-Autorisierung (Device Code Flow)
  2. receipts_find_candidates  – Qonto-Transaktionen ohne Beleg + passende Mails
  3. receipts_attach           – Beleg an Transaktion anhängen (nach Bestätigung)
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


# ------------------------------------------------------------------
# Matching helpers
# ------------------------------------------------------------------

def _amount_variants(amount: float) -> List[str]:
    """Generate common string representations of an amount for fuzzy search."""
    variants = set()
    for val in (amount, round(amount)):
        variants.add(f"{val:.2f}")
        variants.add(f"{val:.2f}".replace(".", ","))
        variants.add(str(int(val)))
        # German thousand separator: 1.234,56
        if val >= 1000:
            german = f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            variants.add(german)
    return list(variants)


def _score_match(tx: dict, msg: dict) -> float:
    """
    Score how well an email matches a Qonto transaction (0.0 – 1.0).

    Signals:
      - Amount found in subject or body preview  (+0.5)
      - Any word of tx label in sender address   (+0.3)
      - Any word of tx label in subject          (+0.2)
    """
    score = 0.0
    subject = (msg.get("subject") or "").lower()
    body = (msg.get("body_preview") or "").lower()
    sender = (msg.get("from_address") or "").lower()
    label = (tx.get("label") or "").lower()

    # Amount matching
    for variant in _amount_variants(tx.get("amount", 0)):
        if variant in subject or variant in body:
            score += 0.5
            break

    # Label words in sender
    label_words = [w for w in re.split(r"\W+", label) if len(w) > 3]
    if any(w in sender for w in label_words):
        score += 0.3
    elif any(w in subject for w in label_words):
        score += 0.2

    return min(score, 1.0)


def _fmt_tx(tx: dict) -> str:
    date = tx.get("emitted_at", "")[:10]
    return f"{date}  {tx['label']:<35}  {tx['amount']:>10.2f} {tx['currency']}"


def _fmt_msg(msg: dict, attachments: list) -> str:
    date = msg.get("received_at", "")[:10]
    att_names = ", ".join(a["name"] for a in attachments)
    return (
        f"  Von:     {msg['from_name']} <{msg['from_address']}>\n"
        f"  Datum:   {date}\n"
        f"  Betreff: {msg['subject']}\n"
        f"  Anhang:  {att_names}"
    )


# ------------------------------------------------------------------

def register_tools(mcp: "FastMCP") -> None:

    @mcp.tool()
    def receipts_authorize() -> str:
        """
        Startet die Microsoft Office 365 Autorisierung via Device Code Flow.

        Gibt sofort einen Code und eine URL zurück. Der Benutzer öffnet die URL
        und gibt den Code ein. Danach receipts_authorize_complete aufrufen.

        Muss nur einmal ausgeführt werden – Token werden danach automatisch erneuert.
        """
        client_id = os.getenv("MS_CLIENT_ID", "").strip()
        tenant_id = os.getenv("MS_TENANT_ID", "common").strip()

        if not client_id:
            return (
                "MS_CLIENT_ID fehlt in .env.\n\n"
                "Setup:\n"
                "  1. Azure Portal → App-Registrierungen → Neue Registrierung\n"
                "  2. API-Berechtigungen: Microsoft Graph → Mail.Read, offline_access\n"
                "  3. Authentifizierung → 'Öffentliche Clientflows zulassen' → Ja\n"
                "  4. Client-ID und Tenant-ID in .env eintragen."
            )

        from skills.receipts.ms_oauth import (
            clear_pending_flow,
            initiate_device_code_flow,
            needs_authorization,
            save_pending_flow,
        )

        if not needs_authorization(client_id, tenant_id):
            return "Bereits autorisiert. Token ist gueltig."

        flow = initiate_device_code_flow(client_id, tenant_id)
        save_pending_flow({
            "client_id": client_id,
            "tenant_id": tenant_id,
            "device_code": flow["device_code"],
            "interval": flow.get("interval", 5),
            "expires_in": flow.get("expires_in", 900),
        })

        url = flow.get("verification_uri", "https://microsoft.com/devicelogin")
        user_code = flow.get("user_code", "")
        return (
            f"Bitte oeffne diese URL und gib den Code ein:\n\n"
            f"  URL:  {url}\n"
            f"  Code: {user_code}\n\n"
            f"Danach 'receipts_authorize_complete' aufrufen."
        )

    @mcp.tool()
    def receipts_authorize_complete() -> str:
        """
        Wartet auf die Bestätigung des Microsoft Device Code Flows.

        Muss nach receipts_authorize aufgerufen werden, nachdem der Benutzer
        den Code unter https://microsoft.com/devicelogin eingegeben hat.
        Wartet bis zu 15 Minuten auf die Bestätigung.
        """
        from skills.receipts.ms_oauth import (
            clear_pending_flow,
            load_pending_flow,
            poll_device_code,
        )

        flow = load_pending_flow()
        if not flow:
            return (
                "Kein laufender Autorisierungsvorgang gefunden.\n"
                "Bitte zuerst 'receipts_authorize' aufrufen."
            )

        try:
            poll_device_code(
                client_id=flow["client_id"],
                tenant_id=flow["tenant_id"],
                device_code=flow["device_code"],
                interval=flow.get("interval", 5),
                expires_in=flow.get("expires_in", 900),
            )
        except Exception as exc:
            clear_pending_flow()
            return f"Autorisierung fehlgeschlagen: {exc}"

        clear_pending_flow()
        return "Autorisierung erfolgreich! Office 365 Token gespeichert."

    # ------------------------------------------------------------------

    @mcp.tool()
    def receipts_find_candidates(
        days_back: int = 60,
        min_score: float = 0.3,
        email_window_days: int = 14,
    ) -> str:
        """
        Findet Qonto-Transaktionen ohne Beleg und sucht passende Mails mit Anhängen.

        Für jede Transaktion ohne Beleg werden Mails aus einem Zeitfenster rund um
        das Transaktionsdatum gesucht. Mails werden nach Betrag und Absender/Betreff
        mit der Transaktion abgeglichen und nach Übereinstimmung bewertet.

        Args:
            days_back:        Wie viele Tage zurück Transaktionen gesucht werden (Standard: 60).
            min_score:        Minimale Übereinstimmungspunktzahl 0.0–1.0 (Standard: 0.3).
            email_window_days: Zeitfenster ±Tage rund um die Transaktion für Mail-Suche (Standard: 14).

        Gibt eine Liste von Kandidaten zurück. Zur Bestätigung bitte 'receipts_attach' aufrufen.
        """
        client_id = os.getenv("MS_CLIENT_ID", "").strip()
        tenant_id = os.getenv("MS_TENANT_ID", "common").strip()

        from skills.receipts.graph_client import GraphClient
        from skills.receipts.ms_oauth import MsAuthError
        from skills.receipts.qonto_transactions import QontoTransactionClient, QontoTransactionError

        # ── Qonto ──────────────────────────────────────────────────────
        try:
            qonto = QontoTransactionClient()
        except QontoTransactionError as exc:
            return f"Qonto-Konfigurationsfehler: {exc}"

        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

        transactions = qonto.get_transactions_without_receipts(date_from, date_to)
        if not transactions:
            return f"Keine Transaktionen ohne Beleg in den letzten {days_back} Tagen gefunden."

        # ── MS Graph ───────────────────────────────────────────────────
        try:
            graph = GraphClient(client_id, tenant_id)
        except MsAuthError as exc:
            return f"MS Graph Fehler: {exc}"

        # Fetch all emails in the broad window (oldest tx date – today + buffer)
        oldest_tx = min(tx["emitted_at"][:10] for tx in transactions)
        email_since = datetime.fromisoformat(oldest_tx) - timedelta(days=email_window_days)
        email_since = email_since.replace(tzinfo=timezone.utc)

        try:
            all_messages = graph.get_messages_with_attachments(
                since=email_since, until=now
            )
        except MsAuthError as exc:
            return f"Office 365 Fehler: {exc}\nBitte zuerst 'receipts_authorize' ausfuehren."
        except Exception as exc:
            return f"Fehler beim Abrufen der Mails: {exc}"

        # ── Matching ───────────────────────────────────────────────────
        output_lines = [
            f"Transaktionen ohne Beleg: {len(transactions)}",
            f"E-Mails mit Anhaengen im Zeitraum: {len(all_messages)}",
            "",
        ]

        found_any = False
        for tx in transactions:
            tx_date = datetime.fromisoformat(tx["emitted_at"].replace("Z", "+00:00"))
            window_start = tx_date - timedelta(days=email_window_days)
            window_end = tx_date + timedelta(days=email_window_days)

            # Filter emails within the window
            candidates = []
            for msg in all_messages:
                try:
                    msg_date = datetime.fromisoformat(
                        msg["received_at"].replace("Z", "+00:00")
                    )
                except (ValueError, KeyError):
                    continue
                if not (window_start <= msg_date <= window_end):
                    continue
                score = _score_match(tx, msg)
                if score >= min_score:
                    candidates.append((score, msg))

            candidates.sort(key=lambda x: x[0], reverse=True)

            output_lines.append(f"── Transaktion ────────────────────────────────────────────")
            output_lines.append(f"   {_fmt_tx(tx)}")
            output_lines.append(f"   ID: {tx['id']}")

            if not candidates:
                output_lines.append("   Keine passenden Mails gefunden.")
            else:
                found_any = True
                output_lines.append(f"   Kandidaten ({len(candidates)}):")
                for i, (score, msg) in enumerate(candidates[:3], 1):
                    try:
                        attachments = graph.list_attachments(msg["id"])
                    except Exception:
                        attachments = []
                    att_list = ", ".join(
                        f"{a['name']} ({a['size_bytes'] // 1024} KB)" for a in attachments
                    ) or "(keine Dateianhaenge)"
                    output_lines.append(
                        f"\n   [{i}] Score: {score:.0%}\n"
                        f"       Von:     {msg['from_name']} <{msg['from_address']}>\n"
                        f"       Datum:   {msg['received_at'][:10]}\n"
                        f"       Betreff: {msg['subject']}\n"
                        f"       Anhang:  {att_list}\n"
                        f"       Mail-ID: {msg['id']}\n"
                        f"       Anhang-IDs: {[a['id'] for a in attachments]}"
                    )
            output_lines.append("")

        if not found_any:
            output_lines.append(
                "Keine passenden Mails fuer die Transaktionen gefunden.\n"
                "Tipp: min_score verringern oder email_window_days erhoehen."
            )
        else:
            output_lines.append(
                "Zum Anhaengen: receipts_attach(\n"
                "  transaction_id='<ID>',\n"
                "  message_id='<Mail-ID>',\n"
                "  attachment_id='<Anhang-ID>'\n"
                ")"
            )

        return "\n".join(output_lines)

    # ------------------------------------------------------------------

    @mcp.tool()
    def receipts_attach(
        transaction_id: str,
        message_id: str,
        attachment_id: str,
    ) -> str:
        """
        Haengt einen E-Mail-Anhang als Beleg an eine Qonto-Transaktion.

        Vorher mit receipts_find_candidates die IDs ermitteln und vom Benutzer bestätigen lassen.

        Args:
            transaction_id:  Qonto-Transaktions-ID (aus receipts_find_candidates).
            message_id:      Microsoft Graph Nachrichten-ID der E-Mail.
            attachment_id:   Microsoft Graph Anhang-ID der Datei.
        """
        client_id = os.getenv("MS_CLIENT_ID", "").strip()
        tenant_id = os.getenv("MS_TENANT_ID", "common").strip()

        from skills.receipts.graph_client import GraphClient
        from skills.receipts.ms_oauth import MsAuthError
        from skills.receipts.qonto_transactions import QontoTransactionClient, QontoTransactionError

        # ── Download attachment from Office 365 ────────────────────────
        try:
            graph = GraphClient(client_id, tenant_id)
            attachments = graph.list_attachments(message_id)
            att_info = next((a for a in attachments if a["id"] == attachment_id), None)
            if att_info is None:
                return f"Anhang '{attachment_id}' nicht gefunden in Mail '{message_id}'."

            file_bytes = graph.download_attachment(message_id, attachment_id)
        except MsAuthError as exc:
            return f"Office 365 Fehler: {exc}\nBitte 'receipts_authorize' ausfuehren."
        except Exception as exc:
            return f"Fehler beim Herunterladen des Anhangs: {exc}"

        filename = att_info["name"]
        content_type = att_info.get("content_type", "application/pdf")

        # ── Upload to Qonto ────────────────────────────────────────────
        try:
            qonto = QontoTransactionClient()
        except QontoTransactionError as exc:
            return f"Qonto-Konfigurationsfehler: {exc}"

        success = qonto.attach_receipt(transaction_id, file_bytes, filename, content_type)

        if success:
            size_kb = len(file_bytes) // 1024
            return (
                f"Beleg erfolgreich angehaengt!\n"
                f"  Transaktion: {transaction_id}\n"
                f"  Datei:       {filename} ({size_kb} KB)\n"
                f"  Typ:         {content_type}"
            )
        else:
            return (
                f"Beleg-Upload fehlgeschlagen fuer Transaktion {transaction_id}.\n"
                f"Bitte Logs pruefen oder Beleg manuell in Qonto hochladen."
            )
