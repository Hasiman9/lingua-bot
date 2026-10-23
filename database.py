"""One async SQLite connection, serialized writes, UTC Unix timestamps."""
import asyncio

import aiosqlite


class Database:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self, campaign_at):
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS deliveries (
                user_id INTEGER PRIMARY KEY,
                first_verified_at REAL NOT NULL,
                scheduled_at REAL NOT NULL,
                verification_state TEXT NOT NULL,
                second_verified_at REAL,
                status TEXT NOT NULL,
                delivered_at REAL,
                retry_attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                manifest TEXT
            );
            CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_attempt_at);
        """)
        # Legacy records are history/eligibility, never an automatically runnable queue.
        await self.connection.commit()

    async def close(self):
        await self.connection.close()

    async def get(self, user_id):
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM deliveries WHERE user_id=?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def schedule(self, user_id, now, campaign_at):
        async with self.lock:
            await self.connection.execute("""
                INSERT INTO deliveries
                (user_id, first_verified_at, scheduled_at, verification_state, status, next_attempt_at)
                VALUES (?, ?, ?, 'initial_passed', 'pending', ?)
                ON CONFLICT(user_id) DO NOTHING
            """, (user_id, now, campaign_at, campaign_at))
            await self.connection.commit()
        return await self.get(user_id)

    async def update(self, user_id, **values):
        allowed = {"verification_state", "second_verified_at", "status", "delivered_at",
                   "retry_attempts", "next_attempt_at", "progress", "manifest"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid delivery update")
        async with self.lock:
            await self.connection.execute(
                "UPDATE deliveries SET " + ", ".join(f"{key}=?" for key in values) + " WHERE user_id=?",
                (*values.values(), user_id),
            )
            await self.connection.commit()

    async def claim_due(self, now):
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM deliveries WHERE status='pending' AND next_attempt_at<=? AND scheduled_at<=? "
                "ORDER BY next_attempt_at LIMIT 1", (now, now)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            await self.connection.execute(
                "UPDATE deliveries SET status='processing' WHERE user_id=?", (row["user_id"],)
            )
            await self.connection.commit()
            return dict(row)
