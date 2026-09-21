# -*- coding: utf-8 -*-
"""Хранилище долгов: Supabase через PostgREST + хранилище в памяти для тестов."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

DEFAULT_CURRENCY = "BYN"


class StorageError(RuntimeError):
    """Ошибка обращения к хранилищу."""


@dataclass
class Debt:
    """Один долг: кто, кому, сколько и в какой валюте."""

    chat_id: int
    from_name: str
    to_name: str
    currency: str
    amount: float
    raw_text: str | None = None
    created_at: str | None = None
    id: int | None = None

    def pretty(self) -> str:
        """Человекочитаемое описание долга."""
        return f"{self.from_name} → {self.to_name}: {self.amount:.2f} {self.currency}"


def _row_to_debt(row: dict[str, Any]) -> Debt:
    """Преобразует строку из БД в Debt."""
    return Debt(
        id=row.get("id"),
        chat_id=int(row.get("chat_id") or 0),
        from_name=str(row.get("from_name") or ""),
        to_name=str(row.get("to_name") or ""),
        currency=str(row.get("currency") or DEFAULT_CURRENCY).upper(),
        amount=float(row.get("amount") or 0),
        raw_text=row.get("raw_text"),
        created_at=str(row.get("created_at") or "") or None,
    )


class Storage(Protocol):
    """Интерфейс хранилища долгов и настроек чата."""

    def add_debt(self, chat_id: int, from_name: str, to_name: str, currency: str,
                 amount: float, raw_text: str | None = None) -> Debt: ...

    def list_debts(self, chat_id: int) -> list[Debt]: ...

    def delete_debts(self, chat_id: int) -> int: ...

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
                 amount: float, raw_text: str | None = None) -> Debt:
        """Сохраняет долг и возвращает записанную строку."""
        rows = self._request(
            "POST",
            self._debts_table,
            payload={
                "chat_id": chat_id,
                "from_name": from_name,
                "to_name": to_name,
                "currency": currency.upper(),
                "amount": round(float(amount), 2),
                "raw_text": raw_text,
            },
            prefer="return=representation",
        )
        if rows:
            return _row_to_debt(rows[0])
        return Debt(
            chat_id=chat_id, from_name=from_name, to_name=to_name,
            currency=currency.upper(), amount=round(float(amount), 2), raw_text=raw_text,
        )

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
    _next_id: int = 1

    def add_debt(self, chat_id: int, from_name: str, to_name: str, currency: str,
                 amount: float, raw_text: str | None = None) -> Debt:
        """Добавляет долг в память."""
        debt = Debt(
            id=self._next_id,
            chat_id=chat_id,
            from_name=from_name,
            to_name=to_name,
            currency=currency.upper(),
            amount=round(float(amount), 2),
            raw_text=raw_text,
            created_at="1970-01-01T00:00:00+00:00",
        )
        self._next_id += 1
        self.debts.append(debt)
        return debt

    def list_debts(self, chat_id: int) -> list[Debt]:
        """Долги конкретного чата."""
        return [debt for debt in self.debts if debt.chat_id == chat_id]

    def delete_debts(self, chat_id: int) -> int:
        """Удаляет долги чата."""
        before = len(self.debts)
        self.debts = [debt for debt in self.debts if debt.chat_id != chat_id]
        return before - len(self.debts)

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
