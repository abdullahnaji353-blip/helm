import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from assistant import (
    DesktopAssistant,
    DuckDuckGoResultsParser,
    action_approved,
    simple_open_app_intent,
    search_web,
    test_screen_capture,
    validate_desktop_action,
    validate_ollama_host,
    validate_url,
)
from mcp_client import MCPConnector, MCPError


class ValidateOllamaHostTests(unittest.TestCase):
    def test_allows_localhost(self):
        self.assertEqual(
            validate_ollama_host("http://localhost:11434/"),
            "http://localhost:11434",
        )

    def test_rejects_non_local_hosts(self):
        with self.assertRaises(ValueError):
            validate_ollama_host("https://example.com")

    def test_rejects_non_http_hosts(self):
        with self.assertRaises(ValueError):
            validate_ollama_host("https://localhost:11434")


class ValidateUrlTests(unittest.TestCase):
    def test_accepts_http_and_https_urls(self):
        self.assertTrue(validate_url("https://example.com/path?q=hello"))
        self.assertTrue(validate_url("http://localhost:8080"))

    def test_rejects_incomplete_or_unsafe_urls(self):
        for value in ("", "example.com", "file:///tmp/file", "https://user@example.com"):
            with self.subTest(value=value):
                self.assertFalse(validate_url(value))


class ScreenCaptureTests(unittest.TestCase):
    def test_test_capture_uses_and_closes_screenshot(self):
        screenshot = MagicMock()
        screenshot.size = (1920, 1080)
        with (
            patch("assistant.screen_capture_access_available", return_value=True),
            patch.object(
                DesktopAssistant, "desktop_tools"
            ) as desktop_tools,
        ):
            desktop_tools.return_value.screenshot.return_value = screenshot
            self.assertEqual(test_screen_capture(), (1920, 1080))
        screenshot.close.assert_called_once()

    def test_test_capture_reports_missing_permission(self):
        with (
            patch("assistant.screen_capture_access_available", return_value=False),
            self.assertRaisesRegex(RuntimeError, "Screen Recording access"),
        ):
            test_screen_capture()


class ValidateDesktopActionTests(unittest.TestCase):
    def test_approval_accepts_yes_and_y_only(self):
        self.assertTrue(action_approved("y"))
        self.assertTrue(action_approved(" yes "))
        self.assertFalse(action_approved("no"))
        self.assertFalse(action_approved("yes please"))

    def test_accepts_in_bounds_click(self):
        action = validate_desktop_action(
            {"action": "click", "x": 10, "y": 20, "reason": "Select button"},
            (100, 100),
        )
        self.assertEqual(action["x"], 10)
        self.assertEqual(action["y"], 20)

    def test_rejects_out_of_bounds_and_boolean_coordinates(self):
        for x, y in ((100, 20), (True, 20), (-1, 0)):
            with self.subTest(x=x, y=y):
                with self.assertRaises(ValueError):
                    validate_desktop_action(
                        {"action": "click", "x": x, "y": y, "reason": "Click"},
                        (100, 100),
                    )

    def test_only_allows_safe_keys_and_bounded_text(self):
        with self.assertRaises(ValueError):
            validate_desktop_action(
                {"action": "press", "key": "a", "reason": "Type a key"},
                (100, 100),
            )
        with self.assertRaises(ValueError):
            validate_desktop_action(
                {"action": "type", "text": "x" * 501, "reason": "Type text"},
                (100, 100),
            )

    def test_bounds_scroll_and_wait(self):
        with self.assertRaises(ValueError):
            validate_desktop_action(
                {
                    "action": "scroll",
                    "direction": "down",
                    "amount": 11,
                    "reason": "Scroll",
                },
                (100, 100),
            )
        with self.assertRaises(ValueError):
            validate_desktop_action(
                {"action": "wait", "seconds": 5, "reason": "Wait"},
                (100, 100),
            )


class DesktopTaskModeTests(unittest.TestCase):
    def setUp(self):
        self.screen_access_patch = patch(
            "assistant.screen_capture_access_available", return_value=True
        )
        self.screen_access_patch.start()
        self.addCleanup(self.screen_access_patch.stop)
        self.pyautogui = MagicMock()
        self.pyautogui.size.return_value = (100, 100)
        self.pyautogui.FailSafeException = Exception
        self.actions = [
            json.dumps({
            "action": "click",
            "x": 10,
            "y": 20,
            "reason": "Select the visible button",
            }),
            json.dumps({"action": "done", "message": "Finished."}),
        ]

    def test_autonomous_task_runs_without_prompting(self):
        assistant = DesktopAssistant(autonomous=True)
        output = io.StringIO()
        with (
            patch.object(
            DesktopAssistant, "desktop_tools", return_value=self.pyautogui
            ),
            patch.object(assistant, "vision_chat", side_effect=self.actions),
            patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
            redirect_stdout(output),
        ):
            assistant.desktop_task("Click the visible button")

        self.pyautogui.click.assert_called_once_with(10, 20)
        self.assertIn("Autonomous mode", output.getvalue())

    def test_approval_mode_emits_prompt_marker_and_accepts_yes(self):
        assistant = DesktopAssistant()
        output = io.StringIO()
        with (
            patch.object(
            DesktopAssistant, "desktop_tools", return_value=self.pyautogui
            ),
            patch.object(assistant, "vision_chat", side_effect=self.actions),
            patch("builtins.input", return_value="yes"),
            redirect_stdout(output),
        ):
            assistant.desktop_task("Click the visible button")

        self.pyautogui.click.assert_called_once_with(10, 20)
        self.assertIn("APPROVAL_REQUIRED:", output.getvalue())

    def test_allow_all_response_approves_and_skips_later_prompts(self):
        assistant = DesktopAssistant()
        output = io.StringIO()
        actions = self.actions[:1] + self.actions[:1] + self.actions[1:]
        with (
            patch.object(
                DesktopAssistant, "desktop_tools", return_value=self.pyautogui
            ),
            patch.object(assistant, "vision_chat", side_effect=actions),
            patch("builtins.input", return_value="all") as prompt,
            redirect_stdout(output),
        ):
            assistant.desktop_task("Click the visible button")

        self.assertEqual(self.pyautogui.click.call_count, 2)
        prompt.assert_called_once()
        self.assertIn("Allow All enabled for this task", output.getvalue())

    def test_missing_screen_permission_is_reported_before_capture(self):
        assistant = DesktopAssistant()
        output = io.StringIO()
        with (
            patch(
                "assistant.screen_capture_access_available", return_value=False
            ),
            patch.object(
                DesktopAssistant,
                "desktop_tools",
                side_effect=AssertionError("capture must not start"),
            ),
            redirect_stdout(output),
            self.assertRaisesRegex(RuntimeError, "Screen Recording access"),
        ):
            assistant.desktop_task("Click the visible button")

        self.assertIn("SCREEN_ACCESS_DENIED:", output.getvalue())

    def test_simple_open_app_task_uses_native_launcher_without_vision(self):
        assistant = DesktopAssistant()
        with (
            patch.object(assistant, "open_app") as open_app,
            patch.object(
                assistant,
                "desktop_tools",
                side_effect=AssertionError("vision automation should not run"),
            ),
        ):
            assistant.desktop_task("Please open Safari.")

        open_app.assert_called_once_with("safari")


class SimpleOpenAppIntentTests(unittest.TestCase):
    def test_recognizes_simple_allowlisted_app_requests(self):
        self.assertEqual(simple_open_app_intent("Open Safari"), "safari")
        self.assertEqual(simple_open_app_intent("Could you launch Notes?"), "notes")

    def test_does_not_capture_tasks_with_extra_actions_or_unknown_apps(self):
        self.assertIsNone(simple_open_app_intent("Open Safari and go to example.com"))
        self.assertIsNone(simple_open_app_intent("Open CapCut"))


class DuckDuckGoResultsParserTests(unittest.TestCase):
    def test_parses_result_title_snippet_and_redirect_url(self):
        parser = DuckDuckGoResultsParser()
        parser.feed(
            '<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fexample.com%2Fguide">Helpful <b>Guide</b></a>'
            '<div class="result__snippet">A useful <b>example</b> snippet.</div>'
        )
        self.assertEqual(
            parser.results,
            [{
                "title": "Helpful Guide",
                "url": "https://example.com/guide",
                "snippet": "A useful example snippet.",
            }],
        )

    def test_search_rejects_empty_query_without_network_access(self):
        with patch("assistant.urlopen") as urlopen:
            with self.assertRaises(ValueError):
                search_web(" ")
        urlopen.assert_not_called()


class MCPConnectorTests(unittest.TestCase):
    SERVER_SCRIPT = """
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-server", "version": "1.0"},
        }
        sys.stdout.write(
            json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "ready"},
            }) + "\\n"
            + json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n"
        )
        sys.stdout.flush()
        continue
    elif method == "tools/list":
        result = {"tools": [{
            "name": "echo",
            "description": "Return the input text.",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}]}
    else:
        result = {}
    if "id" in request:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""

    def test_connect_lists_tools_and_calls_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "servers.json"
            config_path.write_text(
                json.dumps({
                    "mcpServers": {
                        "test": {
                            "command": sys.executable,
                            "args": ["-u", "-c", self.SERVER_SCRIPT],
                        }
                    }
                }),
                encoding="utf-8",
            )
            with MCPConnector(config_path) as connector:
                connected = connector.connect()
                self.assertEqual(connected["errors"], {})
                self.assertEqual(connected["servers"][0]["tools"][0]["name"], "echo")
                result = connector.call_tool("test", "echo", {"text": "hello"})
                self.assertEqual(result["content"][0]["text"], "hello")

    def test_builtin_ping_server_lists_and_returns_pong(self):
        project_root = Path(__file__).resolve().parents[1]
        config = {
            "mcpServers": {
                "local-ping": {
                    "command": sys.executable,
                    "args": ["-u", str(project_root / "mcp_ping_server.py")],
                    "cwd": str(project_root),
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "servers.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with MCPConnector(config_path) as connector:
                connected = connector.connect()
                self.assertEqual(connected["errors"], {})
                self.assertEqual(
                    connected["servers"][0]["tools"][0]["name"],
                    "ping",
                )
                result = connector.call_tool("local-ping", "ping", {})
                self.assertEqual(result["content"][0]["text"], "pong")

    def test_missing_config_is_reported(self):
        with MCPConnector("/path/that/does/not/exist/mcp.json") as connector:
            with self.assertRaisesRegex(MCPError, "MCP config not found"):
                connector.connect()

    def test_unknown_tool_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "servers.json"
            config_path.write_text(
                json.dumps({
                    "mcpServers": {
                        "test": {
                            "command": sys.executable,
                            "args": ["-u", "-c", self.SERVER_SCRIPT],
                        }
                    }
                }),
                encoding="utf-8",
            )
            with MCPConnector(config_path) as connector:
                connector.connect()
                with self.assertRaisesRegex(MCPError, "not advertised"):
                    connector.call_tool("test", "missing", {})


class ChatToolTests(unittest.TestCase):
    def test_chat_runs_requested_tool_then_returns_reply(self):
        assistant = DesktopAssistant()
        replies = [
            {"content": "", "tool_calls": [
                {"function": {"name": "set_volume", "arguments": {"level": 20}}}]},
            {"content": "Volume is at 20%."},
        ]
        with patch.object(assistant, "ollama_chat", side_effect=replies), \
                patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(assistant.chat("volume to 20"), "Volume is at 20%.")
        run.assert_called_once_with(
            ["osascript", "-e", "set volume output volume 20"], check=True
        )
        self.assertEqual(assistant.messages[-2], {"role": "tool", "content": "Volume set to 20%.", "tool_name": "set_volume"})

    def test_tools_reject_bad_arguments_without_running_anything(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            self.assertIn("0 to 100", assistant.run_tool("set_volume", {"level": 500}))
            self.assertIn("not a valid", assistant.run_tool("open_app", {"name": "x; rm -rf /"}))
        run.assert_not_called()

    def test_failed_chat_leaves_history_unchanged(self):
        assistant = DesktopAssistant()
        with patch.object(assistant, "ollama_chat", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                assistant.chat("hello")
        self.assertEqual(len(assistant.messages), 1)


class NewDesktopActionTests(unittest.TestCase):
    def test_open_and_hotkey_actions_are_validated(self):
        size = (1440, 900)
        self.assertEqual(
            validate_desktop_action({"action": "open", "app": "Safari", "reason": "r"}, size)["app"], "Safari"
        )
        self.assertEqual(
            validate_desktop_action({"action": "hotkey", "keys": ["Command", "L"], "reason": "r"}, size)["keys"],
            ["command", "l"],
        )
        for bad in (
            {"action": "open", "app": "Safari; rm -rf ~", "reason": "r"},
            {"action": "hotkey", "keys": ["l"], "reason": "r"},
            {"action": "hotkey", "keys": ["command", "f13"], "reason": "r"},
            {"action": "hotkey", "keys": ["fn", "q"], "reason": "r"},
        ):
            with self.assertRaises(ValueError):
                validate_desktop_action(bad, size)


if __name__ == "__main__":
    unittest.main()
