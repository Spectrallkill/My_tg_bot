import os
import re
import time
import threading
from datetime import date, datetime, timedelta

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from flask import Flask

# ─── Keep-alive Flask server ──────────────────────────────────────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "I am alive!"

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    def run():
        try:
            flask_app.run(host="0.0.0.0", port=port)
        except OSError:
            pass
    threading.Thread(target=run, daemon=True).start()

# ─── Bot & config ─────────────────────────────────────────────────────────────

TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"]) if os.environ.get("ADMIN_ID") else None

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

VENUES = ["самал", "ататюрк", "арбат"]

VENUE_EMOJI = {
    "самал": "🏠",
    "ататюрк": "🎵",
    "арбат": "🎸",
}

bookings:         dict = {venue: {} for venue in VENUES}
pending_bookings: dict = {}
delete_queue:     list = []

DELETE_AFTER = 60
_lock = threading.Lock()

# ─── Admin check ──────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    if ADMIN_ID is None:
        return False
    return user_id == ADMIN_ID

# ─── Date helpers ─────────────────────────────────────────────────────────────

def today_str() -> str:
    return date.today().isoformat()

def tomorrow_str() -> str:
    return (date.today() + timedelta(days=1)).isoformat()

def day_after_tomorrow_str() -> str:
    return (date.today() + timedelta(days=2)).isoformat()

def format_date_label(date_str: str) -> str:
    d     = date.fromisoformat(date_str)
    today = date.today()
    if d == today + timedelta(days=1):
        return f"Завтра, {d.strftime('%d.%m')}"
    if d == today + timedelta(days=2):
        return f"Послезавтра, {d.strftime('%d.%m')}"
    return d.strftime("%d.%m.%Y")

# ─── Background: auto-delete messages ─────────────────────────────────────────

def auto_delete_worker():
    while True:
        time.sleep(5)
        now = time.monotonic()
        with _lock:
            remaining = []
            for delete_at, chat_id, message_id in delete_queue:
                if now >= delete_at:
                    try:
                        bot.delete_message(chat_id, message_id)
                    except Exception:
                        pass
                else:
                    remaining.append((delete_at, chat_id, message_id))
            delete_queue.clear()
            delete_queue.extend(remaining)

# ─── Background: midnight calendar cleanup ────────────────────────────────────

def midnight_cleanup_worker():
    while True:
        now           = datetime.now()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time.sleep((next_midnight - now).total_seconds())

        today = date.today().isoformat()
        with _lock:
            for venue in VENUES:
                bookings[venue].pop(today, None)
            for venue in VENUES:
                stale = [d for d in list(bookings[venue]) if d < today]
                for d in stale:
                    del bookings[venue][d]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def schedule_delete(msg, delay: int = DELETE_AFTER):
    with _lock:
        delete_queue.append((time.monotonic() + delay, msg.chat.id, msg.message_id))

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

def parse_time(t: str) -> int:
    if not _TIME_RE.match(t):
        raise ValueError(f"Некорректное время: {t!r}")
    h, m = map(int, t.split(":"))
    return h * 60 + m

def time_to_minutes(t: str) -> int:
    return parse_time(t)

def overlaps(s1, e1, s2, e2) -> bool:
    return s1 < e2 and s2 < e1

# ─── Schedule formatters ──────────────────────────────────────────────────────

def get_schedule_text() -> str:
    tomorrow = tomorrow_str()
    dat      = day_after_tomorrow_str()
    lines    = ["📋 <b>Расписание репетиций</b>\n"]
    any_booking = any(bookings[v].get(d) for v in VENUES for d in (tomorrow, dat))
    if not any_booking:
        lines.append("✨ Все площадки свободны!")
        return "\n".join(lines)
    for d, day_label in [(tomorrow, "📆 Завтра"), (dat, "📆 Послезавтра")]:
        if not any(bookings[v].get(d) for v in VENUES):
            continue
        lines.append(day_label)
        lines.append("━" * 22)
        for venue in VENUES:
            slots = bookings[venue].get(d, {})
            if not slots:
                continue
            lines.append(f"\n{VENUE_EMOJI[venue]} <b>{venue.upper()}</b>")
            for i, (user, data) in enumerate(sorted(slots.items(), key=lambda x: time_to_minutes(x[1]["start"]))):
                prefix = "  └" if i == len(slots) - 1 else "  ├"
                lines.append(f"{prefix} <code>{data['start']} – {data['end']}</code>  👤 {user}")
        lines.append("")
    return "\n".join(lines)


def get_day_schedule_text(date_str: str) -> str:
    label = format_date_label(date_str)
    lines = [f"📋 <b>Расписание — {label}</b>\n"]
    if not any(bookings[v].get(date_str) for v in VENUES):
        lines.append("✨ Все площадки свободны!")
        return "\n".join(lines)
    for venue in VENUES:
        slots = bookings[venue].get(date_str, {})
        if not slots:
            continue
        lines.append(f"{VENUE_EMOJI[venue]} <b>{venue.upper()}</b>")
        for i, (user, data) in enumerate(sorted(slots.items(), key=lambda x: time_to_minutes(x[1]["start"]))):
            prefix = "  └" if i == len(slots) - 1 else "  ├"
            lines.append(f"{prefix} <code>{data['start']} – {data['end']}</code>  👤 {user}")
        lines.append("")
    return "\n".join(lines)


def get_venue_schedule_text(venue: str) -> str:
    tomorrow = tomorrow_str()
    dat      = day_after_tomorrow_str()
    lines    = [f"{VENUE_EMOJI[venue]} <b>Расписание — {venue.upper()}</b>\n"]
    if not any(bookings[venue].get(d) for d in (tomorrow, dat)):
        lines.append("✨ Площадка свободна!")
        return "\n".join(lines)
    for d, day_label in [(tomorrow, "📆 Завтра"), (dat, "📆 Послезавтра")]:
        slots = bookings[venue].get(d, {})
        if not slots:
            continue
        lines.append(day_label)
        for i, (user, data) in enumerate(sorted(slots.items(), key=lambda x: time_to_minutes(x[1]["start"]))):
            prefix = "  └" if i == len(slots) - 1 else "  ├"
            lines.append(f"{prefix} <code>{data['start']} – {data['end']}</code>  👤 {user}")
        lines.append("")
    return "\n".join(lines)

# ─── Admin panel ──────────────────────────────────────────────────────────────

def get_admin_text() -> str:
    tomorrow = tomorrow_str()
    dat      = day_after_tomorrow_str()
    total    = sum(len(bookings[v].get(d, {})) for v in VENUES for d in (tomorrow, dat))
    lines    = [
        "🔐 <b>Админ-панель</b>\n",
        f"📊 Активных броней: <b>{total}</b>\n",
        f"📆 <b>Завтра ({format_date_label(tomorrow)}):</b>",
    ]
    for venue in VENUES:
        count = len(bookings[venue].get(tomorrow, {}))
        lines.append(f"  {VENUE_EMOJI[venue]} {venue.upper()}: {count}")
    lines.append(f"\n📆 <b>Послезавтра ({format_date_label(dat)}):</b>")
    for venue in VENUES:
        count = len(bookings[venue].get(dat, {}))
        lines.append(f"  {VENUE_EMOJI[venue]} {venue.upper()}: {count}")
    return "\n".join(lines)


def get_admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("📋 Всё расписание", callback_data="adm_view"))
    kb.row(
        InlineKeyboardButton("🗑 Завтра",       callback_data="adm_clear_tomorrow"),
        InlineKeyboardButton("🗑 Послезавтра",  callback_data="adm_clear_dat"),
    )
    kb.row(InlineKeyboardButton("💣 Очистить ВСЁ", callback_data="adm_clear_all"))
    return kb

# ─── Keyboards ────────────────────────────────────────────────────────────────

def get_main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("📅 Всё расписание", callback_data="schedule"))
    kb.row(
        InlineKeyboardButton("📅 Завтра",      callback_data="sched_day_tomorrow"),
        InlineKeyboardButton("📅 Послезавтра", callback_data="sched_day_dat"),
    )
    kb.row(
        InlineKeyboardButton("🏠 Самал",   callback_data="sched_самал"),
        InlineKeyboardButton("🎵 Ататюрк", callback_data="sched_ататюрк"),
        InlineKeyboardButton("🎸 Арбат",   callback_data="sched_арбат"),
    )
    return kb


def get_date_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📅 Завтра",      callback_data=f"date_tomorrow_{user_id}"),
        InlineKeyboardButton("📅 Послезавтра", callback_data=f"date_dat_{user_id}"),
    )
    kb.row(InlineKeyboardButton("❌ Отмена", callback_data=f"date_cancel_{user_id}"))
    return kb

# ─── Handlers ─────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    text = (
        "🎸 <b>Бот бронирования репетиционных площадок</b>\n\n"
        "📍 <b>Площадки:</b>\n"
        "  🏠 Самал  |  🎵 Ататюрк  |  🎸 Арбат\n\n"
        "📌 Введи <code>/help</code> чтобы увидеть все команды с примерами"
    )
    sent = bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())
    schedule_delete(sent)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    text = (
        "📖 <b>Справка по командам</b>\n\n"
        "▶️ <b>/book</b> — забронировать время\n"
        "  Формат: <code>/book ЧЧ:ММ ЧЧ:ММ площадка</code>\n"
        "  Примеры:\n"
        "  <code>/book 18:00 20:00 самал</code>\n"
        "  <code>/book 14:30 16:00 ататюрк</code>\n"
        "  <code>/book 20:00 22:00 арбат</code>\n"
        "  ⏱ Максимум 3 часа. Бот спросит: завтра или послезавтра.\n\n"
        "📋 <b>/schedule</b> — всё расписание\n"
        "  Показывает все брони на завтра и послезавтра.\n"
        "  Пример: <code>/schedule</code>\n\n"
        "❌ <b>/cancel</b> — отменить свою бронь\n"
        "  Формат: <code>/cancel площадка</code>\n"
        "  Примеры:\n"
        "  <code>/cancel самал</code>\n"
        "  <code>/cancel ататюрк</code>\n"
        "  <code>/cancel арбат</code>\n\n"
        "📍 <b>Площадки:</b> самал, ататюрк, арбат\n"
        "📆 <b>Окно бронирования:</b> завтра и послезавтра\n"
        "🕛 <b>Сброс расписания:</b> каждый день в 00:00"
    )
    sent = bot.send_message(message.chat.id, text)
    schedule_delete(sent, delay=120)


@bot.message_handler(commands=["schedule"])
def cmd_schedule(message):
    sent = bot.send_message(message.chat.id, get_schedule_text(), reply_markup=get_main_keyboard())
    schedule_delete(sent)


@bot.message_handler(commands=["book"])
def cmd_book(message):
    try:
        args = message.text.split()
        if len(args) < 4:
            raise ValueError

        start = args[1]
        end   = args[2]
        venue = args[3].lower()

        try:
            start_m = parse_time(start)
        except ValueError:
            sent = bot.send_message(message.chat.id,
                f"❌ <b>Неверное время начала:</b> <code>{start}</code>\n"
                "Используй формат <code>ЧЧ:ММ</code>, например <code>18:00</code>.\n"
                "Часы: 00–23, минуты: 00–59.")
            schedule_delete(sent)
            return

        try:
            end_m = parse_time(end)
        except ValueError:
            sent = bot.send_message(message.chat.id,
                f"❌ <b>Неверное время окончания:</b> <code>{end}</code>\n"
                "Используй формат <code>ЧЧ:ММ</code>, например <code>20:00</code>.\n"
                "Часы: 00–23, минуты: 00–59.")
            schedule_delete(sent)
            return

        if venue not in VENUES:
            venues_list = ", ".join(f"<b>{v}</b>" for v in VENUES)
            sent = bot.send_message(message.chat.id,
                f"❌ Площадка <b>«{venue}»</b> не найдена.\n\n📍 Доступные: {venues_list}")
            schedule_delete(sent)
            return

        if end_m <= start_m:
            sent = bot.send_message(message.chat.id,
                "❌ <b>Неверное время!</b>\nОкончание должно быть позже начала.")
            schedule_delete(sent)
            return

        if end_m - start_m > 180:
            sent = bot.send_message(message.chat.id,
                "❌ <b>Слишком долго!</b>\nМаксимум — <b>3 часа</b>.")
            schedule_delete(sent)
            return

        user_id = message.from_user.id
        with _lock:
            pending_bookings[user_id] = {"start": start, "end": end, "venue": venue}

        sent = bot.send_message(
            message.chat.id,
            f"📅 <b>На какой день?</b>\n\n"
            f"{VENUE_EMOJI[venue]} <b>{venue.upper()}</b>  🕒 <code>{start} – {end}</code>",
            reply_markup=get_date_keyboard(user_id),
        )
        schedule_delete(sent, delay=120)

    except (IndexError, ValueError):
        sent = bot.send_message(message.chat.id,
            "ℹ️ <b>Формат:</b> <code>/book 18:00 20:00 самал</code>\n\n"
            "📍 Площадки: самал, ататюрк, арбат")
        schedule_delete(sent)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    args      = message.text.split()
    user_name = message.from_user.first_name

    if len(args) < 2:
        sent = bot.send_message(message.chat.id,
            "ℹ️ <b>Формат:</b> <code>/cancel самал</code>\n\n"
            "📍 Площадки: самал, ататюрк, арбат")
        schedule_delete(sent)
        return

    venue = args[1].lower()

    if venue not in VENUES:
        venues_list = ", ".join(f"<b>{v}</b>" for v in VENUES)
        sent = bot.send_message(message.chat.id,
            f"❌ Площадка <b>«{venue}»</b> не найдена.\n\n📍 Доступные: {venues_list}")
        schedule_delete(sent)
        return

    for d in (tomorrow_str(), day_after_tomorrow_str()):
        with _lock:
            if user_name in bookings[venue].get(d, {}):
                data = bookings[venue][d].pop(user_name)
                s1 = bot.send_message(message.chat.id,
                    f"🗑 <b>Бронь отменена</b>\n\n"
                    f"{VENUE_EMOJI[venue]} <b>{venue.upper()}</b> — {format_date_label(d)}\n"
                    f"<code>{data['start']} – {data['end']}</code>")
                s2 = bot.send_message(message.chat.id, get_schedule_text(), reply_markup=get_main_keyboard())
                schedule_delete(s1)
                schedule_delete(s2)
                return

    sent = bot.send_message(message.chat.id,
        f"ℹ️ У тебя нет брони на {VENUE_EMOJI[venue]} <b>{venue.upper()}</b>.")
    schedule_delete(sent)


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        sent = bot.send_message(message.chat.id, "⛔ Нет доступа.")
        schedule_delete(sent)
        return
    sent = bot.send_message(message.chat.id, get_admin_text(), reply_markup=get_admin_keyboard())
    schedule_delete(sent, delay=300)

# ─── Booking callbacks ────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("date_"))
def cb_date(call):
    parts    = call.data.split("_")
    action   = parts[1]
    owner_id = parts[2] if len(parts) > 2 else None

    if owner_id and str(call.from_user.id) != owner_id:
        bot.answer_callback_query(call.id, "Это не ваш запрос.", show_alert=True)
        return

    user_id = call.from_user.id

    if action == "cancel":
        with _lock:
            pending_bookings.pop(user_id, None)
        bot.edit_message_text("❌ Бронирование отменено.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    with _lock:
        pending = pending_bookings.pop(user_id, None)

    if not pending:
        bot.answer_callback_query(call.id, "Запрос устарел. Попробуйте снова.", show_alert=True)
        return

    date_str = tomorrow_str() if action == "tomorrow" else day_after_tomorrow_str()
    start    = pending["start"]
    end      = pending["end"]
    venue    = pending["venue"]
    start_m  = time_to_minutes(start)
    end_m    = time_to_minutes(end)

    for user, data in bookings[venue].get(date_str, {}).items():
        if overlaps(start_m, end_m, time_to_minutes(data["start"]), time_to_minutes(data["end"])):
            bot.edit_message_text(
                f"❌ <b>Время занято!</b>\n\n"
                f"{VENUE_EMOJI[venue]} <b>{venue.upper()}</b> — {format_date_label(date_str)}\n"
                f"<code>{data['start']} – {data['end']}</code>  👤 {user}",
                call.message.chat.id, call.message.message_id)
            schedule_delete(call.message, 60)
            bot.answer_callback_query(call.id)
            return

    user_name = call.from_user.first_name
    with _lock:
        bookings[venue].setdefault(date_str, {})[user_name] = {"start": start, "end": end}

    bot.edit_message_text(
        f"✅ <b>Забронировано!</b>\n\n"
        f"{VENUE_EMOJI[venue]} <b>{venue.upper()}</b>\n"
        f"📆 {format_date_label(date_str)}\n"
        f"🕒 <code>{start} – {end}</code>\n"
        f"👤 {user_name}",
        call.message.chat.id, call.message.message_id)
    schedule_delete(call.message, 60)
    bot.answer_callback_query(call.id)

    sent = bot.send_message(call.message.chat.id, get_schedule_text(), reply_markup=get_main_keyboard())
    schedule_delete(sent)

# ─── Schedule view callbacks ──────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "schedule")
def cb_schedule(call):
    sent = bot.send_message(call.message.chat.id, get_schedule_text(), reply_markup=get_main_keyboard())
    schedule_delete(sent)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "sched_day_tomorrow")
def cb_sched_tomorrow(call):
    sent = bot.send_message(call.message.chat.id,
        get_day_schedule_text(tomorrow_str()), reply_markup=get_main_keyboard())
    schedule_delete(sent)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "sched_day_dat")
def cb_sched_dat(call):
    sent = bot.send_message(call.message.chat.id,
        get_day_schedule_text(day_after_tomorrow_str()), reply_markup=get_main_keyboard())
    schedule_delete(sent)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sched_"))
def cb_venue_schedule(call):
    venue = call.data.replace("sched_", "")
    if venue in VENUES:
        sent = bot.send_message(call.message.chat.id, get_venue_schedule_text(venue))
        schedule_delete(sent)
    bot.answer_callback_query(call.id)

# ─── Admin callbacks ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def cb_admin(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Нет доступа.", show_alert=True)
        return

    data = call.data

    if data == "adm_view":
        sent = bot.send_message(call.message.chat.id, get_schedule_text(), reply_markup=get_main_keyboard())
        schedule_delete(sent)
        bot.answer_callback_query(call.id)

    elif data == "adm_clear_tomorrow":
        tomorrow = tomorrow_str()
        with _lock:
            for venue in VENUES:
                bookings[venue].pop(tomorrow, None)
        bot.edit_message_text(
            f"🗑 <b>Брони на завтра ({format_date_label(tomorrow)}) удалены.</b>",
            call.message.chat.id, call.message.message_id,
            reply_markup=get_admin_keyboard())
        bot.answer_callback_query(call.id, "✅ Завтра очищено")

    elif data == "adm_clear_dat":
        dat = day_after_tomorrow_str()
        with _lock:
            for venue in VENUES:
                bookings[venue].pop(dat, None)
        bot.edit_message_text(
            f"🗑 <b>Брони на послезавтра ({format_date_label(dat)}) удалены.</b>",
            call.message.chat.id, call.message.message_id,
            reply_markup=get_admin_keyboard())
        bot.answer_callback_query(call.id, "✅ Послезавтра очищено")

    elif data == "adm_clear_all":
        with _lock:
            for venue in VENUES:
                bookings[venue].clear()
        bot.edit_message_text(
            "💣 <b>Все брони удалены.</b>",
            call.message.chat.id, call.message.message_id,
            reply_markup=get_admin_keyboard())
        bot.answer_callback_query(call.id, "✅ Всё очищено")

    else:
        bot.answer_callback_query(call.id)

# ─── Register bot commands ────────────────────────────────────────────────────

def register_commands():
    bot.set_my_commands([
        BotCommand("schedule", "Расписание репетиций"),
    ])

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=auto_delete_worker,      daemon=True).start()
    threading.Thread(target=midnight_cleanup_worker, daemon=True).start()
    keep_alive()
    bot.delete_webhook(drop_pending_updates=True)
    register_commands()
    print("Бот запущен")
    bot.infinity_polling(restart_on_change=False, timeout=30, long_polling_timeout=20)
