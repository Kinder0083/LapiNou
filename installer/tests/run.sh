#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
###############################################################################
# Tests de l'installateur Proxmox (lapinou-install.sh, provision.sh, post-update.sh)
#
#   bash installer/tests/run.sh
#
# Les commandes Proxmox (pct, pveam, pvesm, pvesh, qm, ip...) et système (systemctl,
# apt-get, git, runuser...) sont simulées par installer/tests/stubs, qui journalisent
# chaque appel et ses arguments : on vérifie ce qui est lancé, et qu'aucun secret
# n'apparaît dans les arguments d'un processus. Un faux broker MQTT (fake_mqtt.py)
# répond au test de connexion. Rien n'est installé ni modifié hors d'un dossier
# temporaire. Demande python3 et shellcheck.
###############################################################################
# Les « bash -c '...' » des vérifications reçoivent leurs valeurs en arguments.
# shellcheck disable=SC2016
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
INSTALLER=$(dirname "$HERE")
REPO=$(dirname "$INSTALLER")
STUBS="$HERE/stubs"
PASSED=0
FAILED=0
FAILURES=()
CASE_DIRS=()
CASE=""
T=""
BROKER_PID=""

# Secrets des tests : caractères spéciaux, espaces, accents, $(...) jamais évalué.
GH_TOKEN_IN="ghp_TESTtoken0123456789abcdefXYZ"
MQTT_PASS_IN='mq p@ss "w$rd;'"'"' é'
HA_TOKEN_IN='eyJhbGciOiJIUzI1NiJ9.HA-secret-token'
ANTH_KEY_IN='sk-ant-api03-TEST-secret-KEY'
OAI_KEY_IN='sk-oai-TEST-secret-KEY'
ADMIN_IN='Adm1n "$(id)";é pass'
SECRETS=("$GH_TOKEN_IN" "$MQTT_PASS_IN" "$HA_TOKEN_IN" "$ANTH_KEY_IN" "$OAI_KEY_IN" "$ADMIN_IN")

stop_broker() {
    if [ -n "$BROKER_PID" ]; then kill "$BROKER_PID" 2>/dev/null; wait "$BROKER_PID" 2>/dev/null; fi
    BROKER_PID=""
}
trap 'stop_broker' EXIT

new_case() {
    stop_broker
    CASE="$1"
    T=$(mktemp -d "${TMPDIR:-/tmp}/lapinou-installer-test.XXXXXX")
    CASE_DIRS+=("$T")
    mkdir -p "$T/tmp"
    : > "$T/log"
    # Variables des commandes simulées remises à zéro.
    while read -r v; do unset "$v"; done < <(compgen -v | grep '^STUB_' || true)
    unset FAKE_RC
    export STUB_DIR="$T" STUB_LOG="$T/log"
    GH_IN="$GH_TOKEN_IN" ID_IN="" NET_IN=1 PROVIDER_IN=1 CONFIRM_IN="o" PREFIX_IN=() MQTT_USER_IN="mqttuser"
    MQTT_PORT_IN="" LMS_IN="192.168.1.20" REINSTALL_IN=0 MQTT_HOST_IN="127.0.0.1"
    HA_TOKEN_ANS="$HA_TOKEN_IN" RAM_IN="" IP_IN=""
}

check() {  # check "description" commande...
    if "${@:2}"; then
        PASSED=$((PASSED + 1))
    else
        FAILED=$((FAILED + 1))
        FAILURES+=("$CASE : $1")
        echo "  ÉCHEC [$CASE] $1 ($T)"
    fi
}

log_has() { grep -qF -- "$1" "$T/log"; }
log_lacks() { ! grep -qF -- "$1" "$T/log"; }
out_has() { grep -qF -- "$1" "$T/out"; }
rc_is() { [ "$RC" = "$1" ]; }
rc_not0() { [ "$RC" != 0 ]; }
file_has() { grep -qF -- "$2" "$1"; }
no_secret_in() {  # aucun secret (ni son base64) dans ce fichier
    local s
    for s in "${SECRETS[@]}"; do
        grep -qF -- "$s" "$1" && return 1
        grep -qF -- "$(printf '%s' "$s" | base64 -w0)" "$1" && return 1
    done
    return 0
}
answer_is() {  # answer_is fichier CLÉ valeur
    [ "$(grep "^$2=" "$1" | cut -d= -f2- | base64 -d)" = "$3" ]
}

start_broker() {  # start_broker code-connack
    python3 "$HERE/fake_mqtt.py" "$T/broker-port" "$T/broker-seen" "$1" &
    BROKER_PID=$!
    for _ in $(seq 1 50); do [ -s "$T/broker-port" ] && break; /bin/sleep 0.1; done
    MQTT_PORT_IN=$(cat "$T/broker-port")
}

closed_port() {  # un port local où rien n'écoute
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

# Réponses dans l'ordre des questions de lapinou-install.sh.
answers() {
    local a=("${PREFIX_IN[@]}")
    a+=("" "" "$GH_IN")                 # dépôt, branche, jeton
    [ "$REINSTALL_IN" = 1 ] || a+=("$ID_IN")  # ID (pas demandé à la réinstallation)
    a+=("" "$RAM_IN" "" "")             # nom, RAM, cœurs, disque
    if [ "$NET_IN" = 2 ]; then a+=("2"); else a+=("" "$IP_IN" "" ""); fi  # mode, IP, masque, passerelle
    a+=("$MQTT_HOST_IN" "$MQTT_PORT_IN" "$MQTT_USER_IN")
    [ -n "$MQTT_USER_IN" ] && a+=("$MQTT_PASS_IN")
    a+=("http://192.168.1.20:8123" "$HA_TOKEN_ANS")
    a+=("$LMS_IN")                      # Lyrion (vide ou - : sans)
    if [ -n "$LMS_IN" ] && [ "$LMS_IN" != "-" ]; then a+=(""); fi  # port
    case "$PROVIDER_IN" in
        1) a+=("" "$ANTH_KEY_IN") ;;
        2) a+=("2" "mistral" "https://api.mistral.ai/v1" "mistral-small-latest" "$OAI_KEY_IN") ;;
    esac
    a+=("" "")                          # budget, fuseau
    a+=("$ADMIN_IN" "$ADMIN_IN" "")     # mot de passe admin (2 fois), lapin
    a+=("$CONFIRM_IN")
    printf '%s\n' "${a[@]}"
}

run_install() {
    answers > "$T/tty"
    PATH="$STUBS:$PATH" TMPDIR="$T/tmp" LAPINOU_TTY="$T/tty" \
        bash "$INSTALLER/lapinou-install.sh" > "$T/out" 2>&1 < /dev/null
    RC=$?
}

tmp_empty() { [ -z "$(ls -A "$T/tmp")" ]; }

###############################################################################
echo "== lapinou-install.sh (hôte Proxmox)"
###############################################################################

new_case "réponses par défaut"
start_broker 0
run_install
check "se termine sans erreur" rc_is 0
check "modèle Debian 13 téléchargé" log_has "pveam download local debian-13-standard_13.1-2_amd64.tar.zst"
check "pct create avec le modèle Debian 13" log_has "pct create 105 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"
check "4 cœurs" log_has "--cores 4 "
check "6144 Mo" log_has "--memory 6144 "
check "rootfs local-lvm:20" log_has "--rootfs local-lvm:20 "
check "non privilégié" log_has "--unprivileged 1 "
check "démarrage automatique" log_has "--onboot 1 "
check "IP fixe proposée" log_has "--net0 name=eth0,bridge=vmbr0,ip=192.168.1.160/24,gw=192.168.1.1 "
check "étiquette lapinou" log_has "--tags lapinou"
check "pct start" log_has "pct start 105"
check "fichiers poussés avant provision.sh" file_has "$T/pushed-at-provision" "answers"
check "réponses poussées en 0600 (source 600)" file_has "$T/pushed/perms" "answers 600 0600"
check "provision.sh lancé dans le conteneur" log_has "pct exec 105 -- bash /root/lapinou-setup/provision.sh --install /root/lapinou-setup/answers"
check "dossier d'installation effacé du conteneur" log_has "pct exec 105 -- rm -rf /root/lapinou-setup"
check "aucun secret dans les arguments des commandes" no_secret_in "$T/log"
check "aucun secret affiché" no_secret_in "$T/out"
check "réponses effacées de l'hôte" tmp_empty
check "mot de passe MQTT transmis tel quel" answer_is "$T/pushed/answers" MQTT_PASS "$MQTT_PASS_IN"
check "mot de passe admin transmis tel quel" answer_is "$T/pushed/answers" ADMIN_PASS "$ADMIN_IN"
check "jeton GitHub transmis" answer_is "$T/pushed/answers" GH_TOKEN "$GH_TOKEN_IN"
check "jeton HA transmis" answer_is "$T/pushed/answers" HA_TOKEN "$HA_TOKEN_IN"
check "clé Anthropic transmise" answer_is "$T/pushed/answers" ANTHROPIC_KEY "$ANTH_KEY_IN"
check "Lyrion : hôte de HA, port 9000" answer_is "$T/pushed/answers" LMS_HOST "192.168.1.20"
check "adresse publique du serveur" answer_is "$T/pushed/answers" PUBLIC_URL "http://192.168.1.160:8080"
check "le broker a reçu l'identifiant" python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(bytes.fromhex(d["user"]).decode()!="mqttuser")' "$T/broker-seen"
check "le broker a reçu le mot de passe exact" python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(bytes.fromhex(d["password"]).decode()!=sys.argv[2])' "$T/broker-seen" "$MQTT_PASS_IN"
check "jeton HA envoyé par l'entrée de curl" file_has "$T/curl-stdin" "Authorization: Bearer $HA_TOKEN_IN"
check "clé Anthropic envoyée par l'entrée de curl" file_has "$T/curl-stdin" "x-api-key: $ANTH_KEY_IN"
check "jeton GitHub envoyé par l'entrée de curl" file_has "$T/curl-stdin" "Authorization: Bearer $GH_TOKEN_IN"
check "URL de la page affichée" out_has "http://192.168.1.160:8080"
check "marche à suivre de la clé USB" out_has "FAT32"

new_case "Debian 13 absent : Debian 12 local"
start_broker 0
export STUB_AVAILABLE="debian-12-standard_12.7-1_amd64.tar.zst" STUB_LOCAL_TPL="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
run_install
check "se termine sans erreur" rc_is 0
check "pct create avec Debian 12" log_has "pct create 105 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
check "pas de téléchargement" log_lacks "pveam download"

new_case "Debian 13 déjà local"
start_broker 0
export STUB_LOCAL_TPL="local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"
run_install
check "pct create avec Debian 13 local" log_has "pct create 105 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"
check "pas de téléchargement" log_lacks "pveam download"

new_case "MQTT injoignable"
MQTT_PORT_IN=$(closed_port)
run_install
check "échoue" rc_not0
check "message clair" out_has "broker MQTT injoignable sur 127.0.0.1:$MQTT_PORT_IN"
check "arrêt avant pct create" log_lacks "pct create"
check "aucun modèle téléchargé" log_lacks "pveam download"
check "réponses effacées de l'hôte" tmp_empty

new_case "MQTT : identifiants refusés"
start_broker 5
run_install
check "échoue" rc_not0
check "message clair" out_has "identifiant ou mot de passe MQTT refusé"
check "arrêt avant pct create" log_lacks "pct create"

new_case "Home Assistant : jeton refusé"
start_broker 0
export STUB_HA_CODE=401
run_install
check "échoue" rc_not0
check "message clair" out_has "Home Assistant : jeton refusé"
check "arrêt avant pct create" log_lacks "pct create"

new_case "Anthropic : clé refusée"
start_broker 0
export STUB_ANTHROPIC_CODE=401
run_install
check "échoue" rc_not0
check "message clair" out_has "Anthropic : clé API refusée"
check "arrêt avant pct create" log_lacks "pct create"

new_case "Lyrion injoignable"
start_broker 0
export STUB_LMS_CODE=000
run_install
check "échoue" rc_not0
check "message clair" out_has "Lyrion injoignable"
check "arrêt avant pct create" log_lacks "pct create"

new_case "dépôt privé sans jeton"
start_broker 0
GH_IN=""
export STUB_GH_CODE=404
run_install
check "échoue" rc_not0
check "demande un jeton" out_has "S'il est privé, donnez un jeton"
check "arrêt avant pct create" log_lacks "pct create"

new_case "installation refusée au récapitulatif"
start_broker 0
CONFIRM_IN="n"
run_install
check "s'arrête" rc_not0
check "annulée" out_has "Installation annulée"
check "pas de pct create" log_lacks "pct create"
check "réponses effacées de l'hôte" tmp_empty

new_case "DHCP et fournisseur compatible OpenAI"
start_broker 0
NET_IN=2 PROVIDER_IN=2
run_install
check "se termine sans erreur" rc_is 0
check "réseau DHCP" log_has "--net0 name=eth0,bridge=vmbr0,ip=dhcp "
check "pas d'adresse publique imposée" answer_is "$T/pushed/answers" PUBLIC_URL ""
check "fournisseur transmis" answer_is "$T/pushed/answers" OAI_NAME "mistral"
check "clé transmise" answer_is "$T/pushed/answers" OAI_KEY "$OAI_KEY_IN"
check "pas de clé Anthropic" answer_is "$T/pushed/answers" ANTHROPIC_KEY ""
check "adresse lue dans le conteneur" out_has "http://192.168.1.77:8080"
check "aucun secret dans les arguments des commandes" no_secret_in "$T/log"

new_case "ID déjà pris"
start_broker 0
export STUB_USED_IDS="105"
run_install
check "échoue" rc_not0
check "message clair" out_has "L'ID 105 est déjà utilisé"
check "pas de pct create" log_lacks "pct create"

new_case "conteneur existant : mise à jour proposée"
export STUB_EXISTING=105 STUB_CT_TOKEN="$GH_TOKEN_IN"
printf '\n\n\n\n' > "$T/tty"     # choix 1, dépôt et branche gardés, confirmation
PATH="$STUBS:$PATH" TMPDIR="$T/tmp" LAPINOU_TTY="$T/tty" bash "$INSTALLER/lapinou-install.sh" > "$T/out" 2>&1 < /dev/null
RC=$?
check "se termine sans erreur" rc_is 0
check "propose la mise à jour" out_has "1) Mettre à jour"
check "signale le conteneur existant" out_has "Un conteneur LapiNou existe déjà : 105"
check "lance provision.sh --update avec le dépôt d'install.conf" log_has "pct exec 105 -- bash /root/lapinou-setup/provision.sh --update Kinder0083/LapiNou main"
check "dépôt lu dans install.conf" out_has "Installation trouvée : Kinder0083/LapiNou, branche main (/usr/local/lib/lapinou/install.conf)"
check "clone (modifiable par lapinou) jamais lu" log_lacks "/opt/lapinou/.git/"
check "dépôt du clone modifié sans effet" bash -c '! grep -q evil "$1" "$2"' _ "$T/log" "$T/out"
check "jeton lu : fichier ordinaire, sans lien" log_has 'test -f "$f" && test ! -L "$f" && head -c 4096'
check "lecture du jeton bornée dans le temps" log_has "timeout 15 pct exec 105 -- sh -c f=/etc/lapinou/git-credentials"
check "pas de pct create" log_lacks "pct create"
check "pas de pct destroy" log_lacks "pct destroy"
check "pas de fichier de réponses poussé" log_lacks "/root/lapinou-setup/answers"
check "jeton du conteneur absent des arguments" no_secret_in "$T/log"
check "réponses effacées de l'hôte" tmp_empty

new_case "conteneur existant : sans install.conf"
export STUB_EXISTING=105 STUB_NO_INSTALL_CONF=1
printf '\n\n\n\n' > "$T/tty"
PATH="$STUBS:$PATH" TMPDIR="$T/tmp" LAPINOU_TTY="$T/tty" bash "$INSTALLER/lapinou-install.sh" > "$T/out" 2>&1 < /dev/null
RC=$?
check "se termine sans erreur" rc_is 0
check "le signale" out_has "Pas de /usr/local/lib/lapinou/install.conf lisible"
check "dépôt par défaut, pas celui du clone" log_has "provision.sh --update Kinder0083/LapiNou main"
check "clone jamais lu" log_lacks "/opt/lapinou/.git/"

new_case "conteneur existant : autre dépôt retapé"
export STUB_EXISTING=105
printf '\nautre/depot\ndev\n\n' > "$T/tty"
PATH="$STUBS:$PATH" TMPDIR="$T/tmp" LAPINOU_TTY="$T/tty" bash "$INSTALLER/lapinou-install.sh" > "$T/out" 2>&1 < /dev/null
RC=$?
check "se termine sans erreur" rc_is 0
check "le signale" out_has "Nouveau dépôt ou branche : autre/depot (dev)"
check "provision.sh reçoit le dépôt tapé" log_has "provision.sh --update autre/depot dev"

new_case "conteneur existant : mise à jour refusée"
export STUB_EXISTING=105
printf '\n\n\nn\n' > "$T/tty"
PATH="$STUBS:$PATH" TMPDIR="$T/tmp" LAPINOU_TTY="$T/tty" bash "$INSTALLER/lapinou-install.sh" > "$T/out" 2>&1 < /dev/null
RC=$?
check "s'arrête" rc_not0
check "rien de lancé" log_lacks "provision.sh"

new_case "conteneur existant : réinstallation"
start_broker 0
export STUB_EXISTING=105
PREFIX_IN=("2" "105") REINSTALL_IN=1
run_install
check "se termine sans erreur" rc_is 0
check "efface l'ancien conteneur" log_has "pct destroy 105 --purge"
check "recrée le conteneur sous le même ID" log_has "pct create 105 "
check "ID imposé, affiché" out_has "ID du conteneur : 105 (réinstallation)"
check "lance provision.sh --install" log_has "provision.sh --install"

new_case "conteneur existant : réinstallation non confirmée"
export STUB_EXISTING=105
printf '2\n999\n' > "$T/tty"
PATH="$STUBS:$PATH" TMPDIR="$T/tmp" LAPINOU_TTY="$T/tty" bash "$INSTALLER/lapinou-install.sh" > "$T/out" 2>&1 < /dev/null
RC=$?
check "s'arrête" rc_not0
check "pas de pct destroy" log_lacks "pct destroy"

new_case "Lyrion : réponse vide"
start_broker 0
LMS_IN=""
run_install
check "se termine sans erreur" rc_is 0
check "sans Lyrion" answer_is "$T/pushed/answers" LMS_HOST ""
check "Lyrion pas testé" log_lacks "jsonrpc.js"

new_case "Lyrion : « - »"
start_broker 0
LMS_IN="-"
run_install
check "se termine sans erreur" rc_is 0
check "sans Lyrion" answer_is "$T/pushed/answers" LMS_HOST ""
check "Lyrion pas testé" log_lacks "jsonrpc.js"

new_case "Lyrion protégé (401)"
start_broker 0
export STUB_LMS_CODE=401
run_install
check "se termine sans erreur" rc_is 0
check "avertit" out_has "Lyrion : joignable mais protégé par mot de passe"
check "continue jusqu'à pct create" log_has "pct create 105 "
check "Lyrion gardé" answer_is "$T/pushed/answers" LMS_HOST "192.168.1.20"

new_case "MQTT muet (connexion ouverte, aucune réponse)"
start_broker -1
run_install
check "échoue" rc_not0
check "délai clair" out_has "pas de réponse en 8 s"
check "arrêt avant pct create" log_lacks "pct create"

new_case "IP : octet > 255"
IP_IN="192.168.1.300"
run_install
check "échoue" rc_not0
check "message clair" out_has "Adresse, masque ou passerelle invalide"
check "pas de pct create" log_lacks "pct create"

new_case "RAM nulle"
RAM_IN="0"
run_install
check "échoue" rc_not0
check "message clair" out_has "RAM invalide"

new_case "adresse MQTT invalide"
MQTT_HOST_IN="broker;rm -rf"
run_install
check "échoue" rc_not0
check "message clair" out_has "Adresse du broker MQTT invalide"
check "pas de pct create" log_lacks "pct create"

new_case "jeton HA avec guillemet"
HA_TOKEN_ANS='abc"def'
run_install
check "échoue" rc_not0
check "message clair" out_has "Jeton de Home Assistant invalide"
check "rien envoyé à HA" bash -c '! grep -q "/api/" "$1"' _ "$T/log"

###############################################################################
echo "== provision.sh (dans le conteneur, racine simulée)"
###############################################################################

put() { printf '%s=%s\n' "$1" "$(printf '%s' "$2" | base64 -w0)"; }

setup_root() {
    R="$T/root"
    mkdir -p "$R/opt/lapinou/server/.venv/bin" "$R/opt/lapinou/server/systemd" "$R/usr/local/bin" "$T/setup"
    cp "$INSTALLER/provision.sh" "$INSTALLER/post-update.sh" "$REPO/server/systemd/lapinou.service" "$T/setup/"
    cp "$REPO/server/systemd/lapinou.service" "$R/opt/lapinou/server/systemd/"
    # Faux Python du venv : journalisé, garde son entrée standard, écrit les réglages.
    cat > "$R/opt/lapinou/server/.venv/bin/python" <<EOF
#!/usr/bin/env bash
. "$STUBS/_stub.sh"
case "\$*" in
    -c*|*lapinou.web.auth*) [ -n "\${REAL_PY:-}" ] && exec "\$REAL_PY" "\$@" ;;
esac
n=\$(ls "\$STUB_DIR"/py-stdin.* 2>/dev/null | wc -l)
cat > "\$STUB_DIR/py-stdin.\$n"
case "\$*" in
    -c*|*lapinou.web.auth*) touch "\$LAPINOU_ROOT/etc/lapinou/config.toml" "\$LAPINOU_ROOT/etc/lapinou/secrets.toml" ;;
esac
exit 0
EOF
    chmod +x "$R/opt/lapinou/server/.venv/bin/python"
    (
        umask 077
        {
            put GH_REPO "Kinder0083/LapiNou"; put GH_BRANCH "main"; put GH_TOKEN "$GH_TOKEN_IN"
            put MQTT_HOST "192.168.1.5"; put MQTT_PORT 1883; put MQTT_USER mqttuser
            put MQTT_PASS "$MQTT_PASS_IN"; put HA_TOKEN "$HA_TOKEN_IN"; put ANTHROPIC_KEY "$ANTH_KEY_IN"
            put ADMIN_PASS "$ADMIN_IN"; put HA_URL "http://192.168.1.20:8123"
            put LMS_HOST "192.168.1.20"; put LMS_PORT 9000; put BUDGET "12.5"
            put OAI_NAME mistral; put OAI_URL "https://api.mistral.ai/v1"; put OAI_KEY "$OAI_KEY_IN"
            put OAI_MODEL "mistral-small-latest"; put PUBLIC_URL "http://192.168.1.160:8080"
            put TIMEZONE "Europe/Paris"
        } > "$T/setup/answers"
    )
}

run_provision() {
    PATH="$STUBS:$PATH" LAPINOU_ROOT="$R" LAPINOU_UV="$STUBS/uv" LAPINOU_HEALTH_TRIES=3 \
        bash "$T/setup/provision.sh" "$@" > "$T/out" 2>&1 < /dev/null
    RC=$?
}

py_stdin_with() {  # une entrée du faux Python contient ce texte
    grep -qlF -- "$1" "$T"/py-stdin.* 2>/dev/null
}
mode_is() { [ "$(stat -c %a "$1")" = "$2" ]; }
same_file() { cmp -s "$1" "$2"; }
missing() { [ ! -e "$1" ]; }

new_case "installation"
setup_root
run_provision --install "$T/setup/answers"
check "se termine sans erreur" rc_is 0
check "paquets : ffmpeg, libspeexdsp-dev, python3-dev, gcc, polkit" \
    log_has "apt-get install -y -qq --no-install-recommends ca-certificates curl git ffmpeg protobuf-compiler libspeexdsp-dev python3 python3-dev python3-venv gcc libc6-dev dbus polkitd"
check "utilisateur système lapinou" log_has "useradd --system --user-group --home-dir /var/lib/lapinou --shell /usr/sbin/nologin lapinou"
check "dossiers à lapinou" log_has "chown -h lapinou:lapinou $R/etc/lapinou $R/var/lib/lapinou"
check "/etc/lapinou en 0750" mode_is "$R/etc/lapinou" 750
check "identifiants git en 0600" mode_is "$R/etc/lapinou/git-credentials" 600
check "identifiants git : le jeton" file_has "$R/etc/lapinou/git-credentials" "https://x-access-token:$GH_TOKEN_IN@github.com"
check "clone par l'aide d'identifiants" log_has "credential.helper=store --file=/etc/lapinou/git-credentials clone -q --branch main https://github.com/Kinder0083/LapiNou.git $R/opt/lapinou"
check "remote sans jeton, aide enregistrée" log_has "config credential.helper store --file=/etc/lapinou/git-credentials"
check "uv sync figé" log_has "sync -q --frozen --no-dev"
check "ha-mcp épinglé" log_has "tool install -q --python 3.13 ha-mcp==9.2.0"
check "modèles téléchargés" log_has "-m lapinou.voice.models fetch"
check "Piper installé" log_has "-m lapinou.voice.models piper-setup"
check "réglages écrits par lapinou.config (réponses sur stdin)" py_stdin_with "MQTT_PASS=$(printf '%s' "$MQTT_PASS_IN" | base64 -w0)"
check "mot de passe admin par python -m lapinou.web.auth" log_has "-m lapinou.web.auth --config $R/etc/lapinou/config.toml"
check "mot de passe admin sur stdin" py_stdin_with "$ADMIN_IN"
check "secrets.toml en 0600" mode_is "$R/etc/lapinou/secrets.toml" 600
check "aucun secret dans les arguments des commandes" no_secret_in "$T/log"
check "aucun secret affiché" no_secret_in "$T/out"
check "fichier de réponses effacé" missing "$T/setup/answers"
check "post-update.sh installé en root" same_file "$R/usr/local/lib/lapinou/post-update.sh" "$INSTALLER/post-update.sh"
check "copie root de post-update.sh exécutable" mode_is "$R/usr/local/lib/lapinou/post-update.sh" 755
check "lapinou.service installé" same_file "$R/etc/systemd/system/lapinou.service" "$REPO/server/systemd/lapinou.service"
check "lapinou-update.service lance la copie root" file_has "$R/etc/systemd/system/lapinou-update.service" "ExecStart=/usr/local/lib/lapinou/post-update.sh --root"
check "lapinou-update.path surveille la demande" file_has "$R/etc/systemd/system/lapinou-update.path" "PathChanged=/var/lib/lapinou/update/request"
check "règle polkit : lapinou.service" file_has "$R/etc/polkit-1/rules.d/50-lapinou.rules" 'action.lookup("unit") == "lapinou.service"'
check "règle polkit : restart, utilisateur lapinou" file_has "$R/etc/polkit-1/rules.d/50-lapinou.rules" 'subject.user == "lapinou"'
check "chemin .path activé" log_has "systemctl enable -q --now lapinou-update.path"
check "service activé" log_has "systemctl enable -q lapinou"
check "service démarré" log_has "systemctl restart lapinou"
check "pas de surcharge sans besoin" missing "$R/etc/systemd/system/lapinou.service.d/lxc.conf"
check "santé vérifiée" log_has "http://127.0.0.1:8080/health"
check "install.conf : dépôt et branche" file_has "$R/usr/local/lib/lapinou/install.conf" "REPO=Kinder0083/LapiNou"
check "install.conf : branche" file_has "$R/usr/local/lib/lapinou/install.conf" "BRANCH=main"
check "install.conf en 0644, à root" mode_is "$R/usr/local/lib/lapinou/install.conf" 644
check "install.conf passé à root" log_has "chown root:root $R/usr/local/lib/lapinou/install.conf.new"

new_case "installation : dossier en lien symbolique"
setup_root
mkdir -p "$R/etc" "$T/outside"
chmod 0700 "$T/outside"
ln -s "$T/outside" "$R/etc/lapinou"
run_provision --install "$T/setup/answers"
check "refusée" rc_not0
check "message clair" out_has "est un lien symbolique : refusé"
check "rien changé hors du lien" mode_is "$T/outside" 700
check "pas de chown" log_lacks "chown"

new_case "226/NAMESPACE : surcharge palier 1"
setup_root
export STUB_RESTART_FAILS=1 STUB_EXEC_STATUS=226
run_provision --install "$T/setup/answers"
check "se termine sans erreur" rc_is 0
check "ProtectHostname=no" file_has "$R/etc/systemd/system/lapinou.service.d/lxc.conf" "ProtectHostname=no"
check "ProtectClock=no" file_has "$R/etc/systemd/system/lapinou.service.d/lxc.conf" "ProtectClock=no"
check "palier 1 seulement" bash -c '! grep -q PrivateDevices "$1"' _ "$R/etc/systemd/system/lapinou.service.d/lxc.conf"
check "avertissement" out_has "226/NAMESPACE"

new_case "226/NAMESPACE : surcharge palier 2"
setup_root
export STUB_RESTART_FAILS=2 STUB_EXEC_STATUS=226
run_provision --install "$T/setup/answers"
check "se termine sans erreur" rc_is 0
check "PrivateDevices=no" file_has "$R/etc/systemd/system/lapinou.service.d/lxc.conf" "PrivateDevices=no"

new_case "autre échec de démarrage"
setup_root
export STUB_RESTART_FAILS=1 STUB_EXEC_STATUS=1
run_provision --install "$T/setup/answers"
check "échoue" rc_not0
check "pas de surcharge" missing "$R/etc/systemd/system/lapinou.service.d/lxc.conf"
check "diagnostic indiqué" out_has "journalctl -u lapinou"
check "fichier de réponses effacé quand même" missing "$T/setup/answers"

new_case "serveur muet"
setup_root
export STUB_HEALTH_RC=7
run_provision --install "$T/setup/answers"
check "échoue" rc_not0
check "message clair" out_has "Le serveur ne répond pas sur http://127.0.0.1:8080/health"

# Réglages écrits par le vrai lapinou.config (venv du serveur : cd server && uv sync).
REAL_PY="${LAPINOU_TEST_PYTHON:-$REPO/server/.venv/bin/python}"
if [ -x "$REAL_PY" ]; then
    new_case "réglages réels (lapinou.config, lapinou.web.auth)"
    setup_root
    REAL_PY="$REAL_PY" run_provision --install "$T/setup/answers"
    check "se termine sans erreur" rc_is 0
    check "relus par load_settings, secrets compris" "$REAL_PY" - "$R/etc/lapinou/config.toml" \
        "$MQTT_PASS_IN" "$HA_TOKEN_IN" "$ANTH_KEY_IN" "$OAI_KEY_IN" "$GH_TOKEN_IN" "$ADMIN_IN" <<'PY'
import sys
from pathlib import Path
from argon2 import PasswordHasher
from lapinou.config import load_settings
path, mqtt, ha, anth, oai, gh, admin = sys.argv[1:]
s = load_settings(Path(path))
assert s.mqtt.host == "192.168.1.5" and s.mqtt.port == 1883 and s.mqtt.username == "mqttuser"
assert s.mqtt.password == mqtt, s.mqtt.password
assert s.ha.url == "http://192.168.1.20:8123" and s.ha.token == ha
assert s.lms.host == "192.168.1.20" and s.lms.port == 9000
assert s.brain.providers["anthropic"].api_key == anth
assert s.brain.providers["mistral"].kind == "openai" and s.brain.providers["mistral"].api_key == oai
assert s.brain.fallback_chain == ["mistral:mistral-small-latest", "homeassistant"]
assert s.brain.primary == "claude-opus-5-5"
assert s.budget.monthly_usd == 12.5
assert s.ota.repo == "Kinder0083/LapiNou" and s.ota.token == gh
assert s.server.public_url == "http://192.168.1.160:8080" and s.server.timezone == "Europe/Paris"
assert PasswordHasher().verify(s.admin.password_hash, admin)
PY
    check "aucun secret dans config.toml" no_secret_in "$R/etc/lapinou/config.toml"
    check "secrets.toml en 0600" mode_is "$R/etc/lapinou/secrets.toml" 600
    check "fichier de réponses effacé" missing "$T/setup/answers"
else
    echo "  (réglages réels ignorés : pas de $REAL_PY ; cd server && uv sync)"
fi

new_case "mise à jour (--update)"
setup_root
mkdir -p "$R/opt/lapinou/.git" "$R/etc/lapinou" "$R/var/lib/lapinou/update"
export STUB_USER_EXISTS=1
run_provision --update Kinder0083/LapiNou main
check "se termine sans erreur" rc_is 0
check "remote remis sur le dépôt noté" log_has "remote set-url origin https://github.com/Kinder0083/LapiNou.git"
check "git fetch de la branche (notée origin/main)" log_has "fetch -q origin +refs/heads/main:refs/remotes/origin/main"
check "avance rapide seulement" log_has "merge -q --ff-only origin/main"
check "branche suivie" log_has "branch -q --set-upstream-to=origin/main"
check "dossiers existants non retouchés en root" log_lacks "chown -h lapinou"
check "install.conf réécrit" file_has "$R/usr/local/lib/lapinou/install.conf" "REPO=Kinder0083/LapiNou"
check "pas de clone" log_lacks " clone "
check "réglages non réécrits" bash -c '! ls "$1"/py-stdin.* >/dev/null 2>&1 || ! grep -l MQTT_PASS "$1"/py-stdin.*' _ "$T"
check "pas de nouvel utilisateur" log_lacks "useradd"
check "unités réinstallées" file_has "$R/etc/systemd/system/lapinou-update.service" "post-update.sh --root"
check "service redémarré" log_has "systemctl restart lapinou"

new_case "mise à jour : autre branche"
setup_root
mkdir -p "$R/opt/lapinou/.git" "$R/etc/lapinou" "$R/var/lib/lapinou/update"
export STUB_USER_EXISTS=1 STUB_GIT_BRANCH=main
run_provision --update autre/depot dev
check "se termine sans erreur" rc_is 0
check "remote sur le dépôt tapé" log_has "remote set-url origin https://github.com/autre/depot.git"
check "passe à la branche, suivie" log_has "checkout -q -B dev --track origin/dev"
check "install.conf mis à jour" file_has "$R/usr/local/lib/lapinou/install.conf" "BRANCH=dev"

new_case "mise à jour : autre branche, vrai git, puis « Mettre à jour »"
# Un vrai dépôt (bare, local) à la place de github.com (url.insteadOf) : après le passage de main à
# dev par provision.sh, le bouton « Mettre à jour » (git pull --ff-only, updater.py) doit marcher.
setup_root
mkdir -p "$R/etc/lapinou" "$R/var/lib/lapinou/update"
REAL_GIT=$(PATH=$(printf '%s' "$PATH" | sed "s#$STUBS:##g") command -v git)
G="$T/git"
mkdir -p "$G/github/autre"
"$REAL_GIT" init -q --bare -b main "$G/github/autre/depot.git"
"$REAL_GIT" clone -q "$G/github/autre/depot.git" "$G/work" 2>/dev/null
"$REAL_GIT" -C "$G/work" -c user.name=t -c user.email=t@t commit -q --allow-empty -m un
"$REAL_GIT" -C "$G/work" push -q origin HEAD:main
"$REAL_GIT" -C "$G/work" -c user.name=t -c user.email=t@t commit -q --allow-empty -m deux
"$REAL_GIT" -C "$G/work" push -q origin HEAD:dev
cp "$R/opt/lapinou/server/.venv/bin/python" "$T/python-stub"
rm -rf "$R/opt/lapinou"
"$REAL_GIT" clone -q --branch main "$G/github/autre/depot.git" "$R/opt/lapinou"
mkdir -p "$R/opt/lapinou/server/.venv/bin" "$R/opt/lapinou/server/systemd"
cp "$REPO/server/systemd/lapinou.service" "$R/opt/lapinou/server/systemd/"
cp -p "$T/python-stub" "$R/opt/lapinou/server/.venv/bin/python"
export STUB_USER_EXISTS=1 STUB_GIT_REAL="$REAL_GIT" GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0="url.file://$G/github/.insteadOf" GIT_CONFIG_VALUE_0="https://github.com/"
run_provision --update autre/depot dev
check "se termine sans erreur" rc_is 0
check "sur la branche dev" bash -c '[ "$("$1" -C "$2" rev-parse --abbrev-ref HEAD)" = dev ]' _ "$REAL_GIT" "$R/opt/lapinou"
check "dev suit origin/dev" bash -c '[ "$("$1" -C "$2" rev-parse --abbrev-ref "@{u}")" = origin/dev ]' _ "$REAL_GIT" "$R/opt/lapinou"
"$REAL_GIT" -C "$G/work" -c user.name=t -c user.email=t@t commit -q --allow-empty -m trois
"$REAL_GIT" -C "$G/work" push -q origin HEAD:dev
check "« Mettre à jour » : git pull --ff-only marche" "$REAL_GIT" -C "$R/opt/lapinou" pull -q --ff-only
check "et prend le nouveau commit" bash -c '[ "$("$1" -C "$2" log -1 --format=%s)" = trois ]' _ "$REAL_GIT" "$R/opt/lapinou"
unset STUB_GIT_REAL GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0

new_case "mise à jour : update/ en lien symbolique"
setup_root
mkdir -p "$R/opt/lapinou/.git" "$R/etc/lapinou" "$R/var/lib/lapinou" "$T/outside"
echo "garde" > "$T/outside/fichier"
chmod 0700 "$T/outside"
ln -s "$T/outside" "$R/var/lib/lapinou/update"
export STUB_USER_EXISTS=1
run_provision --update Kinder0083/LapiNou main
check "refusée" rc_not0
check "message clair" out_has "update n'est pas un dossier (lien symbolique ?) : mise à jour refusée"
check "rien changé hors du lien (droits)" mode_is "$T/outside" 700
check "rien changé hors du lien (contenu)" file_has "$T/outside/fichier" "garde"
check "aucune commande root sur le lien" log_lacks "$T/outside"
check "pas de chown" log_lacks "chown"
check "rien installé" missing "$R/etc/systemd/system/lapinou-update.service"

new_case "mise à jour : /etc/lapinou en lien symbolique"
setup_root
mkdir -p "$R/opt/lapinou/.git" "$R/etc" "$R/var/lib/lapinou/update" "$T/outside"
ln -s "$T/outside" "$R/etc/lapinou"
export STUB_USER_EXISTS=1
run_provision --update Kinder0083/LapiNou main
check "refusée" rc_not0
check "pas de chown" log_lacks "chown"

new_case "mise à jour : dépôt invalide"
setup_root
run_provision --update "../x" main
check "refusée" rc_not0

new_case "dépendances seules (--post-update)"
setup_root
mkdir -p "$R/opt/lapinou/.git" "$R/etc/systemd/system"
printf '# ancienne unité\n' > "$R/etc/systemd/system/lapinou.service"
run_provision --post-update
check "se termine sans erreur" rc_is 0
check "uv sync" log_has "sync -q --frozen --no-dev"
check "pas de git pull (fait par la page)" log_lacks "pull"
check "pas de redémarrage (fait par la page)" log_lacks "systemctl restart lapinou"
check "unité du dépôt non installée en root" file_has "$R/etc/systemd/system/lapinou.service" "ancienne unité"
check "signale l'unité modifiée" out_has "lapinou.service"
check "signale les scripts modifiés" out_has "provision.sh post-update.sh : changé dans le dépôt"

new_case "dépendances seules, rien de changé"
setup_root
mkdir -p "$R/opt/lapinou/.git" "$R/opt/lapinou/installer" "$R/etc/systemd/system" "$R/usr/local/lib/lapinou"
cp "$INSTALLER/provision.sh" "$INSTALLER/post-update.sh" "$R/opt/lapinou/installer/"
cp "$INSTALLER/provision.sh" "$INSTALLER/post-update.sh" "$R/usr/local/lib/lapinou/"
cp "$REPO/server/systemd/lapinou.service" "$R/etc/systemd/system/"
run_provision --post-update
check "se termine sans erreur" rc_is 0
check "aucun avertissement" bash -c '! grep -q "relancez" "$1"' _ "$T/out"

###############################################################################
echo "== post-update.sh (bouton « Mettre à jour »)"
###############################################################################

setup_update() {
    R="$T/root"
    mkdir -p "$R/var/lib/lapinou/update" "$R/usr/local/lib/lapinou"
    cat > "$R/usr/local/lib/lapinou/provision.sh" <<'EOF'
echo "dépendances : $1"
exit "${FAKE_RC:-0}"
EOF
}

run_update_pair() {  # la page (lapinou) demande, le service root répond
    PATH="$STUBS:$PATH" LAPINOU_ROOT="$R" STUB_UID=1000 STUB_REAL_SLEEP=1 LAPINOU_UPDATE_WAIT=30 \
        bash "$INSTALLER/post-update.sh" > "$T/out" 2>&1 < /dev/null &
    local client=$!
    for _ in $(seq 1 100); do [ -s "$R/var/lib/lapinou/update/request" ] && break; /bin/sleep 0.1; done
    PATH="$STUBS:$PATH" LAPINOU_ROOT="$R" STUB_UID=0 \
        bash "$INSTALLER/post-update.sh" --root > "$T/root-out" 2>&1 < /dev/null
    wait "$client"
    RC=$?
}

new_case "demande servie"
setup_update
export FAKE_RC=0
run_update_pair
check "réussit" rc_is 0
check "journal du passage root rendu à la page" out_has "dépendances : --post-update"
check "statut public" file_has "$R/run/lapinou-update/status" " 0"

new_case "échec rendu à la page"
setup_update
export FAKE_RC=3
run_update_pair
check "code de retour transmis" rc_is 3

new_case "service root absent"
setup_update
PATH="$STUBS:$PATH" LAPINOU_ROOT="$R" STUB_UID=1000 LAPINOU_UPDATE_WAIT=2 \
    bash "$INSTALLER/post-update.sh" > "$T/out" 2>&1 < /dev/null
RC=$?
check "échoue" rc_is 1
check "message clair" out_has "pas de réponse de lapinou-update.service"

new_case "demande en lien symbolique ignorée"
setup_update
ln -s /etc/hostname "$R/var/lib/lapinou/update/request"
PATH="$STUBS:$PATH" LAPINOU_ROOT="$R" STUB_UID=0 bash "$INSTALLER/post-update.sh" --root > "$T/out" 2>&1 < /dev/null
RC=$?
check "passe quand même (à la main)" rc_is 0
check "statut « manual »" file_has "$R/run/lapinou-update/status" "manual 0"

###############################################################################
echo "== shellcheck"
###############################################################################

CASE="shellcheck"
SCRIPTS=("installer/lapinou-install.sh" "installer/provision.sh" "installer/post-update.sh" "installer/tests/run.sh")
for f in "$STUBS"/*; do SCRIPTS+=("installer/tests/stubs/$(basename "$f")"); done
if command -v shellcheck >/dev/null 2>&1; then
    check "sans avertissement" bash -c 'cd "$1" && shift && shellcheck -x "$@"' _ "$REPO" "${SCRIPTS[@]}"
else
    check "shellcheck installé" false
fi

stop_broker
echo ""
echo "$PASSED réussis, $FAILED en échec"
# Dossiers des cas gardés seulement en cas d'échec (chemin indiqué à chaque ÉCHEC).
if [ "$FAILED" = 0 ]; then rm -rf "${CASE_DIRS[@]}"; fi
[ "$FAILED" = 0 ]
