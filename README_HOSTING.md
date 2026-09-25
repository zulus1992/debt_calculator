# 🚀 Ветка `hosting` — бот работает постоянно (мгновенные ответы)

Ветка `hosting` — для любого хостинга с постоянным процессом (HidenCloud, Pterodactyl-панели,
VPS, Termux): бот висит на long polling и отвечает **сразу**. Никаких запусков «по расписанию»
не нужно: один постоянный процесс — и всё.

## Что важно в этой ветке

| Файл | Что делает |
|---|---|
| `bot.py` | `run()` стартует с сохранённого `last_update_id`, ловит SIGTERM/SIGINT (мягкая остановка контейнера) и сохраняет смещение при выключении |
| `webhook.py`, `api/telegram.py`, `vercel.json` | режим вебхука: Telegram сам присылает апдейты на HTTPS-эндпоинт — постоянный процесс не нужен (см. «Способ 2» ниже) |
| `start.sh` | точка входа для панели: ставит зависимости, проверяет настройки, запускает бота в постоянном режиме |
| `Procfile` | для хостингов, которые читают Procfile (Railway и подобные) |
| код бота, DeepSeek, Supabase | те же модули: `bot.py`, `deepseek.py`, `storage.py`, `debts.py`, `rates.py` |

> ⚠️ Держите запущенным **только один** экземпляр. Если параллельно работает второй процесс
> (на ПК, в панели или локально `python bot.py`), Telegram отдаст апдейты одному «слушателю»,
> а второй получит `HTTP 409 Conflict`.

## Способ 2: вебхук — мгновенные ответы бесплатно, без постоянного процесса

Так бот работает на **serverless-хостинге**: Telegram сам присылает каждый апдейт отдельным
HTTPS-запросом на наш эндпоинт (`webhook.py`, WSGI). Не нужен ни VPS, ни панель с «вечно живым»
контейнером — а значит, не мешает и автосон бесплатных тарифов: ответ приходит за 1–3 секунды
(задержка = один запрос к DeepSeek).

| Что нужно | Где брать |
|---|---|
| HTTPS-адрес | Vercel (Hobby — бесплатно) или PythonAnywhere (free: `https://USERNAME.pythonanywhere.com`) |
| `WEBHOOK_SECRET` | сгенерировать: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| остальные ключи | те же, что и раньше (Telegram, DeepSeek, Supabase — secret-ключ базы) |

Файлы режима: `webhook.py` (WSGI-приложение), `api/telegram.py` (точка входа для Vercel),
`vercel.json` (лимит времени функции). Локальный запуск — `python webhook.py --serve`.

### Вариант 2.1. Vercel (5 минут)

1. В Vercel → *Add New… → Project* → импортировать этот репозиторий, ветку **`hosting`**.
   Framework Preset — **Other**, Root Directory — корень (в `vercel.json` уже настроено, что
   функция `api/telegram.py` может работать до 60 секунд).
2. *Settings → Environment Variables* — добавить:
   `TELEGRAM_BOT_TOKEN`, `DEEPSEEK_API_KEY`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`,
   `WEBHOOK_SECRET`, при желании `DEFAULT_CURRENCY`, `ALLOWED_USER_IDS`.
3. Deploy. Адрес обработчика: `https://<проект>.vercel.app/api/telegram`
   (любой ответ `ok` в браузере — уже хорошо: значит, функция жива).
4. Локально (там, где лежит проект с заполненным `.env`) сказать Telegram, куда присылать апдейты:
   ```powershell
   python bot.py --set-webhook https://<проект>.vercel.app/api/telegram
   python bot.py --webhook-info                     # должно быть: url, pending 0, без ошибок
   ```
5. Написать боту «Леша должен Диме 3 рубля» — ответ за 1–3 секунды.

### Вариант 2.2. PythonAnywhere (бесплатный тариф) — пошагово

Что важно знать про бесплатный аккаунт *до* начала:

* своё веб-приложение живёт по адресу `https://USERNAME.pythonanywhere.com` с готовым HTTPS —
  именно он и нужен Telegram;
* **исходящие запросы идут через allowlist** — список разрешённых сайтов. Проверить все три
  домена (Telegram, DeepSeek, Supabase) можно за 20 секунд командой из шага 5
  (`python bot.py --check`). Если какой-то домен не разрешён и это Telegram — бесплатный тариф
  для вебхука не подойдёт, берите Vercel; если DeepSeek/Supabase — заявка на добавление
  (нужна ссылка на документацию API), платный аккаунт (там интернет без ограничений) либо Vercel;
* лимит CPU-секунд считается для консолей и задач, **веб-приложения под него не попадают**;
* «Always-on tasks» платные, но для вебхука они не нужны — бот живёт как веб-приложение.

**1. Консоль.** Зарегистрироваться → *Consoles → Bash*.

**2. Забрать код** (ветку `hosting`):

```bash
cd ~
git clone -b hosting https://github.com/USER/REPO.git debt_calculator
cd debt_calculator
```

Приватный репозиторий: `git clone -b hosting https://<TOKEN>@github.com/USER/REPO.git`
(fine-grained token только на этот репозиторий) либо загрузить ZIP через *Files → Upload*.

**3. Зависимости** (в виртуальном окружении — обе зависимости из `requirements.txt`):

```bash
mkvirtualenv --python=/usr/bin/python3.12 debt-bot
pip install -r requirements.txt
echo $VIRTUAL_ENV          # пригодится путь: /home/USERNAME/.virtualenvs/debt-bot
```

**4. Файл `.env` рядом с `bot.py`** (в git его нет):

```bash
cd ~/debt_calculator
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=123456789:AA...
DEEPSEEK_API_KEY=sk-...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SECRET_KEY=sb_secret_...        # secret-ключ базы: Project Settings → API Keys → Secret keys
DEFAULT_CURRENCY=BYN
ALLOWED_USER_IDS=
REQUIRE_MENTION=1        # в группах отвечать только на обращение «@бот …», в личке — всегда
WEBHOOK_SECRET=вставьте_свой_секрет
EOF
chmod 600 .env
```

Секрет: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

**5. Проверка ключей и allowlist** (самое важное на этом хостинге):

```bash
python bot.py --check
```

Ожидаемый результат — три строки `✓ Telegram`, `✓ DeepSeek`, `✓ Supabase` (в том числе
`таблица chat_members доступна`) и `• Telegram: вебхук не установлен`.
Если какой-то запрос блокируется allowlist'ом, вы увидите ошибку прокси (403/503) или таймаут —
для такого домена нужна заявка на добавление (форма «Anaconda Notebooks/PythonAnywhere Allow List
Request» со ссылкой на документацию API), платный аккаунт или Vercel.

**6. Создать веб-приложение:** *Web → Add a new web app → Manual configuration → Python 3.12*.
Заполнить:

| Поле | Значение |
|---|---|
| Source code | `/home/USERNAME/debt_calculator` |
| Working directory | `/home/USERNAME/debt_calculator` |
| Virtualenv | `/home/USERNAME/.virtualenvs/debt-bot` (пусто, если шаг 3 пропущен) |

**7. WSGI-файл** (`/var/www/USERNAME_pythonanywhere_com_wsgi.py` — ссылка на него есть на вкладке Web):
стереть содержимое и вставить

```python
import sys
path = "/home/USERNAME/debt_calculator"
if path not in sys.path:
    sys.path.insert(0, path)
from webhook import app as application
```

или записать его из консоли одной командой:

```bash
printf 'import sys\npath = "/home/%s/debt_calculator"\nif path not in sys.path:\n    sys.path.insert(0, path)\nfrom webhook import app as application\n' "$USER" > /var/www/${USER}_pythonanywhere_com_wsgi.py
```

**8. Reload** на вкладке *Web*, затем открыть в браузере `https://USERNAME.pythonanywhere.com/` —
должно вернуться `ok` (это проверка живости нашего вебхука).

**9. Сказать Telegram адрес** (из той же Bash-консоли):

```bash
cd ~/debt_calculator
python bot.py --set-webhook https://USERNAME.pythonanywhere.com/api/telegram
python bot.py --webhook-info        # url, pending 0, без last error
```

**10. Проверка:** написать боту «Леша должен Диме 3 рубля» — ответ за 1–3 секунды, затем `/debts`.

**11. Обновление кода:** `cd ~/debt_calculator && git pull origin hosting`,
затем **обязательно обновите зависимости** (у вебхука нет `start.sh`, который делает это сам):

```bash
workon debt-bot                        # то же окружение, что указано в Web → Virtualenv
pip install -r requirements.txt        # появились новые пакеты (например supabase)
python -c "import supabase; print(supabase.__version__)"   # проверка
python bot.py --check                  # ключи и права базы
```

после этого **Reload** на вкладке *Web*. Если в обновлении менялась схема БД (появились таблицы
или колонки), не забудьте ещё раз выполнить `db/schema.sql` в Supabase → SQL Editor: скрипт
идемпотентный и просто добавит недостающее (например, `chat_members.is_registered`,
`debts.group_id` и таблицу `currency_rates`).

**12. Логи:** *Web → Error log* — туда попадают наши логи с таймстампами (старт бота, ошибки
DeepSeek/Supabase, «Повтор апдейта …»). Быстрая диагностика без логов — `python bot.py --check`
и `python bot.py --webhook-info` из консоли.

**13. Возврат на long polling:** `python bot.py --delete-webhook` (можно прямо из консоли
PythonAnywhere) — после этого бот снова работает через `getUpdates`.

> ℹ️ Если позже захочется переехать на Vercel: код уже готов (вариант 2.1), менять нужно только
> адрес в `--set-webhook`.

### Локальная проверка перед деплоем

```powershell
python webhook.py --serve                 # http://127.0.0.1:8080, GET / — проверка живости
# публичный адрес для теста: ngrok http 8080  или  cloudflared tunnel --url http://127.0.0.1:8080
python bot.py --set-webhook https://<адрес туннеля>/api/telegram
```

### Как вернуться на long polling

```powershell
python bot.py --delete-webhook            # снять вебхук — Telegram снова отдаёт апдейты getUpdates
python bot.py                             # постоянный процесс (long polling)
```

Смещение обработанных апдейтов (`bot_state.last_update_id`) общее для всех режимов, поэтому
переключение вебхук ↔ long polling не теряет и не дублирует сообщения. Повторные
доставки одного апдейта бот отбрасывает сам (`accepted: false` в ответе на вебхук).

### Диагностика режима вебхука

| Симптом | Причина и что делать |
|---|---|
| `403` в ответе вебхука, сообщения не приходят | секрет в `WEBHOOK_SECRET` (Vercel/PythonAnywhere) не совпадает с тем, что отправляли в `--set-webhook`. Задайте одинаковое значение и повторите `--set-webhook` |
| `500 WEBHOOK_SECRET не задан` | переменная не добавлена в окружение хостинга — бот отказывается обрабатывать запросы без проверки подписи |
| В `--webhook-info` поле `last error` = `Wrong response from the webhook` | на хостинге нет ключей DeepSeek/Supabase: смотрите логи функции (Vercel → Deployments → Logs) |
| В `--webhook-info` поле `last error` = `SSL error` / `Bad Gateway` | адрес недоступен либо сертификат не готов; проверьте деплой и повторите `--set-webhook` |
| В логе `Supabase не ответил (попытка 1 из 3)`, в чате `Supabase не ответил вовремя` | сетевой сбой: база или прокси не ответили. Бот повторяет запрос сам (`SUPABASE_RETRIES`, по умолчанию 2 повтора — до трёх попыток); если и это не помогло, проверьте доступность базы (`python bot.py --check`) и при необходимости увеличьте `REQUEST_TIMEOUT` |
| Ответы приходят дважды | по одному адресу работают и вебхук, и `python bot.py`: остановите постоянный процесс |
| В группе бот молчит | так задумано: нужен «@бот …», «/команда» в начале сообщения («/help», «/debts@бот») или ответ на сообщение бота. Чтобы он разбирал любые сообщения — `REQUIRE_MENTION=0` и privacy mode **Disable** у @BotFather (`/setprivacy`), иначе Telegram не отдаёт боту обычные сообщения |
| На PythonAnywhere `--check` ругается на DeepSeek/Supabase (403/503, ошибка прокси) | домен не в allowlist бесплатного аккаунта: заявка на добавление, платный аккаунт или Vercel |
| На PythonAnywhere в error log `ImportError: No module named webhook` | в WSGI-файле неверный путь к проекту (должен быть `/home/USERNAME/debt_calculator`) или не сделан **Reload** |
| `Таблица не найдена: выполните db/schema.sql` | не применена свежая схема: выполните `db/schema.sql` в Supabase → SQL Editor (таблица добавляется идемпотентно). В скобках бот показывает ответ PostgREST: `PGRST205` — таблицы нет в схеме |
| `column is_registered does not exist` или `column group_id does not exist` | схема в Supabase старее кода: выполните `db/schema.sql` ещё раз — колонки и тип `kind = 'expense'` добавляются идемпотентно |
| Хочется «пинговать» сервис, чтобы не остывал | `GET https://<адрес>/api/telegram` отвечает `ok` — годится для uptime-мониторов; заодно такой пинг запускает автообновление курсов, если уже прошло время `RATES_HOUR` |

Дальше в этом файле описан **способ 1** — постоянный процесс на панели хостинга.

## Что подготовить

* проект Supabase с применённым `db/schema.sql` (таблицы `debts`, `chat_members`,
  `currency_rates`, `bot_settings`, `bot_state`);
* ключ DeepSeek;
* токен бота от `@BotFather`;
* `SUPABASE_SECRET_KEY` — **secret-ключ базы** `sb_secret_…` (не publishable/anon!),
  иначе запись блокирует RLS; прежний legacy-ключ service_role в `SUPABASE_SERVICE_KEY`
  тоже принимается;
* необязательно: `CHAT_PASSWORD` (пароль для чатов) и `RATES_API_KEY` (ключ ExchangeRate-API
  для `/d` и `/rates`; без ключа работает открытый эндпоинт `open.er-api.com`). Курсы бот
  подтягивает сам раз в день в 12:00 по Минску — час задаётся в `RATES_HOUR` (0–23),
  cron и ручные запуски не нужны.

## Шаги на HidenCloud (панель в стиле Pterodactyl)

1. **Создать сервер.** Обычно: категория «Bot» / «Python», egg «Python» (или Generic),
   RAM ≥ 512 МБ, диск ≥ 1 ГБ, ближайший регион. Проверьте, что панель показывает Python 3.10+
   (в bash-консоли: `python3 --version`).
2. **Скопировать файлы** из ветки `hosting` в панель — через встроенный **File Manager** или **SFTP**:
   ```
   bot.py  config.py  debts.py  deepseek.py  storage.py  telegram_api.py
   requirements.txt  start.sh  Procfile
   webhook.py  api/telegram.py  vercel.json   (нужны только для режима вебхука)
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
   SUPABASE_SECRET_KEY=sb_secret_...      # secret-ключ (Supabase → API Keys → «Publishable and secret API keys»)
   DEFAULT_CURRENCY=BYN
   ALLOWED_USER_IDS=                        # пусто = отвечает всем; иначе список id через запятую
   REQUIRE_MENTION=1                        # в группах — только по обращению «@бот …»
   LOG_LEVEL=INFO
   WEBHOOK_SECRET=                          # нужен только для режима вебхука (Способ 2)
   ```
5. **Start** в панели и смотрите консоль:
   ```
   == Python: Python 3.12.x ==
   == Установка зависимостей ==
   == Проверка настроек и сервисов ==
   ✓ Telegram: @ваш_бот (id ...)
   ✓ DeepSeek: ключ принят, модель deepseek-chat
   • Ключ базы: secret-ключ (sb_secret_…) — то, что нужно (заголовки: apikey)
   ✓ Supabase: таблица debts доступна
   ✓ Supabase: запись разрешена (проба ничего не меняет в базе)
   • Проверка заголовков ключа (какой вариант принимает база):
     • только apikey — база ответила, строк: 0
     • apikey + Authorization — база ответила, строк: 0
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
   Если бот работает не через `start.sh`, а вебхуком (PythonAnywhere, Vercel), зависимости
   ставятся отдельно: `pip install -r requirements.txt` в окружении веб-приложения и Reload —
   иначе в логах будет `ModuleNotFoundError: No module named 'supabase'`.
3. Если код обновился до версии с регистрацией участников (`/reg`), паролем чата, общими счетами
   и курсами валют — **повторно выполните `db/schema.sql`** в Supabase → SQL Editor: добавятся
   таблица `currency_rates`, колонки `chat_members.is_registered`, `debts.group_id`,
   `bot_settings.is_authorized` и тип записи `kind = 'expense'`. Заодно колонка
   `currency_rates.rate` переведётся из `numeric(20, 8)` в `bigint` (`курс × 10⁸`) —
   скрипт идемпотентный, данные не теряются.

Локально перед загрузкой полезно прогнать проверки:
```powershell
python -m unittest tests.test_pipeline   # 263 теста, без внешних сервисов
python bot.py --check                    # проверка ключей и сервисов
```

## Диагностика и типичные проблемы

| Симптом | Причина и что делать |
|---|---|
| `HTTP 409 Conflict` | работает второй «слушатель»: остановите локальный `python bot.py` (или второй процесс в панели) — Telegram отдаёт апдейты только одному |
| `Sweep...` / бот не отвечает, в логе «Новых сообщений нет» | процесс живёт, но ключи неверны — смотрите вывод `python bot.py --check` в консоли сервера |
| `Таблица не найдена` | в Supabase не применён `db/schema.sql` (нужны `debts`, `chat_members`, `currency_rates`, `bot_settings`, `bot_state`) |
| `ключ базы — publishable (публичный)` / `HTTP 401` | в переменных окружения публичный ключ; нужен **secret-ключ**: Supabase → Project Settings → API Keys → «Publishable and secret API keys» → Secret keys → `sb_secret_…` |
| `Supabase отклонил ключ или доступ` (в ответе `42501`, `permission denied`, `row-level security`) | запрос ушёл от роли `anon`, а не `service_role`: в переменных окружения публичный ключ (publishable/anon) или ключ от другого проекта. Возьмите **secret-ключ** `sb_secret_…` (не publishable) и проверьте `python bot.py --check` — он печатает тип ключа и отдельно проверяет, разрешает ли база запись |
| `permission denied for schema public` (42501) | ключ принят, но у роли нет прав на схему: выполните **`db/grants.sql`** в Supabase → SQL Editor (выдаёт права роли `service_role`, которой соответствует secret-ключ) и повторите `python bot.py --check`. Если права выданы, а ошибка осталась — проверьте, что ключ и `SUPABASE_URL` от одного проекта |
| `ModuleNotFoundError: No module named 'supabase'` (или другого пакета из `requirements.txt`) | новая зависимость не установлена в том окружении, из которого работает веб-приложение: активируйте своё окружение (`workon debt-bot`), затем `cd ~/debt_calculator && pip install -r requirements.txt`, проверьте `python -c "import supabase"`, убедитесь, что в *Web → Virtualenv* указано то же окружение, и нажмите **Reload**. На панелях с `start.sh` зависимости ставятся сами при Restart |
| Бот просит пароль | задан `CHAT_PASSWORD`: пришлите `/password ваш-пароль` (пароль задаётся в окружении хостинга) |
| `/d` пишет «Курсов за эти даты нет» | не задан `RATES_API_KEY` (и не задан `RATES_OPEN_URL`) или курсы ещё не обновлялись: подождите ближайшие 12:00 по Минску либо выполните `python bot.py --rates` |
| Курсы обновились не в 12:00 | на постоянном процессе обновление случается при первой проверке после 12:00 (обычно в течение получаса), а на вебхуке — при первом апдейте или пинге `GET /`; час задаётся в `RATES_HOUR` |
| Ответ приходит с задержкой | хостинг «усыпляет» процесс (у бесплатных тарифов бывает авто-сон) — см. раздел ниже |
| Контейнер перезапускается сам | смотрите лимиты RAM/CPU тарифа; боту достаточно 256–512 МБ, но панель может ограничивать жёстче |

Вместо захода в панель настройки можно проверить командой `python bot.py --check` в консоли
хостинга: она печатает статус Telegram, DeepSeek, Supabase, таблицы курсов и признак пароля.

## Если хостинг усыпляет процесс

Бесплатные тарифы иногда останавливают контейнер при простое. Варианты:

* платный тариф того же хостинга (или VPS) — самый предсказуемый;
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
* **Android + Termux**: `pkg install python`, `pip install -r requirements.txt`, затем
  `termux-wake-lock && python bot.py &` (телефон должен быть включён).

## Быстрый переезд между режимами

Смещение обработанных сообщений хранится в `bot_state.last_update_id`, поэтому при переключении
между вебхуком и long polling ничего не теряется и не дублируется: оба режима читают одно и то же
значение.

При переходе **на вебхук** не забудьте остановить постоянный процесс (иначе Telegram будет
доставлять апдейты в двух местах), а при возврате **с вебхука** — снять его:

```powershell
python bot.py --delete-webhook     # Telegram снова отдаёт апдейты через getUpdates
```

