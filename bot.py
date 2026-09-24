# -*- coding: utf-8 -*-
"""Телеграм-бот «калькулятор долгов»: DeepSeek разбирает сообщение, Supabase хранит долги.

Режимы:
    python bot.py            # постоянный процесс (long polling), ответы мгновенно — для хостинга
    python bot.py --check    # проверить настройки и доступность сервисов
    python bot.py --demo     # демонстрация без Telegram (в памяти, офлайн-разбор)
    python bot.py --rates    # обновить курсы валют вручную (--rates --force — заново за сегодня)

Курсы валют подтягиваются сами раз в день в 12:00 по Минску (RATES_HOUR): постоянный процесс
проверяет расписание сам, а режим вебхука — при первом апдейте после назначенного часа.

Вебхук (мгновенные ответы на serverless-хостингах — Vercel, PythonAnywhere, WSGI):
    python bot.py --set-webhook https://<домен>/api/telegram   # Telegram шлёт апдейты нам
    python bot.py --webhook-info                               # что сейчас настроено
    python bot.py --delete-webhook                             # вернуться на long polling

Важно: одновременно должен работать только ОДИН режим — Telegram отдаёт апдейты
одному «слушателю», второй получит HTTP 409 Conflict или потеряет сообщения.
Если задан CHAT_PASSWORD, бот просит пароль при добавлении в чат и работает только
в тех чатах, где пароль введён верно.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from config import (
    ConfigError,
    DEFAULT_RATES_HOUR,
    Settings,
    load_settings,
    require_settings,
    webhook_secret_problem,
)
from debts import (
    Balance,
    ExpenseSummary,
    format_currency_set,
    format_debt_saved,
    format_debts_dump,
    format_debts_report,
    format_expense_saved,
    format_help,
    format_members_report,
    format_registered,
    format_repayment_saved,
    format_transfers,
    minimal_transfers,
    normalize_name,
    split_amount,
)
from deepseek import (
    DeepSeekParser,
    ParsedMessage,
    check_api_key,
    detect_currency,
    heuristic_parse,
)
from members import (
    member_from_telegram,
    registered_members,
    resolve_member,
    resolve_side,
    with_aliases,
)
from rates import (
    RatesError,
    convert_debts,
    format_rates_report,
    format_used_rates,
    history_start,
    rate_table,
    update_rates,
    update_rates_scheduled,
)
from storage import ChatMember, InMemoryStorage, Storage, StorageError, SupabaseStorage
from telegram_api import TelegramBot, TelegramError

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
logger = logging.getLogger("debt_bot")

NOT_A_DEBT_REPLY = (
    "🤔 Похоже на долг, но не хватает данных. Пример: «Леша должен Диме 3 рубля»."
)
NOT_A_REPAYMENT_REPLY = (
    "🤔 Похоже на возврат долга, но не хватает данных. Пример: «Леша вернул Диме 3 рубля»."
)
NOT_AN_EXPENSE_REPLY = (
    "🤔 Похоже на общий счёт, но не хватает данных. Пример: «Дима заплатил 10 за всех»."
)
UNKNOWN_REPLY = (
    "🤷 Не понял сообщение.\n"
    "• Записать долг: «Леша должен Диме 3 рубля»\n"
    "• Записать возврат: «Леша вернул Диме 3 рубля»\n"
    "• Общий счёт: «Дима заплатил 10 за всех»\n"
    "• Показать долги: /debts\n"
    "• Справка: /help"
)
DENIED_REPLY = "⛔ Извините, этот бот настроен только для определённых пользователей."
LAST_UPDATE_ID_KEY = "last_update_id"
# Что делать с чатом, куда бота добавили: без пароля (CHAT_PASSWORD) он не работает.
ADDED_REPLY = (
    "👋 Привет! Я калькулятор долгов: «Леша должен Диме 3 рубля», возвраты, общие счета "
    "(«Дима заплатил 10 за всех»), учёт по участникам чата.\n"
    "Справка: /help"
)
PASSWORD_REPLY = (
    "🔐 Чтобы я начал работать в этом чате, пришлите пароль:\n"
    "• /password ваш-пароль\n"
    "• или просто сообщением с паролем — второй раз спрашивать не буду."
)
PASSWORD_OK_REPLY = (
    "✅ Пароль принят — работаю в этом чате.\n"
    "Пишите как обычно: «Леша должен Диме 3 рубля», /reg, /debts, /d, /help."
)
PASSWORD_FAIL_REPLY = (
    "❌ Пароль не подошёл.\n"
    "Пришлите его ещё раз: /password ваш-пароль"
)
PASSWORD_COMMANDS = ("/password", "/pass", "/auth", "/start")
# Как связать имя из сообщения с человеком в чате: без /reg записи не ведутся.
REGISTER_HINT = (
    "Как это исправить:\n"
    "• себя: /reg Женя, ЖеняШ, как вас ещё зовут\n"
    "• другого: /reg @его_ник Гоша, Гоша Петров, кличка\n"
    "После регистрации повторите сообщение — тогда и запишу.\n"
    "Кто уже есть в чате: /who"
)
REG_HANDLE_RE = re.compile(r"^@(?P<handle>\w{3,32})")
REG_ALIAS_SPLIT_RE = re.compile(r"[,;]+")
MAX_SKIPPED_SHOWN = 5
# Сколько последних дней курсов показывать в /rates (и искать для пересчёта).
RATES_HISTORY_DAYS = 30
# Сколько строк TXT-отчёта печатать в консоли (--demo): сам файл целиком не нужен.
DEMO_PREVIEW_LINES = 8
# Команды выгрузки записей файлом TXT: копия строк таблицы debts этого чата.
EXPORT_COMMANDS = ("/export", "/report", "/txt", "/файл")


@dataclass(frozen=True)
class TxtReport:
    """Готовый TXT-отчёт: такой ответ бот отправляет файлом-документом.

    handle_text отвечает обычным текстом (строкой), а отчёт — этим объектом: Telegram
    не умеет отправлять файл сообщением, поэтому DebtBot по типу ответа решает, что
    звать — sendMessage или sendDocument.
    """

    filename: str
    text: str
    caption: str = ""

    def preview(self, limit: int = DEMO_PREVIEW_LINES) -> str:
        """Отчёт там, где файла нет (демо и логи): имя файла и первые строки содержимого."""
        lines = self.text.splitlines()
        head = lines[:limit]
        if len(lines) > limit:
            head.append(f"… (в файле ещё строк: {len(lines) - limit})")
        return "\n".join([f"[файл {self.filename}]", *head])


class HeuristicParser:
    """Разбор только офлайн-эвристиками: демо-режим и тесты без DeepSeek."""

    def parse(self, text: str, default_currency: str = "BYN", *,
              members: Sequence[Any] = (), author: Any = None) -> ParsedMessage:
        """Разбирает текст регулярными выражениями.

        Состав чата здесь не нужен: имена из текста сопоставляются с участниками
        уже в handle_text (resolve_side), поэтому параметры принимаются и игнорируются.
        """
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


def _load_members(storage: Storage, chat_id: int) -> list[ChatMember]:
    """Состав чата из хранилища; ошибка чтения не должна ломать ответ боту."""
    try:
        return list(storage.list_members(chat_id))
    except StorageError as exc:
        logger.warning("Не удалось прочитать участников чата: %s", exc)
        return []


def _member_name(member: ChatMember | None, fallback: str) -> str:
    """Имя для хранения: имя участника, его ник или то, как написали в сообщении."""
    if member is not None:
        if member.display_name.strip():
            return member.display_name.strip()
        if member.username:
            return member.username
    return normalize_name(fallback)


def password_candidate(raw: str) -> str:
    """Что человек прислал как пароль: аргумент /password или всё сообщение целиком."""
    text = str(raw or "").strip()
    command, _, argument = text.partition(" ")
    if command.lower() in PASSWORD_COMMANDS:
        return argument.strip()
    return text


def looks_like_password(raw: str) -> bool:
    """Похоже ли сообщение на попытку ввести пароль: одно слово или «/password <…>»."""
    text = str(raw or "").strip()
    if not text:
        return False
    command, _, argument = text.partition(" ")
    if command.startswith("/"):
        return command.lower() in PASSWORD_COMMANDS and bool(argument.strip())
    return len(text.split()) == 1 and len(text) >= 3


def is_password_attempt(raw: str, password: str) -> bool:
    """Совпадает ли присланное с паролем чата (сравнение постоянного времени).

    Пароль в чате — не про криптографию, но сравнение без «раннего выхода» не даёт
    подбирать его по времени ответа.
    """
    secret = str(password or "").strip()
    candidate = password_candidate(raw)
    if not secret or not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), secret.encode("utf-8"))


def _chat_authorized(storage: Storage, chat_id: int) -> bool:
    """Подтвердил ли чат пароль (сбой чтения трактуем как «нет»)."""
    try:
        return bool(storage.chat_authorized(chat_id))
    except StorageError as exc:
        logger.warning("Не удалось прочитать доступ чата %s: %s", chat_id, exc)
        return False


def added_to_chat_reply(settings: Settings) -> str:
    """Что ответить, когда бота добавили в чат: привет + просьба о пароле, если он задан."""
    if settings.password_required:
        return f"{ADDED_REPLY}\n\n{PASSWORD_REPLY}"
    return ADDED_REPLY


def rates_schedule_text(settings: Settings) -> str:
    """Как курсы обновляются сами: раз в день, час — по Минску (RATES_HOUR)."""
    try:
        hour = int(getattr(settings, "rates_hour", DEFAULT_RATES_HOUR))
    except (TypeError, ValueError):
        hour = DEFAULT_RATES_HOUR
    return f"раз в день в {hour:02d}:00 по Минску"


def rates_report(storage: Storage, settings: Settings, chat_currency: str) -> str:
    """Команда /rates: показать курсы валют (обновив их, если за сегодня их ещё нет)."""
    base = str(settings.rates_base or "BYN").upper()
    try:
        update = update_rates(settings, storage)
        since = (date.today() - timedelta(days=RATES_HISTORY_DAYS)).isoformat()
        points = storage.rates_since(base, since)
    except StorageError as exc:
        return f"⚠️ Проблема с базой данных: {exc}"
    report = format_rates_report(points, base, target=chat_currency,
                                 schedule=rates_schedule_text(settings))
    extras: list[str] = []
    if update.problems:
        extras.append("⚠️ " + "; ".join(update.problems[:3]))
    elif not update.saved and update.reason and "уже сохранены" not in update.reason:
        extras.append(f"ℹ️ {update.reason}.")
    return "\n".join([report, *extras]) if extras else report


def converted_report(chat_id: int, storage: Storage, settings: Settings,
                     members: Sequence[ChatMember], chat_currency: str) -> str:
    """Команда /d: все записи чата, приведённые к валюте чата по курсу на дату записи."""
    try:
        debts = storage.list_debts(chat_id)
        if not debts:
            return "📭 Долгов нет — пересчитывать нечего."
        base = str(settings.rates_base or "BYN").upper()
        update = update_rates(settings, storage)          # раз в день, без cron
        points = storage.rates_since(base, history_start(debts))
    except StorageError as exc:
        return f"⚠️ Проблема с базой данных: {exc}"
    converted = convert_debts(debts, chat_currency, rate_table(points), base)
    header: list[str] = []
    if converted.changed:
        header.append(f"💱 Привёл к {chat_currency.upper()} по курсу на дату записи:")
        header.extend(format_used_rates(converted.rates_used, chat_currency.upper()))
    else:
        header.append("💱 Курсов за эти даты в базе нет — показываю записи как есть.")
        header.append("Обновить курсы: /rates")
    if converted.skipped:
        header.append("• Без курса оставил: " + ", ".join(sorted(set(converted.skipped))))
    if update.problems:
        header.append("⚠️ " + "; ".join(update.problems[:2]))
    report = format_debts_report(converted.debts, chat_currency, members)
    return "\n".join([*header, "", report])


def settle_report(chat_id: int, storage: Storage, settings: Settings,
                  members: Sequence[ChatMember], chat_currency: str) -> str:
    """Команда /settle: взаимозачёт — минимальный список переводов, чтобы всё закрылось.

    Считаем в валюте чата и по курсу на дату каждой записи (как /d), поэтому суммы
    совпадают с приведённым отчётом, а переводов получается меньше, чем пар долгов.
    """
    try:
        debts = storage.list_debts(chat_id)
        if not debts:
            return "📭 Долгов нет — закрывать нечего."
        base = str(settings.rates_base or "BYN").upper()
        update = update_rates(settings, storage)
        points = storage.rates_since(base, history_start(debts))
    except StorageError as exc:
        return f"⚠️ Проблема с базой данных: {exc}"
    converted = convert_debts(debts, chat_currency, rate_table(points), base)
    transfers = minimal_transfers(converted.debts, members)
    if not converted.changed:
        transfers = minimal_transfers(debts, members)      # курсов нет — считаем как есть
    lines: list[str] = []
    if converted.changed:
        lines.append(f"💱 Считаю в {chat_currency.upper()} по курсу на дату записи:")
        lines.extend(format_used_rates(converted.rates_used, chat_currency.upper()))
        lines.append("")
    lines.append(format_transfers(transfers, chat_currency))
    if converted.skipped:
        lines.append("• Без курса оставил: " + ", ".join(sorted(set(converted.skipped))))
    if update.problems:
        lines.append("⚠️ " + "; ".join(update.problems[:2]))
    return "\n".join(lines)


def debts_txt_report(chat_id: int, storage: Storage) -> str | TxtReport:
    """Команда /export: TXT-файл со всеми записями чата — копия строк таблицы debts.

    Берутся только записи этого чата (chat_id из сообщения) и ничего не считается:
    ни взаимозачёта, ни пересчёта по курсам — строки таблицы как есть. Если записей нет,
    файл не отправляем: пустой документ в чате только мешает.
    """
    try:
        debts = storage.list_debts(chat_id)
    except StorageError as exc:
        return f"⚠️ Проблема с базой данных: {exc}"
    if not debts:
        return (
            "📭 Записей нет — выгружать нечего.\n"
            "Запишите долг сообщением: «Леша должен Диме 3 рубля»."
        )
    return TxtReport(
        filename=f"debts_{chat_id}_{date.today().isoformat()}.txt",
        text=format_debts_dump(debts, chat_id),
        caption=f"📄 Выгрузка таблицы debts: записей {len(debts)} (chat_id {chat_id}).",
    )


def _not_registered_reply(sides: Sequence[tuple[ChatMember | None, str | None]]) -> str:
    """Отказ записывать, если сторона записи — не зарегистрированный участник чата.

    Учёт ведётся только по людям с командой /reg: имя из текста, которое ни с кем
    не связано, — это повод попросить регистрацию, а не повод писать долг «на строку».
    """
    unknown: list[str] = []
    unregistered: list[str] = []
    for member, name in sides:
        label = str(name or "").strip()
        if member is None:
            if label:
                unknown.append(label)
        elif not member.is_registered:
            unregistered.append(member.label)
    if not unknown and not unregistered:
        return ""
    lines = ["❌ Не записал: записываю только на зарегистрированных участников."]
    if unknown:
        lines.append("• Не знаю такого человека в чате: " + ", ".join(unknown))
    if unregistered:
        lines.append("• Ещё не зарегистрирован: " + ", ".join(unregistered))
    lines.append("")
    lines.append(REGISTER_HINT)
    return "\n".join(lines)


def parse_registration(argument: str, author: ChatMember | None,
                       members: Sequence[ChatMember]) -> tuple[ChatMember | None, list[str], str]:
    """Разбирает аргументы /reg и отвечает: кого регистрируем, какие имена, что не так.

    Формы:
      «/reg @Genia Женя, ЖеняШ, шаман» — регистрируем участника с таким @ником;
      «/reg Женя, ЖеняШ, шаман»        — регистрируем автора сообщения;
      «/reg @Genia»                    — без новых имён: просто отметить участника.
    """
    raw = (argument or "").strip()
    if not raw:
        return author, [], ""
    target: ChatMember | None = author
    rest = raw
    handle_match = REG_HANDLE_RE.match(raw)
    if handle_match:
        handle = handle_match.group("handle").lower()
        target = next(
            (member for member in members if member.username.lower() == handle),
            None,
        )
        rest = raw[handle_match.end():]
        if target is None:
            known = ", ".join(f"@{member.username}" for member in members if member.username)
            return None, [], (
                f"❌ Не нашёл @{handle_match.group('handle')} в этом чате.\n"
                "Я запоминаю людей по их сообщениям — пусть этот человек напишет что-нибудь "
                "в чат, и я его узнаю.\n"
                f"Известные @ники: {known or 'пока никого'}.\n"
                "Себя можно зарегистрировать так: /reg Женя, ЖеняШ, кличка"
            )
    elif author is None:
        return None, [], (
            "❌ Не понял, кого регистрируем: не вижу автора сообщения.\n"
            "Напишите так: /reg @его_ник Имя, кличка — или /reg Имя, кличка про себя."
        )
    rest = rest.lstrip(":—-–— \t")
    aliases = [part.strip().lstrip("@") for part in REG_ALIAS_SPLIT_RE.split(rest)]
    return target, [alias for alias in aliases if alias], ""


def register_command(argument: str, storage: Storage, members: Sequence[ChatMember],
                     author: ChatMember | None) -> str:
    """Команда /reg: связывает участника чата с именами, по которым его узнают."""
    target, aliases, problem = parse_registration(argument, author, members)
    if problem:
        return problem
    if target is None:
        return (
            "Укажите, кого регистрируем:\n"
            "• себя: /reg Женя, ЖеняШ, шаман\n"
            "• другого: /reg @Genie Женя, ЖеняШ"
        )
    before = {alias.lower() for alias in target.aliases}
    updated = with_aliases(target, aliases)
    added = [alias for alias in updated.aliases if alias.lower() not in before]
    storage.register_member(updated)
    return format_registered(updated, added)


def _skipped_names(members: Sequence[ChatMember], excluded: Sequence[ChatMember]) -> list[str]:
    """Кто не участвует в общем счёте: не зарегистрирован (подсказка для ответа)."""
    excluded_ids = {member.user_id for member in excluded}
    names = [
        member.label for member in members
        if not member.is_registered and member.user_id not in excluded_ids
    ]
    if len(names) > MAX_SKIPPED_SHOWN:
        hidden = len(names) - MAX_SKIPPED_SHOWN
        names = [*names[:MAX_SKIPPED_SHOWN], f"и ещё {hidden}"]
    return names


def _resolve_group_names(names: Sequence[str], members: Sequence[ChatMember],
                         fallback_author: ChatMember | None) -> tuple[list[ChatMember], list[str]]:
    """Сопоставляет имена из сообщения с зарегистрированными участниками чата.

    Возвращает (участники, непонятные имена). Незарегистрированные тоже попадают
    во второй список: записи на них не ведутся, сначала нужно /reg.
    """
    found: list[ChatMember] = []
    unknown: list[str] = []
    seen: set[int] = set()
    for name in names:
        member = resolve_member(name, members, fallback_author)
        if member is None or not member.is_registered:
            unknown.append(str(name))
        elif member.user_id not in seen:
            seen.add(member.user_id)
            found.append(member)
    return found, unknown


def save_expense(parsed: ParsedMessage, raw: str, chat_id: int, storage: Storage,
                 members: Sequence[ChatMember], author: ChatMember | None,
                 currency: str = "BYN") -> str:
    """Записывает общий счёт: делит сумму между участниками чата поровну.

    «Дима заплатил 10 за всех» → каждому, кроме Димы, достаётся доля 10 / 4 = 2.50.
    «…кроме Оли» — Оля в делёж не входит; «кроме себя» — сам плативший тоже.
    Оригинал сообщения сохраняется в каждой доле (raw_text), а все доли получают
    один group_id: благодаря ему /undo убирает общий счёт целиком, а не строку.
    """
    if not parsed.is_expense:
        return NOT_AN_EXPENSE_REPLY
    payer = resolve_side(parsed.from_name, parsed.from_user_id, members, author)
    problem = _not_registered_reply([(payer, parsed.from_name)])
    if problem or payer is None:
        return problem

    if parsed.participants:
        participants, unknown = _resolve_group_names(parsed.participants, members, payer)
        if unknown:
            return (
                "❌ Не понял, за кого счёт: " + ", ".join(unknown) + "\n"
                "За кого платили, тоже должно быть зарегистрировано.\n" + REGISTER_HINT
            )
        if payer.user_id not in {member.user_id for member in participants}:
            participants.append(payer)          # кто платил, тот тоже участник счёта
    else:
        participants = registered_members(members)

    excluded: list[ChatMember] = []
    if parsed.exclude:
        excluded, unknown_excluded = _resolve_group_names(parsed.exclude, members, payer)
        if unknown_excluded:
            return (
                "❌ Не понял, кого исключить: " + ", ".join(unknown_excluded) + "\n"
                + REGISTER_HINT
            )

    excluded_ids = {member.user_id for member in excluded}
    debtors = [
        member for member in participants
        if member.user_id != payer.user_id and member.user_id not in excluded_ids
    ]
    if not debtors:
        return (
            "🧾 Делить не с кого: кроме платившего, в счёте никого.\n"
            "Если счёт общий, напишите «… за всех», а остальным нужно "
            "зарегистрироваться: /reg Имя, кличка."
        )

    amount = round(float(parsed.amount or 0), 2)
    payer_in_split = payer.user_id not in excluded_ids
    people = len(debtors) + (1 if payer_in_split else 0)
    shares = split_amount(amount, people)
    pair_shares = list(zip(debtors, shares))
    group_id = uuid.uuid4().hex[:16]
    storage.add_debts(chat_id, [
        {
            "from_name": _member_name(member, member.label),
            "to_name": _member_name(payer, payer.label),
            "from_user_id": member.user_id,
            "to_user_id": payer.user_id,
            "currency": currency,
            "amount": share,
            "kind": "expense",
            "raw_text": raw,
            "group_id": group_id,
        }
        for member, share in pair_shares
    ])
    return format_expense_saved(ExpenseSummary(
        payer=payer,
        currency=currency,
        amount=amount,
        share=shares[0] if shares else 0.0,
        people=people,
        debtors=pair_shares,
        excluded=excluded,
        skipped=_skipped_names(members, excluded),
        raw_text=raw,
    ))


def handle_text(
    text: str,
    chat_id: int,
    *,
    storage: Storage,
    parser: Any,
    settings: Settings,
    members: Sequence[ChatMember] | None = None,
    author: ChatMember | None = None,
) -> str | TxtReport:
    """Обрабатывает одно сообщение и формирует ответ бота.

    Функция не знает про Telegram — это делает её простой для тестов.
    `members` и `author` — состав чата и автор сообщения: по ним ИИ (и локальный
    резолвер) понимают, кто такой «Лешак» из текста, и запись привязывается к user id.

    Обычный ответ — строка; команда выгрузки (/export) отвечает объектом TxtReport:
    Telegram не умеет отправлять файл сообщением, поэтому решение «файл или текст»
    принимает вызывающая сторона (DebtBot).
    """
    raw = (text or "").strip()
    if members is None:
        members = _load_members(storage, chat_id)
    if not raw:
        return format_help(settings.default_currency)

    default_currency = storage.get_default_currency(chat_id, settings.default_currency)

    # Пароль чата: пока он не введён верно, бот в этом чате ничего не делает —
    # ни долгов, ни отчётов, ни регистрации участников.
    if settings.password_required and not _chat_authorized(storage, chat_id):
        if is_password_attempt(raw, settings.chat_password):
            storage.set_chat_authorized(chat_id, True)
            logger.info("Чат %s подтвердил пароль.", chat_id)
            return PASSWORD_OK_REPLY
        if looks_like_password(raw):
            return PASSWORD_FAIL_REPLY
        return PASSWORD_REPLY

    command, _, argument = raw.partition(" ")
    command = command.lower()

    if command in ("/start", "/help"):
        return format_help(default_currency)
    if command in ("/reg", "/register"):
        return register_command(argument, storage, members, author)
    if command in ("/who", "/members"):
        return format_members_report(members)
    if command in ("/rates", "/rate"):
        return rates_report(storage, settings, default_currency)
    if command in ("/d", "/convert"):
        return converted_report(chat_id, storage, settings, members, default_currency)
    if command in ("/settle", "/offset", "/зачёт", "/зачет"):
        return settle_report(chat_id, storage, settings, members, default_currency)
    if command in EXPORT_COMMANDS:
        return debts_txt_report(chat_id, storage)
    if command == "/debts":
        return format_debts_report(storage.list_debts(chat_id), default_currency, members)
    if command == "/currency":
        return set_default_currency(argument or raw, chat_id, storage, default_currency)
    if command == "/reset":
        removed = storage.delete_debts(chat_id)
        return f"🧹 Удалено записей: {removed}." if removed else "📭 Записей и так нет."
    if command == "/undo":
        removed = storage.delete_last_debt(chat_id)
        if removed is None:
            return "📭 Записей нет — удалять нечего."
        if removed.group_id:
            # Общий счёт — одна операция: убираем все его доли, а не одну строку.
            rest = storage.delete_group(chat_id, removed.group_id)
            text = f" «{removed.raw_text}»" if removed.raw_text else ""
            return f"🗑 Удалил общий счёт{text} целиком: записей {rest + 1}."
        return f"🗑 Удалил последнюю запись: {removed.pretty()}"

    parsed = parser.parse(raw, default_currency, members=members, author=author)
    explicit_currency = detect_currency(raw)

    if parsed.intent in ("debt", "repayment"):
        # Тип операции определяет ИИ по смыслу: «должен» — долг, «вернул» — возврат.
        is_repayment = parsed.intent == "repayment"
        if not (parsed.is_repayment if is_repayment else parsed.is_debt):
            return NOT_A_REPAYMENT_REPLY if is_repayment else NOT_A_DEBT_REPLY
        member_from = resolve_side(parsed.from_name, parsed.from_user_id, members, author)
        member_to = resolve_side(parsed.to_name, parsed.to_user_id, members, author)
        problem = _not_registered_reply([
            (member_from, parsed.from_name),
            (member_to, parsed.to_name),
        ])
        if problem:
            return problem
        currency = (parsed.currency or explicit_currency or default_currency).upper()
        record = storage.add_debt(
            chat_id=chat_id,
            from_name=_member_name(member_from, str(parsed.from_name or "")),
            to_name=_member_name(member_to, str(parsed.to_name or "")),
            currency=currency,
            amount=float(parsed.amount or 0),
            raw_text=raw,
            kind="repayment" if is_repayment else "debt",
            from_user_id=member_from.user_id if member_from else None,
            to_user_id=member_to.user_id if member_to else None,
        )
        formatter = format_repayment_saved if is_repayment else format_debt_saved
        return formatter(record, members)

    if parsed.intent == "expense":
        return save_expense(
            parsed, raw, chat_id, storage, members, author,
            currency=(parsed.currency or explicit_currency or default_currency).upper(),
        )

    if parsed.intent == "debts":
        return format_debts_report(storage.list_debts(chat_id), default_currency, members)
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


def mentions_bot(message: Mapping[str, Any], bot_username: str) -> bool:
    """Обращается ли сообщение к боту: «@бот …», «/команда@бот» или ответ на его сообщение."""
    handle = str(bot_username or "").lstrip("@")
    if not handle:
        return False
    pattern = re.compile(rf"@{re.escape(handle)}(?![\w])", re.IGNORECASE)
    if pattern.search(str(message.get("text") or "")) or pattern.search(str(message.get("caption") or "")):
        return True
    reply_from = (message.get("reply_to_message") or {}).get("from") or {}
    return str(reply_from.get("username") or "").lower() == handle.lower()


# Команда в начале сообщения: «/help», «/d 10 USD», «/debts@наш_бот», «/зачёт».
# После команды должен идти пробел или конец строки — «/usr/bin/ls» командой не считается.
COMMAND_HEAD_RE = re.compile(
    r"^/(?P<name>[A-Za-zА-Яа-яЁё0-9_]{1,32})(?:@(?P<handle>[A-Za-z0-9_]{3,32}))?(?:\s|$)"
)


def is_command_for_bot(text: str, bot_username: str = "") -> bool:
    """Считается ли сообщение командой боту (обращение без @упоминания).

    Telegram с включённым privacy mode и сам присылает боту в группах команды в начале
    сообщения, поэтому «/help» и «/debts» — это уже обращение к боту. Чужие команды
    («/help@другой_бот») игнорируем, чтобы не отвечать за других ботов.
    """
    match = COMMAND_HEAD_RE.match(str(text or "").strip())
    if not match:
        return False
    addressee = str(match.group("handle") or "").lstrip("@").lower()
    if not addressee:
        return True
    handle = str(bot_username or "").lstrip("@").lower()
    return bool(handle) and addressee == handle


def clean_bot_mention(text: str, bot_username: str) -> str:
    """Убирает обращение к боту, чтобы разбор текста не сбивался.

    «/debts@бот» → «/debts», «@бот Леша должен Диме 3» → «Леша должен Диме 3».
    Упоминания других людей («@Дима») при этом остаются на месте.
    """
    handle = str(bot_username or "").lstrip("@")
    raw = text or ""
    if not handle:
        return raw.strip()
    cleaned = re.sub(rf"(/[\w]+)@{re.escape(handle)}(?![\w])", r"\1", raw, flags=re.IGNORECASE)
    cleaned = re.sub(rf"@{re.escape(handle)}(?![\w])", " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def addressing(message: Mapping[str, Any], bot_username: str,
               settings: Settings) -> tuple[bool, str]:
    """Решает, отвечать ли на сообщение, и отдаёт текст без обращения к боту.

    * личный чат — отвечаем всегда (там обращаться некуда);
    * группа/супергруппа — если позвали: «@бот …», «/команда@бот», «/команда» в начале
      сообщения или ответ на сообщение бота (`REQUIRE_MENTION=0` это правило отключает).

    Команды без упоминания (`/help`, `/debts`) считаем обращением к боту: Telegram
    с включённым privacy mode и сам присылает их боту, а человеку лишнее «@бот» писать
    неудобно. Чужие команды («/help@другой_бот») при этом игнорируем.

    Если имя бота неизвестно (не задан `BOT_USERNAME` и не ответил getMe), лишнего молчания
    не допускаем: при включённом privacy mode Telegram и так присылает в группы только
    адресованные сообщения.
    """
    text = str(message.get("text") or "")
    chat_type = str((message.get("chat") or {}).get("type") or "private").lower()
    handle = str(bot_username or "").lstrip("@")
    cleaned = clean_bot_mention(text, handle)

    if chat_type == "private" or not settings.require_mention:
        return True, cleaned
    if not handle:
        logger.warning("Имя бота неизвестно — в группе отвечаю на любое сообщение.")
        return True, cleaned
    if mentions_bot(message, handle):
        return True, cleaned
    return is_command_for_bot(cleaned, handle), cleaned


def configure_logging(level: str = "INFO") -> None:
    """Включает логи с таймстампами.

    Нужно и локально, и на хостингах: у WSGI-приложений (PythonAnywhere, Vercel) всё, что
    пишется в stderr, попадает в error log веб-приложения — иначе ошибки DeepSeek/Supabase
    будет просто негде увидеть.
    """
    logging.basicConfig(
        level=getattr(logging, str(level or "INFO").upper(), logging.INFO),
        format=LOG_FORMAT,
    )


def _storage_for(settings: Settings) -> SupabaseStorage:
    """Хранилище Supabase с таблицами из настроек (общее для бота, вебхука и --check)."""
    return SupabaseStorage(
        settings.supabase_url,
        settings.supabase_key,
        debts_table=settings.debts_table,
        settings_table=settings.settings_table,
        state_table=settings.state_table,
        members_table=settings.members_table,
        rates_table=settings.rates_table,
        timeout=settings.request_timeout,
    )


def build_runtime(settings: Settings) -> tuple[Storage, DeepSeekParser, TelegramBot]:
    """Собирает рабочие сервисы: хранилище Supabase, парсер DeepSeek, клиент Telegram.

    Используется и постоянным процессом (bot.py), и вебхуком (webhook.py).
    """
    storage = _storage_for(settings)
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
                 telegram: TelegramBot, *, bot_username: str | None = None) -> None:
        self._settings = settings
        self._storage = storage
        self._parser = parser
        self._telegram = telegram
        self._stop = False
        # Имя бота без @ нужно, чтобы понимать обращения «@бот …» в группах.
        self._bot_username = str(bot_username or settings.bot_username or "").lstrip("@")

    @property
    def bot_username(self) -> str:
        """Имя бота без @ (одна попытка getMe, дальше — кеш)."""
        if not self._bot_username:
            try:
                me = self._telegram.get_me() or {}
            except TelegramError as exc:
                logger.warning("getMe не ответил (%s) — имя бота неизвестно.", exc)
                return ""
            self._bot_username = str(me.get("username") or "").lstrip("@")
        return self._bot_username

    def _refresh_rates_if_due(self) -> None:
        """Подтягивает курсы, если по расписанию пора (раз в день в settings.rates_hour).

        Вызывается и в цикле опроса, и при обработке апдейта: на serverless-хостинге
        фонового цикла нет, поэтому первый апдейт после назначенного часа запускает
        обновление сам. Пока час не наступил и пока курсы за сегодня уже есть, обращений
        к API и базе не будет. Проблемы пишем только в лог — сообщения важнее.
        """
        try:
            result = update_rates_scheduled(self._settings, self._storage)
        except RatesError as exc:
            logger.warning("Автообновление курсов: %s", exc)
            return
        except StorageError as exc:
            logger.warning("Автообновление курсов, база недоступна: %s", exc)
            return
        if result is None:
            return
        if result.saved:
            logger.info("Курсы обновлены автоматически: %s значений на %s (%s)",
                        result.saved, result.rate_date, ", ".join(result.currencies))
        for problem in result.problems:
            logger.warning("Курсы: %s", problem)

    def run(self, poll_timeout: int = 25, max_updates: int | None = None) -> int:
        """Постоянный режим (long polling): ответы приходят мгновенно.

        Используется на хостинге: процесс живёт всё время, при остановке контейнера
        (SIGTERM/SIGINT) корректно завершается и сохраняет смещение в bot_state,
        чтобы после перезапуска повторно доставленные апдейты не записались дважды.
        """
        me = self._telegram.get_me()
        self._bot_username = self._bot_username or str(me.get("username") or "").lstrip("@")
        offset = self._load_offset()
        logger.info(
            "Бот @%s (id %s) запущен (long polling). Стартовое смещение: %s",
            me.get("username"), me.get("id"), offset,
        )
        self._install_signal_handlers()
        processed = 0
        try:
            while not self._stop:
                self._refresh_rates_if_due()      # 12:00 по Минску — курсы обновляются сами
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
        цикл опроса не должен из-за этого падать.
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
        self._refresh_rates_if_due()              # в вебхук-режиме это единственный «таймер»
        membership = update.get("my_chat_member")
        if isinstance(membership, Mapping):
            self._handle_membership(membership)
            return
        message = update.get("message") or {}
        text = message.get("text")
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if not text or chat_id is None:
            return

        # В группе отвечаем только на обращение («@бот …», «/команда@бот», ответ на наше
        # сообщение), иначе — молчим, чтобы не комментировать весь чат.
        should_handle, text = addressing(message, self.bot_username, self._settings)
        if not should_handle:
            logger.info("Сообщение %s не адресовано боту — пропускаю.", message.get("message_id"))
            return

        if not is_allowed(user_id, self._settings):
            logger.warning("Сообщение от недопущенного пользователя id=%s", user_id)
            self._send(chat_id, DENIED_REPLY, message)
            return

        self._telegram.send_typing(chat_id)
        # Из автора сообщения собирается состав чата: по нему ИИ понимает, что «Лешак» — это
        # Леша Козлов, и запись привязывается к его user id.
        author = member_from_telegram(int(chat_id), message.get("from") or {})
        members = self._remember_author(author)
        try:
            reply = handle_text(
                text, int(chat_id),
                storage=self._storage, parser=self._parser, settings=self._settings,
                members=members, author=author,
            )
        except StorageError as exc:
            logger.error("Хранилище: %s", exc)
            reply = f"⚠️ Проблема с базой данных: {exc}"
        except Exception as exc:  # noqa: BLE001 — бот не должен падать из-за одного сообщения
            logger.exception("Ошибка обработки сообщения: %s", exc)
            reply = "⚠️ Внутренняя ошибка, попробуйте ещё раз."
        self._send_reply(chat_id, reply, message)

    def _handle_membership(self, membership: Mapping[str, Any]) -> None:
        """Реакция на добавление и удаление бота: просит пароль, сбрасывает доступ.

        Telegram присылает `my_chat_member`, когда бота добавляют в чат или удаляют из него.
        Если задан CHAT_PASSWORD, до верного пароля бот в этом чате не работает; при выходе
        из чата доступ сбрасывается — вернут бота обратно, пароль спросят снова.
        """
        chat = membership.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        new_status = str((membership.get("new_chat_member") or {}).get("status") or "").lower()
        old_status = str((membership.get("old_chat_member") or {}).get("status") or "").lower()
        if new_status in ("member", "administrator") and old_status in ("left", "kicked", ""):
            logger.info("Бот добавлен в чат %s — приветствие отправлено.", chat_id)
            self._send(chat_id, added_to_chat_reply(self._settings), {})
        elif new_status in ("left", "kicked"):
            logger.info("Бот удалён из чата %s — доступ сброшен.", chat_id)
            try:
                self._storage.set_chat_authorized(int(chat_id), False)
            except StorageError as exc:
                logger.warning("Не удалось сбросить доступ чата %s: %s", chat_id, exc)

    def _remember_author(self, author: ChatMember | None) -> list[ChatMember]:
        """Сохраняет автора в составе чата и возвращает актуальный список участников."""
        if author is None:
            return []
        try:
            self._storage.remember_member(author)
        except StorageError as exc:
            logger.warning("Не удалось запомнить участника %s: %s", author.user_id, exc)
        members = _load_members(self._storage, author.chat_id)
        if all(member.user_id != author.user_id for member in members):
            members = [*members, author]      # состав мог не прочитаться — автора всё равно знаем
        return members

    def _send(self, chat_id: Any, text: str, message: Mapping[str, Any]) -> None:
        """Отправляет ответ, логируя проблемы доставки."""
        try:
            self._telegram.send_message(chat_id, text, reply_to=message.get("message_id"))
        except TelegramError as exc:
            logger.error("sendMessage: %s", exc)

    def _send_reply(self, chat_id: Any, reply: str | TxtReport,
                    message: Mapping[str, Any]) -> None:
        """Отправляет ответ: обычный текст — сообщением, TXT-отчёт — файлом-документом.

        Если документ не дошёл, честно сообщаем об этом в чат: молчание выглядело бы как
        «бот ничего не нашёл», а причина (лимиты, права в чате) важна для человека.
        """
        if not isinstance(reply, TxtReport):
            self._send(chat_id, reply, message)
            return
        try:
            self._telegram.send_document(
                chat_id, reply.filename, reply.text,
                caption=reply.caption, reply_to=message.get("message_id"),
            )
        except TelegramError as exc:
            logger.error("sendDocument: %s", exc)
            self._send(chat_id, f"⚠️ Не удалось отправить файл {reply.filename}: {exc}", message)


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
    print("  ⚠ Постоянный процесс (`python bot.py`) должен быть остановлен:")
    print("    по одному адресу Telegram шлёт апдейты только одним способом.")
    return 0


def update_rates_mode(settings: Settings, *, force: bool = False) -> int:
    """Команда --rates: обновляет курсы валют в базе (обычно это делается само раз в день)."""
    problems = settings.problems()
    if problems:
        print("Ошибка: проверьте настройки:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1
    try:
        storage = _storage_for(settings)
        result = update_rates(settings, storage, force=force)
    except (StorageError, RatesError) as exc:
        print("Ошибка:", exc, file=sys.stderr)
        return 1

    if result.saved:
        print(f"✓ Курсы записаны: {result.saved} значений на {result.rate_date}")
        print("  валюты: " + ", ".join(result.currencies))
    else:
        print("Курсы не обновлялись:", result.reason or "нет данных")
    for problem in result.problems:
        print("⚠", problem)
    print("  посмотреть в Telegram: /rates, привести долги к валюте чата: /d")
    print(f"  дальше курсы подтягиваются сами: {rates_schedule_text(settings)}")
    return 0 if (result.saved or not result.problems) else 1


def delete_webhook_mode(settings: Settings, *, drop_pending: bool = False) -> int:
    """Команда --delete-webhook: снова long polling."""
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
    print("✓ Вебхук снят — бот снова работает через getUpdates (python bot.py).")
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
        if settings.require_mention:
            print(f"• В группах отвечаю только на обращение: «@{me.get('username')} …», "
                  "«/debts@...» или ответ на моё сообщение")
        else:
            print("• REQUIRE_MENTION=0 — в группах отвечаю на любое сообщение")
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
            print("• Telegram: вебхук не установлен — режим: python bot.py (long polling)")
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
        storage = _storage_for(settings)
        debts = storage.list_debts(0)
        print(f"✓ Supabase: таблица {settings.debts_table} доступна (пробный запрос: {len(debts)} строк)")
        storage.get_default_currency(0, settings.default_currency)
        print(f"✓ Supabase: таблица {settings.settings_table} доступна")
        members = storage.list_members(0)
        print(f"✓ Supabase: таблица {settings.members_table} доступна (участников: {len(members)})")
        rate_points = storage.rates_since(settings.rates_base, date.today().isoformat())
        print(f"✓ Supabase: таблица {settings.rates_table} доступна "
              f"(курсов на сегодня: {len(rate_points)})")
    except StorageError as exc:
        ok = False
        print("✗ Supabase:", exc)

    rates_problem = settings.rates_problem()
    if rates_problem:
        print("⚠", rates_problem)
    else:
        print(f"✓ Курсы валют: {settings.rates_source}, база {settings.rates_base}, "
              f"валюты {', '.join(settings.rates_currencies)}")
        print(f"  обновление — {rates_schedule_text(settings)}; вручную: python bot.py --rates")

    if settings.password_required:
        print("• CHAT_PASSWORD задан — бот просит пароль при добавлении в чат "
              "и работает только там, где пароль введён")
    else:
        print("• CHAT_PASSWORD не задан — бот работает в любом чате без пароля")

    print()
    print("Итог:", "всё готово — запускайте python bot.py" if ok
          else "есть проблемы — исправьте и повторите --check")
    return ok


DEMO_MESSAGES = (
    "/who",                                  # кто в чате и кто зарегистрирован
    "Лешак должен Диме 3 рубля",             # «Лешак» — это Леша Козлов, «Диме» — Дмитрий Болт
    "/reg Лёха, Лешак",                      # автор (Леша) добавляет себе имена
    "Маша заняла у Пети 10$",
    "покажи долги",
    "валюта по умолчанию доллар",
    "Петя должен Маше 5 долларов",
    "я должен Диме 2 рубля",                 # «я» — это автор сообщения (Леша Козлов)
    "покажи долги",
    "Леша вернул Диме 1 рубль",              # возврат: уменьшает сальдо
    "Дима заплатил 10 за всех",              # общий счёт: 10.00 делится на зарегистрированных
    "Маша оплатила ужин 30 рублей за всех кроме Пети",   # общий счёт с исключением
    "Гоша должен Диме 4 рубля",              # Гоша не зарегистрирован — записи не будет
    "/reg @gosha_p Гоша, Гоша Петров",       # регистрируем Гошу по @нику
    "Гоша должен Диме 4 рубля",              # теперь записывается
    "/rates",                                # курсы валют из базы (в демо — без API)
    "/d",                                    # все записи в валюте чата по курсу на дату
    "/settle",                               # взаимозачёт: минимум переводов
    "/export",                               # TXT-файл с записями чата (копия таблицы debts)
    "/debts",
    "/undo",                                 # убираем последний счёт или запись
    "/currency BYN",
    "привет",
)

# Участники демо-чата: так бот понимает, что «Лешак» и «Лёха» — это @kozlovAlex.
# Гоша специально без отметки /reg — на нём видно, как бот просит регистрацию.
DEMO_MEMBERS = (
    ChatMember(chat_id=1, user_id=101, username="kozlovAlex", display_name="Леша Козлов",
               aliases=["Леша"], is_registered=True),
    ChatMember(chat_id=1, user_id=102, username="bdzmity", display_name="Дмитрий Болт",
               aliases=["Дима", "Димон"], is_registered=True),
    ChatMember(chat_id=1, user_id=103, username="petrova_m", display_name="Маша Петрова",
               aliases=["Маша"], is_registered=True),
    ChatMember(chat_id=1, user_id=104, username="petya_k", display_name="Петя Кузнецов",
               aliases=["Петя"], is_registered=True),
    ChatMember(chat_id=1, user_id=105, username="gosha_p", display_name="Гоша Петров"),
)


def describe_reply(reply: str | TxtReport) -> str:
    """Ответ для консоли (--demo): текст как есть, отчёт — имя файла и первые строки."""
    return reply.preview() if isinstance(reply, TxtReport) else reply


def run_demo() -> int:
    """Прогон сценария без внешних сервисов: хранилище в памяти + офлайн-разбор."""
    settings = Settings(default_currency="BYN")
    today = date.today().isoformat()
    storage = InMemoryStorage(default_currency=settings.default_currency,
                              default_created_at=f"{today}T10:00:00+00:00")
    parser = HeuristicParser()
    chat_id = 1
    for member in DEMO_MEMBERS:
        storage.remember_member(member)
    demo_rates(storage, today)
    author = DEMO_MEMBERS[0]          # сообщения пишет Леша Козлов: «я» = он
    print("Демонстрация работы бота (без Telegram, DeepSeek и Supabase)")
    print("=" * 64)
    print("Участники чата: " + ", ".join(member.label for member in DEMO_MEMBERS))
    for message in DEMO_MESSAGES:
        reply = handle_text(message, chat_id, storage=storage, parser=parser,
                            settings=settings, author=author)
        print(f"\n👤 {message}\n🤖 {describe_reply(reply)}")
    print("\n" + "=" * 64)
    print("Чат с паролем (CHAT_PASSWORD): пока пароль не введён, бот не работает")
    protected = Settings(default_currency="BYN", chat_password="сезам")
    guarded = InMemoryStorage(default_currency="BYN")
    for message in ("Леша должен Диме 3 рубля", "/password наугад", "сезам"):
        reply = handle_text(message, 42, storage=guarded, parser=parser, settings=protected)
        print(f"\n👤 {message}\n🤖 {describe_reply(reply)}")
    print("=" * 64)
    print(f"Итого записей в памяти: {len(storage.debts)}")
    return 0


def demo_rates(storage: InMemoryStorage, today: str) -> None:
    """Курсы для демонстрации: /d и /rates показывают пересчёт без обращения к API."""
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    storage.save_rates([
        {"rate_date": yesterday, "base": "BYN", "currency": "USD", "rate": 3.20, "source": "demo"},
        {"rate_date": yesterday, "base": "BYN", "currency": "EUR", "rate": 3.45, "source": "demo"},
        {"rate_date": today, "base": "BYN", "currency": "USD", "rate": 3.25, "source": "demo"},
        {"rate_date": today, "base": "BYN", "currency": "EUR", "rate": 3.52, "source": "demo"},
        {"rate_date": today, "base": "BYN", "currency": "RUB", "rate": 0.0331, "source": "demo"},
        {"rate_date": today, "base": "BYN", "currency": "CNY", "rate": 0.4523, "source": "demo"},
        {"rate_date": today, "base": "BYN", "currency": "THB", "rate": 0.0994, "source": "demo"},
    ])


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
    parser.add_argument(
        "--rates",
        action="store_true",
        help="обновить курсы валют в базе (обычно это делается само раз в день)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="вместе с --rates: обновить курсы, даже если за сегодня они уже есть",
    )
    parser.add_argument("--poll-timeout", type=int, default=25, help="время ожидания апдейтов, сек")
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
    """Точка входа: рабочий режим, --check, --demo или --rates."""
    configure_stdout()
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if args.demo:
        return run_demo()
    if args.check:
        return 0 if check_services(settings) else 1
    if args.rates:
        return update_rates_mode(settings, force=args.force)
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
        bot.run(poll_timeout=max(5, args.poll_timeout))
    except (TelegramError, StorageError) as exc:
        logger.error("Сбой: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
