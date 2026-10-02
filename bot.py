"""
FaceControl_bot — Attendance + Finance tracking for a dispatching company.

=== ATTENDANCE (original feature set) ===
- Posts a pinned Check In / Check Out message at the start of each shift.
- Admins assign each worker to a shift (/setworker) so lateness is judged
  against THEIR actual schedule.
- Check-ins more than `early_checkin_minutes` before shift start are rejected.
- /weekly  -> CSV, current calendar week, no fines (late/early minutes only).
- /monthly -> CSV, current calendar month, WITH attendance fines.

=== FINANCE (new feature set) ===
Roles:
- Admins (you): full control — all commands, including private-only ones.
- Viewers (the 4 bosses): read-only — can run report/view commands, cannot
  log charges, bonuses, expenses, or change any settings.

Dispatcher charges (visible in the group — about a specific person's work):
  /rejection <reply-or-name>            -> flat fee (see /setrejectionfee)
  /charge <reply-or-name> <amount> <reason...>  -> custom amount

Private-only (DM the bot — never posted anywhere dispatchers can see):
  /expense <amount> <description...>    -> general company expense, no name attached
  /bonus <reply-or-name> <amount> <note...>     -> bonus added to a dispatcher's payout

Monthly dispatch-board report:
  Send (as a document, in a private chat with the bot, admin only) the
  Dispatch board exported as CSV or XLSX. Put the target month in the
  caption, e.g. "September 2026" or "2026-09" — if omitted, the bot uses
  the most recent month found in the file. The bot replies privately with
  two Excel files:
    1. Main Gross — per-MC-company breakdown (miles, loads, RPM, gross),
       active days, average weekly gross, total payout, company income
       (dispatch_fee_percent of gross), expenses, and the remainder.
    2. Dispatchers Gross — per dispatcher: gross (with per-company detail),
       commission, bonuses, charges (with itemized detail), and net payout.

Run:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC..."
    python bot.py
"""

import json
import csv
import io
import logging
import re
import sqlite3
from datetime import datetime, timedelta, time as dtime, date as ddate
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------
# Config & storage
# --------------------------------------------------------------------------

import os

BASE_DIR = Path(__file__).parent
# DATA_DIR should point at a persistent Volume on Railway (see README) so a
# redeploy never wipes admins, worker assignments, attendance history, or
# ledger entries. Falls back to the code directory for local testing, where
# persistence across runs doesn't matter.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "checkins.db"

DEFAULT_CONFIG = {
    # attendance
    "shifts": {
        "morning": {"start": "07:00", "end": "15:00"},
        "main": {"start": "15:00", "end": "23:00"},
        "night": {"start": "23:00", "end": "07:00"},
    },
    "grace_minutes": 10,
    "early_checkin_minutes": 60,
    "timezone": "Asia/Tashkent",
    "admins": [],
    "viewers": [],
    "group_chat_id": None,
    "workers": {},  # {"<user_id>": {"name": "Sam", "shift": "main", "sheet_name": "Doniyor"}}
    # finance
    "rejection_fee": 50,
    "dispatch_fee_percent": 3,
    "commission_percent": 1,
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
            action TEXT NOT NULL,
            shift TEXT NOT NULL,
            shift_date TEXT NOT NULL,
            ts TEXT NOT NULL,
            status TEXT NOT NULL,
            late_minutes REAL DEFAULT 0,
            early_minutes REAL DEFAULT 0,
            assigned INTEGER DEFAULT 0
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(logs)")}
    for col, decl in [
        ("shift_date", "TEXT DEFAULT ''"),
        ("late_minutes", "REAL DEFAULT 0"),
        ("early_minutes", "REAL DEFAULT 0"),
        ("assigned", "INTEGER DEFAULT 0"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE logs ADD COLUMN {col} {decl}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,              -- 'rejection' | 'other_charge' | 'bonus' | 'company_expense'
            dispatcher_name TEXT,            -- matches worker's sheet_name; NULL for company_expense
            amount REAL NOT NULL,            -- always positive; sign is implied by kind
            note TEXT,
            added_by INTEGER,
            created_at TEXT NOT NULL,
            entry_month TEXT                 -- 'YYYY-MM' this entry counts toward in reports; usually
                                              -- the month it was logged in, but can be backdated
                                              -- (e.g. /charge 15 September sleeping, logged in October)
        )
        """
    )
    existing_ledger_cols = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
    if "entry_month" not in existing_ledger_cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN entry_month TEXT")
    conn.execute("UPDATE ledger SET entry_month = substr(created_at, 1, 7) WHERE entry_month IS NULL")
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


def log_ledger(kind, dispatcher_name, amount, note, added_by, tz, entry_month=None):
    """entry_month, if given, is a 'YYYY-MM' string that overrides which month's
    report this entry counts toward (for backdated corrections). Returns the new
    row's id, shown to the admin so a mistake can be undone with /removecharge."""
    now = datetime.now(tz)
    if not entry_month:
        entry_month = f"{now.year:04d}-{now.month:02d}"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO ledger (kind, dispatcher_name, amount, note, added_by, created_at, entry_month) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, dispatcher_name, amount, note, added_by, now.isoformat(), entry_month),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _extract_month_arg(args: list, tz) -> tuple:
    """Scans a command's args for a month name (e.g. 'September', optionally
    followed by a 4-digit year like 'September 2026') and strips it out,
    wherever it appears. Returns (remaining_args, entry_month, month_label):
      - entry_month: 'YYYY-MM' to file the entry under, or None if no month
        was given (caller then defaults to the current month).
      - month_label: human-readable string ('September 2026') for the
        confirmation message, or None if no month was given.
    If a year isn't given, assumes the current year — unless that month
    hasn't happened yet this year, in which case it assumes last year
    (so '/charge 15 December ...' typed in January means last December).
    """
    now = datetime.now(tz)
    new_args = list(args)
    for i, a in enumerate(new_args):
        low = a.lower().strip(",.()")
        if low in MONTH_NAMES:
            month_num = MONTH_NAMES[low]
            del new_args[i]
            year_num = now.year
            if i < len(new_args) and re.fullmatch(r"(19|20)\d{2}", new_args[i]):
                year_num = int(new_args[i])
                del new_args[i]
            elif year_num == now.year and month_num > now.month:
                year_num -= 1
            entry_month = f"{year_num:04d}-{month_num:02d}"
            month_label = ddate(year_num, month_num, 1).strftime("%B %Y")
            return new_args, entry_month, month_label
    return new_args, None, None


# --------------------------------------------------------------------------
# Shift window math (attendance)
# --------------------------------------------------------------------------

def _to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def shift_window(cfg: dict, shift_name: str, start_date: ddate):
    tz = get_tz(cfg)
    w = cfg["shifts"][shift_name]
    sh, sm = map(int, w["start"].split(":"))
    eh, em = map(int, w["end"].split(":"))
    start_dt = datetime(start_date.year, start_date.month, start_date.day, sh, sm, tzinfo=tz)
    end_dt = datetime(start_date.year, start_date.month, start_date.day, eh, em, tzinfo=tz)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def match_shift_date(cfg: dict, shift_name: str, ts: datetime, boundary: str) -> ddate:
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
    now_minutes = now.hour * 60 + now.minute
    best_name, best_diff = None, None
    for name, window in cfg["shifts"].items():
        b_minutes = _to_minutes(window[field])
        diff = min((now_minutes - b_minutes) % (24 * 60), (b_minutes - now_minutes) % (24 * 60))
        if best_diff is None or diff < best_diff:
            best_name, best_diff = name, diff
    return best_name


def resolve_boundary(cfg: dict, user_id: int, now: datetime, action: str):
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


def late_fee(minutes: float) -> float:
    if minutes < 25:
        return 0
    if minutes < 45:
        return 15
    extra_hours = int((minutes - 45) // 60)
    return 25 + extra_hours * 10


MISSING_PUNCH_FINE = 25

STATUS_LABEL = {
    "on_time": "✅ On time",
    "late": "⚠️ Late",
    "early": "⚠️ Left early",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def is_admin(user_id: int, cfg: dict) -> bool:
    return user_id in cfg.get("admins", [])


def is_viewer_or_admin(user_id: int, cfg: dict) -> bool:
    return user_id in cfg.get("admins", []) or user_id in cfg.get("viewers", [])


def sheet_name_for(cfg: dict, user_id: int):
    w = cfg["workers"].get(str(user_id))
    return w.get("sheet_name") if w else None


# --------------------------------------------------------------------------
# Basic / shared commands
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot ready. /myid for your Telegram ID. /post in the group for check-in buttons."
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    c = update.effective_chat
    await update.message.reply_text(
        f"Your user id: `{u.id}`\nThis chat id: `{c.id}`", parse_mode=ParseMode.MARKDOWN
    )


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


async def cmd_addviewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    if target_id is None:
        await update.message.reply_text("Reply to the person's message with /addviewer, or use /addviewer <user_id>.")
        return
    if target_id not in cfg["viewers"]:
        cfg["viewers"].append(target_id)
        save_config(cfg)
    await update.message.reply_text(f"Viewer added: {target_id} (can view reports, cannot edit anything).")


# --------------------------------------------------------------------------
# Attendance: buttons, posting, tap handling
# --------------------------------------------------------------------------

def build_buttons_message(cfg: dict) -> str:
    lines = ["🕒 *Attendance*", "Tap below to check in or out.", ""]
    for name, w in cfg["shifts"].items():
        lines.append(f"• {name.capitalize()}: {w['start']}–{w['end']}")
    return "\n".join(lines)


def build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Check In", callback_data="checkin"),
          InlineKeyboardButton("🔴 Check Out", callback_data="checkout")]]
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
        chat_id=chat_id, text=build_buttons_message(cfg),
        parse_mode=ParseMode.MARKDOWN, reply_markup=build_keyboard(),
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
    text = f"{user.full_name} {verb} for *{shift_name}* at {now.strftime('%H:%M')} — {STATUS_LABEL[status]}"
    if not assigned:
        text += "\n_(not yet assigned to a shift — ask an admin to run /setworker)_"
    await query.answer(text=f"{verb.capitalize()} recorded ({status.replace('_',' ')})")
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


async def cmd_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    lines = [
        f"Grace period: {cfg['grace_minutes']} min",
        f"Earliest check-in: {cfg['early_checkin_minutes']} min before shift start",
        f"Timezone: {cfg['timezone']}", "",
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
        await update.message.reply_text("Usage: /setshift <name> <HH:MM start> <HH:MM end>")
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
        await update.message.reply_text("Usage: /setearly <minutes>")
        return
    cfg["early_checkin_minutes"] = int(context.args[0])
    save_config(cfg)
    await update.message.reply_text(f"Workers may check in up to {cfg['early_checkin_minutes']} min before shift start.")


async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    cfg["group_chat_id"] = update.effective_chat.id
    save_config(cfg)
    await update.message.reply_text("This group is now set as the attendance group.")


# --------------------------------------------------------------------------
# Worker <-> shift assignment (+ sheet_name link for finance)
# --------------------------------------------------------------------------

async def cmd_setworker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setworker <shift>                        -- reply to the worker's message
    /setworker <user_id> <shift>               -- direct
    /setworker <shift> <SheetName>              -- reply + also link their Google Sheet name
    /setworker <user_id> <shift> <SheetName>    -- direct + link
    """
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return

    args = context.args
    target_id, target_name, shift_name, sheet_name = None, None, None, None

    if update.message.reply_to_message and len(args) in (1, 2):
        target_id = update.message.reply_to_message.from_user.id
        target_name = update.message.reply_to_message.from_user.full_name
        shift_name = args[0]
        sheet_name = args[1] if len(args) == 2 else None
    elif len(args) in (2, 3) and args[0].isdigit():
        target_id = int(args[0])
        target_name = f"user {target_id}"
        shift_name = args[1]
        sheet_name = args[2] if len(args) == 3 else None
    else:
        await update.message.reply_text(
            "Usage:\n"
            "• Reply to worker's message: /setworker <shift> [SheetName]\n"
            "• Or: /setworker <user_id> <shift> [SheetName]"
        )
        return

    if shift_name not in cfg["shifts"]:
        await update.message.reply_text(f"Unknown shift '{shift_name}'. Current shifts: {', '.join(cfg['shifts'])}")
        return

    entry = cfg["workers"].get(str(target_id), {})
    entry.update({"name": target_name, "shift": shift_name})
    if sheet_name:
        entry["sheet_name"] = sheet_name
    cfg["workers"][str(target_id)] = entry
    save_config(cfg)

    msg = f"{target_name} is now assigned to the {shift_name} shift."
    if sheet_name:
        msg += f" Linked to sheet name '{sheet_name}'."
    elif "sheet_name" not in entry:
        msg += " (No Google Sheet name linked yet — finance reports won't match them until you add one, e.g. /setworker main Doniyor as a reply.)"
    await update.message.reply_text(msg)


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
        await update.message.reply_text("No workers assigned yet.")
        return
    lines = ["👷 Assigned workers:"]
    for uid, w in cfg["workers"].items():
        sn = w.get("sheet_name", "—")
        lines.append(f"• {w['name']} — {w['shift']} — sheet name: {sn} (id {uid})")
    await update.message.reply_text("\n".join(lines))


# --------------------------------------------------------------------------
# Attendance reporting: /weekly (no fines) and /monthly (with fines)
# --------------------------------------------------------------------------

def _period_dates(period_start: ddate, period_end: ddate):
    d = period_start
    while d <= period_end:
        yield d
        d += timedelta(days=1)


def _fetch_punch(conn, user_id: int, shift: str, shift_date: ddate, action: str):
    return conn.execute(
        "SELECT ts, late_minutes, early_minutes FROM logs "
        "WHERE user_id = ? AND shift = ? AND shift_date = ? AND action = ? ORDER BY ts LIMIT 1",
        (user_id, shift, shift_date.isoformat(), action),
    ).fetchone()


def build_period_rows(cfg: dict, period_start: ddate, period_end: ddate, with_fines: bool):
    conn = sqlite3.connect(DB_PATH)
    rows = []
    totals = {}
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
                "date": d.isoformat(), "worker": name, "shift": shift,
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


def get_monthly_attendance_fines(cfg: dict, year: int, month: int) -> dict:
    """
    {sheet_name: total_attendance_fine} for a given month, for linked workers only.

    IMPORTANT: a fully blank day (no check-in AND no check-out) is treated as a
    day off, not a violation — it is NEVER fined here, even though /monthly
    (the standalone attendance report) does fine it. Since this number now
    feeds directly into real payroll, assuming every day is a workday would
    silently dock someone's pay for a legitimate rest day. Only genuine
    partial problems are fined: showing up late, leaving early, or punching
    only one side of a pair (in without out, or out without in).
    """
    period_start = ddate(year, month, 1)
    next_month = ddate(year + (month == 12), (month % 12) + 1, 1)
    period_end = next_month - timedelta(days=1)
    rows, _ = build_period_rows(cfg, period_start, period_end, with_fines=True)

    uid_by_name = {w["name"]: int(uid) for uid, w in cfg["workers"].items()}
    out = {}
    for r in rows:
        if r["check_in"] == "MISSING" and r["check_out"] == "MISSING":
            continue  # full day off — not a violation
        uid = uid_by_name.get(r["worker"])
        sn = sheet_name_for(cfg, uid) if uid is not None else None
        if sn:
            out[sn] = out.get(sn, 0.0) + r.get("fine_usd", 0.0)
    return out


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_viewer_or_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins/viewers only.")
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
        document=csv_bytes, filename=f"weekly_{monday.isoformat()}_to_{sunday.isoformat()}.csv",
        caption=f"📋 Weekly attendance: {monday.isoformat()} – {sunday.isoformat()} (no charges).",
    )


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_viewer_or_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins/viewers only.")
        return
    if not cfg["workers"]:
        await update.message.reply_text("No workers assigned yet — use /setworker first.")
        return
    tz = get_tz(cfg)
    today = datetime.now(tz).date()
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

    csv_bytes = rows_to_csv(rows, ["date", "worker", "shift", "check_in", "late_minutes", "check_out", "early_minutes", "fine_usd"])
    name_by_id = {int(uid): w["name"] for uid, w in cfg["workers"].items()}
    summary_lines = [f"💰 Attendance fines for {period_start.strftime('%B %Y')}:"]
    grand_total = 0.0
    for uid, total in sorted(totals.items(), key=lambda x: -x[1]):
        summary_lines.append(f"• {name_by_id.get(uid, uid)}: ${total:.0f}")
        grand_total += total
    summary_lines.append(f"\nTotal: ${grand_total:.0f}")

    await update.message.reply_document(
        document=csv_bytes, filename=f"monthly_attendance_{period_start.strftime('%Y-%m')}.csv",
        caption="\n".join(summary_lines),
    )


# --------------------------------------------------------------------------
# Scheduled auto-posting
# --------------------------------------------------------------------------

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    chat_id = cfg.get("group_chat_id")
    if chat_id:
        await post_buttons(context, chat_id)


def schedule_shift_jobs(app: Application, cfg: dict):
    tz = get_tz(cfg)
    for name, w in cfg["shifts"].items():
        h, m = map(int, w["start"].split(":"))
        app.job_queue.run_daily(scheduled_post, time=dtime(hour=h, minute=m, tzinfo=tz), name=f"post_{name}")


# ==========================================================================
# FINANCE MODULE
# ==========================================================================

def _resolve_dispatcher_target(cfg: dict, update: Update, name_arg: str = None):
    """
    Resolve a /rejection, /charge, or /bonus target to a sheet_name string.
    Prefers a reply to the worker's message (looks up their linked sheet_name);
    falls back to treating the argument as a raw sheet name.
    Returns (sheet_name, display_name) or (None, None) if unresolvable.
    """
    if update.message.reply_to_message:
        uid = update.message.reply_to_message.from_user.id
        display = update.message.reply_to_message.from_user.full_name
        sn = sheet_name_for(cfg, uid)
        if sn:
            return sn, display
        # not linked — fall back to their Telegram name as the sheet name
        return display, display
    if name_arg:
        return name_arg, name_arg
    return None, None


async def cmd_rejection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    args, entry_month, month_label = _extract_month_arg(context.args, get_tz(cfg))
    name_arg = args[0] if args else None
    sheet_name, display = _resolve_dispatcher_target(cfg, update, name_arg)
    if not sheet_name:
        await update.message.reply_text(
            "Reply to the dispatcher's message with /rejection, or use /rejection <name>.\n"
            "Optionally add a month to backdate it, e.g. /rejection Asilbek September."
        )
        return
    fee = cfg.get("rejection_fee", 50)
    entry_id = log_ledger("rejection", sheet_name, fee, "rejection", update.effective_user.id, get_tz(cfg), entry_month)
    suffix = f" (for {month_label}, id {entry_id})" if month_label else f" (id {entry_id})"
    await update.message.reply_text(f"🔻 Rejection charge logged for {display}: ${fee:.0f}{suffix}")


async def cmd_charge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    args, entry_month, month_label = _extract_month_arg(context.args, get_tz(cfg))
    if update.message.reply_to_message:
        if len(args) < 2:
            await update.message.reply_text(
                "Usage (as reply): /charge <amount> <reason...>\n"
                "Optionally add a month anywhere to backdate it, e.g. /charge 15 September sleeping"
            )
            return
        amount_str, reason = args[0], " ".join(args[1:])
        sheet_name, display = _resolve_dispatcher_target(cfg, update)
    else:
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: /charge <name> <amount> <reason...>  (or reply to their message)\n"
                "Optionally add a month anywhere to backdate it, e.g. /charge Asilbek 15 September sleeping"
            )
            return
        name_arg, amount_str, reason = args[0], args[1], " ".join(args[2:])
        sheet_name, display = _resolve_dispatcher_target(cfg, update, name_arg)

    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text(f"'{amount_str}' isn't a valid amount.")
        return

    entry_id = log_ledger("other_charge", sheet_name, amount, reason, update.effective_user.id, get_tz(cfg), entry_month)
    suffix = f" (for {month_label}, id {entry_id})" if month_label else f" (id {entry_id})"
    await update.message.reply_text(f"🔻 Charge logged for {display}: ${amount:.0f} — {reason}{suffix}")


async def cmd_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please DM me this command — expenses shouldn't post in the group.")
        return
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    args, entry_month, month_label = _extract_month_arg(context.args, get_tz(cfg))
    if len(args) < 2:
        await update.message.reply_text(
            "Usage (DM only): /expense <amount> <description...>\n"
            "Optionally add a month anywhere to backdate it, e.g. /expense 300 September fuel reimbursement"
        )
        return
    try:
        amount = float(args[0])
    except ValueError:
        await update.message.reply_text(f"'{args[0]}' isn't a valid amount.")
        return
    description = " ".join(args[1:])
    entry_id = log_ledger("company_expense", None, amount, description, update.effective_user.id, get_tz(cfg), entry_month)
    suffix = f" (for {month_label}, id {entry_id})" if month_label else f" (id {entry_id})"
    await update.message.reply_text(f"Company expense logged privately: ${amount:.0f} — {description}{suffix}")


async def cmd_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please DM me this command.")
        return
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    args, entry_month, month_label = _extract_month_arg(context.args, get_tz(cfg))
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage (DM only): /bonus <dispatcher_sheet_name> <amount> [note...]\n"
            "Optionally add a month anywhere to backdate it, e.g. /bonus Doniyor 100 September great month"
        )
        return
    name_arg, amount_str = args[0], args[1]
    note = " ".join(args[2:]) if len(args) > 2 else "bonus"
    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text(f"'{amount_str}' isn't a valid amount.")
        return
    entry_id = log_ledger("bonus", name_arg, amount, note, update.effective_user.id, get_tz(cfg), entry_month)
    suffix = f" (for {month_label}, id {entry_id})" if month_label else f" (id {entry_id})"
    await update.message.reply_text(f"🎁 Bonus logged for {name_arg}: ${amount:.0f} — {note}{suffix}")


async def cmd_avans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM only: logs a cash advance (avans) paid to a dispatcher — money already
    handed to them, deducted from their net dispatch fee at month-end, same as
    a charge but tracked separately so it shows as its own column."""
    cfg = context.bot_data["cfg"]
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please DM me this command.")
        return
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    args, entry_month, month_label = _extract_month_arg(context.args, get_tz(cfg))
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage (DM only): /avans <dispatcher_sheet_name> <amount> [note...]\n"
            "Optionally add a month anywhere to backdate it, e.g. /avans Asilbek 100 September"
        )
        return
    name_arg, amount_str = args[0], args[1]
    note = " ".join(args[2:]) if len(args) > 2 else "avans"
    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text(f"'{amount_str}' isn't a valid amount.")
        return
    entry_id = log_ledger("advance", name_arg, amount, note, update.effective_user.id, get_tz(cfg), entry_month)
    suffix = f" (for {month_label}, id {entry_id})" if month_label else f" (id {entry_id})"
    await update.message.reply_text(f"💵 Advance logged for {name_arg}: ${amount:.0f} — {note}{suffix}")


LEDGER_KIND_LABEL = {
    "rejection": "Rejection", "other_charge": "Charge",
    "bonus": "Bonus", "company_expense": "Expense", "advance": "Advance",
}


async def cmd_recentcharges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM only: lists the most recent ledger entries (rejections, charges,
    bonuses, expenses) with their ids, so a mistaken one can be found and
    removed with /removecharge <id>. Optionally filter: /recentcharges <name>."""
    cfg = context.bot_data["cfg"]
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please DM me this command.")
        return
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    args = context.args
    name_filter = None
    limit = 20
    for a in args:
        if a.isdigit():
            limit = min(int(a), 100)
        else:
            name_filter = a
    conn = sqlite3.connect(DB_PATH)
    if name_filter:
        rows = conn.execute(
            "SELECT id, kind, dispatcher_name, amount, note, entry_month FROM ledger "
            "WHERE dispatcher_name LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{name_filter}%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, kind, dispatcher_name, amount, note, entry_month FROM ledger "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No ledger entries found.")
        return
    lines = ["🧾 Most recent ledger entries (newest first):"]
    for rid, kind, name, amount, note, entry_month in rows:
        who = name or "(company)"
        lines.append(f"#{rid} — {LEDGER_KIND_LABEL.get(kind, kind)} — {who} — ${amount:.0f} — {note} — {entry_month}")
    lines.append("\nTo delete a mistaken entry: /removecharge <id>")
    await update.message.reply_text("\n".join(lines))


async def cmd_removecharge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM only: deletes one ledger entry (rejection/charge/bonus/expense) by
    id. Use /recentcharges first to find the id."""
    cfg = context.bot_data["cfg"]
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please DM me this command.")
        return
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removecharge <id>  (see /recentcharges for ids)")
        return
    rid = int(context.args[0])
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT kind, dispatcher_name, amount, note, entry_month FROM ledger WHERE id = ?", (rid,)
    ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(f"No ledger entry with id {rid}.")
        return
    conn.execute("DELETE FROM ledger WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    kind, name, amount, note, entry_month = row
    who = name or "(company)"
    await update.message.reply_text(
        f"🗑 Deleted entry #{rid} — {LEDGER_KIND_LABEL.get(kind, kind)} — {who} — ${amount:.0f} — {note} — {entry_month}"
    )


async def cmd_setrejectionfee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not context.args:
        await update.message.reply_text(f"Usage: /setrejectionfee <amount>\nCurrent: ${cfg['rejection_fee']}")
        return
    try:
        cfg["rejection_fee"] = float(context.args[0])
    except ValueError:
        await update.message.reply_text("That's not a valid number.")
        return
    save_config(cfg)
    await update.message.reply_text(f"Rejection fee set to ${cfg['rejection_fee']:.0f}")


async def cmd_setdispatchfee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not context.args:
        await update.message.reply_text(f"Usage: /setdispatchfee <percent>\nCurrent: {cfg['dispatch_fee_percent']}%")
        return
    cfg["dispatch_fee_percent"] = float(context.args[0])
    save_config(cfg)
    await update.message.reply_text(f"Company dispatch fee set to {cfg['dispatch_fee_percent']}% of gross.")


async def cmd_setcommission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins only.")
        return
    if not context.args:
        await update.message.reply_text(f"Usage: /setcommission <percent>\nCurrent: {cfg['commission_percent']}%")
        return
    cfg["commission_percent"] = float(context.args[0])
    save_config(cfg)
    await update.message.reply_text(f"Dispatcher commission set to {cfg['commission_percent']}% of their gross.")


async def cmd_financesettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.bot_data["cfg"]
    if not is_viewer_or_admin(update.effective_user.id, cfg):
        await update.message.reply_text("Admins/viewers only.")
        return
    await update.message.reply_text(
        f"Rejection fee: ${cfg['rejection_fee']:.0f}\n"
        f"Company dispatch fee: {cfg['dispatch_fee_percent']}% of gross\n"
        f"Dispatcher commission: {cfg['commission_percent']}% of their gross"
    )


# ---- Dispatch board parsing -----------------------------------------------

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_money(raw: str) -> float:
    """Handles '$1 770,25', '$447,04', '$0,00', '1770.25', '' -> float."""
    if raw is None:
        return 0.0
    s = str(raw).strip().replace("$", "").replace("\xa0", " ").strip()
    if not s:
        return 0.0
    s = s.replace(" ", "")
    # European style: comma = decimal separator (since thousands sep was a space, already stripped)
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:
        # e.g. "1,770.25" (comma=thousands, dot=decimal) — strip commas
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_miles(raw: str) -> float:
    if raw is None:
        return 0.0
    s = str(raw).strip().replace("\xa0", " ").replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def extract_day_number(pickup_text: str):
    """Pulls the day-of-month out of messy pickup strings like
    'TREMONT, Pa Aug 1, 22:45 EDT' or 'Portland, OR Mon, Aug 3, 03:45 PDT'."""
    import re
    m = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})",
        pickup_text or "", re.IGNORECASE,
    )
    return int(m.group(2)) if m else None


def detect_month_divider(row: list) -> int:
    """If this row is just a month-name divider, return its month number, else None."""
    for cell in row:
        cell_str = str(cell).strip().lower()
        if cell_str in MONTH_NAMES:
            return MONTH_NAMES[cell_str]
    return None


def read_rows_from_file(file_path: str):
    """Yields raw rows (list of cell strings) from a .csv or .xlsx file."""
    if file_path.lower().endswith(".csv"):
        with open(file_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                yield row
    else:
        from openpyxl import load_workbook
        wb = load_workbook(file_path, data_only=True)
        ws = wb.worksheets[0]
        for row in ws.iter_rows(values_only=True):
            yield ["" if c is None else str(c) for c in row]


def parse_dispatch_board(file_path: str, target_year: int, target_month: int):
    """
    Returns a list of load dicts for the target month:
      {mc_company, load_id, status, dispatcher, miles, rate, pickup_date}
    Tracks month-divider rows to know which block of rows belongs to which month.
    Assumes the sheet only spans one "era" of consecutive years for simplicity;
    target_year is used to label output but month-matching is by divider name only.
    """
    rows = list(read_rows_from_file(file_path))
    results = []
    current_month = None

    # Find header row to map columns by name, falling back to known fixed layout.
    header_idx = None
    header_map = {}
    for i, row in enumerate(rows[:5]):
        norm = [str(c).strip().upper() for c in row]
        if "LOAD ID" in norm or "DISPATCH" in norm:
            header_idx = i
            for j, name in enumerate(norm):
                header_map[name] = j
            break

    def col(row, *names, default_idx=None):
        for n in names:
            if n in header_map and header_map[n] < len(row):
                return row[header_map[n]]
        if default_idx is not None and default_idx < len(row):
            return row[default_idx]
        return ""

    start_idx = (header_idx + 1) if header_idx is not None else 1
    for row in rows[start_idx:]:
        if not any(str(c).strip() for c in row):
            continue
        month_hit = detect_month_divider(row)
        if month_hit:
            current_month = month_hit
            continue

        load_id = str(col(row, "LOAD ID", default_idx=1)).strip()
        if not load_id:
            continue  # not a data row

        if current_month != target_month:
            continue

        mc_company = str(col(row, "1", default_idx=0)).strip() or str(row[0]).strip()
        status = str(col(row, "STATUS", default_idx=5)).strip().upper()
        pickup_text = str(col(row, "PICK UP", default_idx=6)).strip()
        dispatcher = str(col(row, "DISPATCH", default_idx=8)).strip()
        miles = parse_miles(col(row, "MILE", default_idx=9))
        rate = parse_money(col(row, "RATE", default_idx=10))

        results.append({
            "mc_company": mc_company, "load_id": load_id, "status": status,
            "dispatcher": dispatcher, "miles": miles, "rate": rate,
            "pickup_day": extract_day_number(pickup_text),
        })
    return results


# ---- Report generation -----------------------------------------------------

def generate_finance_reports(cfg: dict, loads: list, year: int, month: int, out_dir: Path):
    """Builds the two xlsx reports. Returns (main_gross_path, dispatchers_gross_path)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill

    header_fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    bold = Font(bold=True)

    dispatch_pct = cfg.get("dispatch_fee_percent", 3) / 100.0
    commission_pct = cfg.get("commission_percent", 1) / 100.0

    total_gross = sum(l["rate"] for l in loads)
    total_miles = sum(l["miles"] for l in loads)
    total_loads = len(loads)

    # per MC company breakdown
    by_company = {}
    for l in loads:
        c = by_company.setdefault(l["mc_company"], {"gross": 0.0, "miles": 0.0, "loads": 0})
        c["gross"] += l["rate"]
        c["miles"] += l["miles"]
        c["loads"] += 1

    # per dispatcher (sheet_name) breakdown
    by_dispatcher = {}
    for l in loads:
        d = by_dispatcher.setdefault(l["dispatcher"], {"gross": 0.0, "loads": 0, "by_company": {}})
        d["gross"] += l["rate"]
        d["loads"] += 1
        bc = d["by_company"].setdefault(l["mc_company"], 0.0)
        d["by_company"][l["mc_company"]] = bc + l["rate"]

    # ledger entries for this month (rejections / other charges / bonuses / company expenses)
    conn = sqlite3.connect(DB_PATH)
    ledger_rows = conn.execute(
        "SELECT kind, dispatcher_name, amount, note, created_at FROM ledger "
        "WHERE entry_month = ?",
        (f"{year:04d}-{month:02d}",),
    ).fetchall()
    conn.close()

    company_expenses = [(amt, note, ts) for kind, name, amt, note, ts in ledger_rows if kind == "company_expense"]
    dispatcher_charges = {}   # sheet_name -> [(kind, amount, note, ts), ...]
    dispatcher_bonuses = {}   # sheet_name -> [(amount, note, ts), ...]
    dispatcher_advances = {}  # sheet_name -> [(amount, note, ts), ...]
    for kind, name, amt, note, ts in ledger_rows:
        if kind in ("rejection", "other_charge") and name:
            dispatcher_charges.setdefault(name, []).append((kind, amt, note, ts))
        elif kind == "bonus" and name:
            dispatcher_bonuses.setdefault(name, []).append((amt, note, ts))
        elif kind == "advance" and name:
            dispatcher_advances.setdefault(name, []).append((amt, note, ts))

    attendance_fines = get_monthly_attendance_fines(cfg, year, month)

    # active days (distinct calendar days in the month with >=1 load, by pickup day)
    active_days_set = {l["pickup_day"] for l in loads if l.get("pickup_day")}
    active_days = len(active_days_set) if active_days_set else "n/a (no parseable pickup dates)"

    # weekly average (simple: total / (days_in_month/7))
    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    avg_weekly_gross = total_gross / (days_in_month / 7.0) if days_in_month else 0

    all_dispatcher_names = (set(by_dispatcher) | set(dispatcher_charges) | set(dispatcher_bonuses)
                             | set(dispatcher_advances) | set(attendance_fines))

    net_payouts = {}
    for name in all_dispatcher_names:
        gross = by_dispatcher.get(name, {}).get("gross", 0.0)
        commission = gross * commission_pct
        bonus_total = sum(a for a, _, _ in dispatcher_bonuses.get(name, []))
        charge_total = sum(a for _, a, _, _ in dispatcher_charges.get(name, []))
        advance_total = sum(a for a, _, _ in dispatcher_advances.get(name, []))
        fine_total = attendance_fines.get(name, 0.0)
        net = commission + bonus_total - charge_total - advance_total - fine_total
        net_payouts[name] = {
            "gross": gross, "commission": commission, "bonus": bonus_total,
            "charges": charge_total, "advances": advance_total,
            "attendance_fines": fine_total, "net": net,
        }

    total_payout = sum(v["net"] for v in net_payouts.values())
    company_income = total_gross * dispatch_pct
    total_company_expenses = sum(a for a, _, _ in company_expenses)
    remainder = company_income - total_payout - total_company_expenses

    month_label = ddate(year, month, 1).strftime("%B %Y")

    # ---------------- Main Gross workbook ----------------
    wb1 = Workbook()
    ws = wb1.active
    ws.title = "Main Gross"
    ws.append([f"Main Gross — {month_label}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["MC Company", "Loads", "Miles", "RPM ($/mi)", "Gross ($)"])
    for c in ws[3]:
        c.font = bold
        c.fill = header_fill
    for company, d in sorted(by_company.items(), key=lambda x: -x[1]["gross"]):
        rpm = d["gross"] / d["miles"] if d["miles"] else 0
        ws.append([company, d["loads"], round(d["miles"], 1), round(rpm, 2), round(d["gross"], 2)])
    total_row = ["TOTAL", total_loads, round(total_miles, 1),
                 round(total_gross / total_miles, 2) if total_miles else 0, round(total_gross, 2)]
    ws.append(total_row)
    for c in ws[ws.max_row]:
        c.font = bold

    ws.append([])
    ws.append(["Average weekly gross", round(avg_weekly_gross, 2)])
    ws.append(["Active days (days with >=1 load)", active_days])
    ws.append(["Total payout to dispatchers (net)", round(total_payout, 2)])
    ws.append([f"Company income ({cfg.get('dispatch_fee_percent',3)}% of gross)", round(company_income, 2)])

    ws.append([])
    ws.append(["EXPENSES"])
    ws[ws.max_row][0].font = bold
    ws.append(["Dispatcher salaries (net payouts)", round(total_payout, 2)])
    ws.append(["Other company expenses", round(total_company_expenses, 2)])
    for amt, note, ts in company_expenses:
        ws.append(["  • " + (note or ""), round(amt, 2)])
    ws.append([])
    ws.append(["REMAINDER (company income − salaries − other expenses)", round(remainder, 2)])
    ws[ws.max_row][0].font = bold
    ws[ws.max_row][1].font = bold

    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 12), 45)

    main_path = out_dir / f"main_gross_{year:04d}-{month:02d}.xlsx"
    wb1.save(main_path)

    # ---------------- Dispatchers Gross workbook ----------------
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.title = "Dispatchers Gross"
    ws2.append([f"Dispatchers Gross — {month_label}"])
    ws2["A1"].font = Font(bold=True, size=14)
    ws2.append([])
    ws2.append(["Dispatcher", "Gross ($)", "Commission", "Bonuses", "Advances", "Charges", "Net Dispatch Fee"])
    for c in ws2[3]:
        c.font = bold
        c.fill = header_fill
    for name in sorted(all_dispatcher_names):
        v = net_payouts[name]
        ws2.append([name, round(v["gross"], 2), round(v["commission"], 2), round(v["bonus"], 2),
                    round(v["advances"], 2),
                    round(v["charges"] + v["attendance_fines"], 2), round(v["net"], 2)])

    ws2.append([])
    ws2.append(["CHARGE DETAIL (by dispatcher)"])
    ws2[ws2.max_row][0].font = bold
    ws2.append(["Dispatcher", "Type", "Amount", "Note", "When"])
    for c in ws2[ws2.max_row]:
        c.font = bold
    for name in sorted(all_dispatcher_names):
        for kind, amt, note, ts in dispatcher_charges.get(name, []):
            ws2.append([name, kind, round(amt, 2), note, ts[:16]])
        fine = attendance_fines.get(name, 0.0)
        if fine:
            ws2.append([name, "attendance_fine", round(fine, 2), "late/missing punches", ""])

    ws2.append([])
    ws2.append(["BONUS DETAIL (by dispatcher)"])
    ws2[ws2.max_row][0].font = bold
    ws2.append(["Dispatcher", "Amount", "Note", "When"])
    for c in ws2[ws2.max_row]:
        c.font = bold
    for name in sorted(all_dispatcher_names):
        for amt, note, ts in dispatcher_bonuses.get(name, []):
            ws2.append([name, round(amt, 2), note, ts[:16]])

    ws2.append([])
    ws2.append(["ADVANCE DETAIL (by dispatcher)"])
    ws2[ws2.max_row][0].font = bold
    ws2.append(["Dispatcher", "Amount", "Note", "When"])
    for c in ws2[ws2.max_row]:
        c.font = bold
    for name in sorted(all_dispatcher_names):
        for amt, note, ts in dispatcher_advances.get(name, []):
            ws2.append([name, round(amt, 2), note, ts[:16]])

    ws2.append([])
    ws2.append(["GROSS DETAIL — by company (per dispatcher)"])
    ws2[ws2.max_row][0].font = bold
    ws2.append(["Dispatcher", "MC Company", "Gross ($)"])
    for c in ws2[ws2.max_row]:
        c.font = bold
    for name in sorted(by_dispatcher):
        for company, amt in sorted(by_dispatcher[name]["by_company"].items(), key=lambda x: -x[1]):
            ws2.append([name, company, round(amt, 2)])

    for col_cells in ws2.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws2.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 12), 45)

    disp_path = out_dir / f"dispatchers_gross_{year:04d}-{month:02d}.xlsx"
    wb2.save(disp_path)

    return main_path, disp_path


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin DMs the bot the exported dispatch board (csv/xlsx) -> generates reports."""
    cfg = context.bot_data["cfg"]
    if update.effective_chat.type != ChatType.PRIVATE:
        return  # ignore documents posted in the group
    if not is_admin(update.effective_user.id, cfg):
        return

    doc = update.message.document
    if not doc or not (doc.file_name.lower().endswith(".csv") or doc.file_name.lower().endswith(".xlsx")):
        return

    tz = get_tz(cfg)
    now = datetime.now(tz)
    year, month = now.year, now.month
    caption = (update.message.caption or "").strip()
    if caption:
        parsed = None
        try:
            if "-" in caption and len(caption.split("-")[0]) == 4:
                y, m = caption.split("-")[:2]
                parsed = (int(y), int(m))
            else:
                parts = caption.lower().split()
                for p in parts:
                    if p in MONTH_NAMES:
                        mnum = MONTH_NAMES[p]
                        ynum = next((int(x) for x in parts if x.isdigit() and len(x) == 4), now.year)
                        parsed = (ynum, mnum)
                        break
        except Exception:
            parsed = None
        if parsed:
            year, month = parsed

    tg_file = await doc.get_file()
    local_path = BASE_DIR / f"upload_{doc.file_unique_id}_{doc.file_name}"
    await tg_file.download_to_drive(str(local_path))

    await update.message.reply_text(f"Processing dispatch board for {ddate(year, month, 1).strftime('%B %Y')}…")

    try:
        loads = parse_dispatch_board(str(local_path), year, month)
        if not loads:
            await update.message.reply_text(
                f"No rows found for {ddate(year, month, 1).strftime('%B %Y')} in that file. "
                "Check the month divider rows exist, or specify the month in the caption, e.g. 'September 2026'."
            )
            return
        main_path, disp_path = generate_finance_reports(cfg, loads, year, month, BASE_DIR)
        await update.message.reply_document(document=open(main_path, "rb"), filename=main_path.name)
        await update.message.reply_document(document=open(disp_path, "rb"), filename=disp_path.name)
    except Exception as e:
        logger.exception("Finance report generation failed")
        await update.message.reply_text(f"Something went wrong processing that file: {e}")
    finally:
        try:
            local_path.unlink()
        except Exception:
            pass


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

    # basic / attendance
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("shifts", cmd_shifts))
    app.add_handler(CommandHandler("setshift", cmd_setshift))
    app.add_handler(CommandHandler("setgrace", cmd_setgrace))
    app.add_handler(CommandHandler("setearly", cmd_setearly))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("addviewer", cmd_addviewer))
    app.add_handler(CommandHandler("setworker", cmd_setworker))
    app.add_handler(CommandHandler("removeworker", cmd_removeworker))
    app.add_handler(CommandHandler("workers", cmd_workers))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CallbackQueryHandler(on_button))

    # finance
    app.add_handler(CommandHandler("rejection", cmd_rejection))
    app.add_handler(CommandHandler("charge", cmd_charge))
    app.add_handler(CommandHandler("expense", cmd_expense))
    app.add_handler(CommandHandler("bonus", cmd_bonus))
    app.add_handler(CommandHandler("avans", cmd_avans))
    app.add_handler(CommandHandler("recentcharges", cmd_recentcharges))
    app.add_handler(CommandHandler("removecharge", cmd_removecharge))
    app.add_handler(CommandHandler("setrejectionfee", cmd_setrejectionfee))
    app.add_handler(CommandHandler("setdispatchfee", cmd_setdispatchfee))
    app.add_handler(CommandHandler("setcommission", cmd_setcommission))
    app.add_handler(CommandHandler("financesettings", cmd_financesettings))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    schedule_shift_jobs(app, cfg)

    logger.info("Bot starting (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
