#!/bin/sh
# SPDX-License-Identifier: MIT
# webroot.sh: what the LapiNou web page does as root, run only by lapinou-webroot (setuid, for the
# group web) with a clean environment. Every value comes on the standard input, never as an
# argument; the arguments are checked words. Each command prints one JSON object on one line:
# {"return":"0",...} or {"return":"1","msg":"<French message>"}. Nothing is ever evaluated: values
# are only expanded inside double quotes and stored with conf.sh, which keeps any text as given.
#
#   get                       every setting the page shows, and for each secret only whether it is set
#   set                       settings, one per line on stdin: SECTION<tab>KEY<tab>VALUE; all are
#                             checked before any is written
#   secret NAME               a secret (the value on stdin; empty removes it); wifi_password goes in
#                             wpa.conf, web_password is the page's own password (httpd's login)
#   ota check|install|status  lapinou-ota check (its answer kept in /usr/ota/check.json, which
#                             lapinou-mqtt publishes), lapinou-ota install started on its own, or
#                             where an install is and its log
#   restart mqtt|cloud|all    lapinou-mqtt or lapinou-cloud started again (immortaldog does it), or
#                             every LapiNou daemon (S40lapinlibre restart)
#   test mqtt|server|stt|llm|tts
#                             the MQTT bridge's connection, the LapiNou server's /api/health, or a
#                             cloud service's answer to its key (the models list)
#
# Writes hold conf.sh's lock (lapinou.lock); the network files are replaced whole, under the same
# lock. The settings' rules: docs/lapinou/page-web.md.

# ShellCheck: A && B || C means "C unless both A and B succeed"; with_lock runs functions by name.
# shellcheck disable=SC2015,SC2317
PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
LC_ALL=C
export LC_ALL
umask 022
# No file name is ever made from a value.
set -f

CONF=/usr/etc/conf
SECRETS=/usr/etc/conf/lapinou.secrets
WPA=/usr/etc/conf/wpa.conf
NETWORK=/usr/etc/conf/network.conf
HOSTNAME_FILE=/usr/etc/conf/hostname
KAROTZ_CONF=/usr/etc/conf/karotz.conf
VOICE_CONF=/usr/etc/conf/voice.conf
OTA=/usr/karotz/bin/lapinou-ota
OTA_DIR=/usr/ota
OTA_LOCK=/tmp/lapinou-ota.lock
OTA_CHECK=/usr/ota/check.json
OTA_AVAILABLE=/usr/ota/available
OTA_LOG=/usr/ota/last.log
INETD=/karotz/scripts/inetd.sh
S40=/usr/etc/init.d/S40lapinlibre
CACERT=/usr/etc/ssl/certs/ca-certificates.crt
SECRET_NAMES="mqtt_password cloud_stt_key cloud_llm_key cloud_tts_key ota_token"
NL='
'
TAB=$(printf '\t')

. /usr/share/lapinou/conf.sh

# --- replies ---

# js VALUE: VALUE as a JSON string, in J.
js() {
    J=$(printf '%s' "$1" | awk 'BEGIN { RS = "\001" }
        { n = length($0)
          for (i = 1; i <= n; i++) {
              c = substr($0, i, 1)
              if (c == "\\") c = "\\\\"; else if (c == "\"") c = "\\\""; else if (c == "\n") c = "\\n"
              else if (c == "\r") c = "\\r"; else if (c == "\t") c = "\\t"
              out = out c
          } }
        END { printf "\"%s\"", out }')
}
ok() { printf '{"return":"0"%s}\n' "${1:+,$1}"; exit 0; }
fail() { js "$1"; printf '{"return":"1","msg":%s}\n' "$J"; exit 1; }

# --- checks: each is true for a value its setting takes ---

printable() { case $1 in *[[:cntrl:]]*) return 1 ;; esac; [ ${#1} -le 256 ]; }
is_flag() { case $1 in 0 | 1) return 0 ;; esac; return 1; }
is_host() {
    case $1 in '' | -* | *[!A-Za-z0-9.:-]*) return 1 ;; esac
    [ ${#1} -le 253 ]
}
is_port() {
    case $1 in '' | *[!0-9]* | 0*) return 1 ;; esac
    [ ${#1} -le 5 ] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}
is_int_range() { # value min max
    case $1 in '' | -) return 1 ;; esac
    case ${1#-} in *[!0-9]*) return 1 ;; esac
    [ ${#1} -le 4 ] && [ "$1" -ge "$2" ] && [ "$1" -le "$3" ]
}
is_url() {
    case $1 in http://?* | https://?*) ;; *) return 1 ;; esac
    case $1 in *[[:space:][:cntrl:]\"\\]*) return 1 ;; esac
    [ ${#1} -le 256 ]
}
is_name() { # a model, a voice: letters, digits and . _ : / -
    case $1 in '' | *[!A-Za-z0-9._:/-]*) return 1 ;; esac
    [ ${#1} -le 64 ]
}
is_topic() { case $1 in '' | *[!A-Za-z0-9_/-]*) return 1 ;; esac; [ ${#1} -le 64 ]; }
is_ipv4() {
    case $1 in *[!0-9.]* | .* | *. | *..*) return 1 ;; esac
    _ifs=$IFS
    IFS=.
    # shellcheck disable=SC2086
    set -- $1
    IFS=$_ifs
    [ $# -eq 4 ] || return 1
    for _o; do
        [ ${#_o} -le 3 ] && [ "$_o" -le 255 ] || return 1
    done
}
is_hostname() {
    case $1 in '' | -* | *- | *[!a-z0-9-]*) return 1 ;; esac
    [ ${#1} -le 63 ]
}
is_tz() { case $1 in '' | [!A-Za-z0-9]* | *[!A-Za-z0-9_+:./,-]*) return 1 ;; esac; [ ${#1} -le 64 ]; }
is_lang() { case $1 in [a-z][a-z] | [a-z][a-z]-[A-Z][A-Z]) return 0 ;; esac; return 1; }
wifi_text_ok() { case $1 in *\"* | *\\* | *[[:cntrl:]]*) return 1 ;; esac; return 0; }

# check SECTION KEY VALUE: empty when the setting takes the value, else the French reason, in WHY.
# An empty value is taken by every setting but the Wi-Fi name: the default comes back.
check() {
    WHY=""
    _v=$3
    case $1.$2 in
    mqtt.enabled | server.enabled | cloud.enabled) [ -z "$_v" ] || is_flag "$_v" || WHY="doit valoir 0 ou 1" ;;
    mqtt.host | server.host) [ -z "$_v" ] || is_host "$_v" || WHY="doit être un nom ou une adresse, comme 192.168.1.10" ;;
    mqtt.port) [ -z "$_v" ] || is_port "$_v" || WHY="doit être un nombre de 1 à 65535" ;;
    mqtt.user) printable "$_v" || WHY="ne peut pas contenir de caractère de contrôle (256 caractères au plus)" ;;
    mqtt.prefix | mqtt.discovery_prefix) [ -z "$_v" ] || is_topic "$_v" || WHY="ne peut avoir que des lettres, chiffres, _, - et /" ;;
    cloud.stt_url | cloud.llm_url | cloud.tts_url | ota.url)
        [ -z "$_v" ] || is_url "$_v" || WHY="doit être une adresse http:// ou https:// sans espace" ;;
    cloud.stt_model | cloud.llm_model | cloud.tts_model | cloud.tts_voice)
        [ -z "$_v" ] || is_name "$_v" || WHY="ne peut avoir que des lettres, chiffres et . _ : / -" ;;
    cloud.llm_kind) case $_v in '' | anthropic | openai) ;; *) WHY="doit valoir anthropic ou openai" ;; esac ;;
    cloud.llm_effort) case $_v in '' | low | medium | high | none) ;; *) WHY="doit valoir low, medium, high ou none" ;; esac ;;
    audio.volume) [ -z "$_v" ] || is_int_range "$_v" 0 100 || WHY="doit être un nombre de 0 à 100" ;;
    audio.mic_gain_db) [ -z "$_v" ] || is_int_range "$_v" -20 30 || WHY="doit être un nombre de -20 à 30" ;;
    assistant.language) [ -z "$_v" ] || is_lang "$_v" || WHY="doit être un code de langue comme fr ou fr-FR" ;;
    time.tz) [ -z "$_v" ] || is_tz "$_v" || WHY="doit être un fuseau horaire comme Europe/Paris" ;;
    network.hostname)
        [ -z "$_v" ] || is_hostname "$_v" || WHY="ne peut avoir que des minuscules, chiffres et tirets (63 au plus)" ;;
    network.ip | network.netmask | network.gateway | network.dns)
        [ -z "$_v" ] || is_ipv4 "$_v" || WHY="doit être une adresse comme 192.168.1.20, ou rester vide" ;;
    network.wifi_ssid)
        if [ -z "$_v" ] || [ ${#_v} -gt 32 ]; then
            WHY="doit avoir de 1 à 32 caractères"
        elif ! wifi_text_ok "$_v"; then
            WHY="ne peut pas contenir de guillemet (\") ni de barre oblique inverse (\\)"
        fi
        ;;
    *) WHY="n'est pas un réglage de la page" ;;
    esac
    [ -z "$WHY" ]
}

# --- the network files: network.conf (KEY="value" lines), wpa.conf and hostname ---

# net_get KEY: a value of network.conf, in NV.
net_get() {
    NV=""
    [ -f "$NETWORK" ] || return 0
    while IFS= read -r _l || [ -n "$_l" ]; do
        case $_l in "$1="*)
            NV=${_l#"$1="}
            NV=${NV#\"}
            NV=${NV%\"}
            ;;
        esac
    done < "$NETWORK"
}

# wpa_get FIELD: ssid or psk of wpa.conf (between its quotes), in WV.
wpa_get() {
    WV=""
    [ -f "$WPA" ] || return 0
    while IFS= read -r _l || [ -n "$_l" ]; do
        _l=${_l#"${_l%%[![:space:]]*}"}
        case $_l in "$1=\""*\")
            WV=${_l#"$1=\""}
            WV=${WV%\"}
            ;;
        esac
    done < "$WPA"
}

# replace FILE MODE: the standard input replaces FILE in one step.
replace() {
    cat > "$1.$$.new" && chmod "$2" "$1.$$.new" && mv -f "$1.$$.new" "$1" || { rm -f "$1.$$.new"; return 1; }
}

# wpa_write SSID PSK: wpa.conf as the USB key's applysettings.sh writes it.
wpa_write() {
    printf 'network={\nssid="%s"\npsk="%s"\nkey_mgmt=WPA-PSK\nscan_ssid=1\nproto=WPA RSN\n}\n' "$1" "$2" | replace "$WPA" 600
}

# net_write: network.conf from IP NETMASK GATEWAY DNS.
net_write() {
    printf 'IP="%s"\nNETMASK="%s"\nGATEWAY="%s"\nDNS="%s"\n' "$IP" "$NETMASK" "$GATEWAY" "$DNS" | replace "$NETWORK" 644
}

# with_lock COMMAND...: COMMAND holding conf.sh's lock.
with_lock() {
    mkdir -p "$CONF" || return 1
    (
        flock -x 8 || exit 1
        "$@"
    ) 8>> "$CONF/lapinou.lock"
}

# --- get ---

secret_set_flag() { # name: true when the secret has a value
    [ -n "$(lapinou_secret_get "$1")" ]
}

cmd_get() {
    out=""
    add() { js "$3"; out="$out${out:+,}\"$1.$2\":$J"; }
    for sk in mqtt.enabled mqtt.host mqtt.port mqtt.user mqtt.prefix mqtt.discovery_prefix server.enabled server.host \
        cloud.enabled cloud.stt_url cloud.stt_model cloud.llm_kind cloud.llm_url cloud.llm_model cloud.llm_effort \
        cloud.tts_url cloud.tts_model cloud.tts_voice audio.volume audio.mic_gain_db assistant.language time.tz ota.url \
        ota.repo; do
        add "${sk%%.*}" "${sk#*.}" "$(lapinou_get "${sk%%.*}" "${sk#*.}")"
    done
    for k in ip:IP netmask:NETMASK gateway:GATEWAY dns:DNS; do
        net_get "${k#*:}"
        add network "${k%%:*}" "$NV"
    done
    wpa_get ssid
    add network wifi_ssid "$WV"
    h=""
    [ -f "$HOSTNAME_FILE" ] && read -r h < "$HOSTNAME_FILE"
    add network hostname "$h"
    for s in $SECRET_NAMES; do
        if secret_set_flag "$s"; then f=true; else f=false; fi
        out="$out,\"$s\":{\"set\":$f}"
    done
    wpa_get psk
    if [ -n "$WV" ]; then f=true; else f=false; fi
    out="$out,\"wifi_password\":{\"set\":$f}"
    if [ -s "$CONF/web_password.hash" ]; then f=true; else f=false; fi
    out="$out,\"web_password\":{\"set\":$f}"
    ok "\"settings\":{$out}"
}

# --- set ---

# server_files HOST: the controller's and voice-daemon's servers, as applysettings.sh sets them for
# a LapiNou server (the platform 9123, apps 8080, speech 8081, speech recognition 8443).
server_files() {
    _lapinou_write "$KAROTZ_CONF" 644 karotz ip_server "$1" &&
        _lapinou_write "$KAROTZ_CONF" 644 karotz port_server 9123 &&
        _lapinou_write "$KAROTZ_CONF" 644 karotz url_app "http://$1:8080" &&
        _lapinou_write "$KAROTZ_CONF" 644 karotz domain_app "$1" || return 1
    for l in en fr de es it nl pt; do
        _lapinou_write "$VOICE_CONF" 644 tts "$l" "http://$1:8081/$l" || return 1
    done
    _lapinou_write "$VOICE_CONF" 644 asr nbserver 1 && _lapinou_write "$VOICE_CONF" 644 asr server0 "$1" &&
        _lapinou_write "$VOICE_CONF" 644 asr port 8443
}

# write_all: the checked lines of $LINES, written (conf.sh's lapinou_set holds its own lock; the
# network files are written here under it).
write_network() {
    net_get IP; IP=$NV
    net_get NETMASK; NETMASK=$NV
    net_get GATEWAY; GATEWAY=$NV
    net_get DNS; DNS=$NV
    wpa_get ssid; ssid=$WV
    wpa_get psk; psk=$WV
    net=""
    wifi=""
    _ifs=$IFS
    IFS=$NL
    for line in $LINES; do
        IFS=$_ifs
        s=${line%%"$TAB"*}
        rest=${line#*"$TAB"}
        k=${rest%%"$TAB"*}
        v=${rest#*"$TAB"}
        [ "$s" = network ] || continue
        case $k in
        ip) IP=$v net=1 ;;
        netmask) NETMASK=$v net=1 ;;
        gateway) GATEWAY=$v net=1 ;;
        dns) DNS=$v net=1 ;;
        wifi_ssid) ssid=$v wifi=1 ;;
        hostname)
            if [ -n "$v" ]; then
                printf '%s\n' "$v" | replace "$HOSTNAME_FILE" 644 || return 1
            else
                rm -f "$HOSTNAME_FILE"
            fi
            ;;
        esac
    done
    IFS=$_ifs
    if [ -n "$wifi" ] && [ -n "$psk" ]; then
        wpa_write "$ssid" "$psk" || return 1
    fi
    [ -z "$net" ] || net_write
}

cmd_set() {
    LINES=$(cat)
    [ -n "$LINES" ] || fail "Aucun réglage reçu."
    server_host=""
    _ifs=$IFS
    IFS=$NL
    for line in $LINES; do
        IFS=$_ifs
        case $line in *"$TAB"*"$TAB"*) ;; *) fail "Réglage mal formé." ;; esac
        s=${line%%"$TAB"*}
        rest=${line#*"$TAB"}
        k=${rest%%"$TAB"*}
        v=${rest#*"$TAB"}
        case $v in *"$TAB"*) fail "Le réglage $s.$k ne peut pas contenir de tabulation." ;; esac
        _lapinou_name_ok "$s" && _lapinou_name_ok "$k" || fail "Réglage inconnu."
        check "$s" "$k" "$v" || fail "Le réglage $s.$k $WHY."
        [ "$s.$k" = server.host ] && server_host=$v
        if [ "$s.$k" = network.wifi_ssid ]; then
            wpa_get psk
            [ -n "$WV" ] || fail "Donnez aussi le mot de passe Wi-Fi."
        fi
    done
    IFS=$_ifs
    IFS=$NL
    for line in $LINES; do
        IFS=$_ifs
        s=${line%%"$TAB"*}
        rest=${line#*"$TAB"}
        k=${rest%%"$TAB"*}
        v=${rest#*"$TAB"}
        [ "$s" = network ] && continue
        lapinou_set "$s" "$k" "$v" || fail "Le réglage $s.$k n'a pas pu être enregistré."
    done
    IFS=$_ifs
    with_lock write_network || fail "Les réglages du réseau n'ont pas pu être enregistrés."
    # conf.sh's writes take the lock themselves.
    if [ -n "$server_host" ]; then
        server_files "$server_host" || fail "Les réglages du serveur n'ont pas pu être enregistrés."
    fi
    ok
}

# --- secrets ---

cmd_secret() {
    name=$1
    value=$(cat)
    case $name in
    web_password)
        [ ${#value} -ge 8 ] && [ ${#value} -le 64 ] ||
            fail "Le mot de passe doit avoir de 8 à 64 caractères."
        case $value in *[!!-~]* | *\"* | *\\* | *:*)
            fail "Le mot de passe ne peut avoir que des lettres, chiffres et signes, sans espace, guillemet (\"), barre oblique inverse (\\) ni deux-points (:)." ;;
        esac
        printf '%s\n' "$value" | replace "$CONF/web_password" 600 || fail "Le mot de passe n'a pas pu être enregistré."
        "$INETD" conf > /dev/null 2>&1
        [ -s "$CONF/web_password.hash" ] && [ ! -e "$CONF/web_password" ] ||
            fail "Le mot de passe n'a pas pu être mis en place."
        ok
        ;;
    wifi_password)
        [ ${#value} -ge 8 ] && [ ${#value} -le 63 ] ||
            fail "Le mot de passe Wi-Fi doit avoir de 8 à 63 caractères."
        wifi_text_ok "$value" ||
            fail "Le mot de passe Wi-Fi ne peut pas contenir de guillemet (\") ni de barre oblique inverse (\\)."
        wpa_get ssid
        [ -n "$WV" ] || fail "Donnez aussi le nom du réseau Wi-Fi."
        wifi_secret() {
            wpa_get ssid
            wpa_write "$WV" "$value"
        }
        with_lock wifi_secret || fail "Le mot de passe Wi-Fi n'a pas pu être enregistré."
        ok
        ;;
    esac
    case " $SECRET_NAMES " in *" $name "*) ;; *) fail "Secret inconnu." ;; esac
    case $value in *[[:cntrl:]]*) fail "Le secret ne peut pas contenir de caractère de contrôle." ;; esac
    [ ${#value} -le 1024 ] || fail "Le secret est trop long (1024 caractères au plus)."
    lapinou_secret_set "$name" "$value" || fail "Le secret n'a pas pu être enregistré."
    ok
}

# --- updates ---

# ota_running: true while lapinou-ota holds its lock.
ota_running() {
    [ -e "$OTA_LOCK" ] || return 1
    ! (flock -n 7) 7>> "$OTA_LOCK" 2> /dev/null
}

cmd_ota() {
    case $1 in
    check)
        ota_running && fail "Une mise à jour est en cours."
        mkdir -p "$OTA_DIR" 2> /dev/null
        out=$(timeout 180 "$OTA" check 2> "$OTA_DIR/check.err")
        st=$?
        installed=$(printf '%s' "$out" | sed -n 's/.*"installed":"\([^"]*\)".*/\1/p')
        available=$(printf '%s' "$out" | sed -n 's/.*"available":"\([^"]*\)".*/\1/p')
        if [ $st != 0 ] || [ -z "$available" ]; then
            why=$(grep -v '^$' "$OTA_DIR/check.err" 2> /dev/null | tail -n 1)
            rm -f "$OTA_DIR/check.err"
            fail "La recherche de mise à jour a échoué${why:+ : $why}"
        fi
        rm -f "$OTA_DIR/check.err"
        case $installed$available in *[!A-Za-z0-9._+-]*) fail "Réponse de lapinou-ota inattendue." ;; esac
        now=$(date +%s)
        printf '{"installed":"%s","available":"%s","checked":%s}\n' "$installed" "$available" "$now" |
            replace "$OTA_CHECK" 644
        printf '%s\n' "$available" | replace "$OTA_AVAILABLE" 644
        ok "\"installed\":\"$installed\",\"available\":\"$available\",\"checked\":$now"
        ;;
    install)
        ota_running && fail "Une mise à jour est déjà en cours."
        setsid "$OTA" install < /dev/null > /dev/null 2>&1 &
        ok "\"started\":true"
        ;;
    status)
        if ota_running; then r=true; else r=false; fi
        log=""
        [ -f "$OTA_LOG" ] && log=$(tail -n 40 "$OTA_LOG")
        js "$log"
        last=null
        if [ -f "$OTA_CHECK" ]; then
            read -r last < "$OTA_CHECK"
            case $last in '{"installed":"'*'}') ;; *) last=null ;; esac
        fi
        ok "\"running\":$r,\"log\":$J,\"last_check\":$last"
        ;;
    *) fail "Commande inconnue." ;;
    esac
}

# --- restarts ---

cmd_restart() {
    case $1 in
    mqtt) killall lapinou-mqtt > /dev/null 2>&1 ;;
    cloud) killall lapinou-cloud > /dev/null 2>&1 ;;
    all) setsid "$S40" restart < /dev/null > /dev/null 2>&1 & ;;
    *) fail "Commande inconnue." ;;
    esac
    ok
}

# --- tests ---

# curl_status URL [HEADER]: the HTTP status of a GET (000 when there is none), in CODE. The header
# (a key) goes to curl in a configuration on its standard input, never in its arguments.
curl_status() {
    _h=""
    if [ -n "${2:-}" ]; then
        _e=$(printf '%s' "$2" | sed 's/\\/\\\\/g; s/"/\\"/g')
        _h="header = \"$_e\""
    fi
    CODE=$(printf '%s\n' "$_h" | curl -K - -s -o /dev/null -w '%{http_code}' --max-time 10 --cacert "$CACERT" \
        --proto =http,https "$1" 2> /dev/null)
    case $CODE in [0-9][0-9][0-9]) ;; *) CODE=000 ;; esac
}

# api_base URL: the service's address up to /v1 (added when missing), in BASE.
api_base() {
    BASE=${1%/}
    case $BASE in
    */v1/*) BASE=${BASE%%/v1/*}/v1 ;;
    */v1) ;;
    *) BASE=$BASE/v1 ;;
    esac
}

http_verdict() { # what
    case $CODE in
    200) ok "\"ok\":true,\"detail\":\"$1 répond.\"" ;;
    401 | 403) fail "$1 refuse la clé (HTTP $CODE)." ;;
    000) fail "$1 ne répond pas." ;;
    *) fail "$1 répond HTTP $CODE." ;;
    esac
}

cmd_test() {
    case $1 in
    mqtt)
        [ "$(lapinou_get mqtt enabled)" = 1 ] || fail "MQTT n'est pas activé."
        st=$(timeout 7 dbus-send --system --print-reply --reply-timeout=5000 --dest=com.mindscape.karotz.LapinouMqtt \
            /com/mindscape/karotz/LapinouMqtt com.mindscape.karotz.LapinouMqtt.get_state 2> /dev/null |
            sed -n 's/^ *string "\(.*\)"$/\1/p')
        case $st in
        connected) ok "\"ok\":true,\"detail\":\"Connecté au broker.\"" ;;
        connecting) fail "Connexion au broker en cours : réessayez dans quelques secondes." ;;
        disconnected) fail "Le broker ne répond pas, ou refuse l'identifiant ou le mot de passe." ;;
        disabled) fail "Le pont MQTT attend ses réglages : redémarrez-le." ;;
        *) fail "Le pont MQTT (lapinou-mqtt) ne répond pas." ;;
        esac
        ;;
    server)
        host=$(lapinou_get server host)
        [ -n "$host" ] || fail "Aucun serveur LapiNou n'est réglé."
        is_host "$host" || fail "L'adresse du serveur n'est pas valable."
        case $host in *:*) url="http://[$host]:8080/api/health" ;; *) url="http://$host:8080/api/health" ;; esac
        curl_status "$url"
        http_verdict "Le serveur LapiNou"
        ;;
    stt | llm | tts)
        key=""
        case $1 in
        stt) url=$(lapinou_get cloud stt_url https://api.openai.com/v1/audio/transcriptions) name="La reconnaissance vocale"
            key=$(lapinou_secret_get cloud_stt_key) kind=openai ;;
        tts) url=$(lapinou_get cloud tts_url https://api.openai.com/v1/audio/speech) name="La synthèse vocale"
            key=$(lapinou_secret_get cloud_tts_key) kind=openai ;;
        llm)
            kind=$(lapinou_get cloud llm_kind anthropic)
            if [ "$kind" = openai ]; then d=https://api.openai.com; else d=https://api.anthropic.com; fi
            url=$(lapinou_get cloud llm_url "$d") name="Le modèle de langage"
            key=$(lapinou_secret_get cloud_llm_key)
            ;;
        esac
        is_url "$url" || fail "$name n'a pas d'adresse valable."
        api_base "$url"
        if [ "$kind" = anthropic ]; then
            # Anthropic's version header goes as an argument (no secret in it); the key on stdin.
            _e=$(printf '%s' "$key" | sed 's/\\/\\\\/g; s/"/\\"/g')
            CODE=$(printf 'header = "x-api-key: %s"\n' "$_e" | curl -K - -s -o /dev/null -w '%{http_code}' --max-time 10 \
                --cacert "$CACERT" --proto =http,https -H 'anthropic-version: 2023-06-01' "$BASE/models" 2> /dev/null)
            case $CODE in [0-9][0-9][0-9]) ;; *) CODE=000 ;; esac
        elif [ -n "$key" ]; then
            curl_status "$BASE/models" "Authorization: Bearer $key"
        else
            curl_status "$BASE/models"
        fi
        http_verdict "$name"
        ;;
    *) fail "Commande inconnue." ;;
    esac
}

case ${1:-} in
get) cmd_get ;;
set) cmd_set ;;
secret) cmd_secret "${2:-}" ;;
ota) cmd_ota "${2:-}" ;;
restart) cmd_restart "${2:-}" ;;
test) cmd_test "${2:-}" ;;
*) fail "Commande inconnue." ;;
esac
