#!/usr/bin/env bash
# ExecStop helper for ticket-poller.service.
#
# The unit's main process is systemd-inhibit, which does not forward SIGTERM to the
# poller: it just dies, and with KillMode=mixed systemd then SIGKILLs the rest of the
# cgroup, so the poller never gets to close Chromium or finish a send. Instead, SIGTERM
# the poller (systemd-inhibit's only child) and wait until it has exited. systemd-inhibit
# then exits with the poller's status. TimeoutStopSec bounds the wait.
set -euo pipefail

main=${MAINPID:-}
[[ -n $main ]] || exit 0                  # already exited on its own (event over, crash)
children=$(pgrep -P "$main") || exit 0    # nothing left to stop
for pid in $children; do
    kill -TERM "$pid" 2>/dev/null || true
done
for pid in $children; do
    while kill -0 "$pid" 2>/dev/null; do
        sleep 0.2
    done
done
