#!/bin/sh
# CHROMIUM_PATH on the NAS: start Debian 12's Chromium that deploy/nas/fetch-chromium.sh
# extracted into .nas/chromium/ (a link to .nas/chromium-<version>/). The few libraries it
# needs that UGOS lacks sit next to it, hence LD_LIBRARY_PATH (inherited by Chromium's
# child processes). Debian's own /usr/bin/chromium launcher is skipped on purpose: it adds
# flags (/etc/chromium.d) that the desktop's Arch Chromium doesn't use.
root=$(cd -- "$(dirname -- "$(readlink -f -- "$0")")/../.." && pwd -P)/.nas/chromium/root
LD_LIBRARY_PATH=$root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export LD_LIBRARY_PATH
exec "$root/usr/lib/chromium/chromium" "$@"
