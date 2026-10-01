# FaceControl_bot — Attendance + Finance

One bot, two jobs:
1. **Attendance** — Check In/Out buttons, shift assignment, late/early tracking, weekly/monthly reports with attendance fines.
2. **Finance** — rejection/charge/bonus logging, company expenses, and two private monthly Excel reports built from your Google Sheet dispatch board plus everything logged in the bot.

This file only covers what's **new** (finance). For the original attendance setup (BotFather, GitHub deploy, shift assignment), see the setup steps you already have from before — nothing about that changed.

## ⚠️ Set up persistent storage before you do anything else

Without this, **every time you push an update to GitHub, Railway wipes all your data** — admins, worker assignments, attendance history, and all logged charges/bonuses. This almost certainly just happened to you. Fix it once, now:

1. On Railway, click into the FaceControl_bot box → **Settings** tab.
2. Find **Volumes** in the left-side settings list → click **+ New Volume**.
3. Set the mount path to `/data` → create it.
4. Go to the **Variables** tab → add a new variable: `DATA_DIR` = `/data`.
5. This triggers a redeploy. Once it's done, your admin list, worker assignments, and attendance history will persist across every future update — you'll never have to redo this setup again.

**Recovering from the wipe that already happened:**
1. Run `/addadmin` (no arguments) — since the admin list is currently empty, this works for anyone and makes you admin again.
2. Redo `/setworker <shift> <SheetName>` (reply to each worker's message) for everyone.
3. Redo `/addviewer` for your 4 bosses.
4. Redo `/setrejectionfee 200` (and any other settings you'd changed).
5. Do steps 1-4 **after** setting up the Volume above, so this is the last time you ever have to redo them.

## Roles

- **Admins** (you): full control — every command, including the private-only ones.
- **Viewers** (the 4 bosses): read-only — `/weekly`, `/monthly`, `/financesettings`. Cannot log charges, bonuses, or expenses, and cannot change any settings.

Add a viewer the same way you add a worker — reply to their message:
```
/addviewer
```
or `/addviewer <user_id>` directly.

## Linking a worker to their Google Sheet name

The dispatch board identifies people by the name in the **DISPATCH** column (e.g. "Doniyor", "Asilbek"). For finance reports to match a Telegram worker to their sheet entries, link the two when you assign their shift:

```
/setworker main Doniyor        (reply to their message)
```
or
```
/setworker <user_id> main Doniyor
```

Check it worked with `/workers` — it'll show the linked sheet name next to each person.

## Logging charges, bonuses, expenses

**In the group** (visible — these name a specific person's work issue):
```
/rejection                 (reply to the dispatcher's message)
/rejection Asilbek         (or name them directly)

/charge 30 missed call with broker      (reply to their message)
/charge Asilbek 30 missed call with broker
```

**DM the bot privately** (never posted anywhere dispatchers can see):
```
/expense 300 fuel reimbursement
/bonus Doniyor 100 great month
```

Change the rules any time:
```
/setrejectionfee 50       (dollar amount per rejection)
/setdispatchfee 3         (company's % of gross)
/setcommission 1          (dispatcher's % of their own gross)
/financesettings          (view current numbers)
```

## Monthly dispatch board report

1. In Google Sheets, open your Dispatch board tab → **File → Download → Comma-separated values (.csv)** (or keep it as .xlsx if you prefer — both work).
2. **DM the file to the bot** (not the group). In the caption, write the month you want, e.g. `September 2026` or `2026-09`. If you leave the caption blank, it uses the most recent month found in the file.
3. The bot replies privately with two files:
   - **Main Gross** — per-MC-company breakdown (loads, miles, RPM, gross), average weekly gross, active days, total payout, company income (your %), expenses, and the final remainder.
   - **Dispatchers Gross** — per dispatcher: gross (with a breakdown by company further down), commission, bonuses, charges (itemized below), and their net dispatch fee (commission + bonuses − charges, including attendance fines).

Only you receive these (not the 4 bosses) — forward them yourself if you want to share.

## Important limitations to know about

- **The sheet export should be just the Dispatch board tab.** Other tabs (Dispatch fee, per-MC tabs, Payments) aren't read — you said income only needs that one tab, and expenses are logged directly in the bot instead.
- **Any Rate $ counts toward gross and commission, regardless of status** — including the CANCELED-but-still-charged rows, per what you confirmed.
- **Day-off safe:** a dispatcher's attendance fines only count real problems — late arrival, early leaving, or forgetting one side of a check-in/out pair. A day with zero activity at all is treated as a day off, never fined. (This is different from the standalone `/monthly` attendance report, which still fines every blank day — that one's for attendance review, not payroll, so it stays stricter.)
- **Net payout can go negative** if someone's charges exceed their commission — the report shows the real negative number rather than flooring it at zero. That's your call to make each month, not the bot's.
- **Pickup dates without a year** (the sheet only shows "Aug 1, 22:45 EDT") mean month-matching relies on the divider rows in your sheet ("August", "September", etc.) rather than the dates themselves — so those divider rows must stay in the export.
- Rows where the parser can't find a pickup date are simply excluded from the "Active days" count rather than guessed at.

## Full command list (new, finance-related)

| Command | Who | Where |
|---|---|---|
| `/rejection` (reply or name) | Admin | Group |
| `/charge <amount> <reason>` (reply) / `/charge <name> <amount> <reason>` | Admin | Group |
| `/expense <amount> <description>` | Admin | **DM only** |
| `/bonus <name> <amount> <note>` | Admin | **DM only** |
| `/setrejectionfee <amount>` | Admin | Either |
| `/setdispatchfee <percent>` | Admin | Either |
| `/setcommission <percent>` | Admin | Either |
| `/financesettings` | Admin/Viewer | Either |
| `/addviewer` (reply) / `/addviewer <user_id>` | Admin | Either |
| Send the dispatch board file | Admin | **DM only** |
