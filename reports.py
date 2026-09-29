# -*- coding: utf-8 -*-
"""Ежемесячный отчёт по долгам на почту: письмо через Gmail API, запуск по cron.

Отчёт — не «внутренний таймер», а команда, которую вызывает планировщик хостинга:
письмо уходит раз в месяц в одно и то же время, а бот в остальное время работает как обычно.

    # первое число каждого месяца, 10:00 (crontab — время сервера)
    0 10 1 * *  cd /path/to/debt_calculator && python reports.py >> reports.log 2>&1

Кому письмо — решает секрет REPORT_EMAIL (.env): все отчёты уходят на этот адрес.
Отправка — через Gmail API (gmail_api.py), доступы GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET и
GMAIL_REFRESH_TOKEN тоже живут в секретах, а не в чате и не в коде.

Письмо всегда одно, а вложений столько, сколько чатов с записями: на каждый чат — свой
CSV-файл `debts_<chat_id>_<год-месяц>.csv` (та же выгрузка таблицы debts, что отдаёт
команда /export), плюс в теле письма — отчёт по чату словами, как в /debts и /who.

Защита от повторной отправки: месяц последнего письма («2026-09») хранится в bot_state под
ключом reports_sent, поэтому один и тот же месяц не уйдёт дважды, даже если cron сработает
лишний раз или скрипт дёрнут руками. Переслать принудительно — `python reports.py --force`.

Проверки:
    python reports.py --check      # что настроено (без сети)
    python reports.py --dry-run    # показать письмо в консоли, ничего не отправляя
    python reports.py --to me@example.com   # разово отправить на другой адрес
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from config import Settings, load_settings
from debts import format_debts_dump, format_debts_report, format_members_report
from gmail_api import Attachment, GmailSender, MailError, SentMessage
from rates import minsk_now
from storage import Storage, StorageError, storage_from_settings

logger = logging.getLogger("debt_bot.reports")

# Ключ в bot_state: месяц, за который отчёт уже отправлен («2026-09»).
REPORT_STATE_KEY = "reports_sent"
MONTH_TITLES = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


def report_period(moment: datetime | None = None) -> str:
    """Период отчёта «год-месяц» по Минску (2026-09): по нему же защищаемся от повторов."""
    return (moment or minsk_now()).strftime("%Y-%m")


def period_title(period: str) -> str:
    """Период словами: «2026-09» → «сентябрь 2026» (для темы письма и итогов в консоли)."""
    parts = str(period or "").split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return str(period or "")
    month = int(parts[1])
    if not 1 <= month <= 12:
        return str(period)
    return f"{MONTH_TITLES[month - 1]} {parts[0]}"


@dataclass(frozen=True)
class ChatReport:
    """Отчёт по одному чату: id, валюта, число записей, текст и файл-вложение.

    Вложение — CSV-выгрузка записей этого чата (та же, что отдаёт /export): в письме
    каждый чат лежит отдельным файлом, а не общей таблицей. У чата без записей файла
    нет: пустой CSV в письме только мешает.
    """

    chat_id: int
    currency: str
    records: int
    text: str
    attachment: Attachment | None = None

    @property
    def file(self) -> str:
        """Имя CSV-файла этого чата во вложении (пусто — записей нет, файла тоже)."""
        return self.attachment.filename if self.attachment else ""


@dataclass(frozen=True)
class MonthlyLetter:
    """Письмо с отчётами: период, тема, текст и чаты, попавшие внутрь."""

    period: str
    subject: str
    body: str
    chats: tuple[ChatReport, ...] = ()
    attachments: tuple[Attachment, ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        """Имена файлов во вложении — по одному на каждый чат с записями."""
        return tuple(attachment.filename for attachment in self.attachments)

    @property
    def empty(self) -> bool:
        """Ни в одном чате нет записей — отправлять нечего."""
        return not any(chat.records for chat in self.chats)


def chat_report(chat_id: int, storage: Storage, period: str) -> ChatReport:
    """Отчёт по одному чату: долги (со взаимозачётом), состав участников и CSV-файл.

    Текст берём тот же, что бот показывает командой /debts (плюс состав чата из /who),
    а файл — ту же выгрузку таблицы, что команда /export (`debts.format_debts_dump`):
    письмо повторяет то, что человек видит в чате, и ничего не считает по-своему.
    """
    debts = storage.list_debts(chat_id)
    currency = storage.get_default_currency(chat_id)
    members = storage.list_members(chat_id)
    title = f"💬 Чат {chat_id}"
    lines = [title, "=" * len(title), "", format_debts_report(debts, currency, members)]
    attachment = None
    if debts:
        name = f"debts_{chat_id}_{period}.csv"
        attachment = Attachment(filename=name, text=format_debts_dump(debts))
        lines.extend(["", f"📄 Файл во вложении: {name} (CSV, записей {len(debts)})"])
    lines.extend(["", format_members_report(members)])
    return ChatReport(chat_id=chat_id, currency=currency, records=len(debts),
                      text="\n".join(lines), attachment=attachment)


def build_monthly_letter(storage: Storage, period: str | None = None,
                         *, chat_ids: Sequence[int] | None = None) -> MonthlyLetter:
    """Собирает письмо с отчётами по всем чатам, где есть записи.

    Чаты без записей в письмо не попадают: они бы только удлиняли его («бот добавлен,
    долгов не писали»). Если записей нет нигде, `letter.empty` истинно — такое письмо
    по умолчанию не отправляется (но видно в `--dry-run`, чтобы проверить вид отчёта).
    """
    current = period or report_period()
    ids = list(chat_ids) if chat_ids is not None else storage.list_chat_ids()
    chats = tuple(chat_report(chat_id, storage, current) for chat_id in ids)
    filled = tuple(chat for chat in chats if chat.records)
    records = sum(chat.records for chat in filled)
    attachments = tuple(chat.attachment for chat in filled if chat.attachment is not None)
    header = [f"📊 Отчёт по долгам за {period_title(current)}", ""]
    if filled:
        header.extend([
            f"Чатов с записями: {len(filled)}, записей всего: {records}.",
            f"Файлов во вложении: {len(attachments)} — по одному CSV на каждый чат.",
        ])
        body = "\n\n".join(["\n".join(header), *(chat.text for chat in filled)])
    else:
        header.append("За этот месяц записей нет: долгов, возвратов и общих счетов не писали.")
        body = "\n".join(header)
    subject = f"Отчёт по долгам за {period_title(current)}"
    return MonthlyLetter(period=current, subject=subject, body=body, chats=chats,
                         attachments=attachments)


@dataclass(frozen=True)
class ReportRun:
    """Итог отправки: что ушло, что нет и почему."""

    period: str
    subject: str = ""
    body: str = ""
    chats: tuple[int, ...] = ()
    files: tuple[str, ...] = ()           # имена CSV-файлов во вложении (по одному на чат)
    sent: tuple[str, ...] = ()            # кому письмо ушло
    message_id: str = ""                  # id сообщения в Gmail
    reason: str = ""                      # почему не отправляли
    problems: tuple[str, ...] = ()

    @property
    def delivered(self) -> bool:
        """Письмо действительно отправлено."""
        return bool(self.sent)


def send_monthly_reports(settings: Settings, storage: Storage, *,
                         period: str | None = None, force: bool = False, to: str = "",
                         dry_run: bool = False, sender: Any = None, session: Any = None,
                         now: datetime | None = None) -> ReportRun:
    """Собирает письмо с отчётами по всем чатам и отправляет его на REPORT_EMAIL.

    Повтор защищён отметкой в базе (bot_state → reports_sent): cron может сработать лишний
    раз или скрипт дёрнут руками, а письмо должно уйти один раз за месяц. `force=True`
    (`--force`) отправляет заново — так проверяют настройки сразу после их заполнения.

    `sender` подменяется в тестах: нужен только метод send(to=, subject=, body=, attachments=).
    Письмо одно, а вложения — по файлу на каждый чат: один CSV на чат, чтобы выгрузку
    можно было открыть отдельно (те же строки таблицы debts, что отдаёт /export).
    Пустые чаты в письмо не попадают; если записей нет нигде, письмо не отправляется,
    но `--dry-run` и `--force` его всё равно показывают и отправляют — это удобно, чтобы
    убедиться, что доступы Gmail работают.
    """
    current = period or report_period(now)
    problem = settings.reports_problem()
    if problem:
        return ReportRun(period=current, reason=problem, problems=(problem,))
    letter = build_monthly_letter(storage, current)
    chats = tuple(chat.chat_id for chat in letter.chats)
    common = {"period": current, "subject": letter.subject, "body": letter.body,
              "chats": chats, "files": letter.files}

    if not chats and not dry_run:
        return ReportRun(**common, reason="в базе нет ни одного чата — отчёт формировать не из чего")
    if letter.empty and not (force or dry_run):
        return ReportRun(**common, reason="за этот месяц записей нет — письмо не отправляю")
    if not force:
        already = str(storage.get_state(REPORT_STATE_KEY, "") or "")
        if already == current:
            return ReportRun(**common, reason=(
                f"отчёт за {period_title(current)} уже отправлен — повторить: --force"))
    if dry_run:
        return ReportRun(**common, reason="проверка без отправки (--dry-run)")

    recipients = (str(to).strip(),) if str(to or "").strip() else tuple(settings.report_recipients)
    mailer = sender or GmailSender(
        settings.gmail_client_id,
        settings.gmail_client_secret,
        settings.gmail_refresh_token,
        sender=settings.gmail_sender,
        api_url=settings.gmail_api_url,
        token_url=settings.gmail_token_url,
        timeout=settings.request_timeout,
        session=session,
    )
    delivered: list[str] = []
    message_id = ""
    for address in recipients:
        message: SentMessage = mailer.send(to=address, subject=letter.subject, body=letter.body,
                                           attachments=letter.attachments)
        delivered.append(address)
        message_id = message.message_id or message_id
        logger.info("Отчёт за %s отправлен: %s (файлов: %d)",
                    period_title(current), address, len(letter.attachments))
    storage.set_state(REPORT_STATE_KEY, current)
    return ReportRun(**common, sent=tuple(delivered), message_id=message_id)


def reports_check_lines(settings: Settings) -> list[str]:
    """Что настроено для отчёта на почту — строки для `bot.py --check` и `reports.py --check`.

    Отправку здесь не проверяем: `--check` не должен слать письма и ходить в Google.
    Проверить доступы по-настоящему: `python reports.py --dry-run` покажет письмо,
    а `--force` отправит его сразу.
    """
    problem = settings.reports_problem()
    recipients = settings.report_recipients
    if problem:
        return [
            f"⚠ Отчёт на почту: {problem}",
            "  См. .env.example, блок «Отчёт на почту»: REPORT_EMAIL и доступы Gmail API.",
        ]
    if not recipients:
        return ["• Отчёт на почту: не настроен (REPORT_EMAIL пуст) — письма не отправляются"]
    lines = [
        f"✓ Отчёт на почту: раз в месяц на {', '.join(recipients)} — одно письмо с CSV-файлом "
        "по каждому чату (отправка — Gmail API, запуск — cron: python reports.py)",
    ]
    if settings.gmail_sender:
        lines.append(f"  отправитель: {settings.gmail_sender}")
    else:
        lines.append("  отправитель: аккаунт, выдавший GMAIL_REFRESH_TOKEN")
    return lines


def build_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки для запуска по cron."""
    parser = argparse.ArgumentParser(
        prog="reports.py",
        description="Ежемесячный отчёт по долгам на почту (Gmail API): запускается по cron.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="собрать письмо и напечатать его в консоли, ничего не отправляя")
    parser.add_argument("--force", action="store_true",
                        help="отправить, даже если за этот месяц отчёт уже уходил")
    parser.add_argument("--to", metavar="EMAIL",
                        help="отправить на этот адрес вместо REPORT_EMAIL (разовая проверка)")
    parser.add_argument("--period", metavar="YYYY-MM",
                        help="период отчёта (по умолчанию — текущий месяц по Минску)")
    parser.add_argument("--check", action="store_true",
                        help="показать, настроен ли отчёт на почту, и выйти")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа для cron: собрать отчёт и отправить его, показав итог в консоли."""
    # Вывод настраиваем до разбора аргументов: иначе --help на Windows с cp1251
    # не напечатает русский текст (как в bot.py и webhook.py). Импорт внутри функции:
    # bot.py сам обращается сюда за проверкой настроек (`python bot.py --check`).
    from bot import configure_logging, configure_stdout

    configure_stdout()
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if args.check:
        for line in reports_check_lines(settings):
            print(line)
        return 0 if settings.reports_problem() is None else 1

    problem = settings.reports_problem()
    if problem:
        print("Ошибка:", problem, file=sys.stderr)
        print("  Настройки отчёта: см. .env.example, блок «Отчёт на почту».", file=sys.stderr)
        return 1
    db_problem = settings.database_problem()
    if db_problem:
        print("Ошибка: без настроек базы отчёт не собрать —", db_problem, file=sys.stderr)
        print("  Проверить всё: python bot.py --check", file=sys.stderr)
        return 1
    try:
        storage = storage_from_settings(settings)
        run = send_monthly_reports(settings, storage, period=args.period, force=args.force,
                                   to=args.to or "", dry_run=args.dry_run)
    except (StorageError, MailError) as exc:
        print("Ошибка:", exc, file=sys.stderr)
        return 1

    title = period_title(run.period)
    if args.dry_run:
        print(f"Письмо за {title} — черновик, ничего не отправлено")
        print(f"  кому: {', '.join(settings.report_recipients)}")
        print(f"  тема: {run.subject}")
        print(f"  чатов в письме: {len(run.chats)}")
        print(f"  файлов во вложении: {len(run.files)}")
        for name in run.files:
            print(f"    • {name}")
        print("-" * 60)
        print(run.body)
        return 0
    if run.delivered:
        print(f"✓ Отчёт за {title} отправлен: {', '.join(run.sent)}")
        print(f"  тема: {run.subject}, чатов в письме: {len(run.chats)}, "
              f"файлов во вложении: {len(run.files)}")
        for name in run.files:
            print(f"    • {name}")
        if run.message_id:
            print(f"  id сообщения в Gmail: {run.message_id}")
        return 0

    print("Отчёт не отправлен:", run.reason or "неизвестная причина")
    for issue in run.problems:
        print("⚠", issue)
    print("  Проверить настройки: python reports.py --check, показать письмо: --dry-run")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
