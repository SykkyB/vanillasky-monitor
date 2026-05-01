from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import httpx

from .config import Settings, load
from .db import DB
from .notifier import send_alert
from .poller import fetch_route, make_client

DATA_DIR = Path("/app/data")
DB_PATH = DATA_DIR / "state.db"
HEARTBEAT = DATA_DIR / "heartbeat"


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run_one_cycle(
    settings: Settings, db: DB, client: httpx.AsyncClient, tg_client: httpx.AsyncClient
) -> None:
    log = logging.getLogger("cycle")
    results = await asyncio.gather(
        *(fetch_route(client, r) for r in settings.routes),
        return_exceptions=False,
    )

    for route, dates in zip(settings.routes, results):
        from .poller import _filter_window

        in_window = _filter_window(dates, settings.min_days_ahead, settings.lookahead_days)
        if dates and not in_window:
            log.info("[%s] %d dates returned, none in window", route.key, len(dates))
        if not in_window:
            continue

        new = db.record_dates(route.key, in_window)
        if new:
            log.info("[%s] %d NEW dates: %s", route.key, len(new), new)
            await send_alert(
                tg_client,
                settings.bot_token,
                settings.chat_id,
                route,
                new,
                settings.passenger_count,
            )
        else:
            log.info("[%s] %d dates available, all already known", route.key, len(in_window))

    HEARTBEAT.touch()


async def main() -> None:
    _setup_logging()
    log = logging.getLogger("main")

    settings = load("/app/config.yml")
    log.info(
        "Started. routes=%d, interval=%ss, window=[+%dd .. +%dd], passengers=%d",
        len(settings.routes),
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

    async with make_client() as client, httpx.AsyncClient() as tg_client:
        while not stop.is_set():
            try:
                await run_one_cycle(settings, db, client, tg_client)
            except Exception:
                log.exception("Cycle failed")

            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    log.info("Shutting down")


if __name__ == "__main__":
    asyncio.run(main())
