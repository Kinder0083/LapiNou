<!-- SPDX-License-Identifier: MIT -->
# Entraîner le mot d'activation « Lapinou »

Le serveur détecte « Lapinou » dans le micro de chaque lapin avec
`lapinou.voice.wakeword.WakeWord`, sur la chaîne d'[openWakeWord](https://github.com/dscripka/openWakeWord) :
PCM 16 kHz → mélspectrogramme → plongements de 96 valeurs toutes les 80 ms (modèles
`melspectrogram.onnx` et `embedding_model.onnx` d'openWakeWord) → notre classifieur
`server/models/lapinou_fr.onnx` sur les 16 derniers plongements (≈ 2 s d'audio).

Ce dossier refait ce classifieur. La recette tourne sur CPU (machine de développement :
4 cœurs, 8 Go) en quelques heures.

## Ce qui est dans le dépôt, ce qui ne l'est pas

| Fichier | Où | Licence |
|---|---|---|
| `server/models/lapinou_fr.onnx` (notre classifieur, < 1 Mo) | dépôt | MIT (entraîné ici) |
| `melspectrogram.onnx`, `embedding_model.onnx` | téléchargés dans `server/models/openwakeword/` (ignoré par git) par `uv run python -m lapinou.voice.wakeword download`, SHA-256 vérifié | ceux de la version v0.5.1 d'openWakeWord ; le code d'openWakeWord est Apache-2.0 |
| modèles « hey jarvis » & co. d'openWakeWord | **jamais** utilisés ni distribués | CC BY-NC-SA 4.0 |
| voix Piper, données d'entraînement | seulement sur la machine d'entraînement (`~/wakeword-train/`) | voir ci-dessous |
| `server/tests/data/*.wav` (voix de synthèse réservées au test) | dépôt | produits par `train.py export-tests` à partir des voix gilles (CC0), mls_1840 et mls (CC BY 4.0) |

## Voix Piper utilisées

Toutes viennent de [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) (`fr/fr_FR/…`).
Licence de chaque voix = celle de son jeu de données (fichier `MODEL_CARD` de la voix) :

| Voix | Locuteurs | Licence | Rôle |
|---|---|---|---|
| `fr_FR-siwis-medium`, `fr_FR-siwis-low` | 1 | CC BY 4.0 | entraînement |
| `fr_FR-upmc-medium` | 2 (Jessica, Pierre) | CC BY-SA 4.0 | entraînement |
| `fr_FR-mls-medium` | 125 (MLS) | CC BY 4.0 | entraînement (117 locuteurs) et test (8 locuteurs réservés, `MLS_HELD_OUT`) |
| `fr_FR-gilles-low` | 1 | CC0 | test seulement |
| `fr_FR-mls_1840-low` | 1 | CC BY 4.0 | test seulement (le locuteur 1840 est aussi exclu de mls-medium à l'entraînement) |
| `fr_FR-tom-medium` | 1 | AGPL-3.0 | **non utilisée** (licence) |

Ces voix ne servent qu'à fabriquer des exemples ; aucune n'est distribuée avec LapiNou.

## Recette

Sur la machine d'entraînement (Linux, Python 3.12) :

```sh
mkdir -p ~/wakeword-train/{voices,data} && cd ~/wakeword-train
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python piper-tts onnxruntime numpy scipy onnx
uv pip install -p .venv/bin/python torch --index-url https://download.pytorch.org/whl/cpu

# Voix Piper (≈ 420 Mo)
HF=https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR
for v in siwis/medium/fr_FR-siwis-medium siwis/low/fr_FR-siwis-low upmc/medium/fr_FR-upmc-medium \
         mls/medium/fr_FR-mls-medium gilles/low/fr_FR-gilles-low mls_1840/low/fr_FR-mls_1840-low; do
  curl -sSL -o voices/$(basename $v).onnx $HF/$v.onnx
  curl -sSL -o voices/$(basename $v).onnx.json $HF/$v.onnx.json
done

# Négatifs pré-calculés d'openWakeWord (plongements, pas d'audio) :
#  - validation (≈ 11 h, 185 Mo) pour mesurer les fausses détections ;
#  - le début (1,5 Go sur 17 Go, ≈ 512 000 fenêtres) des 2 000 h d'ACAV100M pour l'entraînement.
OWW=https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main
curl -sSL -o data/validation_set_features.npy $OWW/validation_set_features.npy
curl -sSL -r 0-1572864127 -o data/acav_part.bin $OWW/openwakeword_features_ACAV100M_2000_hrs_16bit.npy

# Depuis server/ d'une copie du dépôt. Mémoire plafonnée (la machine de développement
# partage l'hôte avec d'autres VM) : chaque étape dans un scope systemd à 4,5 Go sans swap.
uv run python -m lapinou.voice.wakeword download     # modèles de caractéristiques
T="sudo systemd-run --scope -q --uid=$USER -p MemoryMax=4500M -p MemorySwapMax=0    $HOME/wakeword-train/.venv/bin/python tools/train_wakeword/train.py --jobs 3"
$T generate --voices ~/wakeword-train/voices --mix-positives 9000   # synthèse (≈ 9 h, pic 1,3 Go)
$T features                                          # augmentation + plongements (≈ 1 h, pic 2,9 Go)
$T train --acav ~/wakeword-train/data/acav_part.bin --dropout 0.3 --noise 0.3 --neg-weight 20  # ≈ 20 min, pic 3,9 Go
$T evaluate --validation ~/wakeword-train/data/validation_set_features.npy
$T export-tests --voices ~/wakeword-train/voices     # server/tests/data/*.wav
uv run pytest -q tests/test_wakeword.py
```

`train` écrit `server/models/lapinou_fr.onnx`. Choisir ensuite le seuil d'après
`evaluate` (règle utilisée : le plus bas seuil à ≤ 5 fausses détections par heure sur la
validation openWakeWord) et le reporter dans `VoiceConfig.wake_threshold`
(`server/lapinou/config.py`) et `VALIDATED_THRESHOLD` (`tests/test_wakeword.py`).

### Ce que fait chaque étape

- **generate** : synthèse Piper. Positifs : « Lapinou » seul ou après un mot (« Hé
  Lapinou », « Dis Lapinou », « Il a dit Lapinou. »…), quelques graphies (« Lâpinou »,
  « Lapinoû ») ; débit (`length_scale` 0,75 à 1,4), bruit du générateur, durée des
  phonèmes et hauteur (± 10 %, par rééchantillonnage) tirés au hasard. **Voix mélangées** :
  3 000 locuteurs nouveaux obtenus en interpolant deux à deux les plongements de locuteur
  des 117 locuteurs MLS d'entraînement (lignes ajoutées à `emb_g.weight`, comme
  piper-sample-generator) ; fichiers `mix_*.npz`. Négatifs : mots
  proches (« lapin », « lapereau », « papinou », « lapinot », « minou », « pinou »,
  « la pinède »…) et ≈ 3 100 phrases (une liste écrite à la main + des phrases
  combinées), une sur cinq réservée au test. Les voix low et MLS dérapent souvent
  (phonème de plusieurs secondes, babillage) : les alignements phonème/audio de Piper
  servent à couper chaque extrait à la fin du dernier phonème et à refuser ceux dont
  « Lapinou » ne dure pas 0,25 à 0,95 s.
- **features** : chaque extrait est posé sur un fond (silence numérique, bruit blanc/rose/brun,
  ronflement secteur, paroles lointaines) à un rapport signal/bruit de 10 à 40 dB (8 à 25 dB
  pour les paroles), avec réverbération synthétique (40 %),
  filtrage passe-haut/passe-bas (50 %), niveau de −32 à −2 dBFS et aller-retour mu-law
  8 bits (70 %, le format du flux des lapins). Les plongements sont calculés par le code
  du serveur (`lapinou.voice.wakeword.clip_features`), donc exactement comme en service.
  Positifs : les 4 fenêtres qui finissent 0 à 240 ms après la fin du mot. Négatifs :
  toutes les fenêtres (pas de 160 ms) des phrases, des fonds seuls, les fenêtres
  autour de la fin des mots proches et de phrases isolées suivies de silence.
- **train** : réseau dense 1536 → 128 → 128 → 1 (LayerNorm, ReLU, dropout), lots moitié
  positifs moitié négatifs, poids des négatifs croissant de 1 à 20, puis moitié des
  négatifs pris parmi les plus difficiles. Négatifs : nos phrases et mots proches + 300 000
  fenêtres ACAV100M (parole anglaise surtout, musique, bruits).
- **evaluate** : rappel sur les voix réservées (augmentées comme à l'entraînement),
  fausses détections par heure sur les phrases réservées et sur la validation
  openWakeWord (≈ 11 h), taux de déclenchement sur les mots proches.

### Exemples réels (à faire avec les lapins, Tâche 27)

La synthèse ne remplace pas le vrai micro du lapin. Pour ajouter des négatifs réels :

```sh
# serveur LapiNou arrêté (le port 9300 doit être libre)
uv run python tools/train_wakeword/record_negatives.py --rabbit salon --minutes 120 --out ~/wakeword-train/real
$T features --real-negatives ~/wakeword-train/real   # après avoir effacé work/features/train_real.npy
$T train --acav ~/wakeword-train/data/acav_part.bin
```

Enregistrer la télé, la radio, des conversations et le lapin qui parle lui-même, sans
jamais dire « Lapinou ». Chaque étape réutilise ce qui existe déjà dans `--work` : effacer
le fichier concerné pour le refaire.

## Différences avec la recette officielle d'openWakeWord, limites

La recette officielle (`openwakeword/train.py` et son carnet) génère des dizaines de
milliers d'exemples avec un modèle Piper anglais multi-locuteurs sur GPU, ajoute des
réponses impulsionnelles réelles (MIT RIR), des bruits et musiques (AudioSet, FMA) et
entraîne sur les 2 000 h de négatifs ACAV100M (17 Go). Ici, sur CPU et en français :

- moins de locuteurs (≈ 120 voix de synthèse françaises, dont beaucoup de qualité MLS
  moyenne) et des positifs uniquement synthétiques ;
- réverbération et bruits synthétiques au lieu d'enregistrements réels ;
- 300 000 fenêtres ACAV100M (≈ 6 % du jeu complet) ;
- « la pinou » n'est pas un négatif (décision R16 du projet) : c'est un homophone exact de
  « Lapinou » (/lapinu/), Piper le prononce à l'identique, et l'apprendre comme négatif
  contredirait les positifs. Le modèle se déclenchera donc sur « la pinou ».

Conséquences attendues : moins robuste aux accents, aux enfants et aux pièces très
réverbérantes qu'un modèle entraîné sur de vraies voix ; à reprendre avec des
enregistrements réels des lapins (`record_negatives.py`, et des « Lapinou » réels comme
positifs).

## Résultats du modèle livré (2026-11-16) : objectifs NON atteints

Données : 6 158 positifs (3 316 voix d'origine + 2 842 voix mélangées), 3 029 mots proches,
4 973 phrases (≈ 5 h 30), 300 000 fenêtres ACAV100M. Modèle : 1536 → 128 → 128 → 1,
dropout 0,3, bruit 0,3 sur les plongements, poids des négatifs 20, 30 000 pas.

`evaluate` (voix réservées, extraits augmentés ; validation openWakeWord ≈ 11 h) :

| seuil | rappel voix réservées | mots proches déclenchés | phrases réservées (FD/h) | validation oWW (FD/h) |
|---|---|---|---|---|
| 0,5 | 0,39 | 0,12 | 95 | 66 |
| 0,7 | 0,25 | 0,03 | 34 | 24 |
| 0,8 | 0,22 | 0,02 | 20 | 14 |
| **0,9 (défaut)** | 0,22 | 0,01 | 5,1 | 5,0 |
| 0,95 | 0,18 | 0 | 1,7 | 1,9 |

Fichiers de test (`tests/data`, voix réservées, 12,1 min de négatifs) : seuil 0,5 → 5/10
positifs, 17 fausses détections ; 0,8 → 5/10, 4 ; **0,9 → 4/10, 3** ; 0,95 → 2/10, 1.
Objectif (≥ 9/10 et 0 fausse détection) non atteint : les deux tests correspondants sont
`xfail(strict=True)`.

Diagnostic : le classifieur apprend les locuteurs, pas le mot. Sur des voix jamais vues
(locuteurs MLS réservés, voix mélangées, gilles, mls_1840), « Lapinou » et les mots
proches — même très différents (« lavabo », « nounours ») — reçoivent des scores
semblables. Une sonde linéaire sur les plongements d'extraits propres sépare « Lapinou »
des mots distincts à ≈ 0,97 sur les voix d'entraînement mais ≈ 0,72 sur des voix
nouvelles. Ni la régularisation (taille 32 à 128, dropout 0,5, bruit), ni les 3 000 voix
mélangées, ni les négatifs « phrase isolée », ni le rapport signal/bruit corrigé n'ont
changé cet ordre de grandeur. Hypothèses : voix Piper françaises trop peu nombreuses et
de qualité inégale (MLS : 70 % des « Lapinou » refusés par le contrôle d'alignement),
plongement openWakeWord (appris sur de l'anglais) peu discriminant pour ces voix. Pistes :
vrais enregistrements de « Lapinou » (plusieurs personnes) et négatifs réels du lapin
(`record_negatives.py`, tâche 27), filtrage des extraits par reconnaissance vocale,
beaucoup plus de positifs.
