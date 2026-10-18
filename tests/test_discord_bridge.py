import json
import time
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from discord_bridge import DiscordBotAPI, DiscordBridge, DiscordError, PendingApproval


def make_bridge(api=None, owner_user_id="999"):
    bridge = DiscordBridge(
        bot_token="a-fake-discord-bot-token-value",
        channel_id="12345",
        owner_user_id=owner_user_id,
        project_root=Path("."),
    )
    bridge._bot_user_id = "1"  # the bot's own ID, so its messages are filtered out
    if api is not None:
        bridge.api = api
    return bridge


class DiscordBotAPITests(unittest.TestCase):
    def test_send_text_posts_to_the_channel_url(self):
        response = MagicMock()
        response.read.return_value = b'{"id":"1"}'
        with patch("discord_bridge.urlopen", return_value=response) as urlopen:
            api = DiscordBotAPI("a-fake-discord-bot-token-value")
            api.send_text("42", "hello")
            request = urlopen.call_args.args[0]
        self.assertIn("/channels/42/messages", request.full_url)
        self.assertEqual(json.loads(request.data)["content"], "hello")
        self.assertEqual(request.headers["Authorization"], "Bot a-fake-discord-bot-token-value")

    def test_rejects_malformed_token(self):
        with self.assertRaises(ValueError):
            DiscordBotAPI("short")

    def test_send_text_surfaces_api_failure(self):
        with patch("discord_bridge.urlopen", side_effect=HTTPError("url", 401, "unauthorized", {}, None)):
            api = DiscordBotAPI("a-fake-discord-bot-token-value")
            with self.assertRaisesRegex(DiscordError, "HTTP 401"):
                api.send_text("42", "hello")

    def test_get_messages_after_reverses_to_oldest_first(self):
        response = MagicMock()
        response.read.return_value = json.dumps([{"id": "2"}, {"id": "1"}]).encode()
        with patch("discord_bridge.urlopen", return_value=response):
            api = DiscordBotAPI("a-fake-discord-bot-token-value")
            messages = api.get_messages_after("42", after=None)
        self.assertEqual([m["id"] for m in messages], ["1", "2"])


class DiscordMessageHandlingTests(unittest.TestCase):
    def test_owner_message_is_dispatched(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"author": {"id": "999"}, "content": "what time is it?"})
        bridge.handle_owner_message.assert_called_once_with("what time is it?")

    def test_non_owner_messages_are_silently_ignored(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"author": {"id": "111"}, "content": "open Safari"})
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_not_called()

    def test_the_bots_own_messages_are_ignored(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"author": {"id": "1"}, "content": "Helm could not complete that task"})
        bridge.handle_owner_message.assert_not_called()

    def test_pairing_mode_adopts_the_first_sender_and_runs_no_commands(self):
        api = MagicMock()
        bridge = make_bridge(api, owner_user_id=None)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"author": {"id": "555"}, "content": "hello"})
        self.assertEqual(bridge.owner_user_id, "555")
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_called_once()
        self.assertIn("555", api.send_text.call_args.args[1])

    def test_pairing_mode_ignores_further_senders_once_paired(self):
        api = MagicMock()
        bridge = make_bridge(api, owner_user_id=None)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_message({"author": {"id": "555"}, "content": "hello"})
        api.send_text.reset_mock()
        bridge._handle_message({"author": {"id": "777"}, "content": "open Safari"})
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
        with patch("discord_bridge.subprocess.Popen", return_value=process):
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
        with patch("discord_bridge.subprocess.Popen", side_effect=OSError("no python")):
            bridge._run_remote_task("do something")  # must not raise RuntimeError on release


if __name__ == "__main__":
    unittest.main()
