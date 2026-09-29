# -*- coding: utf-8 -*-
"""Отправка писем через Gmail API: OAuth2-токен + REST, только requests (без SDK Google).

Почему Gmail API, а не SMTP: на хостингах SMTP часто закрыт, а «пароль приложения» к
аккаунту Google уже не выдают — вместо него работает OAuth2-доступ, который не хранится
в чате, может быть отозван в кабинете Google и не даёт ничего, кроме отправки писем.

Как это устроено:

    POST https://oauth2.googleapis.com/token
        grant_type=refresh_token&client_id=<...>&client_secret=<...>&refresh_token=<...>
        → {"access_token": "ya29...", "expires_in": 3599, "token_type": "Bearer"}

    POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send
        Authorization: Bearer <access_token>
        {"raw": "<письмо RFC 5322, base64url>"}          → {"id": "<id сообщения>"}

Доступы берутся из секретов (.env): GMAIL_CLIENT_ID и GMAIL_CLIENT_SECRET — от OAuth-клиента
типа «Desktop app» в Google Cloud, GMAIL_REFRESH_TOKEN — из согласия на право gmail.send.
Отправитель — тот аккаунт, который выдал refresh-токен; GMAIL_SENDER задаёт, какой адрес
стоит в «From» (по умолчанию его подставляет сам Gmail).

Кому писать и о чём — решает reports.py; здесь только транспорт: «отдать письмо Gmail».
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Mapping, Sequence

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
# Access-токен живёт час; обновляем заранее, чтобы запрос не «сгорел» на границе.
TOKEN_MARGIN_SECONDS = 60
DEFAULT_TOKEN_LIFETIME = 3600


class MailError(RuntimeError):
    """Письмо отправить не удалось (настройки, доступ или сеть)."""


def _short_text(response: Any) -> str:
    """Короткий ответ сервиса для сообщения об ошибке (без переводов строк)."""
    text = str(getattr(response, "text", "") or "").strip().replace("\n", " ")
    return text[:200]


@dataclass(frozen=True)
class Attachment:
    """Файл во вложении: имя, содержимое и тип (у отчёта — CSV-выгрузка одного чата).

    Содержимое — текст: письмо собирается целиком в памяти, отдельного «потока» файла нет.
    """

    filename: str
    text: str
    content_type: str = "text/csv; charset=utf-8"


def _content_parts(content_type: str) -> tuple[str, str]:
    """Разбирает тип файла на maintype и subtype: 'text/csv; charset=utf-8' → ('text','csv')."""
    maintype, _, rest = str(content_type or "").partition("/")
    subtype = rest.split(";")[0].strip()
    return maintype.strip() or "application", subtype or "octet-stream"


def build_raw_message(*, sender: str, to: str, subject: str, body: str,
                      attachments: Sequence[Attachment] = ()) -> str:
    """Собирает письмо (RFC 5322) и отдаёт его в base64url — это формат поля raw Gmail API.

    Тема и текст кодируются штатным `EmailMessage`: русская тема уходит в MIME-кодировке,
    поэтому в почте читается словами, а не «=?utf-8?…». «From» ставим, только если адрес
    отправителя задан явно: без него Gmail сам подставляет аккаунт, выдавший токен.

    Вложения — файлы CSV (по одному на чат, см. reports.py): имя и тип сохраняются, а
    текст уходит в base64, поэтому Excel и Google Sheets открывают файл таблицей.
    """
    message = EmailMessage()
    if str(sender or "").strip():
        message["From"] = str(sender).strip()
    message["To"] = str(to).strip()
    message["Subject"] = str(subject)
    message.set_content(body, charset="utf-8")
    for attachment in attachments:
        maintype, subtype = _content_parts(attachment.content_type)
        message.add_attachment(
            attachment.text.encode("utf-8"),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
            params={"charset": "utf-8"} if maintype == "text" else None,
        )
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


@dataclass(frozen=True)
class SentMessage:
    """Отправленное письмо: id сообщения в Gmail, адрес и тема."""

    message_id: str
    to: str
    subject: str


class GmailSender:
    """Клиент Gmail API: обновляет access-токен по refresh-токену и отправляет письма.

    Сессию requests можно подменить (тесты) — тогда в сеть ничего не уходит.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *,
                 sender: str = "", api_url: str = GMAIL_API_URL, token_url: str = TOKEN_URL,
                 timeout: float = 30.0, session: Any = None) -> None:
        self._client_id = str(client_id or "").strip()
        self._client_secret = str(client_secret or "").strip()
        self._refresh_token = str(refresh_token or "").strip()
        self._sender = str(sender or "").strip()
        self._api_url = str(api_url or GMAIL_API_URL).rstrip("/")
        self._token_url = str(token_url or TOKEN_URL)
        self._timeout = timeout
        self._session = session or requests
        self._access_token = ""
        self._expires_at = 0.0        # до какого момента (unix-время) токен годен

    def access_token(self, *, force: bool = False) -> str:
        """Access-токен: пока он жив — из памяти, иначе обновляем по refresh-токену.

        Один refresh-токен работает годами, а access-токен живёт час, поэтому обновление
        делается на каждую отправку «холодного» процесса: `python reports.py` запускается
        cron-ом раз в месяц и каждый раз начинает с нуля.
        """
        if not force and self._access_token and time.time() < self._expires_at:
            return self._access_token
        missing = [
            name for name, value in (
                ("GMAIL_CLIENT_ID", self._client_id),
                ("GMAIL_CLIENT_SECRET", self._client_secret),
                ("GMAIL_REFRESH_TOKEN", self._refresh_token),
            )
            if not value
        ]
        if missing:
            raise MailError("Не заданы доступы Gmail API: " + ", ".join(missing) + ".")
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }
        try:
            response = self._session.post(self._token_url, data=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise MailError(f"Google недоступен (обновление токена): {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise MailError(self._token_problem(status, response))
        try:
            data = response.json() or {}
        except ValueError as exc:
            raise MailError("Ответ Google на обновление токена — не JSON.") from exc
        token = str(data.get("access_token") or "")
        if not token:
            raise MailError(f"Google не вернул access-токен: {data}")
        try:
            lifetime = float(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        if lifetime <= TOKEN_MARGIN_SECONDS:
            lifetime = DEFAULT_TOKEN_LIFETIME
        self._access_token = token
        self._expires_at = time.time() + lifetime - TOKEN_MARGIN_SECONDS
        return token

    def send(self, *, to: str, subject: str, body: str,
             attachments: Sequence[Attachment] = ()) -> SentMessage:
        """Отправляет письмо методом users/me/messages/send и возвращает его id в Gmail.

        Вложения (файлы CSV отчёта) попадают в то же поле raw: Gmail принимает письмо
        целиком, отдельного запроса на файл не нужно.
        """
        recipient = str(to or "").strip()
        if not recipient:
            raise MailError("Не задан получатель письма: проверьте REPORT_EMAIL.")
        raw = build_raw_message(sender=self._sender, to=recipient, subject=subject, body=body,
                                attachments=attachments)
        url = f"{self._api_url}/users/me/messages/send"
        response = self._post(url, payload={"raw": raw}, token=self.access_token())
        if int(getattr(response, "status_code", 0) or 0) == 401:
            # Access-токен успел истечь: обновляем и повторяем один раз — письмо не ушло,
            # дубликата не будет (Gmail отвечает 401 до приёма сообщения).
            response = self._post(url, payload={"raw": raw}, token=self.access_token(force=True))
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise MailError(self._send_problem(status, response))
        try:
            data = response.json() or {}
        except ValueError as exc:
            raise MailError("Ответ Gmail на отправку — не JSON.") from exc
        return SentMessage(message_id=str(data.get("id") or ""), to=recipient, subject=subject)

    def _post(self, url: str, *, payload: Mapping[str, Any], token: str) -> Any:
        """POST с токеном: сетевые сбои превращаем в MailError, а не в трейсбек."""
        try:
            return self._session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise MailError(f"Gmail недоступен: {exc}") from exc

    def _token_problem(self, status: int, response: Any) -> str:
        """Понятное объяснение отказа Google на обновление access-токена."""
        if status in (400, 401):
            return (
                f"Google отклонил обновление токена (HTTP {status}): проверьте GMAIL_CLIENT_ID, "
                "GMAIL_CLIENT_SECRET и GMAIL_REFRESH_TOKEN — refresh-токен мог истечь или быть "
                f"отозван. Ответ Google: {_short_text(response)}"
            )
        if status == 403:
            return (
                "Google отказал в обновлении токена (HTTP 403): у OAuth-клиента нет доступа "
                f"к Gmail API или согласие отозвано. Ответ Google: {_short_text(response)}"
            )
        return f"Google вернул HTTP {status} на обновление токена: {_short_text(response)}"

    def _send_problem(self, status: int, response: Any) -> str:
        """Понятное объяснение отказа Gmail на отправку письма."""
        if status == 401:
            return (
                "Gmail отклонил токен (HTTP 401): проверьте GMAIL_REFRESH_TOKEN — "
                "он мог истечь или быть отозван."
            )
        if status == 403:
            return (
                "Gmail отказал в отправке (HTTP 403): у refresh-токена нет права gmail.send — "
                "выдайте согласие заново с этим правом. "
                f"Ответ Gmail: {_short_text(response)}"
            )
        if status == 429:
            return "Gmail: слишком много запросов (HTTP 429) — попробуйте позже."
        return f"Gmail вернул HTTP {status}: {_short_text(response)}"
