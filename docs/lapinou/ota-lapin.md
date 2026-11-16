<!-- SPDX-License-Identifier: MIT -->
# Mises à jour signées sur le lapin (`lapinou-ota`)

`/usr/karotz/bin/lapinou-ota` met le lapin à jour sans clé USB, avec une version publiée et signée. Le format des versions et les adresses du serveur sont ceux de `docs/lapinou/ota.md` (branche du serveur).

```
lapinou-ota check      # {"installed":"v1.0.0","available":"v1.1.0"}
lapinou-ota install    # télécharge, vérifie, écrit, redémarre
```

## D'où vient la mise à jour

Réglages de `/usr/etc/conf/lapinou.conf` :

| Réglage | Rôle |
|---|---|
| `[ota] url` | le relais d'un serveur LapiNou. Par défaut, quand `[server] enabled=1` : `http://<[server] host>:8080/ota/` |
| `[ota] repo` | sans serveur : le dépôt GitHub dont on prend la dernière Release (`Kinder0083/LapiNou` par défaut) |
| `ota_token` (dans `lapinou.secrets`) | pour un dépôt privé : jeton à grain fin, lecture seule de « Contents » |

Avec un serveur, le lapin lit `<url>manifest.json` et `<url>manifest.json.sig`, vérifie la signature, puis lit chaque fichier par `<url><version>/<nom>` (`version` du manifeste), ce qui marche même si le serveur change de version pendant le téléchargement.

Sans serveur, il lit `https://api.github.com/repos/<repo>/releases/latest`, puis chaque asset par son adresse d'API. Tout est en **HTTPS** (redirections comprises, sinon refus) avec les certificats de `/usr/etc/ssl/certs`. Le jeton ne part que vers `api.github.com`, dans un en-tête passé à `curl` par son entrée standard (jamais dans ses arguments) ; l'hôte de stockage vers lequel GitHub redirige ne le reçoit pas. L'étiquette de la Release doit être la `version` du manifeste.

## Vérifications (rien n'est écrit avant)

1. La clé publique `/usr/share/lapinou/ota-pubkey.pub` (PEM) existe et n'est pas le fichier provisoire (PLACEHOLDER) : sinon refus, avant tout accès au réseau.
2. Signature Ed25519 des octets exacts de `manifest.json` (`manifest.json.sig`, 64 octets), par `openssl pkeyutl -verify -pubin -rawin` (OpenSSL 3.6 de l'image).
3. Version : si c'est celle du lapin (`/usr/etc/conf/sys_version`), rien à faire.
4. Tailles annoncées : `zImage` ≤ mtd1, `rootfs.img.gz` ≤ mtd2 (`/proc/mtd`), place libre dans `/usr`.
5. Téléchargement de `zImage`, `rootfs.img.gz` et `usr.tar.gz` seulement, dans `/usr/ota/new` (la mémoire flash, pas la RAM) ; taille et SHA-256 de chacun contre le manifeste ; décompression des deux archives ; système de fichiers décompressé ≤ disque en mémoire.

Au moindre échec, `/usr/ota/new` est effacé et le lapin reste tel quel (code 1). Des fichiers laissés par une coupure sont effacés au lancement suivant.

## Installation

Comme l'`autorun` de la clé USB (fonctions de `install-lib.sh`, copié dans `/usr/share/lapinou`) : arrêt des démons (`S40lapinlibre stop`), `zImage` dans mtd1, `rootfs.img.gz` dans mtd2, `/usr` vidé sauf `/usr/etc/conf`, les applications, les messages, `uuid.conf` et `/usr/ota`, `usr.tar.gz` décompressé, `karotz.conf` et `voice.conf` du lapin remis, `sys_version` et `lapinlibre_build_id` (calculé comme `image.py`, pour qu'une clé USB de la même version n'applique que ses réglages) écrits, redémarrage. mtd0 (chargeur) et mtd3 à mtd5 (secours) ne sont jamais nommés. Journal : `/usr/ota/last.log`.

## Signer une version

```
tools/sign-ota.py --key <clé privée> --dir out/usbkey --version vX.Y.Z     # manifest.json + manifest.json.sig
tools/sign-ota.py --verify --pubkey <clé publique> --dir out/usbkey
```

Sans `--file`, le manifeste liste `zImage`, `rootfs.img.gz`, `usr.tar.gz` et l'archive de la clé USB (`LapinLibre-*.zip` ou `lapinou-key-*.zip`) présente dans le dossier. La vraie paire de clés est créée par le propriétaire (Tâche 26) ; la clé privée n'entre jamais dans le dépôt.
