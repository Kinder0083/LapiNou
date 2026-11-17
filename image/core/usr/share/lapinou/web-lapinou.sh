# SPDX-License-Identifier: MIT
# The LapiNou page's programs (/usr/www/cgi-bin/lapinou/*), sourced after LapinLibre's web.sh and its
# web_begin (the web user, the local network, the cross-site rule, the methods and sizes). What
# needs root (the settings, the secrets, the updates, the daemons) goes through lapinou-webroot,
# with every value on its standard input; its JSON answer is the reply. docs/lapinou/page-web.md.

LAPINOU_ROOT=/usr/karotz/bin/lapinou-webroot
LAPINOU_SECRETS=/usr/etc/conf/lapinou.secrets
LAPINOU_OTA_CHECK=/usr/ota/check.json

# lapinou_needs_password: true while a secret is stored and the page has no password (httpd's
# login). The secrets file is root's, but its size can be seen.
lapinou_needs_password() {
    [ ! -s "$WEB_HASH" ] && [ -s "$LAPINOU_SECRETS" ]
}

# lapinou_guard: refuses the request (401) while the page needs a password; the page then offers to
# create one.
lapinou_guard() {
    if lapinou_needs_password; then
        web_refuse 401 "Des secrets sont enregistrés : créez d'abord le mot de passe de la page (onglet Sécurité)."
    fi
}

# lapinou_post: refuses anything but POST (405).
lapinou_post() {
    [ "${REQUEST_METHOD:-}" = POST ] || web_refuse 405 "Cette demande se fait en POST seulement."
}

# lapinou_reply WORD...: lapinou-webroot's answer (its standard input is the program's), as the
# reply; then the end of the program.
lapinou_reply() {
    _lr=$("$LAPINOU_ROOT" "$@" 2> /dev/null)
    case $_lr in
    '{"return":"0"'*) [ "$WEB_KIND" = change ] && web_ok ;;
    '{"return":"1"'*) [ "$WEB_KIND" = change ] && web_log "lapinou-webroot $1 refused it" ;;
    *) web_fail "Le lapin n'a pas pu faire cette demande." ;;
    esac
    reply_json "$_lr"
}
