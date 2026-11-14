#!/usr/bin/env bash
# Run a ticket-poller command on the NAS by hand, in the same environment as
# ticket-poller.service: e.g. deploy/nas/run.sh poll-once, deploy/nas/run.sh test-notify.
# Needs ticket-poller-xvfb.service running for the commands that open Chromium.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
NAS=$ROOT/.nas

[[ -x $ROOT/.venv/bin/ticket-poller ]] || { echo "run deploy/nas/install.sh first" >&2; exit 1; }
cd -- "$ROOT"
# Keep these in sync with the Environment= lines in ticket-poller.service.
export PYTHONUNBUFFERED=1 DISPLAY=:99 XAUTHORITY=$NAS/xauthority HOME=$NAS/home TMPDIR=$NAS/tmp
umask 077
exec "$ROOT/.venv/bin/ticket-poller" "$@"
