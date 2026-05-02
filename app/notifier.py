from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import httpx

from .config import Route

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"
BOOK_URL = "https://ticket.vanillasky.ge/en/tickets"


@dataclass(frozen=True)
class ReleasedFlight:
    flight_date: str  # ISO YYYY-MM-DD
    flight_time: str | None
    price: str | None


def _format_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d-%B-%Y")
    except ValueError:
        return iso


def _format_message(
    route: Route, flights: list[ReleasedFlight], passenger_count: int
) -> str:
    pax_word = "passenger" if passenger_count == 1 else "passengers"
    lines = [
        f"✈️ *{route.from_name} → {route.to_name}* — TICKETS RELEASED!",
        "",
        f"For *{passenger_count}* {pax_word}:",
    ]
    for f in flights:
        bits = [f"`{_format_date(f.flight_date)}`"]
        if f.flight_time:
            bits.append(f.flight_time)
        if f.price:
            bits.append(f.price)
        lines.append("• " + " — ".join(bits))
    lines.append("")
    lines.append(f"👉 [Open booking page]({BOOK_URL})")
    lines.append(f"Pick *{route.from_name} → {route.to_name}* and one of the dates above.")
    return "\n".join(lines)


async def send_alert(
    client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    route: Route,
    flights: list[ReleasedFlight],
    passenger_count: int,
) -> None:
    text = _format_message(route, flights, passenger_count)
    url = f"{TG_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = await client.post(url, json=payload, timeout=20.0)
        if resp.status_code != 200:
            log.error("Telegram error %s: %s", resp.status_code, resp.text[:300])
        else:
            log.info("[%s] alerted %d new released flights", route.key, len(flights))
    except httpx.HTTPError as e:
        log.error("Telegram request failed: %s", e)
