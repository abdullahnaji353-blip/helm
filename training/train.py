#!/usr/bin/env python3
"""Training curriculum for Chatbot: basic -> heavy tasks, each with an objective check.

Chatbot runs every task by itself (autonomous, through `assistant.py --chat`).
A task that fails is retried once with a small hint. Messages are dry-run:
chats are opened and verified, but nothing is sent. Results go to
training/results.jsonl; successful screen tasks are remembered as skills.

Usage: python training/train.py [task-id ...]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
HISTORY = Path.home() / "Library/Application Support/Local Desktop Assistant/chat_history.json"
RESULTS = ROOT / "training" / "results.jsonl"
sys.path.insert(0, str(ROOT))
from screen_elements import read_app  # noqa: E402


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def volume() -> str:
    return sh("osascript", "-e", "output volume of (get volume settings)")


def clipboard() -> str:
    return sh("pbpaste")


def safari_url() -> str:
    return sh("osascript", "-e", 'tell application "Safari" to get URL of front document')


TASKS = [
    # --- basic ---------------------------------------------------------------
    {"id": "status", "level": "basic", "say": "what's my battery and the time?",
     "check": lambda out, reply: "%" in reply},
    {"id": "open-app", "level": "basic", "say": "open TextEdit",
     "check": lambda out, reply: bool(sh("pgrep", "-x", "TextEdit"))},
    {"id": "volume", "level": "basic", "say": "set the volume to 25",
     "check": lambda out, reply: volume() == "25"},
    {"id": "copy", "level": "basic", "say": "copy the word banana to the clipboard",
     "check": lambda out, reply: clipboard().strip().lower() == "banana"},
    {"id": "dm-top", "level": "basic", "say": "go to discord then tizi aziz then say hi",
     "check": lambda out, reply: "Dry run: opened and verified" in out},
    # --- medium ----------------------------------------------------------------
    {"id": "dm-old", "level": "medium", "say": "on discord send jstparkz the message yo",
     "hint": "it's a direct message with a person, name SMT | jstparkz",
     "check": lambda out, reply: "Dry run: opened and verified" in out and "jstparkz" in out.lower()},
    {"id": "channel", "level": "medium", "say": "say gg in the polls channel of the KeyMau Esports server on discord",
     "hint": "that's a channel in a server, not a DM",
     "check": lambda out, reply: "Dry run: opened and verified #polls" in out},
    {"id": "youtube", "level": "medium", "say": "find youtube videos about lofi music",
     "check": lambda out, reply: "youtube.com/watch" in out},
    # --- heavy -----------------------------------------------------------------
    {"id": "web-fact", "level": "heavy", "say": "search the web for how tall the Eiffel Tower is in meters and tell me",
     "hint": "search, then read the results page",
     "check": lambda out, reply: bool(re.search(r"\b(3[0-3]\d)\b", reply))},
    {"id": "web-to-clipboard", "level": "heavy", "say": "find a youtube video about cats and copy its link to the clipboard",
     "hint": "web_search on youtube, then clipboard copy of one /watch link",
     "check": lambda out, reply: "youtube.com/watch" in clipboard()},
    {"id": "chatgpt", "level": "heavy", "say": "ask ChatGPT what 17 times 3 is and tell me what it answered",
     "hint": "paste the question into ChatGPT with submit, wait, then read_screen ChatGPT",
     "check": lambda out, reply: "51" in reply},
    {"id": "learn-youtube", "level": "heavy",
     "say": "search youtube for how to train an AI and tell me the titles of the top 3 videos",
     "check": lambda out, reply: reply.count("\n") >= 2 or reply.lower().count("ai") >= 3},
    {"id": "screen-safari", "level": "heavy", "say": "use the screen to open Safari and go to wikipedia.org",
     "hint": "control_screen: open Safari, command+l, type the address, enter",
     "check": lambda out, reply: "wikipedia.org" in safari_url()},
    {"id": "screen-textedit", "level": "heavy",
     "say": "use the screen to make a new TextEdit document and type Hello from Chatbot",
     "hint": "control_screen: open TextEdit, command+n, type the text",
     "check": lambda out, reply: "Hello from Chatbot" in read_app("TextEdit")[0]},
]


def run(say: str) -> tuple[str, str, float]:
    if HISTORY.exists():
        HISTORY.unlink()  # every task starts from a clean conversation
    env = {**os.environ, "CHATBOT_DRY_RUN": "1"}
    started = time.time()
    try:
        proc = subprocess.run(
            [str(PYTHON), "-u", str(ROOT / "assistant.py"), "--chat", say, "--autonomy", "autonomous"],
            capture_output=True, text=True, timeout=600, env=env, cwd=ROOT,
        )
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as error:
        out = (error.stdout or b"").decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
        out += "\nTIMEOUT"
    match = re.search(r"^CHAT_REPLY_JSON:(.*)$", out, re.M)
    reply = json.loads(match.group(1)) if match else ""
    return out, reply, time.time() - started


def main() -> int:
    wanted = set(sys.argv[1:])
    saved_history = HISTORY.read_bytes() if HISTORY.exists() else None
    saved_volume, saved_clipboard = volume(), clipboard()
    passed = 0
    selected = [t for t in TASKS if not wanted or t["id"] in wanted]
    try:
        for task in selected:
            attempts = [task["say"]] + ([f"{task['say']} (hint: {task['hint']})"] if task.get("hint") else [])
            for attempt, say in enumerate(attempts, start=1):
                out, reply, seconds = run(say)
                ok = bool(task["check"](out, reply))
                tools = re.findall(r"^TOOL: (\w+)", out, re.M)
                print(f"[{'PASS' if ok else 'FAIL'}] {task['level']:6} {task['id']:16} try {attempt} "
                      f"{seconds:5.0f}s tools={tools} reply={reply[:110]!r}", flush=True)
                with RESULTS.open("a", encoding="utf-8") as log:
                    log.write(json.dumps({"id": task["id"], "try": attempt, "say": say, "ok": ok,
                                          "tools": tools, "reply": reply, "seconds": round(seconds),
                                          "output": out[-3000:]}) + "\n")
                if ok:
                    passed += 1
                    break
    finally:
        if saved_history is not None:
            HISTORY.write_bytes(saved_history)
        subprocess.run(["osascript", "-e", f"set volume output volume {saved_volume or 50}"])
        subprocess.run(["pbcopy"], input=saved_clipboard, text=True)
    print(f"\n{passed}/{len(selected)} tasks passed")
    return 0 if passed == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
