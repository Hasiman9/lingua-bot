"""Explicit, persistent campaigns. No Telegram calls; times are UTC Unix seconds."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path

REQUIREMENTS_VERSION = 'five-channel-v1'

MESSAGE = (
    "🎁✨Спасибо за доверие, вот ваши материалы-подарочки:\n\n"
    "https://drive.google.com/drive/folders/1lvzrprek8YgQt42iv7BWS9eSveJUFm-W?usp=sharing"
)
ELIGIBLE = """
    SELECT d.user_id FROM deliveries d
    WHERE d.first_verified_at IS NOT NULL AND d.status != 'delivered'
      AND d.delivered_at IS NULL
      AND d.status NOT IN ('processing', 'sending', 'uncertain')
      AND NOT EXISTS (SELECT 1 FROM campaign_recipients r WHERE r.user_id=d.user_id
                      AND (r.status IN ('delivered', 'sending', 'uncertain') OR r.delivered_at IS NOT NULL))
"""


class CampaignStore:
    def __init__(self, path):
        self.path = path

    def connect(self, readonly=False):
        uri = Path(self.path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw')
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self):
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, activated_at REAL NOT NULL,
                    scheduled_at REAL NOT NULL, status TEXT NOT NULL,
                    completed_at REAL, message TEXT NOT NULL, requirements_version TEXT
                );
                CREATE TABLE IF NOT EXISTS campaign_recipients (
                    campaign_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    status TEXT NOT NULL, next_attempt_at REAL NOT NULL,
                    retry_attempts INTEGER NOT NULL DEFAULT 0,
                    second_verified_at REAL, delivered_at REAL,
                    PRIMARY KEY(campaign_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS delivery_control (
                    id INTEGER PRIMARY KEY CHECK (id=1), not_before REAL NOT NULL
                );
                INSERT OR IGNORE INTO delivery_control VALUES (1, 0);
            """)
            columns = {r['name'] for r in db.execute('PRAGMA table_info(campaigns)')}
            if 'requirements_version' not in columns:
                db.execute('ALTER TABLE campaigns ADD COLUMN requirements_version TEXT')
            # Legacy campaigns have no policy version: retain history but require explicit
            # replacement before the new scheduler can send under the five-channel policy.
            # Only the bot calls this, while holding the existing process lock.
            db.execute("UPDATE campaign_recipients SET status='pending' WHERE status='processing'")
            db.execute("UPDATE campaign_recipients SET status='uncertain' WHERE status='sending'")

    def activate(self, campaign_id, at=None, clock=time.time, replace_active=False):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM campaigns WHERE id=?', (campaign_id,)).fetchone():
                raise ValueError('Campaign ID already used; its schedule will not be reset')
            if db.execute("SELECT 1 FROM campaigns WHERE status='active'").fetchone():
                if not replace_active:
                    raise ValueError('An active campaign exists; use --replace-active to explicitly replace it')
                if db.execute("""SELECT 1 FROM campaign_recipients r JOIN campaigns c ON c.id=r.campaign_id
                              WHERE c.status='active' AND r.status IN ('processing','sending')""").fetchone():
                    raise ValueError('A delivery is in flight; wait for it to finish before replacing the campaign')
                db.execute("UPDATE campaigns SET status='superseded' WHERE status='active'")
            now = clock()
            due = now if at is None else at
            if at is not None and due <= now:
                raise ValueError('Delivery time must be in the future')
            db.execute("""INSERT INTO campaigns
                       (id,activated_at,scheduled_at,status,completed_at,message,requirements_version)
                       VALUES (?, ?, ?, 'active', NULL, ?, ?)""",
                       (campaign_id, now, due, MESSAGE, REQUIREMENTS_VERSION))
            db.execute("INSERT INTO campaign_recipients (campaign_id,user_id,status,next_attempt_at) "
                       "SELECT ?, user_id, 'pending', ? FROM (" + ELIGIBLE + ')', (campaign_id, due))
            return due

    def preview(self):
        with closing(self.connect(readonly=True)) as db:
            db.execute('BEGIN')
            delivered = "SELECT user_id FROM deliveries WHERE status='delivered' OR delivered_at IS NOT NULL"
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='campaigns'").fetchone():
                count = db.execute("SELECT count(*) FROM deliveries WHERE first_verified_at IS NOT NULL "
                                   "AND status NOT IN ('delivered','processing','sending','uncertain') "
                                   "AND delivered_at IS NULL").fetchone()[0]
                return dict(eligible=count, previously_delivered=db.execute(
                    'SELECT count(*) FROM (' + delivered + ')').fetchone()[0],
                    uncertain_users=0, campaigns=[], deliveries=[])
            campaigns = [dict(r) for r in db.execute('SELECT * FROM campaigns ORDER BY activated_at')]
            for item in campaigns:
                item['requires_explicit_replacement'] = (
                    item['status'] == 'active' and item.get('requirements_version') != REQUIREMENTS_VERSION)
            return dict(
                eligible=db.execute('SELECT count(*) FROM (' + ELIGIBLE + ')').fetchone()[0],
                previously_delivered=db.execute('SELECT count(*) FROM (' + delivered +
                    " UNION SELECT user_id FROM campaign_recipients WHERE status='delivered' OR delivered_at IS NOT NULL)").fetchone()[0],
                uncertain_users=db.execute("SELECT count(DISTINCT user_id) FROM campaign_recipients WHERE status IN ('uncertain','sending')").fetchone()[0],
                campaigns=campaigns,
                deliveries=[dict(r) for r in db.execute(
                    'SELECT campaign_id,status,count(*) AS count FROM campaign_recipients GROUP BY campaign_id,status')],
            )

    def claim(self, now):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""UPDATE campaigns SET status='completed', completed_at=?
                WHERE status='active' AND requirements_version=? AND scheduled_at<=? AND NOT EXISTS (
                    SELECT 1 FROM campaign_recipients r WHERE r.campaign_id=campaigns.id
                    AND r.status IN ('pending','processing','sending'))""", (now, REQUIREMENTS_VERSION, now))
            if db.execute('SELECT not_before FROM delivery_control WHERE id=1').fetchone()[0] > now:
                return None
            row = db.execute("""SELECT r.*, c.message FROM campaign_recipients r
                JOIN campaigns c ON c.id=r.campaign_id
                WHERE c.status='active' AND c.requirements_version=? AND c.scheduled_at<=? AND r.status='pending'
                AND r.next_attempt_at<=? ORDER BY r.next_attempt_at,r.user_id LIMIT 1""",
                             (REQUIREMENTS_VERSION, now, now)).fetchone()
            if row:
                db.execute("UPDATE campaign_recipients SET status='processing' WHERE campaign_id=? AND user_id=?",
                           (row['campaign_id'], row['user_id']))
                return dict(row)

    def defer(self, until):
        """Persist Telegram's global delivery cooldown across restarts and campaigns."""
        with closing(self.connect()) as db, db:
            db.execute('UPDATE delivery_control SET not_before=max(not_before, ?) WHERE id=1', (until,))

    def update(self, row, **values):
        allowed = {'status', 'next_attempt_at', 'retry_attempts', 'second_verified_at', 'delivered_at'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid campaign update')
        with closing(self.connect()) as db, db:
            db.execute('UPDATE campaign_recipients SET ' + ','.join(k + '=?' for k in values)
                       + ' WHERE campaign_id=? AND user_id=?',
                       (*values.values(), row['campaign_id'], row['user_id']))
