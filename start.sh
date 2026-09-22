#!/bin/sh
# Точка входа для хостинга (HidenCloud / Pterodactyl и любые панели с постоянным процессом).
# Запускает бота в режиме long polling: сообщения обрабатываются мгновенно.
#
# В панели хостинга укажите команду запуска:  bash start.sh
set -e

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN=python
fi
echo "== Python: $("$PYTHON_BIN" --version 2>&1) =="

echo "== Установка зависимостей =="
"$PYTHON_BIN" -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

if [ ! -f .env ] && [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "!! Не найден .env и переменные окружения не заданы."
    echo "!! Создайте .env на основе .env.example:"
    echo "!!   TELEGRAM_BOT_TOKEN=..., DEEPSEEK_API_KEY=...,"
    echo "!!   SUPABASE_URL=..., SUPABASE_SERVICE_KEY=... (ключ service_role)"
    exit 1
fi

echo "== Проверка настроек и сервисов =="
"$PYTHON_BIN" bot.py --check || echo "!! Проверка выявила проблемы — разбирайтесь по выводу выше"

echo "== Запуск бота: long polling, ответы мгновенные (Ctrl+C / Stop в панели — остановка) =="
exec "$PYTHON_BIN" bot.py
