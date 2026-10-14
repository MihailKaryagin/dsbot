"""Persistent settings and counters. No Discord or network dependency."""
import json
import random
import sqlite3
from pathlib import Path

DEFAULT_PHRASES = ["Я тут, если что 👀", "Так, а что тут происходит?", "Продолжайте, я слушаю 😎"]
DEFAULT_PROMPT = "Ты дружелюбный участник Discord-чата. Пиши по-русски, коротко, 1–2 предложения, с лёгким юмором."


def parse_phrases(text):
    phrases = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))
    if not 1 <= len(phrases) <= 50:
        raise ValueError("Нужны 1–50 непустых фраз, каждая с новой строки.")
    if any(len(p) > 1800 for p in phrases) or len("\n".join(phrases)) > 4000:
        raise ValueError("Максимум 1800 символов на фразу и 4000 на весь список.")
    return phrases


def render_phrase(phrases, previous, user, channel, server):
    candidates = [p for p in phrases if p != previous] or phrases
    template = random.choice(candidates)
    text = template
    for key, value in {"user": user, "channel": channel, "server": server}.items():
        text = text.replace("{" + key + "}", value)
    return template, text[:2000]


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute('''CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0, every_n INTEGER NOT NULL DEFAULT 20,
            mode TEXT NOT NULL DEFAULT 'phrases', phrases TEXT NOT NULL,
            prompt TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            last_id INTEGER NOT NULL DEFAULT 0, last_phrase TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0
        )''')
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(channels)")}
        additions = {
            "periodic_enabled": "INTEGER NOT NULL DEFAULT 1",
            "smart_enabled": "INTEGER NOT NULL DEFAULT 0",
            "smart_mentions": "INTEGER NOT NULL DEFAULT 1",
            "smart_arguments": "INTEGER NOT NULL DEFAULT 1",
            "smart_name": "INTEGER NOT NULL DEFAULT 0",
            "smart_activity": "TEXT NOT NULL DEFAULT 'medium'",
        }
        for column, definition in additions.items():
            if column not in existing:
                self.db.execute(f"ALTER TABLE channels ADD COLUMN {column} {definition}")
        # Older SMART builds used short column names. Import them without touching
        # phrases, prompts, counters or any other saved settings.
        legacy = {"periodic": "periodic_enabled", "smart": "smart_enabled", "mentions": "smart_mentions",
                  "arguments": "smart_arguments", "name": "smart_name",
                  "activity": "smart_activity"}
        for old, new in legacy.items():
            if old in existing and new not in existing:
                self.db.execute(f'UPDATE channels SET "{new}"="{old}"')
        self.db.commit()

    def ensure(self, channel_id, guild_id):
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO channels
                (channel_id, guild_id, phrases, prompt) VALUES (?, ?, ?, ?)''',
                (channel_id, guild_id, json.dumps(DEFAULT_PHRASES, ensure_ascii=False), DEFAULT_PROMPT))
        return self.get(channel_id)

    def get(self, channel_id):
        row = self.db.execute("SELECT * FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["phrases"] = json.loads(result["phrases"])
        return result

    def update(self, channel_id, **values):
        allowed = {"enabled", "every_n", "mode", "phrases", "prompt", "count", "last_phrase", "last_error",
                   "periodic_enabled", "smart_enabled", "smart_mentions", "smart_arguments", "smart_name", "smart_activity"}
        if not values or set(values) - allowed:
            raise ValueError("Unknown settings")
        if "every_n" in values and not 1 <= values["every_n"] <= 10000:
            raise ValueError("Интервал должен быть от 1 до 10000.")
        if "mode" in values and values["mode"] not in {"phrases", "ai"}:
            raise ValueError("Unknown mode")
        if "smart_activity" in values and values["smart_activity"] not in {"low", "medium", "high"}:
            raise ValueError("Activity must be low, medium or high")
        for key in ("periodic_enabled", "smart_enabled", "smart_mentions", "smart_arguments", "smart_name"):
            if key in values:
                values[key] = int(bool(values[key]))
        if "phrases" in values:
            values["phrases"] = json.dumps(parse_phrases("\n".join(values["phrases"])), ensure_ascii=False)
        with self.db:
            self.db.execute("UPDATE channels SET " + ", ".join(k + "=?" for k in values)
                            + ", revision=revision+1 WHERE channel_id=?", (*values.values(), channel_id))

    def tick(self, channel_id, message_id):
        """Return a settings snapshot exactly on N, 2N, ...; ignore replayed IDs."""
        config = self.get(channel_id)
        if not config or not config["enabled"] or message_id <= config["last_id"]:
            return None
        if not config["periodic_enabled"]:
            with self.db:
                self.db.execute("UPDATE channels SET last_id=? WHERE channel_id=?", (message_id, channel_id))
            return None
        count = config["count"] + 1
        due = count >= config["every_n"]
        with self.db:
            self.db.execute("UPDATE channels SET count=?, last_id=? WHERE channel_id=?",
                            (0 if due else count, message_id, channel_id))
        return config if due else None

    def close(self):
        self.db.close()
