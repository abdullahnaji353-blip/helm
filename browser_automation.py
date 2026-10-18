"""Playwright-based web automation for control_screen.

Replaces screenshot-plus-Accessibility reading for web tasks: Helm drives its
own visible Chromium window and reads the page's accessibility tree (roles and
names) directly, so clicks and typing target named elements instead of pixel
coordinates. Non-browser apps are unaffected and still use screen_elements.py.
"""
from __future__ import annotations

import re

INTERACTIVE_ROLES = {
    "link", "button", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "menuitem", "tab", "switch", "option",
}
MAX_ELEMENTS = 60


class BrowserEngine:
    """Lazily-started, reused Playwright Chromium browser."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None

    def page(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._playwright.chromium.launch(headless=False)
        self._page = self._browser.new_page()
        return self._page

    def close(self) -> None:
        for obj in (self._browser, self._playwright):
            try:
                if obj is not None:
                    obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception:
                pass
        self._playwright = self._browser = self._page = None


def goto(page, url: str) -> None:
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        url = "https://" + url
    page.goto(url, wait_until="domcontentloaded", timeout=15000)


ARIA_LINE = re.compile(r'^\s*-\s*([a-zA-Z][a-zA-Z0-9]*)\s*(?:"((?:[^"\\]|\\.)*)")?')


def snapshot(page) -> tuple[str, list[dict]]:
    """Return (numbered description for the model, deduplicated elements list).

    Playwright's old page.accessibility API was removed; aria_snapshot() is its
    replacement, returning a YAML-like tree of role/name pairs as text.
    """
    try:
        tree_text = page.locator("body").aria_snapshot()
    except Exception:
        tree_text = ""
    seen: set[tuple[str, str]] = set()
    elements: list[dict] = []
    for line in tree_text.splitlines():
        match = ARIA_LINE.match(line)
        if not match:
            continue
        role, name = match.group(1), (match.group(2) or "").strip()
        if role not in INTERACTIVE_ROLES or (not name and role not in {"textbox", "searchbox"}):
            continue
        key = (role, name)
        if key in seen:
            continue
        seen.add(key)
        elements.append({"role": role, "name": name[:80]})
        if len(elements) == MAX_ELEMENTS:
            break
    lines = [f"{i}. {e['role']} \"{e['name']}\"" for i, e in enumerate(elements, 1)]
    body = "\n".join(lines) if lines else "(no labeled interactive elements found)"
    description = f"Page: {page.title()!r} ({page.url})\n{body}"
    return description, elements


def _locator(page, element: dict):
    name = element["name"]
    if name:
        return page.get_by_role(element["role"], name=name, exact=True).first
    return page.get_by_role(element["role"]).first


def click_element(page, element: dict) -> None:
    _locator(page, element).click(timeout=5000)


def type_into_element(page, element: dict, text: str) -> None:
    _locator(page, element).fill(text, timeout=5000)
