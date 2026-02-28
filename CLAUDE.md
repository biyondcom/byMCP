# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**byMCP** is a central MCP (Model Context Protocol) server for biyond workflows.
It exposes domain-specific tools as MCP tools that Claude can call directly.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the MCP server (stdio transport – used by Claude Code)
python server.py

# Register with Claude Code (run once)
claude mcp add byMCP -- python C:/Users/Micha/claude/byMCP/server.py

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
utils/
  logger.py                 # Rotating file logger + IBAN masking filter
  iban_validator.py         # MOD-97 validation
```

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
