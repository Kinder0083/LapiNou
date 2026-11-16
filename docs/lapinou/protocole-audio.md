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

Quand `lapinou-mic` tourne, il est le seul à ouvrir `/dev/audio` : `voice-daemon` (reconnaissance vocale) et `multimedia-daemon` (`record`) lisent le micro sur `/var/run/lapinou/mic.sock`. Ils n'ouvrent `/dev/audio` eux-mêmes que si le fichier de la socket n'existe pas (pas de `lapinou-mic` sur ce lapin). Si le fichier existe mais que personne n'écoute (`lapinou-mic` arrêté ou en redémarrage), ils ne touchent pas à `/dev/audio`.

- `voice-daemon` garde le format réseau historique de `asr_server.py` (mu-law 8 kHz : un octet sur deux du flux 16 kHz, par blocs de 1024 octets).
- `multimedia-daemon` convertit le mu-law en échantillons 16 bits non signés avant `flac`. Le gain de `lapinou-mic` (`mic_gain_db`) s'applique aussi à ces deux clients.
- Lecture : au plus 1 s d'attente par lecture (`SO_RCVTIMEO`) ; sans octet pendant 1 s, le bloc en cours est complété par du silence mu-law et la connexion est gardée. Si la connexion est perdue (client trop lent déconnecté, `lapinou-mic` redémarré), le client se reconnecte ; le son perdu pendant la coupure est sauté (seul le bloc en cours est complété par du silence). Tant que personne n'écoute, il réessaie toutes les 200 ms, 100 fois (20 s) : ensuite la session de reconnaissance se termine par un événement d'erreur (`<nomatch>`, « connection lost »), et l'enregistrement par `multi_recordfinished` avec le code d'erreur.
- Marques de lecture : `karotz-player` (variable `LAPINOU_PLAYER_EVENTS_FD` donnée par `multimedia-daemon`, rien d'écrit sans elle) écrit une ligne sur ce descripteur à chaque instant utile : `start` juste avant les premiers échantillons écrits sur `/dev/dsp`, `end` à la fin ou au remplacement du son, `pause` et `resume`. `multimedia-daemon` en tire `mark_playback(id, true)` au `start` (ou au `resume`) et `mark_playback(id, false)` à `end` (ou à `pause`, ou quand le lecteur disparaît). Un `play` qui remplace un son en cours n'obtient son `start` que quand son propre son démarre vraiment (après un téléchargement, par exemple). `multimedia-daemon` suit la présence de `LapinouMic` sur le bus par `NameOwnerChanged` ; sans `lapinou-mic`, il n'appelle rien et ne journalise rien. Les lectures de MPD et la synthèse vocale (`voice-daemon`) ne sont pas marquées.
