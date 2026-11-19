<!-- SPDX-License-Identifier: MIT -->
# Fonctions LapinLibre dans Home Assistant (conception courte)

## Intention

Le propriétaire pilote surtout le lapin depuis Home Assistant (MQTT). La page avancée du lapin
(`avance.html`) sait faire plus que ce que `lapinou-mqtt` expose. Il veut ces fonctions dans HA.

## Objectifs, non-objectifs

- Objectifs (validés par le propriétaire le 2026-11-19) :
  - `select` **Radio** (stations de `radios.txt` sur la clé), **Humeur**, **Histoire** ;
  - boutons **Humeur au hasard**, **Pause**, **Reprendre**, **Stop**, **Suivant**, **Précédent**,
    **Dire l'heure**, **Oreilles au hasard**, **S'endormir**, **Se réveiller**,
    **Se réveiller en silence**.
- Non-objectifs : la gestion des badges RFID (reste sur la page web ; `event.lapinou_badge` sert
  de déclencheur), la galerie de photos, les journaux, le redémarrage, le choix de la voix.
- Anti-objectif : un comportement différent de la page web pour la même action (le pont lance
  les mêmes programmes).

## Approches

- **A. Le pont lance les programmes de la page web** (`cgi_run`, comme `cmd/sound`, `cmd/sleep`) :
  retenue. Une seule implémentation de chaque action ; les verrous, la veille et l'état de lecture
  restent ceux de la page.
- B. Le pont appelle les démons sur D-Bus lui-même : écartée (double les règles de la page).
- C. Le serveur LapiNou expose ces fonctions : écartée (le mode sans serveur et HA passent par le
  pont du lapin).

## Conception

- Les listes viennent des programmes de lecture de la page (`radios_list`, `moods_list`,
  `stories_list`) : lues à la connexion au broker, au « online » de HA et toutes les 5 minutes
  (clé insérée ou retirée) ; la découverte des trois `select` est republiée seulement quand une
  liste change. Une liste vide : le `select` est retiré (découverte vide, retenue).
- Une option de `select` est le libellé de la page (le nom de la station, le texte de l'humeur, le
  nom de l'histoire ; l'identifiant s'il est vide). Deux libellés égaux : le second prend
  « (identifiant) » en plus. Au plus 200 options par liste.
- Commandes (sur `<préfixe>/<id>/cmd/…`) :
  - `radio <libellé>` → `sound?url=<url de la station>` ;
  - `mood <libellé>` → `apps/moods?id=` ; `mood_random` → `apps/moods` sans id ;
  - `story <libellé>` → `stories?id=` ;
  - `media pause|resume|stop|next|previous` → `sound_control?cmd=` ;
  - `clock` → `apps/clock` ; `ears_random` → `ears_random` (positions de sa réponse dans l'état) ;
  - `sleep on|off|silent` (`silent` → `wakeup?silent=1`) : l'interrupteur « Veille » reste.
- Un libellé inconnu ou un refus de la page : une ligne au journal, rien d'autre.
- Les `select` sont sans état (optimistes) : la page ne dit pas ce qui joue.
- Micrologiciel v0.3.4 ; le serveur ne change pas.

## Laissé au constructeur

La forme exacte du lecteur JSON des listes, les icônes, l'ordre des entités, la doc
(`docs/lapinou/mqtt.md`).
