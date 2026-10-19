"""Chat window: Tkinter UI that spawns assistant.py and speaks its stdout protocol.

This is the Windows equivalent of the chat flow in
Sources/MacMenuAssistant/ChatView.swift's `send()`/`launch()`/`receive()`. It
reuses the exact same subprocess contract (python -u assistant.py --chat
<text> --autonomy approval|autonomous, line-prefixed stdout, "y"/"all"/"n"
written to stdin for approvals) so assistant.py needs no UI-side protocol
changes to support this second frontend.

Deferred from this first pass (see the plan/README): voice input, the
3-background-agent pipeline view, the Logs tab, and bridge (WhatsApp/
Telegram/Discord/Slack) settings. Chat, task execution, and the approval
flow are implemented.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _python_executable() -> str:
    venv_python = _project_root() / ".venv" / "Scripts" / "python.exe"
    return str(venv_python) if venv_python.exists() else sys.executable


class BackendSession:
    """Owns one assistant.py subprocess and its stdout-parsing thread."""

    def __init__(self, on_event) -> None:
        self._on_event = on_event  # called with dict events from the reader thread
        self.process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, text: str, autonomous: bool) -> None:
        if self.running:
            return
        script = _project_root() / "assistant.py"
        arguments = [
            _python_executable(), "-u", str(script),
            "--chat", text,
            "--autonomy", "autonomous" if autonomous else "approval",
        ]
        try:
            self.process = subprocess.Popen(
                arguments,
                cwd=str(_project_root()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            self._on_event({"type": "error", "text": f"Could not start Helm: {error}"})
            return
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            self._on_event(self._parse_line(line))
        process.wait()
        self._on_event({"type": "finished", "code": process.returncode})

    @staticmethod
    def _parse_line(line: str) -> dict:
        if line.startswith("APPROVAL_REQUIRED: "):
            return {"type": "approval", "text": line[len("APPROVAL_REQUIRED: "):]}
        if line.startswith("CHAT_REPLY_JSON:"):
            try:
                reply = json.loads(line[len("CHAT_REPLY_JSON:"):])
            except json.JSONDecodeError:
                return {"type": "activity", "text": line}
            return {"type": "reply", "text": str(reply)}
        if line.startswith("TOOL: "):
            return {"type": "activity", "text": line[len("TOOL: "):]}
        if line.startswith("CURSOR: "):
            return {"type": "cursor", "text": line[len("CURSOR: "):]}
        if line.startswith("Error:") or line.startswith("SCREEN_ACCESS_DENIED:"):
            return {"type": "error", "text": line}
        if line.startswith("Allow this action?"):
            return {"type": "noop", "text": line}
        return {"type": "activity", "text": line}

    def respond(self, answer: str) -> None:
        if self.process is None or self.process.stdin is None:
            return
        try:
            self.process.stdin.write(answer + "\n")
            self.process.stdin.flush()
        except OSError:
            pass

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


class ChatWindow(tk.Toplevel):
    """Borderless-ish chat popup, the nearest Tk equivalent of the macOS popover."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Helm")
        self.geometry("420x640")
        self.protocol("WM_DELETE_WINDOW", self.withdraw)

        self._queue: queue.Queue[dict] = queue.Queue()
        self._session: BackendSession | None = None
        self._autonomous = tk.BooleanVar(value=False)
        self._pending_approval: str | None = None

        self._build_widgets()
        self.after(100, self._drain_queue)

    def _build_widgets(self) -> None:
        top_bar = ttk.Frame(self)
        top_bar.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(
            top_bar, text="Act autonomously", variable=self._autonomous,
        ).pack(side="left")
        ttk.Button(top_bar, text="New chat", command=self._new_chat).pack(side="right")

        self.transcript = tk.Text(self, wrap="word", state="disabled", height=24)
        self.transcript.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.transcript.tag_configure("user", foreground="#2563eb")
        self.transcript.tag_configure("bot", foreground="#111827")
        self.transcript.tag_configure("activity", foreground="#6b7280")
        self.transcript.tag_configure("error", foreground="#dc2626")

        self.approval_frame = ttk.Frame(self)
        self.approval_label = ttk.Label(self.approval_frame, wraplength=380)
        self.approval_label.pack(fill="x", padx=4, pady=(4, 2))
        buttons = ttk.Frame(self.approval_frame)
        buttons.pack(fill="x", pady=(0, 4))
        ttk.Button(buttons, text="Allow", command=lambda: self._answer_approval("y")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Allow All", command=lambda: self._answer_approval("all")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Stop task", command=lambda: self._answer_approval("n")).pack(side="left", padx=4)

        entry_bar = ttk.Frame(self)
        entry_bar.pack(fill="x", padx=8, pady=8)
        self.entry = ttk.Entry(entry_bar)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Control-Return>", lambda _event: self._send())
        self.entry.bind("<Return>", lambda _event: self._send())
        ttk.Button(entry_bar, text="Send", command=self._send).pack(side="left", padx=(6, 0))

    def show(self) -> None:
        self.deiconify()
        self.lift()
        self.entry.focus_set()

    def _append(self, text: str, tag: str) -> None:
        self.transcript.configure(state="normal")
        if self.transcript.index("end-1c") != "1.0":
            self.transcript.insert("end", "\n")
        self.transcript.insert("end", text, tag)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _new_chat(self) -> None:
        if self._session is not None and self._session.running:
            self._session.stop()
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._hide_approval()

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text or (self._session is not None and self._session.running):
            return
        self.entry.delete(0, "end")
        self._append(f"You: {text}", "user")
        self._session = BackendSession(on_event=self._queue.put)
        self._session.start(text, autonomous=self._autonomous.get())

    def _answer_approval(self, answer: str) -> None:
        if self._session is None:
            return
        self._session.respond(answer)
        self._hide_approval()

    def _hide_approval(self) -> None:
        self._pending_approval = None
        self.approval_frame.pack_forget()

    def _drain_queue(self) -> None:
        try:
            while True:
                event = self._queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "reply":
            self._append(f"Helm: {event['text']}", "bot")
        elif kind == "activity":
            self._append(event["text"], "activity")
        elif kind == "error":
            self._append(event["text"], "error")
        elif kind == "approval":
            self._pending_approval = event["text"]
            self.approval_label.configure(text=f"Allow this action? {event['text']}")
            self.approval_frame.pack(fill="x", padx=8, pady=(0, 6))
        elif kind == "finished":
            self._hide_approval()
