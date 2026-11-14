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
uv run ticket-poller notify-failure  # the ops push the failure unit sends (push only, no DB)
```

`poll-once --save DIR` keeps the captured JSON; `poll-once --notify` acts exactly like one
service poll (records state, sends owed alerts). `run` and `poll-once` hold an instance
lock, so stop the service before using `poll-once`.

## Install and run the service

```sh
deploy/install.sh                          # uv sync --frozen --no-dev, install units, daemon-reload
systemctl --user enable --now ticket-poller
systemctl --user status ticket-poller
journalctl --user -u ticket-poller -f      # one INFO line per poll; secrets are redacted
journalctl --user -u ticket-poller --since today -p warning
systemctl --user disable --now ticket-poller   # stop for good
deploy/install.sh --uninstall              # disable, stop and remove the units (state is kept)
```

`install.sh` is idempotent and never enables or starts anything; re-run it after editing
`deploy/*.service`. It installs two units:

- `ticket-poller.service`, the poller (below).
- `ticket-poller-failure.service`, a oneshot that runs `ticket-poller notify-failure
  ticket-poller.service`, pushing "Ticket poller service FAILED — check journalctl --user
  -u ticket-poller" to the ops topic (`NTFY_OPS_TOPIC`, else `NTFY_TOPIC`). It is best
  effort: it reads only the ntfy settings from the environment and `.env`, without
  validating the rest, and needs no state database or email, so it still pushes after an
  exit 78 for invalid config or `.env` permissions (and says so, without any secret). The poller starts it via `OnFailure=` when it
  ends up failed (exit 78, or too many restarts); `RestartMode=direct` keeps ordinary
  auto-restarts from triggering it. Never enable it yourself.

The poller unit:

- is a **user** service tied to the desktop session (`PartOf`/`WantedBy=graphical-session.target`):
  it starts at login and stops at logout. KDE exports `DISPLAY`, `WAYLAND_DISPLAY`,
  `XAUTHORITY` and `XDG_RUNTIME_DIR` into the user manager, so the browser gets the current
  session's display. `loginctl enable-linger` is not needed and doesn't help: with no session
  there is no display. For unattended reboots use SDDM autologin plus lock-on-login
  (Chromium keeps rendering while the screen is locked).
- **blocks suspend while it runs**, via `systemd-inhibit --what=sleep --mode=block` around
  the poller (not `idle`, so the screen still dims and the monitor still powers off). The
  lock is released automatically when the service stops. Check it with `systemd-inhibit --list`.
- runs `.venv/bin/ticket-poller run` directly (no `uv run`, so no network at startup) and
  reads `.env` itself from the repo directory.
- stops cleanly within `TimeoutStopSec=45`: `deploy/stop-poller.sh` sends SIGTERM to the
  poller (systemd-inhibit doesn't forward it) and waits while it closes Chromium.
- has an in-process **hang watchdog**: if one poll, notification step or browser close takes
  longer than `WATCHDOG_TIMEOUT_SEC` (default 900), the poller logs CRITICAL and exits 1,
  and systemd restarts it. Sleeps and backoff waits never count.

Exit codes: **0** event over or stopped (not restarted) · **1** crash or watchdog (restarted
after 60 s) · **75** another poller holds the lock (retried after 60 s) · **78** bad config,
unsafe `.env` permissions, or an unreadable/corrupt state database (not restarted; fix and
`systemctl --user restart ticket-poller`). More than 20 restarts in an hour leaves the unit
failed. A failed unit sends the ops push above.

If email delivery fails (a failed canary, Gmail rejecting the App Password, or 3 failed
emails in a row), the poller sends one ops push saying email is failing. It sends no more
until an email gets through again; this state survives restarts.

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
  "state database unusable" means `~/.local/share/em_ticket_scrape/state.sqlite3` is
  corrupt or unreadable: move it aside (alert history and the price baseline restart).
- **"email delivery failing" push**: check `SENDER_APP_PASSWORD` (Gmail revokes App
  Passwords when the account password changes), then `uv run ticket-poller test-notify`.
- **"canary DEGRADED … parse warnings (N degraded)"**: Ticketmaster's JSON changed shape
  enough to lose data, or the quantity dropdown stopped working (`no quickpicks for qty=3;
  inferred from maxQuantity`: such polls are PARTIAL, since seats offered only as 3 are
  invisible to the fallback); the `parse warning` lines in the journal say which part.
  Warnings without degraded polls only show on the canary's `Parser:` line.
- **exit 75 / "another poller is running"**: stop the service before `poll-once`.
- **ntfy push on iOS only shows after opening the app**: subscribe on the default
  `ntfy.sh` server (not a custom server URL); if it still only updates on refresh,
  reinstall the app and subscribe again.
- No pings at healthchecks for > 60 min means the poller is down, blocked, or the PC slept.

## Running on the NAS

The poller can run on the UGREEN NAS (`phils-nas.local`, UGOS = Debian 12, x86_64) instead
of the desktop, headed under a virtual display (Xvfb). `deploy/nas/` holds that variant:

- `ticket-poller-xvfb.service`: Xvfb on `:99` (1400x900x24, `-nolisten tcp`, a fresh auth
  cookie in `.nas/xauthority` on each start), `Restart=always`. Its `xvfb-helper.sh`
  clears a stale `/tmp/.X99-lock` of ours after a crash and holds the unit back until the
  display's socket exists.
- `ticket-poller.service`: the desktop unit without the graphical session, `systemd-inhibit`
  (Access denied on UGOS) or the `ExecStop` helper. It `Requires=` the Xvfb unit (stopping
  or restarting Xvfb stops or restarts the poller, and so does an Xvfb crash), sets
  `DISPLAY=:99` and `XAUTHORITY`, points `HOME` and `TMPDIR` at `.nas/home` and `.nas/tmp`
  (Chromium and Playwright write there; the real ones are world-writable on UGOS), is
  capped at `MemoryHigh=1.5G` / `MemoryMax=2.5G` and is `WantedBy=default.target`.
- `run.sh`: runs a `ticket-poller` command by hand in the service's environment.
- `ticket-poller-failure.service` is the desktop one, unchanged.

UGOS has no Chromium or Xvfb, and its apt/dpkg state is broken (don't `apt install`), so
everything runs from in-project copies under `~/repos/em_ticket_scrape/.nas/` on the NAS:
`bin/uv`, `python/` (the `.venv` links into it), `ms-playwright/chromium-1255/` (Playwright's
Chrome for Testing, matching `playwright` in `uv.lock`) and `xvfb/root/usr/bin/Xvfb`
(extracted from Debian's `xvfb` .deb; it uses the system xkbcomp, xkb-data and xauth).
`push.sh` never overwrites `.nas`, `.venv`, `.env` or the state dir; `install.sh` only
syncs `.venv` and creates `.nas/home` and `.nas/tmp`.

`$HOME` on UGOS is world-writable, so the project dir is kept 0700 and the state and profile
live inside it. `.env` on the NAS is the desktop one plus these overrides (absolute paths:
the service's `HOME` is `.nas/home`, so `~` would point there):

```sh
CHROMIUM_PATH=/home/pferreira/repos/em_ticket_scrape/.nas/ms-playwright/chromium-1255/chrome-linux64/chrome
BROWSER_PROFILE_DIR=/home/pferreira/repos/em_ticket_scrape/state/chromium-profile
STATE_DIR=/home/pferreira/repos/em_ticket_scrape/state
BROWSER_EXTRA_ARGS=--ozone-platform=x11
```

**Linger** keeps the user manager, and so the service, running with nobody logged in and
starts it at boot. Enable it once on the NAS (polkit allows it without sudo):
`loginctl enable-linger pferreira`; check with `loginctl show-user pferreira -p Linger`.

**Push, install, enable.** The NAS rsync is restricted, so `push.sh` streams a tar over ssh:

```sh
deploy/nas/push.sh                      # on the desktop: copy the working tree to the NAS
ssh pferreira@phils-nas.local
cd ~/repos/em_ticket_scrape
deploy/nas/install.sh                   # uv sync with .nas/ uv+Python, install units, daemon-reload
systemctl --user start ticket-poller-xvfb
deploy/nas/run.sh poll-once             # dry run, in the service's environment
deploy/nas/run.sh test-notify
systemctl --user enable --now ticket-poller-xvfb ticket-poller
journalctl --user -u ticket-poller -f
deploy/nas/install.sh --uninstall       # disable, stop and remove the units (state is kept)
```

There is no `uv` on the NAS PATH: use `deploy/nas/run.sh …` for the commands in
[Setup](#setup) (it runs `.venv/bin/ticket-poller` with the unit's `DISPLAY`, `XAUTHORITY`,
`HOME` and `TMPDIR`). Like the desktop installer, `install.sh` is idempotent, never enables or
starts anything, and should be re-run after every push. It checks that the in-project uv,
Chromium (the revision `uv.lock`'s Playwright needs) and Xvfb exist and load all their
libraries, warns about linger and the `.env` overrides, and tells you to restart the
service if it is running.

**Never run both hosts at once.** They poll from the same IP, which doubles the
Ticketmaster traffic and sends duplicate alerts. Switch over in this order:

1. On the desktop: `systemctl --user disable --now ticket-poller`.
2. Migrate the state DB (alert history, price baseline) while the desktop service is
   stopped. It is in WAL mode, so take a consistent single-file copy with `.backup`
   rather than copying `state.sqlite3` alone, then make it private on the NAS:

   ```sh
   sqlite3 ~/.local/share/em_ticket_scrape/state.sqlite3 ".backup $HOME/state-migrate.sqlite3"
   ssh pferreira@phils-nas.local \
     'umask 077 && mkdir -p ~/repos/em_ticket_scrape/state && cat > ~/repos/em_ticket_scrape/state/state.sqlite3' \
     < ~/state-migrate.sqlite3
   rm ~/state-migrate.sqlite3
   ```

   Do **not** copy the Chromium profile: it belongs to the desktop's Chromium version, and
   Playwright's Chromium on the NAS is a different one. The NAS starts with a fresh profile
   (that passed Ticketmaster's bot check in testing).
3. Copy the real `.env` (mode 600) with the overrides above, then push, install and enable
   as above.

To move back, disable the NAS units (`systemctl --user disable --now ticket-poller
ticket-poller-xvfb`) before enabling the desktop service again.

**After every UGOS firmware update**, check that the system libraries Chromium and Xvfb link
against are still there (re-running `deploy/nas/install.sh` does the same), and that linger
survived (it lives in the overlay root fs):

```sh
cd ~/repos/em_ticket_scrape
ldd .nas/ms-playwright/chromium-1255/chrome-linux64/chrome | grep 'not found'   # no output = OK
ldd .nas/xvfb/root/usr/bin/Xvfb | grep 'not found'
loginctl show-user pferreira -p Linger
```

## Development

```sh
uv sync
uv run ticket-poller --help
uv run ruff check && uv run ruff format --check
uv run mypy --strict src tests
uv run pytest -q
uv run pytest -q -m live   # needs a display and /usr/bin/chromium; no network
systemd-analyze --user verify deploy/ticket-poller.service deploy/ticket-poller-failure.service
```
