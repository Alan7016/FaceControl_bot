"""
Telegram Check-In / Check-Out bot for dispatchers & staff.

Core features:
- Posts a pinned message with "Check In" / "Check Out" buttons at the
  start of each shift (and on demand via /post).
- Admins assign each worker to a shift (/setworker) so lateness is judged
  against THEIR actual schedule, not a guess.
- Check-ins more than `early_checkin_minutes` (default 60) before a shift's
  start are rejected outright.
- Every tap is logged (on time / late / early) to a local SQLite database.
- /weekly  -> CSV of the current calendar week (Mon-Sun): late & early
              minutes per worker, no dollar amounts.
- /monthly -> CSV of the current calendar month with fines calculated:
      * missing check-in AND/OR check-out for a shift -> flat $25
        (charged once, even if both are missing)
      * late check-in: <25 min -> $0, 25-44 min -> $15, 45+ min -> $25,
        plus $10 for every additional full hour beyond 45 min
        (e.g. 1h45m late = $25 + $10 = $35)
      * early check-out is reported but never fined

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
from datetime import datetime, timedelta, time as dtime, date as ddate
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
    "grace_minutes": 10,          # used only for the ✅/⚠️ label on the tap itself
    "early_checkin_minutes": 60,  # can't check in earlier than this before shift start
    "timezone": "Asia/Tashkent",
    "admins": [],
    "group_chat_id": None,
    "workers": {},  # {"<user_id>": {"name": "Sam", "shift": "main"}}
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
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
            action TEXT NOT NULL,       -- 'in' or 'out'
            shift TEXT NOT NULL,
            shift_date TEXT NOT NULL,   -- ISO date of the shift's SCHEDULED start
            ts TEXT NOT NULL,           -- ISO timestamp of the actual tap
            status TEXT NOT NULL,       -- 'on_time', 'late', 'early'
            late_minutes REAL DEFAULT 0,
            early_minutes REAL DEFAULT 0,
            assigned INTEGER DEFAULT 0  -- 1 if the worker had a registered shift
        )
        """
    )
    # lightweight migration for anyone upgrading from the earlier version
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(logs)")}
    for col, decl in [
        ("shift_date", "TEXT DEFAULT ''"),
        ("late_minutes", "REAL DEFAULT 0"),
        ("early_minutes", "REAL DEFAULT 0"),
        ("assigned", "INTEGER DEFAULT 0"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE logs ADD COLUMN {col} {decl}")
    conn.commit()
    conn.close()


def log_action(user_id, full_name, username, action, shift, shift_date, ts, status,
                late_minutes=0, early_minutes=0, assigned=False):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO logs (user_id, full_name, username, action, shift, shift_date, ts, "
        "status, late_minutes, early_minutes, assigned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, full_name, username, action, shift, shift_date.isoformat(), ts.isoformat(),
         status, late_minutes, early_minutes, 1 if assigned else 0),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Shift window math
# --------------------------------------------------------------------------

def _to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def shift_window(cfg: dict, shift_name: str, start_date: ddate):
    """(start_dt, end_dt) for the shift OCCURRENCE that begins on start_date."""
    tz = get_tz(cfg)
    w = cfg["shifts"][shift_name]
    sh, sm = map(int, w["start"].split(":"))
    eh, em = map(int, w["end"].split(":"))
    start_dt = datetime(start_date.year, start_date.month, start_date.day, sh, sm, tzinfo=tz)
    end_dt = datetime(start_date.year, start_date.month, start_date.day, eh, em, tzinfo=tz)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)  # overnight shift
    return start_dt, end_dt


def match_shift_date(cfg: dict, shift_name: str, ts: datetime, boundary: str) -> ddate:
    """
    Find which shift OCCURRENCE (identified by its start date) a tap belongs to,
    by checking yesterday/today/tomorrow's occurrence and picking whichever
    boundary (start for check-in, end for check-out) is closest to ts.
    """
    best_date, best_diff = None, None
    for delta in (-1, 0, 1):
        d = ts.date() + timedelta(days=delta)
        start_dt, end_dt = shift_window(cfg, shift_name, d)
        boundary_dt = start_dt if boundary == "start" else end_dt
        diff = abs((ts - boundary_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best_date, best_diff = d, diff
    return best_date


def _closest_shift_name(cfg: dict, now: datetime, field: str) -> str:
    """Fallback guess for workers with no registered shift assignment."""
    now_minutes = now.hour * 60 + now.minute
    best_name, best_diff = None, None
    for name, window in cfg["shifts"].items():
        b_minutes = _to_minutes(window[field])
        diff = min((now_minutes - b_minutes) % (24 * 60), (b_minutes - now_minutes) % (24 * 60))
        if best_diff is None or diff < best_diff:
            best_name, best_diff = name, diff
    return best_name


def resolve_boundary(cfg: dict, user_id: int, now: datetime, action: str):
    """
    Returns (shift_name, boundary_dt, shift_date, assigned) for a check-in ('in')
    or check-out ('out') tap happening at `now`.
    """
    worker = cfg["workers"].get(str(user_id))
    field = "start" if action == "in" else "end"
    if worker:
        shift_name = worker["shift"]
        shift_date = match_shift_date(cfg, shift_name, now, field)
        start_dt, end_dt = shift_window(cfg, shift_name, shift_date)
        boundary_dt = start_dt if action == "in" else end_dt
        return shift_name, boundary_dt, shift_date, True
    else:
        shift_name = _closest_shift_name(cfg, now, field)
        shift_date = now.date()
        start_dt, end_dt = shift_window(cfg, shift_name, shift_date)
        boundary_dt = start_dt if action == "in" else end_dt
        return shift_name, boundary_dt, shift_date, False


# --------------------------------------------------------------------------
# Fine calculation
# --------------------------------------------------------------------------

def late_fee(minutes: float) -> float:
    if minutes < 25:
        return 0
    if minutes < 45:
        return 15
    extra_hours = int((minutes - 45) // 60)
    return 25 + extra_hours * 10


MISSING_PUNCH_FINE = 25

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
    user = query.from_user

    shift_name, boundary_dt, shift_date, assigned = resolve_boundary(cfg, user.id, now, action)
    diff_minutes = (now - boundary_dt).total_seconds() / 60

    if action == "in":
        early_limit = cfg.get("early_checkin_minutes", 60)
        if diff_minutes < -early_limit:
            earliest = (boundary_dt - timedelta(minutes=early_limit)).strftime("%H:%M")
            await query.answer(
                f"Too early to check in for {shift_name} (starts {boundary_dt.strftime('%H:%M')}). "
                f"You can check in from {earliest}.",
                show_alert=True,
            )
            return
        late_minutes = max(0.0, diff_minutes)
        status = "late" if late_minutes > cfg.get("grace_minutes", 10) else "on_time"
        log_action(user.id, user.full_name, user.username or "", "in", shift_name, shift_date,
                   now, status, late_minutes=late_minutes, assigned=assigned)
    else:
        early_minutes = max(0.0, -diff_minutes)
        status = "early" if early_minutes > cfg.get("grace_minutes", 10) else "on_time"
        log_action(user.id, user.full_name, user.username or "", "out", shift_name, shift_date,
                   now, status, early_minutes=early_minutes, assigned=assigned)

    verb = "checked in" if action == "in" else "checked out"
    text = (
        f"{user.full_name} {verb} for *{shift_name}* at {now.strftime('%H:%M')} "
        f"— {STATUS_LABEL[status]}"
    )
    if not assigned:
        text += "\n_(not yet assigned to a shift — ask an admin to run /setworker)_"
    await query.answer(text=f"{verb.capitalize()} recorded ({status.replace('_',' ')})")
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


async def cmd_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    lines = [
        f"Grace period: {cfg['grace_minutes']} min",
        f"Earliest check-in: {cfg['early_checkin_minutes']} min before shift start",
        f"Timezone: {cfg['timezone']}",
        "",
    ]
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


async def cmd_setearly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /setearly <minutes>  (how early workers may check in before their shift)")
        return
    cfg["early_checkin_minutes"] = int(context.args[0])
    save_config(cfg)
    await update.message.reply_text(f"Workers may now check in up to {cfg['early_checkin_minutes']} minutes before their shift starts.")


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if cfg["admins"] and not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    target = update.effective_user.id if not context.args else int(context.args[0])
    if target not in cfg["admins"]:
        cfg["admins"].append(target)
        save_config(cfg)
    await update.message.reply_text(f"Admin added: {target}")


# --------------------------------------------------------------------------
# Worker <-> shift assignment
# --------------------------------------------------------------------------

async def cmd_setworker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setworker <shift>              -- as a REPLY to the worker's message
    /setworker <user_id> <shift>    -- direct, if you already know their id
    """
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return

    args = context.args
    target_id, target_name, shift_name = None, None, None

    if update.message.reply_to_message and len(args) == 1:
        target_id = update.message.reply_to_message.from_user.id
        target_name = update.message.reply_to_message.from_user.full_name
        shift_name = args[0]
    elif len(args) == 2 and args[0].isdigit():
        target_id = int(args[0])
        target_name = f"user {target_id}"
        shift_name = args[1]
    else:
        await update.message.reply_text(
            "Usage:\n"
            "• Reply to the worker's message with: /setworker <shift>\n"
            "• Or: /setworker <user_id> <shift>"
        )
        return

    if shift_name not in cfg["shifts"]:
        await update.message.reply_text(f"Unknown shift '{shift_name}'. Current shifts: {', '.join(cfg['shifts'])}")
        return

    cfg["workers"][str(target_id)] = {"name": target_name, "shift": shift_name}
    save_config(cfg)
    await update.message.reply_text(f"{target_name} is now assigned to the {shift_name} shift.")


async def cmd_removeworker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return

    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    if target_id is None or str(target_id) not in cfg["workers"]:
        await update.message.reply_text("Reply to the worker's message with /removeworker, or use /removeworker <user_id>.")
        return

    name = cfg["workers"].pop(str(target_id))["name"]
    save_config(cfg)
    await update.message.reply_text(f"Removed shift assignment for {name}.")


async def cmd_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not cfg["workers"]:
        await update.message.reply_text("No workers assigned yet. Reply to someone's message with /setworker <shift> to add one.")
        return
    lines = ["👷 Assigned workers:"]
    for uid, w in cfg["workers"].items():
        lines.append(f"• {w['name']} — {w['shift']} (id {uid})")
    await update.message.reply_text("\n".join(lines))


# --------------------------------------------------------------------------
# Reporting: /weekly (no fines) and /monthly (with fines)
# --------------------------------------------------------------------------

def _period_dates(period_start: ddate, period_end: ddate):
    d = period_start
    while d <= period_end:
        yield d
        d += timedelta(days=1)


def _fetch_punch(conn, user_id: int, shift: str, shift_date: ddate, action: str):
    row = conn.execute(
        "SELECT ts, late_minutes, early_minutes FROM logs "
        "WHERE user_id = ? AND shift = ? AND shift_date = ? AND action = ? "
        "ORDER BY ts LIMIT 1",
        (user_id, shift, shift_date.isoformat(), action),
    ).fetchone()
    return row


def build_period_rows(cfg: dict, period_start: ddate, period_end: ddate, with_fines: bool):
    """
    One row per (worker, shift-day) within the period. Only covers workers
    with a registered shift assignment, since fines/expectations need a
    known schedule to compare against.
    """
    tz = get_tz(cfg)
    conn = sqlite3.connect(DB_PATH)
    rows = []
    totals = {}  # user_id -> total fine

    for uid_str, w in cfg["workers"].items():
        uid = int(uid_str)
        name, shift = w["name"], w["shift"]
        totals.setdefault(uid, 0.0)
        for d in _period_dates(period_start, period_end):
            in_row = _fetch_punch(conn, uid, shift, d, "in")
            out_row = _fetch_punch(conn, uid, shift, d, "out")

            checkin_missing = in_row is None
            checkout_missing = out_row is None
            late_minutes = round(in_row[1], 1) if in_row else None
            early_minutes = round(out_row[2], 1) if out_row else None

            fine = 0.0
            if with_fines:
                if checkin_missing or checkout_missing:
                    fine = MISSING_PUNCH_FINE
                    if not checkin_missing and late_minutes:
                        fine += late_fee(late_minutes)
                else:
                    fine = late_fee(late_minutes or 0)
                totals[uid] += fine

            rows.append({
                "date": d.isoformat(),
                "worker": name,
                "shift": shift,
                "check_in": "MISSING" if checkin_missing else in_row[0][11:16],
                "late_minutes": "-" if checkin_missing else late_minutes,
                "check_out": "MISSING" if checkout_missing else out_row[0][11:16],
                "early_minutes": "-" if checkout_missing else early_minutes,
                **({"fine_usd": fine} if with_fines else {}),
            })
    conn.close()
    return rows, totals


def rows_to_csv(rows: list, fieldnames: list) -> io.BytesIO:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return io.BytesIO(buf.getvalue().encode())


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not cfg["workers"]:
        await update.message.reply_text("No workers assigned yet — use /setworker first.")
        return

    tz = get_tz(cfg)
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    rows, _ = build_period_rows(cfg, monday, min(sunday, today), with_fines=False)
    if not rows:
        await update.message.reply_text("No shift data for this week yet.")
        return

    csv_bytes = rows_to_csv(rows, ["date", "worker", "shift", "check_in", "late_minutes", "check_out", "early_minutes"])
    await update.message.reply_document(
        document=csv_bytes,
        filename=f"weekly_{monday.isoformat()}_to_{sunday.isoformat()}.csv",
        caption=f"📋 Weekly attendance: {monday.isoformat()} – {sunday.isoformat()} (no charges — see /monthly for fines).",
    )


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not cfg["workers"]:
        await update.message.reply_text("No workers assigned yet — use /setworker first.")
        return

    tz = get_tz(cfg)
    today = datetime.now(tz).date()
    # optional "YYYY-MM" argument to pull a past month
    if context.args:
        try:
            year, month = map(int, context.args[0].split("-"))
        except Exception:
            await update.message.reply_text("Usage: /monthly  or  /monthly YYYY-MM")
            return
    else:
        year, month = today.year, today.month

    period_start = ddate(year, month, 1)
    next_month = ddate(year + (month == 12), (month % 12) + 1, 1)
    period_end = min(next_month - timedelta(days=1), today)
    if period_start > today:
        await update.message.reply_text("That month hasn't started yet.")
        return

    rows, totals = build_period_rows(cfg, period_start, period_end, with_fines=True)
    if not rows:
        await update.message.reply_text("No shift data for that month yet.")
        return

    csv_bytes = rows_to_csv(
        rows, ["date", "worker", "shift", "check_in", "late_minutes", "check_out", "early_minutes", "fine_usd"]
    )

    name_by_id = {int(uid): w["name"] for uid, w in cfg["workers"].items()}
    summary_lines = [f"💰 Fines for {period_start.strftime('%B %Y')}:"]
    grand_total = 0.0
    for uid, total in sorted(totals.items(), key=lambda x: -x[1]):
        summary_lines.append(f"• {name_by_id.get(uid, uid)}: ${total:.0f}")
        grand_total += total
    summary_lines.append(f"\nTotal: ${grand_total:.0f}")

    await update.message.reply_document(
        document=csv_bytes,
        filename=f"monthly_{period_start.strftime('%Y-%m')}.csv",
        caption="\n".join(summary_lines),
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
    app.add_handler(CommandHandler("setearly", cmd_setearly))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("setworker", cmd_setworker))
    app.add_handler(CommandHandler("removeworker", cmd_removeworker))
    app.add_handler(CommandHandler("workers", cmd_workers))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CallbackQueryHandler(on_button))

    schedule_shift_jobs(app, cfg)

    logger.info("Bot starting (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
