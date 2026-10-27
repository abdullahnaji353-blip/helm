#!/usr/bin/env python3
"""Non-interactive CLI for running one TV action (used by scheduled launchd jobs).

Not for interactive use: there is no approval prompt here, since a scheduled job runs
unattended. Confirmation happens once, when the schedule itself is created
(tv_scheduler.schedule_action), not each time it fires.
"""

from __future__ import annotations

import sys
from pathlib import Path

import tv_control

TV_SERIAL_PATH = (
    Path.home() / "Library" / "Application Support" / "Local Desktop Assistant" / "tv_device.txt"
)


def _active_serial() -> str:
    try:
        serial = TV_SERIAL_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        serial = ""
    if not serial or not tv_control.is_connected(serial):
        raise tv_control.AdbError("No TV is connected.")
    return serial


def run_action(action: str, args: list[str]) -> None:
    serial = _active_serial()
    if action == "wake":
        tv_control.wake(serial)
    elif action == "sleep":
        tv_control.sleep_device(serial)
    elif action == "open_app":
        if not args:
            raise ValueError("open_app needs an app name.")
        package = tv_control.find_package(serial, args[0])
        if package is None:
            raise ValueError(f"{args[0]!r} is not installed on the TV.")
        tv_control.launch_app(serial, package)
    elif action == "set_volume":
        if not args:
            raise ValueError("set_volume needs a level.")
        tv_control.set_volume(serial, int(args[0]))
    elif action == "remote_key":
        if not args:
            raise ValueError("remote_key needs a key name.")
        tv_control.send_key(serial, args[0])
    else:
        raise ValueError(f"Unknown action {action!r}.")


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "Usage: tv_schedule_cli.py <wake|sleep|open_app|set_volume|remote_key> [args...]",
            file=sys.stderr,
        )
        return 2
    action, *rest = argv
    try:
        run_action(action, rest)
    except (tv_control.AdbError, ValueError) as error:
        print(f"tv_schedule_cli: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
