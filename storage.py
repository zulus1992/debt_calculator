# -*- coding: utf-8 -*-
"""Хранилище долгов: Supabase через PostgREST + хранилище в памяти для тестов."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

import requests

DEFAULT_CURRENCY = "BYN"


class StorageError(RuntimeError):
    """Ошибка обращения к хранилищу."""


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

    def get_default_currency(self, chat_id: int, fallback: str = DEFAULT_CURRENCY) -> str: ...

    def set_default_currency(self, chat_id: int, currency: str) -> None: ...

    def get_state(self, key: str, default: str | None = None) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...


class SupabaseStorage:
    """Supabase через REST API PostgREST. Требуется ключ service_role (запуск на сервере)."""

    def __init__(
        self,
        url: str,
        key: str,
        *,
        debts_table: str = "debts",
        settings_table: str = "bot_settings",
        state_table: str = "bot_state",
        members_table: str = "chat_members",
        timeout: float = 30.0,
        session: Any = None,
    ) -> None:
        url = str(url or "").strip().rstrip("/")
        key = str(key or "").strip().strip("'\"").strip()
        if not url or not key:
            raise StorageError("Нужны SUPABASE_URL и SUPABASE_SERVICE_KEY.")
        self._rest = url + "/rest/v1"
        self._key = key
        self._debts_table = debts_table
        self._settings_table = settings_table
        self._state_table = state_table
        self._members_table = members_table
        self._timeout = timeout
        self._session = session or requests

    @property
    def tables(self) -> tuple[str, str]:
        """Имена таблиц (долги, настройки)."""
        return self._debts_table, self._settings_table

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        """Заголовки запроса к PostgREST."""
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: Any = None,
        prefer: str | None = None,
    ) -> Any:
        """Запрос к PostgREST с понятными сообщениями об ошибках."""
        try:
            response = self._session.request(
                method,
                f"{self._rest}/{path}",
                params=params,
                json=payload,
                headers=self._headers(prefer),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise StorageError(f"Supabase недоступен: {exc}") from exc

        if response.status_code in (401, 403):
            raise StorageError(
                "Supabase отклонил ключ (HTTP 401/403): нужен ключ service_role "
                "(с anon-ключом запись блокирует RLS)."
            )
        if response.status_code == 404:
            raise StorageError(
                "Таблица не найдена (HTTP 404): выполните db/schema.sql в Supabase → SQL Editor."
            )
        if response.status_code >= 400:
            raise StorageError(
                f"Supabase вернул HTTP {response.status_code}: {response.text[:200]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def add_debt(self, chat_id: int, from_name: str, to_name: str, currency: str,
                 amount: float, raw_text: str | None = None,
                 kind: str = "debt", from_user_id: int | None = None,
                 to_user_id: int | None = None,
                 group_id: str | None = None) -> Debt:
        """Сохраняет запись (долг, возврат или долю общего счёта) и возвращает её."""
        kind = str(kind or "debt").lower()
        rows = self._request(
            "POST",
            self._debts_table,
            payload=self._debt_body(
                chat_id, from_name, to_name, currency, amount, kind=kind,
                from_user_id=from_user_id, to_user_id=to_user_id,
                raw_text=raw_text, group_id=group_id,
            ),
            prefer="return=representation",
        )
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
        rows = self._request(
            "POST",
            self._debts_table,
            payload=payload,
            prefer="return=representation",
        )
        return [_row_to_debt(row) for row in rows or []]

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
        rows = self._request(
            "GET",
            self._debts_table,
            params={"chat_id": f"eq.{chat_id}", "select": "*", "order": "created_at.asc"},
        )
        return [_row_to_debt(row) for row in rows or []]

    def delete_debts(self, chat_id: int) -> int:
        """Удаляет все долги чата, возвращает число удалённых записей."""
        rows = self._request(
            "DELETE",
            self._debts_table,
            params={"chat_id": f"eq.{chat_id}"},
            prefer="return=representation",
        )
        return len(rows or [])

    def delete_last_debt(self, chat_id: int) -> Debt | None:
        """Удаляет последнюю запись чата (команда /undo) и возвращает её.

        Последняя — по времени создания, а при равных метках по id: сначала читаем строку,
        потом удаляем именно её, чтобы в ответе показать, что именно убрали.
        """
        rows = self._request(
            "GET",
            self._debts_table,
            params={
                "chat_id": f"eq.{chat_id}",
                "select": "*",
                "order": "created_at.desc,id.desc",
                "limit": 1,
            },
        )
        if not rows:
            return None
        debt = _row_to_debt(rows[0])
        if debt.id is None:
            return None
        self._request(
            "DELETE",
            self._debts_table,
            params={"id": f"eq.{debt.id}"},
            prefer="return=representation",
        )
        return debt

    def delete_group(self, chat_id: int, group_id: str) -> int:
        """Удаляет оставшиеся доли одного общего счёта (группа записей по group_id)."""
        if not group_id:
            return 0
        rows = self._request(
            "DELETE",
            self._debts_table,
            params={"chat_id": f"eq.{chat_id}", "group_id": f"eq.{group_id}"},
            prefer="return=representation",
        )
        return len(rows or [])

    def remember_member(self, member: ChatMember) -> None:
        """Запоминает участника чата (upsert по паре chat_id + user_id).

        Это автообучение по автору сообщения: отметку /reg и уже собранные алиасы
        такие записи не трогают — иначе каждый ответ бота стирал бы регистрацию.
        """
        self._request(
            "POST",
            self._members_table,
            params={"on_conflict": "chat_id,user_id"},
            payload=self._member_body(member),
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def register_member(self, member: ChatMember) -> None:
        """Сохраняет участника как зарегистрированного: команда /reg с его именами."""
        payload = self._member_body(member)
        payload["aliases"] = list(member.aliases)
        payload["is_registered"] = True
        self._request(
            "POST",
            self._members_table,
            params={"on_conflict": "chat_id,user_id"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )

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
        rows = self._request(
            "GET",
            self._members_table,
            params={
                "chat_id": f"eq.{chat_id}",
                "select": "chat_id,user_id,username,display_name,aliases,last_seen,is_registered",
                "order": "display_name.asc",
                "limit": 200,
            },
        )
        return [_row_to_member(row) for row in rows or []]

    def get_default_currency(self, chat_id: int, fallback: str = DEFAULT_CURRENCY) -> str:
        """Валюта по умолчанию для чата."""
        rows = self._request(
            "GET",
            self._settings_table,
            params={"chat_id": f"eq.{chat_id}", "select": "default_currency", "limit": 1},
        )
        if rows and rows[0].get("default_currency"):
            return str(rows[0]["default_currency"]).upper()
        return (fallback or DEFAULT_CURRENCY).upper()

    def set_default_currency(self, chat_id: int, currency: str) -> None:
        """Сохраняет валюту по умолчанию для чата (upsert по chat_id)."""
        self._request(
            "POST",
            self._settings_table,
            params={"on_conflict": "chat_id"},
            payload={"chat_id": chat_id, "default_currency": currency.upper()},
            prefer="resolution=merge-duplicates,return=representation",
        )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Читает служебное значение (например last_update_id) из bot_state."""
        rows = self._request(
            "GET",
            self._state_table,
            params={"key": f"eq.{key}", "select": "value", "limit": 1},
        )
        if rows and rows[0].get("value") is not None:
            return str(rows[0]["value"])
        return default

    def set_state(self, key: str, value: str) -> None:
        """Записывает служебное значение (upsert по key)."""
        self._request(
            "POST",
            self._state_table,
            params={"on_conflict": "key"},
            payload={"key": key, "value": str(value)},
            prefer="resolution=merge-duplicates,return=representation",
        )


@dataclass
class InMemoryStorage:
    """Хранилище в памяти: тесты и режим demo без Supabase."""

    default_currency: str = DEFAULT_CURRENCY
    debts: list[Debt] = field(default_factory=list)
    currencies: dict[int, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)
    members: dict[tuple[int, int], ChatMember] = field(default_factory=dict)
    _next_id: int = 1

    def add_debt(self, chat_id: int, from_name: str, to_name: str, currency: str,
                 amount: float, raw_text: str | None = None,
                 kind: str = "debt", from_user_id: int | None = None,
                 to_user_id: int | None = None,
                 group_id: str | None = None) -> Debt:
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
            created_at="1970-01-01T00:00:00+00:00",
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

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Читает служебное значение из памяти."""
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        """Сохраняет служебное значение в память."""
        self.state[key] = str(value)
