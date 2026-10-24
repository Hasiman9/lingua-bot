# Lingua materials bot

The existing polling bot verifies all five channels on confirmation. Materials are now
sent only by an **explicitly activated, one-time SQLite campaign**. Deploying, editing
configuration, starting the bot, and previewing do not activate delivery.

## One-time delivery policy

- `campaign.py activate --immediate --replace-active --confirm` stores the activation time
  as the **immediate due time**, using UTC Unix seconds in the existing SQLite database.
  No five-minute delay or old October 22 date controls this delivery.
- The required channels are `@linguamaterials`, `@nymomenglish`, `@sofikenglish`,
  `@miss_di_english`, and `@shek_english`. The removed channel has no button or membership check.
- Activation atomically snapshots previously verified users from `deliveries`.
  Users who first verify after activation are not added to that campaign.
- Existing subscribers, subscription records, legacy dates and delivery history stay intact.
  New `campaigns` and `campaign_recipients` tables hold campaign state and history.
- Legacy completed deliveries and any user with a new delivered receipt are excluded.
  Legacy `processing` records are conservatively excluded because their send outcome is unknown.
- Immediately before every send (including safe retries), all five current channels are checked again.
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
- Recipients are paced at no more than one per second; checks occur after the pacing wait.
  Telegram `RetryAfter` responses pause campaign delivery globally, including other recipients.
  The cooldown and per-user retry state persist across restarts. Every safe retry rechecks membership.
- Restarts retain the due time and retry backoff. A newly explicitly activated campaign resumes
  after downtime, even if its deadline passed. Pre-upgrade campaigns remain held for explicit
  replacement: they have no `five-channel-v1` policy marker and cannot send on deployment.
- Campaigns finish once there are no pending/in-flight recipients. Missing subscriptions,
  blocked users and uncertain sends are terminal for that campaign. Verification outages and
  explicit rate limits keep it active until safe retries finish.
- Reusing a campaign ID fails without changing anything. `--replace-active` explicitly marks
  any unfinished campaign as `superseded`, preserving all its recipient records and timestamps,
  and snapshots currently eligible users into a fresh campaign. An in-flight delivery blocks
  replacement; wait until it finishes. Completed campaigns and their timestamps are never changed.
- Existing verified users (including the previously reported 59) need not press `ГОТОВО` again.
  Actual recipient counts come from the production database, excluding delivered/uncertain users.
  Campaigns store recipient snapshots, not old channel lists: the final check uses current config.

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
.venv/bin/python diagnose_channels.py
```

Review the changed files and test output. Preview can read the legacy database without migrating
it. Old active campaigns display `requires_explicit_replacement: true` and remain held after
startup. `eligible` means recorded initial verification, **not** live membership verification.
`previously_delivered` counts distinct users across legacy records and campaign receipts;
`uncertain_users` identifies held campaign outcomes. Recipient counts by campaign/status preserve
the old history, so superseded pending rows are historical and are not runnable deliveries.
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
.venv/bin/python diagnose_channels.py
```

Require diagnostic output `administrator_ok=5 failed=0` before activation. Use the exact bot
token/environment used by the existing task. Startup adds compatible schema fields but does not
activate or authorize old campaigns. Preview opens SQLite read-only;
it reports UTC scheduled times, eligible counts, and counts by recipient status including pending.
Neither operator command connects to Telegram. Activation can run while the bot is running;
SQLite transactions serialize activation and scheduler work.

### 4. Explicitly activate only when ready

```bash
cd ~/lingua-bot
.venv/bin/python campaign.py activate --immediate --replace-active --confirm
.venv/bin/python campaign.py preview
```

**Delivery becomes due at successful activation**, without any five-minute wait.
The default campaign ID is `materials-immediate-v1`. Repeating the command fails safely and
does not reset or reopen completed deliveries. No restart is required after activation.
The next scheduler scan (normally within 30 seconds), pacing and final checks determine actual
receipt times. `--replace-active` is safe when no unfinished campaign exists and allows a held
old overdue campaign to be explicitly replaced without losing its history.

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
- `CHANNELS` in `config.py` must contain exactly five distinct channels. The bot needs admin
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

## Diagnose subscription verification on PythonAnywhere

After manually deploying these files, run from the existing project/virtualenv:

```bash
cd ~/lingua-bot
.venv/bin/python diagnose_channels.py
```

This command loads `BOT_TOKEN` from the project's `.env` (process environment takes precedence),
authenticates using `getMe`, and calls `getChatMember` for the bot in each of the five configured
channels. It does not open SQLite, start polling, change webhooks, activate campaigns, or send
messages. It can run alongside the existing Always-on task; no startup-script change is needed.
The authenticated bot ID/username lets you confirm that you are checking the intended bot.
It checks bot access/admin rights, not any particular user's subscriptions.

Each channel prints `channel_id`, `role=bot`, returned `status` (or `unavailable`), `category`,
and a safe reason. Failures also include the Telegram exception class. The command continues
through all five channels even if one fails. Exit codes: **0** = all five admin checks passed,
**1** = one or more channel failures, **2** = configuration/authentication/setup failure.

| Category | Meaning / action |
| --- | --- |
| `ok` | The bot is an administrator (in verification logs, the user is subscribed). |
| `insufficient_admin` | Add/promote this exact bot as administrator in the channel; `status=member`, `left`, or `kicked` is not sufficient. Telegram can also explicitly reject membership-list access. |
| `channel_id_or_access` | Telegram could not resolve/access the channel. Check its current public username or use the actual negative numeric channel ID for a private channel. `chat not found` alone cannot prove which cause applies. |
| `access_denied` | Check whether the bot was removed/banned or cannot access the channel. |
| `timeout` / `network_error` | Check PythonAnywhere outbound Telegram access/network/proxy settings; rerun after connectivity recovers. |
| `rate_limited` | Telegram asked for a retry later; wait before rerunning. |
| `authentication_error` | Check that the environment uses the correct, valid bot token. Do not share the token. |
| `invalid_user` | The user ID could not be resolved by Telegram. |
| `api_bad_request` / `api_error` | Telegram rejected the request for another reason; retain the channel ID and exception class for investigation. |
| `missing_subscription` | User lookup succeeded but returned no active subscription; this is not an API failure. |

Once the updated bot is manually restarted, pressing `ГОТОВО` adds detailed lines to its
Always-on log. Search for `get_chat_member channel_id=` or `verification summary`.
Verification normally makes **10 requests**: one bot check and one user check for each of five
channels. `role=bot` versus `role=subscriber` identifies which request failed. If the bot access
request fails, the user lookup for that channel is explicitly logged as skipped. A non-admin
bot never establishes successful verification, even if a user lookup returns `member`.
All five channels must still pass. A rate-limit response withholds remaining verification requests
and delivery until retry; it never counts as successful verification. Missing subscriptions and API/permission failures remain
separate, and API failures do not create eligible subscriber records.

Logs contain channel IDs, statuses, elapsed milliseconds, exception types and allowlisted
error descriptions. Arbitrary raw exception text, request URLs, tokens and invite links are
not logged by these checks. Unknown API error text is intentionally omitted to avoid leaking secrets.
Do not infer a particular failing channel from the old generic user-facing error alone.

## Local tests

Python 3.10+ is required. Existing dependencies remain unchanged:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, controlled clocks and mocked Telegram calls. They cover
immediate activation, explicit replacement of old overdue campaigns, persistence/recovery,
preservation of 59 simulated legacy users, snapshot eligibility, five-channel
rechecks, exact message content, preserved legacy records, safe retries, uncertain sends,
duplicate prevention, CLI previews/activation, future dates, and startup/logging behavior.
