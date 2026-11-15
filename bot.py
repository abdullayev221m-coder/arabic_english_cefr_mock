import asyncio
import html
import io
import json
import os
import random
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
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
GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").split(",") if m.strip()]

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
    await message.answer(
        "Salom! Men CEFR mock botiman 🎙📖🎧\n\n"
        "Speaking (ovozli javob), Reading va Listening mashqlari bor.\n\nTilni tanlang:",
        reply_markup=lang_kb(),
    )


@dp.message(Command("menu"))
async def menu_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Bo'limni tanlang:", reply_markup=menu_kb())


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
    last_err: Exception | None = None
    for model in GEMINI_MODELS:
        try:
            resp = await gemini.aio.models.generate_content(
                model=model,
                contents=contents,
                config=gtypes.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            return _parse_json(resp.text)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("Gemini javob bermadi")


def format_result(ev: dict) -> str:
    mistakes = "\n".join(
        f"• ❌ {esc(m.get('said', ''))}\n  ✅ {esc(m.get('correct', ''))}\n  💡 {esc(m.get('why', ''))}"
        for m in ev.get("mistakes", [])[:5]
    ) or "—"
    strengths = "\n".join(f"• {esc(s)}" for s in ev.get("strengths", []))
    advice = "\n".join(f"• {esc(a)}" for a in ev.get("advice", []))
    return (
        f"📊 <b>Natija: {esc(ev.get('score', '?'))}/75 ({esc(ev.get('level', '?'))})</b>\n"
        f"Fluency {esc(ev.get('fluency', '?'))} · Vocabulary {esc(ev.get('vocabulary', '?'))} · "
        f"Grammar {esc(ev.get('grammar', '?'))} · Pronunciation {esc(ev.get('pronunciation', '?'))} · "
        f"Task {esc(ev.get('task', '?'))}\n\n"
        f"👍 <b>Kuchli tomonlar</b>\n{strengths}\n\n"
        f"🛠 <b>Xatolar</b>\n{mistakes}\n\n"
        f"📈 <b>Maslahat</b>\n{advice}\n\n"
        f"🗣 <b>Namunaviy javob</b>\n{esc(ev.get('improved_answer', ''))}"
    )


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
        await note.edit_text(f"❌ Xatolik: {esc(e)}\nBiroz kutib, qayta yuborib ko'ring.")
        return

    await note.delete()
    await message.answer(f"📝 <b>Siz aytganingiz:</b>\n<i>{esc(ev.get('transcript', ''))}</i>")
    await send_long(message.chat.id, format_result(ev))
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
        pass

    item = CONTENT[d["lang"]][d["kind"]][d["idx"]]
    q = item["questions"][d["qi"]]
    choice = int(cb.data.split(":")[1])
    correct = q["answer"]
    ok = choice == correct
    score = d["score"] + (1 if ok else 0)

    if ok:
        fb = "✅ To'g'ri!"
    else:
        fb = f"❌ Noto'g'ri. To'g'ri javob: {LET[correct]}) {esc(q['options'][correct])}"
    if q.get("explain"):
        fb += f"\n💡 {esc(q['explain'])}"
    await bot.send_message(chat_id, fb)

    qi = d["qi"] + 1
    await state.update_data(qi=qi, score=score)
    if qi < len(item["questions"]):
        await send_question(chat_id, state)
        return

    total = len(item["questions"])
    pct = round(100 * score / total)
    text = (
        f"🏁 <b>Natija: {score}/{total} ({pct}%)</b>\n"
        f"Taxminiy daraja: <b>{level_from_pct(pct)}</b> (yo'naltiruvchi baho)"
    )
    if d["kind"] == "listening" and item.get("script"):
        text += f"\n\n📜 <b>Matn (transcript):</b>\n<tg-spoiler>{esc(item['script'])}</tg-spoiler>"
    await send_long(chat_id, text)
    await state.clear()
    await bot.send_message(chat_id, "Yana mashq qilamizmi?", reply_markup=menu_kb())


@dp.message()
async def fallback(message: types.Message):
    await message.answer("Boshlash uchun /start yoki /menu bosing.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
