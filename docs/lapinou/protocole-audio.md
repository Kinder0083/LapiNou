<!-- SPDX-License-Identifier: MIT -->
# Protocole audio LapiNou (port TCP 9300)

Le d√©mon `lapinou-mic` du lapin envoie le micro au serveur LapiNou. Le sch√©ma est dans `userland/proto/lapinou-audio.proto`.

## Trames

Chaque trame est : 4 octets de longueur (big-endian), puis un message `LapinouAudio` s√©rialis√© de cette longueur. Le serveur refuse une trame de plus de 65536 octets.

- `rabbit_id` : contenu de `/usr/karotz/uuid.conf`. Il est identique dans toutes les trames de la connexion.
- `seq` : num√©ro de trame, +1 √† chaque trame. Un saut signale des trames perdues (tampon circulaire du lapin plein) ; le serveur le compte dans `RabbitStream.gaps`.
- `mulaw` : audio G.711 mu-law, 16 kHz, mono (un octet par √©chantillon).
- `first_sample` : index, depuis le d√©but du flux du lapin, du premier √©chantillon de `mulaw`. S'il manque, le serveur le d√©duit de la trame pr√©c√©dente.
- `mark` : rep√®re de lecture (`playback_id`, `at_sample`, `start`) : d√©but ou fin d'un son jou√© par le lapin, situ√© dans l'axe des √©chantillons du micro (sert √† l'annulation d'√©cho).

## Hello

Le Hello voyage dans le champ 6 de la premi√®re `LapinouAudio` de chaque connexion (`rabbit_id`, `firmware_version`, `sample_rate`). Cette premi√®re trame peut ne contenir que le Hello ou aussi de l'audio. Le serveur ferme la connexion si la premi√®re trame n'a pas de Hello ou si `sample_rate` n'est pas 16000.

## Reconnexion

Quand un lapin se reconnecte, le serveur garde le m√™me `RabbitStream` : `seq` repart de la valeur envoy√©e par le lapin, sans compter de perte entre deux connexions.

## C√¥t√© lapin (`lapinou-mic`)

`userland/src/usr/lapinou-mic/lapinou-mic.c` encode les trames √† la main (varints et champs de longueur, sans biblioth√®que protobuf).

- Le micro (`/dev/audio`, OSS mu-law 16 kHz mono) est lu par blocs d'au plus 2048 octets ; chaque bloc devient une trame audio (`seq` suivant, `first_sample` = √©chantillons d√©j√† captur√©s). Le gain `[audio] mic_gain_db` (12 dB par d√©faut, de -20 √† +30) est appliqu√© avant l'envoi : chaque octet est d√©cod√©, multipli√© (facteur en Q12), √©cr√™t√© et r√©encod√©, par une table de 256 octets.
- Le Hello voyage sur la plus ancienne trame en attente, ou seul dans une trame (avec son propre `seq`) s'il n'y en a pas.
- Les trames attendent dans une file de 2 s d'audio au plus ; au-del√†, les plus anciennes sont perdues (saut de `seq`). La trame en cours d'envoi lors d'une coupure est renvoy√©e en entier √† la connexion suivante.
- `mark_playback(playback_id, start)` (D-Bus `com.mindscape.karotz.LapinouMic`) ajoute une trame `mark` √† `at_sample` = √©chantillons d√©j√† captur√©s : ceux d√©j√† lus **plus** ceux encore dans le tampon du pilote (`SNDCTL_DSP_GETISPACE`, champ `bytes`). Incertitude restante pour l'annulation d'√©cho : le pilote ne compte que des fragments entiers (un fragment de plus ou de moins, quelques ms √† quelques dizaines de ms), et le d√©lai entre l'appel D-Bus et le premier son r√©ellement sorti du haut-parleur (tampon de lecture de `/dev/dsp`) n'est pas compris ; le serveur doit chercher l'alignement fin autour de `at_sample` (mesure pr√©vue sur le lapin).
- Reconnexion toutes les 2 s (5 fois), puis toutes les 10 s ; le compte ne repart de z√©ro qu'une fois qu'une connexion a envoy√© sa premi√®re trame et tenu 10 s (un serveur qui accepte puis ferme atteint le palier de 10 s). Toutes les adresses de l'h√¥te sont essay√©es ; une adresse IP est utilis√©e telle quelle, un nom est r√©solu dans un fil d'ex√©cution √† part (la boucle principale n'attend jamais le DNS).
- Coupure Wi-Fi : le tampon d'envoi TCP est r√©duit √† 16 Ko (‚âà 1 s d'audio) ; une connexion qui n'avance plus pendant 10 s alors que des trames attendent est abandonn√©e.
- Le m√™me audio (gain compris) est servi localement sur `/var/run/lapinou/mic.sock` (mode 0666). Un client trop lent est d√©connect√© : il doit se reconnecter.

## Clients locaux du micro

Quand `lapinou-mic` tourne, il est le seul ‡ ouvrir `/dev/audio` : `voice-daemon` (reconnaissance vocale) et `multimedia-daemon` (`record`) lisent le micro sur `/var/run/lapinou/mic.sock`. Ils n'ouvrent `/dev/audio` eux-mÍmes que si le fichier de la socket n'existe pas (pas de `lapinou-mic` sur ce lapin). Si le fichier existe mais que personne n'Ècoute (`lapinou-mic` arrÍtÈ ou en redÈmarrage), ils ne touchent pas ‡ `/dev/audio`.

- `voice-daemon` garde le format rÈseau historique de `asr_server.py` (mu-law 8 kHz : un octet sur deux du flux 16 kHz, par blocs de 1024 octets).
- `multimedia-daemon` convertit le mu-law en Èchantillons 16 bits non signÈs avant `flac`. Le gain de `lapinou-mic` (`mic_gain_db`) s'applique aussi ‡ ces deux clients.
- Lecture : au plus 1 s d'attente par lecture (`SO_RCVTIMEO`) ; sans octet pendant 1 s, le bloc en cours est complÈtÈ par du silence mu-law et la connexion est gardÈe. Si la connexion est perdue (client trop lent dÈconnectÈ, `lapinou-mic` redÈmarrÈ), le client se reconnecte ; le son perdu pendant la coupure est sautÈ (seul le bloc en cours est complÈtÈ par du silence). Tant que personne n'Ècoute, il rÈessaie toutes les 200 ms, 100 fois (20 s) : ensuite la session de reconnaissance se termine par un ÈvÈnement d'erreur (`<nomatch>`, ´ connection lost ª), et l'enregistrement par `multi_recordfinished` avec le code d'erreur.
- Marques de lecture : `karotz-player` (variable `LAPINOU_PLAYER_EVENTS_FD` donnÈe par `multimedia-daemon`, rien d'Ècrit sans elle) Ècrit une ligne sur ce descripteur ‡ chaque instant utile : `start` juste avant les premiers Èchantillons Ècrits sur `/dev/dsp`, `end` ‡ la fin ou au remplacement du son, `pause` et `resume`. `multimedia-daemon` en tire `mark_playback(id, true)` au `start` (ou au `resume`) et `mark_playback(id, false)` ‡ `end` (ou ‡ `pause`, ou quand le lecteur disparaÓt). Un `play` qui remplace un son en cours n'obtient son `start` que quand son propre son dÈmarre vraiment (aprËs un tÈlÈchargement, par exemple). `multimedia-daemon` suit la prÈsence de `LapinouMic` sur le bus par `NameOwnerChanged` ; sans `lapinou-mic`, il n'appelle rien et ne journalise rien. Les lectures de MPD et la synthËse vocale (`voice-daemon`) ne sont pas marquÈes.
