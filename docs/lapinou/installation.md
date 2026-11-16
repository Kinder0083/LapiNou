<!-- SPDX-License-Identifier: MIT -->
# Installer le serveur LapiNou sur Proxmox

Un seul script, `installer/lapinou-install.sh`, à lancer **en root sur l'hôte Proxmox**
(console de l'hôte dans l'interface web, ou `ssh root@<proxmox>`). Il pose des questions,
teste vos services, puis crée et installe le conteneur. Comptez 15 à 25 minutes.

## La commande

### Dépôt public

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Kinder0083/LapiNou/main/installer/lapinou-install.sh)"
```

ou, avec wget :

```bash
bash -c "$(wget -qO- https://raw.githubusercontent.com/Kinder0083/LapiNou/main/installer/lapinou-install.sh)"
```

Laissez la question « Jeton GitHub » vide.

### Dépôt privé (tant que `Kinder0083/LapiNou` l'est)

Il faut un **jeton GitHub à grain fin** : <https://github.com/settings/personal-access-tokens>
→ *Generate new token* → *Only select repositories* : `Kinder0083/LapiNou` → *Repository
permissions* : **Contents : Read-only** (rien d'autre). Il sert à télécharger le script, à
cloner le dépôt dans le conteneur, au bouton « Mettre à jour » et aux mises à jour des lapins
(OTA).

Collez le jeton quand c'est demandé (rien ne s'affiche), puis lancez l'installation :

```bash
read -rsp "Jeton GitHub : " LAPINOU_GITHUB_TOKEN; echo; export LAPINOU_GITHUB_TOKEN
bash -c "$(printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw"\n' "$LAPINOU_GITHUB_TOKEN" | curl -fsSL -K - 'https://api.github.com/repos/Kinder0083/LapiNou/contents/installer/lapinou-install.sh?ref=main')"
unset LAPINOU_GITHUB_TOKEN
```

ou, avec wget (le jeton passe par un fichier 0600 effacé aussitôt) :

```bash
read -rsp "Jeton GitHub : " LAPINOU_GITHUB_TOKEN; echo; export LAPINOU_GITHUB_TOKEN
(umask 077; printf 'header = Authorization: Bearer %s\nheader = Accept: application/vnd.github.raw\n' "$LAPINOU_GITHUB_TOKEN" > /root/.lapinou-wgetrc)
bash -c "$(wget -qO- --config=/root/.lapinou-wgetrc 'https://api.github.com/repos/Kinder0083/LapiNou/contents/installer/lapinou-install.sh?ref=main')"; rm -f /root/.lapinou-wgetrc
unset LAPINOU_GITHUB_TOKEN
```

Le script reprend le jeton de `LAPINOU_GITHUB_TOKEN` : appuyez sur Entrée à la question
« Jeton GitHub ». Le jeton n'apparaît jamais dans la ligne de commande d'un processus (`ps`),
ni dans l'historique du shell.

## Ce qui est demandé

| Question | Défaut |
|---|---|
| Dépôt, branche, jeton GitHub | `Kinder0083/LapiNou`, `main` |
| ID du conteneur, nom d'hôte | prochain ID libre, `lapinou` |
| RAM, cœurs, disque | 6144 Mo, 4, 20 Go |
| Réseau | IP fixe (proposée : `.160` du réseau du pont) ou DHCP |
| Broker MQTT : adresse, port, identifiant, mot de passe | port 1883 |
| Home Assistant : URL et jeton longue durée (profil HA → Sécurité) | vide : sans HA |
| Lyrion : adresse, port | l'adresse de HA, port 9000 ; vide : sans Lyrion |
| Cerveau : Anthropic, compatible OpenAI, ou les deux | Anthropic |
| Budget mensuel, fuseau horaire | 20 $, celui de l'hôte |
| Mot de passe admin de la page de réglages (deux fois) | 8 caractères au moins |
| Adresse IP du lapin | vide : inconnue |

Détecté seul : version de Proxmox, modèle **Debian 13** (sinon 12, téléchargé si besoin),
stockage (`local-lvm`, sinon `local`, sinon le premier), pont réseau (choix s'il y en a
plusieurs).

**Avant de créer quoi que ce soit**, le script teste le dépôt GitHub, le broker MQTT
(connexion avec vos identifiants), l'API de Home Assistant, Lyrion et la clé du fournisseur.
Au moindre échec il s'arrête avec un message clair : corrigez et relancez. Puis il affiche un
récapitulatif (les secrets n'y figurent pas) et demande confirmation.

## Ce que fait le script

Sur l'hôte : conteneur **non privilégié**, démarrage automatique, `nesting=1`, étiquette
`lapinou` ; vérification d'internet (et du DNS) dans le conteneur. Vos réponses partent dans le
conteneur dans un fichier 0600, effacé à la fin, réussite ou échec.

Dans le conteneur (`installer/provision.sh`) :

- paquets : git, ffmpeg, protoc, libspeexdsp-dev, python3-dev, gcc, polkit ;
- uv (version épinglée), utilisateur système `lapinou` ;
- `/opt/lapinou` : clone du dépôt ; pour un dépôt privé, le jeton est dans
  `/etc/lapinou/git-credentials` (0600, à `lapinou`), jamais dans l'adresse du dépôt ;
- venv du serveur (`uv sync --frozen`), `ha-mcp` (épinglé, dans `~/.local/bin` du service),
  modèles Parakeet, Silero VAD et voix Piper (SHA-256 vérifiés), Piper ;
- `/etc/lapinou/config.toml` et `secrets.toml` (0600) écrits par le serveur lui-même ; le mot
  de passe admin est haché (argon2id) par `python -m lapinou.web.auth` ;
- services : `lapinou.service` (voir `service.md`), `lapinou-update.path` et
  `lapinou-update.service` (bouton « Mettre à jour »), règle polkit
  `/etc/polkit-1/rules.d/50-lapinou.rules` (bouton « Redémarrer »), journaux plafonnés à
  200 Mo ;
- démarrage du service, puis vérification de `http://127.0.0.1:8080/health`.

Si le conteneur refuse une option de durcissement de `lapinou.service`
(`status=226/NAMESPACE`), le script ajoute
`/etc/systemd/system/lapinou.service.d/lxc.conf`, qui la désactive (d'abord
`ProtectHostname` et `ProtectClock`, puis si besoin d'autres options du noyau), et le dit.

## À la fin

Le script affiche l'adresse de la page de réglages, `http://<IP du conteneur>:8080`
(connexion avec le mot de passe admin choisi), et la marche à suivre pour le premier lapin :

1. page de réglages → **Clé USB** : Wi-Fi du lapin, puis téléchargez l'archive ;
2. dézippez-la sur une clé USB vide formatée en **FAT32** ;
3. lapin éteint, mettez la clé, allumez-le, attendez la fin de l'installation.

## Relancer le script

Le script reconnaît un conteneur LapiNou existant (étiquette ou nom `lapinou`) et propose :

1. **Mettre à jour** (défaut) : `git pull`, dépendances, modèles, services ; réglages et
   données gardés ; le jeton est repris du conteneur ;
2. **Réinstaller** : efface le conteneur et ses données (il faut retaper son ID), puis
   installation complète ;
3. installer un autre conteneur à côté ;
4. quitter.

## Mises à jour depuis la page

Le bouton « Mettre à jour » fait `git pull`, puis `installer/post-update.sh` : celui-ci
demande à `lapinou-update.service` (root) de mettre à jour les paquets, le venv, `ha-mcp` et
les modèles, et le service redémarre. Ce passage root n'exécute que les copies installées par
le script (`/usr/local/lib/lapinou/`), jamais un fichier du dépôt que le service peut
modifier ; une modification de `lapinou.service` ou des scripts d'installation s'applique donc
en relançant le script sur Proxmox (choix 1). Le journal du dernier passage :
`/run/lapinou-update/log`, ou `journalctl -u lapinou-update`.

## En cas de problème

```bash
pct exec <ID> -- journalctl -u lapinou -n 100     # journal du serveur
pct exec <ID> -- systemctl status lapinou
pct enter <ID>                                    # shell dans le conteneur
```

Hors Proxmox, le serveur tourne aussi en Docker : voir `service.md` (section Docker) ; il n'y
a pas de Docker dans le conteneur créé ici.

## Tests de l'installateur

`bash installer/tests/run.sh` (Linux, `python3` et `shellcheck`) : les commandes Proxmox et
système sont simulées et journalisées ; un faux broker MQTT répond. Les tests vérifient entre
autres qu'aucun secret n'apparaît dans les arguments d'une commande. Avec le venv du serveur
(`cd server && uv sync`), les réglages sont aussi écrits et relus par le vrai `lapinou.config`.
L'installation réelle n'est vérifiée que sur un Proxmox.
