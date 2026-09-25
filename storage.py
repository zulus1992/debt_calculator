# -*- coding: utf-8 -*-
"""Хранилище долгов: Supabase через официальный SDK (supabase-py) + память для тестов."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Protocol, Sequence

from supabase import Client, ClientOptions, PostgrestAPIError, SupabaseException, create_client

DEFAULT_CURRENCY = "BYN"
# Курс хранится целым числом (bigint/int8): rate = курс × RATE_SCALE.
# Так база не «плывёт» на дробных числах, а точность (8 знаков) сохраняется полностью.
RATE_SCALE = 100_000_000
RATE_DIGITS = 8


class StorageError(RuntimeError):
    """Ошибка обращения к хранилищу."""


# Коды ошибок PostgREST, которые понятнее объяснить словами, а не показывать как есть:
# PGRST205 — таблицы нет в схеме; PGRST301/42501 — проблема с ключом или правами (RLS).
TABLE_MISSING_CODES = ("PGRST205", "42P01")
KEY_PROBLEM_CODES = ("PGRST301", "42501")

# Ключи нового формата Supabase (sb_publishable_…/sb_secret_…) — не JWT: их передают только
# в заголовке apikey, а в `Authorization: Bearer` они не аутентифицируют запрос
# (Supabase → API keys → «Known limitations»; в supabase-js за это отвечает omitApiKeyAsBearer).
# supabase-py этого не учитывает и подставляет ключ в оба заголовка, поэтому для новых ключей
# Authorization убираем сами: иначе PostgREST выполняет запрос от роли anon, и запись
# отклоняет RLS (Postgres 42501 — «new row violates row-level security policy»).
NEW_API_KEY_PREFIXES = ("sb_publishable_", "sb_secret_")


def is_new_api_key(key: str) -> bool:
    """Ключ нового формата Supabase: он должен уходить только в заголовке apikey."""
    return str(key or "").strip().lower().startswith(NEW_API_KEY_PREFIXES)


def create_supabase_client(url: str, key: str, timeout: float = 30.0,
                           key_header: str = "apikey") -> Client:
    """Создаёт клиент официального SDK supabase-py.

    Таймаут задаётся для PostgREST: бот обращается только к нему, а по умолчанию SDK ждёт
    ответа 120 секунд — для ответа в Telegram это слишком долго.

    `key_header` определяет, в каких заголовках уходит ключ нового формата (sb_secret_…):
    «apikey» (по умолчанию) — только apikey, как требует Supabase и как делает supabase-js;
    «both» — оставить и Authorization: Bearer, как по умолчанию делает supabase-py (нужно на
    нестандартных шлюзах, где роль выбирают по Authorization). Значения те же, что в
    config.KEY_HEADER_MODES. Legacy-JWT всегда уходит в оба заголовка: роль service_role
    задаёт именно Authorization.
    """
    try:
        client = create_client(url, key, options=ClientOptions(postgrest_client_timeout=timeout))
    except SupabaseException as exc:
        raise StorageError(f"Не удалось создать клиент Supabase: {exc}") from exc
    if is_new_api_key(key) and str(key_header or "").lower() != "both":
        # PostgREST-клиент SDK собирает лениво (при первом table()) из options.headers,
        # поэтому правку заголовков достаточно сделать сразу после создания клиента.
        client.options.headers.pop("Authorization", None)
    return client


def probe_key_headers(url: str, key: str, *, table: str = "debts", timeout: float = 30.0,
                      client_factory: Any = None) -> list[tuple[str, str]]:
    """Пробует оба режима заголовков и возвращает (название режима, результат) — для --check.

    Так видно, почему база отвечает «permission denied for schema public» (42501): шлюз либо
    принимает ключ только в apikey, либо ему нужен ещё и Authorization. Проба читает одну
    строку из таблицы долгов и ничего не меняет.
    """
    make_client = client_factory or create_supabase_client
    results: list[tuple[str, str]] = []
    for title, key_header in (("только apikey", "apikey"), ("apikey + Authorization", "both")):
        try:
            client = make_client(url, key, timeout, key_header)
            response = client.table(table).select("id").limit(1).execute()
            results.append((title, f"база ответила, строк: {len(_rows(response))}"))
        except PostgrestAPIError as exc:
            results.append((title, f"{getattr(exc, 'code', '')}: {getattr(exc, 'message', exc)}"))
        except Exception as exc:  # noqa: BLE001 — диагностика не должна падать целиком
            results.append((title, f"не удалось: {exc}"))
    return results


def _api_error_message(exc: Exception) -> str:
    """Понятное описание ошибки PostgREST: нет таблицы, плохой ключ или код ошибки.

    SDK бросает PostgrestAPIError с полями от PostgREST (message/code/hint). Разбираться
    в них человеку не нужно, поэтому «нет таблицы» и «ключ/RLS» переводим в подсказки,
    а остальное показываем с кодом.

    Если SDK не смог разобрать тело ошибки (PostgREST вернул неполный JSON), в `code`
    оказывается HTTP-статус, а исходный ответ — в `details`: клеим всё в одну строку,
    чтобы 404 («нет таблицы») и 401/403 («ключ или RLS») ловились в любом случае.
    """
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    details = str(getattr(exc, "details", "") or "")
    blob = f"{code} {message} {details}".lower()

    if (code in ("404",) or code in TABLE_MISSING_CODES
            or "could not find the table" in blob or "does not exist" in blob):
        return (
            "Таблица не найдена: выполните db/schema.sql в Supabase → SQL Editor "
            f"(ответ PostgREST: {code or message})."
        )
    if ("permission denied for schema" in blob or "permission denied for table" in blob
            or "permission denied for relation" in blob):
        return (
            "У роли нет прав на данные: ключ принят, но Postgres отказал — "
            f"{code or 'ошибка'}: {message}. Нужны права (GRANT) для роли service_role, "
            "которой соответствует secret-ключ: выполните db/grants.sql в Supabase → "
            "SQL Editor. Проверить: python bot.py --check."
        )
    if (code in ("401", "403") or code in KEY_PROBLEM_CODES or "jwt" in blob
            or "api key" in blob or "permission denied" in blob or "row-level security" in blob):
        return (
            "Supabase отклонил ключ или доступ: нужен secret-ключ базы (sb_secret_…) или "
            "legacy service_role — с publishable/anon запрос уходит от роли anon, и запись "
            f"блокирует RLS. Ответ PostgREST: {code or 'без кода'} — {message}. "
            "Какой ключ используется, покажет: python bot.py --check."
        )
    return f"Supabase вернул ошибку {code or 'без кода'}: {message}"


@dataclass
class Debt:
    """Одна запись: кто, кому, сколько и в какой валюте."""

    chat_id: int
    from_name: str
    to_name: str
    currency: str
    amount: float
    kind: str = "debt"                 # debt — долг, repayment — возврат («Леша вернул Диме 3»)
    from_user_id: int | None = None    # Telegram user id должника (если узнан среди участников чата)
    to_user_id: int | None = None      # Telegram user id кредитора
    raw_text: str | None = None
    created_at: str | None = None
    id: int | None = None
    group_id: str | None = None         # общий счёт: у долей одного платежа один и тот же group_id

    @property
    def is_repayment(self) -> bool:
        """Это возврат долга, а не новый долг."""
        return str(self.kind or "debt").lower() == "repayment"

    @property
    def is_expense(self) -> bool:
        """Это доля общего счёта: «Дима заплатил 10 за всех»."""
        return str(self.kind or "debt").lower() == "expense"

    def pretty(self) -> str:
        """Человекочитаемое описание записи."""
        if self.is_repayment:
            return f"↩️ {self.from_name} вернул {self.to_name}: {self.amount:.2f} {self.currency}"
        if self.is_expense:
            return (f"🧾 доля общего счёта: {self.from_name} → {self.to_name}: "
                    f"{self.amount:.2f} {self.currency}")
        return f"{self.from_name} → {self.to_name}: {self.amount:.2f} {self.currency}"


def _to_int(value: Any) -> int | None:
    """Приводит значение из БД к int (None, если пусто или не число)."""
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _row_to_debt(row: dict[str, Any]) -> Debt:
    """Преобразует строку из БД в Debt."""
    return Debt(
        id=row.get("id"),
        chat_id=int(row.get("chat_id") or 0),
        from_name=str(row.get("from_name") or ""),
        to_name=str(row.get("to_name") or ""),
        currency=str(row.get("currency") or DEFAULT_CURRENCY).upper(),
        amount=float(row.get("amount") or 0),
        kind=str(row.get("kind") or "debt").lower(),
        from_user_id=_to_int(row.get("from_user_id")),
        to_user_id=_to_int(row.get("to_user_id")),
        raw_text=row.get("raw_text"),
        created_at=str(row.get("created_at") or "") or None,
        group_id=str(row.get("group_id") or "") or None,
    )


def _rows(response: Any) -> list[dict[str, Any]]:
    """Строки ответа PostgREST: SDK кладёт их в поле data (пустой ответ — None)."""
    return list(getattr(response, "data", None) or [])


@dataclass
class RatePoint:
    """Курс валюты на дату: сколько базовой валюты стоит 1 единица currency."""

    rate_date: str                     # ISO-дата: 2026-09-21
    base: str                          # базовая валюта, к которой приведён курс (обычно BYN)
    currency: str                      # валюта, курс которой храним
    rate: Decimal                      # 1 USD = 3.25 BYN → rate = Decimal("3.25") при base = BYN
    source: str | None = None          # откуда курс: exchangerate-api.com


def scale_rate(value: Any) -> int:
    """Курс → целое для базы: 3.2531 → 325310000 (умножение на RATE_SCALE)."""
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((number * RATE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))


def unscale_rate(value: Any) -> Decimal:
    """Целое из базы → курс: 325310000 → Decimal("3.2531")."""
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip()) / RATE_SCALE
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(0)


def _row_to_rate(row: dict[str, Any]) -> RatePoint:
    """Преобразует строку currency_rates в RatePoint."""
    return RatePoint(
        rate_date=str(row.get("rate_date") or "")[:10],
        base=str(row.get("base") or DEFAULT_CURRENCY).upper(),
        currency=str(row.get("currency") or "").upper(),
        rate=unscale_rate(row.get("rate")),
        source=str(row.get("source") or "") or None,
    )


def _chunks(rows: Sequence[Any], size: int) -> list[Sequence[Any]]:
    """Делит список на порции: PostgREST спокойнее с вставкой по частям."""
    return [rows[index:index + size] for index in range(0, len(rows), size)]


@dataclass
class ChatMember:
    """Участник чата: по нему бот понимает, кто такой «Лешак» или «Дима»."""

    chat_id: int
    user_id: int
    username: str = ""                 # без @
    display_name: str = ""             # «Леша Козлов»
    aliases: list[str] = field(default_factory=list)   # «Леша», «Лёха» — подсказки для сопоставления
    last_seen: str | None = None
    is_registered: bool = False         # отметка /reg: записи ведутся только на зарегистрированных

    @property
    def label(self) -> str:
        """Как показывать участника в ответах: «Леша Козлов (@kozlovAlex)»."""
        name = self.display_name.strip()
        if self.username:
            return f"{name} (@{self.username})" if name else f"@{self.username}"
        return name or (f"id{self.user_id}" if self.user_id else "?")


def _row_to_member(row: dict[str, Any]) -> ChatMember:
    """Преобразует строку chat_members в ChatMember."""
    aliases = row.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [part.strip() for part in aliases.split(",") if part.strip()]
    return ChatMember(
        chat_id=int(row.get("chat_id") or 0),
        user_id=int(row.get("user_id") or 0),
        username=str(row.get("username") or "").lstrip("@"),
        display_name=str(row.get("display_name") or ""),
        aliases=[str(alias) for alias in aliases],
        last_seen=str(row.get("last_seen") or "") or None,
        is_registered=bool(row.get("is_registered")),
    )


class Storage(Protocol):
    """Интерфейс хранилища долгов, участников чата и настроек."""

    def add_debt(self, chat_id: int, from_name: str, to_name: str, currency: str,
                 amount: float, raw_text: str | None = None,
                 kind: str = "debt", from_user_id: int | None = None,
                 to_user_id: int | None = None,
                 group_id: str | None = None) -> Debt: ...

    def add_debts(self, chat_id: int, records: Sequence[Mapping[str, Any]]) -> list[Debt]: ...

    def list_debts(self, chat_id: int) -> list[Debt]: ...

    def delete_debts(self, chat_id: int) -> int: ...

    def delete_last_debt(self, chat_id: int) -> Debt | None: ...

    def delete_group(self, chat_id: int, group_id: str) -> int: ...

    def remember_member(self, member: ChatMember) -> None: ...

    def register_member(self, member: ChatMember) -> None: ...

    def list_members(self, chat_id: int) -> list[ChatMember]: ...

    def chat_authorized(self, chat_id: int) -> bool: ...

    def set_chat_authorized(self, chat_id: int, value: bool = True) -> None: ...

    def save_rates(self, points: Sequence[Mapping[str, Any]]) -> int: ...

    def rates_since(self, base: str, date_from: str) -> list[RatePoint]: ...

    def has_rates(self, rate_date: str, base: str) -> bool: ...

    def get_default_currency(self, chat_id: int, fallback: str = DEFAULT_CURRENCY) -> str: ...

    def set_default_currency(self, chat_id: int, currency: str) -> None: ...

    def get_state(self, key: str, default: str | None = None) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...


class SupabaseStorage:
    """Supabase через официальный SDK (supabase-py); под капотом — тот же PostgREST.

    Ключ доступа — secret-ключ базы (sb_secret_…) или legacy service_role: SDK сам
    подставляет его в заголовки каждого запроса, а бот работает на сервере, а не в браузере.
    Имена таблиц приходят из настроек, по умолчанию — как в db/schema.sql.
    В upsert-ах передаём returning="minimal" — это значение контракта PostgREST
    («Prefer: return=minimal»): строки в ответе нужны только при вставке долгов.
    """

    def __init__(
        self,
        url: str,
        key: str,
        *,
        debts_table: str = "debts",
        settings_table: str = "bot_settings",
        state_table: str = "bot_state",
        members_table: str = "chat_members",
        rates_table: str = "currency_rates",
        timeout: float = 30.0,
        key_header: str = "apikey",
        client: Client | None = None,
    ) -> None:
        """Готовит клиент SDK; при передаче готового `client` url и ключ не нужны (тесты)."""
        url = str(url or "").strip().rstrip("/")
        key = str(key or "").strip().strip("'\"").strip()
        if client is not None:
            self._client = client
        else:
            if not url or not key:
                raise StorageError(
                    "Нужны SUPABASE_URL и ключ базы: SUPABASE_SECRET_KEY (sb_secret_…) "
                    "или legacy SUPABASE_SERVICE_KEY (service_role)."
                )
            self._client = create_supabase_client(url, key, timeout, key_header)
        self._debts_table = debts_table
        self._settings_table = settings_table
        self._state_table = state_table
        self._members_table = members_table
        self._rates_table = rates_table
        self._timeout = timeout

    @property
    def tables(self) -> tuple[str, str]:
        """Имена таблиц (долги, настройки)."""
        return self._debts_table, self._settings_table

    def _table(self, name: str) -> Any:
        """Билдер таблицы PostgREST: дальше цепочка select/insert/upsert/delete и execute()."""
        return self._client.table(name)

    def _execute(self, query: Any) -> Any:
        """Выполняет запрос SDK и переводит ошибки PostgREST в StorageError.

        Сообщения остаются человеческими: 401/403 («ключ или RLS») и «нет таблицы» —
        самые частые проблемы при настройке, и по тексту ошибки должно быть понятно,
        что править в переменных окружения или в базе.
        """
        try:
            return query.execute()
        except PostgrestAPIError as exc:
            raise StorageError(_api_error_message(exc)) from exc

    def add_debt(self, chat_id: int, from_name: str, to_name: str, currency: str,
                 amount: float, raw_text: str | None = None,
                 kind: str = "debt", from_user_id: int | None = None,
                 to_user_id: int | None = None,
                 group_id: str | None = None) -> Debt:
        """Сохраняет запись (долг, возврат или долю общего счёта) и возвращает её."""
        kind = str(kind or "debt").lower()
        response = self._execute(self._table(self._debts_table).insert(
            self._debt_body(
                chat_id, from_name, to_name, currency, amount, kind=kind,
                from_user_id=from_user_id, to_user_id=to_user_id,
                raw_text=raw_text, group_id=group_id,
            ),
        ))
        rows = _rows(response)
        if rows:
            return _row_to_debt(rows[0])
        return Debt(
            chat_id=chat_id, from_name=from_name, to_name=to_name,
            currency=currency.upper(), amount=round(float(amount), 2), kind=kind,
            from_user_id=from_user_id, to_user_id=to_user_id, raw_text=raw_text,
            group_id=group_id,
        )

    def add_debts(self, chat_id: int, records: Sequence[Mapping[str, Any]]) -> list[Debt]:
        """Сохраняет несколько записей одним запросом — доли общего счёта.

        Общий счёт («Дима заплатил 10 за всех») — это одна операция, поэтому все доли
        пишутся одним запросом: они появляются в базе одновременно и с одинаковым
        group_id, а /undo убирает счёт целиком, а не одну строку.
        """
        if not records:
            return []
        payload = [
            self._debt_body(
                chat_id,
                str(record.get("from_name") or ""),
                str(record.get("to_name") or ""),
                str(record.get("currency") or DEFAULT_CURRENCY),
                float(record.get("amount") or 0),
                kind=str(record.get("kind") or "debt"),
                from_user_id=_to_int(record.get("from_user_id")),
                to_user_id=_to_int(record.get("to_user_id")),
                raw_text=record.get("raw_text"),
                group_id=record.get("group_id"),
            )
            for record in records
        ]
        response = self._execute(self._table(self._debts_table).insert(payload))
        return [_row_to_debt(row) for row in _rows(response)]

    @staticmethod
    def _debt_body(chat_id: int, from_name: str, to_name: str, currency: str, amount: float,
                   *, kind: str = "debt", from_user_id: int | None = None,
                   to_user_id: int | None = None, raw_text: str | None = None,
                   group_id: str | None = None) -> dict[str, Any]:
        """Тело записи для PostgREST: одинаковое для одиночной и групповой вставки."""
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "from_name": from_name,
            "to_name": to_name,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "currency": str(currency or DEFAULT_CURRENCY).upper(),
            "amount": round(float(amount), 2),
            "kind": str(kind or "debt").lower(),
            "raw_text": raw_text,
        }
        if group_id:
            body["group_id"] = group_id
        return body

    def list_debts(self, chat_id: int) -> list[Debt]:
        """Все долги чата в порядке добавления."""
        query = (
            self._table(self._debts_table)
            .select("*")
            .eq("chat_id", chat_id)
            .order("created_at")
        )
        return [_row_to_debt(row) for row in _rows(self._execute(query))]

    def delete_debts(self, chat_id: int) -> int:
        """Удаляет все долги чата, возвращает число удалённых записей."""
        query = self._table(self._debts_table).delete().eq("chat_id", chat_id)
        return len(_rows(self._execute(query)))

    def delete_last_debt(self, chat_id: int) -> Debt | None:
        """Удаляет последнюю запись чата (команда /undo) и возвращает её.

        Последняя — по времени создания, а при равных метках по id: сначала читаем строку,
        потом удаляем именно её, чтобы в ответе показать, что именно убрали.
        """
        query = (
            self._table(self._debts_table)
            .select("*")
            .eq("chat_id", chat_id)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .limit(1)
        )
        rows = _rows(self._execute(query))
        if not rows:
            return None
        debt = _row_to_debt(rows[0])
        if debt.id is None:
            return None
        self._execute(
            self._table(self._debts_table).delete().eq("id", debt.id).eq("chat_id", chat_id)
        )
        return debt

    def delete_group(self, chat_id: int, group_id: str) -> int:
        """Удаляет оставшиеся доли одного общего счёта (группа записей по group_id)."""
        if not group_id:
            return 0
        query = (
            self._table(self._debts_table).delete()
            .eq("chat_id", chat_id).eq("group_id", group_id)
        )
        return len(_rows(self._execute(query)))

    def remember_member(self, member: ChatMember) -> None:
        """Запоминает участника чата (upsert по паре chat_id + user_id).

        Это автообучение по автору сообщения: отметку /reg и уже собранные алиасы
        такие записи не трогают — иначе каждый ответ бота стирал бы регистрацию.
        """
        self._execute(self._table(self._members_table).upsert(
            self._member_body(member),
            on_conflict="chat_id,user_id",
            returning="minimal",
        ))

    def register_member(self, member: ChatMember) -> None:
        """Сохраняет участника как зарегистрированного: команда /reg с его именами."""
        payload = self._member_body(member)
        payload["aliases"] = list(member.aliases)
        payload["is_registered"] = True
        self._execute(self._table(self._members_table).upsert(
            payload,
            on_conflict="chat_id,user_id",
            returning="minimal",
        ))

    @staticmethod
    def _member_body(member: ChatMember) -> dict[str, Any]:
        """Тело участника для PostgREST.

        Алиасы добавляются, только если они есть: пустой список при upsert затёр бы
        имена, которые человек уже указал через /reg.
        """
        body: dict[str, Any] = {
            "chat_id": member.chat_id,
            "user_id": member.user_id,
            "username": member.username or None,
            "display_name": member.display_name,
            "last_seen": member.last_seen or datetime.now(timezone.utc).isoformat(),
        }
        if member.aliases:
            body["aliases"] = list(member.aliases)
        return body

    def list_members(self, chat_id: int) -> list[ChatMember]:
        """Участники чата, которых бот успел запомнить (для сопоставления имён)."""
        query = (
            self._table(self._members_table)
            .select("chat_id,user_id,username,display_name,aliases,last_seen,is_registered")
            .eq("chat_id", chat_id)
            .order("display_name")
            .limit(200)
        )
        return [_row_to_member(row) for row in _rows(self._execute(query))]

    def get_default_currency(self, chat_id: int, fallback: str = DEFAULT_CURRENCY) -> str:
        """Валюта по умолчанию для чата."""
        query = (
            self._table(self._settings_table)
            .select("default_currency")
            .eq("chat_id", chat_id)
            .limit(1)
        )
        rows = _rows(self._execute(query))
        if rows and rows[0].get("default_currency"):
            return str(rows[0]["default_currency"]).upper()
        return (fallback or DEFAULT_CURRENCY).upper()

    def set_default_currency(self, chat_id: int, currency: str) -> None:
        """Сохраняет валюту по умолчанию для чата (upsert по chat_id)."""
        self._execute(self._table(self._settings_table).upsert(
            {"chat_id": chat_id, "default_currency": currency.upper()},
            on_conflict="chat_id",
            returning="minimal",
        ))

    def chat_authorized(self, chat_id: int) -> bool:
        """Работает ли бот в этом чате (чат подтвердил пароль)."""
        query = (
            self._table(self._settings_table)
            .select("is_authorized")
            .eq("chat_id", chat_id)
            .limit(1)
        )
        rows = _rows(self._execute(query))
        return bool(rows and rows[0].get("is_authorized"))

    def set_chat_authorized(self, chat_id: int, value: bool = True) -> None:
        """Запоминает, что чат ввёл верный пароль (или сбрасывает доступ)."""
        self._execute(self._table(self._settings_table).upsert(
            {"chat_id": chat_id, "is_authorized": bool(value)},
            on_conflict="chat_id",
            returning="minimal",
        ))

    def save_rates(self, points: Sequence[Mapping[str, Any]]) -> int:
        """Сохраняет курсы валют (upsert по дате, базовой и целевой валюте).

        Курс кладём целым (курс × RATE_SCALE) — колонка rate объявлена как bigint.
        """
        rows = [
            {
                "rate_date": str(point.get("rate_date") or "")[:10],
                "base": str(point.get("base") or DEFAULT_CURRENCY).upper(),
                "currency": str(point.get("currency") or "").upper(),
                "rate": scale_rate(point.get("rate") or 0),
                "source": point.get("source"),
            }
            for point in points
            if str(point.get("currency") or "").strip()
        ]
        for chunk in _chunks(rows, 500):
            self._execute(self._table(self._rates_table).upsert(
                list(chunk),
                on_conflict="rate_date,base,currency",
                returning="minimal",
            ))
        return len(rows)

    def rates_since(self, base: str, date_from: str) -> list[RatePoint]:
        """Курсы базовой валюты с указанной даты включительно (по возрастанию даты)."""
        query = (
            self._table(self._rates_table)
            .select("*")
            .eq("base", str(base or DEFAULT_CURRENCY).upper())
            .gte("rate_date", date_from)
            .order("rate_date")
            .limit(5000)
        )
        return [_row_to_rate(row) for row in _rows(self._execute(query))]

    def has_rates(self, rate_date: str, base: str) -> bool:
        """Есть ли в базе курсы на эту дату (чтобы не дёргать API дважды в день)."""
        query = (
            self._table(self._rates_table)
            .select("currency")
            .eq("base", str(base or DEFAULT_CURRENCY).upper())
            .eq("rate_date", rate_date)
            .limit(1)
        )
        return bool(_rows(self._execute(query)))

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Читает служебное значение (например last_update_id) из bot_state."""
        query = self._table(self._state_table).select("value").eq("key", key).limit(1)
        rows = _rows(self._execute(query))
        if rows and rows[0].get("value") is not None:
            return str(rows[0]["value"])
        return default

    def set_state(self, key: str, value: str) -> None:
        """Записывает служебное значение (upsert по key)."""
        self._execute(self._table(self._state_table).upsert(
            {"key": key, "value": str(value)},
            on_conflict="key",
            returning="minimal",
        ))

    def check_write_access(self) -> str:
        """Проверяет, разрешает ли база запись этим ключом; "" — всё в порядке.

        Делает настоящий PATCH по фильтру, который не совпадёт ни с одной строкой (имя
        state-записи со случайным суффиксом), — данные не меняются, но видно, пускает ли
        база запись. Так ловится частая ошибка настройки: в переменных окружения публичный
        ключ (publishable/anon) вместо secret — PostgREST отвечает 42501, и бот не смог бы
        записать ни один долг.
        """
        probe_key = f"__check_{uuid.uuid4().hex[:8]}"
        try:
            self._execute(
                self._table(self._state_table)
                .update({"value": "check"})
                .eq("key", probe_key)
            )
        except StorageError as exc:
            return str(exc)
        return ""


@dataclass
class InMemoryStorage:
    """Хранилище в памяти: тесты и режим demo без Supabase."""

    default_currency: str = DEFAULT_CURRENCY
    debts: list[Debt] = field(default_factory=list)
    currencies: dict[int, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)
    members: dict[tuple[int, int], ChatMember] = field(default_factory=dict)
    rates: list[RatePoint] = field(default_factory=list)
    authorized: set[int] = field(default_factory=set)
    # Дата записей для тестов и демо: по ней /d ищет курс на «дату сообщения».
    default_created_at: str = "1970-01-01T00:00:00+00:00"
    _next_id: int = 1

    def add_debt(self, chat_id: int, from_name: str, to_name: str, currency: str,
                 amount: float, raw_text: str | None = None,
                 kind: str = "debt", from_user_id: int | None = None,
                 to_user_id: int | None = None,
                 group_id: str | None = None,
                 created_at: str | None = None) -> Debt:
        """Добавляет запись (долг, возврат или долю общего счёта) в память."""
        debt = Debt(
            id=self._next_id,
            chat_id=chat_id,
            from_name=from_name,
            to_name=to_name,
            currency=currency.upper(),
            amount=round(float(amount), 2),
            kind=str(kind or "debt").lower(),
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            raw_text=raw_text,
            created_at=created_at or self.default_created_at,
            group_id=group_id,
        )
        self._next_id += 1
        self.debts.append(debt)
        return debt

    def add_debts(self, chat_id: int, records: Sequence[Mapping[str, Any]]) -> list[Debt]:
        """Добавляет несколько записей — доли общего счёта."""
        return [
            self.add_debt(
                chat_id=chat_id,
                from_name=str(record.get("from_name") or ""),
                to_name=str(record.get("to_name") or ""),
                currency=str(record.get("currency") or self.default_currency),
                amount=float(record.get("amount") or 0),
                raw_text=record.get("raw_text"),
                kind=str(record.get("kind") or "debt"),
                from_user_id=_to_int(record.get("from_user_id")),
                to_user_id=_to_int(record.get("to_user_id")),
                group_id=record.get("group_id"),
                created_at=record.get("created_at"),
            )
            for record in records
        ]

    def list_debts(self, chat_id: int) -> list[Debt]:
        """Долги конкретного чата."""
        return [debt for debt in self.debts if debt.chat_id == chat_id]

    def delete_debts(self, chat_id: int) -> int:
        """Удаляет долги чата."""
        before = len(self.debts)
        self.debts = [debt for debt in self.debts if debt.chat_id != chat_id]
        return before - len(self.debts)

    def delete_last_debt(self, chat_id: int) -> Debt | None:
        """Удаляет последнюю добавленную запись чата (команда /undo)."""
        for index in range(len(self.debts) - 1, -1, -1):
            if self.debts[index].chat_id == chat_id:
                return self.debts.pop(index)
        return None

    def delete_group(self, chat_id: int, group_id: str) -> int:
        """Удаляет оставшиеся доли одного общего счёта (группа записей по group_id)."""
        if not group_id:
            return 0
        before = len(self.debts)
        self.debts = [
            debt for debt in self.debts
            if not (debt.chat_id == chat_id and debt.group_id == group_id)
        ]
        return before - len(self.debts)

    def remember_member(self, member: ChatMember) -> None:
        """Запоминает участника чата в памяти, не сбрасывая регистрацию и алиасы."""
        current = self.members.get((member.chat_id, member.user_id))
        aliases = list(member.aliases)
        for alias in current.aliases if current else []:
            if alias not in aliases:
                aliases.append(alias)
        self.members[(member.chat_id, member.user_id)] = ChatMember(
            chat_id=member.chat_id,
            user_id=member.user_id,
            username=member.username or (current.username if current else ""),
            display_name=member.display_name or member.username
            or (current.display_name if current else "") or f"id{member.user_id}",
            aliases=aliases,
            last_seen=member.last_seen or "1970-01-01T00:00:00+00:00",
            is_registered=member.is_registered or bool(current and current.is_registered),
        )

    def register_member(self, member: ChatMember) -> None:
        """Сохраняет участника как зарегистрированного (/reg) вместе с его именами."""
        self.members[(member.chat_id, member.user_id)] = ChatMember(
            chat_id=member.chat_id,
            user_id=member.user_id,
            username=member.username,
            display_name=member.display_name or member.username or f"id{member.user_id}",
            aliases=list(member.aliases),
            last_seen=member.last_seen or "1970-01-01T00:00:00+00:00",
            is_registered=True,
        )

    def list_members(self, chat_id: int) -> list[ChatMember]:
        """Участники чата из памяти."""
        return [
            member for (member_chat, _), member in self.members.items()
            if member_chat == chat_id
        ]

    def get_default_currency(self, chat_id: int, fallback: str = DEFAULT_CURRENCY) -> str:
        """Валюта по умолчанию для чата."""
        return (self.currencies.get(chat_id) or fallback or self.default_currency).upper()

    def set_default_currency(self, chat_id: int, currency: str) -> None:
        """Запоминает валюту по умолчанию для чата."""
        self.currencies[chat_id] = currency.upper()

    def chat_authorized(self, chat_id: int) -> bool:
        """Работает ли бот в этом чате (чат подтвердил пароль)."""
        return int(chat_id) in self.authorized

    def set_chat_authorized(self, chat_id: int, value: bool = True) -> None:
        """Запоминает, что чат ввёл верный пароль (или сбрасывает доступ)."""
        if value:
            self.authorized.add(int(chat_id))
        else:
            self.authorized.discard(int(chat_id))

    def save_rates(self, points: Sequence[Mapping[str, Any]]) -> int:
        """Сохраняет курсы валют в памяти: ключ — дата + база + валюта (курс как в базе, целым)."""
        saved = 0
        for point in points:
            currency = str(point.get("currency") or "").strip().upper()
            if not currency:
                continue
            base = str(point.get("base") or DEFAULT_CURRENCY).upper()
            rate_date = str(point.get("rate_date") or "")[:10]
            fresh = RatePoint(
                rate_date=rate_date,
                base=base,
                currency=currency,
                rate=unscale_rate(scale_rate(point.get("rate") or 0)),
                source=str(point.get("source") or "") or None,
            )
            for index, existing in enumerate(self.rates):
                if (existing.rate_date, existing.base, existing.currency) == \
                        (rate_date, base, currency):
                    self.rates[index] = fresh
                    break
            else:
                self.rates.append(fresh)
            saved += 1
        return saved

    def rates_since(self, base: str, date_from: str) -> list[RatePoint]:
        """Курсы базовой валюты с указанной даты включительно."""
        upper = str(base or DEFAULT_CURRENCY).upper()
        found = [
            point for point in self.rates
            if point.base == upper and point.rate_date >= date_from
        ]
        return sorted(found, key=lambda point: (point.rate_date, point.currency))

    def has_rates(self, rate_date: str, base: str) -> bool:
        """Есть ли в памяти курсы на эту дату."""
        upper = str(base or DEFAULT_CURRENCY).upper()
        return any(point.base == upper and point.rate_date == rate_date for point in self.rates)

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Читает служебное значение из памяти."""
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        """Сохраняет служебное значение в память."""
        self.state[key] = str(value)
