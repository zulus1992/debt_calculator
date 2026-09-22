# -*- coding: utf-8 -*-
"""Курсы валют: загрузка из ExchangeRate-API, хранение в базе и конвертация сумм.

API: https://www.exchangerate-api.com/docs (ключ бесплатно — app.exchangerate-api.com).
Один запрос отдаёт все валюты сразу, ключ передаётся в адресе:

    GET https://v6.exchangerate-api.com/v6/<RATES_API_KEY>/latest/BYN
        → {"result": "success", "base_code": "BYN", "time_last_update_utc": "...",
           "conversion_rates": {"BYN": 1, "USD": 0.3077, "EUR": 0.2841, ...}}
    GET https://open.er-api.com/v6/latest/BYN          (без ключа, открытый эндпоинт)
        → то же, но поле называется "rates" (нужна ссылка на exchangerate-api.com)

Ошибки приходят кодом: {"result": "error", "error-type": "invalid-key|quota-reached|..."}.

Курсы в ответе обратные (`conversion_rates[X]` = сколько X за 1 base), поэтому мы их
переворачиваем и храним «в базовой валюте»: при base = BYN значение rate = 3.25 для USD
означает «1 USD = 3.25 BYN». Так их удобно и показывать, и пересчитывать: сумма в базовой
валюте равна amount × rate[валюта], а в любой другой — делится на её курс.

Курсы обновляются не чаще раза в день и без cron: перед запросом к API проверяем, нет ли
уже курсов на сегодняшнюю дату в базе (вручную — `python bot.py --rates --force`).
Истории за прошлые дни в бесплатном тарифе нет: база копит по одному снимку в сутки,
а для дат без снимка берётся ближайший сохранённый курс.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

import requests

from storage import Debt, RatePoint, Storage

# Как показывать валюты человеку.
CURRENCY_TITLES: dict[str, str] = {
    "BYN": "белорусский рубль",
    "RUB": "российский рубль",
    "USD": "доллар США",
    "EUR": "евро",
    "CNY": "китайский юань",
    "THB": "тайский бат",
    "PLN": "польский злотый",
    "UAH": "украинская гривна",
    "KZT": "казахстанский тенге",
    "GBP": "фунт стерлингов",
}
# Насколько назад смотреть курсы, если на дату записи их нет (выходные и праздники).
LOOKBACK_DAYS = 7
# Откуда берём курсы: exchangerate-api.com. Ключ бесплатный — app.exchangerate-api.com,
# без ключа работает открытый эндпоинт (лимит запросов и обязательна ссылка на сервис).
SOURCE_TITLE = "exchangerate-api.com"
OPEN_URL = "https://open.er-api.com/v6"
# Понятные расшифровки error-type из ответов ExchangeRate-API.
ERROR_TITLES: dict[str, str] = {
    "unsupported-code": "валюта не поддерживается сервисом",
    "malformed-request": "неверный запрос к API (проверьте RATES_API_URL)",
    "invalid-key": "неверный RATES_API_KEY (ключ из кабинета app.exchangerate-api.com)",
    "inactive-account": "аккаунт не активирован: подтвердите e-mail в кабинете",
    "quota-reached": "исчерпан лимит запросов тарифа — подождите или смените план",
}


class RatesError(RuntimeError):
    """Проблема при получении курсов валют."""


@dataclass(frozen=True)
class RatesUpdate:
    """Итог обновления курсов: что записали и что не получилось."""

    rate_date: str
    saved: int = 0
    currencies: tuple[str, ...] = ()    # какие валюты сохранили
    problems: tuple[str, ...] = ()
    reason: str = ""          # почему не обновляли (курсы уже есть, ошибка API и т.п.)

    @property
    def updated(self) -> bool:
        """Было ли реальное обновление."""
        return self.saved > 0


def currency_title(code: str) -> str:
    """Название валюты для ответов бота: USD → «доллар США»."""
    upper = str(code or "").upper()
    return CURRENCY_TITLES.get(upper, upper or "валюта")


def _to_float(value: Any) -> float | None:
    """Число из ответа API: '3,2531' → 3.2531 (None — если это не положительное число)."""
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def rate_table(points: Sequence[RatePoint]) -> dict[str, dict[str, float]]:
    """Таблица курсов: {дата: {валюта: сколько базовой валюты за 1 единицу валюты}}."""
    table: dict[str, dict[str, float]] = {}
    for point in points:
        day = table.setdefault(point.rate_date, {})
        day[str(point.base or "BYN").upper()] = 1.0
        day[point.currency.upper()] = point.rate
    return table


def rate_for(table: Mapping[str, Mapping[str, float]], day: str,
             currency: str) -> tuple[float | None, str | None]:
    """Курс валюты на дату: точно на эту дату, иначе — ближайший сохранённый.

    Курсы копятся по одному снимку в сутки, поэтому на выходные и на дни до начала сбора
    берём ближайшую дату с этим курсом: сначала предыдущую, если её нет — следующую.
    """
    code = str(currency or "").upper()
    if not code or not table:
        return None, None
    target = str(day or "")
    exact = table.get(target)
    if exact and exact.get(code):
        return float(exact[code]), target
    earlier = [item for item in sorted(table, reverse=True) if item <= target]
    later = [item for item in sorted(table) if item > target]
    for candidate in (*earlier, *later):
        value = table[candidate].get(code)
        if value:
            return float(value), candidate
    return None, None


def convert_amount(amount: float, from_code: str, to_code: str,
                   table: Mapping[str, Mapping[str, float]], day: str,
                   base: str = "BYN") -> tuple[float | None, str | None]:
    """Пересчитывает сумму из одной валюты в другую по курсу на дату.

    Возвращает (сумма, дата использованного курса) или (None, None), если курса нет.
    """
    source = str(from_code or base).upper()
    target = str(to_code or base).upper()
    if source == target:
        return round(float(amount), 2), str(day or "")
    source_rate, source_day = rate_for(table, day, source)
    target_rate, target_day = rate_for(table, day, target)
    if not source_rate or not target_rate:
        return None, None
    value = float(amount) * source_rate / target_rate
    used = [item for item in (source_day, target_day) if item]
    return round(value, 2), (max(used) if used else None)


@dataclass
class ConvertedDebts:
    """Записи, приведённые к валюте чата, и что при этом использовалось."""

    debts: list[Debt]
    target: str
    rates_used: dict[str, dict[str, float]]            # дата курса → {валюта: сколько target за 1}
    skipped: list[str] = field(default_factory=list)   # записи без курса

    @property
    def changed(self) -> bool:
        """Были ли пересчёты (нужно ли показывать шапку с курсами)."""
        return bool(self.rates_used)


def convert_debts(debts: Sequence[Debt], target: str,
                  table: Mapping[str, Mapping[str, float]], base: str = "BYN") -> ConvertedDebts:
    """Приводит все записи к валюте чата по курсу на дату самой записи."""
    converted: list[Debt] = []
    skipped: list[str] = []
    used: dict[str, dict[str, float]] = {}
    to_code = str(target or base).upper()
    for debt in debts:
        day = debt_day(debt)
        value, rate_day = convert_amount(debt.amount, debt.currency, to_code, table, day, base)
        if value is None:
            skipped.append(f"{debt.amount:.2f} {debt.currency}")
            converted.append(debt)                     # без курса — оставляем как есть
            continue
        from_code = str(debt.currency or base).upper()
        if from_code != to_code and rate_day:
            source_rate, _ = rate_for(table, day, from_code)
            target_rate, _ = rate_for(table, day, to_code)
            if source_rate and target_rate:
                used.setdefault(rate_day, {})[from_code] = round(source_rate / target_rate, 6)
        converted.append(replace(debt, amount=value, currency=to_code))
    return ConvertedDebts(debts=converted, target=to_code, rates_used=used, skipped=skipped)


def debt_day(debt: Debt) -> str:
    """Дата записи (день сообщения) в ISO-виде: по ней и берём курс."""
    created = str(debt.created_at or "").strip()
    return created[:10] if len(created) >= 10 else date.today().isoformat()


def history_start(debts: Sequence[Debt]) -> str:
    """С какой даты читать курсы: самая ранняя запись минус неделя (выходные и праздники)."""
    days = [debt_day(debt) for debt in debts]
    earliest = min(days) if days else date.today().isoformat()
    try:
        start = date.fromisoformat(earliest) - timedelta(days=LOOKBACK_DAYS)
    except ValueError:
        start = date.today() - timedelta(days=LOOKBACK_DAYS)
    return start.isoformat()


def format_used_rates(rates_used: Mapping[str, Mapping[str, float]], target: str) -> list[str]:
    """Строки «какой курс взят на какую дату» для шапки отчёта /d."""
    lines: list[str] = []
    for day in sorted(rates_used):
        parts = [
            f"1 {code} = {_pretty_rate(value)} {target}"
            for code, value in sorted(rates_used[day].items())
        ]
        if parts:
            lines.append(f"• {_ru_date(day)}: " + ", ".join(parts))
    return lines


def format_rates_report(points: Sequence[RatePoint], base: str, *,
                        target: str = "", source: str = SOURCE_TITLE) -> str:
    """Ответ на /rates: курсы к базовой валюте, которые лежат в базе."""
    base_code = str(base or "BYN").upper()
    if not points:
        return (
            "💱 Курсов валют пока нет.\n"
            f"Проверьте RATES_API_KEY (ключ с {SOURCE_TITLE}) и повторите: /rates "
            "(или python bot.py --rates)."
        )
    latest = max(point.rate_date for point in points)
    lines = [f"💱 Курсы валют (база {base_code}, {source}, {_ru_date(latest)}):"]
    for point in sorted((item for item in points if item.rate_date == latest),
                        key=lambda item: item.currency):
        lines.append(f"• 1 {point.currency} = {_pretty_rate(point.rate)} {base_code} "
                     f"({currency_title(point.currency)})")
    chat_currency = str(target or base_code).upper()
    if chat_currency != base_code:
        lines.append(f"Валюта чата: {chat_currency}. Привести к ней все долги: /d")
    else:
        lines.append("Валюта чата: " + base_code +
                     ". Нужна другая — /currency USD, потом /d.")
    lines.append("Обновляю курсы раз в день — при первом обращении за сутки.")
    return "\n".join(lines)


def _pretty_rate(value: float) -> str:
    """Курс в читаемом виде: 3.2531, 0.033412, 100.00."""
    number = float(value)
    if number >= 100:
        return f"{number:.2f}"
    if number >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _ru_date(value: str) -> str:
    """ISO-дата в виде «21.09.2026» (как есть, если формат другой)."""
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return str(value or "")


def _get_json(url: str, timeout: float, session: Any = None) -> Any:
    """GET к API курсов с понятными сообщениями об ошибках (ключ передаётся в адресе)."""
    http = session or requests
    headers = {"Accept": "application/json", "User-Agent": "debt-calculator-bot/1.0"}
    requester = getattr(http, "get", None)
    if requester is None:                       # подменённая в тестах сессия без GET
        raise RatesError("HTTP-клиент не умеет GET — проверьте настройки.")
    try:
        response = requester(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise RatesError(f"{SOURCE_TITLE} недоступен: {exc}") from exc
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 404:
        raise RatesError(f"{SOURCE_TITLE}: адрес не найден (HTTP 404): проверьте RATES_API_URL.")
    if status == 429:
        raise RatesError(f"{SOURCE_TITLE}: слишком много запросов (HTTP 429) — "
                         "сработал лимит тарифа, попробуйте позже.")
    if status >= 400:
        text = str(getattr(response, "text", ""))[:150]
        raise RatesError(f"{SOURCE_TITLE} вернул HTTP {status}: {text}")
    try:
        return response.json()
    except (ValueError, AttributeError) as exc:
        raise RatesError(f"{SOURCE_TITLE} вернул не JSON.") from exc


def fetch_latest(base: str, *, api_url: str = "", api_key: str = "", open_url: str = OPEN_URL,
                 timeout: float = 30.0, session: Any = None) -> dict[str, float]:
    """Курсы всех валют к базовой одним запросом: /latest/{base}."""
    url = latest_url(base, api_url=api_url, api_key=api_key, open_url=open_url)
    return parse_rates(_get_json(url, timeout, session), base)


def fetch_points(settings: Any, *, session: Any = None,
                 today: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Точки для записи в базу: по одной на каждую валюту из RATES_CURRENCIES.

    Сервис отдаёт сразу все валюты к базовой, поэтому запрос один; в базу кладём только
    нужные — иначе каждый день сохранялось бы больше сотни строк.
    """
    base = str(settings.rates_base or "BYN").upper()
    day = today or date.today().isoformat()
    rates = fetch_latest(
        base,
        api_url=settings.rates_api_url,
        api_key=settings.rates_api_key,
        open_url=getattr(settings, "rates_open_url", OPEN_URL),
        timeout=settings.request_timeout,
        session=session,
    )
    points: list[dict[str, Any]] = []
    problems: list[str] = []
    for code in settings.rates_currencies:
        currency = str(code or "").upper()
        if not currency or currency == base:
            continue
        value = rates.get(currency)
        if not value:
            problems.append(f"{currency}: {SOURCE_TITLE} не вернул курс")
            continue
        points.append({"rate_date": day, "base": base, "currency": currency,
                       "rate": value, "source": SOURCE_TITLE})
    return points, problems


def update_rates(settings: Any, storage: Storage, *, force: bool = False,
                 today: str | None = None, session: Any = None) -> RatesUpdate:
    """Обновляет курсы в базе: не чаще одного раза в день.

    Cron не нужен: перед обращением к API проверяем, что курсов на сегодня в базе нет.
    Без RATES_API_KEY работает открытый эндпоинт (без ключа, с лимитом запросов).
    """
    day = today or date.today().isoformat()
    base = str(settings.rates_base or "BYN").upper()
    if not force and storage.has_rates(day, base):
        return RatesUpdate(rate_date=day, reason="курсы на сегодня уже сохранены")
    if not str(settings.rates_api_key or "").strip() \
            and not str(getattr(settings, "rates_open_url", "") or "").strip():
        return RatesUpdate(rate_date=day,
                           reason="курсы не настроены (нет RATES_API_KEY и RATES_OPEN_URL)")
    try:
        points, problems = fetch_points(settings, session=session, today=day)
    except RatesError as exc:
        return RatesUpdate(rate_date=day, problems=(str(exc),),
                           reason="курсы получить не удалось")
    if not points:
        return RatesUpdate(rate_date=day, problems=tuple(problems),
                           reason="курсы получить не удалось")
    saved = storage.save_rates(points)
    currencies = tuple(point["currency"] for point in points)
    return RatesUpdate(rate_date=day, saved=saved, currencies=currencies,
                       problems=tuple(problems))


def _error_text(payload: Mapping[str, Any]) -> str:
    """Понятное сообщение по error-type из ответа сервиса."""
    kind = str(payload.get("error-type") or payload.get("error_type") or "").strip().lower()
    return ERROR_TITLES.get(kind, f"ошибка сервиса {SOURCE_TITLE}") + f" (error-type: {kind or '?'})"


def parse_rates(payload: Any, base: str = "") -> dict[str, float]:
    """Курсы из ответа /latest: {валюта: сколько базовой валюты стоит 1 единица}.

    Сервис отдаёт обратные курсы (`conversion_rates[X]` = сколько X за 1 base), поэтому
    переворачиваем: rate[X] = 1 / conversion_rates[X]. Базовая валюта — всегда 1.0.
    Понимает и платный эндпоинт (`conversion_rates`), и открытый (`rates`).
    """
    if not isinstance(payload, Mapping):
        raise RatesError(f"{SOURCE_TITLE} вернул не JSON-объект: {str(payload)[:120]}")
    if str(payload.get("result") or "").strip().lower() == "error":
        raise RatesError(_error_text(payload))
    quoted = payload.get("conversion_rates") or payload.get("rates")
    if not isinstance(quoted, Mapping) or not quoted:
        raise RatesError(f"{SOURCE_TITLE} не вернул курсы: {str(payload)[:120]}")
    base_code = str(payload.get("base_code") or base or "").upper()
    rates: dict[str, float] = {}
    for code, value in quoted.items():
        currency = str(code or "").upper()
        if len(currency) != 3 or not currency.isalpha():
            continue
        number = _to_float(value)
        if not number:
            continue
        rates[currency] = 1.0 if currency == base_code else round(1.0 / number, 8)
    if base_code:
        rates[base_code] = 1.0
    if not rates:
        raise RatesError(f"{SOURCE_TITLE} не вернул ни одной валюты")
    return rates


def latest_url(base: str, *, api_url: str = "", api_key: str = "",
               open_url: str = OPEN_URL) -> str:
    """Адрес запроса курсов: с ключом личного кабинета или открытый (без ключа)."""
    base_code = str(base or "USD").upper()
    key = str(api_key or "").strip()
    if key:
        return f"{str(api_url or '').rstrip('/')}/{key}/latest/{base_code}"
    return f"{str(open_url or OPEN_URL).rstrip('/')}/latest/{base_code}"


