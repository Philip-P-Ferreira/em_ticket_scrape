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
```

Exit codes: 0 = done/signal, 1 = crash, 75 = another poller holds the lock,
78 = invalid configuration or unsafe `.env` permissions.
