"""Payroll skill – registriert alle Payroll-Tools beim MCP Server."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_tools(mcp: "FastMCP") -> None:
    """Register all payroll tools with the given FastMCP instance."""
    from datetime import datetime
    from pathlib import Path
    from typing import Optional

    from skills.payroll.csv_parser import parse_csv
    from skills.payroll.idempotency import (
        is_already_processed,
        make_idempotency_key,
        query_all,
        record_failure,
        record_pending,
        record_success,
    )
    from skills.payroll.pdf_processor import process_pdf
    from skills.payroll.qonto_client import QontoClient, QontoConfigError

    # ------------------------------------------------------------------

    @mcp.tool()
    def payroll_list_employees(csv_path: str) -> str:
        """
        Liest eine Mitarbeiter-CSV ein und gibt die Liste der Mitarbeiter zurück.

        Args:
            csv_path: Absoluter Pfad zur CSV-Datei (Spalten: name, iban, zielordner).
        """
        result = parse_csv(csv_path)
        lines = []
        if result.errors:
            lines.append("Fehler beim CSV-Einlesen:")
            lines.extend(f"  {e}" for e in result.errors)
        lines.append(f"\n{len(result.employees)} Mitarbeiter:")
        for emp in result.employees:
            lines.append(f"  {emp.name}  |  {emp.iban_masked}  |  {emp.target_dir}")
        return "\n".join(lines)

    # ------------------------------------------------------------------

    @mcp.tool()
    def payroll_check_transfer(name: str, period: str, amount_cents: int) -> str:
        """
        Prüft ob eine SEPA-Überweisung für diesen Mitarbeiter/Periode bereits ausgeführt wurde.

        Args:
            name:         Vollständiger Name des Mitarbeiters (z.B. "Michael Richter").
            period:       Abrechnungsperiode als YYYY-MM (z.B. "2026-02").
            amount_cents: Nettobetrag in Euro-Cent (z.B. 763363 = 7633.63 €).
        """
        key = make_idempotency_key(name, period, amount_cents)
        done = is_already_processed(key)
        eur = amount_cents / 100
        status = "bereits erfolgreich ausgefuehrt" if done else "noch nicht verarbeitet"
        return f"{name} / {period} / {eur:.2f} EUR:  {status}"

    # ------------------------------------------------------------------

    @mcp.tool()
    def payroll_list_transfers(period: Optional[str] = None) -> str:
        """
        Listet alle bekannten Überweisungen aus dem Idempotenz-Store auf.

        Args:
            period: Filter auf eine Periode YYYY-MM. Ohne Angabe: alle Perioden.
        """
        rows = query_all(period=period)
        if not rows:
            label = f"Periode {period}" if period else "alle Perioden"
            return f"Keine Eintraege fuer {label}."
        lines = [f"{'Name':<25} {'Periode':<8} {'EUR':>10}  {'Status':<10}  Transfer-ID"]
        lines.append("-" * 75)
        for r in rows:
            eur = r["amount_cents"] / 100
            lines.append(
                f"{r['employee_name']:<25} {r['period']:<8} {eur:>10.2f}  "
                f"{r['status']:<10}  {r['transfer_id'] or '-'}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------

    @mcp.tool()
    def payroll_process(
        pdf_path: str,
        csv_path: str,
        period: Optional[str] = None,
        skip_transfers: bool = False,
    ) -> str:
        """
        Verarbeitet ein mehrseitiges Lohnzettel-PDF: Seiten aufteilen, Mitarbeiter
        zuordnen, PDFs speichern und (optional) SEPA-Überweisungen via Qonto auslösen.

        SCA (Smartphone-Genehmigung): Bei jeder Überweisung erscheint eine
        Push-Benachrichtigung in der Qonto-App. Die Überweisung wird erst nach
        Genehmigung abgeschlossen. Dieses Tool wartet bis zu 5 Minuten pro Überweisung.

        Args:
            pdf_path:        Absoluter Pfad zum Lohnzettel-PDF.
            csv_path:        Absoluter Pfad zur Mitarbeiter-CSV.
            period:          Abrechnungsperiode YYYY-MM (Standard: aktueller Monat).
            skip_transfers:  True → nur PDFs speichern, keine Banküberweisung.
        """
        if period is None:
            period = datetime.now().strftime("%Y-%m")

        logs: list = []

        def _log(level: str, msg: str) -> None:
            logs.append(f"[{level}] {msg}")

        # ── CSV ──────────────────────────────────────────────────────
        csv_result = parse_csv(csv_path)
        if csv_result.errors:
            return "CSV-Fehler:\n" + "\n".join(csv_result.errors)
        if not csv_result.employees:
            return "CSV enthaelt keine gueltigen Mitarbeiter."

        employees = csv_result.employees
        _log("INFO", f"CSV: {len(employees)} Mitarbeiter geladen.")

        # ── PDF ──────────────────────────────────────────────────────
        _log("INFO", f"PDF-Verarbeitung: {Path(pdf_path).name}")
        pdf_result = process_pdf(
            pdf_path=Path(pdf_path),
            employees=employees,
            period=period,
        )
        for saved in pdf_result.saved_files:
            _log("INFO", f"PDF gespeichert: {saved}")
        for page_num in pdf_result.unmatched_pages:
            _log("WARNING", f"Seite {page_num}: kein Mitarbeiter gefunden.")
        for error in pdf_result.errors:
            _log("ERROR", f"PDF-Fehler: {error}")

        # ── Transfers ─────────────────────────────────────────────────
        if skip_transfers:
            _log("INFO", "Ueberweisungen uebersprungen (skip_transfers=True).")
            return "\n".join(logs)

        try:
            client = QontoClient()
        except QontoConfigError as exc:
            _log("ERROR", f"Qonto-Konfiguration fehlt: {exc}")
            return "\n".join(logs)

        transfer_ok: list = []
        transfer_fail: list = []

        for emp in employees:
            if emp.amount_cents <= 0:
                _log("WARNING", f"{emp.name}: kein Betrag gefunden, uebersprungen.")
                transfer_fail.append(emp.name)
                continue

            key = make_idempotency_key(emp.name, period, emp.amount_cents)
            if is_already_processed(key):
                _log("INFO", f"{emp.name}: bereits verarbeitet – uebersprungen.")
                transfer_ok.append(emp.name)
                continue

            record_pending(key, emp.name, period, emp.amount_cents)
            _log(
                "INFO",
                f"Ueberweisung: {emp.name} | {emp.iban_masked} | "
                f"{emp.amount_cents / 100:.2f} EUR",
            )

            result = client.create_transfer(
                credit_name=emp.name,
                credit_iban=emp.iban,
                amount_cents=emp.amount_cents,
                period=period,
                idempotency_key=key,
                log_callback=_log,
            )

            if result.success:
                record_success(key, result.transfer_id)
                _log("INFO", f"{emp.name}: Ueberweisung OK (ID: {result.transfer_id or '-'}).")
                transfer_ok.append(emp.name)
            else:
                record_failure(key, result.error)
                _log("ERROR", f"{emp.name}: fehlgeschlagen – {result.error}")
                transfer_fail.append(emp.name)

        # ── Zusammenfassung ───────────────────────────────────────────
        logs.append("")
        logs.append(f"=== Zusammenfassung {period} ===")
        logs.append(f"PDFs gespeichert:      {len(pdf_result.saved_files)}")
        logs.append(f"Ueberweisungen OK:     {len(transfer_ok)}")
        logs.append(f"Ueberweisungen Fehler: {len(transfer_fail)}")
        if transfer_fail:
            logs.append(f"Fehlgeschlagen: {', '.join(transfer_fail)}")

        return "\n".join(logs)
