# -*- coding: utf-8 -*-
"""Настройки бота: переменные окружения + файл .env рядом со скриптом."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

ENV_FILE = Path(__file__).with_name(".env")

DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_CURRENCY = "BYN"
DEFAULT_DEBTS_TABLE = "debts"
DEFAULT_SETTINGS_TABLE = "bot_settings"
DEFAULT_STATE_TABLE = "bot_state"


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
    supabase_key: str = ""
    debts_table: str = DEFAULT_DEBTS_TABLE
    settings_table: str = DEFAULT_SETTINGS_TABLE
    state_table: str = DEFAULT_STATE_TABLE
    default_currency: str = DEFAULT_CURRENCY
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    request_timeout: float = 30.0
    log_level: str = "INFO"

    @property
    def rest_url(self) -> str:
        """Базовый URL REST API Supabase (PostgREST)."""
        return self.supabase_url.rstrip("/") + "/rest/v1"

    @property
    def currencies(self) -> tuple[str, ...]:
        """Известные коды валют (для подсказок и нормализации)."""
        return ("BYN", "USD", "EUR", "RUB", "PLN", "UAH", "KZT", "GBP")

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
            issues.append("SUPABASE_SERVICE_KEY не задан (ключ service_role из настроек проекта)")
        else:
            key_problem = supabase_key_problem(self.supabase_key)
            if key_problem:
                issues.append(key_problem)
        return issues


def _clean(value: object) -> str:
    """Срезает пробелы и кавычки — частая ошибка при вставке ключей."""
    return str(value or "").strip().strip("'\"").strip()


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


def supabase_key_problem(key: str) -> str | None:
    """Проверяет ключ Supabase и возвращает текст проблемы (None — ключ подходит)."""
    if not key:
        return None
    role = jwt_role(key)
    if role == "service_role":
        return None
    if role:
        return (
            f"SUPABASE_SERVICE_KEY — ключ с ролью «{role}». Нужен service_role: "
            "Supabase → Project Settings → API Keys → «Legacy anon, service_role API keys» → "
            "service_role → Reveal (или новый secret-ключ sb_secret_…)."
        )
    if key.startswith("sb_secret_"):
        return None
    if key.startswith("sb_publishable_"):
        return (
            "SUPABASE_SERVICE_KEY — publishable (публичный) ключ, запись будет отклонена. "
            "Нужен secret-ключ (sb_secret_…) или legacy service_role."
        )
    return (
        "SUPABASE_SERVICE_KEY не похож ни на JWT (eyJ…), ни на secret-ключ (sb_secret_…): "
        "проверьте, что скопирован ключ целиком."
    )


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
        supabase_key=get("SUPABASE_SERVICE_KEY") or get("SUPABASE_KEY"),
        debts_table=get("DEBTS_TABLE", DEFAULT_DEBTS_TABLE),
        settings_table=get("SETTINGS_TABLE", DEFAULT_SETTINGS_TABLE),
        state_table=get("BOT_STATE_TABLE", DEFAULT_STATE_TABLE),
        default_currency=get("DEFAULT_CURRENCY", DEFAULT_CURRENCY).upper(),
        allowed_user_ids=_parse_user_ids(get("ALLOWED_USER_IDS")),
        request_timeout=timeout,
        log_level=get("LOG_LEVEL", "INFO").upper(),
    )


def require_settings(settings: Settings) -> None:
    """Бросает ConfigError, если конфигурация неполная."""
    issues = settings.problems()
    if issues:
        raise ConfigError("Проверьте настройки:\n- " + "\n- ".join(issues))
