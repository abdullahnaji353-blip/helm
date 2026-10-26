import os
import stat
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

import phone_control as pc


def tiny_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * 3 * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class ParsingTests(unittest.TestCase):
    def test_parse_devices_reads_state_and_model(self):
        output = (
            "* daemon started successfully\n"
            "List of devices attached\n"
            "R58M123 device usb:1-1 product:beyond1 model:SM_G973F device:beyond1 transport_id:1\n"
            "192.168.1.5:5555 unauthorized transport_id:2\n"
            "\n"
        )
        devices = pc.parse_devices(output)
        self.assertEqual(
            devices,
            [
                pc.Device("R58M123", "device", "SM_G973F"),
                pc.Device("192.168.1.5:5555", "unauthorized", ""),
            ],
        )
        self.assertEqual(devices[0].label, "SM G973F")
        self.assertEqual(devices[1].label, "192.168.1.5:5555 (unauthorized)")

    def test_png_size(self):
        self.assertEqual(pc.png_size(tiny_png(3, 7)), (3, 7))

    def test_png_size_rejects_error_text(self):
        with self.assertRaises(pc.AdbError):
            pc.png_size(b"error: device unauthorized.\n")

    def test_parse_wlan_ip(self):
        output = (
            "30: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
            "    inet 192.168.1.42/24 brd 192.168.1.255 scope global wlan0\n"
        )
        self.assertEqual(pc.parse_wlan_ip(output), "192.168.1.42")
        self.assertIsNone(pc.parse_wlan_ip("Device \"wlan0\" does not exist."))


class ViewportTests(unittest.TestCase):
    def test_fit_letterboxes_and_centers(self):
        vp = pc.Viewport.fit(600, 1200, 1080, 2400)
        self.assertEqual((vp.width, vp.height), (540, 1200))
        self.assertEqual((vp.x, vp.y), (30, 0))

    def test_to_device_maps_and_clamps(self):
        vp = pc.Viewport.fit(600, 1200, 1080, 2400)
        self.assertEqual(vp.to_device(30, 0), (0, 0))
        self.assertEqual(vp.to_device(30 + 270, 600), (540, 1200))
        self.assertEqual(vp.to_device(10_000, -50), (1079, 0))

    def test_contains_excludes_letterbox_bars(self):
        vp = pc.Viewport.fit(600, 1200, 1080, 2400)
        self.assertFalse(vp.contains(10, 600))
        self.assertTrue(vp.contains(300, 600))

    def test_landscape_frame(self):
        vp = pc.Viewport.fit(400, 800, 2400, 1080)
        self.assertEqual((vp.width, vp.height), (400, 180))
        self.assertEqual(vp.to_device(200, vp.y + 90), (1200, 540))


class GestureTests(unittest.TestCase):
    def test_classify_press(self):
        self.assertEqual(pc.classify_press((10, 10), (12, 13), 0.1), "tap")
        self.assertEqual(pc.classify_press((10, 10), (12, 13), 0.7), "long_press")
        self.assertEqual(pc.classify_press((10, 10), (10, 60), 0.1), "swipe")
        self.assertEqual(pc.classify_press((10, 10), (10, 60), 2.0), "swipe")

    def test_commands(self):
        self.assertEqual(pc.tap(5, 6), "input tap 5 6")
        self.assertEqual(pc.swipe(1, 2, 3, 4, 0), "input swipe 1 2 3 4 1")
        self.assertEqual(pc.long_press(7, 8), "input swipe 7 8 7 8 800")
        self.assertEqual(pc.keyevent("back"), "input keyevent 4")

    def test_scroll_down_moves_finger_up_and_stays_on_screen(self):
        self.assertEqual(pc.scroll(500, 1200, 2, 2400), "input swipe 500 1200 500 480 250")
        self.assertEqual(pc.scroll(500, 100, 10, 2400), "input swipe 500 100 500 0 250")
        self.assertEqual(pc.scroll(500, 2300, -10, 2400), "input swipe 500 2300 500 2399 250")


class TextTests(unittest.TestCase):
    def test_spaces_and_quotes_are_shell_safe(self):
        self.assertEqual(pc.input_text_command("it's ok"), "input text 'it'\"'\"'s%sok'")
        self.assertEqual(pc.input_text_command("a;rm -rf /"), "input text 'a;rm%s-rf%s/'")

    def test_untypeable_characters_are_dropped(self):
        self.assertEqual(pc.input_text_command("hi \U0001f600"), "input text hi%s")
        self.assertIsNone(pc.input_text_command("\U0001f600"))


class FindAdbTests(unittest.TestCase):
    def test_returns_first_executable_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "nope" / "adb"
            present = Path(temp) / "platform-tools" / "adb"
            present.parent.mkdir()
            present.write_text("")
            present.chmod(present.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(pc.find_adb([missing, present]), str(present))
            self.assertIsNone(pc.find_adb([missing]))


@unittest.skipUnless(os.name == "posix", "fake adb is a POSIX script")
class FakeAdbTests(unittest.TestCase):
    """Runs Adb/InputSender against a stand-in adb that logs what it receives."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log = root / "log.txt"
        self.png = root / "screen.png"
        self.png.write_bytes(tiny_png(4, 9))
        self.adb_path = root / "adb"
        self.adb_path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"log = open({str(self.log)!r}, 'a')\n"
            "args = sys.argv[1:]\n"
            "log.write('ARGS ' + ' '.join(args) + '\\n'); log.flush()\n"
            "if args[-3:] == ['exec-out', 'screencap', '-p']:\n"
            f"    sys.stdout.buffer.write(open({str(self.png)!r}, 'rb').read())\n"
            "elif args[-1:] == ['shell']:\n"
            "    for line in sys.stdin:\n"
            "        log.write('SHELL ' + line); log.flush()\n"
            "elif args[:1] == ['connect']:\n"
            "    print('failed to connect to ' + args[1])\n"
        )
        self.adb_path.chmod(0o755)
        self.addCleanup(self.temp.cleanup)

    def log_lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_screencap_is_scoped_to_serial(self):
        adb = pc.Adb(str(self.adb_path), "ABC")
        self.assertEqual(pc.png_size(adb.screencap()), (4, 9))
        self.assertIn("ARGS -s ABC exec-out screencap -p", self.log_lines())

    def test_connect_failure_raises_even_though_adb_exits_zero(self):
        with self.assertRaisesRegex(pc.AdbError, "failed to connect"):
            pc.Adb(str(self.adb_path)).connect("10.0.0.9:5555")

    def test_input_sender_reuses_one_shell_in_order(self):
        sender = pc.InputSender(pc.Adb(str(self.adb_path), "ABC"))
        sender.send(pc.tap(1, 2))
        sender.send(pc.keyevent("home"))
        sender.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(self.log_lines()) < 3:
            time.sleep(0.05)
        self.assertEqual(
            self.log_lines(),
            ["ARGS -s ABC shell", "SHELL input tap 1 2", "SHELL input keyevent 3"],
        )


if __name__ == "__main__":
    unittest.main()
