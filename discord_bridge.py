"""Local Discord bot bridge for Helm.

Polls a single configured Discord channel for new messages (Discord's REST
API has no Telegram-style global getUpdates; discovering new DMs needs a
persistent Gateway websocket connection, which this bridge avoids to stay
dependency-free). Only acts on messages from one pre-configured owner user
ID in that channel; everyone else is ignored.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_ROOT = "https://discord.com/api/v10"
MAX_MESSAGE_LENGTH = 2_000
POLL_INTERVAL = 3.0
LOGGER = logging.getLogger("helm.discord")


class DiscordError(RuntimeError):
    pass


class DiscordRateLimited(DiscordError):
    """Discord answered 429; retry_after is the seconds it asked us to wait."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Discord rate-limited this request; retry after {retry_after:.1f}s.")
        self.retry_after = retry_after


class DiscordBotAPI:
    def __init__(self, bot_token: str) -> None:
        if not bot_token.strip() or len(bot_token.strip()) < 20:
            raise ValueError("Discord bot token does not look valid.")
        self.bot_token = bot_token.strip()

    def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 15) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{API_ROOT}{path}",
            data=data,
            headers={
                "Authorization": f"Bot {self.bot_token}",
                "Content-Type": "application/json",
                "User-Agent": "Helm-Discord-Bridge/1.0",
            },
            method=method,
        )
        try:
            response = urlopen(request, timeout=timeout)
            body = response.read()
            return json.loads(body) if body else None
        except HTTPError as error:
            if error.code == 429:
                try:
                    retry_after = float(error.headers.get("Retry-After", "1"))
                except (TypeError, ValueError):
                    retry_after = 1.0
                raise DiscordRateLimited(min(retry_after, 60.0)) from error
            try:
                detail = error.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            raise DiscordError(f"Discord returned HTTP {error.code} for {method} {path}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise DiscordError(f"Could not reach Discord for {method} {path}.") from error
        except json.JSONDecodeError as error:
            raise DiscordError("Discord returned invalid JSON.") from error

    def get_messages_after(self, channel_id: str, after: str | None, limit: int = 50) -> list[dict]:
        path = f"/channels/{channel_id}/messages?limit={limit}"
        if after is not None:
            path += f"&after={after}"
        result = self._call("GET", path, timeout=POLL_INTERVAL + 10)
        messages = [m for m in result if isinstance(m, dict)] if isinstance(result, list) else []
        # Discord returns newest-first; the bridge wants to replay oldest-first.
        messages.reverse()
        return messages

    def send_text(self, channel_id: str, text: str) -> None:
        body = text.strip()
        if not body:
            raise ValueError("Discord message cannot be empty.")
        if len(body) > MAX_MESSAGE_LENGTH:
            body = body[: MAX_MESSAGE_LENGTH - 1] + "…"
        self._call("POST", f"/channels/{channel_id}/messages", {"content": body})

    def current_user_id(self) -> str:
        result = self._call("GET", "/users/@me")
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise DiscordError("Could not identify the bot's own user ID.")
        return result["id"]


class PendingApproval:
    def __init__(self, code: str) -> None:
        self.code = code
        self.reply: queue.Queue[bool] = queue.Queue(maxsize=1)


class DiscordBridge:
    def __init__(
        self,
        *,
        bot_token: str,
        channel_id: str,
        owner_user_id: str | None,
        project_root: Path,
        python: str = sys.executable,
    ) -> None:
        self.api = DiscordBotAPI(bot_token)
        self.channel_id = channel_id
        self.owner_user_id = owner_user_id
        self.project_root = project_root.resolve()
        self.python = python
        self._bot_user_id: str | None = None
        self._last_message_id: str | None = None
        self._task_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: PendingApproval | None = None
        self._active_process: subprocess.Popen[str] | None = None
        self._stopping = threading.Event()

    def run_forever(self) -> None:
        self._bot_user_id = self.api.current_user_id()
        while not self._stopping.is_set():
            wait_seconds = POLL_INTERVAL
            try:
                for message in self.api.get_messages_after(self.channel_id, self._last_message_id):
                    self._last_message_id = message.get("id") or self._last_message_id
                    self._handle_message(message)
            except DiscordRateLimited as error:
                LOGGER.warning("Rate-limited: %s", error)
                wait_seconds = error.retry_after
            except DiscordError as error:
                LOGGER.warning("Poll failed: %s", error)
            self._stopping.wait(wait_seconds)

    def stop(self) -> None:
        self._stopping.set()
        self.stop_active_task()

    def _handle_message(self, message: dict) -> None:
        author = message.get("author")
        text = message.get("content")
        if not isinstance(author, dict) or not isinstance(text, str):
            return
        author_id = author.get("id")
        if not isinstance(author_id, str) or author_id == self._bot_user_id:
            return  # ignore the bot's own messages
        if self.owner_user_id is None:
            # Pairing mode: the first person to write in the channel becomes the owner. No
            # command ever runs here, same as Telegram's chat-ID pairing.
            self.owner_user_id = author_id
            print(f"DISCORD_BRIDGE_PAIRED:{author_id}", flush=True)
            try:
                self.api.send_text(
                    self.channel_id,
                    f"Paired to <@{author_id}>. Only messages from you will run on this Mac from now on.",
                )
            except DiscordError:
                LOGGER.exception("Could not send pairing confirmation")
            return
        if author_id != self.owner_user_id:
            return  # silently ignore anyone who is not the configured owner
        text = text.strip()
        if not text or len(text) > MAX_MESSAGE_LENGTH:
            self.api.send_text(self.channel_id, "Command ignored: text must be 1–2,000 characters.")
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
            self.api.send_text(self.channel_id, "Stopped the active remote task.")
            return
        if not self._task_lock.acquire(blocking=False):
            self.api.send_text(self.channel_id, "A remote task is already running. Send STOP to cancel it.")
            return
        threading.Thread(target=self._run_remote_task, args=(command,), daemon=True).start()

    def _answer_approval(self, command: str) -> None:
        parts = command.split()
        if len(parts) != 2 or not parts[1].isalnum():
            self.api.send_text(self.channel_id, "Use APPROVE <code> or DENY <code> from the pending confirmation.")
            return
        with self._pending_lock:
            pending = self._pending
            if pending is None or parts[1] != pending.code:
                self.api.send_text(self.channel_id, "That confirmation code is not current or has expired.")
                return
            if not pending.reply.empty():
                self.api.send_text(self.channel_id, "That confirmation was already answered.")
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
                raise DiscordError("Could not find assistant.py beside the bridge.")
            environment = os.environ.copy()
            environment.pop("HELM_DISCORD_BOT_TOKEN", None)
            environment["HELM_DISCORD_REMOTE"] = "1"
            arguments = [
                self.python, "-u", str(script), "--chat", prompt, "--autonomy", "autonomous",
                "--history", str(Path.home() / "Library/Application Support/Local Desktop Assistant/discord_history.json"),
            ]
            provider = environment.get("HELM_DISCORD_PROVIDER", "ollama")
            if provider in {"ollama", "anthropic", "openai", "gemini", "openrouter"}:
                arguments += ["--provider", provider]
            model = environment.get("HELM_DISCORD_MODEL")
            if model:
                arguments += ["--model", model]
            disabled_tools = environment.get("HELM_DISCORD_DISABLED_TOOLS", "")
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
                    raise DiscordError("Remote task exceeded the 5-minute limit.")
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
                raise DiscordError("Remote task did not complete successfully.")
            if not isinstance(response, str) or not response.strip():
                raise DiscordError("Remote task ended without a verified text response.")
            self.api.send_text(self.channel_id, response.strip())
        except (OSError, subprocess.SubprocessError, DiscordError) as error:
            LOGGER.warning("Remote Discord task failed: %s", error)
            try:
                self.api.send_text(self.channel_id, f"Helm could not complete that task: {error}")
            except DiscordError:
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
            self.channel_id,
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
            raise DiscordError("Remote approval channel is unavailable.")
        process.stdin.write("y\n" if approved else "n\n")
        process.stdin.flush()
        if not approved:
            raise DiscordError("Remote action was not approved.")


def configure_logging() -> Path:
    log_directory = Path.home() / "Library" / "Logs" / "Helm"
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_directory, 0o700)
    log_path = log_directory / "discord-bridge.log"
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    os.chmod(log_path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    return log_path


def bridge_from_environment() -> DiscordBridge:
    bot_token = os.environ.get("HELM_DISCORD_BOT_TOKEN", "")
    channel_id = os.environ.get("HELM_DISCORD_CHANNEL_ID", "").strip()
    owner_raw = os.environ.get("HELM_DISCORD_OWNER_USER_ID", "").strip()
    if not bot_token:
        raise ValueError("Missing Discord configuration: bot token.")
    if not channel_id.isdigit():
        raise ValueError("Missing or invalid Discord configuration: channel ID.")
    if owner_raw and not owner_raw.isdigit():
        raise ValueError("Discord owner user ID must be numeric.")
    owner_user_id: str | None = owner_raw or None  # empty means pairing mode
    project_root = Path(os.environ.get("ASSISTANT_PROJECT_DIR", Path(__file__).resolve().parent))
    return DiscordBridge(
        bot_token=bot_token,
        channel_id=channel_id,
        owner_user_id=owner_user_id,
        project_root=project_root,
        python=os.environ.get("HELM_DISCORD_PYTHON", sys.executable),
    )


def run_bridge() -> int:
    configure_logging()
    try:
        bridge = bridge_from_environment()
    except ValueError as error:
        LOGGER.exception("Discord bridge configuration failed")
        print(f"DISCORD_BRIDGE_ERROR:{error}", flush=True)
        return 1
    mode = "pairing (no owner set yet)" if bridge.owner_user_id is None else f"owner={bridge.owner_user_id}"
    LOGGER.info("Discord bridge polling started, channel=%s %s", bridge.channel_id, mode)
    print(f"DISCORD_BRIDGE_READY:channel {bridge.channel_id}, {mode}", flush=True)

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
        bridge.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run_bridge())
