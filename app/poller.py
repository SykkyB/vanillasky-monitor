from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import httpx

from .config import Route

log = logging.getLogger(__name__)

API_BASE = "https://ticket.vanillasky.ge"
TICKETS_URL = f"{API_BASE}/en/tickets"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_FORM_BUILD_ID_RE = re.compile(r'name="form_build_id" value="([^"]+)"')
_PRICE_RE = re.compile(r"(\d+)\s*GEL", re.IGNORECASE)
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_NO_TICKETS_MARKER = "There are no available tickets"
_BOOKABLE_MARKER = "Choose Flight"


@dataclass(frozen=True)
class BookableResult:
    bookable: bool
    price: str | None = None
    flight_time: str | None = None


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


async def fetch_destinations(client: httpx.AsyncClient, origin: str) -> list[str]:
    """GET /custom/check-dest/{from_id} — list of destination city names that
    Vanilla Sky has configured as routes from this origin."""
    from .config import CITY_IDS, CITY_NAMES

    if origin not in CITY_IDS:
        return []
    url = f"{API_BASE}/custom/check-dest/{CITY_IDS[origin]}"
    try:
        resp = await client.get(url, timeout=20.0)
        resp.raise_for_status()
        dest_ids = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("[%s] check-dest failed: %s", origin, e)
        return []
    return [CITY_NAMES[d] for d in dest_ids if d in CITY_NAMES]


async def fetch_route_graph(
    client: httpx.AsyncClient, origins: tuple[str, ...]
) -> list[Route]:
    """For each origin call check-dest, return deduped list of Route pairs."""
    routes: list[Route] = []
    seen: set[tuple[str, str]] = set()
    for origin in origins:
        dests = await fetch_destinations(client, origin)
        for dest in dests:
            key = (origin, dest)
            if key not in seen:
                seen.add(key)
                routes.append(Route(from_name=origin, to_name=dest))
    return routes


async def fetch_form_build_id(client: httpx.AsyncClient) -> str:
    resp = await client.get(TICKETS_URL, timeout=20.0)
    resp.raise_for_status()
    m = _FORM_BUILD_ID_RE.search(resp.text)
    if not m:
        raise RuntimeError("form_build_id not found on /en/tickets")
    return m.group(1)


async def fetch_schedule(client: httpx.AsyncClient, route: Route) -> list[str]:
    """GET /custom/check-flight/{from}/{to} — returns flight schedule (days the
    plane flies). Does NOT mean tickets are on sale; only POST tells you that."""
    url = f"{API_BASE}/custom/check-flight/{route.from_id}/{route.to_id}"
    try:
        resp = await client.get(url, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("[%s] schedule fetch failed: %s", route.key, e)
        return []

    raw = data.get("from", []) if isinstance(data, dict) else []
    dates: list[str] = []
    for item in raw:
        if isinstance(item, str):
            try:
                datetime.strptime(item, "%Y-%m-%d")
                dates.append(item)
            except ValueError:
                log.warning("[%s] non-ISO date in schedule: %r", route.key, item)
    return sorted(set(dates))


async def check_bookable(
    client: httpx.AsyncClient,
    form_build_id: str,
    route: Route,
    flight_date_iso: str,
    passenger_count: int,
) -> BookableResult:
    """POST the booking form to find out whether tickets are actually on sale
    for this date and number of passengers. Parses Vanilla Sky's HTML response."""
    display_date = date.fromisoformat(flight_date_iso).strftime("%d %b %Y")
    payload = [
        ("departure", str(route.from_id)),
        ("arrive", str(route.to_id)),
        ("types", "0"),  # one-way
        ("date_picker", display_date),
        ("date_picker_arrive", ""),
        ("person_count", str(passenger_count)),
        ("person_types[adult]", str(passenger_count)),
        ("person_types[child]", "0"),
        ("person_types[infant]", "0"),
        ("op", ""),
        ("form_build_id", form_build_id),
        ("form_id", "form_select_date"),
    ]
    body = urlencode(payload).encode("utf-8")
    try:
        resp = await client.post(
            TICKETS_URL,
            content=body,
            timeout=30.0,
            follow_redirects=True,
            headers={
                "Referer": TICKETS_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("[%s] %s POST failed: %s", route.key, flight_date_iso, e)
        return BookableResult(bookable=False)

    html = resp.text
    if _NO_TICKETS_MARKER in html:
        return BookableResult(bookable=False)

    if _BOOKABLE_MARKER not in html:
        # Unknown page — log a fingerprint and treat as not bookable so we don't
        # spam alerts on weird pages (e.g. site maintenance).
        log.warning(
            "[%s] %s: unexpected response (size=%d, has 'flight'=%s)",
            route.key,
            flight_date_iso,
            len(html),
            "flight" in html.lower(),
        )
        return BookableResult(bookable=False)

    # We're on the "Choose Flight" results page. Extract first time + price.
    # Parse only the flight-items block, not the whole page (avoids false
    # matches in headers/footers).
    flight_block = html
    block_match = re.search(
        r'<div class="flight-items-bl"[^>]*>(.*?)<div class="flight-page-content"|'
        r'<div class="flight-items-bl"[^>]*>(.*?)</form>',
        html,
        re.S,
    )
    if block_match:
        flight_block = block_match.group(1) or block_match.group(2) or html

    time_match = _TIME_RE.search(flight_block)
    price_match = _PRICE_RE.search(flight_block)

    flight_time = (
        f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
    )
    price = f"{price_match.group(1)} GEL" if price_match else None
    return BookableResult(bookable=True, price=price, flight_time=flight_time)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
