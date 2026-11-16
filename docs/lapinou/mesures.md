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

- Piper est GPL-3.0-or-later (paquet `piper-tts`) : extra optionnel `uv sync --extra piper`, non
  installé par défaut ; le serveur MIT l'importe seulement à l'exécution (R24). L'installateur
  devra l'installer (ou choisir Kokoro, plus lent).
- Les voix Piper vont dans `~/.cache/lapinou/piper/` (`<voix>.onnx` et `<voix>.onnx.json`, de
  `huggingface.co/rhasspy/piper-voices`) ; les modèles Kokoro dans `~/.cache/lapinlibre/kokoro/`
  (vérifiés par SHA-256 par `KokoroEngine`).
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
