# SPDX-License-Identifier: MIT
"""Entraînement du mot d'activation « Lapinou » (voir README.md de ce dossier).

Étapes (chacune relit les sorties de la précédente dans --work) :
  generate  synthèse Piper : « Lapinou » (positifs), mots proches et phrases (négatifs),
            voix d'entraînement et voix réservées au test
  features  augmentation (bruit, réverbération, égalisation, mu-law du lapin) et
            plongements openWakeWord (mêmes calculs que lapinou.voice.wakeword)
  train     classifieur (PyTorch, CPU) exporté en ONNX
  evaluate  rappel sur les voix réservées, fausses détections par heure
  export-tests  fichiers server/tests/data/{lapinou,not_lapinou}_*.wav

Dépendances (environnement d'entraînement, pas celui du serveur) :
  piper-tts onnxruntime numpy scipy torch onnx
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random
import struct
import sys
import time
import wave
from multiprocessing import Pool
from pathlib import Path

import numpy as np

SERVER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVER))

from lapinou.voice import wakeword as ww  # noqa: E402

SR = ww.SAMPLE_RATE

# ---------------------------------------------------------------- voix et textes

# (fichier de voix Piper, locuteurs) ; None = voix à un seul locuteur.
# fr_FR-mls-medium a 125 locuteurs (MLS, CC BY 4.0) ; 1840 est aussi la voix
# fr_FR-mls_1840-low, réservée au test, donc exclu de l'entraînement.
MLS_HELD_OUT = ["1840", "2506", "3698", "5232", "9121", "10827", "11772", "12823"]
TRAIN_VOICES = ["fr_FR-siwis-medium", "fr_FR-siwis-low", "fr_FR-upmc-medium", "fr_FR-mls-medium"]
TEST_VOICES = ["fr_FR-gilles-low", "fr_FR-mls_1840-low", "fr_FR-mls-medium"]

POSITIVE_TEXTS = [
    "Lapinou", "Lapinou !", "Lapinou ?", "Lapinou.", "Lapinou,", "Lapinou...",
    "Hé Lapinou", "Dis Lapinou", "Bonjour Lapinou", "Coucou Lapinou", "Allez Lapinou",
    "Oh Lapinou", "Salut Lapinou", "Dis-moi Lapinou", "Eh Lapinou !",
]
# Graphies qui varient un peu la prononciation (timbre du « a », longueur du « ou »).
POSITIVE_TEXTS += ["Lâpinou", "Lapinoû", "Lappinou !", "Il a dit Lapinou.", "Viens, Lapinou."]
# Mots proches. « la pinou » est volontairement absent : c'est un homophone exact
# (/lapinu/), l'apprendre comme négatif contredirait les positifs.
NEAR_MISSES = [
    "lapin", "lapins", "le lapin", "un lapin", "lapereau", "lapereaux", "papinou",
    "lapinot", "lapine", "la pinède", "lapidé", "la piscine", "pinou", "minou", "kapinou",
    "mapinou", "lapi", "la pie", "l'appli", "la tisane", "papi nous", "lapin roux",
    "lapin doux", "la pinte", "Pinocchio", "là, pis nous", "la bise nous", "lapis-lazuli",
    "la Pologne", "la pendule", "l'épinard", "les pinous", "tapinois", "chapi chapo",
    "la pipe", "rapido", "lavabo", "la puce", "la pilule", "nounours", "bisounours",
    "lapin nain", "Capucine", "la peinture", "l'abri bus", "la piqûre", "le pinson",
]

_SENTENCES = """\
Il fait beau aujourd'hui, on pourrait aller se promener au parc.
Tu as pensé à acheter du pain en rentrant du travail ?
Les informations de vingt heures commencent dans cinq minutes.
Allume la lumière du salon, s'il te plaît.
Quelle heure est-il ? Je crois qu'on va être en retard.
Le chat dort encore sur le canapé depuis ce matin.
On mange quoi ce soir ? Des pâtes ou une salade ?
La réunion a été déplacée à jeudi après-midi.
Mon frère arrive par le train de dix-huit heures douze.
Il faut sortir les poubelles, c'est demain le ramassage.
Les enfants, venez à table, le dîner est prêt !
J'ai regardé un très bon film hier soir à la télévision.
Le match de football se termine sur un match nul.
La météo annonce de la pluie pour tout le week-end.
Tu peux baisser le volume de la radio ?
Le lapin mange des carottes dans le jardin.
Mon petit lapin s'appelle Caramel, il est tout blanc.
Les lapins de garenne creusent des terriers dans la prairie.
J'ai trouvé un lapereau perdu près de la haie.
Papi nous a raconté une histoire de pirates.
La piscine municipale ferme à dix-neuf heures.
On a planté des pins le long de la clôture.
Le minou de la voisine miaule devant la porte.
Il a mis la pendule à l'heure ce matin.
Ferme les volets, il commence à faire froid.
Je vais faire les courses au supermarché du coin.
Elle a réussi son examen de piano avec mention.
Le facteur est passé, il y a un colis pour toi.
Nous partirons en vacances en Bretagne au mois d'août.
Peux-tu mettre une minuterie de dix minutes pour les œufs ?
La voiture fait un drôle de bruit quand je freine.
Les feuilles mortes recouvrent toute la pelouse.
Il reste du café dans la cafetière si tu veux.
Le bébé dort, parle moins fort s'il te plaît.
J'adore la tarte aux pommes de ta grand-mère.
Ce soir, il y a un documentaire sur les volcans.
Le train est annoncé avec vingt minutes de retard.
On se retrouve devant le cinéma à huit heures et demie.
La boulangerie est fermée le lundi.
Mets la musique un peu plus fort, j'adore cette chanson.
Bonjour, je voudrais prendre rendez-vous chez le dentiste.
Il neige sur les montagnes, les pistes vont ouvrir.
Le chien aboie chaque fois que quelqu'un passe dans la rue.
Ma sœur a adopté deux lapins nains et un hamster.
Les invités arrivent dans une heure, il faut ranger le salon.
Tu as vu mes clés ? Je ne les trouve plus nulle part.
La lampe de la cuisine est grillée, il faut la changer.
Il paraît que le nouveau restaurant italien est excellent.
On a gagné trois à un, quel match incroyable !
Le président a prononcé un discours à la télévision.
Les températures vont baisser cette nuit, jusqu'à moins deux degrés.
Elle lit un roman policier dans le jardin.
Je dois rappeler la banque avant midi.
Le gâteau au chocolat est en train de cuire dans le four.
Nous avons visité le château et ses jardins.
Il faut arroser les plantes, la terre est sèche.
Le robinet de la salle de bain fuit encore.
Demain matin, réveille-moi à sept heures.
Quelle température fait-il dehors en ce moment ?
J'ai oublié mon parapluie au bureau.
Les oiseaux chantent dès le lever du soleil.
Il y a des embouteillages sur le périphérique.
On regarde la suite de la série ce soir ?
Mon ordinateur est vraiment lent depuis la mise à jour.
La pharmacie de garde est ouverte jusqu'à minuit.
Elle a peint la chambre en bleu clair.
Le pinson et la mésange viennent à la mangeoire.
Je n'ai pas bien compris, tu peux répéter ?
Le petit déjeuner est servi de sept à dix heures.
Les pompiers sont intervenus rapidement.
Il a acheté une nouvelle paire de chaussures de course.
Qu'est-ce qu'on fait ce week-end, la plage ou la montagne ?
Ce livre de cuisine propose des recettes très simples.
Le vent souffle fort, attention aux branches.
Tu veux un thé ou une tisane ?
Les élèves rentrent en classe après la récréation.
Je préfère les pommes aux poires.
La pile de la télécommande est vide.
Mamie nous attend pour le déjeuner dimanche.
J'ai rendez-vous chez le médecin mardi matin.
Le tonnerre gronde au loin, l'orage approche.
Pense à fermer le gaz avant de partir.
Il parle trois langues couramment.
La lessive est terminée, il faut étendre le linge.
Nous avons perdu la partie de cartes hier soir.
Le marché a lieu tous les samedis sur la place.
Peux-tu éteindre la lumière de l'entrée ?
Les pâtes sont trop cuites, dommage.
On entend la mer depuis la fenêtre de l'hôtel.
Le spectacle commence à vingt heures trente précises.
"""

_SUBJECTS = ["Je", "Tu", "Il", "Elle", "On", "Nous", "Vous", "Ils", "Mon voisin", "Ma cousine",
             "Le professeur", "La boulangère", "Mon père", "Ta mère", "Les enfants", "Le chat"]
_VERBS = ["regarde", "prépare", "cherche", "achète", "range", "oublie", "répare", "écoute",
          "trouve", "lave", "apporte", "dessine", "attend", "aime bien", "déteste", "nettoie"]
_OBJECTS = ["la voiture", "le jardin", "la cuisine", "un gâteau", "les fenêtres", "la radio",
            "une chanson", "le journal", "la télévision", "les assiettes", "un cadeau",
            "la pinède", "le lapin", "les pinceaux", "la pizza", "le bus", "la piscine",
            "le minou", "une tisane", "la pendule", "les papiers", "un pull", "la lampe"]
_TAILS = ["ce matin.", "depuis une heure.", "avant le dîner.", "tous les jours.", "avec patience.",
          "dans le salon.", "sans rien dire.", "pour demain.", "encore une fois.", "le dimanche."]


def sentences() -> list[str]:
    """Phrases négatives : liste écrite à la main + phrases combinées (déterministe)."""
    fixed = [s.strip() for s in _SENTENCES.splitlines() if s.strip()]
    rng = random.Random(1234)
    combos = [
        f"{s} {v} {o} {t}"
        for s, v, o, t in itertools.product(_SUBJECTS, _VERBS, _OBJECTS, _TAILS)
    ]
    rng.shuffle(combos)
    return fixed + combos[:3000]


def held_out(text: str) -> bool:
    """Une phrase sur cinq est réservée au test (partage stable par empreinte)."""
    return hashlib.sha256(text.encode()).digest()[0] % 5 == 0


# ---------------------------------------------------------------- synthèse Piper

_VOICES: dict[str, object] = {}
_VOICE_DIR: Path | None = None


def _voice(name: str):
    """Voix Piper avec alignements phonème/audio, un fil de calcul, deux voix en cache."""
    import onnxruntime as ort
    from piper import PiperVoice
    from piper.config import PiperConfig
    from piper.patch_voice_with_alignment import add_alignment_output
    from piper.voice import ESPEAK_DATA_DIR

    import onnx

    if name not in _VOICES:
        while len(_VOICES) >= 2:
            _VOICES.pop(next(iter(_VOICES)))
        path = _VOICE_DIR / f"{name}.onnx"
        model = onnx.load(str(path))
        if name == MIX_VOICE:
            add_mixed_speakers(model, json.loads(Path(f"{path}.json").read_text()))
        try:
            add_alignment_output(model)
        except ValueError:
            pass  # déjà corrigé
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        config = PiperConfig.from_dict(json.loads(Path(f"{path}.json").read_text()))
        _VOICES[name] = PiperVoice(
            config=config,
            session=ort.InferenceSession(model.SerializeToString(), options,
                                         providers=["CPUExecutionProvider"]),
            espeak_data_dir=Path(ESPEAK_DATA_DIR), download_dir=Path.cwd(),
        )
    return _VOICES[name]


# Voix mélangées : sans elles le classifieur retient les ~120 locuteurs d'entraînement
# (rappel ~1 sur eux, ~0,5 sur des locuteurs MLS jamais vus). Comme le générateur
# d'openWakeWord (piper-sample-generator), on interpole les plongements de locuteur de
# fr_FR-mls-medium : de nouvelles lignes ajoutées à emb_g.weight, identifiants 125 et plus.
# Seuls des locuteurs d'entraînement sont mélangés.
MIX_VOICE = "fr_FR-mls-medium"
MIX_SPEAKERS = 3000


def mixed_rows(weight: np.ndarray, train_ids: list[int]) -> np.ndarray:
    """MIX_SPEAKERS plongements, chacun entre deux locuteurs d'entraînement (déterministe)."""
    rng = np.random.default_rng(2026)
    a = rng.choice(train_ids, MIX_SPEAKERS)
    b = rng.choice(train_ids, MIX_SPEAKERS)
    b = np.where(a == b, np.roll(b, 1), b)
    t = rng.uniform(0.2, 0.8, (MIX_SPEAKERS, 1))
    mix = (1 - t) * weight[a] + t * weight[b]
    norm = (1 - t) * np.linalg.norm(weight[a], axis=1, keepdims=True) + t * np.linalg.norm(
        weight[b], axis=1, keepdims=True)
    return (mix / np.linalg.norm(mix, axis=1, keepdims=True) * norm).astype(weight.dtype)


def _mls_train_ids(config: dict) -> list[int]:
    return sorted(v for k, v in config["speaker_id_map"].items() if k not in MLS_HELD_OUT)


def add_mixed_speakers(model, config: dict) -> None:
    from onnx import numpy_helper

    for i, init in enumerate(model.graph.initializer):
        if init.name == "emb_g.weight":
            weight = numpy_helper.to_array(init)
            rows = mixed_rows(weight, _mls_train_ids(config))
            new = numpy_helper.from_array(np.concatenate((weight, rows)), init.name)
            model.graph.initializer[i].CopyFrom(new)
            return
    raise ValueError("emb_g.weight absent de la voix MLS")


def _init_worker(voice_dir: str) -> None:
    global _VOICE_DIR
    _VOICE_DIR = Path(voice_dir)
    os.nice(5)


def speakers(name: str, split: str) -> list[int | None]:
    """Les locuteurs d'une voix pour un partage (« train », « test » ou « mix »)."""
    config = json.loads((_VOICE_DIR / f"{name}.onnx.json").read_text())
    if split == "mix":
        base = len(config["speaker_id_map"])
        return list(range(base, base + MIX_SPEAKERS)) if name == MIX_VOICE else []
    ids = config.get("speaker_id_map") or {}
    if not ids:
        return [None]
    if name != "fr_FR-mls-medium":
        return sorted(ids.values())
    keep = (lambda k: k in MLS_HELD_OUT) if split == "test" else (lambda k: k not in MLS_HELD_OUT)
    return sorted(v for k, v in ids.items() if keep(k))


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    from scipy.signal import resample_poly

    if rate == SR:
        return audio
    g = np.gcd(SR, rate)
    return resample_poly(audio, SR // g, rate // g)


def trim(audio: np.ndarray, margin_s: float = 0.03) -> np.ndarray:
    frame = 160
    n = len(audio) // frame
    if n == 0:
        return audio
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    loud = np.nonzero(rms > rms.max() * 10 ** (-40 / 20))[0]
    if len(loud) == 0:
        return audio
    m = int(margin_s * SR)
    return audio[max(0, loud[0] * frame - m) : min(len(audio), (loud[-1] + 1) * frame + m)]


WORD = ["l", "a", "p", "i", "n", "u"]  # « Lapinou » sans les marques d'accent
PUNCT = set(" .,;:!?¡¿—…'\"-^$")


def _render(voice, text: str, config, positive: bool) -> np.ndarray | None:
    """Synthèse validée par les alignements, coupée à la fin du dernier phonème parlé.

    Les voix Piper « low » et MLS dérapent parfois (phonème de plusieurs secondes,
    babillage après le texte) : un extrait positif est refusé si un phonème de
    « Lapinou » dure plus de 0,3 s ou le mot entier hors de 0,25 à 0,95 s, et tout
    extrait dont les phonèmes durent en moyenne plus de 0,16 s.
    """
    rate = voice.config.sample_rate
    pieces = []
    for chunk in voice.synthesize(text, config, include_alignments=True):
        audio, alignments = chunk.audio_float_array, chunk.phoneme_alignments
        if not alignments:
            return None
        phones = [x.phoneme for x in alignments]
        ends = np.cumsum([x.num_samples for x in alignments])
        if max(x.num_samples for x in alignments if x.phoneme not in PUNCT) > 0.6 * rate:
            return None
        spoken = [i for i, ph in enumerate(phones) if ph not in PUNCT and ph != "ˈ"]
        if not spoken:
            continue
        if sum(alignments[i].num_samples for i in spoken) / len(spoken) > 0.16 * rate:
            return None  # débit anormalement lent : babillage
        start = max(0, int(ends[spoken[0]] - alignments[spoken[0]].num_samples - 0.05 * rate))
        stop = int(ends[spoken[-1]] + 0.06 * rate)
        if positive:
            letters = [(i, phones[i]) for i in spoken]
            found = [k for k in range(len(letters) - len(WORD) + 1)
                     if [p for _, p in letters[k : k + len(WORD)]] == WORD]
            if not found:
                return None
            idx = [i for i, _ in letters[found[-1] : found[-1] + len(WORD)]]
            durations = [alignments[i].num_samples / rate for i in idx]
            first = ends[idx[0]] - alignments[idx[0]].num_samples
            span = (ends[idx[-1]] - first) / rate
            if max(durations) > 0.3 or not 0.25 <= span <= 0.95:
                return None
            stop = int(ends[idx[-1]] + 0.06 * rate)
        pieces.append(audio[start:stop])
    return np.concatenate(pieces) if pieces else None


def _synth(job: tuple) -> np.ndarray | None:
    """Une synthèse -> float32 16 kHz sans silences autour ; None après 6 essais ratés."""
    from piper import SynthesisConfig

    name, speaker, text, kind, seed = job
    voice = _voice(name)
    rng = random.Random(seed)
    for _ in range(6):
        length, pitch = rng.uniform(0.75, 1.4), rng.uniform(0.9, 1.12) if rng.random() < 0.6 else 1.0
        config = SynthesisConfig(speaker_id=speaker, length_scale=length,
                                 noise_scale=rng.uniform(0.3, 0.8), noise_w_scale=rng.uniform(0.2, 0.9))
        audio = _render(voice, text, config, kind == "positive")
        if audio is not None:
            break
    else:
        return None
    audio = to_16k(audio.astype(np.float64), voice.config.sample_rate)
    if pitch != 1.0:  # hauteur et débit ensemble (lecture plus rapide ou plus lente)
        positions = np.arange(0, len(audio) - 1, pitch)
        audio = np.interp(positions, np.arange(len(audio)), audio)
    return trim(audio).astype(np.float32)


def _jobs(kind: str, split: str, count: int, rng: random.Random) -> list[tuple]:
    voices = TEST_VOICES if split == "test" else TRAIN_VOICES
    pool = [(v, s) for v in voices for s in speakers(v, split)]
    if kind == "positive":
        texts = POSITIVE_TEXTS
    elif kind == "near":
        texts = NEAR_MISSES
    else:
        texts = [t for t in sentences() if held_out(t) == (split == "test")]
    jobs = []
    for i in range(count):
        voice, speaker = pool[i % len(pool)] if kind != "sentence" else rng.choice(pool)
        text = texts[i % len(texts)] if kind == "sentence" else rng.choice(texts)
        jobs.append((voice, speaker, text, kind, f"{split}-{kind}-{i}"))
    return sorted(jobs, key=lambda j: j[0])  # une voix à la fois par processus


def save_clips(path: Path, clips: list[np.ndarray], texts: list[str]) -> None:
    offsets = np.cumsum([0] + [len(c) for c in clips])
    np.savez(path, audio=np.concatenate(clips).astype(np.float32), offsets=offsets,
             texts=np.array(texts))


def load_clips(path: Path) -> list[np.ndarray]:
    data = np.load(path)
    audio, offsets = data["audio"], data["offsets"]
    return [audio[a:b] for a, b in zip(offsets[:-1], offsets[1:])]


def load_texts(path: Path) -> list[str]:
    return [str(t) for t in np.load(path)["texts"]]


def cmd_generate(args: argparse.Namespace) -> None:
    global _VOICE_DIR
    _VOICE_DIR = Path(args.voices)
    out = Path(args.work) / "clips"
    out.mkdir(parents=True, exist_ok=True)
    plan = [
        ("positive", "train", args.positives), ("near", "train", args.near),
        ("sentence", "train", args.sentences),
        ("positive", "test", 200), ("near", "test", 300), ("sentence", "test", 700),
        ("positive", "mix", args.mix_positives), ("near", "mix", args.mix_near),
        ("sentence", "mix", args.mix_sentences),
    ]
    with Pool(args.jobs, _init_worker, (str(_VOICE_DIR),)) as pool:
        for kind, split, count in plan:
            target = out / f"{split}_{kind}.npz"
            if target.exists():
                continue
            jobs = _jobs(kind, split, count, random.Random(f"{kind}-{split}"))
            t0 = time.time()
            results = pool.map(_synth, jobs, chunksize=16)
            kept = [(c, j[2]) for c, j in zip(results, jobs) if c is not None]
            save_clips(target, [c for c, _ in kept], [t for _, t in kept])
            for voice in sorted({j[0] for j in jobs}):
                ok = [c is not None for c, j in zip(results, jobs) if j[0] == voice]
                print(f"  {voice}: {sum(ok)}/{len(ok)}", flush=True)
            seconds = sum(len(c) for c, _ in kept) / SR
            print(f"{target.name}: {len(kept)} extraits ({len(jobs) - len(kept)} refusés), "
                  f"{seconds / 60:.1f} min, "
                  f"{time.time() - t0:.0f} s", flush=True)


# ---------------------------------------------------------------- augmentation

def mulaw_encode(pcm: np.ndarray) -> np.ndarray:
    """int16 -> octets mu-law G.711 (inverse de audio_gateway.mulaw_decode)."""
    x = pcm.astype(np.int32)
    sign = np.where(x < 0, 0x80, 0)
    mag = np.minimum(np.abs(x), 32635) + 0x84
    exponent = np.floor(np.log2(mag)).astype(np.int32) - 7
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


def mulaw_decode(data: np.ndarray) -> np.ndarray:
    u = ~data.astype(np.int32) & 0xFF
    x = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)
    return np.where(u & 0x80, 0x84 - x, x - 0x84).astype(np.int16)


def colored_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Bruit blanc, rose ou brun (1/f^a), rms 1."""
    alpha = rng.choice([0.0, 1.0, 2.0])
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n)
    f[0] = f[1] if n > 1 else 1.0
    noise = np.fft.irfft(spectrum / f ** (alpha / 2), n)
    return noise / (np.sqrt(np.mean(noise**2)) + 1e-9)


def hum(n: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(n) / SR
    base = rng.choice([50.0, 100.0, 120.0])
    sig = sum(np.sin(2 * np.pi * base * k * t + rng.uniform(0, 6)) / k for k in range(1, 5))
    return sig / (np.sqrt(np.mean(sig**2)) + 1e-9)


def reverb(audio: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    from scipy.signal import fftconvolve

    rt60 = rng.uniform(0.15, 0.7)
    n = int(rt60 * SR)
    rir = rng.standard_normal(n) * np.exp(-6.9 * np.arange(n) / n)
    rir[0] = 1.0 / rng.uniform(0.3, 1.0)  # chemin direct
    wet = fftconvolve(audio, rir)[: len(audio)]
    return wet / (np.max(np.abs(wet)) + 1e-9) * np.max(np.abs(audio))


def equalize(audio: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    from scipy.signal import butter, sosfilt

    if rng.random() < 0.6:
        audio = sosfilt(butter(2, rng.uniform(80, 400), "highpass", fs=SR, output="sos"), audio)
    if rng.random() < 0.6:
        audio = sosfilt(butter(2, rng.uniform(2800, 7500), "lowpass", fs=SR, output="sos"), audio)
    return audio


def background(n: int, rng: np.random.Generator, babble: list[np.ndarray] | None) -> np.ndarray:
    """Fond sonore (amplitude pleine échelle = 1)."""
    kind = rng.random()
    if kind < 0.12:
        return np.zeros(n)
    level = 10 ** (rng.uniform(-75, -35) / 20)
    bg = colored_noise(n, rng) * level
    if kind < 0.2:
        bg += hum(n, rng) * level
    if kind > 0.6 and babble:  # paroles lointaines (autres phrases)
        speech = np.concatenate([babble[i] for i in rng.integers(0, len(babble), 4)])
        speech = np.resize(speech, n)
        bg += speech * 10 ** (rng.uniform(-45, -20) / 20)
    return bg


def channel(audio: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Égalisation, int16, puis mu-law aller-retour (le micro du lapin) la plupart du temps."""
    if rng.random() < 0.5:
        audio = equalize(audio, rng)
    pcm = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    if rng.random() < 0.7:
        pcm = mulaw_decode(mulaw_encode(pcm))
    return pcm


def scene(word: np.ndarray, rng: np.random.Generator, babble: list[np.ndarray],
          pre_s: float, post_s: float, rev: bool = True) -> tuple[np.ndarray, int]:
    """Un mot sur un fond sonore -> (int16, indice de fin du mot)."""
    word = word / (np.max(np.abs(word)) + 1e-9) * 10 ** (rng.uniform(-32, -2) / 20)
    if rev and rng.random() < 0.4:
        word = reverb(np.concatenate((word, np.zeros(int(0.15 * SR)))), rng)
    pre, post = int(pre_s * SR), int(post_s * SR)
    audio = background(pre + len(word) + post, rng, babble)
    audio[pre : pre + len(word)] += word
    return channel(audio, rng), pre + len(word)


def windows(feats: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """Fenêtres de 16 plongements se terminant aux lignes `ends` (incluses)."""
    return np.stack([feats[e - ww.N_FEATURES + 1 : e + 1] for e in ends]).astype(np.float16)


_MODELS: ww.FeatureModels | None = None
_BABBLE: dict[str, list[np.ndarray]] = {}


def _feat_init(feature_dir: str, clips_dir: str) -> None:
    """Processus de calcul : modèles openWakeWord et phrases de fond."""
    global _MODELS
    _MODELS = ww.FeatureModels(Path(feature_dir))
    for split in ("train", "test"):
        _BABBLE[split] = load_clips(Path(clips_dir) / f"babble_{split}.npz")
    os.nice(5)


def _row_of_sample(sample: int) -> int:
    """Ligne de plongement (avec les 16 lignes « silence » du départ) qui suit l'échantillon."""
    return ww.N_FEATURES + sample // ww.CHUNK


def _word_task(task: tuple) -> np.ndarray:
    """Positifs / mots proches : fenêtres finissant de 0 à `span` blocs après la fin du mot."""
    word, seed, split, offsets = task
    babble = _BABBLE[split]
    rng = np.random.default_rng(seed)
    audio, end = scene(word, rng, babble, rng.uniform(0.9, 1.6), 0.5)
    feats = ww.clip_features(_MODELS, audio)
    last = _row_of_sample(end)
    ends = np.clip(last + np.asarray(offsets), ww.N_FEATURES, len(feats) - 1)
    return windows(feats, ends)


def _stream_task(task: tuple) -> np.ndarray:
    """Négatifs continus : phrases enchaînées sur un fond, toutes les `stride` fenêtres."""
    clips, seed, split, stride = task
    babble = _BABBLE[split]
    rng = np.random.default_rng(seed)
    parts = []
    for clip in clips:
        audio, _ = scene(clip, rng, babble, rng.uniform(0.05, 0.6), 0.0)
        parts.append(audio)
    audio = np.concatenate(parts)
    feats = ww.clip_features(_MODELS, audio)
    return windows(feats, np.arange(ww.N_FEATURES, len(feats), stride))


def _noise_task(task: tuple) -> np.ndarray:
    seconds, seed, split, stride = task
    babble = _BABBLE[split]
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    audio = background(n, rng, babble) * 10 ** (rng.uniform(0, 40) / 20)  # parfois fort
    feats = ww.clip_features(_MODELS, channel(np.clip(audio, -1, 1), rng))
    return windows(feats, np.arange(ww.N_FEATURES, len(feats), stride))


def read_wav(path: Path) -> np.ndarray:
    """WAV 16 kHz mono, PCM 16 bits ou mu-law (format 7) -> int16."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path}: pas un WAV")
    pos, fmt, body = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHI", data[pos + 8 : pos + 16])
        elif cid == b"data":
            body = data[pos + 8 : pos + 8 + size]
        pos += 8 + size + (size & 1)
    if fmt is None or body is None:
        raise ValueError(f"{path}: WAV incomplet")
    tag, channels, rate = fmt
    if channels != 1 or rate != SR:
        raise ValueError(f"{path}: {channels} canaux, {rate} Hz (attendu : mono 16 kHz)")
    if tag == 7:
        return mulaw_decode(np.frombuffer(body, np.uint8))
    if tag == 1:
        return np.frombuffer(body, "<i2").copy()
    raise ValueError(f"{path}: format WAV {tag} non géré")


def _real_task(task: tuple) -> np.ndarray:
    path, stride = task
    feats = ww.clip_features(_MODELS, read_wav(Path(path)))
    return windows(feats, np.arange(ww.N_FEATURES, len(feats), stride))


POS_OFFSETS = [0, 1, 2, 3]  # fin du mot + 0 à 240 ms
NEAR_OFFSETS = [-2, -1, 0, 1, 2, 3, 4]


def cmd_features(args: argparse.Namespace) -> None:
    """Mémoire bornée : extraits chargés étape par étape, tâches envoyées au fil de l'eau
    (imap), résultats recopiés dans un .npy projeté plutôt que concaténés en mémoire."""
    work = Path(args.work)
    clips = work / "clips"
    out = work / "features"
    out.mkdir(exist_ok=True)

    def train_clips(kind: str) -> list[np.ndarray]:
        """Extraits d'entraînement : voix d'origine puis voix mélangées (si générées)."""
        mix = clips / f"mix_{kind}.npz"
        return load_clips(clips / f"train_{kind}.npz") + (load_clips(mix) if mix.exists() else [])

    for split in ("train", "test"):
        if not (clips / f"babble_{split}.npz").exists():  # une phrase sur sept pour les fonds
            sentences_ = load_clips(clips / f"{split}_sentence.npz")[::7]
            save_clips(clips / f"babble_{split}.npz", sentences_, [""] * len(sentences_))
            del sentences_

    def run(name: str, fn, tasks) -> None:
        target = out / f"{name}.npy"
        if target.exists():
            return
        t0 = time.time()
        with Pool(args.jobs, _feat_init, (args.feature_dir, str(clips))) as pool:
            parts = [p for p in pool.imap(fn, tasks, chunksize=4) if len(p)]
        data = np.lib.format.open_memmap(
            target.with_suffix(".part.npy"), "w+", np.float16,
            (sum(len(p) for p in parts), ww.N_FEATURES, 96))
        row = 0
        while parts:
            part = parts.pop(0)
            data[row : row + len(part)] = part
            row += len(part)
        data.flush()
        shape = data.shape
        del data
        target.with_suffix(".part.npy").replace(target)
        print(f"{name}: {shape} en {time.time() - t0:.0f} s", flush=True)

    sub, sub_t = "train", "test"
    if not (out / "train_pos.npy").exists():
        pos = train_clips("positive")
        run("train_pos", _word_task,
            ((w, 1000 + i * 10 + k, sub, POS_OFFSETS) for i, w in enumerate(pos)
             for k in range(args.pos_aug)))
        del pos
    if not (out / "train_near.npy").exists():
        near = train_clips("near")
        run("train_near", _word_task,
            ((w, 500000 + i * 10 + k, sub, NEAR_OFFSETS) for i, w in enumerate(near)
             for k in range(2)))
        del near
    if not (out / "train_speech.npy").exists():
        speech = train_clips("sentence")
        groups = [speech[i : i + 12] for i in range(0, len(speech), 12)]
        run("train_speech", _stream_task,
            ((g, 900000 + i * 10 + k, sub, 2) for i, g in enumerate(groups) for k in range(2)))
        del speech, groups
    run("train_noise", _noise_task, ((30.0, 1300000 + i, sub, 2) for i in range(args.noise_min * 2)))
    if args.real_negatives:
        wavs = sorted(Path(args.real_negatives).glob("*.wav"))
        run("train_real", _real_task, ((str(p), 1) for p in wavs))

    # Test (voix réservées) : positifs au propre et augmentés, phrases et mots proches.
    test_pos = load_clips(clips / "test_positive.npz")
    run("test_pos", _word_task, ((w, 2000000 + i, sub_t, [0, 1, 2, 3]) for i, w in enumerate(test_pos)))
    test_near = load_clips(clips / "test_near.npz")
    run("test_near", _word_task,
        ((w, 2100000 + i, sub_t, NEAR_OFFSETS) for i, w in enumerate(test_near)))
    if not (out / "test_speech.npy").exists():
        speech_t = load_clips(clips / "test_sentence.npz")
        groups_t = [speech_t[i : i + 12] for i in range(0, len(speech_t), 12)]
        run("test_speech", _stream_task, ((g, 2200000 + i, sub_t, 1) for i, g in enumerate(groups_t)))


# ---------------------------------------------------------------- entraînement

def build_model(hidden: int = 128, dropout: float = 0.2):
    import torch.nn as nn

    return nn.Sequential(
        nn.Flatten(),
        nn.Linear(ww.N_FEATURES * 96, hidden), nn.LayerNorm(hidden), nn.ReLU(), nn.Dropout(dropout),
        nn.Linear(hidden, hidden), nn.LayerNorm(hidden), nn.ReLU(), nn.Dropout(dropout),
        nn.Linear(hidden, 1),
    )


def acav_windows(path: Path, count: int, seed: int) -> np.ndarray:
    """Fenêtres pré-calculées openWakeWord (ACAV100M, float16, (N, 16, 96)), début du fichier."""
    with open(path, "rb") as f:
        header = f.read(128)
    if not header.startswith(b"\x93NUMPY"):
        raise ValueError(f"{path}: pas un fichier .npy")
    rows = (path.stat().st_size - 128) // (16 * 96 * 2)
    data = np.memmap(path, np.float16, "r", 128, (rows, 16, 96))
    index = np.sort(np.random.default_rng(seed).choice(rows, min(count, rows), replace=False))
    return data[index]  # copie des seules lignes choisies (le fichier reste projeté)


def cmd_train(args: argparse.Namespace) -> None:
    import torch

    torch.manual_seed(0)
    torch.set_num_threads(args.jobs)
    feat = Path(args.work) / "features"
    # Mémoire bornée : fichiers projetés (mmap), un seul tableau de négatifs rempli par morceaux.
    pos = np.load(feat / "train_pos.npy")
    parts = [np.load(feat / f"train_{n}.npy", mmap_mode="r") for n in ("near", "speech", "noise")]
    if (feat / "train_real.npy").exists():
        parts.append(np.load(feat / "train_real.npy", mmap_mode="r"))
    if args.acav:
        parts.append(acav_windows(Path(args.acav), args.acav_count, 0))
    neg = np.empty((sum(len(p) for p in parts), ww.N_FEATURES, 96), np.float16)
    row = 0
    for part in parts:
        neg[row : row + len(part)] = part
        row += len(part)
    del parts
    print(f"positifs {len(pos)}, négatifs {len(neg)}", flush=True)
    model = build_model(args.hidden, args.dropout)
    noise = args.noise * float(np.std(pos[:20000].astype(np.float32)))
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-3)
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="none")
    rng = np.random.default_rng(0)
    steps = args.steps
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, args.lr, total_steps=steps)
    hard = np.zeros(0, np.int64)
    half = args.batch // 2
    for step in range(steps):
        model.train()
        pi = rng.integers(0, len(pos), half)
        ni = rng.integers(0, len(neg), half)
        if len(hard) and step > steps // 3:  # moitié des négatifs : les plus difficiles
            ni[: half // 2] = hard[rng.integers(0, len(hard), half // 2)]
        x = torch.from_numpy(np.concatenate((pos[pi], neg[ni])).astype(np.float32))
        if noise:  # bruit sur les plongements : contre l'apprentissage par cœur
            x = x + torch.randn_like(x) * noise
        y = torch.cat((torch.ones(half), torch.zeros(half)))
        # Les négatifs pèsent de plus en plus : peu de fausses détections.
        w_neg = 1.0 + (args.neg_weight - 1.0) * min(1.0, step / (0.6 * steps))
        w = torch.cat((torch.ones(half), torch.full((half,), w_neg)))
        loss = (loss_fn(model(x).squeeze(1), y) * w).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        if step % 2000 == 0 or step == steps - 1:
            scores = predict(model, neg)
            hard = np.nonzero(scores > 0.1)[0]
            recall = float(np.mean(predict(model, pos) > 0.5))
            print(f"pas {step} perte {loss.item():.4f} poids_neg {w_neg:.0f} "
                  f"rappel_train {recall:.3f} négatifs>0.5 {int(np.sum(scores > 0.5))} "
                  f"difficiles {len(hard)}", flush=True)
    export(model, Path(args.output))


def predict(model, data: np.ndarray, batch: int = 8192) -> np.ndarray:
    import torch

    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(data), batch):
            x = torch.from_numpy(data[i : i + batch].astype(np.float32))
            out.append(torch.sigmoid(model(x)).squeeze(1).numpy())
    return np.concatenate(out) if out else np.zeros(0)


def export(model, path: Path) -> None:
    import torch

    model.eval()
    full = torch.nn.Sequential(model, torch.nn.Sigmoid())
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(full, torch.zeros(1, ww.N_FEATURES, 96), str(path), input_names=["input"],
                      output_names=["score"], dynamic_axes={"input": {0: "batch"}, "score": {0: "batch"}},
                      opset_version=17, dynamo=False)
    print(f"modèle : {path} ({path.stat().st_size} octets)", flush=True)


# ---------------------------------------------------------------- évaluation

def events(scores: np.ndarray, threshold: float) -> int:
    """Détections comptées comme WakeWord.feed : une par passage au-dessus du seuil."""
    above = scores >= threshold
    return int(np.sum(above[1:] & ~above[:-1]) + (above[0] if len(above) else 0))


def onnx_scores(model_path: Path, data: np.ndarray) -> np.ndarray:
    import onnxruntime as ort

    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    return np.concatenate([sess.run(None, {"input": data[i : i + 8192].astype(np.float32)})[0][:, 0]
                           for i in range(0, len(data), 8192)])


def cmd_evaluate(args: argparse.Namespace) -> None:
    feat = Path(args.work) / "features"
    model = Path(args.model)
    pos = onnx_scores(model, np.load(feat / "test_pos.npy")).reshape(-1, 4).max(axis=1)
    near = onnx_scores(model, np.load(feat / "test_near.npy")).reshape(-1, len(NEAR_OFFSETS))
    speech = onnx_scores(model, np.load(feat / "test_speech.npy"))
    speech_h = len(speech) * 0.08 / 3600
    report = {"thresholds": {}}
    val = None
    if args.validation:  # openWakeWord : ~11 h de plongements consécutifs (parole, musique, bruit)
        rows = np.load(args.validation)
        val_data = np.lib.stride_tricks.sliding_window_view(rows, (ww.N_FEATURES, 96))[:, 0]
        val = onnx_scores(model, val_data)
        val_h = len(rows) * 0.08 / 3600
    for t in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]:
        r = {
            "recall_test_voices": round(float(np.mean(pos >= t)), 3),
            "near_miss_fp_rate": round(float(np.mean(near.max(axis=1) >= t)), 3),
            "speech_fp_per_hour": round(events(speech, t) / speech_h, 2),
        }
        if val is not None:
            r["oww_validation_fp_per_hour"] = round(events(val, t) / val_h, 2)
        report["thresholds"][str(t)] = r
        print(t, r, flush=True)
    report["test_speech_hours"] = round(speech_h, 2)
    misses = load_texts(Path(args.work) / "clips" / "test_near.npz")
    print("mots proches (score max, seuil 0.5) :",
          sorted({(round(float(s), 2), m) for s, m in zip(near.max(axis=1), misses) if s >= 0.5})[-15:])
    (Path(args.work) / "evaluation.json").write_text(json.dumps(report, indent=2))


# ---------------------------------------------------------------- fichiers de test

def write_wav(path: Path, pcm: np.ndarray, mulaw: bool = True) -> None:
    """WAV mono 16 kHz : mu-law 8 bits (format 7, comme le flux du lapin) ou PCM 16 bits."""
    body = mulaw_encode(pcm).tobytes() if mulaw else pcm.astype("<i2").tobytes()
    tag, width = (7, 1) if mulaw else (1, 2)
    fmt = np.array([tag, 1], "<u2").tobytes() + np.array([SR, SR * width], "<u4").tobytes()
    fmt += np.array([width, 8 * width, 0], "<u2").tobytes()
    chunks = b"fmt " + np.uint32(len(fmt)).tobytes() + fmt
    chunks += b"fact" + np.uint32(4).tobytes() + np.uint32(len(pcm)).tobytes() if mulaw else b""
    chunks += b"data" + np.uint32(len(body)).tobytes() + body
    path.write_bytes(b"RIFF" + np.uint32(4 + len(chunks)).tobytes() + b"WAVE" + chunks)


def cmd_export_tests(args: argparse.Namespace) -> None:
    """10 positifs et 3 min de négatifs, voix et phrases jamais vues à l'entraînement."""
    global _VOICE_DIR
    _VOICE_DIR = Path(args.voices)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random("export-tests")
    nrng = np.random.default_rng(77)
    pool = [(v, s) for v in TEST_VOICES for s in speakers(v, "test")]
    rng.shuffle(pool)
    texts = ["Lapinou", "Lapinou !", "Dis Lapinou", "Lapinou ?", "Hé Lapinou"]
    attempt = 0
    for i in range(10):
        word = None
        while word is None:
            voice, speaker = pool[attempt % len(pool)]
            word = _synth((voice, speaker, texts[i % len(texts)], "positive", f"export-{attempt}"))
            attempt += 1
        word = word / np.max(np.abs(word)) * 10 ** (nrng.uniform(-20, -6) / 20)
        bg = colored_noise(int(1.6 * SR) + len(word), nrng) * 10 ** (-62 / 20)
        bg[int(0.8 * SR) : int(0.8 * SR) + len(word)] += word
        write_wav(out / f"lapinou_{i + 1:02d}.wav", np.clip(bg * 32767, -32768, 32767).astype(np.int16))
    texts = [t for t in sentences() if held_out(t)]
    near = NEAR_MISSES
    for f in range(args.neg_files):
        parts, total = [], 0
        while total < 60 * SR:
            voice, speaker = rng.choice(pool)
            text = rng.choice(near) if rng.random() < 0.3 else rng.choice(texts)
            clip = _synth((voice, speaker, text, "sentence", f"export-neg-{f}-{total}"))
            if clip is None:
                continue
            clip = clip / np.max(np.abs(clip)) * 10 ** (nrng.uniform(-24, -6) / 20)
            gap = colored_noise(int(nrng.uniform(0.2, 0.8) * SR), nrng) * 10 ** (-62 / 20)
            parts += [gap, clip]
            total += len(gap) + len(clip)
        pcm = np.clip(np.concatenate(parts)[: 60 * SR] * 32767, -32768, 32767).astype(np.int16)
        write_wav(out / f"not_lapinou_{f + 1:02d}.wav", pcm)
    print(f"fichiers de test dans {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", default=str(Path.home() / "wakeword-train" / "work"))
    p.add_argument("--jobs", type=int, default=3)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--voices", required=True, help="dossier des voix Piper (.onnx + .onnx.json)")
    g.add_argument("--positives", type=int, default=10000)
    g.add_argument("--near", type=int, default=3000)
    g.add_argument("--sentences", type=int, default=4000)
    g.add_argument("--mix-positives", type=int, default=6000, help="voix mélangées")
    g.add_argument("--mix-near", type=int, default=3000)
    g.add_argument("--mix-sentences", type=int, default=1500)
    f = sub.add_parser("features")
    f.add_argument("--feature-dir", default=str(ww.FEATURE_DIR))
    f.add_argument("--pos-aug", type=int, default=3)
    f.add_argument("--noise-min", type=int, default=30)
    f.add_argument("--real-negatives", help="dossier de WAV enregistrés (record_negatives.py)")
    t = sub.add_parser("train")
    t.add_argument("--acav", help="début de openwakeword_features_ACAV100M_2000_hrs_16bit.npy")
    t.add_argument("--acav-count", type=int, default=300000)
    t.add_argument("--steps", type=int, default=30000)
    t.add_argument("--batch", type=int, default=1024)
    t.add_argument("--lr", type=float, default=1e-3)
    t.add_argument("--hidden", type=int, default=128)
    t.add_argument("--dropout", type=float, default=0.2)
    t.add_argument("--noise", type=float, default=0.0, help="bruit gaussien (x écart type des plongements)")
    t.add_argument("--neg-weight", type=float, default=20.0)
    t.add_argument("--output", default=str(ww.DEFAULT_MODEL))
    e = sub.add_parser("evaluate")
    e.add_argument("--model", default=str(ww.DEFAULT_MODEL))
    e.add_argument("--validation", help="validation_set_features.npy d'openWakeWord")
    x = sub.add_parser("export-tests")
    x.add_argument("--voices", required=True)
    x.add_argument("--out", default=str(SERVER / "tests" / "data"))
    x.add_argument("--neg-files", type=int, default=3)
    args = p.parse_args()
    {"generate": cmd_generate, "features": cmd_features, "train": cmd_train,
     "evaluate": cmd_evaluate, "export-tests": cmd_export_tests}[args.cmd](args)


if __name__ == "__main__":
    main()
