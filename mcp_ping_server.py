#!/usr/bin/env python3
"""Minimal stdio MCP server used to verify the local connector."""

from __future__ import annotations

import json
import sys
from typing import Any


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            print(f"Invalid JSON-RPC input: {error}", file=sys.stderr, flush=True)
            continue

        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            print("Invalid JSON-RPC request.", file=sys.stderr, flush=True)
            continue

        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            if "id" in request:
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32600, "message": "Invalid request."},
                })
            continue
        if method.startswith("notifications/"):
            continue

        if method == "initialize":
            params = request.get("params")
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            if not isinstance(version, str):
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "Missing protocol version."},
                })
                continue
            result: dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-ping", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{
                    "name": "ping",
                    "description": "Return pong to verify the MCP connection.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }]
            }
        elif method == "tools/call":
            params = request.get("params")
            arguments = params.get("arguments", {}) if isinstance(params, dict) else None
            if (
                not isinstance(params, dict)
                or params.get("name") != "ping"
                or not isinstance(arguments, dict)
                or arguments
            ):
                send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "ping takes no arguments."},
                })
                continue
            result = {"content": [{"type": "text", "text": "pong"}]}
        elif method == "ping":
            result = {}
        else:
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found."},
            })
            continue

        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
