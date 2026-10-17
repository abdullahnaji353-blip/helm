import json
import time
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from telegram_bridge import PendingApproval, TelegramBotAPI, TelegramBridge, TelegramError


def make_bridge(api=None, owner_chat_id=999):
    bridge = TelegramBridge(
        bot_token="123456789:AAFakeTokenFakeTokenFakeToken123",
        owner_chat_id=owner_chat_id,
        project_root=Path("."),
    )
    if api is not None:
        bridge.api = api
    return bridge


class TelegramBotAPITests(unittest.TestCase):
    def test_send_text_posts_to_the_bot_token_url(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
        opener = MagicMock(return_value=response)
        api = TelegramBotAPI("123456789:AAFakeTokenFakeTokenFakeToken123", opener=opener)
        api.send_text(42, "hello")
        request = opener.call_args.args[0]
        self.assertIn("/bot123456789:AAFakeTokenFakeTokenFakeToken123/sendMessage", request.full_url)
        self.assertEqual(json.loads(request.data)["chat_id"], 42)
        self.assertEqual(json.loads(request.data)["text"], "hello")

    def test_rejects_malformed_token(self):
        with self.assertRaises(ValueError):
            TelegramBotAPI("not-a-real-token")

    def test_send_text_surfaces_api_failure(self):
        opener = MagicMock(side_effect=HTTPError("url", 401, "unauthorized", {}, None))
        api = TelegramBotAPI("123456789:AAFakeTokenFakeTokenFakeToken123", opener=opener)
        with self.assertRaisesRegex(TelegramError, "HTTP 401"):
            api.send_text(42, "hello")

    def test_get_updates_rejects_not_ok_response(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":false,"description":"bad request"}'
        api = TelegramBotAPI("123456789:AAFakeTokenFakeTokenFakeToken123", opener=MagicMock(return_value=response))
        with self.assertRaisesRegex(TelegramError, "bad request"):
            api.get_updates(None)


class TelegramUpdateHandlingTests(unittest.TestCase):
    def test_owner_message_is_dispatched(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "text": "what time is it?"}})
        bridge.handle_owner_message.assert_called_once_with("what time is it?")

    def test_non_owner_messages_are_silently_ignored(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_update({"update_id": 1, "message": {"chat": {"id": 111}, "text": "open Safari"}})
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_not_called()

    def test_pairing_mode_replies_with_chat_id_and_runs_no_commands(self):
        api = MagicMock()
        bridge = make_bridge(api, owner_chat_id=None)
        bridge.handle_owner_message = MagicMock()
        bridge._handle_update({"update_id": 1, "message": {"chat": {"id": 555}, "text": "hello"}})
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_called_once()
        self.assertIn("555", api.send_text.call_args.args[1])

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
        with patch("telegram_bridge.subprocess.Popen", return_value=process):
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
        with patch("telegram_bridge.subprocess.Popen", side_effect=OSError("no python")):
            bridge._run_remote_task("do something")  # must not raise RuntimeError on release


if __name__ == "__main__":
    unittest.main()
