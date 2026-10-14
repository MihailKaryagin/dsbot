from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from ai import AIError, generate
from core import Store, parse_phrases, render_phrase
from runtime import InstanceLock
from smart import detect, insult_level

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("every20")


class FriendlyError(app_commands.AppCommandError):
    pass


async def private(interaction, text):
    kwargs = dict(ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if interaction.response.is_done():
        await interaction.followup.send(text, **kwargs)
    else:
        await interaction.response.send_message(text, **kwargs)


class Every20(discord.Client):
    def __init__(self, store, guild_id=0, ai_key="", model="gpt-6.1-sol", content_enabled=False,
                 base_url="https://codex.sale/v1"):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = content_enabled
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.store = store
        self.target_guild = guild_id
        self.ai_key = ai_key
        self.model = model
        self.content_enabled = content_enabled
        self.base_url = base_url
        self.session = None
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(Controls(self))
        self.tree.on_error = self.command_error
        self.locks = defaultdict(asyncio.Lock)
        self.context = defaultdict(lambda: deque(maxlen=20))
        self.ai_slots = asyncio.Semaphore(3)
        self.last_decisions = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        if self.target_guild:
            guild = discord.Object(id=self.target_guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("Slash-команды /bot зарегистрированы.")

    async def on_ready(self):
        log.info("ГОТОВ: %s. Открой Discord и выполни /bot setup в нужном канале.", self.user)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def command_error(self, interaction, error):
        if isinstance(error, (FriendlyError, app_commands.CheckFailure)):
            message = str(error)
        else:
            log.error("Ошибка команды: %s", type(getattr(error, "original", error)).__name__)
            message = "Не удалось выполнить команду. Проверь права бота и logs/bot.log."
        await private(interaction, message)

    async def on_error(self, event_method, *args, **kwargs):
        log.exception("Ошибка события %s", event_method)

    async def make_reply(self, config, user, channel, server, context, trigger="periodic"):
        template, fallback = render_phrase(config["phrases"], config["last_phrase"], user, channel, server)
        if config["mode"] == "phrases":
            return fallback, template, ""
        if not self.ai_key or not self.content_enabled:
            return fallback, template, "ИИ не настроен: нужны ключ API и ENABLE_MESSAGE_CONTENT=true."
        try:
            async with self.ai_slots:
                answer = await generate(self.session, self.ai_key, self.model, config["prompt"], context,
                                        base_url=self.base_url, trigger=trigger)
            return answer, "", ""
        except AIError as exc:
            return fallback, template, str(exc)

    async def on_message(self, message):
        if (message.guild is None or message.author.bot or message.webhook_id
                or not isinstance(message.channel, discord.TextChannel)
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)
                or (self.target_guild and message.guild.id != self.target_guild)):
            return
        channel_id = message.channel.id
        config = self.store.get(channel_id)
        if not config:
            return
        if not config["enabled"]:
            self.record_decision(channel_id, message.id, "channel_paused")
            return
        async with self.locks[channel_id]:
            config = self.store.get(channel_id)
            if not config["enabled"]:
                self.record_decision(channel_id, message.id, "channel_paused")
                return
            if message.id <= config["last_id"]:
                self.record_decision(channel_id, message.id, "duplicate_or_old_message")
                return
            author_id = getattr(message.author, "id", 0)
            content = (getattr(message, "clean_content", "") or getattr(message, "content", "") or "")[:600]
            prior = list(self.context[channel_id])
            decision = None
            if config["smart_enabled"] and config["mode"] == "ai" and self.content_enabled:
                bot_id = getattr(self.user, "id", None)
                mentioned = {getattr(user, "id", None) for user in (getattr(message, "mentions", None) or [])}
                resolved = getattr(getattr(message, "reference", None), "resolved", None)
                replied_id = getattr(getattr(resolved, "author", None), "id", None)
                names = tuple(n for n in (getattr(self.user, "name", ""),
                                          getattr(self.user, "display_name", "")) if n)
                decision = detect(content,
                                  bot_mentioned=bot_id is not None and bot_id in mentioned,
                                  replied_to_bot=bot_id is not None and replied_id == bot_id,
                                  other_mentioned=any(i not in {None, bot_id, author_id} for i in mentioned),
                                  replied_to_other=replied_id not in {None, bot_id, author_id},
                                  bot_names=names, recent=prior, author_id=author_id,
                                  mentions=bool(config["smart_mentions"]),
                                  arguments=bool(config["smart_arguments"]),
                                  name=bool(config["smart_name"]), activity=config["smart_activity"])
            if config["mode"] == "ai" and self.content_enabled:
                self.context[channel_id].append({"author": message.author.display_name[:80],
                                                 "author_id": author_id, "text": content or "[вложение]",
                                                 "hostile": bool(insult_level(content)) and
                                                 (decision is None or decision.reason != "quoted_or_discussion")})
            due = self.store.tick(channel_id, message.id)
            smart_trigger = bool(decision and decision.trigger)
            if due is None and not smart_trigger:
                if config["smart_enabled"]:
                    reason = ("mode_is_phrases" if config["mode"] != "ai" else
                              "message_content_disabled" if not self.content_enabled else
                              "empty_content_or_intent" if not content and not getattr(message, "attachments", None) else
                              decision.reason if decision else "no_signal")
                    self.record_decision(channel_id, message.id, reason)
                return
            trigger = decision.kind if smart_trigger else "periodic"
            permissions = message.channel.permissions_for(message.guild.me)
            if not (permissions.view_channel and permissions.send_messages):
                self.store.update(channel_id, last_error="Нет View Channel / Send Messages в этом канале.")
                self.record_decision(channel_id, message.id, "missing_send_permission", trigger)
                return
            text, template, error = await self.make_reply(
                config, message.author.display_name, message.channel.name, message.guild.name,
                [{"author": item["author"], "text": item["text"]} for item in self.context[channel_id]],
                trigger=trigger)
            current = self.store.get(channel_id)
            # A pause/settings change during generation cancels the old answer.
            if not current["enabled"] or current["revision"] != config["revision"]:
                self.record_decision(channel_id, message.id, "settings_changed_during_generation", trigger)
                return
            try:
                await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
                self.store.update(channel_id, last_phrase=template, last_error=error)
                self.record_decision(channel_id, message.id,
                                     "sent_fallback" if error else (decision.reason if smart_trigger else "periodic_sent"),
                                     trigger)
                if error:
                    log.warning("Канал %s: %s", channel_id, error)
            except discord.HTTPException as exc:
                self.store.update(channel_id, last_error=f"Discord HTTP {exc.status}; цикл пропущен.")
                self.record_decision(channel_id, message.id, "discord_send_http_error", trigger)
                log.warning("Отправка: канал %s, HTTP %s", channel_id, exc.status)

    def record_decision(self, channel_id, message_id, reason, trigger="none"):
        """Log identifiers and reason codes only; never raw chat text or credentials."""
        self.last_decisions[channel_id] = reason
        log.info("SMART channel=%s message=%s decision=%s trigger=%s", channel_id, message_id, reason, trigger)


class SettingsModal(discord.ui.Modal):
    def __init__(self, controls, interaction, field):
        super().__init__(title="Список ответов" if field == "phrases" else "Промпт для ИИ", timeout=300)
        self.controls = controls
        self.channel_id = interaction.channel_id
        self.owner_id = interaction.user.id
        self.field = field
        config = controls.config(interaction)
        self.value_input = discord.ui.TextInput(
            label="Каждая фраза с новой строки" if field == "phrases" else "Как бот должен отвечать?",
            style=discord.TextStyle.paragraph, max_length=4000, min_length=1,
            default="\n".join(config["phrases"]) if field == "phrases" else config["prompt"])
        self.add_item(self.value_input)

    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id or interaction.channel_id != self.channel_id:
            await private(interaction, "Эта форма открыта другим пользователем или в другом канале.")
            return
        try:
            await self.controls.interaction_check(interaction)
            value = self.value_input.value.strip()
            if self.field == "phrases":
                value = parse_phrases(value)
            elif not value:
                raise ValueError("Промпт не должен быть пустым.")
            self.controls.bot.store.update(self.channel_id, **{self.field: value})
            await private(interaction, "Сохранено. " + ("Режим меняется через /bot mode." if self.field == "prompt" else ""))
        except (ValueError, FriendlyError) as exc:
            await private(interaction, str(exc))

    async def on_error(self, interaction, error):
        log.error("Ошибка формы: %s", type(error).__name__)
        await private(interaction, "Не удалось сохранить настройки. Проверь logs/bot.log.")


class Controls(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="bot", description="Настройки ответов в текущем канале",
                         guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
        self.bot = bot

    async def interaction_check(self, interaction):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            raise FriendlyError("Используй команду в обычном текстовом канале сервера, не в ветке/ЛС.")
        if self.bot.target_guild and interaction.guild.id != self.bot.target_guild:
            raise FriendlyError("Этот сервер не совпадает с DISCORD_GUILD_ID в .env.")
        if not interaction.user.guild_permissions.manage_guild:
            raise FriendlyError("Нужно право «Управление сервером» или «Администратор».")
        return True

    def config(self, interaction):
        return self.bot.store.ensure(interaction.channel_id, interaction.guild_id)

    @app_commands.command(description="Включить бота в этом канале с интервалом N сообщений")
    @app_commands.describe(every="Сколько сообщений между ответами, по умолчанию 20")
    async def setup(self, interaction: discord.Interaction, every: app_commands.Range[int, 1, 10000] = 20):
        permissions = interaction.channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages):
            raise FriendlyError("Дай боту View Channel и Send Messages в этом канале.")
        self.config(interaction)
        self.bot.store.update(interaction.channel_id, enabled=1, every_n=every, count=0, last_error="")
        await private(interaction, f"Включено: ответ на каждое {every}-е сообщение. /bot phrases — изменить фразы.")

    @app_commands.command(description="Задать готовые ответы: одна фраза на строку")
    async def phrases(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SettingsModal(self, interaction, "phrases"))

    @app_commands.command(description="Задать инструкцию для ИИ через форму")
    async def prompt(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SettingsModal(self, interaction, "prompt"))

    @app_commands.command(description="Выбрать готовые фразы или ИИ по промпту")
    async def mode(self, interaction: discord.Interaction, value: Literal["phrases", "ai"]):
        if value == "ai" and (not self.bot.ai_key or not self.bot.content_enabled):
            raise FriendlyError("Для ИИ заполни CODEX_SALE_API_KEY, включи ENABLE_MESSAGE_CONTENT=true в .env "
                                "и Message Content Intent в Developer Portal, затем перезапусти бота.")
        self.config(interaction)
        self.bot.store.update(interaction.channel_id, mode=value, count=0, last_error="")
        self.bot.context.pop(interaction.channel_id, None)
        await private(interaction, f"Режим: {value}. Счётчик сброшен." +
                      (" Последние 20 сообщений этого канала будут передаваться в codex.sale." if value == "ai" else ""))

    @app_commands.command(description="Включить или выключить реакции SMART в этом канале")
    async def smart(self, interaction: discord.Interaction, enabled: bool):
        self.config(interaction)
        if enabled and (not self.bot.content_enabled or not self.bot.ai_key):
            raise FriendlyError("Для SMART нужны ENABLE_MESSAGE_CONTENT=true и CODEX_SALE_API_KEY в .env.")
        self.bot.store.update(interaction.channel_id, smart_enabled=enabled)
        self.bot.context.pop(interaction.channel_id, None)
        await private(interaction, "SMART включён." if enabled else "SMART выключен. Ответы по счётчику сохранены.")

    @app_commands.command(description="Настроить поводы для SMART")
    async def smart_settings(self, interaction: discord.Interaction,
                             mentions: bool | None = None, arguments: bool | None = None,
                             name: bool | None = None,
                             activity: Literal["low", "medium", "high"] | None = None):
        self.config(interaction)
        updates = {}
        for key, value in (("smart_mentions", mentions), ("smart_arguments", arguments), ("smart_name", name),
                           ("smart_activity", activity)):
            if value is not None:
                updates[key] = value
        if updates:
            self.bot.store.update(interaction.channel_id, **updates)
        c = self.config(interaction)
        await private(interaction, f"SMART: {bool(c['smart_enabled'])} · mentions={bool(c['smart_mentions'])} · "
                      f"arguments={bool(c['smart_arguments'])} · name={bool(c['smart_name'])}\n"
                      f"Activity={c['smart_activity']}.")

    @app_commands.command(description="Изменить интервал и сбросить счётчик")
    async def every(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 10000]):
        self.config(interaction)
        self.bot.store.update(interaction.channel_id, every_n=number, count=0)
        await private(interaction, f"Теперь ответ на каждое {number}-е сообщение.")

    @app_commands.command(description="Включить или выключить ответы по счётчику, не меняя SMART")
    async def periodic(self, interaction: discord.Interaction, enabled: bool):
        self.config(interaction)
        self.bot.store.update(interaction.channel_id, periodic_enabled=enabled, count=0)
        await private(interaction, "Ответы по счётчику включены." if enabled else
                      "Ответы по счётчику выключены. SMART продолжит реагировать на обращения и конфликты.")

    @app_commands.command(description="Остановить автоматические ответы в этом канале")
    async def pause(self, interaction: discord.Interaction):
        self.config(interaction)
        self.bot.store.update(interaction.channel_id, enabled=0)
        self.bot.context.pop(interaction.channel_id, None)
        await private(interaction, "Пауза. Сообщения на паузе не считаются. /bot resume — продолжить.")

    @app_commands.command(description="Продолжить ответы с сохранённого счётчика")
    async def resume(self, interaction: discord.Interaction):
        self.config(interaction)
        self.bot.store.update(interaction.channel_id, enabled=1)
        await private(interaction, "Автоматические ответы включены.")

    @app_commands.command(description="Посмотреть настройки, счётчик и последнюю ошибку")
    async def status(self, interaction: discord.Interaction):
        c = self.config(interaction)
        await private(interaction, f"**{'Включён' if c['enabled'] else 'Пауза'}** · режим: {c['mode']}\n"
                      f"Счётчик: **{c['count']}/{c['every_n']}** · периодические ответы: {bool(c['periodic_enabled'])} "
                      f"· фраз: **{len(c['phrases'])}**\n"
                      f"Smart: **{bool(c['smart_enabled'])}** · mentions={bool(c['smart_mentions'])} · "
                      f"arguments={bool(c['smart_arguments'])} · name={bool(c['smart_name'])}\n"
                      f"Activity={c['smart_activity']}\n"
                      f"ИИ: {'ключ задан' if self.bot.ai_key else 'ключ не задан'} · модель: {self.bot.model}\n"
                      f"Последняя ошибка: {c['last_error'] or 'нет'}\n"
                      f"Последнее решение: {self.bot.last_decisions.get(interaction.channel_id, 'нет после запуска')}")

    @app_commands.command(description="Узнать причину последнего ответа или пропуска")
    async def diagnose(self, interaction: discord.Interaction):
        self.config(interaction)
        reason = self.bot.last_decisions.get(interaction.channel_id, "нет обработанных сообщений после запуска")
        await private(interaction, f"Последнее решение: **{reason}**. История причин: logs/bot.log. "
                      "В логах указаны ID канала и сообщения, текст чата туда не записывается.")

    @app_commands.command(description="Показать пробный ответ только тебе, не меняя счётчик")
    async def test(self, interaction: discord.Interaction):
        c = self.config(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        text, _, error = await self.bot.make_reply(c, interaction.user.display_name, interaction.channel.name,
                                                 interaction.guild.name, list(self.bot.context[interaction.channel_id]))
        await private(interaction, text)
        if error:
            await private(interaction, "Использован резервный ответ: " + error)

    @app_commands.command(description="Обнулить счётчик и очистить контекст ИИ")
    async def reset(self, interaction: discord.Interaction):
        self.config(interaction)
        self.bot.store.update(interaction.channel_id, count=0, last_error="")
        self.bot.context.pop(interaction.channel_id, None)
        await private(interaction, "Счётчик и контекст очищены. Список фраз и промпт сохранены.")


def main():
    load_dotenv(ROOT / ".env", encoding="utf-8-sig")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "PASTE_BOT_TOKEN_HERE":
        print("Не задан токен. Запусти configure.bat или заполни DISCORD_TOKEN в .env.")
        return 1
    try:
        guild_id = int(os.getenv("DISCORD_GUILD_ID", "0").strip() or "0")
        if guild_id < 0:
            raise ValueError
    except ValueError:
        print("DISCORD_GUILD_ID должен содержать только цифры ID сервера.")
        return 1
    content = os.getenv("ENABLE_MESSAGE_CONTENT", "false").strip().lower()
    if content not in {"true", "false"}:
        print("ENABLE_MESSAGE_CONTENT должен быть true или false.")
        return 1
    base_url = (os.getenv("CODEX_SALE_BASE_URL") or os.getenv("OPENAI_BASE_URL")
                or "https://codex.sale/v1").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith("/v1"):
        print("CODEX_SALE_BASE_URL должен быть HTTPS-адресом с окончанием /v1.")
        return 1
    (ROOT / "logs").mkdir(exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(ROOT / "logs/bot.log", maxBytes=2_000_000,
                                                            backupCount=3, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    store = None
    try:
        with InstanceLock(ROOT / "data/bot.lock"):
            store = Store(ROOT / "data/bot.sqlite3")
            key = (os.getenv("CODEX_SALE_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
            client = Every20(store, guild_id, key,
                             os.getenv("OPENAI_MODEL", "gpt-6.1-sol").strip() or "gpt-6.1-sol",
                             content == "true", base_url)
            client.run(token, log_handler=None)
        return 0
    except discord.LoginFailure:
        log.error("Неверный DISCORD_TOKEN. Скопируй Bot Token заново в .env.")
    except discord.PrivilegedIntentsRequired:
        log.error("Включи Message Content Intent в Developer Portal → Bot, либо поставь ENABLE_MESSAGE_CONTENT=false.")
    except (discord.HTTPException, aiohttp.ClientError, OSError, RuntimeError) as exc:
        log.error("Запуск остановлен: %s. Проверь сеть, ID сервера и права; см. README.html.", type(exc).__name__)
        if isinstance(exc, RuntimeError):
            log.error("%s", exc)
    finally:
        if store:
            store.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
