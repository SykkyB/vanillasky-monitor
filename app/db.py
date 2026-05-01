from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_dates (
    route_key   TEXT NOT NULL,
    flight_date TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (route_key, flight_date)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    route_key  TEXT NOT NULL,
    new_dates  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def known_dates(self, route_key: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT flight_date FROM seen_dates WHERE route_key = ?", (route_key,)
        )
        return {row[0] for row in cur.fetchall()}

    def record_dates(self, route_key: str, dates: list[str]) -> list[str]:
        """Insert/update seen dates. Return the list of dates that are new."""
        if not dates:
            return []
        now = int(time.time())
        known = self.known_dates(route_key)
        new = [d for d in dates if d not in known]

        with self.conn:
            for d in dates:
                self.conn.execute(
                    """INSERT INTO seen_dates(route_key, flight_date, first_seen, last_seen)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(route_key, flight_date) DO UPDATE SET last_seen = excluded.last_seen""",
                    (route_key, d, now, now),
                )
            if new:
                self.conn.execute(
                    "INSERT INTO events(ts, route_key, new_dates) VALUES (?, ?, ?)",
                    (now, route_key, json.dumps(new)),
                )
        return new
