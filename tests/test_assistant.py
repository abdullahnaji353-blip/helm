import io
import json
import sys
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from assistant import (
    DesktopAssistant,
    capture_screenshot,
    action_approved,
    calculator_display_value,
    parse_calculation,
    names_match,
    apps_mentioned,
    written_tool_call,
    recall_skills,
    remember_skill,
    redact_sensitive_text,
    redact_sensitive_values,
    requires_confirmation,
    chat_title_matches,
    simple_open_app_intent,
    screen_capture_access_available,
    search_web,
    test_screen_capture,
    validate_desktop_action,
    validate_ollama_host,
    validate_url,
    looks_like_web_task,
    looks_like_action_request,
    condense_history,
    HISTORY_FULL_DETAIL_MESSAGES,
    validate_browser_action,
    browser_url_is_safe,
)
from mcp_client import MCPConnector, MCPError
from providers import openai_turn, parse_json_object, to_anthropic_messages, to_openai_messages
import tv_control


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


class BrowserUrlSafetyTests(unittest.TestCase):
    def test_allows_only_http_urls_without_credentials(self):
        for value in ("https://example.com", "example.com/path", "http://localhost:8080"):
            with self.subTest(value=value):
                self.assertTrue(browser_url_is_safe(value))
        for value in (
            "javascript:alert(1)",
            "file:///Users/me/private.txt",
            "https://user:secret@example.com",
            "https://example.com:invalid",
        ):
            with self.subTest(value=value):
                self.assertFalse(browser_url_is_safe(value))

    def test_browser_goto_rejects_unsafe_schemes_before_navigation(self):
        from browser_automation import goto
        page = MagicMock()
        with self.assertRaises(ValueError):
            goto(page, "file:///Users/me/private.txt")
        page.goto.assert_not_called()


class ScreenCaptureTests(unittest.TestCase):
    def test_native_capture_bridge_supplies_screenshot_to_python(self):
        from PIL import Image

        image = Image.new("RGB", (32, 18), "navy")
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload.getvalue()
        with (
            patch.dict(os.environ, {
                "HELM_CAPTURE_URL": "http://127.0.0.1:4567",
                "HELM_CAPTURE_TOKEN": "test-capability",
            }),
            patch("assistant.urlopen", return_value=response) as open_url,
        ):
            screenshot = capture_screenshot()
        self.assertEqual(screenshot.size, (32, 18))
        self.assertEqual(screenshot.getpixel((0, 0)), (0, 0, 128))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:4567/screenshot")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-capability")
        screenshot.close()

    def test_native_capture_permission_check_reads_helm_status(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"allowed":true,"width":32,"height":18}'
        with (
            patch.dict(os.environ, {
                "HELM_CAPTURE_URL": "http://127.0.0.1:4567",
                "HELM_CAPTURE_TOKEN": "test-capability",
            }),
            patch("assistant.urlopen", return_value=response) as open_url,
        ):
            self.assertTrue(screen_capture_access_available())
        self.assertEqual(open_url.call_args.args[0].full_url, "http://127.0.0.1:4567/status")

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

    def test_sensitive_action_classifier_covers_irreversible_actions(self):
        for action in (
            "send a message",
            "submit a purchase",
            "format this drive",
            "delete this file",
            "change system settings",
        ):
            with self.subTest(action=action):
                self.assertTrue(requires_confirmation(action))
        self.assertFalse(requires_confirmation("read the current time"))

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
        for target, value in (
            ("assistant.frontmost_elements", ("Test", [])), ("assistant.time.sleep", None),
            ("assistant.frontmost_app_name", "Test"),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
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

    def test_autonomous_sensitive_screen_action_still_requires_confirmation(self):
        assistant = DesktopAssistant(autonomous=True)
        action = json.dumps({
            "action": "click",
            "x": 10,
            "y": 20,
            "reason": "Click Buy Now to complete the purchase",
        })
        output = io.StringIO()
        with (
            patch.object(DesktopAssistant, "desktop_tools", return_value=self.pyautogui),
            patch.object(assistant, "vision_chat", return_value=action),
            patch("builtins.input", return_value="no") as prompt,
            redirect_stdout(output),
        ):
            result = assistant.desktop_task("Buy this item")

        self.pyautogui.click.assert_not_called()
        prompt.assert_called_once()
        self.assertIn("declined", result)
        self.assertIn("APPROVAL_REQUIRED:", output.getvalue())

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
        self.assertEqual(simple_open_app_intent("Open discord now"), "discord")

    def test_does_not_capture_tasks_with_extra_actions_or_unknown_apps(self):
        self.assertIsNone(simple_open_app_intent("Open Safari and go to example.com"))
        with patch("assistant.find_application", return_value=None):
            self.assertIsNone(simple_open_app_intent("Open CapCut"))

    def test_recognizes_a_discovered_app_outside_the_builtin_shortlist(self):
        from app_discovery import Application

        app = Application(
            name="CapCut", path=Path("/Applications/CapCut.app"),
            bundle_id="com.example.capcut", source="installed",
        )
        with patch("assistant.find_application", return_value=app):
            self.assertEqual(simple_open_app_intent("Open CapCut"), "CapCut")


class ChatOpenAppDispatchTests(unittest.TestCase):
    def test_simple_app_launch_bypasses_model_and_reports_launcher_result(self):
        assistant = DesktopAssistant()
        output = io.StringIO()
        with patch.object(DesktopAssistant, "open_app", return_value="Opened Discord.") as open_app, \
                patch.object(assistant, "model_turn") as model_turn, \
                redirect_stdout(output):
            reply = assistant.chat("Open discord now")

        open_app.assert_called_once_with("Discord")
        model_turn.assert_not_called()
        self.assertEqual(reply, "Opened Discord.")
        self.assertIn("TOOL: open_app", output.getvalue())
        self.assertEqual(assistant.messages[-1], {"role": "assistant", "content": reply})

    def test_disabled_app_launch_is_not_attempted(self):
        assistant = DesktopAssistant(disabled_tools=frozenset({"open_app"}))
        with patch("assistant.subprocess.run") as run, \
                patch.object(assistant, "model_turn") as model_turn:
            reply = assistant.chat("Open Safari")

        run.assert_not_called()
        model_turn.assert_not_called()
        self.assertIn("Opening apps is disabled", reply)

    def test_failed_app_launch_does_not_claim_success(self):
        assistant = DesktopAssistant()
        from app_discovery import Application
        with patch("assistant.discover_applications", return_value=[
            Application("Discord", Path("/Applications/Discord.app"), "com.discordapp.Discord", "installed")
        ]), patch(
            "assistant.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["open", "-a", "/Applications/Discord.app"]),
        ):
            reply = assistant.chat("Open Discord")

        self.assertIn("failed", reply)
        self.assertNotIn("Opened Discord", reply)


class ApplicationToolTests(unittest.TestCase):
    def setUp(self):
        from app_discovery import Application

        self.app = Application(
            "CapCut", Path("/Applications/CapCut.app"),
            "com.example.capcut", "installed",
        )

    def test_opens_discovered_app_and_verifies_its_bundle_is_running(self):
        assistant = DesktopAssistant()
        with (
            patch("assistant.discover_applications", return_value=[self.app]),
            patch("assistant.subprocess.run") as run,
            patch("assistant.running_application_bundle_ids", return_value={"com.example.capcut"}),
        ):
            result = assistant.run_tool("open_app", {"name": "CapCut"})

        run.assert_called_once_with(
            ["open", "-a", "/Applications/CapCut.app"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result, "Opened CapCut.")

    def test_does_not_claim_launch_success_without_running_process(self):
        assistant = DesktopAssistant()
        with (
            patch("assistant.discover_applications", return_value=[self.app]),
            patch("assistant.subprocess.run"),
            patch("assistant.running_application_bundle_ids", return_value=set()),
            patch("assistant.time.monotonic", side_effect=[0, 6]),
            patch("assistant.time.sleep"),
        ):
            result = assistant.run_tool("open_app", {"name": "CapCut"})

        self.assertIn("did not appear in the running-app list", result)

    def test_lists_installed_and_downloaded_apps_with_location_and_running_state(self):
        from app_discovery import Application

        downloaded = Application(
            "Aiming.Pro", Path("/Users/test/Downloads/Aiming.Pro.app"),
            "com.example.aiming", "downloaded",
        )
        assistant = DesktopAssistant()
        with (
            patch("assistant.discover_applications", return_value=[self.app, downloaded]),
            patch("assistant.running_application_bundle_ids", return_value={"com.example.capcut"}),
            patch("assistant.running_app_names", return_value=[]),
        ):
            result = assistant.run_tool("list_applications", {})

        self.assertIn("CapCut — Installed, running — /Applications/CapCut.app", result)
        self.assertIn(
            "Aiming.Pro — Downloaded, not installed, not running — "
            "/Users/test/Downloads/Aiming.Pro.app",
            result,
        )


class BrowserSearchTests(unittest.TestCase):
    def test_empty_query_opens_nothing(self):
        with patch("assistant.subprocess.run") as run:
            with self.assertRaises(ValueError):
                search_web(" ")
        run.assert_not_called()

    def test_opens_youtube_search_in_chrome_and_keeps_only_videos(self):
        links = json.dumps([
            {"t": "SwiftUI full course for beginners", "u": "https://www.youtube.com/watch?v=abc"},
            {"t": "YouTube home page link here", "u": "https://www.youtube.com/"},
            {"t": "SwiftUI full course for beginners", "u": "https://www.youtube.com/watch?v=abc"},
        ])
        replies = [MagicMock(returncode=0), MagicMock(returncode=0, stdout=links)]
        with patch("assistant.subprocess.run", side_effect=replies) as run, patch("assistant.time.sleep"):
            url, results, _ = search_web("swiftui course", "youtube", "chrome")
        self.assertEqual(run.call_args_list[0][0][0], ["open", "-a", "Google Chrome", url])
        self.assertIn("youtube.com/results?search_query=swiftui+course", url)
        self.assertEqual(
            results,
            [{"title": "SwiftUI full course for beginners", "url": "https://www.youtube.com/watch?v=abc", "snippet": ""}],
        )

    def test_unreadable_page_still_reports_the_open_results(self):
        replies = [MagicMock(returncode=0), MagicMock(returncode=1, stdout="")]
        with patch("assistant.subprocess.run", side_effect=replies), patch("assistant.time.sleep"), \
                patch("assistant.read_app", return_value=("", [])):
            url, results, note = search_web("latte art", "tiktok", "safari")
        self.assertEqual(results, [])
        self.assertIn("Allow JavaScript from Apple Events", note)


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
        with patch.object(assistant, "model_turn", side_effect=replies), \
                patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(assistant.chat("volume to 20"), "Volume is at 20%.")
        run.assert_called_once_with(
            ["osascript", "-e", "set volume output volume 20"], check=True
        )
        tool_message = assistant.messages[-2]
        self.assertEqual(tool_message["content"], "Volume set to 20%.")
        self.assertEqual(tool_message["tool_call_id"], assistant.messages[-3]["tool_calls"][0]["id"])

    def test_tools_reject_bad_arguments_without_running_anything(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            self.assertIn("0 to 100", assistant.run_tool("set_volume", {"level": 500}))
            self.assertIn("not a valid", assistant.run_tool("open_app", {"name": "x; rm -rf /"}))
        run.assert_not_called()

    def test_disabled_tool_cannot_be_called_even_if_the_model_returns_it(self):
        assistant = DesktopAssistant(disabled_tools=frozenset({"open_app"}))
        with patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            result = assistant.run_tool("open_app", {"name": "Safari"})
        self.assertIn("not enabled", result)
        run.assert_not_called()

    def test_failed_chat_leaves_history_unchanged(self):
        assistant = DesktopAssistant()
        with patch.object(assistant, "model_turn", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                assistant.chat("hello")
        self.assertEqual(len(assistant.messages), 1)

    def test_calculation_parser_accepts_supported_operations_and_rejects_extras(self):
        buttons, expected = parse_calculation("3 × 5")
        self.assertEqual(buttons, ["3", "Multiply", "5", "Equals"])
        self.assertEqual(expected, 15)
        _, expected = parse_calculation("3 - 9")
        self.assertEqual(expected, -6)
        _, expected = parse_calculation("7 / 2")
        self.assertEqual(expected, 3.5)
        with self.assertRaisesRegex(ValueError, "Division by zero"):
            parse_calculation("1 / 0")
        with self.assertRaisesRegex(ValueError, "one calculation"):
            parse_calculation("__import__('os').system('id')")

    def test_calculator_tool_clears_enters_and_verifies_actual_display(self):
        assistant = DesktopAssistant(autonomous=True)
        reads = iter([("0", []), ("3×5\n15", [])])
        with patch.object(assistant, "focus_app", return_value="") as focus, \
                patch.object(assistant, "interact", return_value="Pressed button") as interact, \
                patch("assistant.read_app", side_effect=lambda *args, **kwargs: next(reads)), \
                redirect_stdout(io.StringIO()):
            result = assistant.run_tool("calculator", {"expression": "3 × 5"})
        self.assertIn("verified", result)
        self.assertIn("15", result)
        focus.assert_called_once_with("Calculator")
        self.assertEqual(interact.call_count, 5)
        self.assertEqual(interact.call_args_list[0].args[1]["label"], "All Clear")
        self.assertEqual(interact.call_args_list[-1].args[1]["label"], "Equals")

    def test_calculator_tool_does_not_claim_success_if_display_is_wrong(self):
        assistant = DesktopAssistant(autonomous=True)
        reads = iter([("0", []), ("3×5\n14", [])])
        with patch.object(assistant, "focus_app", return_value=""), \
                patch.object(assistant, "interact", return_value="Pressed button"), \
                patch("assistant.read_app", side_effect=lambda *args, **kwargs: next(reads)), \
                redirect_stdout(io.StringIO()):
            result = assistant.run_tool("calculator", {"expression": "3 × 5"})
        self.assertIn("verification failed", result)
        self.assertIn("shows 14", result)

    def test_common_credentials_are_redacted_before_history_is_saved(self):
        assistant = DesktopAssistant()
        assistant.messages.append({
            "role": "user",
            "content": "password is hunter2; token sk-proj-123456789012345678901234",
        })
        assistant.messages.append({
            "role": "assistant",
            "api_key": "provider-secret",
        })
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / "history.json"
            with patch("assistant.CHAT_HISTORY_PATH", history):
                assistant.save_history()
            saved = history.read_text(encoding="utf-8")
        self.assertNotIn("hunter2", saved)
        self.assertNotIn("sk-proj-123456789012345678901234", saved)
        self.assertNotIn("provider-secret", saved)
        self.assertIn("[REDACTED]", saved)

    def test_sensitive_text_redaction_preserves_nonsecret_text(self):
        redacted = redact_sensitive_text("The password is hidden")
        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn("hidden", redacted)
        self.assertEqual(
            redact_sensitive_values({"api_key": "secret-value", "label": "ordinary"}),
            {"api_key": "[REDACTED]", "label": "ordinary"},
        )


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


class ProviderTests(unittest.TestCase):
    HISTORY = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "volume 20 and open notes"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "set_volume", "arguments": {"level": 20}}},
            {"id": "b", "function": {"name": "open_app", "arguments": {"name": "Notes"}}},
        ]},
        {"role": "tool", "content": "ok1", "tool_name": "set_volume", "tool_call_id": "a"},
        {"role": "tool", "content": "ok2", "tool_name": "open_app", "tool_call_id": "b"},
        {"role": "user", "content": "look", "images": ["QUJD"]},
    ]

    def test_anthropic_groups_tool_results_and_converts_images(self):
        system, messages = to_anthropic_messages(self.HISTORY)
        self.assertEqual(system, "sys")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "user"])
        self.assertEqual([b["id"] for b in messages[1]["content"]], ["a", "b"])
        self.assertEqual([b["tool_use_id"] for b in messages[2]["content"]], ["a", "b"])
        self.assertEqual(messages[3]["content"][0]["source"]["data"], "QUJD")

    def test_openai_serializes_arguments_and_images(self):
        messages = to_openai_messages(self.HISTORY)
        self.assertEqual(json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]), {"level": 20})
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "a", "content": "ok1"})
        self.assertTrue(messages[5]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_old_history_without_ids_becomes_plain_text(self):
        old = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"},
               {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "system_status"}}]},
               {"role": "tool", "content": "It is noon."}]
        _, messages = to_anthropic_messages(old)
        self.assertEqual(messages[1], {"role": "assistant", "content": [{"type": "text", "text": "(used system_status)"}]})
        self.assertEqual(messages[2]["content"], "[tool result] It is noon.")

    def test_openai_turn_parses_tool_calls_and_needs_a_key(self):
        with self.assertRaises(RuntimeError):
            openai_turn("openai", "m", "", [{"role": "user", "content": "x"}], None)
        body = json.dumps({"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "open_app", "arguments": json.dumps({"name": "Notes"})}}]}}]})
        response = MagicMock()
        response.__enter__.return_value.read.return_value = body.encode()
        with patch("providers.urlopen", return_value=response) as urlopen:
            result = openai_turn("gemini", "gemini-2.5-flash", "k", [{"role": "user", "content": "x"}], None)
        request = urlopen.call_args[0][0]
        self.assertIn("generativelanguage.googleapis.com", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer k")
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], {"name": "Notes"})

    def test_parse_json_object_tolerates_prose_around_json(self):
        self.assertEqual(parse_json_object('Sure! ```json\n{"action": "done"}\n```'), {"action": "done"})


class ElementClickTests(unittest.TestCase):
    ELEMENTS = [{"id": 1, "role": "Button", "label": "7", "x": 402, "y": 722}]

    def test_click_element_resolves_to_the_controls_centre(self):
        action = validate_desktop_action(
            {"action": "click_element", "id": 1, "reason": "press 7"}, (1440, 900), self.ELEMENTS
        )
        self.assertEqual((action["action"], action["x"], action["y"]), ("click", 402, 722))

    def test_unknown_element_number_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_desktop_action({"action": "click_element", "id": 9, "reason": "r"}, (1440, 900), self.ELEMENTS)
        with self.assertRaises(ValueError):
            validate_desktop_action({"action": "click_element", "id": True, "reason": "r"}, (1440, 900), self.ELEMENTS)


class FocusGuardTests(unittest.TestCase):
    def test_keys_are_not_sent_when_focus_moved_to_another_app(self):
        assistant = DesktopAssistant(autonomous=True)
        fake = MagicMock()
        fake.size.return_value = (1440, 900)
        actions = [
            json.dumps({"action": "type", "text": "hello", "reason": "r"}),
            json.dumps({"action": "done", "message": "stopped"}),
        ]
        with patch("assistant.screen_capture_access_available", return_value=True), \
                patch.object(DesktopAssistant, "desktop_tools", return_value=fake), \
                patch.object(assistant, "vision_chat", side_effect=actions), \
                patch("assistant.frontmost_elements", return_value=("Calculator", [])), \
                patch("assistant.frontmost_app_name", return_value="Code"), \
                patch("assistant.mark_elements", side_effect=lambda image, _: image), \
                redirect_stdout(io.StringIO()) as output:
            assistant.desktop_task("Type hello in Calculator")
        fake.write.assert_not_called()
        self.assertIn("Code is in front now", output.getvalue())


class RepeatGuardTests(unittest.TestCase):
    def test_same_action_three_times_stops_the_task(self):
        assistant = DesktopAssistant(autonomous=True)
        fake = MagicMock()
        fake.size.return_value = (1440, 900)
        scroll = json.dumps({"action": "scroll", "direction": "down", "amount": 3, "reason": "look"})
        with patch("assistant.screen_capture_access_available", return_value=True), \
                patch.object(DesktopAssistant, "desktop_tools", return_value=fake), \
                patch.object(assistant, "vision_chat", side_effect=[scroll] * 5), \
                patch("assistant.frontmost_elements", return_value=("Safari", [])), \
                patch("assistant.frontmost_app_name", return_value="Safari"), \
                patch("assistant.mark_elements", side_effect=lambda image, _: image), \
                patch("assistant.time.sleep"), redirect_stdout(io.StringIO()):
            result = assistant.desktop_task("Scroll forever")
        self.assertIn("kept repeating", result)
        self.assertEqual(fake.scroll.call_count, 2)


class SendMessageTests(unittest.TestCase):
    def test_dm_and_channel_titles_are_never_confused(self):
        self.assertTrue(chat_title_matches("@Tizi aziz - Discord", "tizi aziz", "person"))
        self.assertFalse(chat_title_matches("#tizi-aziz | Fans - Discord", "tizi aziz", "person"))
        self.assertTrue(chat_title_matches("#📊┃polls | KeyMau Esports - Discord", "polls", "channel", "keymau esports"))
        self.assertFalse(chat_title_matches("#polls | Other Server - Discord", "polls", "channel", "keymau esports"))
        self.assertFalse(chat_title_matches("@polls - Discord", "polls", "channel"))

    def test_names_match_tolerates_typos_but_not_other_people(self):
        self.assertTrue(names_match("tiziz aziz", "@Tizi Aziz - Discord"))
        self.assertTrue(names_match("Tizi Aziz", "Discord | @tizi aziz"))
        self.assertFalse(names_match("tizi aziz", "#polls | KeyMau Esports - Discord"))

    def _run(self, title, screen="", call_button=True):
        assistant = DesktopAssistant(autonomous=True)
        fake = MagicMock()
        with patch.object(DesktopAssistant, "desktop_tools", return_value=fake), \
                patch("assistant.read_app", return_value=(screen, [])), \
                patch("assistant.find_in_app", side_effect=lambda app, matches, roles=None:
                      (None, "") if roles else ("button" if call_button else None, "")), \
                patch("assistant.subprocess.run"), patch("assistant.time.sleep"), \
                patch("assistant.wait_for_frontmost", return_value=True), \
                patch("assistant.window_title", return_value=title), \
                patch("assistant.press_matching", return_value=""), \
                patch("assistant.frontmost_app_name", return_value="Discord"), \
                patch("builtins.input", return_value="yes") as prompt, \
                redirect_stdout(io.StringIO()):
            result = assistant.send_message("Discord", "tiziz aziz", "hi")
        return result, fake, prompt

    def test_sends_only_after_the_right_chat_opens(self):
        result, fake, prompt = self._run("@Tizi aziz - Discord")
        self.assertIn("Sent", result)
        prompt.assert_called_once()
        fake.write.assert_called_with("hi", interval=0.03)

    def test_no_permission_channel_sends_nothing(self):
        result, fake, prompt = self._run("@Tizi aziz - Discord", screen="You do not have permission to send messages in this channel.")
        self.assertIn("don't have permission", result)
        prompt.assert_not_called()
        fake.write.assert_not_called()

    def test_flags_bots_by_missing_call_button(self):
        result, _, _ = self._run("@Tizi aziz - Discord", call_button=False)
        self.assertIn("looks like a bot", result)

    def test_wrong_chat_sends_nothing(self):
        result, fake, prompt = self._run("#polls | KeyMau Esports - Discord")
        self.assertIn("Nothing was sent", result)
        prompt.assert_not_called()
        self.assertNotIn(unittest.mock.call("hi", interval=0.03), fake.write.call_args_list)

    def test_autonomous_send_still_requires_confirmation(self):
        assistant = DesktopAssistant(autonomous=True)
        fake = MagicMock()
        with patch.object(DesktopAssistant, "desktop_tools", return_value=fake), \
                patch("assistant.read_app", return_value=("", [])), \
                patch("assistant.find_in_app", return_value=("button", "")), \
                patch("assistant.subprocess.run"), patch("assistant.time.sleep"), \
                patch("assistant.wait_for_frontmost", return_value=True), \
                patch("assistant.window_title", return_value="@Tizi aziz - Discord"), \
                patch("assistant.press_matching", return_value=""), \
                patch("assistant.find_in_app", side_effect=lambda app, matches, roles=None:
                      (None, "") if roles else ("button", "")), \
                patch("assistant.frontmost_app_name", return_value="Discord"), \
                patch("builtins.input", return_value="no"), redirect_stdout(io.StringIO()):
            result = assistant.send_message("Discord", "Tizi aziz", "hi")
        self.assertIn("declined", result)
        fake.write.assert_not_called()


class SkillMemoryTests(unittest.TestCase):
    def test_remembers_and_recalls_similar_tasks_only(self):
        with tempfile.TemporaryDirectory() as folder, patch("assistant.SKILLS_PATH", Path(folder) / "skills.json"):
            remember_skill("open youtube in safari", ["open Safari", "press command+l", "type 'youtube.com'"])
            self.assertIn("press command+l", recall_skills("open youtube.com in Safari please"))
            self.assertEqual(recall_skills("set the volume to 30"), "")

    def test_loads_only_relevant_local_skill_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "skills"
            relevant = root / "web-research" / "SKILL.md"
            irrelevant = root / "video-editing" / "SKILL.md"
            relevant.parent.mkdir(parents=True)
            irrelevant.parent.mkdir()
            relevant.write_text(
                "---\nname: web-research\ndescription: Research topics on the web\n---\n"
                "Compare sources and cite every factual claim.\n",
                encoding="utf-8",
            )
            irrelevant.write_text(
                "---\nname: video-editing\ndescription: Edit a video\n---\nTrim clips.\n",
                encoding="utf-8",
            )
            with patch("assistant.SKILLS_PATH", Path(folder) / "missing.json"), \
                    patch("assistant.CUSTOM_SKILLS_PATH", root):
                matched = recall_skills("research this topic on the web")
                missed = recall_skills("set my volume")
        self.assertIn("cite every factual claim", matched)
        self.assertNotIn("Trim clips", matched)
        self.assertEqual(missed, "")


class PausedTaskTests(unittest.TestCase):
    def test_save_load_round_trip_drops_images(self):
        from assistant import save_paused_task, load_paused_task, clear_paused_task
        with tempfile.TemporaryDirectory() as folder, patch("assistant.PAUSED_TASK_PATH", Path(folder) / "paused_task.json"):
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "shot", "images": ["base64..."]},
            ]
            save_paused_task("open settings", messages, ["click Settings"])
            saved = load_paused_task()
            self.assertEqual(saved["task"], "open settings")
            self.assertEqual(saved["completed_steps"], ["click Settings"])
            self.assertTrue(all("images" not in m for m in saved["messages"]))
            clear_paused_task()
            self.assertIsNone(load_paused_task())

    def test_pending_action_is_persisted_in_a_private_file(self):
        from assistant import save_paused_task, load_paused_task
        with tempfile.TemporaryDirectory() as folder, patch("assistant.PAUSED_TASK_PATH", Path(folder) / "paused_task.json"):
            save_paused_task(
                "send a message",
                [{"role": "user", "content": "send it"}],
                [],
                pending_action='click button "Send"',
            )
            saved = load_paused_task()
            self.assertEqual(saved["pending_action"], 'click button "Send"')
            self.assertEqual(Path(folder, "paused_task.json").stat().st_mode & 0o777, 0o600)

    def test_resume_inspects_before_retrying_a_pending_browser_action(self):
        assistant = DesktopAssistant()
        page = MagicMock()
        saved = {
            "task": "submit the form",
            "messages": [{"role": "user", "content": "submit it"}],
            "completed_steps": [],
            "pending_action": 'click button "Submit"',
            "mode": "browser",
            "url": "https://example.com/form",
        }
        with patch("assistant.load_paused_task", return_value=saved), \
                patch("assistant.BROWSER_ENGINE.page", return_value=page), \
                patch("assistant.browser_automation.goto"), \
                patch.object(assistant, "_run_browser_loop", return_value="resumed") as run:
            self.assertEqual(assistant.resume_task(), "resumed")
        messages = run.call_args.args[2]
        self.assertIn("may have executed", messages[-1]["content"])
        self.assertIn("Do not repeat a side effect", messages[-1]["content"])

    def test_resume_reports_browser_page_restore_failure(self):
        assistant = DesktopAssistant()
        with patch("assistant.load_paused_task", return_value={
            "task": "continue",
            "messages": [],
            "completed_steps": [],
            "mode": "browser",
            "url": "https://example.com",
        }), patch("assistant.BROWSER_ENGINE.page", return_value=MagicMock()), \
                patch("assistant.browser_automation.goto", side_effect=RuntimeError("navigation failed")), \
                patch.object(assistant, "_run_browser_loop") as run:
            result = assistant.resume_task()
        self.assertIn("Could not restore the paused browser page", result)
        run.assert_not_called()

    def test_resume_with_no_paused_task(self):
        assistant = DesktopAssistant()
        with tempfile.TemporaryDirectory() as folder, patch("assistant.PAUSED_TASK_PATH", Path(folder) / "paused_task.json"):
            self.assertEqual(assistant.resume_task(), "There is no paused task to resume.")


class BrowserTaskTests(unittest.TestCase):
    def test_looks_like_web_task_detects_urls_and_domains(self):
        self.assertTrue(looks_like_web_task("open github.com and star a repo"))
        self.assertTrue(looks_like_web_task("go to https://example.com/page"))
        self.assertFalse(looks_like_web_task("open Calculator"))

    def test_validate_browser_action_requires_a_known_ref(self):
        elements = [{"role": "link", "name": "Sign in"}]
        clean = validate_browser_action({"action": "click", "ref": 1, "reason": "open sign in"}, elements)
        self.assertEqual(clean["element"], elements[0])
        with self.assertRaises(ValueError):
            validate_browser_action({"action": "click", "ref": 9, "reason": "x"}, elements)

    def test_web_task_routes_through_browser_loop_to_done(self):
        page = MagicMock(url="https://example.com")
        elements = [{"role": "link", "name": "Sign in"}]
        actions = [
            json.dumps({"action": "click", "ref": 1, "reason": "open sign in"}),
            json.dumps({"action": "done", "message": "Signed in."}),
        ]
        da = DesktopAssistant(autonomous=True)
        output = io.StringIO()
        with (
            patch("assistant.BROWSER_ENGINE.page", return_value=page),
            patch("assistant.browser_automation.goto"),
            patch("assistant.browser_automation.snapshot", return_value=("Page: 'Example'", elements)),
            patch("assistant.browser_automation.click_element") as click_element,
            patch.object(da, "vision_chat", side_effect=actions),
            patch("assistant.time.sleep"),
            redirect_stdout(output),
        ):
            result = da.desktop_task("open github.com and sign in")
        click_element.assert_called_once_with(page, elements[0])
        self.assertEqual(
            result,
            "Signed in.\n\nPages visited:\n- https://example.com",
        )


class AgentPlanningTests(unittest.TestCase):
    def test_each_background_role_has_distinct_specialist_instructions(self):
        assistants = {
            role: DesktopAssistant(agent_role=role)
            for role in ("researcher", "operator", "verifier")
        }

        self.assertIn("cite the URLs", assistants["researcher"].messages[0]["content"])
        self.assertIn("existing approval flow", assistants["operator"].messages[0]["content"])
        self.assertIn("Independently check", assistants["verifier"].messages[0]["content"])
        self.assertNotIn("send_message", {
            tool["function"]["name"] for tool in assistants["researcher"].tools
        })
        self.assertIn("send_message", {
            tool["function"]["name"] for tool in assistants["operator"].tools
        })
        self.assertNotIn("send_message", {
            tool["function"]["name"] for tool in assistants["verifier"].tools
        })

    def test_plan_uses_no_action_tools_and_restores_chat_state(self):
        assistant = DesktopAssistant(agent_role="researcher")
        original_messages = assistant.messages
        original_tools = assistant.tools

        def planned_turn():
            self.assertEqual(assistant.tools, [])
            self.assertEqual(assistant.messages[-1]["content"], "Research local-first assistants")
            return {
                "content": json.dumps({
                    "steps": ["Find projects", "Compare features", "Summarize evidence"]
                })
            }

        with patch.object(assistant, "model_turn", side_effect=planned_turn):
            plan = assistant.create_task_plan("Research local-first assistants")

        self.assertEqual(plan, ["Find projects", "Compare features", "Summarize evidence"])
        self.assertIs(assistant.messages, original_messages)
        self.assertIs(assistant.tools, original_tools)

    def test_rejects_invalid_plan_checkpoint_count(self):
        assistant = DesktopAssistant(agent_role="verifier")
        with patch.object(
            assistant,
            "model_turn",
            return_value={"content": json.dumps({"steps": ["Only one step"]})},
        ), self.assertRaisesRegex(RuntimeError, "invalid checkpoint list"):
            assistant.create_task_plan("Check a result")


class ClipboardToolTests(unittest.TestCase):
    def test_copy_then_read(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            run.return_value = MagicMock(stdout="hello")
            self.assertIn("Copied 5", assistant.run_tool("clipboard", {"action": "copy", "text": "hello"}))
            self.assertIn("hello", assistant.run_tool("clipboard", {"action": "read"}))
        self.assertEqual(run.call_args_list[0][0][0], ["pbcopy"])


class AppManagementTests(unittest.TestCase):
    def test_uninstall_always_asks_even_when_autonomous(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch("assistant.Path.exists", return_value=True), \
                patch("builtins.input", return_value="n") as prompt, \
                patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            result = assistant.uninstall_app("Chess")
        prompt.assert_called_once()
        run.assert_not_called()
        self.assertIn("nothing was removed", result)

    def test_refuses_to_remove_itself(self):
        with patch("assistant.Path.exists", return_value=True), redirect_stdout(io.StringIO()):
            self.assertIn("cannot be uninstalled", DesktopAssistant().uninstall_app("Finder"))

    def test_uninstall_passes_app_bundle_path_as_data(self):
        assistant = DesktopAssistant()
        with patch("assistant.Path.exists", return_value=True), \
                patch("builtins.input", return_value="yes"), \
                patch("assistant.subprocess.run", return_value=MagicMock(returncode=0)) as run, \
                redirect_stdout(io.StringIO()):
            result = assistant.uninstall_app("Example App")

        delete_command = run.call_args_list[1].args[0]
        self.assertIn("item 1 of argv", delete_command[2])
        self.assertEqual(delete_command[3], "/Applications/Example App.app")
        self.assertIn("Moved Example App to the Trash", result)

    def test_install_requires_confirmation_even_in_autonomous_mode(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch("assistant.Path.exists", return_value=False), \
                patch("builtins.input", return_value="n") as prompt, \
                patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            result = assistant.install_app("Example App")
        prompt.assert_called_once()
        run.assert_not_called()
        self.assertIn("declined", result)

    def test_install_dry_run_has_no_external_effects(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch.dict("os.environ", {"HELM_DRY_RUN": "1"}), \
                patch("assistant.Path.exists", return_value=False), \
                patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
            result = assistant.install_app("Example App")
        run.assert_not_called()
        self.assertIn("Dry run", result)


class QuitAppTests(unittest.TestCase):
    def test_rejects_invalid_app_name_before_running_anything(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run:
            result = assistant.run_tool("quit_app", {"name": "x; rm -rf /"})
        self.assertIn("not a valid", result)
        run.assert_not_called()

    def test_refuses_to_quit_itself(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run:
            result = assistant.run_tool("quit_app", {"name": "Helm"})
        self.assertIn("cannot quit itself", result)
        run.assert_not_called()

    def test_a_hung_running_state_check_does_not_crash_the_process(self):
        """The crash seen live: System Events itself can hang, not just the quit call."""
        import subprocess as subprocess_module
        assistant = DesktopAssistant()
        with patch(
            "assistant.subprocess.run", side_effect=subprocess_module.TimeoutExpired(cmd="osascript", timeout=10),
        ):
            result = assistant.run_tool("quit_app", {"name": "Spotify"})
        self.assertIn("did not respond in time", result)

    def test_a_hung_final_confirmation_check_does_not_crash_the_process(self):
        import subprocess as subprocess_module
        assistant = DesktopAssistant()
        before_check = MagicMock(returncode=0, stdout="true")
        quit_reply = MagicMock(returncode=0, stderr="")
        with patch(
            "assistant.subprocess.run",
            side_effect=[before_check, quit_reply, subprocess_module.TimeoutExpired(cmd="osascript", timeout=10)],
        ), patch("assistant.time.sleep"):
            result = assistant.run_tool("quit_app", {"name": "Spotify"})
        self.assertIn("could not confirm it actually exited", result)

    def test_reports_quit_only_after_verifying_the_process_is_gone(self):
        assistant = DesktopAssistant()
        before_check = MagicMock(returncode=0, stdout="true")
        quit_reply = MagicMock(returncode=0, stderr="")
        after_check = MagicMock(returncode=0, stdout="false")
        with patch("assistant.subprocess.run", side_effect=[before_check, quit_reply, after_check]) as run, \
                patch("assistant.time.sleep"):
            result = assistant.run_tool("quit_app", {"name": "Spotify"})
        self.assertEqual(result, "Quit Spotify.")
        self.assertIn("tell application \"Spotify\" to quit", run.call_args_list[1][0][0][2])

    def test_reports_honestly_when_the_app_is_still_running_after_quit(self):
        assistant = DesktopAssistant()
        before_check = MagicMock(returncode=0, stdout="true")
        quit_reply = MagicMock(returncode=0, stderr="")
        after_check = MagicMock(returncode=0, stdout="true")
        with patch("assistant.subprocess.run", side_effect=[before_check, quit_reply, after_check]), \
                patch("assistant.time.sleep"):
            result = assistant.run_tool("quit_app", {"name": "Spotify"})
        self.assertIn("still running", result)

    def test_app_that_was_never_running_is_never_launched(self):
        """tell application X to quit launches X first if it wasn't running — must not do that."""
        assistant = DesktopAssistant()
        before_check = MagicMock(returncode=0, stdout="false")
        with patch("assistant.subprocess.run", return_value=before_check) as run, \
                patch("assistant.time.sleep"):
            result = assistant.run_tool("quit_app", {"name": "Spotify"})
        self.assertEqual(result, "Spotify was not running.")
        run.assert_called_once()  # only the running-state check — quit was never sent

    def test_surfaces_a_genuine_applescript_error(self):
        assistant = DesktopAssistant()
        before_check = MagicMock(returncode=0, stdout="true")
        quit_reply = MagicMock(returncode=1, stderr="Some other AppleScript error.")
        with patch("assistant.subprocess.run", side_effect=[before_check, quit_reply]), patch("assistant.time.sleep"):
            result = assistant.run_tool("quit_app", {"name": "Spotify"})
        self.assertIn("Could not quit Spotify", result)

    def test_a_blocking_save_dialog_times_out_instead_of_hanging_forever(self):
        import subprocess as subprocess_module
        assistant = DesktopAssistant()
        before_check = MagicMock(returncode=0, stdout="true")
        with patch(
            "assistant.subprocess.run",
            side_effect=[before_check, subprocess_module.TimeoutExpired(cmd="osascript", timeout=10)],
        ), patch("assistant.time.sleep"):
            result = assistant.run_tool("quit_app", {"name": "TextEdit"})
        self.assertIn("did not quit within 10 seconds", result)


class ListRunningAppsTests(unittest.TestCase):
    def test_lists_all_running_app_names_without_system_events(self):
        assistant = DesktopAssistant()
        with (
            patch("assistant.running_app_names", return_value=["Discord", "Finder", "Safari"]) as list_apps,
            patch("assistant.subprocess.run", side_effect=AssertionError("should not use Apple Events")),
        ):
            result = assistant.run_tool("list_running_apps", {})
        list_apps.assert_called_once_with()
        self.assertIn("Safari", result)
        self.assertIn("Discord", result)
        self.assertIn("Finder", result)
        self.assertNotIn("Helm", result)

    def test_reports_cleanly_when_nothing_else_is_running(self):
        assistant = DesktopAssistant()
        with patch("assistant.running_app_names", return_value=[]):
            result = assistant.run_tool("list_running_apps", {})
        self.assertIn("No other apps are currently running", result)

    def test_reports_appkit_listing_errors(self):
        assistant = DesktopAssistant()
        with patch("assistant.running_app_names", side_effect=RuntimeError("AppKit unavailable")):
            result = assistant.run_tool("list_running_apps", {})
        self.assertIn("AppKit unavailable", result)


class RunningAppNamesTests(unittest.TestCase):
    def test_includes_apps_without_visible_windows_and_excludes_helm(self):
        import sys
        from screen_elements import running_app_names

        apps = []
        for name in ("Safari", "Helm", "Finder", "Safari", None):
            app = MagicMock()
            app.localizedName.return_value = name
            apps.append(app)
        appkit = MagicMock()
        appkit.NSWorkspace.sharedWorkspace.return_value.runningApplications.return_value = apps
        with patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertEqual(running_app_names(), ["Finder", "Safari"])


class EmailTests(unittest.TestCase):
    def test_rejects_an_invalid_recipient_address(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run:
            result = assistant.run_tool("email", {"to": "not-an-email", "subject": "Hi", "body": "Hello"})
        self.assertIn("valid email address", result)
        run.assert_not_called()

    def test_rejects_an_invalid_cc_address(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run:
            result = assistant.run_tool(
                "email", {"to": "sam@example.com", "subject": "Hi", "body": "Hello", "cc": "not-an-email"}
            )
        self.assertIn("cc address", result)
        run.assert_not_called()

    def test_rejects_missing_subject_or_body(self):
        assistant = DesktopAssistant()
        with patch("assistant.subprocess.run") as run:
            result = assistant.run_tool("email", {"to": "sam@example.com", "subject": "", "body": "Hello"})
        self.assertIn("subject and a body", result)
        run.assert_not_called()

    def test_never_prompts_for_approval_since_nothing_is_sent(self):
        assistant = DesktopAssistant()
        reply = MagicMock(returncode=0)
        with patch("assistant.subprocess.run", return_value=reply), \
                patch("builtins.input", side_effect=AssertionError("must not prompt; nothing is ever sent")):
            result = assistant.run_tool("email", {"to": "sam@example.com", "subject": "Hi", "body": "Hello there"})
        self.assertIn("Opened a Gmail compose window in Chrome", result)

    def test_opens_a_gmail_compose_link_in_chrome_with_fields_filled_in(self):
        assistant = DesktopAssistant()
        reply = MagicMock(returncode=0)
        with patch("assistant.subprocess.run", return_value=reply) as run:
            assistant.run_tool(
                "email", {"to": "sam@example.com", "subject": "Hi there", "body": "Hello!", "cc": "pat@example.com"}
            )
        command = run.call_args[0][0]
        self.assertEqual(command[:3], ["open", "-a", "Google Chrome"])
        url = command[3]
        self.assertTrue(url.startswith("https://mail.google.com/mail/?"))
        self.assertIn("to=sam%40example.com", url)
        self.assertIn("su=Hi", url)
        self.assertIn("cc=pat%40example.com", url)

    def test_surfaces_a_chrome_launch_error(self):
        assistant = DesktopAssistant()
        error = subprocess.CalledProcessError(1, "open", stderr=b"Chrome not found")
        with patch("assistant.subprocess.run", side_effect=error):
            result = assistant.run_tool("email", {"to": "sam@example.com", "subject": "Hi", "body": "Hello"})
        self.assertIn("Could not open Gmail compose", result)

    def test_does_not_crash_if_chrome_hangs(self):
        with patch(
            "assistant.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="open", timeout=15),
        ):
            result = DesktopAssistant().run_tool("email", {"to": "sam@example.com", "subject": "Hi", "body": "Hello"})
        self.assertIn("did not respond in time", result)


class WrittenToolCallTests(unittest.TestCase):
    def test_parses_a_tool_call_written_as_text(self):
        from assistant import CHAT_TOOLS
        calls = written_tool_call("clipboard(action=copy, text=banana)", CHAT_TOOLS)
        self.assertEqual(calls[0]["function"], {"name": "clipboard", "arguments": {"action": "copy", "text": "banana"}})
        self.assertEqual(written_tool_call("I think clipboard(x) is neat", CHAT_TOOLS), [])
        self.assertEqual(written_tool_call("rm_rf(path=/)", CHAT_TOOLS), [])


class InteractTests(unittest.TestCase):
    def test_click_presses_the_named_control(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch.object(assistant, "focus_app", return_value=""), \
                patch.object(DesktopAssistant, "desktop_tools", return_value=MagicMock()), \
                patch("assistant.find_in_app", return_value=("button", "7")), \
                patch("assistant.press") as press, patch("assistant.time.sleep"), \
                patch("assistant.element_center", return_value=None), \
                patch("assistant.read_app", return_value=("7", [])):
            result = assistant.interact("click_in_app", {"app": "Calculator", "label": "7"})
        press.assert_called_once_with("button")  # no on-screen position: falls back to Accessibility
        self.assertIn("Pressed '7'", result)

    def test_risky_clicks_ask_even_in_full_autonomy(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch("builtins.input", return_value="n") as prompt, \
                patch.object(assistant, "focus_app") as focus, redirect_stdout(io.StringIO()):
            result = assistant.interact("click_in_app", {"app": "Finder", "label": "Empty Trash"})
        prompt.assert_called_once()
        focus.assert_not_called()
        self.assertIn("nothing was done", result)


class KnowledgeTests(unittest.TestCase):
    KNOWLEDGE = [{"name": "Spotify", "category": "music", "importance": "popular", "what": "Music streaming.",
                  "how_people_use_it": "Playlists.", "benefits": "Any song.", "plans": {"Individual": "$12.99/month"},
                  "tech": "Java.", "chatbot_tips": ""}]

    def test_mentioned_apps_are_recalled_into_the_system_prompt(self):
        self.assertEqual([e["name"] for e in apps_mentioned("is spotify worth it?", self.KNOWLEDGE)], ["Spotify"])
        assistant = DesktopAssistant()
        with patch("assistant.load_knowledge", return_value=self.KNOWLEDGE):
            assistant.recall_apps("how much is Spotify premium")
        self.assertIn("$12.99/month", assistant.messages[0]["content"])

    def test_knowledge_tool_finds_by_name_or_category(self):
        assistant = DesktopAssistant()
        with patch("assistant.load_knowledge", return_value=self.KNOWLEDGE), redirect_stdout(io.StringIO()):
            self.assertIn("Java", assistant.run_tool("app_knowledge", {"query": "spotify"}))
            self.assertIn("Spotify", assistant.run_tool("app_knowledge", {"query": "music"}))


class TypingTests(unittest.TestCase):
    def test_documents_are_typed_not_pasted(self):
        from assistant import type_out
        fake = MagicMock()
        with patch("assistant.subprocess.run") as run, patch("assistant.app_pid", return_value=None):
            type_out(fake, "\u201cHi\u201d \u2014 it\u2019s fine")
        self.assertEqual("".join(c.args[0] for c in fake.write.call_args_list), '"Hi"  -  it\'s fine')
        run.assert_not_called()  # no pbcopy, no Cmd+V
        fake.hotkey.assert_not_called()


class FocusDuringTypingTests(unittest.TestCase):
    def test_typing_stops_the_moment_focus_moves(self):
        from assistant import FocusLost, type_out
        fake = MagicMock()
        fronts = iter(["TextEdit"] + ["Claude"] * 50)
        with patch("assistant.frontmost_app_name", side_effect=lambda: next(fronts)), \
                patch("assistant.app_pid", return_value=None):
            with self.assertRaises(FocusLost):
                type_out(fake, "Netflix has changed how people watch entertainment at home today.", app="TextEdit")
        typed = "".join(call.args[0] for call in fake.write.call_args_list)
        self.assertLess(len(typed), 40)  # stopped after the first few words


class CoordinateClickTests(unittest.TestCase):
    def test_click_shows_helms_cursor_and_leaves_the_users_mouse_alone(self):
        assistant = DesktopAssistant(autonomous=True)
        fake = MagicMock()
        fake.size.return_value = (1440, 900)
        with patch.object(assistant, "focus_app", return_value=""), \
                patch.object(DesktopAssistant, "desktop_tools", return_value=fake), \
                patch("assistant.find_in_app", return_value=("button", "7")), \
                patch("assistant.element_center", return_value=(402, 722)), \
                patch("assistant.frontmost_app_name", return_value="Calculator"), \
                patch("assistant.press") as press, patch("assistant.time.sleep"), \
                patch("assistant.read_app", return_value=("7", [])):
            assistant.interact("click_in_app", {"app": "Calculator", "label": "7"})
        press.assert_called_once_with("button")  # pressed via Accessibility: the user's mouse stays put
        fake.click.assert_not_called()


class BackgroundTypingTests(unittest.TestCase):
    def test_known_app_gets_keys_directly_without_focus(self):
        from assistant import type_out
        fake = MagicMock()
        with patch("assistant.app_pid", return_value=617), patch("assistant.post_text") as post:
            type_out(fake, "hello \u201cworld\u201d", app="TextEdit")
        post.assert_called_once_with(617, 'hello "world"')
        fake.write.assert_not_called()  # nothing went through the shared keyboard


class FileOperationsTests(unittest.TestCase):
    def test_path_outside_home_is_rejected(self):
        assistant = DesktopAssistant()
        self.assertIn("inside your home folder", assistant.file_operations("list", "/etc"))

    def test_protected_subfolder_is_rejected(self):
        assistant = DesktopAssistant()
        self.assertIn("protected folder", assistant.file_operations("list", "~/.ssh"))

    def test_list_and_read_text_need_no_confirmation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            (Path(folder) / "notes.txt").write_text("hello")
            assistant = DesktopAssistant()
            with patch("builtins.input") as prompt:
                self.assertIn("notes.txt", assistant.file_operations("list", "~"))
                self.assertEqual(assistant.file_operations("read_text", "~/notes.txt"), "hello")
            prompt.assert_not_called()

    def test_delete_goes_to_trash_and_always_asks(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            (Path(folder) / "old.txt").write_text("x")
            assistant = DesktopAssistant(autonomous=True)
            with patch("builtins.input", return_value="n") as prompt, \
                    patch("assistant.subprocess.run") as run, redirect_stdout(io.StringIO()):
                result = assistant.file_operations("delete", "~/old.txt")
            prompt.assert_called_once()
            run.assert_not_called()
            self.assertIn("declined", result)
            self.assertTrue((Path(folder) / "old.txt").exists())  # nothing happened

    def test_delete_passes_path_as_data_not_apple_script_source(self):
        name = 'report " & say "not code" & ".txt'
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            target = Path(folder) / name
            target.write_text("x")
            assistant = DesktopAssistant(autonomous=True)
            with patch("builtins.input", return_value="yes"), \
                    patch("assistant.subprocess.run", return_value=MagicMock(returncode=0)) as run, \
                    redirect_stdout(io.StringIO()):
                result = assistant.file_operations("delete", str(target))

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["osascript", "-e"])
        self.assertIn("item 1 of argv", command[2])
        self.assertNotIn(name, command[2])
        self.assertEqual(command[3], str(target.resolve()))
        self.assertIn("Moved report", result)

    def test_make_folder_combines_a_split_path_and_destination(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            assistant = DesktopAssistant()
            result = assistant.file_operations("make_folder", "test", "Desktop")
            self.assertIn("Created folder", result)
            self.assertTrue((Path(folder) / "Desktop" / "test").is_dir())

    def test_copy_does_not_require_confirmation_but_move_does(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HOME": folder}):
            (Path(folder) / "a.txt").write_text("x")
            assistant = DesktopAssistant(autonomous=True)
            with patch("builtins.input") as prompt:
                result = assistant.file_operations("copy", "~/a.txt", "~/b.txt")
            prompt.assert_not_called()
            self.assertIn("Copied", result)
            self.assertTrue((Path(folder) / "b.txt").exists())
            self.assertTrue((Path(folder) / "a.txt").exists())  # source untouched


class DragInAppTests(unittest.TestCase):
    def test_drags_between_two_found_elements(self):
        assistant = DesktopAssistant(autonomous=True)
        fake = MagicMock()
        fake.position.return_value = (10, 10)
        found = iter([("item-a", "a.txt"), ("item-b", "Archive")])
        with patch.object(assistant, "focus_app", return_value=""), \
                patch.object(DesktopAssistant, "desktop_tools", return_value=fake), \
                patch("assistant.find_in_app", side_effect=lambda app, m, roles=None: next(found)), \
                patch("assistant.element_center", side_effect=[(50, 50), (200, 200)]), \
                patch("assistant.frontmost_app_name", return_value="Finder"), \
                patch("assistant.real_drag") as drag, patch("assistant.time.sleep"):
            result = assistant.interact("drag_in_app", {"app": "Finder", "from_label": "a.txt", "to_label": "Archive"})
        drag.assert_called_once_with(fake, (50, 50), (200, 200))
        self.assertIn("Dragged 'a.txt' onto 'Archive'", result)

    def test_missing_source_element_is_reported(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch.object(assistant, "focus_app", return_value=""), \
                patch.object(DesktopAssistant, "desktop_tools", return_value=MagicMock()), \
                patch("assistant.find_in_app", return_value=(None, "")), \
                patch("assistant.real_drag") as drag:
            result = assistant.interact("drag_in_app", {"app": "Finder", "from_label": "ghost.txt", "to_label": "Archive"})
        drag.assert_not_called()
        self.assertIn("No 'ghost.txt'", result)


class WindowControlTests(unittest.TestCase):
    def test_screen_bounds_uses_display_points_not_retina_pixel_dimensions(self):
        import sys
        from types import SimpleNamespace
        from screen_elements import screen_bounds

        quartz = MagicMock()
        quartz.CGMainDisplayID.return_value = 7
        quartz.CGDisplayBounds.return_value = SimpleNamespace(
            size=SimpleNamespace(width=1440.0, height=900.0)
        )
        quartz.CGDisplayPixelsWide.return_value = 2880
        quartz.CGDisplayPixelsHigh.return_value = 1800
        with patch.dict(sys.modules, {"Quartz": quartz}):
            self.assertEqual(screen_bounds(), (1440, 900))

        quartz.CGDisplayBounds.assert_called_once_with(7)
        quartz.CGDisplayPixelsWide.assert_not_called()
        quartz.CGDisplayPixelsHigh.assert_not_called()

    def test_rejects_invalid_app_name_before_touching_the_screen(self):
        assistant = DesktopAssistant()
        with patch("assistant.window_control") as control:
            result = assistant.run_tool("window_control", {"app": "x; rm -rf /", "action": "close"})
        control.assert_not_called()
        self.assertIn("not a valid app name", result)

    def test_forwards_action_and_params_to_screen_elements(self):
        assistant = DesktopAssistant()
        with patch("assistant.window_control", return_value="Snapped Safari to left.") as control:
            result = assistant.run_tool(
                "window_control", {"app": "Safari", "action": "snap", "position": "left"}
            )
        control.assert_called_once_with("Safari", "snap", position="left")
        self.assertEqual(result, "Snapped Safari to left.")

    def test_no_window_reports_cleanly(self):
        from screen_elements import window_control
        with patch("screen_elements._main_window", return_value=(None, None)):
            result = window_control("NoSuchApp", "minimize")
        self.assertIn("no window", result)

    def test_unknown_snap_position_is_rejected(self):
        from screen_elements import window_control
        with patch("screen_elements._main_window", return_value=(object(), object())):
            result = window_control("Safari", "snap", position="diagonal")
        self.assertIn("Unknown snap position", result)

    def test_snap_accepts_up_and_down_as_top_and_bottom(self):
        import sys
        from screen_elements import window_control

        window = object()
        for position, expected in (("up", (0, 0, 100, 50)), ("down", (0, 50, 100, 50))):
            with (
                patch.dict(sys.modules, {"ApplicationServices": MagicMock()}),
                patch("screen_elements._main_window", return_value=(object(), window)),
                patch("screen_elements.screen_bounds", return_value=(100, 100)),
                patch("screen_elements._set_frame", return_value=True) as set_frame,
            ):
                result = window_control("Safari", "snap", position=position)
            set_frame.assert_called_once_with(window, *expected)
            self.assertIn(f"Snapped Safari to {position}", result)


class LooksLikeActionRequestTests(unittest.TestCase):
    def test_imperative_action_phrases_are_detected(self):
        for text in ("Open Calculator", "send a message to Sam", "play some music", "set the volume to 20"):
            self.assertTrue(looks_like_action_request(text), text)

    def test_questions_and_plain_chat_are_not_flagged(self):
        for text in ("what time is it?", "how are you", "", "   "):
            self.assertFalse(looks_like_action_request(text), text)


class CondenseHistoryTests(unittest.TestCase):
    def test_short_history_is_left_untouched(self):
        saved = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        self.assertEqual(condense_history(saved), saved)

    def test_older_turns_drop_tool_calls_and_tool_results_but_keep_text(self):
        saved = (
            [
                {"role": "user", "content": "do an old task"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "content": "old tool result", "tool_name": "open_app", "tool_call_id": "c1"},
                {"role": "assistant", "content": "Done with the old task."},
            ]
            + [{"role": "user", "content": f"recent {i}"} for i in range(HISTORY_FULL_DETAIL_MESSAGES)]
        )
        condensed = condense_history(saved)
        older = condensed[: len(condensed) - HISTORY_FULL_DETAIL_MESSAGES]
        self.assertEqual(older, [
            {"role": "user", "content": "do an old task"},
            {"role": "assistant", "content": "Done with the old task."},
        ])
        self.assertEqual(condensed[-HISTORY_FULL_DETAIL_MESSAGES:], saved[-HISTORY_FULL_DETAIL_MESSAGES:])

    def test_recent_turns_keep_tool_calls_intact(self):
        saved = [{"role": "user", "content": f"old {i}"} for i in range(10)] + [
            {"role": "user", "content": "do it now"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        ]
        condensed = condense_history(saved)
        self.assertEqual(condensed[-1], {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]})


class MissingToolCallRetryTests(unittest.TestCase):
    def test_nudges_once_then_accepts_the_tool_call(self):
        assistant = DesktopAssistant(autonomous=True)
        turns = [
            {"content": "I have opened the Calculator app."},  # hallucinated, no tool call
            {"content": "", "tool_calls": [{
                "function": {"name": "open_app", "arguments": {"name": "Calculator"}},
            }]},
            {"content": "Opened Calculator."},  # follow-up turn after the tool result
        ]
        with patch.object(assistant, "model_turn", side_effect=turns), \
                patch.object(assistant, "run_tool", return_value="Opened Calculator.") as run_tool:
            reply = assistant.chat("Open Calculator and verify it is visible")
        run_tool.assert_called_once_with("open_app", {"name": "Calculator"})
        self.assertIn("Calculator", reply)
        nudges = [m for m in assistant.messages if m.get("content", "").startswith("That looked like a request")]
        self.assertEqual(len(nudges), 1)

    def test_only_nudges_once_even_if_still_no_tool_call(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch.object(assistant, "model_turn", return_value={"content": "I have opened the Calculator app."}):
            reply =             assistant.chat("Open Calculator and verify it is visible")
        self.assertEqual(reply, "I have opened the Calculator app.")

    def test_does_not_nudge_for_plain_questions(self):
        assistant = DesktopAssistant(autonomous=True)
        with patch.object(assistant, "model_turn", return_value={"content": "It is sunny today."}) as model_turn:
            assistant.chat("what's the weather like?")
        model_turn.assert_called_once()


class TvControlDispatchTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tv_serial_path = Path(self._temp.name) / "tv_device.txt"
        self.tv_devices_path = Path(self._temp.name) / "tv_devices.json"
        for patch_target, value in (
            ("assistant.TV_SERIAL_PATH", self.tv_serial_path),
            ("assistant.TV_DEVICES_PATH", self.tv_devices_path),
        ):
            patcher = patch(patch_target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tv_connect_saves_serial_and_reports_device_name(self):
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.connect", return_value="192.168.1.5:5555"),
            patch("assistant.tv_control.device_name", return_value="Chromecast with Google TV"),
        ):
            result = assistant.run_tool("tv_connect", {"host": "192.168.1.5"})
        self.assertEqual(self.tv_serial_path.read_text(encoding="utf-8"), "192.168.1.5:5555")
        self.assertIn("Chromecast with Google TV", result)

    def test_tv_connect_reports_adb_error(self):
        assistant = DesktopAssistant()
        with patch("assistant.tv_control.connect", side_effect=tv_control.AdbError("timed out")):
            result = assistant.run_tool("tv_connect", {"host": "192.168.1.5"})
        self.assertIn("Could not connect", result)

    def test_tv_open_app_requires_a_connected_tv(self):
        assistant = DesktopAssistant()
        result = assistant.run_tool("tv_open_app", {"name": "Netflix"})
        self.assertIn("No TV is connected", result)

    def test_tv_open_app_launches_known_package(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.find_package", return_value="com.netflix.ninja") as find_package,
            patch("assistant.tv_control.launch_app") as launch_app,
        ):
            result = assistant.run_tool("tv_open_app", {"name": "Netflix"})
        find_package.assert_called_once_with("192.168.1.5:5555", "Netflix")
        launch_app.assert_called_once_with("192.168.1.5:5555", "com.netflix.ninja")
        self.assertEqual(result, "Opened Netflix on the TV.")

    def test_tv_install_app_asks_for_confirmation_first(self):
        assistant = DesktopAssistant()
        with patch("builtins.input", return_value="no") as prompt:
            result = assistant.run_tool("tv_install_app", {"apk_path": "/tmp/app.apk"})
        prompt.assert_called_once()
        self.assertIn("declined", result)

    def test_tv_install_app_installs_after_confirmation(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("builtins.input", return_value="yes"),
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.install_apk") as install_apk,
        ):
            result = assistant.run_tool("tv_install_app", {"apk_path": "/tmp/app.apk"})
        install_apk.assert_called_once_with("192.168.1.5:5555", "/tmp/app.apk")
        self.assertIn("Installed", result)

    def test_tv_uninstall_app_asks_for_confirmation_first(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.find_package", return_value="com.netflix.ninja"),
            patch("assistant.tv_control.uninstall_app") as uninstall_app,
            patch("builtins.input", return_value="no") as prompt,
        ):
            result = assistant.run_tool("tv_uninstall_app", {"name": "Netflix"})
        prompt.assert_called_once()
        uninstall_app.assert_not_called()
        self.assertIn("declined", result)

    def test_tv_remote_key_sends_key(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.send_key") as send_key,
        ):
            result = assistant.run_tool("tv_remote_key", {"key": "home"})
        send_key.assert_called_once_with("192.168.1.5:5555", "home")
        self.assertEqual(result, "Sent home to the TV.")

    def test_tv_connect_saves_device_under_slugified_name(self):
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.connect", return_value="192.168.1.5:5555"),
            patch("assistant.tv_control.device_name", return_value="Living Room TV"),
        ):
            assistant.run_tool("tv_connect", {"host": "192.168.1.5"})
        devices = json.loads(self.tv_devices_path.read_text(encoding="utf-8"))
        self.assertEqual(devices, {"living_room_tv": "192.168.1.5:5555"})

    def test_tv_connect_honors_explicit_name(self):
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.connect", return_value="192.168.1.5:5555"),
            patch("assistant.tv_control.device_name", return_value="Living Room TV"),
        ):
            result = assistant.run_tool("tv_connect", {"host": "192.168.1.5", "name": "Bedroom"})
        devices = json.loads(self.tv_devices_path.read_text(encoding="utf-8"))
        self.assertEqual(devices, {"bedroom": "192.168.1.5:5555"})
        self.assertIn("'bedroom'", result)

    def test_tv_list_devices_marks_the_active_one(self):
        self.tv_devices_path.write_text(
            json.dumps({"living_room": "192.168.1.5:5555", "bedroom": "192.168.1.9:5555"}),
            encoding="utf-8",
        )
        self.tv_serial_path.write_text("192.168.1.9:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        result = assistant.run_tool("tv_list_devices", {})
        self.assertIn("living_room: 192.168.1.5:5555\n", result)
        self.assertIn("bedroom: 192.168.1.9:5555 (active)", result)

    def test_tv_use_device_switches_active_serial(self):
        self.tv_devices_path.write_text(
            json.dumps({"bedroom": "192.168.1.9:5555"}), encoding="utf-8",
        )
        assistant = DesktopAssistant()
        with patch("assistant.tv_control.is_connected", return_value=True):
            result = assistant.run_tool("tv_use_device", {"name": "Bedroom"})
        self.assertEqual(self.tv_serial_path.read_text(encoding="utf-8"), "192.168.1.9:5555")
        self.assertIn("Switched to 'Bedroom'", result)

    def test_tv_use_device_reports_unknown_name(self):
        assistant = DesktopAssistant()
        result = assistant.run_tool("tv_use_device", {"name": "Nope"})
        self.assertIn("No saved TV", result)

    def test_tv_search_opens_deep_link(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.search") as search,
        ):
            result = assistant.run_tool("tv_search", {"app": "Netflix", "query": "Bluey"})
        search.assert_called_once_with("192.168.1.5:5555", "Netflix", "Bluey")
        self.assertIn("Netflix", result)
        self.assertIn("Bluey", result)

    def test_tv_set_volume_dispatches(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.set_volume") as set_volume,
        ):
            result = assistant.run_tool("tv_set_volume", {"level": 5})
        set_volume.assert_called_once_with("192.168.1.5:5555", 5)
        self.assertIn("5", result)

    def test_tv_wake_dispatches(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.wake") as wake,
        ):
            result = assistant.run_tool("tv_wake", {})
        wake.assert_called_once_with("192.168.1.5:5555")
        self.assertEqual(result, "Woke the TV.")

    def test_tv_sleep_dispatches(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.sleep_device") as sleep_device,
        ):
            result = assistant.run_tool("tv_sleep", {})
        sleep_device.assert_called_once_with("192.168.1.5:5555")
        self.assertEqual(result, "Put the TV to sleep.")

    def test_tv_tap_text_dispatches(self):
        self.tv_serial_path.write_text("192.168.1.5:5555", encoding="utf-8")
        assistant = DesktopAssistant()
        with (
            patch("assistant.tv_control.is_connected", return_value=True),
            patch("assistant.tv_control.tap_text") as tap_text,
        ):
            result = assistant.run_tool("tv_tap_text", {"text": "Always"})
        tap_text.assert_called_once_with("192.168.1.5:5555", "Always")
        self.assertIn("Always", result)

    def test_tv_schedule_action_asks_for_confirmation_first(self):
        assistant = DesktopAssistant()
        with patch("builtins.input", return_value="no") as prompt:
            result = assistant.run_tool(
                "tv_schedule_action", {"name": "bedtime", "action": "sleep", "hour": 21, "minute": 0},
            )
        prompt.assert_called_once()
        self.assertIn("declined", result)

    def test_tv_schedule_action_schedules_after_confirmation(self):
        assistant = DesktopAssistant()
        with (
            patch("builtins.input", return_value="yes"),
            patch("assistant.tv_scheduler.schedule_action") as schedule_action,
        ):
            result = assistant.run_tool(
                "tv_schedule_action",
                {"name": "movie_time", "action": "open_app", "arg": "Netflix", "hour": 20, "minute": 0},
            )
        schedule_action.assert_called_once_with("movie_time", "open_app", ["Netflix"], 20, 0)
        self.assertIn("movie_time", result)
        self.assertIn("20:00", result)

    def test_tv_list_scheduled_actions_formats_entries(self):
        assistant = DesktopAssistant()
        with patch("assistant.tv_scheduler.list_scheduled_actions", return_value=[
            {"name": "bedtime", "action": "sleep", "args": [], "hour": 21, "minute": 0},
        ]):
            result = assistant.run_tool("tv_list_scheduled_actions", {})
        self.assertIn("bedtime: sleep daily at 21:00", result)

    def test_tv_remove_scheduled_action_asks_for_confirmation_first(self):
        assistant = DesktopAssistant()
        with (
            patch("builtins.input", return_value="no") as prompt,
            patch("assistant.tv_scheduler.remove_scheduled_action") as remove_action,
        ):
            result = assistant.run_tool("tv_remove_scheduled_action", {"name": "bedtime"})
        prompt.assert_called_once()
        remove_action.assert_not_called()
        self.assertIn("declined", result)

    def test_tv_remove_scheduled_action_removes_after_confirmation(self):
        assistant = DesktopAssistant()
        with (
            patch("builtins.input", return_value="yes"),
            patch("assistant.tv_scheduler.remove_scheduled_action") as remove_action,
        ):
            result = assistant.run_tool("tv_remove_scheduled_action", {"name": "bedtime"})
        remove_action.assert_called_once_with("bedtime")
        self.assertIn("Removed", result)


if __name__ == "__main__":
    unittest.main()
