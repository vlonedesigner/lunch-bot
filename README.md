# lunch-bot

Telegram-бот для обедов из Google-таблицы «Маранг».

- При `/start` сотрудник выбирает себя из списка (список = листы сотрудников в таблице, новые подхватываются сами).
- Каждый будний день в 15:00 (МСК) каждому приходит его обед на сегодня. В дни без заказа — тишина.
- Кнопки: **Сегодня / Завтра / Неделя / Всё** — своё расписание; **👥 Другие** — расписание любого сотрудника; **⚙️ Настройки** — сменить имя, выключить уведомления.

## Пайплайн

```
push в main ─▶ GitHub Actions: тесты ─▶ build (amd64+arm64) ─▶ Docker Hub <user>/lunch-bot:latest
                                                                       │
                                               сервер: Watchtower ◀────┘ подтягивает и перезапускает
```

Если тесты упали, образ не публикуется, и на сервере остаётся рабочая версия.

---

## Деплой на сервер (для админа)

Требуется: Docker + Compose, Watchtower уже работает на сервере.

```bash
mkdir -p /opt/lunch-bot && cd /opt/lunch-bot
# положить сюда deploy/docker-compose.yml и заполненный .env (шаблон: deploy/.env.example)
docker compose up -d
docker logs -f lunch-bot     # ожидается: «Бот запущен. Сотрудников в таблице: N ...»
```

Дальше обновления приходят сами через Watchtower, руками ничего делать не нужно.

| | |
|---|---|
| Состояние (кто есть кто, настройки) | volume `lunch-bot-data` → `/data/state.json` |
| Входящие порты | не нужны (long polling, только исходящие запросы к api.telegram.org и docs.google.com) |
| Ресурсы | ~30 МБ RAM, процессор почти не нагружается |
| Watchtower в режиме `--label-enable` | метка `com.centurylinklabs.watchtower.enable=true` уже стоит в compose |

**Важно:** на одном токене может работать только один экземпляр бота. Если он запущен где-то ещё, в логах будет `409 Conflict`.

---

## Настройка CI (один раз, владелец репозитория)

1. Docker Hub → Account settings → Personal access tokens → токен с правами **Read & Write**.
2. GitHub → репозиторий → Settings → Secrets and variables → Actions → New repository secret:
   - `DOCKERHUB_USERNAME` — логин Docker Hub
   - `DOCKERHUB_TOKEN` — токен из п.1
3. Push в `main` → вкладка Actions: зелёный прогон → на Docker Hub появляется образ `lunch-bot`.

## Переменные окружения

| Переменная | Обязательна | По умолчанию | |
|---|---|---|---|
| `BOT_TOKEN` | да | | токен @BotFather |
| `SHEET_ID` | да | | ID Google-таблицы |
| `NOTIFY_TIME` | | `15:00` | время уведомления |
| `TZ_NAME` | | `Europe/Moscow` | часовой пояс |
| `NOTIFY_ON_EMPTY_DAYS` | | `0` | `1` — присылать «обеда нет» в дни без заказа |
| `EXCLUDE_SHEETS` | | `Шаблон` | листы через запятую, которые не считать сотрудниками |
| `STATE_FILE` | | `/data/state.json` | |

## Локально

```bash
python3 tests/test_parse.py                               # тесты, без сети
BOT_TOKEN=... SHEET_ID=... STATE_FILE=./state.json python3 bot.py
```
