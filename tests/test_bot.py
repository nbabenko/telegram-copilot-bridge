import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"


os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("BOT_USERNAME", "dharmaedit_bot")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("REPO_PATH", tempfile.gettempdir())
os.environ.setdefault("UPLOAD_DIR", str(Path(tempfile.gettempdir()) / "telegram-copilot-uploads"))
os.environ.setdefault("COPILOT_BIN", "/bin/true")
os.environ.setdefault("COPILOT_TIMEOUT", "5")
os.environ.setdefault("TELEGRAM_TIMEOUT", "1")

spec = importlib.util.spec_from_file_location("telegram_bridge_bot", BOT_PATH)
bot = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(bot)


class DummyProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class BotTests(unittest.TestCase):
    def setUp(self):
        bot.ACTIVE_REQUESTS.clear()

    def tearDown(self):
        bot.ACTIVE_REQUESTS.clear()

    def test_busy_lock_text_points_to_cancel_and_debug(self):
        text = bot.busy_lock_text()
        self.assertIn("/cancel", text)
        self.assertIn("/debug", text)
        self.assertNotIn("не 10 рук", text)

    def test_describe_attachment_includes_video_size(self):
        attachment = bot.describe_attachment(
            {
                "video": {
                    "file_id": "abc123",
                    "file_name": "clip.mp4",
                    "file_size": 12345,
                }
            }
        )

        self.assertEqual(attachment["file_id"], "abc123")
        self.assertEqual(attachment["preferred_name"], "clip.mp4")
        self.assertEqual(attachment["file_size"], 12345)
        self.assertEqual(attachment["kind"], "video")

    def test_download_telegram_file_rejects_oversize_media_before_api_call(self):
        with self.assertRaises(RuntimeError) as error:
            bot.download_telegram_file("file-id", "large.mp4", bot.TELEGRAM_DOWNLOAD_MAX_BYTES + 1)

        self.assertIn("larger than 20 MB", str(error.exception))

    def test_cancel_active_request_marks_request_and_terminates_process(self):
        process = DummyProcess()
        bot.ACTIVE_REQUESTS[1] = {
            "chat_id": 100,
            "message": {},
            "raw_blocks": [],
            "debug_enabled": False,
            "process": process,
            "cancel_requested": False,
        }

        request = bot.cancel_active_request(1)

        self.assertIsNotNone(request)
        self.assertTrue(request["cancel_requested"])
        self.assertTrue(process.terminated)

    def test_handle_message_cancel_active_request(self):
        process = DummyProcess()
        bot.ACTIVE_REQUESTS[1] = {
            "chat_id": 100,
            "message": {},
            "raw_blocks": [],
            "debug_enabled": False,
            "process": process,
            "cancel_requested": False,
        }
        state = {"sessions": {"1": {"has_session": True}}}
        message = {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 1},
            "message_id": 50,
            "text": "/cancel",
        }
        sent = []

        with patch.object(bot, "send_message", side_effect=lambda *args, **kwargs: sent.append(args[1])):
            with patch.object(bot, "send_group_done_ack") as done_ack:
                bot.handle_message(message, state)

        self.assertTrue(process.terminated)
        self.assertTrue(bot.ACTIVE_REQUESTS[1]["cancel_requested"])
        self.assertIn("Stopping the active request", sent[-1])
        done_ack.assert_not_called()

    def test_process_copilot_request_resets_session_on_failure(self):
        state = {"sessions": {"1": {"has_session": True}}}
        message = {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 1},
            "message_id": 51,
            "text": "test",
        }
        sent = []

        with patch.object(bot, "send_typing"):
            with patch.object(bot, "send_message", side_effect=lambda *args, **kwargs: sent.append(args[1])):
                with patch.object(bot, "send_group_done_ack"):
                    with patch.object(bot, "set_latest_debug_trace"):
                        with patch.object(bot, "stream_copilot", return_value=(False, False, "Exit code 1")):
                            bot.process_copilot_request(state, message, "prompt", True, 1, 100)

        self.assertFalse(state["sessions"]["1"]["has_session"])
        self.assertEqual(sent[0], "Working...")
        self.assertIn("The task failed", sent[-1])

    def test_process_copilot_request_reports_cancel_and_resets_session(self):
        state = {"sessions": {"1": {"has_session": True}}}
        message = {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 1},
            "message_id": 52,
            "text": "test",
        }
        sent = []

        with patch.object(bot, "send_typing"):
            with patch.object(bot, "send_message", side_effect=lambda *args, **kwargs: sent.append(args[1])):
                with patch.object(bot, "send_group_done_ack"):
                    with patch.object(bot, "set_latest_debug_trace"):
                        with patch.object(bot, "stream_copilot", return_value=(False, False, "Request was cancelled.")):
                            bot.process_copilot_request(state, message, "prompt", True, 1, 100)

        self.assertFalse(state["sessions"]["1"]["has_session"])
        self.assertEqual(sent[0], "Working...")
        self.assertIn("Cancelled the active request", sent[-1])


if __name__ == "__main__":
    unittest.main()
