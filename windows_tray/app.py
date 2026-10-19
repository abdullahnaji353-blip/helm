"""Entry point: `python -m windows_tray.app`.

Starts a system-tray icon (the Windows analogue of Helm's NSStatusItem menu-
bar icon) with an Open Chat / Quit menu, backed by the Tkinter ChatWindow.
Tkinter's mainloop must run on the main thread, so the tray icon runs in a
background thread instead.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from windows_tray.chat_window import ChatWindow


def _tray_image() -> Image.Image:
    icon_path = Path(__file__).resolve().parent.parent / "Resources" / "Helm.icns"
    if icon_path.exists():
        try:
            return Image.open(icon_path)
        except Exception:
            pass
    # Fallback: a simple sparkle-ish mark, generated so the app needs no bundled .ico.
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, size - 8, size - 8), fill=(37, 99, 235, 255))
    draw.text((22, 20), "H", fill=(255, 255, 255, 255))
    return image


class TrayApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()  # no window of its own; ChatWindow is the visible UI
        self.chat_window = ChatWindow(self.root)
        self.chat_window.withdraw()

        self.icon = pystray.Icon(
            "helm",
            _tray_image(),
            "Helm",
            menu=pystray.Menu(
                pystray.MenuItem("Open Chat", self._open_chat, default=True),
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    def _open_chat(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.chat_window.show)

    def _quit(self, _icon=None, _item=None) -> None:
        self.icon.stop()
        self.root.after(0, self.root.quit)

    def run(self) -> None:
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.root.mainloop()


def main() -> int:
    TrayApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
