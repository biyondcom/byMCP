"""
Invoices skill – Rechnungsextraktion und SharePoint-Import.

Workflow:
  1. invoice_extract  – PDF lesen, Daten extrahieren, Vorschau anzeigen
  2. invoice_import   – extrahierte Rechnung in SharePoint-Listen importieren

Benötigt:
  - MS_CLIENT_ID, MS_TENANT_ID (wie receipts skill)
  - SHAREPOINT_SITE_URL       z.B. https://firma.sharepoint.com/sites/meineSite
  - SHAREPOINT_INVOICES_LIST  z.B. Rechnungen
  - SHAREPOINT_POSITIONS_LIST z.B. Rechnungspositionen
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_PENDING_FILE = Path.home() / ".byMCP" / "pending_invoice.json"


def register_tools(mcp: "FastMCP") -> None:

    @mcp.tool()
    def invoice_extract(
        pdf_path: Optional[str] = None,
        message_id: Optional[str] = None,
        attachment_id: Optional[str] = None,
    ) -> str:
        """
        Extrahiert Rechnungsdaten aus PDF (lokale Datei oder E-Mail-Anhang).

        Zeigt Vorschau und speichert pending state für invoice_import().
        Danach invoice_import() aufrufen um die Rechnung nach SharePoint zu importieren.

        Args:
            pdf_path:      Absoluter Pfad zur lokalen PDF-Datei.
            message_id:    Microsoft Graph Nachrichten-ID (für E-Mail-Anhang).
            attachment_id: Microsoft Graph Anhang-ID (für E-Mail-Anhang).
        """
        from skills.invoices.pdf_extractor import extract_invoice
        from skills.receipts.ms_oauth import MsAuthError

        # --- Validierung ---
        if pdf_path and (message_id or attachment_id):
            return "Fehler: Entweder pdf_path ODER message_id+attachment_id angeben, nicht beides."
        if not pdf_path and not (message_id and attachment_id):
            return (
                "Fehler: Entweder pdf_path (lokale Datei) oder "
                "message_id + attachment_id (E-Mail-Anhang) angeben."
            )

        # --- PDF-Bytes holen ---
        if pdf_path:
            p = Path(pdf_path)
            if not p.exists():
                return f"Datei nicht gefunden: {pdf_path}"
            if not p.suffix.lower() == ".pdf":
                return f"Keine PDF-Datei: {pdf_path}"
            try:
                pdf_bytes = p.read_bytes()
            except OSError as exc:
                return f"Fehler beim Lesen der Datei: {exc}"
            source_filename = p.name
        else:
            client_id = os.getenv("MS_CLIENT_ID", "").strip()
            tenant_id = os.getenv("MS_TENANT_ID", "common").strip()
            if not client_id:
                return "MS_CLIENT_ID fehlt in .env. Bitte konfigurieren."
            try:
                from skills.receipts.graph_client import GraphClient
                graph = GraphClient(client_id, tenant_id)
                pdf_bytes = graph.download_attachment(message_id, attachment_id)
            except MsAuthError as exc:
                return f"Office 365 Fehler: {exc}\nBitte 'receipts_authorize' aufrufen."
            except Exception as exc:
                return f"Fehler beim Herunterladen des Anhangs: {exc}"
            source_filename = f"email-attachment-{attachment_id[:8]}.pdf"

        # --- Extraktion ---
        try:
            invoice_data, warnings = extract_invoice(pdf_bytes, source_filename)
        except Exception as exc:
            return f"Fehler bei der PDF-Extraktion: {exc}"

        # --- Pending state speichern ---
        try:
            _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PENDING_FILE.write_text(
                json.dumps(invoice_data.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            return f"Fehler beim Speichern des Pending State: {exc}"

        # --- Vorschau aufbauen ---
        d = invoice_data.to_dict()
        lines = ["═" * 60, "  RECHNUNG – EXTRAHIERTE DATEN", "═" * 60, ""]

        header_fields = [
            ("Rechnungsnummer", "rechnungsnummer"),
            ("Rechnungsdatum",  "rechnungsdatum"),
            ("Lieferant",       "lieferant"),
            ("USt-IdNr",        "lieferant_ust_id_nr"),
            ("Nettobetrag",     "nettobetrag"),
            ("MwSt Betrag",     "mwst_betrag"),
            ("Bruttobetrag",    "bruttobetrag"),
            ("Zahlungsziel",    "zahlungsziel"),
            ("Bestellnummer",   "bestellnummer"),
        ]
        for label, key in header_fields:
            val = d.get(key) or "–"
            lines.append(f"  {label:<22} {val}")

        lines.append("")
        positionen = d.get("positionen", [])
        if positionen:
            lines.append(f"  Positionen: {len(positionen)}")
            for pos in positionen[:5]:
                desc = pos.get("beschreibung") or ""
                qty  = pos.get("menge") or ""
                unit = pos.get("einheit") or ""
                total = pos.get("gesamtpreis") or ""
                lines.append(f"    • {desc[:40]:<40}  {qty} {unit}  {total}")
            if len(positionen) > 5:
                lines.append(f"    … und {len(positionen) - 5} weitere")
        else:
            lines.append("  Positionen: keine erkannt")

        if warnings:
            lines.append("")
            lines.append("  Warnungen:")
            for w in warnings:
                lines.append(f"    ⚠ {w}")

        lines.append("")
        lines.append("═" * 60)
        lines.append("Pending state gespeichert. Jetzt 'invoice_import()' aufrufen.")

        return "\n".join(lines)

    # ------------------------------------------------------------------

    @mcp.tool()
    def invoice_import() -> str:
        """
        Importiert die zuletzt extrahierte Rechnung nach SharePoint.

        Liest pending state aus ~/.byMCP/pending_invoice.json.
        Erstellt zuerst den Kopfdatensatz in der Rechnungen-Liste,
        dann alle Positionen in der Rechnungspositionen-Liste (Lookup-Verknüpfung).

        Benötigte Env-Vars:
          SHAREPOINT_SITE_URL, SHAREPOINT_INVOICES_LIST, SHAREPOINT_POSITIONS_LIST
        """
        from skills.invoices.pdf_extractor import InvoiceData
        from skills.invoices.sharepoint_client import (
            SharePointClient,
            SharePointError,
            find_lookup_column,
            map_invoice_fields,
            map_position_fields,
        )
        from skills.receipts.ms_oauth import MsAuthError

        # --- Pending state laden ---
        if not _PENDING_FILE.exists():
            return (
                "Kein pending state gefunden.\n"
                "Bitte zuerst 'invoice_extract()' aufrufen."
            )
        try:
            raw = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
            invoice = InvoiceData.from_dict(raw)
        except Exception as exc:
            return f"Fehler beim Laden des pending state: {exc}"

        # --- Env-Vars ---
        site_url = os.getenv("SHAREPOINT_SITE_URL", "").strip()
        invoices_list = os.getenv("SHAREPOINT_INVOICES_LIST", "Rechnungen").strip()
        positions_list = os.getenv("SHAREPOINT_POSITIONS_LIST", "Rechnungspositionen").strip()
        client_id = os.getenv("MS_CLIENT_ID", "").strip()
        tenant_id = os.getenv("MS_TENANT_ID", "common").strip()

        if not site_url:
            return (
                "SHAREPOINT_SITE_URL fehlt in .env.\n"
                "Beispiel: SHAREPOINT_SITE_URL=https://firma.sharepoint.com/sites/meineSite"
            )
        if not client_id:
            return "MS_CLIENT_ID fehlt in .env."

        # --- SharePoint-Client ---
        try:
            sp = SharePointClient(site_url, client_id, tenant_id)
        except Exception as exc:
            return f"Fehler beim Erstellen des SharePoint-Clients: {exc}"

        output_lines: list[str] = ["SharePoint Import", "─" * 40]

        # --- Spalten beider Listen entdecken ---
        try:
            invoice_cols = sp.get_list_columns(invoices_list)
        except MsAuthError as exc:
            return f"Autorisierungsfehler: {exc}\nBitte 'receipts_authorize' aufrufen."
        except SharePointError as exc:
            if exc.status_code == 403:
                return (
                    f"Zugriff verweigert (403) auf Liste '{invoices_list}'.\n"
                    "Sicherstellen dass Sites.ReadWrite.All in Azure App-Registrierung "
                    "freigegeben und Consent erteilt ist.\n"
                    "Danach ms_tokens.json löschen und neu autorisieren."
                )
            if exc.status_code == 404:
                return (
                    f"Liste '{invoices_list}' nicht gefunden (404).\n"
                    "SHAREPOINT_INVOICES_LIST in .env prüfen."
                )
            return f"SharePoint Fehler beim Abrufen der Spalten: {exc}"

        try:
            positions_cols = sp.get_list_columns(positions_list)
        except MsAuthError as exc:
            return f"Autorisierungsfehler: {exc}\nBitte 'receipts_authorize' aufrufen."
        except SharePointError as exc:
            if exc.status_code == 404:
                return (
                    f"Liste '{positions_list}' nicht gefunden (404).\n"
                    "SHAREPOINT_POSITIONS_LIST in .env prüfen."
                )
            return f"SharePoint Fehler beim Abrufen der Positionsspalten: {exc}"

        # --- Kopfdatensatz erstellen ---
        invoice_dict = invoice.to_dict()
        header_fields, header_warnings = map_invoice_fields(invoice_dict, invoice_cols)

        if header_warnings:
            output_lines.append("Kopfdaten-Warnungen:")
            for w in header_warnings:
                output_lines.append(f"  ⚠ {w}")

        try:
            header_id = sp.create_item(invoices_list, header_fields)
        except MsAuthError as exc:
            return f"Autorisierungsfehler: {exc}"
        except SharePointError as exc:
            if exc.status_code == 403:
                return f"Zugriff verweigert (403) beim Erstellen des Rechnungs-Items.\nSites.ReadWrite.All prüfen."
            return f"Fehler beim Erstellen des Rechnungs-Items: {exc}"

        output_lines.append(f"✓ Rechnung erstellt: {invoices_list} Item #{header_id}")

        # --- Positionen erstellen ---
        lookup_internal = find_lookup_column(positions_cols)
        if not lookup_internal and invoice.positionen:
            output_lines.append(
                f"⚠ Keine Lookup-Spalte in '{positions_list}' gefunden – "
                "Positionen werden ohne Verknüpfung erstellt."
            )

        failed_positions = 0
        for i, pos in enumerate(invoice.positionen, 1):
            pos_dict = asdict(pos)
            pos_fields, pos_warnings = map_position_fields(
                pos_dict, positions_cols, lookup_internal or "", header_id
            )
            if pos_warnings:
                for w in pos_warnings:
                    output_lines.append(f"  ⚠ Position {i}: {w}")
            try:
                pos_id = sp.create_item(positions_list, pos_fields)
                output_lines.append(f"  ✓ Position {i} erstellt: Item #{pos_id}")
            except Exception as exc:
                output_lines.append(f"  ✗ Position {i} fehlgeschlagen: {exc}")
                failed_positions += 1

        # --- Pending state löschen (nur bei vollständigem Erfolg) ---
        if failed_positions == 0:
            try:
                _PENDING_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            output_lines.append("")
            output_lines.append("Import abgeschlossen. Pending state gelöscht.")
        else:
            output_lines.append("")
            output_lines.append(
                f"⚠ {failed_positions} Position(en) fehlgeschlagen.\n"
                f"Pending state bleibt erhalten. Rechnungs-Item ID: #{header_id}.\n"
                "Positionen können nach Fehlerbehebung manuell nachgetragen werden."
            )

        return "\n".join(output_lines)
