# Lingua materials bot

The existing polling bot verifies all six channels on confirmation. Materials are now
sent only by an **explicitly activated, one-time SQLite campaign**. Deploying, editing
configuration, starting the bot, and previewing do not activate delivery.

## One-time delivery policy

- `campaign.py activate --confirm` stores the activation time and activation + **300 seconds**
  as UTC Unix timestamps in the existing SQLite database.
- Activation atomically snapshots previously verified users from `deliveries`.
  Users who first verify after activation are not added to that campaign.
- Existing subscribers, subscription records, legacy dates and delivery history stay intact.
  New `campaigns` and `campaign_recipients` tables hold campaign state and history.
- Legacy completed deliveries and any user with a new delivered receipt are excluded.
  Legacy `processing` records are conservatively excluded because their send outcome is unknown.
- Immediately before every send (including safe retries), all six channels are checked again.
  The bot must be an administrator in each channel. Missing membership skips that user for
  this campaign; confirming again does not reopen it. Verification API errors retry with backoff.
- The exact text below is persisted with the campaign and sent in **one plain-text message**,
  with no inline button, formatting or link preview:

```text
🎁✨Спасибо за доверие, вот ваши материалы-подарочки:

https://drive.google.com/drive/folders/1lvzrprek8YgQt42iv7BWS9eSveJUFm-W?usp=sharing
```

- Scheduler scans occur every 30 seconds, in batches of 25. Delivery starts on the first
  scan at/after the stored time; checks, recipient count and Telegram limits can delay receipt.
- Restarts retain the countdown and retry backoff. An explicitly activated campaign resumes
  after downtime, even if its deadline passed. Old campaigns are never automatically activated.
- Campaigns finish once there are no pending/in-flight recipients. Missing subscriptions,
  blocked users and uncertain sends are terminal for that campaign. Verification outages and
  explicit rate limits keep it active until safe retries finish.
- Reusing a campaign ID fails without changing anything. A second campaign cannot be activated
  while one is active. Completed campaigns and their timestamps are never changed.

### Duplicate prevention

The sender commits `sending` **before** calling Telegram. A timeout or unexpected send error
becomes `uncertain`; interrupted `sending` records become `uncertain` on restart. These users
are excluded from future campaigns too. They are not retried automatically, because Telegram
has no send idempotency key. This favors avoiding duplicates: a crash before the request may
leave someone unsent and held for review. An explicit Telegram rate-limit rejection can retry;
each retry rechecks membership. Interrupted pre-send verification can safely resume.

Preview reports `uncertain` counts. For investigation, inspect `campaign_recipients` by
`campaign_id` and `user_id`; do not reset these records blindly.

## Deploy safely on PythonAnywhere through GitHub

Use the existing project directory, virtualenv, `.env`, database and Always-on task.
The commands below assume the project is `~/lingua-bot` and its environment is `.venv`.
If the existing task uses another virtualenv, use its Python executable for these commands too.
If `start-bot.sh` exports `DATABASE_PATH`, use the **same exported value** in the console.
Verify the database path printed by preview matches the bot startup log.

### 1. Review the published commit

Use the existing checkout of `https://github.com/Hasiman9/lingua-bot` on branch `main`.
The update contains no `.env`, database, virtualenv or `start-bot.sh`.
Do not replace the project with a fresh checkout or reset local server changes.

### 2. Stop the old task, back up, and install

In PythonAnywhere's Tasks page, **stop the existing Always-on task before replacing code**.
The old code can still send overdue deliveries until stopped. Do not start a second polling process.
In a PythonAnywhere Bash console:

```bash
cd ~/lingua-bot
.venv/bin/python - <<'PY'
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path.cwd() / '.env')
path = Path(os.getenv('DATABASE_PATH', 'data/bot.sqlite3')).resolve()
target = path.with_name(path.name + '.backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
backup = sqlite3.connect(target)
source.backup(backup)
backup.close()
source.close()
print('Backed up', path, 'to', target)
PY
git status --short --branch
git pull --ff-only origin main
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python campaign.py preview
```

Review the changed files and test output. Before first startup the preview can read the legacy
database without migrating it; it should show no activated campaigns and zero pending campaign
deliveries. `eligible` means recorded initial verification, **not** live membership verification.
If Git reports local changes or refuses the fast-forward, resolve that separately before
continuing; do not use `git reset --hard` or `git clean` on the production checkout.

### 3. Start the existing Always-on task and preview

Start the same task again from the Tasks page with its **existing command**, including
`start-bot.sh` if used. `main.py` remains the entry point; no cron or new task is needed.
The wrapper is not present in this repository and is not replaced by this deployment.
Wait for the log line `Telegram Application started successfully; long polling is running`.
Then run:

```bash
cd ~/lingua-bot
.venv/bin/python campaign.py preview
```

Startup adds campaign tables but does not schedule anything. Preview opens SQLite read-only;
it reports UTC scheduled times, eligible counts, and counts by recipient status including pending.
Neither operator command connects to Telegram. Activation can run while the bot is running;
SQLite transactions serialize activation and scheduler work.

### 4. Explicitly activate only when ready

```bash
cd ~/lingua-bot
.venv/bin/python campaign.py activate --confirm
.venv/bin/python campaign.py preview
```

**The five-minute countdown begins at successful activation**, not deployment or restart.
The default campaign ID is `materials-five-minute-v1`. Repeating the command fails safely and
does not reset the countdown or reopen completed deliveries. No restart is required after activation.

### Future delivery date

After the current campaign is completed, choose a new ID and a future UTC date:

```bash
.venv/bin/python campaign.py activate --id materials-december-2026 --at 2026-12-01T09:00:00Z --confirm
.venv/bin/python campaign.py preview
```

Replace the example with your desired **future** date. Naive, non-UTC and past timestamps are
rejected. This is another one-time campaign of the same materials; users already delivered or
held as uncertain are excluded. New materials with a separate receipt policy would require a
separate change; changing the campaign ID alone does not resend these materials.

## Configuration and operation

- Keep the existing `BOT_TOKEN` and `DATABASE_PATH` in `.env`. Relative database paths resolve
  against the project directory. Existing process environment takes precedence over `.env`.
- `CHANNELS` in `config.py` must contain exactly six distinct channels. The bot needs admin
  rights in each. `member`, `administrator`, `creator`/`owner`, and restricted users with
  `is_member=True` pass; pending join requests do not.
- The legacy `CAMPAIGN_DATE`, `CAMPAIGN_TIME`, `CAMPAIGN_TIMEZONE`, `MATERIALS_URL` and
  `MATERIALS_MESSAGE` remain load-compatible but do not schedule or control this explicit
  campaign. The fixed message is in `campaigns.py`. A blank legacy URL does not disable an
  explicitly activated campaign. The legacy configuration must still pass its existing validation.
- Keep only one polling process, using the existing process lock. Disable Botmother or any
  competing webhook/poller. No web app, webhook, timezone change or external scheduler is needed.
- Startup logs show configuration/database paths and lifecycle stages with token redaction.
  The scheduler-registration log alone does not mean polling has started.
- Keep `.env` and the persistent database through updates. Do not delete the database or
  copy a local test database over production. Use SQLite's backup API for consistent backups.

## Local tests

Python 3.10+ is required. Existing dependencies remain unchanged:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, controlled clocks and mocked Telegram calls. They cover
activation, inactive overdue campaigns, persistence/recovery, snapshot eligibility, six-channel
rechecks, exact message content, preserved legacy records, safe retries, uncertain sends,
duplicate prevention, CLI previews/activation, future dates, and startup/logging behavior.
