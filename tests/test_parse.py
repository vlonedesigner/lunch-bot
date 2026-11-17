"""Тесты без сети: разбор таблицы и сценарии в Telegram на заглушках.
Запускаются в CI перед сборкой образа — если что-то падает, образ не публикуется."""
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("SHEET_ID", "SHEETID")
os.environ.setdefault("BOT_TOKEN", "TEST")
import bot  # noqa: E402

PERSON_CSV = (HERE / "fixture.csv").read_text(encoding="utf-8")
HTMLVIEW = (HERE / "htmlview_fixture.html").read_text(encoding="utf-8")
SERVICE_CSV = "Наименование?,нет\nМеню,1\n"  # служебный лист: нет строки «Наименование»


def day_total(schedule, d):
    return sum(i["price"] * i["qty"] for i in schedule.get(d, []))


# ---------- разбор таблицы ----------

def test_dates_and_totals():
    schedule, dates = bot.parse_schedule(PERSON_CSV, date(2026, 11, 17))
    assert dates[0] == date(2026, 11, 16) and dates[-1] == date(2026, 12, 13) and len(dates) == 28
    assert day_total(schedule, date(2026, 11, 17)) == 700   # совпадает со строкой «Сумма» в таблице
    assert day_total(schedule, date(2026, 11, 18)) == 485
    assert day_total(schedule, date(2026, 12, 1)) == 410
    assert date(2026, 11, 21) not in schedule               # выходные пустые


def test_month_rollover():
    schedule, _ = bot.parse_schedule(PERSON_CSV, date(2026, 12, 4))
    assert day_total(schedule, date(2026, 12, 4)) == 470
    assert day_total(schedule, date(2026, 11, 30)) == 540


def test_sheet_list():
    sheets = bot.parse_sheet_list(HTMLVIEW)
    assert len(sheets) == 27
    assert ("Али Дизайнер", "2010") in sheets and ("Меню", "503595993") in sheets


# ---------- сценарии в Telegram (сеть заглушена) ----------

class FakeTG:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    def __call__(self, method, **p):
        self.calls.append((method, p))
        self.next_id += 1
        return {"message_id": self.next_id}

    def texts(self, chat_id=None):
        return [p.get("text", "") for m, p in self.calls
                if m in ("sendMessage", "editMessageText") and (chat_id is None or p.get("chat_id") == chat_id)]

    def last(self):
        return [c for c in self.calls if c[0] != "answerCallbackQuery"][-1]


def setup(fake_now):
    service = {"503595993", "10001", "259985880", "952299015", "675072832"}

    def http_get(url):
        if url.endswith("htmlview"):
            return HTMLVIEW
        gid = url.rsplit("gid=", 1)[1]
        return SERVICE_CSV if gid in service else PERSON_CSV

    bot.http_get = http_get
    bot.now = lambda: fake_now
    bot.SHEETS = bot.Sheets()
    fake = FakeTG()
    bot.tg = fake
    store = bot.Store(Path(tempfile.mkdtemp()) / "state.json")
    return fake, store


def msg(chat, text):
    return {"message": {"chat": {"id": chat, "type": "private"}, "text": text}}


def cb(chat, data, mid=1):
    return {"callback_query": {"id": "q", "data": data, "message": {"chat": {"id": chat}, "message_id": mid}}}


def test_people_filter():
    setup(datetime(2026, 11, 17, 12, tzinfo=bot.TZ))
    names = [p["name"] for p in bot.SHEETS.list_people()]
    assert "Али Дизайнер" in names and "Хадя" in names
    for service in ("Меню", "Сводный заказ", "Сводка ноябрь", "Лист16", "Шаблон", "скрипт"):
        assert service not in names
    assert len(names) == 21


def test_onboarding_change_view_notify():
    fake, store = setup(datetime(2026, 11, 17, 12, tzinfo=bot.TZ))
    A, B = 111, 222

    # новый пользователь получает список имён
    bot.dispatch(msg(A, "/start"), store)
    m, p = fake.last()
    assert p["text"] == bot.CHOOSE_TEXT
    buttons = [b["callback_data"] for row in p["reply_markup"]["inline_keyboard"] for b in row]
    assert "set:2010" in buttons and len(buttons) == 21

    # до выбора имени кнопки «Сегодня» не работают — снова просит выбрать
    bot.dispatch(msg(A, bot.BTN_TODAY), store)
    assert fake.last()[1]["text"] == bot.CHOOSE_TEXT

    # выбирает себя
    bot.dispatch(cb(A, "set:2010"), store)
    assert store.user(A)["gid"] == "2010" and store.user(A)["name"] == "Али Дизайнер"

    # свой обед — с именем
    bot.dispatch(msg(A, bot.BTN_TODAY), store)
    t = fake.last()[1]["text"]
    assert "Али Дизайнер" in t and "Гранола" in t and "700 ₽" in t

    # смена имени
    bot.dispatch(msg(A, bot.BTN_SETTINGS), store)
    bot.dispatch(cb(A, "choose"), store)
    bot.dispatch(cb(A, "set:2003"), store)
    assert store.user(A)["name"] == "Хадя"
    bot.dispatch(cb(A, "set:2010"), store)

    # чужие листы: B не зарегистрирован, но смотреть может
    bot.dispatch(msg(B, bot.BTN_OTHERS), store)
    bot.dispatch(cb(B, "view:2003"), store)
    m, p = fake.last()
    assert m == "editMessageText" and "Хадя" in p["text"] and "Неделя" in p["text"]
    bot.dispatch(cb(B, "show:2003:all"), store)
    assert "Все заказанные обеды" in fake.last()[1]["text"]
    assert store.user(B) is None

    # B регистрируется и выключает уведомления
    bot.dispatch(msg(B, "/start"), store)
    bot.dispatch(cb(B, "set:2015"), store)
    bot.dispatch(cb(B, "notify"), store)
    assert store.user(B)["notify"] is False

    # 14:59 — тишина; 15:00 — уведомление только A, один раз
    n_before = len(fake.calls)
    bot.now = lambda: datetime(2026, 11, 17, 14, 59, tzinfo=bot.TZ)
    bot.notify_all(store)
    assert len(fake.calls) == n_before
    bot.now = lambda: datetime(2026, 11, 17, 15, 0, 30, tzinfo=bot.TZ)
    bot.notify_all(store)
    bot.notify_all(store)
    sent = fake.calls[n_before:]
    assert len(sent) == 1 and sent[0][1]["chat_id"] == A and "Али Дизайнер" in sent[0][1]["text"]

    # суббота — без уведомлений
    n_before = len(fake.calls)
    bot.now = lambda: datetime(2026, 11, 21, 15, 5, tzinfo=bot.TZ)
    bot.notify_all(store)
    assert len(fake.calls) == n_before

    # состояние переживает перезапуск
    store2 = bot.Store(store.path)
    assert store2.user(A)["gid"] == "2010" and store2.user(B)["notify"] is False


def test_view_after_restart_shows_name():
    fake, store = setup(datetime(2026, 11, 17, 12, tzinfo=bot.TZ))
    bot.dispatch(cb(5, "view:2003"), store)  # список сотрудников ещё не загружался
    assert fake.last()[1]["text"].startswith("👤 Хадя")


def test_blocked_user_removed():
    fake, store = setup(datetime(2026, 11, 17, 15, 1, tzinfo=bot.TZ))
    store.set_user(333, gid="2010", name="Али Дизайнер")

    def blocked(method, **p):
        raise bot.TelegramError(403, "Forbidden: bot was blocked by the user")

    bot.tg = blocked
    bot.notify_all(store)
    assert store.user(333) is None


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("✓", t.__name__)
    print(f"OK — {len(tests)} тестов")
