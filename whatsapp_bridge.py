"""Local WhatsApp Cloud API webhook bridge for Helm."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


GRAPH_API_VERSION = "v23.0"
GRAPH_API_ROOT = "https://graph.facebook.com"
MAX_WEBHOOK_BYTES = 1_000_000
MAX_MESSAGE_LENGTH = 4_000
PHONE_PATTERN = re.compile(r"^[1-9]\d{7,14}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{24,256}$")
LOGGER = logging.getLogger("helm.whatsapp")


class WhatsAppError(RuntimeError):
    pass


class WhatsAppCloudAPI:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        recipient: str,
        api_version: str = GRAPH_API_VERSION,
        opener=urlopen,
    ) -> None:
        if not access_token.strip():
            raise ValueError("WhatsApp access token is required.")
        if not phone_number_id.isdigit():
            raise ValueError("WhatsApp phone number ID must contain digits only.")
        if not PHONE_PATTERN.fullmatch(recipient):
            raise ValueError("WhatsApp recipient must be an E.164 number without '+' or spaces.")
        if not re.fullmatch(r"v\d{1,3}\.\d{1,2}", api_version):
            raise ValueError("WhatsApp Graph API version must look like v23.0.")
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.recipient = recipient
        self.api_version = api_version
        self.opener = opener

    def send_text(self, text: str) -> None:
        body = text.strip()
        if not body:
            raise ValueError("WhatsApp message cannot be empty.")
        if len(body) > MAX_MESSAGE_LENGTH:
            body = body[: MAX_MESSAGE_LENGTH - 1] + "…"
        payload = json.dumps({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.recipient,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }).encode("utf-8")
        request = Request(
            f"{GRAPH_API_ROOT}/{self.api_version}/{self.phone_number_id}/messages",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "User-Agent": "Helm-WhatsApp-Bridge/1.0",
            },
            method="POST",
        )
        try:
            response = self.opener(request, timeout=15)
            try:
                result = json.loads(response.read())
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise WhatsAppError("WhatsApp Cloud API returned invalid JSON.") from error
            if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
                raise WhatsAppError("WhatsApp Cloud API returned an unexpected send response.")
        except WhatsAppError:
            raise
        except HTTPError as error:
            raise WhatsAppError(
                f"WhatsApp Cloud API rejected the message (HTTP {error.code}). "
                "Check the API setup and recipient session/template rules."
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise WhatsAppError("Could not reach the WhatsApp Cloud API.") from error


class PendingApproval:
    def __init__(self, code: str) -> None:
        self.code = code
        self.reply: queue.Queue[bool] = queue.Queue(maxsize=1)


class WhatsAppBridge:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        app_secret: str,
        verify_token: str,
        owner_number: str,
        project_root: Path,
        api_version: str = GRAPH_API_VERSION,
        python: str = sys.executable,
    ) -> None:
        if not app_secret.strip() or not verify_token.strip():
            raise ValueError("WhatsApp app secret and webhook verification token are required.")
        if len(verify_token) < 24 or not TOKEN_PATTERN.fullmatch(verify_token):
            raise ValueError("Webhook verification token must be at least 24 letters, digits, '_' or '-'.")
        self.api = WhatsAppCloudAPI(access_token, phone_number_id, owner_number, api_version)
        self.app_secret = app_secret.encode("utf-8")
        self.verify_token = verify_token
        self.owner_number = owner_number
        self.project_root = project_root.resolve()
        self.python = python
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()
        self._task_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: PendingApproval | None = None
        self._active_process: subprocess.Popen[str] | None = None

    def verify_signature(self, body: bytes, signature: str) -> bool:
        if not signature.startswith("sha256="):
            return False
        supplied = signature.removeprefix("sha256=")
        expected = hmac.new(self.app_secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(supplied, expected)

    def handle_webhook(self, body: bytes, signature: str) -> bool:
        if len(body) > MAX_WEBHOOK_BYTES:
            raise ValueError("Webhook payload exceeds the size limit.")
        if not self.verify_signature(body, signature):
            raise PermissionError("Invalid WhatsApp webhook signature.")
        try:
            update = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Webhook payload is not valid JSON.") from error
        if not isinstance(update, dict) or update.get("object") != "whatsapp_business_account":
            return False
        messages = self._messages(update)
        for message in messages:
            message_id = message.get("id")
            sender = message.get("from")
            content = message.get("text")
            if (
                not isinstance(message_id, str)
                or not isinstance(sender, str)
                or sender != self.owner_number
                or not isinstance(content, dict)
                or not isinstance(content.get("body"), str)
            ):
                continue
            if self._already_seen(message_id):
                continue
            text = content["body"].strip()
            if not text or len(text) > MAX_MESSAGE_LENGTH:
                self.api.send_text("Command ignored: text must be 1–4,000 characters.")
                continue
            self.handle_owner_message(text)
        return True

    @staticmethod
    def _messages(update: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        entries = update.get("entry")
        if not isinstance(entries, list):
            return result
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
                continue
            for change in entry["changes"]:
                if not isinstance(change, dict) or not isinstance(change.get("value"), dict):
                    continue
                messages = change["value"].get("messages")
                if isinstance(messages, list):
                    result.extend(item for item in messages if isinstance(item, dict))
        return result

    def _already_seen(self, message_id: str) -> bool:
        with self._seen_lock:
            if message_id in self._seen:
                return True
            self._seen[message_id] = None
            while len(self._seen) > 2_000:
                self._seen.popitem(last=False)
            return False

    def handle_owner_message(self, text: str) -> None:
        command = text.strip()
        upper = command.upper()
        if upper.startswith("APPROVE ") or upper.startswith("DENY "):
            self._answer_approval(command)
            return
        if upper in {"STOP", "CANCEL"}:
            self.stop_active_task()
            self.api.send_text("Stopped the active remote task.")
            return
        if not self._task_lock.acquire(blocking=False):
            self.api.send_text("A remote task is already running. Send STOP to cancel it.")
            return
        threading.Thread(target=self._run_remote_task, args=(command,), daemon=True).start()

    def _answer_approval(self, command: str) -> None:
        parts = command.split()
        if len(parts) != 2 or not parts[1].isalnum():
            self.api.send_text("Use APPROVE <code> or DENY <code> from the pending confirmation.")
            return
        with self._pending_lock:
            pending = self._pending
            if pending is None or not hmac.compare_digest(parts[1], pending.code):
                self.api.send_text("That confirmation code is not current or has expired.")
                return
            if not pending.reply.empty():
                self.api.send_text("That confirmation was already answered.")
                return
            pending.reply.put_nowait(parts[0].upper() == "APPROVE")

    def stop_active_task(self) -> None:
        with self._pending_lock:
            pending = self._pending
            process = self._active_process
        if pending and pending.reply.empty():
            pending.reply.put_nowait(False)
        if process and process.poll() is None:
            process.terminate()

    def _run_remote_task(self, prompt: str) -> None:
        process: subprocess.Popen[str] | None = None
        try:
            script = self.project_root / "assistant.py"
            if not script.is_file():
                raise WhatsAppError("Could not find assistant.py beside the bridge.")
            environment = os.environ.copy()
            for name in (
                "HELM_WHATSAPP_ACCESS_TOKEN",
                "HELM_WHATSAPP_APP_SECRET",
                "HELM_WHATSAPP_VERIFY_TOKEN",
            ):
                environment.pop(name, None)
            environment["HELM_WHATSAPP_REMOTE"] = "1"
            arguments = [
                self.python,
                "-u",
                str(script),
                "--chat",
                prompt,
                "--autonomy",
                "autonomous",
                "--history",
                str(
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "Local Desktop Assistant"
                    / "whatsapp_history.json"
                ),
            ]
            provider = environment.get("HELM_WHATSAPP_PROVIDER", "ollama")
            if provider in {"ollama", "anthropic", "openai", "gemini", "openrouter"}:
                arguments += ["--provider", provider]
            model = environment.get("HELM_WHATSAPP_MODEL")
            if model:
                arguments += ["--model", model]
            disabled_tools = environment.get("HELM_WHATSAPP_DISABLED_TOOLS", "")
            if disabled_tools:
                arguments += ["--disable-tools", disabled_tools]
            process = subprocess.Popen(
                arguments,
                cwd=self.project_root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._pending_lock:
                self._active_process = process
            timeout = threading.Timer(300, process.terminate)
            timeout.daemon = True
            timeout.start()
            response: str | None = None
            started = time.monotonic()
            assert process.stdout is not None
            for line in process.stdout:
                if time.monotonic() - started > 300:
                    process.terminate()
                    raise WhatsAppError("Remote task exceeded the 5-minute limit.")
                line = line.rstrip("\r\n")
                if line.startswith("APPROVAL_REQUIRED: "):
                    self._request_approval(process, line.removeprefix("APPROVAL_REQUIRED: "))
                elif line.startswith("CHAT_REPLY_JSON:"):
                    try:
                        response = json.loads(line.removeprefix("CHAT_REPLY_JSON:"))
                    except json.JSONDecodeError:
                        response = None
            status = process.wait(timeout=5)
            timeout.cancel()
            if status != 0:
                raise WhatsAppError("Remote task did not complete successfully.")
            if not isinstance(response, str) or not response.strip():
                raise WhatsAppError("Remote task ended without a verified text response.")
            self.api.send_text(response.strip())
        except (OSError, subprocess.SubprocessError, WhatsAppError) as error:
            LOGGER.warning("Remote WhatsApp task failed: %s", error)
            try:
                self.api.send_text(f"Helm could not complete that task: {error}")
            except WhatsAppError:
                LOGGER.exception("Could not send remote task failure response")
        finally:
            if "timeout" in locals():
                timeout.cancel()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            with self._pending_lock:
                self._active_process = None
                self._pending = None
            try:
                self._task_lock.release()
            except RuntimeError:
                pass  # this method can run standalone (e.g. in tests) without the caller's lock

    def _request_approval(self, process: subprocess.Popen[str], action: str) -> None:
        code = f"{secrets.randbelow(900_000) + 100_000}"
        pending = PendingApproval(code)
        with self._pending_lock:
            self._pending = pending
        self.api.send_text(
            f"Helm requests approval for: {action[:500]}\n"
            f"Reply APPROVE {code} to allow this action, or DENY {code}. "
            "This code expires in 90 seconds."
        )
        try:
            approved = pending.reply.get(timeout=90)
        except queue.Empty:
            approved = False
        finally:
            with self._pending_lock:
                if self._pending is pending:
                    self._pending = None
        if process.stdin is None:
            raise WhatsAppError("Remote approval channel is unavailable.")
        process.stdin.write("y\n" if approved else "n\n")
        process.stdin.flush()
        if not approved:
            raise WhatsAppError("Remote action was not approved.")


def configure_logging() -> Path:
    log_directory = Path.home() / "Library" / "Logs" / "Helm"
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_directory, 0o700)
    log_path = log_directory / "whatsapp-bridge.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    os.chmod(log_path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    return log_path


def create_server(bridge: WhatsAppBridge, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HelmWhatsAppBridge/1.0"

        def log_message(self, format: str, *args: object) -> None:
            LOGGER.info("HTTP request received; details suppressed")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/webhook":
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            mode = query.get("hub.mode", [""])[0]
            token = query.get("hub.verify_token", [""])[0]
            challenge = query.get("hub.challenge", [""])[0]
            if (
                mode != "subscribe"
                or not hmac.compare_digest(token, bridge.verify_token)
                or not challenge
                or len(challenge) > 256
            ):
                self.send_error(403)
                return
            payload = challenge.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/webhook":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length < 1 or length > MAX_WEBHOOK_BYTES:
                    raise ValueError("Invalid webhook size.")
                body = self.rfile.read(length)
                handled = bridge.handle_webhook(
                    body, self.headers.get("X-Hub-Signature-256", "")
                )
            except PermissionError:
                self.send_error(401)
                return
            except (ValueError, OSError):
                self.send_error(400)
                return
            self.send_response(200 if handled else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return ThreadingHTTPServer((host, port), Handler)


def bridge_from_environment() -> WhatsAppBridge:
    required = {
        "HELM_WHATSAPP_ACCESS_TOKEN": os.environ.get("HELM_WHATSAPP_ACCESS_TOKEN", ""),
        "HELM_WHATSAPP_PHONE_NUMBER_ID": os.environ.get("HELM_WHATSAPP_PHONE_NUMBER_ID", ""),
        "HELM_WHATSAPP_APP_SECRET": os.environ.get("HELM_WHATSAPP_APP_SECRET", ""),
        "HELM_WHATSAPP_VERIFY_TOKEN": os.environ.get("HELM_WHATSAPP_VERIFY_TOKEN", ""),
        "HELM_WHATSAPP_OWNER_NUMBER": os.environ.get("HELM_WHATSAPP_OWNER_NUMBER", ""),
    }
    missing = [name.removeprefix("HELM_WHATSAPP_").lower() for name, value in required.items() if not value]
    if missing:
        raise ValueError("Missing WhatsApp configuration: " + ", ".join(missing))
    project_root = Path(os.environ.get("ASSISTANT_PROJECT_DIR", Path(__file__).resolve().parent))
    return WhatsAppBridge(
        access_token=required["HELM_WHATSAPP_ACCESS_TOKEN"],
        phone_number_id=required["HELM_WHATSAPP_PHONE_NUMBER_ID"],
        app_secret=required["HELM_WHATSAPP_APP_SECRET"],
        verify_token=required["HELM_WHATSAPP_VERIFY_TOKEN"],
        owner_number=required["HELM_WHATSAPP_OWNER_NUMBER"],
        project_root=project_root,
        api_version=os.environ.get("HELM_WHATSAPP_API_VERSION", GRAPH_API_VERSION),
        python=os.environ.get("HELM_WHATSAPP_PYTHON", sys.executable),
    )


def cloud_api_from_environment() -> WhatsAppCloudAPI:
    required = {
        "HELM_WHATSAPP_ACCESS_TOKEN": os.environ.get("HELM_WHATSAPP_ACCESS_TOKEN", ""),
        "HELM_WHATSAPP_PHONE_NUMBER_ID": os.environ.get("HELM_WHATSAPP_PHONE_NUMBER_ID", ""),
        "HELM_WHATSAPP_OWNER_NUMBER": os.environ.get("HELM_WHATSAPP_OWNER_NUMBER", ""),
    }
    missing = [name.removeprefix("HELM_WHATSAPP_").lower() for name, value in required.items() if not value]
    if missing:
        raise ValueError("Missing WhatsApp configuration: " + ", ".join(missing))
    return WhatsAppCloudAPI(
        access_token=required["HELM_WHATSAPP_ACCESS_TOKEN"],
        phone_number_id=required["HELM_WHATSAPP_PHONE_NUMBER_ID"],
        recipient=required["HELM_WHATSAPP_OWNER_NUMBER"],
        api_version=os.environ.get("HELM_WHATSAPP_API_VERSION", GRAPH_API_VERSION),
    )


def run_server() -> int:
    configure_logging()
    try:
        bridge = bridge_from_environment()
        host = os.environ.get("HELM_WHATSAPP_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("WhatsApp webhook must bind to loopback; expose it using a secure tunnel.")
        port = int(os.environ.get("HELM_WHATSAPP_PORT", "8765"))
        if not 1024 <= port <= 65535:
            raise ValueError("WhatsApp webhook port must be between 1024 and 65535.")
        server = create_server(bridge, host=host, port=port)
    except (OSError, ValueError) as error:
        LOGGER.exception("WhatsApp bridge configuration failed")
        print(f"WHATSAPP_BRIDGE_ERROR:{error}", flush=True)
        return 1
    LOGGER.info("WhatsApp webhook listening on %s:%d", host, port)
    print(f"WHATSAPP_BRIDGE_READY:http://{host}:{port}/webhook", flush=True)
    def stop_bridge(_signal: int, _frame: object) -> None:
        bridge.stop_active_task()
        threading.Thread(target=server.shutdown, daemon=True).start()

    def cancel_active_task(_signal: int, _frame: object) -> None:
        # The host app's emergency-stop shortcut sends this to cancel an in-flight remote task
        # without shutting down the bridge (its webhook server) itself.
        bridge.stop_active_task()

    signal.signal(signal.SIGTERM, stop_bridge)
    signal.signal(signal.SIGINT, stop_bridge)
    signal.signal(signal.SIGUSR1, cancel_active_task)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())
