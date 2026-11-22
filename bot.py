import asyncio
import html
import io
import json
import logging
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from google import genai
from google.genai import types as gtypes
from gtts import gTTS

BOT_TOKEN = os.environ["BOT_TOKEN"]
gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
# Bir nechta model vergul bilan yozilishi mumkin: biri xato bersa yoki limiti tugasa, keyingisi sinaladi.
# Birinchisi eng tez (flash-lite), keyingilari zaxira.
GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODEL", "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.8-flash"
    ).split(",")
    if m.strip()
]
# Thinking darajasi: minimal (eng tez) | low | medium | high
THINKING = os.environ.get("GEMINI_THINKING", "minimal")
# Bir vaqtda ketadigan Gemini so'rovlari soni (daqiqalik limitga urilmaslik uchun navbat).
GEMINI_SEM = asyncio.Semaphore(int(os.environ.get("GEMINI_CONCURRENCY", "4")))
# Limit/band xatosida necha marta (kutib) qayta urinish.
RETRY_ROUNDS = 3
log = logging.getLogger("bot")

CONTENT = json.loads((Path(__file__).parent / "content.json").read_text(encoding="utf-8"))


def _merge_extra() -> None:
    """content_extra.json dagi qo'shimcha savollarni CONTENT ga qo'shadi (eskilari o'zgarmaydi)."""
    p = Path(__file__).parent / "content_extra.json"
    if not p.exists():
        return
    extra = json.loads(p.read_text(encoding="utf-8"))
    for lang, sections in extra.items():
        base = CONTENT.setdefault(lang, {})
        for kind, val in sections.items():
            if kind == "speaking":
                sp = base.setdefault("speaking", {})
                for part, qs in val.items():
                    cur = sp.setdefault(part, [])
                    cur += [q for q in qs if q not in cur]
            else:  # reading / listening
                lst = base.setdefault(kind, [])
                titles = {it.get("title") for it in lst}
                lst += [it for it in val if it.get("title") not in titles]


_merge_extra()

LANGS = {
    "en": {"label": "🇬🇧 English", "exam": "English", "tts": "en"},
    "ar": {"label": "🇸🇦 العربية", "exam": "Arabic", "tts": "ar"},
}
PART_NAMES = {"p1": "Part 1", "p2": "Part 2", "p3": "Part 3"}
PART_TIME = {"p1": 30, "p2": 120, "p3": 120}
LET = "ABCDEF"

SYSTEM_PROMPT = """You are a certified {language} speaking examiner using the CEFR scale (A2-C1 range).
Score on a 0-75 scale. You receive an AUDIO recording of a candidate answering a speaking question in {language}.
First transcribe exactly what the candidate said (verbatim, in {language}), then evaluate strictly and fairly on:
fluency & coherence, lexical resource, grammatical range & accuracy (for Arabic include word forms, agreement and
case endings where audible), pronunciation & intonation (you can hear the audio), and task achievement.
Answer ONLY with JSON:
{
 "transcript": "verbatim transcript of the answer",
 "score": <0-75 integer>,
 "level": "<A2|B1|B2|C1>",
 "fluency": <0-9 number>, "vocabulary": <0-9>, "grammar": <0-9>, "pronunciation": <0-9>, "task": <0-9>,
 "strengths": ["...", "..."],
 "mistakes": [{"said": "...", "correct": "...", "why": "..."}],
 "advice": ["...", "..."],
 "improved_answer": "a short model answer at the candidate's target level"
}
Write 'strengths', 'advice' and 'why' in Uzbek (Latin script). Keep 'transcript', 'said', 'correct' and
'improved_answer' in {language}. Be concise. If the audio is silent, unclear or very short, give a low score and say so."""


class Exam(StatesGroup):
    waiting_voice = State()
    quiz = State()


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

USER_LANG: dict[int, str] = {}
AUDIO_CACHE: dict[tuple[str, int], str] = {}

# ---------------- Ma'lumotlar bazasi (SQLite): til, natijalar, haftalik reyting ----------------
# Railway'da Volume ulangan bo'lsa, baza avtomatik shu yerda saqlanadi (qayta deploydan keyin ham o'chmaydi).
_vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.environ.get("DB_PATH") or (
    os.path.join(_vol, "bot.db") if _vol else str(Path(__file__).parent / "bot.db")
)
if not _vol and not os.environ.get("DB_PATH"):
    log.warning("Volume ulanmagan: bot.db qayta deploydan keyin o'chib ketadi!")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
    chat_id INTEGER PRIMARY KEY, name TEXT, lang TEXT, anon INTEGER DEFAULT 0, joined INTEGER
);
CREATE TABLE IF NOT EXISTS results(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, kind TEXT, part TEXT, lang TEXT,
    pct REAL, raw TEXT, ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_results_ts ON results(ts);
CREATE INDEX IF NOT EXISTS idx_results_user ON results(chat_id, ts);
""")
db.commit()

TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET", "5"))))  # O'zbekiston: UTC+5
MIN_ATTEMPTS = int(os.environ.get("MIN_ATTEMPTS", "3"))  # reytingga kirish uchun haftada kamida shuncha mashq
_NAMES: dict[int, str] = {}

for _r in db.execute("SELECT chat_id, lang FROM users WHERE lang IS NOT NULL AND lang != ''"):
    USER_LANG[_r["chat_id"]] = _r["lang"]


def week_start_ts() -> int:
    """Joriy haftaning boshi: dushanba 00:00 (TZ bo'yicha)."""
    now = datetime.now(TZ)
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def db_touch_user(uid: int, name: str):
    name = (name or "Foydalanuvchi")[:40]
    if _NAMES.get(uid) == name:
        return
    db.execute(
        "INSERT INTO users(chat_id, name, joined) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name",
        (uid, name, int(time.time())),
    )
    db.commit()
    _NAMES[uid] = name


def db_set_lang(uid: int, lang: str):
    db.execute("UPDATE users SET lang=? WHERE chat_id=?", (lang, uid))
    db.commit()


def db_add_result(uid: int, kind: str, part: str, lang: str, pct: float, raw: str):
    db.execute(
        "INSERT INTO results(chat_id, kind, part, lang, pct, raw, ts) VALUES(?,?,?,?,?,?,?)",
        (uid, kind, part, lang, float(pct), raw, int(time.time())),
    )
    db.commit()


def db_toggle_anon(uid: int) -> bool:
    row = db.execute("SELECT anon FROM users WHERE chat_id=?", (uid,)).fetchone()
    new = 0 if (row and row["anon"]) else 1
    db.execute("UPDATE users SET anon=? WHERE chat_id=?", (new, uid))
    db.commit()
    return bool(new)


def db_stats(uid: int):
    now = int(time.time())
    d7, d14 = now - 7 * 86400, now - 14 * 86400
    total = db.execute(
        "SELECT COUNT(*) AS n, AVG(pct) AS a, MAX(pct) AS m FROM results WHERE chat_id=?", (uid,)
    ).fetchone()
    by_kind = db.execute(
        "SELECT kind, COUNT(*) AS n, AVG(pct) AS a FROM results WHERE chat_id=? GROUP BY kind", (uid,)
    ).fetchall()
    w1 = db.execute(
        "SELECT COUNT(*) AS n, AVG(pct) AS a FROM results WHERE chat_id=? AND ts>=?", (uid, d7)
    ).fetchone()
    w0 = db.execute(
        "SELECT COUNT(*) AS n, AVG(pct) AS a FROM results WHERE chat_id=? AND ts>=? AND ts<?",
        (uid, d14, d7),
    ).fetchone()
    last = db.execute(
        "SELECT pct FROM results WHERE chat_id=? ORDER BY id DESC LIMIT 6", (uid,)
    ).fetchall()
    return total, by_kind, w1, w0, [r["pct"] for r in reversed(last)]


def db_week_ranking():
    return db.execute(
        "SELECT r.chat_id AS uid, u.name AS name, u.anon AS anon, COUNT(*) AS n, AVG(r.pct) AS avg_pct "
        "FROM results r JOIN users u ON u.chat_id = r.chat_id WHERE r.ts >= ? "
        "GROUP BY r.chat_id HAVING COUNT(*) >= ? ORDER BY avg_pct DESC, n DESC",
        (week_start_ts(), MIN_ATTEMPTS),
    ).fetchall()


def db_week_count(uid: int) -> int:
    return db.execute(
        "SELECT COUNT(*) AS n FROM results WHERE chat_id=? AND ts>=?", (uid, week_start_ts())
    ).fetchone()["n"]


class TouchUser(BaseMiddleware):
    """Har bir xabar/tugmada foydalanuvchini bazada yangilab turadi."""

    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and not u.is_bot:
            try:
                db_touch_user(u.id, u.first_name or u.username or "Foydalanuvchi")
            except Exception as e:  # noqa: BLE001
                log.warning("Foydalanuvchini saqlab bo'lmadi: %s", e)
        return await handler(event, data)


dp.message.outer_middleware(TouchUser())
dp.callback_query.outer_middleware(TouchUser())


def lang_of(chat_id: int) -> str:
    return USER_LANG.get(chat_id, "en")


def esc(s) -> str:
    return html.escape(str(s))


async def send_long(chat_id: int, text: str, **kw):
    while text:
        if len(text) <= 4000:
            chunk, text = text, ""
        else:
            cut = text.rfind("\n", 0, 4000)
            cut = cut if cut > 0 else 4000
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        await bot.send_message(chat_id, chunk, **kw)


# ---------------- Klaviaturalar ----------------
def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=LANGS["en"]["label"], callback_data="lang:en"),
        InlineKeyboardButton(text=LANGS["ar"]["label"], callback_data="lang:ar"),
    ]])


def menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="🎤 Speaking Part 1", callback_data="part:p1"),
         b(text="🎤 Part 2", callback_data="part:p2")],
        [b(text="🎤 Part 3", callback_data="part:p3"),
         b(text="📝 To'liq speaking mock", callback_data="mock")],
        [b(text="📖 Reading", callback_data="list:reading"),
         b(text="🎧 Listening", callback_data="list:listening")],
        [b(text="📈 Statistikam", callback_data="stats"),
         b(text="🏆 Haftalik reyting", callback_data="top")],
        [b(text="🌐 Tilni o'zgartirish", callback_data="chlang")],
    ])


def items_kb(lang: str, kind: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{i + 1}. {it['title']}", callback_data=f"item:{kind}:{i}")]
            for i, it in enumerate(CONTENT[lang][kind])]
    rows.append([InlineKeyboardButton(text="⬅️ Menyu", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------- Boshlash va menyu ----------------
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Salom! Men CEFR mock botiman 🎙📖🎧\n\n"
        "Speaking (ovozli javob), Reading va Listening mashqlari bor.\n\nTilni tanlang:",
        reply_markup=lang_kb(),
    )


@dp.message(Command("menu"))
async def menu_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Bo'limni tanlang:", reply_markup=menu_kb())


# ---------------- STATISTIKA va HAFTALIK REYTING ----------------
def format_stats(uid: int) -> str:
    total, by_kind, w1, w0, last = db_stats(uid)
    if not total["n"]:
        return "Hali natijalar yo'q. Biror mashq bajaring, statistikangiz shu yerda chiqadi 📊"
    names = {"speaking": "🎤 Speaking", "reading": "📖 Reading", "listening": "🎧 Listening"}
    lines = [
        "📈 <b>Sizning statistikangiz</b>",
        "",
        f"Jami mashqlar: <b>{total['n']}</b>",
        f"O'rtacha: <b>{total['a']:.0f}%</b> · Eng yaxshi: <b>{total['m']:.0f}%</b>",
        "",
    ]
    for r in by_kind:
        extra = f" (≈{r['a'] * 0.75:.0f}/75)" if r["kind"] == "speaking" else ""
        lines.append(f"{names.get(r['kind'], r['kind'])}: {r['a']:.0f}%{extra} · {r['n']} ta")
    lines.append("")
    if w1["n"]:
        t = f"Oxirgi 7 kun: <b>{w1['a']:.0f}%</b> ({w1['n']} ta mashq)"
        if w0["n"]:
            diff = w1["a"] - w0["a"]
            t += f"\nOldingi 7 kunga nisbatan: {'▲' if diff >= 0 else '▼'} {abs(diff):.0f}%"
        lines.append(t)
    else:
        lines.append("Oxirgi 7 kunda mashq qilmagansiz.")
    if len(last) > 1:
        lines.append("Oxirgi natijalar: " + " → ".join(f"{p:.0f}" for p in last))
    return "\n".join(lines)


def format_top(uid: int) -> str:
    rows = db_week_ranking()
    start = datetime.fromtimestamp(week_start_ts(), TZ)
    lines = [f"🏆 <b>Haftalik reyting</b>", f"{start:%d.%m} dan boshlab (har dushanba yangilanadi)", ""]
    medals = ["🥇", "🥈", "🥉"]
    if not rows:
        lines.append(f"Bu hafta hali hech kim {MIN_ATTEMPTS} ta mashq bajarmagan. Birinchi bo'ling! 💪")
    me_rank = None
    for i, r in enumerate(rows, 1):
        if r["uid"] == uid:
            me_rank = i
    for i, r in enumerate(rows[:10], 1):
        mark = medals[i - 1] if i <= 3 else f"{i}."
        name = "Anonim" if (r["anon"] and r["uid"] != uid) else esc(r["name"] or "Foydalanuvchi")
        if r["uid"] == uid:
            name = f"<b>{name} (siz)</b>"
        lines.append(f"{mark} {name} — {r['avg_pct']:.0f}% ({r['n']} ta)")
    if me_rank and me_rank > 10:
        me = rows[me_rank - 1]
        lines += ["…", f"{me_rank}. <b>Siz</b> — {me['avg_pct']:.0f}% ({me['n']} ta)"]
    elif not me_rank:
        have = db_week_count(uid)
        need = max(0, MIN_ATTEMPTS - have)
        lines += ["", f"Reytingga kirish uchun yana <b>{need}</b> ta mashq bajaring (bu hafta: {have} ta)."]
    lines += [
        "",
        f"<i>O'rtacha natija (%) bo'yicha, haftada kamida {MIN_ATTEMPTS} ta mashq. "
        "Speaking bali /75 dan foizga aylantiriladi. "
        "Ismingiz Telegramdagi ismingiz bilan ko'rinadi, yashirish: /anon</i>",
    ]
    return "\n".join(lines)


def top_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕶 Ismni yashirish / ko'rsatish", callback_data="anon")],
        [InlineKeyboardButton(text="⬅️ Menyu", callback_data="menu")],
    ])


def anon_text(on: bool) -> str:
    return ("🕶 Endi reytingda boshqalarga «Anonim» bo'lib ko'rinasiz." if on
            else "👤 Endi reytingda ismingiz ko'rinadi.")


@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    await message.answer(format_stats(message.chat.id))


@dp.message(Command("top"))
async def top_cmd(message: types.Message):
    await message.answer(format_top(message.chat.id), reply_markup=top_kb())


@dp.message(Command("anon"))
async def anon_cmd(message: types.Message):
    await message.answer(anon_text(db_toggle_anon(message.chat.id)))


@dp.callback_query(F.data == "stats")
async def stats_cb(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(format_stats(cb.message.chat.id))


@dp.callback_query(F.data == "top")
async def top_cb(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(format_top(cb.message.chat.id), reply_markup=top_kb())


@dp.callback_query(F.data == "anon")
async def anon_cb(cb: types.CallbackQuery):
    await cb.answer(anon_text(db_toggle_anon(cb.message.chat.id)), show_alert=True)


@dp.callback_query(F.data == "menu")
async def menu_cb(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.answer("Bo'limni tanlang:", reply_markup=menu_kb())


@dp.callback_query(F.data == "chlang")
async def change_lang(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.answer("Tilni tanlang:", reply_markup=lang_kb())


@dp.callback_query(F.data.startswith("lang:"))
async def set_lang(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    lang = cb.data.split(":")[1]
    USER_LANG[cb.message.chat.id] = lang
    db_set_lang(cb.message.chat.id, lang)
    await cb.message.answer(f"Til: {LANGS[lang]['label']}\nBo'limni tanlang:", reply_markup=menu_kb())


# ---------------- SPEAKING ----------------
async def ask_question(chat_id: int, state: FSMContext, part: str, queue: list[str]):
    lang = lang_of(chat_id)
    q = random.choice(CONTENT[lang]["speaking"][part])
    prev = (await state.get_data()).get("results", [])
    await state.set_state(Exam.waiting_voice)
    await state.update_data(part=part, question=q, queue=queue, results=prev, lang=lang)
    await bot.send_message(
        chat_id,
        f"<b>{PART_NAMES[part]}</b> ({LANGS[lang]['label']})\n\n{esc(q)}\n\n"
        f"⏱ Tavsiya: ~{PART_TIME[part]} soniya. Javobni <b>ovozli xabar</b> qilib yuboring.",
    )


@dp.callback_query(F.data.startswith("part:"))
async def pick_part(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await ask_question(cb.message.chat.id, state, cb.data.split(":")[1], [])


@dp.callback_query(F.data == "mock")
async def pick_mock(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await ask_question(cb.message.chat.id, state, "p1", ["p2", "p3"])


def is_quota_error(e: Exception) -> bool:
    s = str(e)
    return any(t in s for t in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = text[text.find("{"): text.rfind("}") + 1]
    return json.loads(text)


async def evaluate_audio(lang: str, part: str, question: str, audio: bytes, mime: str) -> dict:
    """Ovozni Gemini'ga yuboradi: transkript + baho bitta so'rovda. Modellar ketma-ket sinaladi."""
    system = SYSTEM_PROMPT.replace("{language}", LANGS[lang]["exam"])
    contents = [
        f"Part: {PART_NAMES[part]}\nQuestion: {question}\n\nEvaluate the candidate's audio answer.",
        gtypes.Part.from_bytes(data=audio, mime_type=mime),
    ]
    # Yangi modellarda temperature/top_p/top_k olib tashlangan — yubormaymiz.
    base = dict(system_instruction=system, response_mime_type="application/json")
    configs = []
    try:  # eski SDK'da thinking_level bo'lmasa, shu variant o'tkazib yuboriladi
        configs.append(gtypes.GenerateContentConfig(
            **base, thinking_config=gtypes.ThinkingConfig(thinking_level=THINKING.upper())
        ))
    except Exception as e:  # noqa: BLE001
        log.warning("thinking_level ishlamadi (google-genai ni yangilang): %s", e)
    configs.append(gtypes.GenerateContentConfig(**base))  # thinking'siz zaxira variant

    errors: list[str] = []
    async with GEMINI_SEM:  # so'rovlar navbat bilan ketadi, birdan ko'p yuborilmaydi
        for attempt in range(RETRY_ROUNDS):
            quota_hit = False
            for model in GEMINI_MODELS:  # har bir modelning limiti alohida
                for cfg in configs:
                    try:
                        resp = await gemini.aio.models.generate_content(
                            model=model, contents=contents, config=cfg
                        )
                        return _parse_json(resp.text)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Gemini xatosi (%s): %s", model, e)
                        msg = f"{model}: {str(e)[:250]}"
                        if msg not in errors:
                            errors.append(msg)
                        if is_quota_error(e):
                            quota_hit = True
                            break  # bu modelda limit — keyingi modelga o'tamiz
            if not quota_hit:
                break
            await asyncio.sleep(3 * (attempt + 1))  # limit
