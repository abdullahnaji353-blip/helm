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


if __name__ == "__main__":
    unittest.main()
