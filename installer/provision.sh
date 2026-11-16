#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
###############################################################################
# LapiNou - installation du serveur DANS le conteneur (lancé par lapinou-install.sh)
#
#   bash provision.sh --install <fichier-réponses>   première installation
#   bash provision.sh --update <dépôt> <branche>     mise à jour (git pull + dépendances)
#   bash provision.sh --post-update                  dépendances seules (post-update.sh)
#
# Lancé en root dans le LXC (pct exec). Le fichier de réponses (0600, une ligne
# CLÉ=valeur-en-base64) est effacé dès la fin du script, réussite ou échec : les
# secrets ne passent jamais par la ligne de commande d'un processus.
#
# Crée l'utilisateur système lapinou et :
#   /opt/lapinou      clone du dépôt (à lapinou), venv server/.venv
#   /etc/lapinou      config.toml, secrets.toml (0600), git-credentials (0600)
#   /var/lib/lapinou  données et HOME du service (modèles, ha-mcp dans .local/bin)
# Installe les unités lapinou.service, lapinou-update.service/.path (bouton
# « Mettre à jour » -> post-update.sh en root) et la règle polkit du bouton
# « Redémarrer ». Chemins décrits dans docs/lapinou/service.md.
###############################################################################
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
msg() { echo -e "${BLUE}▶${NC} $1"; }
ok() { echo -e "${GREEN}✓${NC} $1"; }
err() { echo -e "${RED}✗${NC} $1" >&2; exit 1; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

# Préfixe des chemins : vide en vrai ; un dossier temporaire dans installer/tests.
R="${LAPINOU_ROOT:-}"
ETC="$R/etc/lapinou"
DATA="$R/var/lib/lapinou"
OPT="$R/opt/lapinou"
LIB="$R/usr/local/lib/lapinou"
UNITS="$R/etc/systemd/system"
POLKIT="$R/etc/polkit-1/rules.d"
JOURNALD="$R/etc/systemd/journald.conf.d"
UV="${LAPINOU_UV:-/usr/local/bin/uv}"
HEALTH_URL="${LAPINOU_HEALTH_URL:-http://127.0.0.1:8080/health}"
HEALTH_TRIES="${LAPINOU_HEALTH_TRIES:-90}"

# Versions épinglées (uv : celle du Dockerfile ; ha-mcp demande Python >= 3.13).
UV_VERSION="0.13.8"
HA_MCP_VERSION="9.2.0"
PACKAGES=(ca-certificates curl git ffmpeg protobuf-compiler libspeexdsp-dev python3 python3-dev
          python3-venv gcc libc6-dev dbus polkitd)

SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODE="${1:-}"
ANSWERS="${2:-}"
declare -A A=()

cleanup() {
    if [ -n "$ANSWERS" ] && [ -e "$ANSWERS" ]; then
        shred -u "$ANSWERS" 2>/dev/null || rm -f "$ANSWERS"
    fi
}
trap cleanup EXIT

# Branche git : ni « - » au début (option), ni « .. » (chemin).
is_branch() { [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9._/-]*$ && "$1" != *..* ]]; }

# Lance une commande en tant que lapinou (HOME = dossier de données, outils de ~/.local/bin).
as_lapinou() {
    runuser -u lapinou -- env HOME="$DATA" PATH="$DATA/.local/bin:$PATH" "$@"
}

load_answers() {
    [ -f "$ANSWERS" ] || err "Fichier de réponses introuvable : $ANSWERS"
    local line key
    # Pas de « IFS== read » : il mangerait le « = » final du base64.
    while IFS= read -r line; do
        key=${line%%=*}
        [[ "$key" =~ ^[A-Z0-9_]+$ ]] || continue
        A[$key]="$(printf '%s' "${line#*=}" | base64 -d)"
    done < "$ANSWERS"
    [[ "${A[GH_REPO]:-}" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || err "Dépôt GitHub invalide."
    is_branch "${A[GH_BRANCH]:-}" || err "Branche invalide."
    [[ "${A[GH_TOKEN]:-}" =~ ^[A-Za-z0-9_]*$ ]] || err "Jeton GitHub invalide."
}

install_packages() {
    msg "Paquets système (git, ffmpeg, protoc, libspeexdsp, compilateur, polkit)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${PACKAGES[@]}" >/dev/null
    ok "Paquets installés"
}

install_uv() {
    if [ "$("$UV" --version 2>/dev/null | awk '{print $2}')" = "$UV_VERSION" ]; then
        return
    fi
    msg "Installation de uv $UV_VERSION (venv dédié, depuis PyPI)..."
    mkdir -p "$LIB"
    python3 -m venv "$LIB/uv"
    "$LIB/uv/bin/pip" install -q "uv==$UV_VERSION"
    ln -sf "$LIB/uv/bin/uv" "$R/usr/local/bin/uv"
    ln -sf "$LIB/uv/bin/uvx" "$R/usr/local/bin/uvx"
    ok "uv $UV_VERSION"
}

create_user_and_dirs() {
    if ! id lapinou >/dev/null 2>&1; then
        useradd --system --user-group --home-dir /var/lib/lapinou --shell /usr/sbin/nologin lapinou
        ok "Utilisateur système lapinou créé"
    fi
    local d
    for d in "$ETC" "$DATA" "$DATA/update" "$OPT"; do
        [ ! -L "$d" ] || err "$d est un lien symbolique : refusé."
        [ ! -e "$d" ] || [ -d "$d" ] || err "$d existe et n'est pas un dossier : refusé."
        mkdir -p "$d"
    done
    chown -h lapinou:lapinou "$ETC" "$DATA" "$DATA/update" "$OPT"
    chmod 0750 "$ETC" "$DATA" "$DATA/update"
    chmod 0755 "$OPT"
}

# Mise à jour : ces dossiers appartiennent à lapinou, qui peut y remplacer un dossier par un
# lien symbolique. En root on ne fait que vérifier (pas de lien, de vrais dossiers) ; seul
# update/, s'il manque (installation ancienne), est créé dans un dossier vérifié.
check_dirs() {
    local d
    for d in "$ETC" "$DATA" "$OPT"; do
        { [ ! -L "$d" ] && [ -d "$d" ]; } || err "$d n'est pas un dossier (lien symbolique ?) : mise à jour refusée."
    done
    if [ -L "$DATA/update" ] || { [ -e "$DATA/update" ] && [ ! -d "$DATA/update" ]; }; then
        err "$DATA/update n'est pas un dossier (lien symbolique ?) : mise à jour refusée."
    fi
    if [ ! -e "$DATA/update" ]; then
        # Créé directement en 0750 (pas de chmod, qui suivrait un lien posé entre-temps).
        mkdir -m 0750 "$DATA/update"
        chown -h lapinou:lapinou "$DATA/update"
    fi
}

# Accès git du dépôt privé : fichier d'identifiants 0600 lu par « git credential store »
# (jamais de jeton dans l'URL du remote ni dans une ligne de commande).
write_git_credentials() {
    local file="$ETC/git-credentials"
    if [ -z "${A[GH_TOKEN]:-}" ]; then
        rm -f "$file"
        return
    fi
    (umask 077; printf 'https://x-access-token:%s@github.com\n' "${A[GH_TOKEN]}" > "$file")
    chown lapinou:lapinou "$file"
    chmod 0600 "$file"
    ok "Accès au dépôt privé enregistré (/etc/lapinou/git-credentials, 0600)"
}

clone_repo() {
    local helper=()
    if [ -f "$ETC/git-credentials" ]; then
        helper=(-c credential.helper= -c "credential.helper=store --file=/etc/lapinou/git-credentials")
    fi
    if [ -d "$OPT/.git" ]; then
        # Dépôt et branche : ceux d'install.conf (ou retapés sur l'hôte), pas ceux du clone.
        msg "Dépôt déjà présent : mise à jour depuis ${A[GH_REPO]} (branche ${A[GH_BRANCH]})..."
        as_lapinou git -C "$OPT" remote set-url origin "https://github.com/${A[GH_REPO]}.git"
        as_lapinou git -C "$OPT" fetch -q origin "${A[GH_BRANCH]}" || err "git fetch a échoué dans /opt/lapinou"
        if [ "$(as_lapinou git -C "$OPT" rev-parse --abbrev-ref HEAD)" = "${A[GH_BRANCH]}" ]; then
            as_lapinou git -C "$OPT" merge -q --ff-only FETCH_HEAD || err "git pull a échoué dans /opt/lapinou"
        else
            as_lapinou git -C "$OPT" checkout -q -B "${A[GH_BRANCH]}" FETCH_HEAD \
                || err "Passage à la branche ${A[GH_BRANCH]} impossible"
        fi
    else
        msg "Clonage de ${A[GH_REPO]} (branche ${A[GH_BRANCH]})..."
        as_lapinou git "${helper[@]}" clone -q --branch "${A[GH_BRANCH]}" \
            "https://github.com/${A[GH_REPO]}.git" "$OPT" \
            || err "Clonage impossible (jeton, dépôt ou branche ?)"
    fi
    if [ -f "$ETC/git-credentials" ]; then
        as_lapinou git -C "$OPT" config credential.helper "store --file=/etc/lapinou/git-credentials"
    fi
    ok "Code dans /opt/lapinou ($(as_lapinou git -C "$OPT" rev-parse --short HEAD))"
}

# Dépendances Python, ha-mcp, modèles : partagé avec post-update.sh (même ordre).
python_deps() {
    local python=()
    # Debian 13 : Python 3.13 du système (roues de speexaec). Debian 12 (3.11) : uv télécharge
    # un Python 3.12, et speexaec se compile (gcc, libspeexdsp-dev).
    if python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        python=(--python /usr/bin/python3)
    else
        python=(--python 3.12)
    fi
    msg "Environnement Python du serveur (uv sync, quelques minutes)..."
    (cd "$OPT/server" && as_lapinou "$UV" sync -q --frozen --no-dev "${python[@]}") \
        || err "uv sync a échoué"
    ok "venv /opt/lapinou/server/.venv"

    msg "ha-mcp $HA_MCP_VERSION (outil Home Assistant, ~/.local/bin)..."
    as_lapinou "$UV" tool install -q --python 3.13 "ha-mcp==$HA_MCP_VERSION" \
        || err "Installation de ha-mcp impossible"
    ok "ha-mcp $HA_MCP_VERSION"

    msg "Modèles (Parakeet, Silero VAD, voix Piper ; SHA-256 vérifiés)..."
    (cd "$OPT/server" && as_lapinou "$OPT/server/.venv/bin/python" -m lapinou.voice.models fetch) \
        || err "Téléchargement des modèles impossible"
    if [ ! -x "$DATA/.local/share/lapinou/piper-venv/bin/piper" ]; then
        (cd "$OPT/server" && as_lapinou "$OPT/server/.venv/bin/python" -m lapinou.voice.models piper-setup) \
            || err "Installation de Piper impossible"
    fi
    ok "Modèles et Piper prêts"
}

# Réglages : écrits par lapinou.config (TOML échappé, secrets.toml 0600) ; les réponses
# arrivent sur l'entrée standard, jamais en argument.
write_config() {
    msg "Écriture de /etc/lapinou/config.toml et secrets.toml..."
    local py
    py=$(cat <<'PY'
import base64, sys
from pathlib import Path
from lapinou.config import Settings, load_settings, save_settings

a = {}
for line in sys.stdin:
    key, _, value = line.strip().partition("=")
    if key:
        a[key] = base64.b64decode(value).decode("utf-8")
path = Path(sys.argv[1])
data = load_settings(path).model_dump(mode="python")
get = lambda k, d="": a.get(k, "") or d
data["server"]["public_url"] = get("PUBLIC_URL")
data["server"]["timezone"] = get("TIMEZONE", "Europe/Paris")
data["mqtt"].update(host=get("MQTT_HOST"), port=int(get("MQTT_PORT", "1883")),
                    username=get("MQTT_USER"), password=get("MQTT_PASS"))
data["ha"].update(url=get("HA_URL"), token=get("HA_TOKEN"))
data["lms"].update(host=get("LMS_HOST"), port=int(get("LMS_PORT", "9000")))
providers = data["brain"]["providers"]
if get("ANTHROPIC_KEY"):
    providers["anthropic"] = {"kind": "anthropic", "base_url": "", "api_key": get("ANTHROPIC_KEY")}
if get("OAI_KEY") or get("OAI_URL"):
    name = get("OAI_NAME", "openai")
    providers[name] = {"kind": "openai", "base_url": get("OAI_URL"), "api_key": get("OAI_KEY")}
    ref = f"{name}:{get('OAI_MODEL')}"
    if get("ANTHROPIC_KEY"):
        data["brain"]["fallback_chain"] = [ref, "homeassistant"]
    else:
        data["brain"]["primary"] = data["brain"]["fast"] = ref
data["budget"]["monthly_usd"] = float(get("BUDGET", "20"))
data["ota"].update(repo=get("GH_REPO", "Kinder0083/LapiNou"), token=get("GH_TOKEN"))
settings = Settings.model_validate(data)
settings._path = path
save_settings(settings)
PY
)
    (cd "$OPT/server" && as_lapinou "$OPT/server/.venv/bin/python" -c "$py" "$ETC/config.toml" \
        < "$ANSWERS") || err "Réglages refusés (voir le message ci-dessus)"
    # Mot de passe admin de la page (R40) : haché argon2id par le serveur, lu sur stdin.
    printf '%s\n' "${A[ADMIN_PASS]}" \
        | (cd "$OPT/server" && as_lapinou "$OPT/server/.venv/bin/python" -m lapinou.web.auth \
            --config "$ETC/config.toml") >/dev/null \
        || err "Mot de passe admin refusé (8 caractères au moins)"
    chown lapinou:lapinou "$ETC/config.toml" "$ETC/secrets.toml"
    chmod 0600 "$ETC/secrets.toml"
    ok "Réglages écrits (secrets.toml 0600, mot de passe admin haché)"
}

install_units() {
    msg "Services systemd, règle polkit, mise à jour par la page..."
    mkdir -p "$LIB" "$UNITS" "$POLKIT" "$JOURNALD"
    # Copies root : lapinou-update.service n'exécute jamais en root un fichier que lapinou peut
    # modifier (/opt/lapinou lui appartient).
    # Ces trois fichiers viennent du dossier de ce script (poussé par lapinou-install.sh depuis
    # GitHub), pas du clone : celui-ci appartient à lapinou.
    cp "$SELF_DIR/post-update.sh" "$LIB/post-update.sh.new"
    cp "$SELF_DIR/provision.sh" "$LIB/provision.sh.new"
    mv -f "$LIB/post-update.sh.new" "$LIB/post-update.sh"
    mv -f "$LIB/provision.sh.new" "$LIB/provision.sh"
    chown root:root "$LIB/post-update.sh" "$LIB/provision.sh"
    chmod 0755 "$LIB/post-update.sh" "$LIB/provision.sh"
    # Dépôt et branche de cette installation, relus par lapinou-install.sh à la mise à jour
    # (le clone, à lapinou, ne fait pas foi).
    printf '# SPDX-License-Identifier: MIT\n# Écrit par provision.sh (root) : lu par lapinou-install.sh.\nREPO=%s\nBRANCH=%s\n' \
        "${A[GH_REPO]}" "${A[GH_BRANCH]}" > "$LIB/install.conf.new"
    chown root:root "$LIB/install.conf.new"
    chmod 0644 "$LIB/install.conf.new"
    mv -f "$LIB/install.conf.new" "$LIB/install.conf"
    cp "$SELF_DIR/lapinou.service" "$UNITS/lapinou.service"
    chmod 0644 "$UNITS/lapinou.service"

    cat > "$UNITS/lapinou-update.service" <<'UNIT'
# SPDX-License-Identifier: MIT
# Bouton « Mettre à jour » : post-update.sh en root (installé par installer/provision.sh).
[Unit]
Description=LapiNou, mise à jour demandée par la page de réglages

[Service]
Type=oneshot
ExecStart=/usr/local/lib/lapinou/post-update.sh --root
TimeoutStartSec=900
UNIT

    cat > "$UNITS/lapinou-update.path" <<'UNIT'
# SPDX-License-Identifier: MIT
# La page (utilisateur lapinou) écrit une demande ici ; systemd lance lapinou-update.service.
[Unit]
Description=LapiNou, demandes de mise à jour de la page de réglages

[Path]
PathChanged=/var/lib/lapinou/update/request
Unit=lapinou-update.service

[Install]
WantedBy=multi-user.target
UNIT

    # Bouton « Redémarrer » : lapinou peut redémarrer lapinou.service, rien d'autre (sudo est
    # exclu par NoNewPrivileges).
    cat > "$POLKIT/50-lapinou.rules" <<'RULES'
// SPDX-License-Identifier: MIT
// Le service lapinou peut redémarrer lapinou.service (bouton « Redémarrer » de la page).
polkit.addRule(function (action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "lapinou.service" &&
        action.lookup("verb") == "restart" &&
        subject.user == "lapinou") {
        return polkit.Result.YES;
    }
});
RULES
    chmod 0644 "$POLKIT/50-lapinou.rules"

    # Rotation des journaux : journald, plafonné.
    printf '# SPDX-License-Identifier: MIT\n[Journal]\nSystemMaxUse=200M\n' > "$JOURNALD/lapinou.conf"

    systemctl daemon-reload
    systemctl restart systemd-journald >/dev/null 2>&1 || true
    systemctl restart polkit >/dev/null 2>&1 || true
    systemctl enable -q --now lapinou-update.path
    systemctl enable -q lapinou
    ok "Unités installées"
}

# Dans un LXC non privilégié, certaines options de durcissement font échouer le démarrage
# (status=226/NAMESPACE) : on les retire par une surcharge, par paliers.
DROPIN_1="ProtectHostname=no
ProtectClock=no"
# shellcheck disable=SC2034  # lu par ${!body}
DROPIN_2="$DROPIN_1
ProtectKernelTunables=no
ProtectKernelModules=no
ProtectKernelLogs=no
ProtectControlGroups=no
PrivateDevices=no"

namespace_failure() {
    [ "$(systemctl show -p ExecMainStatus --value lapinou 2>/dev/null)" = "226" ]
}

start_service() {
    msg "Démarrage de lapinou.service..."
    local level
    for level in 0 1 2; do
        if [ "$level" -gt 0 ]; then
            local body="DROPIN_$level"
            warn "Démarrage refusé par le conteneur (226/NAMESPACE) : durcissement allégé (palier $level)"
            mkdir -p "$UNITS/lapinou.service.d"
            printf '# SPDX-License-Identifier: MIT\n# Ajouté par provision.sh : options refusées dans ce LXC.\n[Service]\n%s\n' \
                "${!body}" > "$UNITS/lapinou.service.d/lxc.conf"
            systemctl daemon-reload
            systemctl reset-failed lapinou >/dev/null 2>&1 || true
        fi
        if systemctl restart lapinou && sleep 3 && systemctl is-active -q lapinou; then
            ok "lapinou.service actif"
            return
        fi
        namespace_failure || break
    done
    err "lapinou.service ne démarre pas. Diagnostic : journalctl -u lapinou -n 50"
}

health_check() {
    msg "Vérification de $HEALTH_URL..."
    local i
    for ((i = 1; i <= HEALTH_TRIES; i++)); do
        if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
            ok "Le serveur répond ($HEALTH_URL)"
            return
        fi
        sleep 2
    done
    err "Le serveur ne répond pas sur $HEALTH_URL. Diagnostic : journalctl -u lapinou -n 50"
}

[ "$(id -u)" = "0" ] || err "provision.sh se lance en root dans le conteneur."
case "$MODE" in
    --install)
        load_answers
        install_packages
        install_uv
        create_user_and_dirs
        write_git_credentials
        clone_repo
        python_deps
        write_config
        cleanup
        install_units
        start_service
        health_check
        ok "Installation terminée"
        ;;
    --update)
        A[GH_REPO]="${2:-}"
        A[GH_BRANCH]="${3:-}"
        ANSWERS=""
        [[ "${A[GH_REPO]}" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || err "usage : provision.sh --update <dépôt> <branche>"
        is_branch "${A[GH_BRANCH]}" || err "Branche invalide."
        id lapinou >/dev/null 2>&1 || err "Pas d'utilisateur lapinou : choisissez la réinstallation."
        check_dirs
        [ -d "$OPT/.git" ] || err "Aucune installation dans /opt/lapinou : choisissez la réinstallation."
        install_packages
        install_uv
        clone_repo
        python_deps
        install_units
        start_service
        health_check
        ok "Mise à jour terminée"
        ;;
    --post-update)
        # Bouton « Mettre à jour » (git pull déjà fait par la page, redémarrage ensuite par
        # elle). Rien du dépôt n'est installé en root : une unité modifiée attend l'installateur.
        [ -d "$OPT/.git" ] || err "Aucune installation dans /opt/lapinou."
        install_packages
        install_uv
        python_deps
        changed=()
        cmp -s "$OPT/server/systemd/lapinou.service" "$UNITS/lapinou.service" || changed+=(lapinou.service)
        cmp -s "$OPT/installer/provision.sh" "$LIB/provision.sh" || changed+=(provision.sh)
        cmp -s "$OPT/installer/post-update.sh" "$LIB/post-update.sh" || changed+=(post-update.sh)
        if [ "${#changed[@]}" -gt 0 ]; then
            warn "${changed[*]} : changé dans le dépôt ; relancez l'installateur sur Proxmox (mise à jour) pour l'appliquer."
        fi
        ok "Dépendances à jour"
        ;;
    *)
        err "usage : provision.sh --install <fichier-réponses> | --update <dépôt> <branche> | --post-update"
        ;;
esac
