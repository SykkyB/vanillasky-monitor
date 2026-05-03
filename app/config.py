from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import yaml

CITY_IDS: dict[str, int] = {
    "Tbilisi": 1,
    "Ambrolauri": 2,
    "Batumi": 4,
    "Kutaisi": 5,
    "Mestia": 6,
    "Natakhtari": 7,
}

CITY_NAMES: dict[int, str] = {v: k for k, v in CITY_IDS.items()}


@dataclass(frozen=True)
class Route:
    from_name: str
    to_name: str

    @property
    def from_id(self) -> int:
        return CITY_IDS[self.from_name]

    @property
    def to_id(self) -> int:
        return CITY_IDS[self.to_name]

    @property
    def key(self) -> str:
        return f"{self.from_name}->{self.to_name}"


@dataclass(frozen=True)
class QuietHours:
    from_time: time
    to_time: time

    def covers(self, t: time) -> bool:
        if self.from_time == self.to_time:
            return False
        if self.from_time < self.to_time:
            return self.from_time <= t < self.to_time
        # Crosses midnight: from=23:00, to=09:00 means [23:00, 24:00) ∪ [00:00, 09:00).
        return t >= self.from_time or t < self.to_time

    def display(self) -> str:
        return f"{self.from_time.strftime('%H:%M')}–{self.to_time.strftime('%H:%M')}"


@dataclass(frozen=True)
class Settings:
    poll_interval_seconds: int
    min_days_ahead: int
    lookahead_days: int
    passenger_count: int
    monitor_origins: tuple[str, ...]
    extra_routes: tuple[Route, ...]
    quiet_hours: QuietHours | None
    redirect_url_base: str
    bot_token: str
    chat_id: str


def load(config_path: str | Path = "config.yml") -> Settings:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    extra_routes = []
    for r in raw.get("extra_routes") or []:
        for side in (r["from"], r["to"]):
            if side not in CITY_IDS:
                raise ValueError(f"Unknown city: {side}. Known: {sorted(CITY_IDS)}")
        extra_routes.append(Route(from_name=r["from"], to_name=r["to"]))

    monitor_origins = tuple(raw.get("monitor_origins") or [])
    for o in monitor_origins:
        if o not in CITY_IDS:
            raise ValueError(f"Unknown origin: {o}. Known: {sorted(CITY_IDS)}")

    if not monitor_origins and not extra_routes:
        raise ValueError("Config must define monitor_origins or extra_routes (or both)")

    quiet_hours: QuietHours | None = None
    qh_raw = raw.get("quiet_hours")
    if qh_raw and qh_raw.get("from") and qh_raw.get("to"):
        try:
            from_t = datetime.strptime(qh_raw["from"], "%H:%M").time()
            to_t = datetime.strptime(qh_raw["to"], "%H:%M").time()
            quiet_hours = QuietHours(from_time=from_t, to_time=to_t)
        except ValueError as e:
            raise ValueError(f"Invalid quiet_hours format (need HH:MM): {e}") from e

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars are required")

    return Settings(
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 300)),
        min_days_ahead=int(raw.get("min_days_ahead", 10)),
        lookahead_days=int(raw.get("lookahead_days", 45)),
        passenger_count=int(raw.get("passenger_count", 2)),
        monitor_origins=monitor_origins,
        extra_routes=tuple(extra_routes),
        quiet_hours=quiet_hours,
        redirect_url_base=str(raw.get("redirect_url_base") or "").rstrip("/"),
        bot_token=bot_token,
        chat_id=chat_id,
    )
