# -*- coding: utf-8 -*-
"""Логика долгов: нормализация имён, взаимозачёт, итоги и тексты ответов бота."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from members import label_for, member_by_id
from storage import ChatMember, Debt

MAX_ROWS_IN_HISTORY = 15
# Подсказка в конце ответа о записи: итоги в ответе не выводим (чтобы не засорять чат),
# но всегда понятно, куда смотреть. Меняется в одном месте.
SETTLE_HINT = "Итог: /settle"

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


def identity_of(user_id: int | None, name: str) -> str:
    """Ключ человека: user id, если он известен, иначе имя в нормальном виде.

    Именно поэтому «Лешак» и «Леша» в одном чате — один и тот же человек: оба сообщения
    ссылаются на одного участника (@kozlovAlex), и учёт идёт по этому ключу, а не по строке.
    """
    if user_id:
        return f"id:{int(user_id)}"
    key = name_key(name)
    return f"name:{key}" if key else ""


def person_labels(debts: Sequence[Debt], members: Sequence[ChatMember] = ()) -> dict[str, str]:
    """Ключ человека -> подпись: «Леша Козлов (@kozlovAlex)» или просто «Леша»."""
    labels: dict[str, str] = {}
    for debt in debts:
        sides = ((debt.from_user_id, debt.from_name), (debt.to_user_id, debt.to_name))
        for user_id, name in sides:
            key = identity_of(user_id, name)
            if not key:
                continue
            member = member_by_id(user_id, members)
            if member is not None:
                labels[key] = member.label          # участник узнан — показываем ник
                continue
            display = normalize_name(name)
            current = labels.get(key)
            if display and (current is None or _case_rank(display) < _case_rank(current)):
                labels[key] = display
    return labels


def _label(labels: Mapping[str, str], key: str) -> str:
    """Подпись человека по ключу (если её почему-то нет — сам ключ без префикса)."""
    return labels.get(key) or key.split(":", 1)[-1].capitalize()


def split_amount(amount: float, people: int) -> list[float]:
    """Делит сумму на равные доли без потери копеек: 10 на 3 → [3.34, 3.33, 3.33].

    Считаем в копейках, поэтому сумма долей всегда равна исходной сумме:
    «лишние» копейки достаются первым участникам списка.
    """
    if people <= 0:
        return []
    cents = int(round(float(amount) * 100))
    base, rest = divmod(cents, people)
    return [round((base + (1 if index < rest else 0)) / 100, 2) for index in range(people)]


def net_balances(debts: Sequence[Debt], members: Sequence[ChatMember] = ()) -> list[Balance]:
    """Сальдо по парам с взаимозачётом: (A→B 10) + (B→A 4) = A→B 6."""
    labels = person_labels(debts, members)
    pairs: dict[tuple[str, str, str], float] = defaultdict(float)
    for debt in debts:
        debtor = identity_of(debt.from_user_id, debt.from_name)
        creditor = identity_of(debt.to_user_id, debt.to_name)
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
            balances.append(Balance(_label(labels, debtor_key), _label(labels, creditor_key),
                                    currency, net))
        elif net < 0:
            balances.append(Balance(_label(labels, creditor_key), _label(labels, debtor_key),
                                    currency, abs(net)))
    balances.sort(key=lambda item: (-item.amount, item.currency, item.debtor))
    return balances


def minimal_transfers(debts: Sequence[Debt], members: Sequence[ChatMember] = ()) -> list[Balance]:
    """Взаимозачёт по всему чату: минимальный набор переводов, чтобы всё закрылось.

    Сначала считаем сальдо каждого человека по валюте (сколько он должен минус сколько
    должны ему), затем «жадно» сводим крупнейшего должника с крупнейшим кредитором.
    Благодаря этому A→B 10 и B→C 10 превращаются в один перевод A→C 10: вместо цепочки
    платежей получается несколько, а суммы совпадают с парным взаимозачётом.
    """
    labels = person_labels(debts, members)
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for debt in debts:
        debtor = identity_of(debt.from_user_id, debt.from_name)
        creditor = identity_of(debt.to_user_id, debt.to_name)
        if not debtor or not creditor or debtor == creditor:
            continue
        currency = (debt.currency or "BYN").upper()
        sign = -1.0 if debt.is_repayment else 1.0      # возврат уменьшает долг
        totals[(debtor, currency)] -= sign * float(debt.amount)
        totals[(creditor, currency)] += sign * float(debt.amount)

    result: list[Balance] = []
    for currency in sorted({currency for _, currency in totals}):
        owing = sorted(
            ([-amount, key] for (key, code), amount in totals.items()
             if code == currency and amount < -0.005),
            reverse=True,
        )
        owed = sorted(
            ([amount, key] for (key, code), amount in totals.items()
             if code == currency and amount > 0.005),
            reverse=True,
        )
        debtor_index = creditor_index = 0
        while debtor_index < len(owing) and creditor_index < len(owed):
            debtor_amount, debtor_key = owing[debtor_index]
            creditor_amount, creditor_key = owed[creditor_index]
            amount = round(min(debtor_amount, creditor_amount), 2)
            if amount > 0:
                result.append(Balance(_label(labels, debtor_key), _label(labels, creditor_key),
                                      currency, amount))
            owing[debtor_index][0] = round(debtor_amount - amount, 2)
            owed[creditor_index][0] = round(creditor_amount - amount, 2)
            if owing[debtor_index][0] <= 0.005:
                debtor_index += 1
            if owed[creditor_index][0] <= 0.005:
                creditor_index += 1
    result.sort(key=lambda item: (-item.amount, item.currency, item.debtor))
    return result


def totals_by_person(
    debts: Sequence[Debt], members: Sequence[ChatMember] = (),
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Итоги по людям: (сколько каждый должен, сколько должны каждому) по валютам."""
    labels = person_labels(debts, members)
    owes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    owed: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for debt in debts:
        debtor = _label(labels, identity_of(debt.from_user_id, debt.from_name))
        creditor = _label(labels, identity_of(debt.to_user_id, debt.to_name))
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


def format_debt_saved(debt: Debt, members: Sequence[ChatMember] = ()) -> str:
    """Ответ на успешно записанный долг: сразу видно, каких участников узнали.

    Итоги в ответе не выводим — только короткая подсказка SETTLE_HINT в конце.
    """
    return "\n".join([
        "✅ Записал долг:",
        f"• Кто должен: {label_for(debt.from_user_id, debt.from_name, members)}",
        f"• Кому: {label_for(debt.to_user_id, debt.to_name, members)}",
        f"• Сумма: {debt.amount:.2f} {debt.currency}",
        SETTLE_HINT,
    ])


def format_repayment_saved(debt: Debt, members: Sequence[ChatMember] = ()) -> str:
    """Ответ на записанный возврат долга.

    Итоги в ответе не выводим — только короткая подсказка SETTLE_HINT в конце.
    """
    return "\n".join([
        "↩️ Записал возврат долга:",
        f"• Кто вернул: {label_for(debt.from_user_id, debt.from_name, members)}",
        f"• Кому вернул: {label_for(debt.to_user_id, debt.to_name, members)}",
        f"• Сумма: {debt.amount:.2f} {debt.currency}",
        SETTLE_HINT,
    ])


@dataclass(frozen=True)
class ExpenseSummary:
    """Что записали по общему счёту — для человеческого ответа в чат."""

    payer: ChatMember
    currency: str
    amount: float
    share: float
    people: int                                        # на сколько человек разделили
    debtors: list[tuple[ChatMember, float]]            # (участник, его доля)
    excluded: list[ChatMember] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # не участвуют (не зарегистрированы)
    raw_text: str = ""


def format_expense_saved(summary: ExpenseSummary) -> str:
    """Ответ на записанный общий счёт: кто платил, на кого делили и сколько с каждого.

    Итоги в ответе не выводим — только короткая подсказка SETTLE_HINT в конце.
    """
    lines = [
        "🧾 Записал общий счёт:",
        f"• Заплатил: {summary.payer.label}",
        f"• Сумма: {summary.amount:.2f} {summary.currency} — делю на {summary.people} чел.",
        f"• С каждого: {summary.share:.2f} {summary.currency}",
    ]
    if summary.debtors:
        shown = ", ".join(
            f"{member.label} — {amount:.2f} {summary.currency}"
            for member, amount in summary.debtors[:MAX_ROWS_IN_HISTORY]
        )
        lines.append(f"• Кто скидывается: {shown}")
    else:
        lines.append("• Делить не с кем — все остальные исключены.")
    if summary.excluded:
        lines.append("• Исключены: " + ", ".join(member.label for member in summary.excluded))
    if summary.skipped:
        lines.append("• Не участвуют (не зарегистрированы): " + ", ".join(summary.skipped))
    if summary.raw_text:
        lines.append(f"• Оригинал сохранён: «{summary.raw_text}»")
    lines.append(SETTLE_HINT)
    return "\n".join(lines)


def format_registered(member: ChatMember, added: Sequence[str] = ()) -> str:
    """Ответ на /reg: кого зарегистрировали и по каким именам его теперь узнают."""
    lines = [f"✅ Зарегистрировал: {member.label}"]
    if member.aliases:
        lines.append("• Узнаю по именам: " + ", ".join(member.aliases))
    else:
        lines.append("• Узнаю по имени и @нику из Telegram.")
        lines.append("• Добавить другие имена: /reg Лёха, Лешак, кличка")
    if added:
        lines.append("• Добавлено сейчас: " + ", ".join(added))
    lines.append("• Записи с этими именами теперь попадут на него, а не на строку текста.")
    lines.append("Кто уже зарегистрирован: /who")
    return "\n".join(lines)


def format_members_report(members: Sequence[ChatMember]) -> str:
    """Ответ на /who: состав чата и отметка регистрации."""
    if not members:
        return (
            "👥 Пока никого не знаю. Пусть каждый напишет пару слов в чат — и я запомню, "
            "кто есть кто.\nЗатем зарегистрируйтесь: /reg Имя, кличка, как ещё вас зовут."
        )
    registered = [member for member in members if member.is_registered]
    lines = [
        f"👥 Кто есть кто в чате (зарегистрированы: {len(registered)} из {len(members)}).",
        "Долги, возвраты и общие счета записываю только на зарегистрированных:",
    ]
    for member in sorted(members, key=lambda item: (not item.is_registered, item.label.lower())):
        mark = "✅" if member.is_registered else "⬜"
        names = f" — имена: {', '.join(member.aliases)}" if member.aliases else ""
        lines.append(f"{mark} {member.label}{names}")
    lines.append("")
    lines.append("Зарегистрировать себя: /reg Женя, ЖеняШ, как вас ещё зовут")
    lines.append("Зарегистрировать другого: /reg @его_ник Имя, кличка")
    return "\n".join(lines)


def _row_line(debt: Debt, labels: Mapping[str, str]) -> str:
    """Строка записи для отчёта: «Леша Козлов → Дмитрий Болт: 3.00 BYN»."""
    left = _label(labels, identity_of(debt.from_user_id, debt.from_name))
    right = _label(labels, identity_of(debt.to_user_id, debt.to_name))
    if debt.is_repayment:
        return f"↩️ {left} вернул {right}: {debt.amount:.2f} {debt.currency}"
    if debt.is_expense:
        return f"🧾 {left} → {right}: {debt.amount:.2f} {debt.currency} (доля общего счёта)"
    return f"{left} → {right}: {debt.amount:.2f} {debt.currency}"


def _expense_lines(debts: Sequence[Debt], labels: Mapping[str, str]) -> list[str]:
    """Блок «Общие счета»: один оплаченный счёт — строка с оригиналом сообщения.

    Доли одного платежа объединяются по group_id (а если он не сохранился —
    по тексту сообщения и плательщику).
    """
    groups: dict[tuple[str, str], list[Debt]] = {}
    for debt in debts:
        creditor = identity_of(debt.to_user_id, debt.to_name)
        group_key = (debt.group_id or "", creditor)
        groups.setdefault(group_key, []).append(debt)

    lines: list[str] = []
    for group in groups.values():
        first = group[0]
        payer = _label(labels, identity_of(first.to_user_id, first.to_name))
        share = first.amount
        currency = first.currency
        text = (first.raw_text or "").strip()
        title = f"«{text}»" if text else f"{payer} оплатил"
        lines.append(
            f"• 🧾 {title} — платил {payer}, доля {share:.2f} {currency}, "
            f"должников {len(group)}"
        )
    return lines


def format_debts_report(debts: Sequence[Debt], default_currency: str = "BYN",
                        members: Sequence[ChatMember] = ()) -> str:
    """Отчёт по долгам: сальдо по парам, итоги по людям и сами записи."""
    if not debts:
        return (
            "📭 Долгов нет. Напишите, например: «Леша должен Диме 3 рубля» "
            f"(валюта по умолчанию: {default_currency})."
        )

    labels = person_labels(debts, members)
    balances = net_balances(debts, members)
    transfers = minimal_transfers(debts, members)
    owes, owed = totals_by_person(debts, members)
    debts_only = [debt for debt in debts if not debt.is_repayment]
    repayments = [debt for debt in debts if debt.is_repayment]
    expenses = [debt for debt in debts if debt.is_expense]
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

    # Взаимозачёт по всему чату: если он короче парного, показываем отдельно — это
    # готовый список переводов («Леша переводит Диме»), а не сальдо по каждой паре.
    if transfers and [item.pretty() for item in transfers] != [item.pretty() for item in balances]:
        lines.append("")
        lines.append("Минимум переводов, чтобы всё закрылось:")
        lines.extend(f"• {transfer.pretty()}" for transfer in transfers)

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

    if expenses:
        lines.append("")
        lines.append("Общие счета (🧾 сумма счёта делится между участниками):")
        lines.extend(_expense_lines(expenses, labels))

    if repayments:
        lines.append("")
        lines.append("Возвраты (учтены в зачёте):")
        lines.extend(f"• {_row_line(debt, labels)}" for debt in repayments[:MAX_ROWS_IN_HISTORY])

    if debts_only:
        lines.append("")
        lines.append(f"Сумма долгов без взаимозачёта: {_money_by_currency(dict(total_by_currency))}")
    if returned_by_currency:
        lines.append(f"Возвратов записано: {_money_by_currency(dict(returned_by_currency))}")

    if len(debts) <= MAX_ROWS_IN_HISTORY:
        lines.append("")
        lines.append("Все записи:")
        lines.extend(f"• {_row_line(debt, labels)}" for debt in debts)
    return "\n".join(lines)


def format_currency_set(currency: str) -> str:
    """Ответ на смену валюты по умолчанию."""
    return (
        f"💱 Валюта по умолчанию: {currency.upper()}.\n"
        "Если в сообщении валюта не названа — буду использовать её."
    )


def format_transfers(transfers: Sequence[Balance], target: str = "") -> str:
    """Ответ /settle: только список переводов, которые закрывают все долги."""
    title = "Минимум переводов, чтобы всё закрылось"
    if target:
        title += f" (валюта чата: {target.upper()})"
    if not transfers:
        return f"🎉 {title}: никто ничего не должен."
    return "\n".join([f"🧮 {title}:",
                      *(f"• {transfer.pretty()}" for transfer in transfers),
                      "",
                      "Перевели — и записи можно свести зачётом: /debts"])


def format_help(default_currency: str = "BYN") -> str:
    """Справка по возможностям бота."""
    return "\n".join([
        "🤖 Калькулятор долгов. Что умею:",
        "",
        "0. Зарегистрировать участников — без этого записи не ведутся:",
        "   /reg Женя, ЖеняШ, жена, шаман — как вас ещё зовут в чате",
        "   /reg @Genia Женя, ЖеняШ, жекич — зарегистрировать другого участника",
        "   /who — кто уже есть в чате и кто зарегистрирован",
        "",
        "1. Записать долг — просто напишите сообщением:",
        "   «Леша должен Диме 3 рубля» или «Маша заняла у Пети 10$».",
        "   Разбираю через DeepSeek: кто должен, кому, сколько и в какой валюте.",
        "",
        "2. Записать возврат долга (уменьшает сальдо):",
        "   «Леша вернул Диме 3 рубля» или «Маша отдала Пете 10$».",
        "",
        "3. Общий счёт — делю сумму между участниками поровну:",
        "   «Дима заплатил 10 за всех», «я заплатил 10»,",
        "   «Маша оплатила ужин 30 рублей за всех кроме Оли»,",
        "   «Петя скинулся на такси 20 кроме себя».",
        "   Оригинал сообщения сохраняю в записи, а /undo убирает весь счёт целиком.",
        "",
        "4. Показать и посчитать долги (с взаимозачётом):",
        "   /debts или «покажи долги»",
        "",
        "5. Взаимозачёт: кто кому сколько переводит, чтобы всё закрылось:",
        "   /settle — минимум переводов (если A→B и B→C, то A платит C)",
        "",
        "6. Привести всё к валюте чата по курсу на дату записи:",
        "   /d — все долги в одной валюте по курсу того дня, когда их записали",
        "   /rates — курсы валют (бел. и рос. рубли, доллар, евро, юань, бат)",
        "",
        "7. Задать валюту по умолчанию:",
        f"   /currency BYN или «валюта по умолчанию доллар» (сейчас: {default_currency})",
        "",
        "8. Удалить последнюю запись: /undo",
        "9. Удалить все записи этого чата: /reset",
        "",
        "Если чат защищён паролем, пришлите его один раз: /password ваш-пароль.",
        "Данные хранятся в Supabase, отдельно по каждому чату.",
    ])
