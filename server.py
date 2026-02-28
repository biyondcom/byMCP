"""
byMCP – Zentraler MCP Server für biyond Workflows.

Startet einen FastMCP Server (stdio) und registriert alle Skills.

Skills:
  payroll  – PDF-Lohnzettelverarbeitung & Qonto SEPA-Überweisungen

Verwendung:
  python server.py                        # startet den MCP Server
  claude mcp add byMCP -- python /pfad/zu/server.py

Konfiguration:
  Kopiere .env.example zu .env und fülle alle Werte aus.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# .env aus dem Projektverzeichnis laden (vor allen Skill-Imports)
load_dotenv(Path(__file__).parent / ".env", override=False)

mcp = FastMCP(
    "byMCP",
    instructions=(
        "Zentraler MCP Server für biyond Workflows. "
        "Verfügbare Skills: payroll (Lohnzettelverarbeitung & Qonto-Überweisungen). "
        "Alle Pfadangaben müssen absolute Pfade sein."
    ),
)

# ── Skills registrieren ────────────────────────────────────────────────
from skills.payroll import register_tools as _payroll  # noqa: E402

_payroll(mcp)

# ── Einstiegspunkt ─────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
