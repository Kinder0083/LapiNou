#!/bin/bash
# SPDX-License-Identifier: MIT
# The key's settings (lapinlibre.txt) onto the rabbit: the Wi-Fi network,
# a fixed address or DHCP, the SSH key, the telnet choice, the web page's password, the app server
# and the language, all in /usr/etc/conf, which installs keep.
# A value left empty keeps what the rabbit has.
#
#   applysettings.sh --check    check the file only (the install runs this before erasing)
#   applysettings.sh            check, then apply
#
# Runs on whichever system the rabbit has when the key's install starts, so only bash and the
# common tools. The file may come from Windows (CRLF line ends, a UTF-8 byte-order mark) and may
# quote its values. Never logs the Wi-Fi password or the web password.

source /mnt/usbkey/install-lib.sh

FILE=/mnt/usbkey/lapinlibre.txt
CONF=/usr/etc/conf
BOM=$(printf '\357\273\277')

WIFI_SSID= WIFI_PASSWORD= IP= NETMASK= GATEWAY= DNS= SSH_PUBKEY= TELNET= WEB_PASSWORD=
SERVER= TTS_SERVER= ASR_SERVER= LANGUAGE= HOSTNAME=
LAPINOU_SERVER= MQTT_HOST= MQTT_PORT= MQTT_USER= MQTT_PASSWORD= TZ=
CLOUD_STT_URL= CLOUD_STT_KEY= CLOUD_LLM_KIND= CLOUD_LLM_URL= CLOUD_LLM_MODEL= CLOUD_LLM_KEY= CLOUD_TTS_URL= CLOUD_TTS_KEY=
problems=""
problem() { problems="${problems}${problems:+; }$1"; }

if [ -f "${FILE}" ]; then
    n=0
    while IFS= read -r line || [ -n "${line}" ]; do
        n=$((n + 1))
        line=${line%$'\r'}
        [ ${n} -eq 1 ] && line=${line#${BOM}}
        # Trim, and skip blank lines and comments.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "${line}" in ''|'#'*) continue ;; esac
        case "${line}" in *=*) ;; *) problem "line ${n} has no =" ; continue ;; esac
        key=${line%%=*}
        value=${line#*=}
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        case "${value}" in \"*\") value=${value#\"}; value=${value%\"} ;; \'*\') value=${value#\'}; value=${value%\'} ;; esac
        case "${key}" in
        WIFI_SSID|WIFI_PASSWORD|IP|NETMASK|GATEWAY|DNS|SSH_PUBKEY|TELNET|WEB_PASSWORD|SERVER|TTS_SERVER|ASR_SERVER|LANGUAGE|HOSTNAME|\
        LAPINOU_SERVER|MQTT_HOST|MQTT_PORT|MQTT_USER|MQTT_PASSWORD|TZ|CLOUD_STT_URL|CLOUD_STT_KEY|CLOUD_LLM_KIND|CLOUD_LLM_URL|\
        CLOUD_LLM_MODEL|CLOUD_LLM_KEY|CLOUD_TTS_URL|CLOUD_TTS_KEY)
            printf -v "${key}" '%s' "${value}" ;;
        *) problem "line ${n}: unknown setting ${key}" ;;
        esac
    done < "${FILE}"
fi

is_address() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

# The Wi-Fi network: both or neither; the characters wpa_supplicant's quoted strings can hold.
if [ -n "${WIFI_SSID}${WIFI_PASSWORD}" ]; then
    [ -n "${WIFI_SSID}" ] || problem "WIFI_PASSWORD is set but WIFI_SSID is empty"
    [ -n "${WIFI_PASSWORD}" ] || problem "WIFI_SSID is set but WIFI_PASSWORD is empty"
    case "${WIFI_SSID}${WIFI_PASSWORD}" in *\"*|*\\*) problem "the Wi-Fi name and password can't contain \" or \\" ;; esac
    [ ${#WIFI_PASSWORD} -ge 8 ] && [ ${#WIFI_PASSWORD} -le 63 ] || [ -z "${WIFI_PASSWORD}" ] || problem "WIFI_PASSWORD must be 8 to 63 characters (WPA)"
elif [ ! -s "${CONF}/wpa.conf" ]; then
    problem "no Wi-Fi settings: fill in WIFI_SSID and WIFI_PASSWORD in lapinlibre.txt"
fi
if [ -n "${IP}" ]; then
    [ -n "${WIFI_SSID}" ] || problem "IP is set: give WIFI_SSID and WIFI_PASSWORD with it"
    for v in IP NETMASK GATEWAY DNS; do
        is_address "${!v}" || problem "${v} must be an address like 192.168.1.10 when IP is set"
    done
fi
if [ -n "${SSH_PUBKEY}" ] && ! [[ "${SSH_PUBKEY}" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+)\ [A-Za-z0-9+/=]+(\ .*)?$ ]]; then
    problem "SSH_PUBKEY isn't one public key line (ssh-ed25519 AAAA... comment)"
fi
case "${TELNET}" in ''|yes|no) ;; *) problem "TELNET must be yes, no or empty" ;; esac
# The web password: printable ASCII without spaces (values are trimmed), and none of the
# characters the web server's configuration or the settings file give a meaning to.
web_password_ok() {
    local LC_ALL=C
    [ ${#1} -ge 8 ] && [ ${#1} -le 64 ] || return 1
    case "$1" in *[!!-~]*|*\"*|*\\*|*:*) return 1 ;; esac
    return 0
}
[ -z "${WEB_PASSWORD}" ] || web_password_ok "${WEB_PASSWORD}" ||
    problem "WEB_PASSWORD must be 8 to 64 characters, without spaces, \", \\ or :"
# The app server: a host name or address (SERVER), or a URL for speech and host:port for speech
# recognition when those run elsewhere.
is_host() { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; }
[ -z "${SERVER}" ] || is_host "${SERVER}" || problem "SERVER must be a host name or address, like 192.168.1.10"
[ -z "${TTS_SERVER}" ] || [[ "${TTS_SERVER}" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?$ ]] ||
    problem "TTS_SERVER must be a URL like http://192.168.1.10:8081"
[ -z "${ASR_SERVER}" ] || [[ "${ASR_SERVER}" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] ||
    problem "ASR_SERVER must be host or host:port, like 192.168.1.10:8443"
[ -z "${LANGUAGE}" ] || [[ "${LANGUAGE}" =~ ^[a-z][a-z]-[A-Z][A-Z]$ ]] || problem "LANGUAGE must look like en-US or fr-FR"
[ -z "${HOSTNAME}" ] || [[ "${HOSTNAME}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] ||
    problem "HOSTNAME must be lower-case letters, digits and dashes, like lapinlibre or kitchen-rabbit"

# LapiNou's settings: any text goes in a stored value safely, so the checks are for typos.
# Host names and addresses, ports, URLs, a model's name, the time zone, no control characters.
printable() { local LC_ALL=C; case "$1" in *[[:cntrl:]]*) return 1 ;; esac; return 0; }
[ -z "${LAPINOU_SERVER}" ] || is_host "${LAPINOU_SERVER}" || problem "LAPINOU_SERVER must be a host name or address, like 192.168.1.10"
[ -z "${MQTT_HOST}" ] || is_host "${MQTT_HOST}" || problem "MQTT_HOST must be a host name or address, like 192.168.1.50"
[ -z "${MQTT_PORT}" ] || { [[ "${MQTT_PORT}" =~ ^[0-9]{1,5}$ ]] && [ "${MQTT_PORT}" -ge 1 ] && [ "${MQTT_PORT}" -le 65535 ]; } ||
    problem "MQTT_PORT must be a number from 1 to 65535"
for v in MQTT_USER MQTT_PASSWORD CLOUD_STT_KEY CLOUD_LLM_KEY CLOUD_TTS_KEY; do
    printable "${!v}" || problem "${v} can't have control characters"
done
for v in CLOUD_STT_URL CLOUD_LLM_URL CLOUD_TTS_URL; do
    [ -z "${!v}" ] || [[ "${!v}" =~ ^https?://[^[:space:]]+$ ]] || problem "${v} must be a URL like https://api.example.com/v1"
done
case "${CLOUD_LLM_KIND}" in ''|anthropic|openai) ;; *) problem "CLOUD_LLM_KIND must be anthropic or openai" ;; esac
[ -z "${CLOUD_LLM_MODEL}" ] || [[ "${CLOUD_LLM_MODEL}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*$ ]] || problem "CLOUD_LLM_MODEL isn't a model name"
[ -z "${TZ}" ] || [[ "${TZ}" =~ ^[A-Za-z0-9][A-Za-z0-9_+:./,-]*$ ]] || problem "TZ must be a time zone like Europe/Paris"

[ -z "${problems}" ] || ERROR "lapinlibre.txt: ${problems}"
[ "$1" = "--check" ] && { LOG "Settings: lapinlibre.txt is fine"; exit 0; }

mkdir -p "${CONF}"
if [ -n "${WIFI_SSID}" ]; then
    printf 'network={\nssid="%s"\npsk="%s"\nkey_mgmt=WPA-PSK\nscan_ssid=1\nproto=WPA RSN\n}\n' \
        "${WIFI_SSID}" "${WIFI_PASSWORD}" > "${CONF}/wpa.conf"
    chmod 600 "${CONF}/wpa.conf"
    # The address settings, read by /usr/scripts/waitfornetwork.sh: empty IP means DHCP.
    printf 'IP="%s"\nNETMASK="%s"\nGATEWAY="%s"\nDNS="%s"\n' "${IP}" "${NETMASK}" "${GATEWAY}" "${DNS}" > "${CONF}/network.conf"
    LOG "Settings: Wi-Fi network ${WIFI_SSID}, $([ -n "${IP}" ] && echo "address ${IP}" || echo DHCP)"
else
    LOG "Settings: Wi-Fi kept"
fi
if [ -n "${SSH_PUBKEY}" ]; then
    printf '%s\n' "${SSH_PUBKEY}" > "${CONF}/authorized_keys"
    chmod 600 "${CONF}/authorized_keys"
    LOG "Settings: SSH key ${SSH_PUBKEY%% *} ${SSH_PUBKEY##* }"
fi
if [ -n "${TELNET}" ]; then
    echo "${TELNET}" > "${CONF}/telnet"
    LOG "Settings: telnet ${TELNET}"
fi
# The web password as given; the rabbit hashes it at its next start (inetd.sh) and removes this.
# None given: no password, so any earlier one goes.
if [ -n "${WEB_PASSWORD}" ]; then
    rm -f "${CONF}/web_password.new"
    (umask 077 && printf '%s\n' "${WEB_PASSWORD}" > "${CONF}/web_password.new") &&
        chmod 600 "${CONF}/web_password.new" && mv -f "${CONF}/web_password.new" "${CONF}/web_password" ||\
        ERROR "can't write the web password"
    LOG "Settings: web password set"
else
    rm -f "${CONF}/web_password" "${CONF}/web_password.new" "${CONF}/web_password.hash"
    LOG "Settings: no web password"
fi

# KEY=VALUE in [SECTION] of an ini file, replacing the key's line or adding it (and the section).
set_ini() { # file section key value
    [ -f "$1" ] || : > "$1"
    awk -v s="[$2]" -v k="$3" -v v="$4" '
        $0 == s { print; in_s = 1; next }
        /^\[/ { if (in_s && !done) { print k "=" v; done = 1 } in_s = 0 }
        in_s && index($0, k "=") == 1 { if (!done) { print k "=" v; done = 1 } next }
        { print }
        END { if (!done) { if (!in_s) print s; print k "=" v } }' "$1" > "$1.new" && mv -f "$1.new" "$1"
}
# The app server (tools/app-server: the platform on 9123, apps on 8080, speech on 8081, speech
# recognition on 8443), read by the controller (karotz.conf) and voice-daemon (voice.conf).
if [ -n "${SERVER}" ]; then
    set_ini "${CONF}/karotz.conf" karotz ip_server "${SERVER}"
    set_ini "${CONF}/karotz.conf" karotz port_server 9123
    set_ini "${CONF}/karotz.conf" karotz url_app "http://${SERVER}:8080"
    set_ini "${CONF}/karotz.conf" karotz domain_app "${SERVER}"
    # The controller reboots the rabbit when the update server offers newer versions: a closed
    # port on the rabbit itself makes that check fail at once.
    set_ini "${CONF}/karotz.conf" karotz url_update "http://127.0.0.1:1"
    LOG "Settings: app server ${SERVER}"
fi
# The controller's platform account, whatever the app server: any values, but both set.
[ -s "${CONF}/account.conf" ] || printf '[account]\nid=lapinlibre\npwd=lapinlibre\n' > "${CONF}/account.conf"
TTS_URL=${TTS_SERVER:-${SERVER:+http://${SERVER}:8081}}
if [ -n "${TTS_URL}" ]; then
    # voice-daemon looks a language's server up by its first two letters.
    for l in en fr de es it nl pt; do set_ini "${CONF}/voice.conf" tts "${l}" "${TTS_URL%/}/${l}"; done
    LOG "Settings: speech ${TTS_URL}"
fi
ASR=${ASR_SERVER:-${SERVER:+${SERVER}:8443}}
if [ -n "${ASR}" ]; then
    host=${ASR%%:*}; port=8443
    case "${ASR}" in *:*) port=${ASR##*:} ;; esac
    set_ini "${CONF}/voice.conf" asr nbserver 1
    set_ini "${CONF}/voice.conf" asr server0 "${host}"
    set_ini "${CONF}/voice.conf" asr port "${port}"
    LOG "Settings: speech recognition ${host}:${port}"
fi
# The rabbit's name, which linuxrc sets and mDNS answers to (<name>.local).
if [ -n "${HOSTNAME}" ]; then
    echo "${HOSTNAME}" > "${CONF}/hostname"
    LOG "Settings: name ${HOSTNAME}"
fi
if [ -n "${LANGUAGE}" ]; then
    set_ini "${CONF}/karotz.conf" karotz lang "${LANGUAGE}"
    LOG "Settings: language ${LANGUAGE}"
fi

# LapiNou's settings, in /usr/etc/conf/lapinou.conf; the passwords and keys in lapinou.secrets (0600).
# The same functions as image/core/usr/share/lapinou/conf.sh, which this system may not have yet.
LAPINOU_CONF_DIR=${CONF}
_lapinou_dir() { printf '%s' "${LAPINOU_CONF_DIR:-/usr/etc/conf}"; }

# Is $1 a name this format allows for a section or a key?
_lapinou_name_ok() {
    case "$1" in
    ''|*[!A-Za-z0-9_.-]*) return 1 ;;
    esac
    return 0
}

# _lapinou_read FILE SECTION KEY: print the stored value, decoded, or return 1 when there is none.
_lapinou_read() {
    [ -f "$1" ] || return 1
    LAPINOU_S="$2" LAPINOU_K="$3" awk '
        BEGIN { s = "[" ENVIRON["LAPINOU_S"] "]"; k = ENVIRON["LAPINOU_K"]; kl = length(k) + 1 }
        /^\[/ { in_s = ($0 == s); next }
        in_s && substr($0, 1, kl) == k "=" { v = substr($0, kl + 1); found = 1 }
        END {
            if (!found) exit 1
            out = ""; n = length(v)
            for (i = 1; i <= n; i++) {
                c = substr(v, i, 1)
                if (c == "\\" && i < n) {
                    i++; c = substr(v, i, 1)
                    if (c == "n") c = "\n"; else if (c == "r") c = "\r"
                    else if (c != "\\" && c != "=") c = "\\" c
                }
                out = out c
            }
            printf "%s\n", out
        }' "$1"
}

# _lapinou_write FILE MODE SECTION KEY VALUE: replace the key's line (and drop repeats), or add it
# (and the section), through a temporary file in the same directory.
_lapinou_write() {
    local file="$1" mode="$2" tmp
    tmp="${file}.$$.new"
    [ -f "${file}" ] || { (umask 077; : > "${file}") || return 1; }
    (
        umask 077
        LAPINOU_S="$3" LAPINOU_K="$4" LAPINOU_V="$5" awk '
            BEGIN {
                s = "[" ENVIRON["LAPINOU_S"] "]"; k = ENVIRON["LAPINOU_K"]; kl = length(k) + 1
                v = ENVIRON["LAPINOU_V"]; enc = ""; n = length(v)
                for (i = 1; i <= n; i++) {
                    c = substr(v, i, 1)
                    if (c == "\\") c = "\\\\"; else if (c == "\n") c = "\n"
                    else if (c == "\r") c = "\r"; else if (c == "=") c = "\="
                    enc = enc c
                }
                line = k "=" enc
            }
            /^\[/ { if (in_s && !done) { print line; done = 1 } in_s = ($0 == s) }
            in_s && substr($0, 1, kl) == k "=" { if (!done) { print line; done = 1 } next }
            { print }
            END { if (!done) { if (!in_s) print s; print line } }' "${file}" > "${tmp}"
    ) && chmod "${mode}" "${tmp}" && mv -f "${tmp}" "${file}" || { rm -f "${tmp}"; return 1; }
}

lapinou_get() { # section key [default]
    _lapinou_name_ok "$1" && _lapinou_name_ok "$2" || { printf '%s\n' "${3-}"; return 2; }
    local v
    v=$(_lapinou_read "$(_lapinou_dir)/lapinou.conf" "$1" "$2") && [ -n "${v}" ] && { printf '%s\n' "${v}"; return 0; }
    printf '%s\n' "${3-}"
}

lapinou_set() { # section key value
    _lapinou_name_ok "$1" && _lapinou_name_ok "$2" || { echo "lapinou_set: bad section or key" >&2; return 2; }
    mkdir -p "$(_lapinou_dir)" && _lapinou_write "$(_lapinou_dir)/lapinou.conf" 644 "$1" "$2" "$3"
}

lapinou_secret_get() { # key
    _lapinou_name_ok "$1" || return 2
    _lapinou_read "$(_lapinou_dir)/lapinou.secrets" secrets "$1" || echo ""
}

lapinou_secret_set() { # key value
    _lapinou_name_ok "$1" || { echo "lapinou_secret_set: bad key" >&2; return 2; }
    mkdir -p "$(_lapinou_dir)" && _lapinou_write "$(_lapinou_dir)/lapinou.secrets" 600 secrets "$1" "$2"
}
# A value left empty keeps what the rabbit has. Never logs a password or key.
ln_set() { lapinou_set "$1" "$2" "$3" || ERROR "can't write ${CONF}/lapinou.conf"; }
ln_secret() { lapinou_secret_set "$1" "$2" || ERROR "can't write ${CONF}/lapinou.secrets"; }
if [ -n "${LAPINOU_SERVER}" ]; then
    ln_set server host "${LAPINOU_SERVER}"; ln_set server enabled 1
    LOG "Settings: LapiNou server ${LAPINOU_SERVER}"
fi
if [ -n "${MQTT_HOST}" ]; then
    ln_set mqtt host "${MQTT_HOST}"; ln_set mqtt enabled 1
    LOG "Settings: MQTT ${MQTT_HOST}"
fi
[ -z "${MQTT_PORT}" ] || ln_set mqtt port "${MQTT_PORT}"
[ -z "${MQTT_USER}" ] || ln_set mqtt user "${MQTT_USER}"
[ -z "${MQTT_PASSWORD}" ] || { ln_secret mqtt_password "${MQTT_PASSWORD}"; LOG "Settings: MQTT password set"; }
[ -z "${CLOUD_STT_URL}" ] || ln_set cloud stt_url "${CLOUD_STT_URL}"
[ -z "${CLOUD_STT_KEY}" ] || ln_secret cloud_stt_key "${CLOUD_STT_KEY}"
[ -z "${CLOUD_LLM_KIND}" ] || ln_set cloud llm_kind "${CLOUD_LLM_KIND}"
[ -z "${CLOUD_LLM_URL}" ] || ln_set cloud llm_url "${CLOUD_LLM_URL}"
[ -z "${CLOUD_LLM_MODEL}" ] || ln_set cloud llm_model "${CLOUD_LLM_MODEL}"
[ -z "${CLOUD_LLM_KEY}" ] || ln_secret cloud_llm_key "${CLOUD_LLM_KEY}"
[ -z "${CLOUD_TTS_URL}" ] || ln_set cloud tts_url "${CLOUD_TTS_URL}"
[ -z "${CLOUD_TTS_KEY}" ] || ln_secret cloud_tts_key "${CLOUD_TTS_KEY}"
if [ -n "${CLOUD_STT_URL}${CLOUD_LLM_URL}${CLOUD_TTS_URL}" ]; then
    ln_set cloud enabled 1
    LOG "Settings: cloud services"
fi
if [ -n "${TZ}" ]; then
    ln_set time tz "${TZ}"
    LOG "Settings: time zone ${TZ}"
fi
exit 0
