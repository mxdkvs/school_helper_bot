"""
Чат-бот «Школьный Помощник» для мессенджера MAX.
Команда «Саб-лимонад», направление «Подземелье знаний».
"""

import glob
import json
import logging
import os
import re
import sqlite3
import ssl
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

import certifi
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(BASE_DIR, ".env")):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("school_helper")

MAX_TOKEN = os.getenv("MAX_TOKEN", "").strip()
MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")
DB_PATH = os.getenv("DB_PATH", "data/school_helper.db")
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(BASE_DIR, DB_PATH)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
MAX_TEXT_LEN = 3900


def read_certs(path):
    data = open(path, "rb").read()
    if b"-----BEGIN CERTIFICATE-----" in data:
        blocks = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                            data.decode("ascii", "ignore"), flags=re.S)
    else:
        blocks = [ssl.DER_cert_to_PEM_cert(data)]
    good = []
    for b in blocks:
        try:
            ssl.create_default_context(cadata=b)
            good.append(b.strip() + "\n")
        except Exception:
            log.warning("Файл %s не похож на сертификат, пропускаю", os.path.basename(path))
    return good


def find_ca_bundle():
    files = []
    if os.getenv("CA_BUNDLE"):
        files.append(os.path.join(BASE_DIR, os.getenv("CA_BUNDLE")))
    for folder in (BASE_DIR, os.path.join(BASE_DIR, "certs")):
        for ext in ("*.pem", "*.crt", "*.cer"):
            files += [f for f in glob.glob(os.path.join(folder, ext)) if f not in files]
    extra = []
    for f in files:
        if os.path.exists(f):
            certs = read_certs(f)
            if certs:
                log.info("Сертификат: %s", os.path.basename(f))
                extra += certs
    if not extra:
        return True
    combined = os.path.join(tempfile.gettempdir(), "school_helper_ca.pem")
    with open(certifi.where(), encoding="utf-8") as src, open(combined, "w", encoding="utf-8") as dst:
        dst.write(src.read().rstrip() + "\n" + "".join(extra))
    return combined


CA_BUNDLE = find_ca_bundle()


class MaxAPI:
    def __init__(self, token, base_url=MAX_API_URL):
        self.base = base_url
        self.s = requests.Session()
        self.s.headers.update({"Authorization": token})
        self.s.verify = CA_BUNDLE

    def _req(self, method, path, params=None, body=None, timeout=40):
        for attempt in range(3):
            r = self.s.request(method, self.base + path, params=params, json=body, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            if r.status_code >= 400:
                log.error("MAX %s %s -> %s %s", method, path, r.status_code, r.text[:300])
                r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()

    def me(self):
        return self._req("GET", "/me")

    def get_updates(self, marker=None, timeout=30):
        params = {"timeout": timeout, "limit": 100,
                  "types": "message_created,message_callback,bot_started"}
        if marker is not None:
            params["marker"] = marker
        return self._req("GET", "/updates", params=params, timeout=timeout + 10)

    def send(self, chat_id, text, keyboard=None):
        parts, mid = split_text(text), None
        for i, part in enumerate(parts):
            body = {"text": part, "format": "markdown"}
            if keyboard and i == len(parts) - 1:
                body["attachments"] = [keyboard]
            resp = self._req("POST", "/messages", params={"chat_id": chat_id}, body=body)
            mid = ((resp or {}).get("message") or {}).get("body", {}).get("mid")
        return mid

    def edit(self, mid, text, keyboard=None):
        body = {"text": text[:MAX_TEXT_LEN], "format": "markdown",
                "attachments": [keyboard] if keyboard else []}
        self._req("PUT", "/messages", params={"message_id": mid}, body=body)
        return mid

    def answer(self, callback_id, notification=None, text=None, keyboard=None):
        body = {}
        if notification:
            body["notification"] = notification
        if text is not None:
            msg = {"text": text[:MAX_TEXT_LEN], "format": "markdown"}
            msg["attachments"] = [keyboard] if keyboard else []
            body["message"] = msg
        self._req("POST", "/answers", params={"callback_id": callback_id}, body=body)

    def typing(self, chat_id):
        try:
            self._req("POST", f"/chats/{chat_id}/actions", body={"action": "typing_on"}, timeout=10)
        except Exception:
            pass


def keyboard(rows):
    return {"type": "inline_keyboard", "payload": {"buttons": [
        [{"type": "callback", "text": t[:60], "payload": p} for t, p in row] for row in rows
    ]}}


def split_text(text, limit=MAX_TEXT_LEN):
    parts, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            parts.append(para[:limit]); para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            parts.append(cur); cur = ""
        cur += para + "\n"
    if cur.strip():
        parts.append(cur)
    return [p.rstrip() for p in parts] or [" "]


class GigaChat:
    OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def __init__(self, auth_key, scope=GIGACHAT_SCOPE, model=GIGACHAT_MODEL):
        self.auth_key, self.scope, self.model = auth_key, scope, model
        self.token, self.expires_at = None, 0
        self.lock = threading.Lock()

    def _get_token(self):
        with self.lock:
            if self.token and time.time() < self.expires_at - 60:
                return self.token
            r = requests.post(self.OAUTH_URL, verify=CA_BUNDLE, timeout=20,
                              headers={"Authorization": f"Basic {self.auth_key}",
                                       "RqUID": str(uuid.uuid4()),
                                       "Content-Type": "application/x-www-form-urlencoded",
                                       "Accept": "application/json"},
                              data={"scope": self.scope})
            r.raise_for_status()
            data = r.json()
            self.token = data["access_token"]
            self.expires_at = data.get("expires_at", 0) / 1000 or time.time() + 1700
            return self.token

    def ask(self, system, user, temperature=0.6):
        r = requests.post(self.CHAT_URL, verify=CA_BUNDLE, timeout=90,
                          headers={"Authorization": f"Bearer {self._get_token()}",
                                   "Content-Type": "application/json"},
                          json={"model": self.model, "temperature": temperature,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


SYSTEM_PROMPT = (
    "Ты — «Школьный Помощник», дружелюбный помощник для школьников 7–11 классов в мессенджере MAX. "
    "Пиши по-русски, просто и конкретно, короткими пунктами, без таблиц. "
    "Не выдумывай ссылки: называй только реально существующие надёжные ресурсы "
    "(РЭШ resh.edu.ru, ФИПИ fipi.ru, Большая российская энциклопедия bigenc.ru, учебники, "
    "научно-популярные журналы и музеи). Если не уверен в точном адресе — пиши название без ссылки. "
    "Поддерживай ученика, не делай работу за него целиком — давай структуру и направление."
)


TZ_OFFSET = float(os.getenv("TZ_OFFSET", "12"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))
_ctx = threading.local()

CITIES = [("Калининград", 2), ("Москва", 3), ("Самара", 4), ("Екатеринбург", 5), ("Омск", 6),
          ("Новосибирск", 7), ("Иркутск", 8), ("Якутск", 9), ("Владивосток", 10), ("Магадан", 11),
          ("Камчатка", 12)]
DAY_REMINDER = os.getenv("PLAN_REMINDER_TIME", "15:30")
EVE_TIME, MORNING_TIME = "19:00", "08:00"

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
SEP = "━━━━━━━━━━━━━━━"


def cur_tz():
    return getattr(_ctx, "tz", LOCAL_TZ)


def use_tz(minutes):
    _ctx.tz = timezone(timedelta(minutes=minutes))


def fmt_offset(minutes):
    sign = "+" if minutes >= 0 else "−"
    h, m = divmod(abs(int(minutes)), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def offset_from_clock(h, m):
    utc = datetime.now(timezone.utc)
    diff = (h * 60 + m) - (utc.hour * 60 + utc.minute)
    diff = (diff + 660) % 1440 - 660
    return round(diff / 30) * 30


def now_local():
    return datetime.now(cur_tz())


def today():
    return now_local().date()


def at(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(d, dtime(h, m), cur_tz()).timestamp()


def plural(n, one, few, many):
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def date_full(d):
    return f"{WD[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def date_short(d):
    delta = (d - today()).days
    return {0: "Сегодня", 1: "Завтра", 2: "Послезавтра"}.get(delta, f"{WD[d.weekday()]} {d:%d.%m}")


def in_days(d):
    n = (d - today()).days
    if n < 0:
        return f"прошло {-n} {plural(n, 'день', 'дня', 'дней')}"
    return {0: "сегодня", 1: "завтра"}.get(n, f"через {n} {plural(n, 'день', 'дня', 'дней')}")


def urgency(d):
    n = (d - today()).days
    return "⚫" if n < 0 else "🔴" if n <= 1 else "🟠" if n <= 3 else "🟢"


def bar(done, total, width=8):
    if not total:
        return "▱" * width + " 0%"
    pct = round(100 * done / total)
    filled = round(width * done / total)
    return "▰" * filled + "▱" * (width - filled) + f" {pct}%"


WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6}
FILLER = r"\b(?:напомни(?:те)?|напоминание|мне|у меня|у нас|дедлайн|не забыть|не забудь|что|будет|о том,? что)\b"


def parse_when(text):
    t = " " + text + " "
    base, d, tm = today(), None, None

    m = re.search(r"\s(?:в|к)?\s*(\d{1,2}):(\d{2})\b", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        tm = f"{int(m.group(1)):02d}:{m.group(2)}"
        t = t[:m.start()] + " " + t[m.end():]

    rules = [
        (r"\bпослезавтра\b", lambda m: base + timedelta(days=2)),
        (r"\bзавтра\b", lambda m: base + timedelta(days=1)),
        (r"\bсегодня\b", lambda m: base),
        (r"\bчерез\s+(\d{1,2})\s*(?:дн\w*|день)", lambda m: base + timedelta(days=int(m.group(1)))),
        (r"\bчерез\s+неделю\b", lambda m: base + timedelta(days=7)),
        (r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", lambda m: _mkdate(m.group(1), m.group(2), m.group(3))),
        (r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b",
         lambda m: _mkdate(m.group(1), MONTHS.index(m.group(2).lower()) + 1, None)),
        (r"\b(?:в|во|до|к)?\s*(понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресень[ея])\b",
         lambda m: _next_weekday(m.group(1))),
    ]
    for pattern, fn in rules:
        m = re.search(pattern, t, flags=re.I)
        if m:
            d = fn(m)
            if d:
                t = t[:m.start()] + " " + t[m.end():]
                break
    if not d:
        return None
    title = re.sub(FILLER, " ", t, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" ,.-—:!")
    title = re.sub(r"^(?:про|о|об|к|по)\s+", "", title, flags=re.I)
    return d, tm, (title[:1].upper() + title[1:]) if title else ""


def _mkdate(day, month, year):
    try:
        y = int(year) if year else today().year
        if y < 100:
            y += 2000
        d = date(y, int(month), int(day))
        if not year and d < today():
            d = date(y + 1, int(month), int(day))
        return d
    except ValueError:
        return None


def _next_weekday(word):
    word = word.lower()
    wd = next(v for k, v in WEEKDAYS.items() if word.startswith(k))
    delta = (wd - today().weekday()) % 7 or 7
    return today() + timedelta(days=delta)


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.c.executescript("""
            CREATE TABLE IF NOT EXISTS plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, subject TEXT,
                start_date TEXT, exam_date TEXT, days_json TEXT, created_at REAL,
                archived INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, text TEXT, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS deadlines(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, title TEXT,
                due_date TEXT, due_time TEXT, done INTEGER DEFAULT 0, created_at REAL);
            CREATE TABLE IF NOT EXISTS projects(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, topic TEXT,
                sections_json TEXT, created_at REAL);
            CREATE TABLE IF NOT EXISTS reminders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, kind TEXT, ref_id INTEGER,
                arg INTEGER DEFAULT 0, due_at REAL, sent INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS users(chat_id INTEGER PRIMARY KEY, tz_min INTEGER);
            """)
            self.c.commit()

    def q(self, sql, args=(), one=False):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else rows

    def insert(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur.lastrowid

    def get_kv(self, k):
        row = self.q("SELECT v FROM kv WHERE k=?", (k,), one=True)
        return row["v"] if row else None

    def set_kv(self, k, v):
        self.q("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, str(v)))


def extract_json(text):
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None


def extract_quoted(text):
    m = re.search(r"[«\"“](.+?)[»\"”]", text)
    return m.group(1) if m else None


def clean_lines(value):
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    value = re.sub(r"^#{1,6}\s*", "", str(value), flags=re.M).replace("**", "")
    return "\n".join(line.rstrip() for line in value.strip().splitlines())


def project_offline(topic):
    return {
        "structure": (
            "1. Введение — почему тема важна, цель, 2–4 задачи, гипотеза\n"
            "2. Теория — что уже известно, основные понятия\n"
            "3. Практика — опыт, опрос, расчёт, модель или изделие\n"
            "4. Результаты — что получилось, подтвердилась ли гипотеза\n"
            "5. Заключение — вывод по каждой задаче\n"
            "6. Список источников — не меньше 5"),
        "sources": (
            "• РЭШ — resh.edu.ru\n"
            "• Большая российская энциклопедия — bigenc.ru\n"
            "• КиберЛенинка — cyberleninka.ru (научные статьи)\n"
            "• Учебник по предмету и справочники из библиотеки\n"
            "• Журналы «Наука и жизнь», «Квант»\n"
            "• Сайты музеев и научных центров по теме"),
        "slides": (
            "1. Титульный — тема, автор, класс, руководитель\n"
            "2. Актуальность и проблема\n"
            "3. Цель и задачи\n"
            "4. Гипотеза\n"
            "5. Теория: ключевые факты\n"
            "6. Теория: схема или инфографика\n"
            "7. Практика: фото, графики\n"
            "8. Выводы\n"
            "9. Источники и «Спасибо за внимание»"),
        "idea": (
            f"• Проведи мини-опрос одноклассников по теме «{topic}» и построй диаграмму\n"
            "• Или поставь простой опыт/сделай модель и сфотографируй этапы\n"
            "• Начни с практики — так станет ясно, какая теория нужна"),
    }


def plan_offline(subject, n_days):
    stages = [
        ("Теория", [("16:00–16:45", "Повторить правила, формулы, определения"),
                    ("16:45–17:00", "Перерыв ☕"),
                    ("17:00–17:45", "Сделать шпаргалку на 1 лист")]),
        ("Типовые задания", [("16:00–16:45", "Разобрать примеры с решениями"),
                             ("16:45–17:00", "Перерыв ☕"),
                             ("17:00–17:45", "Решить 10 базовых заданий")]),
        ("Задачи посложнее", [("16:00–16:45", "Решить 8–10 заданий повышенного уровня"),
                              ("16:45–17:00", "Перерыв ☕"),
                              ("17:00–17:30", "Выписать темы, где были ошибки")]),
        ("Пробный вариант", [("16:00–17:00", "Решить вариант целиком на время"),
                             ("17:00–17:15", "Перерыв ☕"),
                             ("17:15–17:45", "Проверить и разобрать ошибки")]),
        ("Работа над ошибками", [("16:00–16:45", "Повторить слабые темы"),
                                 ("16:45–17:00", "Перерыв ☕"),
                                 ("17:00–17:30", "Решить 5 заданий по слабым темам")]),
        ("Лёгкое повторение", [("17:00–17:40", "Пробежаться по шпаргалке"),
                               ("17:40–18:00", "Собрать всё к дню Х"),
                               ("22:00", "Лечь спать вовремя 😴")]),
    ]
    short = {1: [3], 2: [0, 5], 3: [0, 3, 5], 4: [0, 1, 3, 5], 5: [0, 1, 3, 4, 5]}
    if n_days in short:
        order = short[n_days]
    else:
        order = [round(i * 4 / (n_days - 2)) for i in range(n_days - 1)] + [5]
    days = []
    for idx in order:
        title, slots = stages[idx]
        days.append({"title": title, "slots": [{"time": t, "task": s} for t, s in slots]})
    checklist = ["Собрать список тем", "Сделать шпаргалку", "Разобрать типовые задания",
                 "Решить вариант на время", "Разобрать свои ошибки", "Повторить слабые темы",
                 "Подготовить всё к дню Х"]
    return days, checklist


def valid_days(days):
    return (isinstance(days, list) and days and
            all(isinstance(d, dict) and isinstance(d.get("slots"), list) and d["slots"] for d in days))


HOME = ("🏠 Меню", "home")


def when_keyboard():
    return keyboard([
        [("Сегодня", "dw:0"), ("Завтра", "dw:1"), ("Послезавтра", "dw:2")],
        [("Через 3 дня", "dw:3"), ("Через неделю", "dw:7")],
        [("✖ Отмена", "dls")],
    ])


HELP = (
    "ℹ️ **Как пользоваться**\n" + SEP + "\n"
    "📚 **Проект** — напиши тему, например «Физика — Электричество в быту». "
    "Получишь структуру, источники, план слайдов и идею для практики.\n\n"
    "📅 **План подготовки** — напиши «Осталось 3 дня до математики». "
    "План листается по дням кнопками ◀ ▶, а чек-лист можно отмечать.\n\n"
    "⏰ **Дедлайн** — просто напиши «Послезавтра проект по физике» или «15.11 в 18:00 сдать доклад». "
    f"Я напомню накануне в {EVE_TIME} и в сам день в {MORNING_TIME}.\n\n"
    "🗑 Любой план или дедлайн можно удалить, а план — завершить и вернуть из архива.\n\n"
    "🕒 Время напоминаний — по твоему часовому поясу. Сменить: «⚙️ Часовой пояс» в меню или /tz.\n\n"
    "Команды: /start — меню, /plans — планы, /deadlines — дедлайны, /tz — часовой пояс, /cancel — отмена."
)

RE_DAYS = re.compile(r"(?:остал\w*)\s+(\d{1,2})\s*(?:дн\w*|день|сут\w*)\s+(?:до\s+)?(.+)", re.I)
RE_PROJECT = re.compile(r"\b(проект\w*|доклад\w*|реферат\w*|исследовани\w*)\b", re.I)
RE_ASK = re.compile(r"^\s*(расскажи|помоги|подбери|найди|как|что такое|объясни|почему)\b", re.I)

SUBJECT_FORMS = {"русского": "русский язык", "английского": "английский язык",
                 "обществознания": "обществознание", "литературы": "литература", "алгебры": "алгебра",
                 "огэ": "ОГЭ", "егэ": "ЕГЭ", "впр": "ВПР", "контрольной": "контрольная"}


def normalize_subject(s):
    words = s.split()
    if not words:
        return s
    w = words[0].lower()
    if w in SUBJECT_FORMS:
        words[0] = SUBJECT_FORMS[w]
        if words[1:2] == ["языка"]:
            words.pop(1)
    elif w.endswith("ики"):
        words[0] = w[:-1] + "а"
    elif w.endswith("ии"):
        words[0] = w[:-1] + "я"
    s = " ".join(words)
    return s[:1].upper() + s[1:]


class SchoolHelperBot:
    def __init__(self, api, db, giga=None):
        self.api, self.db, self.giga = api, db, giga
        self.state = {}
        self.state_lock = threading.Lock()

    def set_state(self, chat_id, **data):
        with self.state_lock:
            self.state[chat_id] = data

    def get_state(self, chat_id):
        with self.state_lock:
            return self.state.get(chat_id)

    def pop_state(self, chat_id):
        with self.state_lock:
            return self.state.pop(chat_id, None)

    def owns(self, table, obj_id, chat_id):
        row = self.db.q(f"SELECT chat_id FROM {table} WHERE id=?", (obj_id,), one=True)
        return bool(row) and row["chat_id"] == chat_id

    def show(self, chat_id, screen, mid=None):
        text, kb = screen
        if mid:
            try:
                return self.api.edit(mid, text, kb)
            except Exception:
                log.warning("Не удалось изменить сообщение, отправляю новое")
        return self.api.send(chat_id, text, kb)

    def handle_update(self, upd):
        try:
            t = upd.get("update_type")
            chat_id = upd.get("chat_id") or ((upd.get("message") or {}).get("recipient") or {}).get("chat_id")
            if chat_id:
                self.activate(chat_id)
            if t == "bot_started":
                self.pop_state(chat_id)
                if self.user_tz(chat_id) is None:
                    self.show(chat_id, self.tz_screen(chat_id, first=True))
                else:
                    self.show(chat_id, self.home_screen(chat_id))
            elif t == "message_created":
                msg = upd["message"]
                if msg.get("sender", {}).get("is_bot"):
                    return
                text = (msg.get("body") or {}).get("text") or ""
                self.on_text(msg["recipient"]["chat_id"], text.strip())
            elif t == "message_callback":
                cb, msg = upd["callback"], upd.get("message") or {}
                chat_id = msg["recipient"]["chat_id"]
                mid = (msg.get("body") or {}).get("mid")
                self.on_callback(chat_id, cb["callback_id"], cb.get("payload", ""), mid)
        except Exception:
            log.exception("Ошибка обработки: %s", json.dumps(upd, ensure_ascii=False)[:500])

    def on_text(self, chat_id, text):
        low = text.lower()
        commands = {
            ("/start", "start", "старт", "меню", "/menu"): lambda: self.home_screen(chat_id),
            ("/help", "помощь"): lambda: (HELP, keyboard([[HOME]])),
            ("/plans", "планы", "/progress", "прогресс"): lambda: self.plans_screen(chat_id),
            ("/deadlines", "дедлайны"): lambda: self.deadlines_screen(chat_id),
        }
        if low in ("/start", "start", "старт") and self.user_tz(chat_id) is None:
            return self.show(chat_id, self.tz_screen(chat_id, first=True))
        if low in ("/tz", "/time", "часовой пояс", "время"):
            return self.show(chat_id, self.tz_screen(chat_id))
        for keys, fn in commands.items():
            if low in keys:
                self.pop_state(chat_id)
                return self.show(chat_id, fn())
        if low in ("/cancel", "отмена"):
            self.pop_state(chat_id)
            return self.show(chat_id, self.home_screen(chat_id, "Хорошо, отменил 👌"))

        st = self.pop_state(chat_id)
        step = st["step"] if st else None

        m = re.fullmatch(r"\s*(\d{1,2})[:. ](\d{2})\s*", text) if step == "tz" else None
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            off = offset_from_clock(int(m.group(1)), int(m.group(2)))
            self.set_user_tz(chat_id, off)
            return self.show(chat_id, self.home_screen(
                chat_id, f"✅ Готово! Твой пояс — **{fmt_offset(off)}**, сейчас у тебя {now_local():%H:%M}.\n"
                         "Все дедлайны и напоминания буду считать по твоему времени."))
        if step == "project_topic":
            return self.make_project(chat_id, text)
        if step == "plan_subject":
            return self.ask_plan_days(chat_id, normalize_subject(text))
        if step == "plan_days":
            when = parse_when(text)
            m = re.search(r"\d+", text)
            if when:
                n = (when[0] - today()).days
            elif m:
                n = int(m.group())
            else:
                self.set_state(chat_id, **st)
                return self.api.send(chat_id, "Напиши число дней (например, 5) или дату (например, 15.11).")
            return self.make_plan(chat_id, st["subject"], n)
        if step == "dl_title":
            when = parse_when(text)
            if when and when[2]:
                return self.create_deadline(chat_id, when[2], when[0], when[1])
            return self.ask_deadline_when(chat_id, text[:1].upper() + text[1:])
        if step == "dl_when":
            when = parse_when(text)
            if not when:
                self.set_state(chat_id, **st)
                return self.api.send(chat_id, "Не понял дату 🤔 Напиши, например: «завтра», «15.11», "
                                              "«в пятницу» или «послезавтра в 18:00».", when_keyboard())
            return self.create_deadline(chat_id, st["title"], when[0], when[1])

        m = RE_DAYS.search(text)
        if m:
            subject = normalize_subject(re.sub(r"[.!?]+$", "", m.group(2)).strip())
            return self.make_plan(chat_id, subject, int(m.group(1)))
        when = parse_when(text)
        if when and when[2] and not RE_ASK.search(text):
            return self.create_deadline(chat_id, when[2], when[0], when[1])
        if RE_PROJECT.search(text):
            return self.make_project(chat_id, text)
        return self.free_answer(chat_id, text)

    def on_callback(self, chat_id, cb_id, payload, mid):
        parts = payload.split(":")
        act, args = parts[0], [int(x) if x.lstrip("-").isdigit() else x for x in parts[1:]]

        def edit(screen, note=None):
            self.api.answer(cb_id, notification=note, text=screen[0], keyboard=screen[1])

        def note(text):
            self.api.answer(cb_id, notification=text)

        owner_table = {"pv": "plans", "ck": "plans", "pfin": "plans", "pres": "plans", "pdel": "plans",
                       "pdely": "plans", "dv": "deadlines", "ddone": "deadlines", "ddel": "deadlines",
                       "ddely": "deadlines", "dplan": "deadlines", "pj": "projects", "pjdel": "projects",
                       "pjdl": "projects"}
        if act in owner_table and not self.owns(owner_table[act], args[0], chat_id):
            return note("Уже удалено")
        if act == "tk":
            row = self.db.q("SELECT p.chat_id, t.plan_id, t.done FROM tasks t JOIN plans p ON p.id=t.plan_id "
                            "WHERE t.id=?", (args[0],), one=True)
            if not row or row["chat_id"] != chat_id:
                return note("Уже удалено")

        if act == "noop":
            return note("👆 Листай кнопками ◀ ▶")
        if act == "home":
            self.pop_state(chat_id)
            return edit(self.home_screen(chat_id))
        if act == "help":
            return edit((HELP, keyboard([[HOME]])))
        if act == "tz":
            return edit(self.tz_screen(chat_id))
        if act == "tzset":
            self.set_user_tz(chat_id, args[0])
            self.pop_state(chat_id)
            return edit(self.home_screen(
                chat_id, f"✅ Часовой пояс: **{fmt_offset(args[0])}**, сейчас у тебя {now_local():%H:%M}."),
                f"🕒 {fmt_offset(args[0])}")

        if act == "proj":
            self.set_state(chat_id, step="project_topic")
            return edit(self.projects_screen(chat_id))
        if act == "pj":
            return edit(self.project_screen(args[0], args[1] if len(args) > 1 else "structure"))
        if act == "pjdel":
            self.db.q("DELETE FROM projects WHERE id=?", (args[0],))
            self.set_state(chat_id, step="project_topic")
            return edit(self.projects_screen(chat_id), "🗑 Проект удалён")
        if act == "pjdl":
            topic = self.db.q("SELECT topic FROM projects WHERE id=?", (args[0],), one=True)["topic"]
            return edit(self.ask_deadline_when_screen(chat_id, f"Проект: {topic}"))

        if act == "plans":
            self.pop_state(chat_id)
            return edit(self.plans_screen(chat_id))
        if act == "arch":
            return edit(self.plans_screen(chat_id, archived=True))
        if act == "padd":
            self.set_state(chat_id, step="plan_subject")
            return edit(("📅 **Новый план подготовки**\n" + SEP + "\n\n"
                         "К чему готовимся? Напиши одним сообщением.\n\n"
                         "*Например: «Контрольная по алгебре» или «ОГЭ по русскому»*",
                         keyboard([[("✖ Отмена", "plans")]])))
        if act == "pd":
            st = self.pop_state(chat_id) or {}
            if not st.get("subject"):
                return note("Начни заново: «Новый план»")
            self.api.answer(cb_id, text=f"⏳ Составляю план «{st['subject']}» на {args[0]} "
                                        f"{plural(args[0], 'день', 'дня', 'дней')}…")
            return self.make_plan(chat_id, st["subject"], args[0], mid)
        if act == "pv":
            return edit(self.plan_screen(args[0], args[1] if len(args) > 1 else None))
        if act == "ck":
            return edit(self.checklist_screen(args[0]))
        if act == "tk":
            done = 0 if row["done"] else 1
            self.db.q("UPDATE tasks SET done=? WHERE id=?", (done, args[0]))
            d, t = self.plan_stats(row["plan_id"])
            msg = ("🎉 Всё выполнено! Ты готов(а)!" if d == t else "✅ Отлично, так держать!") if done else "Отметка снята"
            return edit(self.checklist_screen(row["plan_id"]), msg)
        if act == "pfin":
            self.db.q("UPDATE plans SET archived=1 WHERE id=?", (args[0],))
            self.db.q("DELETE FROM reminders WHERE sent=0 AND kind LIKE 'plan%' AND ref_id=?", (args[0],))
            return edit(self.plans_screen(chat_id), "🏁 План завершён и перенесён в архив")
        if act == "pres":
            self.db.q("UPDATE plans SET archived=0 WHERE id=?", (args[0],))
            self.schedule_plan(args[0], chat_id, with_nudges=False)
            return edit(self.plan_screen(args[0]), "♻️ План снова активен")
        if act == "pdel":
            subj = self.db.q("SELECT subject FROM plans WHERE id=?", (args[0],), one=True)["subject"]
            return edit((f"🗑 **Удалить план «{subj}»?**\n\nЧек-лист и напоминания тоже удалятся. "
                         "Если план просто закончился — лучше нажми «🏁 Завершить», он сохранится в архиве.",
                         keyboard([[("Да, удалить", f"pdely:{args[0]}"), ("Отмена", f"pv:{args[0]}")]])))
        if act == "pdely":
            self.delete_plan(args[0])
            return edit(self.plans_screen(chat_id), "🗑 План удалён")

        if act == "dls":
            self.pop_state(chat_id)
            return edit(self.deadlines_screen(chat_id))
        if act == "dadd":
            self.set_state(chat_id, step="dl_title")
            return edit(("➕ **Новый дедлайн**\n" + SEP + "\n\n"
                         "Что нужно сделать? Напиши одним сообщением.\n\n"
                         "*Например: «Проект по физике» — или сразу с датой: "
                         "«Послезавтра проект по физике»*",
                         keyboard([[("✖ Отмена", "dls")]])))
        if act == "dw":
            st = self.pop_state(chat_id) or {}
            if not st.get("title"):
                return note("Начни заново: «Добавить дедлайн»")
            return self.create_deadline(chat_id, st["title"], today() + timedelta(days=args[0]), None,
                                        cb_id=cb_id)
        if act == "dv":
            return edit(self.deadline_screen(args[0]))
        if act == "ddone":
            self.db.q("UPDATE deadlines SET done=1 WHERE id=?", (args[0],))
            self.db.q("DELETE FROM reminders WHERE sent=0 AND kind LIKE 'dl%' AND ref_id=?", (args[0],))
            return edit(self.deadlines_screen(chat_id), "🎉 Готово! Минус одна задача")
        if act == "ddel":
            title = self.db.q("SELECT title FROM deadlines WHERE id=?", (args[0],), one=True)["title"]
            return edit((f"🗑 **Удалить дедлайн «{title}»?**",
                         keyboard([[("Да, удалить", f"ddely:{args[0]}"), ("Отмена", f"dv:{args[0]}")]])))
        if act == "ddely":
            self.db.q("DELETE FROM deadlines WHERE id=?", (args[0],))
            self.db.q("DELETE FROM reminders WHERE kind LIKE 'dl%' AND ref_id=?", (args[0],))
            return edit(self.deadlines_screen(chat_id), "🗑 Дедлайн удалён")
        if act == "dplan":
            dl = self.db.q("SELECT * FROM deadlines WHERE id=?", (args[0],), one=True)
            n = max(1, (date.fromisoformat(dl["due_date"]) - today()).days)
            self.api.answer(cb_id, text=f"⏳ Составляю план «{dl['title']}» на {n} "
                                        f"{plural(n, 'день', 'дня', 'дней')}…")
            return self.make_plan(chat_id, dl["title"], n, mid)

        if act == "snz":
            r = self.db.q("SELECT * FROM reminders WHERE id=? AND chat_id=?", (args[0], chat_id), one=True)
            if r:
                self.db.insert("INSERT INTO reminders(chat_id, kind, ref_id, arg, due_at) VALUES(?,?,?,?,?)",
                               (chat_id, r["kind"], r["ref_id"], r["arg"], time.time() + 3600))
            return note("⏰ Напомню через час")

        note("Кнопка устарела — открой /start")

    def user_tz(self, chat_id):
        row = self.db.q("SELECT tz_min FROM users WHERE chat_id=?", (chat_id,), one=True)
        return row["tz_min"] if row else None

    def activate(self, chat_id):
        tz = self.user_tz(chat_id)
        use_tz(tz if tz is not None else int(TZ_OFFSET * 60))

    def set_user_tz(self, chat_id, minutes):
        self.db.q("INSERT OR REPLACE INTO users(chat_id, tz_min) VALUES(?, ?)", (chat_id, int(minutes)))
        use_tz(int(minutes))
        for p in self.db.q("SELECT id FROM plans WHERE chat_id=? AND archived=0", (chat_id,)):
            self.schedule_plan(p["id"], chat_id, with_nudges=False)
        for d in self.db.q("SELECT id FROM deadlines WHERE chat_id=? AND done=0", (chat_id,)):
            self.schedule_deadline(d["id"], chat_id)

    def tz_screen(self, chat_id, first=False):
        self.set_state(chat_id, step="tz")
        tz = self.user_tz(chat_id)
        lines = ["👋 Привет! Я **Школьный Помощник**." if first else "🕒 **Часовой пояс**", SEP,
                 "Чтобы дедлайны и напоминания приходили по *твоему* времени, скажи, "
                 "**сколько у тебя сейчас на часах?**",
                 "", "Напиши, например: *15:24* — или выбери город 👇"]
        if tz is not None:
            lines += ["", f"Сейчас стоит: {fmt_offset(tz)} · у тебя {now_local():%H:%M}"]
        btns = [(f"{name} {fmt_offset(h * 60)}", f"tzset:{h * 60}") for name, h in CITIES]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([("Пропустить »", "home")] if first else [HOME])
        return "\n".join(lines), keyboard(rows)

    def home_screen(self, chat_id, greeting=None):
        dls = self.db.q("SELECT * FROM deadlines WHERE chat_id=? AND done=0 ORDER BY due_date, due_time LIMIT 3",
                        (chat_id,))
        plans = self.db.q("SELECT * FROM plans WHERE chat_id=? AND archived=0 ORDER BY exam_date LIMIT 3",
                          (chat_id,))
        lines = [greeting or "🎒 **Школьный Помощник**", SEP]
        lines.append("⏰ **Ближайшие дедлайны**")
        if dls:
            for d in dls:
                dd = date.fromisoformat(d["due_date"])
                tm = f", {d['due_time']}" if d["due_time"] else ""
                lines.append(f"{urgency(dd)} {date_short(dd)}{tm} · {d['title']}")
        else:
            lines.append("*Пока пусто*")
        lines.append("")
        lines.append("📅 **Планы подготовки**")
        if plans:
            for p in plans:
                done, total = self.plan_stats(p["id"])
                lines.append(f"▸ {p['subject']} — {bar(done, total, 6)}")
        else:
            lines.append("*Пока нет планов*")
        lines += [SEP, "Что делаем? 👇"]
        kb = keyboard([
            [("📚 Материалы для проекта", "proj")],
            [("📅 Планы", "plans"), ("⏰ Дедлайны", "dls")],
            [("➕ Новый план", "padd"), ("➕ Дедлайн", "dadd")],
            [("ℹ️ Как пользоваться", "help"), ("⚙️ Часовой пояс", "tz")],
        ])
        return "\n".join(lines), kb

    def projects_screen(self, chat_id):
        rows = self.db.q("SELECT id, topic FROM projects WHERE chat_id=? ORDER BY id DESC LIMIT 5", (chat_id,))
        text = ("📚 **Материалы для проекта**\n" + SEP + "\n\n"
                "Напиши тему проекта одним сообщением — подберу структуру, источники, "
                "план слайдов и идею для практики.\n\n"
                "*Например: «Физика — Электричество в быту»*")
        kb_rows = []
        if rows:
            text += "\n\n🗂 **Мои проекты:**"
            kb_rows = [[(f"📄 {r['topic'][:40]}", f"pj:{r['id']}")] for r in rows]
        kb_rows.append([HOME])
        return text, keyboard(kb_rows)

    PROJECT_TABS = [("structure", "🧱 Структура"), ("sources", "📖 Источники"),
                    ("slides", "🖥 Слайды"), ("idea", "💡 Идея")]

    def project_screen(self, pid, tab="structure"):
        p = self.db.q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
        sections = json.loads(p["sections_json"])
        title = dict(self.PROJECT_TABS)[tab]
        text = (f"📚 **{p['topic']}**\n{SEP}\n**{title}**\n\n{sections.get(tab, '—')}\n{SEP}\n"
                "*Переключай разделы кнопками ниже*")
        tabs = [((f"• {name} •" if key == tab else name), f"pj:{pid}:{key}") for key, name in self.PROJECT_TABS]
        return text[:MAX_TEXT_LEN], keyboard([
            tabs[:2], tabs[2:],
            [("⏰ Поставить дедлайн", f"pjdl:{pid}"), ("🗑 Удалить", f"pjdel:{pid}")],
            [("« Проекты", "proj"), HOME],
        ])

    def plans_screen(self, chat_id, archived=False):
        plans = self.db.q("SELECT * FROM plans WHERE chat_id=? AND archived=? ORDER BY exam_date",
                          (chat_id, 1 if archived else 0))
        n_arch = self.db.q("SELECT COUNT(*) c FROM plans WHERE chat_id=? AND archived=1",
                           (chat_id,), one=True)["c"]
        lines = ["🗂 **Архив планов**" if archived else "📅 **Мои планы подготовки**", SEP]
        rows = []
        if not plans:
            lines.append("*Здесь пока пусто.*" if archived else
                         "*Планов пока нет.*\n\nНажми «➕ Новый план» или напиши:\n«Осталось 3 дня до математики»")
        for p in plans:
            done, total = self.plan_stats(p["id"])
            ex = date.fromisoformat(p["exam_date"])
            lines.append(f"**{p['subject']}**\n{bar(done, total)}  ·  {urgency(ex)} {date_short(ex)}\n")
            rows.append([(f"📖 {p['subject'][:40]}", f"pv:{p['id']}")])
        if archived:
            rows.append([("« Планы", "plans"), HOME])
        else:
            rows.append([("➕ Новый план", "padd")])
            if n_arch:
                rows.append([(f"🗂 Архив ({n_arch})", "arch")])
            rows.append([HOME])
        return "\n".join(lines).rstrip(), keyboard(rows)

    def plan_stats(self, pid):
        row = self.db.q("SELECT COUNT(*) total, COALESCE(SUM(done),0) done FROM tasks WHERE plan_id=?",
                        (pid,), one=True)
        return row["done"], row["total"]

    def plan_screen(self, pid, day=None):
        p = self.db.q("SELECT * FROM plans WHERE id=?", (pid,), one=True)
        days = json.loads(p["days_json"])
        start, exam = date.fromisoformat(p["start_date"]), date.fromisoformat(p["exam_date"])
        n = len(days)
        if day is None or day < 0:
            day = (today() - start).days
        day = max(0, min(day, n - 1))
        d, info = start + timedelta(days=day), days[day]
        done, total = self.plan_stats(pid)

        lines = [f"📅 **{p['subject']}**",
                 f"День {day + 1} из {n} · {date_full(d)}" + (" · **сегодня**" if d == today() else ""),
                 SEP, f"🎯 **{info.get('title') or 'Занятие'}**", ""]
        for s in info["slots"]:
            lines.append(f"`{s.get('time', '').strip() or '—'}`  {s.get('task', '').strip()}")
        lines += [SEP, f"✅ Чек-лист: {bar(done, total)}",
                  f"🏁 День Х: {date_full(exam)} ({in_days(exam)})"]
        if p["archived"]:
            lines.append("\n🗂 *План в архиве — напоминания выключены*")

        nav = [("◀", f"pv:{pid}:{day - 1}") if day > 0 else ("·", "noop"),
               (f"{day + 1} / {n}", f"pv:{pid}"),
               ("▶", f"pv:{pid}:{day + 1}") if day < n - 1 else ("·", "noop")]
        manage = ([("♻️ Вернуть", f"pres:{pid}"), ("🗑 Удалить", f"pdel:{pid}")] if p["archived"] else
                  [("🏁 Завершить", f"pfin:{pid}"), ("🗑 Удалить", f"pdel:{pid}")])
        return "\n".join(lines)[:MAX_TEXT_LEN], keyboard([
            nav, [(f"✅ Чек-лист · {done}/{total}", f"ck:{pid}")], manage,
            [("« Планы", "arch" if p["archived"] else "plans"), HOME]])

    def checklist_screen(self, pid):
        p = self.db.q("SELECT * FROM plans WHERE id=?", (pid,), one=True)
        tasks = self.db.q("SELECT * FROM tasks WHERE plan_id=? ORDER BY id", (pid,))
        done, total = self.plan_stats(pid)
        text = (f"✅ **Чек-лист · {p['subject']}**\n{SEP}\n{bar(done, total, 10)}\n\n"
                + ("🎉 **Всё выполнено! Ты готов(а). Удачи!**" if total and done == total
                   else "*Нажимай на пункт, чтобы отметить*"))
        rows = [[(("✅ " if t["done"] else "◻️ ") + t["text"], f"tk:{t['id']}")] for t in tasks]
        rows.append([("« К плану", f"pv:{pid}"), HOME])
        return text, keyboard(rows)

    def deadlines_screen(self, chat_id):
        dls = self.db.q("SELECT * FROM deadlines WHERE chat_id=? AND done=0 ORDER BY due_date, due_time",
                        (chat_id,))
        n_done = self.db.q("SELECT COUNT(*) c FROM deadlines WHERE chat_id=? AND done=1",
                           (chat_id,), one=True)["c"]
        lines, rows = ["⏰ **Мои дедлайны**", SEP], []
        if not dls:
            lines.append("*Дедлайнов нет — можно выдохнуть* 😌\n\n"
                         "Добавь кнопкой ниже или просто напиши:\n«Послезавтра проект по физике»")
        for d in dls:
            dd = date.fromisoformat(d["due_date"])
            tm = f", {d['due_time']}" if d["due_time"] else ""
            lines.append(f"{urgency(dd)} **{date_short(dd)}**{tm}\n      {d['title']}")
            rows.append([(f"{urgency(dd)} {d['title'][:40]}", f"dv:{d['id']}")])
        if n_done:
            lines.append(f"\n✔️ Выполнено: {n_done}")
        rows += [[("➕ Добавить дедлайн", "dadd")], [HOME]]
        return "\n".join(lines), keyboard(rows)

    def deadline_screen(self, did):
        d = self.db.q("SELECT * FROM deadlines WHERE id=?", (did,), one=True)
        dd = date.fromisoformat(d["due_date"])
        lines = [f"📌 **{d['title']}**", SEP,
                 f"🗓 {date_full(dd)} — {urgency(dd)} {in_days(dd)}"]
        if d["due_time"]:
            lines.append(f"🕒 до {d['due_time']}")
        lines += ["", "🔔 " + self.reminder_text_for_deadline(dd, d["due_time"]),
                  f"🌍 По твоему времени · {fmt_offset(cur_tz().utcoffset(None).total_seconds() // 60)}"]
        return "\n".join(lines), keyboard([
            [("✅ Готово", f"ddone:{did}"), ("🗑 Удалить", f"ddel:{did}")],
            [("📅 Составить план подготовки", f"dplan:{did}")],
            [("« Дедлайны", "dls"), HOME]])

    @staticmethod
    def reminder_text_for_deadline(dd, tm):
        parts = []
        if at(dd - timedelta(days=1), EVE_TIME) > time.time():
            parts.append(f"накануне в {EVE_TIME}")
        if at(dd, MORNING_TIME) > time.time():
            parts.append(f"в день дедлайна в {MORNING_TIME}")
        if tm and at(dd, tm) - 7200 > time.time():
            parts.append("за 2 часа")
        return ("Напомню " + ", ".join(parts)) if parts else "Дедлайн совсем скоро — удачи! 💪"

    def ask_plan_days(self, chat_id, subject):
        self.set_state(chat_id, step="plan_days", subject=subject)
        self.api.send(chat_id, f"📅 **{subject}**\n\nСколько дней осталось до дня Х?\n"
                               "*Выбери кнопку, напиши число или дату (например, 15.11)*",
                      keyboard([[("1", "pd:1"), ("2", "pd:2"), ("3", "pd:3"), ("4", "pd:4")],
                                [("5", "pd:5"), ("7", "pd:7"), ("10", "pd:10"), ("14", "pd:14")],
                                [("✖ Отмена", "plans")]]))

    def ask_deadline_when_screen(self, chat_id, title):
        self.set_state(chat_id, step="dl_when", title=title)
        return (f"📌 **{title}**\n\nКогда дедлайн?\n"
                "*Выбери кнопку или напиши: «15.11», «в пятницу», «завтра в 18:00»*", when_keyboard())

    def ask_deadline_when(self, chat_id, title):
        self.show(chat_id, self.ask_deadline_when_screen(chat_id, title))

    def create_deadline(self, chat_id, title, due, tm, cb_id=None):
        did = self.db.insert("INSERT INTO deadlines(chat_id, title, due_date, due_time, created_at) "
                             "VALUES(?,?,?,?,?)", (chat_id, title[:100], due.isoformat(), tm, time.time()))
        self.schedule_deadline(did, chat_id)
        text, kb = self.deadline_screen(did)
        text = "✨ **Дедлайн добавлен!**\n\n" + text
        if cb_id:
            self.api.answer(cb_id, notification="Дедлайн добавлен", text=text, keyboard=kb)
        else:
            self.show(chat_id, (text, kb))

    def schedule_deadline(self, did, chat_id):
        d = self.db.q("SELECT * FROM deadlines WHERE id=?", (did,), one=True)
        due, tm = date.fromisoformat(d["due_date"]), d["due_time"]
        self.db.q("DELETE FROM reminders WHERE sent=0 AND kind LIKE 'dl%' AND ref_id=?", (did,))
        plan = [("dl_eve", at(due - timedelta(days=1), EVE_TIME)), ("dl_day", at(due, MORNING_TIME))]
        if tm:
            plan.append(("dl_soon", at(due, tm) - 7200))
        for kind, ts in plan:
            if ts > time.time():
                self.db.insert("INSERT INTO reminders(chat_id, kind, ref_id, due_at) VALUES(?,?,?,?)",
                               (chat_id, kind, did, ts))

    def make_project(self, chat_id, request_text, mid=None):
        topic = extract_quoted(request_text) or request_text
        topic = re.sub(r"^(?:расскажи|помоги|подбери)\w*\s+(?:о|об|с|про)?\s*", "", topic, flags=re.I)
        topic = (topic[:1].upper() + topic[1:])[:80]
        mid = mid or self.api.send(chat_id, "⏳ Подбираю структуру, источники и план слайдов…")
        self.api.typing(chat_id)
        sections = None
        if self.giga:
            try:
                data = extract_json(self.giga.ask(SYSTEM_PROMPT, (
                    f"Запрос ученика: «{request_text}». Подготовь помощь по школьному проекту.\n"
                    "Ответь ТОЛЬКО JSON-объектом без пояснений, с ключами:\n"
                    '"structure" — структура проекта: актуальность, цель, задачи, гипотеза, главы, '
                    "практика, заключение (нумерованные строки);\n"
                    '"sources" — 5–7 проверенных источников, каждая строка начинается с «• »;\n'
                    '"slides" — план презентации на 8–10 слайдов: «N. Заголовок — что разместить»;\n'
                    '"idea" — 2–3 идеи практической части, которые реально сделать в школе или дома, '
                    "строки с «• ».\nЗначения — строки, строки разделяй \\n. Без markdown. "
                    "Каждый раздел до 900 символов."), temperature=0.4))
                if data and all(k in data for k in ("structure", "sources", "slides", "idea")):
                    sections = {k: clean_lines(data[k]) for k in ("structure", "sources", "slides", "idea")}
            except Exception:
                log.exception("GigaChat недоступен, использую шаблон")
        sections = sections or project_offline(topic)
        pid = self.db.insert("INSERT INTO projects(chat_id, topic, sections_json, created_at) VALUES(?,?,?,?)",
                             (chat_id, topic, json.dumps(sections, ensure_ascii=False), time.time()))
        self.show(chat_id, self.project_screen(pid), mid)

    def make_plan(self, chat_id, subject, n_days, mid=None):
        n_days = max(1, min(int(n_days), 30))
        mid = mid or self.api.send(chat_id, f"⏳ Составляю план «{subject}» на {n_days} "
                                            f"{plural(n_days, 'день', 'дня', 'дней')}…")
        self.api.typing(chat_id)
        days, checklist = None, None
        if self.giga:
            try:
                data = extract_json(self.giga.ask(SYSTEM_PROMPT, (
                    f"Ученик готовится: «{subject}». До контрольной/экзамена осталось {n_days} дн.\n"
                    f"Составь план ровно на {n_days} дн. (последний день — лёгкое повторение и отдых).\n"
                    "Ответь ТОЛЬКО JSON без пояснений в формате:\n"
                    '{"days": [{"title": "тема дня, 2–4 слова", "slots": [{"time": "16:00–16:45", '
                    '"task": "конкретное действие до 60 символов"}]}], '
                    '"checklist": ["пункт до 45 символов"]}\n'
                    "В каждом дне 2–4 слота между 15:00 и 21:00, всего 1,5–2 часа, с перерывом. "
                    "В checklist 6–9 главных пунктов на весь период."), temperature=0.4))
                if data and valid_days(data.get("days")):
                    days = data["days"][:n_days]
                    checklist = [clean_lines(c)[:60] for c in data.get("checklist", []) if str(c).strip()][:10]
            except Exception:
                log.exception("GigaChat недоступен, использую шаблон")
        if not days or not checklist:
            days, checklist = plan_offline(subject, n_days)

        start = today()
        exam = start + timedelta(days=n_days)
        pid = self.db.insert("INSERT INTO plans(chat_id, subject, start_date, exam_date, days_json, created_at) "
                             "VALUES(?,?,?,?,?,?)", (chat_id, subject[:80], start.isoformat(), exam.isoformat(),
                                                     json.dumps(days, ensure_ascii=False), time.time()))
        for item in checklist:
            self.db.insert("INSERT INTO tasks(plan_id, text) VALUES(?,?)", (pid, item))
        self.schedule_plan(pid, chat_id, with_nudges=True)
        text, kb = self.plan_screen(pid, 0)
        self.show(chat_id, ("✨ **План готов!** Листай дни кнопками ◀ ▶\n\n" + text, kb), mid)

    def schedule_plan(self, pid, chat_id, with_nudges):
        p = self.db.q("SELECT * FROM plans WHERE id=?", (pid,), one=True)
        start, exam = date.fromisoformat(p["start_date"]), date.fromisoformat(p["exam_date"])
        self.db.q("DELETE FROM reminders WHERE sent=0 AND kind LIKE 'plan%' AND kind!='plan_nudge' "
                  "AND ref_id=?", (pid,))
        items = []
        if with_nudges:
            for h in (2, 4):
                ts = time.time() + h * 3600
                if 8 <= datetime.fromtimestamp(ts, cur_tz()).hour < 22:
                    items.append(("plan_nudge", h, ts))
        for i in range((exam - start).days):
            items.append(("plan_day", i, at(start + timedelta(days=i), DAY_REMINDER)))
        items.append(("plan_eve", 0, at(exam - timedelta(days=1), EVE_TIME)))
        items.append(("plan_xday", 0, at(exam, MORNING_TIME)))
        for kind, arg, ts in items:
            if ts > time.time():
                self.db.insert("INSERT INTO reminders(chat_id, kind, ref_id, arg, due_at) VALUES(?,?,?,?,?)",
                               (chat_id, kind, pid, arg, ts))

    def delete_plan(self, pid):
        self.db.q("DELETE FROM tasks WHERE plan_id=?", (pid,))
        self.db.q("DELETE FROM reminders WHERE kind LIKE 'plan%' AND ref_id=?", (pid,))
        self.db.q("DELETE FROM plans WHERE id=?", (pid,))

    def free_answer(self, chat_id, text):
        if not self.giga:
            return self.show(chat_id, self.home_screen(
                chat_id, "🤔 Не совсем понял. Попробуй так:\n"
                         "• «Проект по биологии “Почему желтеют листья”»\n"
                         "• «Осталось 3 дня до математики»\n"
                         "• «Послезавтра сдать доклад по истории»"))
        self.api.typing(chat_id)
        try:
            reply = clean_lines(self.giga.ask(SYSTEM_PROMPT, text + "\n\nОтветь кратко, до 1500 символов."))
        except Exception:
            log.exception("GigaChat недоступен")
            reply = "Не получилось ответить прямо сейчас 😔 Попробуй ещё раз чуть позже."
        self.api.send(chat_id, reply, keyboard([[HOME]]))

    def build_reminder(self, r):
        kind, ref = r["kind"], r["ref_id"]
        snooze = ("⏰ Через час", f"snz:{r['id']}")
        if kind.startswith("plan"):
            p = self.db.q("SELECT * FROM plans WHERE id=?", (ref,), one=True)
            if not p or p["archived"]:
                return None
            done, total = self.plan_stats(ref)
            exam = date.fromisoformat(p["exam_date"])
            if kind == "plan_nudge":
                if total and done == total:
                    return None
                return (f"⏰ **Как подготовка к «{p['subject']}»?**\n{SEP}\n"
                        f"Прошло {r['arg']} ч. с начала плана. Отметь, что уже сделано 💪\n\n"
                        f"✅ {bar(done, total)}",
                        keyboard([[("✅ Чек-лист", f"ck:{ref}"), snooze]]))
            if kind == "plan_day":
                days = json.loads(p["days_json"])
                if r["arg"] >= len(days):
                    return None
                info = days[r["arg"]]
                slots = "\n".join(f"`{s.get('time', '')}`  {s.get('task', '')}" for s in info["slots"])
                return (f"📅 **Сегодня по плану · {p['subject']}**\n"
                        f"День {r['arg'] + 1} из {len(days)} — {info.get('title', '')}\n{SEP}\n{slots}\n{SEP}\n"
                        f"🏁 День Х {in_days(exam)} · ✅ {bar(done, total)}",
                        keyboard([[("📖 Открыть план", f"pv:{ref}:{r['arg']}"), snooze]]))
            if kind == "plan_eve":
                return (f"🔔 **Завтра — {p['subject']}!**\n{SEP}\n"
                        f"Чек-лист: {done} из {total}. Вечером — лёгкое повторение, и спать вовремя 😴",
                        keyboard([[("✅ Чек-лист", f"ck:{ref}")]]))
            if kind == "plan_xday":
                return (f"🍀 **Сегодня — {p['subject']}!**\n\nТы подготовился(-ась) — у тебя всё получится! 💪",
                        keyboard([[("🏁 Завершить план", f"pfin:{ref}")]]))
        if kind.startswith("dl"):
            d = self.db.q("SELECT * FROM deadlines WHERE id=?", (ref,), one=True)
            if not d or d["done"]:
                return None
            tm = f" до {d['due_time']}" if d["due_time"] else ""
            head = {"dl_eve": "🔔 **Завтра дедлайн!**", "dl_day": "⏰ **Сегодня дедлайн!**",
                    "dl_soon": "⚡ **Осталось 2 часа!**"}[kind]
            tail = "Не забудь подготовиться 😉" if kind == "dl_eve" else "Ты справишься! 💪"
            return (f"{head}\n{SEP}\n📌 {d['title']}{tm}\n\n{tail}",
                    keyboard([[("✅ Готово", f"ddone:{ref}"), snooze],
                              [("📅 План подготовки", f"dplan:{ref}")] if kind == "dl_eve" else [HOME]]))
        return None

    def reminder_tick(self):
        due = self.db.q("SELECT * FROM reminders WHERE sent=0 AND due_at<=? ORDER BY due_at", (time.time(),))
        for r in due:
            self.db.q("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
            if time.time() - r["due_at"] > 6 * 3600:
                continue
            self.activate(r["chat_id"])
            msg = self.build_reminder(r)
            if msg:
                try:
                    self.api.send(r["chat_id"], *msg)
                except Exception:
                    log.exception("Не удалось отправить напоминание %s", r["id"])

    def reminder_loop(self):
        while True:
            try:
                self.reminder_tick()
            except Exception:
                log.exception("Ошибка в цикле напоминаний")
            time.sleep(30)

    def run(self):
        try:
            me = self.api.me()
        except requests.exceptions.SSLError as e:
            raise SystemExit(
                "Ошибка SSL при подключении к MAX.\n"
                f"Подробности: {e}\n"
                "Скачай с https://www.gosuslugi.ru/crt сертификаты «Russian Trusted Root CA» и "
                "«Russian Trusted Sub CA» и положи их в папку:\n"
                f"{os.path.join(BASE_DIR, 'certs')}")
        log.info("Бот запущен: %s (@%s). Пояс по умолчанию: UTC%+g (ученик может выбрать свой)",
                 me.get("name") or me.get("first_name"), me.get("username"), TZ_OFFSET)
        threading.Thread(target=self.reminder_loop, daemon=True).start()
        pool = ThreadPoolExecutor(max_workers=8)
        marker = self.db.get_kv("marker")
        marker = int(marker) if marker else None
        while True:
            try:
                data = self.api.get_updates(marker)
                for upd in data.get("updates", []):
                    pool.submit(self.handle_update, upd)
                if data.get("marker") is not None:
                    marker = data["marker"]
                    self.db.set_kv("marker", marker)
            except requests.exceptions.ReadTimeout:
                continue
            except Exception:
                log.exception("Ошибка получения обновлений, повтор через 5 с")
                time.sleep(5)


def main():
    if not MAX_TOKEN:
        raise SystemExit("Укажи токен бота в переменной MAX_TOKEN (файл .env).")
    giga = GigaChat(GIGACHAT_AUTH_KEY) if GIGACHAT_AUTH_KEY else None
    if not giga:
        log.warning("GIGACHAT_AUTH_KEY не задан — бот работает на встроенных шаблонах.")
    SchoolHelperBot(MaxAPI(MAX_TOKEN), DB(DB_PATH), giga).run()


if __name__ == "__main__":
    main()
