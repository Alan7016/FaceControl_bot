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

## 4. Daily use
- The bot automatically posts fresh Check In/Out buttons (and pins them,
  unpinning the previous day's) at the start of each shift.
- Staff just tap the button that applies — the bot figures out which shift
  they mean and whether they're on time.
- `/report` — today's log, right in the chat.
- `/export` or `/export 7` — download a CSV of the last 30 (or N) days.

## 5. Deploy so it runs 24/7
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
