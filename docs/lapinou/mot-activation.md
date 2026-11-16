<!-- SPDX-License-Identifier: MIT -->
# Mot d'activation « Lapinou »

Le serveur LapiNou écoute le micro de chaque lapin (flux 16 kHz, voir `protocole-audio.md`) et
attend qu'on dise « Lapinou ». La méthode principale est la **transcription** (décision R23) ;
le classifieur acoustique openWakeWord reste possible en option.

| `voice.wake_method` | Ce qui se passe |
|---|---|
| `asr` (défaut) | Silero VAD découpe la parole, Parakeet transcrit chaque segment, le nom est cherché au début du texte. |
| `both` | Comme `asr`, mais un segment n'est transcrit que si le classifieur openWakeWord (`voice.wake_model`) s'est déclenché pendant ce segment. Sans `wake_model` : `asr` seul, avec un avertissement. Économise du calcul, au prix du rappel du classifieur. |
| `oww` | Classifieur openWakeWord seul (`lapinou.voice.wakeword.load_wakeword`). Il faut l'entraîner (`server/tools/train_wakeword/README.md`) ; les objectifs de précision n'ont pas été atteints. |

## Mise en place de la méthode `asr`

```
cd server && uv sync                                # installe sherpa-onnx 2.0.1
uv run python -m lapinou.voice.models fetch [--dir D]   # Parakeet + Silero, ~640 Mo, SHA-256 vérifié
```

`lapinou.voice.models` est LE moyen de les installer : il vérifie les SHA-256 que le `check_models()`
de LapinLibre attend. Ils vont dans `~/.cache/lapinlibre/asr`. Sans eux, les tests de détection
réels sont ignorés (`skip`) et `wake_asr.models_present()` rend `False` (simple présence des
fichiers ; les SHA-256 sont vérifiés au chargement par `load_recogniser()`).
`asr_server.py --fetch` de LapinLibre n'est pas modifié mais ne fonctionne plus (voir en bas).

Le code est `server/lapinou/voice/wake_asr.py` : `AsrWakeDetector(transcriber, names=["lapinou"],
max_segment_s=6.0)`, un par lapin, partageant un seul `Recogniser` (`load_recogniser()`, ~0,9 Go de
mémoire). `feed(pcm_int16)` rend une liste de `WakeHit(at_sample, text, command)` :

- `at_sample` : début du segment de parole, en échantillons depuis la création du détecteur
  (`reset()` ne remet pas le compteur à zéro) ;
- `text` : la transcription complète ; `command` : ce qui suit le nom dans le même segment, vide
  si l'utilisateur s'est tu après « Lapinou » (le serveur écoute alors la suite).

Une parole de plus de `max_segment_s` (6 s) est transcrite par morceaux : seul le premier est
comparé au nom (un « lapinou » au milieu d'une longue phrase ne déclenche rien) et les
morceaux suivants, contigus, complètent `command` et `text` du **même** objet `WakeHit`, dont
`more` reste vrai tant qu'un morceau peut encore suivre. Les morceaux d'une parole sans nom
ne sont pas transcrits.

`feed()` transcrit dans l'appel : on le lance dans un fil (`asyncio.to_thread`), un détecteur
n'est pas partagé entre fils. Un segment n'est rendu qu'après `voice.vad_silence_ms` (700 ms) de
silence : la détection arrive donc environ 0,7 s après la fin de la phrase, plus le temps de
transcription.

## Ce qui est reconnu

Le texte est mis en minuscules, sans accents ni ponctuation. Le nom doit être au début :

- éventuellement après un mot court (`hé`, `eh`, `dis`, `dit`, `ok`, `et`, `salut`, `coucou`…) ;
- avec une faute d'édition au plus sur le nom (7 lettres) : `lapino`, `lapinot`, `lapinoux`,
  `lapinau` ;
- en un à trois mots collés : `la pinou`, `lapi nou`, `l'apinou`, `la pi nou`.

Un nom en plusieurs mots n'est accepté que sans ponctuation entre eux (« Lapin, où es-tu ? » ne
déclenche rien), et « lapin ou/où » est refusé (« Lapin ou lièvre ? »). Jamais au milieu d'une phrase : « mon lapinou est mignon », « le lapin mange… », « la pinède » ne
déclenchent rien. Deux fautes ne passent pas (`lapin`, `lapins`).

Parakeet n'a pas « Lapinou » dans son vocabulaire : il l'écrit `Lapinou`, `Lapinot`, `La Pinou`,
`L'apinou`, `D'apinou`… (tous reconnus), parfois autre chose (`Dila Pinou`, `Pinot` tout seul)
ou rien (non reconnus : le rappel n'est pas de 100 %).

## Confidentialité

L'audio et les transcriptions des segments sans mot d'activation ne sont jamais journalisés
ni stockés (ils ne quittent pas la mémoire du détecteur). L'orchestrateur ne doit pas
journaliser `WakeHit.text` ni `WakeHit.command` : ils contiennent ce que dit l'utilisateur.

## Mesures

Machine : VM de développement, Intel Core i3-1215U, 4 cœurs, 6 Go, Ubuntu 24.04, Parakeet sur
2 fils, mémoire plafonnée (`systemd-run --scope -p MemoryMax=3500M -p MemorySwapMax=0`). Les
mesures ont été faites alors que la VM compilait et testait le firmware en parallèle (charge 3 à 7
sur 4 cœurs) : ce sont des valeurs pessimistes.

| Scénario (`tools/bench_wake_asr.py`) | Audio | Calcul | Facteur temps réel | Mémoire max. |
|---|---|---|---|---|
| Fond sonore sans parole (VAD seul), 1 lapin | 10 min | 2,9 s | 0,005 (211x) | 0,93 Go |
| Parole continue (négatifs de test, 92 % de parole), 1 lapin, 2 fils | 12,5 min | 185 s | 0,25 (4x) | 1,0 Go |
| Parole continue, 3 lapins en même temps, 2 fils | 3 x 4 min | 147 s | 0,20 au total ; chaque flux va 1,6x plus vite que le temps réel | 1,0 Go |
| Parole continue, 1 lapin, 4 fils (charge de la VM plus forte) | 4 min | 87 s | 0,35 | 1,0 Go |

- Chargement des modèles : 2 à 5 s, ~0,9 Go de mémoire (une fois, partagés par tous les lapins).
  Un détecteur de plus ne coûte que son VAD (quelques Mo).
- Le coût suit la parole : le VAD seul tourne 200 fois plus vite que le temps réel, et Parakeet
  ne transcrit que les segments de parole. Les négatifs de test sont le pire cas (parole sans
  pause) ; dans une maison la parole est une faible part de la journée, le coût moyen est donc
  bien plus proche de la ligne « fond sonore » (non mesuré en conditions réelles).
- Latence : la détection est rendue environ 0,7 s après la fin de la phrase (silence du VAD),
  plus la transcription du segment (facteur 0,25 environ, soit 0,3 à 1 s pour une phrase de
  2 à 4 s, sur cette machine chargée).
- `max_segment_s` (6 s) borne la durée transcrite en une fois ; la mémoire de Parakeet ne
  grandit pas avec la parole.

À reproduire : `cd server && systemd-run --user --scope -p MemoryMax=3500M -p MemorySwapMax=0
uv run python tools/bench_wake_asr.py --threads 2 [--rabbits N]`.

### Précision (tests `server/tests/test_wake_asr.py`, voix de synthèse)

| Test (voix de synthèse Piper) | Résultat |
|---|---|
| Nom + commande, voix siwis (nette) : « Lapinou, allume le salon », « Hé Lapinou, quelle heure est-il ? », « Dis Lapinou, éteins la lumière… » | 3/3 détectés, commande exacte (sauf « éteins » entendu « était ») |
| Mêmes phrases, voix gilles (« low ») | 3/3 détectés ; une fois le nom seul (pause longue : la commande est dans le segment suivant) |
| « Lapinou. » seul, 2 voix | 2/2, commande vide |
| Pièges : « Mon lapinou est mignon », « Le lapin mange une carotte… », « …dans la pinède… », 2 voix chacun | 0/6 détection |
| Enregistrement français de LapinLibre (`test_wavs/fr.wav`) | 0 détection |
| 12,5 min de négatifs (parole française, 3 voix, retournée + bruit rose, ralentie, accélérée) | 0 détection |
| « Lapinou » seul des tests du classifieur (`lapinou_*.wav`, voix « low » bruitées) | 2/10 : la transcription est mauvaise sur ces voix (`Tell me`, `Uh uh`, …) ; plancher de test : 2 |

Ces fichiers sont de la synthèse vocale Piper, pas de vrais lapins : la précision réelle (micro
faible du lapin, accents, bruit de la maison, voix d'enfants) reste à mesurer avec des
enregistrements réels (tâche 27).

## Pistes si le rappel est insuffisant

- Mots-clés (« hotwords ») de sherpa-onnx pour pousser « lapinou » dans le décodage : demande un
  `Recogniser` configuré autrement que celui de LapinLibre (non modifié ici).
- Pré-filtre openWakeWord (`both`) entraîné avec de vrais enregistrements : il garde les
  transcriptions de segments sans le mot, il ne rattrape pas un mot mal transcrit.
- Ajouter d'autres noms à `names` (par exemple un second nom pour le lapin) ; la même tolérance
  s'applique à chacun.

## Intégrité des modèles (à savoir)

Le 2026-11-16, `asr_server.py --fetch` échoue : l'archive Parakeet de la release GitHub a été
republiée (SHA-256 `1495e91b…` au lieu de `5793d0fd…`), avec d'autres encodeur, décodeur et
joiner (modèle réexporté en novembre 2026 ; `tokens.txt` est identique). Les SHA-256 d'origine
des trois fichiers sont ceux de la révision `2bda32ec70b097a55adaa07d9a7173915b43cc78` du dépôt
Hugging Face `csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8` : `lapinou.voice.models`
télécharge de cette révision, avec les pins de LapinLibre importés (R25).
