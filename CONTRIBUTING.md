<!-- SPDX-License-Identifier: MIT -->
# Contribuer à LapiNou

Merci de votre intérêt ! LapiNou est un petit projet familial, construit sur LapinLibre : toute
aide est bienvenue, en français ou en anglais.

## Vous avez un Karotz ?

C'est la meilleure aide qui soit : les tests sur de vrais lapins.

- Installez LapiNou (voir [docs/lapinou/demarrage.md](docs/lapinou/demarrage.md)) et dites-nous ce
  qui marche ou non : ouvrez une *issue* avec le modèle du lapin, la version (`sys_version`, page
  du lapin) et ce que dit le journal ; ne joignez jamais le contenu de `/usr/etc/conf` (vos
  mots de passe y sont).
- Le mot d'activation gagne à entendre de vraies voix et de vraies pièces : une mesure de fausses
  détections chez vous (télé, conversation) est précieuse ([mot-activation.md](docs/lapinou/mot-activation.md)).
- **Avant toute écriture en mémoire flash**, faites la sauvegarde complète de votre lapin ; ne
  touchez jamais aux partitions de secours.

## Proposer un changement

1. Une *issue* d'abord pour un changement important, pour en parler.
2. Une branche, des commits courts et clairs, en français de préférence.
3. Chaque fichier porte `SPDX-License-Identifier: MIT` dans ses 5 premières lignes, fins de ligne
   LF, pas de lien symbolique : `python -I scripts/check-repo.py` le vérifie.
4. Tests : `cd server && uv sync && uv run pytest -q` pour le serveur,
   `bash installer/tests/run.sh` pour l'installateur, `./build.sh test` pour le firmware (Linux ou
   WSL, voir [tests/README.md](tests/README.md)). Un test qui échoue d'abord, puis le code.
5. Jamais de secret (clé, jeton, mot de passe) dans un commit : le dépôt peut être public.

En contribuant, vous acceptez que votre contribution soit publiée sous la licence MIT du projet
([LICENSE](LICENSE)).
