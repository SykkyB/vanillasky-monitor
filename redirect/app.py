"""Cloudflare-tunnelled redirect service that pre-fills Vanilla Sky's
booking form via an auto-submit HTML page.

Flow on user click:
  1. User clicks a link in Telegram: vs.sys-lab.xyz/go?from=7&to=6&date=...
  2. Browser hits this service via Cloudflare tunnel.
  3. We fetch a fresh (or cached) form_build_id from ticket.vanillasky.ge.
  4. We return an HTML page with an auto-submitting <form> targeting
     /en/tickets, populated with the requested route + date + pax.
  5. User's browser POSTs the form. Vanilla Sky processes it as if the
     user filled it out manually, and redirects to /en/flights-form.
  6. User lands on the "Choose Flight" page with the right context.

If we can't fetch the form_build_id (Vanilla Sky down, network issue),
we 302 the user to /en/tickets so they can fill the form by hand."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from html import escape

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("redirect")

VS_BASE = "https://ticket.vanillasky.ge"
TICKETS_URL = f"{VS_BASE}/en/tickets"
USER_AGENT = "Mozilla/5.0 (compatible; vanilla-sky-redirect/1.0)"
FORM_ID_TTL_SECONDS = 300  # 5 min

_FORM_BUILD_ID_RE = re.compile(r'name="form_build_id" value="([^"]+)"')

# Single-key cache: ("default" → (build_id, expires_at)).
_form_id_cache: dict[str, tuple[str, float]] = {}

app = FastAPI(title="Vanilla Sky Redirect", docs_url=None, redoc_url=None)


async def _get_form_build_id(client: httpx.AsyncClient, force: bool = False) -> str:
    now = time.time()
    cached = _form_id_cache.get("default")
    if not force and cached and cached[1] > now:
        return cached[0]

    resp = await client.get(TICKETS_URL, timeout=20.0)
    resp.raise_for_status()
    m = _FORM_BUILD_ID_RE.search(resp.text)
    if not m:
        raise RuntimeError("form_build_id not found on /en/tickets")

    build_id = m.group(1)
    _form_id_cache["default"] = (build_id, now + FORM_ID_TTL_SECONDS)
    log.info("Refreshed form_build_id (cached %ds)", FORM_ID_TTL_SECONDS)
    return build_id


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Vanilla Sky redirect</h1>"
        "<p>This service auto-submits a Vanilla Sky booking form. "
        "It is intended to be reached via deep links from a Telegram bot.</p>"
        f'<p><a href="{TICKETS_URL}">Open Vanilla Sky booking</a></p>'
    )


from fastapi import Query  # noqa: E402


@app.get("/go", response_class=HTMLResponse)
async def go(
    from_: int = Query(0, alias="from"),
    to: int = Query(0),
    date: str = Query(""),
    pax: int = Query(1),
    back_date: str = Query(""),  # optional: triggers native round-trip
) -> HTMLResponse:
    # ---- input validation ----
    if from_ < 1 or to < 1:
        raise HTTPException(400, "Invalid 'from' or 'to' (need positive city ID)")
    if from_ == to:
        raise HTTPException(400, "'from' and 'to' must differ")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "Invalid 'date' (need YYYY-MM-DD)")
    if pax < 1 or pax > 9:
        raise HTTPException(400, "Invalid 'pax' (1-9)")

    try:
        flight_dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid 'date'") from None
    display_date = flight_dt.strftime("%d %b %Y")  # e.g. "31 May 2026"

    # Optional return leg → triggers Vanilla Sky's native round-trip mode.
    types_value = "0"  # one-way
    back_display = ""
    if back_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", back_date):
            raise HTTPException(400, "Invalid 'back_date' (need YYYY-MM-DD)")
        try:
            back_dt = datetime.strptime(back_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Invalid 'back_date'") from None
        if back_dt.date() < flight_dt.date():
            raise HTTPException(400, "'back_date' must be on or after 'date'")
        back_display = back_dt.strftime("%d %b %Y")
        types_value = "1"  # round-trip

    # ---- get form_build_id ----
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        try:
            form_build_id = await _get_form_build_id(client)
        except Exception as e:
            log.error("Couldn't fetch form_build_id: %s — falling back to plain redirect", e)
            return RedirectResponse(TICKETS_URL, status_code=302)

    # ---- render auto-submit HTML ----
    if back_display:
        date_caption = f"{display_date} → {back_display}"
        trip_caption = "round-trip"
    else:
        date_caption = display_date
        trip_caption = "one-way"

    html_body = f"""<!doctype html>
<html lang="en"><head>
  <title>Booking redirect…</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; padding: 2em;
            text-align: center; background: #fafafa; color: #333; }}
    .spinner {{ display: inline-block; width: 40px; height: 40px;
                border: 4px solid #e5e7eb; border-top-color: #b91c1c;
                border-radius: 50%; animation: spin 0.8s linear infinite; }}
    @keyframes spin {{ from {{ transform: rotate(0); }} to {{ transform: rotate(360deg); }} }}
    button {{ margin-top: 1em; padding: 0.7em 1.5em; font-size: 1em;
              background: #b91c1c; color: white; border: 0; border-radius: 4px;
              cursor: pointer; }}
  </style>
</head><body>
  <div class="spinner"></div>
  <p>Loading Vanilla Sky booking…<br>
  <small>{escape(date_caption)} · {trip_caption} · {pax} passenger{"s" if pax > 1 else ""}</small></p>

  <form id="f" method="POST"
        action="{escape(TICKETS_URL)}"
        enctype="application/x-www-form-urlencoded">
    <input type="hidden" name="departure" value="{from_}">
    <input type="hidden" name="arrive" value="{to}">
    <input type="hidden" name="types" value="{types_value}">
    <input type="hidden" name="date_picker" value="{escape(display_date)}">
    <input type="hidden" name="date_picker_arrive" value="{escape(back_display)}">
    <input type="hidden" name="person_count" value="{pax}">
    <input type="hidden" name="person_types[adult]" value="{pax}">
    <input type="hidden" name="person_types[child]" value="0">
    <input type="hidden" name="person_types[infant]" value="0">
    <input type="hidden" name="op" value="">
    <input type="hidden" name="form_build_id" value="{escape(form_build_id)}">
    <input type="hidden" name="form_id" value="form_select_date">
    <noscript>
      <p>JavaScript is disabled. Click below to continue:</p>
      <button type="submit">Continue to Vanilla Sky</button>
    </noscript>
  </form>

  <script>
    setTimeout(function() {{ document.getElementById("f").submit(); }}, 50);
  </script>
</body></html>"""
    return HTMLResponse(html_body)
