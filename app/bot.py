from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

import httpx

from .config import CITY_IDS, Route, Settings
from .db import DB
from .links import booking_link, is_tunnel_alive
from .poller import (
    check_bookable,
    fetch_destinations,
    fetch_form_build_id,
    fetch_schedule,
)

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"
OFFSET_FILE = Path("/app/data/bot_offset.json")
LONG_POLL_TIMEOUT = 25
POST_RATE_LIMIT_SEC = 0.5
DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y")

HELP_TEXT = (
    "*Vanilla Sky monitor*\n\n"
    "*Search:*\n"
    "`/check FROM TO DATE [PAX]` — check a specific route\n"
    "`/check FROM DATE [PAX]` — scan all destinations on a date\n"
    "`/check FROM` — show everything on sale from FROM (full window)\n"
    "`/routes` — show full Vanilla Sky route graph\n\n"
    "*Polling control:*\n"
    "`/status` — show monitor state\n"
    "`/pause` — stop background polling (search still works)\n"
    "`/resume` — start background polling again\n"
    "`/tunnel_on`, `/tunnel_off` — toggle clickable booking links\n\n"
    "DATE: `DD-MM-YYYY`, `DD/MM/YYYY`, `DD.MM.YYYY` "
    "(e.g. `31-05-2026`, `31/05/2026`, `31.05.2026`)\n"
    "PAX: 1–9, defaults to 1\n\n"
    "Cities: Tbilisi, Ambrolauri, Batumi, Kutaisi, Mestia, Natakhtari\n"
    "(prefixes work: `Nat`, `Mes`, `B`, etc.)\n\n"
    "Examples:\n"
    "`/check Natakhtari Mestia 31-05-2026`\n"
    "`/check Mes 31.05.2026 3`\n"
    "`/check Natakhtari 31/05/2026`\n"
    "`/check Natakhtari`"
)


def _load_offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text())["offset"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(json.dumps({"offset": offset}))


def _parse_date(s: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _safe_iso(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _resolve_city(arg: str) -> str | None:
    """Case-insensitive exact or unambiguous-prefix match against city list."""
    if not arg:
        return None
    al = arg.lower()
    exact = [k for k in CITY_IDS if k.lower() == al]
    if exact:
        return exact[0]
    prefix = [k for k in CITY_IDS if k.lower().startswith(al)]
    return prefix[0] if len(prefix) == 1 else None


def _parse_pax(s: str) -> int | None:
    try:
        n = int(s)
    except ValueError:
        return None
    return n if 1 <= n <= 9 else None


async def _tg_send(
    client: httpx.AsyncClient, bot_token: str, chat_id: int | str, text: str
) -> None:
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
            log.error("Telegram send error %s: %s", resp.status_code, resp.text[:300])
    except httpx.HTTPError as e:
        log.error("Telegram send failed: %s", e)


async def _check_one_route(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
    from_canonical: str,
    to_canonical: str,
    flight_date: date,
    pax: int,
) -> None:
    if from_canonical == to_canonical:
        await _tg_send(tg_client, settings.bot_token, chat_id, "❌ FROM and TO must differ")
        return

    route = Route(from_name=from_canonical, to_name=to_canonical)
    iso = flight_date.isoformat()

    log.info("[/check] %s -> %s on %s for %d pax", from_canonical, to_canonical, iso, pax)

    try:
        form_build_id = await fetch_form_build_id(vs_client)
    except Exception as e:
        await _tg_send(
            tg_client, settings.bot_token, chat_id, f"❌ Couldn't get form ID: `{e}`"
        )
        return

    result = await check_bookable(vs_client, form_build_id, route, iso, pax)

    display_date = flight_date.strftime("%d-%B-%Y")
    pax_word = "passenger" if pax == 1 else "passengers"

    if result.bookable:
        bits = [
            f"✅ *{from_canonical} → {to_canonical}* — `{display_date}`",
            f"Available for *{pax}* {pax_word}",
        ]
        if result.flight_time:
            bits.append(f"🕒 {result.flight_time}")
        if result.price:
            bits.append(f"💰 {result.price} (per 1 passenger)")

        redirect_base = await _resolve_redirect_base(db, settings, vs_client)
        if redirect_base:
            link = booking_link(redirect_base, route, iso, pax)
            bits.append(f"\n👉 [Book this flight]({link})")
        else:
            bits.append("\n👉 [Open booking page](https://ticket.vanillasky.ge/en/tickets)")
        msg = "\n".join(bits)
    else:
        msg = (
            f"❌ *{from_canonical} → {to_canonical}* — `{display_date}`\n"
            f"No tickets for *{pax}* {pax_word}."
        )
    await _tg_send(tg_client, settings.bot_token, chat_id, msg)


async def _scan_origin_full(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
    from_canonical: str,
    pax: int,
) -> None:
    """No date — scan all destinations from FROM across every date Vanilla Sky
    currently has scheduled. Ignores min_days_ahead / lookahead_days; only
    drops dates that are in the past."""
    log.info("[/check origin-full] %s for %d pax (no window)", from_canonical, pax)

    dest_names = await fetch_destinations(vs_client, from_canonical)
    if not dest_names:
        await _tg_send(
            tg_client,
            settings.bot_token,
            chat_id,
            f"No flights configured from *{from_canonical}*",
        )
        return

    await _tg_send(
        tg_client,
        settings.bot_token,
        chat_id,
        f"🔎 Scanning *every scheduled date* across {len(dest_names)} "
        f"destinations from *{from_canonical}*, this may take a minute…",
    )

    try:
        form_build_id = await fetch_form_build_id(vs_client)
    except Exception as e:
        await _tg_send(
            tg_client, settings.bot_token, chat_id, f"❌ Couldn't get form ID: `{e}`"
        )
        return

    today = date.today()
    sem = asyncio.Semaphore(3)  # cap concurrency so we don't hammer the site

    async def _probe(route: Route, d: str):
        async with sem:
            return d, await check_bookable(vs_client, form_build_id, route, d, pax)

    results_by_dest: dict[str, list[tuple[str, str | None, str | None]]] = {}
    for dest in dest_names:
        route = Route(from_name=from_canonical, to_name=dest)
        schedule = await fetch_schedule(vs_client, route)
        upcoming = sorted(
            d for d in schedule
            if (parsed := _safe_iso(d)) is not None and parsed >= today
        )
        results_by_dest[dest] = []
        if not upcoming:
            continue
        outcomes = await asyncio.gather(*(_probe(route, d) for d in upcoming))
        for d, r in outcomes:
            if r.bookable:
                results_by_dest[dest].append((d, r.flight_time, r.price))

    pax_word = "passenger" if pax == 1 else "passengers"
    have_any = any(v for v in results_by_dest.values())

    if not have_any:
        msg = (
            f"❌ Nothing on sale from *{from_canonical}* right now "
            f"for *{pax}* {pax_word}."
        )
    else:
        redirect_base = await _resolve_redirect_base(db, settings, vs_client)
        lines = [
            f"✅ *From {from_canonical}* — currently on sale for *{pax}* {pax_word}:",
            "",
        ]
        for dest in dest_names:
            available = results_by_dest.get(dest, [])
            if not available:
                continue
            lines.append(f"*→ {dest}* ({len(available)})")
            route = Route(from_name=from_canonical, to_name=dest)
            for d, t, p in available:
                date_label = f"`{date.fromisoformat(d).strftime('%d-%B-%Y')}`"
                if redirect_base:
                    pretty = date.fromisoformat(d).strftime("%d-%B-%Y")
                    link = booking_link(redirect_base, route, d, pax)
                    date_label = f"[{pretty}]({link})"
                bits = [date_label]
                if t:
                    bits.append(t)
                if p:
                    bits.append(f"{p} (per 1 pax)")
                lines.append("  • " + " — ".join(bits))
            lines.append("")
        empty_dests = [d for d in dest_names if not results_by_dest.get(d)]
        if empty_dests:
            lines.append(f"_Empty: {', '.join(empty_dests)}_")
        if not redirect_base:
            lines.append("\n👉 [Open booking page](https://ticket.vanillasky.ge/en/tickets)")
        msg = "\n".join(lines)

    await _tg_send(tg_client, settings.bot_token, chat_id, msg)


async def _scan_destinations(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
    from_canonical: str,
    flight_date: date,
    pax: int,
) -> None:
    iso = flight_date.isoformat()

    log.info("[/check scan] %s on %s for %d pax", from_canonical, iso, pax)

    dest_names = await fetch_destinations(vs_client, from_canonical)
    if not dest_names:
        await _tg_send(
            tg_client,
            settings.bot_token,
            chat_id,
            f"No flights configured from *{from_canonical}*",
        )
        return

    try:
        form_build_id = await fetch_form_build_id(vs_client)
    except Exception as e:
        await _tg_send(
            tg_client, settings.bot_token, chat_id, f"❌ Couldn't get form ID: `{e}`"
        )
        return

    available: list[tuple[str, str | None, str | None]] = []
    for dest in dest_names:
        route = Route(from_name=from_canonical, to_name=dest)
        result = await check_bookable(vs_client, form_build_id, route, iso, pax)
        if result.bookable:
            available.append((dest, result.flight_time, result.price))
        await asyncio.sleep(POST_RATE_LIMIT_SEC)

    display_date = flight_date.strftime("%d-%B-%Y")
    pax_word = "passenger" if pax == 1 else "passengers"

    if not available:
        checked = ", ".join(dest_names)
        msg = (
            f"❌ No tickets from *{from_canonical}* on `{display_date}` "
            f"for *{pax}* {pax_word}\n\n"
            f"Checked: {checked}"
        )
    else:
        redirect_base = await _resolve_redirect_base(db, settings, vs_client)
        lines = [
            f"✅ From *{from_canonical}* on `{display_date}` for *{pax}* {pax_word}:",
            "",
        ]
        for dest, ftime, price in available:
            dest_label = f"*{dest}*"
            if redirect_base:
                route = Route(from_name=from_canonical, to_name=dest)
                link = booking_link(redirect_base, route, iso, pax)
                dest_label = f"[*{dest}*]({link})"
            bits = [f"→ {dest_label}"]
            if ftime:
                bits.append(f"🕒 {ftime}")
            if price:
                bits.append(f"💰 {price} (per 1 pax)")
            lines.append("• " + " — ".join(bits))
        if not redirect_base:
            lines.append("\n👉 [Open booking page](https://ticket.vanillasky.ge/en/tickets)")
        msg = "\n".join(lines)

    await _tg_send(tg_client, settings.bot_token, chat_id, msg)


async def _handle_check(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
    args: list[str],
) -> None:
    """Dispatch:
        4 args  → FROM TO DATE PAX
        3 args  → FROM TO DATE (pax=1)  OR  FROM DATE PAX
        2 args  → FROM DATE (pax=1)
        1 arg   → FROM only (pax=1, full window scan)
        else    → help
    """
    if len(args) not in (1, 2, 3, 4):
        await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
        return

    from_canonical = _resolve_city(args[0])
    if not from_canonical:
        await _tg_send(
            tg_client,
            settings.bot_token,
            chat_id,
            f"❌ Unknown FROM `{args[0]}`. Known: {', '.join(CITY_IDS)}",
        )
        return

    if len(args) == 1:
        await _scan_origin_full(
            settings, db, vs_client, tg_client, chat_id, from_canonical, pax=1
        )
        return

    if len(args) == 4:
        to_canonical = _resolve_city(args[1])
        flight_date = _parse_date(args[2])
        pax = _parse_pax(args[3])
        if not to_canonical or flight_date is None or pax is None:
            await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
            return
    elif len(args) == 3:
        # disambiguate args[1]: city or date?
        maybe_city = _resolve_city(args[1])
        maybe_date = _parse_date(args[1])
        if maybe_city:
            to_canonical = maybe_city
            flight_date = _parse_date(args[2])
            pax = 1
            if flight_date is None:
                await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
                return
        elif maybe_date:
            to_canonical = None
            flight_date = maybe_date
            pax = _parse_pax(args[2])
            if pax is None:
                await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
                return
        else:
            await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
            return
    else:  # 2 args
        to_canonical = None
        flight_date = _parse_date(args[1])
        pax = 1
        if flight_date is None:
            await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
            return

    if flight_date < date.today():
        await _tg_send(tg_client, settings.bot_token, chat_id, "❌ Date is in the past")
        return

    if to_canonical:
        await _check_one_route(
            settings, db, vs_client, tg_client, chat_id,
            from_canonical, to_canonical, flight_date, pax,
        )
    else:
        await _scan_destinations(
            settings, db, vs_client, tg_client, chat_id,
            from_canonical, flight_date, pax,
        )


async def _resolve_redirect_base(
    db: DB, settings: Settings, client: httpx.AsyncClient
) -> str | None:
    """Return base URL for booking links, or None if tunnel is disabled/unreachable."""
    if not db.get_flag("tunnel_enabled"):
        return None
    if not settings.redirect_url_base:
        return None
    if not await is_tunnel_alive(client, settings.redirect_url_base):
        return None
    return settings.redirect_url_base


async def _handle_tunnel_on(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
) -> None:
    db.set_flag("tunnel_enabled", True)
    if not settings.redirect_url_base:
        msg = (
            "🌐 *Tunnel mode enabled.*\n\n"
            "But `redirect_url_base` is not configured in `config.yml`. "
            "Until you set it, alerts will still use plain text. "
            "Once you deploy the redirect service, add its URL to "
            "`redirect_url_base` and links will appear automatically."
        )
    else:
        alive = await is_tunnel_alive(vs_client, settings.redirect_url_base)
        marker = "✅ reachable now" if alive else "⚠️ NOT reachable now"
        msg = (
            f"🌐 *Tunnel mode enabled.*\n\n"
            f"Base: `{settings.redirect_url_base}` — {marker}\n\n"
            "Each alert will probe the tunnel and use clickable links if alive, "
            "or plain text if down."
        )
    await _tg_send(tg_client, settings.bot_token, chat_id, msg)


async def _handle_tunnel_off(
    settings: Settings, db: DB, tg_client: httpx.AsyncClient, chat_id: int
) -> None:
    db.set_flag("tunnel_enabled", False)
    await _tg_send(
        tg_client,
        settings.bot_token,
        chat_id,
        "🚫 *Tunnel mode disabled.* Alerts will use plain-text format.",
    )


async def _handle_pause(
    settings: Settings, db: DB, tg_client: httpx.AsyncClient, chat_id: int
) -> None:
    db.set_flag("polling_paused", True)
    log.info("Polling paused by chat=%s", chat_id)
    await _tg_send(
        tg_client,
        settings.bot_token,
        chat_id,
        "⏸️ *Polling paused.*\n\n"
        "Background checks are stopped. /check and /routes still work.\n"
        "Use /resume to start polling again.",
    )


async def _handle_resume(
    settings: Settings, db: DB, tg_client: httpx.AsyncClient, chat_id: int
) -> None:
    db.set_flag("polling_paused", False)
    log.info("Polling resumed by chat=%s", chat_id)
    await _tg_send(
        tg_client,
        settings.bot_token,
        chat_id,
        "▶️ *Polling resumed.* Next cycle in ≤5 minutes.",
    )


async def _handle_status(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
) -> None:
    paused = db.get_flag("polling_paused")
    state = "⏸️ *paused*" if paused else "▶️ *active*"

    qh_text = "—"
    in_quiet_now = False
    if settings.quiet_hours:
        qh_text = settings.quiet_hours.display()
        from .main import _local_now  # local import to avoid circular at top

        tz = os.environ.get("TZ", "UTC")
        in_quiet_now = settings.quiet_hours.covers(_local_now(tz).time())
    quiet_marker = " (now)" if in_quiet_now else ""

    tunnel_enabled = db.get_flag("tunnel_enabled")
    if not settings.redirect_url_base:
        tunnel_text = "—  _(redirect_url_base not set in config)_"
    elif not tunnel_enabled:
        tunnel_text = f"🚫 disabled (`{settings.redirect_url_base}`)"
    else:
        alive = await is_tunnel_alive(vs_client, settings.redirect_url_base)
        mark = "✅ reachable" if alive else "⚠️ unreachable"
        tunnel_text = f"🌐 enabled — {mark} (`{settings.redirect_url_base}`)"

    origins = ", ".join(settings.monitor_origins) or "—"
    extras = ", ".join(f"{r.from_name}→{r.to_name}" for r in settings.extra_routes) or "—"

    msg = (
        f"*Polling:* {state}\n"
        f"*Quiet hours:* {qh_text}{quiet_marker}\n"
        f"*Interval:* {settings.poll_interval_seconds}s\n"
        f"*Window:* +{settings.min_days_ahead}d ... +{settings.lookahead_days}d\n"
        f"*Default pax:* {settings.passenger_count}\n"
        f"*Tunnel links:* {tunnel_text}\n"
        f"*Origins:* {origins}\n"
        f"*Extra routes:* {extras}"
    )
    await _tg_send(tg_client, settings.bot_token, chat_id, msg)


async def _handle_routes(
    settings: Settings,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
) -> None:
    log.info("[/routes] requested by chat=%s", chat_id)

    lines = ["📍 *Vanilla Sky route graph*", ""]
    found_any = False
    for origin in CITY_IDS:
        dests = await fetch_destinations(vs_client, origin)
        if dests:
            found_any = True
            lines.append(f"*{origin}* → {', '.join(dests)}")
        await asyncio.sleep(0.3)

    if not found_any:
        lines = ["❌ Couldn't fetch route graph (API failure)"]

    await _tg_send(tg_client, settings.bot_token, chat_id, "\n".join(lines))


async def _process_update_safe(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    update: dict,
) -> None:
    """Wrapper so background tasks don't crash silently and exceptions are logged."""
    try:
        await _process_update(settings, db, vs_client, tg_client, update)
    except Exception:
        log.exception("Failed to process update %s", update.get("update_id"))


async def _process_update(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    update: dict,
) -> None:
    msg = update.get("message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if str(chat_id) != str(settings.chat_id):
        log.info("Ignored message from unauthorized chat_id=%s", chat_id)
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    cmd, *args = text.split()
    cmd = cmd.lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    if cmd in ("/start", "/help"):
        await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
    elif cmd == "/check":
        await _handle_check(settings, db, vs_client, tg_client, chat_id, args)
    elif cmd == "/routes":
        await _handle_routes(settings, vs_client, tg_client, chat_id)
    elif cmd == "/pause":
        await _handle_pause(settings, db, tg_client, chat_id)
    elif cmd == "/resume":
        await _handle_resume(settings, db, tg_client, chat_id)
    elif cmd == "/status":
        await _handle_status(settings, db, vs_client, tg_client, chat_id)
    elif cmd == "/tunnel_on":
        await _handle_tunnel_on(settings, db, vs_client, tg_client, chat_id)
    elif cmd == "/tunnel_off":
        await _handle_tunnel_off(settings, db, tg_client, chat_id)
    else:
        await _tg_send(
            tg_client, settings.bot_token, chat_id, "Unknown command. Try /help"
        )


async def run_bot(
    settings: Settings,
    db: DB,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    offset = _load_offset()
    log.info("Bot listener started, offset=%s", offset)

    url = f"{TG_API}/bot{settings.bot_token}/getUpdates"

    while not stop.is_set():
        try:
            resp = await tg_client.get(
                url,
                params={
                    "offset": offset,
                    "timeout": LONG_POLL_TIMEOUT,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=LONG_POLL_TIMEOUT + 10,
            )
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("getUpdates failed: %s, retrying in 5s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            continue

        if not data.get("ok"):
            log.error("Telegram getUpdates error: %s", data)
            await asyncio.sleep(5)
            continue

        for update in data["result"]:
            offset = max(offset, update["update_id"] + 1)
            _save_offset(offset)
            # Fire-and-forget so a long-running /check doesn't block other commands.
            asyncio.create_task(
                _process_update_safe(settings, db, vs_client, tg_client, update)
            )

    log.info("Bot listener stopped")
