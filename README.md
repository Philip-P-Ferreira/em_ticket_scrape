# ticket-poller

Polls one Ticketmaster event page in a headed Chromium and alerts by email and ntfy push
when matching tickets appear. Install, operation and deployment docs are still to come.

## Development

```sh
uv sync
cp .env.example .env && chmod 600 .env   # then fill in the secrets
uv run ticket-poller --help

uv run ruff check && uv run ruff format --check
uv run mypy --strict src tests
uv run pytest -q
uv run pytest -q -m live   # needs a display and /usr/bin/chromium; no network
```

`uv run ticket-poller poll-once` loads the event once in the headed browser and prints what
it found and what would alert. It is a dry run: no notifications and no state writes.
Add `--save DIR` to keep the captured JSON responses. `poll-once --notify` behaves exactly
like one service poll: it records the poll and sends any owed alerts.

`uv run ticket-poller run` is the service loop: it polls every 5 min ± 1 min (backing off
on errors and blocks) and alerts on new matches. It also sends the outage and recovery
notices and the daily 08:00 canary to `ADMIN_EMAIL`, pings healthchecks after each
successful poll, and exits 0 at `STOP_AFTER` or on SIGTERM/SIGINT.

Exit codes: 0 = event over/signal, 1 = crash, 75 = another poller holds the lock,
78 = invalid configuration or unsafe `.env` permissions.
