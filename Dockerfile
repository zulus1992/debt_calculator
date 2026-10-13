# Bothost (и любой Docker-хостинг): запускаем наш вебхук-сервер напрямую.
#
# Зачем: если панель не распознаёт HTTP-сервер в проекте, она добавляет свою
# служебную обёртку на PORT и запускает bot.py как подпроцесс. У обёртки есть
# только GET /health, поэтому POST /api/telegram получает 404, а наш сервер не
# может занять порт («Address already in use»). Со своим Dockerfile панель просто
# выполняет CMD — и порт занимает наш сервер.
#
# `python bot.py` поднимает WSGI-сервер, слушает 0.0.0.0 и порт из переменной PORT.
FROM python:3.12-slim

WORKDIR /app

# Не буферизовать вывод (логи видны сразу) и не писать .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY . .

# Порт берётся из PORT (его задаёт панель); по умолчанию 8080. Строка ниже —
# подсказка хостингу; реально приложение слушает значение из PORT.
EXPOSE 8080

CMD ["python", "bot.py"]
