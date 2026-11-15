<!-- SPDX-License-Identifier: MIT -->
# LapiNou — conception d'ensemble

*Date : 2026-11-15 (révisé le 2026-11-16 après relecture) · Statut : soumis à validation du propriétaire · Auteur : Claude, avec le propriétaire du lapin*

## 1. Intention

Redonner vie à un Karotz (Nabaztag v3) en en faisant un **assistant vocal domestique moderne, à soi**, dans l'esprit d'Alexa ou Google Home :
on l'appelle « **Lapinou** », il comprend, répond avec sa voix, ses oreilles et sa LED, pilote la maison via Home Assistant, et annonce ce qui s'y passe.

Le propriétaire veut aussi **partager** le résultat avec d'autres possesseurs de Karotz : tout doit s'installer et se régler facilement, visuellement, sans rien écrire en dur.

Pourquoi maintenant : le firmware actuel (OpenKarotz sur Free Rabbits OS, socle Violet 2012) est obsolète, ses services en ligne sont morts, et un firmware libre moderne (LapinLibre, actif en octobre 2026) vient de rendre la reconstruction réaliste.

## 2. Pour qui

- **Le foyer du propriétaire, enfants compris.** Lapinou parle à toute la famille : ton doux, réponses courtes, garde-fous (voir §9).
- **D'autres possesseurs de Karotz**, plus tard : bricoleurs avec ou sans serveur à la maison, avec ou sans Home Assistant.

## 3. Objectifs, non-objectifs, anti-objectifs

### Objectifs (première version, tout livré ensemble)
1. Le lapin tourne sous **un fork de LapinLibre** (noyau 2.6.32), installé par-dessus Free Rabbits OS, sans toucher au chargeur de démarrage ni aux zones de secours.
2. **MQTT + découverte automatique Home Assistant** : le lapin apparaît seul dans HA avec ses entités (§6.4), via le broker Mosquitto existant.
3. **« Lapinou » en écoute continue** (mot d'activation détecté côté serveur), **avec annulation d'écho dès la v1** (le lapin peut être interrompu pendant qu'il parle), plus le bouton de la tête comme raccourci.
4. **Cerveau interchangeable** : Claude par défaut, tout fournisseur compatible OpenAI, et repli automatique sur l'assistant local de Home Assistant.
5. **Pilotage de la maison** par le serveur MCP Home Assistant déjà en place, avec une liste d'outils autorisés.
6. Capacités : discussion, **recherche web**, **minuteurs et rappels**, **musique et radio**, **histoires et jeux**, **mémoire durable** (consultable et effaçable).
7. **Mode sans serveur** : bouton → services en ligne appelés par le lapin seul.
8. **Page de réglages visuelle sur le lapin** (Wi-Fi, MQTT, serveur, mode sans serveur, voix, volume, sécurité, mises à jour) et **page de réglages du serveur** (cerveau, voix, personnalité, outils, mémoire, journal, coûts).
9. **Mise à jour du firmware par un bouton** dans la page web (OTA), la clé USB restant le moyen de dépannage.
10. **Installation du serveur en un seul script** lancé sur l'hôte Proxmox, dans le style du script FSAO/GMAO du propriétaire (§7.1).

### Non-objectifs
- Pas de passage au noyau 6.1 (gardé pour plus tard, hors de ce projet).
- Pas de remplacement de la carte mère (Raspberry Pi) ni de modification matérielle.
- Pas d'application mobile ; le navigateur suffit.
- Pas de redistribution de fichiers propriétaires de Violet ni de Free Rabbits OS (licence non commerciale).
- Pas d'accès depuis internet (tout reste sur le réseau local ; pas de Tailscale dans cette version).

### Anti-objectifs (échec même si « ça marche »)
- La voix de la maison qui part dans le cloud : **seul du texte** quitte la maison en mode serveur ; jamais d'écoute continue envoyée sur internet.
- Un lapin qu'on ne peut plus récupérer : toute mise à jour doit pouvoir être annulée (zones de secours intactes, clé USB d'usine, sauvegarde complète).
- Un enfant qui ouvre la porte d'entrée ou désarme l'alarme à la voix.
- Une facture API qui dérape sans prévenir.
- Un lapin lent ou bavard : réponse qui commence à sortir plus de ~3 s après la fin de la phrase dans le cas courant.

## 4. Contraintes

### Matériel (relevé sur le lapin le 2026-11-15, `inventaire/INVENTAIRE.md`)
- Samsung **S3C2440A**, ARM920T **ARMv4T**, 405 MHz, **sans unité flottante** ; **64 Mo** de RAM ; NAND 256 Mo (8 zones MTD, zones 3-5 = secours identiques à la clé d'usine, zone 7 vide).
- Codec **UDA134X** (micro faible, ~ -35 dBFS), Wi-Fi **RT2573** (802.11b/g, WPA2), RFID **NXP RC523** (I2C), LED via SPI, oreilles PWM + capteurs, caméra S3C2440.
- Port série 3,3 V « UART » sur la carte, menu de récupération du chargeur de démarrage.
- Sauvegarde complète vérifiée : `sauvegarde/` (procédure de restauration dans `sauvegarde/LISEZMOI.md`).

### Socle logiciel : LapinLibre (`sources/LapinLibre`, commit e269168)
- Buildroot 2026.02.3, GCC 14.3, uClibc-ng 1.0.57, noyau 2.6.32.71 avec code de carte RABBITV3.
- Démons C/C++ sur D-Bus système (`com.mindscape.karotz.<Nom>`), contrôleur C++, karotzVM (V8), BusyBox httpd + CGI en shell, `immortaldog`.
- `voice-daemon` : TTS par HTTP POST → MP3 → madplay ; ASR par TCP (port 8443), micro `/dev/audio` mu-law 16 kHz dont **un octet sur deux est envoyé (8 kHz)**, une seule réponse par session, déclenché par appui long.
- Limites d'image : noyau ≤ 3 Mio, rootfs ≤ 5 Mio ; `/usr` YAFFS 234 Mio. Chaque nouvelle bibliothèque doit être épinglée (SHA-256) et listée dans `system/core-files.txt`.
- Codecs disponibles : Speex/SpeexDSP (AEC, débruitage), FLAC, Vorbis ; **pas d'Opus**.
- Compilation : Ubuntu 24.04 ou WSL2 uniquement ; CI GitHub existante (`release` ~40 min).
- Licences : code LapinLibre MIT, noyau GPL-2.0, `system/external` GPL-2.0+ ; en-tête SPDX obligatoire sur chaque fichier (vérifié par `scripts/check-repo.py`).
- Jamais testé par ses auteurs sur un lapin venant d'un autre firmware : **notre lapin sert de banc d'essai** (sauvegarde faite).

### Environnement du propriétaire
- **Proxmox** sur un mini-PC à processeur de bureau (Core i3/i5/i7 ou Ryzen) ; Home Assistant OS 18.4 / Core 2026.11.1 en VM (192.168.1.135) ; apps installées : ESPHome, Terminal & SSH, File editor, **Lyrion Music Server**, Duck DNS.
- **Musique de la maison : Lyrion (LMS) + plusieurs Squeezebox dans différentes pièces.**
- **Mosquitto déjà en place dans un LXC** (adresse à saisir dans les réglages).
- Serveur MCP Home Assistant (ha-mcp) déjà utilisé par Claude.
- Lapin actuellement en 192.168.1.103 (IP fixe sous FROS ; LapinLibre gère DHCP ou IP fixe).
- Abonnement Claude Max avec crédits API mensuels (100 $) → plafond de dépense à prévoir.

### Dépôt
- **Privé au départ**, sur le compte GitHub `Kinder0083`, nom **LapiNou**, fork de LapinLibre, licence MIT pour nos ajouts. Conséquence : les téléchargements de versions (firmware, serveur) demandent un **jeton GitHub**, comme le script FSAO (§7.1, §6.6).

## 5. Approches envisagées

### Firmware
| Approche | Résumé | Verdict |
|---|---|---|
| A. Système neuf sur noyau Pragmatec 2.6.28 | réécrire tout l'espace utilisateur nous-mêmes | écartée : des semaines de travail que LapinLibre a déjà faites |
| B. Firmware entièrement neuf, noyau 6.1 | réécrire les pilotes | écartée : long, risqué, 6.1 en fin de vie fin 2027 |
| C. Par étapes, couche matérielle isolée | A puis B | remplacée par D |
| **D. LapinLibre comme socle (choisie)** | fork, on ajoute MQTT, écoute continue, réglages, mode sans serveur, OTA | retenue : chaîne de compilation, pilotes et API déjà faits, code libre |

### Où vit le cerveau
- **Serveur local + LLM dans le cloud (choisi pour le mode principal)** : le lapin (64 Mo, sans FPU) ne peut faire ni détection de mot d'activation, ni transcription, ni synthèse neuronale.
- **Envoyer l'audio directement à Claude** : impossible, l'API Claude n'accepte pas l'audio ; inutile de toute façon (transcription locale en 0,2-0,3 s).
- **Mode sans serveur (ajouté dès la v1)** : bouton → transcription, LLM et voix en ligne appelés par le lapin.

### Installation du serveur
- **LXC léger + firmware compilé par GitHub (choisi)**, contre une VM Ubuntu tout-en-un qui compilerait aussi le firmware (plus lourde).

### Orchestration de la conversation
- Piste A (le pipeline Assist de HA fait tout) contre **piste B (notre propre assistant, choisie)** : B permet personnalité, mémoire, gestes synchronisés, outils au-delà de HA, et un cerveau interchangeable. Le pipeline HA reste le **repli** quand aucun LLM n'est disponible.

## 6. Ce qui est décidé

### 6.1 Vue d'ensemble

```
             réseau local                                     internet (texte seulement)
┌─────────────────────────┐   flux micro 16 kHz (TCP)   ┌──────────────────────────────┐   ┌─────────────┐
│ Karotz (fork LapinLibre)│ ─────────────────────────▶ │ Serveur LapiNou (LXC)        │──▶│ LLM : Claude│
│  démons d'origine       │ ◀── URL des réponses MP3 ─ │  mot d'activation « Lapinou »│   │ ou compatible│
│  + lapinou-mqtt         │                            │  VAD + Parakeet (local)      │   │ OpenAI      │
│  + lapinou-mic          │ ◀──── MQTT ──────┐         │  cerveau + outils + mémoire  │   └─────────────┘
│  + page de réglages     │                  │         │  synthèse vocale (local)     │
│  + OTA, mode sans serv. │              ┌───┴──────┐  │  page de réglages            │
└─────────────────────────┘              │ Mosquitto│◀▶│                              │
        ▲  (mode sans serveur :          │  (LXC)   │  └──────────┬───────────────────┘
        │   appels directs en ligne)     └───┬──────┘             │ MCP
        │                                    ▼                    ▼
        └──────────────────────────── Home Assistant (VM) ◀── ha-mcp
```

### 6.2 Dépôt et organisation
- Fork privé `Kinder0083/LapiNou` de `sweetlilmre/LapinLibre`, suivi de l'amont en branche `upstream`.
- Nos ajouts restent **séparables** pour pouvoir être proposés à l'amont : nouveaux démons dans `userland/src/usr/lapinou-*`, pages dans `image/core/usr/www/lapinou/`, serveur dans `server/`, installateur dans `installer/`, docs dans `docs/lapinou/`.
- Respect des règles du dépôt (SPDX, LF, `check-repo.py`, `core-files.txt`, tests enregistrés).

### 6.3 Firmware (côté lapin)

**Nouveaux démons** (C, `-std=gnu99`, sur D-Bus comme les autres, surveillés par `immortaldog`, démarrés dans `S40lapinlibre`) :

| Démon | Rôle |
|---|---|
| `lapinou-mqtt` | pont D-Bus ↔ MQTT (bibliothèque paho-mqtt-c ou client MQTT minimal, à épingler) ; publie la découverte HA, l'état, les événements ; exécute les commandes ; reconnexion automatique, LWT « hors ligne » |
| `lapinou-mic` | **seul propriétaire de `/dev/audio`** : capture continue 16 kHz, envoie au serveur, et redistribue l'audio aux autres usages (voice-daemon pour l'ASR historique, multimedia `record`, k2k) par une interface locale, pour supprimer les conflits d'ouverture du micro |
| `lapinou-cloud` | mode sans serveur : enregistre la phrase après appui, appelle transcription → LLM → synthèse en ligne, joue la réponse |
| `lapinou-ota` | télécharge, vérifie (SHA-256 + signature) et installe une nouvelle version ; jamais d'écriture en zones 0 et 3-5 |

**Modifications de l'existant** (minimales, chacune proposable à l'amont) : `voice-daemon` et `multimedia-daemon` lisent l'audio depuis `lapinou-mic` quand il est présent ; le contrôleur laisse l'appui long déclencher « Lapinou » (serveur ou mode sans serveur) quand c'est configuré.

**Audio** : flux continu **16 kHz mu-law** (≈ 128 kbit/s) sans la décimation actuelle ; gain logiciel réglable.
**Annulation d'écho dès la v1** : `lapinou-mic` passe la capture dans SpeexDSP (AEC + débruitage, en virgule fixe) avec, comme référence, le son envoyé au haut-parleur. Cela impose que **toute la lecture audio passe aussi par `lapinou-mic`** (ou qu'il en reçoive une copie synchronisée) : `karotz-player`/`madplay` écrivent vers lui plutôt que directement vers `/dev/dsp`. Première tâche technique : **mesurer** sur le vrai lapin que l'UDA134X capture pendant la lecture (duplex), et la charge CPU de l'AEC à 16 kHz ; si le CPU ne suit pas, l'AEC peut être faite **côté serveur** (le lapin envoie aussi la référence de lecture, le serveur annule l'écho). Le flux porte un drapeau « lecture en cours ».

**Réglages persistants** : fichiers INI dans `/usr/etc/conf/lapinou.conf` (+ fichiers existants de LapinLibre), conservés par les mises à jour ; `applysettings.sh` accepte les nouvelles clés dans `lapinlibre.txt` (clé USB).

**Page de réglages du lapin** (`http://<lapin>/lapinou/`, mot de passe admin obligatoire dès que des secrets y sont stockés) :
- État (version, Wi-Fi, MQTT, serveur, mémoire libre, test LED/oreilles/voix)
- Réseau (Wi-Fi, DHCP/IP fixe, nom d'hôte)
- MQTT (adresse, port, identifiant, mot de passe, préfixe de topics, préfixe de découverte HA `homeassistant`)
- Serveur LapiNou (adresse, ou découverte mDNS)
- Mode sans serveur (fournisseurs, clés, modèles)
- Voix et son (volume, gain micro, langue)
- Sécurité (mot de passe web, SSH par clé, telnet)
- Mises à jour (version installée / disponible, bouton « Mettre à jour », journal)

Page servie par BusyBox httpd, CGI en shell comme le reste de LapinLibre, JS sans dépendance externe ; secrets jamais renvoyés en clair à la page.

### 6.4 MQTT et Home Assistant
- Topics sous `lapinou/<id_lapin>/…` (`state`, `availability`, `event/…`, `cmd/…`) ; découverte sous `homeassistant/…` avec un appareil par lapin.
- Entités : LED (`light`, couleur + pulsation), oreilles (`select` positions/humeurs + `button` réinitialiser), bouton de la tête (`event` : simple, double, triple, long), badge RFID (`event` + `sensor` dernier badge), volume (`number`), parler (`text` ou service via `notify`), jouer un son/une radio (`select`/`text`), photo (`camera` ou `image`), veille (`switch`), état assistant (`sensor` : écoute / réfléchit / parle / hors ligne), mise à jour (`update`), Wi-Fi (`sensor` signal).
- Notifications vocales depuis HA : publier un texte → le lapin le dit (TTS du serveur, ou en ligne en mode sans serveur).

### 6.5 Serveur LapiNou
- **Python 3.12**, un seul service (`lapinou-server`) découpé en modules ; base **SQLite** ; s'appuie sur le code `tools/app-server` de LapinLibre (ASR sherpa-onnx, TTS) plutôt que de le refaire.
- **Passerelle lapin** : reçoit le flux micro de chaque lapin (plusieurs lapins par serveur), envoie les réponses (URL MP3 jouée par `Multimedia.play`) et les gestes (par MQTT).
- **Mot d'activation** : modèle openWakeWord **« Lapinou » entraîné en français** (synthèse Piper FR + enregistrements réels du lapin en négatifs/positifs), livré dans le dépôt ; seuil réglable ; bip/LED à la détection.
- **VAD + transcription** : Silero VAD + Parakeet TDT 0.6B v3 int8 (FR), sur le CPU du LXC.
- **Cerveau** : interface unique, trois implémentations — Anthropic (Claude, modèle par défaut `claude-opus-5-5`, modèle rapide `claude-haiku-5-5` pour les demandes simples, choix réglable), compatible OpenAI (Mistral, OpenAI, Groq, OpenRouter, Ollama…), **repli Home Assistant** (API conversation/Assist de HA). Bascule automatique sur panne ou budget épuisé.
- **Outils du cerveau** : Home Assistant via **une instance ha-mcp lancée dans le LXC** (même outil que celui déjà utilisé avec Claude, configuré avec l'URL de HA et un jeton longue durée ; client MCP standard ; liste blanche d'outils, de domaines et d'entités appliquée **par le serveur LapiNou** avant chaque appel) ; recherche web (outil serveur du fournisseur quand il existe) ; minuteurs, alarmes et rappels (stockés en base, annoncés par le lapin) ; histoires et jeux (avec chorégraphie oreilles/LED) ; mémoire (ajouter, rappeler, oublier).
- **Musique et radio → Lyrion** : la musique et les radios partent vers les **Squeezebox de la maison** via LMS (API JSON-RPC de LMS, ou `media_player` HA en secours) ; la pièce est choisie à la voix (« dans la cuisine ») d'après les noms des lecteurs LMS, avec un **lecteur par défaut réglable par lapin** (la pièce où il se trouve) ; commandes : lancer, pause, volume, suivant, « qu'est-ce qui passe ? », grouper des pièces. Le haut-parleur du lapin ne sert qu'aux sons courts, à la voix, et à la radio si on le demande explicitement (« sur le lapin »). Le lapin **ne devient pas** un lecteur Squeezebox en v1.
- **Serveur de plateforme LapinLibre** : le serveur LapiNou implémente aussi le protocole « plateforme » de LapinLibre (port 9123, protobuf), **pour plusieurs lapins**, afin que le contrôleur garde toutes ses fonctions (fuseau horaire, rappels de bouton, appui long, veille programmée, apps karotzVM, badges → apps) et n'affiche pas l'état « déconnecté ». En mode sans serveur, le contrôleur est modifié pour fonctionner **sans plateforme** : fuseau horaire, veille et réglages lus dans la configuration locale du lapin.
- **Synthèse vocale** : moteur local de qualité en français (Kokoro si sa voix FR convient, sinon Piper FR), MP3 servi en HTTP au lapin.
- **Gestes** : chaque réponse peut porter des indications (oreilles, LED) jouées au début / pendant / à la fin de la parole.
- **Mémoire durable** : souvenirs du foyer en base, injectés dans le contexte ; visibles, modifiables et effaçables dans la page de réglages.
- **Page de réglages du serveur** : cerveau (fournisseurs, clés, modèles, repli), budget mensuel et consommation, voix, personnalité, mot d'activation et seuil, outils autorisés, mémoire, minuteurs actifs, lapins connectés, journal des conversations (rétention courte réglable), mises à jour.
- **Livraison** : installation native (venv + systemd) dans le LXC par le script ; un `Dockerfile` équivalent pour les utilisateurs hors Proxmox.

### 6.6 Compilation et distribution
- **GitHub Actions** dans le dépôt privé compile le firmware (workflow `release` de LapinLibre adapté) et publie à chaque étiquette : archive clé USB, paquet OTA (+ SHA-256 + signature), version du serveur.
- Le **serveur** télécharge les versions avec le jeton GitHub saisi à l'installation et les **sert au lapin sur le réseau local** (OTA depuis le serveur ; le lapin n'a pas besoin du jeton).
- En mode sans serveur, l'OTA passe par une adresse configurable (jeton à saisir sur le lapin tant que le dépôt est privé), ou par clé USB.
- Les paquets OTA sont **signés** (clé privée dans les secrets GitHub, clé publique dans le firmware).

### 6.7 Installation (première fois)
1. Sur l'hôte Proxmox : `bash -c "$(curl -fsSL …/installer/lapinou-install.sh)"` (jeton GitHub demandé tant que le dépôt est privé).
2. Le script crée le LXC, installe le serveur, télécharge les modèles et le firmware, puis affiche l'adresse de la page de réglages.
3. La page du serveur propose de **générer la clé USB** du lapin : archive à dézipper sur une clé FAT32, `lapinlibre.txt` déjà rempli (Wi-Fi, MQTT, adresse du serveur, clé SSH, mot de passe web).
4. Clé dans le lapin, démarrage, installation ; ensuite tout se règle et se met à jour depuis les pages web.

### 6.8 Installation sur le lapin du propriétaire
- La **clé USB est construite** (livrable pour les autres possesseurs) mais **n'est pas testée pour l'instant** sur ce lapin.
- Sur ce lapin, **Claude écrit directement le nouveau système par SSH** depuis Free Rabbits OS (compte `karotz`, root), décision du propriétaire du 2026-11-16 :
  - n'écrit que le **noyau (mtd1)**, le **système (mtd2)** et le contenu de **`/usr` (mtd6)** ; **ne touche jamais** au chargeur de démarrage (mtd0) ni aux zones de secours (mtd3-5) ;
  - vérifie chaque image (taille, SHA-256) avant écriture et relit après écriture (`nanddump`, comme l'autorun d'usine) ;
  - conserve ce qu'il faut de `/usr/etc/conf` (Wi-Fi) pour que le lapin revienne sur le réseau ; prévoit l'IP fixe actuelle 192.168.1.103 ou DHCP réservé ;
  - annonce l'opération avant de la lancer ; en cas d'échec : restauration depuis `sauvegarde/` par SSH si le lapin répond, sinon clé d'usine, sinon port série.
- Cette procédure devient un outil **« migration depuis FROS/OpenKarotz par SSH »** documenté pour les autres.

## 7. Installateur Proxmox (inspiré de `GMAO/gmao-iris-install.sh`)

### 7.1 Comportement attendu
Même esprit que le script GMAO Iris : un seul fichier bash, lancé en root sur l'hôte Proxmox, messages colorés (`msg/ok/warn/err`), questions avec valeurs par défaut lues sur `/dev/tty`, récapitulatif puis confirmation.
- Vérifie qu'on est sur Proxmox ; détecte automatiquement le modèle Debian (12, ou 13 si disponible), le stockage (local-lvm > local > premier), les ponts réseau, un ID de conteneur libre.
- Demande : jeton et dépôt GitHub (`Kinder0083/LapiNou`, branche `main`), ID, RAM (défaut 6144 Mo), cœurs (défaut 4), disque (défaut 20 Go), IP fixe ou DHCP, **broker MQTT (IP, port, identifiant, mot de passe)**, **URL Home Assistant + jeton longue durée** (sert à ha-mcp et au repli Assist), **adresse du serveur Lyrion** (détectée si possible), **clé API Anthropic** (et/ou autre fournisseur), budget mensuel, mot de passe admin de la page de réglages, adresse du lapin si connue.
- Teste au passage la connexion au broker, à HA et à l'API avant de continuer, avec un message clair en cas d'échec.
- Crée un LXC **non privilégié**, démarrage automatique, configure le DNS, vérifie internet, installe les paquets, clone le dépôt, crée le venv, télécharge les modèles (Parakeet, VAD, voix, mot d'activation) avec vérification SHA-256, écrit la configuration, installe les services systemd (redémarrage automatique, rotation des journaux).
- Installe un **post-update.sh** et le bouton « Mettre à jour » de la page du serveur, comme FSAO.
- Fin : affiche l'URL de la page de réglages et la marche à suivre pour la clé USB.
- Ré-exécutable sans casse (détecte un conteneur existant, propose mise à jour ou réinstallation).

## 8. Comportements clés

### 8.1 Conversation avec serveur
1. Le lapin envoie le micro en continu ; le serveur détecte « Lapinou » → bip court + LED « écoute » (MQTT).
2. VAD détecte la fin de phrase (0,7 s de silence) → Parakeet transcrit.
3. Le cerveau répond (outils au besoin) ; LED « réfléchit », oreilles qui bougent doucement.
4. La première phrase de la réponse est synthétisée et jouée dès qu'elle est prête (réponse découpée par phrases pour réduire l'attente).
5. Après la réponse, une courte fenêtre de suite (≈ 5 s) permet d'enchaîner sans redire « Lapinou ».
6. Appui sur la tête pendant que le lapin parle = l'interrompre.

### 8.2 Sans serveur
Appui long → enregistrement jusqu'au relâchement (≤ 15 s) → transcription en ligne → LLM → synthèse en ligne → lecture. Pas de mot d'activation, pas de mémoire durable ni de minuteurs persistants hors du lapin en v1 (minuteurs simples gérés par `scheduler-daemon`). Home Assistant joignable par son API conversation si configuré.

### 8.3 Mode dégradé
Pas d'internet → repli Home Assistant (commandes simples) ; pas de serveur → le lapin bascule en mode sans serveur s'il est configuré, sinon LED et message vocal pré-enregistré « je n'arrive pas à joindre mon serveur » ; MQTT absent → tout le reste fonctionne.

## 9. Sécurité et enfants
- **Personnalité** : Lapinou, gentil, joueur, phrases courtes, vocabulaire simple, ne fait pas peur, pas de contenu inadapté aux enfants ; refuse poliment et redirige.
- **Actions sensibles** : serrures, alarme, portes de garage, chauffage au-delà d'une plage, appareils marqués « sensible » → **désactivées par défaut** ; si activées, **confirmation vocale** et, au choix, code PIN parlé. Liste blanche de domaines/entités réglable dans la page du serveur.
- **Recherche web** : résultats filtrés, résumés adaptés aux enfants.
- **Budget** : plafond mensuel dans le serveur + rappel de régler le plafond de la console Anthropic ; à 80 % → bascule vers le modèle rapide ; à 100 % → repli Home Assistant ; consommation visible dans la page.
- **Vie privée** : l'audio ne quitte jamais la maison en mode serveur ; journal des conversations à durée courte réglable ; mémoire effaçable.
- **Réseau** : mot de passe obligatoire sur les pages de réglages dès qu'elles contiennent des secrets ; MQTT avec identifiants ; SSH par clé uniquement ; secrets stockés hors des fichiers servis.

## 10. Risques connus
| Risque | Parade |
|---|---|
| Installation LapinLibre jamais testée depuis FROS | sauvegarde complète faite ; zones de secours intactes ; clé d'usine ; port série en dernier recours |
| Partage de `/dev/audio` (OSS) entre plusieurs démons | `lapinou-mic` seul propriétaire du micro, redistribution aux autres |
| Duplex (capter pendant la lecture) non documenté sur l'UDA134X | mesure en toute première tâche ; si impossible, l'AEC n'a pas de sens et on revient à « détection coupée pendant la lecture » + interruption au bouton (à signaler au propriétaire) |
| Écho : le lapin s'entend parler | AEC SpeexDSP dès la v1, sur le lapin ou côté serveur selon la charge CPU mesurée |
| Écriture directe par SSH depuis FROS | images vérifiées avant/après ; zones 0 et 3-5 intactes ; sauvegarde complète ; clé d'usine ; port série |
| Micro faible, mot d'activation mal reconnu | gain logiciel, entraînement avec de vrais enregistrements du lapin, seuil réglable |
| CPU du lapin pour MQTT + flux + TLS | flux mu-law sans calcul ; TLS évité en local ; mesurer la charge |
| Place dans le rootfs (≤ 5 Mio) | nos ajouts dans `/usr` (YAFFS, 234 Mio) |
| OTA interrompue | n'écrire mtd1/mtd2 qu'après vérification complète ; zones de secours et clé USB toujours disponibles |
| Dépôt privé : jetons | le serveur relaie les versions au lapin ; jeton côté lapin seulement en mode sans serveur |

## 11. Critères d'acceptation
- Le lapin s'installe par clé USB depuis FROS, démarre sous LapiNou, et revient à l'état sauvegardé si besoin (procédure testée).
- Dans HA, l'appareil « Lapinou » apparaît sans configuration manuelle ; LED, oreilles, volume, parole, sons, photo fonctionnent ; bouton et badges déclenchent des automatisations.
- « Lapinou, allume la lumière du salon » fonctionne de bout en bout ; une demande de déverrouiller la porte est refusée par défaut.
- « Lapinou, quel temps fera-t-il demain ? », « rappelle-moi dans 10 minutes… », « raconte-moi une histoire » fonctionnent ; « mets France Inter » lance la radio sur la Squeezebox par défaut, et « … dans la cuisine » sur celle de la cuisine.
- On peut interrompre Lapinou à la voix pendant qu'il parle (annulation d'écho), ou au bouton.
- Le début de la réponse arrive en ≤ 3 s pour une commande simple, sur le réseau du propriétaire.
- Couper internet → les commandes simples passent encore par Home Assistant. Couper le serveur → mode sans serveur (si configuré).
- Le script d'installation, lancé sur un Proxmox vierge, produit un serveur fonctionnel sans autre manipulation que répondre aux questions.
- La mise à jour par bouton fonctionne, et une mise à jour volontairement corrompue est refusée sans abîmer le lapin.

## 12. Laissé au constructeur
- Choix précis des bibliothèques (client MQTT C, client MCP Python, moteur TTS final après écoute), du format exact des messages lapin ↔ serveur (s'appuyer sur le cadrage protobuf + longueur déjà utilisé par LapinLibre), et des schémas SQLite.
- Découpage interne des modules du serveur, gestion des processus, tests (harnais qemu de LapinLibre pour les démons, pytest pour le serveur, tests sur le lapin).
- Détails de la page de réglages (mise en page, textes), dans le style de la page web existante de LapinLibre.
- Procédure d'entraînement du modèle « Lapinou ».
- Valeurs par défaut des seuils (mot d'activation, VAD, gain) après mesures sur le lapin.

## 13. Choix faits par Claude (à vérifier par le propriétaire)
1. Page de réglages **sur le lapin** (en plus de celle du serveur), à cause du mode sans serveur.
2. Serveur installé **en natif dans le LXC** (venv + systemd, comme FSAO) ; Dockerfile pour les autres.
3. **Plusieurs lapins** gérés par un même serveur dès la v1.
4. Flux micro en **16 kHz mu-law** ; AEC SpeexDSP sur le lapin, ou côté serveur si le CPU du lapin ne suit pas.
5. Mode sans serveur : fournisseurs **compatibles OpenAI** pour la transcription et la voix, Claude ou compatible OpenAI pour le cerveau.
6. Contrôle et gestes par **MQTT** ; audio par flux TCP dédié ; réponses jouées par URL MP3.
7. Paquets OTA **signés**, servis au lapin par le serveur.
8. Interfaces en **français d'abord**, prêtes à être traduites.
9. Home Assistant piloté par **une instance ha-mcp dans le LXC** (pas par celle de ton PC), liste blanche appliquée par LapiNou.
10. Le serveur LapiNou **joue aussi le rôle de serveur de plateforme LapinLibre** (multi-lapins) ; en mode sans serveur, le contrôleur tourne sans plateforme.
11. Musique : **vers les Squeezebox via Lyrion par défaut**, pièce choisie à la voix, lecteur par défaut réglable par lapin ; le lapin ne devient pas un lecteur Squeezebox en v1.
12. Taille par défaut du LXC : 4 cœurs, 6 Go de RAM, 20 Go de disque (Kokoro + Parakeet sur processeur de bureau).
