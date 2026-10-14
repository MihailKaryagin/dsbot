import asyncio
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from ai import AIError, extract_text, generate
from bot import Controls, Every20, FriendlyError
from core import Store, parse_phrases, render_phrase
from runtime import InstanceLock
from smart import detect, insult_level


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.sqlite3"
        self.store = Store(self.path)
        self.store.ensure(10, 1)
        self.store.update(10, enabled=1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_exact_twentieth_and_fortieth(self):
        due = [i for i in range(1, 61) if self.store.tick(10, i)]
        self.assertEqual(due, [20, 40, 60])

    def test_restart_persists_counter_and_settings(self):
        for i in range(1, 14):
            self.store.tick(10, i)
        self.store.update(10, prompt="Мой промпт", phrases=["Привет"])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get(10)["count"], 13)
        self.assertEqual(self.store.get(10)["prompt"], "Мой промпт")
        for i in range(14, 20):
            self.assertIsNone(self.store.tick(10, i))
        self.assertIsNotNone(self.store.tick(10, 20))

    def test_pause_duplicates_and_isolation(self):
        self.store.tick(10, 1)
        self.store.tick(10, 1)
        self.store.tick(10, 0)
        self.assertEqual(self.store.get(10)["count"], 1)
        self.store.update(10, enabled=0)
        self.store.tick(10, 2)
        self.assertEqual(self.store.get(10)["count"], 1)
        self.store.ensure(11, 1)
        self.store.update(11, enabled=1)
        self.store.tick(11, 3)
        self.assertEqual(self.store.get(11)["count"], 1)

    def test_periodic_can_be_disabled_without_disabling_channel(self):
        self.store.update(10, periodic_enabled=False, count=0)
        for i in range(1, 30):
            self.assertIsNone(self.store.tick(10, i))
        self.assertEqual(self.store.get(10)["count"], 0)
        self.assertEqual(self.store.get(10)["last_id"], 29)
        self.store.update(10, periodic_enabled=True)
        self.assertIsNone(self.store.tick(10, 30))
        self.assertEqual(self.store.get(10)["count"], 1)

    def test_phrases_validation_and_no_repeat(self):
        self.assertEqual(parse_phrases(" a\n\na\nb "), ["a", "b"])
        for invalid in ["", "x" * 1801, "\n".join(str(i) for i in range(51))]:
            with self.assertRaises(ValueError):
                parse_phrases(invalid)
        self.assertEqual(render_phrase(["old", "Привет, {user} в {channel}"], "old", "Миша", "чат", "S")[1],
                         "Привет, Миша в чат")
        self.assertEqual(render_phrase(["one"], "one", "U", "C", "S")[1], "one")

    def test_invalid_interval_and_mode(self):
        for number in [0, 10001]:
            with self.assertRaises(ValueError):
                self.store.update(10, every_n=number)
        with self.assertRaises(ValueError):
            self.store.update(10, mode="wrong")

    def test_single_instance_lock(self):
        path = Path(self.tmp.name) / "test.lock"
        with InstanceLock(path):
            with self.assertRaises(RuntimeError):
                with InstanceLock(path):
                    pass
        with InstanceLock(path):
            pass

    def test_new_database_has_no_reply_timing_columns(self):
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(channels)")}
        self.assertNotIn("smart_cooldown", columns)
        self.assertNotIn("last_reply_at", columns)
        self.assertNotIn("reply_window_count", columns)

    def test_migrates_legacy_smart_settings(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute('''CREATE TABLE channels (
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0, every_n INTEGER NOT NULL DEFAULT 20,
            mode TEXT NOT NULL DEFAULT 'phrases', phrases TEXT NOT NULL,
            prompt TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            last_id INTEGER NOT NULL DEFAULT 0, last_phrase TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
            smart INTEGER DEFAULT 0, mentions INTEGER DEFAULT 1,
            activity TEXT DEFAULT 'medium', cooldown INTEGER DEFAULT 60
        )''')
        connection.execute("INSERT INTO channels (channel_id,guild_id,phrases,prompt,smart,mentions,activity,cooldown) "
                           "VALUES (10,1,'[\"Привет\"]','Старый промпт',1,0,'high',90)")
        connection.commit()
        connection.close()
        migrated = Store(legacy_path)
        try:
            c = migrated.get(10)
            self.assertEqual((c["smart_enabled"], c["smart_mentions"], c["smart_activity"]),
                             (1, 0, "high"))
            self.assertEqual((c["prompt"], c["phrases"]), ("Старый промпт", ["Привет"]))
        finally:
            migrated.close()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")
        self.store.ensure(10, 1)
        self.store.update(10, enabled=1, phrases=["@everyone привет"])
        self.bot = Every20(self.store)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.name = "chat"
        self.channel.send = AsyncMock()
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)

    async def asyncTearDown(self):
        await self.bot.close()
        self.store.close()
        self.tmp.cleanup()

    def message(self, message_id, **overrides):
        args = dict(id=message_id, channel=self.channel, guild=SimpleNamespace(id=1, name="server", me=object()),
                    author=SimpleNamespace(bot=False, display_name="Миша"), webhook_id=None,
                    type=discord.MessageType.default, clean_content="Привет")
        args.update(overrides)
        return SimpleNamespace(**args)

    async def test_real_handler_counts_and_disables_mentions(self):
        for i in range(1, 41):
            await self.bot.on_message(self.message(i))
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(self.channel.send.call_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})

    async def test_ignores_bots_webhooks_dm_system_and_threads(self):
        for overrides in [dict(author=SimpleNamespace(bot=True)), dict(webhook_id=1), dict(guild=None),
                          dict(type=discord.MessageType.pins_add), dict(channel=MagicMock(spec=discord.Thread))]:
            await self.bot.on_message(self.message(1, **overrides))
        self.assertEqual(self.store.get(10)["count"], 0)

    async def test_unknown_channel_disabled_and_wrong_guild(self):
        self.channel.id = 99
        await self.bot.on_message(self.message(1))
        self.channel.id = 10
        self.store.update(10, enabled=0)
        await self.bot.on_message(self.message(2))
        self.store.update(10, enabled=1)
        self.bot.target_guild = 2
        await self.bot.on_message(self.message(3))
        self.assertEqual(self.store.get(10)["count"], 0)

    async def test_concurrent_messages_one_reply_per_cycle(self):
        await asyncio.gather(*(self.bot.on_message(self.message(i)) for i in range(1, 101)))
        self.assertEqual(self.channel.send.await_count, 5)

    async def test_permission_and_http_failures_do_not_crash(self):
        self.store.update(10, every_n=1)
        self.channel.permissions_for.return_value.send_messages = False
        await self.bot.on_message(self.message(1))
        self.channel.send.assert_not_awaited()
        self.assertIn("Send Messages", self.store.get(10)["last_error"])
        self.channel.permissions_for.return_value.send_messages = True
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        await self.bot.on_message(self.message(2))
        self.assertIn("403", self.store.get(10)["last_error"])

    async def test_ai_failure_uses_fallback(self):
        self.bot.ai_key = "fake"
        self.bot.content_enabled = True
        self.store.update(10, mode="ai", every_n=1)
        with patch("bot.generate", new=AsyncMock(side_effect=AIError("таймаут"))):
            await self.bot.on_message(self.message(1))
        self.assertEqual(self.channel.send.call_args.args[0], "@everyone привет")
        self.assertEqual(self.store.get(10)["last_error"], "таймаут")

    async def test_pause_during_generation_cancels_send(self):
        self.store.update(10, every_n=1)
        async def reply(*args, **kwargs):
            self.store.update(10, enabled=0)
            return "Не отправляй", "", ""
        self.bot.make_reply = reply
        await self.bot.on_message(self.message(1))
        self.channel.send.assert_not_awaited()

    async def test_management_permission_check(self):
        interaction = SimpleNamespace(guild=SimpleNamespace(id=1), channel=self.channel,
                                      user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)))
        controls = Controls(self.bot)
        with self.assertRaises(FriendlyError):
            await controls.interaction_check(interaction)
        interaction.user.guild_permissions.manage_guild = True
        self.assertTrue(await controls.interaction_check(interaction))

    async def test_slash_commands_serialize(self):
        payload = self.bot.tree.get_command("bot").to_dict(self.bot.tree)
        self.assertEqual({item["name"] for item in payload["options"]},
                         {"setup", "phrases", "prompt", "mode", "every", "pause", "resume", "status", "test", "reset",
                          "smart", "smart_settings", "diagnose", "periodic"})
        self.assertTrue(payload["default_member_permissions"])
        settings = next(item for item in payload["options"] if item["name"] == "smart_settings")
        self.assertNotIn("cooldown", {option["name"] for option in settings["options"]})
        self.assertFalse(self.bot.tree.get_command("bot").get_command("test").checks)

    async def test_smart_insults_are_not_time_limited_even_with_legacy_columns(self):
        self.bot.ai_key = "fake"
        self.bot.content_enabled = True
        self.store.update(10, mode="ai", smart_enabled=True, smart_activity="high", periodic_enabled=False)
        self.bot._connection.user = SimpleNamespace(id=999, name="Бот", display_name="Бот")
        for statement in (
            "ALTER TABLE channels ADD COLUMN smart_cooldown INTEGER DEFAULT 10",
            "ALTER TABLE channels ADD COLUMN last_reply_at REAL DEFAULT 0",
            "ALTER TABLE channels ADD COLUMN reply_window_start REAL DEFAULT 0",
            "ALTER TABLE channels ADD COLUMN reply_window_count INTEGER DEFAULT 0",
        ):
            self.store.db.execute(statement)
        self.store.db.execute("UPDATE channels SET smart_cooldown=10, last_reply_at=9999999999, "
                              "reply_window_start=9999999999, reply_window_count=6 WHERE channel_id=10")
        self.store.db.commit()
        with patch("bot.generate", new=AsyncMock(return_value="Без личных оскорблений.")):
            with self.assertLogs("every20", level="INFO") as captured:
                for i, phrase in enumerate(("пидорас", "долбаеб", "ты идиот", "дура",
                                            "кирик ты пидор", "пидорас", "долбаеб"), start=1):
                    await self.bot.on_message(self.message(i, clean_content=phrase))
        self.assertEqual(self.channel.send.await_count, 7)
        self.assertEqual(self.channel.send.call_args.args[0], "Без личных оскорблений.")
        self.assertIn("strong_insult", captured.output[0])
        self.assertEqual(len(captured.output), 7)
        self.assertNotIn("cooldown", " ".join(captured.output))
        self.assertNotIn("пидорас", " ".join(captured.output))
        self.assertNotIn("долбаеб", " ".join(captured.output))

    async def test_smart_mild_and_direct_conflict(self):
        self.bot.ai_key = "fake"
        self.bot.content_enabled = True
        self.store.update(10, mode="ai", smart_enabled=True, smart_activity="high", periodic_enabled=False)
        with patch("bot.generate", new=AsyncMock(return_value="Давайте спокойнее.")):
            await self.bot.on_message(self.message(1, clean_content="Обычный разговор"))
            self.channel.send.assert_not_awaited()
            await self.bot.on_message(self.message(2, clean_content="дура"))
            self.assertEqual(self.bot.last_decisions[10], "mild_insult_high_activity")
            await self.bot.on_message(self.message(3, clean_content="кирик ты пидор"))
            self.assertEqual(self.bot.last_decisions[10], "direct_insult")
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_direct_bot_mention_without_insult(self):
        self.bot.ai_key = "fake"
        self.bot.content_enabled = True
        self.bot._connection.user = SimpleNamespace(id=999, name="Бот", display_name="Бот")
        self.store.update(10, mode="ai", smart_enabled=True, periodic_enabled=False)
        with patch("bot.generate", new=AsyncMock(return_value="Я здесь.")):
            await self.bot.on_message(self.message(1, clean_content="Привет", mentions=[SimpleNamespace(id=999)]))
        self.assertEqual(self.channel.send.call_args.args[0], "Я здесь.")
        self.assertEqual(self.bot.last_decisions[10], "bot_mention")


class SmartTests(unittest.TestCase):
    def test_reported_phrases(self):
        for phrase in ("пидорас", "долбаеб", "дура", "кирик ты пидор"):
            with self.subTest(phrase=phrase):
                self.assertTrue(detect(phrase, activity="high").trigger)

    def test_variants_and_context(self):
        self.assertEqual(insult_level("ДОЛБОЁБ"), "strong")
        self.assertEqual(insult_level("пидoр"), "strong")
        self.assertEqual(detect("дура", activity="low").reason, "ambiguous_insult_no_target")
        self.assertEqual(detect("ты дура", activity="low").reason, "direct_insult")
        self.assertEqual(detect("дура", activity="low", author_id=2,
                                recent=[{"author_id": 1, "text": "ты идиот"}]).reason,
                         "insult_in_conflict_context")
        self.assertEqual(detect("Слово «пидор» — оскорбление", activity="high").reason,
                         "quoted_or_discussion")
        self.assertEqual(detect("вот дуракская задачка", activity="high").reason, "no_signal")

    def test_mentions_options_and_hostile_replies(self):
        self.assertEqual(detect("Привет", bot_mentioned=True).kind, "mention")
        self.assertEqual(detect("Привет", bot_mentioned=True, mentions=False).reason, "no_signal")
        self.assertEqual(detect("Привет, Бот", name=True, bot_names=("Бот",)).kind, "name")
        self.assertEqual(detect("заткнись", replied_to_other=True).reason, "hostile_reply")
        self.assertEqual(detect("ты дура", arguments=False).reason, "arguments_disabled")


class AITests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_response(self):
        data = {"output": [{"type": "reasoning"},
                           {"type": "message", "content": [{"type": "output_text", "text": "Привет"}]}]}
        self.assertEqual(extract_text(data), "Привет")
        self.assertEqual(extract_text({"output_text": "Привет ещё раз"}), "Привет ещё раз")
        with self.assertRaises(AIError):
            extract_text({"output": []})

    async def test_successful_codex_sale_response(self):
        session = MagicMock()
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={"output": [{"type": "message", "content":
                                                              [{"type": "output_text", "text": "Короткий ответ"}]}]})
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        answer = await generate(session, "fake-key", "gpt-6.1-sol", "Пиши кратко", [{"text": "Привет"}],
                                trigger="argument")
        self.assertEqual(answer, "Короткий ответ")
        payload = session.post.call_args.kwargs["json"]
        self.assertIn("конфликт", payload["input"])
        self.assertEqual(payload["model"], "gpt-6.1-sol")

    async def test_http_error_and_request_shape(self):
        session = MagicMock()
        response = MagicMock(status=429)
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        with self.assertRaisesRegex(AIError, "429"):
            await generate(session, "fake-key", "gpt-4.1-mini", "Пиши кратко", [])
        payload = session.post.call_args.kwargs["json"]
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_output_tokens"], 800)
        self.assertEqual(session.post.call_args.args[0], "https://codex.sale/v1/responses")
        self.assertFalse(payload["stream"])

    async def test_timeout_fallback(self):
        session = MagicMock()
        session.post.return_value.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError)
        with self.assertRaises(AIError):
            await generate(session, "fake-key", "model", "prompt", [])


if __name__ == "__main__":
    unittest.main()
