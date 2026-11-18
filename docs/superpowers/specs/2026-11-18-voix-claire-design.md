<!-- SPDX-License-Identifier: MIT -->
# Voix claire de Lapinou (conception courte)

## Intention

Le propriétaire trouve la voix de Lapinou (Piper `fr_FR-siwis-medium`) caverneuse et robotique ;
il veut une voix de personnage plus fun. Après écoute de 8 essais sur le PC puis sur le lapin, il a
choisi l'essai 2 : même hauteur, moins de graves, un peu plus de clarté.

## Objectifs, non-objectifs

- Objectif : chaque phrase synthétisée par le serveur passe par le filtre de l'essai 2.
- Non-objectifs : changer de moteur ou de voix, la hauteur, la vitesse, l'émotion (piste « voix en
  ligne dirigée », à décider plus tard) ; la phrase « Je n'arrive pas à me connecter » embarquée
  dans le lapin.
- Anti-objectif : un son différent de celui écouté, ou un Lapinou muet si le filtre échoue.

## Approches

- **A. Transformer la voix Piper** (hauteur, graves) : retenue, réduite au seul filtre de l'essai 2.
- B. Voix en ligne dirigée : écartée pour l'instant (envoi du texte, coût, développement).
- C. A puis B : B reste possible plus tard.

Réalisation du filtre :

- **ffmpeg** (retenu, choix de Claude) : le filtre exact écouté ; ffmpeg est déjà un prérequis
  du serveur.
- Filtre en numpy : son légèrement différent (ou scipy, lourde dépendance de plus), écarté.

## Conception

- Filtre (celui de l'essai) : `highpass=f=180,equalizer=f=250:t=q:w=1:g=-4,equalizer=f=3200:t=q:w=1.2:g=3`,
  appliqué au WAV du moteur avant l'encodage MP3, à la même fréquence et en 16 bits mono.
- Réglage `[voice] clarity` (booléen, `true` par défaut) ; `false` : voix d'origine.
- S'applique à `render_mp3`, donc au lapin (`speak`), au cache des notifications et au TTS de
  compatibilité du port 8081.
- Si ffmpeg échoue : journal (avertissement) et voix d'origine, jamais de silence.
- Coût attendu : environ 0,1 s par phrase ; mesuré et noté dans `docs/lapinou/mesures.md`.

## Laissé au constructeur

Le nom exact de la fonction, la forme des tests (ffmpeg simulé ou réel), la place dans la doc.
