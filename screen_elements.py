"""Read the frontmost app's clickable controls through macOS Accessibility.

Small vision models are bad at guessing pixel positions, so each screen-control
step lists real controls (role, label, exact centre) and draws their numbers on
the screenshot; the model then clicks by number instead of by coordinate.
"""

from __future__ import annotations

CLICKABLE_ROLES = {
    "AXButton", "AXLink", "AXTextField", "AXTextArea", "AXSearchField", "AXCheckBox",
    "AXRadioButton", "AXPopUpButton", "AXComboBox", "AXMenuButton", "AXMenuItem",
    "AXDisclosureTriangle", "AXSlider", "AXIncrementor", "AXTab", "AXCell",
}
LABEL_ATTRIBUTES = ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXHelp")
SUBROLE_LABELS = {
    "AXCloseButton": "close window", "AXMinimizeButton": "minimize window",
    "AXFullScreenButton": "full screen", "AXZoomButton": "zoom window",
}
MAX_ELEMENTS = 60
MAX_VISITED = 5000


def _attribute(element, name):
    import ApplicationServices as AS

    error, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def _frame(element) -> tuple[float, float, float, float] | None:
    import ApplicationServices as AS

    position, size = _attribute(element, "AXPosition"), _attribute(element, "AXSize")
    if position is None or size is None:
        return None
    ok_point, point = AS.AXValueGetValue(position, AS.kAXValueCGPointType, None)
    ok_size, extent = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_point and ok_size):
        return None
    return point.x, point.y, extent.width, extent.height


def _main_window(app_name: str):
    element = _app_element(app_name)
    if element is None:
        return None, None
    window = (_attribute(element, "AXMainWindow") or _attribute(element, "AXFocusedWindow")
              or (_attribute(element, "AXWindows") or [None])[0])
    return element, window


def _set_frame(window, x: float | None, y: float | None, width: float | None, height: float | None) -> bool:
    import ApplicationServices as AS
    import Quartz

    ok = True
    if x is not None and y is not None:
        point_value = AS.AXValueCreate(AS.kAXValueCGPointType, Quartz.CGPoint(x, y))
        ok = AS.AXUIElementSetAttributeValue(window, "AXPosition", point_value) == 0 and ok
    if width is not None and height is not None:
        size_value = AS.AXValueCreate(AS.kAXValueCGSizeType, Quartz.CGSize(width, height))
        ok = AS.AXUIElementSetAttributeValue(window, "AXSize", size_value) == 0 and ok
    return ok


def screen_bounds() -> tuple[int, int]:
    """Main display size in points (top-left origin), matching Accessibility coordinates."""
    import Quartz

    display = Quartz.CGMainDisplayID()
    return int(Quartz.CGDisplayPixelsWide(display)), int(Quartz.CGDisplayPixelsHigh(display))


SNAP_REGIONS = {
    "left": (0.0, 0.0, 0.5, 1.0), "right": (0.5, 0.0, 0.5, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5), "bottom": (0.0, 0.5, 1.0, 0.5),
    "top-left": (0.0, 0.0, 0.5, 0.5), "top-right": (0.5, 0.0, 0.5, 0.5),
    "bottom-left": (0.0, 0.5, 0.5, 0.5), "bottom-right": (0.5, 0.5, 0.5, 0.5),
    "full": (0.0, 0.0, 1.0, 1.0),
    "center": (0.1, 0.1, 0.8, 0.8),
}


def window_control(app_name: str, action: str, **params) -> str:
    """move/resize/snap/minimize/unminimize/close the app's main window. Returns a status string."""
    import ApplicationServices as AS

    _element, window = _main_window(app_name)
    if window is None:
        return f"{app_name} has no window to control (not running, or no visible window)."
    if action == "minimize":
        ok = AS.AXUIElementSetAttributeValue(window, "AXMinimized", True) == 0
        return f"Minimized {app_name}." if ok else f"Could not minimize {app_name}."
    if action == "unminimize":
        ok = AS.AXUIElementSetAttributeValue(window, "AXMinimized", False) == 0
        return f"Restored {app_name}'s window." if ok else f"Could not restore {app_name}."
    if action == "close":
        button = _attribute(window, "AXCloseButton")
        ok = button is not None and press(button)
        return f"Closed {app_name}'s window." if ok else f"Could not close {app_name}'s window."
    if action == "move":
        ok = _set_frame(window, params.get("x"), params.get("y"), None, None)
        return f"Moved {app_name} to ({params.get('x')}, {params.get('y')})." if ok else f"Could not move {app_name}."
    if action == "resize":
        ok = _set_frame(window, None, None, params.get("width"), params.get("height"))
        return f"Resized {app_name} to {params.get('width')}x{params.get('height')}." if ok else f"Could not resize {app_name}."
    if action == "snap":
        region = SNAP_REGIONS.get(params.get("position", ""))
        if region is None:
            return f"Unknown snap position. Use one of: {', '.join(SNAP_REGIONS)}."
        width, height = screen_bounds()
        fx, fy, fw, fh = region
        ok = _set_frame(window, round(fx * width), round(fy * height), round(fw * width), round(fh * height))
        return f"Snapped {app_name} to {params['position']}." if ok else f"Could not snap {app_name}."
    return f"Unknown window action: {action}."


def frontmost_app_name() -> str:
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return ""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.localizedName() if app else ""


def wait_for_frontmost(app_name: str, timeout: float = 4.0) -> bool:
    """`open -a` can switch Spaces; keys sent mid-animation are lost, so wait for focus."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if frontmost_app_name().lower() == app_name.lower():
            time.sleep(0.5)  # let the Space animation finish
            return True
        time.sleep(0.1)
    return False


def bring_to_front(app_name: str) -> bool:
    """Ask via Accessibility (honoured more often than a plain launch) to focus the app."""
    try:
        import ApplicationServices as AS
        from AppKit import NSWorkspace
    except ImportError:
        return False
    matches = [
        a for a in NSWorkspace.sharedWorkspace().runningApplications()
        if (a.localizedName() or "").lower() == app_name.lower()
    ]
    if not matches:
        return False
    element = AS.AXUIElementCreateApplication(matches[0].processIdentifier())
    AS.AXUIElementSetAttributeValue(element, "AXFrontmost", True)
    error, windows = AS.AXUIElementCopyAttributeValue(element, "AXWindows", None)
    if error == 0 and windows:
        AS.AXUIElementPerformAction(windows[0], "AXRaise")
    return wait_for_frontmost(app_name, timeout=2.0)


def window_title(app_name: str) -> str:
    """Title of the app's frontmost window (Discord shows the open chat there)."""
    try:
        import ApplicationServices as AS
        from AppKit import NSWorkspace
    except ImportError:
        return ""
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if (app.localizedName() or "").lower() == app_name.lower():
            element = AS.AXUIElementCreateApplication(app.processIdentifier())
            window = _attribute(element, "AXFocusedWindow") or (_attribute(element, "AXWindows") or [None])[0]
            return str(_attribute(window, "AXTitle") or "") if window is not None else ""
    return ""


def press_matching(app_name: str, matches, before_press=None) -> str:
    """Press the first control of the app whose label satisfies `matches`; return its label.

    Direct-message links are preferred, so a person's DM wins over a same-named channel.
    """
    try:
        import ApplicationServices as AS
        from AppKit import NSWorkspace
    except ImportError:
        return ""
    apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications()
            if (a.localizedName() or "").lower() == app_name.lower()]
    if not apps:
        return ""
    root = _app_element(app_name)  # also asks Electron apps to expose their content
    stack, visited, fallback = [root], 0, None
    while stack and visited < 40000:  # Discord's tree is huge; DMs far down are deep in it
        element = stack.pop()
        visited += 1
        if _attribute(element, "AXRole") in {"AXLink", "AXButton", "AXCell"}:
            label = str(_attribute(element, "AXTitle") or _attribute(element, "AXDescription") or "")
            if label and matches(label.replace("(direct message)", "")):
                if "direct message" in label.lower():
                    if before_press:
                        before_press(element_center(element), label)
                    AS.AXUIElementPerformAction(element, "AXPress")
                    return label
                fallback = fallback or (element, label)
        stack.extend(reversed(list(_attribute(element, "AXChildren") or [])))
    if fallback:
        if before_press:
            before_press(element_center(fallback[0]), fallback[1])
        AS.AXUIElementPerformAction(fallback[0], "AXPress")
        return fallback[1]
    return ""


def _app_element(app_name: str):
    import ApplicationServices as AS
    from AppKit import NSWorkspace

    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if (app.localizedName() or "").lower() == app_name.lower():
            element = AS.AXUIElementCreateApplication(app.processIdentifier())
            # Chromium/Electron apps (Chrome, Discord, ChatGPT) only expose page content
            # to Accessibility once a client asks for it.
            AS.AXUIElementSetAttributeValue(element, "AXManualAccessibility", True)
            return element
    return None


def read_app(app_name: str, max_chars: int = 6000) -> tuple[str, list[dict[str, str]]]:
    """Visible text and links of an app's front window, in reading order."""
    try:
        element = _app_element(app_name)
    except ImportError:
        return "", []
    if element is None:
        return "", []
    # The main window, not a focused popup (e.g. Chrome's "Restore pages?" bubble).
    window = (_attribute(element, "AXMainWindow") or _attribute(element, "AXFocusedWindow")
              or (_attribute(element, "AXWindows") or [None])[0])
    if window is None:
        return "", []
    texts: list[str] = []
    links: list[dict[str, str]] = []
    size, visited, stack = 0, 0, [window]
    while stack and size < max_chars and visited < 20000:
        node = stack.pop()
        visited += 1
        role = _attribute(node, "AXRole")
        if role == "AXLink":
            url = _attribute(node, "AXURL")
            label = " ".join(str(_attribute(node, "AXTitle") or _attribute(node, "AXDescription") or "").split())
            if url is not None and label:
                links.append({"title": label[:150], "url": str(url)})
        elif role in {"AXStaticText", "AXHeading", "AXTextArea", "AXTextField"}:
            value = (_attribute(node, "AXValue") or _attribute(node, "AXTitle") or _attribute(node, "AXDescription")
                     or _attribute(node, "AXPlaceholderValue"))  # e.g. Discord's "no permission" notice
            text = " ".join(str(value or "").split())
            if text and (not texts or texts[-1] != text):
                texts.append(text)
                size += len(text) + 1
        stack.extend(reversed(list(_attribute(node, "AXChildren") or [])))
    return "\n".join(texts)[:max_chars], links


PRESSABLE_ROLES = {
    "AXButton", "AXLink", "AXMenuItem", "AXMenuButton", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXTab", "AXCell", "AXRow", "AXDisclosureTriangle", "AXStaticText",
}
# Finder icon-view files expose as AXImage, not any of the above — kept separate so
# click_in_app/menu_in_app don't start matching decorative icons in other apps.
DRAG_ROLES = PRESSABLE_ROLES | {"AXImage"}


def _label(element) -> str:
    for name in ("AXTitle", "AXDescription", "AXValue", "AXHelp"):
        value = _attribute(element, name)
        if value not in (None, "") and not isinstance(value, (list, tuple)):
            return " ".join(str(value).split())
    return ""


def find_in_app(app_name: str, matches, roles=PRESSABLE_ROLES):
    """First element (in reading order) of the app's front window whose label matches."""
    element = _app_element(app_name)
    if element is None:
        return None, ""
    window = _attribute(element, "AXFocusedWindow") or (_attribute(element, "AXWindows") or [None])[0]
    stack, visited, loose = [window] if window is not None else [], 0, None
    while stack and visited < 20000:
        node = stack.pop()
        visited += 1
        if _attribute(node, "AXRole") in roles:
            label = _label(node)
            if label and matches(label):
                if _attribute(node, "AXRole") != "AXStaticText":
                    return node, label
                loose = loose or (node, label)  # plain text only if nothing pressable matches
        stack.extend(reversed(list(_attribute(node, "AXChildren") or [])))
    return loose or (None, "")


def element_center(element) -> tuple[int, int] | None:
    """On-screen centre of an element in screen points (what mouse clicks use)."""
    frame = _frame(element)
    if not frame or frame[2] <= 2 or frame[3] <= 2:
        return None
    return int(frame[0] + frame[2] / 2), int(frame[1] + frame[3] / 2)


def press(element) -> bool:
    import ApplicationServices as AS

    return AS.AXUIElementPerformAction(element, "AXPress") == 0


def choose_menu(app_name: str, path: list[str], matches) -> str:
    """Walk the menu bar, e.g. ["File", "New"], pressing each matching item."""
    element = _app_element(app_name)
    if element is None:
        return ""
    current = _attribute(element, "AXMenuBar")
    chosen = []
    for step in path:
        if current is None:
            return ""
        candidates = list(_attribute(current, "AXChildren") or [])
        # Menu bar items hold an AXMenu child whose children are the items.
        expanded = []
        for candidate in candidates:
            if _attribute(candidate, "AXRole") == "AXMenu":
                expanded += list(_attribute(candidate, "AXChildren") or [])
            else:
                expanded.append(candidate)
        hit = next((c for c in expanded if matches(step, _label(c))), None)
        if hit is None:
            return ""
        chosen.append(_label(hit))
        if step is path[-1]:
            press(hit)
        else:
            current = hit
    return " > ".join(chosen)


def focus_field(app_name: str, matches) -> str:
    """Focus the text field whose label/placeholder matches (or the first one)."""
    import ApplicationServices as AS

    roles = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
    field, label = find_in_app(app_name, matches, roles)
    if field is None:
        return ""
    AS.AXUIElementSetAttributeValue(field, "AXFocused", True)
    return label or "text field"


def frontmost_elements(screen_size: tuple[int, int]) -> tuple[str, list[dict]]:
    """Return (app name, controls) for the frontmost app, in reading order.

    Each control is {"id", "role", "label", "x", "y"} with (x, y) its centre in
    screen points, which is what pyautogui clicks use. Empty if Accessibility is
    not granted or the app exposes nothing.
    """
    try:
        import ApplicationServices as AS
        from AppKit import NSWorkspace
    except ImportError:
        return "", []
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None or not AS.AXIsProcessTrusted():
        return (app.localizedName() if app else ""), []
    root = AS.AXUIElementCreateApplication(app.processIdentifier())
    AS.AXUIElementSetMessagingTimeout(root, 1.0)
    width, height = screen_size
    found: list[dict] = []
    seen: set[tuple[int, int]] = set()
    stack = [root]
    visited = 0
    while stack and len(found) < MAX_ELEMENTS and visited < MAX_VISITED:
        element = stack.pop()
        visited += 1
        role = _attribute(element, "AXRole")
        if role == "AXMenuBar":
            continue  # hundreds of menu items; "hotkey" covers menus
        if role in CLICKABLE_ROLES:
            frame = _frame(element)
            if frame and frame[2] > 3 and frame[3] > 3:
                x, y = int(frame[0] + frame[2] / 2), int(frame[1] + frame[3] / 2)
                if 0 <= x < width and 0 <= y < height and (x, y) not in seen:
                    label = next(
                        (str(v) for v in map(lambda a: _attribute(element, a), LABEL_ATTRIBUTES)
                         if v not in (None, "") and not isinstance(v, (list, tuple))),
                        "",
                    ) or SUBROLE_LABELS.get(_attribute(element, "AXSubrole"), "")
                    seen.add((x, y))
                    found.append({
                        "id": len(found) + 1,
                        "role": role[2:],
                        "label": " ".join(label.split())[:40],
                        "x": x,
                        "y": y,
                    })
        children = _attribute(element, "AXChildren") or []
        stack.extend(reversed(list(children)))
    return app.localizedName(), found


def describe_elements(app_name: str, elements: list[dict]) -> str:
    if not elements:
        return f"Frontmost app: {app_name or 'unknown'}. No accessible controls were found; use x/y clicks or the keyboard."
    lines = [f"[{e['id']}] {e['role']} \"{e['label']}\" at ({e['x']},{e['y']})" for e in elements]
    return f"Frontmost app: {app_name}. Clickable controls (numbers match the boxes on the screenshot):\n" + "\n".join(lines)


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


# --- Background input: send keys straight to one app (like cua-driver's background delivery) ---

KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "enter": 36,
    "return": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "tab": 48, "space": 49, "`": 50, "backspace": 51, "delete": 51, "escape": 53, "left": 123,
    "right": 124, "down": 125, "up": 126, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}
MODIFIER_FLAGS = {"command": 1 << 20, "shift": 1 << 17, "option": 1 << 19, "ctrl": 1 << 18}


def app_pid(app_name: str) -> int | None:
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return None
    return next((a.processIdentifier() for a in NSWorkspace.sharedWorkspace().runningApplications()
                 if (a.localizedName() or "").lower() == app_name.lower()), None)


def post_keys(pid: int, keys: list[str]) -> bool:
    """Press a key or shortcut (e.g. ["command", "n"]) in one app without focusing it."""
    import Quartz

    *modifiers, key = [k.lower() for k in keys]
    code = KEY_CODES.get(key)
    if code is None:
        return False
    flags = 0
    for modifier in modifiers:
        flags |= MODIFIER_FLAGS.get(modifier, 0)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPostToPid(pid, event)
    return True


def post_text(pid: int, text: str, interval: float = 0.012) -> None:
    """Type Unicode text into one app without focusing it; keys can't land in any other window."""
    import time

    import Quartz

    for line_number, line in enumerate(text.split("\n")):
        if line_number:
            post_keys(pid, ["enter"])
        for start in range(0, len(line), 4):  # small chunks look like typing
            chunk = line[start:start + 4]
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
                Quartz.CGEventPostToPid(pid, event)
            time.sleep(interval * len(chunk))
