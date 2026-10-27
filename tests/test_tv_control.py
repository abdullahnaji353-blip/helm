import subprocess
import unittest
from unittest.mock import patch

import tv_control


def fake_run(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class AdbPathTests(unittest.TestCase):
    def test_honors_override_env_var(self):
        with patch.dict("os.environ", {"HELM_ADB_PATH": "/opt/custom/adb"}):
            self.assertEqual(tv_control._adb_path(), "/opt/custom/adb")


class ConnectTests(unittest.TestCase):
    def test_connect_returns_serial(self):
        with patch("subprocess.run", return_value=fake_run(stdout="connected to 192.168.1.5:5555")):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                serial = tv_control.connect("192.168.1.5")
        self.assertEqual(serial, "192.168.1.5:5555")

    def test_connect_raises_on_failure_text(self):
        with patch("subprocess.run", return_value=fake_run(stdout="failed to connect to '192.168.1.5:5555'")):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                with self.assertRaises(tv_control.AdbError):
                    tv_control.connect("192.168.1.5")

    def test_connect_raises_on_nonzero_exit(self):
        with patch("subprocess.run", return_value=fake_run(returncode=1, stderr="device offline")):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                with self.assertRaises(tv_control.AdbError):
                    tv_control.connect("192.168.1.5")


class PackageLookupTests(unittest.TestCase):
    def test_list_packages_parses_output(self):
        output = "package:com.netflix.ninja\npackage:com.google.android.youtube.tv\n"
        with patch("subprocess.run", return_value=fake_run(stdout=output)):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                packages = tv_control.list_packages("serial")
        self.assertEqual(packages, ["com.google.android.youtube.tv", "com.netflix.ninja"])

    def test_find_package_prefers_known_app(self):
        output = "package:com.netflix.ninja\n"
        with patch("subprocess.run", return_value=fake_run(stdout=output)):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                package = tv_control.find_package("serial", "Netflix")
        self.assertEqual(package, "com.netflix.ninja")

    def test_find_package_returns_none_when_not_installed(self):
        with patch("subprocess.run", return_value=fake_run(stdout="")):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                package = tv_control.find_package("serial", "Netflix")
        self.assertIsNone(package)

    def test_find_package_ambiguous_match_returns_none(self):
        output = "package:com.example.one\npackage:com.example.two\n"
        with patch("subprocess.run", return_value=fake_run(stdout=output)):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                package = tv_control.find_package("serial", "example")
        self.assertIsNone(package)


class LaunchAppTests(unittest.TestCase):
    def test_launch_app_wakes_the_device_first(self):
        # Regression test: a launch intent sent while a screensaver/dream is active gets
        # queued silently behind it instead of coming to the front, unless woken first.
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.launch_app("serial", "com.netflix.ninja")
        calls = [c.args[0] for c in mock_run.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertIn(tv_control._KEYCODE_WAKEUP, calls[0])
        self.assertIn("monkey", calls[1])
        self.assertIn("com.netflix.ninja", calls[1])


class OpenUrlTests(unittest.TestCase):
    def test_open_url_wakes_the_device_first(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.open_url("serial", "https://example.com")
        calls = [c.args[0] for c in mock_run.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertIn(tv_control._KEYCODE_WAKEUP, calls[0])
        self.assertIn("https://example.com", calls[1])


class InstallAndUninstallTests(unittest.TestCase):
    def test_install_apk_rejects_non_apk(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.install_apk("serial", "/tmp/not-an-apk.txt")

    def test_install_apk_rejects_missing_file(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.install_apk("serial", "/tmp/does-not-exist.apk")

    def test_uninstall_app_runs_adb_uninstall(self):
        with patch("subprocess.run", return_value=fake_run(stdout="Success")) as mock_run:
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.uninstall_app("serial", "com.netflix.ninja")
        args = mock_run.call_args[0][0]
        self.assertIn("uninstall", args)
        self.assertIn("com.netflix.ninja", args)


class RemoteKeyTests(unittest.TestCase):
    def test_send_key_rejects_unknown_key(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.send_key("serial", "not-a-real-key")

    def test_send_key_sends_correct_keycode(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.send_key("serial", "home")
        args = mock_run.call_args[0][0]
        self.assertIn("3", args)  # KEYCODE_HOME


class WallpaperTests(unittest.TestCase):
    def test_set_wallpaper_rejects_missing_file(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.set_wallpaper("serial", "/tmp/does-not-exist.png")

    def test_set_wallpaper_rejects_wrong_extension(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "wallpaper.gif"
            bad.write_bytes(b"not really a gif")
            with self.assertRaises(tv_control.AdbError):
                tv_control.set_wallpaper("serial", str(bad))


class SearchTests(unittest.TestCase):
    def test_search_builds_correct_youtube_url(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.search("serial", "YouTube", "Ms Rachel")
        args = mock_run.call_args[0][0]
        joined = " ".join(args)
        self.assertIn("android.intent.action.VIEW", joined)
        self.assertIn("https://www.youtube.com/results?search_query=Ms+Rachel", joined)

    def test_search_rejects_unknown_app(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.search("serial", "SomeRandomApp", "query")

    def test_search_rejects_empty_query(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.search("serial", "YouTube", "   ")


class VolumeTests(unittest.TestCase):
    def test_set_volume_rejects_negative(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.set_volume("serial", -1)

    def test_set_volume_rejects_above_explicit_max(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control.set_volume("serial", 99, max_level=15)

    def test_set_volume_rejects_above_autodetected_max(self):
        with patch("tv_control.get_volume_range", return_value=(0, 15)):
            with self.assertRaises(tv_control.AdbError):
                tv_control.set_volume("serial", 99)

    def test_set_volume_proceeds_when_range_cannot_be_detected(self):
        with (
            patch("tv_control.get_volume_range", return_value=None),
            patch("subprocess.run", return_value=fake_run()) as mock_run,
        ):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.set_volume("serial", 99)
        self.assertIn("media_session", mock_run.call_args[0][0])

    def test_set_volume_sends_correct_command(self):
        with patch("tv_control.get_volume_range", return_value=(0, 100)):
            with patch("subprocess.run", return_value=fake_run()) as mock_run:
                with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                    tv_control.set_volume("serial", 7)
        args = mock_run.call_args[0][0]
        self.assertIn("media_session", args)
        self.assertIn("7", args)

    def test_get_volume_range_parses_min_and_max(self):
        output = "- STREAM_MUSIC:\n   Min: 0\n   Max: 100\n   streamVolume:8\n"
        with patch("subprocess.run", return_value=fake_run(stdout=output)):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                range_ = tv_control.get_volume_range("serial")
        self.assertEqual(range_, (0, 100))

    def test_get_volume_parses_dumpsys_output(self):
        # Real-world dumpsys format: "Current:" lists a device-code -> level map, not the
        # level itself (device code 2 = speaker here) — the actual level is streamVolume.
        output = (
            "- STREAM_MUSIC:\n   Muted: false\n   Max: 100\n   streamVolume:8\n"
            "   Current: 2 (speaker): 8, 4 (headset): 8\n"
        )
        with patch("subprocess.run", return_value=fake_run(stdout=output)):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                level = tv_control.get_volume("serial")
        self.assertEqual(level, 8)

    def test_get_volume_does_not_mistake_device_code_for_level(self):
        # Regression test: an earlier regex matched the "2" in "Current: 2 (speaker): 10"
        # (a device-code field) instead of the real level (10).
        output = "- STREAM_MUSIC:\n   streamVolume:10\n   Current: 2 (speaker): 10\n"
        with patch("subprocess.run", return_value=fake_run(stdout=output)):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                level = tv_control.get_volume("serial")
        self.assertEqual(level, 10)

    def test_get_volume_returns_none_when_unparseable(self):
        with patch("subprocess.run", return_value=fake_run(stdout="nonsense")):
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                level = tv_control.get_volume("serial")
        self.assertIsNone(level)


class PowerTests(unittest.TestCase):
    def test_wake_sends_wakeup_keycode(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.wake("serial")
        self.assertIn("224", mock_run.call_args[0][0])

    def test_sleep_sends_sleep_keycode(self):
        with patch("subprocess.run", return_value=fake_run()) as mock_run:
            with patch.dict("os.environ", {"HELM_ADB_PATH": "adb"}):
                tv_control.sleep_device("serial")
        self.assertIn("223", mock_run.call_args[0][0])


SAMPLE_UI_DUMP = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="YouTube" resource-id="com.android.tv.settings:id/app_label"
        clickable="true" bounds="[100,200][400,300]" />
  <node text="" resource-id="com.android.tv.settings:id/icon"
        clickable="false" bounds="[0,0][50,50]" />
  <node text="Always" resource-id="android:id/button_always"
        clickable="true" bounds="[1200,980][1400,1050]" />
</hierarchy>"""


class UiTreeTests(unittest.TestCase):
    def test_find_elements_matches_by_text(self):
        with patch("tv_control.ui_dump", return_value=SAMPLE_UI_DUMP):
            matches = tv_control.find_elements("serial", "Always")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].resource_id, "android:id/button_always")

    def test_find_elements_is_case_insensitive(self):
        with patch("tv_control.ui_dump", return_value=SAMPLE_UI_DUMP):
            matches = tv_control.find_elements("serial", "always")
        self.assertEqual(len(matches), 1)

    def test_element_center_is_midpoint_of_bounds(self):
        element = tv_control.UiElement(
            text="Always", resource_id="x", bounds=(1200, 980, 1400, 1050), clickable=True,
        )
        self.assertEqual(element.center, (1300, 1015))

    def test_tap_text_taps_the_matched_element_center(self):
        with (
            patch("tv_control.ui_dump", return_value=SAMPLE_UI_DUMP),
            patch("tv_control.tap") as mock_tap,
        ):
            element = tv_control.tap_text("serial", "Always")
        mock_tap.assert_called_once_with("serial", 1300, 1015)
        self.assertEqual(element.resource_id, "android:id/button_always")

    def test_tap_text_raises_when_no_match(self):
        with patch("tv_control.ui_dump", return_value=SAMPLE_UI_DUMP):
            with self.assertRaises(tv_control.AdbError):
                tv_control.tap_text("serial", "NoSuchButton")

    def test_parse_ui_dump_raises_on_invalid_xml(self):
        with self.assertRaises(tv_control.AdbError):
            tv_control._parse_ui_dump("not xml")


if __name__ == "__main__":
    unittest.main()
