#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
###############################################################################
# LapiNou - après une mise à jour du code (bouton « Mettre à jour », comme FSAO)
#
# La page de réglages (server/lapinou/updater.py) fait « git pull » puis lance ce
# script en tant que lapinou, puis redémarre le service si tout s'est bien passé.
#
# - Lancé par lapinou : écrit une demande dans /var/lib/lapinou/update/request ;
#   lapinou-update.path démarre lapinou-update.service, qui lance en root la copie
#   installée de ce script (/usr/local/lib/lapinou/post-update.sh --root). On attend
#   son code de retour et on affiche son journal.
# - Lancé en root (--root, ou à la main) : paquets système, uv, dépendances Python,
#   ha-mcp et modèles (provision.sh --post-update, installé en root). Le code du
#   dépôt (modifiable par lapinou) ne s'exécute jamais en root.
###############################################################################
set -euo pipefail

R="${LAPINOU_ROOT:-}"
REQUEST="$R/var/lib/lapinou/update/request"
RUN="${LAPINOU_UPDATE_RUN:-$R/run/lapinou-update}"
PROVISION="$R/usr/local/lib/lapinou/provision.sh"
WAIT_S="${LAPINOU_UPDATE_WAIT:-850}"

say() { echo "post-update: $1"; }

valid_nonce() { [[ "$1" =~ ^[0-9a-f]{16}$ ]]; }

read_request() {
    local nonce=""
    if [ -f "$REQUEST" ] && [ ! -L "$REQUEST" ]; then
        nonce=$(head -c 64 "$REQUEST" | head -n 1)
    fi
    if valid_nonce "$nonce"; then echo "$nonce"; else echo "manual"; fi
}

root_mode() {
    mkdir -p "$RUN"
    chmod 0755 "$RUN"
    local nonce done_nonce="" rc
    # Une demande arrivée pendant un passage (service déjà actif) est servie au tour suivant.
    while nonce=$(read_request); [ "$nonce" != "$done_nonce" ]; do
        rc=0
        bash "$PROVISION" --post-update > "$RUN/log.new" 2>&1 || rc=$?
        mv -f "$RUN/log.new" "$RUN/log"
        chmod 0644 "$RUN/log"
        printf '%s %s\n' "$nonce" "$rc" > "$RUN/status.new"
        chmod 0644 "$RUN/status.new"
        mv -f "$RUN/status.new" "$RUN/status"
        cat "$RUN/log"
        done_nonce="$nonce"
        [ "$nonce" != "manual" ] || return "$rc"
    done
    return "${rc:-0}"
}

client_mode() {
    [ -d "$(dirname "$REQUEST")" ] || { say "pas de $(dirname "$REQUEST") : installation incomplète"; exit 1; }
    local nonce status="" got="" rc i
    nonce=$(od -An -tx1 -N8 /dev/urandom | tr -d ' \n')
    printf '%s\n' "$nonce" > "$REQUEST"
    say "demande envoyée à lapinou-update.service, en attente..."
    for ((i = 0; i < WAIT_S; i++)); do
        if [ -f "$RUN/status" ]; then
            status=$(cat "$RUN/status")
            got=${status%% *}
            if [ "$got" = "$nonce" ]; then
                rc=${status##* }
                cat "$RUN/log" 2>/dev/null || true
                [[ "$rc" =~ ^[0-9]+$ ]] || rc=1
                return "$rc"
            fi
        fi
        sleep 1
    done
    say "pas de réponse de lapinou-update.service en ${WAIT_S} s (journalctl -u lapinou-update)"
    return 1
}

if [ "$(id -u)" = "0" ]; then
    root_mode
else
    client_mode
fi
