"""Local Telegram bot bridge for Helm.

Long-polls Telegram's Bot API (no public URL or webhook needed). Only acts on
messages from one pre-configured owner chat ID; everyone else is ignored. If
no owner is configured yet, the bridge runs in pairing mode: it tells whoever
messages it their chat ID (and does nothing else) so setup is copy-paste.
"""

from __future__ import annotations

import json
import hashlib
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
import fcntl
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen


API_ROOT = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4_000
TOKEN_PATTERN = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{20,}$")  # BotFather token shape
LOGGER = logging.getLogger("helm.telegram")


class TelegramError(RuntimeError):
    pass


class TelegramConflictError(TelegramError):
    pass


def _build_opener(proxy_url: str | None):
    """An HTTP(S) CONNECT proxy opener (e.g. a VPN app's local proxy, or a reachable relay) for
    networks that block Telegram's IP ranges outright. SOCKS proxies aren't supported (would need
    an extra dependency) — point a VPN/proxy app's HTTP(S) listener here instead."""
    if not proxy_url:
        return urlopen
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Proxy URL must look like http://host:port or https://user:pass@host:port.")
    return build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url})).open


class TelegramBotAPI:
    def __init__(self, bot_token: str, opener=None, proxy_url: str | None = None) -> None:
        if not TOKEN_PATTERN.fullmatch(bot_token.strip()):
            raise ValueError("Telegram bot token does not look like one from @BotFather.")
        self.bot_token = bot_token.strip()
        self.opener = opener if opener is not None else _build_opener(proxy_url)

    def _call(self, method: str, payload: dict, timeout: float) -> dict:
        request = Request(
            f"{API_ROOT}/bot{self.bot_token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Helm-Telegram-Bridge/1.0"},
            method="POST",
        )
        try:
            response = self.opener(request, timeout=timeout)
            try:
                result = json.loads(response.read())
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise TelegramError("Telegram returned invalid JSON.") from error
            if not isinstance(result, dict) or not result.get("ok"):
                raise TelegramError(f"Telegram rejected {method}: {result.get('description', result) if isinstance(result, dict) else result}")
            return result
        except TelegramError:
            raise
        except HTTPError as error:
            if error.code == 409 and method == "getUpdates":
                try:
                    body = json.loads(error.read())
                    detail = body.get("description", "") if isinstance(body, dict) else ""
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    detail = ""
                explanation = f" Telegram says: {detail}" if detail else ""
                raise TelegramConflictError(
                    "Telegram polling conflict (HTTP 409). Another process is already polling this bot. "
                    "Stop any other Helm instance or service using this bot token, then restart Helm." + explanation
                ) from error
            raise TelegramError(f"Telegram returned HTTP {error.code} for {method}.") from error
        except (URLError, TimeoutError, OSError) as error:
            raise TelegramError(f"Could not reach Telegram for {method}.") from error

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout + 10)
        updates = result.get("result")
        return [u for u in updates if isinstance(u, dict)] if isinstance(updates, list) else []

    def send_text(self, chat_id: int, text: str) -> None:
        body = text.strip()
        if not body:
            raise ValueError("Telegram message cannot be empty.")
        if len(body) > MAX_MESSAGE_LENGTH:
            body = body[: MAX_MESSAGE_LENGTH - 1] + "…"
        self._call("sendMessage", {"chat_id": chat_id, "text": body}, timeout=15)


class PendingApproval:
    def __init__(self, code: str) -> None:
        self.code = code
        self.reply: queue.Queue[bool] = queue.Queue(maxsize=1)


class TelegramBridge:
    def __init__(
        self,
        *,
        bot_token: str,
        owner_chat_id: int | None,
        project_root: Path,
        python: str = sys.executable,
        proxy_url: str | None = None,
    ) -> None:
        self.api = TelegramBotAPI(bot_token, proxy_url=proxy_url)
        self.owner_chat_id = owner_chat_id
        self.project_root = project_root.resolve()
        self.python = python
        self._offset: int | None = None
        self._task_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: PendingApproval | None = None
        self._active_process: subprocess.Popen[str] | None = None
        self._stopping = threading.Event()

    def run_forever(self) -> int:
        while not self._stopping.is_set():
            try:
                for update in self.api.get_updates(self._offset):
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)
            except TelegramConflictError as error:
                LOGGER.error("Poll stopped: %s", error)
                print(f"TELEGRAM_BRIDGE_ERROR:{error}", flush=True)
                return 1
            except TelegramError as error:
                LOGGER.warning("Poll failed: %s", error)
                time.sleep(3)  # back off before retrying so a flaky network doesn't spin
        return 0

    def stop(self) -> None:
        self._stopping.set()
        self.stop_active_task()

    def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), int) or not isinstance(text, str):
            return
        chat_id = chat["id"]
        if self.owner_chat_id is None:
            # Pairing mode: tell whoever wrote in their chat ID. No command ever runs here.
            try:
                self.api.send_text(
                    chat_id,
                    f"This Telegram chat's ID is {chat_id}. Enter it in Helm's Settings as the "
                    "owner chat, then message me again.",
                )
            except TelegramError:
                LOGGER.exception("Could not send pairing reply")
            return
        if chat_id != self.owner_chat_id:
            return  # silently ignore anyone who is not the configured owner
        text = text.strip()
        if not text or len(text) > MAX_MESSAGE_LENGTH:
            self.api.send_text(chat_id, "Command ignored: text must be 1–4,000 characters.")
            return
        self.handle_owner_message(text)

    def handle_owner_message(self, text: str) -> None:
        command = text.strip()
        upper = command.upper()
        if upper.startswith("APPROVE ") or upper.startswith("DENY "):
            self._answer_approval(command)
            return
        if upper in {"STOP", "CANCEL"}:
            self.stop_active_task()
            self.api.send_text(self.owner_chat_id, "Stopped the active remote task.")
            return
        if not self._task_lock.acquire(blocking=False):
            self.api.send_text(self.owner_chat_id, "A remote task is already running. Send STOP to cancel it.")
            return
        threading.Thread(target=self._run_remote_task, args=(command,), daemon=True).start()

    def _answer_approval(self, command: str) -> None:
        parts = command.split()
        if len(parts) != 2 or not parts[1].isalnum():
            self.api.send_text(self.owner_chat_id, "Use APPROVE <code> or DENY <code> from the pending confirmation.")
            return
        with self._pending_lock:
            pending = self._pending
            if pending is None or parts[1] != pending.code:
                self.api.send_text(self.owner_chat_id, "That confirmation code is not current or has expired.")
                return
            if not pending.reply.empty():
                self.api.send_text(self.owner_chat_id, "That confirmation was already answered.")
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
        timeout: threading.Timer | None = None
        try:
            script = self.project_root / "assistant.py"
            if not script.is_file():
                raise TelegramError("Could not find assistant.py beside the bridge.")
            environment = os.environ.copy()
            environment.pop("HELM_TELEGRAM_BOT_TOKEN", None)
            environment["HELM_TELEGRAM_REMOTE"] = "1"
            arguments = [
                self.python, "-u", str(script), "--chat", prompt, "--autonomy", "autonomous",
                "--history", str(Path.home() / "Library/Application Support/Local Desktop Assistant/telegram_history.json"),
            ]
            provider = environment.get("HELM_TELEGRAM_PROVIDER", "ollama")
            if provider in {"ollama", "anthropic", "openai", "gemini", "openrouter"}:
                arguments += ["--provider", provider]
            model = environment.get("HELM_TELEGRAM_MODEL")
            if model:
                arguments += ["--model", model]
            disabled_tools = environment.get("HELM_TELEGRAM_DISABLED_TOOLS", "")
            if disabled_tools:
                arguments += ["--disable-tools", disabled_tools]
            process = subprocess.Popen(
                arguments, cwd=self.project_root, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
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
                    raise TelegramError("Remote task exceeded the 5-minute limit.")
                line = line.rstrip("\r\n")
                if line.startswith("APPROVAL_REQUIRED: "):
                    self._request_approval(process, line.removeprefix("APPROVAL_REQUIRED: "))
                elif line.startswith("CHAT_REPLY_JSON:"):
                    try:
                        response = json.loads(line.removeprefix("CHAT_REPLY_JSON:"))
                    except json.JSONDecodeError:
                        response = None
            status = process.wait(timeout=5)
            if status != 0:
                raise TelegramError("Remote task did not complete successfully.")
            if not isinstance(response, str) or not response.strip():
                raise TelegramError("Remote task ended without a verified text response.")
            self.api.send_text(self.owner_chat_id, response.strip())
        except (OSError, subprocess.SubprocessError, TelegramError) as error:
            LOGGER.warning("Remote Telegram task failed: %s", error)
            try:
                self.api.send_text(self.owner_chat_id, f"Helm could not complete that task: {error}")
            except TelegramError:
                LOGGER.exception("Could not send remote task failure response")
        finally:
            if timeout is not None:
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
            self.owner_chat_id,
            f"Helm requests approval for: {action[:500]}\n"
            f"Reply APPROVE {code} to allow this action, or DENY {code}. This code expires in 90 seconds.",
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
            raise TelegramError("Remote approval channel is unavailable.")
        process.stdin.write("y\n" if approved else "n\n")
        process.stdin.flush()
        if not approved:
            raise TelegramError("Remote action was not approved.")


def configure_logging() -> Path:
    log_directory = Path.home() / "Library" / "Logs" / "Helm"
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_directory, 0o700)
    log_path = log_directory / "telegram-bridge.log"
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    os.chmod(log_path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    return log_path


def acquire_polling_lock(bot_token: str):
    """Prevent two local Helm processes from polling the same Telegram bot token."""
    token_hash = hashlib.sha256(bot_token.strip().encode("utf-8")).hexdigest()[:24]
    lock_directory = Path.home() / "Library" / "Application Support" / "Helm"
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_directory, 0o700)
    lock_path = lock_directory / f"telegram-poller-{token_hash}.lock"
    lock_file = lock_path.open("a", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise TelegramError(
            "Another Helm process is already polling this Telegram bot on this Mac. "
            "Switch to that Helm window or stop it before starting this bridge."
        ) from error
    return lock_file


def bridge_from_environment() -> TelegramBridge:
    bot_token = os.environ.get("HELM_TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise ValueError("Missing Telegram configuration: bot token.")
    owner_raw = os.environ.get("HELM_TELEGRAM_OWNER_CHAT_ID", "").strip()
    owner_chat_id: int | None = None
    if owner_raw:
        try:
            owner_chat_id = int(owner_raw)
        except ValueError as error:
            raise ValueError("Telegram owner chat ID must be a whole number.") from error
    project_root = Path(os.environ.get("ASSISTANT_PROJECT_DIR", Path(__file__).resolve().parent))
    return TelegramBridge(
        bot_token=bot_token,
        owner_chat_id=owner_chat_id,
        project_root=project_root,
        python=os.environ.get("HELM_TELEGRAM_PYTHON", sys.executable),
        proxy_url=os.environ.get("HELM_TELEGRAM_PROXY_URL") or None,
    )


def run_bridge() -> int:
    configure_logging()
    polling_lock = None
    try:
        bridge = bridge_from_environment()
        polling_lock = acquire_polling_lock(os.environ["HELM_TELEGRAM_BOT_TOKEN"])
    except TelegramError as error:
        LOGGER.error("Telegram bridge could not start: %s", error)
        print(f"TELEGRAM_BRIDGE_ERROR:{error}", flush=True)
        return 1
    except ValueError as error:
        LOGGER.exception("Telegram bridge configuration failed")
        print(f"TELEGRAM_BRIDGE_ERROR:{error}", flush=True)
        return 1
    mode = "pairing (no owner set yet)" if bridge.owner_chat_id is None else f"owner={bridge.owner_chat_id}"
    if os.environ.get("HELM_TELEGRAM_PROXY_URL"):
        mode += ", via proxy"
    LOGGER.info("Telegram bridge polling started, %s", mode)
    print(f"TELEGRAM_BRIDGE_READY:{mode}", flush=True)

    def stop_bridge(_signal: int, _frame: object) -> None:
        bridge.stop()

    def cancel_active_task(_signal: int, _frame: object) -> None:
        # The host app's emergency-stop shortcut sends this to cancel an in-flight remote task
        # without shutting down the bridge itself.
        bridge.stop_active_task()

    signal.signal(signal.SIGTERM, stop_bridge)
    signal.signal(signal.SIGINT, stop_bridge)
    signal.signal(signal.SIGUSR1, cancel_active_task)
    try:
        exit_code = bridge.run_forever()
    except KeyboardInterrupt:
        exit_code = 0
    finally:
        if polling_lock is not None:
            polling_lock.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_bridge())
