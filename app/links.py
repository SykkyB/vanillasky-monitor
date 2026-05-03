from __future__ import annotations

import logging

import httpx

from .config import Route

log = logging.getLogger(__name__)


async def is_tunnel_alive(client: httpx.AsyncClient, base_url: str) -> bool:
    """Ping <base>/health. Anything but 200 → considered down."""
    if not base_url:
        return False
    try:
        resp = await client.get(f"{base_url}/health", timeout=3.0)
    except httpx.HTTPError as e:
        log.debug("Tunnel health probe failed: %s", e)
        return False
    return resp.status_code == 200


def booking_link(
    base_url: str,
    route: Route,
    flight_date_iso: str,
    pax: int,
    back_date_iso: str | None = None,
) -> str:
    """Construct a deep-link to the redirect service. If back_date_iso is
    provided, the redirect triggers Vanilla Sky's native round-trip mode
    with both legs pre-filled."""
    url = (
        f"{base_url}/go"
        f"?from={route.from_id}&to={route.to_id}"
        f"&date={flight_date_iso}&pax={pax}"
    )
    if back_date_iso:
        url += f"&back_date={back_date_iso}"
    return url
