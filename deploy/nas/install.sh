#!/usr/bin/env bash
# Install (or update) the NAS variant of the ticket-poller systemd user units: Xvfb :99,
# the poller and its OnFailure= push unit. Run it ON the NAS, from the repo checkout that
# deploy/nas/push.sh copied there. Idempotent; safe to re-run. It syncs the locked runtime
# deps with the in-project uv and Python (.nas/), installs the units and reloads systemd,
# but never enables, starts or restarts anything.
#
#   deploy/nas/install.sh              install / update
#   deploy/nas/install.sh --uninstall  disable + stop the units and remove them
set -euo pipefail

XVFB_UNIT=ticket-poller-xvfb.service
UNIT=ticket-poller.service
FAILURE_UNIT=ticket-poller-failure.service   # started by OnFailure= in $UNIT
UNITS=("$XVFB_UNIT" "$UNIT" "$FAILURE_UNIT")
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
NAS=$ROOT/.nas                               # in-project uv, Python, Chromium and Xvfb
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
HOME_REAL=$(realpath -m -- "$HOME")          # path checks compare resolved paths only
DEFAULT_ROOT='%h/repos/em_ticket_scrape'     # the path written in the templates

die() { echo "error: $*" >&2; exit 1; }
unit_source() {   # the failure unit is shared with the desktop install
    if [[ $1 == "$FAILURE_UNIT" ]]; then echo "$ROOT/deploy/$1"; else echo "$ROOT/deploy/nas/$1"; fi
}
missing_libs() { ldd "$1" 2>&1 | grep 'not found' || true; }

[[ $EUID -ne 0 ]] || die "run as the NAS user, not root"

if [[ ${1:-} == --uninstall ]]; then
    systemctl --user disable --now "$UNIT" "$XVFB_UNIT" 2>/dev/null || true
    systemctl --user stop "$FAILURE_UNIT" 2>/dev/null || true
    for unit in "${UNITS[@]}"; do rm -f -- "$UNIT_DIR/$unit"; done
    systemctl --user daemon-reload
    systemctl --user reset-failed "${UNITS[@]}" 2>/dev/null || true
    echo "Removed ${UNITS[*]} from $UNIT_DIR."
    echo "Kept: .env, the state dir (STATE_DIR), .venv and .nas in $ROOT."
    exit 0
fi
[[ $# -eq 0 ]] || die "usage: $0 [--uninstall]"

# The repo path is substituted into the units with sed and must be a plain path.
[[ $ROOT =~ ^[A-Za-z0-9_./-]+$ ]] || die "repo path '$ROOT' has characters a unit path can't hold"

UV=$NAS/bin/uv
XVFB=$NAS/xvfb/root/usr/bin/Xvfb
[[ -x $UV ]] || die "$UV is missing (in-project uv; see README, Running on the NAS)"
[[ -x $XVFB ]] || die "$XVFB is missing (Xvfb extracted from the Debian xvfb .deb)"
compgen -G "$NAS/ms-playwright/chromium-*/chrome-linux64/chrome" >/dev/null \
    || die "no Playwright Chromium under $NAS/ms-playwright"
# Xvfb needs the system xkbcomp; xvfb-helper.sh needs xauth and mcookie.
for tool in /usr/bin/xkbcomp /usr/bin/xauth /usr/bin/mcookie /usr/bin/ldd; do
    [[ -x $tool ]] || die "$tool is missing"
done

# The project dir holds .env and the state: private (UGOS creates dirs 0777).
# .nas/home and .nas/tmp are the service's private HOME and TMPDIR.
chmod 700 -- "$ROOT"
mkdir -p -- "$NAS/home" "$NAS/tmp"
chmod 700 -- "$NAS/home" "$NAS/tmp"

echo "==> Syncing runtime dependencies from uv.lock (in-project uv and Python)"
# Python, the uv cache, uv's temp/lock files and its config lookups stay inside the
# project: UV_NO_CONFIG skips uv.toml files in the world-writable $HOME (pyproject.toml has
# no [tool.uv] to lose).
(
    cd -- "$ROOT"
    export UV_PYTHON_INSTALL_DIR=$NAS/python UV_CACHE_DIR=$NAS/cache/uv TMPDIR=$NAS/tmp
    export UV_PYTHON_PREFERENCE=only-managed UV_NO_CONFIG=1
    "$UV" sync --frozen --no-dev --quiet
)
[[ -x $ROOT/.venv/bin/ticket-poller ]] || die ".venv/bin/ticket-poller missing after uv sync"
python=$(readlink -f -- "$ROOT/.venv/bin/python")
[[ $python == "$NAS/python/"* ]] || die ".venv uses $python, not the Python in $NAS/python"

# The Chromium build must match the Playwright version in uv.lock.
revision=$("$ROOT/.venv/bin/python" - <<'PY'
import json
from pathlib import Path

import playwright

browsers = Path(playwright.__file__).parent / "driver/package/browsers.json"
print(next(b["revision"] for b in json.loads(browsers.read_text())["browsers"]
           if b["name"] == "chromium"))
PY
)
CHROME=$NAS/ms-playwright/chromium-$revision/chrome-linux64/chrome
[[ -x $CHROME ]] || die "Playwright in uv.lock needs $CHROME; install it with:
  PLAYWRIGHT_BROWSERS_PATH=$NAS/ms-playwright $ROOT/.venv/bin/playwright install --no-shell chromium
then point CHROMIUM_PATH in .env at it"
for binary in "$CHROME" "$XVFB"; do
    libs=$(missing_libs "$binary")
    [[ -z $libs ]] || die "$binary is missing shared libraries (a UGOS update?):"$'\n'"$libs"
done

echo "==> Installing ${UNITS[*]} into $UNIT_DIR"
mkdir -p -- "$UNIT_DIR"
# UGOS creates $HOME, ~/repos and new dirs in them 0777 without a sticky bit, whatever the
# umask: another uid could rename the project or unit dir away and plant code that runs as
# us. Take group/other write off every dir below $HOME down to both. Not $HOME itself: on
# UGOS a chmod of it also strips its UGOS ACL (system.ugacl_self), and SSH key login to the
# NAS was refused right after that was done once (README: Running on the NAS). Paths are
# resolved first (symlinks, //, trailing /), so $HOME can't slip through as another spelling.
for path in "$ROOT" "$UNIT_DIR"; do
    dir=$(realpath -m -- "$path")
    while [[ $dir == "$HOME_REAL"/* ]]; do
        if [[ -O $dir ]]; then chmod go-w -- "$dir"; fi
        dir=${dir%/*}
    done
done
if ! "$ROOT/deploy/nas/check-perms.sh" -v "$HOME_REAL" "$ROOT" "$UNIT_DIR"; then
    cat >&2 <<EOF
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
WARNING: the dirs above are still writable by other users (or not owned by you), who
could replace the poller's code or units through them. Fix them by hand (chmod go-w, as
their owner) and re-run; re-check after UGOS updates. Leave \$HOME itself alone: read the
README (Running on the NAS).
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
EOF
fi
staging=$UNIT_DIR/.ticket-poller-staging   # verify needs real unit file names
mkdir -p -- "$staging"
trap 'rm -rf -- "$staging"' EXIT
staged=()
for unit in "${UNITS[@]}"; do
    tmp=$staging/$unit
    if [[ $ROOT == "$HOME_REAL/repos/em_ticket_scrape" ]]; then
        cp -- "$(unit_source "$unit")" "$tmp"
    else
        sed "s|$DEFAULT_ROOT|$ROOT|g" "$(unit_source "$unit")" >"$tmp"
    fi
    chmod 0644 -- "$tmp"
    staged+=("$tmp")
done
# verify exits 0 on ignored keys/values, so any output counts as a failure.
problems=$(systemd-analyze --user verify "${staged[@]}" 2>&1) || true
[[ -z $problems ]] || { echo "$problems" >&2; die "unit failed verification"; }
for unit in "${UNITS[@]}"; do mv -f -- "$staging/$unit" "$UNIT_DIR/$unit"; done
systemctl --user daemon-reload

# Checks that only warn. They never print .env values.
if [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null) != yes ]]; then
    echo "WARNING: linger is off, so the service stops when you log out: loginctl enable-linger"
fi
if [[ ! -f $ROOT/.env ]]; then
    echo "WARNING: $ROOT/.env is missing; copy it from the desktop and add the NAS overrides."
elif [[ $(stat -c '%u %a' -- "$ROOT/.env") != "$(id -u) 600" ]]; then
    echo "WARNING: $ROOT/.env must be yours with mode 600 (run: chmod 600 .env); the service would exit 78."
else
    "$ROOT/.venv/bin/python" - "$ROOT" "$CHROME" 2>/dev/null <<'PY' || echo "WARNING: could not parse $ROOT/.env"
import sys
from pathlib import Path

from dotenv import dotenv_values

root, chrome = Path(sys.argv[1]), Path(sys.argv[2])
env = dotenv_values(root / ".env", encoding="utf-8")


def path(key: str) -> Path | None:
    value = (env.get(key) or "").strip()
    return (root / Path(value).expanduser()).resolve() if value else None


def warn(message: str) -> None:
    print(f"WARNING: .env: {message}")


for key in ("CHROMIUM_PATH", "STATE_DIR", "BROWSER_PROFILE_DIR"):
    if (env.get(key) or "").strip().startswith("~"):
        warn(f"{key} must be an absolute path: the service's HOME is .nas/home")
if path("CHROMIUM_PATH") != chrome.resolve():
    warn(f"CHROMIUM_PATH must be {chrome} (the Chromium matching Playwright)")
for key in ("STATE_DIR", "BROWSER_PROFILE_DIR"):
    value = path(key)
    if value is None or not value.is_relative_to(root):
        warn(f"{key} must be inside {root} (the default is under the world-writable $HOME)")
extra = env.get("BROWSER_EXTRA_ARGS")
if extra is not None and "--ozone-platform=x11" not in extra:
    warn("BROWSER_EXTRA_ARGS must include --ozone-platform=x11")
PY
fi

restart_hint=""
if systemctl --user is-active --quiet "$UNIT"; then
    restart_hint="$UNIT is running: load the new code with  systemctl --user restart $UNIT"
fi
cat <<EOF

Installed. Nothing has been enabled or started. Next steps (README: Running on the NAS):
  cd $ROOT
  systemctl --user start $XVFB_UNIT
  deploy/nas/run.sh poll-once        # dry run, in the service's environment
  deploy/nas/run.sh test-notify      # email + push reach the admin
  deploy/nas/run.sh notify-failure   # the ops push $FAILURE_UNIT sends on failure
  systemctl --user enable --now $XVFB_UNIT $UNIT   # only after the desktop poller is disabled
  journalctl --user -u ticket-poller -f
$FAILURE_UNIT is not enabled: $UNIT starts it via OnFailure=.
${restart_hint:+$restart_hint
}Re-run this script after each push. Uninstall: $0 --uninstall
EOF
