# -*- coding: utf-8 -*-
"""Разбор сообщений о долгах: DeepSeek (основной путь) + офлайн-эвристики (фолбэк).

Результат разбора — ParsedMessage с намерением (intent):
    debt          — сообщение о долге: from / to / currency / amount
    repayment     — возврат долга: «Леша вернул Диме 3 рубля» (from вернул to)
    expense       — общий счёт: «Дима заплатил 10 за всех» (делим на участников,
                    participants — за кого платили, exclude — кого исключить)
    debts         — просьба показать долги
    set_currency  — установить валюту по умолчанию
    help          — вопрос про возможности бота
    none          — не поняли (note — что именно)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import requests

from members import format_roster

INTENTS = ("debt", "repayment", "expense", "debts", "set_currency", "help", "none")

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
# Имя: обычное слово от двух букв или местоимение «я» («я должен Диме 3» — долг на автора).
NAME_TOKEN = r"(?:[A-Za-zА-Яа-яЁё][\w\-]{1,29}|[Яя])"
NAME_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё][\w\-]{1,29}")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d{1,2})?")
DEBT_RE = re.compile(
    rf"(?P<debtor>{NAME_TOKEN})\s+{DEBT_VERBS}\s+(?:у\s+|от\s+)?"
    rf"(?P<creditor>{NAME_TOKEN})\s*(?:—|-|:)?\s*"
    rf"(?P<amount>\d+(?:[.,]\d{{1,2}})?)?(?P<tail>[^\n]*)",
    re.IGNORECASE,
)

# Возврат долга: «Леша вернул Диме 3 рубля», «Маша отдала Пете 10$», «рассчитался с Димой на 5»
REPAYMENT_VERBS = (
    r"(?:вернул[аи]?|отдал[аи]?|возвратил[аи]?|возместил[аи]?|погасил[аи]?|"
    r"рассчитал(?:ся|ась|ись)?|returned|repaid|paid\s+back)"
)
REPAYMENT_RE = re.compile(
    rf"(?P<payer>{NAME_TOKEN})\s+{REPAYMENT_VERBS}\s+"
    rf"(?:мне\s+|долг\s+)?(?:у\s+|от\s+|для\s+|с\s+)?"
    rf"(?P<payee>{NAME_TOKEN})\s*(?:—|-|:)?\s*"
    rf"(?P<amount>\d+(?:[.,]\d{{1,2}})?)?(?P<tail>[^\n]*)",
    re.IGNORECASE,
)

DEBTS_KEYWORDS = ("долг", "долги", "сколько", "кто кому", "баланс", "расчёт", "расчет", "сальдо")
CURRENCY_KEYWORDS = ("валют", "currency", "по умолчанию")
HELP_KEYWORDS = ("помощь", "help", "что ты умеешь", "как пользоваться", "команды")

# Общий счёт: «Дима заплатил 10 за всех», «я заплатил 10 за всех кроме Оли», «Маша оплатила ужин».
EXPENSE_VERBS = (
    r"(?:заплатил[аи]?|оплатил[аи]?|оплач[уи]?|потратил[аи]?|скинул(?:ся|ась|ись)|"
    r"скинулись|скидыва(?:лся|лась|лись)|собрал[аи]?\s+деньги|проставил(?:ся|ась)|"
    r"paid\s+for|paid|cashed\s+out)"
)
EXPENSE_RE = re.compile(rf"(?P<payer>{NAME_TOKEN})\s+{EXPENSE_VERBS}\b(?P<tail>[^\n]*)", re.IGNORECASE)
# «за всех», «на всех», «поровну» — платили за весь чат.
ALL_RE = re.compile(r"\b(?:за|на)\s+(?:всех|все|всю|нас|всём|всем)\b|\bпоровну\b", re.IGNORECASE)
# «за себя» — платил только за себя, делить не с кем.
SELF_ONLY_RE = re.compile(r"\b(?:за|на)\s+себя\b", re.IGNORECASE)
# «за Машу и Петю» — платили за конкретных людей.
FOR_SOMEONE_RE = re.compile(r"\b(?:за|на)\s+(?P<names>[^\n]*)", re.IGNORECASE)
# «кроме Оли», «без Пети», «кроме себя» — кого не включать в общий счёт.
EXCLUDE_RE = re.compile(
    r"(?:кроме|без|исключая|не\s+считая|за\s+исключением)\s+(?P<names>[^\n]*)",
    re.IGNORECASE,
)


class DeepSeekError(RuntimeError):
    """Ошибка обращения к DeepSeek."""


@dataclass
class ParsedMessage:
    """Результат разбора сообщения."""

    intent: str = "none"
    from_name: str | None = None
    to_name: str | None = None
    from_user_id: int | None = None      # Telegram user id должника/плательщика (если узнан)
    to_user_id: int | None = None        # Telegram user id кредитора/получателя
    currency: str | None = None
    amount: float | None = None
    note: str | None = None
    source: str = "ai"  # ai | heuristic | fallback
    participants: list[str] | None = None          # за кого заплатили (None — за всех)
    exclude: list[str] = field(default_factory=list)   # кого не включать в общий счёт

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

    @property
    def is_repayment(self) -> bool:
        """Готов ли результат к сохранению как возврат долга."""
        return (
            self.intent == "repayment"
            and bool(self.from_name)
            and bool(self.to_name)
            and isinstance(self.amount, (int, float))
            and float(self.amount) > 0
        )

    @property
    def is_expense(self) -> bool:
        """Готов ли результат к записи как общий счёт («заплатил за всех»)."""
        return (
            self.intent == "expense"
            and bool(self.from_name or self.from_user_id)
            and isinstance(self.amount, (int, float))
            and float(self.amount) > 0
        )


SYSTEM_PROMPT = """Ты — разборщик сообщений о долгах для телеграм-бота (русский и английский).
Верни ТОЛЬКО JSON без пояснений:
{"intent":"debt|repayment|expense|debts|set_currency|help|none","from":"имя","to":"имя","from_user_id":123,"to_user_id":456,"currency":"BYN","amount":3.0,"participants":["Маша"],"exclude":["Оля"],"note":"короткое пояснение"}

Правила:
1. intent=debt — кто-то кому-то должен: «Леша должен Диме 3 рубля», «Маша заняла у Пети 10$».
   from — должник (кто должен), to — кредитор (кому должны), amount — число с точкой.
2. intent=repayment — долг возвращают: «Леша вернул Диме 3 рубля», «Маша отдала Пете 10$»,
   «рассчитался с Димой на 5». from — кто вернул, to — кому вернул, amount — сумма возврата.
3. Валюту приводи к коду ISO: рубль/руб/бр = BYN, доллар/$/бакс = USD, евро/€ = EUR,
   российский рубль/₽ = RUB, злотый = PLN, гривна = UAH, тенге = KZT, фунт = GBP.
   Если валюта не названа — поле currency не заполняй.
4. intent=debts — просят показать или посчитать долги («покажи долги», «сколько я должен»).
5. intent=set_currency — просят задать валюту по умолчанию («валюта по умолчанию доллар»).
6. intent=help — спрашивают, что умеет бот.
7. intent=none — всё остальное (в note коротко почему).
8. Имена приводи к именительному падежу (кто?): «Диме»/«Диму» → «Дима», «Леше» → «Леша»,
   «Пете» → «Петя». Пиши только само имя, без лишних слов.
9. Тип операции определяй сам по смыслу сообщения: «должен», «занял», «одолжил» — это debt;
   «вернул», «отдал», «погасил», «рассчитался», «закрыл долг» — это repayment. Не путай их:
   возврат уменьшает долг, а не создаёт новый.
10. Если ниже дан список участников чата, сопоставь людей из сообщения с ним: «Лешак» может
    оказаться «Леша Козлов» (@kozlovAlex). Когда совпадение уверенное — заполни from_user_id
    и to_user_id идентификаторами из списка. Сомневаешься — оставь id пустыми.
11. «я», «мне», «меня», «мой» — это автор сообщения (он указан в списке): бери его имя и id.
12. intent=expense — один человек платит за всех, и сумму делят поровну: «Дима заплатил 10 за всех»,
    «я заплатил 10», «Маша оплатила ужин 30 рублей», «Петя скинулся на такси 20 евро».
    from — кто заплатил («я» — автор сообщения), amount — вся уплаченная сумма, currency — как обычно.
    Кого включать в делёж:
    * «за всех», «на всех», «поровну» или про людей вообще не сказано — поле participants не заполняй;
    * «кроме Оли», «без Пети», «кроме себя» — имена в exclude (для «себя» пиши «я»);
    * названы конкретные люди («оплатил 10 за Машу и Петю») — перечисли их в participants;
    * «заплатил за себя» — participants: ["я"].
13. Не путай expense и repayment: «заплатил 10 за всех», «скинулись на подарок» — это expense
    (трата делится между участниками); «заплатил/вернул долг Диме 3» — это repayment.
14. Пометка «не зарегистрирован» в списке участников — не повод отказываться от разбора:
    верни имя и id как обычно, бот сам попросит человека зарегистрироваться."""


def _to_user_id(value: Any) -> int | None:
    """Приводит id участника из ответа модели к int (None — если пусто или не число)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


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

    def parse(self, text: str, default_currency: str = "BYN", *,
              members: Sequence[Any] = (), author: Any = None) -> ParsedMessage:
        """Разбирает текст: сначала DeepSeek, при сбое — офлайн-эвристики.

        `members` и `author` — состав чата и автор сообщения: по ним модель понимает,
        что «Лешак» из сообщения — это Леша Козлов (@kozlovAlex, id=…), и возвращает
        from_user_id / to_user_id, чтобы учёт шёл по пользователям, а не по строкам имён.
        """
        prompt = (
            f"Валюта по умолчанию: {default_currency}.\n"
            f"Сообщение пользователя: {text}"
        )
        roster = format_roster(list(members or []), author)
        error_note: str | None = None
        parsed: ParsedMessage | None = None
        try:
            parsed = self._to_message(self._complete(prompt, roster=roster))
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

    def _complete(self, user_prompt: str, roster: str = "") -> str:
        """Запрос к DeepSeek в JSON-режиме (roster — состав чата для сопоставления имён)."""
        if not self._api_key:
            raise DeepSeekError("Не задан DEEPSEEK_API_KEY.")
        system = SYSTEM_PROMPT if not roster else f"{SYSTEM_PROMPT}\n\n{roster}"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
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
            from_user_id=_to_user_id(data.get("from_user_id")),
            to_user_id=_to_user_id(data.get("to_user_id")),
            currency=_to_currency(data.get("currency")),
            amount=_to_amount(data.get("amount")),
            note=_to_name(data.get("note")),
            participants=_to_name_list(data.get("participants")),
            exclude=_to_name_list(data.get("exclude")) or [],
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


def _to_name_list(value: Any) -> list[str] | None:
    """Список имён из ответа модели: 'Маша, Петя' или ['Маша'] -> ['Маша', 'Петя'].

    Пустой результат — None: это значит «не указано», а не «никого».
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = re.split(r"[,;]|\s+и\s+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return None
    cleaned = [name for name in (_to_name(part) for part in parts) if name]
    return cleaned or None


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


def _parse_repayment(raw: str, default_currency: str = "BYN") -> ParsedMessage | None:
    """Пытается распознать возврат долга: «Леша вернул Диме 3 рубля».

    Вызывается раньше проверки на «долг/долги», иначе фраза «Леша вернул долг Диме 3»
    была бы принята за просьбу показать отчёт.
    """
    match = REPAYMENT_RE.search(raw)
    if not match:
        return None
    payer, payee = match.group("payer"), match.group("payee")
    if not payer or not payee:
        return None
    if detect_currency(payer) is not None or detect_currency(payee) is not None:
        return None                      # «вернул рублями»: слово похоже на имя, но это валюта
    tail = match.group("tail") or ""
    amount = _to_amount(match.group("amount"))
    if amount is None:
        numbers = NUMBER_RE.findall(tail[:60])
        amount = _to_amount(numbers[0]) if numbers else None
    if not amount:
        return None
    return ParsedMessage(
        intent="repayment",
        from_name=_to_name(payer),
        to_name=_to_name(payee),
        currency=detect_currency(tail) or detect_currency(raw) or default_currency,
        amount=amount,
        source="heuristic",
    )


def _parse_expense(raw: str, default_currency: str = "BYN") -> ParsedMessage | None:
    """Пытается распознать общий счёт: «Дима заплатил 10 за всех кроме Оли».

    Возвращает None, если фразы про оплату нет или не нашлась сумма. Кого делить —
    разбирается так: «за всех» (или вообще ничего не сказано) — на весь чат,
    «кроме Оли» — исключение, «за Машу и Петю» — платили за конкретных людей.
    """
    match = EXPENSE_RE.search(raw)
    if not match:
        return None
    payer = match.group("payer")
    if detect_currency(payer) is not None:
        return None                     # «рублями заплатил»: слово похоже на имя, но это валюта
    tail = match.group("tail") or ""
    numbers = NUMBER_RE.findall(tail[:80]) or NUMBER_RE.findall(raw)
    amount = _to_amount(numbers[0]) if numbers else None
    if not amount:
        return None

    exclude_match = EXCLUDE_RE.search(raw)
    exclude = _segment_names(exclude_match.group("names")) if exclude_match else []

    participants: list[str] | None = None
    if SELF_ONLY_RE.search(raw):
        participants = ["я"]
    elif not ALL_RE.search(raw):
        someone = FOR_SOMEONE_RE.search(raw)
        # Именем считаем только слово с большой буквы: «за ужин» — это не человек,
        # а «за Машу» — человек (в офлайне без ИИ иначе не отличить).
        names = _segment_names(someone.group("names")) if someone else []
        proper = [name for name in names if name[:1].isupper()]
        participants = proper or None

    return ParsedMessage(
        intent="expense",
        from_name=_to_name(payer),
        currency=detect_currency(tail) or detect_currency(raw) or default_currency,
        amount=amount,
        participants=participants,
        exclude=exclude,
        source="heuristic",
    )


def heuristic_parse(text: str, default_currency: str = "BYN") -> ParsedMessage | None:
    """Разбор сообщения без внешних сервисов (регулярные выражения).

    Понимает «Леша должен Диме 3 рубля», «3 рубля: Леша должен Диме», «Леша вернул Диме 3»,
    «Дима заплатил 10 за всех кроме Оли», «покажи долги», «валюта по умолчанию доллар».
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

    # Общий счёт («Дима заплатил 10 за всех») проверяем раньше возврата и отчёта:
    # в таких фразах тоже встречается слово «долг», но это не просьба показать долги.
    expense = _parse_expense(raw, default_currency)
    if expense is not None:
        return expense

    # Возврат проверяем до «долг/долги»: «вернул долг Диме 3» — это не отчёт.
    repayment = _parse_repayment(raw, default_currency)
    if repayment is not None:
        return repayment

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


