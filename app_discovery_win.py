"""Discover installed and downloaded Windows applications.

Windows equivalent of app_discovery.py. There is no macOS-style app bundle with
an Info.plist, so "discovery" here means walking Start Menu shortcuts (.lnk)
and resolving each one to the executable it launches. `Application.bundle_id`
holds that resolved, lowercased executable path (or None if it couldn't be
resolved) and is used the same way app_discovery.py uses a CFBundleIdentifier:
as the stable key that ties an installed app to its running process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Application:
    name: str
    path: Path
    bundle_id: str | None
    source: str


def application_roots() -> list[tuple[Path, str]]:
    home = Path.home()
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    app_data = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    return [
        (program_data / "Microsoft" / "Windows" / "Start Menu" / "Programs", "installed"),
        (app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs", "installed"),
        (home / "Downloads", "downloaded"),
    ]


def _resolve_shortcut(path: Path) -> tuple[Path | None, str | None]:
    """Resolve a .lnk's target executable path. Returns (target_path, icon_path)."""
    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(path))
        target = shortcut.Targetpath
        return (Path(target) if target else None, shortcut.IconLocation or None)
    except Exception:
        return None, None


def _application(path: Path, source: str) -> Application:
    if path.suffix.casefold() == ".lnk":
        target, _icon = _resolve_shortcut(path)
        bundle_id = str(target).casefold() if target else None
        return Application(name=path.stem, path=path.resolve(), bundle_id=bundle_id, source=source)
    # A bare .exe (e.g. found under Downloads) is its own target.
    return Application(name=path.stem, path=path.resolve(), bundle_id=str(path.resolve()).casefold(), source=source)


def discover_applications(
    roots: Iterable[tuple[Path, str]] | None = None,
) -> list[Application]:
    found: dict[Path, Application] = {}
    errors: list[OSError] = []

    def onerror(error: OSError) -> None:
        errors.append(error)

    for root, source in roots if roots is not None else application_roots():
        if not root.exists():
            continue
        for directory, subdirectories, files in os.walk(root, onerror=onerror, followlinks=False):
            subdirectories[:] = [name for name in subdirectories if not name.startswith(".")]
            for name in files:
                suffix = Path(name).suffix.casefold()
                if suffix not in (".lnk", ".exe"):
                    continue
                path = Path(directory) / name
                found.setdefault(path.resolve(), _application(path, source))

    if errors:
        paths = ", ".join(str(error.filename or error) for error in errors[:3])
        raise RuntimeError(f"Could not inspect application folders: {paths}")
    return sorted(found.values(), key=lambda app: (app.name.casefold(), str(app.path).casefold()))


def find_application(name: str, applications: Iterable[Application] | None = None) -> Application | None:
    query = name.strip().casefold()
    if not query:
        return None
    matches = [
        app for app in (applications if applications is not None else discover_applications())
        if query in {app.name.casefold(), app.path.stem.casefold(),
                     app.bundle_id.casefold() if app.bundle_id else ""}
    ]
    if len(matches) == 1:
        return matches[0]
    if matches and all(app.bundle_id and app.bundle_id == matches[0].bundle_id for app in matches):
        installed = [app for app in matches if app.source == "installed"]
        if len(installed) == 1:
            return installed[0]
        preferred = installed or matches
        exact_bundle_name = [app for app in preferred if app.path.stem.casefold() == query]
        if len(exact_bundle_name) == 1:
            return exact_bundle_name[0]
    return None


def running_application_bundle_ids() -> set[str]:
    try:
        import psutil
    except ImportError as error:
        raise RuntimeError("Checking running apps requires the psutil package.") from error
    bundle_ids: set[str] = set()
    for process in psutil.process_iter(["exe"]):
        try:
            exe = process.info.get("exe")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if exe:
            bundle_ids.add(exe.casefold())
    return bundle_ids
