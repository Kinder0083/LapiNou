<!-- SPDX-License-Identifier: MIT -->
# Publier une version de LapiNou

Une version s'appelle `vX.Y.Z` (R44). Pousser l'étiquette `vX.Y.Z` lance le workflow GitHub
`release` (`.github/workflows/release.yml`), qui construit, vérifie, signe et publie tout seul.
Les tests du serveur et de l'installateur tournent aussi à chaque push de branche (workflow
`server`, `.github/workflows/server.yml`).

## Ce que publie une version

Release GitHub `vX.Y.Z`, chaque fichier en asset séparé (R41, R42, `docs/lapinou/ota.md`) :

| Asset | Contenu |
|---|---|
| `manifest.json` | `{"version": "vX.Y.Z", "files": {…}}` : taille et SHA-256 de **tous** les fichiers ci-dessous |
| `manifest.json.sig` | signature Ed25519 brute (64 octets) des octets exacts de `manifest.json` |
| `zImage`, `rootfs.img.gz`, `usr.tar.gz` | les images que `lapinou-ota` installe sur le lapin |
| `lapinou-key-vX.Y.Z.zip` | la clé USB (à décompresser à la racine d'une clé FAT32 vide) |
| `lapinou-server-vX.Y.Z.tar.gz` | le serveur : `server/`, `tools/app-server/`, `userland/proto/`, `installer/`, `docs/lapinou/`, `LICENSES/` du commit étiqueté |

Aucun fichier `.sha256` à part : seul le manifeste signé fait foi.

## Étapes du workflow `release`

| Tâche | Rôle | Durée attendue |
|---|---|---|
| `preflight` | l'étiquette est `vX.Y.Z` ; le secret `OTA_SIGNING_KEY` existe, est une clé Ed25519, et sa clé publique est **exactement** celle des deux fichiers `.pub` du dépôt (ni l'un ni l'autre provisoire). Sinon : échec ici, avant la longue construction, et rien n'est publié | moins d'une minute |
| `server` | tests du serveur (`uv run pytest`) et de l'installateur (`installer/tests/run.sh`, `shellcheck`) | 5 à 10 minutes |
| `key` | construction du firmware (Buildroot, `./build.sh check fetch toolchain system v8 kernel userland image`), avec la chaîne de compilation publiée et le cache des téléchargements d'amont (celui que le workflow `downloads` enregistre sous `main` : une étiquette ne voit que le cache de `main`) ; zippe la clé USB | environ 40 minutes (LapinLibre v0.1.0 : 36,5 minutes) ; bien plus au premier lancement sans cache, limite 350 minutes |
| `publish` | `scripts/lapinou-release.sh assemble` : rassemble les fichiers, fait l'archive du serveur, signe avec `tools/sign-ota.py --version vX.Y.Z` (qui exige que ce soit le `RELEASE_VERSION` de la clé construite), revérifie avec `tools/sign-ota.py --verify` et les deux clés publiques du dépôt ; puis crée la Release | 1 à 3 minutes |

`publish` ne tourne que si les trois autres ont réussi. La clé privée n'est jamais affichée :
elle est écrite dans un fichier temporaire `0600` (umask 077) le temps d'une étape, puis effacée
par `shred`. Lancé à la main (« Run workflow », même sur une étiquette), le workflow construit les mêmes fichiers depuis
la branche choisie et les garde 7 jours avec l'exécution ; il ne signe et ne publie rien.

Les tests du firmware ne tournent pas dans GitHub : lance-les sur la machine de construction
avant de poser l'étiquette (`docs/build.md`, « Make a release »).

## Une seule fois : créer la paire de clés de signature

Les lapins (`image/core/usr/share/lapinou/ota-pubkey.pub`) et le serveur
(`server/lapinou/ota-pubkey.pub`) refusent toute version tant que ces fichiers sont provisoires
(PLACEHOLDER). Le propriétaire crée la vraie paire **sur sa propre machine**, jamais dans le
dossier du dépôt (la clé privée ne doit jamais être ajoutée à git, le dépôt peut être public).

Conseil : fais d'abord une **répétition complète** sur un dépôt jetable, avec une paire jetable
(voir [Répétition sur un dépôt jetable](#répétition-sur-un-dépôt-jetable)), avant de créer la
vraie paire.

Dans un terminal bash (Linux, ou Git Bash sous Windows ; OpenSSL 3.0 ou plus récent) :

1. Ouvre un dossier de travail **hors du dépôt et hors de tout dossier synchronisé** (OneDrive,
   Dropbox, Google Drive ; sous Windows, `Documents`, `Bureau` et `Images` sont souvent
   synchronisés par OneDrive). Par exemple :

   ```bash
   mkdir -p /c/lapinou-secrets && cd /c/lapinou-secrets   # Git Bash ; sous Linux : ~/lapinou-secrets
   umask 077
   ```

2. Crée la clé privée Ed25519 (fichier `ota-signing.key`) :

   ```bash
   openssl genpkey -algorithm ed25519 -out ota-signing.key
   ```

3. Crée la clé publique, avec la ligne de licence exigée par le dépôt :

   ```bash
   { echo '# SPDX-License-Identifier: MIT'; openssl pkey -in ota-signing.key -pubout; } > ota-pubkey.pub
   cat ota-pubkey.pub    # -----BEGIN PUBLIC KEY----- … -----END PUBLIC KEY-----
   ```

   Sans OpenSSL, avec Python et le paquet `cryptography` (celui du serveur), à la place des
   étapes 2 et 3 :

   ```bash
   python3 - <<'EOF'
   import os
   from cryptography.hazmat.primitives import serialization as s
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
   k = Ed25519PrivateKey.generate()
   fd = os.open("ota-signing.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   os.write(fd, k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
   os.close(fd)
   pub = k.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo)
   open("ota-pubkey.pub", "wb").write(b"# SPDX-License-Identifier: MIT\n" + pub)
   EOF
   ```

4. **Sauvegarde** le contenu de `ota-signing.key` dans ton **gestionnaire de mots de passe**
   (note sécurisée, lignes `BEGIN` et `END` comprises). Perdue, plus aucune mise à jour sans fil
   n'est possible : il faudrait réinstaller chaque lapin par clé USB avec une nouvelle clé
   publique.

5. Mets la clé privée dans le secret du dépôt GitHub, sous le nom `OTA_SIGNING_KEY` :

   ```bash
   gh secret set OTA_SIGNING_KEY --repo Kinder0083/LapiNou < ota-signing.key
   ```

   ou sur github.com : Settings → Secrets and variables → Actions → New repository secret, nom
   `OTA_SIGNING_KEY`, valeur : tout le contenu du fichier, lignes `BEGIN` et `END` comprises
   (vide ensuite le presse-papiers, par exemple en copiant un autre texte).

6. Remplace les deux fichiers provisoires par la clé publique, vérifie, puis commite :

   ```bash
   cd <dossier du dépôt LapiNou>
   cp /c/lapinou-secrets/ota-pubkey.pub image/core/usr/share/lapinou/ota-pubkey.pub
   cp /c/lapinou-secrets/ota-pubkey.pub server/lapinou/ota-pubkey.pub
   bash scripts/lapinou-release.sh check-key --signing-key /c/lapinou-secrets/ota-signing.key
   #   → signing key: Ed25519, matches 2 public key file(s)
   python -I scripts/check-repo.py
   git add image/core/usr/share/lapinou/ota-pubkey.pub server/lapinou/ota-pubkey.pub
   git status        # seulement ces deux fichiers, jamais ota-signing.key
   git commit -m "ota: vraie clé publique de signature des versions"
   git push
   ```

   Les tests du serveur et du firmware n'utilisent pas ces fichiers (ils créent leurs propres
   paires jetables) : ils restent verts avec la vraie clé.

7. Efface les copies de la clé privée qui traînent : `ota-signing.key` du dossier de travail
   (sous Linux `shred -u ota-signing.key` ; sous Windows, supprime le fichier puis vide la
   corbeille) et toute autre copie (téléchargements, clé USB de transfert). Il ne reste que le
   secret GitHub et la sauvegarde du gestionnaire de mots de passe. Pour un nouveau `check-key`
   plus tard, recrée le fichier depuis la sauvegarde, puis efface-le de nouveau.

Les lapins déjà installés avec la clé provisoire reçoivent la vraie clé publique par la
prochaine installation par clé USB (ou migration) : `lapinou-ota` les refuse tant qu'ils ont la
clé provisoire.

## Chaque version

1. Tout est commité et poussé ; le workflow `server` est vert sur le commit.
2. Sur la machine de construction : `./build.sh` complet, construction et tests du firmware
   réussis (`docs/build.md`).
3. Choisis le numéro : la dernière version (`git describe --tags --match 'v[0-9]*' --abbrev=0`)
   avec le numéro mineur ou de correctif augmenté.
4. Pose et pousse l'étiquette sur le commit testé (un push : lancé à la main par « Run
   workflow », même sur une étiquette, le workflow ne signe et ne publie rien) :

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

5. Sur GitHub, Actions → `release` de l'étiquette : environ 50 minutes en tout.
6. À la fin, Releases → `v0.1.0` : les sept assets du tableau plus haut, et les notes avec
   l'empreinte de la construction. Contrôle possible sur ta machine, après téléchargement des
   assets dans un dossier vide :

   ```bash
   python -I tools/sign-ota.py --verify --pubkey server/lapinou/ota-pubkey.pub --dir <dossier>
   ```

Les serveurs LapiNou voient la nouvelle version à leur prochaine vérification (relais OTA), les
lapins sans serveur à leur vérification quotidienne.

## Si une tâche échoue

Rien n'est publié. Un échec pendant l'envoi des fichiers par `gh release create` peut toutefois
laisser un **brouillon** de Release (invisible des lapins et des serveurs, mais il gêne un nouvel
essai sous le même nom). Avant de relancer :

1. Supprime le brouillon s'il existe :

   ```bash
   gh release list --repo Kinder0083/LapiNou          # un brouillon est marqué « Draft »
   gh release delete v0.1.0 --repo Kinder0083/LapiNou --yes
   ```

   ou sur github.com : Releases → le brouillon → icône de corbeille.
2. Supprime l'étiquette : `git push origin :refs/tags/v0.1.0` puis `git tag -d v0.1.0`.
3. Corrige, puis reprends à l'étape 1 de [Chaque version](#chaque-version).

Messages fréquents : `OTA_SIGNING_KEY is missing` : le secret n'existe pas (étape 5 de la
création de la paire). `isn't the public key of OTA_SIGNING_KEY` ou `PLACEHOLDER` : le secret et
les fichiers `.pub` commités ne vont pas ensemble (étape 6).

## Répétition sur un dépôt jetable

Pour voir tout le chemin (étiquette → Release signée) sans risque pour le vrai dépôt :

1. Crée un dépôt privé jetable et pousse-y la branche :
   `gh repo create <toi>/lapinou-essai --private`, puis
   `git push https://github.com/<toi>/lapinou-essai.git lapinou/v1:main`.
2. Dans un dossier temporaire, crée une **paire jetable** (étapes 2 et 3 plus haut) et mets sa
   clé privée dans le secret `OTA_SIGNING_KEY` **du dépôt jetable** (`--repo <toi>/lapinou-essai`).
3. Dans un clone du dépôt jetable seulement, remplace les deux `.pub` par la clé publique
   jetable, commite, pousse, puis pousse une étiquette `v0.1.0`.
4. Vérifie la Release `v0.1.0` du dépôt jetable : sept assets, et `sign-ota.py --verify` avec la
   clé publique jetable.
5. Supprime le dépôt jetable (`gh repo delete <toi>/lapinou-essai --yes`, après
   `gh auth refresh -s delete_repo`) et la paire jetable. Ce commit de clés jetables ne doit
   jamais arriver dans `Kinder0083/LapiNou`.
