<!-- SPDX-License-Identifier: MIT -->
# Mises à jour OTA servies par le serveur

Le serveur LapiNou relaie aux lapins la dernière version publiée sur GitHub (R38), sur son port
HTTP **8080**, sans jeton ni mot de passe (le réseau local seulement).

## Une version sur GitHub (R41, R42)

Assets de la Release `vX.Y.Z` :

- `manifest.json` : `{"version": "vX.Y.Z", "files": {"<nom>": {"sha256": "<hex>", "size": N}, …}}`.
  `version` est l'étiquette de la Release. Les fichiers listés sont `zImage`, `rootfs.img.gz`,
  `usr.tar.gz` et l'archive de la clé USB (`LapinLibre-vX.Y.Z.zip` ou `lapinou-key-vX.Y.Z.zip`).
- `manifest.json.sig` : signature **Ed25519** des octets exacts de `manifest.json`, 64 octets
  bruts.
- chaque fichier listé, sous le même nom.

Clé publique (contenu PEM, extension `.pub`, R42 amendée) : `image/core/usr/share/lapinou/ota-pubkey.pub` sur le lapin ;
`[ota] pubkey_path` sur le serveur (par défaut `server/lapinou/ota-pubkey.pub`, livré). Le
fichier du dépôt est **provisoire** (PLACEHOLDER) tant que la vraie paire n'est pas créée
(T26) : d'ici là, serveur et lapin refusent toute version.

Le serveur vérifie la signature avant de télécharger les fichiers, puis la taille et le
SHA-256 de chacun. Il n'installe la version (et ne construit de clé USB à partir d'elle)
qu'après ces vérifications, et sert le manifeste octet pour octet.

## Adresses pour les lapins

| Adresse | Contenu |
|---|---|
| `GET /ota/manifest.json` | manifeste de la version en service |
| `GET /ota/manifest.json.sig` | sa signature (64 octets) |
| `GET /ota/<version>/<nom>` | un fichier listé (ou `manifest.json`, `manifest.json.sig`) d'une version encore servie |
| `GET /ota/<nom>` | un fichier de la version en service (forme courte) |

**Règle de lecture du lapin** :

1. lire `manifest.json` et `manifest.json.sig` ;
2. vérifier la signature ;
3. prendre `version` dans ce manifeste ;
4. télécharger chaque fichier par `/ota/<version>/<nom>`, puis vérifier taille et SHA-256.

Une version remplacée reste servie par `/ota/<version>/…` au moins **10 minutes** après le
changement : un lapin qui a lu l'ancien manifeste reçoit toujours les fichiers de ce
manifeste-là. Après ce délai, 404 : le lapin recommence à l'étape 1. Un nom inconnu, ou une
version qui n'est plus servie, répond 404.
