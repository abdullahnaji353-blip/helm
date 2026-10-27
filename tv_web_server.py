#!/usr/bin/env python3
"""Local web remote control for the connected Android TV.

Run this, then open the printed URL on your phone's browser (same Wi-Fi
network as this Mac) for a touch remote: app shortcuts, D-pad, volume,
search, and a small note/clock widget.

Only non-destructive TV actions are exposed here (launch app, search, remote
keys, volume, wake/sleep, the note). Installing/uninstalling apps and
changing the wallpaper stay chat-only, where Helm can ask you to confirm each
one — this server has no interactive terminal to ask through, and it's
reachable by anyone on your Wi-Fi who has the link.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import tv_control

STATIC_DIR = Path(__file__).resolve().parent / "tv_web_static"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Local Desktop Assistant"
TOKEN_PATH = APP_SUPPORT_DIR / "tv_web_token.txt"
TV_SERIAL_PATH = APP_SUPPORT_DIR / "tv_device.txt"
NOTE_PATH = APP_SUPPORT_DIR / "tv_web_note.txt"

MAX_BODY_BYTES = 4096
MAX_NOTE_CHARS = 280
DEFAULT_PORT = 8766

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def load_or_create_token() -> str:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(9)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def active_serial() -> str | None:
    try:
        serial = TV_SERIAL_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not serial or not tv_control.is_connected(serial):
        return None
    return serial


def read_note() -> str:
    try:
        return NOTE_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_note(text: str) -> None:
    NOTE_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTE_PATH.write_text(text[:MAX_NOTE_CHARS], encoding="utf-8")


def dedupe_display_names(mapping: dict[str, str], exclude: frozenset[str] = frozenset()) -> list[str]:
    """tv_control's app dicts have multiple aliases for the same app (e.g. 'disney+' and
    'disney plus' both map to the same package) — keep only the first alias per app."""
    seen_values: set[str] = set()
    names = []
    for name, value in mapping.items():
        if name in exclude or value in seen_values:
            continue
        seen_values.add(value)
        names.append(name)
    return names


def lan_ip() -> str:
    """Best-effort local network IP for the printed phone URL (no packets are actually sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        probe.close()


def create_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT, token: str | None = None) -> ThreadingHTTPServer:
    token = token or load_or_create_token()

    class Handler(BaseHTTPRequestHandler):
        server_version = "HelmTVRemote/1.0"

        def log_message(self, format: str, *args: object) -> None:
            pass  # avoid leaking LAN request details (paths, IPs) to stdout/stderr

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, query: dict) -> bool:
            supplied = self.headers.get("X-Helm-Token") or query.get("token", [""])[0]
            return secrets.compare_digest(supplied, token)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 1 or length > MAX_BODY_BYTES:
                raise ValueError("Invalid request body size.")
            parsed = json.loads(self.rfile.read(length))
            if not isinstance(parsed, dict):
                raise ValueError("Request body must be a JSON object.")
            return parsed

        def _serve_static(self, filename: str, content_type: str) -> None:
            try:
                data = (STATIC_DIR / filename).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in STATIC_FILES:
                filename, content_type = STATIC_FILES[parsed.path]
                self._serve_static(filename, content_type)
                return
            if parsed.path == "/api/status":
                if not self._authorized(parse_qs(parsed.query)):
                    self.send_error(401)
                    return
                serial = active_serial()
                self._json(200, {
                    "connected": serial is not None,
                    "device": tv_control.device_name(serial) if serial else None,
                    "apps": dedupe_display_names(tv_control.KNOWN_TV_APPS, exclude=frozenset({"settings"})),
                    "searchApps": dedupe_display_names(tv_control.SEARCH_URL_TEMPLATES),
                    "note": read_note(),
                })
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error(404)
                return
            if not self._authorized(parse_qs(parsed.query)):
                self.send_error(401)
                return
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if parsed.path == "/api/note":
                write_note(str(body.get("text", "")))
                self._json(200, {"ok": True})
                return
            self._handle_tv_action(parsed.path, body)

        def _handle_tv_action(self, path: str, body: dict) -> None:
            serial = active_serial()
            if serial is None:
                self._json(409, {"ok": False, "error": "No TV is connected."})
                return
            try:
                if path == "/api/key":
                    tv_control.send_key(serial, str(body.get("key", "")))
                elif path == "/api/open":
                    package = tv_control.find_package(serial, str(body.get("name", "")))
                    if package is None:
                        self._json(404, {"ok": False, "error": "App not found on the TV."})
                        return
                    tv_control.launch_app(serial, package)
                elif path == "/api/search":
                    tv_control.search(serial, str(body.get("app", "")), str(body.get("query", "")))
                elif path == "/api/volume":
                    tv_control.set_volume(serial, int(body.get("level", 0)))
                elif path == "/api/wake":
                    tv_control.wake(serial)
                elif path == "/api/sleep":
                    tv_control.sleep_device(serial)
                else:
                    self.send_error(404)
                    return
            except tv_control.AdbError as error:
                self._json(502, {"ok": False, "error": str(error)})
                return
            except (ValueError, TypeError) as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            self._json(200, {"ok": True})

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Default 0.0.0.0 so your phone can reach it.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    token = load_or_create_token()
    server = create_server(args.host, args.port, token)
    url = f"http://{lan_ip()}:{args.port}/?token={token}"

    print("TV remote running. On your phone (same Wi-Fi as this Mac), open:")
    print(f"  {url}")
    print("Anyone with this link on your network can control the TV — don't share it beyond your household.")
    print("Press Control+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
