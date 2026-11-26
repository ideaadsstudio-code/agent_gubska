# Развёртывание и подключение

Для тестов бот можно запустить на своём компьютере (раздел 0), для настоящих клиентов нужен хостинг (разделы 1–4).
Секреты вводятся только в `.env` на своём компьютере или в Variables на Railway. Никогда не пересылайте их в чаты
(Telegram, мессенджеры, переписку с Claude): если секрет туда попал, отключите его и создайте новый.

## 0. Запуск на своём компьютере

Нужны Python 3.11+ (https://www.python.org/downloads/) и git.

```bash
git clone https://github.com/ideaadsstudio-code/agent_gubska.git
```

```bash
cd agent_gubska
```

```bash
git checkout claude/bold-sagan-5oza0q
```

```bash
python -m venv .venv
```

Активировать окружение: на macOS и Linux `source .venv/bin/activate`, на Windows `.venv\Scripts\activate`.

```bash
pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` и заполните минимум:
`ANTHROPIC_API_KEY` (ключ из https://platform.claude.com), `PLAYGROUND_TOKEN` (любой пароль), `STUDIO_NAME`.
Файл `.env` не попадает в git.

```bash
uvicorn app.main:create_app --factory --port 8000
```

Тестовый чат: http://localhost:8000/playground?token=<PLAYGROUND_TOKEN>. Остановить сервер: Ctrl+C.

### Telegram и Instagram локально: туннель

Telegram и Meta должны достучаться до компьютера из интернета. Для этого нужен туннель с публичным HTTPS-адресом.

1. Зарегистрируйтесь на https://ngrok.com, установите ngrok и возьмите бесплатный постоянный домен
   (Dashboard → Domains).
2. Запустите в отдельном окне терминала, сервер при этом должен работать:

```bash
ngrok http 8000 --url=https://<ваш-домен>.ngrok-free.app
```

3. В `.env` добавьте `PUBLIC_URL=https://<ваш-домен>.ngrok-free.app`, а также переменные Telegram из раздела 2
   и Instagram из раздела 4. Перезапустите сервер: webhook поставятся сами.
4. В Meta используйте этот адрес вместо адреса Railway.

Бот работает, только пока включены компьютер, сервер и ngrok. Для настоящих клиентов переходите на Railway (раздел 1):
код менять не нужно, переменные из `.env` переносятся в Variables.


## 1. Railway (около 30 минут)

1. Зарегистрируйтесь на https://railway.com через GitHub и включите тариф **Hobby** ($5 в месяц).
2. **New Project → Deploy from GitHub repo →** репозиторий `agent_gubska`, ветка с ботом.
   Railway сам найдёт `requirements.txt` и `railway.json` (команда запуска, проверка `/health`, одна копия сервиса).
3. **Диск для базы:** в сервисе → **Volumes → New Volume**, путь монтирования `/data`.
4. **Публичный адрес:** Settings → Networking → **Generate Domain**. Получится `https://<имя>.up.railway.app`.
   Сервер сам узнаёт его из `RAILWAY_PUBLIC_DOMAIN`.
5. **Variables** (пока без Instagram):

   | Переменная | Значение |
   |---|---|
   | `DB_PATH` | `/data/agent.db` |
   | `ANTHROPIC_API_KEY` | ключ из https://platform.claude.com (Settings → API keys) |
   | `TELEGRAM_BOT_TOKEN` | токен от @BotFather (шаг 2) |
   | `TELEGRAM_WEBHOOK_SECRET` | случайная строка 30–40 символов, **только латинские буквы и цифры** (Telegram не принимает спецсимволы) |
   | `IG_VERIFY_TOKEN` | ещё одна такая же строка из букв и цифр (понадобится в Meta) |
   | `PLAYGROUND_TOKEN` | пароль тестового чата, только буквы и цифры: он вставляется в адрес страницы |
   | `STUDIO_NAME` | название студии для текстов политики |
   | `PRIVACY_CONTACT` | почта для запросов по данным |
   | `APPROVAL_MODE` | `true` |

6. Проверьте в браузере: `https://<адрес>/health` отвечает `{"ok":true,...}`, открываются `/privacy` и `/data-deletion`.

Сервис должен работать в **одной копии**: база SQLite и очередь диалогов живут внутри одного процесса.

## 2. Telegram (15 минут)

1. В Telegram откройте **@BotFather** → `/newbot`, задайте имя. Токен вставьте в `TELEGRAM_BOT_TOKEN` на Railway.
2. Создайте группу с менеджером и добавьте туда бота.
3. Railway перезапустит сервис, и при старте бот сам поставит свой webhook.
4. Напишите в группе `/chatid`. Бот ответит числом вида `-100…`. Вставьте его в `TELEGRAM_CHAT_ID`.
5. После перезапуска в группе начнут работать кнопки и команды:
   - `/personal @ник`: бот никогда не трогает этот контакт;
   - `/bot @ник`: отдать боту диалог, который фильтр оставил мастеру;
   - `/forget @ник`: удалить все данные клиента по его просьбе.

## 3. Тестовый чат (сразу после шагов 1–2)

Откройте `https://<адрес>/playground?token=<PLAYGROUND_TOKEN>` с телефона или компьютера и пишите как клиент.
Бот отвечает настоящей моделью с текущей базой знаний, карточки «менеджеру» показываются прямо в чате.
В Instagram и Telegram ничего не уходит, расход попадает в общую сводку.

## 4. Meta (1–2 дня)

Перед началом проверьте в Instagram мастера:
- аккаунт профессиональный (бизнес или автор);
- Настройки → Сообщения и ответы на истории → Управление сообщениями → **Подключённые инструменты** → включено **«Разрешить доступ к сообщениям»**.

Дальше:
1. https://developers.facebook.com/apps → **Создать приложение** → сценарий «Управление сообщениями и контентом в Instagram».
2. В разделе **API setup with Instagram login** скопируйте **Instagram app ID** → `IG_APP_ID`
   и **Instagram app secret** → `IG_APP_SECRET` (в Railway → Variables).
3. Там же блок **Set up Instagram business login → Business login settings → OAuth redirect URIs**:
   добавьте `https://<адрес>/auth/instagram/callback` и сохраните.
4. **Подключение аккаунта мастера по ссылке:** откройте `https://<адрес>/auth/instagram/start?token=<PLAYGROUND_TOKEN>`,
   скопируйте ссылку и отправьте мастеру. Она открывает её на телефоне и нажимает «Разрешить».
   Сервер сам сохранит токен (60 дней, продлевается автоматически) и подпишет аккаунт на сообщения,
   в Telegram придёт «Instagram подключён». Пароль мастера никому не передаётся.
   Запасной вариант: «Add account» → «Generate token» в панели Meta и токен в `IG_ACCESS_TOKEN`.
5. **Webhooks:** Callback URL `https://<адрес>/webhooks/instagram`, Verify token — значение `IG_VERIFY_TOKEN`.
   Подпишитесь на поля `messages` и `messaging_referral`.
   Подписку самого аккаунта (`subscribed_apps`) сервер делает сам после подключения и при каждом старте.
6. **Роли → Тестировщики Instagram:** добавьте 1–2 личных аккаунта. Приглашение принимается в Instagram:
   Настройки → Приложения и сайты → Приглашения тестировщика.
7. **Настройки приложения → Основное:** иконка, категория, Privacy Policy URL `https://<адрес>/privacy`,
   User data deletion → Data deletion instructions URL `https://<адрес>/data-deletion`.
8. Задайте `BOT_LAUNCH_DATE` (сегодняшняя дата, например `2026-12-01`): переписка до этой даты остаётся мастеру.
9. Переключите приложение в режим **Live**.

### Проверка, нужны ли App Review и верификация бизнеса

1. Напишите мастеру с **тестового аккаунта** (с ролью) вопрос про тату. Бот должен прислать черновик в Telegram.
2. Напишите с **аккаунта без роли** в приложении, например попросите знакомого.
   - Черновик пришёл: Standard Access хватает, App Review и верификация не нужны.
   - Ничего не пришло: нужен App Review (Advanced Access для `instagram_business_manage_messages`),
     а для него верификация бизнеса. Тогда готовим заявку.
   - Meta не даёт включить Live без верификации: начинаем с неё.

## 5. Анализ старой переписки

На своём компьютере, с Python 3.11+ и ключом API в переменной `ANTHROPIC_API_KEY`:

```bash
pip install -r requirements.txt
python scripts/analyze_export.py путь/к/instagram-export.zip --dry-run
python scripts/analyze_export.py путь/к/instagram-export.zip --yes
```

`--dry-run` ничего не отправляет в API и показывает объём и оценку стоимости. Результат появляется в `analysis_out/`:
черновик базы знаний, стиль мастера, тестовые вопросы и список возможных личных контактов для `/personal`.
После переноса нужного в `knowledge/knowledge_base.md` удалите архив и `analysis_out/`.
Архив можно и загрузить в чат с Claude Code: тогда анализ запущу я.
