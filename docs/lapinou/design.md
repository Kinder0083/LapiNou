<!-- SPDX-License-Identifier: MIT -->
# L'identité visuelle de LapiNou

Ludique et colorée (R45) : une boîte à jouets. Le lapin dessiné est le héros de la page ; les
commandes sont des « jouets » épais et arrondis, avec une ombre pleine décalée plutôt qu'un flou.
Thème clair ou sombre selon l'appareil (`prefers-color-scheme`), animations coupées avec
`prefers-reduced-motion`, contrastes WCAG AA, textes simples en français.

Les jetons sont dans `image/core/usr/www/lapinou/tokens.css` et le lapin dans
`image/core/usr/www/lapinou/lapin.svg`. Ces deux fichiers sont faits pour être repris tels quels par
l'interface du serveur (suite prévue : T23b). Aucune ressource extérieure, aucune police téléchargée,
aucune étape de construction.

## Couleurs

| Jeton | Clair | Sombre | Rôle |
|---|---|---|---|
| `--ln-sky` | `#e9f5ff` | `#121c33` | le fond de la page |
| `--ln-paper` | `#ffffff` | `#1c2a4a` | les panneaux |
| `--ln-cloud` | `#d6ebfb` | `#2a3c63` | remplissages calmes, onglets au repos |
| `--ln-ink` | `#1b2a4a` | `#f2f6ff` | le texte, le trait du dessin et des panneaux |
| `--ln-ink-soft` | `#46597d` | `#b9c6e4` | le texte secondaire (AA sur `sky` et `paper`) |
| `--ln-carrot`, `--ln-raspberry`, `--ln-lemon`, `--ln-mint`, `--ln-lagoon`, `--ln-lilac` | | (identiques) | les couleurs vives des jouets, des pastilles et des liserés |
| `--ln-on-bright` | `#1b2a4a` | `#1b2a4a` | le texte posé sur une couleur vive, dans les deux thèmes |
| `--ln-carrot-deep` | `#b8460a` | `#ffa45c` | la carotte en texte, l'anneau de focus |
| `--ln-ok-*`, `--ln-bad-*`, `--ln-warn-*` | | | messages : fond (`-bg`) et texte (`-ink`) |
| `--ln-led` | `#3ddc97` | | la couleur du nez du lapin, mise par la page selon la LED |

Règle : jamais de texte clair sur une couleur vive ; toujours `--ln-on-bright`.

## Formes

Plus la chose est grande, plus elle est ronde : `--ln-radius-blob` (36 px, la bulle du lapin),
`--ln-radius-panel` (24 px, panneaux), `--ln-radius-field` (14 px, champs), `--ln-radius-pill`
(boutons, onglets, pastilles). Trait `--ln-border` (3 px) ; ombre « jouet » `--ln-lift`
(décalage plein de 5 px). Les boutons s'enfoncent au clic (l'ombre disparaît).

## Texte

`--ln-font` : la police arrondie de l'appareil quand il en a une (`ui-rounded`, SF Pro Rounded,
Nunito, Varela Round), sinon celle du système. Échelle `--ln-step--1` à `--ln-step-3` (0,875 à
2,3 rem). Titres en graisse 900, en minuscules (pas de capitales d'affiche).

## Mouvement

`--ln-bounce` pour les oreilles. À l'ouverture, les oreilles remuent deux fois ; le halo du nez
respire doucement. Les oreilles prennent la position commandée. Rien de tout cela avec
`prefers-reduced-motion: reduce`.

## Le lapin

`lapin.svg` (240 × 300) : corps rond, deux longues oreilles, le nez LED, comme un Karotz. La page le
charge et le met dans le document (DOMParser), pour que ses classes suivent la feuille de style :
`.ln-ear-left` et `.ln-ear-right` tournent autour de leur base, `.ln-led` et `.ln-glow` prennent
`--ln-led`, `.ln-fur` le pelage. Ouvert seul, le fichier garde ses couleurs.
