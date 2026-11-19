"""
CEFR Mock Bot (English + Arabic)

Bo'limlar:
  * Reading   - matn + test savollar (tugmalar bilan)
  * Listening - matn avtomatik ovozga aylantiriladi (gTTS) + test savollar
  * Speaking  - 3 qism, ovozli (yoki matnli) javob; GEMINI_API_KEY bo'lsa AI baholaydi

Ma'lumot: shu papkadagi cefr_mocks.json
Railway Variables:
  BOT_TOKEN        (majburiy)
  GEMINI_API_KEY   (ixtiyoriy - Speaking AI baholash uchun)
  GEMINI_MODEL     (ixtiyoriy, standart: gemini-2.5-flash)
  FEEDBACK_LANG    (ixtiyoriy, standart: Uzbek)
"""

import asyncio
import base64
import html
import io
import json
import logging
import os
import re
from pathlib import Path

import httpx
from gtts import gTTS
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cefr-bot")

# --------------------------------------------------------------------------
# Sozlamalar
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "cefr_mocks.json"

TOKEN_NAMES = (
    "BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TOKEN",
    "API_TOKEN",
    "TOKEN",
)


def env_first(*names):
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


BOT_TOKEN = env_first(*TOKEN_NAMES)
GEMINI_KEY = env_first("GEMINI_API_KEY", "GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
FEEDBACK_LANG = os.getenv("FEEDBACK_LANG", "Uzbek").strip()

MAX_VOICE_SECONDS = 300
MAX_FILE_BYTES = 15 * 1024 * 1024
HTML = ParseMode.HTML

LANG_LABELS = {"english": "🇬🇧 English", "arabic": "🇸🇦 العربية"}
SECTION_LABELS = {
    "reading": "📖 Reading",
    "listening": "🎧 Listening",
    "speaking": "🎤 Speaking",
}


def load_data():
    if not DATA_FILE.exists():
        raise SystemExit("cefr_mocks.json topilmadi. Uni bot.py bilan bir papkaga yuklang.")
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"cefr_mocks.json to'liq emas yoki buzilgan ({exc}). "
            "Uni nusxalamang: GitHub'da 'Upload files' orqali fayl sifatida qayta yuklang."
        )
    if "languages" not in data or not data["languages"]:
        raise SystemExit("cefr_mocks.json ichida 'languages' topilmadi")
    return data


DATA = load_data()
LANGS = DATA["languages"]

# --------------------------------------------------------------------------
# Yordamchi funksiyalar
# --------------------------------------------------------------------------


def esc(text):
    return html.escape(str(text), quote=False)


def letter(lang, index):
    letters = "أبجد" if lang == "arabic" else "ABCD"
    return letters[index] if index < len(letters) else str(index + 1)


def kb(rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


def get_test(lang, ti):
    return LANGS[lang]["tests"][ti]


def clear_state(context):
    context.user_data.pop("quiz", None)
    context.user_data.pop("speak", None)


async def show(update, context, text, markup=None):
    """Callback bo'lsa xabarni tahrirlaydi, aks holda yangi xabar yuboradi."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    if query is not None:
        try:
            await query.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.info("edit failed, sending new message: %s", exc)
    await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup)


# --------------------------------------------------------------------------
# Menyular
# --------------------------------------------------------------------------


def home_markup():
    rows = [[(LANG_LABELS.get(key, key.title()), f"L:{key}")] for key in LANGS]
    return kb(rows)


HOME_TEXT = (
    "👋 <b>CEFR Mock Test</b>\n\n"
    "Reading, Listening va Speaking bo'yicha mashq qiling.\n"
    "Tilni tanlang:"
)


async def show_home(update, context):
    clear_state(context)
    await show(update, context, HOME_TEXT, home_markup())


async def show_levels(update, context, lang):
    tests = LANGS[lang]["tests"]
    buttons = [(f"{t['level']} · Mock {i + 1}", f"T:{lang}:{i}") for i, t in enumerate(tests)]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([("⬅️ Orqaga", "H")])
    label = LANG_LABELS.get(lang, lang.title())
    await show(update, context, f"{label}\n\nDarajani tanlang:", kb(rows))


async def show_sections(update, context, lang, ti):
    test = get_test(lang, ti)
    text = f"📘 <b>{esc(test['title'])}</b>\nDaraja: <b>{esc(test['level'])}</b>\n\nBo'limni tanlang:"
    rows = [[(label, f"S:{lang}:{ti}:{sec}")] for sec, label in SECTION_LABELS.items() if sec in test]
    rows.append([("⬅️ Orqaga", f"L:{lang}")])
    await show(update, context, text, kb(rows))


def after_markup(lang, ti, sec):
    return kb(
        [
            [("🔁 Qayta urinish", f"S:{lang}:{ti}:{sec}")],
            [("📋 Boshqa bo'lim", f"T:{lang}:{ti}")],
            [("🏠 Bosh menyu", "H")],
        ]
    )


# --------------------------------------------------------------------------
# Reading / Listening (test savollar)
# --------------------------------------------------------------------------

_audio_cache = {}


def _synthesize(text, code, slow):
    buf = io.BytesIO()
    gTTS(text=text, lang=code, slow=slow).write_to_fp(buf)
    return buf.getvalue()


async def get_audio(lang, test):
    key = test["id"]
    if key in _audio_cache:
        return _audio_cache[key]
    code = LANGS[lang].get("tts_lang", "en-US").split("-")[0]
    slow = test.get("level") == "A1"
    last_error = None
    for _ in range(2):
        try:
            data = await asyncio.to_thread(_synthesize, test["listening"]["script"], code, slow)
            _audio_cache[key] = data
            return data
        except Exception as exc:  # tarmoq xatolari
            last_error = exc
            await asyncio.sleep(1)
    raise last_error


async def send_question(bot, chat_id, quiz):
    item = quiz["qs"][quiz["i"]]
    quiz["n"] += 1
    lines = [f"❓ <b>{quiz['i'] + 1}/{len(quiz['qs'])}</b>", "", esc(item["q"]), ""]
    buttons = []
    for j, option in enumerate(item["options"]):
        lines.append(f"{letter(quiz['lang'], j)}) {esc(option)}")
        buttons.append((letter(quiz["lang"], j), f"a:{quiz['n']}:{j}"))
    quiz["last_text"] = "\n".join(lines)
    await bot.send_message(chat_id, quiz["last_text"], parse_mode=HTML, reply_markup=kb([buttons]))


async def start_quiz(update, context, lang, ti, sec):
    chat_id = update.effective_chat.id
    test = get_test(lang, ti)
    section = test[sec]
    clear_state(context)

    if sec == "reading":
        text = f"📖 <b>{esc(section['title'])}</b>\n\n{esc(section['text'])}"
        await context.bot.send_message(chat_id, text, parse_mode=HTML)
        await context.bot.send_message(chat_id, "Matnni o'qib, savollarga javob bering 👇")
    else:
        status = await context.bot.send_message(chat_id, "🎧 Audio tayyorlanmoqda, biroz kuting...")
        try:
            audio = await get_audio(lang, test)
        except Exception:
            log.exception("TTS xatosi")
            await status.edit_text("⚠️ Audio yaratib bo'lmadi. Birozdan keyin qayta urinib ko'ring.")
            await context.bot.send_message(
                chat_id, "Bo'lim tanlang:", reply_markup=after_markup(lang, ti, sec)
            )
            return
        try:
            await status.delete()
        except BadRequest:
            pass
        await context.bot.send_audio(
            chat_id,
            audio=io.BytesIO(audio),
            filename=f"{test['id']}.mp3",
            title=section["title"],
        )
        await context.bot.send_message(
            chat_id, "🎧 Audioni diqqat bilan tinglang (xohlagancha qayta eshitishingiz mumkin), so'ng savollarga javob bering 👇"
        )

    quiz = {
        "lang": lang,
        "ti": ti,
        "sec": sec,
        "qs": section["questions"],
        "i": 0,
        "score": 0,
        "n": 0,
        "done_n": None,
        "last_text": "",
    }
    context.user_data["quiz"] = quiz
    await send_question(context.bot, chat_id, quiz)


async def handle_answer(update, context, data):
    query = update.callback_query
    chat_id = update.effective_chat.id
    quiz = context.user_data.get("quiz")
    try:
        _, n_text, j_text = data.split(":")
        n, j = int(n_text), int(j_text)
    except ValueError:
        await query.answer()
        return

    if not quiz or quiz["n"] != n or quiz["done_n"] == n:
        await query.answer("Bu savol yopilgan.")
        return
    quiz["done_n"] = n
    await query.answer()

    item = quiz["qs"][quiz["i"]]
    correct = item["answer"]
    ok = j == correct
    if ok:
        quiz["score"] += 1
        result = "✅ <b>To'g'ri!</b>"
    else:
        right = f"{letter(quiz['lang'], correct)}) {item['options'][correct]}"
        result = f"❌ <b>Noto'g'ri.</b>\nTo'g'ri javob: {esc(right)}"
    try:
        await query.edit_message_text(quiz["last_text"] + "\n\n" + result, parse_mode=HTML)
    except BadRequest as exc:
        log.info("edit skipped: %s", exc)

    quiz["i"] += 1
    if quiz["i"] < len(quiz["qs"]):
        await send_question(context.bot, chat_id, quiz)
        return

    await finish_quiz(context, chat_id, quiz)


async def finish_quiz(context, chat_id, quiz):
    context.user_data.pop("quiz", None)
    total = len(quiz["qs"])
    score = quiz["score"]
    pct = round(100 * score / total)
    mood = "🎉" if pct >= 80 else "👍" if pct >= 60 else "💪"
    label = SECTION_LABELS[quiz["sec"]]
    text = f"🏁 <b>{label}</b>\n\nNatija: <b>{score}/{total}</b> ({pct}%) {mood}"
    if quiz["sec"] == "listening":
        script = get_test(quiz["lang"], quiz["ti"])["listening"]["script"]
        text += f"\n\n📝 Audio matni (ochish uchun bosing):\n<tg-spoiler>{esc(script)}</tg-spoiler>"
    await context.bot.send_message(
        chat_id, text, parse_mode=HTML, reply_markup=after_markup(quiz["lang"], quiz["ti"], quiz["sec"])
    )


# --------------------------------------------------------------------------
# Speaking
# --------------------------------------------------------------------------


def speaking_prompt(test, part):
    sp = test["speaking"]
    if part == 1:
        qs = "\n".join(f"{i}. {esc(q)}" for i, q in enumerate(sp["part1"], 1))
        return (
            "🎤 <b>Speaking · Part 1</b>\n\n"
            f"{qs}\n\n"
            "Har bir savolga 2–3 gapdan javob bering. "
            "Hammasiga <b>bitta ovozli xabar</b> yuboring (taxminan 1 daqiqa)."
        )
    if part == 2:
        p2 = sp["part2"]
        points = "\n".join(f"• {esc(p)}" for p in p2["points"])
        return (
            "🎤 <b>Speaking · Part 2</b>\n\n"
            f"📌 <b>{esc(p2['topic'])}</b>\n{points}\n\n"
            f"⏱ Tayyorlanish: {p2['prep_seconds']} soniya · Gapirish: {p2['speak_seconds']} soniyagacha\n\n"
            "Tayyor bo'lgach, <b>ovozli xabar</b> yuboring."
        )
    qs = "\n".join(f"{i}. {esc(q)}" for i, q in enumerate(sp["part3"], 1))
    return (
        "🎤 <b>Speaking · Part 3</b>\n\n"
        f"{qs}\n\n"
        "Sabab va misollar bilan javob bering (har bir savolga 45–60 soniya). "
        "Hammasiga <b>bitta ovozli xabar</b> yuboring."
    )


CANCEL_MARKUP = kb([[("❌ Bekor qilish", "C")]])


async def start_speaking(update, context, lang, ti):
    clear_state(context)
    context.user_data["speak"] = {"lang": lang, "ti": ti, "part": 1, "items": []}
    test = get_test(lang, ti)
    await context.bot.send_message(
        update.effective_chat.id,
        speaking_prompt(test, 1),
        parse_mode=HTML,
        reply_markup=CANCEL_MARKUP,
    )


async def on_user_message(update, context):
    message = update.effective_message
    speak = context.user_data.get("speak")
    if not speak:
        await message.reply_text("Menyuni ochish uchun /start ni bosing.")
        return

    media = message.voice or message.audio
    if media is not None:
        if media.duration and media.duration > MAX_VOICE_SECONDS:
            await message.reply_text("Ovozli xabar juda uzun. 5 daqiqadan qisqaroq yuboring.")
            return
        if media.file_size and media.file_size > MAX_FILE_BYTES:
            await message.reply_text("Fayl hajmi katta. Qisqaroq ovoz yuboring.")
            return
        tg_file = await media.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        item = {
            "part": speak["part"],
            "kind": "audio",
            "data": audio_bytes,
            "mime": getattr(media, "mime_type", None) or "audio/ogg",
        }
    elif message.text:
        text = message.text.strip()
        if len(text) < 15:
            await message.reply_text("Javob juda qisqa. Ovozli xabar yuboring yoki batafsilroq yozing.")
            return
        item = {"part": speak["part"], "kind": "text", "text": text}
    else:
        return

    speak["items"].append(item)
    lang, ti = speak["lang"], speak["ti"]
    chat_id = update.effective_chat.id

    if speak["part"] < 3:
        speak["part"] += 1
        await message.reply_text("✅ Qabul qilindi.")
        await context.bot.send_message(
            chat_id,
            speaking_prompt(get_test(lang, ti), speak["part"]),
            parse_mode=HTML,
            reply_markup=CANCEL_MARKUP,
        )
        return

    context.user_data.pop("speak", None)
    await finish_speaking(context, chat_id, lang, ti, speak["items"])


def build_scoring_prompt(lang, test):
    sp = test["speaking"]
    p2 = sp["part2"]
    lang_name = LANGS[lang]["language_name"]
    tasks = (
        f"Part 1 questions: {' | '.join(sp['part1'])}\n"
        f"Part 2 topic: {p2['topic']} Points: {' | '.join(p2['points'])}\n"
        f"Part 3 questions: {' | '.join(sp['part3'])}"
    )
    return (
        f"You are an experienced CEFR speaking examiner for {lang_name}. "
        f"The candidate is practising at level {test['level']}. "
        "Below are the tasks, followed by the candidate's answers in order (audio or text).\n\n"
        f"TASKS:\n{tasks}\n\n"
        "Score each criterion as an integer from 0 to 5: fluency, vocabulary, grammar, "
        "pronunciation, task (task achievement). If all answers are text, set pronunciation to null.\n"
        "Return ONLY valid JSON in this exact shape:\n"
        '{"scores":{"fluency":0,"vocabulary":0,"grammar":0,"pronunciation":0,"task":0},'
        '"cefr":"B1","strengths":"...","improvements":"...","corrections":["..."]}\n'
        f"Write strengths and improvements in {FEEDBACK_LANG} (2-3 short sentences each). "
        f"In corrections give up to 3 short examples: the candidate's mistake and the correct {lang_name} version."
    )


def parse_json_reply(raw):
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON topilmadi")
    return json.loads(cleaned[start : end + 1])


async def score_speaking(lang, test, items):
    parts = [{"text": build_scoring_prompt(lang, test)}]
    for item in items:
        parts.append({"text": f"Candidate answer for Part {item['part']}:"})
        if item["kind"] == "audio":
            parts.append(
                {
                    "inline_data": {
                        "mime_type": item["mime"],
                        "data": base64.b64encode(item["data"]).decode("ascii"),
                    }
                }
            )
        else:
            parts.append({"text": item["text"]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=150) as client:
        response = await client.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body)
        response.raise_for_status()
        payload = response.json()
    reply_parts = payload["candidates"][0]["content"]["parts"]
    raw = "".join(p.get("text", "") for p in reply_parts)
    return parse_json_reply(raw)


def clamp_score(value):
    try:
        return max(0, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def format_scoring(result):
    names = [
        ("fluency", "Fluency"),
        ("vocabulary", "Vocabulary"),
        ("grammar", "Grammar"),
        ("pronunciation", "Pronunciation"),
        ("task", "Task achievement"),
    ]
    scores = result.get("scores", {}) or {}
    lines = ["📊 <b>Speaking natijasi</b>", ""]
    total, maximum = 0, 0
    for key, label in names:
        value = clamp_score(scores.get(key))
        if value is None:
            continue
        total += value
        maximum += 5
        lines.append(f"• {label}: <b>{value}/5</b>")
    if maximum:
        lines.append(f"\nJami: <b>{total}/{maximum}</b>")
    if result.get("cefr"):
        lines.append(f"🎯 Taxminiy daraja: <b>{esc(result['cefr'])}</b>")
    if result.get("strengths"):
        lines.append(f"\n✅ <b>Kuchli tomonlar</b>\n{esc(result['strengths'])}")
    if result.get("improvements"):
        lines.append(f"\n📈 <b>Yaxshilash kerak</b>\n{esc(result['improvements'])}")
    corrections = result.get("corrections") or []
    if corrections:
        lines.append("\n✏️ <b>Tuzatishlar</b>")
        lines.extend(f"- {esc(c)}" for c in corrections[:3])
    return "\n".join(lines)[:3900]


async def finish_speaking(context, chat_id, lang, ti, items):
    test = get_test(lang, ti)
    markup = after_markup(lang, ti, "speaking")

    if not GEMINI_KEY:
        criteria = DATA.get("speaking_rubric", {}).get("criteria", [])
        rubric = "\n".join(f"• {esc(c)} (0–5)" for c in criteria)
        await context.bot.send_message(
            chat_id,
            "✅ Javoblaringiz qabul qilindi.\n\n"
            "AI baholash hozir yoqilmagan. O'zingizni quyidagi mezonlar bo'yicha baholang:\n"
            f"{rubric}",
            parse_mode=HTML,
            reply_markup=markup,
        )
        return

    status = await context.bot.send_message(chat_id, "⏳ Javoblaringiz baholanmoqda (30–60 soniya)...")
    try:
        result = await score_speaking(lang, test, items)
        text = format_scoring(result)
    except Exception:
        log.exception("Baholash xatosi")
        text = "⚠️ Baholashda xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring."
    try:
        await status.delete()
    except BadRequest:
        pass
    await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup)


# --------------------------------------------------------------------------
# Handlerlar
# --------------------------------------------------------------------------


async def cmd_start(update, context):
    await show_home(update, context)


async def cmd_cancel(update, context):
    await show_home(update, context)


async def on_callback(update, context):
    query = update.callback_query
    data = query.data or ""
    try:
        if data.startswith("a:"):
            await handle_answer(update, context, data)
            return

        await query.answer()
        parts = data.split(":")
        action = parts[0]

        if action in ("H", "C"):
            await show_home(update, context)
        elif action == "L" and len(parts) == 2 and parts[1] in LANGS:
            clear_state(context)
            await show_levels(update, context, parts[1])
        elif action == "T" and len(parts) == 3 and parts[1] in LANGS:
            clear_state(context)
            await show_sections(update, context, parts[1], int(parts[2]))
        elif action == "S" and len(parts) == 4 and parts[1] in LANGS:
            lang, ti, sec = parts[1], int(parts[2]), parts[3]
            if sec in ("reading", "listening"):
                await start_quiz(update, context, lang, ti, sec)
            elif sec == "speaking":
                await start_speaking(update, context, lang, ti)
        else:
            await show_home(update, context)
    except (IndexError, KeyError, ValueError):
        log.exception("Noto'g'ri callback: %s", data)
        await show_home(update, context)


async def on_error(update, context):
    log.error("Kutilmagan xato", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Bot tokeni topilmadi. Railway > Variables ichida BOT_TOKEN ni kiriting "
            f"(qabul qilinadigan nomlar: {', '.join(TOKEN_NAMES)})."
        )
    if not GEMINI_KEY:
        log.warning("GEMINI_API_KEY yo'q: Speaking AI baholash o'chirilgan.")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO | (filters.TEXT & ~filters.COMMAND), on_user_message)
    )
    app.add_error_handler(on_error)

    log.info("Bot ishga tushdi: %s ta til", len(LANGS))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

# ===== bot.py fayli oxiri (shu qator ko'rinsa, fayl to'liq) =====

