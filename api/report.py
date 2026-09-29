# -*- coding: utf-8 -*-
"""Точка входа для Vercel: https://<проект>.vercel.app/api/report

Отчёт на почту по внешнему расписанию (cron-job.org и подобные): GET или POST с секретом
в адресе (`?token=…`) или в заголовке `X-Report-Token`. Параметры — kind=week|month,
period, force, to, dry (см. webhook.ReportApp).

Корень проекта добавляем в sys.path явно: в serverless-сборке пути импорта не гарантированы,
а webhook.py тянет за собой config.py, reports.py, gmail_api.py, storage.py и bot.py.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from webhook import reports_app as app  # noqa: E402 — импорт после правки sys.path

application = app
