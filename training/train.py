#!/usr/bin/env python3
"""Train Helm on the curriculum (training/curriculum.py, 1000+ tasks).

Helm does every task by itself (autonomous `assistant.py --chat`). A failed
task is retried once with a small hint. Messages are dry runs: chats are
opened and verified, never sent. Afterwards training/REPORT.md lists every
failure with the likely problem and fix; it opens in VS Code and is copied to
the clipboard, ready to paste into GitHub Copilot Chat.

Usage:
  python training/train.py --level easy --sample 20     # 20 random easy tasks
  python training/train.py --sample 40                  # 10 per level
  python training/train.py calc-3plus5 dm-STK-go         # specific task ids
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "training")]
from curriculum import LEVELS, TASKS, clipboard, volume  # noqa: E402

PYTHON = ROOT / ".venv" / "bin" / "python"
HISTORY = Path.home() / "Library/Application Support/Local Desktop Assistant/chat_history.json"
RESULTS = ROOT / "training" / "results.jsonl"
REPORT = ROOT / "training" / "REPORT.md"
TOOL_ERRORS = ("could not", "failed", "not installed", "kept", "no '", "nothing was", "not a valid", "declined")


def run(say: str) -> tuple[str, str, float]:
    if HISTORY.exists():
        HISTORY.unlink()  # every task starts from a clean conversation
    started = time.time()
    try:
        proc = subprocess.run(
            [str(PYTHON), "-u", str(ROOT / "assistant.py"), "--chat", say, "--autonomy", "autonomous"],
            capture_output=True, text=True, timeout=300, cwd=ROOT,
            env={**os.environ, "HELM_DRY_RUN": "1"},
        )
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as error:
        out = (error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or "") + "\nTIMEOUT"
    match = re.search(r"^CHAT_REPLY_JSON:(.*)$", out, re.M)
    reply = json.loads(match.group(1)) if match else ""
    return out, reply, time.time() - started


def diagnose(task: dict, out: str, reply: str, tools: list[str]) -> tuple[str, str]:
    """Best guess at why a task failed, and what to change."""
    results = re.findall(r"^RESULT: (.*)$", out, re.M)
    if "TIMEOUT" in out:
        return "Took longer than 5 minutes.", "Cut steps: give the task a dedicated tool instead of control_screen."
    if "Error:" in out and not reply:
        error = re.findall(r"^Error: (.*)$", out, re.M)
        return f"Crashed: {error[-1] if error else 'unknown error'}", "Handle this error in assistant.py instead of exiting."
    if not tools and task["expect"]:
        return (f"Answered in words instead of acting (expected `{task['expect']}`).",
                f"Add an example mapping this request to `{task['expect']}` in SYSTEM_PROMPT, or sharpen that tool's description.")
    if task["expect"] and task["expect"] not in tools and tools:
        return (f"Used {tools} instead of `{task['expect']}`.",
                f"Clarify when to use `{task['expect']}` vs `{tools[0]}` in the tool descriptions.")
    bad = [r for r in results if any(word in r.lower() for word in TOOL_ERRORS)]
    if bad:
        return f"Tool reported a problem: {bad[-1][:200]}", "Make that tool recover (retry, search another way) before giving up."
    if not task["expect"]:
        return "Gave a wrong or unclear answer.", "Small-model knowledge gap: let it use web_search, or use a bigger brain."
    return ("Claimed success but the real check failed (the Mac state is not what was asked).",
            "Add verification inside the tool so it notices and retries.")


def write_report(rows: list[dict]) -> str:
    by_level = {level: Counter(r["ok"] for r in rows if r["level"] == level) for level in LEVELS}
    failed = [r for r in rows if not r["ok"]]
    lines = [
        "# Helm training report",
        "",
        "@workspace This is Helm's latest training run. For each failure below, propose the code change "
        "(in assistant.py, screen_elements.py or providers.py) that would make it pass. Keep fixes small.",
        "",
        "| Level | Passed | Failed |", "|---|---|---|",
    ]
    for level, counts in by_level.items():
        if counts:
            lines.append(f"| {level} | {counts[True]} | {counts[False]} |")
    lines += ["", f"**Total: {len(rows) - len(failed)}/{len(rows)} passed**", ""]
    if not failed:
        lines.append("Everything passed. 🎉")
    for r in failed:
        lines += [
            f"## ❌ {r['id']} ({r['level']})",
            f"- **Asked:** {r['say']}",
            f"- **Tools used:** {', '.join(r['tools']) or 'none'}",
            f"- **Reply:** {r['reply'][:200] or '(none)'}",
            f"- **Problem:** {r['problem']}",
            f"- **Suggested fix:** {r['fix']}",
            f"- **Reproduce:** `HELM_DRY_RUN=1 .venv/bin/python assistant.py --chat \"{r['say']}\" --autonomy autonomous`",
            "",
        ]
    report = "\n".join(lines)
    REPORT.write_text(report, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="Task ids to run")
    parser.add_argument("--level", choices=LEVELS)
    parser.add_argument("--sample", type=int, help="Run this many random tasks (spread across levels)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the report in VS Code")
    args = parser.parse_args()

    pool = [t for t in TASKS if (not args.ids or t["id"] in args.ids) and (not args.level or t["level"] == args.level)]
    if args.sample:
        levels = [args.level] if args.level else LEVELS
        per_level = max(1, args.sample // len(levels))
        pool = [t for level in levels for t in random.sample([p for p in pool if p["level"] == level],
                                                            min(per_level, sum(p["level"] == level for p in pool)))]
    print(f"Training on {len(pool)} of {len(TASKS)} tasks…", flush=True)

    saved_history = HISTORY.read_bytes() if HISTORY.exists() else None
    saved_volume, saved_clipboard = volume(), clipboard()
    rows = []
    try:
        for number, task in enumerate(pool, start=1):
            attempts = [task["say"]] + ([f"{task['say']} (hint: {task['hint']})"] if task.get("hint") else [])
            for attempt, say in enumerate(attempts, start=1):
                if task.get("setup"):
                    task["setup"]()
                out, reply, seconds = run(say)
                tools = re.findall(r"^TOOL: (\w+)", out, re.M)
                ok = bool(task["check"](out, reply))
                if task.get("cleanup"):
                    task["cleanup"]()
                print(f"[{number}/{len(pool)}] {'PASS' if ok else 'FAIL'} {task['level']:7} {task['id']:34} "
                      f"try {attempt} {seconds:4.0f}s tools={tools} reply={reply[:80]!r}", flush=True)
                row = {"id": task["id"], "level": task["level"], "say": say, "try": attempt, "ok": ok,
                       "tools": tools, "reply": reply, "seconds": round(seconds)}
                if not ok:
                    row["problem"], row["fix"] = diagnose(task, out, reply, tools)
                with RESULTS.open("a", encoding="utf-8") as log:
                    log.write(json.dumps({**row, "output": out[-3000:]}) + "\n")
                if ok or attempt == len(attempts):
                    rows.append(row)
                if ok:
                    break
    finally:
        if saved_history is not None:
            HISTORY.write_bytes(saved_history)
        subprocess.run(["osascript", "-e", f"set volume output volume {saved_volume or 50}"])
        if rows:
            report = write_report(rows)
            subprocess.run(["pbcopy"], input=report, text=True)  # ready to paste into Copilot Chat
            if not args.no_open:
                subprocess.run(["open", "-a", "Visual Studio Code", str(REPORT)])
        else:
            subprocess.run(["pbcopy"], input=saved_clipboard, text=True)
    passed = sum(r["ok"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed. Report: {REPORT} (copied to the clipboard for Copilot)")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
