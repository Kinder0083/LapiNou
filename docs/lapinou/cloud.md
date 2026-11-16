<!-- SPDX-License-Identifier: MIT -->
# Le mode sans serveur (`lapinou-cloud`)

Sans serveur LapiNou, le lapin peut répondre seul en passant par trois services en ligne : la
reconnaissance de la parole, un modèle de langage et la synthèse vocale. C'est le démon
`lapinou-cloud` (`userland/src/usr/lapinou-cloud/`), démarré par `S40lapinlibre` sous
`immortaldog`. Il ne fait rien tant que `lapinou.conf` n'a pas `[cloud] enabled=1`.

## Déroulement

1. Appui long sur la tête : le contrôleur appelle `start_listening()` (si `[cloud] enabled=1` et
   qu'aucune plateforme n'a connecté le lapin). Le démon lit le micro sur la socket de
   `lapinou-mic` (`/var/run/lapinou/mic.sock`, mu-law 16 kHz mono).
2. Relâchement : `stop_listening()`. L'enregistrement s'arrête aussi tout seul à 15 s. Sous
   0,3 s, rien n'est envoyé.
3. L'enregistrement part en WAV (16 kHz, mono, PCM 16 bits, fait en mémoire, 480 Ko au plus) vers
   la reconnaissance de la parole, au format OpenAI `/v1/audio/transcriptions` (multipart :
   `model`, `language=fr`, `response_format=json`, `file`).
4. La transcription part vers le modèle de langage. Le prompt système est
   `/usr/share/lapinou/persona_fr_court.txt`, sans ses lignes de commentaire du début. La
   conversation est gardée en mémoire et oubliée 5 minutes après la dernière réponse (12 tours et
   12 000 octets de texte au plus).
   - `llm_kind=anthropic` : `POST <llm_url>/v1/messages`, en-têtes `x-api-key` et
     `anthropic-version: 2023-06-01`, corps `{model, max_tokens: 1024, system, messages}`. Pas de
     `temperature`, de `top_p` ni de `thinking`. Pour `claude-opus-5-5` (et les autres modèles qui
     l'acceptent), le repli côté serveur : en-tête `anthropic-beta: server-side-fallback-2026-07-01`
     et `"fallbacks": "default"`. Seuls les blocs `text` de la réponse sont dits.
   - `llm_kind=openai` : `POST <llm_url>/v1/chat/completions`, `Authorization: Bearer`, le
     persona en premier message `system`.
5. La réponse (sans les marques Markdown `* # \` _`, coupée à 1 500 octets en fin de phrase) part
   vers la synthèse vocale, au format OpenAI `/v1/audio/speech` (`{model, voice, input,
   response_format: "mp3"}`). Le MP3 (4 Mo au plus) est écrit dans `/tmp/lapinou-tts.mp3` et joué par
   `Multimedia.play`.

`say(s text)` dit un texte par la synthèse vocale seule (le `cmd/say` de `lapinou-mqtt` dans ce
mode), refusé pendant une question (écoute ou réflexion). Un nouvel appui long pendant que le lapin
parle ou réfléchit arrête ce qui est en cours.

## Réglages

Section `[cloud]` de `lapinou.conf`, relue à chaque question :

| Clé | Par défaut | |
|---|---|---|
| `enabled` | `0` | `1` pour s'en servir |
| `stt_url` | `https://api.openai.com/v1/audio/transcriptions` | voir « Adresses » |
| `stt_model` | `whisper-1` | |
| `llm_kind` | `anthropic` | `anthropic` ou `openai` |
| `llm_url` | `https://api.anthropic.com` (`https://api.openai.com` pour `openai`) | |
| `llm_model` | `claude-opus-5-5` (`gpt-4o-mini` pour `openai`) | |
| `tts_url` | `https://api.openai.com/v1/audio/speech` | |
| `tts_model` | `tts-1` | |
| `tts_voice` | `alloy` | |

Les clés sont les secrets `cloud_stt_key`, `cloud_llm_key` et `cloud_tts_key`
(`lapinou.secrets`). Une clé absente n'est pas envoyée (pour un service local sans clé).

Adresses : la barre oblique finale est enlevée ; si l'adresse finit déjà par le chemin du service,
elle est prise telle quelle ; si elle finit par `/v1`, seul le reste du chemin est ajouté ; sinon
le chemin entier (`/v1/...`) est ajouté. Seuls `http://` et `https://` sont acceptés.

## Erreurs

Réseau coupé, statut HTTP autre que 200, réponse qui n'en est pas une, délai dépassé (connexion
10 s ; reconnaissance 30 s, modèle 60 s, synthèse 60 s), réponse trop longue (64 Ko, 256 Ko,
4 Mo) : le son `/usr/karotz/sounds/error.mp3`, la LED orange 3 s, puis la LED d'un lapin
disponible (vert pulsé lent), et le signal `state("error")` puis `state("idle")`. Le démon continue.
Un refus du modèle (`stop_reason: "refusal"` chez Anthropic, `refusal` ou `finish_reason:
"content_filter"` chez OpenAI) : rien de la réponse n'est dit ; la LED orange et une phrase gentille
à la place du son d'erreur. Rien n'est entendu (transcription vide) : le son « je n'ai pas
compris ».

HTTPS : les certificats sont vérifiés avec les certificats d'autorité de `/etc/ssl/certs` (un
fichier par certificat, comme karotzVM ; `$SSL_CERT_DIR` remplace le dossier). Tant que l'horloge
n'est pas réglée (avant 2026), une requête HTTPS échoue.

Les clés, les transcriptions et les réponses ne sont jamais écrites dans le journal.

## D-Bus

`com.mindscape.karotz.LapinouCloud`, objet `/com/mindscape/karotz/LapinouCloud`, interface
`com.mindscape.karotz.LapinouCloud` : `start_listening()`, `stop_listening()`, `say(s text)` ;
signal `state(s)` : `listening`, `thinking`, `speaking`, `idle` ou `error`.
