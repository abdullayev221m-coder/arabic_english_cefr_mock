import asyncio
import html
import io
import json
import logging
import os
import random
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
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
# Birinchisi eng tez (flash-lite), ikkinchisi zaxira (sifatliroq).
GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODEL", "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.8-flash"
    ).split(",")
    if m.strip()
]
# Thinking darajasi: minimal (eng tez) | low | medium | high
# Eng tez: "minimal". Tahlil chuqurroq kerak bo'lsa GEMINI_THINKING=low qiling (sekinroq).
THINKING = os.environ.get("GEMINI_THINKING", "minimal")
# Bir vaqtda ketadigan Gemini so'rovlari soni (daqiqalik limitga urilmaslik uchun navbat).
GEMINI_SEM = asyncio.Semaphore(int(os.environ.get("GEMINI_CONCURRENCY", "4")))
# Limit/band xatosida necha marta (kutib) qayta urinish.
RETRY_ROUNDS = 3
log = logging.getLogger("bot")

CONTENT = json.loads((Path(__file__).parent / "content.json").read_text(encoding="utf-8"))

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
Give a complete but EFFICIENT analysis: specific, no repetition, no filler. Quote the candidate's actual words
when pointing out problems. Answer ONLY with JSON:
{
 "transcript": "verbatim transcript of the answer",
 "score": <0-75 integer>,
 "level": "<A2|B1|B2|C1>",
 "fluency": <0-9 number>, "vocabulary": <0-9>, "grammar": <0-9>, "pronunciation": <0-9>, "task": <0-9>,
 "summary": "3-4 sentence overall assessment",
 "criteria_notes": {"fluency": "1-2 sentences", "vocabulary": "1-2 sentences", "grammar": "1-2 sentences",
                    "pronunciation": "1-2 sentences", "task": "1-2 sentences"},
 "strengths": ["3 specific points"],
 "mistakes": [{"said": "...", "correct": "...", "why": "one short sentence"}],
 "advice": ["4 concrete, actionable steps"],
 "useful_phrases": ["3-4 useful expressions in {language} the candidate could have used"],
 "improved_answer": "a model answer at the candidate's target level (Part 1: ~4 sentences; Parts 2-3: ~8 sentences)"
}
List the most important mistakes (up to 8). Write 'summary', 'criteria_notes', 'strengths', 'advice' and 'why'
in Uzbek (Latin script). Keep 'transcript', 'said', 'correct', 'useful_phrases' and 'improved_answer' in
{language}. If the audio is silent, unclear or very short, give a low score and say so."""


class Exam(StatesGroup):
    waiting_voice = State()
    quiz = State()


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

USER_LANG: dict[int, str] = {}
AUDIO_CACHE: dict[tuple[str, int], str] = {}

# E'lon (broadcast) uchun: admin ID lari (vergul bilan) va foydalanuvchilar ro'yxati.
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_ID", "").split(",") if x.strip()}
USERS_FILE = Path(os.environ.get("USERS_FILE", Path(__file__).parent / "users.json"))
try:
    USERS: set[int] = set(json.loads(USERS_FILE.read_text()))
except Exception:  # noqa: BLE001
    USERS = set()


def _save_users():
    USERS_FILE.write_text(json.dumps(sorted(USERS)))


def add_user(chat_id: int):
    if chat_id not in USERS:
        USERS.add(chat_id)
        _save_users()


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
    add_user(message.chat.id)
    await message.answer(
        "Salom! Men CEFR mock botiman 🎙📖🎧\n\n"
        "Speaking (ovozli javob), Reading va Listening mashqlari bor.\n\nTilni tanlang:",
        reply_markup=lang_kb(),
    )


@dp.message(Command("menu"))
async def menu_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    add_user(message.chat.id)
    await message.answer("Bo'limni tanlang:", reply_markup=menu_kb())


async def _send_one(uid: int, src: types.Message | None, text: str):
    if src:
        await bot.copy_message(uid, src.chat.id, src.message_id)
    else:
        await bot.send_message(uid, text)


@dp.message(Command("broadcast"), F.from_user.id.in_(ADMIN_IDS))
async def broadcast(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    src = message.reply_to_message
    text = (command.args or "").strip()
    if not src and not text:
        await message.answer(
            "Foydalanish:\n/broadcast <i>matn</i>\n"
            "yoki biror xabarga (rasm, video ham mumkin) reply qilib /broadcast yozing."
        )
        return
    users = list(USERS)
    note = await message.answer(f"📢 Yuborilmoqda: {len(users)} ta foydalanuvchi...")
    ok = fail = 0
    for uid in users:
        try:
            try:
                await _send_one(uid, src, text)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await _send_one(uid, src, text)
            ok += 1
        except TelegramForbiddenError:  # foydalanuvchi botni bloklagan
            USERS.discard(uid)
            fail += 1
        except Exception:  # noqa: BLE001
            fail += 1
        await asyncio.sleep(0.05)  # Telegram limiti: ~30 xabar/soniya
    _save_users()
    await note.edit_text(f"✅ Yuborildi: {ok}\n❌ Yuborilmadi: {fail}")


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
    add_user(cb.message.chat.id)
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
            await asyncio.sleep(3 * (attempt + 1))  # limit vaqtinchalik bo'lsa, kutib qayta urinadi
    raise RuntimeError("\n".join(errors) or "Gemini javob bermadi")


def format_result(ev: dict) -> str:
    mistakes = "\n".join(
        f"• ❌ {esc(m.get('said', ''))}\n  ✅ {esc(m.get('correct', ''))}\n  💡 {esc(m.get('why', ''))}"
        for m in ev.get("mistakes", [])[:10]
    ) or "—"
    strengths = "\n".join(f"• {esc(s)}" for s in ev.get("strengths", []))
    advice = "\n".join(f"• {esc(a)}" for a in ev.get("advice", []))
    phrases = "\n".join(f"• {esc(p)}" for p in ev.get("useful_phrases", []))

    notes = ev.get("criteria_notes")
    notes = notes if isinstance(notes, dict) else {}
    labels = [("fluency", "Fluency"), ("vocabulary", "Vocabulary"), ("grammar", "Grammar"),
              ("pronunciation", "Pronunciation"), ("task", "Task")]
    notes_txt = "\n".join(
        f"• <b>{lab}</b> ({esc(ev.get(key, '?'))}/9): {esc(notes[key])}"
        for key, lab in labels if notes.get(key)
    )

    parts = [
        f"📊 <b>Natija: {esc(ev.get('score', '?'))}/75 ({esc(ev.get('level', '?'))})</b>\n"
        f"Fluency {esc(ev.get('fluency', '?'))} · Vocabulary {esc(ev.get('vocabulary', '?'))} · "
        f"Grammar {esc(ev.get('grammar', '?'))} · Pronunciation {esc(ev.get('pronunciation', '?'))} · "
        f"Task {esc(ev.get('task', '?'))}"
    ]
    if ev.get("summary"):
        parts.append(f"📋 <b>Umumiy xulosa</b>\n{esc(ev['summary'])}")
    if notes_txt:
        parts.append(f"🔎 <b>Mezonlar bo'yicha tahlil</b>\n{notes_txt}")
    parts.append(f"👍 <b>Kuchli tomonlar</b>\n{strengths}")
    parts.append(f"🛠 <b>Xatolar</b>\n{mistakes}")
    parts.append(f"📈 <b>Maslahat</b>\n{advice}")
    if phrases:
        parts.append(f"💬 <b>Foydali iboralar</b>\n{phrases}")
    parts.append(f"🗣 <b>Namunaviy javob</b>\n{esc(ev.get('improved_answer', ''))}")
    return "\n\n".join(parts)


@dp.message(Exam.waiting_voice, F.voice | F.audio)
async def got_voice(message: types.Message, state: FSMContext):
    data = await state.get_data()
    part, question, lang = data["part"], data["question"], data.get("lang", "en")
    queue, results = data.get("queue", []), data.get("results", [])

    note = await message.answer("⏳ Javobingiz tahlil qilinmoqda...")
    file_obj = message.voice or message.audio
    mime = getattr(file_obj, "mime_type", None) or "audio/ogg"
    try:
        buf = io.BytesIO()
        await bot.download(file_obj, destination=buf)
        ev = await evaluate_audio(lang, part, question, buf.getvalue(), mime)
    except Exception as e:  # noqa: BLE001
        if is_quota_error(e):
            await note.edit_text("⏳ Hozir server band (limit). 1 daqiqadan keyin qayta yuborib ko'ring.")
        else:
            await note.edit_text(f"❌ Xatolik: {esc(e)}\nBiroz kutib, qayta yuborib ko'ring.")
        return

    await note.delete()
    transcript = f"📝 <b>Siz aytganingiz:</b>\n<i>{esc(ev.get('transcript', ''))}</i>"
    await send_long(message.chat.id, transcript + "\n\n" + format_result(ev))
    results.append(ev.get("score", 0))

    if queue:
        await state.update_data(results=results)
        await ask_question(message.chat.id, state, queue[0], queue[1:])
    else:
        if len(results) > 1:
            avg = round(sum(results) / len(results))
            await message.answer(f"🏁 <b>Mock yakuni: o'rtacha {avg}/75</b>")
        await state.clear()
        await message.answer("Yana mashq qilamizmi?", reply_markup=menu_kb())


@dp.message(Exam.waiting_voice)
async def need_voice(message: types.Message):
    await message.answer("Iltimos, javobni <b>ovozli xabar</b> ko'rinishida yuboring 🎤")


# ---------------- READING / LISTENING ----------------
@dp.callback_query(F.data.startswith("list:"))
async def list_items(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    kind = cb.data.split(":")[1]
    lang = lang_of(cb.message.chat.id)
    if not CONTENT[lang].get(kind):
        await cb.message.answer("Bu til uchun hozircha mashq yo'q.", reply_markup=menu_kb())
        return
    title = "📖 Reading" if kind == "reading" else "🎧 Listening"
    await cb.message.answer(f"{title} ({LANGS[lang]['label']}). Mashqni tanlang:", reply_markup=items_kb(lang, kind))


def _tts_bytes(text: str, lang_code: str) -> bytes:
    buf = io.BytesIO()
    gTTS(text=text, lang=lang_code).write_to_fp(buf)
    return buf.getvalue()


async def send_listening_audio(chat_id: int, lang: str, idx: int, item: dict):
    key = (lang, idx)
    if key in AUDIO_CACHE:
        await bot.send_audio(chat_id, AUDIO_CACHE[key], title=item["title"])
        return
    audio_path = item.get("audio_file")
    if audio_path and (Path(__file__).parent / audio_path).exists():
        data = (Path(__file__).parent / audio_path).read_bytes()
    else:
        data = await asyncio.to_thread(_tts_bytes, item["script"], LANGS[lang]["tts"])
    msg = await bot.send_audio(
        chat_id, BufferedInputFile(data, filename="listening.mp3"), title=item["title"]
    )
    AUDIO_CACHE[key] = msg.audio.file_id


@dp.callback_query(F.data.startswith("item:"))
async def pick_item(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    _, kind, idx = cb.data.split(":")
    idx = int(idx)
    chat_id = cb.message.chat.id
    lang = lang_of(chat_id)
    item = CONTENT[lang][kind][idx]

    if kind == "reading":
        await send_long(chat_id, f"<b>{esc(item['title'])}</b>\n\n{esc(item['text'])}")
    else:
        note = await bot.send_message(chat_id, "🎧 Audio tayyorlanmoqda...")
        try:
            await send_listening_audio(chat_id, lang, idx, item)
        except Exception as e:  # noqa: BLE001
            await note.edit_text(f"❌ Audio xatosi: {esc(e)}")
            return
        await note.delete()
        await bot.send_message(chat_id, "Audioni tinglang (qayta tinglash mumkin), so'ng savollarga javob bering.")

    await state.set_state(Exam.quiz)
    await state.update_data(lang=lang, kind=kind, idx=idx, qi=0, score=0)
    await send_question(chat_id, state)


async def send_question(chat_id: int, state: FSMContext):
    d = await state.get_data()
    item = CONTENT[d["lang"]][d["kind"]][d["idx"]]
    qs = item["questions"]
    qi = d["qi"]
    q = qs[qi]
    lines = [f"<b>Savol {qi + 1}/{len(qs)}</b>", esc(q["q"]), ""]
    for i, o in enumerate(q["options"]):
        lines.append(f"{LET[i]}) {esc(o)}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=LET[i], callback_data=f"ans:{i}") for i in range(len(q["options"]))
    ]])
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)


def level_from_pct(pct: int) -> str:
    if pct >= 85:
        return "C1"
    if pct >= 70:
        return "B2"
    if pct >= 50:
        return "B1"
    if pct >= 30:
        return "A2"
    return "A1"


@dp.callback_query(Exam.quiz, F.data.startswith("ans:"))
async def on_answer(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await state.get_data()
    chat_id = cb.message.chat.id
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
     
