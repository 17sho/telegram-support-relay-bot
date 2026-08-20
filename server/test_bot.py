import importlib.util
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tg-relay-test-")

spec = importlib.util.spec_from_file_location("relay_bot", Path(__file__).with_name("bot.py"))
relay_bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = relay_bot
spec.loader.exec_module(relay_bot)


def message(**overrides):
    values = {
        "text": None,
        "caption": None,
        "photo": None,
        "video": None,
        "voice": None,
        "audio": None,
        "document": None,
        "sticker": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class MessageKindTests(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(relay_bot.message_kind(message(text="hello")), "text")

    def test_captioned_photo_remains_photo(self):
        self.assertEqual(relay_bot.message_kind(message(photo=[object()], caption="说明")), "photo")

    def test_captioned_video_remains_video(self):
        self.assertEqual(relay_bot.message_kind(message(video=object(), caption="说明")), "video")

    def test_captioned_document_remains_document(self):
        self.assertEqual(relay_bot.message_kind(message(document=object(), caption="说明")), "document")

    def test_captioned_audio_remains_audio(self):
        self.assertEqual(relay_bot.message_kind(message(audio=object(), caption="说明")), "audio")


class InputValidationTests(unittest.TestCase):
    def test_parse_valid_user_id(self):
        self.assertEqual(relay_bot.parse_user_id(["123"]), 123)

    def test_parse_invalid_user_ids(self):
        for args in ([], ["abc"], ["0"], ["-1"], ["1", "2"]):
            with self.subTest(args=args):
                self.assertIsNone(relay_bot.parse_user_id(args))


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        relay_bot.init_db()
        with relay_bot.db() as conn:
            conn.execute("delete from rate_limits")

    def test_only_first_excess_message_notifies(self):
        old_limit = relay_bot.MESSAGES_PER_MINUTE
        relay_bot.MESSAGES_PER_MINUTE = 2
        try:
            self.assertEqual(relay_bot.consume_rate_limit(99), (True, False))
            self.assertEqual(relay_bot.consume_rate_limit(99), (True, False))
            self.assertEqual(relay_bot.consume_rate_limit(99), (False, True))
            self.assertEqual(relay_bot.consume_rate_limit(99), (False, False))
        finally:
            relay_bot.MESSAGES_PER_MINUTE = old_limit


class ChallengeTests(unittest.TestCase):
    def setUp(self):
        relay_bot.init_db()

    def test_callback_does_not_expose_visible_answer(self):
        keyboard, token = relay_bot.challenge_keyboard(42, "🍎", ["🍎", "🍋", "⭐", "🌸"])
        correct_button = next(button for row in keyboard.inline_keyboard for button in row if button.text == "🍎")
        self.assertNotIn("🍎", correct_button.callback_data)
        self.assertTrue(correct_button.callback_data.endswith(token))

    def test_send_challenge_random_pool_has_only_requested_five_types(self):
        class Message:
            def __init__(self):
                self.reply_photo = AsyncMock()
                self.reply_text = AsyncMock()

        original_choice = relay_bot.random.choice
        try:
            for challenge_type in ["image", "emoji_count", "shape_count", "odd_emoji", "emoji_sequence"]:
                message = Message()
                relay_bot.random.choice = lambda seq, t=challenge_type: t if set(seq) == {"image", "emoji_count", "shape_count", "odd_emoji", "emoji_sequence"} else original_choice(seq)
                asyncio.run(relay_bot.send_challenge(message, 775))
                if challenge_type == "image":
                    message.reply_photo.assert_awaited_once()
                    message.reply_text.assert_not_awaited()
                else:
                    message.reply_text.assert_awaited_once()
                    message.reply_photo.assert_not_awaited()
        finally:
            relay_bot.random.choice = original_choice

    def test_image_captcha_has_six_unique_choices_refresh_and_png(self):
        image, keyboard = relay_bot.create_image_challenge(776)
        self.assertEqual(image.read(8), b"\x89PNG\r\n\x1a\n")
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        choice_buttons = [button for button in buttons if button.callback_data.startswith("verify:")]
        self.assertEqual(len(choice_buttons), 6)
        self.assertEqual(len({button.text for button in choice_buttons}), 6)
        self.assertTrue(all(len(button.text) == 5 for button in choice_buttons))
        self.assertTrue(all(button.text not in button.callback_data for button in choice_buttons))
        self.assertEqual(buttons[-1].callback_data, "refreshverify:776")
        stored = relay_bot.get_user_status(776)["challenge_answer"]
        self.assertEqual(sum(button.callback_data.endswith(stored) for button in choice_buttons), 1)

    def test_all_challenge_types_generate_eight_choices_and_token(self):
        types = ["add3", "mixed", "sequence", "emoji_count", "odd_emoji", "shape_count", "emoji_sequence"]
        original_choice = relay_bot.random.choice
        try:
            for challenge_type in types:
                with self.subTest(challenge_type=challenge_type):
                    relay_bot.random.choice = lambda seq, t=challenge_type: t if t in seq else original_choice(seq)
                    question, keyboard = relay_bot.create_challenge(777, challenge_type)
                    buttons = [button for row in keyboard.inline_keyboard for button in row]
                    with relay_bot.db() as conn:
                        stored = conn.execute("select challenge_answer from user_status where user_id=777").fetchone()[0]
                    self.assertTrue(question)
                    self.assertEqual(len(buttons), 8)
                    self.assertEqual(sum(button.callback_data.endswith(stored) for button in buttons), 1)
        finally:
            relay_bot.random.choice = original_choice


class VerificationUnlockTests(unittest.TestCase):
    def setUp(self):
        relay_bot.init_db()
        with relay_bot.db() as conn:
            conn.execute("delete from verification_attempts where user_id=888")
            conn.execute("delete from user_status where user_id=888")

    def test_locked_contact_has_unlock_button_and_reset_clears_lock(self):
        relay_bot.record_verification_failure(888)
        relay_bot.record_verification_failure(888)
        self.assertTrue(relay_bot.is_verification_locked(888))
        lock = relay_bot.verification_lock_until(888)
        remaining = (lock - datetime.now()).total_seconds()
        self.assertGreater(remaining, 5.9 * 3600)
        self.assertLessEqual(remaining, 6 * 3600)
        self.assertIn("暂停验证6小时", relay_bot.lock_message(lock))
        keyboard = relay_bot.user_keyboard(888)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("🔓 解除验证锁定", labels)
        relay_bot.reset_verification_failures(888)
        relay_bot.force_reverify(888)
        self.assertFalse(relay_bot.is_verification_locked(888))
        keyboard = relay_bot.user_keyboard(888)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertNotIn("🔓 解除验证锁定", labels)


class VerificationLockNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_notification_sends_rescue_card_to_admin(self):
        user = SimpleNamespace(id=889, username="tester", first_name="Test", last_name="User")
        relay_bot.upsert_user(user)
        relay_bot.record_verification_failure(user.id)
        lock = relay_bot.record_verification_failure(user.id)
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)))
        context = SimpleNamespace(bot=bot)
        await relay_bot.notify_admin_verification_lock(context, user.id, lock)
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], relay_bot.ADMIN_ID)
        self.assertIn("联系人验证已锁定", args[1])
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("🔓 解除验证锁定", labels)
        with relay_bot.db() as conn:
            saved = conn.execute("select user_id from admin_message_map where admin_chat_id=? and admin_message_id=?", (relay_bot.ADMIN_ID, 456)).fetchone()
        self.assertEqual(saved["user_id"], user.id)


class DatabaseMaintenanceTests(unittest.TestCase):
    def setUp(self):
        relay_bot.init_db()

    def test_asyncio_is_available_for_cleanup_callback(self):
        self.assertTrue(callable(relay_bot.asyncio.to_thread))

    def test_database_has_busy_timeout_and_message_indexes(self):
        with relay_bot.db() as conn:
            self.assertEqual(conn.execute("pragma busy_timeout").fetchone()[0], 10000)
            indexes = {row["name"] for row in conn.execute("pragma index_list(messages)")}
        self.assertTrue({"idx_messages_user_id", "idx_messages_user_direction_id", "idx_messages_created_at"}.issubset(indexes))

    def test_cleanup_scope_rejects_forged_callback_values(self):
        self.assertIsNone(relay_bot.parse_cleanup_scope("all"))
        for valid in (30, 90, 180):
            self.assertEqual(relay_bot.parse_cleanup_scope(str(valid)), valid)
        for invalid in ("0", "1", "365", "abc", "-30"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                relay_bot.parse_cleanup_scope(invalid)

    def test_clear_history_preserves_routing_contacts_and_settings(self):
        with relay_bot.db() as conn:
            conn.execute("delete from messages")
            conn.execute("delete from admin_message_map")
            conn.execute("delete from users")
            conn.execute("delete from user_status")
            conn.execute("insert into users(user_id,first_name,updated_at) values(700,'A',?)", (relay_bot.now(),))
            conn.execute("insert into user_status(user_id,verified,blocked,updated_at) values(700,1,0,?)", (relay_bot.now(),))
            conn.execute("insert into admin_message_map values(1,900,700,?)", (relay_bot.now(),))
        relay_bot.save_message(700, "user", "text", "temporary history")
        self.assertEqual(relay_bot.clear_message_history(None), 1)
        with relay_bot.db() as conn:
            self.assertEqual(conn.execute("select count(*) from messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("select count(*) from users where user_id=700").fetchone()[0], 1)
            self.assertEqual(conn.execute("select count(*) from user_status where user_id=700").fetchone()[0], 1)
            self.assertEqual(conn.execute("select user_id from admin_message_map where admin_message_id=900").fetchone()[0], 700)

    def test_delivery_columns_exist(self):
        with relay_bot.db() as conn:
            columns = {row["name"] for row in conn.execute("pragma table_info(messages)")}
        self.assertIn("delivery_status", columns)
        self.assertIn("delivered_count", columns)
        self.assertIn("retracted_at", columns)

    def test_cleanup_removes_only_expired_transient_rows(self):
        old = "2000-01-01 00:00:00"
        fresh = relay_bot.now()
        with relay_bot.db() as conn:
            conn.execute("delete from rate_limits")
            conn.execute("delete from verification_attempts")
            conn.execute("delete from admin_message_map")
            conn.execute("insert into rate_limits values(1,?,1,0)", (old,))
            conn.execute("insert into rate_limits values(2,?,1,0)", (fresh,))
            conn.execute("insert into verification_attempts values(1,1,null,?)", (old,))
            conn.execute("insert into verification_attempts values(2,1,null,?)", (fresh,))
            conn.execute("insert into admin_message_map values(1,101,10,?)", (old,))
            conn.execute("insert into admin_message_map values(1,102,10,?)", (fresh,))
        relay_bot.cleanup_transient_data()
        with relay_bot.db() as conn:
            rate_ids = {row[0] for row in conn.execute("select user_id from rate_limits")}
            verify_ids = {row[0] for row in conn.execute("select user_id from verification_attempts")}
            mapping_ids = {row[0] for row in conn.execute("select admin_message_id from admin_message_map")}
        self.assertEqual(rate_ids, {2})
        self.assertEqual(verify_ids, {2})
        self.assertEqual(mapping_ids, {101, 102})

    def test_delivery_label_only_marks_non_delivered_user_messages(self):
        def row(direction, status):
            return {"direction": direction, "delivery_status": status}
        self.assertEqual(relay_bot.delivery_label(row("user", "pending")), " · ⏳ 待通知")
        self.assertEqual(relay_bot.delivery_label(row("user", "failed")), " · ⚠️ 通知失败")
        self.assertEqual(relay_bot.delivery_label(row("user", "delivered")), "")
        self.assertEqual(relay_bot.delivery_label(row("admin", "failed")), "")

    def test_retracted_message_label(self):
        row = {"direction": "admin", "delivery_status": "delivered", "retracted_at": relay_bot.now()}
        self.assertEqual(relay_bot.delivery_label(row), " · 🗑 已撤回")


if __name__ == "__main__":
    unittest.main()
