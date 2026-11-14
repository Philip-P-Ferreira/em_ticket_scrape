#!/usr/bin/env bash
# Check that no directory from $HOME down to the given paths can be written by others.
# On UGOS $HOME and dirs created directly in it come out 0777 (no sticky bit), so another
# uid could rename ~/repos/em_ticket_scrape or ~/.config away and plant code or units that
# would run as us. install.sh fixes the dirs below $HOME with chmod go-w; this re-checks.
#
#   check-perms.sh [--journal] HOME PATH...
#
# Prints one line per bad directory and exits 1 if there is any. --journal prefixes the
# lines with a syslog "warning" level, for an ExecStartPre= whose failure is ignored.
set -euo pipefail

prefix=""
if [[ ${1:-} == --journal ]]; then prefix="<4>"; shift; fi
[[ $# -ge 2 ]] || { echo "usage: $0 [--journal] HOME PATH..." >&2; exit 2; }
home=${1%/}
shift

bad=0
declare -A seen=()
for path in "$@"; do
    dir=${path%/}
    while :; do
        if [[ -z ${seen[$dir]:-} && -e $dir ]]; then
            seen[$dir]=1
            read -r mode uid < <(stat -L -c '%a %u' -- "$dir")
            if (( 8#$mode & 8#022 )) || [[ $uid != "$EUID" ]]; then
                echo "${prefix}WARNING: $dir is mode $mode, owner uid $uid: others could replace" \
                    "what is below it (README: Running on the NAS)" >&2
                bad=1
            fi
        fi
        [[ $dir == "$home" || $dir != "$home"/* ]] && break
        dir=${dir%/*}
    done
done
exit "$bad"
