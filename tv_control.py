"""Control an Android TV / Google TV over ADB (Android Debug Bridge).

Requires the TV to have Developer options enabled and either legacy network
ADB debugging (`adb connect host:port`, port usually 5555) or Android 11+
Wireless debugging (`adb pair host:port CODE` once, then `adb connect`).

The `adb` binary is resolved from, in order: the `HELM_ADB_PATH` environment
variable, `vendor/platform-tools/adb` next to this file, or `adb` on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ADB_TIMEOUT = 15

# Well-known Android TV app packages, so "open Netflix" works without the
# user knowing package names. Not exhaustive; falls back to matching any
# installed package name/label substring.
KNOWN_TV_APPS: dict[str, str] = {
    "netflix": "com.netflix.ninja",
    "youtube": "com.google.android.youtube.tv",
    "youtube tv": "com.google.android.youtube.tv",
    "disney+": "com.disney.disneyplus",
    "disney plus": "com.disney.disneyplus",
    "prime video": "com.amazon.amazonvideo.livingroom",
    "amazon prime video": "com.amazon.amazonvideo.livingroom",
    "spotify": "com.spotify.tv.android",
    "plex": "com.plexapp.android",
    "hulu": "com.hulu.livingroomplus",
    "hbo max": "com.hbo.hbonow",
    "max": "com.hbo.hbonow",
    "apple tv": "com.apple.atve.androidtv.appletv",
    "twitch": "tv.twitch.android.app",
    "vlc": "org.videolan.vlc",
    "kodi": "org.xbmc.kodi",
    "settings": "com.android.tv.settings",
}

# Common remote-control key events. Full list: `adb shell input keyevent --help`.
KEY_EVENTS: dict[str, int] = {
    "home": 3,
    "back": 4,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "select": 23,
    "ok": 23,
    "enter": 66,
    "menu": 82,
    "play_pause": 85,
    "play": 126,
    "pause": 127,
    "volume_up": 24,
    "volume_down": 25,
    "mute": 164,
    "power": 26,
}


class AdbError(RuntimeError):
    """adb could not perform the requested action."""


@dataclass(frozen=True)
class TvApp:
    package: str
    label: str


def _adb_path() -> str:
    override = os.environ.get("HELM_ADB_PATH")
    if override:
        return override
    bundled = Path(__file__).resolve().parent / "vendor" / "platform-tools" / "adb"
    if bundled.exists():
        return str(bundled)
    found = shutil.which("adb")
    if found:
        return found
    raise AdbError(
        "adb was not found. Set HELM_ADB_PATH, or place platform-tools under "
        "vendor/platform-tools/, or install it on PATH."
    )


def _run(*args: str, timeout: int = ADB_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            [_adb_path(), *args], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as error:
        raise AdbError(f"adb is not executable: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise AdbError(f"adb timed out: {' '.join(args)}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
        raise AdbError(detail)
    return result.stdout.strip()


def pair(host_port: str, pairing_code: str) -> str:
    """One-time pairing for Android 11+ Wireless debugging, e.g. pair('192.168.1.5:41235', '123456')."""
    return _run("pair", host_port, pairing_code)


def connect(host: str, port: int = 5555) -> str:
    """Connect to a TV with network/USB debugging already enabled. Returns the device serial."""
    serial = f"{host}:{port}"
    output = _run("connect", serial, timeout=20)
    if "unable to connect" in output.lower() or "failed" in output.lower():
        raise AdbError(output)
    return serial


def disconnect(serial: str) -> None:
    _run("disconnect", serial)


def is_connected(serial: str) -> bool:
    try:
        devices = _run("devices")
    except AdbError:
        return False
    return any(
        line.startswith(serial) and line.endswith("device")
        for line in devices.splitlines()
    )


def _shell(serial: str, *args: str, timeout: int = ADB_TIMEOUT) -> str:
    return _run("-s", serial, "shell", *args, timeout=timeout)


def device_name(serial: str) -> str:
    model = _shell(serial, "getprop", "ro.product.model")
    return model or serial


def list_packages(serial: str, third_party_only: bool = True) -> list[str]:
    flag = "-3" if third_party_only else ""
    args = ["pm", "list", "packages"] + ([flag] if flag else [])
    output = _shell(serial, *args)
    return sorted(
        line.removeprefix("package:").strip()
        for line in output.splitlines()
        if line.startswith("package:")
    )


def find_package(serial: str, name: str) -> str | None:
    """Resolve a friendly app name to an installed package, or None if not found/not installed."""
    query = name.strip().casefold()
    if not query:
        return None
    known = KNOWN_TV_APPS.get(query)
    installed = set(list_packages(serial, third_party_only=False))
    if known and known in installed:
        return known
    matches = [pkg for pkg in installed if query in pkg.casefold()]
    if len(matches) == 1:
        return matches[0]
    return None


def launch_app(serial: str, package: str) -> None:
    _shell(
        serial, "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1",
        timeout=20,
    )


def install_apk(serial: str, apk_path: str) -> str:
    path = Path(apk_path).expanduser()
    if not path.is_file() or path.suffix.lower() != ".apk":
        raise AdbError(f"{apk_path} is not an .apk file.")
    return _run("-s", serial, "install", "-r", str(path), timeout=120)


def uninstall_app(serial: str, package: str) -> str:
    return _run("-s", serial, "uninstall", package, timeout=60)


def send_key(serial: str, key: str) -> None:
    code = KEY_EVENTS.get(key.strip().lower())
    if code is None:
        raise AdbError(f"Unknown remote key {key!r}. Known keys: {', '.join(sorted(KEY_EVENTS))}.")
    _shell(serial, "input", "keyevent", str(code))


def screenshot(serial: str, destination: str) -> Path:
    dest = Path(destination).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [_adb_path(), "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=20,
        )
    except subprocess.TimeoutExpired as error:
        raise AdbError("adb screenshot timed out.") from error
    if result.returncode != 0 or not result.stdout:
        raise AdbError((result.stderr or b"screencap failed").decode(errors="replace").strip())
    dest.write_bytes(result.stdout)
    return dest


def set_wallpaper(serial: str, image_path: str) -> str:
    """Best-effort: pushes the image and sets it as the home wallpaper via the `cmd wallpaper`
    service. Many Android TV OEM launchers (Samsung/LG, some stock TV UIs) don't support a
    custom home wallpaper at all; this silently has no visible effect on those."""
    path = Path(image_path).expanduser()
    if not path.is_file():
        raise AdbError(f"{image_path} does not exist.")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise AdbError("Wallpaper image must be .png, .jpg, or .jpeg.")
    remote_path = f"/sdcard/Pictures/{path.name}"
    _run("-s", serial, "push", str(path), remote_path, timeout=30)
    try:
        return _shell(serial, "cmd", "wallpaper", "set", "-f", remote_path)
    except AdbError as error:
        raise AdbError(
            f"Pushed the image to the TV, but the TV's launcher rejected setting it as "
            f"wallpaper ({error}). Not all Android TV launchers support a custom wallpaper."
        ) from error
