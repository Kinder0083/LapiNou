<!-- SPDX-License-Identifier: MIT -->
# Mesures LapiNou

## Synthèse vocale (Tâche 19)

Mesures sur la VM de développement (Ubuntu 24.04, 4 vCPU x86-64 sans GPU, ONNX Runtime CPU,
une seule synthèse à la fois, mémoire plafonnée à 3,5 Go). Texte : « Bonjour, je suis
Lapinou. » / « Il fait douze degrés dehors et le ciel est dégagé. » / « Veux-tu que j'allume
la lumière du salon ? » ; chaque phrase est synthétisée séparément (`split_sentences`), après
une phrase d'échauffement. « Première phrase » = temps entre `speak()` et le MP3 + PCM prêts
pour « Bonjour, je suis Lapinou. » (moyenne de 3 essais, MP3 et décodage ffmpeg compris).

| Moteur / voix | Licence (voix) | Chargement | Première phrase | 3 phrases | Facteur temps réel | Fréquence MP3 | Taille MP3 (1re phrase) |
|---|---|---|---|---|---|---|---|
| Kokoro `ff_siwis` | poids Apache-2.0 (données SIWIS, CC BY 4.0) | 2,4 s | 2,95 s | 10,1 s pour 6,6 s | 1,53 | 24 kHz | 12,7 ko |
| Piper `fr_FR-siwis-medium` | CC BY 4.0 | 0,7 s | 0,42 s | 1,4 s pour 6,9 s | 0,20 | 22,05 kHz | 15,5 ko |
| Piper `fr_FR-upmc-medium` | **CC BY-SA 4.0** | 1,0 s | 0,54 s | 1,9 s pour 7,4 s | 0,26 | 22,05 kHz | 14,6 ko |

Pic de mémoire du processus de mesure : 661 Mo (les trois moteurs chargés l'un après l'autre).

**Choix par défaut (provisoire) : Piper `fr_FR-siwis-medium`** (c'est déjà la valeur par défaut
de `settings.voice`). Sur CPU, Kokoro met près de 3 s à rendre la première phrase (plus lent
que le temps réel : la deuxième phrase attendrait la fin de la première) ; Piper siwis la rend
en 0,4 s, avec une licence sans « share-alike » (upmc est CC BY-SA, plus lent, sans gain net
de qualité attendu : non retenue). L'écoute des trois voix sur le vrai lapin décidera
définitivement (Tâche 27) : `[voice] tts_engine = "kokoro"` / `"piper"` et `voice = "ff_siwis"` /
`"fr_FR-siwis-medium"` / `"fr_FR-upmc-medium"` dans `config.toml`, sans autre changement.

Remarques :

- Piper (`piper-tts`) est GPL-3.0-or-later (R30) : le serveur MIT ne l'importe jamais. Il tourne
  dans un processus séparé et durable (`server/lapinou/voice/piper_worker.py`, texte en entrée,
  PCM en sortie sur stdin/stdout, relancé s'il meurt) exécuté par le Python d'un venv à part,
  `~/.local/share/lapinou/piper-venv`, créé par `python -m lapinou.voice.models piper-setup`
  (uv si présent, sinon venv + pip ; `piper-tts==1.8.0`). Le processus ajoute environ 0,4 s de
  chargement du modèle à la première phrase seulement ; la communication par tubes est négligeable.
- Installation (Tâche 25) : `uv run python -m lapinou.voice.models fetch` télécharge les modèles
  de transcription et la voix Piper `fr_FR-siwis-medium` (`~/.cache/lapinou/piper/`, révision
  `e9ab671f` de rhasspy/piper-voices, SHA-256 vérifiés au téléchargement et à chaque chargement) ;
  `... models piper-setup` crée le venv Piper. Les modèles Kokoro vont dans
  `~/.cache/lapinlibre/kokoro/` (vérifiés par SHA-256 par `KokoroEngine`). Seule la voix
  `fr_FR-siwis-medium` est épinglée : une autre voix Piper demande d'ajouter ses SHA-256 à
  `tts.PIPER_VOICES`.
- **ffmpeg est un prérequis** de l'installateur et de l'image Docker : `Voice.load()` et `speak()`
  s'en servent pour décoder le MP3 en PCM de référence et refusent de démarrer sans lui (le
  serveur de compatibilité du port 8081 n'en a pas besoin).
- Kokoro ne tient pas le temps réel sur ces 4 vCPU ; sur une machine plus rapide ou avec
  GPU cela peut changer.

### Alignement du PCM de référence sur le MP3

`Speech.pcm16k` est le décodage ffmpeg (mono, 16 kHz, int16) du MP3 effectivement servi. Mesure
(corrélation croisée entre la synthèse rééchantillonnée et ce décodage) : retard de 46 ms (Kokoro,
24 kHz) à 50 ms (Piper, 22,05 kHz), et durée décodée de 70 ms de plus que la synthèse (retard +
bourrage de la dernière trame). lameenc n'écrit pas d'en-tête LAME/Xing, donc un décodeur qui ne
connaît pas ce retard (libmad, madplay) le joue : le PCM de référence l'inclut. À vérifier sur
le lapin avec karotz-player (Tâche 27) : si le lecteur, lui, retire ce retard, il faudra
décaler la référence de 46 à 50 ms.

Le MP3 garde la fréquence du moteur (pas de rééchantillonnage côté serveur ni côté lapin), à
64 kbit/s, comme `tts_server.py` de LapinLibre.

## Voix claire (R51)

`[voice] clarity = true` (par défaut) fait passer le WAV du moteur par ffmpeg avant l'encodage MP3,
à la même fréquence, avec le filtre `tts.CLARITY_FILTER` :
`highpass=f=180,equalizer=f=250:t=q:w=1:g=-4,equalizer=f=3200:t=q:w=1.2:g=3` (grondement coupé
sous 180 Hz, zone « caverneuse » de 250 Hz baissée de 4 dB, clarté de 3,2 kHz remontée de 3 dB).

| Piper `fr_FR-siwis-medium`, « Bonjour, je suis Lapinou. » | Temps (moyenne de 3, après échauffement) |
|---|---|
| `clarity = false` | 135 ms |
| `clarity = true` | 218 ms (+82 ms : un lancement de ffmpeg) |

Mesuré sur la VM de construction (4 vCPU), pas sur le conteneur du serveur. Si ffmpeg manque
ou échoue, un avertissement « voix claire impossible » est écrit et la phrase garde la voix du
moteur.
