from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .bot import run_bot
from .config import Route, Settings, load
from .db import DB
from .notifier import ReleasedFlight, send_alert
from .poller import (
    _filter_window,
    check_bookable,
    fetch_form_build_id,
    fetch_route_graph,
    fetch_schedule,
    make_client,
)

DATA_DIR = Path("/app/data")
DB_PATH = DATA_DIR / "state.db"
HEARTBEAT = DATA_DIR / "heartbeat"

POST_RATE_LIMIT_SEC = 0.5  # be polite to vanillasky.ge


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _process_route(
    settings: Settings,
    db: DB,
    client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    form_build_id: str,
    route: Route,
) -> None:
    log = logging.getLogger("cycle")

    schedule = await fetch_schedule(client, route)
    db.record_schedule(route.key, schedule)

    in_window = _filter_window(schedule, settings.min_days_ahead, settings.lookahead_days)
    if not in_window:
        log.info("[%s] schedule empty in window", route.key)
        return

    newly_released: list[ReleasedFlight] = []
    bookable_now = 0

    for flight_date in in_window:
        result = await check_bookable(
            client, form_build_id, route, flight_date, settings.passenger_count
        )

        prev = db.get_bookable_state(route.key, flight_date, settings.passenger_count)
        was_bookable = prev.bookable if prev else False
        is_bookable = result.bookable

        transition: str | None = None
        if is_bookable and not was_bookable:
            transition = "released"
            newly_released.append(
                ReleasedFlight(
                    flight_date=flight_date,
                    flight_time=result.flight_time,
                    price=result.price,
                )
            )
            log.info(
                "[%s] %s RELEASED: %s %s",
                route.key,
                flight_date,
                result.flight_time or "?",
                result.price or "?",
            )
        elif was_bookable and not is_bookable:
            transition = "sold_out"
            log.info("[%s] %s sold out", route.key, flight_date)

        if is_bookable:
            bookable_now += 1

        db.update_bookable_state(
            route.key,
            flight_date,
            settings.passenger_count,
            is_bookable,
            result.price,
            result.flight_time,
            transition,
        )

        await asyncio.sleep(POST_RATE_LIMIT_SEC)

    log.info(
        "[%s] checked=%d bookable=%d newly_released=%d",
        route.key,
        len(in_window),
        bookable_now,
        len(newly_released),
    )

    if newly_released:
        await send_alert(
            tg_client,
            settings.bot_token,
            settings.chat_id,
            route,
            newly_released,
            settings.passenger_count,
        )


async def _build_routes_for_cycle(
    settings: Settings, client: httpx.AsyncClient
) -> list[Route]:
    """Combine monitor_origins (auto-expanded via check-dest graph) with the
    explicit extra_routes from config. Deduped, stable order."""
    routes: list[Route] = []
    seen: set[tuple[str, str]] = set()

    if settings.monitor_origins:
        for r in await fetch_route_graph(client, settings.monitor_origins):
            key = (r.from_name, r.to_name)
            if key not in seen:
                seen.add(key)
                routes.append(r)

    for r in settings.extra_routes:
        key = (r.from_name, r.to_name)
        if key not in seen:
            seen.add(key)
            routes.append(r)

    return routes


async def run_one_cycle(
    settings: Settings, db: DB, client: httpx.AsyncClient, tg_client: httpx.AsyncClient
) -> None:
    log = logging.getLogger("cycle")

    try:
        form_build_id = await fetch_form_build_id(client)
    except Exception as e:
        log.error("Couldn't get form_build_id, skipping cycle: %s", e)
        return

    routes = await _build_routes_for_cycle(settings, client)
    if not routes:
        log.warning("No routes resolved for this cycle (graph empty?)")
        return
    log.info("Cycle starting: %d routes to check", len(routes))

    for route in routes:
        try:
            await _process_route(settings, db, client, tg_client, form_build_id, route)
        except Exception:
            log.exception("[%s] route processing failed", route.key)


def _local_now(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()


def _should_skip_cycle(settings: Settings, db: DB) -> str | None:
    """Return a reason string to skip, or None to proceed."""
    if db.get_flag("polling_paused"):
        return "polling is paused (use /resume to start)"
    if settings.quiet_hours:
        tz_name = os.environ.get("TZ", "UTC")
        now_t = _local_now(tz_name).time()
        if settings.quiet_hours.covers(now_t):
            return f"quiet hours {settings.quiet_hours.display()} ({tz_name})"
    return None


async def polling_loop(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    log = logging.getLogger("polling")
    log.info("Polling loop started")
    while not stop.is_set():
        skip_reason = _should_skip_cycle(settings, db)
        if skip_reason:
            log.info("Skipping cycle: %s", skip_reason)
        else:
            try:
                await run_one_cycle(settings, db, vs_client, tg_client)
            except Exception:
                log.exception("Cycle failed")

        # Heartbeat ALWAYS — healthcheck shouldn't fail during pause/quiet.
        HEARTBEAT.touch()

        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
    log.info("Polling loop stopped")


async def main() -> None:
    _setup_logging()
    log = logging.getLogger("main")

    settings = load("/app/config.yml")
    log.info(
        "Started. origins=%s, extra_routes=%d, interval=%ss, "
        "window=[+%dd .. +%dd], passengers=%d",
        list(settings.monitor_origins),
        len(settings.extra_routes),
        settings.poll_interval_seconds,
        settings.min_days_ahead,
        settings.lookahead_days,
        settings.passenger_count,
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = DB(DB_PATH)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with make_client() as vs_client, httpx.AsyncClient() as tg_client:
        await asyncio.gather(
            polling_loop(settings, db, vs_client, tg_client, stop),
            run_bot(settings, db, vs_client, tg_client, stop),
        )

    log.info("Shutting down")


if __name__ == "__main__":
    asyncio.run(main())
