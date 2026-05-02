from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
-- Old tables from schedule-only era. Drop on first run after the migration.
DROP TABLE IF EXISTS seen_dates;
DROP TABLE IF EXISTS events;

-- Schedule snapshots: when did we first/last see a flight day in the API.
CREATE TABLE IF NOT EXISTS schedule_dates (
    route_key   TEXT NOT NULL,
    flight_date TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (route_key, flight_date)
);

-- Real "is the ticket buyable for N passengers" state per (route, date).
CREATE TABLE IF NOT EXISTS bookable_state (
    route_key       TEXT NOT NULL,
    flight_date     TEXT NOT NULL,
    bookable        INTEGER NOT NULL,
    price           TEXT,
    flight_time     TEXT,
    passenger_count INTEGER NOT NULL,
    last_check      INTEGER NOT NULL,
    PRIMARY KEY (route_key, flight_date, passenger_count)
);

-- Transition history (released / sold_out). Drives the "actually a release"
-- pattern analysis later on.
CREATE TABLE IF NOT EXISTS bookable_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    route_key       TEXT NOT NULL,
    flight_date     TEXT NOT NULL,
    transition      TEXT NOT NULL,
    price           TEXT,
    flight_time     TEXT,
    passenger_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bookable_events_ts ON bookable_events(ts);

-- Tiny key/value store for runtime flags like 'polling_paused'.
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class BookableState:
    bookable: bool
    price: str | None
    flight_time: str | None
    last_check: int


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def record_schedule(self, route_key: str, dates: list[str]) -> None:
        if not dates:
            return
        now = int(time.time())
        with self.conn:
            for d in dates:
                self.conn.execute(
                    """INSERT INTO schedule_dates(route_key, flight_date, first_seen, last_seen)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(route_key, flight_date) DO UPDATE SET last_seen = excluded.last_seen""",
                    (route_key, d, now, now),
                )

    def get_bookable_state(
        self, route_key: str, flight_date: str, passenger_count: int
    ) -> BookableState | None:
        cur = self.conn.execute(
            """SELECT bookable, price, flight_time, last_check
               FROM bookable_state
               WHERE route_key = ? AND flight_date = ? AND passenger_count = ?""",
            (route_key, flight_date, passenger_count),
        )
        row = cur.fetchone()
        if not row:
            return None
        return BookableState(
            bookable=bool(row[0]),
            price=row[1],
            flight_time=row[2],
            last_check=row[3],
        )

    def get_flag(self, key: str, default: bool = False) -> bool:
        cur = self.conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return default
        return row[0] == "1"

    def set_flag(self, key: str, value: bool) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO app_state(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, "1" if value else "0"),
            )

    def update_bookable_state(
        self,
        route_key: str,
        flight_date: str,
        passenger_count: int,
        bookable: bool,
        price: str | None,
        flight_time: str | None,
        transition: str | None,
    ) -> None:
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                """INSERT INTO bookable_state(route_key, flight_date, bookable,
                       price, flight_time, passenger_count, last_check)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(route_key, flight_date, passenger_count) DO UPDATE SET
                       bookable    = excluded.bookable,
                       price       = excluded.price,
                       flight_time = excluded.flight_time,
                       last_check  = excluded.last_check""",
                (route_key, flight_date, int(bookable), price, flight_time, passenger_count, now),
            )
            if transition:
                self.conn.execute(
                    """INSERT INTO bookable_events(ts, route_key, flight_date, transition,
                           price, flight_time, passenger_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (now, route_key, flight_date, transition, price, flight_time, passenger_count),
                )
