# SPDX-License-Identifier: MIT
# Commun aux commandes simulées des tests de l'installateur : chaque appel est journalisé
# (nom et arguments séparés par une espace, une ligne) dans $STUB_LOG, pour vérifier ce qui a été lancé et
# qu'aucun secret n'apparaît dans les arguments d'un processus.
# shellcheck shell=bash
stub_log() {
    local name
    name=$(basename "$0")
    { printf '%s' "$name"; printf ' %s' "$@"; printf '\n'; } >> "${STUB_LOG:?}"
}
stub_log "$@"
