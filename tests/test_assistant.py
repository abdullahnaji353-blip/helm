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
    search_web,
    test_screen_capture,
    validate_desktop_action,
    validate_ollama_host,
    validate_url,
)
from mcp_client import MCPConnector, MCPError
from providers import openai_turn, parse_json_object, to_anthropic_messages, to_openai_messages


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

    def test_does_not_capture_tasks_with_extra_actions_or_unknown_apps(self):
        self.assertIsNone(simple_open_app_intent("Open Safari and go to example.com"))
        self.assertIsNone(simple_open_app_intent("Open CapCut"))


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
        self.assertEqual(results, [{"title": "SwiftUI full course for beginners", "url": "https://www.youtube.com/watch?v=abc"}])

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


if __name__ == "__main__":
    unittest.main()
