#!/bin/sh
# SPDX-License-Identifier: MIT
# lapinou-ota: LapiNou's signed updates on the rabbit (Task 11; docs/lapinou/ota.md).
#
#   lapinou-ota check       print {"installed":"...","available":"..."} (the signed manifest's version)
#   lapinou-ota install [--allow-downgrade]
#                           download, check, then write mtd1, mtd2 and /usr, and reboot
#
# Versions (R44): a release is vX.Y.Z (the manifest's version, the release's tag); the rabbit's
# sys_version is the tag it was installed from, or 0.0.0-<commit> for a build that isn't a release
# (anything else, from before R44, counts as 0.0.0). A version lower than the installed one is
# refused unless --allow-downgrade is given; the same version, or the same build (the build id the
# manifest's SHA-256s give equals lapinlibre_build_id), is not installed again.
#
# Where an update comes from (lapinou.conf):
#   [ota] url, else http://<[server] host>:8080/ota/ when [server] enabled=1: a LapiNou server's
#     relay. The rabbit reads <url>manifest.json and <url>manifest.json.sig, checks the signature,
#     then reads each file from <url><version>/<name>, <version> being the manifest's.
#   Otherwise GitHub: the latest release of [ota] repo (Kinder0083/LapiNou by default), HTTPS only.
#     ota_token (lapinou.secrets) for a private repository goes to api.github.com only, never to the
#     storage host the API redirects the downloads to; the release's tag must be the manifest's
#     version.
#
# manifest.json is {"version": "...", "files": {"<name>": {"sha256": "<hex>", "size": N}, ...}};
# manifest.json.sig is the raw 64-byte Ed25519 signature of its exact bytes (R42), checked with
# /usr/share/lapinou/ota-pubkey.pub (PEM). While that file is the placeholder, nothing is read
# from the network and nothing installs. The rabbit takes zImage (mtd1), rootfs.img.gz (mtd2) and
# usr.tar.gz (/usr); the other files listed (the USB key's archive) aren't its to download.
#
# The files go to /usr/ota/new, on the flash (not in RAM). Before the writing, any failure removes
# them, and what an interrupted run left there is removed by the next one. Nothing is written before
# the signature, the version, every size and SHA-256, both archives' decompression and the
# partitions' and RAM disk's sizes check out. Then, as the USB key's install does (install-lib.sh,
# in the root filesystem): LapinLibre's daemons are stopped; mtd1, then mtd2, erased, written and
# read back (nanddump) against the checked SHA-256, written once more if it differs, and the
# install stops there if it differs twice (never any other partition). Only then /usr: the rabbit's
# karotz.conf and voice.conf are kept in /usr/ota/new, /usr/ota/resume.conf is written, and the root
# filesystem's ota-resume.sh replaces /usr (keeping /usr/etc/conf, the apps, the messages and
# uuid.conf), records the version and build id and removes the downloads; the rabbit reboots. If
# the power goes during /usr, linuxrc runs ota-resume.sh again at the next boot (resume.conf is
# still there). From the first erase on, the checked files are never removed by a failure. The
# phases and what a power cut does in each: docs/lapinou/ota-lapin.md. The log of the last install:
# /usr/ota/last.log.
#
# Exit status: 0 done, or nothing to do; 1 failed or refused (nothing written, unless the log says
# the writing had begun); 2 usage. BusyBox ash, with the image's curl and openssl.

PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH

OTA_DIR=/usr/ota
NEW=/usr/ota/new
MARK=/usr/ota/resume.conf
FAILED=/usr/ota/resume.failed
RESUME=/karotz/scripts/ota-resume.sh
CONF=/usr/etc/conf
PUBKEY=/usr/share/lapinou/ota-pubkey.pub
CACERT=/usr/etc/ssl/certs/ca-certificates.crt
CURL=/usr/bin/curl
OPENSSL=/usr/bin/openssl
LOCK=/tmp/lapinou-ota.lock
API=https://api.github.com
API_HOST=api.github.com
DEFAULT_REPO=Kinder0083/LapiNou
NEEDED="zImage rootfs.img.gz usr.tar.gz"
MAX_JSON=2097152
MAX_FILE=314572800

# Everything is said on stderr; check's answer goes to the first output, kept as 3.
exec 3>&1 1>&2

. /usr/share/lapinou/conf.sh
. /karotz/scripts/install-lib.sh
SETUPLOG=/dev/null

MODE=$1
ALLOW_DOWNGRADE=
case "$2" in
'') ;;
--allow-downgrade) [ "${MODE}" = install ] && ALLOW_DOWNGRADE=1 || MODE=usage ;;
*) MODE=usage ;;
esac
WRITING=

# The downloads go, but never once the writing has begun, nor while an interrupted install's
# resume needs them.
cleanup() { [ -n "${WRITING}" ] || [ -f "${MARK}" ] || rm -rf "${NEW}"; }
die() {
    LOG "ERROR: $*"
    if [ -n "${WRITING}" ]; then
        LOG "the writing had begun: the checked files stay in ${NEW}; install the USB key to repair the rabbit"
        nose_solid FF0000
    fi
    cleanup
    exit 1
}
trap 'die "interrupted"' INT TERM HUP

# --- Small checks ------------------------------------------------------------------------------

name_ok() { # a version or file name, as the server's relay allows them
    case "$1" in ''|[!A-Za-z0-9]*|*[!A-Za-z0-9._+-]*) return 1 ;; esac
    [ ${#1} -le 100 ]
}
sha_ok() { case "$1" in *[!0-9a-f]*) return 1 ;; esac; [ ${#1} -eq 64 ]; }
num_ok() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; [ ${#1} -le 10 ]; }
size_of() { wc -c < "$1" | tr -d ' '; }

# A partition's size and erase block in bytes, from /proc/mtd (empty when there is no such
# partition).
mtd_field() { # mtdN column
    local line column=$2
    line=$(grep "^$1:" /proc/mtd 2> /dev/null)
    set -- ${line}
    eval "line=\${${column}}"
    case "${line}" in ''|*[!0-9a-fA-F]*) ;; *) echo $((0x${line})) ;; esac
}
mtd_size() { mtd_field "$1" 2; }
mtd_erase() { mtd_field "$1" 3; }

# A release's version, vX.Y.Z: install-lib.sh's release_ok.
# "X Y Z" of a version: a release's numbers; 0 0 0 for a dev build (0.0.0-<commit>) or anything else.
version_numbers() {
    local v=${1#v}
    if release_ok "$1"; then
        echo "${v%%.*} $(echo "${v#*.}" | tr . ' ')"
    else
        echo "0 0 0"
    fi
}
# Is version $1 lower than version $2?
version_lower() {
    set -- $(version_numbers "$1") $(version_numbers "$2")
    [ "$1" -lt "$4" ] || { [ "$1" -eq "$4" ] && { [ "$2" -lt "$5" ] || { [ "$2" -eq "$5" ] && [ "$3" -lt "$6" ]; }; }; }
}

# --- JSON ----------------------------------------------------------------------------------------

# A JSON document flattened, one "path<TAB>value" line per string, number or literal; the path is
# the keys and indexes joined by "/" (files/zImage/sha256, assets/0/url). Strings keep their
# escapes but \" \\ and \/. Fails on anything that isn't well-formed JSON, on a key with "/" or a
# tab, and past 20 levels. Splitting on the quotes keeps it linear in the document's size.
json_flat() {
    awk '
    function add(t, v) { nt++; ty[nt] = t; tv[nt] = v }
    function lex(seg,   i, c, n, lit) {
        n = length(seg); lit = ""
        for (i = 1; i <= n; i++) {
            c = substr(seg, i, 1)
            if (index("{}[]:,", c) || index(" \t\r\n", c)) {
                if (lit != "") { add("L", lit); lit = "" }
                if (index("{}[]:,", c)) add(c, "")
            } else lit = lit c
        }
        if (lit != "") add("L", lit)
    }
    function unescape(v) { gsub(/\\\//, "/", v); gsub(/\\"/, "\"", v); gsub(/\\\\/, "\\", v); return v }
    function value(path, depth,   t, k, i) {
        if (bad || depth > 20) { bad = 1; return }
        t = ty[pos]
        if (t == "{") {
            pos++
            if (ty[pos] == "}") { pos++; return }
            while (!bad) {
                if (ty[pos] != "S") { bad = 1; return }
                k = tv[pos]; pos++
                if (k ~ /[\/\t]/ || ty[pos] != ":") { bad = 1; return }
                pos++
                value((path == "") ? k : (path "/" k), depth + 1)
                t = ty[pos]; pos++
                if (t == "}") return
                if (t != ",") { bad = 1; return }
            }
            return
        }
        if (t == "[") {
            pos++
            if (ty[pos] == "]") { pos++; return }
            for (i = 0; !bad; i++) {
                value((path == "") ? i : (path "/" i), depth + 1)
                t = ty[pos]; pos++
                if (t == "]") return
                if (t != ",") { bad = 1; return }
            }
            return
        }
        if (t == "S" || (t == "L" && tv[pos] ~ /^(-?[0-9][0-9.eE+-]*|true|false|null)$/)) {
            print path "\t" tv[pos]; pos++; return
        }
        bad = 1
    }
    { doc = doc $0 "\n" }
    END {
        np = split(doc, p, "\"")
        # Outside and inside a string alternate; a quote after an odd run of backslashes is
        # part of the string.
        nt = 0; i = 1; inside = 0
        while (i <= np) {
            if (!inside) { lex(p[i]); inside = 1; i++; continue }
            s = p[i]
            while (match(s, /\\+$/) && RLENGTH % 2 == 1 && i < np) { i++; s = s "\"" p[i] }
            add("S", unescape(s)); inside = 0; i++
        }
        if (!inside) exit 1
        pos = 1; bad = 0
        value("", 0)
        if (bad || pos != nt + 1) exit 1
    }'
}

# The value at a path of a flattened document; fails when it is absent or there twice.
flat_get() { # file path
    awk -F '\t' -v k="$2" '$1 == k { n++; v = substr($0, length(k) + 2) } END { if (n != 1) exit 1; print v }' "$1"
}

# --- Fetching ------------------------------------------------------------------------------------

# A download from the LapiNou server: FILE gets URL, at most MAX bytes. Over http, or https
# checked against the rabbit's CA certificates; no other protocol, no curlrc (-q, first).
http_get() { # url file max
    rm -f "$2"
    "${CURL}" -q -fsS --proto =http,https --cacert "${CACERT}" --connect-timeout 15 --max-time 1800 \
        --speed-limit 1 --speed-time 60 --max-filesize "$3" -A lapinou-ota -o "$2" "$1" < /dev/null ||
        { rm -f "$2"; return 1; }
    [ "$(size_of "$2")" -le "$3" ] || { rm -f "$2"; return 1; }
}

# The host (and port) a URL names.
authority() { local a=${1#*://}; echo "${a%%/*}"; }

# One HTTPS request to GitHub, redirects not followed: FILE gets the body, FILE.h the headers;
# prints the HTTP status. The token goes in a header, from curl's standard input (never in its
# arguments), and only to the API host.
gh_request() { # url file accept max
    set -- "$1" "$2" "$3" "$4" --proto =https --cacert "${CACERT}" -sS --connect-timeout 15 --max-time 1800 \
        --speed-limit 1 --speed-time 60 --max-filesize "$4" -A lapinou-ota -H "Accept: $3" \
        -H "X-GitHub-Api-Version: 2022-11-28" -D "$2.h" -o "$2" -w '%{http_code}'
    local url=$1
    shift 4
    if [ -n "${TOKEN}" ] && [ "$(authority "${url}")" = "${API_HOST}" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "${TOKEN}" | "${CURL}" -q -K - "$@" "${url}"
    else
        "${CURL}" -q "$@" "${url}" < /dev/null
    fi
}

# A GitHub download, following at most 5 redirects, every one to an https address.
gh_get() { # url file accept max
    local url=$1 code i
    for i in 1 2 3 4 5 6; do
        case "${url}" in
        https://*) ;;
        *) rm -f "$2" "$2.h"; LOG "refused an address that isn't https: ${url%%\?*}"; return 1 ;;
        esac
        code=$(gh_request "${url}" "$2" "$3" "$4") || code=failed
        case "${code}" in
        200)
            rm -f "$2.h"
            [ "$(size_of "$2")" -le "$4" ] || { rm -f "$2"; return 1; }
            return 0 ;;
        301|302|303|307|308)
            url=$(awk 'tolower($1) == "location:" { v = $2 } END { print v }' "$2.h" | tr -d '\r')
            rm -f "$2" "$2.h" ;;
        *)
            rm -f "$2" "$2.h"
            LOG "GitHub answered ${code} for ${url%%\?*}$([ "${code}" = 404 ] && echo ' (a private repository: no ota_token, or one without access?)')"
            return 1 ;;
        esac
    done
    LOG "too many redirects for ${1%%\?*}"
    return 1
}

# --- The source ----------------------------------------------------------------------------------

# SOURCE=server with BASE (ending in /), or SOURCE=github with REPO and TOKEN.
pick_source() {
    local url host
    url=$(lapinou_get ota url)
    if [ -z "${url}" ] && [ "$(lapinou_get server enabled)" = 1 ]; then
        host=$(lapinou_get server host)
        [ -n "${host}" ] && url="http://${host}:8080/ota/"
    fi
    if [ -n "${url}" ]; then
        case "${url}" in
        http://?*|https://?*) ;;
        *) die "[ota] url must be an http:// or https:// address" ;;
        esac
        case "${url}" in *[[:space:]\"\'\\]*) die "[ota] url can't have spaces, quotes or backslashes" ;; esac
        SOURCE=server
        BASE=${url%/}/
        LOG "Source: the LapiNou server, ${BASE}"
        return 0
    fi
    SOURCE=github
    REPO=$(lapinou_get ota repo "${DEFAULT_REPO}")
    case "${REPO}" in
    */*/*|/*|*/|*[!A-Za-z0-9._/-]*|'') die "[ota] repo must be owner/name, like ${DEFAULT_REPO}" ;;
    */*) ;;
    *) die "[ota] repo must be owner/name, like ${DEFAULT_REPO}" ;;
    esac
    TOKEN=$(lapinou_secret_get ota_token)
    case "${TOKEN}" in *[!A-Za-z0-9_]*) die "ota_token isn't a GitHub token" ;; esac
    LOG "Source: GitHub, ${REPO}'s latest release$([ -n "${TOKEN}" ] && echo ', with the token')"
}

# The public key: refused while missing or the placeholder (fail closed, R42).
check_pubkey() {
    [ -f "${PUBKEY}" ] || die "no public key ${PUBKEY}: refusing every update"
    if grep -q PLACEHOLDER "${PUBKEY}"; then
        die "${PUBKEY} is still the placeholder, not the real public key: refusing every update"
    fi
    "${OPENSSL}" pkey -pubin -in "${PUBKEY}" -noout > /dev/null 2>&1 ||
        die "${PUBKEY} isn't a public key OpenSSL can read: refusing every update"
}

# NEW/manifest.json and its signature, checked; then VERSION and, per needed file, its size and SHA-256.
fetch_manifest() {
    local f s n
    if [ "${SOURCE}" = server ]; then
        http_get "${BASE}manifest.json" "${NEW}/manifest.json" 1048576 || die "can't read ${BASE}manifest.json"
        http_get "${BASE}manifest.json.sig" "${NEW}/manifest.json.sig" 64 || die "can't read ${BASE}manifest.json.sig"
    else
        gh_get "${API}/repos/${REPO}/releases/latest" "${NEW}/release.json" application/vnd.github+json "${MAX_JSON}" ||
            die "can't read ${REPO}'s latest release"
        json_flat < "${NEW}/release.json" > "${NEW}/release.flat" || die "GitHub's answer isn't JSON"
        TAG=$(flat_get "${NEW}/release.flat" tag_name) && name_ok "${TAG}" || die "the release has no valid tag"
        for f in manifest.json manifest.json.sig; do
            asset_url "${f}" || die "release ${TAG} has no ${f}"
            gh_get "${ASSET}" "${NEW}/${f}" application/octet-stream 1048576 || die "can't download ${f} of release ${TAG}"
        done
    fi
    [ "$(size_of "${NEW}/manifest.json.sig")" -eq 64 ] || die "manifest.json.sig isn't a 64-byte signature"
    "${OPENSSL}" pkeyutl -verify -pubin -inkey "${PUBKEY}" -rawin -in "${NEW}/manifest.json" \
        -sigfile "${NEW}/manifest.json.sig" > /dev/null 2>&1 || die "manifest.json's signature is wrong: refused"
    LOG "manifest.json's signature is good"
    json_flat < "${NEW}/manifest.json" > "${NEW}/manifest.flat" || die "manifest.json isn't valid JSON"
    VERSION=$(flat_get "${NEW}/manifest.flat" version) && name_ok "${VERSION}" || die "manifest.json has no valid version"
    release_ok "${VERSION}" || die "manifest.json's version ${VERSION} isn't a release's, vX.Y.Z (R44): refused"
    if [ "${SOURCE}" = github ] && [ "${TAG}" != "${VERSION}" ]; then
        die "release ${TAG} carries the manifest of ${VERSION}: refused"
    fi
    for f in ${NEEDED}; do
        s=$(flat_get "${NEW}/manifest.flat" "files/${f}/sha256") && sha_ok "${s}" || die "manifest.json gives no SHA-256 for ${f}"
        n=$(flat_get "${NEW}/manifest.flat" "files/${f}/size") && num_ok "${n}" && [ "${n}" -le "${MAX_FILE}" ] ||
            die "manifest.json gives no valid size for ${f}"
        eval "SHA_$(echo "${f}" | tr -c 'A-Za-z0-9\n' _)=\${s} SIZE_$(echo "${f}" | tr -c 'A-Za-z0-9\n' _)=\${n}"
    done
}

# ASSET: the API address of the release's asset of that name.
asset_url() { # name
    ASSET=$(awk -F '\t' -v n="$1" '
        { split($1, k, "/") }
        k[1] == "assets" && k[3] == "name" && $2 == n { want[k[2]] = 1 }
        k[1] == "assets" && k[3] == "url" && k[4] == "" { url[k[2]] = $2 }
        END { for (i in want) if (i in url) { print url[i]; found++ } exit found != 1 }' "${NEW}/release.flat")
}

sha_of() { eval "echo \${SHA_$(echo "$1" | tr -c 'A-Za-z0-9\n' _)}"; }
size_want() { eval "echo \${SIZE_$(echo "$1" | tr -c 'A-Za-z0-9\n' _)}"; }

installed_version() {
    local v
    v=$(head -n 1 "${CONF}/sys_version" 2> /dev/null)
    name_ok "${v}" && echo "${v}"
}

# image.py's build id: the SHA-256 of the three images' SHA-256 digests (raw, by name), 16 digits.
build_id() {
    local fmt
    fmt=$(for f in rootfs.img.gz usr.tar.gz zImage; do sha_of "${f}"; done | awk '
        BEGIN { h = "0123456789abcdef" }
        { for (i = 1; i < length($0); i += 2) printf "\\%03o", (index(h, substr($0, i, 1)) - 1) * 16 + index(h, substr($0, i + 1, 1)) - 1 }')
    printf "${fmt}" | sha256sum | cut -c 1-16
}

# --- The two commands ----------------------------------------------------------------------------

start() {
    mkdir -p "${OTA_DIR}" || die "can't make ${OTA_DIR}"
    exec 9> "${LOCK}"
    flock -n 9 || { LOG "ERROR: another lapinou-ota is running"; exit 1; }
    # An install interrupted during /usr is linuxrc's to finish (at the next boot), with what it kept;
    # if that failed already (ota-resume.sh said why in resume.failed), only the USB key repairs it.
    if [ -f "${FAILED}" ] && [ -f "${MARK}" ]; then
        die "an interrupted install's /usr couldn't be finished at boot ($(head -n 1 "${FAILED}")): install with the USB key"
    fi
    [ ! -f "${MARK}" ] ||
        die "an interrupted install's /usr is to be finished at the next boot (${MARK}): reboot the rabbit"
    # Whatever an interrupted download left.
    cleanup
    mkdir -p "${NEW}" || die "can't make ${NEW}"
    check_pubkey
    pick_source
}

check() {
    start
    fetch_manifest
    cleanup
    printf '{"installed":"%s","available":"%s"}\n' "$(installed_version)" "${VERSION}" >&3
}

download() { # name
    local f=$1 url want
    want=$(size_want "${f}")
    LOG "Downloading ${f} (${want} bytes)"
    if [ "${SOURCE}" = server ]; then
        http_get "${BASE}${VERSION}/${f}" "${NEW}/${f}" "${want}" || die "can't download ${BASE}${VERSION}/${f}"
    else
        asset_url "${f}" || die "release ${TAG} has no ${f}"
        gh_get "${ASSET}" "${NEW}/${f}" application/octet-stream "${want}" || die "can't download ${f} of release ${TAG}"
    fi
    [ "$(size_of "${NEW}/${f}")" -eq "${want}" ] || die "${f} doesn't have the manifest's size"
    [ "$(sha256sum "${NEW}/${f}" | cut -d ' ' -f 1)" = "$(sha_of "${f}")" ] || die "${f}'s SHA-256 isn't the manifest's"
}

# LEN bytes of a partition into FILE: the read-back's one nanddump command, which the check before
# the writing runs too.
nand_read() { # mtdN len file
    /sbin/nanddump -q --bb=skipbad -l "$2" -f "$3" "/dev/$1" > /dev/null 2>&1
}

# Before anything is erased: the tools the writing needs are there, and nanddump reads one erase
# block of mtd1 with the read-back's own options (an older nanddump without --bb would only fail
# once mtd1 is erased).
check_flash_tools() {
    local t erase
    [ -x /sbin/flash_erase ] || [ -x /sbin/flash_eraseall ] ||
        die "no /sbin/flash_erase (nor flash_eraseall) to erase the flash: nothing written"
    for t in /sbin/nandwrite /sbin/nanddump; do
        [ -x "${t}" ] || die "no ${t}: nothing written"
    done
    erase=$(mtd_erase mtd1)
    num_ok "${erase}" && [ "${erase}" -gt 0 ] || die "can't read mtd1's erase block in /proc/mtd: nothing written"
    rm -f "${NEW}/probe"
    if ! nand_read mtd1 "${erase}" "${NEW}/probe" || [ "$(size_of "${NEW}/probe")" != "${erase}" ]; then
        rm -f "${NEW}/probe"
        die "nanddump can't read mtd1 as the read-back does (-q --bb=skipbad -l ${erase}): nothing written"
    fi
    rm -f "${NEW}/probe"
    LOG "Flash tools: erasing, nandwrite and nanddump's read-back of mtd1 work"
}

# Does the partition hold the image? Read back by nanddump (no OOB, bad blocks skipped as nandwrite
# -m skips them), whole erase blocks, and its first <image size> bytes' SHA-256 compared with the
# checked one.
readback_ok() { # mtdN image sha256
    local size erase len got
    size=$(size_of "$2")
    erase=$(mtd_erase "$1")
    num_ok "${erase}" && [ "${erase}" -gt 0 ] || erase=131072
    len=$(( (size + erase - 1) / erase * erase ))
    rm -f "${NEW}/readback"
    nand_read "$1" "${len}" "${NEW}/readback" || { rm -f "${NEW}/readback"; return 1; }
    got=$(head -c "${size}" "${NEW}/readback" | sha256sum | cut -d ' ' -f 1)
    rm -f "${NEW}/readback"
    [ "${got}" = "$3" ]
}

# Erase, write and read back a partition (mtd1 and mtd2 only); once more if it reads back wrong; if
# it still does, stop there: /usr is untouched and the checked files stay.
write_mtd() { # mtdN image sha256
    local try
    case "$1" in mtd1|mtd2) ;; *) die "refusing to write $1" ;; esac
    for try in 1 2; do
        LOG "Writing ${2##*/} to $1"
        erase_partition "/dev/$1" > /dev/null || die "erasing $1 failed"
        if /sbin/nandwrite -pm "/dev/$1" "$2" > /dev/null && readback_ok "$1" "$2" "$3"; then
            LOG "$1 read back: the same as ${2##*/}"
            return 0
        fi
        [ "${try}" = 2 ] || LOG "$1 reads back wrong: writing it once more"
    done
    die "$1 reads back wrong twice: /usr is untouched; read docs/lapinou/ota-lapin.md before rebooting the rabbit"
}

install() {
    local installed f limit free need sectors raw id pair
    mkdir -p "${OTA_DIR}" || die "can't make ${OTA_DIR}"
    SETUPLOG=${OTA_DIR}/last.log
    : > "${SETUPLOG}"
    start
    fetch_manifest
    installed=$(installed_version)
    id=$(build_id)
    LOG "Installed ${installed:-unknown}; available ${VERSION} (build ${id})"
    if [ "${id}" = "$(head -n 1 "${CONF}/lapinlibre_build_id" 2> /dev/null)" ]; then
        LOG "The rabbit already runs the same build: nothing to install"
        cleanup
        exit 0
    fi
    if [ "${VERSION}" = "${installed}" ]; then
        LOG "The rabbit is up to date"
        cleanup
        exit 0
    fi
    if version_lower "${VERSION}" "${installed}"; then
        [ -n "${ALLOW_DOWNGRADE}" ] ||
            die "${VERSION} is lower than the installed ${installed}: refused (lapinou-ota install --allow-downgrade to install it anyway)"
        LOG "${VERSION} is lower than the installed ${installed}: installed as --allow-downgrade asks"
    fi
    # Room for the files before downloading them, and each image fits its partition.
    for pair in zImage:mtd1 rootfs.img.gz:mtd2; do
        f=${pair%%:*}
        limit=$(mtd_size "${pair#*:}")
        [ -n "${limit}" ] || die "no ${pair#*:} in /proc/mtd"
        [ "$(size_want "${f}")" -le "${limit}" ] || die "${f} is $(size_want "${f}") bytes; ${pair#*:} holds ${limit}"
    done
    free=$(df -Pk "${OTA_DIR}" | tail -n 1 | awk '{ print $4 }')
    need=$(( ($(size_want zImage) + $(size_want rootfs.img.gz) + $(size_want usr.tar.gz)) / 1024 + 10240 ))
    num_ok "${free}" || die "can't read the free space in ${OTA_DIR}"
    [ "${free}" -ge "${need}" ] || die "${OTA_DIR} has ${free} kB free; ${need} kB needed"
    for f in ${NEEDED}; do
        download "${f}"
    done
    gzip -t "${NEW}/rootfs.img.gz" 2> /dev/null || die "rootfs.img.gz doesn't decompress"
    gzip -t "${NEW}/usr.tar.gz" 2> /dev/null || die "usr.tar.gz doesn't decompress"
    sectors=$(cat /sys/block/ram0/size 2> /dev/null)
    if num_ok "${sectors}"; then
        raw=$(gzip -dc "${NEW}/rootfs.img.gz" | wc -c | tr -d ' ')
        [ "${raw}" -le $((sectors * 512)) ] ||
            die "the root filesystem is ${raw} bytes uncompressed; the RAM disk holds $((sectors * 512))"
    fi
    [ -f "${RESUME}" ] || die "no ${RESUME} in this root filesystem: install with the USB key"
    check_flash_tools
    LOG "Checks passed: installing ${VERSION} (build ${id})"

    # From here on the rabbit is being changed, and the checked files are kept whatever happens.
    trap '' INT TERM HUP
    WRITING=1
    /usr/etc/init.d/S40lapinlibre stop > /dev/null 2>&1
    nose_pulse FFFF00 500
    write_mtd mtd1 "${NEW}/zImage" "$(sha_of zImage)"
    write_mtd mtd2 "${NEW}/rootfs.img.gz" "$(sha_of rootfs.img.gz)"
    # Both partitions read back right: /usr, by the root filesystem's ota-resume.sh, which the next
    # boot runs again if the power goes before it ends (resume.conf).
    for f in karotz.conf voice.conf; do
        rm -f "${NEW}/kept-${f}"
        if [ -f "${CONF}/${f}" ]; then
            cp -p "${CONF}/${f}" "${NEW}/kept-${f}" || die "can't keep the rabbit's ${f}"
        fi
    done
    rm -f "${FAILED}"
    printf 'FORMAT=1\nVERSION=%s\nBUILD_ID=%s\nUSR_SHA=%s\n' "${VERSION}" "${id}" "$(sha_of usr.tar.gz)" > "${MARK}.new" &&
        sync && mv -f "${MARK}.new" "${MARK}" && sync || die "can't write ${MARK}"
    /bin/sh "${RESUME}" || die "replacing /usr failed (the next boot tries again from ${NEW})"
    LOG "Installed ${VERSION}: rebooting"
    /sbin/reboot -f
    exit 0
}

case "${MODE}" in
check) check ;;
install) install ;;
*) echo "usage: lapinou-ota check | install [--allow-downgrade]"; exit 2 ;;
esac
exit 0
