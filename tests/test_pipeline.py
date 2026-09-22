# -*- coding: utf-8 -*-
"""Тесты без внешних сервисов: разбор сообщений, запись, взаимозачёт, ответы бота.

Запуск:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import io
import json
import unittest
from typing import Any

from bot import (
    DebtBot,
    HeuristicParser,
    clean_bot_mention,
    handle_text,
    is_allowed,
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
    format_debts_report,
    name_key,
    net_balances,
    normalize_name,
    totals_by_person,
)
from deepseek import SYSTEM_PROMPT, ParsedMessage, detect_currency, heuristic_parse
from storage import Debt, InMemoryStorage, StorageError, SupabaseStorage
from telegram_api import TelegramBot, TelegramError, split_message
from webhook import SECRET_HEADER, LazyWebhookApp, WebhookApp, build_app

CHAT = 555


def make_debt(debtor: str, creditor: str, amount: float, currency: str = "BYN",
              chat: int = CHAT, kind: str = "debt") -> Debt:
    """Готовит запись (долг или возврат) для проверок логики."""
    return Debt(chat_id=chat, from_name=debtor, to_name=creditor,
                currency=currency, amount=amount, kind=kind)


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

    def parse(self, text: str, default_currency: str = "BYN") -> ParsedMessage:
        """Всегда отдаёт подготовленный результат."""
        return self.parsed


class BotFlowTests(unittest.TestCase):
    """Сквозной сценарий: сообщение → разбор → запись → отчёт."""

    def setUp(self) -> None:
        self.settings = Settings(default_currency="BYN")
        self.storage = InMemoryStorage(default_currency="BYN")
        self.parser = HeuristicParser()

    def send(self, text: str, chat: int = CHAT) -> str:
        """Отправляет сообщение боту и возвращает ответ."""
        return handle_text(text, chat, storage=self.storage, parser=self.parser,
                           settings=self.settings)

    def test_saves_debt_with_explicit_currency(self) -> None:
        reply = self.send("Леша должен Диме 3 рубля")
        self.assertIn("Записал долг", reply)
        self.assertIn("3.00 BYN", reply)
        self.assertNotIn("взял по умолчанию", reply)
        self.assertEqual(len(self.storage.debts), 1)

    def test_uses_default_currency(self) -> None:
        reply = self.send("Леша должен Диме 3")
        self.assertIn("3.00 BYN", reply)
        self.assertIn("взял по умолчанию", reply)

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
        self.assertIn("Леша → Дима: 2.00 BYN", report)

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

    def send_with(self, parsed: ParsedMessage, text: str = "текст") -> str:
        """Обрабатывает сообщение с заранее заданным ответом «ИИ»."""
        return handle_text(text, CHAT, storage=self.storage,
                           parser=FakeParser(parsed), settings=self.settings)

    def test_ai_debt_is_saved(self) -> None:
        reply = self.send_with(ParsedMessage(
            intent="debt", from_name="петя", to_name="Оля", amount=12.5, currency="eur",
        ))
        self.assertIn("12.50 EUR", reply)
        saved = self.storage.list_debts(CHAT)[0]
        self.assertEqual((saved.from_name, saved.to_name), ("Петя", "Оля"))

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
                "SUPABASE_SERVICE_KEY": "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig",
                "ALLOWED_USER_IDS": "1, 2;3",
                "DEFAULT_CURRENCY": "usd",
            },
            use_env_file=False,
        )
        self.assertEqual(settings.telegram_token, "123:abc")
        self.assertEqual(settings.supabase_url, "https://example.supabase.co")
        self.assertEqual(settings.rest_url, "https://example.supabase.co/rest/v1")
        self.assertEqual(settings.allowed_user_ids, frozenset({1, 2, 3}))
        self.assertEqual(settings.default_currency, "USD")
        self.assertEqual(settings.problems(), [])

    def test_problems_when_settings_empty(self) -> None:
        self.assertEqual(len(load_settings({}, use_env_file=False).problems()), 4)


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


class RunOnceTests(unittest.TestCase):
    """Режим GitHub Actions: обработка накопившихся сообщений и хранение offset."""

    def build(self, updates: list[dict], **settings_kwargs):
        """Собирает бота с хранилищем в памяти и фейковым Telegram."""
        settings = Settings(default_currency="BYN", **settings_kwargs)
        storage = InMemoryStorage(default_currency="BYN")
        telegram = FakeTelegram(updates)
        return DebtBot(settings, storage, HeuristicParser(), telegram), storage, telegram

    def test_processes_updates_and_stores_offset(self) -> None:
        bot, storage, telegram = self.build([
            make_update(10, "Леша должен Диме 3 рубля"),
            make_update(11, "/debts"),
        ])
        self.assertEqual(bot.run_once(), 2)
        self.assertEqual(len(storage.list_debts(7)), 1)
        self.assertEqual(storage.get_state("last_update_id"), "12")
        self.assertEqual(len(telegram.sent), 2)
        self.assertIn("Записал долг", telegram.sent[0][1])
        self.assertIn("Итог с взаимозачётом", telegram.sent[1][1])

    def test_no_updates_leaves_state_empty(self) -> None:
        bot, storage, telegram = self.build([])
        self.assertEqual(bot.run_once(), 0)
        self.assertIsNone(storage.get_state("last_update_id"))
        self.assertEqual(telegram.sent, [])

    def test_denied_user_gets_refusal(self) -> None:
        bot, storage, telegram = self.build(
            [make_update(5, "Леша должен Диме 3 рубля", user=999)],
            allowed_user_ids=frozenset({100}),
        )
        bot.run_once()
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
             **kwargs: Any) -> FakeResponse:
        """Имитация requests.post (Telegram, DeepSeek)."""
        self.calls.append({"method": "POST", "url": url, "payload": json, "timeout": timeout})
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


class SupabaseKeyValidationTests(unittest.TestCase):
    """Проверка ключа Supabase: anon вместо service_role выявляется ещё до запросов."""

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


class LongPollingTests(unittest.TestCase):
    """Постоянный режим (хостинг): старт с сохранённого смещения и его запись при выходе."""

    def build(self, updates: list[dict]):
        """Бот с хранилищем в памяти и фейковым Telegram."""
        settings = Settings(default_currency="BYN")
        storage = InMemoryStorage(default_currency="BYN")
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
        bot = DebtBot(settings, BrokenState(), HeuristicParser(), telegram)
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
        telegram = FakeTelegram([])
        bot = DebtBot(settings, storage, HeuristicParser(), telegram)
        return bot, storage, telegram

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

    def send(self, text: str, chat: int = CHAT) -> str:
        """Отправляет сообщение боту и возвращает ответ."""
        return handle_text(text, chat, storage=self.storage, parser=self.parser,
                           settings=self.settings)

    def test_repayment_is_saved_and_reduces_report(self) -> None:
        self.send("Леша должен Диме 5 рублей")
        reply = self.send("Леша вернул Диме 3 рубля")
        self.assertIn("Записал возврат долга", reply)
        saved = self.storage.list_debts(CHAT)[-1]
        self.assertEqual((saved.kind, saved.amount, saved.raw_text),
                         ("repayment", 3.0, "Леша вернул Диме 3 рубля"))
        # В записях встречается только «Диме», поэтому и в отчёте имя в этой форме.
        self.assertIn("Леша → Диме: 2.00 BYN", self.send("/debts"))

    def test_repayment_without_amount_asks_for_details(self) -> None:
        reply = handle_text(
            "Леша вернул Диме", CHAT, storage=self.storage,
            parser=FakeParser(ParsedMessage(intent="repayment", from_name="Леша", to_name="Дима")),
            settings=self.settings,
        )
        self.assertIn("не хватает данных", reply)
        self.assertEqual(self.storage.list_debts(CHAT), [])

    def test_repayment_reply_mentions_default_currency(self) -> None:
        reply = self.send("Леша вернул Диме 3")
        self.assertIn("3.00 BYN", reply)
        self.assertIn("взял по умолчанию", reply)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
