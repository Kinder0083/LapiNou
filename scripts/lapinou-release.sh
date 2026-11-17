#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Assemble and sign a LapiNou release (Task 26; docs/lapinou/release.md, docs/lapinou/ota.md).
#
#   lapinou-release.sh check-key --signing-key KEY [--pubkey PUB ...]
#   lapinou-release.sh assemble  --version vX.Y.Z --firmware DIR --signing-key KEY
#                                --out DIST [--pubkey PUB ...]
#
# check-key: KEY (the private key, PEM, from the OTA_SIGNING_KEY secret) is an Ed25519 key whose
# public key is each PUB: by default the two committed files that the rabbits
# (image/core/usr/share/lapinou/ota-pubkey.pub) and the server (server/lapinou/ota-pubkey.pub)
# verify with. A placeholder PUB is refused: a release signed now would be refused everywhere.
#
# assemble: check-key, then DIST (new or empty) gets the release assets (R41, R42, R44):
#   zImage, rootfs.img.gz, usr.tar.gz and lapinou-key-vX.Y.Z.zip, copied from DIR (a built key's
#   images and its install.conf, whose RELEASE_VERSION must be vX.Y.Z, and the zipped key);
#   lapinou-server-vX.Y.Z.tar.gz, the server's files at the repository's HEAD (git archive);
#   manifest.json and manifest.json.sig, by tools/sign-ota.py, listing all of the above,
#   then checked by tools/sign-ota.py --verify with each PUB.
# Nothing else is left in DIST. On any failure DIST is emptied: nothing unsigned is published.
# The key is never printed. Exit status: 0 good, 1 a check failed, 2 usage.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMAGES=(zImage rootfs.img.gz usr.tar.gz)
# What the server archive holds: the server and what it imports or reads (R4), the installer,
# the docs and the licences.
SERVER_PATHS=(server tools/app-server userland/proto installer docs/lapinou LICENSES)

die() { echo "lapinou-release: $*" >&2; exit 1; }
usage() { sed -n '4,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

MODE=${1:-}
[ "$MODE" = check-key ] || [ "$MODE" = assemble ] || usage
shift
VERSION="" FIRMWARE="" KEY="" OUT="" PUBKEYS=() VERSION_SET=0
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || usage
    case $1 in
        --version) VERSION=$2 VERSION_SET=1 ;;
        --firmware) FIRMWARE=$2 ;;
        --signing-key) KEY=$2 ;;
        --out) OUT=$2 ;;
        --pubkey) PUBKEYS+=("$2") ;;
        *) usage ;;
    esac
    shift 2
done
[ -n "$KEY" ] || usage
[ ${#PUBKEYS[@]} -gt 0 ] || PUBKEYS=("$REPO/image/core/usr/share/lapinou/ota-pubkey.pub" "$REPO/server/lapinou/ota-pubkey.pub")

check_key() {
    [ -s "$KEY" ] || die "no signing key ($KEY is missing or empty): set the repository secret OTA_SIGNING_KEY (docs/lapinou/release.md)"
    local derived committed p
    derived=$(openssl pkey -in "$KEY" -pubout 2>/dev/null) ||
        die "OTA_SIGNING_KEY isn't a private key openssl can read (PEM, Ed25519)"
    openssl pkey -pubin -noout -text_pub 2>/dev/null <<<"$derived" | head -n 1 | grep -q '^ED25519 ' ||
        die "OTA_SIGNING_KEY isn't an Ed25519 key"
    for p in "${PUBKEYS[@]}"; do
        [ -f "$p" ] || die "no public key $p"
        ! grep -q PLACEHOLDER "$p" ||
            die "$p is still the PLACEHOLDER: commit the real public key first (docs/lapinou/release.md)"
        committed=$(openssl pkey -pubin -in "$p" -pubout 2>/dev/null) || die "$p isn't a public key openssl can read"
        [ "$committed" = "$derived" ] ||
            die "$p isn't the public key of OTA_SIGNING_KEY: the rabbits and the server would refuse the release"
    done
    echo "signing key: Ed25519, matches ${#PUBKEYS[@]} public key file(s)"
}

if [ "$MODE" = check-key ]; then
    if [ -n "$FIRMWARE$OUT" ] || [ "$VERSION_SET" = 1 ]; then usage; fi
    check_key
    exit 0
fi

if [ -z "$FIRMWARE" ] || [ -z "$OUT" ] || [ "$VERSION_SET" = 0 ]; then usage; fi
[[ $VERSION =~ ^v[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}$ ]] || die "--version '$VERSION' isn't a release tag vX.Y.Z (R44)"
KEY_ZIP="lapinou-key-$VERSION.zip"
SERVER="lapinou-server-$VERSION.tar.gz"
check_key

missing=()
for f in "${IMAGES[@]}" install.conf "$KEY_ZIP"; do
    [ -f "$FIRMWARE/$f" ] || missing+=("$f")
done
[ ${#missing[@]} -eq 0 ] || die "$FIRMWARE lacks ${missing[*]}"
built=$(sed -n 's/^RELEASE_VERSION=//p' "$FIRMWARE/install.conf" | tail -n 1)
[ "$built" = "$VERSION" ] ||
    die "the images were built as RELEASE_VERSION=${built:-none}, not $VERSION: build them from the tag"

if [ -e "$OUT" ]; then
    if [ ! -d "$OUT" ] || [ -n "$(ls -A "$OUT")" ]; then die "$OUT exists and isn't an empty folder"; fi
fi
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
done_ok=0
# shellcheck disable=SC2317  # run by the trap
cleanup() { [ "$done_ok" = 1 ] || find "$OUT" -mindepth 1 -delete; }
trap cleanup EXIT

for f in "${IMAGES[@]}" "$KEY_ZIP" install.conf; do
    cp -- "$FIRMWARE/$f" "$OUT/$f"
done
git -C "$REPO" archive --format=tar.gz --prefix="lapinou-server-$VERSION/" -o "$OUT/$SERVER" HEAD -- "${SERVER_PATHS[@]}"

files=()
for f in "${IMAGES[@]}" "$KEY_ZIP" "$SERVER"; do files+=(--file "$f"); done
# install.conf is in DIST while signing only: sign-ota.py checks its RELEASE_VERSION again.
python3 -I "$REPO/tools/sign-ota.py" --key "$KEY" --dir "$OUT" --version "$VERSION" "${files[@]}"
rm -f -- "$OUT/install.conf"
for p in "${PUBKEYS[@]}"; do
    python3 -I "$REPO/tools/sign-ota.py" --verify --pubkey "$p" --dir "$OUT"
done
done_ok=1
ls -l "$OUT"
