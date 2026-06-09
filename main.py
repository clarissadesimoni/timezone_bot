import asyncio, dateparser, pytz, os, re

from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Message, Update, User
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
    CommandHandler,
)
from timezonefinder import TimezoneFinder
from typing import Dict

# ================= CONFIG =================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGODB_URI")

DEFAULT_TZ = "UTC"
DEFAULT_TIME_FMT = "24h"
DEFAULT_DATE_FMT = "iso"

# ==========================================

client = MongoClient(MONGO_URI)
db = client["timezone-db"]
users_col = db["users"]

tf = TimezoneFinder()

TIME_PATTERN = r"!time\((.*?)\)"

# ================= USER ===================

def get_user(user_id):
    user = users_col.find_one({"_id": user_id})

    if not user:
        user = {
            "_id": user_id,
            "timezone": DEFAULT_TZ,
            "time_format": DEFAULT_TIME_FMT,
            "date_format": DEFAULT_DATE_FMT,
            "username": None,
            "chats": []
        }
        users_col.insert_one(user)

    return user

def update_user(user_id, data):
    users_col.update_one(
        {"_id": user_id},
        {"$set": data},
        upsert=True
    )

def register_user_in_chat(user: User, chat_id):
    get_user(user.id)
    users_col.update_one(
        {"_id": user.id},
        {
            "$set": {"username": user.username},
            "$addToSet": {"chats": chat_id}
        },
        upsert=True
    )

def get_chat_users(chat_id):
    return list(users_col.find({"chats": chat_id}))

# ================= PARSING =================

def extract_times(text):
    return re.findall(TIME_PATTERN, text)

def parse_time_expression(expr: str, sender_tz: str):
    # detect range using "-"
    if "-" in expr:
        parts = expr.split("-", 1)
        start_raw = parts[0].strip()
        end_raw = parts[1].strip()

        start_dt = dateparser.parse(
            start_raw,
            settings={
                "TIMEZONE": sender_tz,
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )

        # If end lacks am/pm, inherit from start
        if start_dt and ("am" in start_raw.lower() or "pm" in start_raw.lower()):
            if "am" not in end_raw.lower() and "pm" not in end_raw.lower():
                if "pm" in start_raw.lower():
                    end_raw += " pm"
                elif "am" in start_raw.lower():
                    end_raw += " am"

        end_dt = dateparser.parse(
            end_raw,
            settings={
                "TIMEZONE": sender_tz,
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )

        return (start_dt, end_dt)

    # single time
    dt = dateparser.parse(
        expr,
        settings={
            "TIMEZONE": sender_tz,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    return dt

def parse_times(times, sender_tz):
    parsed = []

    for t in times:
        parsed.append(parse_time_expression(t, sender_tz))

    return parsed

# ================= FORMATTING =================

def format_date(dt: datetime, user: Dict):
    fmt = user.get('date_format', DEFAULT_DATE_FMT)

    if fmt == "iso":
        return dt.strftime("%Y-%m-%d")
    elif fmt == "dmy":
        return dt.strftime("%d/%m/%Y")
    elif fmt == "mdy":
        return dt.strftime("%m/%d/%Y")

def format_datetime(dt: datetime, reference_dt: datetime, user: Dict):
    tz = pytz.timezone(user.get('timezone', DEFAULT_TZ))
    converted = dt.astimezone(tz)
    reference = reference_dt.astimezone(tz)

    same_day = converted.date() == reference.date()
    print(f'Date comparison: dt={format_date(converted, user)} ref={format_date(reference, user)}, same_day={same_day}')

    # time
    if user.get('time_format', DEFAULT_TIME_FMT) == "12h":
        time_str = converted.strftime("%I:%M %p").lstrip("0")
    else:
        time_str = converted.strftime("%H:%M")

    if same_day:
        return time_str
    else:
        date_str = format_date(converted, user)
        weekday = converted.strftime("%a")
        return f"{date_str} ({weekday}) {time_str}"

# ================= INLINE REPLACEMENT =================

def replace_times_inline(text, times, user):
    index = 0

    def repl(match):
        nonlocal index

        if index >= len(times):
            return match.group(0)

        value = times[index]
        index += 1

        # RANGE
        if isinstance(value, tuple):
            start, end = value

            if not start or not end:
                return match.group(0)

            start_str = format_datetime(start, datetime.now(), user)
            end_str = format_datetime(end, start, user)

            return f"{start_str}–{end_str}"

        # SINGLE TIME
        if value is None:
            return match.group(0)

        return format_datetime(value, datetime.now(), user)

    return re.sub(TIME_PATTERN, repl, text)

# ================= MENTIONS =================

def extract_usernames(text):
    return re.findall(r"@(\w+)", text)

def resolve_usernames(usernames):
    ids = []

    for uname in usernames:
        user = users_col.find_one({"username": uname})
        if user:
            ids.append(user["_id"])

    return ids

def get_mentioned_user_ids(message: Message):
    ids = set()

    # text mention
    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention":
                ids.add(ent.user.id)

    # username mentions
    usernames = extract_usernames(message.text)
    ids.update(resolve_usernames(usernames))

    return list(ids)

# ================= ORDERING =================

def order_users(sender_id, mentioned_ids, chat_user_ids):
    ordered = []

    ordered.append(sender_id)

    for uid in mentioned_ids:
        if uid != sender_id and uid in chat_user_ids:
            ordered.append(uid)

    for uid in chat_user_ids:
        if uid not in ordered:
            ordered.append(uid)

    return ordered

# ================= LOCATION =================

def detect_timezone(lat, lon):
    return tf.timezone_at(lat=lat, lng=lon)

# ================= CORE =================

async def process_message(message: Message):
    text = message.text

    times_raw = extract_times(text)
    if not times_raw:
        return None

    sender_id = message.from_user.id
    sender = get_user(sender_id)

    parsed_times = parse_times(times_raw, sender.get('timezone', DEFAULT_TZ))
    print(parsed_times)

    mentioned_ids = get_mentioned_user_ids(message)

    chat_users = get_chat_users(message.chat.id)
    chat_user_ids = [u["_id"] for u in chat_users]

    ordered_ids = order_users(sender_id, mentioned_ids, chat_user_ids)

    lines = []

    for uid in ordered_ids:
        user = get_user(uid)

        sentence = replace_times_inline(text, parsed_times, user)

        tz = user.get('timezone', DEFAULT_TZ)
        lines.append(f"[{tz}] {sentence}")

    return "\n".join(lines)

# ================= HANDLERS =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = update.message

    register_user_in_chat(msg.from_user, msg.chat.id)

    reply = await process_message(msg)

    if reply:
        sent = await msg.reply_text(reply)
        context.chat_data[msg.message_id] = sent.message_id

async def handle_edited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message

    reply = await process_message(msg)
    if not reply:
        return

    old_msg_id = context.chat_data.get(msg.message_id)

    if old_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=msg.chat.id,
                message_id=old_msg_id,
                text=reply,
            )
        except:
            pass
    else:
        await msg.reply_text(reply)

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location

    tz = detect_timezone(loc.latitude, loc.longitude)

    if tz:
        update_user(update.effective_user.id, {"timezone": tz})
        await update.message.reply_text(f"✅ Timezone set to: {tz}")
    else:
        await update.message.reply_text("❌ Could not detect timezone")

# ================= COMMANDS =================

async def set_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = context.args[0] if len(context.args) > 0 else DEFAULT_TZ
    update_user(update.effective_user.id, {"timezone": tz})
    await update.message.reply_text(f"✅ Timezone set to {tz}")

async def set_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = "12h" if len(context.args) > 0 and context.args[0] == "12" else "24h"
    update_user(update.effective_user.id, {"time_format": val})
    await update.message.reply_text(f"✅ Format: {val}")

async def set_date_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fmt = context.args[0] if len(context.args) > 0 else 'iso'
    update_user(update.effective_user.id, {"date_format": fmt})
    await update.message.reply_text(f"✅ Date format: {fmt}")

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("PING RECEIVED")
    await update.message.reply_text("pong")

# ================= MAIN =================

async def main():
    print("Building app...")
    app = ApplicationBuilder().token(TOKEN).build()

    print("Adding handlers...")

    app.add_handler(CommandHandler("settz", set_tz))
    app.add_handler(CommandHandler("setformat", set_format))
    app.add_handler(CommandHandler("setdateformat", set_date_format))
    app.add_handler(CommandHandler("ping", test))

    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edited))

    print("Clearing webhook & pending updates...")
    await app.bot.delete_webhook(drop_pending_updates=True)

    print("Running polling...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
