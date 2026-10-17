import hashlib
import hmac
import json
import unittest
from pathlib import Path
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import MagicMock, patch

from whatsapp_bridge import (
    WhatsAppBridge,
    WhatsAppCloudAPI,
    WhatsAppError,
    create_server,
)


def make_bridge(api=None):
    bridge = WhatsAppBridge(
        access_token="access-token",
        phone_number_id="123456",
        app_secret="app-secret",
        verify_token="a_secure_verify_token_123456",
        owner_number="15551234567",
        project_root=Path("."),
    )
    if api is not None:
        bridge.api = api
    return bridge


def signed_body(bridge, message):
    payload = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"messages": [message]}}]}],
    }).encode()
    signature = "sha256=" + hmac.new(bridge.app_secret, payload, hashlib.sha256).hexdigest()
    return payload, signature


class WhatsAppCloudAPITests(unittest.TestCase):
    def test_send_text_uses_cloud_api_auth_and_recipient(self):
        response = MagicMock()
        response.read.return_value = b'{"messages":[{"id":"wamid.123"}]}'
        opener = MagicMock(return_value=response)
        api = WhatsAppCloudAPI("secret-token", "123456", "15551234567", opener=opener)
        api.send_text("hello")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://graph.facebook.com/v23.0/123456/messages")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(json.loads(request.data)["to"], "15551234567")
        self.assertEqual(json.loads(request.data)["text"]["body"], "hello")

    def test_send_text_surfaces_cloud_api_failure_without_returning_success(self):
        opener = MagicMock(side_effect=HTTPError("url", 401, "unauthorized", {}, None))
        api = WhatsAppCloudAPI("secret-token", "123456", "15551234567", opener=opener)
        with self.assertRaisesRegex(WhatsAppError, "HTTP 401"):
            api.send_text("hello")

    def test_send_text_rejects_success_shaped_invalid_response(self):
        response = MagicMock()
        response.read.return_value = b'{"error":"invalid"}'
        api = WhatsAppCloudAPI("secret-token", "123456", "15551234567", opener=MagicMock(return_value=response))
        with self.assertRaisesRegex(WhatsAppError, "unexpected send response"):
            api.send_text("hello")

    def test_rejects_invalid_phone_and_api_version(self):
        with self.assertRaises(ValueError):
            WhatsAppCloudAPI("token", "123", "+15551234567")
        with self.assertRaises(ValueError):
            WhatsAppCloudAPI("token", "123", "15551234567", api_version="latest")


class WhatsAppWebhookTests(unittest.TestCase):
    def test_valid_signature_accepts_owner_text_and_deduplicates_message(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        message = {
            "id": "wamid.1",
            "from": "15551234567",
            "type": "text",
            "text": {"body": "What time is it?"},
        }
        body, signature = signed_body(bridge, message)
        self.assertTrue(bridge.handle_webhook(body, signature))
        self.assertTrue(bridge.handle_webhook(body, signature))
        bridge.handle_owner_message.assert_called_once_with("What time is it?")

    def test_invalid_signature_rejected(self):
        bridge = make_bridge()
        with self.assertRaises(PermissionError):
            bridge.handle_webhook(b"{}", "sha256=wrong")

    def test_other_senders_and_non_text_messages_are_ignored(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge.handle_owner_message = MagicMock()
        for index, message in enumerate((
            {"id": "wamid.other", "from": "15550000000", "text": {"body": "open Safari"}},
            {"id": "wamid.image", "from": "15551234567", "type": "image"},
        )):
            body, signature = signed_body(bridge, message)
            bridge.handle_webhook(body, signature)
        bridge.handle_owner_message.assert_not_called()
        api.send_text.assert_not_called()

    def test_approval_commands_resolve_only_current_code(self):
        api = MagicMock()
        bridge = make_bridge(api)
        from whatsapp_bridge import PendingApproval
        pending = PendingApproval("123456")
        bridge._pending = pending
        bridge._answer_approval("APPROVE 123456")
        self.assertTrue(pending.reply.get_nowait())
        bridge._answer_approval("DENY 000000")
        api.send_text.assert_called_once()

    def test_approval_codes_expire_and_are_unavailable_after_clearing(self):
        api = MagicMock()
        bridge = make_bridge(api)
        bridge._answer_approval("APPROVE 123456")
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
        with patch("whatsapp_bridge.subprocess.Popen", return_value=process):
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
        self.assertIn("APPROVE", api.send_text.call_args_list[0].args[0])
        self.assertEqual(api.send_text.call_args_list[1].args[0], "Message sent after confirmation.")

    def test_webhook_requires_correct_verification_token(self):
        bridge = make_bridge()
        server = create_server(bridge, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        with urlopen(
            f"http://{host}:{port}/webhook?hub.mode=subscribe"
            f"&hub.verify_token={bridge.verify_token}&hub.challenge=test123"
        ) as response:
            self.assertEqual(response.read(), b"test123")
        with self.assertRaises(HTTPError) as error:
            urlopen(
                f"http://{host}:{port}/webhook?hub.mode=subscribe"
                "&hub.verify_token=wrong&hub.challenge=test123"
            )
        self.assertEqual(error.exception.code, 403)
