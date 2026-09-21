# -*- coding: utf-8 -*-
"""Тесты без внешних сервисов: разбор сообщений, запись, взаимозачёт, ответы бота.

Запуск:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from bot import DebtBot, HeuristicParser, handle_text, is_allowed
from config import Settings, load_settings
from debts import name_key, net_balances, normalize_name, totals_by_person
from deepseek import ParsedMessage, detect_currency, heuristic_parse
from storage import Debt, InMemoryStorage
from telegram_api import split_message

CHAT = 555


def make_debt(debtor: str, creditor: str, amount: float, currency: str = "BYN",
              chat: int = CHAT) -> Debt:
    """Готовит запись о долге для проверок логики."""
    return Debt(chat_id=chat, from_name=debtor, to_name=creditor,
                currency=currency, amount=amount)


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

    def get_me(self) -> dict:
        """Имя бота для логов."""
        return {"id": 1, "username": "test_bot"}

    def get_updates(self, offset=None, *, poll_timeout: int = 30, limit: int = 20) -> list[dict]:
        """Отдаёт подготовленные апдейты."""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
