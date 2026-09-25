# -*- coding: utf-8 -*-
"""Минимальная обёртка над Telegram Bot API (только requests, без внешних SDK)."""

from __future__ import annotations

from typing import Any

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024   # лимит подписи к документу в Bot API
TEXT_DOCUMENT_TYPE = "text/plain; charset=utf-8"
CSV_DOCUMENT_TYPE = "text/csv; charset=utf-8"   # выгрузка /export: копия таблицы debts


class TelegramError(RuntimeError):
    """Ошибка обращения к Telegram Bot API."""


class TelegramBot:
    """Клиент Bot API: длинный опрос (long polling) и отправка сообщений."""

    def __init__(self, token: str, *, timeout: float = 30.0, session: Any = None) -> None:
        token = str(token or "").strip().strip("'\"").strip()
        if not token:
            raise TelegramError("Не задан TELEGRAM_BOT_TOKEN.")
        self._token = token
        self._timeout = timeout
        self._session = session or requests

    def call(self, method: str, *, http_timeout: float | None = None, **payload: Any) -> Any:
        """Вызывает метод Bot API и возвращает поле result.

        HTTP-таймаут вынесен в отдельный параметр http_timeout: у самого Telegram
        тоже есть параметр timeout (ожидание апдейтов), и имена не должны пересекаться.
        """
        return self._request(method, json=payload, http_timeout=http_timeout)

    def _request(self, method: str, *, json: Any = None, data: Any = None, files: Any = None,
                 http_timeout: float | None = None) -> Any:
        """Вызывает метод Bot API и разбирает ответ.

        Обычные методы уходят JSON-ом (`json`), отправка файлов — multipart/form-data
        (`data` + `files`): так работает sendDocument.
        """
        url = TELEGRAM_API.format(token=self._token, method=method)
        try:
            if files is None:
                response = self._session.post(url, json=json, timeout=http_timeout or self._timeout)
            else:
                response = self._session.post(url, data=data, files=files,
                                              timeout=http_timeout or self._timeout)
        except requests.RequestException as exc:
            raise TelegramError(f"Telegram недоступен: {exc}") from exc

        status = response.status_code
        if status == 401:
            raise TelegramError("Telegram отклонил токен бота (HTTP 401): проверьте TELEGRAM_BOT_TOKEN.")
        if status == 409:
            raise TelegramError(
                "Конфликт getUpdates (HTTP 409): этот бот уже запущен где-то ещё "
                "или работает webhook. Остановите второй экземпляр."
            )
        if status == 429:
            raise TelegramError("Слишком много запросов к Telegram (HTTP 429): подождите пару минут.")
        if status != 200:
            raise TelegramError(f"Telegram вернул HTTP {status}: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError("Ответ Telegram не является корректным JSON.") from exc
        if not data.get("ok"):
            raise TelegramError(f"Telegram вернул ошибку: {data.get('description') or data}")
        return data.get("result")

    def get_me(self) -> dict[str, Any]:
        """Данные бота: проверка токена и имени."""
        return dict(self.call("getMe") or {})

    def get_updates(
        self,
        offset: int | None = None,
        *,
        poll_timeout: int = 30,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Забирает новые апдейты методом длинного опроса (long polling)."""
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "limit": limit,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        # HTTP-таймаут должен быть больше времени ожидания на стороне Telegram
        result = self.call("getUpdates", http_timeout=max(self._timeout, poll_timeout + 10), **payload)
        return list(result or [])

    def set_webhook(
        self,
        url: str,
        *,
        secret_token: str | None = None,
        drop_pending_updates: bool = False,
        allowed_updates: tuple[str, ...] = ("message",),
        max_connections: int | None = None,
    ) -> bool:
        """Переводит бота на вебхук: Telegram сам присылает апдейты на url.

        url — только HTTPS (порты 443/80/88/8443) и доступен из интернета.
        secret_token Telegram присылает обратно в заголовке
        X-Telegram-Bot-Api-Secret-Token — по нему эндпоинт отличает Telegram
        от посторонних запросов.
        """
        payload: dict[str, Any] = {
            "url": url,
            "drop_pending_updates": drop_pending_updates,
            "allowed_updates": list(allowed_updates),
        }
        if secret_token:
            payload["secret_token"] = secret_token
        if max_connections is not None:
            payload["max_connections"] = max_connections
        return bool(self.call("setWebhook", **payload))

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        """Убирает вебхук — бот снова готов к long polling."""
        return bool(self.call("deleteWebhook", drop_pending_updates=drop_pending_updates))

    def get_webhook_info(self) -> dict[str, Any]:
        """Состояние вебхука: url, pending_update_count, последняя ошибка доставки."""
        return dict(self.call("getWebhookInfo") or {})

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> list[dict[str, Any]]:
        """Отправляет текст, разбивая его на части по лимиту Telegram."""
        sent: list[dict[str, Any]] = []
        for chunk in split_message(text):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_to is not None:
                payload["reply_to_message_id"] = reply_to
                payload["allow_sending_without_reply"] = True
            if silent:
                payload["disable_notification"] = True
            sent.append(dict(self.call("sendMessage", **payload) or {}))
            reply_to = None  # отвечаем на исходное сообщение только первым куском
        return sent

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        content: str | bytes,
        *,
        caption: str = "",
        reply_to: int | None = None,
        silent: bool = False,
        content_type: str = TEXT_DOCUMENT_TYPE,
    ) -> dict[str, Any]:
        """Отправляет файл документом (multipart/form-data): отчёты по долгам.

        Telegram не принимает файл JSON-ом, поэтому содержимое уходит полем `document`
        вместе с остальными параметрами запроса. Подпись (caption) обрезается по лимиту
        Bot API — длинный текст запроса всё равно лёг бы ошибкой 400.
        `content_type` — тип файла для Telegram и принимающей стороны: выгрузка `/export`
        уходит как `text/csv` (CSV_DOCUMENT_TYPE), чтобы её открывали таблицей, а не текстом.
        """
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = str(caption)[:MAX_CAPTION_LENGTH]
        if reply_to is not None:
            data["reply_to_message_id"] = reply_to
            data["allow_sending_without_reply"] = "true"
        if silent:
            data["disable_notification"] = "true"
        files = {"document": (filename, payload, content_type)}
        return dict(self._request("sendDocument", data=data, files=files) or {})

    def send_typing(self, chat_id: int | str) -> None:
        """Показывает «печатает…» (ошибки игнорируются — это необязательный индикатор)."""
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Режет длинный текст на части, не разрывая строки по возможности."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else [""]

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # очень длинная строка без переносов
            head, line = line[:limit], line[limit:]
            if current:
                parts.append("".join(current))
                current, size = [], 0
            parts.append(head)
        if size + len(line) > limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        parts.append("".join(current))
    return parts
