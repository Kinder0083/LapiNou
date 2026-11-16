#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
###############################################################################
# LapiNou - Installation du serveur sur Proxmox (un seul script)
#
# À lancer en root sur l'hôte Proxmox (voir docs/lapinou/installation.md) :
# - crée un conteneur LXC Debian 13 (sinon 12), non privilégié, démarrage auto ;
# - teste MQTT, Home Assistant, Lyrion et le fournisseur d'IA AVANT de créer quoi
#   que ce soit ;
# - installe le serveur dans le conteneur (installer/provision.sh) : dépôt, venv,
#   modèles, ha-mcp, réglages, mot de passe admin, services systemd ;
# - ré-exécutable : un conteneur LapiNou existant est proposé à la mise à jour.
#
# Les secrets tapés ici (jetons, mots de passe, clés) ne passent jamais en argument
# d'une commande : ils vont au conteneur dans un fichier 0600, effacé ensuite.
###############################################################################
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

msg() { echo -e "${BLUE}▶${NC} $1"; }
ok() { echo -e "${GREEN}✓${NC} $1"; }
err() { echo -e "${RED}✗${NC} $1" >&2; exit 1; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

# Réponses lues sur le terminal (même via « bash -c "$(curl ...)" »).
TTY="${LAPINOU_TTY:-/dev/tty}"
( : < "$TTY" ) 2>/dev/null || err "Pas de terminal : lancez ce script dans un shell interactif (console Proxmox ou ssh)."
exec 3<"$TTY"

# ask VAR "Question" [défaut] : Entrée garde le défaut ; « - » veut dire « aucun ».
ask() {
    local prompt="$2" answer=""
    [ -n "${3:-}" ] && prompt="$prompt [$3]"
    IFS= read -r -u 3 -p "$prompt: " answer || true
    if [ "$answer" = "-" ]; then answer=""; else answer="${answer:-${3:-}}"; fi
    printf -v "$1" '%s' "$answer"
}

# ask_secret VAR "Question" : rien d'affiché, rien dans l'historique ni les journaux.
ask_secret() {
    local answer=""
    IFS= read -r -s -u 3 -p "$2: " answer || true
    echo ""
    printf -v "$1" '%s' "$answer"
}

is_uint() { [[ "$1" =~ ^[0-9]{1,9}$ ]]; }
in_range() { is_uint "$1" && [ "$((10#$1))" -ge "$2" ] && [ "$((10#$1))" -le "$3" ]; }
is_ipv4() {
    local o
    [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do [ "$((10#$o))" -le 255 ] || return 1; done
}
# Branche git : ni « - » au début (option), ni « .. » (chemin).
is_branch() { [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9._/-]*$ && "$1" != *..* ]]; }
is_hostname() { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$ ]]; }
is_host() { is_ipv4 "$1" || { ! [[ "$1" =~ ^[0-9.]+$ ]] && is_hostname "$1"; }; }
# Clés et jetons envoyés en en-tête (configuration curl entre guillemets) : ni " ni \.
is_header_safe() { [[ "$1" != *\"* && "$1" != *\\* ]]; }
is_port() { is_uint "$1" && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
is_http_url() { [[ "$1" =~ ^https?://[^/[:space:]]+(/[^[:space:]]*)?$ ]]; }
shown() { if [ -n "$1" ]; then echo "(saisi)"; else echo "(aucun)"; fi; }

WORK=$(mktemp -d)
CT_SETUP="/root/lapinou-setup"
INSTALL_CONF="/usr/local/lib/lapinou/install.conf"
CTID=""
PUSHED=0
cleanup() {
    if [ -f "$WORK/answers" ]; then shred -u "$WORK/answers" 2>/dev/null || rm -f "$WORK/answers"; fi
    rm -rf "$WORK"
    if [ "$PUSHED" = 1 ] && [ -n "$CTID" ]; then
        pct exec "$CTID" -- rm -rf "$CT_SETUP" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

###############################################################################
# Accès HTTP : les en-têtes secrets passent par la configuration de curl, sur
# son entrée standard (-K -), jamais par ses arguments.
###############################################################################

# http_code URL "en-têtes curl (un par ligne)" [options curl...] -> code HTTP ("000" injoignable)
http_code() {
    local url="$1" headers="$2"
    shift 2
    curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -K - "$@" "$url" <<<"$headers" 2>/dev/null \
        || true
}

header() { printf 'header = "%s"\n' "$1"; }

GH_REPO="" GH_BRANCH="" GH_TOKEN=""

github_headers() {
    header "Accept: application/vnd.github+json"
    if [ -n "$GH_TOKEN" ]; then header "Authorization: Bearer $GH_TOKEN"; fi
}

# fetch_file chemin-dans-le-dépôt destination
fetch_file() {
    local path="$1" dest="$2" conf
    if [ -n "$GH_TOKEN" ]; then
        conf="$(header "Authorization: Bearer $GH_TOKEN")
$(header "Accept: application/vnd.github.raw")"
        curl -fsSL --max-time 60 -K - -o "$dest" \
            "https://api.github.com/repos/$GH_REPO/contents/$path?ref=$GH_BRANCH" <<<"$conf"
    else
        curl -fsSL --max-time 60 -o "$dest" \
            "https://raw.githubusercontent.com/$GH_REPO/$GH_BRANCH/$path"
    fi
}

# Les trois fichiers installés en root dans le conteneur : ceux de ce dossier si le script
# tourne depuis un clone, sinon ceux du dépôt GitHub (même branche).
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi
get_setup_files() {
    mkdir -p "$WORK/setup"
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/provision.sh" ] && [ -f "$SCRIPT_DIR/post-update.sh" ] \
        && [ -f "$SCRIPT_DIR/../server/systemd/lapinou.service" ]; then
        cp "$SCRIPT_DIR/provision.sh" "$SCRIPT_DIR/post-update.sh" \
            "$SCRIPT_DIR/../server/systemd/lapinou.service" "$WORK/setup/"
        return
    fi
    { fetch_file installer/provision.sh "$WORK/setup/provision.sh" \
        && fetch_file installer/post-update.sh "$WORK/setup/post-update.sh" \
        && fetch_file server/systemd/lapinou.service "$WORK/setup/lapinou.service"; } \
        || err "Impossible de récupérer les scripts d'installation depuis $GH_REPO ($GH_BRANCH)."
}

# Pousse les fichiers d'installation (et les réponses) dans le conteneur, en 0600/0700.
push_setup() {
    pct exec "$CTID" -- mkdir -p -m 0700 "$CT_SETUP"
    PUSHED=1
    local f
    for f in provision.sh post-update.sh lapinou.service; do
        pct push "$CTID" "$WORK/setup/$f" "$CT_SETUP/$f" --perms 0700
    done
    if [ -f "$WORK/answers" ]; then
        pct push "$CTID" "$WORK/answers" "$CT_SETUP/answers" --perms 0600
    fi
}

###############################################################################
# Tests de connexion (avant toute création)
###############################################################################

# MQTT 3.1.1 : CONNECT écrit sur une connexion TCP de bash, CONNACK lu. Tout l'échange tourne
# dans un bash enfant sous timeout ; identifiant et mot de passe lui arrivent sur son entrée
# standard (jamais en argument). Il écrit le CONNACK, ou sort en 10 (connexion refusée).
# shellcheck disable=SC2016  # script de l'enfant : ses variables sont les siennes
MQTT_SCRIPT='
set -u
LC_ALL=C
host=$1 port=$2
IFS= read -r user || true
IFS= read -r pass || true
byte() { printf "%b" "\\x$(printf "%02x" "$1")"; }
field() { byte $((${#1} >> 8)); byte $((${#1} & 255)); printf "%s" "$1"; }
cid="lapinou-install-$$" flags=2
len=$((10 + 2 + ${#cid}))
if [ -n "$user" ]; then
    flags=$((flags | 0x80)); len=$((len + 2 + ${#user}))
    if [ -n "$pass" ]; then flags=$((flags | 0x40)); len=$((len + 2 + ${#pass})); fi
fi
[ "$len" -lt 16384 ] || exit 11
exec 9<>"/dev/tcp/$host/$port" || exit 10
{
    byte 16
    if [ "$len" -lt 128 ]; then byte "$len"; else byte $(((len & 127) | 128)); byte $((len >> 7)); fi
    field "MQTT"; byte 4; byte "$flags"; byte 0; byte 30
    field "$cid"
    if [ -n "$user" ]; then field "$user"; fi
    if [ -n "$user" ] && [ -n "$pass" ]; then field "$pass"; fi
} >&9
od -An -tx1 -N4 <&9
'

mqtt_check() {
    local host="$1" port="$2" user="$3" pass="$4" out rc=0 resp
    out=$(timeout 8 bash -c "$MQTT_SCRIPT" mqtt "$host" "$port" 2>/dev/null <<<"$user
$pass") || rc=$?
    case "$rc" in
        0) ;;
        10) MQTT_PROBLEM="broker MQTT injoignable sur $host:$port (connexion refusée)"; return 1 ;;
        11) MQTT_PROBLEM="identifiants MQTT trop longs"; return 1 ;;
        124) MQTT_PROBLEM="broker MQTT injoignable sur $host:$port (pas de réponse en 8 s)"; return 1 ;;
        *) MQTT_PROBLEM="broker MQTT injoignable sur $host:$port"; return 1 ;;
    esac
    read -r -a resp <<<"$out"
    if [ "${resp[0]:-}" != "20" ]; then
        MQTT_PROBLEM="pas de réponse MQTT de $host:$port (est-ce bien un broker MQTT ?)"
        return 1
    fi
    rc=$((16#${resp[3]:-ff}))
    case "$rc" in
        0) return 0 ;;
        4|5) MQTT_PROBLEM="identifiant ou mot de passe MQTT refusé par $host:$port" ;;
        *) MQTT_PROBLEM="connexion MQTT refusée par $host:$port (code $rc)" ;;
    esac
    return 1
}

run_checks() {
    msg "Tests de connexion..."
    local code
    MQTT_PROBLEM=""
    # shellcheck disable=SC2153  # MQTT_PORT : posé par ask (printf -v)
    mqtt_check "$MQTT_HOST" "$MQTT_PORT" "$MQTT_USER" "$MQTT_PASS" \
        || err "MQTT : $MQTT_PROBLEM. Vérifiez l'adresse, le port et les identifiants, puis relancez."
    ok "MQTT : connecté à $MQTT_HOST:$MQTT_PORT"

    if [ -n "$HA_URL" ]; then
        code=$(http_code "${HA_URL%/}/api/" "$(header "Authorization: Bearer $HA_TOKEN")")
        case "$code" in
            200) ok "Home Assistant : API joignable, jeton accepté" ;;
            401|403) err "Home Assistant : jeton refusé par $HA_URL (créez un jeton longue durée dans votre profil HA)." ;;
            000) err "Home Assistant injoignable à $HA_URL." ;;
            *) err "Home Assistant : réponse inattendue $code de ${HA_URL%/}/api/." ;;
        esac
    fi

    if [ -n "$LMS_HOST" ]; then
        code=$(http_code "http://$LMS_HOST:$LMS_PORT/jsonrpc.js" "" \
            -X POST -H "Content-Type: application/json" \
            --data '{"id":1,"method":"slim.request","params":["",["serverstatus",0,0]]}')
        case "$code" in
            200) ok "Lyrion : serveur joignable ($LMS_HOST:$LMS_PORT)" ;;
            401|403) warn "Lyrion : joignable mais protégé par mot de passe ($LMS_HOST:$LMS_PORT) ; LapiNou ne pourra pas le piloter tant que la protection est active. On continue." ;;
            *) err "Lyrion injoignable à http://$LMS_HOST:$LMS_PORT (code $code). Répondez « - » pour s'en passer." ;;
        esac
    fi

    if [ -n "$ANTHROPIC_KEY" ]; then
        code=$(http_code "https://api.anthropic.com/v1/models" \
            "$(header "x-api-key: $ANTHROPIC_KEY")
$(header "anthropic-version: 2023-06-01")")
        case "$code" in
            200) ok "Anthropic : clé acceptée" ;;
            401|403) err "Anthropic : clé API refusée." ;;
            000) err "Anthropic : api.anthropic.com injoignable (internet ?)." ;;
            *) err "Anthropic : réponse inattendue $code." ;;
        esac
    fi

    if [ -n "$OAI_URL" ]; then
        local auth=""
        [ -n "$OAI_KEY" ] && auth=$(header "Authorization: Bearer $OAI_KEY")
        code=$(http_code "${OAI_URL%/}/models" "$auth")
        case "$code" in
            200) ok "Fournisseur $OAI_NAME : joignable, clé acceptée" ;;
            401|403) err "Fournisseur $OAI_NAME : clé API refusée." ;;
            000) err "Fournisseur $OAI_NAME injoignable à $OAI_URL." ;;
            *) err "Fournisseur $OAI_NAME : réponse inattendue $code de ${OAI_URL%/}/models." ;;
        esac
    fi

    if [ -n "$RABBIT_IP" ]; then
        if ping -c 1 -W 2 "$RABBIT_IP" >/dev/null 2>&1; then
            ok "Lapin : $RABBIT_IP répond"
        else
            warn "Lapin : $RABBIT_IP ne répond pas (éteint ?). Ce n'est pas bloquant."
        fi
    fi
}

github_check() {
    local code
    code=$(http_code "https://api.github.com/repos/$GH_REPO" "$(github_headers)")
    case "$code" in
        200) ;;
        401) err "GitHub : jeton refusé." ;;
        403|404)
            if [ -n "$GH_TOKEN" ]; then
                err "GitHub : dépôt $GH_REPO introuvable avec ce jeton (droit « Contents: Read » sur ce dépôt ?)."
            fi
            err "GitHub : dépôt $GH_REPO introuvable. S'il est privé, donnez un jeton." ;;
        000) err "GitHub injoignable : l'hôte Proxmox a-t-il accès à internet ?" ;;
        *) err "GitHub : réponse inattendue $code." ;;
    esac
    code=$(http_code "https://api.github.com/repos/$GH_REPO/branches/$GH_BRANCH" "$(github_headers)")
    [ "$code" = "200" ] || err "GitHub : branche $GH_BRANCH introuvable dans $GH_REPO."
    ok "GitHub : $GH_REPO, branche $GH_BRANCH"
}

###############################################################################
# Mise à jour d'un conteneur existant
###############################################################################

find_existing() {
    local id
    for id in $(pct list 2>/dev/null | awk 'NR > 1 {print $1}'); do
        if pct config "$id" 2>/dev/null | grep -Eq '^(tags: (.*[;,])?lapinou([;,].*)?|hostname: lapinou)$'; then
            echo "$id"
            return
        fi
    done
}

update_existing() {
    local id="$1"
    CTID="$id"
    if ! pct status "$id" | grep -q running; then
        msg "Démarrage du conteneur $id..."
        pct start "$id" || err "Impossible de démarrer le conteneur $id"
        sleep 5
    fi
    pct exec "$id" -- test -d /opt/lapinou/.git \
        || err "Le conteneur $id n'a pas d'installation LapiNou (/opt/lapinou) : choisissez la réinstallation."
    # Dépôt et branche : ceux notés à l'installation dans un fichier root (install.conf), jamais
    # ceux du clone, que le service (lapinou) peut modifier. Les changer : les retaper ici.
    local conf rec_repo rec_branch
    conf=$(pct exec "$id" -- cat "$INSTALL_CONF" 2>/dev/null || true)
    rec_repo=$(sed -n 's/^REPO=//p' <<<"$conf" | head -n 1)
    rec_branch=$(sed -n 's/^BRANCH=//p' <<<"$conf" | head -n 1)
    if [[ "$rec_repo" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] && is_branch "$rec_branch"; then
        ok "Installation trouvée : $rec_repo, branche $rec_branch ($INSTALL_CONF)"
    else
        warn "Pas de $INSTALL_CONF lisible (installation ancienne) : indiquez le dépôt et la branche."
        rec_repo="Kinder0083/LapiNou" rec_branch="main"
    fi
    ask GH_REPO "Dépôt (Entrée : garder)" "$rec_repo"
    ask GH_BRANCH "Branche (Entrée : garder)" "$rec_branch"
    [[ "$GH_REPO" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || err "Dépôt invalide (forme propriétaire/nom)."
    is_branch "$GH_BRANCH" || err "Branche invalide."
    if [ "$GH_REPO" != "$rec_repo" ] || [ "$GH_BRANCH" != "$rec_branch" ]; then
        warn "Nouveau dépôt ou branche : $GH_REPO ($GH_BRANCH)"
    fi
    # Jeton : celui du conteneur, seulement si c'est un fichier ordinaire (pas un lien), lu en
    # 4 Kio au plus et en 15 s au plus.
    # shellcheck disable=SC2016  # $f : variable du sh du conteneur
    GH_TOKEN=$(timeout 15 pct exec "$id" -- sh -c 'f=/etc/lapinou/git-credentials; test -f "$f" && test ! -L "$f" && head -c 4096 "$f"' 2>/dev/null \
        | sed -n 's#^https://x-access-token:\([A-Za-z0-9_]*\)@github.com$#\1#p' | head -n 1 || true)
    echo "  Mise à jour : conteneur $id, $GH_REPO (branche $GH_BRANCH), jeton $(shown "$GH_TOKEN")"
    ask CONFIRM "Confirmer la mise à jour ? (o/n)" o
    [[ "$CONFIRM" =~ ^[OoYy]$ ]] || err "Mise à jour annulée."
    get_setup_files
    push_setup
    msg "Mise à jour dans le conteneur $id (git pull, dépendances, modèles, services)..."
    pct exec "$id" -- bash "$CT_SETUP/provision.sh" --update "$GH_REPO" "$GH_BRANCH" \
        || err "La mise à jour a échoué (voir ci-dessus)."
    local ip
    ip=$(pct exec "$id" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
    echo ""
    ok "LapiNou est à jour : http://${ip:-<adresse du conteneur>}:8080"
    exit 0
}

###############################################################################
# Début
###############################################################################

clear 2>/dev/null || true
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║        LapiNou - Installation du serveur (Proxmox LXC)         ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

command -v pct >/dev/null 2>&1 || err "Ce script doit être exécuté sur un serveur Proxmox (commande pct introuvable)."
[ "$(id -u)" -eq 0 ] || err "Lancez-le en root."

msg "Détection de la configuration Proxmox..."
echo "  $(pveversion 2>/dev/null | head -n 1)"
echo ""

REINSTALL_ID=""
EXISTING=$(find_existing)
if [ -n "$EXISTING" ]; then
    warn "Un conteneur LapiNou existe déjà : $EXISTING ($(pct status "$EXISTING" 2>/dev/null | awk '{print $2}'))"
    echo "  1) Mettre à jour (garde les réglages et les données)"
    echo "  2) Réinstaller (EFFACE le conteneur $EXISTING et ses données)"
    echo "  3) Installer un autre conteneur à côté"
    echo "  4) Quitter"
    ask CHOICE "Votre choix" 1
    case "$CHOICE" in
        1) update_existing "$EXISTING" ;;
        2)
            ask CONFIRM_ID "Tapez l'ID $EXISTING pour confirmer l'effacement"
            [ "$CONFIRM_ID" = "$EXISTING" ] || err "Réinstallation annulée."
            REINSTALL_ID="$EXISTING"
            ;;
        3) ;;
        *) err "Installation annulée." ;;
    esac
    echo ""
fi

# Modèle Debian : 13 si disponible, sinon 12
msg "Recherche du modèle Debian (13, sinon 12)..."
TPL_STORE=$(pvesm status -content vztmpl 2>/dev/null | awk 'NR > 1 {print $1}' | grep -x local || true)
[ -n "$TPL_STORE" ] || TPL_STORE=$(pvesm status -content vztmpl 2>/dev/null | awk 'NR > 1 {print $1; exit}')
[ -n "$TPL_STORE" ] || err "Aucun stockage pour les modèles de conteneur (contenu « vztmpl »)."
TEMPLATE="" TEMPLATE_DOWNLOAD=""
AVAILABLE=""
for VER in 13 12; do
    LOCAL_TPL=$(pveam list "$TPL_STORE" 2>/dev/null | awk 'NR > 1 {print $1}' \
        | grep -E "/debian-$VER-standard_" | sort -V | tail -n 1 || true)
    if [ -n "$LOCAL_TPL" ]; then
        TEMPLATE="$LOCAL_TPL"
        break
    fi
    if [ -z "$AVAILABLE" ]; then
        pveam update >/dev/null 2>&1 || warn "pveam update : erreur ignorée"
        AVAILABLE=$(pveam available --section system 2>/dev/null | awk '{print $2}')
    fi
    REMOTE_TPL=$(grep -E "^debian-$VER-standard_" <<<"$AVAILABLE" | sort -V | tail -n 1 || true)
    if [ -n "$REMOTE_TPL" ]; then
        TEMPLATE="$TPL_STORE:vztmpl/$REMOTE_TPL"
        TEMPLATE_DOWNLOAD="$REMOTE_TPL"
        break
    fi
done
[ -n "$TEMPLATE" ] || err "Aucun modèle Debian 13 ou 12 trouvé (pveam available --section system)."
if [ -n "$TEMPLATE_DOWNLOAD" ]; then ok "Modèle : $TEMPLATE_DOWNLOAD (sera téléchargé)"; else ok "Modèle : $TEMPLATE"; fi

# Stockage : local-lvm > local > premier
msg "Détection du stockage..."
STORAGES=$(pvesm status -content rootdir 2>/dev/null | awk 'NR > 1 {print $1}')
STORAGE=$(grep -x local-lvm <<<"$STORAGES" || grep -x local <<<"$STORAGES" || head -n 1 <<<"$STORAGES" || true)
[ -n "$STORAGE" ] || err "Aucun stockage pour les disques de conteneur."
ok "Stockage : $STORAGE"

# Ponts réseau
msg "Détection des ponts réseau..."
mapfile -t BRIDGES < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' | grep '^vmbr' || true)
[ "${#BRIDGES[@]}" -gt 0 ] || err "Aucun pont réseau vmbr détecté."
if [ "${#BRIDGES[@]}" -eq 1 ]; then
    BRIDGE="${BRIDGES[0]}"
else
    for i in "${!BRIDGES[@]}"; do
        echo "  $((i + 1))) ${BRIDGES[$i]} $(ip -o -4 addr show dev "${BRIDGES[$i]}" 2>/dev/null | awk '{print $4}' | head -n 1)"
    done
    ask BRIDGE_CHOICE "Numéro du pont à utiliser" 1
    { is_uint "$BRIDGE_CHOICE" && [ "$BRIDGE_CHOICE" -ge 1 ] && [ "$BRIDGE_CHOICE" -le "${#BRIDGES[@]}" ]; } \
        || err "Choix de pont invalide."
    BRIDGE="${BRIDGES[$((BRIDGE_CHOICE - 1))]}"
fi
ok "Pont : $BRIDGE"
echo ""

# GitHub
msg "Dépôt GitHub du serveur..."
echo "  Dépôt privé : jeton à grain fin, accès en lecture seule à « Contents » sur ce dépôt"
echo "  (https://github.com/settings/personal-access-tokens). Il sert aussi aux mises à jour."
ask GH_REPO "Dépôt" "Kinder0083/LapiNou"
ask GH_BRANCH "Branche" "main"
ENV_TOKEN="${LAPINOU_GITHUB_TOKEN:-}"
unset LAPINOU_GITHUB_TOKEN
if [ -n "$ENV_TOKEN" ]; then
    ask_secret GH_TOKEN "Jeton GitHub (vide : celui de LAPINOU_GITHUB_TOKEN)"
    GH_TOKEN=${GH_TOKEN:-$ENV_TOKEN}
else
    ask_secret GH_TOKEN "Jeton GitHub (vide : dépôt public)"
fi
[[ "$GH_REPO" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || err "Dépôt invalide (forme propriétaire/nom)."
is_branch "$GH_BRANCH" || err "Branche invalide."
[[ "$GH_TOKEN" =~ ^[A-Za-z0-9_]*$ ]] || err "Jeton GitHub invalide (lettres, chiffres et _ seulement)."
github_check
echo ""

# Conteneur
msg "Conteneur..."
if [ -n "$REINSTALL_ID" ]; then
    CTID="$REINSTALL_ID"
    echo "  ID du conteneur : $CTID (réinstallation)"
else
    ask CTID "ID du conteneur" "$(pvesh get /cluster/nextid 2>/dev/null || echo 100)"
    in_range "$CTID" 100 999999999 || err "ID invalide (nombre ≥ 100)."
    if pct status "$CTID" >/dev/null 2>&1 || qm status "$CTID" >/dev/null 2>&1; then
        err "L'ID $CTID est déjà utilisé."
    fi
fi
ask CT_HOSTNAME "Nom d'hôte" "lapinou"
[[ "$CT_HOSTNAME" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || err "Nom d'hôte invalide."
ask RAM "RAM (Mo)" 6144
ask CORES "Cœurs" 4
ask DISK "Disque (Go)" 20
in_range "$RAM" 1024 1048576 || err "RAM invalide (1024 à 1048576 Mo)."
in_range "$CORES" 1 512 || err "Nombre de cœurs invalide (1 à 512)."
in_range "$DISK" 8 65536 || err "Taille de disque invalide (8 à 65536 Go)."
echo ""

# Réseau
msg "Réseau du conteneur..."
BRIDGE_ADDR=$(ip -o -4 addr show dev "$BRIDGE" 2>/dev/null | awk '{print $4}' | head -n 1)
BRIDGE_IP=${BRIDGE_ADDR%/*}
BRIDGE_CIDR=${BRIDGE_ADDR#*/}
GATEWAY=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
[ -n "$GATEWAY" ] || GATEWAY="$BRIDGE_IP"
echo "  $BRIDGE : ${BRIDGE_ADDR:-sans adresse}, passerelle ${GATEWAY:-inconnue}"
echo "  1) IP fixe (recommandé : les lapins retrouvent toujours le serveur)"
echo "  2) DHCP"
ask NET_MODE "Votre choix" 1
CT_IP=""
if [ "$NET_MODE" = "1" ]; then
    SUGGESTED_IP=""
    is_ipv4 "$BRIDGE_IP" && SUGGESTED_IP="${BRIDGE_IP%.*}.160"
    ask CT_IP "Adresse IP du conteneur" "$SUGGESTED_IP"
    ask CT_CIDR "Masque CIDR" "${BRIDGE_CIDR:-24}"
    ask CT_GW "Passerelle" "$GATEWAY"
    { is_ipv4 "$CT_IP" && is_ipv4 "$CT_GW" && in_range "$CT_CIDR" 1 32; } || err "Adresse, masque ou passerelle invalide."
    if ping -c 1 -W 1 "$CT_IP" >/dev/null 2>&1; then
        err "L'adresse $CT_IP répond déjà sur le réseau : choisissez-en une libre."
    fi
    NET0="name=eth0,bridge=$BRIDGE,ip=$CT_IP/$CT_CIDR,gw=$CT_GW"
    NET_SHOWN="$CT_IP/$CT_CIDR (passerelle $CT_GW)"
    PUBLIC_URL="http://$CT_IP:8080"
else
    warn "DHCP : réservez l'adresse du conteneur dans votre box pour que les lapins le retrouvent."
    NET0="name=eth0,bridge=$BRIDGE,ip=dhcp"
    NET_SHOWN="DHCP"
    PUBLIC_URL=""
fi
echo ""

# Broker MQTT
msg "Broker MQTT (Home Assistant voit les lapins par MQTT)..."
ask MQTT_HOST "Adresse du broker"
[ -n "$MQTT_HOST" ] || err "L'adresse du broker MQTT est obligatoire."
is_host "$MQTT_HOST" || err "Adresse du broker MQTT invalide (nom d'hôte ou IPv4)."
ask MQTT_PORT "Port" 1883
is_port "$MQTT_PORT" || err "Port MQTT invalide."
ask MQTT_USER "Identifiant (vide : aucun)"
MQTT_PASS=""
[ -n "$MQTT_USER" ] && ask_secret MQTT_PASS "Mot de passe MQTT"
echo ""

# Home Assistant
msg "Home Assistant (ha-mcp et repli Assist)..."
ask HA_URL "URL de Home Assistant (ex. http://192.168.1.20:8123 ; vide : sans HA)"
HA_TOKEN=""
if [ -n "$HA_URL" ]; then
    is_http_url "$HA_URL" || err "URL de Home Assistant invalide (http:// ou https://)."
    ask_secret HA_TOKEN "Jeton longue durée de Home Assistant"
    [ -n "$HA_TOKEN" ] || err "Le jeton de Home Assistant est obligatoire avec une URL."
    is_header_safe "$HA_TOKEN" || err "Jeton de Home Assistant invalide (ni guillemet ni barre oblique inverse)."
fi
echo ""

# Lyrion
msg "Lyrion Music Server (musique et radio vers les Squeezebox)..."
HA_HOST=""
if [ -n "$HA_URL" ]; then HA_HOST=$(sed -E 's#^https?://([^/:]+).*#\1#' <<<"$HA_URL"); fi
if [ -n "$HA_HOST" ]; then
    ask LMS_HOST "Adresse de Lyrion (souvent celle de HA, $HA_HOST ; vide ou - : sans Lyrion)"
else
    ask LMS_HOST "Adresse de Lyrion (vide ou - : sans Lyrion)"
fi
LMS_PORT=9000
if [ -n "$LMS_HOST" ]; then
    is_host "$LMS_HOST" || err "Adresse de Lyrion invalide (nom d'hôte ou IPv4)."
    ask LMS_PORT "Port de Lyrion" 9000
    is_port "$LMS_PORT" || err "Port de Lyrion invalide."
fi
echo ""

# Fournisseur d'IA
msg "Cerveau de Lapinou..."
echo "  1) Anthropic (Claude, recommandé)"
echo "  2) Fournisseur compatible OpenAI (Mistral, OpenAI, Groq, OpenRouter, Ollama...)"
echo "  3) Les deux (Claude d'abord, l'autre en secours)"
ask PROVIDER "Votre choix" 1
ANTHROPIC_KEY="" OAI_NAME="" OAI_URL="" OAI_KEY="" OAI_MODEL=""
case "$PROVIDER" in 1|2|3) ;; *) err "Choix de fournisseur invalide." ;; esac
if [ "$PROVIDER" != "2" ]; then
    ask_secret ANTHROPIC_KEY "Clé API Anthropic"
    [ -n "$ANTHROPIC_KEY" ] || err "La clé API Anthropic est obligatoire pour ce choix."
    is_header_safe "$ANTHROPIC_KEY" || err "Clé API Anthropic invalide (ni guillemet ni barre oblique inverse)."
fi
if [ "$PROVIDER" != "1" ]; then
    ask OAI_NAME "Nom du fournisseur" "openai"
    { [[ "$OAI_NAME" =~ ^[a-z0-9_-]+$ ]] && [ "$OAI_NAME" != "anthropic" ]; } || err "Nom de fournisseur invalide."
    ask OAI_URL "Adresse de son API" "https://api.openai.com/v1"
    is_http_url "$OAI_URL" || err "Adresse d'API invalide."
    ask OAI_MODEL "Modèle"
    [ -n "$OAI_MODEL" ] || err "Le modèle est obligatoire."
    ask_secret OAI_KEY "Clé API (vide : aucune, par ex. Ollama)"
    is_header_safe "$OAI_KEY" || err "Clé API invalide (ni guillemet ni barre oblique inverse)."
fi
ask BUDGET "Budget mensuel (dollars)" 20
[[ "$BUDGET" =~ ^[0-9]+([.][0-9]+)?$ ]] || err "Budget invalide."
DEFAULT_TZ=$(cat /etc/timezone 2>/dev/null || true)
ask TIMEZONE "Fuseau horaire" "${DEFAULT_TZ:-Europe/Paris}"
echo ""

# Page de réglages
msg "Mot de passe admin de la page de réglages (8 caractères au moins)..."
ask_secret ADMIN_PASS "Mot de passe admin"
[ "${#ADMIN_PASS}" -ge 8 ] || err "Mot de passe trop court."
ask_secret ADMIN_PASS2 "Encore une fois"
[ "$ADMIN_PASS" = "$ADMIN_PASS2" ] || err "Les deux mots de passe diffèrent."
ask RABBIT_IP "Adresse IP du lapin (vide : inconnue)"
[ -z "$RABBIT_IP" ] || is_ipv4 "$RABBIT_IP" || err "Adresse du lapin invalide."
echo ""

run_checks

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Résumé :"
[ -n "$REINSTALL_ID" ] && echo -e "  ${RED}Conteneur $REINSTALL_ID effacé puis recréé${NC}"
echo "  Modèle : ${TEMPLATE_DOWNLOAD:-$TEMPLATE}"
echo "  Conteneur : $CTID « $CT_HOSTNAME » (${RAM} Mo, ${CORES} cœurs, ${DISK} Go sur $STORAGE), non privilégié, démarrage auto"
echo "  Réseau : $BRIDGE, $NET_SHOWN"
echo "  GitHub : $GH_REPO (branche $GH_BRANCH), jeton $(shown "$GH_TOKEN")"
echo "  MQTT : $MQTT_HOST:$MQTT_PORT, identifiant ${MQTT_USER:-(aucun)}, mot de passe $(shown "$MQTT_PASS")"
echo "  Home Assistant : ${HA_URL:-(aucun)}, jeton $(shown "$HA_TOKEN")"
echo "  Lyrion : ${LMS_HOST:+$LMS_HOST:$LMS_PORT}${LMS_HOST:-(aucun)}"
echo "  Anthropic : clé $(shown "$ANTHROPIC_KEY")${OAI_URL:+ ; $OAI_NAME : $OAI_URL, modèle $OAI_MODEL, clé $(shown "$OAI_KEY")}"
echo "  Budget : $BUDGET \$ par mois ; fuseau : $TIMEZONE"
echo "  Lapin : ${RABBIT_IP:-(inconnu)}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
ask CONFIRM "Confirmer l'installation ? (o/n)"
[[ "$CONFIRM" =~ ^[OoYy]$ ]] || err "Installation annulée."

# Réponses pour le conteneur : CLÉ=base64, fichier 0600 (printf et read sont internes à bash :
# aucune valeur n'apparaît dans les arguments d'un processus).
put() { printf '%s=%s\n' "$1" "$(printf '%s' "$2" | base64 -w0)"; }
(
    umask 077
    {
        put GH_REPO "$GH_REPO"; put GH_BRANCH "$GH_BRANCH"; put GH_TOKEN "$GH_TOKEN"
        put MQTT_HOST "$MQTT_HOST"; put MQTT_PORT "$MQTT_PORT"; put MQTT_USER "$MQTT_USER"
        put MQTT_PASS "$MQTT_PASS"; put HA_URL "$HA_URL"; put HA_TOKEN "$HA_TOKEN"
        put LMS_HOST "$LMS_HOST"; put LMS_PORT "$LMS_PORT"; put ANTHROPIC_KEY "$ANTHROPIC_KEY"
        put OAI_NAME "$OAI_NAME"; put OAI_URL "$OAI_URL"; put OAI_KEY "$OAI_KEY"
        put OAI_MODEL "$OAI_MODEL"; put BUDGET "$BUDGET"; put TIMEZONE "$TIMEZONE"
        put ADMIN_PASS "$ADMIN_PASS"; put PUBLIC_URL "$PUBLIC_URL"; put RABBIT_IP "$RABBIT_IP"
    } > "$WORK/answers"
)
get_setup_files

if [ -n "$REINSTALL_ID" ]; then
    msg "Effacement du conteneur $REINSTALL_ID..."
    pct stop "$REINSTALL_ID" >/dev/null 2>&1 || true
    pct destroy "$REINSTALL_ID" --purge || err "Impossible d'effacer le conteneur $REINSTALL_ID"
    ok "Conteneur $REINSTALL_ID effacé"
fi

if [ -n "$TEMPLATE_DOWNLOAD" ]; then
    msg "Téléchargement du modèle $TEMPLATE_DOWNLOAD..."
    pveam download "$TPL_STORE" "$TEMPLATE_DOWNLOAD" || err "Échec du téléchargement du modèle."
fi

echo ""
msg "Création du conteneur $CTID..."
pct create "$CTID" "$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CORES" \
    --memory "$RAM" \
    --swap 512 \
    --rootfs "$STORAGE:$DISK" \
    --net0 "$NET0" \
    --unprivileged 1 \
    --onboot 1 \
    --features nesting=1 \
    --ostype debian \
    --tags lapinou \
    || err "Échec de la création du conteneur."
pct start "$CTID" || err "Impossible de démarrer le conteneur."
ok "Conteneur $CTID créé et démarré"

msg "Vérification de l'accès à internet depuis le conteneur..."
ONLINE=0
for _ in $(seq 1 30); do
    if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then ONLINE=1; break; fi
    sleep 2
done
if [ "$ONLINE" = 0 ] && pct exec "$CTID" -- ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
    warn "Pas de DNS dans le conteneur : 1.1.1.1 et 9.9.9.9 ajoutés"
    pct exec "$CTID" -- sh -c 'printf "nameserver 1.1.1.1\nnameserver 9.9.9.9\n" > /etc/resolv.conf'
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && ONLINE=1
fi
[ "$ONLINE" = 1 ] || err "Le conteneur n'a pas accès à internet. Vérifiez le pont $BRIDGE, la passerelle et le pare-feu Proxmox.
Diagnostic : pct enter $CTID ; ip addr ; ip route ; ping 1.1.1.1"
ok "Internet joignable"

msg "Installation du serveur dans le conteneur (10 à 20 minutes)..."
push_setup
pct exec "$CTID" -- bash "$CT_SETUP/provision.sh" --install "$CT_SETUP/answers" \
    || err "L'installation dans le conteneur a échoué (voir ci-dessus). Relancez ce script pour réessayer (réinstallation)."

if [ -z "$CT_IP" ]; then
    CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
fi

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║                 ✅ INSTALLATION TERMINÉE !                     ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "🌐 Page de réglages : http://${CT_IP:-<adresse du conteneur>}:8080"
echo "   (mot de passe admin : celui que vous avez choisi)"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Installer un lapin (clé USB)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  1. Page de réglages > Clé USB : Wi-Fi du lapin, puis télécharger l'archive."
echo "  2. Dézipper l'archive sur une clé USB vide formatée en FAT32."
echo "  3. Lapin éteint : mettre la clé, allumer, attendre la fin de l'installation."
[ -n "$RABBIT_IP" ] && echo "  Lapin connu : $RABBIT_IP"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Commandes utiles"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Journal du serveur :  pct exec $CTID -- journalctl -u lapinou -f"
echo "  Entrer :              pct enter $CTID"
echo "  Mettre à jour :       bouton « Mettre à jour » de la page, ou relancer ce script"
echo ""
