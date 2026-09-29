# -*- coding: utf-8 -*-
"""Режим вебхука: Telegram сам присылает апдейты на наш HTTPS-эндпоинт (WSGI).

Зачем: serverless-хостинги (Vercel, PythonAnywhere и любой WSGI) не держат постоянный
процесс, поэтому long polling там невозможен или «засыпает». В режиме вебхука каждое
сообщение приходит отдельным HTTP-запросом, бот разбирает его через DeepSeek и отвечает —
ответ приходит за 1–3 секунды, «усыпление» контейнера не мешает.

Адреса:
    /api/telegram   апдейты Telegram (секрет — WEBHOOK_SECRET, заголовок от Telegram);
    /api/report     отчёт на почту по внешнему расписанию: GET/POST с секретом в адресе
                    (?token=…) или заголовке X-Report-Token (cron-job.org и подобные);
    /report         то же, что /api/report (короткий адрес для планировщика);
    /               проверка живости (GET) — отвечает «ok» и обновляет курсы по расписанию.

Запуск и переключение режимов:
    python webhook.py --serve                                   # локальная проверка (wsgiref)
    python bot.py --set-webhook https://<домен>/api/telegram    # Telegram шлёт апдейты сюда
    python bot.py --webhook-info                                # что настроено сейчас
    python bot.py --delete-webhook                              # вернуться на long polling

Размещение:
    Vercel          — api/telegram.py делает `from webhook import app` → адрес /api/telegram
    PythonAnywhere  — в WSGI-конфиге: `from webhook import app as application`
    любой сервер    — gunicorn webhook:app (например, VPS с HTTPS-прокси)

Настройки: обычные из .env/окружения плюс WEBHOOK_SECRET — секрет, которым Telegram
подписывает запросы (заголовок X-Telegram-Bot-Api-Secret-Token). Без него эндпоинт
отвечает 500 и ничего не обрабатывает: адрес виден в интернете, и проверка заголовка —
основная защита от поддельных апдейтов.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import sys
import threading
from typing import Any, Callable

from urllib.parse import parse_qs

from bot import DebtBot, build_runtime, configure_logging, configure_stdout
from config import (
    ConfigError,
    Settings,
    load_settings,
    report_secret_problem,
    require_settings,
    webhook_secret_problem,
)
from gmail_api import MailError
from reports import month_period, send_report, week_period
from storage import StorageError, storage_from_settings
from telegram_api import TelegramError

logger = logging.getLogger("debt_bot.webhook")

# Telegram присылает секрет в этом заголовке (в WSGI — с префиксом HTTP_ и в верхнем регистре).
SECRET_HEADER = "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"
SECRET_HEADER_NAME = "X-Telegram-Bot-Api-Secret-Token"
JSON_TYPE = "application/json; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"


class WebhookApp:
    """WSGI-приложение: принимает апдейты Telegram и отвечает на них.

    Зависимости передаются снаружи — так приложение тестируется без сети
    (хранилище в памяти + подменённый Telegram).
    """

    def __init__(self, bot: Any, secret: str, *, lock: Any = None) -> None:
        self._bot = bot
        self._secret = str(secret or "")
        # Обрабатываем апдейты по одному: два параллельных сообщения не должны
        # «обгонять» друг друга в bot_state (смещение обработанных апдейтов).
        self._lock = lock or threading.Lock()

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        """Точка входа WSGI: GET — проверка живости, POST — апдейт от Telegram."""
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if method == "GET":
            # Сюда же стучатся «пингеры» бесплатных тарифов, чтобы контейнер не засыпал.
            # Заодно это шанс подтянуть курсы по расписанию: у вебхука своих таймеров нет.
            self._refresh_rates()
            return _respond(start_response, "200 OK", "ok", TEXT_TYPE)
        if method != "POST":
            return _respond(
                start_response, "405 Method Not Allowed",
                json.dumps(
                    {"ok": False, "error": "поддерживаются только GET (проверка) и POST (апдейты)"},
                    ensure_ascii=False,
                ),
            )
        return self._handle_update(environ, start_response)

    def _refresh_rates(self) -> None:
        """Тихо обновляет курсы по расписанию (раз в день в RATES_HOUR по Минску).

        У вебхука нет фонового цикла, поэтому проверку расписания делает и обработка
        апдейта, и проверка живости. Проблемы только логируем: ответ «ok» должен уйти.
        """
        refresh = getattr(self._bot, "_refresh_rates_if_due", None)
        if not callable(refresh):
            return
        try:
            refresh()
        except Exception as exc:  # noqa: BLE001 — проверка живости отвечает всегда
            logger.warning("Автообновление курсов не удалось: %s", exc)

    def _handle_update(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        """Проверяет секрет, разбирает апдейт и отдаёт ответ Telegram."""
        secret_problem = webhook_secret_problem(self._secret)
        if secret_problem:
            logger.error("Вебхук не настроен: %s", secret_problem)
            return _respond(
                start_response, "500 Internal Server Error",
                json.dumps({"ok": False, "error": secret_problem}, ensure_ascii=False),
            )

        provided = str(environ.get(SECRET_HEADER) or "")
        if not provided or not hmac.compare_digest(
            provided.encode("utf-8", "replace"), self._secret.encode("utf-8")
        ):
            logger.warning("Отклонён запрос без верного заголовка %s", SECRET_HEADER_NAME)
            return _respond(
                start_response, "403 Forbidden",
                json.dumps({"ok": False, "error": "неверный секрет вебхука"}, ensure_ascii=False),
            )

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = 0
        stream = environ.get("wsgi.input")
        raw = stream.read(length) if (stream is not None and length > 0) else b""
        try:
            update = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            logger.warning("Тело запроса не JSON: %r", raw[:200])
            return _respond(
                start_response, "400 Bad Request",
                json.dumps(
                    {"ok": False, "error": "тело запроса должно быть JSON-объектом апдейта"},
                    ensure_ascii=False,
                ),
            )
        if not isinstance(update, dict):
            return _respond(
                start_response, "400 Bad Request",
                json.dumps({"ok": False, "error": "ожидался JSON-объект апдейта"}, ensure_ascii=False),
            )

        try:
            with self._lock:
                accepted = bool(self._bot.process_update(update))
        except Exception as exc:  # noqa: BLE001 — отвечаем 200, иначе Telegram зашлёт повторы
            logger.exception("Ошибка обработки апдейта: %s", exc)
            return _respond(
                start_response, "200 OK",
                json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            )
        return _respond(
            start_response, "200 OK",
            json.dumps({"ok": True, "accepted": accepted}, ensure_ascii=False),
        )


def _respond(start_response: Callable, status: str, body: str, content_type: str = JSON_TYPE) -> list[bytes]:
    """Формирует ответ WSGI с корректным Content-Length (кириллица → UTF-8)."""
    payload = body.encode("utf-8")
    start_response(status, [
        ("Content-Type", content_type),
        ("Content-Length", str(len(payload))),
        ("Cache-Control", "no-store"),  # апдейты кешировать нельзя
    ])
    return [payload]


def _json(start_response: Callable, status: str, **payload: Any) -> list[bytes]:
    """JSON-ответ эндпоинта: кириллица как есть, без кеширования."""
    return _respond(start_response, status, json.dumps(payload, ensure_ascii=False))


# Эндпоинт отчёта на почту: путь, заголовок с секретом и допустимые периоды письма.
REPORT_PATHS = ("/report", "/api/report")
REPORT_TOKEN_HEADER = "HTTP_X_REPORT_TOKEN"
REPORT_TOKEN_HEADER_NAME = "X-Report-Token"
REPORT_KINDS = ("week", "month")


def _query_params(environ: dict[str, Any]) -> dict[str, str]:
    """Параметры строки запроса: имя → первое значение."""
    raw = str(environ.get("QUERY_STRING") or "")
    if not raw:
        return {}
    return {name: values[0] for name, values in parse_qs(raw, keep_blank_values=True).items()}


def _flag(params: dict[str, str], name: str) -> bool:
    """Флаг из параметра запроса: ?force=1, ?dry=true, ?dry=да — все «правдивые» значения."""
    return str(params.get(name) or "").strip().lower() in ("1", "true", "yes", "on", "y", "да")


def report_path(environ: dict[str, Any]) -> bool:
    """Это запрос к эндпоинту отчёта? Путь сравниваем без хвостового «/» и без регистра."""
    path = str(environ.get("PATH_INFO") or "/").strip().lower()
    return (path.rstrip("/") or "/") in REPORT_PATHS


class ReportApp:
    """WSGI-эндпоинт отчёта на почту: его вызывает внешний планировщик (cron-job.org).

    Адрес: GET/POST `/api/report` (он же `/report`). Секрет — `CRON_SECRET`, а если он
    не задан, `WEBHOOK_SECRET`; передаётся в адресе (`?token=…`) или заголовком
    `X-Report-Token`. Без верного секрета эндпоинт отвечает 403 и ничего не отправляет.

    Необязательные параметры:
        kind=week|month   период письма: неделя (по умолчанию) или месяц;
        period=2026-W40   конкретная неделя (или `2026-09` — месяц);
        force=1           отправить, даже если за этот период отчёт уже уходил;
        to=адрес          отправить на этот адрес вместо REPORT_EMAIL (разовая проверка);
        dry=1             собрать письмо и вернуть его текст, ничего не отправляя.

    Ответ — JSON: период, чаты, файлы, кому отправлено и причина, если не отправляли.
    Код 200 — запрос обработан (в том числе «за этот период записей нет» и «уже отправляли»:
    для планировщика это успех, а не сбой), 403 — неверный секрет, 400 — плохие параметры,
    500 — сломаны настройки почты/базы или отправка не удалась.
    """

    def __init__(self, settings: Settings, *, storage: Any = None, sender: Any = None,
                 session: Any = None) -> None:
        self._settings = settings
        self._storage = storage
        self._sender = sender
        self._session = session

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        """Обрабатывает один вызов планировщика: секрет, период, отправка, отчёт в JSON."""
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if method not in ("GET", "POST"):
            return _json(start_response, "405 Method Not Allowed",
                         ok=False, error="поддерживаются GET и POST")
        params = _query_params(environ)
        problem = report_secret_problem(self._settings.report_secret)
        if problem:
            logger.error("Эндпоинт отчёта не настроен: %s", problem)
            return _json(start_response, "500 Internal Server Error", ok=False, error=problem)
        if not self._secret_ok(environ, params):
            logger.warning("Отклонён запрос к эндпоинту отчёта: неверный секрет")
            return _json(start_response, "403 Forbidden", ok=False,
                         error="неверный секрет: пришлите ?token=… или заголовок "
                               f"{REPORT_TOKEN_HEADER_NAME}")
        kind = str(params.get("kind") or "").strip().lower() or "week"
        if kind not in REPORT_KINDS:
            return _json(start_response, "400 Bad Request", ok=False,
                         error=f"kind должен быть {' или '.join(REPORT_KINDS)}, а не «{kind}»")
        try:
            period = (week_period if kind == "week" else month_period)(params.get("period") or None)
        except ValueError as exc:
            return _json(start_response, "400 Bad Request", ok=False, error=str(exc))

        dry_run = _flag(params, "dry") or _flag(params, "dry_run")
        try:
            storage = self._storage or storage_from_settings(self._settings)
            run = send_report(self._settings, storage, period,
                              force=_flag(params, "force"), to=params.get("to", ""),
                              dry_run=dry_run, sender=self._sender, session=self._session)
        except (StorageError, MailError) as exc:
            logger.error("Отчёт не отправлен: %s", exc)
            return _json(start_response, "500 Internal Server Error", ok=False, error=str(exc))

        payload: dict[str, Any] = {
            "ok": not run.problems,
            "kind": kind,
            "period": period.key,
            "title": period.title,
            "sent": list(run.sent),
            "chats": list(run.chats),
            "files": list(run.files),
            "message_id": run.message_id,
            "reason": run.reason,
        }
        if run.problems:
            payload["problems"] = list(run.problems)
        if dry_run:
            payload["body"] = run.body
        logger.info("Эндпоинт отчёта: %s за %s — %s", kind, period.key,
                    ", ".join(run.sent) or (run.reason or "не отправлено"))
        return _json(start_response, "500 Internal Server Error" if run.problems else "200 OK",
                     **payload)

    def _secret_ok(self, environ: dict[str, Any], params: dict[str, str]) -> bool:
        """Сверяет секрет из адреса или заголовка X-Report-Token с настройками."""
        provided = str(params.get("token") or environ.get(REPORT_TOKEN_HEADER) or "")
        expected = self._settings.report_secret
        return bool(provided) and hmac.compare_digest(provided.encode("utf-8", "replace"),
                                                      expected.encode("utf-8", "replace"))


def report_app_from_settings() -> ReportApp:
    """Боевая сборка эндпоинта отчёта: нужны только доступы Gmail и база, Telegram — нет."""
    settings = load_settings()
    configure_logging(settings.log_level)
    problem = report_secret_problem(settings.report_secret)
    if problem:
        raise ConfigError(problem)
    problems = [item for item in (settings.reports_problem(), settings.database_problem()) if item]
    if problems:
        raise ConfigError("Проверьте настройки отчёта:\n- " + "\n- ".join(problems))
    logger.info("Эндпоинт отчёта готов: письма на %s", ", ".join(settings.report_recipients))
    return ReportApp(settings)


class LazyReportApp:
    """Собирает эндпоинт отчёта при первом запросе (важно для serverless).

    Ошибка сборки кешируется и отдаётся текстом в JSON: причина видна прямо в ответе
    планировщику, а не только в логе хостинга.
    """

    def __init__(self, factory: Callable[[], ReportApp] | None = None) -> None:
        self._factory = factory or report_app_from_settings
        self._app: ReportApp | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        """Отдаёт запрос готовому эндпоинту (создавая его при необходимости)."""
        built = self._get()
        if built is None:
            return _json(start_response, "500 Internal Server Error",
                         ok=False, error=self._error or "приложение не собралось")
        return built(environ, start_response)

    def _get(self) -> ReportApp | None:
        """Создаёт (один раз) эндпоинт из настроек; ошибку кеширует для ответов."""
        with self._lock:
            if self._app is None and self._error is None:
                try:
                    self._app = self._factory()
                except (ConfigError, StorageError, TelegramError) as exc:
                    self._error = str(exc)
                    logger.error("Не удалось запустить эндпоинт отчёта: %s", exc)
            return self._app


class RoutesApp:
    """Маршрутизация: `/api/report` — отчёт на почту, всё остальное — вебхук Telegram.

    Обе части собираются лениво и независимо: отчёту не нужны Telegram и DeepSeek, а вебхуку —
    доступы Gmail, поэтому сломанные настройки одной части не ломают другую.
    """

    def __init__(self, *, webhook: Any = None, reports: Any = None) -> None:
        self._webhook = webhook if webhook is not None else LazyWebhookApp()
        self._reports = reports if reports is not None else LazyReportApp()

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        """Отдаёт запрос той части, к которой он относится по пути."""
        if report_path(environ):
            return self._reports(environ, start_response)
        return self._webhook(environ, start_response)


def build_app(settings: Settings, *, storage: Any = None, parser: Any = None,
              telegram: Any = None) -> WebhookApp:
    """Собирает приложение вебхука. Компоненты можно подменить (тесты без сети)."""
    if storage is None or parser is None or telegram is None:
        built_storage, built_parser, built_telegram = build_runtime(settings)
        storage = storage if storage is not None else built_storage
        parser = parser if parser is not None else built_parser
        telegram = telegram if telegram is not None else built_telegram
    bot = DebtBot(settings, storage, parser, telegram)
    return WebhookApp(bot, settings.webhook_secret)


def app_from_settings() -> WebhookApp:
    """Боевая сборка: настройки из окружения/.env, сервисы Supabase + DeepSeek + Telegram."""
    settings = load_settings()
    # На хостингах (PythonAnywhere, Vercel) stderr уходит в лог приложения — включаем логи,
    # чтобы в error log были видны старт и ошибки DeepSeek/Supabase, а не «тишина».
    configure_logging(settings.log_level)
    require_settings(settings)
    built = build_app(settings)
    logger.info(
        "Вебхук готов: доступ %s, валюта по умолчанию %s",
        "всем" if not settings.allowed_user_ids else f"{len(settings.allowed_user_ids)} польз.",
        settings.default_currency,
    )
    return built


class LazyWebhookApp:
    """Собирает приложение при первом апдейте: важно для serverless (cold start).

    Если настройки сломаны, каждый POST получает понятную ошибку (в логе и в теле ответа),
    а GET (проверка живости) продолжает отвечать 200 — «пингер» бесплатного тарифа
    не будет считать контейнер мёртвым.
    """

    def __init__(self, factory: Callable[[], WebhookApp] | None = None) -> None:
        self._factory = factory or app_from_settings
        self._app: WebhookApp | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        """Отдаёт запрос готовому приложению (создавая его при необходимости)."""
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if method == "GET":
            hint = f" — но конфигурация не готова: {self._error}" if self._error else ""
            return _respond(start_response, "200 OK", f"ok{hint}", TEXT_TYPE)

        built = self._get()
        if built is None:
            return _respond(
                start_response, "500 Internal Server Error",
                json.dumps({"ok": False, "error": self._error or "приложение не собралось"},
                           ensure_ascii=False),
            )
        return built(environ, start_response)

    def _get(self) -> WebhookApp | None:
        """Создаёт (один раз) приложение из настроек; ошибку кеширует для ответов."""
        with self._lock:
            if self._app is None and self._error is None:
                try:
                    self._app = self._factory()
                except (ConfigError, StorageError, TelegramError) as exc:
                    self._error = str(exc)
                    logger.error("Не удалось запустить вебхук: %s", exc)
            return self._app


# Vercel, gunicorn и PythonAnywhere ищут либо `app`, либо `application`.
# Маршрутизация: /api/report — отчёт на почту, всё остальное — вебхук Telegram.
app = RoutesApp()
application = app
# Отдельная точка входа для Vercel: api/report.py отдаёт эндпоинт отчёта по своему пути.
reports_app = LazyReportApp()


def serve(host: str = "127.0.0.1", port: int = 8080) -> int:
    """Локальный сервер на wsgiref — проверить маршруты и секрет без хостинга."""
    from wsgiref.simple_server import make_server  # стандартная библиотека, только для проверки

    settings = load_settings()
    problems = settings.problems()
    secret_problem = webhook_secret_problem(settings.webhook_secret)
    if problems or secret_problem:
        for line in problems:
            print("✗", line, file=sys.stderr)
        if secret_problem:
            print("✗", secret_problem, file=sys.stderr)
        return 1

    configure_logging(settings.log_level)
    # Маршрутизация та же, что в бою: /api/report — отчёт на почту, остальное — вебхук.
    local_app = RoutesApp()
    with make_server(host, port, local_app) as server:
        print(f"Локально: http://{host}:{port}  (проверка живости — GET /)")
        print(f"Апдейты Telegram: POST /api/telegram, заголовок {SECRET_HEADER_NAME}")
        print(f"Отчёт на почту: GET /api/report?token=<секрет> "
              f"(kind=week|month, force=1, dry=1, to=адрес, period=2026-W40)")
        print("Telegram требует HTTPS, доступный из интернета:",
              "ngrok http 8080 / cloudflared tunnel --url http://127.0.0.1:8080")
        print("Затем: python bot.py --set-webhook https://<адрес туннеля>/api/telegram")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nОстановлено (Ctrl+C).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки локального сервера."""
    parser = argparse.ArgumentParser(
        prog="webhook.py",
        description="Вебхук-режим бота «калькулятор долгов» (WSGI: Vercel, PythonAnywhere, gunicorn).",
    )
    parser.add_argument("--serve", action="store_true", help="поднять локальный сервер для проверки")
    parser.add_argument("--host", default="127.0.0.1",
                        help="адрес локального сервера (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="порт локального сервера (по умолчанию 8080)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Локальный запуск сервера или подсказка, как включить вебхук."""
    configure_stdout()
    args = build_parser().parse_args(argv)
    if args.serve:
        return serve(args.host, args.port)
    print("Это модуль вебхука: его подключает хостинг (WSGI: webhook:app).")
    print("Локальная проверка:  python webhook.py --serve")
    print("Включить вебхук:     python bot.py --set-webhook https://<домен>/api/telegram")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
