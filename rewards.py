"""A single explicitly activated 72-hour reward window; no broadcast or Telegram calls."""
import time
from contextlib import closing

from campaigns import CampaignStore, MESSAGE, has_materials_receipt

DURATION_SECONDS = 72 * 60 * 60


class RewardStore(CampaignStore):
    def activate(self, clock=time.time):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM reward_window').fetchone():
                raise ValueError('Reward window already activated; its deadline cannot be reset')
            now = clock()
            db.execute('INSERT INTO reward_window VALUES (1, ?, ?, ?)', (now, now + DURATION_SECONDS, MESSAGE))
            return now + DURATION_SECONDS

    def register_verified(self, user_id, now, campaign_at):
        # Registration and enrollment commit together: crashes cannot lose a new user's reward.
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            inserted = db.execute("""INSERT INTO deliveries
                (user_id,first_verified_at,scheduled_at,verification_state,status,next_attempt_at)
                VALUES (?, ?, ?, 'initial_passed', 'pending', ?) ON CONFLICT(user_id) DO NOTHING""",
                                  (user_id, now, campaign_at, campaign_at)).rowcount
            window = db.execute('SELECT * FROM reward_window WHERE id=1').fetchone()
            if (not inserted or not window or not window['activated_at'] <= now < window['expires_at']
                    or has_materials_receipt(db, user_id)):
                return False
            db.execute("INSERT INTO reward_deliveries (user_id,enrolled_at,status,next_attempt_at) VALUES (?,?,'pending',?)",
                       (user_id, now, now))
            return True

    def preview(self, clock=time.time):
        with closing(self.connect(readonly=True)) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reward_window'").fetchone():
                return dict(status='inactive', activated_at=None, expires_at=None, deliveries=[])
            row = db.execute('SELECT * FROM reward_window WHERE id=1').fetchone()
            if row is None:
                return dict(status='inactive', activated_at=None, expires_at=None, deliveries=[])
            now = clock()
            return dict(status='active' if row['activated_at'] <= now < row['expires_at'] else 'expired',
                        activated_at=row['activated_at'], expires_at=row['expires_at'],
                        seconds_remaining=max(0, row['expires_at'] - now),
                        deliveries=[dict(r) for r in db.execute(
                            'SELECT status,count(*) AS count FROM reward_deliveries GROUP BY status')])

    def claim(self, now, user_id=None):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            window = db.execute('SELECT * FROM reward_window WHERE id=1').fetchone()
            if not window:
                return None
            if now >= window['expires_at']:
                db.execute("UPDATE reward_deliveries SET status='expired' WHERE status='pending'")
                return None
            if now < window['activated_at'] or db.execute('SELECT not_before FROM delivery_control WHERE id=1').fetchone()[0] > now:
                return None
            row = db.execute("SELECT * FROM reward_deliveries WHERE status='pending' AND next_attempt_at<=? "
                             "AND (? IS NULL OR user_id=?) ORDER BY next_attempt_at,user_id LIMIT 1",
                             (now, user_id, user_id)).fetchone()
            if row:
                db.execute("UPDATE reward_deliveries SET status='processing' WHERE user_id=?", (row['user_id'],))
                return dict(row) | dict(reward=True, message=window['message'], expires_at=window['expires_at'])

    def begin_send(self, row, now=None, clock=time.time):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            # Read the clock after acquiring SQLite's lock, not before a potential wait.
            now = clock() if now is None else now
            current = db.execute('SELECT status FROM reward_deliveries WHERE user_id=?', (row['user_id'],)).fetchone()
            if not current or current['status'] != 'processing':
                return False
            window = db.execute('SELECT * FROM reward_window WHERE id=1').fetchone()
            if not window or not window['activated_at'] <= now < window['expires_at']:
                status = 'expired'
            elif has_materials_receipt(db, row['user_id']):
                status = 'duplicate_skipped'
            else:
                status = 'sending'
            db.execute('UPDATE reward_deliveries SET status=?,second_verified_at=? WHERE user_id=?',
                       (status, now, row['user_id']))
            return status == 'sending'

    def update(self, row, **values):
        allowed = {'status', 'next_attempt_at', 'retry_attempts', 'second_verified_at', 'delivered_at'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid reward update')
        with closing(self.connect()) as db, db:
            db.execute('UPDATE reward_deliveries SET ' + ','.join(k + '=?' for k in values) + ' WHERE user_id=?',
                       (*values.values(), row['user_id']))
