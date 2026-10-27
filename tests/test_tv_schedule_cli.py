import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tv_control
import tv_schedule_cli


class ActiveSerialTests(unittest.TestCase):
    def test_raises_when_no_serial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("tv_schedule_cli.TV_SERIAL_PATH", Path(temp) / "missing.txt"):
                with self.assertRaises(tv_control.AdbError):
                    tv_schedule_cli._active_serial()

    def test_raises_when_saved_device_not_connected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tv_device.txt"
            path.write_text("192.168.1.5:5555", encoding="utf-8")
            with (
                patch("tv_schedule_cli.TV_SERIAL_PATH", path),
                patch("tv_schedule_cli.tv_control.is_connected", return_value=False),
            ):
                with self.assertRaises(tv_control.AdbError):
                    tv_schedule_cli._active_serial()

    def test_returns_serial_when_connected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tv_device.txt"
            path.write_text("192.168.1.5:5555", encoding="utf-8")
            with (
                patch("tv_schedule_cli.TV_SERIAL_PATH", path),
                patch("tv_schedule_cli.tv_control.is_connected", return_value=True),
            ):
                self.assertEqual(tv_schedule_cli._active_serial(), "192.168.1.5:5555")


class RunActionTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("tv_schedule_cli._active_serial", return_value="192.168.1.5:5555")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_wake_calls_tv_control_wake(self):
        with patch("tv_schedule_cli.tv_control.wake") as wake:
            tv_schedule_cli.run_action("wake", [])
        wake.assert_called_once_with("192.168.1.5:5555")

    def test_sleep_calls_tv_control_sleep(self):
        with patch("tv_schedule_cli.tv_control.sleep_device") as sleep_device:
            tv_schedule_cli.run_action("sleep", [])
        sleep_device.assert_called_once_with("192.168.1.5:5555")

    def test_open_app_requires_an_argument(self):
        with self.assertRaises(ValueError):
            tv_schedule_cli.run_action("open_app", [])

    def test_open_app_launches_resolved_package(self):
        with (
            patch("tv_schedule_cli.tv_control.find_package", return_value="com.netflix.ninja") as find,
            patch("tv_schedule_cli.tv_control.launch_app") as launch,
        ):
            tv_schedule_cli.run_action("open_app", ["Netflix"])
        find.assert_called_once_with("192.168.1.5:5555", "Netflix")
        launch.assert_called_once_with("192.168.1.5:5555", "com.netflix.ninja")

    def test_open_app_raises_when_app_not_found(self):
        with patch("tv_schedule_cli.tv_control.find_package", return_value=None):
            with self.assertRaises(ValueError):
                tv_schedule_cli.run_action("open_app", ["NoSuchApp"])

    def test_set_volume_parses_integer(self):
        with patch("tv_schedule_cli.tv_control.set_volume") as set_volume:
            tv_schedule_cli.run_action("set_volume", ["7"])
        set_volume.assert_called_once_with("192.168.1.5:5555", 7)

    def test_remote_key_sends_key(self):
        with patch("tv_schedule_cli.tv_control.send_key") as send_key:
            tv_schedule_cli.run_action("remote_key", ["home"])
        send_key.assert_called_once_with("192.168.1.5:5555", "home")

    def test_unknown_action_raises(self):
        with self.assertRaises(ValueError):
            tv_schedule_cli.run_action("explode", [])


class MainTests(unittest.TestCase):
    def test_no_args_returns_usage_error(self):
        self.assertEqual(tv_schedule_cli.main([]), 2)

    def test_run_action_error_returns_exit_code_1(self):
        with patch("tv_schedule_cli.run_action", side_effect=ValueError("boom")):
            self.assertEqual(tv_schedule_cli.main(["wake"]), 1)

    def test_success_returns_exit_code_0(self):
        with patch("tv_schedule_cli.run_action"):
            self.assertEqual(tv_schedule_cli.main(["wake"]), 0)


if __name__ == "__main__":
    unittest.main()
