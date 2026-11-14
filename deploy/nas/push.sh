#!/usr/bin/env bash
# Copy this checkout (working tree, including uncommitted edits) to the NAS. The NAS rsync
# is UGREEN-restricted, so this streams a tar over ssh.
#
#   deploy/nas/push.sh            then, on the NAS: deploy/nas/install.sh
#
# Sent: the files git tracks or would track (.gitignore applies), minus NAS-local paths.
# Never sent, and never replaced or deleted on the NAS: .git, .env*, .venv, .nas, state,
# caches. Every top-level entry that IS sent (src, tests, deploy, …) replaces the NAS copy
# whole, so files deleted here disappear there too.
#
# Environment: NAS_HOST (default pferreira@phils-nas.local), NAS_DIR (default
# repos/em_ticket_scrape, relative to the NAS home; the units assume that path).
set -euo pipefail

HOST=${NAS_HOST:-pferreira@phils-nas.local}
DEST=${NAS_DIR:-repos/em_ticket_scrape}
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)

die() { echo "error: $*" >&2; exit 1; }

[[ $# -eq 0 ]] || die "usage: $0 (set NAS_HOST / NAS_DIR to override the target)"
[[ $DEST =~ ^[A-Za-z0-9_][A-Za-z0-9_./-]*$ && $DEST != *..* ]] || die "bad NAS_DIR '$DEST'"

# Top-level names that are NAS-local or secret. Checked here and again on the NAS.
excluded='^(\.git|\.env|\.env\..*|\.venv|\.nas|state|\.state|debug|\.probe|\.push-staging|\.pytest_cache|\.mypy_cache|\.ruff_cache|htmlcov|build|dist)(/|$)'

cd -- "$ROOT"
mapfile -d '' files < <(git ls-files -z --cached --others --exclude-standard)
send=()
for file in "${files[@]}"; do
    [[ $file == .env.example ]] || [[ ! $file =~ $excluded ]] || continue
    [[ $file =~ (^|/)__pycache__/ || $file == *.py[cod] ]] && continue
    [[ -e $file || -L $file ]] || continue   # deleted but still tracked
    send+=("$file")
done
((${#send[@]})) || die "nothing to send"
echo "==> Sending ${#send[@]} files from $ROOT to $HOST:$DEST"

# Runs on the NAS: unpack into a staging dir inside the 0700 project dir, then swap each
# sent top-level entry in. NAS-local entries are skipped even if they were sent.
read -r -d '' remote <<'EOF' || true
set -euo pipefail
dest=$1
umask 077
mkdir -p -- "$dest"
chmod 700 -- "$dest"
cd -- "$dest"
stage=.push-staging
rm -rf -- "$stage"
mkdir -- "$stage"
tar -x -f - -C "$stage" --no-same-owner
# A truncated stream (the local tar failed) must not replace anything.
count=$(find "$stage" ! -type d | wc -l)
if [[ $count -ne $2 ]]; then
    rm -rf -- "$stage"
    echo "error: received $count files, expected $2; nothing changed" >&2
    exit 1
fi
shopt -s dotglob nullglob
for entry in "$stage"/*; do
    name=${entry##*/}
    case $name in
        .env.example) ;;
        .git|.env|.env.*|.venv|.nas|state|.state|debug|.probe|.push-staging)
            echo "skipped NAS-local $name" >&2; continue ;;
    esac
    rm -rf -- "./$name"
    mv -- "$entry" "./$name"
done
rm -rf -- "$stage"
echo "updated $PWD"
EOF
encoded=$(printf '%s' "$remote" | base64 -w0)

printf '%s\0' "${send[@]}" \
    | tar -c -f - --no-recursion --owner=0 --group=0 --numeric-owner --null -T - \
    | ssh -o BatchMode=yes "$HOST" "bash -c \"\$(echo $encoded | base64 -d)\" push '$DEST' ${#send[@]}"

cat <<EOF

Next, on the NAS:
  ssh $HOST
  cd ~/$DEST && deploy/nas/install.sh
  systemctl --user restart ticket-poller   # only if it is running, to load the new code
EOF
