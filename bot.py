import asyncio
import json
import os
import random
import tempfile

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

BOT_TOKEN = os.environ["BOT_TOKEN"]
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
claude = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
CLAUDE_MODEL = "claude-sonnet-5-5"

QUESTIONS = {
    "p1": [
        "Describe your hometown. What do you like most about it?",
        "What do you usually do on weekends?",
        "Tell me about your favourite food and why you like it.",
        "How do you usually travel to school or work?",
        "Do you prefer studying alone or with friends? Why?",
    ],
    "p2": [
        "Some people prefer to live in a big city, others in a small town. "
        "Which do you prefer and why? Give reasons and examples.",
        "Describe a person who has influenced you. Explain how and why.",
        "Talk about a book or film you recently enjoyed and say why you recommend it.",
    ],
    "p3": [
        "Technology makes students lazy. Do you agree or disagree? "
        "Discuss advantages and disadvantages and give your opinion.",
        "Should schools make learning a foreign language compulsory? "
        "Present arguments for and against.",
    ],
}
PART_NAMES = {"p1": "Part 1", "p2": "Part 2", "p3": "Part 3"}
PART_TIME = {"p1": 30, "p2": 120, "p3": 120}

SYSTEM_PROMPT = """You are a certified CEFR speaking examiner (B1-C1 range, 0-75 national CEFR scale).
You receive the transcript of a candidate's spoken answer. Evaluate it strictly and fairly.
Criteria: fluency & coherence, lexical resource, grammatical range & accuracy, task achievement.
Pronunciation cannot be reliably judged from a transcript, so only comment on it if the transcript
shows clear signs (e.g. unclear words). Answer ONLY with JSON:
{
 "score": <0-75 integer>,
 "level": "<A2|B1|B2|C1>",
 "fluency": <0-9 number>, "vocabulary": <0-9>, "grammar": <0-9>, "task": <0-9>,
 "strengths": ["...", "..."],
 "mistakes": [{"said": "...", "correct": "...", "why": "..."}],
 "advice": ["...", "..."],
 "improved_answer": "a short model answer at the candidate's target level"
}
Write 'strengths', 'advice' and 'why' in Uzbek. Keep 'said', 'correct' and 'improved_answer' in English.
Be concise. If the transcript is very short or empty, give a low score and say so."""


class Exam(StatesGroup):
    waiting_voice = State()


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎤 Part 1", callback_data="part:p1"),
             InlineKeyboardButton(text="🎤 Part 2", callback_data="part:p2")],
            [InlineKeyboardButton(text="🎤 Part 3", callback_data="part:p3")],
            [InlineKeyboardButton(text="📝 To'liq mock (3 qism)", callback_data="mock")],
        ]
    )


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Salom! Men CEFR Speaking mock botiman 🎙\n\n"
        "Savol beraman, siz *ovozli xabar* bilan javob berasiz, men esa "
        "bahosini va xatolaringiz tahlilini beraman.\n\nTanlang:",
        reply_markup=menu_kb(),
        parse_mode="Markdown",
    )


@dp.message(Command("menu"))
async def menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Tanlang:", reply_markup=menu_kb())


async def ask_question(chat_id: int, state: FSMContext, part: str, queue: list[str]):
    q = random.choice(QUESTIONS[part])
    await state.set_state(Exam.waiting_voice)
    await state.update_data(part=part, question=q, queue=queue, results=(await state.get_data()).get("results", []))
    await bot.send_message(
        chat_id,
        f"*{PART_NAMES[part]}*\n\n{q}\n\n"
        f"⏱ Tavsiya: ~{PART_TIME[part]} soniya. Javobni *ovozli xabar* qilib yuboring.",
        parse_mode="Markdown",
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


async def transcribe(path: str) -> str:
    with open(path, "rb") as f:
        r = await openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, language="en"
        )
    return r.text.strip()


async def evaluate(part: str, question: str, transcript: str) -> dict:
    resp = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Part: {PART_NAMES[part]}\nQuestion: {question}\n\nTranscript:\n{transcript or '(empty)'}",
        }],
    )
    text = resp.content[0].text.strip()
    text = text[text.find("{"): text.rfind("}") + 1]
    return json.loads(text)


def format_result(ev: dict, transcript: str) -> str:
    mistakes = "\n".join(
        f"• ❌ {m['said']}\n  ✅ {m['correct']}\n  💡 {m['why']}" for m in ev.get("mistakes", [])[:5]
    ) or "—"
    return (
        f"📊 *Natija: {ev['score']}/75 ({ev['level']})*\n"
        f"Fluency {ev['fluency']} · Vocabulary {ev['vocabulary']} · "
        f"Grammar {ev['grammar']} · Task {ev['task']}\n\n"
        f"👍 *Kuchli tomonlar*\n" + "\n".join(f"• {s}" for s in ev["strengths"]) +
        f"\n\n🛠 *Xatolar*\n{mistakes}\n\n"
        f"📈 *Maslahat*\n" + "\n".join(f"• {a}" for a in ev["advice"]) +
        f"\n\n🗣 *Namunaviy javob*\n{ev['improved_answer']}"
    )


@dp.message(Exam.waiting_voice, F.voice | F.audio)
async def got_voice(message: types.Message, state: FSMContext):
    data = await state.get_data()
    part, question = data["part"], data["question"]
    queue, results = data.get("queue", []), data.get("results", [])

    note = await message.answer("⏳ Javobingiz tahlil qilinmoqda...")
    file_obj = message.voice or message.audio
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "answer.ogg")
        await bot.download(file_obj, destination=path)
        try:
            transcript = await transcribe(path)
            ev = await evaluate(part, question, transcript)
        except Exception as e:
            await note.edit_text(f"❌ Xatolik: {e}\nQayta yuborib ko'ring.")
            return

    await note.delete()
    await message.answer(f"📝 *Siz aytganingiz:*\n_{transcript}_", parse_mode="Markdown")
    await message.answer(format_result(ev, transcript), parse_mode="Markdown")
    results.append(ev["score"])

    if queue:
        await state.update_data(results=results)
        await ask_question(message.chat.id, state, queue[0], queue[1:])
    else:
        if len(results) > 1:
            avg = round(sum(results) / len(results))
            await message.answer(f"🏁 *Mock yakuni: o'rtacha {avg}/75*", parse_mode="Markdown")
        await state.clear()
        await message.answer("Yana mashq qilamizmi?", reply_markup=menu_kb())


@dp.message(Exam.waiting_voice)
async def need_voice(message: types.Message):
    await message.answer("Iltimos, javobni *ovozli xabar* ko'rinishida yuboring 🎤", parse_mode="Markdown")


@dp.message()
async def fallback(message: types.Message):
    await message.answer("Boshlash uchun /start bosing.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
