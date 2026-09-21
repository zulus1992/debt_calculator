# -*- coding: utf-8 -*-
"""Телеграм-бот «калькулятор долгов»: DeepSeek разбирает сообщение, Supabase хранит долги.

Запуск:
    python bot.py                 # рабочий режим (длинный опрос Telegram)
    python bot.py --check         # проверить настройки и доступность сервисов
    python bot.py --demo          # демонстрация без Telegram (в памяти, офлайн-разбор)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any, Mapping

from config import ConfigError, Settings, load_settings, require_settings
from debts import (
    format_currency_set,
    format_debt_saved,
    format_debts_report,
    format_help,
    normalize_name,
)
from deepseek import (
    DeepSeekParser,
    ParsedMessage,
    check_api_key,
    detect_currency,
    heuristic_parse,
)
from storage import InMemoryStorage, Storage, StorageError, SupabaseStorage
from telegram_api import TelegramBot, TelegramError

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
logger = logging.getLogger("debt_bot")

NOT_A_DEBT_REPLY = (
    "🤔 Похоже на долг, но не хватает данных. Пример: «Леша должен Диме 3 рубля»."
)
UNKNOWN_REPLY = (
    "🤷 Не понял сообщение.\n"
    "• Записать долг: «Леша должен Диме 3 рубля»\n"
    "• Показать долги: /debts\n"
    "• Справка: /help"
)
DENIED_REPLY = "⛔ Извините, этот бот настроен только для определённых пользователей."
LAST_UPDATE_ID_KEY = "last_update_id"


class HeuristicParser:
    """Разбор только офлайн-эвристиками: демо-режим и тесты без DeepSeek."""

    def parse(self, text: str, default_currency: str = "BYN") -> ParsedMessage:
        """Разбирает текст регулярными выражениями."""
        parsed = heuristic_parse(text, default_currency)
        if parsed is not None:
            return parsed
        return ParsedMessage(
            intent="none",
            note="разбор по шаблону не сработал (нет DeepSeek)",
            source="heuristic",
        )


def set_default_currency(text_value: str, chat_id: int, storage: Storage,
                         fallback: str = "BYN") -> str:
    """Устанавливает валюту по умолчанию по слову или коду валюты."""
    code = detect_currency(text_value)
    if not code:
        return (
            "Укажите валюту кодом или словом:\n"
            "• /currency BYN\n• /currency USD\n• «валюта по умолчанию доллар»\n"
            f"Сейчас установлено: {fallback}."
        )
    storage.set_default_currency(chat_id, code)
    return format_currency_set(code)


def handle_text(
    text: str,
    chat_id: int,
    *,
    storage: Storage,
    parser: Any,
    settings: Settings,
) -> str:
    """Обрабатывает одно сообщение и формирует ответ бота.

    Функция не знает про Telegram — это делает её простой для тестов.
    """
    raw = (text or "").strip()
    if not raw:
        return format_help(settings.default_currency)

    default_currency = storage.get_default_currency(chat_id, settings.default_currency)
    command, _, argument = raw.partition(" ")
    command = command.lower()

    if command in ("/start", "/help"):
        return format_help(default_currency)
    if command == "/debts":
        return format_debts_report(storage.list_debts(chat_id), default_currency)
    if command == "/currency":
        return set_default_currency(argument or raw, chat_id, storage, default_currency)
    if command == "/reset":
        removed = storage.delete_debts(chat_id)
        return f"🧹 Удалено записей: {removed}." if removed else "📭 Записей и так нет."

    parsed = parser.parse(raw, default_currency)
    explicit_currency = detect_currency(raw)

    if parsed.intent == "debt":
        if not parsed.is_debt:
            return NOT_A_DEBT_REPLY
        currency = (parsed.currency or explicit_currency or default_currency).upper()
        debt = storage.add_debt(
            chat_id=chat_id,
            from_name=normalize_name(str(parsed.from_name)),
            to_name=normalize_name(str(parsed.to_name)),
            currency=currency,
            amount=float(parsed.amount or 0),
            raw_text=raw,
        )
        return format_debt_saved(debt, used_default_currency=explicit_currency is None)

    if parsed.intent == "debts":
        return format_debts_report(storage.list_debts(chat_id), default_currency)
    if parsed.intent == "set_currency":
        if not parsed.currency:
            return "Не понял валюту. Пример: /currency USD"
        storage.set_default_currency(chat_id, parsed.currency)
        return format_currency_set(parsed.currency)
    if parsed.intent == "help":
        return format_help(default_currency)

    note = f"\n({parsed.note})" if parsed.note else ""
    return UNKNOWN_REPLY + note


def is_allowed(user_id: int | None, settings: Settings) -> bool:
    """Проверяет пользователя по списку ALLOWED_USER_IDS (пустой список — все допущены)."""
    if not settings.allowed_user_ids:
        return True
    return user_id is not None and int(user_id) in settings.allowed_user_ids


class DebtBot:
    """Длинный опрос Telegram и обработка входящих сообщений."""

    def __init__(self, settings: Settings, storage: Storage, parser: Any,
                 telegram: TelegramBot) -> None:
        self._settings = settings
        self._storage = storage
        self._parser = parser
        self._telegram = telegram

    def run(self, poll_timeout: int = 25, max_updates: int | None = None) -> int:
        """Цикл опроса. max_updates ограничивает число обработанных сообщений (для тестов)."""
        me = self._telegram.get_me()
        logger.info(
            "Бот @%s (id %s) запущен. Остановка — Ctrl+C.", me.get("username"), me.get("id")
        )
        offset: int | None = None
        processed = 0
        while True:
            try:
                updates = self._telegram.get_updates(offset, poll_timeout=poll_timeout)
            except TelegramError as exc:
                logger.error("getUpdates: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                offset = int(update.get("update_id") or 0) + 1
                self._process(update)
                processed += 1
                if max_updates is not None and processed >= max_updates:
                    return processed

    def run_once(self, limit: int = 100) -> int:
        """Обрабатывает накопившиеся апдейты и выходит (режим GitHub Actions / cron).

        Смещение (last_update_id) хранится в базе, поэтому запуски по расписанию
        не теряют и не дублируют сообщения.
        """
        offset = self._load_offset()
        updates = self._telegram.get_updates(offset, poll_timeout=0, limit=limit)
        if not updates:
            logger.info("Новых сообщений нет (offset=%s).", offset)
            return 0

        processed = 0
        for update in updates:
            update_id = int(update.get("update_id") or 0)
            self._process(update)
            processed += 1
            # смещение фиксируем после каждого сообщения: при сбое потеряется максимум одно
            self._storage.set_state(LAST_UPDATE_ID_KEY, str(update_id + 1))
        logger.info("Обработано сообщений: %d (offset -> %s)", processed, offset)
        return processed

    def _load_offset(self) -> int | None:
        """Читает сохранённое смещение апдейтов (None — обработать всё, что накопилось)."""
        try:
            raw = self._storage.get_state(LAST_UPDATE_ID_KEY)
        except StorageError as exc:
            logger.warning("Не удалось прочитать смещение апдейтов: %s", exc)
            return None
        if raw and str(raw).lstrip("-").isdigit():
            return int(raw)
        return None

    def _process(self, update: Mapping[str, Any]) -> None:
        """Обрабатывает один апдейт Telegram."""
        message = update.get("message") or {}
        text = message.get("text")
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if not text or chat_id is None:
            return
        if not is_allowed(user_id, self._settings):
            logger.warning("Сообщение от недопущенного пользователя id=%s", user_id)
            self._send(chat_id, DENIED_REPLY, message)
            return

        self._telegram.send_typing(chat_id)
        try:
            reply = handle_text(
                text, int(chat_id),
                storage=self._storage, parser=self._parser, settings=self._settings,
            )
        except StorageError as exc:
            logger.error("Хранилище: %s", exc)
            reply = f"⚠️ Проблема с базой данных: {exc}"
        except Exception as exc:  # noqa: BLE001 — бот не должен падать из-за одного сообщения
            logger.exception("Ошибка обработки сообщения: %s", exc)
            reply = "⚠️ Внутренняя ошибка, попробуйте ещё раз."
        self._send(chat_id, reply, message)

    def _send(self, chat_id: Any, text: str, message: Mapping[str, Any]) -> None:
        """Отправляет ответ, логируя проблемы доставки."""
        try:
            self._telegram.send_message(chat_id, text, reply_to=message.get("message_id"))
        except TelegramError as exc:
            logger.error("sendMessage: %s", exc)


def check_services(settings: Settings) -> bool:
    """Проверяет настройки и доступность Telegram, DeepSeek и Supabase."""
    print("Проверка настроек и сервисов")
    print("-" * 46)
    problems = settings.problems()
    for problem in problems:
        print("✗", problem)
    if problems:
        print("\nЗаполните .env (см. .env.example) и повторите --check.")
        return False
    print("✓ обязательные переменные заданы")

    ok = True
    try:
        me = TelegramBot(settings.telegram_token, timeout=settings.request_timeout).get_me()
        print(f"✓ Telegram: @{me.get('username')} (id {me.get('id')})")
    except TelegramError as exc:
        ok = False
        print("✗ Telegram:", exc)

    deepseek_problem = check_api_key(
        settings.deepseek_key,
        base_url=settings.deepseek_base_url,
        timeout=settings.request_timeout,
    )
    if deepseek_problem:
        ok = False
        print("✗", deepseek_problem)
    else:
        print(f"✓ DeepSeek: ключ принят, модель {settings.deepseek_model}")

    try:
        storage = SupabaseStorage(
            settings.supabase_url, settings.supabase_key,
            debts_table=settings.debts_table, settings_table=settings.settings_table,
            timeout=settings.request_timeout,
        )
        debts = storage.list_debts(0)
        print(f"✓ Supabase: таблица {settings.debts_table} доступна (пробный запрос: {len(debts)} строк)")
        storage.get_default_currency(0, settings.default_currency)
        print(f"✓ Supabase: таблица {settings.settings_table} доступна")
    except StorageError as exc:
        ok = False
        print("✗ Supabase:", exc)

    print()
    print("Итог:", "всё готово — запускайте python bot.py" if ok
          else "есть проблемы — исправьте и повторите --check")
    return ok


DEMO_MESSAGES = (
    "Леша должен Диме 3 рубля",
    "Маша заняла у Пети 10$",
    "покажи долги",
    "валюта по умолчанию доллар",
    "Петя должен Маше 5 долларов",
    "покажи долги",
    "Леша должен Диме 2 рубля",
    "/debts",
    "/currency BYN",
    "привет",
)


def run_demo() -> int:
    """Прогон сценария без внешних сервисов: хранилище в памяти + офлайн-разбор."""
    settings = Settings(default_currency="BYN")
    storage = InMemoryStorage(default_currency=settings.default_currency)
    parser = HeuristicParser()
    chat_id = 1
    print("Демонстрация работы бота (без Telegram, DeepSeek и Supabase)")
    print("=" * 64)
    for message in DEMO_MESSAGES:
        reply = handle_text(message, chat_id, storage=storage, parser=parser, settings=settings)
        print(f"\n👤 {message}\n🤖 {reply}")
    print("=" * 64)
    print(f"Итого записей в памяти: {len(storage.debts)}")
    return 0


def configure_stdout() -> None:
    """Переключает вывод в UTF-8: иначе консоль Windows (cp1252/cp866) падает на русском тексте."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description="Телеграм-бот «калькулятор долгов»: DeepSeek разбирает сообщения, Supabase хранит данные.",
    )
    parser.add_argument("--check", action="store_true", help="проверить настройки и сервисы и выйти")
    parser.add_argument("--demo", action="store_true", help="демонстрация без Telegram (в памяти)")
    parser.add_argument("--poll-timeout", type=int, default=25, help="время ожидания апдейтов, сек")
    parser.add_argument(
        "--once",
        action="store_true",
        help="обработать накопившиеся сообщения и выйти (режим GitHub Actions / cron)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа: рабочий режим, --check или --demo."""
    configure_stdout()
    args = build_parser().parse_args(argv)
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format=LOG_FORMAT,
    )

    if args.demo:
        return run_demo()
    if args.check:
        return 0 if check_services(settings) else 1

    try:
        require_settings(settings)
        storage = SupabaseStorage(
            settings.supabase_url,
            settings.supabase_key,
            debts_table=settings.debts_table,
            settings_table=settings.settings_table,
            state_table=settings.state_table,
            timeout=settings.request_timeout,
        )
        parser = DeepSeekParser(
            settings.deepseek_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout=settings.request_timeout,
        )
        telegram = TelegramBot(settings.telegram_token, timeout=settings.request_timeout)
    except (ConfigError, StorageError, TelegramError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    bot = DebtBot(settings, storage, parser, telegram)
    try:
        if args.once:
            processed = bot.run_once()
            logger.info("Режим --once завершён, обработано сообщений: %d", processed)
            return 0
        bot.run(poll_timeout=max(5, args.poll_timeout))
    except (TelegramError, StorageError) as exc:
        logger.error("Сбой: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
