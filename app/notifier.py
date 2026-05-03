from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx

from .config import Route
from .links import booking_link, is_tunnel_alive

_PRICE_NUM_RE = re.compile(r"(\d+)\s*GEL", re.IGNORECASE)


def format_price(price_str: str | None, pax: int) -> str | None:
    """'90 GEL' + pax → display string.
       pax==1 → '90 GEL (total)' (per-pax equals total)
       pax>1  → '90 GEL × 2 = 180 GEL'"""
    if not price_str:
        return None
    m = _PRICE_NUM_RE.search(price_str)
    if not m:
        return price_str
    per = int(m.group(1))
    if pax == 1:
        return f"{per} GEL (total)"
    return f"{per} GEL × {pax} = {per * pax} GEL"

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
    route: Route,
    flights: list[ReleasedFlight],
    passenger_count: int,
    redirect_base: str | None,
) -> str:
    pax_word = "passenger" if passenger_count == 1 else "passengers"
    lines = [
        f"✈️ *{route.from_name} → {route.to_name}* — TICKETS RELEASED!",
        "",
        f"For *{passenger_count}* {pax_word}:",
    ]
    for f in flights:
        date_label = f"`{_format_date(f.flight_date)}`"
        if redirect_base:
            link = booking_link(redirect_base, route, f.flight_date, passenger_count)
            date_label = f"[{_format_date(f.flight_date)}]({link})"
        bits = [date_label]
        if f.flight_time:
            bits.append(f.flight_time)
        priced = format_price(f.price, passenger_count)
        if priced:
            bits.append(priced)
        lines.append("• " + " — ".join(bits))
    lines.append("")
    if redirect_base:
        lines.append("Click a date above to open the booking page pre-filled.")
    else:
        lines.append(f"👉 [Open booking page]({BOOK_URL})")
        lines.append(
            f"Pick *{route.from_name} → {route.to_name}* and one of the dates above."
        )
    return "\n".join(lines)


async def send_alert(
    client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    route: Route,
    flights: list[ReleasedFlight],
    passenger_count: int,
    redirect_url_base: str,
    tunnel_enabled: bool,
) -> None:
    redirect_base: str | None = None
    if tunnel_enabled and redirect_url_base:
        if await is_tunnel_alive(client, redirect_url_base):
            redirect_base = redirect_url_base
        else:
            log.info("Tunnel enabled but unreachable, falling back to plain links")

    text = _format_message(route, flights, passenger_count, redirect_base)
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
