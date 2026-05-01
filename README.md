# vanilla-sky

Polls Vanilla Sky's booking site every 5 minutes and sends a Telegram alert
when **tickets actually go on sale** for the routes you care about.

## How it works

Vanilla Sky exposes two surfaces:

1. `GET /custom/check-flight/{from_id}/{to_id}` — JSON list of dates the plane
   is *scheduled* to fly. Updated rarely. **Does not** mean tickets are buyable.
2. `POST /en/tickets` (the booking form) — does the real availability check.
   Returns either `Choose Flight` (with time + price) or `There are no available
   tickets`. This is the truth.

The bot uses (1) to know which dates to probe, then (2) per-date with the
configured `passenger_count` to find the actual buyability. State is stored
per `(route, date, passenger_count)` and an alert fires only on the
`not bookable → bookable` transition (a real release).

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather), copy the token.
2. Get your `chat_id` via [@userinfobot](https://t.me/userinfobot).
3. Copy `.env.example` → `.env`, fill in the token + chat id, `chmod 600 .env`.
4. (Optional) Edit `config.yml` — routes, interval, window, passenger_count.
5. Start it:

```sh
docker compose up -d --build
docker compose logs -f
```

## Files

- `config.yml` — routes, polling window, passenger count.
- `data/state.db` — SQLite state. Tables:
  - `schedule_dates` — when did we first/last see each date in the schedule API.
  - `bookable_state` — current buyability per `(route, date, passenger_count)`.
  - `bookable_events` — release / sold-out transitions over time.
- `data/heartbeat` — touched after each successful cycle (used by healthcheck).

## Inspecting history

Release events (real ticket drops):

```sh
docker compose exec vanilla-sky-monitor python -c "
import sqlite3
from datetime import datetime
c = sqlite3.connect('/app/data/state.db')
for ts, route, date, transition, price, flight_time in c.execute('''
    SELECT ts, route_key, flight_date, transition, price, flight_time
    FROM bookable_events ORDER BY ts DESC LIMIT 30'''):
    print(datetime.fromtimestamp(ts), route, date, transition, price or '-', flight_time or '-')
"
```

Current buyable state (snapshot):

```sh
docker compose exec vanilla-sky-monitor python -c "
import sqlite3
c = sqlite3.connect('/app/data/state.db')
for row in c.execute('''
    SELECT route_key, flight_date, bookable, price, flight_time, passenger_count
    FROM bookable_state ORDER BY route_key, flight_date'''):
    print(row)
"
```

## City IDs (informational)

`1=Tbilisi, 2=Ambrolauri, 4=Batumi, 5=Kutaisi, 6=Mestia, 7=Natakhtari`
