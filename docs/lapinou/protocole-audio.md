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
