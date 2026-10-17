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


def press_matching(app_name: str, matches) -> str:
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
                    AS.AXUIElementPerformAction(element, "AXPress")
                    return label
                fallback = fallback or (element, label)
        stack.extend(reversed(list(_attribute(element, "AXChildren") or [])))
    if fallback:
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
    window = _attribute(element, "AXFocusedWindow") or (_attribute(element, "AXWindows") or [None])[0]
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
            value = _attribute(node, "AXValue") or _attribute(node, "AXTitle") or _attribute(node, "AXDescription")
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
