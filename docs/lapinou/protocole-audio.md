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
- `mark_playback(playback_id, start)` (D-Bus `com.mindscape.karotz.LapinouMic`) ajoute une trame `mark` à `at_sample` = échantillons déjà capturés.
- Reconnexion toutes les 2 s (5 fois), puis toutes les 10 s ; une connexion qui n'avance plus pendant 10 s est abandonnée.
- Le même audio (gain compris) est servi localement sur `/var/run/lapinou/mic.sock`.
