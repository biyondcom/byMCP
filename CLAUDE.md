# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**byMCP** is a central MCP (Model Context Protocol) server for biyond workflows.
It exposes domain-specific tools as MCP tools that Claude can call directly.

## Requirements

- **Python ≥ 3.10** (the `mcp` package requires it). Install from https://www.python.org/downloads/

## Commands

```bash
# Install dependencies (use py -3.12 on Windows if 3.8 is still the default)
py -3.12 -m pip install -r requirements.txt

# Run the MCP server (stdio transport – used by Claude Code)
py -3.12 server.py

# Register with Claude Code (run once)
claude mcp add byMCP -- py -3.12 C:/Users/Micha/claude/byMCP/server.py

# Copy and fill in credentials
cp .env.example .env
```

## Architecture

```
server.py                   # FastMCP entry point; imports and registers all skills
skills/
  payroll/
    __init__.py             # register_tools(mcp) – defines all 4 payroll MCP tools
    csv_parser.py           # Employee dataclass + CSV parsing (UTF-8/latin-1, auto-delimiter)
    pdf_processor.py        # pdfplumber text extraction, employee matching, pypdf page split
    qonto_client.py         # Qonto v2 REST client: VOP + SEPA transfers + SCA polling
    qonto_oauth.py          # OAuth2 authorization code flow, token cache at ~/.byMCP/
    idempotency.py          # SQLite store at ~/.byMCP/idempotency.db
  invoices/
    __init__.py             # register_tools(mcp) – invoice_extract, invoice_import
    pdf_extractor.py        # pdfplumber extraction: regex header fields + table positions
    sharepoint_client.py    # Graph API SharePoint client: site resolution, column discovery, item creation
utils/
  logger.py                 # Rotating file logger + IBAN masking filter
  iban_validator.py         # MOD-97 validation
```

## Invoices Skill – MCP Tools

| Tool | Description |
|---|---|
| `invoice_extract` | PDF einlesen (lokal oder E-Mail-Anhang), Kopf- und Positionsdaten extrahieren, Vorschau anzeigen |
| `invoice_import` | Extrahierte Rechnung in zwei SharePoint-Listen importieren (Kopf + Positionen via Lookup) |

**Workflow:**
1. `receipts_authorize` (einmalig, nach Azure-Konfiguration mit `Sites.ReadWrite.All`)
2. `invoice_extract(pdf_path="/pfad/zu/rechnung.pdf")` → Vorschau prüfen
3. `invoice_import()` → SharePoint-Items in Rechnungen + Rechnungspositionen erstellen

**Scope-Anforderung:** `Sites.ReadWrite.All` (delegiert) in der Azure App-Registrierung.
Nach Hinzufügen: `~/.byMCP/ms_tokens.json` löschen und neu autorisieren.

**Env vars:** `SHAREPOINT_SITE_URL`, `SHAREPOINT_INVOICES_LIST`, `SHAREPOINT_POSITIONS_LIST`
**Pending state:** `~/.byMCP/pending_invoice.json` (wird nach erfolgreichem Import gelöscht)

**Lookup-Spalte:** `sharepoint_client.py` erkennt Lookup-Spalten automatisch via `col.get("lookup")`.
Beim Schreiben von Positionen wird `{internal_name}LookupId = header_item_id` gesetzt.

## Receipts Skill – MCP Tools

| Tool | Description |
|---|---|
| `receipts_authorize` | Einmalige MS Office 365 Autorisierung via Device Code Flow (blockiert bis Code eingegeben) |
| `receipts_find_candidates` | Qonto-Transaktionen ohne Beleg + passende Mails mit Score-Ranking |
| `receipts_attach` | E-Mail-Anhang herunterladen und an Qonto-Transaktion anhängen |

**Workflow:**
1. `receipts_authorize` (einmalig) → gibt Code + URL aus, User gibt Code auf microsoft.com/devicelogin ein
2. `receipts_find_candidates(days_back=60)` → listet Transaktionen + Top-3 Mail-Kandidaten mit Score
3. Claude zeigt Ergebnisse, fragt Benutzer: "Ist Kandidat [1] für Transaktion X korrekt?"
4. `receipts_attach(transaction_id, message_id, attachment_id)` → lädt Anhang herunter + hängt an Qonto an

**Matching-Score (0–100%):**
- Betrag im Mail-Betreff oder Body Preview gefunden: +50%
- Label-Wörter (>3 Zeichen) im Absender: +30%
- Label-Wörter im Betreff: +20%

**Env vars:** `MS_CLIENT_ID`, `MS_TENANT_ID` (default: "common")
**Token-Cache:** `~/.byMCP/ms_tokens.json`

## Adding a New Skill

1. Create `skills/<skill_name>/__init__.py` with `register_tools(mcp: FastMCP) -> None`
2. Import and call it in `server.py`: `from skills.<skill_name> import register_tools as _name; _name(mcp)`
3. Name tools with the skill prefix: `<skill>_<action>` (e.g. `payroll_process`)

## Payroll Skill – MCP Tools

| Tool | Description |
|---|---|
| `payroll_list_employees` | Parse CSV → return employee list |
| `payroll_check_transfer` | Check idempotency store for a specific transfer |
| `payroll_list_transfers` | List all transfers (optionally filtered by period) |
| `payroll_process` | Full workflow: PDF split → save PDFs → Qonto SEPA transfers with SCA |

## Key Technical Details

- **PDF amount extraction**: `Auszahlungsbetrag` appears as a column header; amount is on the next line. Negative lookbehinds `(?<!\d)(?<!\.)` prevent partial amount matches.
- **Qonto API v2**: Uses `/sepa/transfers` (not `/transfers`). Requires `bank_account_id` (UUID resolved from `QONTO_DEBIT_IBAN`), `beneficiary` object, and `amount` as decimal string.
- **VOP**: `POST /sepa/verify_payee` mandatory per EU regulation (Oct 2025). Returns `proof_token.token`.
- **SCA**: Transfers return HTTP 428 with `sca_session_token`. Poll `GET /sca_sessions/{token}` — Qonto returns `{"result": "waiting"|"allow"|"deny"}`. Retry transfer with `X-Qonto-Sca-Session-Token` header after approval.
- **Idempotency key**: SHA-256 of `name|period|amount_cents` — prevents duplicate transfers.
- **Token storage**: OAuth2 tokens at `~/.byMCP/qonto_tokens.json`, idempotency DB at `~/.byMCP/idempotency.db`.
