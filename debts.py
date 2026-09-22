# -*- coding: utf-8 -*-
"""Логика долгов: нормализация имён, взаимозачёт, итоги и тексты ответов бота."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from storage import Debt

MAX_ROWS_IN_HISTORY = 15

# Упрощённая морфология русских имён: что срезаем в ключе и что считаем падежом
CASE_ENDINGS = "аеёиоуыэюя"
DATIVE_ENDINGS = "еуюиы"


def _case_rank(name: str) -> int:
    """0 — имя похоже на именительный падеж, 1 — на «кому/кого» (Диме, Диму)."""
    return 1 if name and name[-1].lower() in DATIVE_ENDINGS else 0


@dataclass(frozen=True)
class Balance:
    """Итог по паре людей после взаимозачёта."""

    debtor: str
    creditor: str
    currency: str
    amount: float

    def pretty(self) -> str:
        """Строка вида «Леша → Дима: 3.00 BYN»."""
        return f"{self.debtor} → {self.creditor}: {self.amount:.2f} {self.currency}"


def normalize_name(name: str) -> str:
    """Имя в читаемом виде: «леша» -> «Леша», лишние пробелы убираются."""
    parts = [part for part in str(name or "").replace("_", " ").split() if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)[:40]


def name_key(name: str) -> str:
    """Ключ сравнения имён: «Дима», «Диме», «Диму» → один ключ «дим».

    Упрощённая морфология: убираем одно падежное окончание в конце
    (а/е/и/о/у/ы/ю/я), если в имени остаётся минимум 3 буквы. Это нужно,
    чтобы долги одного человека не распадались на «Дима» и «Диме».
    """
    key = normalize_name(name).lower().replace("ё", "е")
    if len(key) >= 4 and key[-1] in CASE_ENDINGS:
        key = key[:-1]
    return key


def canonical_names(debts: Sequence[Debt]) -> dict[str, str]:
    """Ключ имени -> отображаемое имя (предпочитается именительная форма)."""
    names: dict[str, str] = {}
    for debt in debts:
        for raw in (debt.from_name, debt.to_name):
            display = normalize_name(raw)
            if not display:
                continue
            key = name_key(display)
            current = names.get(key)
            if current is None or (_case_rank(display) < _case_rank(current)):
                names[key] = display
    return names


def net_balances(debts: Sequence[Debt]) -> list[Balance]:
    """Сальдо по парам с взаимозачётом: (A→B 10) + (B→A 4) = A→B 6."""
    display = canonical_names(debts)
    pairs: dict[tuple[str, str, str], float] = defaultdict(float)
    for debt in debts:
        debtor, creditor = name_key(debt.from_name), name_key(debt.to_name)
        if not debtor or not creditor or debtor == creditor:
            continue
        currency = (debt.currency or "BYN").upper()
        sign = -1.0 if debt.is_repayment else 1.0      # возврат уменьшает долг
        pairs[(debtor, creditor, currency)] += sign * float(debt.amount)

    balances: list[Balance] = []
    handled: set[tuple[str, str, str]] = set()
    for (debtor_key, creditor_key, currency), amount in pairs.items():
        forward = (debtor_key, creditor_key, currency)
        backward = (creditor_key, debtor_key, currency)
        if forward in handled or backward in handled:
            continue
        handled.update({forward, backward})

        net = round(amount - pairs.get(backward, 0.0), 2)
        if net > 0:
            balances.append(Balance(display[debtor_key], display[creditor_key], currency, net))
        elif net < 0:
            balances.append(Balance(display[creditor_key], display[debtor_key], currency, abs(net)))
    balances.sort(key=lambda item: (-item.amount, item.currency, item.debtor))
    return balances


def totals_by_person(
    debts: Sequence[Debt],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Итоги по людям: (сколько каждый должен, сколько должны каждому) по валютам."""
    display = canonical_names(debts)
    owes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    owed: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for debt in debts:
        debtor = display.get(name_key(debt.from_name)) or normalize_name(debt.from_name)
        creditor = display.get(name_key(debt.to_name)) or normalize_name(debt.to_name)
        currency = (debt.currency or "BYN").upper()
        sign = -1.0 if debt.is_repayment else 1.0      # возврат уменьшает «должен» и «должны»
        owes[debtor][currency] += sign * float(debt.amount)
        owed[creditor][currency] += sign * float(debt.amount)
    return (
        {name: dict(values) for name, values in owes.items()},
        {name: dict(values) for name, values in owed.items()},
    )


def _money_by_currency(values: dict[str, float]) -> str:
    """«3.00 BYN, 10.00 USD» из словаря валют."""
    return ", ".join(f"{amount:.2f} {code}" for code, amount in sorted(values.items()))


def format_debt_saved(debt: Debt, used_default_currency: bool) -> str:
    """Ответ на успешно записанный долг."""
    lines = [
        "✅ Записал долг:",
        f"• Кто должен: {debt.from_name}",
        f"• Кому: {debt.to_name}",
        f"• Сумма: {debt.amount:.2f} {debt.currency}",
    ]
    if used_default_currency:
        lines.append(f"(валюта не указана — взял по умолчанию: {debt.currency})")
    return "\n".join(lines)


def format_repayment_saved(debt: Debt, used_default_currency: bool) -> str:
    """Ответ на записанный возврат долга."""
    lines = [
        "↩️ Записал возврат долга:",
        f"• Кто вернул: {debt.from_name}",
        f"• Кому вернул: {debt.to_name}",
        f"• Сумма: {debt.amount:.2f} {debt.currency}",
    ]
    if used_default_currency:
        lines.append(f"(валюта не указана — взял по умолчанию: {debt.currency})")
    lines.append("Итог с учётом возврата: /debts")
    return "\n".join(lines)


def format_debts_report(debts: Sequence[Debt], default_currency: str = "BYN") -> str:
    """Отчёт по долгам: сальдо по парам, итоги по людям и сами записи."""
    if not debts:
        return (
            "📭 Долгов нет. Напишите, например: «Леша должен Диме 3 рубля» "
            f"(валюта по умолчанию: {default_currency})."
        )

    balances = net_balances(debts)
    owes, owed = totals_by_person(debts)
    debts_only = [debt for debt in debts if not debt.is_repayment]
    repayments = [debt for debt in debts if debt.is_repayment]
    total_by_currency: dict[str, float] = defaultdict(float)
    returned_by_currency: dict[str, float] = defaultdict(float)
    for debt in debts_only:
        total_by_currency[(debt.currency or default_currency).upper()] += float(debt.amount)
    for debt in repayments:
        returned_by_currency[(debt.currency or default_currency).upper()] += float(debt.amount)

    header = f"📊 Долги (записей: {len(debts)}"
    header += f", из них возвратов: {len(repayments)})" if repayments else ")"
    lines = [header, ""]
    if balances:
        lines.append("Итог с взаимозачётом:")
        lines.extend(f"• {balance.pretty()}" for balance in balances)
    elif repayments and not debts_only:
        lines.append("По этим записям всё закрыто 🎉")
    else:
        lines.append("После взаимозачёта никто ничего не должен 🎉")

    people: list[str] = []
    for name, values in sorted(owes.items()):
        if any(amount > 0 for amount in values.values()):
            people.append(f"• {name} должен: {_money_by_currency(values)}")
    for name, values in sorted(owed.items()):
        if any(amount > 0 for amount in values.values()):
            people.append(f"• {name} должны: {_money_by_currency(values)}")
    if people:
        lines.append("")
        lines.append("Итого по людям (с учётом возвратов):")
        lines.extend(people)

    if repayments:
        lines.append("")
        lines.append("Возвраты (учтены в зачёте):")
        lines.extend(f"• {debt.pretty()}" for debt in repayments[:MAX_ROWS_IN_HISTORY])

    if debts_only:
        lines.append("")
        lines.append(f"Сумма долгов без взаимозачёта: {_money_by_currency(dict(total_by_currency))}")
    if returned_by_currency:
        lines.append(f"Возвратов записано: {_money_by_currency(dict(returned_by_currency))}")

    if len(debts) <= MAX_ROWS_IN_HISTORY:
        lines.append("")
        lines.append("Все записи:")
        lines.extend(f"• {debt.pretty()}" for debt in debts)
    return "\n".join(lines)


def format_currency_set(currency: str) -> str:
    """Ответ на смену валюты по умолчанию."""
    return (
        f"💱 Валюта по умолчанию: {currency.upper()}.\n"
        "Если в сообщении валюта не названа — буду использовать её."
    )


def format_help(default_currency: str = "BYN") -> str:
    """Справка по возможностям бота."""
    return "\n".join([
        "🤖 Калькулятор долгов. Что умею:",
        "",
        "1. Записать долг — просто напишите сообщением:",
        "   «Леша должен Диме 3 рубля» или «Маша заняла у Пети 10$».",
        "   Разбираю через DeepSeek: кто должен, кому, сколько и в какой валюте.",
        "",
        "2. Записать возврат долга (уменьшает сальдо):",
        "   «Леша вернул Диме 3 рубля» или «Маша отдала Пете 10$».",
        "",
        "3. Показать и посчитать долги (с взаимозачётом):",
        "   /debts или «покажи долги»",
        "",
        "4. Задать валюту по умолчанию:",
        f"   /currency BYN или «валюта по умолчанию доллар» (сейчас: {default_currency})",
        "",
        "5. Удалить последнюю запись: /undo",
        "6. Удалить все записи этого чата: /reset",
        "",
        "Данные хранятся в Supabase, отдельно по каждому чату.",
    ])
