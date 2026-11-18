<!-- SPDX-License-Identifier: MIT -->
# Voix claire de Lapinou : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** chaque phrase synthétisée par le serveur passe par le filtre de l'essai 2 (moins de graves, plus de clarté), réglable par `[voice] clarity`.

**Architecture:** une fonction `clarify(wav) -> wav` dans `server/lapinou/voice/tts.py` fait passer le WAV du moteur par ffmpeg ; `Voice.render_mp3` l'appelle avant `tts_server.to_mp3` quand `Voice.clarity` est vrai, et garde le WAV d'origine si ffmpeg échoue.

**Tech Stack:** Python 3.12, ffmpeg (déjà prérequis), pytest, uv.

**Spec:** `docs/superpowers/specs/2026-11-18-voix-claire-design.md`

## Global Constraints

- Filtre exact : `highpass=f=180,equalizer=f=250:t=q:w=1:g=-4,equalizer=f=3200:t=q:w=1.2:g=3`.
- Sortie : WAV 16 bits mono, **même fréquence** que l'entrée (pas de rééchantillonnage : le Karotz lit le MP3 tel quel).
- `[voice] clarity` : booléen, `true` par défaut. `Voice(...)` garde `clarity=False` par défaut (les tests existants comparent le MP3 à `tts_server`).
- Échec de ffmpeg : avertissement dans le journal, voix d'origine, jamais de silence ni d'exception.
- Commits : trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` ; push sur `lapinou/v1` et `main`.

## Review Focus

- ffmpeg absent du PATH alors que `clarity` est vrai : la phrase sort quand même (voix d'origine) — test `test_clarify_sans_ffmpeg_garde_la_voix`.
- ffmpeg qui renvoie une erreur (WAV illisible) : idem — test `test_clarify_ffmpeg_en_erreur_garde_la_voix`.
- WAV 22 050 Hz (Piper) et 24 000 Hz (Kokoro) : fréquence conservée — test paramétré `test_clarify_garde_la_frequence`.
- Le cache des notifications (`cached_mp3`) et le port 8081 passent par `render_mp3` : couverts par `test_render_mp3_applique_clarity`.
- Une phrase très courte (0,1 s) : pas de WAV vide — test `test_clarify_phrase_courte`.

---

### Task 1: le filtre et le réglage

**Files:**
- Modify: `server/lapinou/config.py` (classe `VoiceConfig`, après `voice`)
- Modify: `server/lapinou/voice/tts.py` (près de `decode_mp3_16k` ; `Voice.__init__`, `from_settings`, `render_mp3`)
- Test: `server/tests/test_tts.py`

**Interfaces:**
- Produces: `CLARITY_FILTER: str` (le filtre ci-dessus) ; `clarify(wav: bytes) -> bytes` (WAV 16 bits mono, même fréquence ; renvoie `wav` inchangé et journalise un avertissement si ffmpeg manque ou échoue) ; `Voice(..., clarity: bool = False)` ; `VoiceConfig.clarity: bool = True`.

- [ ] **Step 1: Écrire les tests (échouent)** dans `server/tests/test_tts.py`, ceux qui lancent ffmpeg marqués `pytest.mark.skipif(shutil.which("ffmpeg") is None, ...)` :
  - `test_clarify_garde_la_frequence[22050, 24000]` : `out = tts.clarify(sine_wav(rate=r))` → `wave.open` donne `getframerate() == r`, `getnchannels() == 1`, `getsampwidth() == 2`, et `out != entrée`.
  - `test_clarify_coupe_les_graves` : sinus 100 Hz à 22 050 Hz → énergie RMS de la sortie < 0,5 × celle de l'entrée (passe-haut à 180 Hz).
  - `test_clarify_phrase_courte` : `sine_wav(0.1)` → au moins 1 000 échantillons en sortie.
  - `test_clarify_sans_ffmpeg_garde_la_voix` (sans skip) : `monkeypatch.setattr(tts.shutil, "which", lambda n: None)` → `clarify(w) == w`, et `caplog` contient un avertissement.
  - `test_clarify_ffmpeg_en_erreur_garde_la_voix` : `clarify(b"pas un wav") == b"pas un wav"`, avertissement journalisé.
  - `test_render_mp3_applique_clarity` : deux `Voice("piper", "fr_FR-siwis-medium", synth=synth)`, l'une avec `clarity=True` → les deux MP3 diffèrent ; `monkeypatch` de `tts.clarify` par un espion → appelé une fois par `render_mp3` si `clarity`, jamais sinon.
  - `test_from_settings` (existant) : ajouter `assert v.clarity is True`, puis `s.voice.clarity = False` → `from_settings(s).clarity is False`.

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `cd server && uv run --frozen pytest -q tests/test_tts.py -k "clarify or clarity or from_settings"`
Expected: FAIL (`clarify` et `clarity` inexistants).

- [ ] **Step 3: Implémenter**
  - `VoiceConfig.clarity: bool = True`, avec un commentaire : moins de graves, plus de clarté (essai 2, R-voix), `false` = voix d'origine.
  - `clarify(wav)` : `shutil.which("ffmpeg")` ; lire la fréquence avec `wave` ; `subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "-", "-af", CLARITY_FILTER, "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", "-f", "wav", "-"], input=wav, capture_output=True, check=True, timeout=30)` ; toute `OSError`, `SubprocessError`, `wave.Error` ou `EOFError` → `log.warning("voix claire impossible (%s) : voix d'origine", e)` et `return wav`. ffmpeg écrit un en-tête WAV sans longueur sur un tube : `to_mp3` lit déjà « ce qu'il y a » (voir le commentaire espeak de `tts_server.to_mp3`) ; le vérifier par le test de fréquence.
  - `Voice.__init__(..., clarity: bool = False)` → `self.clarity` ; `from_settings` passe `clarity=settings.voice.clarity` (sans écraser un `clarity` des `kwargs`) ; `render_mp3` : `wav = synth(text)`, puis `if self.clarity: wav = clarify(wav)`, puis `to_mp3`.

- [ ] **Step 4: Lancer, vérifier le succès, puis toute la suite**

Run: `cd server && uv run --frozen pytest -q tests/test_tts.py` puis `uv run --frozen pytest -q`
Expected: tout PASS (773 + les nouveaux, 4 skipped au plus hors ffmpeg).

- [ ] **Step 5: Commit**

```bash
git add server/lapinou/config.py server/lapinou/voice/tts.py server/tests/test_tts.py
git commit -m "voix: voix claire de Lapinou (moins de graves, plus de clarté), réglage [voice] clarity"
```

### Task 2: mesure, doc et version 0.3.3

**Files:**
- Modify: `docs/lapinou/mesures.md` (section TTS, après les « Remarques »)
- Modify: `docs/lapinou/decisions.md` (une ligne de décision : voix claire, essai 2 choisi à l'écoute)
- Modify: `server/lapinou/__init__.py:4`, `server/pyproject.toml:4`, `server/uv.lock` (paquet `lapinou`) : `0.3.2` → `0.3.3`

**Interfaces:**
- Consumes: `tts.clarify`, `Voice(..., clarity=True)` (Task 1).

- [ ] **Step 1: Mesurer** sur la VM (`~/LapiNou/server`, voix Piper siwis réelle) : moyenne de 3 rendus de « Bonjour, je suis Lapinou. » par `Voice("piper", "fr_FR-siwis-medium").render_mp3`, `clarity` faux puis vrai, après une phrase d'échauffement. Noter les deux temps et l'écart.
- [ ] **Step 2: Écrire** l'écart mesuré et le filtre dans `mesures.md`, la décision dans `decisions.md` ; passer la version à 0.3.3 aux trois endroits.
- [ ] **Step 3: Vérifier**

Run: `cd server && uv run --frozen pytest -q && cd .. && python scripts/check-repo.py`
Expected: tests PASS ; check-repo sans erreur (hors CRLF du clone Windows).

- [ ] **Step 4: Commit et push**, puis CI verte (`check`, `server`) sur les deux branches.

```bash
git add docs/lapinou/mesures.md docs/lapinou/decisions.md server/lapinou/__init__.py server/pyproject.toml server/uv.lock
git commit -m "version: LapiNou v0.3.3 (voix claire)"
```

- [ ] **Step 5: Étiquette v0.3.3** : demander l'accord du propriétaire avant `git tag v0.3.3 && git push origin v0.3.3` ; suivre le workflow `release` ; le serveur se met à jour par son bouton « Mettre à jour ».
