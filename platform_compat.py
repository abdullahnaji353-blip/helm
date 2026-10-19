"""Small OS-dispatched helpers so assistant.py doesn't inline macOS-only commands.

Each function's Darwin branch is a verbatim move of the behavior assistant.py
used to have inline (not a rewrite), so macOS behavior is unchanged. The
Windows branch is a best-effort equivalent using the standard library plus
pywin32/psutil/pyttsx3 (see requirements.txt, all gated to sys_platform ==
"win32"). Anything beyond these five operations (AppleScript automation of
specific apps, Finder's trash-delete script, system volume, media-player
control, etc.) is out of scope for this pass — those stay macOS-only and will
raise a clear error on other platforms rather than silently doing nothing.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess


def _system() -> str:
    return platform.system()


def open_path(target: str, app: str | None = None, timeout: float | None = None) -> None:
    """Open a file/URL, optionally with a named app. Mirrors macOS `open [-a app] target`."""
    system = _system()
    if system == "Darwin":
        command = ["open"]
        if app:
            command += ["-a", app]
        command.append(target)
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
        return
    if system == "Windows":
        if app:
            try:
                subprocess.run([app, target] if target else [app], check=True, timeout=timeout)
                return
            except (OSError, subprocess.CalledProcessError):
                pass  # fall through to the OS default handler for `target`
        os.startfile(target)  # type: ignore[attr-defined]
        return
    raise RuntimeError(f"Opening apps/URLs is not supported on {system}.")


def clipboard_copy(text: str) -> None:
    system = _system()
    if system == "Darwin":
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        return
    if system == "Windows":
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return
    raise RuntimeError(f"Clipboard access is not supported on {system}.")


def clipboard_paste() -> str:
    system = _system()
    if system == "Darwin":
        return subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
    if system == "Windows":
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            try:
                return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))
            except TypeError:
                return ""
        finally:
            win32clipboard.CloseClipboard()
    raise RuntimeError(f"Clipboard access is not supported on {system}.")


def speak(text: str) -> None:
    system = _system()
    if system == "Darwin":
        try:
            subprocess.run(["say", text], check=True)
        except FileNotFoundError as error:
            raise RuntimeError("macOS 'say' command was not found.") from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Speech playback failed (exit code {error.returncode}).") from error
        return
    if system == "Windows":
        try:
            import pyttsx3
        except ImportError as error:
            raise RuntimeError(
                "Text-to-speech needs pyttsx3. Install it with: python -m pip install -r requirements.txt"
            ) from error
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        return
    raise RuntimeError(f"Text-to-speech is not supported on {system}.")


def battery_status() -> str:
    """A short description such as 'battery 82% (charging)', or 'no battery info'."""
    system = _system()
    if system == "Darwin":
        output = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
        match = re.search(r"(\d+)%;\s*([^;]+)", output)
        return f"battery {match.group(1)}% ({match.group(2)})" if match else "no battery info"
    if system == "Windows":
        try:
            import psutil
        except ImportError:
            return "no battery info"
        battery = psutil.sensors_battery()
        if battery is None:
            return "no battery info"
        state = "charging" if battery.power_plugged else "discharging"
        return f"battery {int(battery.percent)}% ({state})"
    return "no battery info"


def paste_combo() -> tuple[str, str]:
    """The (modifier, key) pyautogui.hotkey() pair for "paste" on this OS."""
    return ("command", "v") if _system() == "Darwin" else ("ctrl", "v")
