"""Task 4: transactional, persistent sliding-window reservations."""
import sqlite3
import time
import uuid
from pathlib import Path


class RateLimitExceeded(Exception):
    pass


class TokenLimiter:
    def __init__(self, path: str, limit=50_000, window=60.0, clock=time.time):
        self.path, self.limit, self.window, self.clock = str(path), limit, window, clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, ts REAL NOT NULL, tokens INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_tenant_ts ON usage(tenant,ts)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def reserve(self, tenant: str, tokens: int) -> str:
        if tokens <= 0:
            raise ValueError("Reservation must be positive")
        now, reservation = self.clock(), uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM usage WHERE ts <= ?", (now - self.window,))
            used = db.execute("SELECT COALESCE(SUM(tokens),0) FROM usage WHERE tenant=?", (tenant,)).fetchone()[0]
            if used + tokens > self.limit:
                raise RateLimitExceeded()
            db.execute("INSERT INTO usage VALUES (?,?,?,?)", (reservation, tenant, now, tokens))
        return reservation

    def settle(self, reservation: str, actual: int):
        if actual < 0:
            raise ValueError("Negative usage")
        with self.connect() as db:
            db.execute("UPDATE usage SET tokens=? WHERE id=?", (actual, reservation))
