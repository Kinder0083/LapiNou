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
`LAPINOU_DB`, sinon `/var/lib/lapinou/lapinou.db`. Santé du service (sans contrôle de Host) :
`GET /api/health` → `{"status": "ok", "version": "…"}` (adresse canonique, celle de la page) et
son alias `GET /health` → `{"status": "ok"}` (sondes, Docker).

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
- au plus 10 démarrages en 5 minutes (`StartLimitIntervalSec`, `StartLimitBurst`) ;
- durcissement : `NoNewPrivileges`, `CapabilityBoundingSet=` (aucune capacité),
  `ProtectSystem=strict` avec `ReadWritePaths` sur les trois dossiers ci-dessus, `ProtectHome`,
  `PrivateTmp`, `PrivateDevices`, `ProtectKernel{Tunables,Modules,Logs}`, `ProtectClock`,
  `ProtectHostname`, `ProtectProc=invisible`, `ProtectControlGroups`, `RestrictNamespaces`,
  `SystemCallArchitectures=native`, `RestrictAddressFamilies`.
- **risque assumé** : `/opt/lapinou` (le code) est inscriptible par le service, pour le bouton
  « Mettre à jour ». Une faille du serveur permettrait donc de modifier son propre code, qui
  reste confiné (utilisateur `lapinou`, aucune capacité). Pour l'éviter, retirer
  `/opt/lapinou` de `ReadWritePaths` (`systemctl edit lapinou`) : le bouton échoue alors, et
  les mises à jour se font avec l'installateur. Essayé tel quel sur la VM de développement (Ubuntu 24.04) ; dans
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
| `GET /log?msg=…`, `POST /log` | compte rendu d'une application, dans le journal, sur une ligne (retours à la ligne échappés), 1 Kio au plus ; les 1000 derniers en mémoire |
| `POST /upload`, `POST /upload/<NOM>` | envoi écrit au fil de l'eau dans `/var/lib/lapinou/uploads/` (32 Mio au plus, 2 à la fois sinon 503 ; les 200 derniers gardés, 512 Mio en tout) |
| `GET /text` | page de test pour `http.get` |

Le serveur ASR 8443 (protocole de `asr_server.py`, session réécrite dans
`server/lapinou/web/asr_compat.py`) :

- une grammaire donnée par URL n'est jamais téléchargée : `…/asr/grammar/<id>.abnf` est la
  grammaire de lancement ci-dessus, toute autre URL une grammaire vide ;
- message ≤ 64 Kio, session ≤ 30 s, audio ≤ 30 s, 4 sessions à la fois (au-delà : BUSY) ;
- le modèle Parakeet est celui de l'écoute de Lapinou, qui passe d'abord : une seule
  transcription 8443 à la fois, et le modèle n'est attendu que 2 s (sinon NOMATCH) ;
- sans modèles : NOMATCH ;
- journal : durée d'audio et score seulement (ni grammaire, ni mots entendus, ni réponse) ;
  l'audio n'est pas gardé.

## Docker : `server/Dockerfile`

```
docker build -f server/Dockerfile -t lapinou .          # depuis la racine du dépôt
docker run -d --name lapinou --restart always \
  -v lapinou-etc:/etc/lapinou -v lapinou-data:/var/lib/lapinou \
  -p 9123:9123 -p 9300:9300 -p 8080:8080 -p 8081:8081 -p 8443:8443 lapinou
```

Image en deux étapes (venv construit avec `uv sync --frozen`, puis image d'exécution sans
compilateur), images de base épinglées par empreinte SHA-256, utilisateur `lapinou` non root,
mêmes chemins que le service. Le contexte est filtré par `.dockerignore` (racine du dépôt) :
seuls `server/`, `tools/app-server/` et `userland/proto/` y entrent, sans venv, caches,
réglages locaux, clés (sauf `ota-pubkey.pub`), données ni modèles. Les modèles ne sont
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
