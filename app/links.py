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


def booking_link(base_url: str, route: Route, flight_date_iso: str, pax: int) -> str:
    """Construct a deep-link to the redirect service. The redirect service is
    expected to translate this GET into a POST to ticket.vanillasky.ge with
    the form pre-filled."""
    return (
        f"{base_url}/go"
        f"?from={route.from_id}&to={route.to_id}"
        f"&date={flight_date_iso}&pax={pax}"
    )
