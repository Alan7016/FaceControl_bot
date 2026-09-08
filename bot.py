"""
Telegram Check-In / Check-Out bot for dispatchers & staff.

- Posts a pinned message with "Check In" / "Check Out" buttons at the
  start of each shift (and on demand via /post).
- Matches each tap to the closest shift and flags late arrivals /
  early departures based on a configurable grace period.
- Logs every action to a local SQLite database.
- Shift times are editable live by admins via /setshift, no redeploy needed.

Run:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC..."
    python bot.py
"""

import json
import csv
import io
import logging
import sqlite3
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------
# Config & storage
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "checkins.db"

DEFAULT_CONFIG = {
    "shifts": {
        "morning": {"start": "07:00", "end": "15:00"},
        "main": {"start": "15:00", "end": "23:00"},
        "night": {"start": "23:00", "end": "07:00"},
    },
    "grace_minutes": 10,
    "timezone": "Asia/Tashkent",
    "admins": [],
    "group_chat_id": None,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    # backfill any missing keys (e.g. after upgrading the bot)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone", "Asia/Tashkent"))


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            full_name TEXT,
            username TEXT,
            action TEXT NOT NULL,      -- 'in' or 'out'
            shift TEXT NOT NULL,
            ts TEXT NOT NULL,          -- ISO timestamp, local tz
            status TEXT NOT NULL       -- 'on_time', 'late', 'early'
        )
        """
    )
    conn.commit()
    conn.close()


def log_action(user_id, full_name, username, action, shift, ts, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO logs (user_id, full_name, username, action, shift, ts, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, full_name, username, action, shift, ts.isoformat(), status),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Shift matching logic
# --------------------------------------------------------------------------

def _to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _closest_shift(cfg: dict, now: datetime, field: str):
    """
    field = 'start' for check-in matching, 'end' for check-out matching.
    Finds the shift whose start/end time (wrapped over 24h) is closest
    to `now`, so a slightly-early or slightly-late tap still lands on
    the right shift. Handles overnight shifts (e.g. 23:00-07:00) fine
    because we only compare against the single boundary time, not the
    whole window.
    """
    now_minutes = now.hour * 60 + now.minute
    best_name, best_diff, best_boundary = None, None, None
    for name, window in cfg["shifts"].items():
        boundary = window[field]
        b_minutes = _to_minutes(boundary)
        diff = (now_minutes - b_minutes) % (24 * 60)
        # distance going "forward" vs "backward" around the clock
        diff = min(diff, 24 * 60 - diff)
        if best_diff is None or diff < best_diff:
            best_name, best_diff, best_boundary = name, diff, boundary
    return best_name, best_boundary


def evaluate(cfg: dict, now: datetime, action: str):
    """
    Returns (shift_name, status) where status is 'on_time', 'late', or 'early'.
    For check-in: late if now is after start + grace.
    For check-out: early if now is before end - grace.
    """
    grace = cfg.get("grace_minutes", 10)
    field = "start" if action == "in" else "end"
    shift_name, boundary = _closest_shift(cfg, now, field)
    b_minutes = _to_minutes(boundary)
    now_minutes = now.hour * 60 + now.minute

    # signed difference, shortest path around the 24h clock (positive = after boundary)
    diff = (now_minutes - b_minutes) % (24 * 60)
    if diff > 12 * 60:
        diff -= 24 * 60  # now is actually *before* the boundary, wrapped

    if action == "in":
        status = "late" if diff > grace else "on_time"
    else:
        status = "early" if diff < -grace else "on_time"

    return shift_name, status


# --------------------------------------------------------------------------
# Telegram handlers
# --------------------------------------------------------------------------

STATUS_LABEL = {
    "on_time": "✅ On time",
    "late": "⚠️ Late",
    "early": "⚠️ Left early",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def is_admin(user_id: int, cfg: dict) -> bool:
    return user_id in cfg.get("admins", [])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Check-in/out bot ready. Use /myid to get your Telegram ID for admin setup, "
        "or /post in the group to show the check-in buttons."
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = update.effective_chat
    await update.message.reply_text(
        f"Your user id: `{u.id}`\nThis chat id: `{c.id}`", parse_mode=ParseMode.MARKDOWN
    )


def build_buttons_message(cfg: dict) -> str:
    lines = ["🕒 *Attendance*", "Tap below to check in or out.", ""]
    for name, w in cfg["shifts"].items():
        lines.append(f"• {name.capitalize()}: {w['start']}–{w['end']}")
    return "\n".join(lines)


def build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Check In", callback_data="checkin"),
                InlineKeyboardButton("🔴 Check Out", callback_data="checkout"),
            ]
        ]
    )


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    await post_buttons(context, update.effective_chat.id)


async def post_buttons(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    cfg = context.bot_data["cfg"]
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=build_buttons_message(cfg),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_keyboard(),
    )
    try:
        old_id = context.bot_data.get("pinned_msg_id")
        if old_id:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=old_id)
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=True)
        context.bot_data["pinned_msg_id"] = msg.message_id
    except Exception as e:
        logger.warning("Could not pin message: %s", e)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cfg = context.bot_data["cfg"]
    tz = get_tz(cfg)
    now = datetime.now(tz)
    action = "in" if query.data == "checkin" else "out"

    shift_name, status = evaluate(cfg, now, action)
    user = query.from_user
    full_name = user.full_name
    username = user.username or ""

    log_action(user.id, full_name, username, action, shift_name, now, status)

    verb = "checked in" if action == "in" else "checked out"
    text = (
        f"{full_name} {verb} for *{shift_name}* at {now.strftime('%H:%M')} "
        f"— {STATUS_LABEL[status]}"
    )
    await query.answer(text=f"{verb.capitalize()} recorded ({status.replace('_',' ')})")
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


async def cmd_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    lines = [f"Grace period: {cfg['grace_minutes']} min", f"Timezone: {cfg['timezone']}", ""]
    for name, w in cfg["shifts"].items():
        lines.append(f"{name}: {w['start']}–{w['end']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_setshift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    args = context.args
    if len(args) != 3:
        await update.message.reply_text("Usage: /setshift <name> <HH:MM start> <HH:MM end>\ne.g. /setshift morning 07:00 15:00")
        return
    name, start, end = args
    for t in (start, end):
        try:
            h, m = map(int, t.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            await update.message.reply_text(f"'{t}' isn't a valid HH:MM time.")
            return
    cfg["shifts"][name] = {"start": start, "end": end}
    save_config(cfg)
    await update.message.reply_text(f"Updated: {name} is now {start}–{end}")


async def cmd_setgrace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /setgrace <minutes>")
        return
    cfg["grace_minutes"] = int(context.args[0])
    save_config(cfg)
    await update.message.reply_text(f"Grace period set to {cfg['grace_minutes']} minutes.")


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    # first admin can be added by anyone once the list is empty (bootstrap);
    # after that, only existing admins can add more.
    if cfg["admins"] and not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    target = update.effective_user.id if not context.args else int(context.args[0])
    if target not in cfg["admins"]:
        cfg["admins"].append(target)
        save_config(cfg)
    await update.message.reply_text(f"Admin added: {target}")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    tz = get_tz(cfg)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT full_name, action, shift, ts, status FROM logs WHERE ts LIKE ? ORDER BY ts",
        (f"{today}%",),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No check-ins/outs logged today yet.")
        return
    lines = [f"📋 Report for {today}:"]
    for full_name, action, shift, ts, status in rows:
        t = datetime.fromisoformat(ts).strftime("%H:%M")
        verb = "IN " if action == "in" else "OUT"
        lines.append(f"{t}  {verb}  {full_name} ({shift}) — {STATUS_LABEL[status]}")
    await update.message.reply_text("\n".join(lines))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    days = 30
    if context.args and context.args[0].isdigit():
        days = int(context.args[0])
    since = (datetime.now(get_tz(cfg)) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT full_name, username, action, shift, ts, status FROM logs WHERE ts >= ? ORDER BY ts",
        (since,),
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["full_name", "username", "action", "shift", "timestamp", "status"])
    writer.writerows(rows)
    buf.seek(0)

    await update.message.reply_document(
        document=io.BytesIO(buf.getvalue().encode()),
        filename=f"attendance_last_{days}d.csv",
        caption=f"Attendance export — last {days} day(s), {len(rows)} record(s).",
    )


# --------------------------------------------------------------------------
# Scheduled auto-posting at each shift start
# --------------------------------------------------------------------------

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    chat_id = cfg.get("group_chat_id")
    if chat_id:
        await post_buttons(context, chat_id)


def schedule_shift_jobs(app: Application, cfg: dict):
    tz = get_tz(cfg)
    job_queue = app.job_queue
    for name, w in cfg["shifts"].items():
        h, m = map(int, w["start"].split(":"))
        job_queue.run_daily(scheduled_post, time=dtime(hour=h, minute=m, tzinfo=tz), name=f"post_{name}")


async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registers the current chat as the one the bot should auto-post attendance buttons to."""
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    cfg["group_chat_id"] = update.effective_chat.id
    save_config(cfg)
    await update.message.reply_text("This group is now set as the attendance group.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    import os

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable to your bot's token from @BotFather.")

    init_db()
    cfg = load_config()

    app = Application.builder().token(token).build()
    app.bot_data["cfg"] = cfg

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("shifts", cmd_shifts))
    app.add_handler(CommandHandler("setshift", cmd_setshift))
    app.add_handler(CommandHandler("setgrace", cmd_setgrace))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_button))

    schedule_shift_jobs(app, cfg)

    logger.info("Bot starting (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
