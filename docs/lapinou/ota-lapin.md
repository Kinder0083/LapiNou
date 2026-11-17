<!-- SPDX-License-Identifier: MIT -->
# Mises à jour signées sur le lapin (`lapinou-ota`)

`/usr/karotz/bin/lapinou-ota` met le lapin à jour sans clé USB, avec une version publiée et signée. Le format des versions et les adresses du serveur sont ceux de `docs/lapinou/ota.md` (branche du serveur).

```
lapinou-ota check      # {"installed":"v1.0.0","available":"v1.1.0"}
lapinou-ota install    # télécharge, vérifie, écrit, redémarre
lapinou-ota install --allow-downgrade   # accepte une version plus ancienne que celle installée
```

Qui l'appelle (Tâche 12) : l'onglet « Mises à jour » de la page web du lapin (`check`, `install`),
et cron une fois par jour (`check` seulement, jamais `install`), par `share/lapinou/webroot.sh`, qui
garde la réponse dans `/usr/ota/check.json` et `/usr/ota/available` pour la page et pour
`lapinou-mqtt` (`latest_version`). Voir `docs/lapinou/page-web.md`.

## Versions (R44)

Une version publiée s'appelle `vX.Y.Z` : c'est l'étiquette de la Release et le champ `version` du manifeste (`tools/sign-ota.py` exige `--version vX.Y.Z`, le lapin refuse un manifeste dont la version n'a pas cette forme). `/usr/etc/conf/sys_version` contient cette étiquette, quelle que soit l'installation (clé USB, migration depuis FROS, `lapinou-ota`) ; une construction qui n'est pas exactement une étiquette (`install.conf` : `RELEASE_VERSION=0.0.0-<commit>`) écrit `0.0.0-<commit>`, plus ancienne que toute version publiée. `BUILD_VERSION` (le `git describe`) et `lapinlibre_build_id` (l'empreinte des images) restent à part.

`lapinou-ota install` compare les nombres (`v1.10.0` > `v1.9.0`) : même version, ou même construction (l'empreinte calculée depuis les SHA-256 du manifeste égale `lapinlibre_build_id`) : rien à faire ; version plus ancienne : refus sans `--allow-downgrade`. Un `sys_version` d'avant R44 (une empreinte de commit) compte comme 0.0.0.

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
3. Version (voir plus haut) : `vX.Y.Z` ; même version ou même construction : rien à faire ; plus ancienne : refus sauf `--allow-downgrade`.
4. Tailles annoncées : `zImage` ≤ mtd1, `rootfs.img.gz` ≤ mtd2 (`/proc/mtd`), place libre dans `/usr`.
5. Téléchargement de `zImage`, `rootfs.img.gz` et `usr.tar.gz` seulement, dans `/usr/ota/new` (la mémoire flash, pas la RAM) ; taille et SHA-256 de chacun contre le manifeste ; décompression des deux archives ; système de fichiers décompressé ≤ disque en mémoire.

Au moindre échec, `/usr/ota/new` est effacé et le lapin reste tel quel (code 1). Des fichiers laissés par une coupure sont effacés au lancement suivant.

## Installation

Comme l'`autorun` de la clé USB, avec ses fonctions (`install-lib.sh`, dans le système de fichiers racine : `/karotz/scripts/install-lib.sh`) :

1. avant tout effacement : `/sbin/flash_erase` (ou `flash_eraseall`), `/sbin/nandwrite` et `/sbin/nanddump` présents, et une lecture d'essai d'un bloc d'effacement de mtd1 par `nanddump` avec les options exactes de la relecture (`-q --bb=skipbad -l`) ; sinon refus, rien n'est écrit. Puis arrêt des démons (`S40lapinlibre stop`) ;
2. mtd1 : effacement, `zImage` écrit (`nandwrite -pm`), puis **relu** (`nanddump -q --bb=skipbad`, sans OOB, blocs entiers) et comparé (SHA-256 des premiers octets) à l'image vérifiée. S'il diffère, mtd1 est écrit et relu une seconde fois ; s'il diffère encore, l'installation s'arrête là : `/usr` n'est pas touché, les fichiers vérifiés restent dans `/usr/ota/new` (code 1) ;
3. mtd2 : de même avec `rootfs.img.gz` ;
4. seulement si les deux relectures sont bonnes : `karotz.conf` et `voice.conf` du lapin mis de côté dans `/usr/ota/new`, puis le marqueur `/usr/ota/resume.conf` (`FORMAT=1`, version, empreinte, SHA-256 de `usr.tar.gz` ; `ota-resume.sh` vérifie le format et que la version est `vX.Y.Z`) ;
5. `/karotz/scripts/ota-resume.sh` (dans le système de fichiers racine, pas dans `/usr`) : `/usr` vidé sauf `/usr/etc/conf`, les applications, les messages, `uuid.conf` et `/usr/ota`, `usr.tar.gz` décompressé (une seconde fois si besoin), réglages du lapin remis, `sys_version` et `lapinlibre_build_id` écrits (l'empreinte calculée comme `image.py`, pour qu'une clé USB de la même construction n'applique que ses réglages), `/usr/etc/inittab` retiré, puis le marqueur, puis les téléchargements ;
6. redémarrage.

mtd0 (chargeur) et mtd3 à mtd5 (secours) ne sont jamais nommés. Dès la première écriture, aucun échec n'efface les fichiers vérifiés. Journal : `/usr/ota/last.log`.

## Coupure de courant, phase par phase

| Phase | Si le courant est coupé |
|---|---|
| Lecture du manifeste, téléchargement, vérifications | Sans danger : rien n'est écrit. Les fichiers partiels de `/usr/ota/new` sont effacés au lancement suivant de `lapinou-ota`. |
| Arrêt des démons | Sans danger : le lapin redémarre sur l'ancienne version. |
| De l'effacement de mtd1 à la fin de l'écriture et de la relecture de mtd2 | **Non vérifié** : le noyau ou le système de fichiers racine est incomplet, ou le nouveau noyau démarre avec l'ancien système. Ce que fait alors le chargeur (mtd0), et s'il se rabat sur les partitions de secours (mtd3 à mtd5), n'a pas été vérifié. Réparation : la clé USB, si le lapin démarre encore ; sinon le retour arrière de la sauvegarde (voir `migration.md`). |
| `/usr` vidé ou en cours de décompression (`ota-resume.sh`) | `/usr` est partiel, mais mtd1 et mtd2 sont la nouvelle version, relue. Au démarrage suivant, `linuxrc` (nouveau système de fichiers racine) voit `/usr/ota/resume.conf` juste après avoir monté `/usr`, avant tout usage de `/usr`, et relance `ota-resume.sh` depuis l'archive vérifiée gardée dans `/usr/ota/new` (contrôlée à nouveau par son SHA-256). `lapinou-ota` lui-même est dans `/usr` et peut avoir disparu à ce moment : la reprise n'en dépend pas. Tant que le marqueur est là, `lapinou-ota` refuse de démarrer. Si la reprise échoue (archive qui ne correspond plus, marqueur invalide), elle s'arrête avant de toucher `/usr`, écrit la raison dans `/usr/ota/resume.failed` et le marqueur reste ; `lapinou-ota` dit alors qu'il faut la clé USB (pas seulement un redémarrage). |
| Après le retrait du marqueur | Sans danger : `/usr` est complet ; d'éventuels restes de `/usr/ota/new` sont effacés au lancement suivant. |

Si une partition se relit mal deux fois, le lapin continue de tourner sur l'ancien système en mémoire : ne pas le redémarrer avant d'avoir prévu la réparation (clé USB, ou retour arrière par SSH comme pour la migration), car un redémarrage charge le noyau de mtd1.

## Signer une version

```
tools/sign-ota.py --key <clé privée> --dir out/usbkey --version vX.Y.Z     # obligatoire : l'étiquette de la Release
tools/sign-ota.py --verify --pubkey <clé publique> --dir out/usbkey
```

Sans `--file`, le manifeste liste `zImage`, `rootfs.img.gz`, `usr.tar.gz` et l'archive de la clé USB (`LapinLibre-*.zip` ou `lapinou-key-*.zip`) présente dans le dossier. La vraie paire de clés est créée par le propriétaire (Tâche 26) ; la clé privée n'entre jamais dans le dépôt.
