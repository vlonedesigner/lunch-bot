#!/usr/bin/env python3
"""
Telegram-бот «Обеды».

- Каждый сотрудник при /start выбирает своё имя (лист в Google-таблице).
- Каждый будний день в NOTIFY_TIME бот присылает ему его обед на сегодня.
- Имя можно сменить, уведомления выключить (⚙️ Настройки).
- Можно смотреть расписание любого другого сотрудника (👥 Другие).

Таблица читается напрямую (публичный экспорт CSV), список сотрудников бот
берёт из самой таблицы: новый лист появится в боте сам.

Только стандартная библиотека Python 3.9+.
"""

import csv
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------- НАСТРОЙКИ (переменные окружения) ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
NOTIFY_TIME = os.environ.get("NOTIFY_TIME", "15:00")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
NOTIFY_ON_EMPTY_DAYS = os.environ.get("NOTIFY_ON_EMPTY_DAYS", "0") == "1"
EXCLUDE_SHEETS = {s.strip() for s in os.environ.get("EXCLUDE_SHEETS", "Шаблон").split(",") if s.strip()}
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
CSV_CACHE_SECONDS = 300
SHEETS_CACHE_SECONDS = 1800
# ------------------------------------------------------------------

BASE_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/"
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]

BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_ALL = "🍱 Сегодня", "➡️ Завтра", "📅 Неделя", "🗓 Всё"
BTN_OTHERS, BTN_SETTINGS = "👥 Другие", "⚙️ Настройки"
KEYBOARD = {
    "keyboard": [[{"text": BTN_TODAY}, {"text": BTN_TOMORROW}],
                 [{"text": BTN_WEEK}, {"text": BTN_ALL}],
                 [{"text": BTN_OTHERS}, {"text": BTN_SETTINGS}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
MODES = {"today": "Сегодня", "tomorrow": "Завтра", "week": "Неделя", "all": "Всё"}


def now() -> datetime:
    return datetime.now(TZ)


def log(msg):
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ======================= ЧТЕНИЕ ТАБЛИЦЫ =======================

def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 lunch-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def _num(s):
    s = (s or "").strip().replace(",", ".").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def resolve_header_date(label: str, today: date):
    """'16, пн' -> date. Шапка таблицы сама сдвигается на следующий месяц, поэтому
    ищем ближайшую к сегодня дату с таким числом И таким днём недели."""
    try:
        day_s, wd_s = [p.strip().lower() for p in label.split(",", 1)]
        day = int(day_s)
        wd = WEEKDAYS.index(wd_s[:2])
    except (ValueError, IndexError):
        return None
    best = None
    for delta in range(-62, 63):
        d = today + timedelta(days=delta)
        if d.day == day and d.weekday() == wd and (best is None or abs(delta) < abs((best - today).days)):
            best = d
    return best


def find_header(rows):
    for i, r in enumerate(rows):
        if r and r[0].strip() == "Наименование":
            return i
    return None


def parse_schedule(csv_text: str, today: date):
    """Лист сотрудника -> ({date: [блюдо, ...]}, [все даты шапки])."""
    rows = list(csv.reader(io.StringIO(csv_text)))
    hi = find_header(rows)
    if hi is None:
        raise ValueError("на листе нет строки «Наименование»")
    header = rows[hi]
    col = {h.strip(): i for i, h in enumerate(header)}
    i_price = col.get("Цена", 7)
    i_k, i_b, i_j, i_u = col.get("К", 2), col.get("Б", 3), col.get("Ж", 4), col.get("У", 5)

    date_cols = {}
    for i, h in enumerate(header):
        if "," in h:
            d = resolve_header_date(h, today)
            if d:
                date_cols[i] = d

    schedule = {}
    for r in rows[hi + 1:]:
        if not r or not r[0].strip():
            continue

        def get(i):
            return r[i] if i < len(r) else ""

        for i, d in date_cols.items():
            qty = _num(get(i))
            if qty and qty > 0:
                schedule.setdefault(d, []).append({
                    "name": r[0].strip(),
                    "qty": int(qty) if qty == int(qty) else qty,
                    "price": _num(get(i_price)),
                    "kcal": _num(get(i_k)), "p": _num(get(i_b)),
                    "f": _num(get(i_j)), "c": _num(get(i_u)),
                })
    return schedule, sorted(set(date_cols.values()))


_SHEET_RE = re.compile(r'items\.push\(\{name: "((?:[^"\\]|\\.)*)", pageUrl: "[^"]*", gid: "(\d+)"')


def _js_unescape(s: str) -> str:
    s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: "\\u00" + m.group(1), s)
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s


def parse_sheet_list(htmlview: str):
    """Все листы таблицы по порядку: [(название, gid)]."""
    return [(_js_unescape(n), g) for n, g in _SHEET_RE.findall(htmlview)]


class Sheets:
    """Кэш таблицы: список сотрудников и их листы."""

    def __init__(self):
        self.lock = threading.Lock()
        self.csv = {}          # gid -> (time, text)
        self.people = []       # [{"name", "gid"}]
        self.people_t = 0.0

    def csv_text(self, gid, force=False):
        with self.lock:
            hit = self.csv.get(gid)
        if hit and not force and time.time() - hit[0] < CSV_CACHE_SECONDS:
            return hit[1]
        text = http_get(BASE_URL + f"export?format=csv&gid={gid}")
        with self.lock:
            self.csv[gid] = (time.time(), text)
        return text

    def schedule(self, gid, force=False):
        return parse_schedule(self.csv_text(gid, force), now().date())

    def list_people(self, force=False):
        if self.people and not force and time.time() - self.people_t < SHEETS_CACHE_SECONDS:
            return self.people
        all_sheets = [(n, g) for n, g in parse_sheet_list(http_get(BASE_URL + "htmlview"))
                      if n.strip() not in EXCLUDE_SHEETS]

        def is_person(item):
            try:
                rows = list(csv.reader(io.StringIO(self.csv_text(item[1], force=True))))
                return find_header(rows) is not None
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=8) as ex:
            flags = list(ex.map(is_person, all_sheets))
        people = [{"name": n.strip(), "gid": g} for (n, g), ok in zip(all_sheets, flags) if ok]
        if people:  # при сбое сети оставляем прежний список
            self.people, self.people_t = people, time.time()
        return self.people

    def name_of(self, gid, fallback="?"):
        if not self.people:  # после перезапуска список ещё не загружен
            try:
                self.list_people()
            except Exception:
                pass
        for p in self.people:
            if p["gid"] == gid:
                return p["name"]
        return fallback


SHEETS = Sheets()


# ======================= ХРАНИЛИЩЕ =======================

class Store:
    """state.json: {"offset": int, "users": {chat_id: {gid, name, notify, last_notified}}}"""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        self.data.setdefault("users", {})
        self.data.setdefault("offset", 0)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def user(self, chat_id):
        return self.data["users"].get(str(chat_id))

    def set_user(self, chat_id, **fields):
        u = self.data["users"].setdefault(str(chat_id), {"notify": True})
        u.update(fields)
        self.save()
        return u

    def remove_user(self, chat_id):
        self.data["users"].pop(str(chat_id), None)
        self.save()


# ======================= ФОРМАТИРОВАНИЕ =======================

def fmt_date(d: date) -> str:
    return f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"


def fmt_money(x) -> str:
    x = x or 0
    return f"{int(x)} ₽" if x == int(x) else f"{x} ₽"


def fmt_day(title, items, detailed=True) -> str:
    if not items:
        return f"<b>{escape(title)}</b>\n— обеда нет"
    lines = [f"<b>{escape(title)}</b>"]
    total_price = total_kcal = 0
    for it in items:
        q = f" ×{it['qty']}" if it["qty"] != 1 else ""
        price = (it["price"] or 0) * it["qty"]
        kcal = (it["kcal"] or 0) * it["qty"]
        total_price += price
        total_kcal += kcal
        if detailed:
            extra = f" · {int(kcal)} ккал" if kcal else ""
            lines.append(f"• {escape(it['name'])}{q} — {fmt_money(price)}{extra}")
        else:
            lines.append(f"• {escape(it['name'])}{q}")
    if detailed:
        p = sum((it["p"] or 0) * it["qty"] for it in items)
        f = sum((it["f"] or 0) * it["qty"] for it in items)
        c = sum((it["c"] or 0) * it["qty"] for it in items)
        lines.append(f"<i>Итого: {fmt_money(total_price)} · {int(total_kcal)} ккал · "
                     f"Б {int(p)} / Ж {int(f)} / У {int(c)}</i>")
    else:
        lines[-1] += f"  <i>({fmt_money(total_price)})</i>"
    return "\n".join(lines)


def _next_after(schedule, d):
    later = sorted(x for x in schedule if x > d)
    return later[0] if later else None


def render(gid: str, person: str, mode: str) -> str:
    """Расписание сотрудника: mode = today | tomorrow | week | all."""
    schedule, _ = SHEETS.schedule(gid)
    today = now().date()
    who = f"👤 {escape(person)}\n"

    if mode == "today":
        items = schedule.get(today, [])
        text = who + fmt_day(f"🍱 Сегодня — {fmt_date(today)}", items)
        if not items and (nxt := _next_after(schedule, today)):
            text += f"\n\nСледующий обед — {fmt_date(nxt)}."
        return text

    if mode == "tomorrow":
        d = today + timedelta(days=1)
        items = schedule.get(d, [])
        text = who + fmt_day(f"➡️ Завтра — {fmt_date(d)}", items)
        if not items and (nxt := _next_after(schedule, d)):
            text += "\n\n" + fmt_day(f"Следующий обед — {fmt_date(nxt)}", schedule[nxt])
        return text

    if mode == "week":
        start = today - timedelta(days=today.weekday())
        if today.weekday() >= 5:
            start += timedelta(days=7)
        end = start + timedelta(days=4)
        parts = [who + f"📅 <b>Неделя {start:%d.%m} – {end:%d.%m}</b>"]
        for i in range(5):
            d = start + timedelta(days=i)
            mark = " 👈 сегодня" if d == today else ""
            parts.append(fmt_day(fmt_date(d).capitalize() + mark, schedule.get(d, []), detailed=False))
        return "\n\n".join(parts)

    # all
    days = [d for d in sorted(schedule) if d >= today]
    if not days:
        return who + "Дальше заказов в таблице нет."
    parts = [who + "🗓 <b>Все заказанные обеды</b>"]
    for d in days:
        mark = " 👈 сегодня" if d == today else ""
        parts.append(fmt_day(fmt_date(d).capitalize() + mark, schedule[d], detailed=False))
    total = sum((it["price"] or 0) * it["qty"] for d in days for it in schedule[d])
    parts.append(f"<i>Всего впереди: {len(days)} дн., {fmt_money(total)}</i>")
    return "\n\n".join(parts)


def clip(text, limit=4000):
    """Telegram ограничивает сообщение 4096 символами — режем по границе дня."""
    if len(text) <= limit:
        return text
    out = ""
    for block in text.split("\n\n"):
        if len(out) + len(block) + 4 > limit:
            return out + "\n\n…"
        out = f"{out}\n\n{block}" if out else block
    return out


# ======================= TELEGRAM =======================

def tg(method, **params):
    data = urllib.parse.urlencode(
        {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
         for k, v in params.items()}).encode()
    try:
        with urllib.request.urlopen(API + method, data=data, timeout=int(params.get("timeout", 0)) + 15) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        res = json.loads(e.read().decode() or "{}")
    if not res.get("ok"):
        raise TelegramError(res.get("error_code"), res.get("description", ""))
    return res["result"]


class TelegramError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"{code}: {desc}")
        self.code, self.desc = code, desc


def send(chat_id, text, markup=None):
    return tg("sendMessage", chat_id=chat_id, text=clip(text), parse_mode="HTML",
              reply_markup=markup or KEYBOARD, disable_web_page_preview="true")


def edit(chat_id, message_id, text, markup):
    try:
        tg("editMessageText", chat_id=chat_id, message_id=message_id, text=clip(text),
           parse_mode="HTML", reply_markup=markup, disable_web_page_preview="true")
    except TelegramError as e:
        if "message is not modified" not in e.desc:
            raise


def picker_markup(action: str, current_gid=None):
    """Список сотрудников кнопками, по 2 в ряд. action: set (выбрать себя) | view (смотреть)."""
    people = SHEETS.list_people()
    buttons = [{"text": ("✅ " if p["gid"] == current_gid else "") + p["name"],
                "callback_data": f"{action}:{p['gid']}"} for p in people]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


def view_markup(gid, mode):
    row = [{"text": ("• " if m == mode else "") + title, "callback_data": f"show:{gid}:{m}"}
           for m, title in MODES.items()]
    return {"inline_keyboard": [row, [{"text": "« к списку", "callback_data": "others"}]]}


def settings_text_markup(u):
    notif = u.get("notify", True)
    text = (f"⚙️ <b>Настройки</b>\n\nТы: <b>{escape(u['name'])}</b>\n"
            f"Уведомление в {NOTIFY_TIME}: {'включено' if notif else 'выключено'}")
    markup = {"inline_keyboard": [
        [{"text": "👤 Сменить имя", "callback_data": "choose"}],
        [{"text": "🔕 Выключить уведомления" if notif else "🔔 Включить уведомления",
          "callback_data": "notify"}],
    ]}
    return text, markup


# ======================= ОБРАБОТЧИКИ =======================

MSG_MODES = {
    BTN_TODAY: "today", "/today": "today",
    BTN_TOMORROW: "tomorrow", "/tomorrow": "tomorrow",
    BTN_WEEK: "week", "/week": "week",
    BTN_ALL: "all", "/all": "all",
}
CHOOSE_TEXT = "Выбери себя в списке — по этому листу я буду присылать тебе обеды:"


def handle_message(msg, store: Store):
    chat_id = msg["chat"]["id"]
    if msg["chat"].get("type") != "private":
        return
    text = (msg.get("text") or "").strip()
    cmd = text.split("@")[0].split()[0] if text.startswith("/") else text
    u = store.user(chat_id)

    if cmd == "/start" or (not u and cmd not in (BTN_OTHERS, "/others")):
        if u:
            send(chat_id, f"Ты — <b>{escape(u['name'])}</b>. Каждый будний день в {NOTIFY_TIME} "
                          f"пришлю твой обед. Сменить имя — в ⚙️ Настройках.")
        else:
            send(chat_id, "Привет! Я присылаю обеды из таблицы «Маранг».",
                 markup={"remove_keyboard": True})
            send(chat_id, CHOOSE_TEXT, markup=picker_markup("set"))
        return

    if cmd in MSG_MODES:
        send(chat_id, render(u["gid"], SHEETS.name_of(u["gid"], u["name"]), MSG_MODES[cmd]))
    elif cmd in (BTN_OTHERS, "/others"):
        send(chat_id, "Чей лист посмотреть?", markup=picker_markup("view"))
    elif cmd in (BTN_SETTINGS, "/settings"):
        t, m = settings_text_markup(u)
        send(chat_id, t, markup=m)
    else:
        send(chat_id, "Не понял. Жми кнопки снизу 👇")


def handle_callback(cb, store: Store):
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    data = cb.get("data", "")
    action, _, arg = data.partition(":")
    toast = None

    if action == "set":
        name = SHEETS.name_of(arg, None)
        if not name:
            toast = "Не нашёл такого листа, обнови список"
        else:
            first = store.user(chat_id) is None
            store.set_user(chat_id, gid=arg, name=name)
            edit(chat_id, message_id, f"Готово: ты — <b>{escape(name)}</b>.", None)
            hello = (f"Каждый будний день в {NOTIFY_TIME} буду присылать твой обед. "
                     "Кнопки снизу 👇") if first else "Имя изменено."
            send(chat_id, hello)
    elif action == "choose":
        u = store.user(chat_id)
        edit(chat_id, message_id, CHOOSE_TEXT, picker_markup("set", u and u.get("gid")))
    elif action == "notify":
        u = store.user(chat_id)
        if u:
            u = store.set_user(chat_id, notify=not u.get("notify", True))
            t, m = settings_text_markup(u)
            edit(chat_id, message_id, t, m)
            toast = "Уведомления включены" if u["notify"] else "Уведомления выключены"
    elif action == "others":
        edit(chat_id, message_id, "Чей лист посмотреть?", picker_markup("view"))
    elif action == "view":
        edit(chat_id, message_id, render(arg, SHEETS.name_of(arg), "week"), view_markup(arg, "week"))
    elif action == "show":
        gid, _, mode = arg.partition(":")
        if mode in MODES:
            edit(chat_id, message_id, render(gid, SHEETS.name_of(gid), mode), view_markup(gid, mode))

    params = {"callback_query_id": cb["id"]}
    if toast:
        params["text"] = toast
    try:
        tg("answerCallbackQuery", **params)
    except TelegramError:
        pass


def notify_all(store: Store):
    n = now()
    hh, mm = map(int, NOTIFY_TIME.split(":"))
    target = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (target <= n < target + timedelta(hours=1)):
        return
    today_s = n.date().isoformat()
    due = [(cid, u) for cid, u in list(store.data["users"].items())
           if u.get("notify", True) and u.get("last_notified") != today_s]
    if not due:
        return
    for cid, u in due:
        try:
            schedule, _ = SHEETS.schedule(u["gid"], force=True)
            if schedule.get(n.date()) or NOTIFY_ON_EMPTY_DAYS:
                send(int(cid), render(u["gid"], SHEETS.name_of(u["gid"], u["name"]), "today"))
            u["last_notified"] = today_s
        except TelegramError as e:
            if e.code == 403:  # пользователь заблокировал бота
                log(f"{cid} заблокировал бота — удаляю")
                store.data["users"].pop(cid, None)
            else:
                log(f"уведомление {cid}: {e}")
        except Exception as e:
            log(f"уведомление {cid}: {e}")  # таблица недоступна — попробуем на следующем круге
    store.save()


def dispatch(upd, store: Store):
    try:
        if "message" in upd:
            handle_message(upd["message"], store)
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"], store)
    except (urllib.error.URLError, ValueError) as e:
        chat = (upd.get("message") or upd.get("callback_query", {}).get("message") or {}).get("chat", {})
        log(f"ошибка обработки: {e}")
        if chat.get("id"):
            try:
                send(chat["id"], "Не смог прочитать таблицу, попробуй через минуту.")
            except Exception:
                pass


def main():
    if not BOT_TOKEN or not SHEET_ID:
        sys.exit("Нужны переменные окружения BOT_TOKEN и SHEET_ID")
    store = Store(STATE_FILE)
    tg("setMyCommands", commands=[
        {"command": "today", "description": "Мой обед на сегодня"},
        {"command": "tomorrow", "description": "Мой обед на завтра"},
        {"command": "week", "description": "Моя неделя"},
        {"command": "all", "description": "Всё моё расписание"},
        {"command": "others", "description": "Посмотреть чужие листы"},
        {"command": "settings", "description": "Сменить имя, уведомления"},
    ])
    people = SHEETS.list_people()
    log(f"Бот запущен. Сотрудников в таблице: {len(people)}, пользователей: {len(store.data['users'])}, "
        f"уведомление в {NOTIFY_TIME} ({TZ.key})")
    while True:
        try:
            updates = tg("getUpdates", offset=store.data["offset"], timeout=25,
                         allowed_updates=["message", "callback_query"])
            for upd in updates:
                store.data["offset"] = upd["update_id"] + 1
                dispatch(upd, store)
            if updates:
                store.save()
            notify_all(store)
        except KeyboardInterrupt:
            break
        except TelegramError as e:
            log(f"Telegram: {e}")
            time.sleep(30 if e.code == 409 else 5)  # 409 — бот с этим токеном запущен где-то ещё
        except Exception as e:
            log(f"ошибка: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
