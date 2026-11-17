<!-- SPDX-License-Identifier: MIT -->
# Passer un Karotz de Free Rabbits OS à LapinLibre par SSH

`tools/lapinou-migrate/migrate-from-fros.sh` installe LapinLibre sur un lapin qui tourne sous Free Rabbits OS (FROS, avec ou sans OpenKarotz), sans clé USB : il prend les fichiers d'une clé USB construite (`out/usbkey`, voir `docs/build.md`) et fait par SSH ce que l'`autorun` de la clé ferait.

```
tools/lapinou-migrate/migrate-from-fros.sh --rabbit 192.168.1.103 --key out/usbkey \
    --ssh-key ~/.ssh/id_ed25519 --backup sauvegarde --dry-run
tools/lapinou-migrate/migrate-from-fros.sh --rabbit 192.168.1.103 --key out/usbkey \
    --ssh-key ~/.ssh/id_ed25519 --backup sauvegarde
```

Il tourne sur un PC du même réseau (Linux, ou Git Bash sous Windows) avec `ssh`, `md5sum`, `gzip`, `sed` et `curl`. La passerelle et le DNS du PC (secours de l'adresse fixe, voir `--dhcp`) sont lus par `ip route` et `/etc/resolv.conf` (ou `resolvectl`) sous Linux, par PowerShell (`Get-NetRoute`, `Get-DnsClientServerAddress`) sous Windows ; les variables `LAPINOU_PC_GATEWAY` et `LAPINOU_PC_DNS`, si elles sont définies (même vides), les remplacent. FROS accepte `ssh karotz@<lapin>` (uid 0) avec un mot de passe vide ; son vieux Dropbear demande `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o MACs=+hmac-sha1`, que le script passe lui-même.

## Options

| Option | Rôle |
|---|---|
| `--rabbit IP` | adresse du lapin sous FROS |
| `--key DOSSIER` | la clé construite : `zImage`, `rootfs.img.gz`, `usr.tar.gz`, `install.conf` (empreintes MD5), `build_id`, `install-lib.sh`, `applysettings.sh`, `lapinlibre.txt` |
| `--ssh-pubkey CLÉ` | la clé publique (ligne `ssh-ed25519 AAAA… nom` ou fichier `.pub`) que le SSH de LapinLibre acceptera : LapinLibre n'a pas de connexion par mot de passe. Facultative seulement si le `lapinlibre.txt` de la clé a déjà un `SSH_PUBKEY`. |
| `--ssh-key FICHIER` | la clé privée correspondante : sert à se reconnecter à LapinLibre après le redémarrage (`-i`, sinon l'identité par défaut de `ssh`) ; `FICHIER.pub` tient lieu de `--ssh-pubkey` s'il manque |
| `--backup DOSSIER` | la sauvegarde du lapin (`sauvegarde/`) : les vérifications relisent mtd1 et mtd2 en entier et disent `MATCH` ou `DIFFERENT` par rapport à `data/mtd1_kernel.bin` et `data/mtd2_ramdisk.bin`, c'est-à-dire si le retour arrière rendra l'état actuel |
| `--dhcp` | LapinLibre prend son adresse par DHCP. Par défaut il garde l'adresse actuelle du lapin (`--rabbit`), avec le masque et la passerelle lus dans `/proc/net/route` et le DNS de `/etc/resolv.conf`, une fois que le lapin les a vus répondre (un `ping` de la passerelle, un `nslookup github.com` auprès du DNS). Sinon il prend la passerelle et le DNS du PC (la passerelle seulement si elle est sur le réseau du lapin), s'ils répondent au lapin ; sinon la vérification échoue (code 2) et dit d'utiliser `--dhcp`. Un `network.conf` déjà présent est gardé dans tous les cas. |
| `--dry-run` | toutes les vérifications, sur le PC et sur le lapin, puis arrêt. La mémoire flash n'est pas touchée : seuls les petits fichiers vont dans `/dev/shm` (mémoire vive du lapin), effacés à la fin ; `usr.tar.gz` n'est pas envoyé. |
| `--no-wait` | après le redémarrage, ne pas attendre le SSH et la page web de LapinLibre |

Les réglages appliqués sont ceux du `lapinlibre.txt` de la clé (valeurs vides : on garde ceux du lapin), puis la clé SSH donnée. Le Wi-Fi de FROS (`/usr/etc/conf/wpa.conf`, le même `wpa_supplicant -Dwext`) est gardé tel quel : un `lapinlibre.txt` qui donne `WIFI_SSID`, `WIFI_PASSWORD` ou `IP` est refusé (code 2), car `applysettings.sh` remplacerait alors `wpa.conf` et `network.conf`. Le DNS lu dans `/etc/resolv.conf` n'est retenu que s'il est une adresse IPv4 (sinon la passerelle), parce que `network.conf` est lu par le shell en root.

## Déroulement

1. **Sur le PC** : présence des fichiers, MD5 de `zImage`, `rootfs.img.gz` et `usr.tar.gz` contre `install.conf`, tailles (noyau ≤ 3 Mio, système ≤ 5 Mio), décompression, système ≤ 5 Mio une fois décompressé, présence d'une clé SSH.
2. **Envoi** dans `/dev/shm/lapinou-migrate` (29 Mo de mémoire vive) : `on-rabbit.sh`, `install-lib.sh` et `applysettings.sh` de la clé (leur `/mnt/usbkey` devient ce dossier), `lapinlibre.txt`, `install.conf`, `build_id`, `zImage`, `rootfs.img.gz`.
3. **Vérifications sur le lapin** (`on-rabbit.sh check`), rien n'est écrit : pas déjà sous LapinLibre ; outils `/sbin/nandwrite`, `/sbin/nanddump`, `flash_erase(all)` ; `/` est le disque en mémoire (`root=/dev/ram`) et `/usr` le yaffs de mtd6 ; noms des partitions (`mtd1` « Kernel », `mtd2` « ramdisk », `mtd6` « yaffs ») ; MD5 des fichiers reçus ; place dans les partitions et dans le disque en mémoire ; essai de relecture (un bloc de mtd1 par `nanddump -o -b`) ; Wi-Fi présent ; adresse réseau prévue (passerelle et DNS qui répondent, pour une adresse fixe) ; `applysettings.sh --check` ; réglages Wi-Fi/adresse absents de `lapinlibre.txt` ; place libre dans `/usr`. En mode `check`, il affiche aussi `ls /usr/etc/conf`, compare mtd1 et mtd2 entières avec la sauvegarde (`--backup`) et liste les processus que l'installation arrêtera. `--dry-run` s'arrête ici.
4. **`usr.tar.gz`** (17 Mo, trop gros pour la mémoire vive) est envoyé en flux par `ssh` dans `/usr/.lapinou-migrate/`, puis `on-rabbit.sh install` refait toutes les vérifications, avec son MD5 et sa décompression, **avant d'effacer quoi que ce soit**.
5. **Arrêt de FROS** : tous les processus sont gelés (`SIGSTOP`) puis tués (`SIGKILL`), sauf `init`, `syslogd`, `klogd`, `inetd`, `dropbear`, `wpa_supplicant`, `udhcpc`, `power`, `getty`, la session SSH en cours et un éventuel détenteur de `/dev/watchdog` (l'arrêter laisserait le chien de garde redémarrer le lapin). Ainsi le contrôleur, `immortaldog`, `crond`, OpenKarotz… ne peuvent plus redémarrer le lapin entre l'effacement de mtd1 et la fin de la relecture de mtd2. Le journal dit ce qui tourne encore.
6. **Écriture**, comme l'`autorun` de la clé : `zImage` dans mtd1, `rootfs.img.gz` dans mtd2 (`flash_eraseall` puis `nandwrite -pm`), chacune relue par `nanddump -o -b` et comparée (MD5). FROS tourne depuis la mémoire vive : réécrire mtd1 et mtd2 ne le gêne pas. Puis `/usr` : `karotz.conf`, `voice.conf`, `wpa.conf` et `network.conf` mis de côté, `/usr` vidé sauf `/usr/etc/conf`, `/usr/karotz/apps`, `/usr/karotz/messages`, `/usr/karotz/uuid.conf`, `lost+found`, `usr.tar.gz` décompressé, réglages remis, `network.conf` écrit s'il n'existait pas, `applysettings.sh`, `lapinlibre_build_id`, `/usr/etc/inittab` retiré.
7. **Redémarrage** : `on-rabbit.sh` lance lui-même `reboot -f` 2 s après avoir fini (après le nettoyage de `/usr`, une nouvelle connexion au Dropbear de FROS peut échouer : ses clés d'hôte étaient dans `/usr/etc/dropbear`). Puis attente (5 min au plus, chaque essai SSH ayant 30 s pour se connecter) du SSH par clé et de la page `http://<lapin>/` de LapinLibre.

Seules mtd1 et mtd2 sont effacées ; `/usr` (mtd6) est modifié par son système de fichiers monté. Le chargeur (mtd0) et les partitions de secours (mtd3, mtd4, mtd5) ne sont jamais touchés : le script refuse toute autre partition, et un test vérifie que les deux scripts ne les nomment même pas.

## Codes de sortie

| Code | Sens | État du lapin |
|---|---|---|
| 0 | fait | LapinLibre démarré, SSH par clé et page web répondent |
| 2 | une vérification a échoué avant toute écriture | FROS intact (seul `/dev/shm` et `/usr/.lapinou-migrate` ont servi, nettoyés) |
| 3 | une partition relue diffère de son image | mtd1 et/ou mtd2 réécrits, `/usr` intact, **pas de redémarrage** : FROS tourne encore en mémoire, le SSH marche |
| 1 | autre échec | selon le journal : après l'écriture de mtd1/mtd2, `/usr` peut être à moitié remplacé |

Le journal du lapin (`/dev/shm/lapinou-migrate/install-log.txt`) est affiché au fur et à mesure, préfixé `rabbit:`. Après le début des écritures, une coupure SSH n'interrompt pas `on-rabbit.sh` (il ignore `SIGHUP` et `SIGPIPE`) : relire alors ce journal par SSH avant toute autre action.

## Retour arrière

La sauvegarde complète (`sauvegarde/`, voir son `LISEZMOI.md`) contient chaque partition, avec et sans OOB, et les fichiers de `/usr`. Ne jamais la modifier ; vérifier une copie par `EMPREINTES.md5` avant de l'envoyer.

**Code 3, ou code 1 avant « Cleaning /usr »** (le lapin tourne encore sous FROS, ne pas l'éteindre) : réécrire le noyau et le système de FROS depuis la sauvegarde, par la même session SSH sans mot de passe :

```
S="ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o MACs=+hmac-sha1 karotz@192.168.1.103"
$S 'cat > /dev/shm/mtd1.oob.bin' < sauvegarde/mtd1_kernel.oob.bin
$S 'md5sum /dev/shm/mtd1.oob.bin'          # 9dff392c9daebe6da533d44169251347
$S 'flash_eraseall /dev/mtd1 && nandwrite -o -n /dev/mtd1 /dev/shm/mtd1.oob.bin && rm /dev/shm/mtd1.oob.bin'
$S 'nanddump -o -b /dev/mtd1 | md5sum'    # 4d3d01e2e727ad64905c9b346c26c8b7 (data/mtd1_kernel.bin)
$S 'cat > /dev/shm/mtd2.oob.bin' < sauvegarde/mtd2_ramdisk.oob.bin
$S 'md5sum /dev/shm/mtd2.oob.bin'          # c9bc0aa8bb6f321af665dcf4b99dd581
$S 'flash_eraseall /dev/mtd2 && nandwrite -o -n /dev/mtd2 /dev/shm/mtd2.oob.bin && rm /dev/shm/mtd2.oob.bin'
$S 'nanddump -o -b /dev/mtd2 | md5sum'    # 0b2bd27d3d305a45c82f203b58282595 (data/mtd2_ramdisk.bin)
```

(`nandwrite -o -n` : l'image contient l'OOB, écrite telle quelle, comme dans `sauvegarde/LISEZMOI.md`.) Le lapin redémarre ensuite sous FROS, avec son `/usr` intact.

**LapinLibre démarre mais n'est pas sur le réseau** (mauvais Wi-Fi ou adresse) : copier `out/usbkey` sur une clé USB FAT, remplir son `lapinlibre.txt` (Wi-Fi, adresse, `SSH_PUBKEY`), brancher la clé et allumer le lapin : même build, l'`autorun` applique seulement les réglages (`docs/install.md`).

**Le lapin ne démarre plus, ou `/usr` est à moitié remplacé** : la clé LapinLibre ci-dessus refait une installation complète au démarrage si le système démarre encore assez pour lancer son `autorun`. Sinon, la clé d'usine (`sources/key-factory`) restaure noyau, système et secours ; puis remettre FROS en restaurant mtd1 et mtd2 comme ci-dessus et les fichiers de `/usr` depuis `sauvegarde/usr_fichiers.tar` (`tar -xf - -C /usr` par SSH), ou réinstaller FROS par sa propre procédure.

## Après la migration

- Le SSH de LapinLibre n'accepte que la clé donnée : `ssh karotz@192.168.1.103` (Dropbear récent, plus besoin des options `ssh-rsa`/`hmac-sha1`). Sa clé d'hôte n'est plus celle de FROS : retirer l'ancienne ligne de `~/.ssh/known_hosts` (`ssh-keygen -R 192.168.1.103`).
- Sans `WEB_PASSWORD` dans `lapinlibre.txt`, la page web est ouverte au réseau local sans mot de passe.
- Les mises à jour suivantes se font par la clé USB ou les moyens de LapinLibre ; le script refuse un lapin déjà sous LapinLibre.
