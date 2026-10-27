import json
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import tv_control
import tv_web_server


class TvWebServerTestCase(unittest.TestCase):
    TOKEN = "test-token-123"

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        temp = Path(self._temp.name)
        self.tv_serial_path = temp / "tv_device.txt"
        self.note_path = temp / "note.txt"
        for target, value in (
            ("tv_web_server.TV_SERIAL_PATH", self.tv_serial_path),
            ("tv_web_server.NOTE_PATH", self.note_path),
            ("tv_web_server.STATIC_DIR", Path(tv_web_server.__file__).resolve().parent / "tv_web_static"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.server = tv_web_server.create_server("127.0.0.1", 0, token=self.TOKEN)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def get(self, path, token=None):
        url = f"{self.base}{path}"
        headers = {"X-Helm-Token": token if token is not None else self.TOKEN}
        return urlopen(Request(url, headers=headers))

    def post(self, path, body=None, token=None):
        url = f"{self.base}{path}"
        data = json.dumps(body or {}).encode("utf-8")
        headers = {"X-Helm-Token": token if token is not None else self.TOKEN, "Content-Type": "application/json"}
        return urlopen(Request(url, data=data, headers=headers, method="POST"))


class StaticFileTests(TvWebServerTestCase):
    def test_index_page_served_without_token(self):
        with self.get("/", token="") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])

    def test_unknown_path_is_404(self):
        with self.assertRaises(HTTPError) as error:
            self.get("/nope", token="")
        self.assertEqual(error.exception.code, 404)


class AuthTests(TvWebServerTestCase):
    def test_status_requires_correct_token(self):
        with self.assertRaises(HTTPError) as error:
            self.get("/api/status", token="wrong")
        self.assertEqual(error.exception.code, 401)

    def test_status_succeeds_with_correct_token(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        with patch("tv_web_server.tv_control.is_connected", return_value=True):
            with patch("tv_web_server.tv_control.device_name", return_value="Living Room TV"):
                with self.get("/api/status") as response:
                    data = json.loads(response.read())
        self.assertTrue(data["connected"])
        self.assertEqual(data["device"], "Living Room TV")

    def test_post_action_requires_correct_token(self):
        with self.assertRaises(HTTPError) as error:
            self.post("/api/key", {"key": "home"}, token="wrong")
        self.assertEqual(error.exception.code, 401)


class StatusTests(TvWebServerTestCase):
    def test_status_reports_disconnected_when_no_tv(self):
        with self.get("/api/status") as response:
            data = json.loads(response.read())
        self.assertFalse(data["connected"])
        self.assertIsNone(data["device"])

    def test_status_includes_known_and_search_apps(self):
        with self.get("/api/status") as response:
            data = json.loads(response.read())
        self.assertIn("netflix", data["apps"])
        self.assertIn("youtube", data["searchApps"])

    def test_status_apps_have_no_duplicate_aliases(self):
        # disney+/disney plus and hbo max/max are the same app in tv_control's known-apps
        # dict; the status endpoint should only surface one name per underlying app.
        with self.get("/api/status") as response:
            data = json.loads(response.read())
        self.assertNotIn("disney plus", data["apps"])
        self.assertIn("disney+", data["apps"])
        self.assertNotIn("max", data["apps"])
        self.assertIn("hbo max", data["apps"])
        self.assertNotIn("settings", data["apps"])


class DedupeDisplayNamesTests(unittest.TestCase):
    def test_keeps_first_alias_per_value(self):
        mapping = {"disney+": "com.disney.disneyplus", "disney plus": "com.disney.disneyplus"}
        self.assertEqual(tv_web_server.dedupe_display_names(mapping), ["disney+"])

    def test_excludes_named_entries(self):
        mapping = {"netflix": "com.netflix.ninja", "settings": "com.android.tv.settings"}
        self.assertEqual(
            tv_web_server.dedupe_display_names(mapping, exclude=frozenset({"settings"})),
            ["netflix"],
        )


class NoteTests(TvWebServerTestCase):
    def test_note_round_trips(self):
        with self.post("/api/note", {"text": "buy milk"}) as response:
            self.assertEqual(response.status, 200)
        with self.get("/api/status") as response:
            data = json.loads(response.read())
        self.assertEqual(data["note"], "buy milk")

    def test_note_is_truncated_to_max_length(self):
        long_text = "x" * 1000
        self.post("/api/note", {"text": long_text})
        self.assertEqual(len(self.note_path.read_text(encoding="utf-8")), tv_web_server.MAX_NOTE_CHARS)


class TvActionTests(TvWebServerTestCase):
    def setUp(self):
        super().setUp()
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        patcher = patch("tv_web_server.tv_control.is_connected", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_key_action_requires_connected_tv(self):
        with patch("tv_web_server.tv_control.is_connected", return_value=False):
            with self.assertRaises(HTTPError) as error:
                self.post("/api/key", {"key": "home"})
            self.assertEqual(error.exception.code, 409)

    def test_key_action_dispatches_to_tv_control(self):
        with patch("tv_web_server.tv_control.send_key") as send_key:
            with self.post("/api/key", {"key": "home"}) as response:
                self.assertEqual(response.status, 200)
        send_key.assert_called_once_with("192.168.1.5:5555", "home")

    def test_open_action_resolves_and_launches_app(self):
        with (
            patch("tv_web_server.tv_control.find_package", return_value="com.netflix.ninja") as find,
            patch("tv_web_server.tv_control.launch_app") as launch,
        ):
            self.post("/api/open", {"name": "Netflix"})
        find.assert_called_once_with("192.168.1.5:5555", "Netflix")
        launch.assert_called_once_with("192.168.1.5:5555", "com.netflix.ninja")

    def test_open_action_404s_when_app_not_found(self):
        with patch("tv_web_server.tv_control.find_package", return_value=None):
            with self.assertRaises(HTTPError) as error:
                self.post("/api/open", {"name": "NoSuchApp"})
            self.assertEqual(error.exception.code, 404)

    def test_search_action_dispatches(self):
        with patch("tv_web_server.tv_control.search") as search:
            self.post("/api/search", {"app": "Netflix", "query": "Bluey"})
        search.assert_called_once_with("192.168.1.5:5555", "Netflix", "Bluey")

    def test_volume_action_dispatches(self):
        with patch("tv_web_server.tv_control.set_volume") as set_volume:
            self.post("/api/volume", {"level": 7})
        set_volume.assert_called_once_with("192.168.1.5:5555", 7)

    def test_wake_and_sleep_dispatch(self):
        with patch("tv_web_server.tv_control.wake") as wake:
            self.post("/api/wake")
        wake.assert_called_once_with("192.168.1.5:5555")
        with patch("tv_web_server.tv_control.sleep_device") as sleep_device:
            self.post("/api/sleep")
        sleep_device.assert_called_once_with("192.168.1.5:5555")

    def test_adb_error_returns_502(self):
        with patch("tv_web_server.tv_control.send_key", side_effect=tv_control.AdbError("boom")):
            with self.assertRaises(HTTPError) as error:
                self.post("/api/key", {"key": "home"})
            self.assertEqual(error.exception.code, 502)


if __name__ == "__main__":
    unittest.main()
