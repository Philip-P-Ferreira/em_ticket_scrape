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
Add `--save DIR` to keep the captured JSON responses.

Exit codes: 0 = done/signal, 1 = crash, 75 = another poller holds the lock,
78 = invalid configuration or unsafe `.env` permissions.
