import json
import time
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from slack_bridge import PendingApproval, SlackBotAPI, SlackBridge, SlackError, SlackRateLimited


def make_bridge(api=None, owner_user_id="U999"):
    bridge = SlackBridge(
        bot_token="xoxb-fake-slack-bot-token",
        channel_id="C12345",
        owner_user_id=owner_user_id,
        project_root=Path("."),
    )
    bridge._bot_user_id = "U1"  # the bot's own ID
    if api is not None:
        bridge.api = api
    return bridge


class SlackBotAPITests(unittest.TestCase):
    def test_send_text_posts_to_chat_postmessage(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":true,"ts":"123.456"}'
        with patch("slack_bridge.urlopen", return_value=response) as urlopen:
            api = SlackBotAPI("xoxb-fake-slack-bot-token")
            api.send_text("C42", "hello")
            request = urlopen.call_args.args[0]
        self.assertIn("/chat.postMessage", request.full_url)
        self.assertEqual(json.loads(request.data)["channel"], "C42")
        self.assertEqual(json.loads(request.data)["text"], "hello")
        self.assertEqual(request.headers["Authorization"], "Bearer xoxb-fake-slack-bot-token")

    def test_rejects_token_without_xoxb_prefix(self):
        with self.assertRaises(ValueError):
            SlackBotAPI("not-a-real-token")

    def test_send_text_surfaces_api_failure(self):
        with patch("slack_bridge.urlopen", side_effect=HTTPError("url", 401, "unauthorized", {}, None)):
            api = SlackBotAPI("xoxb-fake-slack-bot-token")
            with self.assertRaisesRegex(SlackError, "HTTP 401"):
                api.send_text("C42", "hello")

    def test_rejects_not_ok_response(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":false,"error":"channel_not_found"}'
        with patch("slack_bridge.urlopen", return_value=response):
            api = SlackBotAPI("xoxb-fake-slack-bot-token")
            with self.assertRaisesRegex(SlackError, "channel_not_found"):
                api.send_text("C42", "hello")

    def test_429_response_raises_rate_limited_with_retry_after(self):
        error = HTTPError("url", 429, "rate limited", {"Retry-After": "2.5"}, None)
        with patch("slack_bridge.urlopen", side_effect=error):
            api = SlackBotAPI("xoxb-fake-slack-bot-token")
            with self.assertRaises(SlackRateLimited) as context:
                api.send_text("C42", "hello")
        self.assertAlmostEqual(context.exception.retry_after, 2.5)

    def test_get_messages_after_reverses_to_oldest_first(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"ok": True, "messages": [{"ts": "2"}, {"ts": "1"}]}).encode()
        with patch("slack_bridge.urlopen", return_value=response):
            api = SlackBotAPI("xoxb-fake-slack-bot-token")
            messages = api.get_messages_after("C42", oldest=None)
        self.assertEqual([m["ts"] for m in messages], ["1", "2"])


class SlackRunForeverTests(unittest.TestCase):
    def test_rate_limit_waits_retry_after_not_poll_interval(self):
        api = MagicMock()
        api.current_user_id.return_value = "U1"
        api.get_messages_after.side_effect = SlackRateLimited(12.0)
        bridge = make_bridge(api)
        bridge._stopping.wait = MagicMock(side_effect=lambda _: bridge._stopping.set())
        bridge.run_forever()
        bridge._stopping.wait.assert_called_once_with(12.0)


class SlackMessageHandlingTests(unittest.TestCase):
    def test_owner_message_is_dispatched(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"user": "U999", "text": "what time is it?"})
        bridge.handle_owner_message.assert_called_once_with("what time is it?")

    def test_non_owner_messages_are_silently_ignored(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"user": "U111", "text": "open Safari"})
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_not_called()

    def test_bot_messages_are_ignored(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"user": "U999", "text": "hi", "bot_id": "B1"})
        bridge.handle_owner_message.assert_not_called()

    def test_pairing_mode_adopts_the_first_sender_and_runs_no_commands(self):
        api = MagicMock()
        bridge = make_bridge(api, owner_user_id=None)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"user": "U555", "text": "hello"})
        self.assertEqual(bridge.owner_user_id, "U555")
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_called_once()
        self.assertIn("U555", api.send_text.call_args.args[1])

    def test_pairing_mode_ignores_further_senders_once_paired(self):
        api = MagicMock()
        bridge = make_bridge(api, owner_user_id=None)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"user": "U555", "text": "hello"})
        api.send_text.reset_mock()
        bridge._handle_message({"user": "U777", "text": "open Safari"})
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_not_called()

    def test_approval_commands_resolve_only_current_code(self):
        api = MagicMock()
        bridge = make_bridge(api)
        pending = PendingApproval("123456")
        bridge._pending = pending
        bridge._answer_approval("APPROVE 123456")
        self.assertTrue(pending.reply.get_nowait())
        bridge._answer_approval("DENY 000000")
        api.send_text.assert_called_once()

    def test_remote_task_waits_for_one_time_approval_before_resuming(self):
        api = MagicMock()
        bridge = make_bridge(api)
        process = MagicMock()
        process.stdout = iter([
            "APPROVAL_REQUIRED: send a message\n",
            'CHAT_REPLY_JSON:"Message sent after confirmation."\n',
        ])
        process.wait.return_value = 0
        process.poll.return_value = 0
        process.stdin = MagicMock()
        with patch("slack_bridge.subprocess.Popen", return_value=process):
            worker = Thread(target=bridge._run_remote_task, args=("send a message",))
            worker.start()
            deadline = time.monotonic() + 2
            while bridge._pending is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(bridge._pending)
            approval_code = bridge._pending.code
            bridge._answer_approval(f"APPROVE {approval_code}")
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        process.stdin.write.assert_called_once_with("y\n")
        self.assertEqual(api.send_text.call_count, 2)
        self.assertIn("APPROVE", api.send_text.call_args_list[0].args[1])
        self.assertEqual(api.send_text.call_args_list[1].args[1], "Message sent after confirmation.")

    def test_run_remote_task_releases_lock_even_without_the_callers_acquire(self):
        bridge = make_bridge(MagicMock())
        with patch("slack_bridge.subprocess.Popen", side_effect=OSError("no python")):
            bridge._run_remote_task("do something")  # must not raise RuntimeError on release


if __name__ == "__main__":
    unittest.main()
