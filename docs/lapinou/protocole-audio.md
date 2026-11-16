<!-- SPDX-License-Identifier: MIT -->
# Protocole audio LapiNou (port TCP 9300)

Le démon `lapinou-mic` du lapin envoie le micro au serveur LapiNou. Le schéma est dans `userland/proto/lapinou-audio.proto`.

## Trames

Chaque trame est : 4 octets de longueur (big-endian), puis un message `LapinouAudio` sérialisé de cette longueur. Le serveur refuse une trame de plus de 65536 octets.

- `rabbit_id` : contenu de `/usr/karotz/uuid.conf`. Il est identique dans toutes les trames de la connexion.
- `seq` : numéro de trame, +1 à chaque trame. Un saut signale des trames perdues (tampon circulaire du lapin plein) ; le serveur le compte dans `RabbitStream.gaps`.
- `mulaw` : audio G.711 mu-law, 16 kHz, mono (un octet par échantillon).
- `first_sample` : index, depuis le début du flux du lapin, du premier échantillon de `mulaw`. S'il manque, le serveur le déduit de la trame précédente.
- `mark` : repère de lecture (`playback_id`, `at_sample`, `start`) : début ou fin d'un son joué par le lapin, situé dans l'axe des échantillons du micro (sert à l'annulation d'écho).

## Hello

Le Hello voyage dans le champ 6 de la première `LapinouAudio` de chaque connexion (`rabbit_id`, `firmware_version`, `sample_rate`). Cette première trame peut ne contenir que le Hello ou aussi de l'audio. Le serveur ferme la connexion si la première trame n'a pas de Hello ou si `sample_rate` n'est pas 16000.

## Reconnexion

Quand un lapin se reconnecte, le serveur garde le même `RabbitStream` : `seq` repart de la valeur envoyée par le lapin, sans compter de perte entre deux connexions.

## Côté lapin (`lapinou-mic`)

`userland/src/usr/lapinou-mic/lapinou-mic.c` encode les trames à la main (varints et champs de longueur, sans bibliothèque protobuf).

- Le micro (`/dev/audio`, OSS mu-law 16 kHz mono) est lu par blocs d'au plus 2048 octets ; chaque bloc devient une trame audio (`seq` suivant, `first_sample` = échantillons déjà capturés). Le gain `[audio] mic_gain_db` (12 dB par défaut, de -20 à +30) est appliqué avant l'envoi : chaque octet est décodé, multiplié (facteur en Q12), écrêté et réencodé, par une table de 256 octets.
- Le Hello voyage sur la plus ancienne trame en attente, ou seul dans une trame (avec son propre `seq`) s'il n'y en a pas.
- Les trames attendent dans une file de 2 s d'audio au plus ; au-delà, les plus anciennes sont perdues (saut de `seq`). La trame en cours d'envoi lors d'une coupure est renvoyée en entier à la connexion suivante.
- `mark_playback(playback_id, start)` (D-Bus `com.mindscape.karotz.LapinouMic`) ajoute une trame `mark` à `at_sample` = échantillons déjà capturés : ceux déjà lus **plus** ceux encore dans le tampon du pilote (`SNDCTL_DSP_GETISPACE`, champ `bytes`). Incertitude restante pour l'annulation d'écho : le pilote ne compte que des fragments entiers (un fragment de plus ou de moins, quelques ms à quelques dizaines de ms), et le délai entre l'appel D-Bus et le premier son réellement sorti du haut-parleur (tampon de lecture de `/dev/dsp`) n'est pas compris ; le serveur doit chercher l'alignement fin autour de `at_sample` (mesure prévue sur le lapin).
- Reconnexion toutes les 2 s (5 fois), puis toutes les 10 s ; le compte ne repart de zéro qu'une fois qu'une connexion a envoyé sa première trame et tenu 10 s (un serveur qui accepte puis ferme atteint le palier de 10 s). Toutes les adresses de l'hôte sont essayées ; une adresse IP est utilisée telle quelle, un nom est résolu dans un fil d'exécution à part (la boucle principale n'attend jamais le DNS).
- Coupure Wi-Fi : le tampon d'envoi TCP est réduit à 16 Ko (≈ 1 s d'audio) ; une connexion qui n'avance plus pendant 10 s alors que des trames attendent est abandonnée.
- Le même audio (gain compris) est servi localement sur `/var/run/lapinou/mic.sock` (mode 0666). Un client trop lent est déconnecté : il doit se reconnecter.

## Clients locaux du micro

Quand `lapinou-mic` tourne, il est le seul à ouvrir `/dev/audio` : `voice-daemon` (reconnaissance vocale) et `multimedia-daemon` (`record`) lisent le micro sur `/var/run/lapinou/mic.sock` ; sans cette socket, ils ouvrent `/dev/audio` comme avant.

- `voice-daemon` garde le format réseau historique de `asr_server.py` (mu-law 8 kHz : un octet sur deux du flux 16 kHz, par blocs de 1024 octets).
- `multimedia-daemon` convertit le mu-law en échantillons 16 bits non signés avant `flac`. Le gain de `lapinou-mic` (`mic_gain_db`) s'applique aussi à ces deux clients.
- Si `lapinou-mic` déconnecte un client trop lent, le client se reconnecte à la socket et comble le trou par du silence.
- À chaque `Multimedia.play(id, …)` joué par `karotz-player`, `multimedia-daemon` appelle `mark_playback(id, true)` dès que le lecteur a ouvert `/dev/dsp` (sondé toutes les 100 ms dans `/proc/<pid>/fd`), puis `mark_playback(id, false)` à la fin ou à l'arrêt du son (et quand un nouveau `play` remplace le son en cours). Si `lapinou-mic` n'est pas sur le bus, rien n'est appelé et rien n'est journalisé. Les lectures de MPD et la synthèse vocale (`voice-daemon`) ne sont pas marquées.
