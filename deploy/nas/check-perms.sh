#!/usr/bin/env bash
# Check that no directory below $HOME down to the given paths can be written by others.
# On UGOS $HOME and dirs created directly in it come out 0777 (no sticky bit), so another
# uid could rename ~/repos/em_ticket_scrape or ~/.config away and plant code or units that
# would run as us. install.sh fixes the dirs below $HOME with chmod go-w; this re-checks.
#
#   check-perms.sh [--journal] [-v] HOME PATH...
#
# Prints one WARNING per bad directory and exits 1 if there is any: no output and exit 0
# mean OK. $HOME itself is not counted: it stays 0777 on UGOS by design (README: Running
# on the NAS); -v prints a NOTE about it. --journal prefixes the lines with a syslog level,
# for an ExecStartPre= whose failure is ignored.
set -euo pipefail

usage() { echo "usage: $0 [--journal] [-v] HOME PATH..." >&2; exit 2; }
journal=0 verbose=0
while [[ ${1:-} == -* ]]; do
    case $1 in
    --journal) journal=1 ;;
    -v) verbose=1 ;;
    *) usage ;;
    esac
    shift
done
[[ $# -ge 2 ]] || usage
home=$(realpath -m -- "$1")
warn_prefix='' note_prefix=''
if (( journal )); then warn_prefix='<4>' note_prefix='<5>'; fi
shift

writable_by_others() {   # prints "MODE UID" if DIR is group/other-writable or not ours
    local mode uid
    read -r mode uid < <(stat -L -c '%a %u' -- "$1")
    if (( 8#$mode & 8#022 )) || [[ $uid != "$EUID" ]]; then echo "$mode $uid"; fi
}

bad=0
declare -A seen=()
for path in "$@"; do
    dir=$(realpath -m -- "$path")
    while [[ $dir != "$home" ]]; do
        if [[ -z ${seen[$dir]:-} && -e $dir ]]; then
            seen[$dir]=1
            found=$(writable_by_others "$dir")
            if [[ -n $found ]]; then
                read -r mode uid <<<"$found"
                echo "${warn_prefix}WARNING: $dir is mode $mode, owner uid $uid: others could replace" \
                    "what is below it (README: Running on the NAS)" >&2
                bad=1
            fi
        fi
        [[ $dir == "$home"/* ]] || break
        dir=${dir%/*}
    done
done
if (( verbose )) && [[ -e $home ]]; then
    found=$(writable_by_others "$home")
    if [[ -n $found ]]; then
        read -r mode uid <<<"$found"
        echo "${note_prefix}NOTE: $home is mode $mode, owner uid $uid, as UGOS sets it; expected, not" \
            "fixed (README: Running on the NAS)" >&2
    fi
fi
exit "$bad"
