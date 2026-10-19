"""Read the foreground app's clickable controls through Windows UI Automation.

Windows port of screen_elements.py. It implements the same public functions
with the same signatures and return shapes, using pywinauto's UIA backend
(instead of the macOS Accessibility API) and pywin32 (instead of AppKit/
Quartz) for window enumeration, focus, and synthetic input. assistant.py
imports whichever of this module or screen_elements.py matches the host OS,
so call sites elsewhere in assistant.py need no changes.

Two differences worth knowing about, inherent to the platforms rather than to
this port:
- "App name" here means a process's executable name (without ".exe"), the
  closest Windows analogue of macOS's unique bundle display name. Two
  processes can share a display name on Windows in a way bundle ids prevent
  on macOS, so matches are exact (case-insensitive) on that name, same as the
  macOS version's exact-equality matching.
- post_keys/post_text use PostMessage to send keys to one window without
  stealing global focus. Classic Win32 controls honor this; many modern
  (Electron/UWP) apps poll real keyboard state via GetAsyncKeyState and
  ignore posted messages, same caveat the macOS version has for some
  Electron apps' Accessibility trees.
"""

from __future__ import annotations

import time

CLICKABLE_CONTROL_TYPES = {
    "Button", "Hyperlink", "Edit", "CheckBox", "RadioButton", "ComboBox",
    "MenuItem", "TabItem", "DataItem", "ListItem", "Slider", "SplitButton",
}
PRESSABLE_CONTROL_TYPES = {
    "Button", "Hyperlink", "MenuItem", "CheckBox", "RadioButton", "ComboBox",
    "TabItem", "DataItem", "ListItem", "SplitButton", "Text",
}
# Windows has no icon-view-only role like Finder's AXImage, but list/data items
# commonly stand in for draggable rows/icons in Explorer-style views.
DRAG_ROLES = PRESSABLE_CONTROL_TYPES | {"Image"}
MAX_ELEMENTS = 60
MAX_VISITED = 5000

SNAP_REGIONS = {
    "left": (0.0, 0.0, 0.5, 1.0), "right": (0.5, 0.0, 0.5, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5), "bottom": (0.0, 0.5, 1.0, 0.5),
    "up": (0.0, 0.0, 1.0, 0.5), "down": (0.0, 0.5, 1.0, 0.5),
    "top-left": (0.0, 0.0, 0.5, 0.5), "top-right": (0.5, 0.0, 0.5, 0.5),
    "bottom-left": (0.0, 0.5, 0.5, 0.5), "bottom-right": (0.5, 0.5, 0.5, 0.5),
    "full": (0.0, 0.0, 1.0, 1.0),
    "center": (0.1, 0.1, 0.8, 0.8),
}


def screen_bounds() -> tuple[int, int]:
    import win32api

    return win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)


def _process_name(pid: int) -> str:
    import psutil

    try:
        name = psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""
    return name[:-4] if name.casefold().endswith(".exe") else name


def _find_hwnd(app_name: str):
    import win32gui
    import win32process

    target = app_name.casefold()
    found = None

    def enum_handler(hwnd, _):
        nonlocal found
        if found is not None or not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if _process_name(pid).casefold() == target:
            found = hwnd

    win32gui.EnumWindows(enum_handler, None)
    return found


def _find_window(app_name: str):
    """A pywinauto UIA window wrapper for the named app's foreground-eligible window."""
    hwnd = _find_hwnd(app_name)
    if hwnd is None:
        return None
    try:
        from pywinauto import Desktop

        return Desktop(backend="uia").window(handle=hwnd)
    except Exception:
        return None


def window_control(app_name: str, action: str, **params) -> str:
    """move/resize/snap/minimize/unminimize/close the app's main window. Returns a status string."""
    import win32con
    import win32gui

    hwnd = _find_hwnd(app_name)
    if hwnd is None:
        return f"{app_name} has no window to control (not running, or no visible window)."
    if action == "minimize":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return f"Minimized {app_name}."
    if action == "unminimize":
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        return f"Restored {app_name}'s window."
    if action == "close":
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return f"Closed {app_name}'s window."
    if action == "move":
        x, y = params.get("x"), params.get("y")
        if x is None or y is None:
            return f"Could not move {app_name}."
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        win32gui.MoveWindow(hwnd, int(x), int(y), right - left, bottom - top, True)
        return f"Moved {app_name} to ({x}, {y})."
    if action == "resize":
        width, height = params.get("width"), params.get("height")
        if width is None or height is None:
            return f"Could not resize {app_name}."
        left, top, _right, _bottom = win32gui.GetWindowRect(hwnd)
        win32gui.MoveWindow(hwnd, left, top, int(width), int(height), True)
        return f"Resized {app_name} to {width}x{height}."
    if action == "snap":
        region = SNAP_REGIONS.get(params.get("position", ""))
        if region is None:
            return f"Unknown snap position. Use one of: {', '.join(SNAP_REGIONS)}."
        width, height = screen_bounds()
        fx, fy, fw, fh = region
        win32gui.MoveWindow(
            hwnd, round(fx * width), round(fy * height), round(fw * width), round(fh * height), True
        )
        return f"Snapped {app_name} to {params['position']}."
    return f"Unknown window action: {action}."


def frontmost_app_name() -> str:
    import win32gui
    import win32process

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return ""
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return _process_name(pid)


def running_app_names() -> list[str]:
    """Names of running Windows apps that currently have a visible top-level window."""
    import win32gui
    import win32process

    pids: set[int] = set()

    def enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pids.add(pid)

    win32gui.EnumWindows(enum_handler, None)
    names = {name for pid in pids if (name := _process_name(pid)) and name.casefold() != "helm"}
    return sorted(names, key=str.casefold)


def wait_for_frontmost(app_name: str, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if frontmost_app_name().casefold() == app_name.casefold():
            time.sleep(0.3)
            return True
        time.sleep(0.1)
    return False


def bring_to_front(app_name: str) -> bool:
    import win32con
    import win32gui

    hwnd = _find_hwnd(app_name)
    if hwnd is None:
        return False
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        return False
    return wait_for_frontmost(app_name, timeout=2.0)


def window_title(app_name: str) -> str:
    import win32gui

    hwnd = _find_hwnd(app_name)
    return win32gui.GetWindowText(hwnd) if hwnd else ""


def _label(element) -> str:
    try:
        return " ".join((element.window_text() or "").split())
    except Exception:
        return ""


def _control_type(element) -> str | None:
    try:
        return element.element_info.control_type
    except Exception:
        return None


def _children(element) -> list:
    try:
        return list(element.children())
    except Exception:
        return []


def press_matching(app_name: str, matches, before_press=None) -> str:
    """Press the first control of the app whose label satisfies `matches`; return its label."""
    window = _find_window(app_name)
    if window is None:
        return ""
    stack, visited, fallback = [window], 0, None
    while stack and visited < 40000:
        node = stack.pop()
        visited += 1
        if _control_type(node) in {"Hyperlink", "Button", "DataItem", "ListItem"}:
            label = _label(node)
            if label and matches(label):
                fallback = fallback or (node, label)
        stack.extend(reversed(_children(node)))
    if fallback:
        node, label = fallback
        if before_press:
            before_press(element_center(node), label)
        press(node)
        return label
    return ""


def read_app(app_name: str, max_chars: int = 6000) -> tuple[str, list[dict[str, str]]]:
    """Visible text and links of an app's foreground window, in reading order."""
    window = _find_window(app_name)
    if window is None:
        return "", []
    texts: list[str] = []
    links: list[dict[str, str]] = []
    text_types = {"Text", "Edit", "Document"}
    size, visited, stack = 0, 0, [window]
    while stack and size < max_chars and visited < 20000:
        node = stack.pop()
        visited += 1
        control_type = _control_type(node)
        if control_type == "Hyperlink":
            label = _label(node)
            if label:
                # UIA has no generic "target URL" attribute the way AXURL does.
                links.append({"title": label[:150], "url": ""})
        elif control_type in text_types:
            text = _label(node)
            if text and (not texts or texts[-1] != text):
                texts.append(text)
                size += len(text) + 1
        stack.extend(reversed(_children(node)))
    return "\n".join(texts)[:max_chars], links


def find_in_app(app_name: str, matches, roles=None):
    """First element (in reading order) of the app's foreground window whose label matches."""
    roles = roles if roles is not None else PRESSABLE_CONTROL_TYPES
    window = _find_window(app_name)
    if window is None:
        return None, ""
    stack, visited, loose = [window], 0, None
    while stack and visited < 20000:
        node = stack.pop()
        visited += 1
        if _control_type(node) in roles:
            label = _label(node)
            if label and matches(label):
                if _control_type(node) != "Text":
                    return node, label
                loose = loose or (node, label)
        stack.extend(reversed(_children(node)))
    return loose or (None, "")


def element_center(element) -> tuple[int, int] | None:
    """On-screen centre of an element in screen pixels (what mouse clicks use)."""
    try:
        rect = element.rectangle()
    except Exception:
        return None
    width, height = rect.width(), rect.height()
    if width <= 2 or height <= 2:
        return None
    return int((rect.left + rect.right) / 2), int((rect.top + rect.bottom) / 2)


def press(element) -> bool:
    try:
        element.set_focus()
    except Exception:
        pass
    try:
        element.invoke()
        return True
    except Exception:
        pass
    try:
        element.click_input()
        return True
    except Exception:
        return False


def choose_menu(app_name: str, path: list[str], matches) -> str:
    """Walk the menu bar, e.g. ["File", "New"], pressing each matching item.

    Best-effort: unlike macOS's single universal menu bar, Windows apps vary
    between a classic MenuBar control, a ribbon, or no exposed menu at all.
    """
    window = _find_window(app_name)
    if window is None:
        return ""
    try:
        bars = window.children(control_type="MenuBar")
        current = bars[0] if bars else None
    except Exception:
        current = None
    chosen: list[str] = []
    for step in path:
        if current is None:
            return ""
        candidates = _children(current)
        hit = next((c for c in candidates if matches(step, _label(c))), None)
        if hit is None:
            return ""
        chosen.append(_label(hit))
        if step is path[-1]:
            press(hit)
        else:
            try:
                hit.invoke()
            except Exception:
                try:
                    hit.click_input()
                except Exception:
                    pass
            children = _children(hit)
            current = children[0] if children else hit
    return " > ".join(chosen)


def focus_field(app_name: str, matches) -> str:
    """Focus the text field whose label/placeholder matches (or the first one)."""
    roles = {"Edit", "ComboBox", "Document"}
    field, label = find_in_app(app_name, matches, roles)
    if field is None:
        return ""
    try:
        field.set_focus()
    except Exception:
        return ""
    return label or "text field"


def frontmost_elements(screen_size: tuple[int, int]) -> tuple[str, list[dict]]:
    """Return (app name, controls) for the foreground app, in reading order.

    Each control is {"id", "role", "label", "x", "y"} with (x, y) its centre in
    screen pixels, which is what pyautogui clicks use.
    """
    import win32gui
    import win32process

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return "", []
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    app_name = _process_name(pid)
    try:
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=hwnd)
    except Exception:
        return app_name, []

    width, height = screen_size
    found: list[dict] = []
    seen: set[tuple[int, int]] = set()
    stack, visited = [window], 0
    while stack and len(found) < MAX_ELEMENTS and visited < MAX_VISITED:
        element = stack.pop()
        visited += 1
        control_type = _control_type(element)
        if control_type in CLICKABLE_CONTROL_TYPES:
            center = element_center(element)
            if center:
                x, y = center
                if 0 <= x < width and 0 <= y < height and (x, y) not in seen:
                    label = _label(element)
                    seen.add((x, y))
                    found.append({
                        "id": len(found) + 1,
                        "role": control_type,
                        "label": label[:40],
                        "x": x,
                        "y": y,
                    })
        stack.extend(reversed(_children(element)))
    return app_name, found


def describe_elements(app_name: str, elements: list[dict]) -> str:
    if not elements:
        return f"Foreground app: {app_name or 'unknown'}. No accessible controls were found; use x/y clicks or the keyboard."
    lines = [f"[{e['id']}] {e['role']} \"{e['label']}\" at ({e['x']},{e['y']})" for e in elements]
    return f"Foreground app: {app_name}. Clickable controls (numbers match the boxes on the screenshot):\n" + "\n".join(lines)


def mark_elements(image, elements: list[dict]):
    """Draw each control's number at its centre (set-of-marks prompting)."""
    from PIL import ImageDraw

    marked = image.convert("RGB")
    draw = ImageDraw.Draw(marked)
    for element in elements:
        x, y, label = element["x"], element["y"], str(element["id"])
        box = (x - 4 - 4 * len(label), y - 8, x + 4 + 4 * len(label), y + 8)
        draw.rectangle(box, fill=(255, 0, 90), outline=(255, 255, 255))
        draw.text((box[0] + 4, y - 6), label, fill=(255, 255, 255))
    return marked


# --- Background input: send keys to one app's window without changing global focus ---


def app_pid(app_name: str) -> int | None:
    import psutil

    target = app_name.casefold()
    for process in psutil.process_iter(["name"]):
        try:
            name = process.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        stem = name[:-4] if name.casefold().endswith(".exe") else name
        if stem.casefold() == target:
            return process.pid
    return None


def _hwnd_for_pid(pid: int):
    import win32gui
    import win32process

    found = None

    def enum_handler(hwnd, _):
        nonlocal found
        if found is not None or not win32gui.IsWindowVisible(hwnd):
            return
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid == pid:
            found = hwnd

    win32gui.EnumWindows(enum_handler, None)
    return found


def post_keys(pid: int, keys: list[str]) -> bool:
    """Press a key or shortcut (e.g. ["ctrl", "n"]) in one app without focusing it."""
    import win32api
    import win32con

    named_vk = {
        "enter": win32con.VK_RETURN, "return": win32con.VK_RETURN, "tab": win32con.VK_TAB,
        "space": win32con.VK_SPACE, "backspace": win32con.VK_BACK, "delete": win32con.VK_DELETE,
        "escape": win32con.VK_ESCAPE, "left": win32con.VK_LEFT, "right": win32con.VK_RIGHT,
        "down": win32con.VK_DOWN, "up": win32con.VK_UP, "home": win32con.VK_HOME,
        "end": win32con.VK_END, "pageup": win32con.VK_PRIOR, "pagedown": win32con.VK_NEXT,
    }
    # macOS's "command" has no literal Windows analogue; "ctrl" is the nearest shortcut modifier.
    modifier_map = {"ctrl": win32con.VK_CONTROL, "command": win32con.VK_CONTROL,
                     "shift": win32con.VK_SHIFT, "option": win32con.VK_MENU, "alt": win32con.VK_MENU}

    hwnd = _hwnd_for_pid(pid)
    if hwnd is None:
        return False
    *modifiers, key = [k.lower() for k in keys]
    vk = named_vk.get(key)
    if vk is None and len(key) == 1:
        scan = win32api.VkKeyScan(key)
        vk = (scan & 0xFF) if scan != -1 else None
    if vk is None:
        return False
    mod_vks = [modifier_map[m] for m in modifiers if m in modifier_map]
    for mod_vk in mod_vks:
        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, mod_vk, 0)
    win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
    win32api.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
    for mod_vk in reversed(mod_vks):
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, mod_vk, 0)
    return True


def post_text(pid: int, text: str, interval: float = 0.012) -> None:
    """Type Unicode text into one app's window without focusing it."""
    import win32api
    import win32con

    hwnd = _hwnd_for_pid(pid)
    if hwnd is None:
        return
    for line_number, line in enumerate(text.split("\n")):
        if line_number:
            post_keys(pid, ["enter"])
        for character in line:
            win32api.PostMessage(hwnd, win32con.WM_CHAR, ord(character), 0)
            time.sleep(interval)
