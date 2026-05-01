from __future__ import annotations

import logging

import httpx

from .config import Route

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"
BOOK_URL = "https://ticket.vanillasky.ge/en/tickets"


def _booking_link(route: Route, flight_date: str) -> str:
    return (
        f"{BOOK_URL}?departure={route.from_id}&arrive={route.to_id}"
        f"&date_picker={flight_date}"
    )


def _format_message(route: Route, new_dates: list[str]) -> str:
    lines = [f"✈️ *{route.from_name} → {route.to_name}* — new dates available!"]
    for d in new_dates:
        lines.append(f"• [{d}]({_booking_link(route, d)})")
    lines.append(f"\n[Open booking page]({BOOK_URL})")
    return "\n".join(lines)


async def send_alert(
    client: httpx.AsyncClient, bot_token: str, chat_id: str, route: Route, new_dates: list[str]
) -> None:
    text = _format_message(route, new_dates)
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
            log.info("[%s] alerted %d new dates", route.key, len(new_dates))
    except httpx.HTTPError as e:
        log.error("Telegram request failed: %s", e)
