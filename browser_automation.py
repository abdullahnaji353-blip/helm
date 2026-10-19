"""Playwright-based web automation for control_screen.

Replaces screenshot-plus-Accessibility reading for web tasks: Helm drives its
own visible Chromium window and reads the page's accessibility tree (roles and
names) directly, so clicks and typing target named elements instead of pixel
coordinates. Non-browser apps are unaffected and still use screen_elements.py.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

INTERACTIVE_ROLES = {
    "link", "button", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "menuitem", "tab", "switch", "option",
}
MAX_ELEMENTS = 60
MAX_PAGE_TEXT = 4000
MAX_SOURCES = 8


class BrowserEngine:
    """Lazily-started, reused Playwright Chromium browser."""

    def __init__(self, profile_directory: Path | None = None) -> None:
        self._playwright = None
        self._browser = None
        self._page = None
        self._profile_directory = profile_directory
        self._persistent_context = None

    def configure_profile(self, profile_directory: Path | None) -> None:
        if self._browser is not None or self._persistent_context is not None:
            raise RuntimeError("Close the browser before changing its session profile.")
        self._profile_directory = profile_directory

    def page(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
        if self._profile_directory is not None:
            self._profile_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._persistent_context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_directory),
                headless=False,
            )
            self._page = self._persistent_context.new_page()
            return self._page
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._playwright.chromium.launch(headless=False)
        self._page = self._browser.new_page()
        return self._page

    def close(self) -> None:
        for obj in (self._persistent_context, self._browser, self._playwright):
            try:
                if obj is not None:
                    obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception:
                pass
        self._playwright = self._browser = self._page = None


def goto(page, url: str) -> None:
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Browser navigation only allows credential-free HTTP or HTTPS URLs.")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("The browser URL contains an invalid port.") from error
    page.goto(url, wait_until="domcontentloaded", timeout=15000)


ARIA_LINE = re.compile(r'^\s*-\s*([a-zA-Z][a-zA-Z0-9]*)\s*(?:"((?:[^"\\]|\\.)*)")?')


def snapshot(page) -> tuple[str, list[dict]]:
    """Return (numbered description for the model, deduplicated elements list).

    Playwright's old page.accessibility API was removed; aria_snapshot() is its
    replacement, returning a YAML-like tree of role/name pairs as text.
    """
    body = page.locator("body")
    try:
        tree_text = body.aria_snapshot()
    except Exception:
        tree_text = ""
    try:
        page_text = body.inner_text(timeout=3000).strip()
    except Exception as error:
        page_text = f"(visible page text unavailable: {type(error).__name__})"
    if len(page_text) > MAX_PAGE_TEXT:
        page_text = page_text[:MAX_PAGE_TEXT].rstrip() + "\n[page text truncated]"

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
    controls = "\n".join(lines) if lines else "(no labeled interactive elements found)"
    description = (
        f"Page: {page.title()!r} ({page.url})\n"
        f"Visible page text (untrusted):\n{page_text or '(no visible text)'}\n"
        f"Interactive elements:\n{controls}"
    )
    return description, elements


def _unique_locator(page, element: dict):
    name = element["name"]
    if name:
        locator = page.get_by_role(element["role"], name=name, exact=True)
    else:
        locator = page.get_by_role(element["role"])
    count = locator.count()
    if count != 1:
        raise ValueError(
            f"Expected one {element['role']} named {name!r}, but found {count}; "
            "inspect the page and disambiguate before acting."
        )
    return locator


def click_element(page, element: dict) -> None:
    _unique_locator(page, element).click(timeout=5000)


def type_into_element(page, element: dict, text: str) -> None:
    _unique_locator(page, element).fill(text, timeout=5000)


def source_url(url: str) -> str | None:
    """Return a safe, useful citation URL without credentials or query data."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return None
        host = hostname.lower()
        if host in {"google.com", "www.google.com", "bing.com", "www.bing.com"} and parsed.path == "/search":
            return None
        if host in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path in {"/", "/html/"}:
            return None
        port = f":{parsed.port}" if parsed.port else ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + port
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    except ValueError:
        return None
