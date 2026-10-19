"""Discover installed and downloaded macOS application bundles."""

from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.parsers.expat import ExpatError


@dataclass(frozen=True)
class Application:
    name: str
    path: Path
    bundle_id: str | None
    source: str


def application_roots() -> list[tuple[Path, str]]:
    home = Path.home()
    return [
        (Path("/Applications"), "installed"),
        (Path("/System/Applications"), "installed"),
        (home / "Applications", "installed"),
        (home / "Downloads", "downloaded"),
    ]


def _application(path: Path, source: str) -> Application:
    metadata: dict = {}
    try:
        with (path / "Contents" / "Info.plist").open("rb") as plist_file:
            loaded = plistlib.load(plist_file)
            if isinstance(loaded, dict):
                metadata = loaded
    except (OSError, plistlib.InvalidFileException, ExpatError, ValueError):
        pass
    name = metadata.get("CFBundleDisplayName") or metadata.get("CFBundleName") or path.stem
    bundle_id = metadata.get("CFBundleIdentifier")
    return Application(
        name=str(name),
        path=path.resolve(),
        bundle_id=str(bundle_id) if bundle_id else None,
        source=source,
    )


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
        for directory, subdirectories, _files in os.walk(root, onerror=onerror, followlinks=False):
            bundle_directories = [
                name for name in subdirectories
                if name.casefold().endswith(".app")
            ]
            for name in bundle_directories:
                path = Path(directory) / name
                found.setdefault(path.resolve(), _application(path, source))
                subdirectories.remove(name)
            subdirectories[:] = [
                name for name in subdirectories
                if not name.startswith(".")
            ]

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
        from AppKit import NSWorkspace
    except ImportError as error:
        raise RuntimeError("Checking running apps requires the macOS AppKit framework.") from error
    return {
        bundle_id
        for app in NSWorkspace.sharedWorkspace().runningApplications()
        if (bundle_id := app.bundleIdentifier())
    }
