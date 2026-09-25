# -*- coding: utf-8 -*-
"""Настройки бота: переменные окружения + файл .env рядом со скриптом."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

ENV_FILE = Path(__file__).with_name(".env")

# Telegram принимает секрет вебхука только из A-Z, a-z, 0-9, «_» и «-», 1–256 символов.
WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
WEBHOOK_SECRET_HINT = (
    'Сгенерируйте: python -c "import secrets; print(secrets.token_urlsafe(32))"'
)

DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_CURRENCY = "BYN"
DEFAULT_DEBTS_TABLE = "debts"
DEFAULT_SETTINGS_TABLE = "bot_settings"
DEFAULT_STATE_TABLE = "bot_state"
DEFAULT_MEMBERS_TABLE = "chat_members"
DEFAULT_RATES_TABLE = "currency_rates"
# Курсы валют: ExchangeRate-API (https://app.exchangerate-api.com — кабинет и бесплатный ключ).
# Без ключа используется открытый эндпоинт open.er-api.com (лимит запросов, нужна ссылка).
DEFAULT_RATES_URL = "https://v6.exchangerate-api.com/v6"
DEFAULT_RATES_OPEN_URL = "https://open.er-api.com/v6"
DEFAULT_RATES_BASE = "BYN"
# Какие валюты тянем из API: бел. рубль, рос. рубль, доллар, евро, юань, тайский бат.
DEFAULT_RATES_CURRENCIES = ("BYN", "RUB", "USD", "EUR", "CNY", "THB")
# Во сколько по Минску (UTC+3) обновлять курсы: раз в день, без cron. 0–23.
DEFAULT_RATES_HOUR = 12

# Как передавать ключ базы в запросах к PostgREST:
#   apikey — только заголовок apikey (так требует Supabase для ключей нового формата
#            sb_secret_…/sb_publishable_… и так делает supabase-js);
#   both   — плюс Authorization: Bearer с тем же ключом (поведение supabase-py по умолчанию;
#            пригодится на нестандартных шлюзах/self-hosted, где роль выбирают по Authorization).
KEY_HEADER_MODES = ("apikey", "both")
DEFAULT_KEY_HEADER = "apikey"

# Ключ доступа бота к базе. Новый формат — secret-ключ Supabase (sb_secret_…):
# Project Settings → API Keys → «Publishable and secret API keys» → Secret keys.
# Прежние имена (SUPABASE_SERVICE_KEY, SUPABASE_KEY) и legacy-ключи service_role работают.
SUPABASE_KEY_ENVS = ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY")
SUPABASE_KEY_HINT = (
    "Supabase → Project Settings → API Keys → «Publishable and secret API keys» → "
    "Secret keys → скопировать sb_secret_…"
)


class ConfigError(RuntimeError):
    """Не хватает обязательных настроек."""


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """Разбирает .env: строки KEY=VALUE, комментарии через #, кавычки срезаются."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"").strip()
    return values


@dataclass(frozen=True)
class Settings:
    """Все настройки приложения."""

    telegram_token: str = ""
    deepseek_key: str = ""
    deepseek_base_url: str = DEFAULT_DEEPSEEK_URL
    deepseek_model: str = DEFAULT_DEEPSEEK_MODEL
    supabase_url: str = ""
    # Ключ базы: SUPABASE_SECRET_KEY (sb_secret_…) или legacy service_role
    supabase_key: str = ""
    # Как ключ уходит в запросах: apikey (по умолчанию) или apikey + Authorization
    supabase_key_header: str = DEFAULT_KEY_HEADER
    debts_table: str = DEFAULT_DEBTS_TABLE
    settings_table: str = DEFAULT_SETTINGS_TABLE
    state_table: str = DEFAULT_STATE_TABLE
    members_table: str = DEFAULT_MEMBERS_TABLE
    rates_table: str = DEFAULT_RATES_TABLE
    rates_api_url: str = DEFAULT_RATES_URL
    rates_api_key: str = ""
    # Пусто — открытый эндпоинт не используется. load_settings подставляет сюда
    # RATES_OPEN_URL (по умолчанию open.er-api.com), поэтому в бою курсы работают и без
    # ключа, а тесты и демо (они создают Settings напрямую) в сеть не ходят.
    rates_open_url: str = ""
    rates_base: str = DEFAULT_RATES_BASE
    rates_currencies: tuple[str, ...] = DEFAULT_RATES_CURRENCIES
    # Во сколько по Минску подтягивать курсы (раз в день, без cron).
    rates_hour: int = DEFAULT_RATES_HOUR
    chat_password: str = ""
    default_currency: str = DEFAULT_CURRENCY
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    request_timeout: float = 30.0
    log_level: str = "INFO"
    webhook_secret: str = ""
    require_mention: bool = True
    bot_username: str = ""

    @property
    def rest_url(self) -> str:
        """Базовый URL REST API Supabase (PostgREST)."""
        return self.supabase_url.rstrip("/") + "/rest/v1"

    @property
    def currencies(self) -> tuple[str, ...]:
        """Известные коды валют (для подсказок и нормализации)."""
        return ("BYN", "USD", "EUR", "RUB", "CNY", "THB", "PLN", "UAH", "KZT", "GBP")

    @property
    def password_required(self) -> bool:
        """Нужен ли пароль, чтобы бот начал работать в чате."""
        return bool(str(self.chat_password or "").strip())

    def rates_problem(self) -> str | None:
        """Проблема с настройками курсов валют (None — всё в порядке).

        Курсы не обязательны для учёта долгов, поэтому в problems() они не попадают:
        без ключа работает открытый эндпоинт, а без сети бот использует уже сохранённые
        в базе курсы.
        """
        if str(self.rates_api_key or "").strip() or str(self.rates_open_url or "").strip():
            return None
        return ("Не задан ни RATES_API_KEY, ни RATES_OPEN_URL — курсы валют брать негде: "
                "/d и /rates будут работать только по уже сохранённым курсам.")

    @property
    def rates_source(self) -> str:
        """Что использовать для курсов: личный кабинет с ключом или открытый эндпоинт."""
        if str(self.rates_api_key or "").strip():
            return "аккаунт exchangerate-api.com (ключ задан)"
        return "открытый эндпоинт open.er-api.com (без ключа)"

    def problems(self) -> list[str]:
        """Список проблем конфигурации (пустой — всё настроено)."""
        issues: list[str] = []
        if not self.telegram_token:
            issues.append("TELEGRAM_BOT_TOKEN не задан (токен от @BotFather)")
        if not self.deepseek_key:
            issues.append("DEEPSEEK_API_KEY не задан (ключ платформы DeepSeek)")
        if not self.supabase_url:
            issues.append("SUPABASE_URL не задан (например https://xxxx.supabase.co)")
        if not self.supabase_key:
            issues.append(
                "Ключ базы не задан: нужен SUPABASE_SECRET_KEY (secret-ключ). "
                f"{SUPABASE_KEY_HINT}. Legacy-ключ service_role можно оставить "
                "в SUPABASE_SERVICE_KEY."
            )
        else:
            key_problem = supabase_key_problem(self.supabase_key)
            if key_problem:
                issues.append(key_problem)
        if self.supabase_key_header not in KEY_HEADER_MODES:
            issues.append(
                f"SUPABASE_KEY_HEADER: допустимы только {', '.join(KEY_HEADER_MODES)} "
                f"(сейчас «{self.supabase_key_header}») — определяет, в каких заголовках "
                "уходит ключ базы."
            )
        return issues


def _clean(value: object) -> str:
    """Срезает пробелы и кавычки — частая ошибка при вставке ключей."""
    return str(value or "").strip().strip("'\"").strip()


def _parse_bool(raw: str, default: bool) -> bool:
    """Понимает 1/0, true/false, yes/no, on/off, да/нет (пусто — значение по умолчанию)."""
    value = _clean(raw).lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on", "да", "истина"):
        return True
    if value in ("0", "false", "no", "off", "нет", "ложь"):
        return False
    return default


def _parse_codes(raw: str) -> tuple[str, ...]:
    """Разбирает список кодов валют: 'byn, usd eur' -> ('BYN', 'USD', 'EUR').

    Пусто — набор по умолчанию (бел. и рос. рубли, доллар, евро, юань, бат).
    Базовую валюту (RATES_BASE) указывать не обязательно: её курс всегда 1.0 и запрос
    к API по ней не делается. А вот валюту, в которой чат ведёт учёт, лучше добавить —
    иначе /d не сможет пересчитать записи в неё и оставит их как есть.
    """
    codes: list[str] = []
    for chunk in str(raw or "").replace(";", ",").replace(" ", ",").split(","):
        code = chunk.strip().upper()
        if len(code) == 3 and code.isalpha() and code not in codes:
            codes.append(code)
    if not codes:
        return DEFAULT_RATES_CURRENCIES
    return tuple(codes)


def _parse_hour(raw: str, default: int) -> int:
    """Час суток 0–23 для расписания курсов ('12' -> 12, мусор -> значение по умолчанию)."""
    text = _clean(raw)
    if not text:
        return default
    try:
        hour = int(text)
    except ValueError:
        return default
    return hour if 0 <= hour <= 23 else default


def _parse_user_ids(raw: str) -> frozenset[int]:
    """Разбирает список id пользователей: '123, 456' -> {123, 456}."""
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            ids.add(int(chunk))
    return frozenset(ids)


def jwt_role(token: str) -> str | None:
    """Роль из payload JWT Supabase: 'anon', 'service_role' или None, если это не JWT."""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    return str(role) if role else None


def _supabase_key(get: Callable[[str], str]) -> str:
    """Ключ доступа к базе: сначала новый SUPABASE_SECRET_KEY, потом прежние имена.

    Приоритет важен при переезде: если в окружении остался старый service_role,
    а рядом уже лежит новый secret-ключ, бот возьмёт новый.
    """
    for name in SUPABASE_KEY_ENVS:
        value = get(name)
        if value:
            return value
    return ""


def supabase_key_problem(key: str) -> str | None:
    """Проверяет ключ доступа к базе и возвращает текст проблемы (None — ключ подходит).

    Подходят два варианта: новый secret-ключ (sb_secret_…) — он и рекомендуется, и
    legacy-ключ service_role (JWT). Публичные ключи (publishable, anon) не годятся:
    с ними RLS отклоняет запись, и бот выглядит «сломанным» при верных настройках.
    """
    value = _clean(key)
    if not value:
        return None
    if value.lower().startswith("sb_secret_"):
        return None
    role = jwt_role(value)
    if role == "service_role":
        return None
    if role:
        return (
            f"Ключ базы — с ролью «{role}». Нужен secret-ключ: {SUPABASE_KEY_HINT} "
            "(или legacy service_role в SUPABASE_SERVICE_KEY)."
        )
    if value.lower().startswith("sb_publishable_"):
        return (
            "Ключ базы — publishable (публичный): запись с ним отклоняет RLS. "
            f"Нужен secret-ключ: {SUPABASE_KEY_HINT}"
        )
    return (
        "Ключ базы не похож ни на secret-ключ (sb_secret_…), ни на legacy JWT (eyJ…): "
        f"проверьте, что скопирован целиком. {SUPABASE_KEY_HINT}"
    )


def webhook_secret_problem(secret: str) -> str | None:
    """Проверяет секрет вебхука и возвращает текст проблемы (None — секрет подходит).

    Без секрета эндпоинт вебхука отказывает в обработке: адрес виден в интернете,
    и без проверки заголовка любой желающий мог бы «писать от имени Telegram».
    """
    value = _clean(secret)
    if not value:
        return (
            "WEBHOOK_SECRET не задан — эндпоинт вебхука ничего не обработает. "
            f"{WEBHOOK_SECRET_HINT}"
        )
    if not WEBHOOK_SECRET_RE.match(value):
        return (
            "WEBHOOK_SECRET содержит недопустимые символы: Telegram принимает только "
            "A-Z, a-z, 0-9, «_», «-» (до 256 символов)."
        )
    return None


def describe_supabase_key(key: str) -> str:
    """Описывает ключ базы словами — для --check и диагностики «почему база отказывает».

    Важно, какой это ключ: secret-ключ (и legacy service_role) пускают бота писать,
    а публичный publishable/anon — нет: запрос выполняется от роли anon, и запись
    отклоняет RLS (Postgres 42501).
    """
    value = _clean(key)
    if not value:
        return "не задан"
    lowered = value.lower()
    if lowered.startswith("sb_secret_"):
        return "secret-ключ (sb_secret_…) — то, что нужно"
    if lowered.startswith("sb_publishable_"):
        return "публичный ключ (sb_publishable_…) — для сервера не годится: RLS не пустит запись"
    role = jwt_role(value)
    if role:
        return f"legacy JWT с ролью «{role}»"
    return "ключ неизвестного формата"


def load_settings(env: Mapping[str, str] | None = None, *, use_env_file: bool = True) -> Settings:
    """Собирает настройки: аргумент -> окружение процесса -> .env -> значения по умолчанию."""
    source: Mapping[str, str] = os.environ if env is None else env
    file_values = load_env_file() if use_env_file else {}

    def get(name: str, default: str = "") -> str:
        """Значение настройки с приоритетом окружения над .env."""
        if name in source and _clean(source[name]):
            return _clean(source[name])
        if name in file_values:
            return _clean(file_values[name])
        return default

    timeout_raw = get("REQUEST_TIMEOUT", "30")
    try:
        timeout = max(5.0, float(timeout_raw.replace(",", ".")))
    except ValueError:
        timeout = 30.0

    return Settings(
        telegram_token=get("TELEGRAM_BOT_TOKEN"),
        deepseek_key=get("DEEPSEEK_API_KEY"),
        deepseek_base_url=get("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_URL).rstrip("/"),
        deepseek_model=get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
        supabase_url=get("SUPABASE_URL").rstrip("/"),
        supabase_key=_supabase_key(get),
        supabase_key_header=get("SUPABASE_KEY_HEADER", DEFAULT_KEY_HEADER).lower(),
        debts_table=get("DEBTS_TABLE", DEFAULT_DEBTS_TABLE),
        settings_table=get("SETTINGS_TABLE", DEFAULT_SETTINGS_TABLE),
        state_table=get("BOT_STATE_TABLE", DEFAULT_STATE_TABLE),
        members_table=get("MEMBERS_TABLE", DEFAULT_MEMBERS_TABLE),
        rates_table=get("RATES_TABLE", DEFAULT_RATES_TABLE),
        rates_api_url=get("RATES_API_URL", DEFAULT_RATES_URL).rstrip("/"),
        rates_api_key=get("RATES_API_KEY"),
        rates_open_url=get("RATES_OPEN_URL", DEFAULT_RATES_OPEN_URL).rstrip("/"),
        rates_base=get("RATES_BASE", DEFAULT_RATES_BASE).upper(),
        rates_currencies=_parse_codes(get("RATES_CURRENCIES")),
        rates_hour=_parse_hour(get("RATES_HOUR", str(DEFAULT_RATES_HOUR)), DEFAULT_RATES_HOUR),
        chat_password=get("CHAT_PASSWORD"),
        default_currency=get("DEFAULT_CURRENCY", DEFAULT_CURRENCY).upper(),
        allowed_user_ids=_parse_user_ids(get("ALLOWED_USER_IDS")),
        request_timeout=timeout,
        log_level=get("LOG_LEVEL", "INFO").upper(),
        webhook_secret=get("WEBHOOK_SECRET"),
        require_mention=_parse_bool(get("REQUIRE_MENTION"), True),
        bot_username=get("BOT_USERNAME").lstrip("@"),
    )


def require_settings(settings: Settings) -> None:
    """Бросает ConfigError, если конфигурация неполная."""
    issues = settings.problems()
    if issues:
        raise ConfigError("Проверьте настройки:\n- " + "\n- ".join(issues))
