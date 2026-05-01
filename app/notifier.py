from __future__ import annotations

import logging
from datetime import date

import httpx

from .config import Route

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"
BOOK_URL = "https://ticket.vanillasky.ge/en/tickets"


def _format_date(iso: str) -> str:
    """ISO 'YYYY-MM-DD' → display 'DD-Month-YYYY' (e.g. '12-May-2026')."""
    try:
        return date.fromisoformat(iso).strftime("%d-%B-%Y")
    except ValueError:
        return iso


def _format_message(route: Route, new_dates: list[str], passenger_count: int) -> str:
    """The Drupal booking form ignores query params, so we don't deep-link
    individual dates. We give the user the dates to copy into the form."""
    lines = [
        f"✈️ *{route.from_name} → {route.to_name}* — new dates available!",
        "",
        f"For {passenger_count} passenger{'s' if passenger_count != 1 else ''}:",
    ]
    for d in new_dates:
        lines.append(f"• `{_format_date(d)}`")
    lines.append("")
    lines.append(f"👉 [Open booking page]({BOOK_URL})")
    lines.append(f"Pick *{route.from_name} → {route.to_name}* and one of the dates above.")
    return "\n".join(lines)


async def send_alert(
    client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    route: Route,
    new_dates: list[str],
    passenger_count: int,
) -> None:
    text = _format_message(route, new_dates, passenger_count)
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
