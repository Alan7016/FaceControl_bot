# Dispatcher Check-In/Out Bot

Telegram bot that posts Check In / Check Out buttons in your staff group,
matches each tap to the closest shift, flags late arrivals or early
departures, and logs everything to a local database.

## 1. Create the bot
1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts.
2. Copy the token it gives you (looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxx`).
3. Add the bot to your staff group, and **make it an admin** of the group
   (it needs admin rights to pin messages).

## 2. Run it locally first (to test)
```bash
pip install -r requirements.txt
export BOT_TOKEN="paste-your-token-here"
python bot.py
```

Then in the group:
- Send `/myid` to get your Telegram user ID and the group's chat ID.
- Send `/addadmin` (with no arguments, while the admin list is still empty)
  to make yourself the first admin — after that, only admins can add others
  with `/addadmin <user_id>`.
- Send `/setgroup` (as admin) so the bot knows which chat to auto-post the
  attendance buttons in every day.
- Send `/post` any time to manually post the Check In/Out buttons right now.

## 3. Configure shifts
Current defaults: morning 07:00–15:00, main 15:00–23:00, night 23:00–07:00,
10-minute grace period, Asia/Tashkent timezone.

Change them any time from the group, as admin:
```
/setshift morning 08:00 16:00
/setgrace 15
/shifts        (view current config)
```
No restart needed — changes save straight to `config.json` and apply immediately.

## 4. Assign workers to shifts
This is what makes lateness/fines accurate — without it, the bot just
guesses the closest shift, which is fine for casual use but not for payroll.

Reply to a message the worker has sent in the group with:
```
/setworker main
```
(or `/setworker <user_id> main` if you already know their Telegram ID from `/myid`).

- `/workers` — list everyone currently assigned and their shift.
- `/removeworker` (as a reply) or `/removeworker <user_id>` — unassign someone.

Reassigning is just running `/setworker` again with the new shift name — no
need to remove first.

## 5. Daily use
- The bot automatically posts fresh Check In/Out buttons (and pins them,
  unpinning the previous day's) at the start of each shift.
- Staff tap the button that applies. If they're assigned to a shift, the bot
  checks them against THEIR schedule specifically.
- Check-ins more than `early_checkin_minutes` (default 60) before a shift's
  start are rejected with a message telling them the earliest allowed time —
  e.g. a 7am shift can't be checked into before 6am.
- `/report` — today's log, right in the chat.
- `/export` or `/export 7` — download a CSV of the last 30 (or N) days (raw log, no fines).

## 6. Weekly & monthly reports
- `/weekly` — CSV covering the current calendar week (Monday–Sunday):
  late minutes and early-checkout minutes per assigned worker, per day.
  **No dollar amounts** — this is for keeping an eye on patterns.
- `/monthly` or `/monthly 2026-08` — CSV covering a calendar month, this time
  **with fines calculated**, plus a summary of each worker's total for the
  month right in the caption. Fine rules:
  - Missing check-in and/or check-out for a shift → flat **$25** (charged
    once even if both are missing that day)
  - Late check-in: under 25 min → $0, 25–44 min → **$15**, 45+ min → **$25**
    plus **$10** for every additional full hour beyond 45 min
    (e.g. 1h45m late = $25 + $10 = $35)
  - Early check-out → reported, never fined

Both reports only cover workers who've been assigned via `/setworker` — there's
no way to judge lateness or flag a missing punch without knowing someone's
schedule.

**Known limitation:** the bot assumes every assigned worker works their shift
every day of the period. If someone has a day off, that day will show as a
"missing punch" fine unless you manually edit it out of the CSV before acting
on it. Let me know if you want a day-off/schedule-exception feature added.

## 7. Deploy so it runs 24/7
Easiest option: **Railway.app** (free tier is plenty for this).
1. Push this folder to a GitHub repo.
2. On railway.app → New Project → Deploy from GitHub repo → pick the repo.
3. In the project's Variables tab, add `BOT_TOKEN` = your token.
4. Railway auto-detects `requirements.txt` and runs `python bot.py`. Done —
   it'll stay online and restart itself if it ever crashes.

(Render.com's free "Background Worker" service works the same way, if you'd
rather use that instead.)

## Notes
- `checkins.db` (SQLite) holds all history — back it up occasionally if you
  want to keep records long-term; it isn't wiped on redeploy on Railway but
  is worth exporting via `/export` periodically as a safety net.
- Because polling is used (not webhooks), there's no public URL or SSL
  certificate to manage — it just needs to keep running.
