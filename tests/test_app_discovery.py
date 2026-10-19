import plistlib
import tempfile
import unittest
from pathlib import Path

from app_discovery import discover_applications, find_application


def make_app_bundle(root: Path, name: str, bundle_id: str) -> Path:
    path = root / f"{name}.app"
    info = path / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": bundle_id,
    }))
    return path


class ApplicationDiscoveryTests(unittest.TestCase):
    def test_discovers_nested_installed_and_downloaded_bundles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed_root = root / "Applications"
            downloads_root = root / "Downloads"
            installed_path = make_app_bundle(
                installed_root / "Video", "CapCut", "com.example.capcut"
            )
            downloaded_path = make_app_bundle(
                downloads_root, "Aiming.Pro", "com.example.aiming"
            )

            applications = discover_applications([
                (installed_root, "installed"),
                (downloads_root, "downloaded"),
            ])

            self.assertEqual(
                [(app.name, app.path, app.source) for app in applications],
                [
                    ("Aiming.Pro", downloaded_path.resolve(), "downloaded"),
                    ("CapCut", installed_path.resolve(), "installed"),
                ],
            )

    def test_prefers_installed_copy_when_bundle_is_also_in_downloads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = make_app_bundle(root / "Applications", "CapCut", "com.example.capcut")
            make_app_bundle(root / "Downloads", "CapCut", "com.example.capcut")
            applications = discover_applications([
                (root / "Applications", "installed"),
                (root / "Downloads", "downloaded"),
            ])

            self.assertEqual(
                find_application("CapCut", applications).path,
                installed.resolve(),
            )

    def test_does_not_pick_between_different_apps_with_same_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_app_bundle(root / "First", "Editor", "com.example.editor.one")
            make_app_bundle(root / "Second", "Editor", "com.example.editor.two")
            applications = discover_applications([
                (root / "First", "installed"),
                (root / "Second", "installed"),
            ])

            self.assertIsNone(find_application("Editor", applications))

    def test_prefers_exact_bundle_name_for_duplicate_installed_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = make_app_bundle(root / "Applications", "CapCut", "com.example.capcut")
            make_app_bundle(root / "Applications", "CapCut 2", "com.example.capcut")
            applications = discover_applications([(root / "Applications", "installed")])

            self.assertEqual(find_application("CapCut", applications).path, original.resolve())


if __name__ == "__main__":
    unittest.main()
