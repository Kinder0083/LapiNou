<!-- SPDX-License-Identifier: MIT -->
# Démarrer avec LapiNou : le parcours du propriétaire

Les étapes, dans l'ordre, de rien jusqu'à un lapin qui répond et se met à jour tout seul. Chaque
étape renvoie au document qui la détaille. Dans les exemples, la première version publiée
s'appelle `v0.3.0` : les étiquettes `v0.1.0` à `v0.2.0` sont celles de LapinLibre, le projet
d'origine, et LapiNou numérote ses versions au-dessus pour éviter toute confusion.

## 1. La paire de clés de signature, puis la première étiquette

Les mises à jour sans fil (OTA) des lapins et du serveur sont signées (Ed25519). Une seule fois,
**sur votre machine et hors du dépôt**, créez la paire de clés, gardez la clé privée dans votre
gestionnaire de mots de passe et dans le secret GitHub `OTA_SIGNING_KEY`, puis commitez la clé
publique dans les deux fichiers `ota-pubkey.pub` du dépôt :
[release.md, « Une seule fois : créer la paire de clés de signature »](release.md#une-seule-fois--créer-la-paire-de-clés-de-signature).

Puis publiez la première version : étiquette `v0.3.0` poussée sur le commit testé, et le workflow
`release` construit, signe et publie la clé USB, les images OTA et l'archive du serveur
([release.md, « Chaque version »](release.md#chaque-version)).

**Important** : un lapin installé avec une version d'**avant** la vraie clé publique (fichier
provisoire `PLACEHOLDER`) refuse toute mise à jour OTA, faute de pouvoir vérifier une signature.
Il faut le **réinstaller une fois** (clé USB, étape 3) avec une version signée par la vraie clé ;
ensuite, l'OTA fonctionne.

## 2. Le serveur LapiNou

Sur un hôte **Proxmox**, un seul script crée le conteneur et installe tout (15 à 25 minutes) ; il
demande le mot de passe admin de la page de réglages, Home Assistant, MQTT, la clé Anthropic… :
[installation.md](installation.md). À la fin, la page est à `http://<IP du conteneur>:8080`.

Hors Proxmox, le serveur tourne en **Docker** : voir l'étape 5 et
[service.md, section Docker](service.md#docker--serverdockerfile).

## 3. Installer le lapin

Deux voies, au choix :

- **Clé USB** (un lapin d'usine, sous LapinLibre, ou à réinstaller) : page de réglages du serveur
  → **Clé USB** (Wi-Fi du lapin, puis téléchargement de l'archive), ou bien la clé de la Release
  `lapinou-key-v0.3.0.zip` avec son `lapinlibre.txt` rempli. Dézippez-la à la racine d'une clé
  USB vide en **FAT32**, lapin éteint, mettez la clé, allumez-le et attendez la fin
  ([install.md](../install.md), en anglais).
- **Migration** d'un lapin sous Free Rabbits OS, par SSH et sans clé USB, depuis un PC du même
  réseau : [migration.md](migration.md) (quelle clé donner, comment créer la clé SSH ; le Wi-Fi
  du lapin est gardé).

Dans les deux cas, l'identifiant du lapin est celui de `/usr/karotz/uuid.conf`, qui sert aussi de
login de la plateforme (R47) : le serveur voit le même lapin sur le micro et sur la plateforme.

## 4. Le premier mot de passe de la page du lapin

Ouvrez `http://<adresse du lapin>/` (ou `http://lapinlibre.local/`). Tant qu'aucun mot de passe
n'existe, la page propose d'en créer un, mais exige une **preuve de présence** (R46) : elle
demande d'**appuyer une fois sur la tête du lapin** dans les 60 secondes. Pendant l'attente, le
**nez du lapin clignote** dans une couleur tirée au hasard, que la page affiche aussi :
n'appuyez que si les deux couleurs sont les mêmes (c'est votre attente, pas celle d'un autre
navigateur). Détails : [page-web.md, « Mot de passe »](page-web.md#mot-de-passe).

## 5. Le jeton d'installation du serveur (Docker)

Avec l'installateur Proxmox, le mot de passe admin du serveur est déjà réglé (étape 2). En
**Docker**, le serveur démarre sans mot de passe et écrit un **code d'installation** à usage
unique dans `/var/lib/lapinou/setup-token` (dans le volume des données) :

```
docker exec lapinou cat /var/lib/lapinou/setup-token
```

Collez-le dans la page `http://<hôte>:8080` avec le mot de passe choisi ; le fichier est effacé
ensuite. Autre voie, sans la page : `docker exec -i lapinou python -m lapinou.web.auth --config
/etc/lapinou/config.toml` (mot de passe lu sur l'entrée standard), puis `docker restart lapinou`.
Voir [service.md](service.md#docker--serverdockerfile).

## 6. Les mises à jour (OTA)

- **Lapins** : sans clé USB. L'onglet « Mises à jour » de la page du lapin, ou sa vérification
  quotidienne, trouve la nouvelle version (sur GitHub, ou par le relais du serveur LapiNou),
  vérifie sa signature et l'installe : [ota-lapin.md](ota-lapin.md).
- **Serveur** : le bouton « Mettre à jour » de sa page de réglages (`git pull`, puis les
  dépendances), ou en relançant l'installateur ; le relais OTA du serveur sert les nouvelles
  versions aux lapins du réseau : [ota.md](ota.md).
- **Docker** : reconstruire l'image et relancer le conteneur (pas de bouton).

Voir aussi : [licences.md](licences.md) (licences des modèles de voix et de reconnaissance).
