# SPDX-License-Identifier: MIT
# LapiNou's settings on the rabbit, for shell programs (the web page's programs, the start-up
# scripts, the USB key's install). Source it:
#
#   . /usr/share/lapinou/conf.sh
#   lapinou_get SECTION KEY [DEFAULT]     the value (DEFAULT, or nothing, when the key is absent or empty)
#   lapinou_set SECTION KEY VALUE         store a value; any text is kept as given
#   lapinou_secret_get KEY                a secret's value
#   lapinou_secret_set KEY VALUE          store a secret
#
# The settings are in /usr/etc/conf/lapinou.conf (an INI file, mode 0644, nothing secret in it);
# the secrets (mqtt_password, cloud_stt_key, cloud_llm_key, cloud_tts_key, ota_token) are in
# /usr/etc/conf/lapinou.secrets (mode 0600, [secrets] section). LAPINOU_CONF_DIR moves both (tests).
#
# A value is written as one line, key=value, with \ as \, a line feed as \n, a carriage return
# as \r and = as \=; reading undoes that. Nothing is ever evaluated, so quotes, $, ;, backticks
# and accents are only text. Writes go to a temporary file that replaces the old one in one step.
# A section or key is letters, digits, _, - and . only; anything else fails (status 2) and writes
# nothing. A trailing line feed in a value does not survive command substitution.
# The same format is read in C by libkarotz-common's lapinou_conf.c.
#
# Runs in BusyBox ash and in bash: POSIX shell and awk only. Sourcing defines functions only.

# >>> the functions image/usbkey/applysettings.sh carries a copy of (tests/image/test_lapinou_conf.py checks they match)
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
# <<< the functions applysettings.sh copies
