from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .config import Route

log = logging.getLogger(__name__)

API_BASE = "https://ticket.vanillasky.ge"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _normalize_date(item: Any) -> str | None:
    """Vanilla Sky's API shape isn't documented; coerce a few likely formats to YYYY-MM-DD."""
    if item is None:
        return None
    if isinstance(item, (int, float)):
        ts = int(item)
        if ts > 10_000_000_000:  # ms
            ts //= 1000
        return datetime.utcfromtimestamp(ts).date().isoformat()
    if isinstance(item, str):
        s = item.strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        if s.isdigit():
            return _normalize_date(int(s))
        log.warning("Unrecognised date string from API: %r", s)
        return None
    if isinstance(item, dict):
        for key in ("date", "day", "flight_date", "value"):
            if key in item:
                return _normalize_date(item[key])
        log.warning("Unrecognised date object from API: %r", item)
        return None
    log.warning("Unrecognised date payload type: %r", item)
    return None


def _filter_window(dates: list[str], min_days_ahead: int, lookahead_days: int) -> list[str]:
    today = date.today()
    lo = today + timedelta(days=min_days_ahead)
    hi = today + timedelta(days=lookahead_days)
    out = []
    for d in dates:
        try:
            dt = date.fromisoformat(d)
        except ValueError:
            continue
        if lo <= dt <= hi:
            out.append(d)
    return sorted(set(out))


async def fetch_route(client: httpx.AsyncClient, route: Route) -> list[str]:
    """Hit /custom/check-flight/{from}/{to} and return outbound flight dates (YYYY-MM-DD)."""
    url = f"{API_BASE}/custom/check-flight/{route.from_id}/{route.to_id}"
    try:
        resp = await client.get(url, timeout=20.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("[%s] fetch failed: %s", route.key, e)
        return []

    try:
        data = resp.json()
    except ValueError:
        log.warning("[%s] non-JSON response: %s", route.key, resp.text[:200])
        return []

    raw_from = data.get("from", []) if isinstance(data, dict) else []
    if raw_from:
        log.info("[%s] raw 'from' payload: %r", route.key, raw_from)

    dates: list[str] = []
    for item in raw_from:
        nd = _normalize_date(item)
        if nd:
            dates.append(nd)
    return sorted(set(dates))


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"{API_BASE}/en/tickets",
        }
    )
