# -*- coding: utf-8 -*-
"""Разбор сообщений о долгах: DeepSeek (основной путь) + офлайн-эвристики (фолбэк).

Результат разбора — ParsedMessage с намерением (intent):
    debt          — сообщение о долге: from / to / currency / amount
    debts         — просьба показать долги
    set_currency  — установить валюту по умолчанию
    help          — вопрос про возможности бота
    none          — не поняли (note — что именно)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

import requests

INTENTS = ("debt", "debts", "set_currency", "help", "none")

# Синонимы валют. По умолчанию «рубль» — белорусский рубль (BYN),
# для российского указывайте «российский рубль», «руб РФ» или ₽.
CURRENCY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "BYN": ("byn", "бр", "б.р", "бел", "белорусск", "руб", "рубль", "рубля", "рублей", "рублю"),
    "USD": ("usd", "us$", "$", "доллар", "доллара", "долларов", "бакс", "баксов", "у.е", "уе"),
    "EUR": ("eur", "€", "евро"),
    "RUB": ("rub", "₽", "российск", "руб рф"),
    "PLN": ("pln", "zł", "злот", "злотых"),
    "UAH": ("uah", "₴", "гривн"),
    "KZT": ("kzt", "₸", "тенге"),
    "GBP": ("gbp", "£", "фунт"),
}

DEBT_VERBS = r"(?:должен|должна|должны|задолжал[аи]?|одолжил[а]?|занял[а]?|owes?|must\s+pay)"
NAME_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё][\w\-]{1,29}")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d{1,2})?")
DEBT_RE = re.compile(
    rf"(?P<debtor>[A-Za-zА-Яа-яЁё][\w\-]{{1,29}})\s+{DEBT_VERBS}\s+(?:у\s+|от\s+)?"
    rf"(?P<creditor>[A-Za-zА-Яа-яЁё][\w\-]{{1,29}})\s*(?:—|-|:)?\s*"
    rf"(?P<amount>\d+(?:[.,]\d{{1,2}})?)?(?P<tail>[^\n]*)",
    re.IGNORECASE,
)

DEBTS_KEYWORDS = ("долг", "долги", "сколько", "кто кому", "баланс", "расчёт", "расчет", "сальдо")
CURRENCY_KEYWORDS = ("валют", "currency", "по умолчанию")
HELP_KEYWORDS = ("помощь", "help", "что ты умеешь", "как пользоваться", "команды")


class DeepSeekError(RuntimeError):
    """Ошибка обращения к DeepSeek."""


@dataclass
class ParsedMessage:
    """Результат разбора сообщения."""

    intent: str = "none"
    from_name: str | None = None
    to_name: str | None = None
    currency: str | None = None
    amount: float | None = None
    note: str | None = None
    source: str = "ai"  # ai | heuristic | fallback

    @property
    def is_debt(self) -> bool:
        """Готов ли результат к сохранению как долг."""
        return (
            self.intent == "debt"
            and bool(self.from_name)
            and bool(self.to_name)
            and isinstance(self.amount, (int, float))
            and float(self.amount) > 0
        )


SYSTEM_PROMPT = """Ты — разборщик сообщений о долгах для телеграм-бота (русский и английский).
Верни ТОЛЬКО JSON без пояснений:
{"intent":"debt|debts|set_currency|help|none","from":"имя","to":"имя","currency":"BYN","amount":3.0,"note":"короткое пояснение"}

Правила:
1. intent=debt — кто-то кому-то должен: «Леша должен Диме 3 рубля», «Маша заняла у Пети 10$».
   from — должник (кто должен), to — кредитор (кому должны), amount — число с точкой.
2. Валюту приводи к коду ISO: рубль/руб/бр = BYN, доллар/$/бакс = USD, евро/€ = EUR,
   российский рубль/₽ = RUB, злотый = PLN, гривна = UAH, тенге = KZT, фунт = GBP.
   Если валюта не названа — поле currency не заполняй.
3. intent=debts — просят показать или посчитать долги («покажи долги», «сколько я должен»).
4. intent=set_currency — просят задать валюту по умолчанию («валюта по умолчанию доллар»).
5. intent=help — спрашивают, что умеет бот.
6. intent=none — всё остальное (в note коротко почему).
7. Имена приводи к именительному падежу (кто?): «Диме»/«Диму» → «Дима», «Леше» → «Леша»,
   «Пете» → «Петя». Пиши только само имя, без лишних слов."""


class DeepSeekParser:
    """Разбор текста через DeepSeek (OpenAI-совместимый API)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: float = 30.0,
        session: Any = None,
    ) -> None:
        self._api_key = str(api_key or "").strip().strip("'\"").strip()
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._session = session or requests

    def parse(self, text: str, default_currency: str = "BYN") -> ParsedMessage:
        """Разбирает текст: сначала DeepSeek, при сбое — офлайн-эвристики."""
        prompt = (
            f"Валюта по умолчанию: {default_currency}.\n"
            f"Сообщение пользователя: {text}"
        )
        error_note: str | None = None
        parsed: ParsedMessage | None = None
        try:
            parsed = self._to_message(self._complete(prompt))
        except DeepSeekError as exc:
            error_note = str(exc)

        if parsed is not None and parsed.intent != "none":
            return parsed

        heuristic = heuristic_parse(text, default_currency)
        if heuristic is not None:
            return heuristic
        if parsed is not None:
            return parsed
        return ParsedMessage(
            intent="none",
            note=error_note or "не удалось разобрать сообщение",
            source="fallback",
        )

    def _complete(self, user_prompt: str) -> str:
        """Запрос к DeepSeek в JSON-режиме."""
        if not self._api_key:
            raise DeepSeekError("Не задан DEEPSEEK_API_KEY.")
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        try:
            response = self._session.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise DeepSeekError(f"DeepSeek недоступен: {exc}") from exc

        if response.status_code == 401:
            raise DeepSeekError("DeepSeek отклонил ключ API (HTTP 401): проверьте DEEPSEEK_API_KEY.")
        if response.status_code == 402:
            raise DeepSeekError("DeepSeek: закончились средства на счёте (HTTP 402).")
        if response.status_code == 429:
            raise DeepSeekError("DeepSeek: слишком много запросов (HTTP 429), попробуйте позже.")
        if response.status_code != 200:
            raise DeepSeekError(f"DeepSeek вернул HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
            return str(data["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("Неожиданный ответ DeepSeek.") from exc

    @staticmethod
    def _to_message(answer: str) -> ParsedMessage | None:
        """Превращает ответ модели в ParsedMessage."""
        data: Mapping[str, Any] | None = None
        try:
            data = json.loads(answer)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", answer or "", re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, Mapping):
            return None

        intent = str(data.get("intent") or "none").strip().lower()
        if intent not in INTENTS:
            intent = "none"
        return ParsedMessage(
            intent=intent,
            from_name=_to_name(data.get("from")),
            to_name=_to_name(data.get("to")),
            currency=_to_currency(data.get("currency")),
            amount=_to_amount(data.get("amount")),
            note=_to_name(data.get("note")),
            source="ai",
        )


def _to_amount(value: Any) -> float | None:
    """Число из данных модели: '3,5' -> 3.5."""
    if value is None or value == "":
        return None
    try:
        return round(float(str(value).replace(",", ".").replace(" ", "")), 2)
    except (TypeError, ValueError):
        return None


def _to_name(value: Any) -> str | None:
    """Аккуратно приводит имя из ответа модели."""
    text = str(value or "").strip().strip("\"'").strip()
    if not text or text.lower() in {"none", "null", "unknown", "-"}:
        return None
    return text[:40]


def _to_currency(value: Any) -> str | None:
    """Код валюты: 'byn' -> 'BYN', 'unknown'/'' -> None."""
    text = str(value or "").strip().upper().strip("\"'")
    if not text or text in {"NONE", "NULL", "UNKNOWN", "-"}:
        return None
    if len(text) == 3 and text.isalpha():
        return text
    return detect_currency(text)


def detect_currency(text: str) -> str | None:
    """Ищет код валюты в тексте по синонимам."""
    lowered = (text or "").lower()
    for code, synonyms in CURRENCY_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in lowered:
                return code
    return None


def check_api_key(
    api_key: str,
    *,
    base_url: str = "https://api.deepseek.com",
    timeout: float = 30.0,
    session: Any = None,
) -> str | None:
    """Проверяет ключ DeepSeek запросом к /models. Возвращает текст проблемы или None."""
    http = session or requests
    if not api_key:
        return "DeepSeek: не задан DEEPSEEK_API_KEY"
    try:
        response = http.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return f"DeepSeek недоступен: {exc}"
    if response.status_code == 200:
        return None
    if response.status_code == 401:
        return "DeepSeek: ключ отклонён (HTTP 401)"
    return f"DeepSeek: HTTP {response.status_code}: {response.text[:120]}"


def _segment_names(segment: str) -> list[str]:
    """Имена из фрагмента текста — без слов, которые являются валютами."""
    return [
        token
        for token in NAME_TOKEN_RE.findall(segment or "")
        if detect_currency(token) is None
    ]


def heuristic_parse(text: str, default_currency: str = "BYN") -> ParsedMessage | None:
    """Разбор сообщения без внешних сервисов (регулярные выражения).

    Понимает «Леша должен Диме 3 рубля», «3 рубля: Леша должен Диме»,
    «покажи долги», «валюта по умолчанию доллар».
    """
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()

    if any(keyword in lowered for keyword in CURRENCY_KEYWORDS):
        code = detect_currency(raw)
        if code:
            return ParsedMessage(intent="set_currency", currency=code, source="heuristic")
    if any(keyword in lowered for keyword in HELP_KEYWORDS):
        return ParsedMessage(intent="help", source="heuristic")
    if any(keyword in lowered for keyword in DEBTS_KEYWORDS):
        return ParsedMessage(intent="debts", source="heuristic")

    match = DEBT_RE.search(raw)
    if match:
        tail = match.group("tail") or ""
        amount = _to_amount(match.group("amount"))
        if amount is None:
            numbers = NUMBER_RE.findall(tail[:60])
            amount = _to_amount(numbers[0]) if numbers else None
        if amount:
            return ParsedMessage(
                intent="debt",
                from_name=_to_name(match.group("debtor")),
                to_name=_to_name(match.group("creditor")),
                currency=detect_currency(tail) or detect_currency(raw) or default_currency,
                amount=amount,
                source="heuristic",
            )

    # порядок слов «3 рубля: Леша должен Диме» — сумма стоит до глагола
    verb = re.search(DEBT_VERBS, raw, re.IGNORECASE)
    if verb:
        before, after = raw[: verb.start()], raw[verb.end():]
        numbers = NUMBER_RE.findall(before)
        debtor = (_segment_names(before) or [None])[-1]
        creditor = (_segment_names(after) or [None])[0]
        amount = _to_amount(numbers[0]) if numbers else None
        if debtor and creditor and amount:
            return ParsedMessage(
                intent="debt",
                from_name=_to_name(debtor),
                to_name=_to_name(creditor),
                currency=detect_currency(raw) or default_currency,
                amount=amount,
                source="heuristic",
            )
    return None


