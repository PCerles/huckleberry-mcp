"""FastMCP Cloud entrypoint.

Exposes a module-level ``mcp`` with the full Huckleberry tool set and **no**
OAuth layer. On FastMCP Cloud the platform provides the public HTTPS URL and
gates access, so the in-repo OAuth 2.1 / Fly volume machinery (used for the
self-hosted Fly.io deploy) is intentionally bypassed here.

Configure these as environment variables in the FastMCP Cloud dashboard:
    HUCKLEBERRY_EMAIL              (required)
    HUCKLEBERRY_PASSWORD          (required)
    HUCKLEBERRY_DEFAULT_CHILD_UID (recommended — one-shot prompts skip a lookup)
    HUCKLEBERRY_TIMEZONE          (optional, default America/New_York)

Entrypoint to set in FastMCP Cloud:  app.py:mcp
"""

from __future__ import annotations

import os

# Force the non-OAuth build path. FastMCP Cloud terminates auth in front of us,
# so the server itself stays a plain tools-only FastMCP instance.
os.environ["MCP_TRANSPORT"] = "stdio"

from huckleberry_mcp.server import _build_mcp

mcp = _build_mcp()
