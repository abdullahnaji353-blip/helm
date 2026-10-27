"""Schedule recurring Android TV actions via macOS launchd (per-user LaunchAgents).

Each schedule is one plist in ~/Library/LaunchAgents, labeled
local.desktop-assistant.tv.<name>, that runs tv_schedule_cli.py daily at a fixed time.
Only runs while this Mac is awake and powered on; it doesn't wake the Mac itself.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "local.desktop-assistant.tv."
CLI_PATH = Path(__file__).resolve().parent / "tv_schedule_cli.py"
LOG_PATH = Path.home() / "Library" / "Logs" / "Helm" / "tv-schedule.log"

VALID_ACTIONS = {"wake", "sleep", "open_app", "set_volume", "remote_key"}


class SchedulerError(RuntimeError):
    """A scheduled TV action could not be created, listed, or removed."""


def _label(name: str) -> str:
    return f"{LABEL_PREFIX}{name}"


def _plist_path(name: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{_label(name)}.plist"


def schedule_action(name: str, action: str, args: list[str], hour: int, minute: int) -> Path:
    """Install a launchd job that runs `action` daily at hour:minute. Overwrites any existing
    schedule with the same name."""
    name = name.strip()
    if not name:
        raise SchedulerError("A schedule name is required.")
    if action not in VALID_ACTIONS:
        raise SchedulerError(f"Unknown action {action!r}. Valid: {', '.join(sorted(VALID_ACTIONS))}.")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise SchedulerError("Time must be a valid 24-hour hour (0-23) and minute (0-59).")
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist_path = _plist_path(name)
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, timeout=10)
    plist = {
        "Label": _label(name),
        "ProgramArguments": [sys.executable, str(CLI_PATH), action, *args],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        plist_path.unlink(missing_ok=True)
        raise SchedulerError((result.stderr or result.stdout).strip() or "launchctl load failed.")
    return plist_path


def list_scheduled_actions() -> list[dict]:
    if not LAUNCH_AGENTS_DIR.exists():
        return []
    schedules = []
    for path in sorted(LAUNCH_AGENTS_DIR.glob(f"{LABEL_PREFIX}*.plist")):
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            continue
        program_args = plist.get("ProgramArguments", [])
        action_args = program_args[2:]  # skip [python executable, cli path]
        interval = plist.get("StartCalendarInterval", {})
        schedules.append({
            "name": path.stem[len(LABEL_PREFIX):],
            "action": action_args[0] if action_args else "",
            "args": action_args[1:],
            "hour": interval.get("Hour"),
            "minute": interval.get("Minute"),
        })
    return schedules


def remove_scheduled_action(name: str) -> None:
    plist_path = _plist_path(name.strip())
    if not plist_path.exists():
        raise SchedulerError(f"No schedule named {name!r} exists.")
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, timeout=10)
    plist_path.unlink()
