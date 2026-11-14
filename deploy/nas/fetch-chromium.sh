#!/usr/bin/env bash
# Fetch Debian 12's Chromium into .nas/ on the NAS, without apt/dpkg (UGOS's dpkg state is
# broken: never apt install there). Run it ON the NAS from the repo checkout.
#
#   deploy/nas/fetch-chromium.sh            newest bookworm-security build
#   deploy/nas/fetch-chromium.sh VERSION    a given one, e.g. 156.0.8078.79-1~deb12u1
#
# Why not Playwright's Chrome for Testing: the NAS was hard-blocked by Ticketmaster
# ("Your Browsing Activity Has Been Paused") on its first request with it, while a distro
# Chromium build of the desktop's version passed (README: Running on the NAS). Keep the
# NAS on the same major version as the desktop's Chromium where possible.
#
# It extracts the chromium and chromium-common .debs, plus the few libraries UGOS lacks
# (fetched with `apt-get download`, which needs no root and doesn't touch dpkg), into
# .nas/chromium-<version>/root, checks that everything loads, then points the .nas/chromium
# link at it. deploy/nas/chromium.sh (CHROMIUM_PATH in .env) starts it. A running poller
# picks up the new build at its next browser launch; restart it to switch right away.
set -euo pipefail

POOL=https://security.debian.org/debian-security/pool/updates/main/c/chromium
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
NAS=$ROOT/.nas
# Libraries Debian's chromium links against that UGOS doesn't ship, and their packages.
declare -A PACKAGE_OF=(
    [libdouble-conversion.so.3]=libdouble-conversion3
    [libharfbuzz-subset.so.0]=libharfbuzz-subset0
    [libminizip.so.1]=libminizip1
    [libopenh264.so.7]=libopenh264-7
)

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "run as the NAS user, not root"
[[ $# -le 1 ]] || die "usage: $0 [VERSION]"
for tool in curl dpkg-deb dpkg-query apt-get ldd; do
    command -v "$tool" >/dev/null || die "$tool is missing"
done

version=${1:-}
if [[ -z $version ]]; then
    version=$(curl -fsSL "$POOL/" \
        | grep -oE 'chromium_[0-9][^"<>/]*~deb12u[0-9]+_amd64\.deb' \
        | sed -E 's/^chromium_(.*)_amd64\.deb$/\1/' | sort -uV | tail -n 1) || true
    [[ -n $version ]] || die "found no bookworm chromium in $POOL"
fi
[[ $version =~ ^[0-9][0-9.]*-[0-9A-Za-z.+~]+$ ]] || die "bad version '$version'"
[[ ! -e $NAS/chromium || -L $NAS/chromium ]] || die "$NAS/chromium exists and is not a link"

dest=$NAS/chromium-$version
link() {
    ln -sfn -- "chromium-$version" "$NAS/chromium"
    "$ROOT/deploy/nas/chromium.sh" --version >/dev/null || die "deploy/nas/chromium.sh can't start it"
    echo "CHROMIUM_PATH in .env: $ROOT/deploy/nas/chromium.sh"
}
if [[ -x $dest/root/usr/lib/chromium/chromium ]]; then
    # Never rewrite a build in place: a running poller's Chromium may be using it.
    link
    echo "chromium $version was already in $dest; $NAS/chromium now points at it."
    exit 0
fi

umask 077
mkdir -p -- "$NAS/tmp"
work=$(mktemp -d -- "$NAS/tmp/fetch-chromium.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
mkdir -- "$work/debs" "$work/root"
cd -- "$work/debs"

echo "==> Downloading chromium $version from $POOL"
for package in chromium chromium-common; do
    curl -fsSLO "$POOL/${package}_${version}_amd64.deb" \
        || die "cannot download ${package}_${version}_amd64.deb (the pool keeps only current builds)"
done
for deb in ./*.deb; do dpkg-deb -x "$deb" "$work/root"; done

binary=$work/root/usr/lib/chromium/chromium
libdir=$work/root/usr/lib/x86_64-linux-gnu
missing() { LD_LIBRARY_PATH=$libdir ldd "$binary" | awk '/not found/ {print $1}'; }

packages=()
for lib in $(missing); do
    package=${PACKAGE_OF[$lib]:-}
    [[ -n $package ]] || die "chromium needs $lib, which UGOS lacks and this script doesn't know"
    if [[ $package == libharfbuzz-subset0 ]]; then
        # Must be the same build as the system's libharfbuzz0b.
        package+="=$(dpkg-query -W -f '${Version}' libharfbuzz0b)"
    fi
    packages+=("$package")
done
if ((${#packages[@]})); then
    echo "==> Downloading the libraries UGOS lacks: ${packages[*]}"
    mkdir -- "$work/libs"
    (cd -- "$work/libs" && apt-get download -q "${packages[@]}" >/dev/null)
    for deb in "$work"/libs/*.deb; do dpkg-deb -x "$deb" "$work/root"; done
fi
still=$(missing)
[[ -z $still ]] || die "chromium still misses libraries:"$'\n'"$still"
reported=$(LD_LIBRARY_PATH=$libdir "$binary" --version) || die "chromium doesn't start"

rm -rf -- "$dest"
mkdir -p -- "$dest"
mv -- "$work/root" "$dest/root"
link
echo "Installed $reported in $dest, linked from $NAS/chromium."
others=$(find "$NAS" -maxdepth 1 -name 'chromium-*' ! -name "chromium-$version" -printf '%f ')
[[ -z $others ]] || echo "Older builds you can delete: $others"
