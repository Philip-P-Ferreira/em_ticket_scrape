#!/usr/bin/env bash
# Install (or update) the ticket-poller systemd user unit. Idempotent; safe to re-run.
# It syncs the locked runtime deps, installs the unit and reloads systemd, but never
# enables, starts or restarts the service.
#
#   deploy/install.sh              install / update
#   deploy/install.sh --uninstall  disable + stop the service and remove the unit
set -euo pipefail

UNIT=ticket-poller.service
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
DEST=$UNIT_DIR/$UNIT
DEFAULT_ROOT='%h/repos/em_ticket_scrape'   # the path written in the template

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "run as your desktop user, not root"

if [[ ${1:-} == --uninstall ]]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f -- "$DEST"
    systemctl --user daemon-reload
    systemctl --user reset-failed "$UNIT" 2>/dev/null || true
    echo "Removed $DEST. State and the browser profile in ~/.local/share/em_ticket_scrape are kept."
    exit 0
fi
[[ $# -eq 0 ]] || die "usage: $0 [--uninstall]"

# The repo path is substituted into the unit with sed and must be a plain path.
[[ $ROOT =~ ^[A-Za-z0-9_./-]+$ ]] || die "repo path '$ROOT' has characters a unit path can't hold"

UV=${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}
[[ -x $UV ]] || die "uv not found (install it, or set UV=/path/to/uv)"
for tool in /usr/bin/chromium /usr/bin/systemd-inhibit /usr/bin/pgrep; do
    [[ -x $tool ]] || die "$tool is missing"
done

echo "==> Syncing runtime dependencies from uv.lock"
# --inexact keeps dev tools that may already be in .venv; they are not installed here.
(cd -- "$ROOT" && "$UV" sync --frozen --no-dev --inexact --quiet)
[[ -x $ROOT/.venv/bin/ticket-poller ]] || die ".venv/bin/ticket-poller missing after uv sync"

echo "==> Installing $DEST"
staging=$UNIT_DIR/.ticket-poller-staging   # verify needs a real unit file name
mkdir -p -- "$staging"
tmp=$staging/$UNIT
trap 'rm -rf -- "$staging"' EXIT
if [[ $ROOT == "$HOME/repos/em_ticket_scrape" ]]; then
    cp -- "$ROOT/deploy/$UNIT" "$tmp"
else
    sed "s|$DEFAULT_ROOT|$ROOT|g" "$ROOT/deploy/$UNIT" >"$tmp"
fi
chmod 0644 -- "$tmp"
# verify exits 0 on ignored keys/values, so any output counts as a failure.
problems=$(systemd-analyze --user verify "$tmp" 2>&1) || true
[[ -z $problems ]] || { echo "$problems" >&2; die "unit failed verification"; }
mv -f -- "$tmp" "$DEST"
systemctl --user daemon-reload

if [[ ! -f $ROOT/.env ]]; then
    echo "WARNING: $ROOT/.env is missing; copy .env.example and fill it in."
elif [[ $(stat -c '%u %a' -- "$ROOT/.env") != "$(id -u) 600" ]]; then
    echo "WARNING: $ROOT/.env must be yours with mode 600 (run: chmod 600 .env); the service would exit 78."
fi

cat <<EOF

Installed. The service has NOT been enabled or started. Next steps:
  cd $ROOT
  uv run ticket-poller test-notify     # email + push reach the admin
  uv run ticket-poller poll-once       # dry run; compare prices with the browser
  systemctl --user enable --now $UNIT
  journalctl --user -u ticket-poller -f
Re-run this script after editing deploy/$UNIT. Uninstall: $0 --uninstall
EOF
