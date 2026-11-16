#!/bin/bash
# SPDX-License-Identifier: MIT
# Moves a Karotz from Free Rabbits OS to LapinLibre over SSH, with a built USB key's files (no key in
# the rabbit). Runs on a PC on the rabbit's network (Linux, or Git Bash on Windows), with ssh, md5sum,
# gzip and sed.
#
#   migrate-from-fros.sh --rabbit IP --key DIR --ssh-pubkey KEY [--dhcp] [--dry-run] [--no-wait]
#
#   --rabbit IP        the rabbit's address; Free Rabbits OS takes ssh karotz@IP with an empty password
#   --key DIR          the built USB key (out/usbkey): zImage, rootfs.img.gz, usr.tar.gz, install.conf...
#   --ssh-pubkey KEY   the public key LapinLibre's SSH will take (a line "ssh-ed25519 AAAA... name", or
#                      a .pub file); LapinLibre has no password login. Not needed when the key's
#                      lapinlibre.txt has an SSH_PUBKEY.
#   --dhcp             LapinLibre takes its address by DHCP; by default it keeps the rabbit's current
#                      address (IP) with the netmask, gateway and DNS it uses now. A network.conf the
#                      rabbit already has is kept either way.
#   --dry-run          every check, on the PC and on the rabbit, then stop: the flash is not touched
#                      (the rabbit only gets the small files in /dev/shm, its RAM, removed at the end)
#   --no-wait          after the reboot, don't wait for LapinLibre's SSH and web page
#
# Writes mtd1 (kernel) and mtd2 (root filesystem) only, each read back, then replaces /usr keeping
# its settings, apps and messages (as the key's install does); never the boot loader or the rescue
# partitions.
# Exit status: 0 done, 2 a check failed before anything was written, 3 a partition read back differs
# from its image (the rabbit keeps running Free Rabbits OS: don't switch it off, see
# docs/lapinou/migration.md), 1 anything else.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
STAGE=/dev/shm/lapinou-migrate
USR_STAGE=/usr/.lapinou-migrate
MiB=1048576

RABBIT= KEY= PUBKEY= NETWORK=static DRY=0 WAIT=1
die() { # status, message
    echo "migrate-from-fros: $2" >&2
    exit "$1"
}
usage() { sed -n '7,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
while [ $# -gt 0 ]; do
    case "$1" in
    --rabbit) RABBIT=${2:-}; shift 2 || usage ;;
    --key) KEY=${2:-}; shift 2 || usage ;;
    --ssh-pubkey) PUBKEY=${2:-}; shift 2 || usage ;;
    --dhcp) NETWORK=dhcp; shift ;;
    --dry-run) DRY=1; shift ;;
    --no-wait) WAIT=0; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option $1" >&2; usage ;;
    esac
done
[ -n "${RABBIT}" ] && [ -n "${KEY}" ] || usage
[[ "${RABBIT}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die 2 "--rabbit must be an address or a host name"

# 1. On the PC: the key's files and their checksums.
for f in zImage rootfs.img.gz usr.tar.gz install.conf build_id install-lib.sh applysettings.sh lapinlibre.txt; do
    [ -f "${KEY}/${f}" ] || die 2 "${KEY}/${f} is missing: is ${KEY} a built USB key?"
done
conf() { sed -n "s/^$1=//p" "${KEY}/install.conf" | tr -d '\r'; }
md5_of() { md5sum < "$1" | cut -d' ' -f1; }
size_of() { echo $(($(wc -c < "$1"))); }
for pair in zImage:KERNEL_MD5 rootfs.img.gz:ROOTFS_MD5 usr.tar.gz:USR_MD5; do
    f=${pair%%:*}
    [ "$(md5_of "${KEY}/${f}")" = "$(conf "${pair#*:}")" ] || die 2 "${f}'s MD5 isn't install.conf's: the key is damaged"
done
[ "$(size_of "${KEY}/zImage")" -le $((3 * MiB)) ] || die 2 "zImage is bigger than mtd1 (3 MiB)"
[ "$(size_of "${KEY}/rootfs.img.gz")" -le $((5 * MiB)) ] || die 2 "rootfs.img.gz is bigger than mtd2 (5 MiB)"
gzip -t "${KEY}/rootfs.img.gz" && gzip -t "${KEY}/usr.tar.gz" || die 2 "an image doesn't decompress"

# The settings: the key's lapinlibre.txt, then the SSH key given here (a later line wins).
WORK=$(mktemp -d) || die 1 "no temporary directory"
trap 'rm -rf "${WORK}"' EXIT
if [ -n "${PUBKEY}" ] && [ -f "${PUBKEY}" ]; then
    PUBKEY=$(head -n 1 "${PUBKEY}" | tr -d '\r')
fi
{ cat "${KEY}/lapinlibre.txt"; echo; echo "# migrate-from-fros.sh"; [ -z "${PUBKEY}" ] || echo "SSH_PUBKEY=${PUBKEY}"; } \
    > "${WORK}/lapinlibre.txt"
grep -q '^[[:space:]]*SSH_PUBKEY[[:space:]]*=[[:space:]]*[^[:space:]]' "${WORK}/lapinlibre.txt" ||
    die 2 "no --ssh-pubkey: LapinLibre takes SSH logins with a key only, so the rabbit would be unreachable"

# 2. The rabbit, over Free Rabbits OS's old Dropbear. Its host key is remembered for this run only
# (LapinLibre's will differ).
SSH=${LAPINOU_SSH:-ssh}
OLD_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=8
    -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o MACs=+hmac-sha1
    -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${WORK}/known_hosts")
rsh() { "${SSH}" "${OLD_OPTS[@]}" "karotz@${RABBIT}" "$@"; }
send() { # local file, remote path
    rsh "cat > '$2'" < "$1" || die 2 "sending ${1##*/} to the rabbit failed"
}
# on-rabbit.sh, its output shown as it comes; its status.
on_rabbit() { # check|install
    rsh "${RUN} $1 ${RABBIT}" 2>&1 | sed 's/^/  rabbit: /'
    return "${PIPESTATUS[0]}"
}
cleanup_remote() { rsh "rm -rf ${STAGE} ${USR_STAGE}" > /dev/null 2>&1; }

echo "Connecting to karotz@${RABBIT}"
rsh "rm -rf ${STAGE} && mkdir -p ${STAGE} && df -k /dev/shm | tail -n 1" > "${WORK}/shm" ||
    die 2 "can't log in to karotz@${RABBIT} (is it running Free Rabbits OS, with SSH?)"
shm_free=$(awk '{ print $(NF - 2) }' "${WORK}/shm")
case "${shm_free}" in ''|*[!0-9]*) die 2 "can't read the rabbit's free RAM in /dev/shm" ;; esac
need=$(( ($(size_of "${KEY}/zImage") + 2 * $(size_of "${KEY}/rootfs.img.gz")) / 1024 + 2048 ))
[ "${shm_free}" -ge "${need}" ] || die 2 "the rabbit's /dev/shm has ${shm_free} kB free; ${need} kB needed"

echo "Sending the install files to ${STAGE}"
send "${HERE}/on-rabbit.sh" "${STAGE}/on-rabbit.sh"
for f in install-lib.sh applysettings.sh; do
    sed "s#/mnt/usbkey#${STAGE}#g" "${KEY}/${f}" > "${WORK}/${f}"
    send "${WORK}/${f}" "${STAGE}/${f}"
done
send "${WORK}/lapinlibre.txt" "${STAGE}/lapinlibre.txt"
for f in install.conf build_id zImage rootfs.img.gz; do
    send "${KEY}/${f}" "${STAGE}/${f}"
done

RUN="NETWORK=${NETWORK} USR_SIZE=$(size_of "${KEY}/usr.tar.gz") bash ${STAGE}/on-rabbit.sh"
echo "Checking on the rabbit"
on_rabbit check
status=$?
if [ ${status} -ne 0 ]; then
    cleanup_remote
    die 2 "the rabbit's checks failed (status ${status}): nothing was written"
fi

if [ ${DRY} -eq 1 ]; then
    cleanup_remote
    echo "Dry run: every check passed. Would write zImage to mtd1 and rootfs.img.gz to mtd2, then"
    echo "replace /usr with usr.tar.gz keeping /usr/etc/conf, apps, messages and uuid.conf, and reboot."
    exit 0
fi

# 3. usr.tar.gz streamed to /usr itself (the RAM can't hold it), checked there before anything is
# erased; then the install.
echo "Sending usr.tar.gz to ${USR_STAGE}"
rsh "rm -rf ${USR_STAGE} && mkdir -p ${USR_STAGE} && cat > ${USR_STAGE}/usr.tar.gz" < "${KEY}/usr.tar.gz" || {
    cleanup_remote
    die 2 "sending usr.tar.gz failed: nothing was written"
}
echo "Installing (mtd1, mtd2, /usr): don't switch the rabbit off"
on_rabbit install
status=$?
case ${status} in
0) ;;
2) cleanup_remote; die 2 "the rabbit's checks failed: nothing was written" ;;
3) die 3 "a partition read back differently: the rabbit still runs Free Rabbits OS from RAM; don't reboot it, restore mtd1 and mtd2 from the backup (docs/lapinou/migration.md)" ;;
*) die 1 "the install failed (status ${status}) after writing began; don't reboot before reading docs/lapinou/migration.md" ;;
esac

# on-rabbit.sh reboots the rabbit itself (a new login to the old system may fail by now).
echo "The rabbit reboots into LapinLibre"
sleep 5

[ ${WAIT} -eq 1 ] || exit 0
# 4. LapinLibre back on the network: SSH with the key, and the web page.
echo "Waiting for LapinLibre (up to 5 minutes)"
ssh_ok= web_ok=
for _ in $(seq 60); do
    sleep 5
    if [ -z "${ssh_ok}" ] && "${SSH}" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
        -o "UserKnownHostsFile=${WORK}/known_hosts_lapinlibre" "karotz@${RABBIT}" true > /dev/null 2>&1; then
        ssh_ok=yes
    fi
    if [ -z "${web_ok}" ] && curl -fsS -m 5 "http://${RABBIT}/" 2> /dev/null | grep -qi lapinlibre; then
        web_ok=yes
    fi
    [ -n "${ssh_ok}" ] && [ -n "${web_ok}" ] && break
done
echo "SSH with the key: ${ssh_ok:-no}; web page: ${web_ok:-no}"
[ -n "${ssh_ok}" ] && [ -n "${web_ok}" ] || die 1 "LapinLibre isn't answering yet at ${RABBIT}: see docs/lapinou/migration.md"
echo "Done: LapinLibre runs at ${RABBIT}"
