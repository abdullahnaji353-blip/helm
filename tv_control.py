"""Control an Android TV / Google TV over ADB (Android Debug Bridge).

Requires the TV to have Developer options enabled and either legacy network
ADB debugging (`adb connect host:port`, port usually 5555) or Android 11+
Wireless debugging (`adb pair host:port CODE` once, then `adb connect`).

The `adb` binary is resolved from, in order: the `HELM_ADB_PATH` environment
variable, `vendor/platform-tools/adb` next to this file, or `adb` on PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

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

# Web deep links that open in-app search results on devices where the app handles
# https links (most Android TV apps register as the default handler for their own domain).
# Best-effort: an app update can change or drop this without notice.
SEARCH_URL_TEMPLATES: dict[str, str] = {
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "netflix": "https://www.netflix.com/search?q={query}",
    "prime video": "https://app.primevideo.com/search/?phrase={query}",
    "amazon prime video": "https://app.primevideo.com/search/?phrase={query}",
    "disney+": "https://www.disneyplus.com/search/{query}",
    "disney plus": "https://www.disneyplus.com/search/{query}",
    "spotify": "https://open.spotify.com/search/{query}",
    "plex": "https://app.plex.tv/desktop/#!/search?query={query}",
}

# STREAM_MUSIC, the stream Android TV remotes/volume keys control.
_VOLUME_STREAM = "3"
_KEYCODE_WAKEUP = "224"
_KEYCODE_SLEEP = "223"


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
    # Dismiss any screensaver/dream first: a launch intent sent while one is active gets queued
    # silently behind it instead of coming to the front.
    wake(serial)
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
    # Some devices write diagnostic lines to stdout before the PNG bytes; find the real start.
    png_start = result.stdout.find(b"\x89PNG\r\n")
    if png_start == -1:
        raise AdbError("screencap did not return a PNG image.")
    dest.write_bytes(result.stdout[png_start:])
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


def open_url(serial: str, url: str) -> None:
    """Open a URL with whichever app handles it (usually that app's own TV client). Dismisses
    any active screensaver first, same reasoning as launch_app."""
    wake(serial)
    _shell(serial, "am", "start", "-a", "android.intent.action.VIEW", "-d", url, timeout=20)


def search(serial: str, app: str, query: str) -> str:
    """Open search results for `query` inside `app` via a web deep link. Returns the matched
    app key. Best-effort: depends on the app still handling its own search URL."""
    key = app.strip().casefold()
    template = SEARCH_URL_TEMPLATES.get(key)
    if template is None:
        raise AdbError(
            f"No search deep link is known for {app!r}. Known apps: "
            f"{', '.join(sorted(SEARCH_URL_TEMPLATES))}."
        )
    query = query.strip()
    if not query:
        raise AdbError("A search query is required.")
    open_url(serial, template.format(query=quote_plus(query)))
    return key


_DUMPSYS_VOLUME_PATTERN = re.compile(r"STREAM_MUSIC:?[\s\S]*?streamVolume:\s*(\d+)")
_DUMPSYS_VOLUME_RANGE_PATTERN = re.compile(r"STREAM_MUSIC:?[\s\S]*?Min:\s*(\d+)[\s\S]*?Max:\s*(\d+)")


def get_volume(serial: str) -> int | None:
    """Best-effort read of the current media volume. Returns None if it can't be parsed
    (dumpsys output format varies across OEMs/Android versions)."""
    try:
        output = _shell(serial, "dumpsys", "audio", timeout=10)
    except AdbError:
        return None
    match = _DUMPSYS_VOLUME_PATTERN.search(output)
    return int(match.group(1)) if match else None


def get_volume_range(serial: str) -> tuple[int, int] | None:
    """Best-effort (min, max) volume bounds for this device. Android TV devices vary widely
    here (commonly 0-15, but some OEM audio HALs use 0-100) — don't assume a fixed range."""
    try:
        output = _shell(serial, "dumpsys", "audio", timeout=10)
    except AdbError:
        return None
    match = _DUMPSYS_VOLUME_RANGE_PATTERN.search(output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def set_volume(serial: str, level: int, max_level: int | None = None) -> None:
    """Set the TV's media volume to an exact level. If max_level isn't given, it's read from
    the device (dumpsys audio); if that also fails, the level is sent unvalidated and the
    device itself will reject or clamp an out-of-range value."""
    if level < 0:
        raise AdbError("Volume level can't be negative.")
    if max_level is None:
        detected_range = get_volume_range(serial)
        max_level = detected_range[1] if detected_range else None
    if max_level is not None and level > max_level:
        raise AdbError(f"Volume level must be between 0 and {max_level}.")
    _shell(serial, "cmd", "media_session", "volume", "--stream", _VOLUME_STREAM,
           "--set", str(level), "--show", timeout=10)


def wake(serial: str) -> None:
    """Wake the TV from standby. Many Android TV devices keep Wi-Fi alive in standby for this
    to work; a fully powered-off TV (unplugged or with Wi-Fi disabled) can't be reached."""
    _shell(serial, "input", "keyevent", _KEYCODE_WAKEUP)


def sleep_device(serial: str) -> None:
    """Put the TV into standby (not a full power-off)."""
    _shell(serial, "input", "keyevent", _KEYCODE_SLEEP)


@dataclass(frozen=True)
class UiElement:
    text: str
    resource_id: str
    bounds: tuple[int, int, int, int]
    clickable: bool

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


_BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def ui_dump(serial: str) -> str:
    """Dump the current screen's accessibility tree as XML. Needs the app on screen to expose
    accessibility nodes; some custom-rendered UIs (games, some video players) won't show anything
    useful here, in which case fall back to screenshot() + send_key()/tap()."""
    _shell(serial, "uiautomator", "dump", "/sdcard/window_dump.xml", timeout=20)
    return _shell(serial, "cat", "/sdcard/window_dump.xml", timeout=15)


def _parse_ui_dump(xml_text: str) -> list[UiElement]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise AdbError(f"Could not parse the TV's on-screen layout: {error}") from error
    elements = []
    for node in root.iter("node"):
        match = _BOUNDS_PATTERN.match(node.get("bounds", ""))
        if not match:
            continue
        bounds = (int(match[1]), int(match[2]), int(match[3]), int(match[4]))
        elements.append(UiElement(
            text=node.get("text", ""),
            resource_id=node.get("resource-id", ""),
            bounds=bounds,
            clickable=node.get("clickable") == "true",
        ))
    return elements


def find_elements(serial: str, text: str) -> list[UiElement]:
    """On-screen elements whose visible text contains `text` (case-insensitive)."""
    query = text.strip().casefold()
    return [el for el in _parse_ui_dump(ui_dump(serial)) if query in el.text.casefold()]


def tap(serial: str, x: int, y: int) -> None:
    """Tap a screen coordinate. Some D-pad-only TV apps ignore synthetic touch events."""
    _shell(serial, "input", "tap", str(x), str(y))


def tap_text(serial: str, text: str) -> UiElement:
    """Find the on-screen element containing `text` and tap its center. Raises if none or
    multiple ambiguous (non-clickable-filtered) matches are found."""
    matches = find_elements(serial, text)
    if not matches:
        raise AdbError(f"No on-screen element containing {text!r} was found.")
    if len(matches) > 1:
        clickable = [el for el in matches if el.clickable]
        if len(clickable) == 1:
            matches = clickable
        else:
            raise AdbError(f"Multiple on-screen elements match {text!r}; be more specific.")
    element = matches[0]
    tap(serial, *element.center)
    return element
