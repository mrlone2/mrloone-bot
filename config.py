"""
config.py — Модуль загрузки и валидации конфигурации бота из файла .env.
Все чувствительные данные (токены, cookies, ID администратора, прокси)
считываются из переменных окружения, чтобы безопасно запускать бота на VPS.
"""

from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем переменные из файла .env (если он существует в корне проекта)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Config:
    """Класс конфигурации приложения mrloone helper bot."""

    bot_token: str
    admin_id: int
    playerok_cookies: str
    playerok_token: str
    proxy_url: str | None
    db_path: Path
    banner_path: Path
    poll_interval_min: float
    poll_interval_max: float
    mock_mode: bool


def _parse_bool(value: str | None, default: bool = False) -> bool:
    """Преобразует строковое значение из .env в bool."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def load_config() -> Config:
    """
    Считывает настройки из .env и возвращает объект Config.
    Вызывает ValueError, если не заданы обязательные параметры.
    """
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("❌ Не задан BOT_TOKEN в файле .env!")

    raw_admin_id = os.getenv("ADMIN_ID", "0").strip()
    try:
        admin_id = int(raw_admin_id)
    except ValueError as exc:
        raise ValueError("❌ ADMIN_ID в .env должен быть целым числом (ваш Telegram ID).") from exc

    playerok_cookies = os.getenv("PLAYEROK_COOKIES", "").strip()
    playerok_token = os.getenv("PLAYEROK_TOKEN", "").strip()

    # Если передан только токен без полной строки cookies — формируем базовую cookie
    if not playerok_cookies and playerok_token:
        playerok_cookies = f"token={playerok_token}"

    proxy_url = os.getenv("PROXY_URL", "").strip() or None

    db_path_raw = os.getenv("DB_PATH", "data/mrloone_bot.db").strip()
    db_path = Path(db_path_raw)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    banner_path_raw = os.getenv("BANNER_PATH", "assets/banner.png").strip()
    banner_path = Path(banner_path_raw)
    if not banner_path.is_absolute():
        banner_path = BASE_DIR / banner_path

    poll_min = float(os.getenv("POLL_INTERVAL_MIN", "5.0"))
    poll_max = float(os.getenv("POLL_INTERVAL_MAX", "10.0"))
    if poll_min > poll_max:
        poll_min, poll_max = poll_max, poll_min

    mock_mode = _parse_bool(os.getenv("MOCK_MODE"), default=False)

    return Config(
        bot_token=bot_token,
        admin_id=admin_id,
        playerok_cookies=playerok_cookies,
        playerok_token=playerok_token,
        proxy_url=proxy_url,
        db_path=db_path,
        banner_path=banner_path,
        poll_interval_min=poll_min,
        poll_interval_max=poll_max,
        mock_mode=mock_mode,
    )
