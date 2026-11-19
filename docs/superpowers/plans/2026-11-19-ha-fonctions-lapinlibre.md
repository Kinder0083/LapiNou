<!-- SPDX-License-Identifier: MIT -->
# Fonctions LapinLibre dans Home Assistant : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `lapinou-mqtt` expose radio, humeurs, histoires, commandes de lecture, l'heure, les
oreilles au hasard et le sommeil/réveil (silencieux) à Home Assistant.

**Architecture:** de nouvelles commandes `cmd/*` lancent les programmes de la page web par
`cgi_run` ; trois listes, lues par les programmes de lecture de la page, alimentent trois `select`
de la découverte.

**Tech Stack:** C (glib, dbus-glib, paho), scénario `tests/scenarios/lapinou_mqtt.py` sous qemu.

**Spec:** `docs/superpowers/specs/2026-11-19-ha-fonctions-lapinlibre-design.md`

## Global Constraints

- Mêmes programmes que la page web : aucune action ne passe ailleurs que par `cgi_run`.
- Liste vide → découverte vide retenue (entité retirée) ; au plus 200 options par liste.
- Libellés en double : « libellé (identifiant) ».
- Rafraîchissement des listes : connexion, « online » de HA, toutes les 300 s ; découverte des
  `select` republiée seulement si la liste a changé.
- Commits avec `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- Une station dont le nom contient guillemets, accents ou `|` : option correcte, URL bien passée.
- Clé retirée pendant que le pont tourne : les trois `select` disparaissent au prochain relevé.
- Un programme de lecture qui échoue ou répond mal : la liste précédente est gardée.
- `cmd/mood` avec un libellé inconnu : journal, aucun programme lancé.
- Une réponse de liste plus grande que `JOB_OUT_MAX` : tronquée → refusée proprement, pas de
  plantage.

---

### Task 1 : listes et découverte

**Files:** `userland/src/usr/lapinou-mqtt/mqtt-json.[ch]` (lecteur d'un tableau d'objets plats sous
une clé), `discovery.[ch]` (trois listes dans `DiscoveryContext`, les nouvelles entités),
`lapinou-mqtt.c` (relevé des listes), `tests/scenarios/lapinou_mqtt.py` (radios.txt dans le bac à
sable, vérification des options), `tests/expected/lapinou_mqtt.txt`.

- Interfaces : `GPtrArray *json_parse_object_list(const char *text, gsize len, const char *key)`
  (tableau de `GHashTable*` plats, NULL si la forme ne va pas) ; `DiscoveryContext` gagne
  `const GPtrArray *radios, *moods, *stories` (libellés) ; un `select` vide donne un message à
  charge utile vide.
- Étapes : écrire les vérifications du scénario (options des trois `select`, boutons présents) →
  les voir échouer → implémenter → réenregistrer → relire le diff de l'enregistrement.

### Task 2 : commandes

**Files:** `lapinou-mqtt.c` (`cmd_radio`, `cmd_mood`, `cmd_mood_random`, `cmd_story`,
`cmd_media`, `cmd_clock`, `cmd_ears_random`, `cmd_sleep` « silent »), scénario et enregistrement.

- Étapes du scénario (avant le code) : chaque commande lance le bon programme avec la bonne
  requête (appels D-Bus enregistrés par le faux `dbus-send`) ; libellé inconnu → journal ; clé
  retirée → `select` retirés au relevé suivant.
- Puis `docs/lapinou/mqtt.md` (commandes et entités), `CHANGELOG.md`, version 0.3.4.

### Task 3 : vérification

`bash tests/run.sh lapinou_mqtt` vert ; suite complète sur la VM ; essai sur le vrai lapin après
publication (avec l'accord du propriétaire pour l'étiquette).
