<!-- SPDX-License-Identifier: MIT -->
# Service du serveur LapiNou

`python -m lapinou --config /etc/lapinou/config.toml --db /var/lib/lapinou/lapinou.db` lance
tout le serveur dans un seul processus, jusqu'à SIGTERM (ou Ctrl-C) :

| Port | Rôle |
|---|---|
| 9123 | plateforme LapinLibre (contrôleur des lapins) |
| 9300 | flux micro LapiNou (`protocole-audio.md`) |
| 8080 | HTTP : page de réglages et API, MP3 des réponses (`/tts/`), OTA (`/ota/`), endpoints LapinLibre des applications |
| 8081 | TTS compatible `voice-daemon` (`tts_server.py`) |
| 8443 | ASR compatible `voice-daemon` (`asr_server.py`) |

Sans `--config` : `LAPINOU_CONFIG`, sinon `/etc/lapinou/config.toml`. Sans `--db` :
`LAPINOU_DB`, sinon `/var/lib/lapinou/lapinou.db`. La santé du service, seule adresse
canonique : `GET /api/health` → `{"status": "ok", "version": "…"}` (sans contrôle de Host).

À l'arrêt (SIGTERM), les services sont refermés dans l'ordre inverse et le journal note
« serveur LapiNou arrêté ». uvicorn relance ensuite le signal reçu : le processus se termine
par SIGTERM, que systemd tient pour un arrêt propre.

## Chemins (installateur, Tâche 25)

| Chemin | Contenu | Écriture par le service |
|---|---|---|
| `/opt/lapinou` | clone du dépôt (`server/`, `tools/app-server/`, `userland/proto/`), venv `server/.venv` | oui : bouton « Mettre à jour » (`git pull`, `installer/post-update.sh`) |
| `/etc/lapinou` | `config.toml`, `secrets.toml` (0600) | oui : la page de réglages les enregistre |
| `/var/lib/lapinou` | `lapinou.db`, `apps/`, `uploads/`, `ota/`, `setup-token` ; HOME du service | oui |

Dans `/var/lib/lapinou` (HOME) : modèles de transcription `.cache/lapinlibre/asr`
(`python -m lapinou.voice.models fetch`), Kokoro `.cache/lapinlibre/kokoro`, Piper
`.cache/lapinou/piper` et son venv `.local/share/lapinou/piper-venv`, outils comme `ha-mcp`
dans `.local/bin` (dans le PATH du service).

## systemd : `server/systemd/lapinou.service`

- utilisateur système dédié `lapinou` (pas root), HOME `/var/lib/lapinou` ;
- `Restart=always`, sortie dans le journal (`journalctl -u lapinou`) ;
- durcissement : `NoNewPrivileges`, `ProtectSystem=strict` avec `ReadWritePaths` sur les trois
  dossiers ci-dessus, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernel*`,
  `RestrictAddressFamilies`. Essayé tel quel sur la VM de développement (Ubuntu 24.04) ; dans
  un LXC non privilégié, une option refusée par le noyau fait échouer le démarrage
  (`status=226/NAMESPACE`) : la retirer par un `systemctl edit lapinou`.

À la charge de l'installateur : créer l'utilisateur et les dossiers (propriétaire `lapinou`),
installer `protoc`, `ffmpeg` et `libspeexdsp`, copier l'unité dans `/etc/systemd/system/`.
Le bouton « Redémarrer » de la page lance `systemctl --no-block restart lapinou` en tant que
`lapinou` : il faut une règle polkit qui le permet (`sudo` est exclu par `NoNewPrivileges`).

## Endpoints LapinLibre des applications (8080)

Les lapins ont `url_app=http://<serveur>:8080` : le serveur reprend le contrat HTTP de
`tools/app-server/app_server.py` (voir `docs/app-server.md`), sans mot de passe et sans
contrôle de Host :

| Adresse | Contenu |
|---|---|
| `GET /file/<apiKey>/<version>/get` | l'application `/var/lib/lapinou/apps/<apiKey>/` en zip (sans `config.bin`) |
| `GET /asr/grammar/<id>.abnf` | grammaire de lancement (une alternative par application hébergée), en `fr-FR` |
| `GET /log?msg=…`, `POST /log` | compte rendu d'une application, dans le journal (64 Kio au plus) |
| `POST /upload`, `POST /upload/<NOM>` | envoi rangé dans `/var/lib/lapinou/uploads/` (32 Mio au plus, les 200 derniers gardés) |
| `GET /text` | page de test pour `http.get` |

Le serveur ASR 8443 utilise la même grammaire de lancement, et le modèle Parakeet déjà chargé
pour l'écoute ; sans modèles, il répond NOMATCH. L'audio des sessions n'est pas gardé.

## Docker : `server/Dockerfile`

```
docker build -f server/Dockerfile -t lapinou .          # depuis la racine du dépôt
docker run -d --name lapinou --restart always \
  -v lapinou-etc:/etc/lapinou -v lapinou-data:/var/lib/lapinou \
  -p 9123:9123 -p 9300:9300 -p 8080:8080 -p 8081:8081 -p 8443:8443 lapinou
```

Image en deux étapes (venv construit avec `uv sync --frozen`, puis image d'exécution sans
compilateur), utilisateur `lapinou` non root, mêmes chemins que le service. Les modèles ne sont
pas dans l'image : ils sont **montés**, dans le volume `/var/lib/lapinou`, et récupérés une
fois :

```
docker exec lapinou python -m lapinou.voice.models fetch
docker restart lapinou
```

Les boutons « Mettre à jour » et « Redémarrer » ne s'appliquent pas à l'image (ni git ni
systemd dedans) : on reconstruit l'image et on relance le conteneur. Les lapins
doivent joindre le serveur à l'adresse de l'hôte : réglez `server.public_url` (ou
`LAPINOU_BASE_URL`) sur `http://<adresse de l'hôte>:8080`, l'adresse détectée dans le
conteneur n'étant pas celle du réseau local.
