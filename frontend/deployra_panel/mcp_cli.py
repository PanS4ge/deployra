"""Deployra MCP stdio bridge dla Claude Desktop, Cursor, Antigravity i innych klientów AI.

Uruchamianie:
  python -m deployra_panel.mcp_cli --url http://twoja-deployra.pl --token dpl_mcp_...
lub przez zmienne środowiskowe:
  DEPLOYRA_URL=http://... DEPLOYRA_TOKEN=dpl_mcp_... python -m deployra_panel.mcp_cli
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Deployra Model Context Protocol (MCP) stdio bridge")
    parser.add_argument("--url", default=os.environ.get("DEPLOYRA_URL", "http://127.0.0.1:8000"), help="Deployra panel URL")
    parser.add_argument("--token", default=os.environ.get("DEPLOYRA_TOKEN", ""), help="Deployra API / MCP token")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    endpoint = f"{base_url}/mcp"
    token = args.token.strip()

    if not token:
        sys.stderr.write("Deployra MCP: warning: running without token. Set --token or DEPLOYRA_TOKEN\n")
        sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req_data = line.encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}" if token else "",
                    "User-Agent": "Deployra-MCP-Bridge/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                resp_bytes = resp.read()
                if resp_bytes:
                    sys.stdout.write(resp_bytes.decode("utf-8") + "\n")
                    sys.stdout.flush()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            err_response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"HTTP {e.code}: {err_body or e.reason}"},
            }
            sys.stdout.write(json.dumps(err_response) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            err_response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"Connection error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
