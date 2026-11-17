<!-- SPDX-License-Identifier: MIT -->
# Licences

Le code de LapiNou est sous licence MIT, comme celui de LapinLibre (fichier `LICENSE` et
`LICENSES/`, exceptions dans le `README.md`). Les modèles ci-dessous ne sont pas dans le dépôt :
ils sont téléchargés (empreinte SHA-256 vérifiée) sur le serveur ou par l'installateur, et
gardent leur propre licence.

## Licences des modèles

| Modèle | Rôle | Licence | Source (déjà citée dans le dépôt) |
|---|---|---|---|
| Parakeet TDT 0.6B v3 (int8, sherpa-onnx) | transcription (serveur, mot d'activation par transcription) | **CC-BY-4.0** | `docs/app-server.md` (« Parakeet v3 is under CC-BY-4.0 »), `server/lapinou/voice/models.py` |
| Silero VAD | détection de parole | **MIT** | `docs/app-server.md` (« Silero VAD under MIT ») |
| Piper (`piper-tts`, le programme) | synthèse vocale, dans un venv séparé jamais importé par le serveur (R30) | **GPL-3.0-or-later** | `server/lapinou/voice/tts.py`, `docs/lapinou/mesures.md` |
| Voix Piper `fr_FR-siwis-medium` (voix par défaut ; aussi le son « pas de serveur » du lapin) | voix de Lapinou | **CC BY 4.0** (données SIWIS) | `server/lapinou/voice/tts.py`, `README.md`, `server/tools/train_wakeword/README.md` |
| Autres voix Piper | — | celle du jeu de données de la voix : CC BY ou **CC BY-SA** selon la voix (`fr_FR-upmc-medium` : CC BY-SA 4.0, donc **non retenue**) ; voir le `MODEL_CARD` de chaque voix | `server/tools/train_wakeword/README.md` |
| Kokoro-82M (moteur `kokoro`) | autre moteur de synthèse | **Apache-2.0** (poids) | `tools/app-server/tts_server.py` |
| openWakeWord : `melspectrogram.onnx`, `embedding_model.onnx` | caractéristiques du détecteur facultatif (méthode `both`) | ceux d'openWakeWord (code Apache-2.0) | `server/tools/train_wakeword/README.md` |
| Classifieur « Lapinou » entraîné localement | détecteur facultatif | dépend de ses données d'entraînement (CC BY-NC-SA, CC BY-SA) : **jamais distribué** (R24) | `server/tools/train_wakeword/README.md` |

Une voix CC BY demande de citer ses auteurs : pour `fr_FR-siwis-medium`, les auteurs de la SIWIS
French Speech Synthesis Database, comme le fait le `README.md`. Les voix sous CC BY-SA ne sont pas
retenues par LapiNou.
