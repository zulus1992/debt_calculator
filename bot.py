# -*- coding: utf-8 -*-
"""Телеграм-бот «калькулятор долгов»: DeepSeek разбирает сообщение, Supabase хранит долги.

Режимы:
    python bot.py            # постоянный процесс (long polling), ответы мгновенно — для хостинга
    python bot.py --once     # обработать накопившееся и выйти — для GitHub Actions/cron
    python bot.py --check    # проверить настройки и доступность сервисов
    python bot.py --demo     # демонстрация без Telegram (в памяти, офлайн-разбор)

Вебхук (мгновенные ответы на serverless-хостингах — Vercel, PythonAnywhere, WSGI):
    python bot.py --set-webhook https://<домен>/api/telegram   # Telegram шлёт апдейты нам
    python bot.py --webhook-info                               # что сейчас настроено
    python bot.py --delete-webhook                             # вернуться на long polling

Важно: одновременно должен работать только ОДИН режим — Telegram отдаёт апдейты
одному «слушателю», второй получит HTTP 409 Conflict или потеряет сообщения.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Any, Mapping

from config import (
    ConfigError,
    Settings,
    load_settings,
    require_settings,
    webhook_secret_problem,
)
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


def build_runtime(settings: Settings) -> tuple[Storage, DeepSeekParser, TelegramBot]:
    """Собирает рабочие сервисы: хранилище Supabase, парсер DeepSeek, клиент Telegram.

    Используется и постоянным процессом (bot.py), и вебхуком (webhook.py).
    """
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
    return storage, parser, telegram


class DebtBot:
    """Длинный опрос Telegram и обработка входящих сообщений."""

    def __init__(self, settings: Settings, storage: Storage, parser: Any,
                 telegram: TelegramBot) -> None:
        self._settings = settings
        self._storage = storage
        self._parser = parser
        self._telegram = telegram
        self._stop = False

    def run(self, poll_timeout: int = 25, max_updates: int | None = None) -> int:
        """Постоянный режим (long polling): ответы приходят мгновенно.

        Используется на хостинге: процесс живёт всё время, при остановке контейнера
        (SIGTERM/SIGINT) корректно завершается и сохраняет смещение в bot_state,
        чтобы после перезапуска или возврата к режиму `--once` ничего не путалось.
        """
        me = self._telegram.get_me()
        offset = self._load_offset()
        logger.info(
            "Бот @%s (id %s) запущен (long polling). Стартовое смещение: %s",
            me.get("username"), me.get("id"), offset,
        )
        self._install_signal_handlers()
        processed = 0
        try:
            while not self._stop:
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
                if not updates and max_updates is not None:
                    # Тестовый/отладочный режим: не крутимся вхолостую на пустой пачке
                    # (в обычном режиме getUpdates ждёт сообщения до poll_timeout секунд).
                    break
        finally:
            self._save_offset(offset)
        logger.info("Остановлено. Обработано сообщений за сессию: %d", processed)
        return processed

    def _install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT -> мягкая остановка (контейнеры хостинга гасят процесс именно так)."""
        def handler(signum: int, _frame: Any) -> None:
            logger.info("Получен сигнал %s — останавливаюсь.", signum)
            self._stop = True

        for name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # не главный поток — просто пропускаем
                pass

    def _save_offset(self, offset: int | None) -> None:
        """Сохраняет смещение апдейтов; ошибки только логируются.

        Значение никогда не уменьшается: при вебхуке в несколько инстансов (serverless)
        «опоздавший» запрос не должен откатить смещение — иначе уже обработанное
        сообщение прилетит повторно и долг запишется дважды.

        В постоянном режиме это ещё и «удобство для переезда»: если сохранение не удалось,
        цикл опроса не должен из-за этого падать (в режиме --once ошибка фатальна,
        потому что там смещение защищает от повторной обработки сообщений).
        """
        if offset is None:
            return
        try:
            current = self._load_offset()
            if current is not None and current >= offset:
                return
            self._storage.set_state(LAST_UPDATE_ID_KEY, str(offset))
        except StorageError as exc:
            logger.warning("Не удалось сохранить смещение апдейтов: %s", exc)

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

    def process_update(self, update: Mapping[str, Any], *, remember: bool = True) -> bool:
        """Обрабатывает один апдейт с защитой от повторов — режим вебхука.

        Telegram повторяет доставку, если эндпоинт ответил ошибкой или не успел ответить,
        поэтому апдейты с уже пройденным update_id отбрасываются. Смещение хранится там же,
        где его использует long polling (`bot_state.last_update_id`), так что переключение
        между режимами не теряет и не дублирует сообщения.

        Возвращает True, если апдейт был обработан (False — это повтор).
        """
        update_id = int(update.get("update_id") or 0)
        offset = self._load_offset()
        if update_id and offset is not None and update_id < offset:
            logger.info("Повтор апдейта %s (смещение %s) — пропускаю.", update_id, offset)
            return False
        self._process(update)
        if remember and update_id:
            self._save_offset(update_id + 1)
        return True

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


def _telegram_for(settings: Settings) -> TelegramBot:
    """Клиент Telegram с проверкой настроек (для команд управления вебхуком)."""
    problems = settings.problems()
    if problems:
        raise ConfigError("Проверьте настройки:\n- " + "\n- ".join(problems))
    return TelegramBot(settings.telegram_token, timeout=settings.request_timeout)


def set_webhook_mode(settings: Settings, url: str, *, drop_pending: bool = False) -> int:
    """Команда --set-webhook: Telegram сам присылает апдейты на наш HTTPS-эндпоинт."""
    url = (url or "").strip()
    if not url.startswith("https://"):
        print(
            "Ошибка: адрес вебхука должен начинаться с https:// — Telegram не доставляет "
            "апдейты по http. Для локальной проверки используйте туннель "
            "(ngrok http 8080 / cloudflared tunnel --url http://127.0.0.1:8080).",
            file=sys.stderr,
        )
        return 1
    secret_problem = webhook_secret_problem(settings.webhook_secret)
    if secret_problem:
        print("Ошибка:", secret_problem, file=sys.stderr)
        return 1
    try:
        telegram = _telegram_for(settings)
        telegram.set_webhook(
            url,
            secret_token=settings.webhook_secret,
            drop_pending_updates=drop_pending,
        )
        info = telegram.get_webhook_info()
    except (ConfigError, TelegramError) as exc:
        print("Ошибка:", exc, file=sys.stderr)
        return 1

    print("✓ Вебхук установлен:", info.get("url"))
    print(f"  ожидает апдейтов: {info.get('pending_update_count', 0)}")
    print("  Теперь напишите боту — ответ придёт за 1–3 секунды (задержка = запрос к DeepSeek).")
    print("  Вернуться на long polling: python bot.py --delete-webhook")
    print("  ⚠ Постоянный процесс (`python bot.py`, cron в Actions) должен быть остановлен:")
    print("    по одному адресу Telegram шлёт апдейты только одним способом.")
    return 0


def delete_webhook_mode(settings: Settings, *, drop_pending: bool = False) -> int:
    """Команда --delete-webhook: снова long polling / --once."""
    try:
        telegram = _telegram_for(settings)
        telegram.delete_webhook(drop_pending_updates=drop_pending)
        info = telegram.get_webhook_info()
    except (ConfigError, TelegramError) as exc:
        print("Ошибка:", exc, file=sys.stderr)
        return 1

    if info.get("url"):
        print("⚠ Telegram всё ещё сообщает адрес вебхука:", info.get("url"), file=sys.stderr)
        return 1
    print("✓ Вебхук снят — бот снова работает через getUpdates (python bot.py или --once).")
    return 0


def show_webhook_info(settings: Settings) -> int:
    """Команда --webhook-info: что сейчас настроено в Telegram."""
    try:
        info = _telegram_for(settings).get_webhook_info()
    except (ConfigError, TelegramError) as exc:
        print("Ошибка:", exc, file=sys.stderr)
        return 1

    url = str(info.get("url") or "")
    print("Режим:", f"вебхук {url}" if url else "long polling (вебхук не установлен)")
    print("Ожидает апдейтов:", info.get("pending_update_count", 0))
    if info.get("last_error_date"):
        print("Последняя ошибка доставки:", info.get("last_error_message"))
    if info.get("ip_address"):
        print("Адрес сервера Telegram:", info.get("ip_address"))
    return 0


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
        telegram = TelegramBot(settings.telegram_token, timeout=settings.request_timeout)
        me = telegram.get_me()
        print(f"✓ Telegram: @{me.get('username')} (id {me.get('id')})")
        info = telegram.get_webhook_info()
        url = str(info.get("url") or "")
        if url:
            print(f"✓ Telegram: включён вебхук — {url}")
            print(f"  ожидает апдейтов: {info.get('pending_update_count', 0)}")
            if info.get("last_error_message"):
                print("  ⚠ последняя ошибка доставки:", info["last_error_message"])
            secret_problem = webhook_secret_problem(settings.webhook_secret)
            if secret_problem:
                ok = False
                print("  ✗", secret_problem)
            else:
                print("  ✓ секрет вебхука задан — заголовки запросов проверяются")
        else:
            print("• Telegram: вебхук не установлен — режимы: python bot.py (long polling)",
                  "или --once (раз в 30 минут)")
            print("  включить вебхук: python bot.py --set-webhook https://<домен>/api/telegram")
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
    parser.add_argument(
        "--set-webhook",
        metavar="URL",
        help="включить режим вебхука: Telegram шлёт апдейты на этот HTTPS-адрес",
    )
    parser.add_argument(
        "--delete-webhook",
        action="store_true",
        help="выключить вебхук и вернуться на long polling",
    )
    parser.add_argument(
        "--webhook-info",
        action="store_true",
        help="показать, как Telegram доставляет апдейты (вебхук или getUpdates)",
    )
    parser.add_argument(
        "--drop-pending",
        action="store_true",
        help="вместе с --set-webhook/--delete-webhook: выбросить накопившиеся апдейты",
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
    if args.set_webhook:
        return set_webhook_mode(settings, args.set_webhook, drop_pending=args.drop_pending)
    if args.delete_webhook:
        return delete_webhook_mode(settings, drop_pending=args.drop_pending)
    if args.webhook_info:
        return show_webhook_info(settings)

    try:
        require_settings(settings)
        storage, parser, telegram = build_runtime(settings)
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
