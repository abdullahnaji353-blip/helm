"""Mirror and control an Android phone from the desktop over adb.

Everything here is UI-free so it can be unit tested without a display:
finding adb, listing devices, grabbing screenshots, mapping window
coordinates onto the phone's screen, telling taps from swipes, and sending
input. phone_mirror.py is the Tkinter window built on top of it.

Only Android is supported. iOS has no public API for driving an iPhone from
another machine; on macOS 15+ with iOS 18+, Apple's own iPhone Mirroring app
does this natively.
"""

from __future__ import annotations

import os
import platform
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ADB_DOWNLOAD_URL = "https://developer.android.com/tools/releases/platform-tools"

# Android KeyEvent codes (android.view.KeyEvent.KEYCODE_*).
KEYCODES = {
    "home": 3,
    "back": 4,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "tab": 61,
    "enter": 66,
    "delete": 67,
    "escape": 111,
    "forward_delete": 112,
    "move_home": 122,
    "move_end": 123,
    "recents": 187,
}

# A press that moves less than this many window pixels is a tap, not a swipe.
TAP_SLOP = 8
# A tap held at least this long becomes a long press.
LONG_PRESS_SECONDS = 0.5

# Keeps adb from flashing a console window on every call in the --windowed exe.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AdbError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    serial: str
    state: str  # "device" when usable; also "unauthorized", "offline", ...
    model: str = ""

    @property
    def label(self) -> str:
        name = self.model.replace("_", " ") or self.serial
        return name if self.state == "device" else f"{name} ({self.state})"


def _adb_candidates() -> list[Path]:
    exe = "adb.exe" if platform.system() == "Windows" else "adb"
    home = Path.home()
    # In a PyInstaller build __file__ is a temp dir; look next to the .exe instead.
    app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    dirs = [app_dir / "platform-tools"]
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            dirs.append(Path(os.environ[env]) / "platform-tools")
    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Android" / "Sdk" / "platform-tools")
        dirs.append(Path("C:/platform-tools"))
    else:
        dirs += [
            home / "Library" / "Android" / "sdk" / "platform-tools",
            home / "Android" / "Sdk" / "platform-tools",
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
        ]
    dirs += [home / "platform-tools", home / "Downloads" / "platform-tools"]
    return [d / exe for d in dirs]


def find_adb(candidates: list[Path] | None = None) -> str | None:
    """Path to adb: PATH first, then the usual SDK/download locations."""
    if candidates is None:
        found = shutil.which("adb")
        if found:
            return found
        candidates = _adb_candidates()
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def parse_devices(output: str) -> list[Device]:
    """Parse `adb devices -l` output."""
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        fields = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(Device(parts[0], parts[1], fields.get("model", "")))
    return devices


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR chunk."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AdbError("Phone did not return a PNG screenshot.")
    return struct.unpack(">II", data[16:24])


def parse_wlan_ip(output: str) -> str | None:
    """The IPv4 address from `ip -f inet addr show wlan0`."""
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", output)
    return match.group(1) if match else None


@dataclass(frozen=True)
class Viewport:
    """Where the phone image sits inside the window, in window pixels."""

    x: int
    y: int
    width: int
    height: int
    device_width: int
    device_height: int

    @classmethod
    def fit(cls, area_w: int, area_h: int, device_w: int, device_h: int) -> "Viewport":
        scale = min(area_w / device_w, area_h / device_h)
        w = max(1, round(device_w * scale))
        h = max(1, round(device_h * scale))
        return cls((area_w - w) // 2, (area_h - h) // 2, w, h, device_w, device_h)

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def to_device(self, x: float, y: float) -> tuple[int, int]:
        """Window point -> phone pixel, clamped to the screen."""
        dx = (x - self.x) * self.device_width / self.width
        dy = (y - self.y) * self.device_height / self.height
        return (
            min(max(int(dx), 0), self.device_width - 1),
            min(max(int(dy), 0), self.device_height - 1),
        )


def classify_press(start: tuple[float, float], end: tuple[float, float], seconds: float) -> str:
    """"tap", "long_press" or "swipe" for a mouse press from start to end (window px)."""
    moved = abs(end[0] - start[0]) > TAP_SLOP or abs(end[1] - start[1]) > TAP_SLOP
    if moved:
        return "swipe"
    return "long_press" if seconds >= LONG_PRESS_SECONDS else "tap"


def input_text_command(text: str) -> str | None:
    """Shell command that types `text`, or None if nothing typeable is left.

    `input text` only handles ASCII, and treats %s as a space, so non-ASCII
    characters are dropped and spaces are encoded.
    """
    typeable = "".join(c for c in text if 32 <= ord(c) < 127)
    if not typeable:
        return None
    return "input text " + shlex.quote(typeable.replace(" ", "%s"))


class InputSender:
    """Sends `input ...` commands through one long-lived `adb shell`.

    Reusing a shell saves spawning adb on the PC for every tap. Commands run
    in the order sent; a dead shell (phone unplugged) is restarted on the
    next command.
    """

    def __init__(self, adb: "Adb"):
        self._adb = adb
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._process: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def send(self, command: str) -> None:
        self._queue.put(command)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=2)

    def _shell(self) -> subprocess.Popen:
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                self._adb.command("shell"),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        return self._process

    def _run(self) -> None:
        while True:
            command = self._queue.get()
            if command is None:
                break
            for _ in range(2):  # one retry with a fresh shell
                try:
                    stdin = self._shell().stdin
                    stdin.write((command + "\n").encode())
                    stdin.flush()
                    break
                except (OSError, ValueError):
                    self._process = None
        if self._process and self._process.poll() is None:
            try:
                self._process.stdin.close()
                self._process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                self._process.kill()


class Adb:
    def __init__(self, path: str, serial: str | None = None):
        self.path = path
        self.serial = serial

    def command(self, *args: str) -> list[str]:
        base = [self.path] + (["-s", self.serial] if self.serial else [])
        return base + list(args)

    def run(self, *args: str, timeout: float = 10) -> bytes:
        try:
            result = subprocess.run(
                self.command(*args),
                capture_output=True,
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as error:
            raise AdbError(f"adb {' '.join(args)} timed out.") from error
        except OSError as error:
            raise AdbError(f"Could not run adb: {error}") from error
        if result.returncode != 0:
            message = (result.stderr or result.stdout).decode(errors="replace").strip()
            raise AdbError(message or f"adb {' '.join(args)} failed.")
        return result.stdout

    def devices(self) -> list[Device]:
        plain = Adb(self.path)  # listing must not be scoped to one serial
        return parse_devices(plain.run("devices", "-l").decode(errors="replace"))

    def screencap(self) -> bytes:
        # exec-out (not shell) so Windows doesn't mangle the binary PNG.
        data = self.run("exec-out", "screencap", "-p", timeout=15)
        png_size(data)
        return data

    def connect(self, address: str) -> str:
        output = Adb(self.path).run("connect", address).decode(errors="replace").strip()
        # adb exits 0 even when the connection fails.
        if "connected to" not in output:
            raise AdbError(output or f"Could not connect to {address}.")
        return output

    def pair(self, address: str, code: str) -> str:
        output = Adb(self.path).run("pair", address, code, timeout=30).decode(errors="replace").strip()
        if "Successfully paired" not in output:
            raise AdbError(output or f"Could not pair with {address}.")
        return output

    def go_wireless(self, port: int = 5555) -> str:
        """Switch the USB-connected phone to Wi-Fi adb and connect to it."""
        ip = parse_wlan_ip(self.run("shell", "ip", "-f", "inet", "addr", "show", "wlan0").decode())
        if not ip:
            raise AdbError("Couldn't find the phone's Wi-Fi address. Is it on Wi-Fi?")
        self.run("tcpip", str(port))
        address = f"{ip}:{port}"
        for _ in range(5):  # adbd restarts after tcpip; give it a moment
            try:
                self.connect(address)
                return address
            except AdbError:
                time.sleep(1)
        raise AdbError(f"Phone switched to Wi-Fi but {address} didn't answer.")


def tap(x: int, y: int) -> str:
    return f"input tap {x} {y}"


def swipe(x1: int, y1: int, x2: int, y2: int, ms: int) -> str:
    return f"input swipe {x1} {y1} {x2} {y2} {max(1, int(ms))}"


def long_press(x: int, y: int, ms: int = 800) -> str:
    return swipe(x, y, x, y, ms)


def keyevent(name: str) -> str:
    return f"input keyevent {KEYCODES[name]}"


def scroll(x: int, y: int, ticks: int, device_height: int) -> str:
    """Swipe that scrolls content by `ticks` wheel notches (positive = down)."""
    distance = max(-0.6, min(0.6, ticks * 0.15)) * device_height
    end_y = int(min(max(y - distance, 0), device_height - 1))
    return swipe(x, y, x, end_y, 250)
