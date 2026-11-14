# ticket-poller

Polls one Ticketmaster event page (Noah Kahan, Birmingham, 2027-04-27) in a headed Chromium
on this desktop and alerts by email and ntfy push when matching tickets appear: any resale
listing buyable as 2 or 3, primary at ≤ $200/ticket, ≥ 20 % below the 7-day median, or
buyable as exactly 3 at ≤ $300 (top priority). Prices are listPrice, excluding service fees.
It polls every 5 min ± 1 min, backs off on errors and blocks, sends outage/recovery notices
and a daily 08:00 canary to `ADMIN_EMAIL`, pings healthchecks after each good poll, and exits
for good at `STOP_AFTER` (after the event).

## Setup

```sh
cd ~/repos/em_ticket_scrape
cp .env.example .env && chmod 600 .env   # must be owned by you, mode 600, or it exits 78
```

Fill in `.env` (all keys are listed in `.env.example`):

| Key | |
|---|---|
| `SENDER_EMAIL`, `SENDER_APP_PASSWORD` | Gmail account + App Password that send the email |
| `NTFY_TOPIC` | alert push topic; it is the credential, so make it long and random |
| `NTFY_OPS_TOPIC` | optional topic for admin notices (defaults to `NTFY_TOPIC`) |
| `HEALTHCHECKS_PING_URL` | optional `https://hc-ping.com/<uuid>`; create the check with period 5 min, grace 60 min |
| `ALERT_RECIPIENTS` | comma-separated alert emails (default: Philip only) |
| `ADMIN_EMAIL` | canary, outage and test notices |

Check it before installing (no service needed):

```sh
uv run ticket-poller test-notify     # email + push to the admin; --no-email / --no-push
uv run ticket-poller poll-once       # dry run: prints listings and would-be alerts, sends/writes nothing
uv run ticket-poller status          # polling, outage, baseline and delivery state
uv run ticket-poller canary-now --print   # render today's canary (omit --print to send it)
```

`poll-once --save DIR` keeps the captured JSON; `poll-once --notify` acts exactly like one
service poll (records state, sends owed alerts). `run` and `poll-once` hold an instance
lock, so stop the service before using `poll-once`.

## Install and run the service

```sh
deploy/install.sh                          # uv sync --frozen --no-dev, install unit, daemon-reload
systemctl --user enable --now ticket-poller
systemctl --user status ticket-poller
journalctl --user -u ticket-poller -f      # one INFO line per poll; secrets are redacted
journalctl --user -u ticket-poller --since today -p warning
systemctl --user disable --now ticket-poller   # stop for good
deploy/install.sh --uninstall              # disable, stop and remove the unit (state is kept)
```

`install.sh` is idempotent and never enables or starts anything; re-run it after editing
`deploy/ticket-poller.service`. The unit:

- is a **user** service tied to the desktop session (`PartOf`/`WantedBy=graphical-session.target`):
  it starts at login and stops at logout. KDE exports `DISPLAY`, `WAYLAND_DISPLAY`,
  `XAUTHORITY` and `XDG_RUNTIME_DIR` into the user manager, so the browser gets the current
  session's display. `loginctl enable-linger` is not needed and doesn't help: with no session
  there is no display. For unattended reboots use SDDM autologin plus lock-on-login
  (Chromium keeps rendering while the screen is locked).
- **blocks suspend and idle sleep while it runs**, via
  `systemd-inhibit --what=sleep:idle --mode=block` around the poller. The lock is released
  automatically when the service stops. Check it with `systemd-inhibit --list`.
- runs `.venv/bin/ticket-poller run` directly (no `uv run`, so no network at startup) and
  reads `.env` itself from the repo directory.
- stops cleanly within `TimeoutStopSec=45`: `deploy/stop-poller.sh` sends SIGTERM to the
  poller (systemd-inhibit doesn't forward it) and waits while it closes Chromium.

Exit codes: **0** event over or stopped (not restarted) · **1** crash (restarted after 60 s)
· **75** another poller holds the lock (retried after 60 s) · **78** bad config or unsafe
`.env` permissions (not restarted; fix and `systemctl --user restart ticket-poller`).
More than 20 restarts in an hour leaves the unit failed.

## Adding Emery

After the 12 h soak passes, set
`ALERT_RECIPIENTS=philip@ferreira-family.com,emery.c.little@gmail.com` in `.env`, then
`systemctl --user restart ticket-poller`. Emery can subscribe to `NTFY_TOPIC` in the ntfy app.

## Using the browser profile by hand

To solve a challenge or sign in, **stop the service first**: a Chromium started on a profile
that is in use just opens a window in the poller's automated browser.

```sh
systemctl --user stop ticket-poller
chromium --user-data-dir=$HOME/.local/share/em_ticket_scrape/chromium-profile https://www.ticketmaster.com/event/2000655CAB442FB5
# … then quit Chromium completely …
systemctl --user start ticket-poller
```

Never delete the profile's `Singleton*` files.

## Troubleshooting

- **blocked** (outage notice says Ticketmaster is blocking): the poller backs off up to
  60 min by itself. If it persists, open the event page in the profile by hand (above).
- **profile_in_use**: a manual Chromium is open on the poller's profile; quit it. The poller
  retries with backoff.
- **no_display** or the unit fails its `DISPLAY` assertion: the graphical session isn't
  running or didn't export its environment. Check `systemctl --user show-environment`; on a
  non-KDE desktop add `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY`
  to the session's autostart.
- **exit 78**: read the error in `journalctl --user -u ticket-poller`; usually `chmod 600 .env`.
- **exit 75 / "another poller is running"**: stop the service before `poll-once`.
- **ntfy push on iOS only shows after opening the app**: subscribe on the default
  `ntfy.sh` server (not a custom server URL); if it still only updates on refresh,
  reinstall the app and subscribe again.
- No pings at healthchecks for > 60 min means the poller is down, blocked, or the PC slept.

## Development

```sh
uv sync
uv run ticket-poller --help
uv run ruff check && uv run ruff format --check
uv run mypy --strict src tests
uv run pytest -q
uv run pytest -q -m live   # needs a display and /usr/bin/chromium; no network
systemd-analyze --user verify deploy/ticket-poller.service
```
