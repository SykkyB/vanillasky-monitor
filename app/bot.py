from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path

import httpx

from .config import CITY_IDS, Route, Settings
from .poller import check_bookable, fetch_form_build_id

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"
OFFSET_FILE = Path("/app/data/bot_offset.json")
LONG_POLL_TIMEOUT = 25  # seconds the server will hold the request

HELP_TEXT = (
    "*Vanilla Sky monitor — commands*\n\n"
    "`/check FROM TO YYYY-MM-DD PAX` — check tickets right now\n\n"
    "Example:\n"
    "`/check Natakhtari Mestia 2026-05-15 3`\n\n"
    "Cities: Tbilisi, Ambrolauri, Batumi, Kutaisi, Mestia, Natakhtari"
)


def _load_offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text())["offset"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(json.dumps({"offset": offset}))


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


async def _handle_check(
    settings: Settings,
    vs_client: httpx.AsyncClient,
    tg_client: httpx.AsyncClient,
    chat_id: int,
    args: list[str],
) -> None:
    if len(args) != 4:
        await _tg_send(tg_client, settings.bot_token, chat_id, HELP_TEXT)
        return

    from_arg, to_arg, date_str, pax_str = args

    cities_lower = {k.lower(): k for k in CITY_IDS}
    from_canonical = cities_lower.get(from_arg.lower())
    to_canonical = cities_lower.get(to_arg.lower())
    if not from_canonical or not to_canonical:
        await _tg_send(
            tg_client,
            settings.bot_token,
            chat_id,
            f"❌ Unknown city. Known: {', '.join(CITY_IDS)}",
        )
        return
    if from_canonical == to_canonical:
        await _tg_send(tg_client, settings.bot_token, chat_id, "❌ FROM and TO must differ")
        return

    try:
        flight_date = date.fromisoformat(date_str)
    except ValueError:
        await _tg_send(
            tg_client,
            settings.bot_token,
            chat_id,
            f"❌ Bad date `{date_str}` — use YYYY-MM-DD",
        )
        return

    if flight_date < date.today():
        await _tg_send(tg_client, settings.bot_token, chat_id, "❌ Date is in the past")
        return

    try:
        pax = int(pax_str)
        if pax < 1 or pax > 9:
            raise ValueError
    except ValueError:
        await _tg_send(tg_client, settings.bot_token, chat_id, "❌ PAX must be 1-9")
        return

    route = Route(from_name=from_canonical, to_name=to_canonical)

    log.info(
        "[/check] %s -> %s on %s for %d pax (chat=%s)",
        from_canonical,
        to_canonical,
        date_str,
        pax,
        chat_id,
    )

    try:
        form_build_id = await fetch_form_build_id(vs_client)
    except Exception as e:
        await _tg_send(
            tg_client,
            settings.bot_token,
            chat_id,
            f"❌ Couldn't get form ID: `{e}`",
        )
        return

    result = await check_bookable(vs_client, form_build_id, route, date_str, pax)

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
            bits.append(f"💰 {result.price}")
        bits.append("\n👉 [Open booking page](https://ticket.vanillasky.ge/en/tickets)")
        msg = "\n".join(bits)
    else:
        msg = (
            f"❌ *{from_canonical} → {to_canonical}* — `{display_date}`\n"
            f"No tickets for *{pax}* {pax_word}."
        )

    await _tg_send(tg_client, settings.bot_token, chat_id, msg)


async def _process_update(
    settings: Settings,
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
        await _handle_check(settings, vs_client, tg_client, chat_id, args)
    else:
        await _tg_send(
            tg_client,
            settings.bot_token,
            chat_id,
            "Unknown command. Try /help",
        )


async def run_bot(
    settings: Settings,
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
            try:
                await _process_update(settings, vs_client, tg_client, update)
            except Exception:
                log.exception("Failed to process update %s", update.get("update_id"))

    log.info("Bot listener stopped")
