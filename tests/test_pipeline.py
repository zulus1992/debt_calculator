# -*- coding: utf-8 -*-
"""Тесты без внешних сервисов: разбор сообщений, запись, взаимозачёт, ответы бота.

Запуск:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import io
import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence

from bot import (
    DebtBot,
    HeuristicParser,
    TxtReport,
    addressing,
    clean_bot_mention,
    handle_text,
    is_allowed,
    is_command_for_bot,
    mentions_bot,
)
from config import (
    ConfigError,
    Settings,
    jwt_role,
    load_settings,
    supabase_key_problem,
    webhook_secret_problem,
)
from debts import (
    DEBTS_DUMP_COLUMNS,
    SETTLE_HINT,
    format_debts_dump,
    format_debts_report,
    minimal_transfers,
    name_key,
    net_balances,
    normalize_name,
    split_amount,
    totals_by_person,
)
from deepseek import (
    SYSTEM_PROMPT,
    DeepSeekParser,
    ParsedMessage,
    detect_currency,
    heuristic_parse,
)
from members import format_roster, member_from_telegram, resolve_member, with_aliases
from rates import (
    RatesError,
    convert_amount,
    convert_debts,
    fetch_latest,
    format_rates_report,
    latest_url,
    minsk_now,
    parse_rates,
    rate_for,
    rate_table,
    rates_day,
    update_rates,
    update_rates_scheduled,
)
from storage import (
    ChatMember,
    Debt,
    InMemoryStorage,
    RATE_DIGITS,
    RATE_SCALE,
    RatePoint,
    StorageError,
    SupabaseStorage,
    is_new_api_key,
    scale_rate,
    unscale_rate,
)
from telegram_api import MAX_CAPTION_LENGTH, TelegramBot, TelegramError, split_message
from webhook import SECRET_HEADER, LazyWebhookApp, WebhookApp, build_app

CHAT = 555


def make_debt(debtor: str, creditor: str, amount: float, currency: str = "BYN",
              chat: int = CHAT, kind: str = "debt",
              user_ids: tuple[int | None, int | None] = (None, None)) -> Debt:
    """Готовит запись (долг или возврат) для проверок логики."""
    return Debt(chat_id=chat, from_name=debtor, to_name=creditor,
                currency=currency, amount=amount, kind=kind,
                from_user_id=user_ids[0], to_user_id=user_ids[1])


class HeuristicParseTests(unittest.TestCase):
    """Разбор сообщений офлайн-эвристиками (фолбэк без DeepSeek)."""

    def test_typical_debt(self) -> None:
        parsed = heuristic_parse("Леша должен Диме 3 рубля")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "debt")
        self.assertEqual(parsed.from_name, "Леша")
        self.assertEqual(parsed.to_name, "Диме")
        self.assertEqual(parsed.amount, 3.0)
        self.assertEqual(parsed.currency, "BYN")

    def test_debt_with_dollar_sign(self) -> None:
        parsed = heuristic_parse("Маша заняла у Пети 10$")
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.from_name, parsed.to_name), ("Маша", "Пети"))
        self.assertEqual(parsed.amount, 10.0)
        self.assertEqual(parsed.currency, "USD")

    def test_amount_before_verb(self) -> None:
        parsed = heuristic_parse("3 рубля: Леша должен Диме")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "debt")
        self.assertEqual(parsed.from_name, "Леша")
        self.assertEqual(parsed.to_name, "Диме")
        self.assertEqual(parsed.amount, 3.0)

    def test_fractional_amount(self) -> None:
        parsed = heuristic_parse("Петя должен Оле 10,50 долларов")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.amount, 10.5)
        self.assertEqual(parsed.currency, "USD")

    def test_debts_query(self) -> None:
        parsed = heuristic_parse("покажи долги")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "debts")

    def test_set_currency(self) -> None:
        parsed = heuristic_parse("валюта по умолчанию доллар")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "set_currency")
        self.assertEqual(parsed.currency, "USD")

    def test_unknown_text(self) -> None:
        self.assertIsNone(heuristic_parse("привет, как дела"))

    def test_currency_detection(self) -> None:
        self.assertEqual(detect_currency("5 евро"), "EUR")
        self.assertEqual(detect_currency("три рубля"), "BYN")
        self.assertEqual(detect_currency("без цифр"), None)


class NameNormalisationTests(unittest.TestCase):
    """Имена: регистр, ё/е и падежи не должны разрывать одного человека."""

    def test_normalize_display(self) -> None:
        self.assertEqual(normalize_name("  леша  "), "Леша")
        self.assertEqual(normalize_name("дима"), "Дима")

    def test_case_insensitive_key(self) -> None:
        self.assertEqual(name_key("Лёша"), name_key("леша"))
        self.assertEqual(name_key("Дима"), name_key("Диме"))

    def test_totals_merge_cases(self) -> None:
        debts = [make_debt("Леша", "Диме", 3), make_debt("Дима", "Леше", 1)]
        owes, owed = totals_by_person(debts)
        self.assertEqual(set(owes), {"Леша", "Дима"})
        self.assertEqual(owes["Леша"]["BYN"], 3.0)
        self.assertEqual(owes["Дима"]["BYN"], 1.0)
        self.assertEqual(owed["Дима"]["BYN"], 3.0)


class BalanceTests(unittest.TestCase):
    """Взаимозачёт и суммирование долгов."""

    def test_netting_same_pair(self) -> None:
        balances = net_balances([make_debt("Леша", "Дима", 10), make_debt("Дима", "Леша", 4)])
        self.assertEqual(len(balances), 1)
        self.assertEqual((balances[0].debtor, balances[0].creditor, balances[0].amount),
                         ("Леша", "Дима", 6.0))

    def test_netting_reverse_wins(self) -> None:
        # Леша должен 3, но Дима должен 8 → сальдо в сторону Димы: Дима → Леша 5
        balances = net_balances([make_debt("Леша", "Дима", 3), make_debt("Диме", "Леша", 8)])
        self.assertEqual((balances[0].debtor, balances[0].creditor, balances[0].amount),
                         ("Дима", "Леша", 5.0))

    def test_full_offset_disappears(self) -> None:
        self.assertEqual(net_balances([make_debt("A", "B", 5), make_debt("B", "A", 5)]), [])

    def test_currencies_are_separate(self) -> None:
        balances = net_balances([
            make_debt("Леша", "Дима", 10, "BYN"),
            make_debt("Дима", "Леша", 4, "USD"),
        ])
        self.assertEqual(len(balances), 2)

    def test_self_debt_ignored(self) -> None:
        self.assertEqual(net_balances([make_debt("Леша", "Леша", 5)]), [])

    def test_case_and_yo_insensitive(self) -> None:
        balances = net_balances([make_debt("лёша", "дима", 5), make_debt("Леша", "Диму", 2)])
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].amount, 7.0)


class FakeParser:
    """Подменяет DeepSeek: возвращает заранее заданный разбор."""

    def __init__(self, parsed: ParsedMessage) -> None:
        self.parsed = parsed

    def parse(self, text: str, default_currency: str = "BYN", **_kwargs) -> ParsedMessage:
        """Всегда отдаёт подготовленный результат (состав чата не используется)."""
        return self.parsed


class BotFlowTests(unittest.TestCase):
    """Сквозной сценарий: сообщение → разбор → запись → отчёт."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage)     # без /reg записи не сохраняются

    def send(self, text: str, chat: int = CHAT) -> str:
        """Отправляет сообщение боту (автор — Леша Козлов) и возвращает ответ."""
        members = self.members if chat == CHAT else seed_chat(self.storage, chat)
        return handle_text(text, chat, storage=self.storage, parser=self.parser,
                           settings=self.settings, members=members,
                           author=replace(MEMBER_LEHA, chat_id=chat))

    def test_saves_debt_with_explicit_currency(self) -> None:
        reply = self.send("Леша должен Диме 3 рубля")
        self.assertIn("Записал долг", reply)
        self.assertIn("3.00 BYN", reply)
        self.assertNotIn("взял по умолчанию", reply)
        self.assertEqual(len(self.storage.debts), 1)

    def test_uses_default_currency_silently(self) -> None:
        # Валюту по умолчанию применяем, но отдельной строкой об этом не сообщаем.
        reply = self.send("Леша должен Диме 3")
        self.assertIn("3.00 BYN", reply)
        self.assertNotIn("взял по умолчанию", reply)

    def test_default_currency_can_be_changed(self) -> None:
        self.send("/currency USD")
        reply = self.send("Маша должна Оле 7")
        self.assertIn("7.00 USD", reply)
        self.assertEqual(self.storage.get_default_currency(CHAT), "USD")

    def test_report_uses_netting(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        self.send("Дима должен Леше 1 рубль")
        report = self.send("/debts")
        self.assertIn("Итог с взаимозачётом", report)
        self.assertIn("Леша Козлов (@kozlovAlex) → Дмитрий Болт (@bdzmity): 2.00 BYN", report)

    def test_report_when_empty(self) -> None:
        self.assertIn("Долгов нет", self.send("/debts"))

    def test_reset_clears_chat(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        self.assertIn("Удалено записей: 1", self.send("/reset"))
        self.assertEqual(self.storage.list_debts(CHAT), [])

    def test_unknown_message(self) -> None:
        self.assertIn("Не понял", self.send("привет, как дела"))

    def test_chats_are_isolated(self) -> None:
        self.send("Леша должен Диме 3 рубля", chat=1)
        self.assertIn("Долгов нет", self.send("/debts", chat=2))

    def test_help_command(self) -> None:
        self.assertIn("Калькулятор долгов", self.send("/help"))


class DeepSeekPathTests(unittest.TestCase):
    """Путь через ИИ: ответы DeepSeek обрабатываются корректно (парсер подменён)."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.members = seed_chat(self.storage)

    def send_with(self, parsed: ParsedMessage, text: str = "текст") -> str:
        """Обрабатывает сообщение с заранее заданным ответом «ИИ»."""
        return handle_text(text, CHAT, storage=self.storage,
                           parser=FakeParser(parsed), settings=self.settings,
                           members=self.members, author=MEMBER_LEHA)

    def test_ai_debt_is_saved(self) -> None:
        reply = self.send_with(ParsedMessage(
            intent="debt", from_name="петя", to_name="Оля", amount=12.5, currency="eur",
        ))
        self.assertIn("12.50 EUR", reply)
        saved = self.storage.list_debts(CHAT)[0]
        self.assertEqual((saved.from_name, saved.to_name), ("Петя Кузнецов", "Оля Смирнова"))
        self.assertEqual((saved.from_user_id, saved.to_user_id), (105, 104))

    def test_ai_debt_without_amount_asks_for_details(self) -> None:
        reply = self.send_with(ParsedMessage(intent="debt", from_name="Леша", to_name="Дима"))
        self.assertIn("не хватает данных", reply)
        self.assertEqual(self.storage.list_debts(CHAT), [])

    def test_ai_currency_change(self) -> None:
        reply = self.send_with(ParsedMessage(intent="set_currency", currency="PLN"))
        self.assertIn("PLN", reply)
        self.assertEqual(self.storage.get_default_currency(CHAT), "PLN")

    def test_ai_none_shows_note(self) -> None:
        reply = self.send_with(ParsedMessage(intent="none", note="это не про долги"))
        self.assertIn("Не понял", reply)
        self.assertIn("это не про долги", reply)


class AccessAndConfigTests(unittest.TestCase):
    """Доступ по списку пользователей и чтение настроек."""

    def test_allowed_for_everyone_by_default(self) -> None:
        self.assertTrue(is_allowed(None, Settings()))
        self.assertTrue(is_allowed(42, Settings()))

    def test_restricted_users(self) -> None:
        settings = Settings(allowed_user_ids=frozenset({1, 2}))
        self.assertTrue(is_allowed(2, settings))
        self.assertFalse(is_allowed(3, settings))
        self.assertFalse(is_allowed(None, settings))

    def test_settings_from_env(self) -> None:
        settings = load_settings(
            {
                "TELEGRAM_BOT_TOKEN": " '123:abc' ",
                "DEEPSEEK_API_KEY": "sk-test",
                "SUPABASE_URL": "https://example.supabase.co/",
                "SUPABASE_SECRET_KEY": "sb_secret_test_key",
                "ALLOWED_USER_IDS": "1, 2;3",
                "DEFAULT_CURRENCY": "usd",
            },
            use_env_file=False,
        )
        self.assertEqual(settings.telegram_token, "123:abc")
        self.assertEqual(settings.supabase_url, "https://example.supabase.co")
        self.assertEqual(settings.supabase_key, "sb_secret_test_key")   # новый secret-ключ
        self.assertEqual(settings.rest_url, "https://example.supabase.co/rest/v1")
        self.assertEqual(settings.allowed_user_ids, frozenset({1, 2, 3}))
        self.assertEqual(settings.default_currency, "USD")
        self.assertEqual(settings.problems(), [])

    def test_problems_when_settings_empty(self) -> None:
        self.assertEqual(len(load_settings({}, use_env_file=False).problems()), 4)

    def test_new_env_settings(self) -> None:
        settings = load_settings({
            "CHAT_PASSWORD": " 'сезам' ",
            "RATES_API_KEY": "test-key",
            "RATES_CURRENCIES": "byn, usd;thb",
            "RATES_BASE": "byn",
            "RATES_OPEN_URL": "https://open.er-api.com/v6/",
        }, use_env_file=False)
        self.assertEqual(settings.chat_password, "сезам")
        self.assertTrue(settings.password_required)
        self.assertEqual(settings.rates_currencies, ("BYN", "USD", "THB"))
        self.assertEqual(settings.rates_base, "BYN")
        self.assertEqual(settings.rates_open_url, "https://open.er-api.com/v6")
        self.assertIsNone(settings.rates_problem())

    def test_defaults_for_password_and_rates(self) -> None:
        settings = load_settings({}, use_env_file=False)
        self.assertFalse(settings.password_required)      # без пароля бот работает везде
        self.assertEqual(settings.rates_base, "BYN")
        self.assertEqual(settings.rates_currencies, ("BYN", "RUB", "USD", "EUR", "CNY", "THB"))
        self.assertEqual(settings.rates_api_url, "https://v6.exchangerate-api.com/v6")
        self.assertEqual(settings.rates_open_url, "https://open.er-api.com/v6")
        # без ключа тоже работаем — через открытый эндпоинт
        self.assertIsNone(settings.rates_problem())
        self.assertIn("open.er-api.com", settings.rates_source)
        with_key = load_settings({"RATES_API_KEY": "test-key"}, use_env_file=False)
        self.assertIn("ключ задан", with_key.rates_source)
        self.assertIsNotNone(Settings(rates_open_url="").rates_problem())


class TelegramHelpersTests(unittest.TestCase):
    """Вспомогательные функции Telegram-клиента."""

    def test_split_short_message(self) -> None:
        self.assertEqual(split_message("привет"), ["привет"])

    def test_split_long_message(self) -> None:
        text = "Строка сообщения\n" * 500
        parts = split_message(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 4096 for part in parts))
        self.assertEqual("".join(parts), text)


class FakeTelegram:
    """Подменяет Telegram: отдаёт заранее заданные апдейты и запоминает ответы."""

    def __init__(self, updates: list[dict]) -> None:
        self.updates = list(updates)
        self.sent: list[tuple[int, str]] = []
        self.documents: list[tuple[int, str, str]] = []
        self.offsets: list[int | None] = []

    def get_me(self) -> dict:
        """Имя бота для логов."""
        return {"id": 1, "username": "test_bot"}

    def get_updates(self, offset=None, *, poll_timeout: int = 30, limit: int = 20) -> list[dict]:
        """Отдаёт подготовленные апдейты и запоминает запрошенное смещение."""
        self.offsets.append(offset)
        return self.updates

    def send_message(self, chat_id, text: str, *, reply_to=None, silent: bool = False) -> list[dict]:
        """Запоминает отправленный ответ."""
        self.sent.append((int(chat_id), text))
        return [{}]

    def send_document(self, chat_id, filename: str, content: str, *, caption: str = "",
                      reply_to=None, silent: bool = False) -> dict:
        """Запоминает отправленный файл (TXT-отчёт)."""
        self.documents.append((int(chat_id), filename, content))
        return {}

    def send_typing(self, chat_id) -> None:
        """Имитация индикатора «печатает»."""


def make_update(update_id: int, text: str, chat: int = 7, user: int = 100) -> dict:
    """Готовит апдейт Telegram с текстовым сообщением."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat},
            "from": {"id": user},
            "text": text,
        },
    }


class BotRunTests(unittest.TestCase):
    """Проверка доступа: недопущенному пользователю бот отвечает отказом."""

    def build(self, updates: list[dict], **settings_kwargs):
        """Собирает бота с хранилищем в памяти и фейковым Telegram."""
        settings = Settings(default_currency="BYN", **settings_kwargs)
        storage = InMemoryStorage(default_currency="BYN")
        seed_chat(storage, chat=7)                 # участники чата из make_update
        telegram = FakeTelegram(updates)
        return DebtBot(settings, storage, HeuristicParser(), telegram), storage, telegram

    def test_denied_user_gets_refusal(self) -> None:
        bot, storage, telegram = self.build(
            [make_update(5, "Леша должен Диме 3 рубля", user=999)],
            allowed_user_ids=frozenset({100}),
        )
        bot.run(poll_timeout=0, max_updates=1)
        self.assertEqual(storage.list_debts(7), [])
        self.assertIn("настроен только", telegram.sent[0][1])


# Секрет вебхука в тестах: то же значение хелпер отправляет в заголовке по умолчанию.
TEST_SECRET = "test-secret_123"


def call_wsgi(app, *, method: str = "POST", path: str = "/api/telegram",
              update: dict | None = None, secret: str | None = TEST_SECRET,
              raw_body: bytes | None = None) -> tuple[str, str, dict[str, str]]:
    """Вызывает WSGI-приложение и возвращает статус, тело и заголовки ответа."""
    if raw_body is not None:
        payload = raw_body
    else:
        payload = json.dumps(update or {}, ensure_ascii=False).encode("utf-8")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(payload)),
        "CONTENT_TYPE": "application/json",
        "wsgi.input": io.BytesIO(payload),
        "wsgi.errors": io.StringIO(),
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "wsgi.url_scheme": "https",
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "443",
        "REMOTE_ADDR": "149.154.167.220",   # диапазон серверов Telegram
    }
    if secret is not None:
        environ[SECRET_HEADER] = secret
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list, exc_info: Any = None) -> None:
        """Запоминает статус и заголовки ответа."""
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = app(environ, start_response)
    return captured["status"], b"".join(chunks).decode("utf-8"), captured["headers"]


class WebhookAppTests(unittest.TestCase):
    """Режим вебхука: секрет, обработка апдейта и защита от повторов."""

    SECRET = TEST_SECRET

    def build(self, *, secret: str | None = None, **settings_kwargs):
        """Приложение вебхука с хранилищем в памяти и подменённым Telegram."""
        settings = Settings(
            default_currency="BYN",
            webhook_secret=self.SECRET if secret is None else secret,
            **settings_kwargs,
        )
        storage = InMemoryStorage(default_currency="BYN")
        seed_chat(storage, chat=7)                 # участники чата из make_update
        telegram = FakeTelegram([])
        app = build_app(settings, storage=storage, parser=HeuristicParser(), telegram=telegram)
        return app, storage, telegram

    def test_get_is_health_check(self) -> None:
        app, _, _ = self.build()
        status, body, _ = call_wsgi(app, method="GET", path="/")
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, "ok")

    def test_debt_is_saved_and_answered(self) -> None:
        app, storage, telegram = self.build()
        status, body, _ = call_wsgi(app, update=make_update(10, "Леша должен Диме 3 рубля"),
                                    secret=self.SECRET)
        self.assertEqual(status, "200 OK")
        self.assertIn('"accepted": true', body)
        self.assertEqual(len(storage.list_debts(7)), 1)
        self.assertIn("Записал долг", telegram.sent[0][1])
        self.assertEqual(storage.get_state("last_update_id"), "11")

    def test_wrong_secret_is_rejected(self) -> None:
        app, storage, telegram = self.build()
        status, _, _ = call_wsgi(app, update=make_update(10, "Леша должен Диме 3 рубля"),
                                 secret="не тот секрет")
        self.assertEqual(status, "403 Forbidden")
        self.assertEqual(storage.list_debts(7), [])
        self.assertEqual(telegram.sent, [])

    def test_missing_header_is_rejected(self) -> None:
        app, storage, _ = self.build()
        status, _, _ = call_wsgi(app, update=make_update(10, "/debts"), secret=None)
        self.assertEqual(status, "403 Forbidden")
        self.assertEqual(storage.list_debts(7), [])

    def test_without_configured_secret_fails_closed(self) -> None:
        app, storage, _ = self.build(secret="")
        status, body, _ = call_wsgi(app, update=make_update(10, "Леша должен Диме 3 рубля"))
        self.assertEqual(status, "500 Internal Server Error")
        self.assertIn("WEBHOOK_SECRET", body)
        self.assertEqual(storage.list_debts(7), [])

    def test_duplicate_update_is_skipped(self) -> None:
        app, storage, telegram = self.build()
        storage.set_state("last_update_id", "11")   # апдейт 10 уже обработан (в polling/до перезапуска)
        status, body, _ = call_wsgi(app, update=make_update(10, "Леша должен Диме 3 рубля"))
        self.assertEqual(status, "200 OK")
        self.assertIn('"accepted": false', body)
        self.assertEqual(storage.list_debts(7), [])
        self.assertEqual(telegram.sent, [])

    def test_invalid_json_is_reported(self) -> None:
        app, _, _ = self.build()
        status, body, _ = call_wsgi(app, raw_body="{это не json".encode("utf-8"))
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("JSON", body)

    def test_other_methods_are_not_allowed(self) -> None:
        app, _, _ = self.build()
        status, _, _ = call_wsgi(app, method="PUT")
        self.assertEqual(status, "405 Method Not Allowed")

    def test_update_without_message_is_accepted_silently(self) -> None:
        app, storage, telegram = self.build()
        status, body, _ = call_wsgi(app, update={"update_id": 5, "edited_message": {}})
        self.assertEqual(status, "200 OK")
        self.assertIn('"accepted": true', body)
        self.assertEqual(telegram.sent, [])
        self.assertEqual(storage.get_state("last_update_id"), "6")

    def test_denied_user_through_webhook(self) -> None:
        app, storage, telegram = self.build(allowed_user_ids=frozenset({100}))
        call_wsgi(app, update=make_update(7, "Леша должен Диме 3 рубля", user=999))
        self.assertEqual(storage.list_debts(7), [])
        self.assertIn("настроен только", telegram.sent[0][1])


class LazyWebhookAppTests(unittest.TestCase):
    """serverless (Vercel): приложение собирается лениво, при первом апдейте."""

    def test_health_answers_even_when_settings_broken(self) -> None:
        def broken() -> WebhookApp:
            """Ломается так же, как боевая сборка при пустых ключах."""
            raise ConfigError("не хватает ключей")

        lazy = LazyWebhookApp(broken)
        status, body, _ = call_wsgi(lazy, method="GET")
        self.assertEqual(status, "200 OK")           # «пингер» видит живой сервис
        self.assertIn("ok", body)

        status, body, _ = call_wsgi(lazy, update=make_update(1, "/help"), secret=TEST_SECRET)
        self.assertEqual(status, "500 Internal Server Error")
        self.assertIn("не хватает ключей", body)

    def test_factory_runs_once_and_updates_are_processed(self) -> None:
        settings = Settings(default_currency="BYN", webhook_secret=TEST_SECRET)
        storage = InMemoryStorage(default_currency="BYN")
        seed_chat(storage, chat=7)                  # участники чата из make_update
        telegram = FakeTelegram([])
        calls: list[int] = []

        def factory() -> WebhookApp:
            """Считает, сколько раз собиралось приложение (на serverless это cold start)."""
            calls.append(1)
            return build_app(settings, storage=storage, parser=HeuristicParser(), telegram=telegram)

        lazy = LazyWebhookApp(factory)
        first = call_wsgi(lazy, update=make_update(10, "Леша должен Диме 3 рубля"), secret=TEST_SECRET)
        second = call_wsgi(lazy, update=make_update(11, "/debts"), secret=TEST_SECRET)
        self.assertEqual((first[0], second[0]), ("200 OK", "200 OK"))
        self.assertEqual(len(calls), 1)              # приложение собралось один раз
        self.assertEqual(len(storage.list_debts(7)), 1)
        self.assertIn("Записал долг", telegram.sent[0][1])
        self.assertIn("Итог с взаимозачётом", telegram.sent[1][1])


class WebhookSecretTests(unittest.TestCase):
    """Секрет вебхука: формат проверяется заранее, для long polling он не нужен."""

    def test_secret_is_not_required_for_polling(self) -> None:
        settings = load_settings({}, use_env_file=False)
        self.assertEqual(settings.webhook_secret, "")
        self.assertEqual(len(settings.problems()), 4)   # те же 4 проблемы, что и раньше

    def test_secret_is_read_from_env(self) -> None:
        settings = load_settings({"WEBHOOK_SECRET": " 'abc_123-XYZ' "}, use_env_file=False)
        self.assertEqual(settings.webhook_secret, "abc_123-XYZ")

    def test_secret_format(self) -> None:
        self.assertIsNone(webhook_secret_problem("abc_123-XYZ"))
        self.assertIsNone(webhook_secret_problem("a" * 256))
        self.assertIn("WEBHOOK_SECRET", webhook_secret_problem("") or "")
        self.assertIn("недопустимые", webhook_secret_problem("плохой секрет") or "")
        self.assertIn("недопустимые", webhook_secret_problem("a" * 257) or "")


class FakeResponse:
    """Ответ HTTP-сессии: заданный JSON и статус."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self) -> Any:
        """Тело ответа как объект."""
        return self._payload


class FakeSession:
    """Подменяет requests: записывает вызовы и отдаёт заготовленные ответы."""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def _next(self) -> FakeResponse:
        """Следующий заготовленный ответ."""
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse({"ok": True, "result": []})

    def post(self, url: str, json: Any = None, timeout: float | None = None,
             data: Any = None, files: Any = None, **kwargs: Any) -> FakeResponse:
        """Имитация requests.post (Telegram, DeepSeek).

        Отправка файла (sendDocument) уходит полями data/files — их тоже запоминаем.
        """
        self.calls.append({"method": "POST", "url": url, "payload": json,
                           "data": data, "files": files, "timeout": timeout})
        return self._next()

    def request(self, method: str, url: str, params: Any = None, json: Any = None,
                headers: Any = None, timeout: float | None = None,
                **kwargs: Any) -> FakeResponse:
        """Имитация requests.request (PostgREST/Supabase)."""
        self.calls.append({
            "method": method, "url": url, "params": params, "payload": json,
            "headers": dict(headers or {}), "timeout": timeout,
        })
        return self._next()

    def get(self, url: str, params: Any = None, headers: Any = None,
            timeout: float | None = None, **kwargs: Any) -> FakeResponse:
        """Имитация requests.get (курсы валют ExchangeRate-API)."""
        self.calls.append({
            "method": "GET", "url": url, "params": params, "payload": None,
            "headers": dict(headers or {}), "timeout": timeout,
        })
        return self._next()


class TelegramClientTests(unittest.TestCase):
    """Сетевой слой Telegram: проверяем содержимое запросов.

    Регрессия: раньше get_updates падал с TypeError, потому что HTTP-таймаут и
    параметр Telegram timeout передавались в call() под одним именем.
    """

    def setUp(self) -> None:
        self.session = FakeSession()
        self.bot = TelegramBot("123:abc", session=self.session)

    def test_get_updates_payload(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": [{"update_id": 1}]})]
        updates = self.bot.get_updates(5, poll_timeout=25)
        self.assertEqual(updates, [{"update_id": 1}])
        call = self.session.calls[0]
        self.assertTrue(call["url"].endswith("/getUpdates"))
        self.assertEqual(call["payload"]["timeout"], 25)
        self.assertEqual(call["payload"]["offset"], 5)
        self.assertGreater(call["timeout"], 25)  # HTTP-таймаут больше ожидания Telegram

    def test_get_me(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": {"id": 1, "username": "bot"}})]
        self.assertEqual(self.bot.get_me()["username"], "bot")

    def test_send_message_splits_and_replies_only_once(self) -> None:
        self.session.responses = [
            FakeResponse({"ok": True, "result": {"message_id": 1}}),
            FakeResponse({"ok": True, "result": {"message_id": 2}}),
        ]
        sent = self.bot.send_message(7, "строка\n" * 1000, reply_to=42)
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.session.calls[0]["payload"]["reply_to_message_id"], 42)
        self.assertNotIn("reply_to_message_id", self.session.calls[1]["payload"])

    def test_error_mapping(self) -> None:
        self.session.responses = [FakeResponse({"ok": False, "description": "nope"}, status=401)]
        with self.assertRaises(TelegramError) as ctx:
            self.bot.get_me()
        self.assertIn("401", str(ctx.exception))

        self.session.responses = [FakeResponse({"ok": False, "description": "conflict"}, status=409)]
        with self.assertRaises(TelegramError) as ctx:
            self.bot.get_updates()
        self.assertIn("409", str(ctx.exception))

        self.session.responses = [FakeResponse({"ok": False, "description": "bad"}, status=400)]
        with self.assertRaises(TelegramError):
            self.bot.send_message(1, "привет")


class TelegramWebhookApiTests(unittest.TestCase):
    """Методы Bot API для вебхука: setWebhook, deleteWebhook, getWebhookInfo."""

    def setUp(self) -> None:
        self.session = FakeSession()
        self.bot = TelegramBot("123:abc", session=self.session)

    def test_set_webhook_payload(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": True})]
        self.assertTrue(self.bot.set_webhook("https://example.vercel.app/api/telegram",
                                             secret_token="s3cret"))
        call = self.session.calls[0]
        self.assertTrue(call["url"].endswith("/setWebhook"))
        self.assertEqual(call["payload"]["url"], "https://example.vercel.app/api/telegram")
        self.assertEqual(call["payload"]["secret_token"], "s3cret")
        self.assertEqual(call["payload"]["allowed_updates"], ["message"])
        self.assertFalse(call["payload"]["drop_pending_updates"])

    def test_set_webhook_drop_pending_and_connections(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": True})]
        self.bot.set_webhook("https://example.test/hook", drop_pending_updates=True,
                             max_connections=10)
        payload = self.session.calls[0]["payload"]
        self.assertTrue(payload["drop_pending_updates"])
        self.assertEqual(payload["max_connections"], 10)
        self.assertNotIn("secret_token", payload)      # без секрета поле не отправляем

    def test_delete_webhook(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": True})]
        self.assertTrue(self.bot.delete_webhook())
        call = self.session.calls[0]
        self.assertTrue(call["url"].endswith("/deleteWebhook"))
        self.assertFalse(call["payload"]["drop_pending_updates"])

    def test_get_webhook_info(self) -> None:
        self.session.responses = [FakeResponse({
            "ok": True, "result": {"url": "https://x/api/telegram", "pending_update_count": 2},
        })]
        info = self.bot.get_webhook_info()
        self.assertEqual((info["url"], info["pending_update_count"]), ("https://x/api/telegram", 2))

    def test_webhook_info_without_webhook(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": {"url": ""}})]
        self.assertEqual(self.bot.get_webhook_info()["url"], "")


class SupabaseStorageTests(unittest.TestCase):
    """Сетевой слой Supabase: payload и параметры запросов PostgREST."""

    def setUp(self) -> None:
        self.session = FakeSession()
        self.storage = SupabaseStorage(
            "https://example.supabase.co/", "service-key", session=self.session,
        )

    def test_add_debt_payload(self) -> None:
        self.session.responses = [FakeResponse([{
            "id": 5, "chat_id": 7, "from_name": "Леша", "to_name": "Дима",
            "currency": "BYN", "amount": 3.0, "created_at": "2026-01-01T00:00:00+00:00",
        }])]
        debt = self.storage.add_debt(7, "Леша", "Дима", "byn", 3, "исходный текст")
        self.assertEqual((debt.id, debt.currency, debt.amount), (5, "BYN", 3.0))
        call = self.session.calls[0]
        self.assertTrue(call["url"].endswith("/rest/v1/debts"))
        self.assertEqual(call["payload"]["currency"], "BYN")
        self.assertEqual(call["payload"]["amount"], 3.0)
        self.assertIn("return=representation", call["headers"]["Prefer"])

    def test_list_and_delete_params(self) -> None:
        self.session.responses = [FakeResponse([]), FakeResponse([{"id": 1}, {"id": 2}])]
        self.assertEqual(self.storage.list_debts(7), [])
        self.assertEqual(self.session.calls[0]["params"]["chat_id"], "eq.7")
        self.assertEqual(self.session.calls[0]["params"]["order"], "created_at.asc")
        self.assertEqual(self.storage.delete_debts(7), 2)

    def test_state_roundtrip(self) -> None:
        self.session.responses = [FakeResponse([]), FakeResponse([]), FakeResponse([{"value": "42"}])]
        self.assertIsNone(self.storage.get_state("last_update_id"))
        self.storage.set_state("last_update_id", 42)
        upsert = self.session.calls[1]
        self.assertTrue(upsert["url"].endswith("/rest/v1/bot_state"))
        self.assertEqual(upsert["params"]["on_conflict"], "key")
        self.assertEqual(upsert["payload"], {"key": "last_update_id", "value": "42"})
        self.assertEqual(self.storage.get_state("last_update_id"), "42")

    def test_currency_settings(self) -> None:
        self.session.responses = [FakeResponse([{"default_currency": "usd"}])]
        self.assertEqual(self.storage.get_default_currency(7, "BYN"), "USD")
        self.assertEqual(self.session.calls[0]["params"]["select"], "default_currency")

    def test_error_messages(self) -> None:
        self.session.responses = [FakeResponse({"message": "no"}, status=401)]
        with self.assertRaises(StorageError) as ctx:
            self.storage.list_debts(1)
        self.assertIn("service_role", str(ctx.exception))

        self.session.responses = [FakeResponse({"message": "missing"}, status=404)]
        with self.assertRaises(StorageError) as ctx:
            self.storage.list_debts(1)
        self.assertIn("schema.sql", str(ctx.exception))

    def test_secret_key_goes_only_in_apikey_header(self) -> None:
        # Ключи нового формата — не JWT: в Authorization их слать нельзя (и не нужно).
        storage = SupabaseStorage("https://example.supabase.co", "sb_secret_abc",
                                  session=self.session)
        self.session.responses = [FakeResponse([])]
        storage.list_debts(7)
        headers = self.session.calls[0]["headers"]
        self.assertEqual(headers["apikey"], "sb_secret_abc")
        self.assertNotIn("Authorization", headers)

    def test_legacy_jwt_goes_in_both_headers(self) -> None:
        # Legacy-ключ отправляем как раньше: роль service_role задаёт именно Authorization.
        storage = SupabaseStorage("https://example.supabase.co", "eyJhbGciOi.legacy.sig",
                                  session=self.session)
        self.session.responses = [FakeResponse([])]
        storage.list_debts(7)
        headers = self.session.calls[0]["headers"]
        self.assertEqual(headers["apikey"], "eyJhbGciOi.legacy.sig")
        self.assertEqual(headers["Authorization"], "Bearer eyJhbGciOi.legacy.sig")

    def test_is_new_api_key_detection(self) -> None:
        self.assertTrue(is_new_api_key("sb_secret_abc"))
        self.assertTrue(is_new_api_key("  sb_publishable_abc  "))
        self.assertFalse(is_new_api_key("eyJhbGciOi.jwt.sig"))
        self.assertFalse(is_new_api_key(""))


class SupabaseKeyValidationTests(unittest.TestCase):
    """Проверка ключа базы: публичные ключи (publishable/anon) выявляются до запросов."""

    @staticmethod
    def make_jwt(role: str) -> str:
        """Собирает JWT с нужной ролью (подпись не проверяется)."""

        def encode(data: dict) -> str:
            raw = json.dumps(data).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return f"{encode({'alg': 'HS256', 'typ': 'JWT'})}.{encode({'role': role})}.signature"

    def test_jwt_role_detection(self) -> None:
        self.assertEqual(jwt_role(self.make_jwt("anon")), "anon")
        self.assertEqual(jwt_role(self.make_jwt("service_role")), "service_role")
        self.assertIsNone(jwt_role("не-jwt"))
        self.assertIsNone(jwt_role(""))

    def test_anon_key_is_reported(self) -> None:
        problem = supabase_key_problem(self.make_jwt("anon")) or ""
        self.assertIn("anon", problem)
        self.assertIn("service_role", problem)

    def test_good_keys_pass(self) -> None:
        self.assertIsNone(supabase_key_problem(self.make_jwt("service_role")))
        self.assertIsNone(supabase_key_problem("sb_secret_abc123"))
        self.assertIsNone(supabase_key_problem(""))

    def test_publishable_and_garbage_are_reported(self) -> None:
        self.assertIn("publishable", supabase_key_problem("sb_publishable_abc") or "")
        self.assertIn("не похож", supabase_key_problem("просто-строка") or "")

    def test_settings_problems_mention_wrong_role(self) -> None:
        settings = load_settings(
            {
                "TELEGRAM_BOT_TOKEN": "1:abc",
                "DEEPSEEK_API_KEY": "sk-x",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_KEY": self.make_jwt("anon"),
            },
            use_env_file=False,
        )
        self.assertIn("anon", " ".join(settings.problems()))

    def test_settings_accept_service_role(self) -> None:
        settings = load_settings(
            {
                "TELEGRAM_BOT_TOKEN": "1:abc",
                "DEEPSEEK_API_KEY": "sk-x",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_KEY": self.make_jwt("service_role"),
            },
            use_env_file=False,
        )
        self.assertEqual(settings.problems(), [])

    def test_settings_accept_secret_key(self) -> None:
        settings = load_settings(
            {
                "TELEGRAM_BOT_TOKEN": "1:abc",
                "DEEPSEEK_API_KEY": "sk-x",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SECRET_KEY": "sb_secret_abc123",
            },
            use_env_file=False,
        )
        self.assertEqual(settings.supabase_key, "sb_secret_abc123")
        self.assertEqual(settings.problems(), [])

    def test_secret_key_wins_over_legacy_names(self) -> None:
        # Старый anon-ключ остался в окружении — новый secret-ключ всё равно важнее.
        settings = load_settings(
            {
                "TELEGRAM_BOT_TOKEN": "1:abc",
                "DEEPSEEK_API_KEY": "sk-x",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SECRET_KEY": "sb_secret_abc123",
                "SUPABASE_SERVICE_KEY": self.make_jwt("anon"),
                "SUPABASE_KEY": self.make_jwt("anon"),
            },
            use_env_file=False,
        )
        self.assertEqual(settings.supabase_key, "sb_secret_abc123")
        self.assertEqual(settings.problems(), [])

    def test_empty_legacy_value_does_not_shadow_new_key(self) -> None:
        # В .env осталась пустая строка прежней переменной — она не должна перебивать новую.
        settings = load_settings(
            {"SUPABASE_SERVICE_KEY": "  ", "SUPABASE_SECRET_KEY": "'sb_secret_abc123'"},
            use_env_file=False,
        )
        self.assertEqual(settings.supabase_key, "sb_secret_abc123")

    def test_missing_key_message_points_to_secret_env(self) -> None:
        problems = " ".join(load_settings({}, use_env_file=False).problems())
        self.assertIn("SUPABASE_SECRET_KEY", problems)
        self.assertIn("Secret keys", problems)

    def test_spaces_and_quotes_around_key_are_tolerated(self) -> None:
        self.assertIsNone(supabase_key_problem("  'sb_secret_abc123'  "))


class LongPollingTests(unittest.TestCase):
    """Постоянный режим (хостинг): старт с сохранённого смещения и его запись при выходе."""

    def build(self, updates: list[dict]):
        """Бот с хранилищем в памяти и фейковым Telegram."""
        settings = Settings(default_currency="BYN")
        storage = InMemoryStorage(default_currency="BYN")
        seed_chat(storage, chat=7)                  # участники чата из make_update
        telegram = FakeTelegram(updates)
        return DebtBot(settings, storage, HeuristicParser(), telegram), storage, telegram

    def test_run_uses_saved_offset_and_persists_it(self) -> None:
        bot, storage, telegram = self.build([make_update(20, "Леша должен Диме 3 рубля")])
        storage.set_state("last_update_id", "15")
        bot.run(poll_timeout=5, max_updates=1)
        self.assertEqual(telegram.offsets[0], 15)               # старт с сохранённого смещения
        self.assertEqual(storage.get_state("last_update_id"), "21")  # записал после обработки
        self.assertEqual(len(storage.list_debts(7)), 1)
        self.assertIn("Записал долг", telegram.sent[0][1])

    def test_run_without_saved_offset_requests_everything(self) -> None:
        bot, storage, telegram = self.build([make_update(1, "/help")])
        bot.run(poll_timeout=0, max_updates=1)
        self.assertIsNone(telegram.offsets[0])                  # обработать всё, что накопилось

    def test_run_with_limit_stops_on_empty_batch(self) -> None:
        bot, storage, telegram = self.build([])
        self.assertEqual(bot.run(poll_timeout=0, max_updates=5), 0)   # без бесконечного цикла
        self.assertEqual(len(telegram.offsets), 1)

    def test_run_survives_broken_state_storage(self) -> None:
        class BrokenState(InMemoryStorage):
            """Хранилище, у которого не работает bot_state."""

            def get_state(self, key: str, default: str | None = None) -> str | None:
                raise StorageError("нет таблицы bot_state")

            def set_state(self, key: str, value: str) -> None:
                raise StorageError("нет таблицы bot_state")

        settings = Settings(default_currency="BYN")
        telegram = FakeTelegram([make_update(3, "Леша должен Диме 3 рубля")])
        storage = BrokenState()
        seed_chat(storage, chat=7)
        bot = DebtBot(settings, storage, HeuristicParser(), telegram)
        self.assertEqual(bot.run(poll_timeout=5, max_updates=1), 1)   # не падает из-за состояния
        self.assertIn("Записал долг", telegram.sent[0][1])


GROUP = -100500


def make_chat_update(update_id: int, text: str, *, chat: int = GROUP,
                     chat_type: str = "supergroup", user: int = 100,
                     username: str = "vasya",
                     reply_from_username: str | None = None) -> dict:
    """Апдейт с типом чата и (опционально) ответом — для проверки правил обращения к боту."""
    message: dict[str, Any] = {
        "message_id": update_id,
        "chat": {"id": chat, "type": chat_type},
        "from": {"id": user, "username": username},
        "text": text,
    }
    if reply_from_username is not None:
        message["reply_to_message"] = {
            "message_id": update_id - 1,
            "chat": {"id": chat, "type": chat_type},
            "from": {"id": 1, "username": reply_from_username, "is_bot": True},
            "text": "предыдущий ответ бота",
        }
    return {"update_id": update_id, "message": message}


class MentionOnlyTests(unittest.TestCase):
    """В группах бот работает только по обращению, в личке — как раньше."""

    BOT = "test_bot"          # столько возвращает FakeTelegram.get_me()

    def build(self, **settings_kwargs):
        """Бот с хранилищем в памяти и подменённым Telegram."""
        settings = Settings(default_currency="BYN", **settings_kwargs)
        storage = InMemoryStorage(default_currency="BYN")
        seed_chat(storage, chat=GROUP)              # участники группового чата
        seed_chat(storage, chat=7)                  # и личного чата
        telegram = FakeTelegram([])
        bot = DebtBot(settings, storage, HeuristicParser(), telegram)
        return bot, storage, telegram

    def test_command_without_mention_is_answered(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "/help"))
        self.assertIn("Калькулятор долгов", telegram.sent[0][1])

    def test_debts_command_without_mention_is_answered(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "/debts"))
        self.assertIn("Долгов нет", telegram.sent[0][1])

    def test_command_for_another_bot_is_ignored(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "/help@other_bot"))
        self.assertEqual(telegram.sent, [])

    def test_path_is_not_a_command(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "/usr/bin/ls Леша должен Диме 3 рубля"))
        self.assertEqual(telegram.sent, [])

    def test_command_inside_text_is_not_an_address(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "а покажи /debts"))
        self.assertEqual(telegram.sent, [])

    def test_group_message_without_mention_is_ignored(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "Леша должен Диме 3 рубля"))
        self.assertEqual(telegram.sent, [])                 # молчим, в чат не лезем
        self.assertEqual(storage.list_debts(GROUP), [])

    def test_group_message_with_mention_is_saved(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, f"@{self.BOT} Леша должен Диме 3 рубля"))
        self.assertIn("Записал долг", telegram.sent[0][1])
        saved = storage.list_debts(GROUP)[0]
        self.assertEqual(saved.raw_text, "Леша должен Диме 3 рубля")   # упоминание вырезано

    def test_mention_at_the_end_is_understood(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, f"Леша должен Диме 3 рубля @{self.BOT}"))
        self.assertIn("Записал долг", telegram.sent[0][1])

    def test_command_with_bot_username(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, f"/debts@{self.BOT}"))
        self.assertIn("Долгов нет", telegram.sent[0][1])

    def test_help_command_with_bot_username(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, f"/help@{self.BOT}"))
        self.assertIn("Калькулятор долгов", telegram.sent[0][1])

    def test_bare_mention_shows_help(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, f"@{self.BOT}"))
        self.assertIn("Калькулятор долгов", telegram.sent[0][1])

    def test_reply_to_bot_is_processed(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(
            10, "а Леша должен Диме 3 рубля?", reply_from_username=self.BOT,
        ))
        self.assertIn("Записал долг", telegram.sent[0][1])

    def test_mention_of_another_bot_is_ignored(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "@other_bot Леша должен Диме 3 рубля"))
        self.assertEqual(telegram.sent, [])

    def test_similar_username_is_not_a_mention(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "@test_bot_super Леша должен Диме 3 рубля"))
        self.assertEqual(telegram.sent, [])

    def test_private_chat_works_without_mention(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, "Леша должен Диме 3 рубля",
                                            chat=7, chat_type="private"))
        self.assertIn("Записал долг", telegram.sent[0][1])
        self.assertEqual(len(storage.list_debts(7)), 1)

    def test_mention_is_stripped_in_private_chat(self) -> None:
        bot, storage, telegram = self.build()
        bot.process_update(make_chat_update(10, f"@{self.BOT} /debts",
                                            chat=7, chat_type="private"))
        self.assertIn("Долгов нет", telegram.sent[0][1])

    def test_require_mention_can_be_disabled(self) -> None:
        bot, storage, telegram = self.build(require_mention=False)
        bot.process_update(make_chat_update(10, "Леша должен Диме 3 рубля"))
        self.assertIn("Записал долг", telegram.sent[0][1])

    def test_bot_username_from_settings(self) -> None:
        bot, _, _ = self.build(bot_username="@my_debt_bot")
        self.assertEqual(bot.bot_username, "my_debt_bot")


class MentionHelperTests(unittest.TestCase):
    """Вспомогательные функции обращения к боту."""

    def test_clean_mention(self) -> None:
        self.assertEqual(clean_bot_mention(" /debts@test_bot ", "test_bot"), "/debts")
        self.assertEqual(clean_bot_mention("@test_bot  Леша  должен  Диме 3", "test_bot"),
                         "Леша должен Диме 3")
        self.assertEqual(clean_bot_mention("привет @test_bot, как дела", "test_bot"),
                         "привет , как дела")
        self.assertEqual(clean_bot_mention("@Дима должен Леше 5", "test_bot"),
                         "@Дима должен Леше 5")          # чужие упоминания не трогаем

    def test_mentions(self) -> None:
        self.assertTrue(mentions_bot({"text": "@test_bot привет"}, "test_bot"))
        self.assertTrue(mentions_bot({"text": "@Test_Bot привет"}, "test_bot"))   # регистр не важен
        self.assertFalse(mentions_bot({"text": "привет"}, "test_bot"))
        self.assertFalse(mentions_bot({"text": "@test_bot_super привет"}, "test_bot"))
        self.assertTrue(mentions_bot({"text": "", "caption": "@test_bot фото"}, "test_bot"))
        self.assertTrue(mentions_bot(
            {"text": "привет", "reply_to_message": {"from": {"username": "test_bot"}}}, "test_bot",
        ))
        self.assertFalse(mentions_bot({"text": "@test_bot"}, ""))

    def test_settings_flags(self) -> None:
        self.assertTrue(load_settings({}, use_env_file=False).require_mention)
        self.assertFalse(load_settings({"REQUIRE_MENTION": "0"}, use_env_file=False).require_mention)
        self.assertFalse(load_settings({"REQUIRE_MENTION": "no"}, use_env_file=False).require_mention)
        self.assertTrue(load_settings({"REQUIRE_MENTION": "да"}, use_env_file=False).require_mention)
        self.assertTrue(load_settings({"REQUIRE_MENTION": "мусор"}, use_env_file=False).require_mention)
        self.assertEqual(load_settings({"BOT_USERNAME": "@my_bot"}, use_env_file=False).bot_username,
                         "my_bot")


class RepaymentTests(unittest.TestCase):
    """Возврат долга: разбор текста, влияние на сальдо и отчёт."""

    def test_heuristic_repayment(self) -> None:
        parsed = heuristic_parse("Леша вернул Диме 3 рубля")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "repayment")
        self.assertEqual((parsed.from_name, parsed.to_name), ("Леша", "Диме"))
        self.assertEqual(parsed.amount, 3.0)
        self.assertEqual(parsed.currency, "BYN")
        self.assertTrue(parsed.is_repayment)

    def test_repayment_with_dollar(self) -> None:
        parsed = heuristic_parse("Маша отдала Пете 10$")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "repayment")
        self.assertEqual(parsed.amount, 10.0)
        self.assertEqual(parsed.currency, "USD")

    def test_word_dolg_does_not_turn_repayment_into_report(self) -> None:
        parsed = heuristic_parse("Леша вернул долг Диме 3 рубля")
        self.assertEqual(parsed.intent, "repayment")      # а не «покажи долги»

    def test_debt_parsing_is_unchanged(self) -> None:
        parsed = heuristic_parse("Леша должен Диме 3 рубля")
        self.assertEqual(parsed.intent, "debt")

    def test_system_prompt_knows_repayment(self) -> None:
        self.assertIn("repayment", SYSTEM_PROMPT)

    def test_netting_subtracts_repayment(self) -> None:
        balances = net_balances([
            make_debt("Леша", "Дима", 5),
            make_debt("Леша", "Дима", 3, kind="repayment"),
        ])
        self.assertEqual(len(balances), 1)
        self.assertEqual((balances[0].debtor, balances[0].amount), ("Леша", 2.0))

    def test_full_repayment_closes_the_debt(self) -> None:
        self.assertEqual(net_balances([
            make_debt("Леша", "Дима", 3),
            make_debt("Леша", "Дима", 3, kind="repayment"),
        ]), [])

    def test_overpayment_flips_direction(self) -> None:
        balances = net_balances([
            make_debt("Леша", "Дима", 3),
            make_debt("Леша", "Дима", 5, kind="repayment"),
        ])
        self.assertEqual((balances[0].debtor, balances[0].creditor, balances[0].amount),
                         ("Дима", "Леша", 2.0))

    def test_totals_are_reduced(self) -> None:
        owes, owed = totals_by_person([
            make_debt("Леша", "Дима", 5),
            make_debt("Леша", "Дима", 2, kind="repayment"),
        ])
        self.assertEqual(owes["Леша"]["BYN"], 3.0)
        self.assertEqual(owed["Дима"]["BYN"], 3.0)

    def test_pretty_marks_repayment(self) -> None:
        self.assertIn("вернул", make_debt("Леша", "Дима", 3, kind="repayment").pretty())

    def test_report_shows_repayments_separately(self) -> None:
        report = format_debts_report([
            make_debt("Леша", "Дима", 5),
            make_debt("Леша", "Дима", 3, kind="repayment"),
        ])
        self.assertIn("из них возвратов: 1", report)
        self.assertIn("Итог с взаимозачётом", report)
        self.assertIn("Леша → Дима: 2.00 BYN", report)
        self.assertIn("Возвраты (учтены в зачёте)", report)
        self.assertIn("Возвратов записано: 3.00 BYN", report)


class RepaymentFlowTests(unittest.TestCase):
    """Сценарии бота: «вернул» уменьшает долг, /undo убирает последнюю запись."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage)

    def send(self, text: str, chat: int = CHAT) -> str:
        """Отправляет сообщение боту (автор — Леша Козлов) и возвращает ответ."""
        members = self.members if chat == CHAT else seed_chat(self.storage, chat)
        return handle_text(text, chat, storage=self.storage, parser=self.parser,
                           settings=self.settings, members=members,
                           author=replace(MEMBER_LEHA, chat_id=chat))

    def test_repayment_is_saved_and_reduces_report(self) -> None:
        self.send("Леша должен Диме 5 рублей")
        reply = self.send("Леша вернул Диме 3 рубля")
        self.assertIn("Записал возврат долга", reply)
        saved = self.storage.list_debts(CHAT)[-1]
        self.assertEqual((saved.kind, saved.amount, saved.raw_text),
                         ("repayment", 3.0, "Леша вернул Диме 3 рубля"))
        self.assertIn("Леша Козлов (@kozlovAlex) → Дмитрий Болт (@bdzmity): 2.00 BYN",
                      self.send("/debts"))

    def test_repayment_without_amount_asks_for_details(self) -> None:
        reply = handle_text(
            "Леша вернул Диме", CHAT, storage=self.storage,
            parser=FakeParser(ParsedMessage(intent="repayment", from_name="Леша", to_name="Дима")),
            settings=self.settings,
        )
        self.assertIn("не хватает данных", reply)
        self.assertEqual(self.storage.list_debts(CHAT), [])

    def test_repayment_uses_default_currency_silently(self) -> None:
        reply = self.send("Леша вернул Диме 3")
        self.assertIn("3.00 BYN", reply)
        self.assertNotIn("взял по умолчанию", reply)

    def test_undo_removes_last_record(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        self.send("Леша вернул Диме 1 рубль")
        reply = self.send("/undo")
        self.assertIn("Удалил последнюю запись", reply)
        self.assertIn("вернул", reply)
        self.assertEqual(len(self.storage.list_debts(CHAT)), 1)
        self.assertNotIn("Возвраты", self.send("/debts"))

    def test_undo_without_records(self) -> None:
        self.assertIn("удалять нечего", self.send("/undo"))

    def test_undo_twice_removes_two_records(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        self.send("Маша должна Оле 5 рублей")
        self.send("/undo")
        self.send("/undo")
        self.assertEqual(self.storage.list_debts(CHAT), [])
        self.assertIn("удалять нечего", self.send("/undo"))

    def test_undo_does_not_touch_other_chats(self) -> None:
        self.send("Леша должен Диме 3 рубля", chat=1)
        self.send("Маша должна Оле 5 рублей", chat=2)
        self.send("/undo", chat=2)
        self.assertEqual(len(self.storage.list_debts(1)), 1)
        self.assertEqual(self.storage.list_debts(2), [])

    def test_help_mentions_new_commands(self) -> None:
        help_text = self.send("/help")
        self.assertIn("возврат", help_text)
        self.assertIn("/undo", help_text)


class SavedReplyHintTests(unittest.TestCase):
    """Ответ о записи — без итогов: только короткая подсказка «Итог: /settle» в конце."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage)

    def send(self, text: str) -> str:
        """Отправляет сообщение боту (автор — Леша Козлов) и возвращает ответ."""
        return handle_text(text, CHAT, storage=self.storage, parser=self.parser,
                           settings=self.settings, members=self.members, author=MEMBER_LEHA)

    def assert_only_hint(self, reply: str) -> None:
        """Никаких итогов и подсказок «подробно» — ровно одна строка в самом конце."""
        self.assertNotIn("📊 Итог", reply)
        self.assertNotIn("🧮", reply)
        self.assertNotIn("Подробно", reply)
        self.assertEqual(reply.count(SETTLE_HINT), 1)
        self.assertEqual(reply.splitlines()[-1], SETTLE_HINT)

    def test_debt_reply_has_only_hint(self) -> None:
        reply = self.send("Леша должен Диме 3 рубля")
        self.assertIn("Записал долг", reply)
        self.assertIn("3.00 BYN", reply)
        self.assert_only_hint(reply)

    def test_repayment_reply_has_only_hint(self) -> None:
        self.send("Леша должен Диме 5 рублей")
        reply = self.send("Леша вернул Диме 3 рубля")
        self.assertIn("Записал возврат долга", reply)
        self.assert_only_hint(reply)

    def test_expense_reply_has_only_hint(self) -> None:
        reply = self.send("Дима заплатил 10 за всех")
        self.assertIn("Записал общий счёт", reply)
        self.assert_only_hint(reply)


class StorageRepaymentTests(unittest.TestCase):
    """Слой Supabase и память: поле kind и удаление последней записи."""

    def setUp(self) -> None:
        self.session = FakeSession()
        self.storage = SupabaseStorage(
            "https://example.supabase.co/", "service-key", session=self.session,
        )

    def test_add_repayment_payload(self) -> None:
        self.session.responses = [FakeResponse([{
            "id": 6, "chat_id": 7, "from_name": "Леша", "to_name": "Дима",
            "currency": "BYN", "amount": 3.0, "kind": "repayment",
        }])]
        debt = self.storage.add_debt(7, "Леша", "Дима", "byn", 3, kind="repayment")
        self.assertEqual(debt.kind, "repayment")
        self.assertTrue(debt.is_repayment)
        self.assertEqual(self.session.calls[0]["payload"]["kind"], "repayment")

    def test_default_kind_is_debt(self) -> None:
        self.session.responses = [FakeResponse([{"id": 1, "chat_id": 7, "amount": 3.0}])]
        self.storage.add_debt(7, "Леша", "Дима", "BYN", 3)
        self.assertEqual(self.session.calls[0]["payload"]["kind"], "debt")

    def test_delete_last_debt_reads_then_deletes(self) -> None:
        self.session.responses = [
            FakeResponse([{
                "id": 5, "chat_id": 7, "from_name": "Леша", "to_name": "Дима",
                "currency": "BYN", "amount": 3.0, "kind": "debt",
            }]),
            FakeResponse([{"id": 5}]),
        ]
        removed = self.storage.delete_last_debt(7)
        self.assertEqual((removed.id, removed.amount), (5, 3.0))
        read_call, delete_call = self.session.calls
        self.assertEqual(read_call["method"], "GET")
        self.assertEqual(read_call["params"]["order"], "created_at.desc,id.desc")
        self.assertEqual(read_call["params"]["limit"], 1)
        self.assertEqual(delete_call["method"], "DELETE")
        self.assertEqual(delete_call["params"]["id"], "eq.5")

    def test_delete_last_debt_without_records_does_not_delete(self) -> None:
        self.session.responses = [FakeResponse([])]
        self.assertIsNone(self.storage.delete_last_debt(7))
        self.assertEqual(len(self.session.calls), 1)

    def test_memory_storage_delete_last(self) -> None:
        memory = InMemoryStorage()
        memory.add_debt(1, "Леша", "Дима", "BYN", 3)
        memory.add_debt(1, "Леша", "Дима", "BYN", 1, kind="repayment")
        removed = memory.delete_last_debt(1)
        self.assertTrue(removed.is_repayment)
        self.assertEqual(len(memory.debts), 1)
        self.assertIsNone(memory.delete_last_debt(99))


MEMBER_LEHA = ChatMember(chat_id=CHAT, user_id=101, username="kozlovAlex",
                         display_name="Леша Козлов", aliases=["Леша", "Лёха"],
                         is_registered=True)
MEMBER_DIMA = ChatMember(chat_id=CHAT, user_id=102, username="bdzmity",
                         display_name="Дмитрий Болт", aliases=["Дима", "Димон"],
                         is_registered=True)
MEMBER_MASHA = ChatMember(chat_id=CHAT, user_id=103, username="petrova_m",
                          display_name="Маша Петрова", aliases=["Маша"],
                          is_registered=True)
MEMBER_OLYA = ChatMember(chat_id=CHAT, user_id=104, username="olga_s",
                         display_name="Оля Смирнова", aliases=["Оля"],
                         is_registered=True)
MEMBER_PETYA = ChatMember(chat_id=CHAT, user_id=105, username="petya_k",
                          display_name="Петя Кузнецов", aliases=["Петя"],
                          is_registered=True)
# Гоша — без отметки /reg: на нём проверяем, что записи на незарегистрированных не ведутся.
MEMBER_GOSHA = ChatMember(chat_id=CHAT, user_id=106, username="gosha_p",
                          display_name="Гоша Петров")
REGISTERED = (MEMBER_LEHA, MEMBER_DIMA, MEMBER_MASHA, MEMBER_OLYA, MEMBER_PETYA)


def seed_chat(storage: InMemoryStorage, chat: int = CHAT,
              members: Sequence[ChatMember] = REGISTERED,
              register: bool = True) -> list[ChatMember]:
    """Готовит чат: добавляет участников и (при register) отмечает их через /reg."""
    for member in members:
        prepared = replace(member, chat_id=chat)
        if register:
            storage.register_member(prepared)
        else:
            storage.remember_member(prepared)
    return storage.list_members(chat)


class MemberHelperTests(unittest.TestCase):
    """Сопоставление имён из сообщения с участниками чата."""

    def test_member_from_telegram(self) -> None:
        member = member_from_telegram(7, {"id": 5, "first_name": "Леша", "last_name": "Козлов",
                                          "username": "kozlovAlex"})
        self.assertIsNotNone(member)
        self.assertEqual((member.user_id, member.username, member.display_name),
                         (5, "kozlovAlex", "Леша Козлов"))
        self.assertIsNone(member_from_telegram(7, {"id": 5, "is_bot": True}))
        self.assertIsNone(member_from_telegram(7, None))

    def test_resolve_by_username(self) -> None:
        self.assertEqual(resolve_member("@kozlovAlex", [MEMBER_LEHA, MEMBER_DIMA]).user_id, 101)

    def test_resolve_by_full_and_partial_name(self) -> None:
        members = [MEMBER_LEHA, MEMBER_DIMA]
        self.assertEqual(resolve_member("Леша Козлов", members).user_id, 101)
        self.assertEqual(resolve_member("Козлов", members).user_id, 101)

    def test_resolve_by_alias_and_case(self) -> None:
        members = [MEMBER_LEHA, MEMBER_DIMA]
        self.assertEqual(resolve_member("дима", members).user_id, 102)
        self.assertEqual(resolve_member("Димон", members).user_id, 102)

    def test_resolve_padezh_forms(self) -> None:
        members = [MEMBER_DIMA]
        self.assertEqual(resolve_member("Диме", members).user_id, 102)      # «Диме» → «Дима»
        self.assertEqual(resolve_member("Дмитрию", members).user_id, 102)   # «Дмитрию» → «Дмитрий»

    def test_resolve_short_names_in_padezh(self) -> None:
        members = [MEMBER_OLYA]
        self.assertEqual(resolve_member("Оле", members).user_id, 104)
        self.assertEqual(resolve_member("Олю", members).user_id, 104)

    def test_resolve_similar_spelling(self) -> None:
        members = [ChatMember(chat_id=CHAT, user_id=101, display_name="Леша Козлов")]
        self.assertEqual(resolve_member("Лешак", members).user_id, 101)      # общий корень

    def test_first_person_is_author(self) -> None:
        self.assertEqual(resolve_member("я", [MEMBER_LEHA], MEMBER_LEHA).user_id, 101)
        self.assertIsNone(resolve_member("я", [MEMBER_LEHA]))

    def test_unknown_and_ambiguous(self) -> None:
        self.assertIsNone(resolve_member("Гоша", [MEMBER_LEHA, MEMBER_DIMA]))
        two_dmitrys = [MEMBER_DIMA, ChatMember(chat_id=CHAT, user_id=103, display_name="Дмитрий Орлов")]
        self.assertIsNone(resolve_member("Дмитрий", two_dmitrys))            # непонятно, кто именно

    def test_roster_for_ai(self) -> None:
        roster = format_roster([MEMBER_LEHA, MEMBER_DIMA], MEMBER_LEHA)
        self.assertIn("id=101", roster)
        self.assertIn("@kozlovAlex", roster)
        self.assertIn("алиасы: Дима, Димон", roster)
        self.assertIn("Автор сообщения: Леша Козлов (@kozlovAlex) (id=101)", roster)
        self.assertIn("[зарегистрирован]", roster)

    def test_roster_marks_unregistered_members(self) -> None:
        roster = format_roster([MEMBER_LEHA, MEMBER_GOSHA])
        self.assertIn("[не зарегистрирован]", roster)
        self.assertIn("Гоша Петров", roster)

    def test_with_aliases_deduplicates_and_registers(self) -> None:
        updated = with_aliases(MEMBER_LEHA, ["лёха", "Лёха", "Женя"])
        self.assertEqual(updated.aliases, ["Леша", "Лёха", "Женя"])
        self.assertTrue(updated.is_registered)
        self.assertEqual(MEMBER_LEHA.aliases, ["Леша", "Лёха"])   # исходный не меняется


class MemberBindingFlowTests(unittest.TestCase):
    """Привязка записей к участникам: «Лешак», «Леша» и «@kozlovAlex» — один человек."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage, members=(MEMBER_LEHA, MEMBER_DIMA))
        self.storage.remember_member(replace(MEMBER_GOSHA, chat_id=CHAT))
        self.members = self.storage.list_members(CHAT)

    def send(self, text: str, author: ChatMember | None = MEMBER_LEHA) -> str:
        """Отправляет сообщение боту от имени участника чата."""
        return handle_text(text, CHAT, storage=self.storage, parser=self.parser,
                           settings=self.settings, members=self.members, author=author)

    def test_debt_is_bound_to_user_ids(self) -> None:
        reply = self.send("Лешак должен Диме 3 рубля")
        saved = self.storage.list_debts(CHAT)[0]
        self.assertEqual((saved.from_user_id, saved.to_user_id), (101, 102))
        self.assertEqual((saved.from_name, saved.to_name), ("Леша Козлов", "Дмитрий Болт"))
        self.assertIn("Леша Козлов (@kozlovAlex)", reply)
        self.assertIn("Дмитрий Болт (@bdzmity)", reply)

    def test_different_spellings_merge_by_id(self) -> None:
        self.send("Лешак должен Диме 3 рубля")
        self.send("Леша Козлов должен Дмитрию Болту 2 рубля")
        report = self.send("/debts")
        self.assertIn("Леша Козлов (@kozlovAlex) → Дмитрий Болт (@bdzmity): 5.00 BYN", report)

    def test_repayment_is_bound_too(self) -> None:
        self.send("Лешак должен Диме 5 рубля")
        self.send("Леша вернул Диме 2 рубля")
        saved = self.storage.list_debts(CHAT)[-1]
        self.assertEqual((saved.kind, saved.from_user_id, saved.to_user_id),
                         ("repayment", 101, 102))
        self.assertIn("Леша Козлов (@kozlovAlex) → Дмитрий Болт (@bdzmity): 3.00 BYN",
                      self.send("/debts"))

    def test_first_person_uses_author(self) -> None:
        self.send("я должен Диме 4 рубля")
        saved = self.storage.list_debts(CHAT)[0]
        self.assertEqual(saved.from_user_id, 101)

    def test_unregistered_person_is_refused(self) -> None:
        # Гоша есть в чате, но без /reg — записи на него не ведутся.
        reply = self.send("Гоша должен Диме 3 рубля")
        self.assertEqual(self.storage.list_debts(CHAT), [])
        self.assertIn("Ещё не зарегистрирован: Гоша Петров (@gosha_p)", reply)
        self.assertIn("/reg", reply)

    def test_unknown_name_is_refused(self) -> None:
        reply = self.send("Незнакомец должен Диме 3 рубля")
        self.assertEqual(self.storage.list_debts(CHAT), [])
        self.assertIn("Не знаю такого человека в чате: Незнакомец", reply)
        self.assertIn("/reg", reply)

    def test_empty_roster_needs_registration(self) -> None:
        reply = handle_text("Леша должен Диме 3 рубля", CHAT, storage=self.storage,
                            parser=self.parser, settings=self.settings, members=[])
        self.assertIn("только на зарегистрированных", reply)
        self.assertIn("/reg", reply)
        self.assertEqual(self.storage.list_debts(CHAT), [])


class IdentityNettingTests(unittest.TestCase):
    """Взаимозачёт по user id: разные написания — один человек."""

    def test_ids_merge_different_spellings(self) -> None:
        balances = net_balances([
            make_debt("Лешак", "Дима", 3, user_ids=(101, 102)),
            make_debt("Леша Козлов", "Дмитрий Болт", 2, user_ids=(101, 102)),
        ], members=[MEMBER_LEHA, MEMBER_DIMA])
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].amount, 5.0)
        self.assertEqual(balances[0].debtor, "Леша Козлов (@kozlovAlex)")
        self.assertEqual(balances[0].creditor, "Дмитрий Болт (@bdzmity)")

    def test_padezh_forms_still_merge_without_ids(self) -> None:
        balances = net_balances([make_debt("Леша", "Дима", 3), make_debt("Леше", "Диме", 2)])
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].amount, 5.0)

    def test_id_and_name_are_not_mixed(self) -> None:
        # Запись с id и запись по имени остаются разными: связать их без id нельзя.
        balances = net_balances([make_debt("Леша", "Дима", 3, user_ids=(101, None)),
                                 make_debt("Леша", "Дима", 2)])
        self.assertEqual(len(balances), 2)

    def test_totals_use_member_labels(self) -> None:
        owes, owed = totals_by_person([make_debt("Лешак", "Дима", 3, user_ids=(101, 102))],
                                      members=[MEMBER_LEHA, MEMBER_DIMA])
        self.assertEqual(owes["Леша Козлов (@kozlovAlex)"]["BYN"], 3.0)
        self.assertEqual(owed["Дмитрий Болт (@bdzmity)"]["BYN"], 3.0)


class MemberStorageTests(unittest.TestCase):
    """Слой Supabase: участники чата и привязка записей к user id."""

    def setUp(self) -> None:
        self.session = FakeSession()
        self.storage = SupabaseStorage(
            "https://example.supabase.co/", "service-key", session=self.session,
        )

    def test_remember_member_upsert(self) -> None:
        self.session.responses = [FakeResponse([])]
        self.storage.remember_member(ChatMember(
            chat_id=7, user_id=101, username="kozlovAlex",
            display_name="Леша Козлов", aliases=["Леша", "Лёха"],
        ))
        call = self.session.calls[0]
        self.assertTrue(call["url"].endswith("/rest/v1/chat_members"))
        self.assertEqual(call["params"]["on_conflict"], "chat_id,user_id")
        self.assertEqual(call["payload"]["user_id"], 101)
        self.assertEqual(call["payload"]["display_name"], "Леша Козлов")
        self.assertEqual(call["payload"]["aliases"], ["Леша", "Лёха"])
        self.assertIn("merge-duplicates", call["headers"]["Prefer"])

    def test_list_members_parsing(self) -> None:
        self.session.responses = [FakeResponse([{
            "chat_id": 7, "user_id": 101, "username": "kozlovAlex",
            "display_name": "Леша Козлов", "aliases": ["Леша"],
        }])]
        members = self.storage.list_members(7)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].label, "Леша Козлов (@kozlovAlex)")
        self.assertEqual(self.session.calls[0]["params"]["chat_id"], "eq.7")

    def test_add_debt_with_user_ids(self) -> None:
        self.session.responses = [FakeResponse([{
            "id": 1, "chat_id": 7, "amount": 3.0, "from_user_id": 101, "to_user_id": 102,
        }])]
        debt = self.storage.add_debt(7, "Леша Козлов", "Дмитрий Болт", "BYN", 3,
                                     from_user_id=101, to_user_id=102)
        payload = self.session.calls[0]["payload"]
        self.assertEqual((payload["from_user_id"], payload["to_user_id"]), (101, 102))
        self.assertEqual((debt.from_user_id, debt.to_user_id), (101, 102))

    def test_memory_storage_members(self) -> None:
        memory = InMemoryStorage()
        memory.remember_member(MEMBER_LEHA)
        memory.remember_member(MEMBER_DIMA)
        self.assertEqual(len(memory.list_members(CHAT)), 2)
        self.assertEqual(memory.list_members(999), [])


class AiRosterTests(unittest.TestCase):
    """ИИ получает состав чата и может вернуть user id участника."""

    def build(self) -> tuple[DeepSeekParser, FakeSession]:
        """Парсер DeepSeek с подменённым HTTP-слоем."""
        session = FakeSession()
        parser = DeepSeekParser("sk-test", base_url="https://api.deepseek.com", session=session)
        return parser, session

    def test_roster_and_author_go_to_the_model(self) -> None:
        parser, session = self.build()
        content = json.dumps({"intent": "debt", "from": "Леша", "to": "Дима", "amount": 3})
        session.responses = [FakeResponse({"choices": [{"message": {"content": content}}]})]
        parser.parse("Лешак должен Диме 3", "BYN",
                     members=[MEMBER_LEHA, MEMBER_DIMA], author=MEMBER_LEHA)
        system = session.calls[0]["payload"]["messages"][0]["content"]
        self.assertIn("id=101", system)
        self.assertIn("@kozlovAlex", system)
        self.assertIn("@bdzmity", system)
        self.assertIn("Автор сообщения", system)

    def test_ids_from_model_are_parsed(self) -> None:
        parser, session = self.build()
        content = json.dumps({
            "intent": "repayment", "from": "Лешак", "to": "Дима", "amount": 3,
            "from_user_id": "101", "to_user_id": 102,
        })
        session.responses = [FakeResponse({"choices": [{"message": {"content": content}}]})]
        parsed = parser.parse("Лешак вернул Диме 3", "BYN", members=[MEMBER_LEHA])
        self.assertEqual(parsed.intent, "repayment")
        self.assertEqual((parsed.from_user_id, parsed.to_user_id), (101, 102))
        self.assertTrue(parsed.is_repayment)

    def test_without_roster_there_is_no_member_list(self) -> None:
        parser, session = self.build()
        content = json.dumps({"intent": "debts"})
        session.responses = [FakeResponse({"choices": [{"message": {"content": content}}]})]
        parser.parse("покажи долги", "BYN")
        system = session.calls[0]["payload"]["messages"][0]["content"]
        self.assertNotIn("Участники чата", system)

    def test_model_ids_are_applied_to_the_record(self) -> None:
        storage = InMemoryStorage(default_currency="BYN")
        storage.remember_member(MEMBER_LEHA)
        storage.remember_member(MEMBER_DIMA)
        parsed = ParsedMessage(intent="debt", from_name="Лешак", to_name="Дима",
                               amount=3.0, currency="BYN", from_user_id=101, to_user_id=102)
        reply = handle_text("Лешак должен Диме 3 рубля", CHAT, storage=storage,
                            parser=FakeParser(parsed), settings=Settings(default_currency="BYN"),
                            members=storage.list_members(CHAT), author=MEMBER_DIMA)
        saved = storage.list_debts(CHAT)[0]
        self.assertEqual((saved.from_user_id, saved.to_user_id), (101, 102))
        self.assertEqual(saved.from_name, "Леша Козлов")
        self.assertIn("@kozlovAlex", reply)

    def test_invented_id_falls_back_to_name(self) -> None:
        storage = InMemoryStorage(default_currency="BYN")
        storage.remember_member(MEMBER_LEHA)
        storage.remember_member(MEMBER_DIMA)
        parsed = ParsedMessage(intent="debt", from_name="Леша", to_name="Дима",
                               amount=3.0, from_user_id=999, to_user_id=999)
        handle_text("Леша должен Диме 3", CHAT, storage=storage, parser=FakeParser(parsed),
                    settings=Settings(default_currency="BYN"),
                    members=storage.list_members(CHAT))
        saved = storage.list_debts(CHAT)[0]
        self.assertEqual((saved.from_user_id, saved.to_user_id), (101, 102))


class RegistrationTests(unittest.TestCase):
    """Команда /reg: имена участника и запрет записей на незарегистрированных."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage, members=(MEMBER_LEHA, MEMBER_DIMA))
        self.storage.remember_member(replace(MEMBER_GOSHA, chat_id=CHAT))

    def send(self, text: str, author: ChatMember | None = MEMBER_LEHA) -> str:
        """Отправляет сообщение от имени участника чата (состав читаем из хранилища)."""
        return handle_text(text, CHAT, storage=self.storage, parser=self.parser,
                           settings=self.settings,
                           members=self.storage.list_members(CHAT), author=author)

    def member(self, user_id: int) -> ChatMember:
        """Участник чата по user id."""
        return next(item for item in self.storage.list_members(CHAT) if item.user_id == user_id)

    def test_self_registration_adds_names(self) -> None:
        reply = self.send("/reg ЖеняШ, жекич")
        leha = self.member(101)
        self.assertTrue(leha.is_registered)
        self.assertEqual(leha.aliases, ["Леша", "Лёха", "ЖеняШ", "жекич"])
        self.assertIn("Добавлено сейчас: ЖеняШ, жекич", reply)

    def test_aliases_are_deduplicated(self) -> None:
        self.send("/reg женя, Женя, Жена")
        aliases = [alias.lower() for alias in self.member(101).aliases]
        self.assertEqual(aliases.count("женя"), 1)
        self.assertIn("жена", aliases)

    def test_register_another_member_by_username(self) -> None:
        reply = self.send("/reg @gosha_p Гоша, Гоша Петров, жекич")
        gosha = self.member(106)
        self.assertTrue(gosha.is_registered)
        self.assertEqual(gosha.aliases, ["Гоша", "Гоша Петров", "жекич"])
        self.assertIn("Гоша Петров (@gosha_p)", reply)
        # теперь по новому имени человек узнаётся, и запись сохраняется
        self.assertIn("Записал долг", self.send("жекич должен Диме 2 рубля"))
        saved = self.storage.list_debts(CHAT)[-1]
        self.assertEqual((saved.from_user_id, saved.to_user_id), (106, 102))

    def test_unknown_username_is_reported(self) -> None:
        reply = self.send("/reg @enot Женя")
        self.assertIn("Не нашёл @enot", reply)
        self.assertIn("@gosha_p", reply)          # подсказка: кого бот уже знает
        self.assertTrue(self.member(101).is_registered)

    def test_who_lists_registered_and_not(self) -> None:
        reply = self.send("/who")
        self.assertIn("зарегистрированы: 2 из 3", reply)
        self.assertIn("✅ Леша Козлов (@kozlovAlex)", reply)
        self.assertIn("⬜ Гоша Петров", reply)

    def test_only_registered_can_be_recorded(self) -> None:
        refused = self.send("Гоша должен Леше 5 рублей")
        self.assertEqual(self.storage.list_debts(CHAT), [])
        self.assertIn("/reg", refused)
        self.send("/reg @gosha_p Гоша")
        self.assertIn("Записал долг", self.send("Гоша должен Леше 5 рублей"))

    def test_registration_is_per_chat(self) -> None:
        other = seed_chat(self.storage, chat=999, members=(MEMBER_LEHA,))
        handle_text("/reg Женя", 999, storage=self.storage, parser=self.parser,
                    settings=self.settings, members=other,
                    author=replace(MEMBER_LEHA, chat_id=999))
        self.assertEqual(self.member(101).aliases, ["Леша", "Лёха"])


class ExpenseFlowTests(unittest.TestCase):
    """Общий счёт: деление на всех, исключения и /undo целого счёта."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage)

    def send(self, text: str, author: ChatMember = MEMBER_DIMA) -> str:
        """Отправляет сообщение от имени участника чата."""
        return handle_text(text, CHAT, storage=self.storage, parser=self.parser,
                           settings=self.settings,
                           members=self.storage.list_members(CHAT), author=author)

    def rows(self) -> list[Debt]:
        """Все записи чата (долги, возвраты и доли общих счетов)."""
        return self.storage.list_debts(CHAT)

    def test_paid_for_all_splits_equally(self) -> None:
        reply = self.send("Дима заплатил 10 за всех")
        rows = self.rows()
        self.assertEqual(len(rows), 4)                          # Леша, Маша, Оля, Петя
        self.assertTrue(all(row.kind == "expense" for row in rows))
        self.assertEqual({row.from_user_id for row in rows}, {101, 103, 104, 105})
        self.assertTrue(all(row.to_user_id == 102 for row in rows))
        self.assertTrue(all(row.amount == 2.0 for row in rows))     # 10 / 5
        self.assertEqual(len({row.group_id for row in rows}), 1)
        self.assertTrue(all(row.raw_text == "Дима заплатил 10 за всех" for row in rows))
        self.assertIn("Сумма: 10.00 BYN — делю на 5 чел.", reply)

    def test_paid_without_scope_is_for_everyone(self) -> None:
        self.send("я заплатил 20 рублей")            # автор сообщения — Дима
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row.to_user_id == 102 for row in rows))

    def test_excluded_person_is_not_charged(self) -> None:
        reply = self.send("Дима заплатил 12 за всех кроме Оли")
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertNotIn(104, {row.from_user_id for row in rows})
        self.assertTrue(all(row.amount == 3.0 for row in rows))     # 12 / 4
        self.assertIn("Исключены: Оля Смирнова (@olga_s)", reply)

    def test_except_self_leaves_payer_out_of_split(self) -> None:
        self.send("я заплатил 9 за всех кроме себя")
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row.amount == 2.25 for row in rows))    # 9 / 4
        self.assertTrue(all(row.from_user_id != 102 for row in rows))

    def test_self_only_expense_is_not_saved(self) -> None:
        reply = self.send("я заплатил 10 за себя")
        self.assertEqual(self.rows(), [])
        self.assertIn("Делить не с кого", reply)

    def test_named_participants_only(self) -> None:
        reply = self.send("Дима оплатил 6 за Машу и Олю")
        rows = self.rows()
        self.assertEqual({row.from_user_id for row in rows}, {103, 104})
        self.assertTrue(all(row.amount == 2.0 for row in rows))     # 6 / 3
        self.assertIn("Кто скидывается: Маша Петрова", reply)

    def test_unregistered_members_are_skipped(self) -> None:
        storage = InMemoryStorage(default_currency="BYN")
        seed_chat(storage, members=(MEMBER_LEHA, MEMBER_DIMA, MEMBER_GOSHA), register=False)
        storage.register_member(MEMBER_LEHA)
        storage.register_member(MEMBER_DIMA)
        reply = handle_text("Дима заплатил 10 за всех", CHAT, storage=storage,
                            parser=self.parser, settings=self.settings,
                            members=storage.list_members(CHAT), author=MEMBER_DIMA)
        rows = storage.list_debts(CHAT)
        self.assertEqual(len(rows), 1)                       # только зарегистрированный Леша
        self.assertEqual(rows[0].from_user_id, 101)
        self.assertEqual(rows[0].amount, 5.0)                # 10 / 2 (Дима платил + Леша)
        self.assertIn("Не участвуют (не зарегистрированы): Гоша Петров", reply)

    def test_unregistered_participant_is_reported(self) -> None:
        reply = self.send("Дима оплатил 10 за Гошу")
        self.assertEqual(self.rows(), [])
        self.assertIn("Не понял, за кого счёт: Гошу", reply)
        self.assertIn("/reg", reply)

    def test_payer_must_be_registered(self) -> None:
        reply = self.send("я заплатил 10 за всех", author=MEMBER_GOSHA)
        self.assertEqual(self.rows(), [])
        self.assertIn("зарегистрированных", reply)

    def test_expense_shows_in_report(self) -> None:
        self.send("Дима заплатил 10 за всех")
        report = self.send("/debts")
        self.assertIn("Общие счета", report)
        self.assertIn("«Дима заплатил 10 за всех»", report)
        self.assertIn("(доля общего счёта)", report)
        self.assertIn("Маша Петрова (@petrova_m) → Дмитрий Болт (@bdzmity): 2.00 BYN", report)

    def test_expense_is_counted_in_netting(self) -> None:
        self.send("Дима заплатил 10 за всех")
        balances = net_balances(self.rows(), self.members)
        debtors = {balance.debtor for balance in balances
                   if balance.creditor.startswith("Дмитрий Болт")}
        self.assertEqual(debtors, {"Леша Козлов (@kozlovAlex)", "Маша Петрова (@petrova_m)",
                                   "Оля Смирнова (@olga_s)", "Петя Кузнецов (@petya_k)"})

    def test_undo_removes_whole_expense(self) -> None:
        self.send("Дима заплатил 10 за всех")
        reply = self.send("/undo")
        self.assertEqual(self.rows(), [])
        self.assertIn("Удалил общий счёт", reply)
        self.assertIn("записей 4", reply)

    def test_undo_keeps_other_records(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        self.send("Дима заплатил 10 за всех")
        self.send("/undo")                                   # убираем счёт целиком
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("Удалил последнюю запись", self.send("/undo"))
        self.assertEqual(self.rows(), [])


class ExpenseStorageTests(unittest.TestCase):
    """Слой Supabase и память: массовая запись долей, группа и регистрация."""

    def setUp(self) -> None:
        self.session = FakeSession()
        self.storage = SupabaseStorage(
            "https://example.supabase.co/", "service-key", session=self.session,
        )

    def test_add_debts_posts_array(self) -> None:
        self.session.responses = [FakeResponse([
            {"id": 1, "chat_id": 7, "amount": 2.5, "kind": "expense", "group_id": "g1"},
            {"id": 2, "chat_id": 7, "amount": 2.5, "kind": "expense", "group_id": "g1"},
        ])]
        rows = self.storage.add_debts(7, [
            {"from_name": "Леша", "to_name": "Дима", "from_user_id": 101, "to_user_id": 102,
             "currency": "BYN", "amount": 2.5, "kind": "expense", "raw_text": "текст",
             "group_id": "g1"},
            {"from_name": "Маша", "to_name": "Дима", "from_user_id": 103, "to_user_id": 102,
             "currency": "BYN", "amount": 2.5, "kind": "expense", "raw_text": "текст",
             "group_id": "g1"},
        ])
        call = self.session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIsInstance(call["payload"], list)
        self.assertEqual(call["payload"][0]["chat_id"], 7)
        self.assertEqual(call["payload"][0]["group_id"], "g1")
        self.assertEqual(call["payload"][1]["from_user_id"], 103)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].is_expense)
        self.assertEqual(rows[0].group_id, "g1")

    def test_add_debts_without_rows_makes_no_request(self) -> None:
        self.assertEqual(self.storage.add_debts(7, []), [])
        self.assertEqual(self.session.calls, [])

    def test_delete_group_filters_by_group(self) -> None:
        self.session.responses = [FakeResponse([{"id": 1}, {"id": 2}])]
        removed = self.storage.delete_group(7, "g1")
        call = self.session.calls[0]
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(call["params"]["chat_id"], "eq.7")
        self.assertEqual(call["params"]["group_id"], "eq.g1")
        self.assertEqual(removed, 2)
        self.assertEqual(self.storage.delete_group(7, ""), 0)

    def test_register_member_marks_registration(self) -> None:
        self.session.responses = [FakeResponse([])]
        self.storage.register_member(ChatMember(
            chat_id=7, user_id=101, username="kozlovAlex", display_name="Леша Козлов",
            aliases=["Леша", "Женя"], is_registered=True,
        ))
        payload = self.session.calls[0]["payload"]
        self.assertTrue(payload["is_registered"])
        self.assertEqual(payload["aliases"], ["Леша", "Женя"])

    def test_remember_member_does_not_wipe_registration(self) -> None:
        # Автообучение по автору сообщения не должно сбрасывать /reg и алиасы.
        self.session.responses = [FakeResponse([])]
        self.storage.remember_member(ChatMember(chat_id=7, user_id=101,
                                                display_name="Леша Козлов"))
        payload = self.session.calls[0]["payload"]
        self.assertNotIn("is_registered", payload)
        self.assertNotIn("aliases", payload)

    def test_memory_add_debts_and_delete_group(self) -> None:
        memory = InMemoryStorage()
        memory.add_debts(7, [
            {"from_name": "Леша", "to_name": "Дима", "currency": "BYN", "amount": 2.5,
             "kind": "expense", "group_id": "g1"},
            {"from_name": "Маша", "to_name": "Дима", "currency": "BYN", "amount": 2.5,
             "kind": "expense", "group_id": "g1"},
        ])
        self.assertEqual(len(memory.list_debts(7)), 2)
        self.assertEqual(memory.delete_group(7, "g1"), 2)
        self.assertEqual(memory.list_debts(7), [])
        self.assertEqual(memory.delete_group(7, "нет"), 0)

    def test_memory_remember_member_keeps_registration(self) -> None:
        memory = InMemoryStorage()
        memory.register_member(MEMBER_LEHA)
        memory.remember_member(ChatMember(chat_id=CHAT, user_id=101,
                                          display_name="Леша Козлов"))
        stored = memory.list_members(CHAT)[0]
        self.assertTrue(stored.is_registered)
        self.assertEqual(stored.aliases, ["Леша", "Лёха"])


class SplitAmountTests(unittest.TestCase):
    """Деление суммы на равные доли: копейки не теряются."""

    def test_simple_split(self) -> None:
        self.assertEqual(split_amount(10, 4), [2.5, 2.5, 2.5, 2.5])

    def test_cents_are_distributed(self) -> None:
        parts = split_amount(10, 3)
        self.assertEqual(len(parts), 3)
        self.assertEqual(round(sum(parts), 2), 10.0)
        self.assertEqual(sorted(parts), [3.33, 3.33, 3.34])

    def test_rounding_down_does_not_overshoot(self) -> None:
        parts = split_amount(10, 7)
        self.assertEqual(round(sum(parts), 2), 10.0)

    def test_no_people(self) -> None:
        self.assertEqual(split_amount(10, 0), [])


class HeuristicExpenseTests(unittest.TestCase):
    """Офлайн-разбор общего счёта: «за всех», «кроме Оли», «за Машу и Петю»."""

    def test_paid_for_everyone(self) -> None:
        parsed = heuristic_parse("Дима заплатил 10 за всех")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "expense")
        self.assertTrue(parsed.is_expense)
        self.assertEqual((parsed.from_name, parsed.amount), ("Дима", 10.0))
        self.assertIsNone(parsed.participants)          # за всех
        self.assertEqual(parsed.exclude, [])

    def test_paid_without_scope_means_everyone(self) -> None:
        parsed = heuristic_parse("я заплатил 15 рублей")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.intent, "expense")
        self.assertIsNone(parsed.participants)

    def test_exclusions_are_parsed(self) -> None:
        parsed = heuristic_parse("Маша оплатила ужин 30 рублей за всех кроме Оли")
        self.assertEqual(parsed.intent, "expense")
        self.assertEqual(parsed.amount, 30.0)
        self.assertEqual(parsed.currency, "BYN")
        self.assertEqual(parsed.exclude, ["Оли"])

    def test_except_self(self) -> None:
        parsed = heuristic_parse("я заплатил 9 за всех кроме себя")
        self.assertEqual(parsed.exclude, ["себя"])

    def test_named_participants(self) -> None:
        parsed = heuristic_parse("Дима оплатил 10 за Машу и Петю")
        self.assertEqual(parsed.participants, ["Машу", "Петю"])

    def test_dish_after_za_is_not_a_person(self) -> None:
        # «за ужин» — не человек: считаем, что платили за всех.
        parsed = heuristic_parse("Дима заплатил 10 за ужин")
        self.assertIsNone(parsed.participants)

    def test_amount_is_required(self) -> None:
        self.assertIsNone(heuristic_parse("Дима заплатил за всех"))


class AiExpenseTests(unittest.TestCase):
    """Ответы ИИ про общий счёт: поля participants/exclude и подсказки в промпте."""

    def build(self) -> tuple[DeepSeekParser, FakeSession]:
        """Парсер DeepSeek с подменённым HTTP-слоем."""
        session = FakeSession()
        parser = DeepSeekParser("sk-test", base_url="https://api.deepseek.com", session=session)
        return parser, session

    def test_expense_fields_are_parsed(self) -> None:
        parser, session = self.build()
        content = json.dumps({
            "intent": "expense", "from": "я", "from_user_id": 102, "amount": 30,
            "currency": "BYN", "exclude": ["Оля"], "participants": "Маша, Петя",
        })
        session.responses = [FakeResponse({"choices": [{"message": {"content": content}}]})]
        parsed = parser.parse("оплатил ужин 30 за Машу и Петю кроме Оли", "BYN",
                              members=[MEMBER_DIMA, MEMBER_MASHA], author=MEMBER_DIMA)
        self.assertEqual(parsed.intent, "expense")
        self.assertTrue(parsed.is_expense)
        self.assertEqual((parsed.from_user_id, parsed.amount), (102, 30.0))
        self.assertEqual(parsed.participants, ["Маша", "Петя"])
        self.assertEqual(parsed.exclude, ["Оля"])

    def test_expense_without_amount_is_not_ready(self) -> None:
        self.assertFalse(ParsedMessage(intent="expense", from_name="Дима").is_expense)

    def test_prompt_teaches_expense(self) -> None:
        self.assertIn("expense", SYSTEM_PROMPT)
        self.assertIn("participants", SYSTEM_PROMPT)
        self.assertIn("кроме", SYSTEM_PROMPT)

    def test_ai_expense_is_recorded(self) -> None:
        storage = InMemoryStorage(default_currency="BYN")
        members = seed_chat(storage)
        parsed = ParsedMessage(intent="expense", from_name="Дима", from_user_id=102,
                               amount=9.0, currency="BYN", exclude=["Оля"])
        reply = handle_text("Дима заплатил 9 за всех кроме Оли", CHAT, storage=storage,
                            parser=FakeParser(parsed), settings=Settings(default_currency="BYN"),
                            members=members, author=MEMBER_DIMA)
        rows = storage.list_debts(CHAT)
        self.assertEqual(len(rows), 3)                          # Леша, Маша, Петя
        self.assertTrue(all(row.amount == 2.25 for row in rows))     # 9 / 4 (с платившим)
        self.assertIn("Записал общий счёт", reply)


class RatesParsingTests(unittest.TestCase):
    """Разбор ответов ExchangeRate-API и арифметика курсов."""

    def test_parse_latest_response(self) -> None:
        payload = {
            "result": "success", "base_code": "BYN",
            "conversion_rates": {"BYN": 1, "USD": 0.3077, "EUR": 0.2841, "RUB": 30.2},
        }
        rates = parse_rates(payload, "BYN")
        self.assertEqual(sorted(rates), ["BYN", "EUR", "RUB", "USD"])
        self.assertEqual(rates["BYN"], Decimal(1))
        self.assertIsInstance(rates["USD"], Decimal)      # курсы держим точными, не float
        # сервис отдаёт «сколько USD за 1 BYN» — храним обратный курс (1 USD = 3.25 BYN)
        self.assertAlmostEqual(float(rates["USD"]), 1 / 0.3077, places=8)
        self.assertEqual(rates["USD"], Decimal("3.24991875"))       # 8 знаков, как в базе

    def test_parse_open_endpoint_uses_rates_field(self) -> None:
        payload = {"result": "success", "provider": "https://www.exchangerate-api.com",
                   "base_code": "USD", "rates": {"USD": 1, "BYN": 3.2, "EUR": "0,91"}}
        rates = parse_rates(payload, "USD")
        self.assertAlmostEqual(float(rates["BYN"]), 1 / 3.2, places=6)
        self.assertAlmostEqual(float(rates["EUR"]), 1 / 0.91, places=6)

    def test_error_types_are_explained(self) -> None:
        cases = {
            "invalid-key": "RATES_API_KEY",
            "quota-reached": "лимит",
            "inactive-account": "аккаунт не активирован",
            "unsupported-code": "не поддерживается",
        }
        for kind, expected in cases.items():
            with self.assertRaises(RatesError) as ctx:
                parse_rates({"result": "error", "error-type": kind}, "BYN")
            self.assertIn(expected, str(ctx.exception))

    def test_broken_payload_is_reported(self) -> None:
        with self.assertRaises(RatesError):
            parse_rates("мусор", "BYN")
        with self.assertRaises(RatesError):
            parse_rates({"result": "success"}, "BYN")

    def test_rate_table_and_lookup(self) -> None:
        table = rate_table([
            RatePoint(rate_date="2026-09-20", base="BYN", currency="USD", rate=Decimal("3.20")),
            RatePoint(rate_date="2026-09-21", base="BYN", currency="USD", rate=Decimal("3.25")),
            RatePoint(rate_date="2026-09-21", base="BYN", currency="EUR", rate=Decimal("3.50")),
        ])
        self.assertEqual(table["2026-09-20"]["BYN"], Decimal(1))
        self.assertEqual(rate_for(table, "2026-09-21", "USD"), (Decimal("3.25"), "2026-09-21"))
        # на дату без курса берём ближайший сохранённый: сначала предыдущий…
        self.assertEqual(rate_for(table, "2026-09-25", "USD"), (Decimal("3.25"), "2026-09-21"))
        # …а если предыдущих нет — следующий (курсы копятся начиная с какого-то дня)
        self.assertEqual(rate_for(table, "2026-09-19", "USD"), (Decimal("3.20"), "2026-09-20"))
        self.assertEqual(rate_for(table, "2026-09-21", "BYN"), (Decimal(1), "2026-09-21"))
        self.assertEqual(rate_for(table, "2026-09-21", "THB"), (None, None))

    def test_convert_amount(self) -> None:
        table = rate_table([
            RatePoint(rate_date="2026-09-21", base="BYN", currency="USD", rate=Decimal("3.25")),
            RatePoint(rate_date="2026-09-21", base="BYN", currency="EUR", rate=Decimal("3.50")),
        ])
        self.assertEqual(convert_amount(10, "USD", "BYN", table, "2026-09-21"),
                         (32.5, "2026-09-21"))
        self.assertEqual(convert_amount(10, "USD", "EUR", table, "2026-09-21"),
                         (9.29, "2026-09-21"))            # 10 × 3.25 ÷ 3.50 = 9.2857… → 9.29
        self.assertEqual(convert_amount(10, "USD", "USD", table, "2026-09-21"),
                         (10.0, "2026-09-21"))
        self.assertEqual(convert_amount(10, "THB", "BYN", table, "2026-09-21"), (None, None))

    def test_client_requests_and_errors(self) -> None:
        session = FakeSession([FakeResponse({"result": "error", "error-type": "quota-reached"})])
        with self.assertRaises(RatesError) as ctx:
            fetch_latest("BYN", api_url="https://v6.exchangerate-api.com/v6",
                         api_key="test-key", session=session)
        self.assertIn("лимит", str(ctx.exception))
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/v6/test-key/latest/BYN"))   # ключ в адресе
        self.assertIsNone(call["params"])

    def test_http_429_is_reported(self) -> None:
        session = FakeSession([FakeResponse({"error": "slow down"}, status=429)])
        with self.assertRaises(RatesError) as ctx:
            fetch_latest("BYN", api_url="https://v6.exchangerate-api.com/v6",
                         api_key="test-key", session=session)
        self.assertIn("429", str(ctx.exception))

    def test_latest_url_shapes(self) -> None:
        self.assertEqual(latest_url("byn", api_url="https://v6.exchangerate-api.com/v6/",
                                    api_key="key"),
                         "https://v6.exchangerate-api.com/v6/key/latest/BYN")
        self.assertEqual(latest_url("byn", open_url="https://open.er-api.com/v6"),
                         "https://open.er-api.com/v6/latest/BYN")

    def test_rates_report_text(self) -> None:
        report = format_rates_report([
            RatePoint(rate_date="2026-09-21", base="BYN", currency="USD", rate=Decimal("3.2531")),
            RatePoint(rate_date="2026-09-21", base="BYN", currency="RUB", rate=Decimal("0.0331")),
        ], "BYN", target="USD", schedule="раз в день в 12:00 по Минску")
        self.assertIn("1 USD = 3.2531 BYN (доллар США)", report)
        self.assertIn("1 RUB = 0.0331 BYN", report)
        self.assertIn("21.09.2026", report)
        self.assertIn("Валюта чата: USD", report)
        self.assertIn("раз в день в 12:00 по Минску", report)
        self.assertIn("Курсов валют пока нет", format_rates_report([], "BYN"))


class RatesUpdateTests(unittest.TestCase):
    """Обновление курсов: раз в день, без cron, с понятными причинами отказа."""

    def settings(self, **kwargs) -> Settings:
        """Настройки с тестовым ключом ExchangeRate-API."""
        return Settings(
            rates_api_key="test-key", rates_api_url="https://v6.exchangerate-api.com/v6",
            rates_base="BYN", rates_currencies=("BYN", "USD", "EUR"),
            request_timeout=5.0, **kwargs,
        )

    def test_one_request_saves_configured_currencies(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([FakeResponse({
            "result": "success", "base_code": "BYN",
            "conversion_rates": {"BYN": 1, "USD": 0.25, "EUR": 0.2, "THB": 8.0},
        })])
        result = update_rates(self.settings(), storage, today="2026-09-21", session=session)
        self.assertEqual(len(session.calls), 1)          # один запрос отдаёт все валюты
        self.assertEqual(result.saved, 2)                # BYN — база, THB не в списке
        self.assertEqual(result.currencies, ("USD", "EUR"))
        self.assertEqual([point.currency for point in storage.rates], ["USD", "EUR"])
        self.assertEqual(storage.rates[0].rate, Decimal("4"))   # 1 USD = 4 BYN
        self.assertEqual(storage.rates[0].source, "exchangerate-api.com")

    def test_second_call_same_day_does_not_fetch(self) -> None:
        storage = InMemoryStorage()
        payload = {"result": "success", "base_code": "BYN", "conversion_rates": {"USD": 0.25}}
        session = FakeSession([FakeResponse(payload), FakeResponse(payload)])
        update_rates(self.settings(), storage, today="2026-09-21", session=session)
        again = update_rates(self.settings(), storage, today="2026-09-21", session=session)
        self.assertEqual(again.saved, 0)
        self.assertIn("уже сохранены", again.reason)
        self.assertEqual(len(session.calls), 1)          # к API сходили один раз
        update_rates(self.settings(), storage, today="2026-09-21", force=True, session=session)
        self.assertEqual(len(session.calls), 2)          # --force обновляет заново

    def test_open_endpoint_without_key(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([FakeResponse({"result": "success", "base_code": "BYN",
                                             "rates": {"USD": 0.25}})])
        settings = Settings(rates_api_key="", rates_open_url="https://open.er-api.com/v6",
                            rates_base="BYN", rates_currencies=("BYN", "USD"),
                            request_timeout=5.0)
        result = update_rates(settings, storage, today="2026-09-21", session=session)
        self.assertEqual(result.saved, 1)
        self.assertTrue(session.calls[0]["url"].endswith("/open.er-api.com/v6/latest/BYN"))

    def test_api_error_becomes_problem(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([FakeResponse({"result": "error", "error-type": "invalid-key"})])
        result = update_rates(self.settings(), storage, today="2026-09-21", session=session)
        self.assertEqual(result.saved, 0)
        self.assertFalse(result.updated)
        self.assertEqual(result.reason, "курсы получить не удалось")
        self.assertTrue(any("RATES_API_KEY" in problem for problem in result.problems))
        self.assertEqual(storage.rates, [])

    def test_missing_currency_is_reported(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([FakeResponse({"result": "success", "base_code": "BYN",
                                             "conversion_rates": {"EUR": 0.2}})])
        result = update_rates(self.settings(), storage, today="2026-09-21", session=session)
        self.assertEqual(result.saved, 1)                # EUR сохранили
        self.assertEqual(result.currencies, ("EUR",))
        self.assertTrue(any("USD" in problem for problem in result.problems))


class RatesScheduleTests(unittest.TestCase):
    """Автообновление курсов: раз в день в RATES_HOUR по Минску, без cron."""

    def settings(self, **kwargs: Any) -> Settings:
        """Настройки с тестовым ключом и расписанием по умолчанию (12:00)."""
        return Settings(
            rates_api_key="test-key", rates_api_url="https://v6.exchangerate-api.com/v6",
            rates_base="BYN", rates_currencies=("BYN", "USD"), request_timeout=5.0, **kwargs,
        )

    def payload(self) -> FakeResponse:
        """Ответ API: 1 BYN = 0.25 USD, то есть 1 USD = 4 BYN."""
        return FakeResponse({"result": "success", "base_code": "BYN",
                             "conversion_rates": {"BYN": 1, "USD": 0.25}})

    def test_before_schedule_hour_nothing_happens(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([])
        result = update_rates_scheduled(self.settings(), storage, session=session,
                                        now=datetime(2026, 9, 21, 11, 59), cache={})
        self.assertIsNone(result)
        self.assertEqual(session.calls, [])              # до 12:00 ни API, ни база не нужны
        self.assertEqual(storage.rates, [])

    def test_rates_are_loaded_at_schedule_hour(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([self.payload()])
        cache: dict[str, str] = {}
        result = update_rates_scheduled(self.settings(), storage, session=session,
                                        now=datetime(2026, 9, 21, 12, 0), cache=cache)
        self.assertIsNotNone(result)
        self.assertTrue(result.updated)
        self.assertEqual(result.rate_date, "2026-09-21")         # дата курса — по Минску
        self.assertEqual(cache["BYN"], "2026-09-21")
        self.assertEqual(storage.rates[0].rate, Decimal("4"))

    def test_second_check_same_day_does_not_touch_api(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([self.payload()])
        cache: dict[str, str] = {}
        update_rates_scheduled(self.settings(), storage, session=session,
                               now=datetime(2026, 9, 21, 12, 5), cache=cache)
        for hour in (13, 18, 23):
            again = update_rates_scheduled(self.settings(), storage, session=session,
                                           now=datetime(2026, 9, 21, hour, 30), cache=cache)
            self.assertIsNone(again)
        self.assertEqual(len(session.calls), 1)                  # запрос к API ровно один

    def test_next_day_is_loaded_again(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([self.payload(), self.payload()])
        cache: dict[str, str] = {}
        update_rates_scheduled(self.settings(), storage, session=session,
                               now=datetime(2026, 9, 21, 12, 0), cache=cache)
        result = update_rates_scheduled(self.settings(), storage, session=session,
                                       now=datetime(2026, 9, 22, 12, 0), cache=cache)
        self.assertTrue(result.updated)
        self.assertEqual(result.rate_date, "2026-09-22")
        self.assertEqual(len(session.calls), 2)

    def test_hour_is_configurable(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([self.payload()])
        result = update_rates_scheduled(self.settings(rates_hour=8), storage, session=session,
                                        now=datetime(2026, 9, 21, 8, 5), cache={})
        self.assertTrue(result.updated)

    def test_problem_is_returned_to_caller(self) -> None:
        storage = InMemoryStorage()
        session = FakeSession([FakeResponse({"result": "error", "error-type": "invalid-key"})])
        result = update_rates_scheduled(self.settings(), storage, session=session,
                                        now=datetime(2026, 9, 21, 12, 0), cache={})
        self.assertIsNotNone(result)
        self.assertFalse(result.updated)
        self.assertTrue(any("RATES_API_KEY" in problem for problem in result.problems))

    def test_minsk_time_and_day(self) -> None:
        self.assertEqual(minsk_now().utcoffset(), timedelta(hours=3))   # UTC+3 круглый год
        self.assertEqual(rates_day(datetime(2026, 9, 21, 23, 30)), "2026-09-21")


class RateScaleTests(unittest.TestCase):
    """Курс в базе — целое (bigint): rate = курс × RATE_SCALE (10⁸)."""

    def test_scale_constants(self) -> None:
        self.assertEqual((RATE_SCALE, RATE_DIGITS), (100_000_000, 8))

    def test_scale_and_unscale(self) -> None:
        self.assertEqual(scale_rate(3.2531), 325310000)
        self.assertEqual(scale_rate(Decimal("0.03311106")), 3311106)
        self.assertEqual(scale_rate("3.25"), 325000000)
        self.assertEqual(unscale_rate(325310000), Decimal("3.2531"))
        self.assertEqual(unscale_rate("326000000"), Decimal("3.26"))
        self.assertEqual(unscale_rate(None), Decimal(0))

    def test_rate_survives_storage_round_trip(self) -> None:
        """После записи в базу и чтения курс не теряет знаков (раньше это был float)."""
        original = Decimal("3.25311234")
        stored = scale_rate(original)                     # как уходит в bigint
        self.assertIsInstance(stored, int)
        self.assertEqual(unscale_rate(stored), original)   # как читается обратно
        self.assertEqual(scale_rate(unscale_rate(stored)), stored)

    def test_convert_amount_rounds_half_up(self) -> None:
        table = rate_table([
            RatePoint(rate_date="2026-09-21", base="BYN", currency="USD",
                      rate=Decimal("3.2531")),
            RatePoint(rate_date="2026-09-21", base="BYN", currency="EUR",
                      rate=Decimal("3.5012")),
        ])
        value, used_day = convert_amount(100, "USD", "EUR", table, "2026-09-21")
        expected = (Decimal(100) * Decimal("3.2531") / Decimal("3.5012")
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.assertEqual(value, float(expected))
        self.assertEqual(used_day, "2026-09-21")


class PasswordTests(unittest.TestCase):
    """Пароль чата: пока он не введён, бот в чате не работает."""

    def setUp(self) -> None:
        self.parser = HeuristicParser()
        self.storage = InMemoryStorage(default_currency="BYN")
        seed_chat(self.storage)
        self.members = self.storage.list_members(CHAT)
        self.settings = Settings(default_currency="BYN", chat_password="сезам")

    def send(self, text: str, chat: int = CHAT) -> str:
        """Отправляет сообщение от имени Леши Козлова."""
        return handle_text(text, chat, storage=self.storage, parser=self.parser,
                           settings=self.settings,
                           members=self.storage.list_members(chat), author=MEMBER_LEHA)

    def test_debt_is_refused_before_password(self) -> None:
        reply = self.send("Леша должен Диме 3 рубля")
        self.assertIn("пришлите пароль", reply)
        self.assertEqual(self.storage.list_debts(CHAT), [])

    def test_help_is_not_available_before_password(self) -> None:
        self.assertIn("пришлите пароль", self.send("/help"))

    def test_wrong_password_is_reported(self) -> None:
        self.assertIn("не подошёл", self.send("/password наугад"))
        self.assertIn("не подошёл", self.send("наугад"))
        self.assertEqual(self.storage.list_debts(CHAT), [])

    def test_correct_password_unlocks_chat(self) -> None:
        self.assertIn("Пароль принят", self.send("/password сезам"))
        self.assertTrue(self.storage.chat_authorized(CHAT))
        self.assertIn("Записал долг", self.send("Леша должен Диме 3 рубля"))

    def test_password_as_plain_message(self) -> None:
        self.assertIn("Пароль принят", self.send("сезам"))

    def test_unlock_is_per_chat(self) -> None:
        seed_chat(self.storage, chat=999)
        self.assertIn("Пароль принят", self.send("/password сезам"))
        reply = self.send("Леша должен Диме 3 рубля", chat=999)
        self.assertIn("пришлите пароль", reply)

    def test_without_password_setting_nothing_is_asked(self) -> None:
        reply = handle_text("Леша должен Диме 3 рубля", CHAT, storage=self.storage,
                            parser=self.parser, settings=Settings(default_currency="BYN"),
                            members=self.members, author=MEMBER_LEHA)
        self.assertIn("Записал долг", reply)

    def test_bot_added_to_chat_asks_password(self) -> None:
        telegram = FakeTelegram([])
        bot = DebtBot(self.settings, self.storage, self.parser, telegram)
        bot.process_update({"update_id": 1, "my_chat_member": {
            "chat": {"id": GROUP, "type": "supergroup"},
            "from": {"id": 100},
            "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "member"},
        }})
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(telegram.sent[0][0], GROUP)
        self.assertIn("пришлите пароль", telegram.sent[0][1])

    def test_greeting_without_password_setting(self) -> None:
        telegram = FakeTelegram([])
        bot = DebtBot(Settings(default_currency="BYN"), self.storage, self.parser, telegram)
        bot.process_update({"update_id": 2, "my_chat_member": {
            "chat": {"id": GROUP},
            "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "administrator"},
        }})
        self.assertIn("калькулятор долгов", telegram.sent[0][1])
        self.assertNotIn("пришлите пароль", telegram.sent[0][1])

    def test_bot_removed_resets_access(self) -> None:
        telegram = FakeTelegram([])
        bot = DebtBot(self.settings, self.storage, self.parser, telegram)
        self.storage.set_chat_authorized(GROUP, True)
        bot.process_update({"update_id": 3, "my_chat_member": {
            "chat": {"id": GROUP},
            "old_chat_member": {"status": "member"},
            "new_chat_member": {"status": "left"},
        }})
        self.assertFalse(self.storage.chat_authorized(GROUP))
        self.assertEqual(telegram.sent, [])              # на выход ничего не пишем


class ConvertedReportTests(unittest.TestCase):
    """Команда /d: все долги приводятся к валюте чата по курсу на дату записи."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN", rates_base="BYN")
        self.storage = InMemoryStorage(default_currency="BYN",
                                       default_created_at="2026-09-21T10:00:00+00:00")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage)
        self.storage.save_rates([
            {"rate_date": "2026-09-21", "base": "BYN", "currency": "USD", "rate": 3.25},
            {"rate_date": "2026-09-21", "base": "BYN", "currency": "EUR", "rate": 3.50},
        ])

    def send(self, text: str) -> str:
        """Отправляет сообщение от имени Леши Козлова."""
        return handle_text(text, CHAT, storage=self.storage, parser=self.parser,
                           settings=self.settings, members=self.members, author=MEMBER_LEHA)

    def test_d_converts_to_chat_currency(self) -> None:
        self.send("Маша заняла у Пети 10$")
        report = self.send("/d")
        self.assertIn("Привёл к BYN", report)
        self.assertIn("1 USD = 3.25 BYN", report)
        self.assertIn("21.09.2026", report)
        self.assertIn("32.50 BYN", report)               # 10 USD × 3.25
        self.assertNotIn("10.00 USD", report)

    def test_d_targets_chat_currency(self) -> None:
        self.storage.set_default_currency(CHAT, "USD")
        self.send("Леша должен Диме 32.5 рубля")
        report = self.send("/d")
        self.assertIn("10.00 USD", report)               # 32.50 BYN ÷ 3.25

    def test_d_uses_nearest_previous_rate(self) -> None:
        # Оставляем только курс за 20-е: для записи от 22-го он и должен примениться.
        self.storage.rates = [
            RatePoint(rate_date="2026-09-20", base="BYN", currency="USD", rate=Decimal("3.10")),
        ]
        self.storage.add_debt(CHAT, "Леша Козлов", "Дмитрий Болт", "USD", 10,
                              created_at="2026-09-22T10:00:00+00:00")
        report = self.send("/d")
        self.assertIn("20.09.2026", report)               # курса на 22-е нет — взяли 20-е
        self.assertIn("1 USD = 3.1 BYN", report)
        self.assertIn("31.00 BYN", report)

    def test_d_keeps_records_without_rates(self) -> None:
        self.storage.add_debt(CHAT, "Леша Козлов", "Дмитрий Болт", "PLN", 40,
                              created_at="2026-09-21T10:00:00+00:00")
        report = self.send("/d")
        self.assertIn("Без курса оставил: 40.00 PLN", report)

    def test_d_without_records(self) -> None:
        self.assertIn("пересчитывать нечего", self.send("/d"))

    def test_rates_command_shows_saved_rates(self) -> None:
        reply = self.send("/rates")
        self.assertIn("1 USD = 3.25 BYN", reply)
        self.assertIn("1 EUR = 3.5 BYN", reply)
        self.assertIn("21.09.2026", reply)
        self.assertIn("Нужна другая", reply)
        self.assertIn("RATES_API_KEY", reply)             # ключа нет — честно сообщаем

    def test_convert_debts_keeps_original_when_no_rate(self) -> None:
        converted = convert_debts([make_debt("Леша", "Дима", 10, currency="USD")],
                                  "BYN", {}, "BYN")
        self.assertFalse(converted.changed)
        self.assertEqual(converted.debts[0].amount, 10.0)   # курс неизвестен — как есть
        self.assertEqual(converted.skipped, ["10.00 USD"])


class RatesStorageTests(unittest.TestCase):
    """Слой Supabase: курсы валют и признак подтверждения пароля."""

    def setUp(self) -> None:
        self.session = FakeSession()
        self.storage = SupabaseStorage(
            "https://example.supabase.co/", "service-key", session=self.session,
        )

    def test_save_rates_upsert(self) -> None:
        self.session.responses = [FakeResponse([])]
        saved = self.storage.save_rates([
            {"rate_date": "2026-09-21", "base": "byn", "currency": "usd",
             "rate": 3.25314, "source": "wise"},
        ])
        self.assertEqual(saved, 1)
        call = self.session.calls[0]
        self.assertTrue(call["url"].endswith("/rest/v1/currency_rates"))
        self.assertEqual(call["params"]["on_conflict"], "rate_date,base,currency")
        self.assertEqual(call["payload"][0], {
            "rate_date": "2026-09-21", "base": "BYN", "currency": "USD",
            "rate": 325314000, "source": "wise",          # bigint: курс × 100000000
        })

    def test_save_rates_without_points_makes_no_request(self) -> None:
        self.assertEqual(self.storage.save_rates([]), 0)
        self.assertEqual(self.session.calls, [])

    def test_rates_since_filters_by_base(self) -> None:
        self.session.responses = [FakeResponse([
            {"rate_date": "2026-09-21", "base": "BYN", "currency": "USD", "rate": 325000000},
        ])]
        points = self.storage.rates_since("byn", "2026-09-01")
        call = self.session.calls[0]
        self.assertEqual(call["params"]["base"], "eq.BYN")
        self.assertEqual(call["params"]["rate_date"], "gte.2026-09-01")
        self.assertEqual((points[0].currency, points[0].rate), ("USD", Decimal("3.25")))

    def test_has_rates(self) -> None:
        self.session.responses = [FakeResponse([{"currency": "USD"}]), FakeResponse([])]
        self.assertTrue(self.storage.has_rates("2026-09-21", "BYN"))
        self.assertFalse(self.storage.has_rates("2026-09-22", "BYN"))

    def test_chat_authorized_flag(self) -> None:
        self.session.responses = [FakeResponse([{"is_authorized": True}])]
        self.assertTrue(self.storage.chat_authorized(7))
        self.session.calls.clear()
        self.session.responses = [FakeResponse([])]
        self.storage.set_chat_authorized(7, True)
        call = self.session.calls[0]
        self.assertEqual(call["payload"], {"chat_id": 7, "is_authorized": True})
        self.assertEqual(call["params"]["on_conflict"], "chat_id")

    def test_memory_authorized_and_rates(self) -> None:
        memory = InMemoryStorage()
        self.assertFalse(memory.chat_authorized(5))
        memory.set_chat_authorized(5)
        self.assertTrue(memory.chat_authorized(5))
        memory.set_chat_authorized(5, False)
        self.assertFalse(memory.chat_authorized(5))
        memory.save_rates([
            {"rate_date": "2026-09-21", "base": "BYN", "currency": "USD", "rate": 3.25},
        ])
        memory.save_rates([
            {"rate_date": "2026-09-21", "base": "BYN", "currency": "USD", "rate": 3.26},
        ])
        self.assertEqual(len(memory.rates), 1)            # upsert, а не дубль
        self.assertEqual(memory.rates[0].rate, Decimal("3.26"))
        self.assertTrue(memory.has_rates("2026-09-21", "BYN"))
        self.assertEqual(len(memory.rates_since("BYN", "2026-09-01")), 1)
        self.assertEqual(memory.rates_since("BYN", "2026-10-01"), [])


class MinimalTransfersTests(unittest.TestCase):
    """Взаимозачёт по всему чату: минимум переводов вместо цепочки долгов."""

    def test_chain_collapses_to_one_transfer(self) -> None:
        # Леша должен Диме 10, Дима должен Маше 10 → Леша переводит Маше 10
        transfers = minimal_transfers([
            make_debt("Леша Козлов", "Дмитрий Болт", 10, user_ids=(101, 102)),
            make_debt("Дмитрий Болт", "Маша Петрова", 10, user_ids=(102, 103)),
        ], [MEMBER_LEHA, MEMBER_DIMA, MEMBER_MASHA])
        self.assertEqual([item.pretty() for item in transfers],
                         ["Леша Козлов (@kozlovAlex) → Маша Петрова (@petrova_m): 10.00 BYN"])

    def test_pairwise_netting_still_applies(self) -> None:
        transfers = minimal_transfers([
            make_debt("Леша Козлов", "Дмитрий Болт", 10, user_ids=(101, 102)),
            make_debt("Дмитрий Болт", "Леша Козлов", 4, user_ids=(102, 101)),
        ], [MEMBER_LEHA, MEMBER_DIMA])
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].amount, 6.0)

    def test_repayments_reduce_transfers(self) -> None:
        transfers = minimal_transfers([
            make_debt("Леша Козлов", "Дмитрий Болт", 10, user_ids=(101, 102)),
            make_debt("Леша Козлов", "Дмитрий Болт", 3, kind="repayment", user_ids=(101, 102)),
        ], [MEMBER_LEHA, MEMBER_DIMA])
        self.assertEqual([item.amount for item in transfers], [7.0])

    def test_transfers_are_fewer_than_debts(self) -> None:
        transfers = minimal_transfers([
            make_debt("Леша Козлов", "Дмитрий Болт", 10, user_ids=(101, 102)),
            make_debt("Маша Петрова", "Дмитрий Болт", 5, user_ids=(103, 102)),
            make_debt("Дмитрий Болт", "Оля Смирнова", 15, user_ids=(102, 104)),
        ], [MEMBER_LEHA, MEMBER_DIMA, MEMBER_MASHA, MEMBER_OLYA])
        self.assertEqual(len(transfers), 2)              # вместо трёх долгов — два перевода
        self.assertEqual({(item.debtor, item.amount) for item in transfers},
                         {("Леша Козлов (@kozlovAlex)", 10.0), ("Маша Петрова (@petrova_m)", 5.0)})
        self.assertTrue(all(item.creditor == "Оля Смирнова (@olga_s)" for item in transfers))

    def test_currencies_are_kept_apart(self) -> None:
        transfers = minimal_transfers([
            make_debt("Леша Козлов", "Дмитрий Болт", 10, currency="BYN", user_ids=(101, 102)),
            make_debt("Леша Козлов", "Дмитрий Болт", 5, currency="USD", user_ids=(101, 102)),
        ], [MEMBER_LEHA, MEMBER_DIMA])
        self.assertEqual([(item.currency, item.amount) for item in transfers],
                         [("BYN", 10.0), ("USD", 5.0)])

    def test_nothing_to_offset(self) -> None:
        self.assertEqual(minimal_transfers([], []), [])
        self.assertEqual(minimal_transfers([
            make_debt("Леша Козлов", "Дмитрий Болт", 5, user_ids=(101, 102)),
            make_debt("Леша Козлов", "Дмитрий Болт", 5, kind="repayment", user_ids=(101, 102)),
        ], [MEMBER_LEHA, MEMBER_DIMA]), [])


class BotAddressingTests(unittest.TestCase):
    """Когда бот отвечает: обращения, команды в начале сообщения и чужие команды."""

    def test_command_detection(self) -> None:
        self.assertTrue(is_command_for_bot("/help", "test_bot"))
        self.assertTrue(is_command_for_bot("/d 10 USD", "test_bot"))
        self.assertTrue(is_command_for_bot("/debts@test_bot", "test_bot"))
        self.assertTrue(is_command_for_bot("/settle", ""))
        self.assertTrue(is_command_for_bot("/зачёт", "test_bot"))     # команда с кириллицей
        self.assertFalse(is_command_for_bot("/help@other_bot", "test_bot"))
        self.assertFalse(is_command_for_bot("привет /help", "test_bot"))
        self.assertFalse(is_command_for_bot("/usr/bin/ls", "test_bot"))
        self.assertFalse(is_command_for_bot("Леша должен Диме 3", "test_bot"))

    def test_addressing_in_group(self) -> None:
        settings = Settings(require_mention=True)
        command = make_chat_update(1, "/help")["message"]
        self.assertEqual(addressing(command, "test_bot", settings), (True, "/help"))
        mention = make_chat_update(2, "@test_bot /debts")["message"]
        self.assertEqual(addressing(mention, "test_bot", settings), (True, "/debts"))
        plain = make_chat_update(3, "Леша должен Диме 3")["message"]
        self.assertEqual(addressing(plain, "test_bot", settings),
                         (False, "Леша должен Диме 3"))
        foreign = make_chat_update(4, "/help@other_bot")["message"]
        self.assertEqual(addressing(foreign, "test_bot", settings), (False, "/help@other_bot"))


class SettleCommandTests(unittest.TestCase):
    """Команда /settle: минимальный набор переводов в валюте чата."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN", rates_base="BYN")
        self.storage = InMemoryStorage(default_currency="BYN",
                                       default_created_at="2026-09-21T10:00:00+00:00")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage)
        self.storage.save_rates([
            {"rate_date": "2026-09-21", "base": "BYN", "currency": "USD", "rate": 3.25},
        ])

    def send(self, text: str) -> str:
        """Отправляет сообщение от имени Леши Козлова."""
        return handle_text(text, CHAT, storage=self.storage, parser=self.parser,
                           settings=self.settings, members=self.members, author=MEMBER_LEHA)

    def test_fewer_transfers_than_debts(self) -> None:
        self.send("Леша должен Диме 10 рублей")
        self.send("Дима должен Маше 10 рублей")
        reply = self.send("/settle")
        self.assertIn("Минимум переводов, чтобы всё закрылось", reply)
        self.assertIn("Леша Козлов (@kozlovAlex) → Маша Петрова (@petrova_m): 10.00 BYN", reply)
        self.assertNotIn("Дмитрий Болт", reply)          # долг «через Диму» больше не нужен

    def test_settle_converts_to_chat_currency(self) -> None:
        self.send("Маша заняла у Пети 10$")
        reply = self.send("/settle")
        self.assertIn("Считаю в BYN", reply)             # по курсу на дату записи
        self.assertIn("Маша Петрова (@petrova_m) → Петя Кузнецов (@petya_k): 32.50 BYN", reply)

    def test_settle_without_debts(self) -> None:
        self.assertIn("закрывать нечего", self.send("/settle"))

    def test_settle_aliases(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        for text in ("/offset", "/зачёт", "/зачет"):
            self.assertIn("Леша Козлов (@kozlovAlex) → Дмитрий Болт (@bdzmity): 3.00 BYN",
                          self.send(text))

    def test_report_shows_minimal_transfers_block(self) -> None:
        self.send("Леша должен Диме 10 рублей")
        self.send("Дима должен Маше 10 рублей")
        report = self.send("/debts")
        self.assertIn("Минимум переводов, чтобы всё закрылось:", report)
        self.assertIn("Леша Козлов (@kozlovAlex) → Маша Петрова (@petrova_m): 10.00 BYN", report)

    def test_block_is_hidden_when_same_as_pairwise(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        self.assertNotIn("Минимум переводов", self.send("/debts"))


class DebtsDumpTests(unittest.TestCase):
    """TXT-выгрузка: файл копирует строки таблицы debts этого чата."""

    def test_dump_has_header_columns_and_row(self) -> None:
        debts = [
            Debt(id=1, chat_id=CHAT, created_at="2026-09-21T10:00:00+00:00",
                 from_name="Леша Козлов", to_name="Дмитрий Болт", from_user_id=101,
                 to_user_id=102, currency="BYN", amount=3.0, kind="debt",
                 raw_text="Леша должен Диме 3 рубля"),
        ]
        lines = format_debts_dump(debts, CHAT).splitlines()
        self.assertEqual(lines[0], f"# Выгрузка таблицы debts: chat_id={CHAT}, записей: 1")
        self.assertEqual(lines[1], "\t".join(DEBTS_DUMP_COLUMNS))
        cells = lines[2].split("\t")
        self.assertEqual(cells[:5], ["1", "2026-09-21T10:00:00+00:00", str(CHAT),
                                     "Леша Козлов", "Дмитрий Болт"])
        self.assertEqual(cells[5:9], ["101", "102", "BYN", "3.00"])
        self.assertEqual(cells[11], "Леша должен Диме 3 рубля")

    def test_empty_dump_keeps_columns(self) -> None:
        dump = format_debts_dump([], CHAT)
        self.assertIn(f"chat_id={CHAT}, записей: 0", dump)
        self.assertIn("raw_text", dump)
        self.assertTrue(dump.endswith("\n"))     # файл заканчивается переводом строки

    def test_missing_fields_stay_empty(self) -> None:
        cells = format_debts_dump([make_debt("Леша", "Дима", 0.5)], CHAT).splitlines()[2].split("\t")
        self.assertEqual(cells[0], "")           # id у записи не задан
        self.assertEqual(cells[10], "")          # group_id у обычного долга пустой
        self.assertEqual(cells[8], "0.50")       # сумма как в таблице: два знака

    def test_newlines_in_message_do_not_break_rows(self) -> None:
        debt = Debt(chat_id=CHAT, from_name="Леша", to_name="Дима", currency="BYN",
                    amount=3.5, raw_text="Леша должен\nДиме 3,5")
        dump = format_debts_dump([debt], CHAT)
        self.assertEqual(len(dump.splitlines()), 3)       # шапка, колонки, одна запись
        self.assertIn("Леша должен\\nДиме 3,5", dump)      # перенос строки экранирован


class ExportCommandTests(unittest.TestCase):
    """/export: бот отдаёт записи чата файлом TXT — и только этого чата."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()
        self.members = seed_chat(self.storage)     # без /reg записи не сохраняются

    def send(self, text: str) -> str | TxtReport:
        """Отправляет сообщение боту (автор — Леша Козлов)."""
        return handle_text(text, CHAT, storage=self.storage, parser=self.parser,
                           settings=self.settings, members=self.members, author=MEMBER_LEHA)

    def test_export_sends_txt_copy_of_table(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        reply = self.send("/export")
        self.assertIsInstance(reply, TxtReport)
        self.assertEqual(reply.filename, f"debts_{CHAT}_{date.today().isoformat()}.txt")
        self.assertIn("\t".join(DEBTS_DUMP_COLUMNS), reply.text)
        self.assertIn("Леша Козлов\tДмитрий Болт\t101\t102\tBYN\t3.00\tdebt", reply.text)
        self.assertIn("Леша должен Диме 3 рубля", reply.text)
        self.assertIn("записей 1", reply.caption)

    def test_export_uses_only_this_chat(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        self.storage.add_debt(777, "Маша", "Оля", "BYN", 5.0, raw_text="Маша должна Оле 5")
        reply = self.send("/export")
        self.assertIsInstance(reply, TxtReport)
        self.assertIn("Леша должен Диме 3 рубля", reply.text)
        self.assertNotIn("\t777\t", reply.text)           # чужие записи в файл не попадают
        self.assertNotIn("Маша должна Оле", reply.text)

    def test_export_without_records_answers_with_message(self) -> None:
        reply = self.send("/export")
        self.assertIsInstance(reply, str)
        self.assertIn("выгружать нечего", reply)

    def test_export_aliases(self) -> None:
        self.send("Леша должен Диме 3 рубля")
        for text in ("/report", "/txt", "/файл"):
            self.assertIsInstance(self.send(text), TxtReport)

    def test_help_mentions_export(self) -> None:
        self.assertIn("/export", self.send("/help"))


class BrokenDocumentTelegram(FakeTelegram):
    """Telegram, который отказывается принимать документы: так проверяем ответ чату."""

    def send_document(self, chat_id, filename: str, content: str, **kwargs: Any) -> dict:
        """Имитация ошибки Bot API при отправке файла."""
        raise TelegramError("Telegram вернул HTTP 400: bad request")


class ExportThroughBotTests(unittest.TestCase):
    """Команда /export через DebtBot: отчёт уходит файлом, а не текстом."""

    def build(self, updates: list[dict], telegram_class: type = FakeTelegram):
        """Собирает бота с хранилищем в памяти и подменённым Telegram."""
        settings = Settings(default_currency="BYN")
        storage = InMemoryStorage(default_currency="BYN")
        seed_chat(storage, chat=7)                 # участники чата из make_update
        telegram = telegram_class(updates)
        return DebtBot(settings, storage, HeuristicParser(), telegram), storage, telegram

    def test_report_is_sent_as_document(self) -> None:
        bot, _, telegram = self.build([
            make_update(5, "Леша должен Диме 3 рубля"),
            make_update(6, "/export"),
        ])
        bot.run(poll_timeout=0, max_updates=2)
        self.assertEqual(len(telegram.documents), 1)
        chat_id, filename, content = telegram.documents[0]
        self.assertEqual(chat_id, 7)
        self.assertEqual(filename, f"debts_7_{date.today().isoformat()}.txt")
        self.assertIn("Леша Козлов", content)
        self.assertIn("Леша должен Диме 3 рубля", content)

    def test_nothing_to_export_goes_as_message(self) -> None:
        bot, _, telegram = self.build([make_update(5, "/export")])
        bot.run(poll_timeout=0, max_updates=1)
        self.assertEqual(telegram.documents, [])
        self.assertIn("выгружать нечего", telegram.sent[0][1])

    def test_document_failure_is_explained_in_chat(self) -> None:
        bot, _, telegram = self.build([
            make_update(5, "Леша должен Диме 3 рубля"),
            make_update(6, "/export"),
        ], telegram_class=BrokenDocumentTelegram)
        bot.run(poll_timeout=0, max_updates=2)
        self.assertEqual(telegram.documents, [])
        self.assertIn("Не удалось отправить файл", telegram.sent[-1][1])


class TelegramDocumentTests(unittest.TestCase):
    """Отправка файла: sendDocument уходит multipart-запросом вместе с содержимым отчёта."""

    def setUp(self) -> None:
        self.session = FakeSession()
        self.bot = TelegramBot("123:abc", session=self.session)

    def test_send_document_payload(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": {"message_id": 5}})]
        result = self.bot.send_document(7, "debts_7.txt", "строка\nвторая",
                                        caption="отчёт", reply_to=42)
        self.assertEqual(result["message_id"], 5)
        call = self.session.calls[0]
        self.assertTrue(call["url"].endswith("/sendDocument"))
        self.assertIsNone(call["payload"])                    # файл уходит не JSON-ом
        self.assertEqual(call["data"]["chat_id"], 7)
        self.assertEqual(call["data"]["caption"], "отчёт")
        self.assertEqual(call["data"]["reply_to_message_id"], 42)
        self.assertEqual(call["data"]["allow_sending_without_reply"], "true")
        filename, content, content_type = call["files"]["document"]
        self.assertEqual(filename, "debts_7.txt")
        self.assertEqual(content.decode("utf-8"), "строка\nвторая")
        self.assertIn("text/plain", content_type)

    def test_caption_is_trimmed_to_limit(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": {"message_id": 5}})]
        self.bot.send_document(7, "debts.txt", "строка",
                               caption="о" * (MAX_CAPTION_LENGTH + 50))
        self.assertEqual(len(self.session.calls[0]["data"]["caption"]), MAX_CAPTION_LENGTH)

    def test_optional_fields_are_skipped(self) -> None:
        self.session.responses = [FakeResponse({"ok": True, "result": {"message_id": 5}})]
        self.bot.send_document(7, "debts.txt", "строка".encode("utf-8"))
        self.assertEqual(set(self.session.calls[0]["data"]), {"chat_id"})

    def test_document_errors_are_reported(self) -> None:
        self.session.responses = [FakeResponse({"ok": False, "description": "bad"}, status=400)]
        with self.assertRaises(TelegramError):
            self.bot.send_document(7, "debts.txt", "строка")


if __name__ == "__main__":
    unittest.main(verbosity=2)
