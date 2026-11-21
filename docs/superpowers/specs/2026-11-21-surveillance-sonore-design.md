<!-- SPDX-License-Identifier: MIT -->
# Surveillance sonore — conception courte

## Intention

Quand la maison est sous alarme, le lapin écoute : un bruit ou une voix dans la pièce devient un
événement dans Home Assistant, avec un enregistrement de 10 s et, s'il y a de la parole, ce qui a
été dit. Le propriétaire décide dans Home Assistant de ce qui suit (notification, sirène,
lumières, le lapin qui parle).

## Objectifs, non-objectifs, anti-objectifs

- Objectifs : un interrupteur « Surveillance sonore » par lapin ; pendant la surveillance, un
  niveau sonore, « Bruit détecté », « Voix détectée », un seuil de bruit réglable et un
  événement par enregistrement (lien du fichier, texte entendu, niveau).
- Non-objectifs : classer les sons (verre brisé, chien) ; écouter en direct ; un réglage par
  pièce ou par heure (Home Assistant le fait avec ses automatisations).
- Anti-objectifs : rien n'est gardé ni publié hors surveillance ; les sons que le lapin joue
  lui-même ne déclenchent rien ; l'état armé survit à un redémarrage du serveur (une alarme qui
  se désarme seule serait pire que pas d'alarme).

## Contraintes

- Le lapin envoie déjà son micro au serveur en continu (lapinou-mic, 16 kHz) : tout se fait
  sur le serveur, rien ne change sur le lapin.
- Les entités vont dans l'appareil « Lapinou » que lapinou-mqtt déclare (`identifiers`
  `lapinou_<id>`), par le broker de `[mqtt]` du serveur ; sans `mqtt.host`, pas de surveillance.
- Le serveur peut s'arrêter : les entités sont « indisponibles » quand le serveur ou le lapin
  l'est (deux disponibilités, mode `all`).

## Approches

1. **Sur le serveur, à partir du flux micro existant (retenue).** Le niveau, le seuil et Silero
   VAD (déjà chargé pour le mot d'activation) tournent sur le flux déjà reçu ; le serveur
   publie ses propres entités MQTT dans l'appareil du lapin. Aucun changement sur le lapin.
2. Sur le lapin (lapinou-mqtt mesure le niveau sur la socket du micro). Rien sans serveur pour
   le niveau, mais pas de VAD ni d'enregistrement sur un processeur de 2010 : écartée.
3. Un enregistrement continu côté serveur, découpé après coup : plus de disque, plus
   d'exposition, rien de mieux pour Home Assistant : écartée.

## Ce qui est décidé

- Niveau : RMS de chaque bloc reçu (≈ 128 ms) en dBFS ; publié au plus une fois par seconde
  (le maximum de la seconde), seulement s'il change de 2 dB ou plus, ou toutes les 30 s.
- Bruit : niveau au-dessus du seuil (défaut −40 dBFS, réglable de −70 à −10 dans Home Assistant)
  pendant au moins 300 ms d'affilée ; il retombe après 5 s calmes.
- Voix : Silero VAD (seuil de LapinLibre) ; retombe après 5 s sans parole.
- Pendant que le lapin joue un son (réponse, lecture locale), les blocs ne comptent pas pour la
  détection (l'écho est déjà retiré, c'est une sécurité de plus).
- Enregistrement : au début d'un bruit ou d'une voix, 10 s en tout, dont les 3 s d'avant
  (mémoire tournante) ; si le bruit ou la voix dure encore à la fin, un nouvel enregistrement
  suit aussitôt. WAV 16 kHz mono dans `<dossier de la base>/surveillance/clips/`, nom avec un
  jeton aléatoire de 128 bits ; servi par `GET /surveillance/clips/<jeton>.wav`, sans
  connexion (le jeton est le secret, comme les MP3 de /tts/). Gardés 7 jours, 500 au plus.
- Si une voix a été entendue, la parole du clip est transcrite (Parakeet, déjà chargé).
- Événement « Événement sonore » (entité `event`, types `bruit` et `voix`) à la fin de chaque
  enregistrement : `enregistrement` (URL), `texte`, `niveau` (crête en dBFS), `debut`.
- Réglages `[surveillance]` du serveur (valeurs ci-dessus), et `[mqtt] discovery_prefix`
  (`homeassistant`). L'état armé et le seuil de chaque lapin sont gardés dans
  `surveillance/state.json`.

## Laissé au constructeur

Noms internes des classes, découpage en fichiers, format exact des journaux.
