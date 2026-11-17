<!-- SPDX-License-Identifier: MIT -->
# La page web du lapin

`http://<lapin>/` est la page LapiNou (Tâche 12) : l'accueil (état et commandes rapides), les
réglages, les mises à jour, et l'ancienne page LapinLibre sous « Avancé » (`avance.html`, inchangée,
avec ses programmes `cgi-bin/*`). Le style est décrit dans `design.md`.

## Fichiers

- `image/core/usr/www/index.html`, `lapinou/app.js`, `lapinou/style.css`, `lapinou/tokens.css`,
  `lapinou/lapin.svg`, `lapinou/fr.json` (tous les textes). JavaScript sans dépendance ; les
  valeurs vont dans la page comme texte, jamais comme HTML. Le tout (avec la page LapinLibre) fait
  moins de 150 Ko.
- `cgi-bin/lapinou/*` : les programmes de la page. Ils lisent `web.sh` de LapinLibre et passent par
  son `web_begin` (utilisateur web, réseau local, règle anti-site-tiers, méthodes, tailles), puis
  `share/lapinou/web-lapinou.sh`.
- `share/lapinou/webroot.sh`, lancé seulement par `/usr/karotz/bin/lapinou-webroot`.

## Droits

httpd et ses programmes tournent sous l'utilisateur `web`, mais les réglages, les secrets
(`lapinou.secrets`, 0600), le Wi-Fi (`wpa.conf`, 0600), les démons et les mises à jour sont à root.
`lapinou-webroot` est setuid root, mode 4750, groupe `web` : seuls root et le groupe web peuvent le
lancer. Il n'accepte que 1 à 3 mots (`a-z`, `0-9`, `_`, `-`, 32 caractères au plus), vide
l'environnement, ferme tout descripteur au-delà de 2 et lance `webroot.sh` avec la même entrée
standard. **Toute valeur passe par l'entrée standard**, jamais en argument. `webroot.sh` vérifie à
nouveau la commande et chaque réglage (liste fermée), puis écrit avec `conf.sh`, qui garde tout
texte tel quel (`a"b$(id);c` est stocké tel quel) et prend le verrou `lapinou.lock`.

## Programmes

| Programme | Méthode | Rôle |
|---|---|---|
| `lapinou/status` | GET | version (`sys_version`), mode de l'assistant, lien au serveur, MQTT, volume, dernière recherche de mise à jour, mot de passe présent ou nécessaire |
| `lapinou/conf_get` | GET | les réglages ; pour chaque secret, seulement `{"set":true\|false}` |
| `lapinou/conf_set` | POST | `section__clé=valeur`, un ou plusieurs ; tout est vérifié avant d'écrire |
| `lapinou/secret_set` | POST | `name` (`mqtt_password`, `cloud_stt_key`, `cloud_llm_key`, `cloud_tts_key`, `ota_token`, `wifi_password`, `web_password`), `value` |
| `lapinou/test` | POST | `what` = `mqtt` (état du pont), `server` (`/api/health` du serveur, port 8080), `stt`, `llm`, `tts` (liste des modèles du service, avec sa clé) |
| `lapinou/ota` | GET `status`, POST `check` / `install` | `lapinou-ota check`, `lapinou-ota install` lancé à part, l'état et le journal (`/usr/ota/last.log`) |
| `lapinou/restart` | POST | `what` = `mqtt`, `cloud` ou `all` |
| `lapinou/volume` | POST | `value` 0 à 100, par `set_volume` de `lapinou-mqtt` |
| `lapinou/press` | POST | `action` = `start` (le nez clignote, un appui sur la tête est attendu 60 s) ou `status` (`pressed`, `waiting`) : la preuve de présence du premier mot de passe (R46) |

`conf_set` prend aussi les secrets (`secret__cloud_llm_key=…`) avec les réglages : c'est ainsi
qu'une nouvelle adresse garde la clé saisie avec elle (ci-dessous).

## Adresses et clés (R46)

- Quand une adresse change (`cloud.stt_url`, `cloud.llm_url`, `cloud.llm_kind`, `cloud.tts_url`,
  `mqtt.host`), son secret est effacé, sauf si la même requête en donne un nouveau. La réponse le
  dit (`"cleared":["cloud_llm_key"]`) et la page demande de le saisir à nouveau.
- Un service qui a une clé ne peut pas avoir d'adresse `http://` hors du réseau local (127/8,
  10/8, 172.16/12, 192.168/16, 169.254/16, `localhost`, `*.local`) : la clé passerait en clair.
  « Tester » ne l'envoie pas non plus vers une telle adresse.
- Les fichiers que fait `webroot.sh` naissent en 0600 (umask 077) et ne prennent leur mode qu'une
  fois écrits ; il ne lit que 8192 octets de son entrée standard.

Réponses JSON comme l'API LapinLibre (`"return":"0"` ou `"1"` avec `msg`, en français).

## Mot de passe

Le mot de passe de la page est celui de LapinLibre (connexion HTTP Basic de httpd, utilisateur
`admin`). Tant qu'un secret est enregistré dans `lapinou.secrets` sans mot de passe de page, tous les
programmes sauf `status`, `press` et la création du mot de passe (`secret_set name=web_password`)
répondent 401, et la page propose d'en créer un. Le premier mot de passe (aucun n'existe encore)
n'est accepté qu'après un appui sur la tête du lapin dans les 60 s (`lapinou/press`, R46) : le nez
clignote pendant l'attente (led-daemon), l'appui est le signal `click` de button-daemon (celui que
suit aussi `dbus_watcher`), valable 120 s et consommé par la création. Changer un mot de passe
existant passe par la connexion de httpd, sans appui. Le nouveau mot de passe est écrit dans
`/usr/etc/conf/web_password`, puis `inetd.sh conf` le hache et refait la configuration de httpd
(lue à chaque requête). Règles : 8 à 64 caractères ASCII, sans espace, `"`, `\` ni `:`.

## Réglages

`mqtt.*`, `server.enabled`/`host` (l'adresse règle aussi `karotz.conf` et `voice.conf` comme
`applysettings.sh`), `cloud.*`, `audio.volume`, `audio.mic_gain_db`, `assistant.language`,
`time.tz`, `ota.url` ; `ota.repo` est en lecture seule. Réseau (lu au prochain démarrage) :
`network.hostname`, `network.wifi_ssid` et `wifi_password` (`wpa.conf` ; ni `"` ni `\`, message
clair), `network.ip`, `netmask`, `gateway`, `dns` (`network.conf`). Après l'enregistrement d'un
onglet, la page relance le démon concerné.

## Mises à jour

Onglet « Mises à jour » : version installée et disponible, « Rechercher », « Installer » (avec
confirmation), progression et journal. Une fois par jour, cron lance `webroot.sh ota check` (ligne
ajoutée par `S40lapinlibre` dans la table de root, à une minute tirée entre 3 h et 6 h) : recherche
seulement, jamais d'installation. Le résultat (`/usr/ota/check.json`, `/usr/ota/available`) est
affiché par la page et publié par `lapinou-mqtt` (`latest_version`).

## Tests

`tests/image/test_lapinou_web.py` ; contrôle visuel sur un vrai lapin : à faire (T27).
