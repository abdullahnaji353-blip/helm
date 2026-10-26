"""Phone Mirror: see an Android phone's screen on the desktop and drive it with the mouse.

Run with `python phone_mirror.py`. Needs adb (Android platform-tools) and a
phone with USB debugging on; see the README's "Phone Mirror" section.

Mouse: click = tap, hold = long press, drag = swipe, wheel = scroll,
right-click = Back. Typing goes to the phone. Ctrl (Cmd on macOS) + H/B/S/P
= Home/Back/Recents/Power, + Up/Down = volume, + V = paste the desktop
clipboard into the phone.

Frames come from `adb exec-out screencap -p`, which gives a few frames per
second -- fine for tapping around, not for video or games. If scrcpy is
installed, the Wireless menu can hand the phone to it for smooth 60 fps.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageTk

import phone_control as pc

SETUP_HELP = (
    "No phone connected.\n\n"
    "1. On the phone: Settings > About phone >\n"
    "   tap Build number 7 times.\n"
    "2. Settings > Developer options >\n"
    "   turn on USB debugging.\n"
    "3. Plug the phone in and tap Allow\n"
    "   on the prompt that appears.\n\n"
    "Already set up for Wi-Fi? Use the\n"
    "Wireless menu above."
)
NO_ADB_HELP = (
    "adb (Android platform-tools) not found.\n\n"
    "Download it, unzip it, and either put the\n"
    "platform-tools folder next to this app,\n"
    "in your home or Downloads folder, or on\n"
    "your PATH. This window picks it up\n"
    "automatically.\n\n"
    "Click here to open the download page."
)
UNAUTHORIZED_HELP = (
    "Phone found but not authorized.\n\n"
    "Unlock the phone and tap Allow on the\n"
    "\"Allow USB debugging?\" prompt.\n\n"
    "No prompt? Unplug, replug, or revoke\n"
    "USB debugging authorizations in\n"
    "Developer options and try again."
)
# Narrowest window that still fits the toolbar buttons.
MIN_WIDTH = 400
TEXT_FLUSH_MS = 120
# adb `input` takes a few hundred ms per command, so batch wheel notches.
SCROLL_FLUSH_MS = 300


class PhoneMirror:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.adb_path = pc.find_adb()
        self.devices: list[pc.Device] = []
        self.serial: str | None = None
        self.adb: pc.Adb | None = None
        self.sender: pc.InputSender | None = None

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frame: tuple[Image.Image, pc.Viewport, bytes] | None = None
        self._frame_error: str | None = None
        self._area = (400, 820)
        self._viewport: pc.Viewport | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._last_png: bytes | None = None
        self._frame_times: list[float] = []
        self._sized_for: str | None = None

        self._press: tuple[float, float, float] | None = None
        self._text = ""
        self._text_job: str | None = None
        self._scroll_ticks = 0
        self._scroll_at = (0, 0)
        self._scroll_job: str | None = None

        self._build_ui()
        self._bind_input()
        threading.Thread(target=self._watch_devices, daemon=True).start()
        threading.Thread(target=self._capture_frames, daemon=True).start()
        self.root.after(30, self._show_frame)

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        self.root.title("Phone Mirror")
        self.root.geometry("420x900")
        self.root.minsize(MIN_WIDTH, 400)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        top = ttk.Frame(self.root, padding=(6, 6, 6, 0))
        top.pack(fill="x")
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(top, textvariable=self.device_var, state="readonly", width=16)
        self.device_box.pack(side="left", fill="x", expand=True)
        self.device_box.bind("<<ComboboxSelected>>", self._on_device_picked)

        wireless = ttk.Menubutton(top, text="Wireless")
        menu = tk.Menu(wireless, tearoff=False)
        menu.add_command(label="Switch USB phone to Wi-Fi", command=self._go_wireless)
        menu.add_command(label="Connect by IP address...", command=self._connect_ip)
        menu.add_command(label="Pair new phone (Android 11+)...", command=self._pair)
        if shutil.which("scrcpy"):
            menu.add_separator()
            menu.add_command(label="Open in scrcpy (smooth 60 fps)", command=self._open_scrcpy)
        wireless["menu"] = menu
        wireless.pack(side="left", padx=(6, 0))

        self.canvas = tk.Canvas(self.root, bg="#111", highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.canvas.bind("<Configure>", self._on_resize)
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._help_item = self.canvas.create_text(
            0, 0, fill="#ddd", font=("TkDefaultFont", 11), justify="center"
        )
        self.canvas.tag_bind(self._help_item, "<Button-1>", self._on_help_click)

        buttons = ttk.Frame(self.root, padding=(6, 0, 6, 0))
        buttons.pack(fill="x")
        for label, action in (
            ("◀ Back", lambda: self._key("back")),
            ("● Home", lambda: self._key("home")),
            ("■ Recents", lambda: self._key("recents")),
        ):
            ttk.Button(buttons, text=label, command=action).pack(side="left", expand=True, fill="x")

        extras = ttk.Frame(self.root, padding=(6, 2, 6, 0))
        extras.pack(fill="x")
        for label, action in (
            ("Power", lambda: self._key("power")),
            ("Vol −", lambda: self._key("volume_down")),
            ("Vol +", lambda: self._key("volume_up")),
            ("Screenshot", self._save_screenshot),
        ):
            ttk.Button(extras, text=label, command=action).pack(side="left", expand=True, fill="x")

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(self.root, textvariable=self.status_var, padding=(8, 4), anchor="w").pack(fill="x")

    def _bind_input(self) -> None:
        c = self.canvas
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<ButtonRelease-1>", self._on_release)
        # Right-click is Button-3 on Windows/Linux but Button-2 on some macOS Tk
        # builds; either way it means Back.
        c.bind("<Button-2>", lambda e: self._key("back"))
        c.bind("<Button-3>", lambda e: self._key("back"))
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Button-4>", lambda e: self._wheel(e.x, e.y, -1))
        c.bind("<Button-5>", lambda e: self._wheel(e.x, e.y, 1))

        mod = "Command" if self.root.tk.call("tk", "windowingsystem") == "aqua" else "Control"
        for key, name in (("h", "home"), ("b", "back"), ("s", "recents"), ("p", "power")):
            self.root.bind(f"<{mod}-{key}>", lambda e, n=name: self._key(n) or "break")
        self.root.bind(f"<{mod}-Up>", lambda e: self._key("volume_up") or "break")
        self.root.bind(f"<{mod}-Down>", lambda e: self._key("volume_down") or "break")
        self.root.bind(f"<{mod}-v>", self._paste)
        self.root.bind("<Key>", self._on_key)
        self._mod_mask = 0x8 if mod == "Command" else 0x4

    def _status(self, text: str) -> None:
        self.status_var.set(text)

    def _on_resize(self, event: tk.Event) -> None:
        self._area = (max(1, event.width), max(1, event.height))
        self.canvas.coords(self._help_item, event.width // 2, event.height // 2)

    def _on_help_click(self, _event: tk.Event) -> None:
        if not self.adb_path:
            webbrowser.open(pc.ADB_DOWNLOAD_URL)

    def _set_help(self, text: str) -> None:
        self.canvas.itemconfigure(self._help_item, text=text)
        if text:
            self.canvas.itemconfigure(self._image_item, image="")
            self._viewport = None

    # ---- devices ------------------------------------------------------------

    def _watch_devices(self) -> None:
        while not self._stop.is_set():
            if not self.adb_path:
                self.adb_path = pc.find_adb()
            devices: list[pc.Device] = []
            if self.adb_path:
                try:
                    devices = pc.Adb(self.adb_path).devices()
                except pc.AdbError:
                    pass
            self.root.after(0, self._update_devices, devices)
            self._stop.wait(2)

    def _update_devices(self, devices: list[pc.Device]) -> None:
        self.devices = devices
        self.device_box["values"] = [d.label for d in devices]
        current = next((d for d in devices if d.serial == self.serial), None)
        if current is None or current.state != "device":
            ready = next((d for d in devices if d.state == "device"), None)
            if ready:
                self._select(ready)
                return
        if current:
            self.device_var.set(current.label)
        elif devices:
            self.device_var.set(devices[0].label)
        else:
            self.device_var.set("")
        if not self.adb_path:
            self._set_help(NO_ADB_HELP)
            self._status("Waiting for adb...")
        elif not any(d.state == "device" for d in devices):
            self._select(None)
            unauthorized = any(d.state == "unauthorized" for d in devices)
            self._set_help(UNAUTHORIZED_HELP if unauthorized else SETUP_HELP)
            self._status("Waiting for phone...")

    def _on_device_picked(self, _event: tk.Event) -> None:
        index = self.device_box.current()
        if 0 <= index < len(self.devices) and self.devices[index].state == "device":
            self._select(self.devices[index])

    def _select(self, device: pc.Device | None) -> None:
        serial = device.serial if device else None
        if serial == self.serial:
            return
        if self.sender:
            self.sender.close()
            self.sender = None
        with self._lock:
            self.serial = serial
            self.adb = pc.Adb(self.adb_path, serial) if serial else None
            self._frame = None
            self._frame_error = None
        if device:
            self.sender = pc.InputSender(self.adb)
            self.device_var.set(device.label)
            self._set_help("Connecting...")
            self._status(f"Connected to {device.label}")

    # ---- frames -------------------------------------------------------------

    def _capture_frames(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                adb = self.adb
            if adb is None:
                self._stop.wait(0.3)
                continue
            try:
                png = adb.screencap()
                image = Image.open(io.BytesIO(png))
                viewport = pc.Viewport.fit(*self._area, *image.size)
                image = image.convert("RGB").resize(
                    (viewport.width, viewport.height), Image.Resampling.BILINEAR
                )
            except (pc.AdbError, OSError) as error:
                with self._lock:
                    if adb is self.adb:
                        self._frame_error = str(error)
                self._stop.wait(1)
                continue
            with self._lock:
                if adb is self.adb:  # drop frames from a device we switched away from
                    self._frame = (image, viewport, png)
                    self._frame_error = None

    def _show_frame(self) -> None:
        if self._stop.is_set():
            return
        with self._lock:
            frame, self._frame = self._frame, None
            error = self._frame_error
        if frame:
            image, viewport, png = frame
            self._photo = ImageTk.PhotoImage(image)
            self.canvas.itemconfigure(self._image_item, image=self._photo)
            self.canvas.coords(self._image_item, viewport.x, viewport.y)
            self.canvas.itemconfigure(self._help_item, text="")
            self._viewport = viewport
            self._last_png = png
            self._fit_window_once(viewport)
            now = time.monotonic()
            self._frame_times = [t for t in self._frame_times if now - t < 2] + [now]
            fps = (len(self._frame_times) - 1) / max(now - self._frame_times[0], 0.001)
            self._status(
                f"{self.device_var.get()}  ·  {viewport.device_width}×"
                f"{viewport.device_height}  ·  {fps:.1f} fps"
            )
        elif error and self.serial:
            self._status(f"Can't read the screen: {error}")
        self.root.after(15, self._show_frame)

    def _fit_window_once(self, viewport: pc.Viewport) -> None:
        """First frame from a phone: resize the window to the phone's shape."""
        if self._sized_for == self.serial:
            return
        self._sized_for = self.serial
        chrome_h = self.root.winfo_height() - self._area[1]
        chrome_w = self.root.winfo_width() - self._area[0]
        target_h = int(self.root.winfo_screenheight() * 0.8) - chrome_h
        target_w = int(target_h * viewport.device_width / viewport.device_height)
        max_w = int(self.root.winfo_screenwidth() * 0.9) - chrome_w
        if target_w > max_w:  # landscape phone or tablet
            target_w = max_w
            target_h = int(target_w * viewport.device_height / viewport.device_width)
        width = max(target_w + chrome_w, MIN_WIDTH)  # extra width is letterboxed
        self.root.geometry(f"{width}x{target_h + chrome_h}")

    # ---- input --------------------------------------------------------------

    def _send(self, command: str) -> None:
        if self.sender:
            self.sender.send(command)

    def _flush_text(self) -> None:
        self._text_job = None
        if self._text:
            command = pc.input_text_command(self._text)
            if command:
                self._send(command)
            if any(not 32 <= ord(c) < 127 for c in self._text):
                self._status("Emoji and non-English characters can't be typed over adb.")
            self._text = ""

    def _key(self, name: str) -> None:
        self._flush_text()
        self._send(pc.keyevent(name))

    def _on_press(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        if self._viewport and self._viewport.contains(event.x, event.y):
            self._press = (event.x, event.y, time.monotonic())

    def _on_release(self, event: tk.Event) -> None:
        if not self._press or not self._viewport:
            self._press = None
            return
        x0, y0, started = self._press
        self._press = None
        seconds = time.monotonic() - started
        vp = self._viewport
        start = vp.to_device(x0, y0)
        end = vp.to_device(event.x, event.y)
        kind = pc.classify_press((x0, y0), (event.x, event.y), seconds)
        self._flush_text()
        if kind == "tap":
            self._send(pc.tap(*start))
        elif kind == "long_press":
            self._send(pc.long_press(*start))
        else:
            self._send(pc.swipe(*start, *end, min(max(seconds * 1000, 80), 2000)))
        self._ripple(x0, y0)

    def _ripple(self, x: float, y: float) -> None:
        dot = self.canvas.create_oval(x - 9, y - 9, x + 9, y + 9, outline="#4af", width=2)
        self.root.after(180, self.canvas.delete, dot)

    def _on_wheel(self, event: tk.Event) -> None:
        # Windows reports multiples of 120 per notch; macOS reports small ints.
        notches = event.delta / 120 if abs(event.delta) >= 120 else event.delta
        self._wheel(event.x, event.y, -notches)

    def _wheel(self, x: float, y: float, ticks: float) -> None:
        if not self._viewport or not self._viewport.contains(x, y):
            return
        self._scroll_ticks += ticks
        self._scroll_at = self._viewport.to_device(x, y)
        if self._scroll_job is None:
            self._scroll_job = self.root.after(SCROLL_FLUSH_MS, self._flush_scroll)

    def _flush_scroll(self) -> None:
        self._scroll_job = None
        ticks, self._scroll_ticks = self._scroll_ticks, 0
        if ticks and self._viewport:
            self._flush_text()
            self._send(pc.scroll(*self._scroll_at, ticks, self._viewport.device_height))

    def _on_key(self, event: tk.Event) -> str | None:
        special = {
            "Return": "enter",
            "KP_Enter": "enter",
            "BackSpace": "delete",
            "Delete": "forward_delete",
            "Tab": "tab",
            "Escape": "back",
            "Up": "up",
            "Down": "down",
            "Left": "left",
            "Right": "right",
            "Home": "move_home",
            "End": "move_end",
        }
        if event.keysym in special:
            self._key(special[event.keysym])
            return "break"
        # Skip Ctrl/Cmd shortcuts, but let AltGr (Ctrl+Alt on Windows) type.
        alt_gr = bool(event.state & 0x20000) and bool(event.state & 0x4)
        if event.state & self._mod_mask and not alt_gr:
            return None
        if event.char and event.char.isprintable():
            self._type(event.char)
            return "break"
        return None

    def _type(self, text: str) -> None:
        self._text += text
        if self._text_job is not None:
            self.root.after_cancel(self._text_job)
        self._text_job = self.root.after(TEXT_FLUSH_MS, self._flush_text)

    def _paste(self, _event: tk.Event) -> str:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        lines = text.replace("\r\n", "\n").split("\n")
        for i, line in enumerate(lines):
            self._text += line
            self._flush_text()
            if i < len(lines) - 1:
                self._key("enter")
        return "break"

    # ---- extras -------------------------------------------------------------

    def _save_screenshot(self) -> None:
        if not self._last_png:
            self._status("No screen to save yet.")
            return
        folder = Path.home() / "Pictures"
        if not folder.is_dir():
            folder = Path.home()
        path = folder / f"phone-{datetime.now():%Y%m%d-%H%M%S}.png"
        path.write_bytes(self._last_png)
        self._status(f"Saved {path}")

    def _in_background(self, label: str, work) -> None:
        """Run a slow adb call off the UI thread and report the outcome."""
        self._status(label)

        def run() -> None:
            try:
                message = work()
            except pc.AdbError as error:
                self.root.after(0, messagebox.showerror, "Phone Mirror", str(error))
                self.root.after(0, self._status, "")
            else:
                self.root.after(0, self._status, message)

        threading.Thread(target=run, daemon=True).start()

    def _require_adb(self) -> bool:
        if not self.adb_path:
            messagebox.showinfo("Phone Mirror", "Install adb first (see the main window).")
            return False
        return True

    def _go_wireless(self) -> None:
        if not self._require_adb():
            return
        usb = next(
            (d for d in self.devices if d.state == "device" and ":" not in d.serial), None
        )
        if not usb:
            messagebox.showinfo(
                "Phone Mirror",
                "Plug the phone in over USB first, with USB debugging on. "
                "The phone and this computer must be on the same Wi-Fi.",
            )
            return

        def work() -> str:
            address = pc.Adb(self.adb_path, usb.serial).go_wireless()
            return f"Wireless at {address} -- you can unplug the cable now."

        self._in_background("Switching to Wi-Fi...", work)

    def _connect_ip(self) -> None:
        if not self._require_adb():
            return
        address = simpledialog.askstring(
            "Connect by IP",
            "Phone address (from Developer options > Wireless debugging),\n"
            "for example 192.168.1.23:5555",
            parent=self.root,
        )
        if address:
            address = address.strip()
            if ":" not in address:
                address += ":5555"
            self._in_background(
                f"Connecting to {address}...", lambda: pc.Adb(self.adb_path).connect(address)
            )

    def _pair(self) -> None:
        if not self._require_adb():
            return
        address = simpledialog.askstring(
            "Pair phone",
            "On the phone: Developer options > Wireless debugging >\n"
            "Pair device with pairing code.\n\n"
            "IP address & port shown in that popup:",
            parent=self.root,
        )
        if not address:
            return
        code = simpledialog.askstring("Pair phone", "Wi-Fi pairing code:", parent=self.root)
        if not code:
            return

        def work() -> str:
            pc.Adb(self.adb_path).pair(address.strip(), code.strip())
            return (
                "Paired. Now use Wireless > Connect by IP with the address on the "
                "main Wireless debugging screen (it has a different port)."
            )

        self._in_background("Pairing...", work)

    def _open_scrcpy(self) -> None:
        if not self.serial:
            messagebox.showinfo("Phone Mirror", "Connect a phone first.")
            return
        try:
            subprocess.Popen(
                ["scrcpy", "-s", self.serial],
                env={**os.environ, "ADB": self.adb_path or "adb"},
            )
        except OSError as error:
            messagebox.showerror("Phone Mirror", f"Couldn't start scrcpy: {error}")

    def close(self) -> None:
        self._stop.set()
        if self.sender:
            self.sender.close()
        self.root.destroy()


def main() -> None:
    try:  # crisp rendering on high-DPI Windows displays
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    PhoneMirror(root)
    root.mainloop()


if __name__ == "__main__":
    main()
