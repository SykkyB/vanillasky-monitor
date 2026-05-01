# vanilla-sky

Polls Vanilla Sky's ticket API every 5 minutes and sends a Telegram alert when
new dates appear on the routes you care about.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your `chat_id` — write something to your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id":...}`.
   Or message [@userinfobot](https://t.me/userinfobot).
3. Copy `.env.example` → `.env` and fill in the token + chat id.
4. (Optional) Edit `config.yml` — routes, polling interval, window.
5. Start it:

```sh
docker compose up -d --build
docker compose logs -f
```

## Files

- `config.yml` — routes and polling window. Edit and `docker compose restart`.
- `data/state.db` — SQLite. Tracks seen dates so you don't get duplicate alerts;
  also keeps an `events` table with the history of every release.
- `data/heartbeat` — touched at the end of every successful cycle; used by
  `docker-compose` healthcheck.

## Inspecting history

```sh
docker compose exec vanilla-sky-monitor python -c "
import sqlite3, json
c = sqlite3.connect('/app/data/state.db')
for ts, route, new in c.execute('SELECT ts, route_key, new_dates FROM events ORDER BY ts DESC LIMIT 20'):
    from datetime import datetime
    print(datetime.fromtimestamp(ts), route, json.loads(new))
"
```

## City IDs (informational)

`1=Tbilisi, 2=Ambrolauri, 4=Batumi, 5=Kutaisi, 6=Mestia, 7=Natakhtari`

Underlying API: `GET https://ticket.vanillasky.ge/custom/check-flight/{from_id}/{to_id}` →
`{"from": [...dates], "to": [...dates]}` (empty arrays = no tickets).
