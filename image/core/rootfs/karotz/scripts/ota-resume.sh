#!/bin/sh
# SPDX-License-Identifier: MIT
# The /usr phase of a LapiNou update (lapinou-ota, Task 11), and its resume at boot.
#
# lapinou-ota runs it once the kernel (mtd1) and the root filesystem (mtd2) are written and read
# back, with /usr/ota/resume.conf (FORMAT=1, VERSION, BUILD_ID, USR_SHA) and, in /usr/ota/new, the checked
# usr.tar.gz and the rabbit's own karotz.conf and voice.conf (kept-*). linuxrc runs it at every boot
# once /usr is mounted: while resume.conf is there, a power cut stopped this phase, so it runs again
# from the start (it can: the archive is kept until the end). It is in the root filesystem, not in
# /usr, which may be half gone by then.
#
# /usr is emptied as the USB key's install does (install-lib.sh's clean_usr, keeping /usr/etc/conf,
# the apps, the messages, uuid.conf and /usr/ota), usr.tar.gz unpacked (once more if that fails),
# the kept settings put back, sys_version and lapinlibre_build_id written, /usr/etc/inittab removed
# (linuxrc copies the new one). resume.conf goes last but one, then the downloads.
# Never touches the flash partitions themselves. Exit status: 0 done or nothing to do, 1 failed (logged
# in /usr/ota/last.log and, the reason only, in /usr/ota/resume.failed, which lapinou-ota reads to
# say that the USB key is needed; resume.conf stays, so the next boot tries again). resume.conf's
# format (FORMAT=1) and its version (a release's, vX.Y.Z, install-lib.sh's release_ok) are checked
# before /usr is touched.

PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH

OTA_DIR=/usr/ota
NEW=/usr/ota/new
MARK=/usr/ota/resume.conf
FAILED=/usr/ota/resume.failed
CONF=/usr/etc/conf

[ -f "${MARK}" ] || exit 0

. /karotz/scripts/install-lib.sh
SETUPLOG=${OTA_DIR}/last.log

fail() {
    LOG "ERROR: finishing /usr: $*; the USB key's install repairs the rabbit"
    # Why, for lapinou-ota, which then says that the USB key is needed.
    printf '%s\n' "$*" > "${FAILED}" 2> /dev/null
    exit 1
}
field() { sed -n "s/^$1=//p" "${MARK}" | head -n 1; }

VERSION=$(field VERSION)
ID=$(field BUILD_ID)
SHA=$(field USR_SHA)
[ "$(field FORMAT)" = 1 ] || fail "${MARK} has an unknown format (FORMAT=1 expected)"
release_ok "${VERSION}" || fail "${MARK} has no valid version (vX.Y.Z)"
case "${ID}" in ''|*[!0-9a-f]*) fail "${MARK} has no valid build id" ;; esac
case "${SHA}" in ''|*[!0-9a-f]*) fail "${MARK} has no valid SHA-256" ;; esac
[ ${#SHA} -eq 64 ] || fail "${MARK} has no valid SHA-256"
[ -f "${NEW}/usr.tar.gz" ] || fail "${NEW}/usr.tar.gz is missing"
[ "$(sha256sum "${NEW}/usr.tar.gz" | cut -d ' ' -f 1)" = "${SHA}" ] ||
    fail "usr.tar.gz's SHA-256 isn't the one checked before the install"

LOG "Installing /usr of ${VERSION}"
clean_usr ota
rm -f "${CONF}/freerabbits_build_id"
if ! unpack_into_usr "${NEW}/usr.tar.gz"; then
    LOG "Unpacking failed: cleaning /usr and trying once more"
    clean_usr ota
    unpack_into_usr "${NEW}/usr.tar.gz" || fail "unpacking usr.tar.gz into /usr failed twice"
fi
mkdir -p "${CONF}"
for f in karotz.conf voice.conf; do
    if [ -f "${NEW}/kept-${f}" ]; then
        cp -p "${NEW}/kept-${f}" "${CONF}/${f}" || fail "can't put the rabbit's ${f} back"
    fi
done
echo "${VERSION}" > "${CONF}/sys_version" || fail "can't write sys_version"
echo "${ID}" > "${CONF}/lapinlibre_build_id" || fail "can't write lapinlibre_build_id"
rm -rf /usr/install /usr/yaffs* /usr/.install_yaffs_start
rm -f /usr/etc/inittab
sync
rm -f "${MARK}" "${FAILED}"
sync
rm -rf "${NEW}"
sync
LOG "Installed ${VERSION}"
exit 0
