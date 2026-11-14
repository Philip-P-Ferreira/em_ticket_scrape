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
# It downloads the chromium and chromium-common .debs and verifies them through Debian's
# signed chain (InRelease checked with gpgv against the system's Debian archive keyring, then
# the SHA256 of Packages.xz, then the SHA256 of each .deb), extracts them plus the few
# libraries UGOS lacks (fetched with `apt-get download`, which verifies them the same way,
# needs no root and doesn't touch dpkg) into .nas/chromium-<version>/root, checks that the
# build loads and starts, and only then points the .nas/chromium link at it. Re-running it
# for a version that is already there re-checks that build before relinking. deploy/nas/chromium.sh (CHROMIUM_PATH in .env) starts it. A running poller
# picks up the new build at its next browser launch; restart it to switch right away.
set -euo pipefail

ARCHIVE=https://security.debian.org/debian-security
SUITE=bookworm-security
PACKAGES_INDEX=main/binary-amd64/Packages.xz
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
for tool in curl gpgv xz sha256sum awk dpkg-deb dpkg-query apt-get ldd; do
    command -v "$tool" >/dev/null || die "$tool is missing"
done
keyrings=()
for keyring in /usr/share/keyrings/debian-archive-keyring.gpg \
    /usr/share/keyrings/debian-archive-bookworm-{security-automatic,automatic,stable}.gpg; do
    [[ -f $keyring ]] && keyrings+=(--keyring "$keyring")
done
((${#keyrings[@]})) || die "no Debian archive keyring in /usr/share/keyrings"
[[ ! -e $NAS/chromium || -L $NAS/chromium ]] || die "$NAS/chromium exists and is not a link"

# Prints "lib1 lib2 ..." that the chromium under $1 (an extracted root) can't load.
missing() {
    LD_LIBRARY_PATH=$1/usr/lib/x86_64-linux-gnu ldd "$1/usr/lib/chromium/chromium" \
        | awk '/not found/ {print $1}'
}
# Checks that the chromium under $1 loads all its libraries and starts; prints its version.
check_build() {
    local still
    [[ -x $1/usr/lib/chromium/chromium ]] || die "no chromium binary in $1"
    still=$(missing "$1") || die "ldd failed on $1/usr/lib/chromium/chromium"
    [[ -z $still ]] || die "chromium in $1 misses libraries:"$'\n'"$still"
    LD_LIBRARY_PATH=$1/usr/lib/x86_64-linux-gnu "$1/usr/lib/chromium/chromium" --version \
        || die "chromium in $1 doesn't start"
}
# Points .nas/chromium at the (already checked) chromium-$version, and back if the
# wrapper then can't start it.
link() {
    local previous
    previous=$(readlink -- "$NAS/chromium" || true)
    ln -sfn -- "chromium-$version" "$NAS/chromium"
    if ! "$ROOT/deploy/nas/chromium.sh" --version >/dev/null; then
        if [[ -n $previous ]]; then ln -sfn -- "$previous" "$NAS/chromium"; else rm -f -- "$NAS/chromium"; fi
        die "deploy/nas/chromium.sh can't start chromium-$version; .nas/chromium left at '${previous:-nothing}'"
    fi
    echo "CHROMIUM_PATH in .env: $ROOT/deploy/nas/chromium.sh"
}

umask 077
mkdir -p -- "$NAS/tmp"
work=$(mktemp -d -- "$NAS/tmp/fetch-chromium.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

version=${1:-}
dest=
if [[ -n $version ]]; then
    [[ $version =~ ^[0-9][0-9.]*-[0-9A-Za-z.+~]+$ ]] || die "bad version '$version'"
    dest=$NAS/chromium-$version
fi
if [[ -z $version || ! -e $dest ]]; then
    echo "==> Verifying $ARCHIVE $SUITE (InRelease, gpgv)"
    mkdir -m 700 -- "$work/gnupg"
    curl -fsSL --proto =https -o "$work/InRelease" "$ARCHIVE/dists/$SUITE/InRelease" \
        || die "cannot download $SUITE's InRelease"
    gpgv --homedir "$work/gnupg" "${keyrings[@]}" --output "$work/Release" "$work/InRelease" \
        2>"$work/gpgv.log" || { cat -- "$work/gpgv.log" >&2; die "$SUITE's InRelease signature doesn't verify"; }
    codename=$(awk -F': ' '$1 == "Codename" {print $2}' "$work/Release")
    [[ $codename == "$SUITE" ]] || die "signed Release is for '$codename', not $SUITE"
    valid_until=$(awk -F': ' '$1 == "Valid-Until" {print $2}' "$work/Release")
    [[ -n $valid_until ]] || die "signed Release has no Valid-Until"
    (($(date -d "$valid_until" +%s) > $(date +%s))) || die "signed Release expired on $valid_until (stale mirror?)"
    index_sha=$(awk -v f="$PACKAGES_INDEX" '/^[^ ]/ {in_sha = ($1 == "SHA256:")} in_sha && $3 == f {print $1}' "$work/Release")
    [[ $index_sha =~ ^[0-9a-f]{64}$ ]] || die "no SHA256 for $PACKAGES_INDEX in the signed Release"
    curl -fsSL --proto =https -o "$work/Packages.xz" "$ARCHIVE/dists/$SUITE/$PACKAGES_INDEX" \
        || die "cannot download $PACKAGES_INDEX"
    echo "$index_sha  $work/Packages.xz" | sha256sum -c --quiet - || die "$PACKAGES_INDEX doesn't match the signed Release"
    xz -dc -- "$work/Packages.xz" >"$work/Packages"
    # Prints "Version Filename SHA256" for each stanza of package $1.
    stanzas() {
        awk -v p="$1" 'BEGIN {RS = ""; FS = "\n"} {
            pk = v = f = h = ""
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^Package: /) pk = substr($i, 10)
                else if ($i ~ /^Version: /) v = substr($i, 10)
                else if ($i ~ /^Filename: /) f = substr($i, 11)
                else if ($i ~ /^SHA256: /) h = substr($i, 9)
            }
            if (pk == p) print v, f, h
        }' "$work/Packages"
    }
    if [[ -z $version ]]; then
        version=$(stanzas chromium | awk '{print $1}' | sort -V | tail -n 1)
        [[ $version =~ ^[0-9][0-9.]*-[0-9A-Za-z.+~]+$ ]] || die "found no chromium in $SUITE"
        dest=$NAS/chromium-$version
    fi
fi

if [[ -e $dest ]]; then
    # Never rewrite a build in place: a running poller's Chromium may be using it.
    echo "==> chromium $version is already in $dest; checking it"
    check_build "$dest/root" >/dev/null
    link
    echo "$NAS/chromium points at the checked chromium-$version."
    exit 0
fi

echo "==> Downloading chromium $version from $ARCHIVE (SHA256 from the signed Packages)"
mkdir -- "$work/debs" "$work/root"
: >"$work/SHA256SUMS"
for package in chromium chromium-common; do
    read -r filename sha < <(stanzas "$package" | awk -v v="$version" '$1 == v {print $2, $3}') \
        || die "$SUITE has no $package $version (the archive keeps only current builds)"
    [[ $filename == pool/*/"${package}_${version}_amd64.deb" && $sha =~ ^[0-9a-f]{64}$ ]] \
        || die "unexpected Packages entry for $package $version: '$filename' '$sha'"
    deb=$work/debs/${filename##*/}
    curl -fsSL --proto =https -o "$deb" "$ARCHIVE/$filename" || die "cannot download $filename"
    echo "$sha  $deb" | sha256sum -c --quiet - || die "${filename##*/} doesn't match the signed Packages SHA256"
    echo "$sha  ${filename##*/}" >>"$work/SHA256SUMS"
    dpkg-deb -x "$deb" "$work/root"
done

packages=()
for lib in $(missing "$work/root"); do
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
    for deb in "$work"/libs/*.deb; do
        dpkg-deb -x "$deb" "$work/root"
        (cd -- "$work/libs" && sha256sum -- "${deb##*/}") >>"$work/SHA256SUMS"
    done
fi
reported=$(check_build "$work/root")

mkdir -p -- "$dest"
mv -- "$work/root" "$dest/root"
mv -- "$work/SHA256SUMS" "$dest/SHA256SUMS"
check_build "$dest/root" >/dev/null
link
echo "Installed $reported in $dest (verified .debs listed in SHA256SUMS), linked from $NAS/chromium."
others=$(find "$NAS" -maxdepth 1 -name 'chromium-*' ! -name "chromium-$version" -printf '%f ')
[[ -z $others ]] || echo "Older builds you can delete: $others"
