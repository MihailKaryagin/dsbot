"""Local credential wizard. Tokens are never sent anywhere by this script."""
from getpass import getpass
from pathlib import Path
import shutil
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parent


def main():
    path = ROOT / ".env"
    if not path.exists():
        shutil.copyfile(ROOT / ".env.example", path)
    old = dotenv_values(path)
    print("Настройка Discord-бота. Ввод токена скрыт — это нормально.")
    token = getpass("Bot Token (Enter — сохранить текущий): ").strip()
    if token:
        if any(c.isspace() for c in token) or token.lower().startswith("bot "):
            raise ValueError("Вставь только токен, без пробелов и префикса Bot.")
        set_key(str(path), "DISCORD_TOKEN", token)
    elif old.get("DISCORD_TOKEN") in {None, "", "PASTE_BOT_TOKEN_HERE"}:
        raise ValueError("Нужен токен бота из Developer Portal → Bot → Reset Token.")
    guild = input("ID сервера (Enter — оставить как есть): ").strip()
    if guild:
        if not guild.isascii() or not guild.isdigit() or int(guild) <= 0:
            raise ValueError("ID сервера должен состоять только из цифр.")
        set_key(str(path), "DISCORD_GUILD_ID", guild)
    print("Готовые фразы работают без ключа codex.sale.")
    choice = input("Настроить ИИ? y = да, n = выключить, Enter = оставить как есть: ").strip().lower()
    if choice == "y":
        key = getpass("codex.sale API key (Enter — сохранить текущий): ").strip()
        if key:
            set_key(str(path), "CODEX_SALE_API_KEY", key)
        elif not (old.get("CODEX_SALE_API_KEY") or old.get("OPENAI_API_KEY")):
            raise ValueError("Для ИИ нужен ключ codex.sale API.")
        set_key(str(path), "ENABLE_MESSAGE_CONTENT", "true")
        print("Включи Message Content Intent в Developer Portal → Bot перед запуском.")
    elif choice == "n":
        set_key(str(path), "ENABLE_MESSAGE_CONTENT", "false")
    print("Сохранено в .env. Запусти start.bat. Если бот уже работает — сначала останови его Ctrl+C.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print("Ошибка:", exc)
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nНастройка прервана.")
        raise SystemExit(1)
