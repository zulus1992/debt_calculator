# -*- coding: utf-8 -*-
"""Точка входа для Vercel: https://<проект>.vercel.app/api/telegram

Vercel ищет в файле WSGI-приложение (`app`/`application`) или класс `handler` —
отдаём готовое приложение из webhook.py. Корень проекта добавляем в sys.path явно:
в serverless-сборке пути импорта не гарантированы, а webhook.py тянет за собой
bot.py, config.py, storage.py, debts.py, deepseek.py и telegram_api.py.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from webhook import app  # noqa: E402 — импорт после правки sys.path

application = app
