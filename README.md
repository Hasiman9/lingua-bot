# Lingua educational materials bot

An asynchronous `python-telegram-bot` polling bot with one command, `/start`.
Users subscribe to six channels and press one confirmation button. All six memberships
must pass both initial verification and automatic verification before delivery.
No anonymous submissions, forwarding, admin replies, EMC links, or message-response handlers remain.

## Configure before running

1. Put the **existing** `@linguamaterials_bot` token in the local `.env` file as `BOT_TOKEN`.
   The old source contained an empty token, so no token could be migrated.
   On a fresh checkout, copy `.env.example` to `.env`. Never commit `.env`.
2. Configure the fixed campaign date and Krasnoyarsk local time in `.env`:
   ```dotenv
   CAMPAIGN_DATE=2026-10-22
   CAMPAIGN_TIME=16:00
   CAMPAIGN_TIMEZONE=Asia/Krasnoyarsk
   ```
   These are the defaults. Change `CAMPAIGN_TIME` to the desired 24-hour `HH:MM` time.
   October 22, 2026 at **16:00 Asia/Krasnoyarsk (UTC+7)** is **09:00 UTC** or
   **11:00 Europe/Berlin (CEST)** on the same day. All eligible users share this fixed timestamp.
   Python's `zoneinfo` resolves the IANA timezone independently of the server timezone.
   Invalid dates, timezones, and ambiguous/nonexistent DST wall times are rejected.
   The old per-user delay setting is no longer used.
3. Set `DATABASE_PATH=data/bot.sqlite3`, or an absolute persistent path.
   Relative paths resolve against the project directory.
4. Edit `CHANNELS` in `config.py`: Channel 1 is `@linguamaterials`; replace Channels 2–6.
   Each needs a unique `chat_id`, display `name`, and `https://t.me/…` join URL.
   Public IDs use `@username`; private IDs must be negative **integers** (normally `-100…`),
   paired with valid invitation links. Placeholders deliberately prevent startup.
5. Add the bot as an **administrator in every required channel**. The bot checks its
   own admin status at verification time, because Telegram only guarantees checks of
   other users when the bot is an administrator. For private channels, add the bot directly
   and obtain the real numeric chat ID. Pending join requests do not count as membership.
6. In the project-root **`.env`**, set **`MATERIALS_URL`** to your real collection link
   (for example, your actual shared folder or materials page). It is blank by default:
   ```dotenv
   MATERIALS_URL=
   ```
   Blank means no materials message is sent; eligible deliveries remain pending. Invalid URLs
   and known placeholders such as `example.com` are rejected. Check that your real HTTP(S) link
   works for recipients; the bot does not inspect its contents or sharing permissions.
7. In **`config.py`**, edit **`MATERIALS_MESSAGE`** to change the single message's plain text.
   The message automatically includes one inline button labeled **🎁 Get Materials**, pointing
   to `MATERIALS_URL`. No HTML formatting is needed. No PDF, video, or individual file is sent.
   Restart the bot after changing the URL or message. Completed users are not sent another message.

Welcome text, channel names, and confirmation/retry labels are editable in `config.py`.

### Configuration flow and subscription-only testing

`config.load_settings()` loads the project-root `.env` **before** reading environment values.
Existing process environment variables take precedence over `.env` (including an explicitly
empty `MATERIALS_URL`). It validates and returns a single `Settings` object containing `token`,
`campaign_at`, `database_path`, `materials_url`, and `materials_message`.
`materials_message` comes from the editable `MATERIALS_MESSAGE` constant in `config.py`;
it is not an environment variable and does not embed a URL at import time. Startup passes the
loaded URL and message explicitly to the delivery service. The sender attaches the URL button.

For local subscription testing, leave `MATERIALS_URL=` blank. `/start` and both subscription
confirmation/retry buttons remain usable; no materials message is sent even if the campaign
is overdue. The message text may also be empty in this mode. To enable delivery later, configure
a real URL and nonempty message, then restart. Already eligible, overdue users can then receive
the message after a successful fresh six-channel check. Invalid/placeholder URLs fail validation.

The automated startup smoke tests load a temporary `.env`, construct the real PTB application,
exercise startup/shutdown hooks and SQLite, and mock all Telegram calls. They never start polling:

```powershell
.venv\Scripts\python -m unittest discover -s tests -p test_startup.py -v
```

## Local installation / VS Code

Python 3.10+ is required (3.12+ recommended for a new server). Open this folder in VS Code.
In its terminal:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python main.py
```

Use **Python: Select Interpreter** to select `.venv`, then **Run Python File** on `main.py`.
On Linux/macOS use `.venv/bin/python` instead. Stop with Ctrl+C. Tests use mocked Telegram
responses and temporary databases; no token or live channels are required.

## Disable Botmother before polling

Stop/disable the existing bot's flows or integration in Botmother and disconnect its Telegram
connection using its current dashboard controls. Do not delete the Telegram bot or create a new one.
Stop every older Python instance or hosting deployment using the same token as well.
Starting `run_polling()` removes the Telegram webhook; Botmother must remain disabled so it
does not reinstall that webhook. This bot drops queued old updates on polling startup.
If a polling conflict occurs, stop the competing instance/integration before restarting.
Clear any old language-specific or chat-scoped command menus through BotFather if present;
startup replaces the default menu with `/start`.

## Persistent scheduling and verification policy

SQLite stores user ID, first verification, due time, verification state, latest second verification,
delivery state/time, attempts, next retry time, and acknowledgment progress (1 for the sent link).
Timestamps are UTC Unix seconds. Async SQLite operations run through `aiosqlite` and serialized
writes. A process-level file lock prevents two instances sharing this database; an async service
lock prevents simultaneous confirmation/delivery. Run **one process on one server** with local
storage, not multiple replicas or a network-mounted SQLite database.

The scheduler scans persistent due work every 30 seconds, including overdue work after startup.
Successful initial verification records eligibility (`initial_passed`, `pending`) and the same
fixed campaign timestamp for every user. Nothing is sent before this timestamp. Delivery begins
on the first scan at/after it and proceeds sequentially in batches; API limits, user count, and
outages can delay completion, so simultaneous receipt at the exact second is not guaranteed.
Interrupted `processing` rows recover as `pending`. Duplicate confirmation clicks never move
an existing pending due time or create a second delivery. Completed users receive no repeat campaign.
All six subscriptions are rechecked immediately before **the materials message**, including retries.
`member`, `administrator`, `creator`/`owner`, and `restricted` with `is_member=True` pass.
Missing membership pauses delivery and sends missing-channel links plus one retry button.

Rejoining users who confirm again retain the fixed campaign date; no new delay begins.
Late initial confirmations are also accepted and processed on the next scan, with another
automatic verification of all six channels. First verification and acknowledged delivery status
are preserved. There is no registration cutoff or automatic second campaign.

On startup, unfinished legacy delay-based schedules migrate to the configured campaign timestamp.
Changing the campaign configuration and restarting similarly reschedules unfinished users
without unblocking missing subscriptions.
Completed deliveries are never rescheduled. Restarts with the same timestamp preserve retry backoff.
Back up the existing SQLite database before upgrading; no manual schema reset is needed.
Existing unfinished multi-file deliveries now receive the one link message after verification,
regardless of their old file progress. Old completed deliveries remain completed. The legacy
`manifest` column is retained for database compatibility but is no longer used for delivery.

API failures and missing admin rights never count as successful verification or genuine
unsubscription. Delivery stays pending with exponential backoff from 60 seconds to a maximum
of one hour, continuing until recovery; Telegram's explicit longer RetryAfter is respected for
send failures. Logs identify channel names and error types, not token-bearing request URLs.
Users who block the bot are marked `blocked`; after unblocking they can use `/start` and confirm
again. A blank `MATERIALS_URL` also waits and retries without sending any materials message.

After Telegram acknowledges the single message, progress, delivery status, and delivery time
are committed together in SQLite. Subsequent scans, confirmation clicks, restarts, or configuration
changes do not resend completed deliveries. Failed sends stay pending (or blocked if the user
blocked the bot), and any retry must pass all six membership checks again.

Telegram does not offer an idempotency key for sends: a crash after Telegram accepts a message but
before SQLite commits, or an ambiguous network timeout, can duplicate that message. Checkpointing
reduces duplicates but cannot guarantee exactly-once sending. Likewise, membership checks are
snapshots; Telegram cannot atomically combine six checks with a send. Every send here requires a
fresh successful check of all six channels, and any known missing subscription or API error stops it.

## PythonAnywhere Always-on task

Use a PythonAnywhere account with Always-on tasks and outbound Telegram API access enabled.
In a Bash console, with this project uploaded to `/home/YOUR_USERNAME/lingua-bot`:

```bash
cd /home/YOUR_USERNAME/lingua-bot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Configure `.env` as above; set `DATABASE_PATH` to
`/home/YOUR_USERNAME/lingua-bot/data/bot.sqlite3`. Keep this persistent database directory
through updates. In the Tasks page, add **one Always-on task** with this command:

```bash
/home/YOUR_USERNAME/lingua-bot/.venv/bin/python -u /home/YOUR_USERNAME/lingua-bot/main.py
```

The bot loads `.env` relative to its own source directory, so it does not depend on the task's
working directory. Use the task's log link for output and its restart controls after configuration
changes. Always-on task supervision handles process restarts; SQLite recovers overdue work.
Keep only this polling instance active and disable Botmother first. No web app, webhook,
systemd service, cron delivery job, or server timezone change is needed on PythonAnywhere.
Always-on availability depends on the hosting plan; the bot itself needs no external scheduler.

## Linux VPS / systemd

Install Python, venv support, and Git using your Linux distribution's package manager. For Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-venv git
sudo useradd --system --create-home --home-dir /opt/lingua-bot linguabot
```

Copy this project to `/opt/lingua-bot`, create its private `.env`, and configure channels/materials.
Then:

```bash
sudo chown -R linguabot:linguabot /opt/lingua-bot
sudo chmod 600 /opt/lingua-bot/.env
sudo -u linguabot python3 -m venv /opt/lingua-bot/.venv
sudo -u linguabot /opt/lingua-bot/.venv/bin/python -m pip install -r /opt/lingua-bot/requirements.txt
sudo cp /opt/lingua-bot/deploy/lingua-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lingua-bot
sudo systemctl status lingua-bot
sudo journalctl -u lingua-bot -f
```

The supplied service restarts automatically after failures and starts at boot. After configuration
or code changes: `sudo systemctl restart lingua-bot`. To stop: `sudo systemctl stop lingua-bot`.
Keep `.env` and the database outside disposable build directories and preserve them
across deployments. The service user needs write permission on the database directory (including
SQLite WAL/SHM and lock files). For backups, stop the service, copy the database and associated
WAL/SHM files if present, then restart; alternatively use SQLite's online backup API. Never delete
the database on redeploy: doing so loses schedules and deduplication history. No paid services,
public HTTP endpoint, or bot-building platform are required.
