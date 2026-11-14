#!/usr/bin/env bash
# ExecStartPre/ExecStartPost helper for ticket-poller-xvfb.service (NAS variant).
#
#   xvfb-helper.sh prepare DISPLAYNUM AUTHFILE   clear our stale lock, write a fresh cookie
#   xvfb-helper.sh wait DISPLAYNUM               wait until the display's socket exists
#
# Xvfb drops a stale /tmp/.X<n>-lock by itself when the PID in it is dead, but not when that
# PID has been reused by another process ("Server is already active for display"), which
# would wedge the unit in a restart loop. -nolock is root-only, so clear such a lock here:
# only if it is ours and its PID is not a running Xvfb. Other users' files are left alone.
set -euo pipefail

die() { echo "xvfb-helper: $*" >&2; exit 1; }

cmd=${1:-}
display=${2:-}
[[ $display =~ ^[0-9]+$ ]] || die "usage: $0 prepare DISPLAYNUM AUTHFILE | wait DISPLAYNUM"
lock=/tmp/.X$display-lock
socket=/tmp/.X11-unix/X$display

case $cmd in
prepare)
    auth=${3:-}
    [[ -n $auth ]] || die "prepare needs AUTHFILE"
    if [[ -f $lock && ! -L $lock && -O $lock ]]; then
        pid=$(tr -dc '0-9' <"$lock" || true)
        if [[ -n $pid && $(cat "/proc/$pid/comm" 2>/dev/null || true) == Xvfb ]]; then
            die "Xvfb (pid $pid) already serves :$display; stop it first"
        fi
        echo "removing stale $lock (pid ${pid:-none} is not a running Xvfb)"
        rm -f -- "$lock"
    fi
    # No lock means no server: a socket of ours left by a killed Xvfb is stale.
    if [[ ! -e $lock && -S $socket && -O $socket ]]; then
        rm -f -- "$socket"
    fi
    # A fresh MIT cookie on each start: only clients that can read AUTHFILE (in the 0700
    # project dir) may connect, including over the abstract socket.
    umask 077
    rm -f -- "$auth"
    : >"$auth"
    xauth -q -f "$auth" add ":$display" MIT-MAGIC-COOKIE-1 "$(mcookie)"
    ;;
wait)
    for _ in $(seq 100); do
        [[ -S $socket ]] && exit 0
        sleep 0.1
    done
    die "$socket did not appear within 10 s"
    ;;
*)
    die "unknown command '$cmd'"
    ;;
esac
