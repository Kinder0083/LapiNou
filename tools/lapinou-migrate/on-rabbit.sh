#!/bin/bash
# SPDX-License-Identifier: MIT
# The rabbit's half of migrate-from-fros.sh: runs as root on a Karotz under Free Rabbits OS, from
# /dev/shm/lapinou-migrate, where the PC has put it with the built USB key's install-lib.sh and
# applysettings.sh (their /mnt/usbkey changed to that directory), zImage, rootfs.img.gz,
# install.conf, build_id and the settings (lapinlibre.txt). The PC streams usr.tar.gz to
# /usr/.lapinou-migrate before "install".
#
#   [NETWORK=static|dhcp] [PC_GATEWAY=a.b.c.d] [PC_DNS=a.b.c.d] [USR_SIZE=bytes]
#       [BACKUP_MTD1_MD5=.. BACKUP_MTD2_MD5=..] on-rabbit.sh check IP
#   [NETWORK=static|dhcp] [PC_GATEWAY=a.b.c.d] [PC_DNS=a.b.c.d] on-rabbit.sh install IP
#
# A fixed address (NETWORK=static, no network.conf yet) takes the gateway and DNS server the rabbit
# uses now, once they answer (a ping, a DNS lookup); else the PC's (PC_GATEWAY, PC_DNS: the PC's
# gateway only when it is on the rabbit's network), when they answer; else the check fails.
#
# "check" writes nothing to the flash: it also reads mtd1 and mtd2 whole and compares them with the
# backup's md5s, lists /usr/etc/conf and the processes "install" would stop. "install" checks
# everything again (usr.tar.gz too), stops Free Rabbits OS's programs (the controller, immortaldog,
# crond...: none may reboot the rabbit half-way), then
# does what the key's autorun does: the kernel to mtd1 and the root filesystem to mtd2, each read
# back and compared, then /usr replaced, keeping its settings (/usr/etc/conf), apps, messages and
# uuid.conf, and the settings applied, and the rabbit reboots 2 s after it exits. Only mtd1 and mtd2
# are ever erased; /usr (mtd6) is changed through its mounted file system.
#
# Exit status: 0 done, 2 a check failed (nothing written), 3 a partition read back differs from its
# image (/usr not touched yet; the rabbit keeps running Free Rabbits OS from RAM), 1 any other
# failure once writing has begun.
#
# Free Rabbits OS has bash 3 and BusyBox, like the systems the key's autorun runs on.

PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH

STAGE=/dev/shm/lapinou-migrate
USR_STAGE=/usr/.lapinou-migrate
CONF=/usr/etc/conf
# The settings files an install keeps whatever usr.tar.gz carries.
KEPT="karotz.conf voice.conf wpa.conf network.conf"

source /dev/shm/lapinou-migrate/install-lib.sh

# install-lib.sh's ERROR, with this script's exit status: 2 while checking, 1 once writing has begun.
FAIL_CODE=2
ERROR() {
    LOG "ERROR: $*"
    nose_solid FF0000
    exit "${FAIL_CODE}"
}

MODE=$1
RABBIT_IP=$2
NETWORK=${NETWORK:-static}

# A partition's /proc/mtd line: its size and erase block in bytes, and its quoted name.
mtd_info() { # mtdN -> MTD_SIZE MTD_ERASE MTD_NAME
    local line
    line=$(grep "^$1:" /proc/mtd 2> /dev/null)
    set -- ${line}
    MTD_SIZE= MTD_ERASE= MTD_NAME=$4
    case "$2" in ''|*[!0-9a-fA-F]*) ;; *) MTD_SIZE=$((0x$2)) ;; esac
    case "$3" in ''|*[!0-9a-fA-F]*) ;; *) MTD_ERASE=$((0x$3)) ;; esac
}

md5_of() { md5sum < "$1" 2> /dev/null | cut -d' ' -f1; }

# The md5 of a file's first SIZE bytes (a partition read back is padded to whole pages).
prefix_md5() { # file size
    local full=$(($2 / 2048))
    {
        dd if="$1" bs=2048 count=${full} 2> /dev/null
        dd if="$1" bs=1 skip=$((full * 2048)) count=$(($2 % 2048)) 2> /dev/null
    } | md5sum | cut -d' ' -f1
}

check_md5() { # file, expected md5, name
    local sum
    sum=$(md5_of "$1")
    [ -n "${sum}" ] && [ "${sum}" = "$2" ] || ERROR "MD5 checksum error: $3"
}

# The image fits its partition, and the partition is the one Free Rabbits OS names so.
check_partition() { # mtdN, expected name, image, image name
    local size
    mtd_info "$1"
    [ "${MTD_NAME}" = "\"$2\"" ] || ERROR "$1 is ${MTD_NAME:-missing} in /proc/mtd, not \"$2\""
    [ -n "${MTD_SIZE}" ] && [ -n "${MTD_ERASE}" ] || ERROR "can't read $1's size in /proc/mtd"
    [ -e "/dev/$1" ] || ERROR "no /dev/$1"
    size=$(($(wc -c < "$3")))
    [ "${size}" -le "${MTD_SIZE}" ] || ERROR "$4 is ${size} bytes; $1 holds ${MTD_SIZE}"
}

hex_ip() { # a /proc/net/route address (little-endian hex)
    local h=$1
    echo "$((0x${h:6:2})).$((0x${h:4:2})).$((0x${h:2:2})).$((0x${h:0:2}))"
}

# The address LapinLibre will boot with, as network.conf's lines: kept when the rabbit already has
# a network.conf, else DHCP or, by default, the address it has now (RABBIT_IP) with the netmask,
# gateway and DNS it uses now.
plan_network() {
    local iface dest gw flags refcnt use metric mask rest
    NET_IP= NET_MASK= NET_GW= NET_DNS=
    if [ -f "${CONF}/network.conf" ]; then
        NET_PLAN=kept
        return 0
    fi
    if [ "${NETWORK}" = dhcp ]; then
        NET_PLAN=dhcp
        return 0
    fi
    NET_PLAN=static
    NET_IP=${RABBIT_IP}
    while read -r iface dest gw flags refcnt use metric mask rest; do
        [ "${iface}" = wlan0 ] || continue
        if [ "${dest}" = 00000000 ]; then
            [ -n "${NET_GW}" ] || NET_GW=$(hex_ip "${gw}")
        elif [ -z "${NET_MASK}" ] && [ "${mask}" != 00000000 ]; then
            NET_MASK=$(hex_ip "${mask}")
        fi
    done < /proc/net/route
    is_ipv4 "${NET_IP}" || ERROR "the rabbit's address ${NET_IP} isn't an IPv4 address: use --dhcp"
    is_ipv4 "${NET_MASK}" && is_ipv4 "${NET_GW}" || ERROR "no netmask or default route on wlan0: use --dhcp"
    # network.conf is sourced as root: only an address goes in, else the gateway serves as DNS.
    NET_DNS=$(awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf 2> /dev/null)
    if ! is_ipv4 "${NET_DNS}"; then
        [ -z "${NET_DNS}" ] || LOG "Network: /etc/resolv.conf's nameserver isn't an IPv4 address; the gateway is used"
        NET_DNS=${NET_GW}
    fi
    probe_network
}

# The first of these programs the rabbit has, or nothing.
first_tool() {
    local t
    for t in "$@"; do
        [ -x "${t}" ] && { echo "${t}"; return 0; }
    done
    return 1
}
# An IPv4 address as a number.
ip_num() {
    local IFS=.
    set -- $1
    echo $(( ($1 << 24) + ($2 << 16) + ($3 << 8) + $4 ))
}
same_network() { # address, address, netmask
    local m
    m=$(ip_num "$3")
    [ $(( $(ip_num "$1") & m )) -eq $(( $(ip_num "$2") & m )) ]
}

# Before network.conf gets them: the gateway answers a ping and the DNS server a lookup. When one
# doesn't, the PC's (on the rabbit's network, for the gateway) takes its place if it answers;
# else the check fails, saying so.
probe_network() {
    local ping lookup
    ping=$(first_tool /bin/ping /usr/bin/ping /sbin/ping /usr/sbin/ping)
    lookup=$(first_tool /usr/bin/nslookup /bin/nslookup /sbin/nslookup /usr/sbin/nslookup)
    if [ -z "${ping}" ]; then
        LOG "Network: no ping on this rabbit: the gateway ${NET_GW} isn't checked"
    elif "${ping}" -c 1 -W 3 "${NET_GW}" > /dev/null 2>&1; then
        LOG "Network: the gateway ${NET_GW} answers"
    elif is_ipv4 "${PC_GATEWAY:-}" && [ "${PC_GATEWAY}" != "${NET_GW}" ] &&
            same_network "${PC_GATEWAY}" "${NET_IP}" "${NET_MASK}" && "${ping}" -c 1 -W 3 "${PC_GATEWAY}" > /dev/null 2>&1; then
        LOG "Network: the gateway ${NET_GW} doesn't answer; the PC's gateway ${PC_GATEWAY} does, and is used"
        [ "${NET_DNS}" != "${NET_GW}" ] || NET_DNS=${PC_GATEWAY}
        NET_GW=${PC_GATEWAY}
    else
        ERROR "the gateway ${NET_GW} doesn't answer a ping, nor does the PC's gateway${PC_GATEWAY:+ ${PC_GATEWAY}} on the rabbit's network: use --dhcp, or check the rabbit's network"
    fi
    if [ -z "${lookup}" ]; then
        LOG "Network: no nslookup on this rabbit: the DNS server ${NET_DNS} isn't checked"
    elif "${lookup}" github.com "${NET_DNS}" > /dev/null 2>&1; then
        LOG "Network: the DNS server ${NET_DNS} answers"
    elif is_ipv4 "${PC_DNS:-}" && [ "${PC_DNS}" != "${NET_DNS}" ] && "${lookup}" github.com "${PC_DNS}" > /dev/null 2>&1; then
        LOG "Network: the DNS server ${NET_DNS} doesn't answer; the PC's DNS server ${PC_DNS} does, and is used"
        NET_DNS=${PC_DNS}
    else
        ERROR "the DNS server ${NET_DNS} doesn't answer a lookup of github.com, nor does the PC's${PC_DNS:+ ${PC_DNS}}: use --dhcp, or check the rabbit's network"
    fi
}

is_ipv4() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

# A setting's value in the staged lapinlibre.txt, read as applysettings.sh reads it (the last line
# wins, CR, byte-order mark, spaces and quotes taken off).
setting() { # NAME
    local line key value v= bom
    bom=$(printf '\357\273\277')
    while IFS= read -r line || [ -n "${line}" ]; do
        line=${line%$'\r'}
        line=${line#${bom}}
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "${line}" in ''|'#'*) continue ;; *=*) ;; *) continue ;; esac
        key=${line%%=*}
        key="${key%"${key##*[![:space:]]}"}"
        [ "${key}" = "$1" ] || continue
        value=${line#*=}
        value="${value#"${value%%[![:space:]]*}"}"
        case "${value}" in \"*\") value=${value#\"}; value=${value%\"} ;; \'*\') value=${value#\'}; value=${value%\'} ;; esac
        v=${value}
    done < "${STAGE}/lapinlibre.txt"
    printf '%s' "${v}"
}

# The processes, kernel threads (an empty command line) left out: "pid name ppid" per line. Run it
# in this shell, not in $( ): the /proc list is taken before it starts any command of its own.
# (/proc's files all have size 0, so their content is what tells.)
proc_list() {
    local d stat comm rest
    for d in /proc/[0-9]*; do
        { read -r stat < "${d}/stat"; } 2> /dev/null || continue
        [ -n "$(tr -d '\000' < "${d}/cmdline" 2> /dev/null)" ] || continue
        comm=${stat#*\(}
        comm=${comm%\)*}
        rest=${stat##*\) }
        set -- ${rest}
        echo "${d##*/} ${comm// /_} $2"
    done
}

# This script and the processes it runs under (the SSH session).
ancestors() {
    local p=$$ i=0 stat
    while [ -n "${p}" ] && [ "${p}" != 0 ] && [ ${i} -lt 30 ]; do
        echo "${p}"
        { read -r stat < "/proc/${p}/stat"; } 2> /dev/null || break
        set -- ${stat##*\) }
        p=$2
        i=$((i + 1))
    done
}

# What keeps running during the install: the system, the network, the SSH session, init's respawned
# programs, and a process that holds the watchdog (stopping it would let the watchdog reboot).
KEEP_PROCS="init syslogd klogd inetd dropbear wpa_supplicant udhcpc power getty"

# Everything else (the controller, immortaldog, crond, OpenKarotz...) could reboot the rabbit between
# erasing mtd1 and the end of mtd2's read-back: plan_stop, part of the checks, lists them in TARGETS
# (and refuses when /proc can't be read); stop_fros then freezes them all (so none can react to
# another's end) and kills them.
plan_stop() {
    local ours pid comm ppid
    proc_list > "${STAGE}/procs"
    grep -q '^1 ' "${STAGE}/procs" && grep -q "^$$ " "${STAGE}/procs" ||
        ERROR "can't list the processes in /proc (neither init nor this script found)"
    ours=" $(ancestors | tr '\n' ' ') "
    TARGETS=
    while read -r pid comm ppid; do
        # This script's own commands (the read of /proc) aren't Free Rabbits OS's.
        [ "${ppid}" != "$$" ] || continue
        case "${ours}" in *" ${pid} "*) continue ;; esac
        case " ${KEEP_PROCS} " in *" ${comm} "*) LOG "Process ${pid} ${comm}: kept"; continue ;; esac
        if ls -l "/proc/${pid}/fd" 2> /dev/null | grep -q watchdog; then
            LOG "Process ${pid} ${comm}: holds the watchdog, kept"
            continue
        fi
        LOG "Process ${pid} ${comm} ($(tr '\000' ' ' < "/proc/${pid}/cmdline" 2> /dev/null | cut -c 1-80 | sed 's/ *$//')): to stop"
        TARGETS="${TARGETS} ${pid}"
    done < "${STAGE}/procs"
    rm -f "${STAGE}/procs"
    LOG "Processes to stop:${TARGETS:- none}"
}
stop_fros() {
    if [ -n "${TARGETS}" ]; then
        LOG "Stopping Free Rabbits OS's processes:${TARGETS}"
        /bin/kill -STOP ${TARGETS} 2> /dev/null
        /bin/kill -KILL ${TARGETS} 2> /dev/null
        sleep 1
    fi
    proc_list > "${STAGE}/procs"
    LOG "Still running: $(awk '{ printf "%s ", $2 }' "${STAGE}/procs")"
    rm -f "${STAGE}/procs"
}

# The rollback writes back sauvegarde/data: say whether mtd1 and mtd2 still hold just that.
compare_backup() { # mtdN md5
    local sum
    [ -n "$2" ] || return 0
    mtd_info "$1"
    rm -f "${STAGE}/whole"
    if /sbin/nanddump -o -b -l "${MTD_SIZE}" -f "${STAGE}/whole" "/dev/$1" > /dev/null 2>&1; then
        sum=$(md5_of "${STAGE}/whole")
    else
        sum=unreadable
    fi
    rm -f "${STAGE}/whole"
    if [ "${sum}" = "$2" ]; then
        LOG "Backup: $1 MATCH (${sum}): the backup restores it as it is now"
    else
        LOG "Backup: $1 DIFFERENT (${sum}, backup $2)"
    fi
}

# Everything that must hold before the flash is touched. With USR, usr.tar.gz too.
check_all() { # [usr]
    local sectors raw ram free need f
    FAIL_CODE=2
    . "${STAGE}/install.conf" || ERROR "can't read install.conf"
    [ -n "${BUILD_ID}" ] && [ -n "${KERNEL_MD5}" ] && [ -n "${ROOTFS_MD5}" ] && [ -n "${USR_MD5}" ] ||
        ERROR "install.conf lacks a build id or a checksum"
    LOG "LapiNou migration from Free Rabbits OS: build ${BUILD_ID} (${BUILD_VERSION}), mode ${MODE}"
    LOG "Rabbit: rootfs $(cat /karotz/etc/rootfs_version 2> /dev/null), kernel $(uname -r 2> /dev/null)"
    [ ! -f "${CONF}/lapinlibre_build_id" ] ||
        ERROR "this rabbit already runs LapinLibre: update it with the USB key instead"
    for f in /sbin/nandwrite /sbin/nanddump; do
        [ -x "${f}" ] || ERROR "no ${f}"
    done
    [ -x /sbin/flash_erase ] || [ -x /sbin/flash_eraseall ] || ERROR "no flash_erase or flash_eraseall"
    [ -x /bin/kill ] || ERROR "no /bin/kill"
    # / must be the RAM disk (the root filesystem is then not read from mtd2 while it is rewritten)
    # and /usr the yaffs partition mtd6.
    awk '$2 == "/usr" && $3 == "yaffs" && $1 == "/dev/mtdblock6" { found = 1 } END { exit !found }' /proc/mounts ||
        ERROR "/usr is not mtd6's yaffs"
    grep -q 'root=/dev/ram' /proc/cmdline 2> /dev/null || ERROR "the root filesystem isn't the RAM disk"
    for f in install-lib.sh applysettings.sh lapinou-conf.sh lapinlibre.txt; do
        [ -f "${STAGE}/${f}" ] || ERROR "${f} is missing from the staged install files"
    done
    check_md5 "${STAGE}/zImage" "${KERNEL_MD5}" zImage
    check_md5 "${STAGE}/rootfs.img.gz" "${ROOTFS_MD5}" rootfs.img.gz
    gzip -t "${STAGE}/rootfs.img.gz" 2> /dev/null || ERROR "rootfs.img.gz doesn't decompress"
    check_partition mtd1 Kernel "${STAGE}/zImage" zImage
    check_partition mtd2 ramdisk "${STAGE}/rootfs.img.gz" rootfs.img.gz
    mtd_info mtd6
    [ "${MTD_NAME}" = '"yaffs"' ] || ERROR "mtd6 is ${MTD_NAME:-missing}, not \"yaffs\""
    sectors=$(cat /sys/block/ram0/size 2> /dev/null)
    case "${sectors}" in
    ''|*[!0-9]*) LOG "RAM disk size unknown: not checked" ;;
    *)
        ram=$((sectors * 512))
        raw=$(($(gzip -dc "${STAGE}/rootfs.img.gz" | wc -c)))
        [ "${raw}" -le "${ram}" ] || ERROR "the root filesystem is ${raw} bytes uncompressed; the RAM disk holds ${ram}"
        ;;
    esac
    # The read-back works with these options: one erase block of mtd1, read only.
    rm -f "${STAGE}/probe"
    mtd_info mtd1
    /sbin/nanddump -o -b -l "${MTD_ERASE}" -f "${STAGE}/probe" /dev/mtd1 > /dev/null 2>&1 ||
        ERROR "nanddump can't read mtd1"
    [ "$(($(wc -c < "${STAGE}/probe")))" -eq "${MTD_ERASE}" ] || ERROR "nanddump's read of mtd1 has the wrong size"
    LOG "Read-back check: mtd1's first block has md5 $(md5_of "${STAGE}/probe")"
    rm -f "${STAGE}/probe"
    # The Wi-Fi network the rabbit uses now: LapinLibre starts the same wpa_supplicant with it.
    [ -s "${CONF}/wpa.conf" ] && grep -q 'network={' "${CONF}/wpa.conf" && grep -q 'ssid=' "${CONF}/wpa.conf" ||
        ERROR "${CONF}/wpa.conf has no Wi-Fi network"
    # The migration keeps that network: settings that would replace it (applysettings.sh rewrites
    # wpa.conf and network.conf when WIFI_SSID is set) are refused.
    for f in WIFI_SSID WIFI_PASSWORD IP; do
        [ -z "$(setting "${f}")" ] ||
            ERROR "lapinlibre.txt sets ${f}: the migration keeps the rabbit's Wi-Fi and address; leave it empty"
    done
    LOG "Settings files: $(ls "${CONF}" | tr '\n' ' ')"
    plan_network
    case "${NET_PLAN}" in
    kept) LOG "Network: ${CONF}/network.conf kept: $(tr '\n' ' ' < "${CONF}/network.conf")" ;;
    dhcp) LOG "Network: DHCP" ;;
    *) LOG "Network: fixed address ${NET_IP}, netmask ${NET_MASK}, gateway ${NET_GW}, DNS ${NET_DNS}" ;;
    esac
    ERROR_SOUND=none bash "${STAGE}/applysettings.sh" --check || ERROR "the settings failed their check"
    free=$(df -k /usr | tail -n 1 | awk '{ print $(NF - 2) }')
    if [ "$1" = usr ]; then
        check_md5 "${USR_STAGE}/usr.tar.gz" "${USR_MD5}" usr.tar.gz
        gzip -t "${USR_STAGE}/usr.tar.gz" 2> /dev/null || ERROR "usr.tar.gz doesn't decompress"
        need=10240
    else
        need=$(( ${USR_SIZE:-0} / 1024 + 10240 ))
    fi
    case "${free}" in ''|*[!0-9]*) ERROR "can't read /usr's free space" ;; esac
    [ "${free}" -ge "${need}" ] || ERROR "/usr has ${free} kB free; ${need} kB needed"
    if [ "${MODE}" = check ]; then
        compare_backup mtd1 "${BACKUP_MTD1_MD5:-}"
        compare_backup mtd2 "${BACKUP_MTD2_MD5:-}"
    fi
    plan_stop
    LOG "Checks passed"
}

# Erase a partition, write its image, read it back and compare. Only mtd1 and mtd2.
write_partition() { # mtdN image
    local size want got len
    case "$1" in
    mtd1|mtd2) ;;
    *) FAIL_CODE=1 ERROR "refusing to write $1" ;;
    esac
    mtd_info "$1"
    size=$(($(wc -c < "$2")))
    want=$(md5_of "$2")
    LOG "Writing ${2##*/} (${size} bytes) to $1"
    erase_partition "/dev/$1" > /dev/null || ERROR "erasing $1 failed"
    /sbin/nandwrite -pm "/dev/$1" "$2" > /dev/null || ERROR "writing ${2##*/} to $1 failed"
    len=$(( (size + MTD_ERASE - 1) / MTD_ERASE * MTD_ERASE ))
    rm -f "${STAGE}/readback"
    if /sbin/nanddump -o -b -l "${len}" -f "${STAGE}/readback" "/dev/$1" > /dev/null 2>&1; then
        got=$(prefix_md5 "${STAGE}/readback" "${size}")
    else
        got="unreadable"
    fi
    rm -f "${STAGE}/readback"
    if [ "${got}" != "${want}" ]; then
        LOG "ERROR: $1 reads back as ${got}, not ${want}: /usr is untouched, do not reboot"
        nose_solid FF0000
        exit 3
    fi
    LOG "$1 read back: ${got}"
}

# /usr emptied as the key's clean_usr does, sparing the staged usr.tar.gz.
clean_usr_but_stage() {
    remove_all_but /usr etc karotz lost+found .lapinou-migrate
    [ -d /usr/etc ] && remove_all_but /usr/etc conf
    [ -d /usr/karotz ] && remove_all_but /usr/karotz apps messages uuid.conf
    return 0
}

install() {
    local f
    check_all usr
    # From here on, a dropped SSH session must not stop the script half-way.
    trap '' HUP PIPE
    FAIL_CODE=1
    stop_fros
    nose_pulse FFFF00 500
    write_partition mtd1 "${STAGE}/zImage"
    write_partition mtd2 "${STAGE}/rootfs.img.gz"

    # /usr: the settings kept aside (in RAM), /usr cleaned, the new one unpacked.
    for f in ${KEPT}; do
        rm -f "${STAGE}/kept-${f}"
        [ -f "${CONF}/${f}" ] && cp -p "${CONF}/${f}" "${STAGE}/kept-${f}"
    done
    LOG "Cleaning /usr"
    clean_usr_but_stage
    rm -f "${CONF}/freerabbits_build_id"
    LOG "Unpacking usr.tar.gz into /usr"
    if ! unpack_into_usr "${USR_STAGE}/usr.tar.gz"; then
        LOG "Unpacking failed: cleaning /usr and trying once more"
        clean_usr_but_stage
        unpack_into_usr "${USR_STAGE}/usr.tar.gz" || ERROR "unpacking usr.tar.gz into /usr failed twice"
    fi
    rm -rf "${USR_STAGE}"
    mkdir -p "${CONF}"
    for f in ${KEPT}; do
        if [ -f "${STAGE}/kept-${f}" ]; then
            cp -p "${STAGE}/kept-${f}" "${CONF}/${f}" || ERROR "can't put the rabbit's ${f} back"
            rm -f "${STAGE}/kept-${f}"
        fi
    done
    if [ "${NET_PLAN}" != kept ]; then
        # The address settings LapinLibre's network.sh reads: empty IP means DHCP.
        printf 'IP="%s"\nNETMASK="%s"\nGATEWAY="%s"\nDNS="%s"\n' "${NET_IP}" "${NET_MASK}" "${NET_GW}" "${NET_DNS}" \
            > "${CONF}/network.conf" || ERROR "can't write network.conf"
        LOG "network.conf written (${NET_PLAN})"
    fi

    echo "${BUILD_VERSION}" > "${CONF}/sys_version"
    rm -rf /usr/install /usr/yaffs* /usr/.install_yaffs_start
    ERROR_SOUND=none bash "${STAGE}/applysettings.sh" || ERROR "the settings were not applied"
    cp "${STAGE}/build_id" "${CONF}/lapinlibre_build_id" || ERROR "can't record the build id"
    # The new root filesystem's inittab is copied at the next boot.
    rm -f /usr/etc/inittab
    sync
    nose_solid 00FF00
    # The reboot from here: a new SSH login may no longer work, since Free Rabbits OS's Dropbear keeps
    # its host keys in the /usr that was just replaced. Two seconds let this session end first.
    LOG "Installed build ${BUILD_ID}: rebooting in 2 s"
    ( sleep 2; /sbin/reboot -f ) < /dev/null > /dev/null 2>&1 &
}

case "${MODE}" in
check) check_all ;;
install) install ;;
*) echo "usage: on-rabbit.sh check|install RABBIT_IP" >&2; exit 2 ;;
esac
exit 0
