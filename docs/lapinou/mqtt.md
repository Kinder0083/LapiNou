<!-- SPDX-License-Identifier: MIT -->
# Lapinou dans Home Assistant (MQTT)

`lapinou-mqtt` (`userland/src/usr/lapinou-mqtt/`) relie les démons du lapin (D-Bus) à un broker
MQTT et se déclare à Home Assistant par la découverte MQTT : l'appareil « Lapinou » apparaît sans
configuration. Il est lancé par `S40lapinlibre` sous `immortaldog`, après les démons qu'il pilote.

## Réglages

Dans `lapinou.conf`, section `[mqtt]` :

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | — | `1` pour se connecter ; sinon le démon attend sans rien faire (`get_state` → `disabled`) |
| `host` | — | adresse ou nom du broker (IPv6 sans crochets acceptée) |
| `port` | `1883` | port TCP |
| `user` | — | identifiant (sans identifiant, le mot de passe n'est pas envoyé : MQTT 3.1.1 l'interdit) |
| `prefix` | `lapinou` | début des topics du lapin |
| `discovery_prefix` | `homeassistant` | préfixe de découverte de Home Assistant |

Le mot de passe est le secret `mqtt_password` (`lapinou.secrets`), transmis au broker tel quel
(guillemets, `$`, `;`, espaces, accents) et jamais écrit dans le journal. Les réglages sont lus au
démarrage : après un changement, relancer le démon (`/usr/etc/init.d/S40lapinlibre restart`, ou
tuer `lapinou-mqtt`, qu'`immortaldog` relance).

`[audio] volume` (0-100) est appliqué au démarrage du démon et mis à jour par `cmd/volume` (voir
« Volume »). `[assistant] language` (`fr`) est la langue de `cmd/say`.

Le serveur LapiNou publie l'état de l'assistant (`<p>/<id>/assistant`) avec son propre réglage
`mqtt.prefix` (`config.toml`, section `[mqtt]`, `lapinou` par défaut). Les deux préfixes doivent
être égaux : si vous changez `prefix` sur le lapin, mettez la même valeur dans `mqtt.prefix` du
serveur (un mot sans `/`, `+` ni `#`), puis redémarrez le service, sinon le capteur « Assistant »
reste vide.

L'identifiant du lapin (`/usr/karotz/uuid.conf`) n'est utilisé que s'il n'a que des lettres,
chiffres, `_` et `-` (64 au plus) ; sinon le démon ne se connecte pas et le note au journal.

## Connexion

Client Paho asynchrone (`libpaho-mqtt3a`, sans TLS), MQTT 3.1.1, session propre, keep-alive 20 s,
identifiant `lapinou-<id>`. Testament (LWT) : `<p>/<id>/availability` = `offline`, retenu, QoS 1.
Le réseau (résolution du nom comprise) se fait dans les fils de Paho : la boucle principale du démon
ne l'attend jamais. Connexion ratée ou perdue (Wi-Fi coupé, broker redémarré) : nouvel essai après
2 s, 5 fois, puis toutes les 10 s ; le compte repart à zéro après 10 s de connexion tenue.

À chaque connexion : abonnements à `<p>/<id>/cmd/#`, `<p>/<id>/assistant` et
`<discovery_prefix>/status`, puis publication (retenue, QoS 1) de toutes les configurations de
découverte, de `availability` = `online`, de l'état et de l'état de mise à jour. Tout est republié
quand Home Assistant publie `online` sur `<discovery_prefix>/status` (son redémarrage). À l'arrêt
propre (SIGTERM), le démon publie `offline` puis se déconnecte.

## Topics

`<p>` = `prefix`, `<id>` = le contenu de `/usr/karotz/uuid.conf`.

| Topic | Sens | Contenu |
|---|---|---|
| `<p>/<id>/availability` | lapin → | `online` / `offline` (retenu, LWT) |
| `<p>/<id>/state` | lapin → | JSON retenu (ci-dessous), republié à chaque changement |
| `<p>/<id>/event/button` | lapin → | `{"type":"click"}` (`click`, `dclick`, `tclick`, `lclick_start`, `lclick_stop`) |
| `<p>/<id>/event/rfid` | lapin → | `{"tag":"D0021A3502B3C4D5","action":"in"}` (`in`, `out`) |
| `<p>/<id>/snapshot` | lapin → | l'image JPEG de `cmd/snapshot` (non retenue) |
| `<p>/<id>/update` | lapin → | `{"installed_version":"v0.3.0","latest_version":"v0.4.0"}` (retenu) : `installed_version` est `sys_version` (le tag installé, R44 ; à défaut la première ligne de `lapinlibre-build`), `latest_version` la version trouvée par la dernière recherche (`/usr/ota/available`, absente tant qu'aucune recherche n'a abouti) ; relus toutes les 30 s et republiés quand ils changent |
| `<p>/<id>/assistant` | serveur → | état de l'assistant, publié par le serveur LapiNou (Tâche 21), recopié dans l'état (64 octets au plus) |
| `<p>/<id>/cmd/<nom>` | → lapin | commandes (ci-dessous) ; un message **retenu** reçu sur `cmd/…` est ignoré (une commande se fait une fois, quand elle est envoyée, pas à chaque reconnexion) |

État :

```json
{"led":"00FF00","ears":{"left":0,"right":0,"preset":"debout"},"volume":60,"sleep":"off",
 "assistant":"hors ligne","wifi_rssi":-57,"version":"v0.3.0","last_tag":null}
```

`led` : la couleur fixe ou la première couleur de la pulsation (au départ celle de l'API web, sinon
`00FF00`) ; `ears` : la dernière position commandée par MQTT (`preset` : le nom du préréglage, sinon
`personnalisée`) ; `volume` : `null` si inconnu ; `sleep` : `on` quand l'API web a endormi le lapin
ou que le contrôleur dort (état 24) ; `wifi_rssi` : niveau de `wlan0` dans `/proc/net/wireless`,
en dBm, `null` sans Wi-Fi (relu toutes les 30 s) ; `version` : la première ligne de
`/karotz/etc/lapinlibre-build` ; `last_tag` : le dernier badge posé.

Commandes (charge utile en UTF-8, 4096 octets au plus ; un objet JSON n'a que des chaînes, nombres
et booléens) :

| Commande | Charge utile | Effet |
|---|---|---|
| `cmd/led` | `{"color":"RRGGBB"}`, `{"color":"RRGGBB","pulse":"RRGGBB","period_ms":700}`, `{"on":true}`, ou `RRGGBB` seul | `Led.light`, `Led.pulse` (pour toujours, période 25 à 10000 ms) ; `{"on":true}` rallume la dernière couleur non noire ; `000000` éteint |
| `cmd/ears` | `{"left":0-16,"right":0-16}`, `{"preset":"…"}`, `{"reset":true}` | `Ears.move` (vitesse 100, celle de l'API web), `Ears.reset` (les deux oreilles) |
| `cmd/say` | le texte (1 à 1024 octets) | `Voice.tts_speak` (voice-daemon, dont le `TTS_SERVER` est le serveur LapiNou, port 8081) ; en mode sans serveur (`[cloud] enabled=1`, `[server] enabled` différent de 1), `LapinouCloud.say(s)` si `com.mindscape.karotz.LapinouCloud` est sur le bus |
| `cmd/sound` | une URL `http(s)://…`, ou le nom d'un son | le programme `sound` de l'API web (`url=` ou `id=`) |
| `cmd/volume` | `0` à `100` | voir « Volume » ; gardé dans `[audio] volume` |
| `cmd/sleep` | `on` / `off` | les programmes `sleep` et `wakeup` de l'API web ; ensuite l'état dit ce qu'ils ont fait : en veille la LED éteinte et les oreilles à 4, au réveil la LED de l'API web et les oreilles à 0 (sauf si `ears_mode` a coupé les mouvements d'oreilles) |
| `cmd/snapshot` | quelconque | le programme `snapshot` de l'API web ; l'image qu'il a rangée (`/var/run/lapinlibre-web/snapshots/…`, 1 Mio au plus) part sur `<p>/<id>/snapshot` |

`cmd/sound`, `cmd/snapshot` et `cmd/sleep` lancent les programmes de l'API web eux-mêmes, comme
`tag_listener` (`cgi_run` : utilisateur `web`, depuis 127.0.0.1, utilisateur web `admin`) : leurs
règles sont celles de la page web, sans copie dans le démon (vérification des URL `valid_url`, le
verrou `web_lock`, l'état de lecture, les refus pendant la veille ou pendant une application).
Leur réponse décide ; un refus est noté au journal avec le message de l'API web. Un programme qui
finit avec un code différent de 0, ou tué (60 s au plus), est noté aussi.

Comme l'API web (`web_hardware`), `cmd/led`, `cmd/ears` et `cmd/say` sont refusées pendant qu'une
application possède le lapin (états 21 et 22 du contrôleur) et pendant la veille ; l'état est
alors republié, pour que Home Assistant revienne à la vérité. Une commande mal formée est ignorée
et notée.

## Volume

- Si le mélangeur du codec a une commande de volume (`SOUND_MIXER_READ_DEVMASK` contient
  `SOUND_MASK_VOLUME`), `cmd/volume` règle `SOUND_MIXER_WRITE_VOLUME` sur `/dev/mixer` (gauche =
  droite) : tout ce que le lapin joue (voix, sons, karotz-player, MPD) suit. Le volume logiciel de
  MPD s'y ajoute (les deux se multiplient).
- Sinon, le démon règle le volume de MPD (`setvol`, mélangeur logiciel de `mpd.conf`) par son
  protocole sur 127.0.0.1:6600 avec le mot de passe de multimedia-daemon, dans un fil (la boucle
  n'attend pas) : seule la musique jouée par MPD suit, pas la voix ni karotz-player.
  multimedia-daemon garde ce volume quand MPD le signale (sa sortie active) et le remet avant de
  jouer ; `lock::yes` met MPD à 100 % sortie coupée, `lock::no` remet le volume gardé : un
  `setvol` fait pendant `lock::yes` est donc perdu au `lock::no` suivant.
- Le volume publié est celui qui a été **appliqué** ; en cas d'échec (mélangeur ou MPD refuse, MPD
  absent) le démon le note et republie l'état avec le volume d'avant (le curseur de Home Assistant
  revient, sans passer à `null`). Au démarrage sans `[audio] volume`, le volume est lu (mélangeur
  ou `status` de MPD).

**À vérifier sur le vrai lapin (Tâche 3)** : que `/dev/mixer` existe et que l'UDA134X expose
`SOUND_MASK_VOLUME` (ALSA OSS, contrôle « Master ») ; qu'un `cmd/volume 30` baisse bien la voix
(`Voice.tts_speak`), un son (`Multimedia.play`) et la musique MPD ; sans commande de volume, que
`setvol` de MPD 0.15 marche hors lecture et que multimedia-daemon le garde ; noter le chemin pris
(`the volume: …` au démarrage dans le journal).

Préréglages des oreilles (17 pas par tour, 0 = droites après une réinitialisation) : `debout` (0, 0),
`endormies` (4, 4 : la position de veille), `tombantes` (8, 8), `perplexe` (0, 8),
`curieuse` (2, 15).

## Entités Home Assistant

Découverte sous `<discovery_prefix>/<composant>/<id>/<objet>/config`, retenue. Chaque entité a un
`unique_id` `lapinou_<id>_<objet>`, `availability_topic` `<p>/<id>/availability`, le même bloc
`device` (`identifiers: ["lapinou_<id>"]`, `name: "Lapinou"`, `manufacturer: "LapiNou"`,
`model: "Karotz"`, `sw_version`) et `origin` (`lapinou-mqtt`).

| Composant / objet | Nom | Topics |
|---|---|---|
| `light/led` | LED | schéma `template` : `cmd/led`, état `led` |
| `select/ears` | Oreilles | `cmd/ears` (`{"preset":…}`), état `ears.preset` |
| `button/ears_reset` | Réinitialiser les oreilles | `cmd/ears` `{"reset":true}` |
| `event/button` | Bouton | `event/button` |
| `event/rfid` | Badge | `event/rfid` (attribut `tag`) |
| `sensor/last_tag` | Dernier badge | état `last_tag` |
| `number/volume` | Volume | `cmd/volume`, état `volume` |
| `text/say` | Parler | `cmd/say` |
| `text/sound` | Jouer un son | `cmd/sound` |
| `image/snapshot` | Photo | `snapshot` (`image/jpeg`) |
| `button/take_snapshot` | Prendre une photo | `cmd/snapshot` |
| `switch/sleep` | Veille | `cmd/sleep`, état `sleep` |
| `sensor/assistant` | Assistant | état `assistant` |
| `sensor/wifi` | Signal Wi-Fi | état `wifi_rssi` (dBm, diagnostic) |
| `update/firmware` | Micrologiciel | `update` (`installed_version`, `latest_version`) ; informatif : pas de `command_topic`, l'installation se fait depuis la page web du lapin |

Ces configurations ont été vérifiées avec les schémas de découverte de Home Assistant 2026.11.1
(aucune clé rejetée ni ignorée) et leurs modèles rendus par son moteur de modèles.

## D-Bus

`com.mindscape.karotz.LapinouMqtt`, chemin `/com/mindscape/karotz/LapinouMqtt`, interface
`com.mindscape.karotz.LapinouMqtt` (`userland/dbus/lapinou-mqtt.xml`) : `get_state()` →
`disabled`, `connecting`, `connected` ou `disconnected` ; `set_volume(i)` → `b`, le volume de la page
web (comme `cmd/volume` : appliqué, gardé, publié ; `false` hors de 0 à 100) ; signal `mqtt_state(s)`
à chaque changement.

## Tests

`tests/scenarios/lapinou_mqtt.py` avec le faux broker `tests/fakes/fake_mqtt_broker.py`, un faux
MPD `tests/fakes/fake_mpd.py`, les faux démons `tests/fakes/fake_karotz_services.py` et les vrais
programmes de l'API web dans le bac à sable de `tests/image/test_web_api.py` : connexion
(testament, abonnements, découverte, mot de passe à caractères spéciaux transmis octet pour octet),
chaque commande, les refus de l'API web (URL, son inconnu, photo pendant une application), le
volume par le mélangeur puis par MPD (et son refus), une commande retenue, les signaux, le mode
sans serveur, la veille, la coupure de connexion et le redémarrage du broker.
