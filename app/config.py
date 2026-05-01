from __future__ import annotations

import os
from dataclasses import dataclass
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
class Settings:
    poll_interval_seconds: int
    min_days_ahead: int
    lookahead_days: int
    passenger_count: int
    routes: tuple[Route, ...]
    bot_token: str
    chat_id: str


def load(config_path: str | Path = "config.yml") -> Settings:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    routes = []
    for r in raw.get("routes", []):
        for side in (r["from"], r["to"]):
            if side not in CITY_IDS:
                raise ValueError(f"Unknown city: {side}. Known: {sorted(CITY_IDS)}")
        routes.append(Route(from_name=r["from"], to_name=r["to"]))

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars are required")

    return Settings(
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 300)),
        min_days_ahead=int(raw.get("min_days_ahead", 10)),
        lookahead_days=int(raw.get("lookahead_days", 45)),
        passenger_count=int(raw.get("passenger_count", 2)),
        routes=tuple(routes),
        bot_token=bot_token,
        chat_id=chat_id,
    )
