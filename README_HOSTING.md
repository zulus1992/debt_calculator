# 🚀 Ветка `hosting` — бот работает постоянно (мгновенные ответы)

Ветка `main` рассчитана на GitHub Actions: бот забирает сообщения раз в 30 минут, поэтому
ответ приходит с задержкой. Ветка `hosting` — для любого хостинга с постоянным процессом
(HidenCloud, Pterodactyl-панели, VPS, Termux): бот висит на long polling и отвечает **сразу**.

## Чем `hosting` отличается от `main`

| Файл | Отличие |
|---|---|
| `bot.py` | `run()` стартует с сохранённого `last_update_id`, ловит SIGTERM/SIGINT (мягкая остановка контейнера) и сохраняет смещение при выключении |
| `.github/workflows/bot.yml` | **cron отключён** (оставлен только ручной запуск «Диагностика настроек и сервисов» + тесты) — иначе Actions и хостинг будут воевать за одни сообщения |
| `start.sh` | новая точка входа для панели: ставит зависимости, проверяет настройки, запускает бота в постоянном режиме |
| `Procfile` | для хостингов, которые читают Procfile (Railway и подобные) |
| код бота, DeepSeek, Supabase | **без изменений** — те же `bot.py`, `deepseek.py`, `storage.py`, `debts.py` |

> ⚠️ Держите запущенным **только один** экземпляр. Если параллельно останется cron из ветки
> `main` или второй процесс на ПК, Telegram отдаст апдейты одному «слушателю», а второй получит
> `HTTP 409 Conflict`. На этой ветке cron уже убран; проверьте, что вы не запускаете `python bot.py` ещё где-то.

## Что подготовить

* проект Supabase с применённым `db/schema.sql` (таблицы `debts`, `bot_settings`, `bot_state`);
* ключ DeepSeek;
* токен бота от `@BotFather`;
* `SUPABASE_SERVICE_KEY` — **service_role** (не anon!), иначе запись блокирует RLS.

## Шаги на HidenCloud (панель в стиле Pterodactyl)

1. **Создать сервер.** Обычно: категория «Bot» / «Python», egg «Python» (или Generic),
   RAM ≥ 512 МБ, диск ≥ 1 ГБ, ближайший регион. Проверьте, что панель показывает Python 3.10+
   (в bash-консоли: `python3 --version`).
2. **Скопировать файлы** из ветки `hosting` в панель — через встроенный **File Manager** или **SFTP**:
   ```
   bot.py  config.py  debts.py  deepseek.py  storage.py  telegram_api.py
   requirements.txt  start.sh  Procfile
   tests/            (необязательно, но удобно для самопроверки)
   .env              (создать на месте, в git его нет — см. .gitignore)
   ```
   Каталог `db/` и `.github/` на хостинге не нужны.
3. **Команда запуска** (Startup Command / Startup File в настройках сервера):
   ```
   bash start.sh
   ```
   Если панель просит именно файл — `start.sh` (внутри уже всё нужное).
4. **Создать `.env`** в корне сервера (File Manager → New File → `.env`):
   ```ini
   TELEGRAM_BOT_TOKEN=123456789:AA...
   DEEPSEEK_API_KEY=sk-...
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_SERVICE_KEY=eyJhbGciOi...      # именно service_role (Reveal в Supabase → API Keys)
   DEFAULT_CURRENCY=BYN
   ALLOWED_USER_IDS=                        # пусто = отвечает всем; иначе список id через запятую
   LOG_LEVEL=INFO
   ```
5. **Start** в панели и смотрите консоль:
   ```
   == Python: Python 3.12.x ==
   == Установка зависимостей ==
   == Проверка настроек и сервисов ==
   ✓ Telegram: @ваш_бот (id ...)
   ✓ DeepSeek: ключ принят, модель deepseek-chat
   ✓ Supabase: таблица debts доступна
   == Запуск бота: long polling, ответы мгновенные ==
   Бот @ваш_бот (id ...) запущен (long polling). Стартовое смещение: 123456789
   ```
6. **Проверка:** напишите боту «Леша должен Диме 3 рубля» — ответ должен прийти в течение 1–3 секунд
   (задержка = один запрос к DeepSeek). Затем «покажи долги».

## Обновление кода на хостинге

1. Скачайте изменённые файлы из ветки `hosting` и загрузите их в панель (File Manager/SFTP),
   либо, если на сервере есть git:
   ```bash
   git pull origin hosting
   ```
2. **Restart** сервера в панели. Зависимости переустановятся сами — это делает `start.sh`.

Локально перед загрузкой полезно прогнать проверки:
```powershell
python -m unittest tests.test_pipeline   # 54 теста, без внешних сервисов
python bot.py --check                    # проверка ключей и сервисов
```

## Диагностика и типичные проблемы

| Симптом | Причина и что делать |
|---|---|
| `HTTP 409 Conflict` | работает второй «слушатель»: отключите cron в ветке `main` (Actions → отключить workflow) или остановите локальный `python bot.py` |
| `Sweep...` / бот не отвечает, в логе «Новых сообщений нет» | процесс живёт, но ключи неверны — смотрите вывод шага `--check` в консоли сервера |
| `Таблица не найдена (HTTP 404)` | в Supabase не применён `db/schema.sql` (нужны `debts`, `bot_settings`, `bot_state`) |
| `ключ с ролью «anon»` | в `.env` попал anon-ключ; нужен `service_role` (Supabase → API Keys → Reveal) |
| Ответ приходит с задержкой в десятки минут | вы всё ещё смотрите на Actions-режим (ветка `main`) или хостинг «усыпляет» процесс (у бесплатных тарифов бывает авто-сон) |
| Контейнер перезапускается сам | смотрите лимиты RAM/CPU тарифа; боту достаточно 256–512 МБ, но панель может ограничивать жёстче |

Вместо захода в панель настройки можно проверить из GitHub: в ветке `hosting` остался ручной
workflow «Диагностика настроек и сервисов» — он использует те же секреты и печатает статус Telegram/DeepSeek/Supabase.

## Если хостинг усыпляет процесс

Бесплатные тарифы иногда останавливают контейнер при простое. Варианты:

* платный тариф того же хостинга (или VPS) — самый предсказуемый;
* вернуться на ветку `main` (GitHub Actions раз в 30 минут) — тогда ответы с задержкой,
  но инфраструктура не нужна вовсе;
* **VPS + systemd** (тот же код, файл `.env` рядом): `/etc/systemd/system/debt-bot.service`
  ```ini
  [Unit]
  Description=Debt calculator telegram bot
  After=network-online.target

  [Service]
  WorkingDirectory=/opt/debt_calculator
  ExecStart=/usr/bin/python3 /opt/debt_calculator/bot.py
  Environment=PYTHONUNBUFFERED=1
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target
  ```
  ```bash
  systemctl enable --now debt-bot && journalctl -u debt-bot -f
  ```
* **Android + Termux**: `pkg install python`, `pip install requests`, затем
  `termux-wake-lock && python bot.py &` (телефон должен быть включён).

## Как вернуться на Actions-режим

Переключите ветку обратно (`git checkout main`) — там cron включён и бот работает «пачками».
Помните: одновременно работать не должны.

## Быстрый переезд между режимами

Смещение обработанных сообщений хранится в `bot_state.last_update_id`, поэтому при переключении
между `--once` (Actions) и long polling ничего не теряется и не дублируется: оба режима читают
одно и то же значение.

