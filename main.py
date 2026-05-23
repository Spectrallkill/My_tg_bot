import asyncio
import os
import time
from datetime import date, timedelta
from threading import Thread

from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# ─── Keep-alive Flask server ──────────────────────────────────────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "I am alive!"

def keep_alive():
    thread = Thread(target=lambda: flask_app.run(host="0.0.0.0", port=8080), daemon=True)
    thread.start()

# ─── Bot setup ────────────────────────────────────────────────────────────────

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

VENUES = ["самал", "ататюрк", "арбат"]

VENUE_EMOJI = {
    "самал": "🏠",
    "ататюрк": "🎵",
    "арбат": "🎸",
}

bookings: dict[str, dict[str, dict[str, dict]]] = {venue: {} for venue in VENUES}
pending_bookings: dict[int, dict] = {}
delete_queue: list[tuple[float, int, int]] = []
booking_ttl_queue: list[tuple[float, str, str, str]] = []

TTL_TODAY    = 24 * 3600
TTL_TOMORROW = 48 * 3600
DELETE_AFTER = 60

# ─── Auto-delete messages ─────────────────────────────────────────────────────

def schedule_delete(msg: Message, delay: int = DELETE_AFTER) -> None:
    delete_queue.append((time.monotonic() + delay, msg.chat.id, msg.message_id))


async def auto_delete_loop() -> None:
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        remaining: list[tuple[float, int, int]] = []
        for delete_at, chat_id, message_id in delete_queue:
            if now >= delete_at:
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
            else:
                remaining.append((delete_at, chat_id, message_id))
        delete_queue.clear()
        delete_queue.extend(remaining)


async def send_auto(message: Message, text: str, reply_markup=None, delay: int = DELETE_AFTER) -> Message:
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    schedule_delete(sent, delay)
    return sent

# ─── Auto-expire bookings ─────────────────────────────────────────────────────

def schedule_booking_ttl(venue: str, date_str: str, user_name: str, ttl: int) -> None:
    booking_ttl_queue.append((time.time() + ttl, venue, date_str, user_name))


async def auto_expire_bookings_loop() -> None:
    while True:
        await asyncio.sleep(10)
        now = time.time()
        remaining: list[tuple[float, str, str, str]] = []
        for expire_at, venue, date_str, user_name in booking_ttl_queue:
            if now >= expire_at:
                try:
                    bookings[venue].get(date_str, {}).pop(user_name, None)
                except Exception:
                    pass
            else:
                remaining.append((expire_at, venue, date_str, user_name))
        booking_ttl_queue.clear()
        booking_ttl_queue.extend(remaining)

# ─── Date helpers ─────────────────────────────────────────────────────────────

def today_str() -> str:
    return date.today().isoformat()

def tomorrow_str() -> str:
    return (date.today() + timedelta(days=1)).isoformat()

def format_date_label(date_str: str) -> str:
    d = date.fromisoformat(date_str)
    today = date.today()
    if d == today:
        return f"Сегодня, {d.strftime('%d.%m')}"
    elif d == today + timedelta(days=1):
        return f"Завтра, {d.strftime('%d.%m')}"
    return d.strftime("%d.%m.%Y")

# ─── Schedule formatters ──────────────────────────────────────────────────────

def time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m

def overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
    return s1 < e2 and s2 < e1

def get_schedule_text() -> str:
    today = today_str()
    tomorrow = tomorrow_str()
    lines = ["📋 <b>Расписание репетиций</b>\n"]
    any_booking = any(bookings[v].get(d) for v in VENUES for d in (today, tomorrow))
    if not any_booking:
        lines.append("✨ Все площадки свободны!")
        return "\n".join(lines)
    for d, day_label in [(today, "📆 Сегодня"), (tomorrow, "📆 Завтра")]:
        has_day = any(bookings[v].get(d) for v in VENUES)
        if not has_day:
            continue
        lines.append(f"{day_label}")
        lines.append("━" * 22)
        for venue in VENUES:
            day_slots = bookings[venue].get(d, {})
            if not day_slots:
                continue
            emoji = VENUE_EMOJI[venue]
            lines.append(f"\n{emoji} <b>{venue.upper()}</b>")
            sorted_slots = sorted(day_slots.items(), key=lambda x: time_to_minutes(x[1]["start"]))
            for i, (user, data) in enumerate(sorted_slots):
                prefix = "  └" if i == len(sorted_slots) - 1 else "  ├"
                lines.append(f"{prefix} <code>{data['start']} – {data['end']}</code>  👤 {user}")
        lines.append("")
    return "\n".join(lines)

def get_day_schedule_text(date_str: str) -> str:
    label = format_date_label(date_str)
    lines = [f"📋 <b>Расписание — {label}</b>\n"]
    has_any = any(bookings[v].get(date_str) for v in VENUES)
    if not has_any:
        lines.append("✨ Все площадки свободны!")
        return "\n".join(lines)
    for venue in VENUES:
        day_slots = bookings[venue].get(date_str, {})
        if not day_slots:
            continue
        emoji = VENUE_EMOJI[venue]
        lines.append(f"{emoji} <b>{venue.upper()}</b>")
        sorted_slots = sorted(day_slots.items(), key=lambda x: time_to_minutes(x[1]["start"]))
        for i, (user, data) in enumerate(sorted_slots):
            prefix = "  └" if i == len(sorted_slots) - 1 else "  ├"
            lines.append(f"{prefix} <code>{data['start']} – {data['end']}</code>  👤 {user}")
        lines.append("")
    return "\n".join(lines)

def get_venue_schedule_text(venue: str) -> str:
    today = today_str()
    tomorrow = tomorrow_str()
    emoji = VENUE_EMOJI[venue]
    lines = [f"{emoji} <b>Расписание — {venue.upper()}</b>\n"]
    has_any = any(bookings[venue].get(d) for d in (today, tomorrow))
    if not has_any:
        lines.append("✨ Площадка свободна!")
        return "\n".join(lines)
    for d, day_label in [(today, "📆 Сегодня"), (tomorrow, "📆 Завтра")]:
        day_slots = bookings[venue].get(d, {})
        if not day_slots:
            continue
        lines.append(f"{day_label}")
        sorted_slots = sorted(day_slots.items(), key=lambda x: time_to_minutes(x[1]["start"]))
        for i, (user, data) in enumerate(sorted_slots):
            prefix = "  └" if i == len(sorted_slots) - 1 else "  ├"
            lines.append(f"{prefix} <code>{data['start']} – {data['end']}</code>  👤 {user}")
        lines.append("")
    return "\n".join(lines)

# ─── Keyboards ────────────────────────────────────────────────────────────────

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Всё расписание", callback_data="schedule")],
            [
                InlineKeyboardButton(text="☀️ Сегодня", callback_data="sched_day_today"),
                InlineKeyboardButton(text="🌙 Завтра",   callback_data="sched_day_tomorrow"),
            ],
            [
                InlineKeyboardButton(text="🏠 Самал",   callback_data="sched_самал"),
                InlineKeyboardButton(text="🎵 Ататюрк", callback_data="sched_ататюрк"),
                InlineKeyboardButton(text="🎸 Арбат",   callback_data="sched_арбат"),
            ],
        ]
    )

def get_date_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Сегодня", callback_data=f"date_today_{user_id}"),
                InlineKeyboardButton(text="📅 Завтра",  callback_data=f"date_tomorrow_{user_id}"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"date_cancel_{user_id}")],
        ]
    )

# ─── Handlers ─────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start(message: Message):
    text = (
        "🎸 <b>Бот бронирования репетиционных площадок</b>\n\n"
        "📍 <b>Доступные площадки:</b>\n"
        "  🏠 Самал\n"
        "  🎵 Ататюрк\n"
        "  🎸 Арбат\n\n"
        "📌 <b>Команды:</b>\n"
        "  <code>/book 18:00 20:00 самал</code> — забронировать\n"
        "  <code>/schedule</code> — всё расписание\n"
        "  <code>/cancel самал</code> — отменить бронь\n\n"
        "⏱ Максимальная длительность — <b>3 часа</b>"
    )
    sent = await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")
    schedule_delete(sent)

@dp.message(Command("schedule"))
async def schedule_cmd(message: Message):
    await send_auto(message, get_schedule_text(), reply_markup=get_main_keyboard())

@dp.message(Command("book"))
async def book(message: Message):
    try:
        args = message.text.split()
        if len(args) < 4:
            raise ValueError("not enough args")
        start = args[1]
        end   = args[2]
        venue = args[3].lower()
        if venue not in VENUES:
            venues_list = ", ".join(f"<b>{v}</b>" for v in VENUES)
            await send_auto(message, f"❌ Площадка <b>«{venue}»</b> не найдена.\n\n📍 Доступные площадки: {venues_list}")
            return
        start_m = time_to_minutes(start)
        end_m   = time_to_minutes(end)
        if end_m <= start_m:
            await send_auto(message, "❌ <b>Неверное время!</b>\nВремя окончания должно быть позже начала.")
            return
        if end_m - start_m > 180:
            await send_auto(message, "❌ <b>Слишком долго!</b>\nМаксимальная длительность — <b>3 часа</b>.")
            return
        user_id = message.from_user.id
        pending_bookings[user_id] = {"start": start, "end": end, "venue": venue}
        emoji = VENUE_EMOJI[venue]
        await send_auto(
            message,
            f"📅 <b>На какой день?</b>\n\n{emoji} <b>{venue.upper()}</b>  🕒 <code>{start} – {end}</code>",
            reply_markup=get_date_keyboard(user_id),
            delay=120,
        )
    except (IndexError, ValueError):
        await send_auto(message, "ℹ️ <b>Формат команды:</b>\n<code>/book 18:00 20:00 самал</code>\n\n📍 Площадки: самал, ататюрк, арбат")

@dp.callback_query(F.data.startswith("date_"))
async def handle_date_choice(callback: CallbackQuery):
    parts    = callback.data.split("_")
    action   = parts[1]
    owner_id = parts[2] if len(parts) > 2 else None
    if owner_id and str(callback.from_user.id) != owner_id:
        await callback.answer("Это не ваш запрос.", show_alert=True)
        return
    user_id = callback.from_user.id
    if action == "cancel":
        pending_bookings.pop(user_id, None)
        await callback.message.edit_text("❌ Бронирование отменено.", parse_mode="HTML")
        await callback.answer()
        return
    pending = pending_bookings.pop(user_id, None)
    if not pending:
        await callback.answer("Запрос устарел. Попробуйте снова.", show_alert=True)
        return
    date_str = today_str() if action == "today" else tomorrow_str()
    start    = pending["start"]
    end      = pending["end"]
    venue    = pending["venue"]
    start_m  = time_to_minutes(start)
    end_m    = time_to_minutes(end)
    day_slots = bookings[venue].get(date_str, {})
    for user, data in day_slots.items():
        if overlaps(start_m, end_m, time_to_minutes(data["start"]), time_to_minutes(data["end"])):
            emoji = VENUE_EMOJI[venue]
            await callback.message.edit_text(
                f"❌ <b>Время занято!</b>\n\n{emoji} <b>{venue.upper()}</b> — {format_date_label(date_str)}\n"
                f"<code>{data['start']} – {data['end']}</code>  👤 {user}",
                parse_mode="HTML",
            )
            schedule_delete(callback.message, 60)
            await callback.answer()
            return
    user_name = callback.from_user.first_name
    bookings[venue].setdefault(date_str, {})[user_name] = {"start": start, "end": end}
    ttl = TTL_TODAY if action == "today" else TTL_TOMORROW
    schedule_booking_ttl(venue, date_str, user_name, ttl)
    emoji = VENUE_EMOJI[venue]
    await callback.message.edit_text(
        f"✅ <b>Забронировано!</b>\n\n{emoji} <b>{venue.upper()}</b>\n"
        f"📆 {format_date_label(date_str)}\n🕒 <code>{start} – {end}</code>\n👤 {user_name}",
        parse_mode="HTML",
    )
    schedule_delete(callback.message, 60)
    await callback.answer()
    sent = await callback.message.answer(get_schedule_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")
    schedule_delete(sent)

@dp.message(Command("cancel"))
async def cancel(message: Message):
    args      = message.text.split()
    user_name = message.from_user.first_name
    if len(args) < 2:
        await send_auto(message, "ℹ️ <b>Формат команды:</b>\n<code>/cancel самал</code>\n\n📍 Площадки: самал, ататюрк, арбат")
        return
    venue = args[1].lower()
    if venue not in VENUES:
        venues_list = ", ".join(f"<b>{v}</b>" for v in VENUES)
        await send_auto(message, f"❌ Площадка <b>«{venue}»</b> не найдена.\n\n📍 Доступные площадки: {venues_list}")
        return
    for d in (today_str(), tomorrow_str()):
        if user_name in bookings[venue].get(d, {}):
            data  = bookings[venue][d].pop(user_name)
            emoji = VENUE_EMOJI[venue]
            await send_auto(message, f"🗑 <b>Бронь отменена</b>\n\n{emoji} <b>{venue.upper()}</b> — {format_date_label(d)}\n<code>{data['start']} – {data['end']}</code>")
            await send_auto(message, get_schedule_text(), reply_markup=get_main_keyboard())
            return
    emoji = VENUE_EMOJI[venue]
    await send_auto(message, f"ℹ️ У тебя нет брони на {emoji} <b>{venue.upper()}</b>.")

@dp.callback_query(F.data == "schedule")
async def show_schedule(callback: CallbackQuery):
    sent = await callback.message.answer(get_schedule_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")
    schedule_delete(sent)
    await callback.answer()

@dp.callback_query(F.data == "sched_day_today")
async def show_today_schedule(callback: CallbackQuery):
    sent = await callback.message.answer(get_day_schedule_text(today_str()), reply_markup=get_main_keyboard(), parse_mode="HTML")
    schedule_delete(sent)
    await callback.answer()

@dp.callback_query(F.data == "sched_day_tomorrow")
async def show_tomorrow_schedule(callback: CallbackQuery):
    sent = await callback.message.answer(get_day_schedule_text(tomorrow_str()), reply_markup=get_main_keyboard(), parse_mode="HTML")
    schedule_delete(sent)
    await callback.answer()

@dp.callback_query(F.data.startswith("sched_"))
async def show_venue_schedule(callback: CallbackQuery):
    venue = callback.data.replace("sched_", "")
    sent  = await callback.message.answer(get_venue_schedule_text(venue), parse_mode="HTML")
    schedule_delete(sent)
    await callback.answer()

# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    asyncio.create_task(auto_delete_loop())
    asyncio.create_task(auto_expire_bookings_loop())
    print("Бот запущен")
    await dp.start_polling(bot)


keep_alive()
asyncio.run(main())
