#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
###############################################################################
# LapiNou - VM de développement (compilation du firmware LapinLibre)
#
# À lancer en root sur l'hôte Proxmox. Crée une VM Ubuntu 24.04 (image cloud)
# avec cloud-init : 4 cœurs, 8 Go de RAM, 80 Go de disque, DHCP, utilisateur
# "lapinou" accessible en SSH par la clé publique que vous collez.
# La VM ne sert que pendant le développement ; vous pourrez la supprimer ensuite.
###############################################################################
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
msg() { echo -e "${BLUE}▶${NC} $1"; }
ok() { echo -e "${GREEN}✓${NC} $1"; }
err() { echo -e "${RED}✗${NC} $1"; exit 1; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

command -v qm >/dev/null 2>&1 || err "Ce script doit être lancé sur l'hôte Proxmox (commande qm introuvable)."
[ "$(id -u)" -eq 0 ] || err "Lancez-le en root."

IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
SUMS_URL="https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
IMG_DIR="/var/lib/vz/template/iso"
IMG="$IMG_DIR/noble-server-cloudimg-amd64.img"

# Stockage : local-lvm > local > premier disponible pour les images de disque
STORAGE=$(pvesm status -content images 2>/dev/null | awk 'NR>1 {print $1}' | grep -x local-lvm || true)
[ -n "$STORAGE" ] || STORAGE=$(pvesm status -content images 2>/dev/null | awk 'NR>1 {print $1}' | head -1)
[ -n "$STORAGE" ] || err "Aucun stockage pour les disques de VM."

# Pont réseau
BRIDGES=$(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | grep '^vmbr' || true)
BRIDGE=$(echo "$BRIDGES" | head -1)
[ -n "$BRIDGE" ] || err "Aucun pont vmbr trouvé."

VMID=$(pvesh get /cluster/nextid)
read -r -p "ID de la VM [$VMID]: " IN < /dev/tty; VMID=${IN:-$VMID}
qm status "$VMID" >/dev/null 2>&1 && err "L'ID $VMID est déjà utilisé."
read -r -p "Stockage [$STORAGE]: " IN < /dev/tty; STORAGE=${IN:-$STORAGE}
read -r -p "Pont réseau [$BRIDGE] (disponibles : $(echo $BRIDGES)): " IN < /dev/tty; BRIDGE=${IN:-$BRIDGE}
read -r -p "Cœurs [4]: " CORES < /dev/tty; CORES=${CORES:-4}
read -r -p "RAM en Mo [8192]: " RAM < /dev/tty; RAM=${RAM:-8192}
read -r -p "Disque en Go [80]: " DISK < /dev/tty; DISK=${DISK:-80}
echo
echo "Collez la clé SSH publique qui aura accès à la VM (une ligne, commence par ssh-ed25519 ou ssh-rsa) :"
read -r PUBKEY < /dev/tty
case "$PUBKEY" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*) ;; *) err "Ce n'est pas une clé SSH publique." ;; esac
KEYFILE=$(mktemp); echo "$PUBKEY" > "$KEYFILE"
FP=$(ssh-keygen -lf "$KEYFILE" 2>/dev/null | awk '{print $2}') || true
[ -n "$FP" ] || { rm -f "$KEYFILE"; err "Clé illisible : recopiez la ligne entière (ssh-ed25519 AAAA… commentaire)."; }
ok "Empreinte de la clé : $FP"
echo "   Vérifiez qu'elle est identique à celle affichée sur votre PC (ssh-keygen -lf <fichier .pub>)."
echo
echo "Mot de passe de secours pour la console Proxmox de la VM (utilisateur lapinou) :"
read -r -s -p "Mot de passe : " CIPASS < /dev/tty; echo
[ ${#CIPASS} -ge 8 ] || { rm -f "$KEYFILE"; err "8 caractères minimum."; }

echo
msg "Récapitulatif : VM $VMID « lapinou-dev », $CORES cœurs, $RAM Mo, $DISK Go sur $STORAGE, réseau $BRIDGE (DHCP)"
read -r -p "Confirmer ? (y/n): " CONFIRM < /dev/tty
[ "$CONFIRM" = "y" ] || err "Annulé."

mkdir -p "$IMG_DIR"
if [ ! -f "$IMG" ]; then
  msg "Téléchargement de l'image Ubuntu 24.04…"
  wget -q --show-progress -O "$IMG.part" "$IMG_URL" || err "Téléchargement impossible."
  mv "$IMG.part" "$IMG"
fi
msg "Vérification de l'empreinte de l'image…"
EXPECTED=$(wget -qO- "$SUMS_URL" | awk '/noble-server-cloudimg-amd64.img/ {print $1}')
ACTUAL=$(sha256sum "$IMG" | awk '{print $1}')
if [ "$EXPECTED" != "$ACTUAL" ]; then
  rm -f "$IMG"; err "Empreinte incorrecte (image supprimée, relancez le script)."
fi
ok "Image vérifiée"

msg "Création de la VM…"
qm create "$VMID" --name lapinou-dev --cores "$CORES" --memory "$RAM" --cpu host \
  --net0 "virtio,bridge=$BRIDGE" --scsihw virtio-scsi-single --agent enabled=1 \
  --ostype l26 --serial0 socket --vga serial0 --onboot 0
qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$IMG,discard=on,ssd=1" >/dev/null
qm resize "$VMID" scsi0 "${DISK}G" >/dev/null
qm set "$VMID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0 \
  --ciuser lapinou --cipassword "$CIPASS" --sshkeys "$KEYFILE" --ipconfig0 ip=dhcp --ciupgrade 1 >/dev/null
unset CIPASS
rm -f "$KEYFILE"
ok "VM $VMID créée"

msg "Démarrage…"
qm start "$VMID"
msg "Attente de l'adresse IP (agent invité, jusqu'à 5 min)…"
IP=""
for _ in $(seq 1 60); do
  IP=$(qm guest cmd "$VMID" network-get-interfaces 2>/dev/null \
       | grep -oE '"ip-address" *: *"[0-9]+(\.[0-9]+){3}"' | grep -oE '[0-9]+(\.[0-9]+){3}' \
       | grep -v '^127\.' | head -1 || true)
  [ -n "$IP" ] && break
  sleep 5
done
echo
if [ -n "$IP" ]; then
  ok "VM prête : ssh lapinou@$IP"
else
  warn "L'image Ubuntu n'a pas d'agent invité : l'adresse IP n'est pas visible ici."
  warn "Cherchez « lapinou-dev » dans votre box, ou connectez-vous à la console (qm terminal $VMID,"
  warn "utilisateur lapinou + mot de passe de secours) et tapez : ip -4 addr show"
fi
