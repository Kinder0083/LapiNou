<!-- SPDX-License-Identifier: MIT -->
# Décisions prises pendant le développement de LapiNou v1

Ce document liste les décisions prises pendant le projet. Celles marquées **(toi)** viennent du propriétaire. Les autres ont été tranchées en cours de route pour avancer, et chacune peut être remise en question. Les numéros (R1, R2…) sont ceux du journal de travail.

## Écarts par rapport au cahier des charges

- **Le serveur pilote le lapin par le protocole LapinLibre, pas par MQTT.** Oreilles, LED, sons et chorégraphies passent par le protocole « plateforme » qui existe et qui est déjà testé. MQTT sert uniquement à Home Assistant.
- **L'annulation d'écho est faite sur le serveur, pas sur le lapin.** Le serveur sait exactement ce qu'il a fait dire au lapin, et le processeur du lapin est trop faible pour ce calcul. Le lapin marque l'instant où il commence à jouer un son.
- **Le mot « Lapinou » est reconnu par transcription (R23, R24).** Au départ, il devait l'être par un petit modèle entraîné (openWakeWord), mais l'entraînement sur des voix de synthèse a échoué. Le serveur repère maintenant les débuts de phrase, les transcrit sur place et cherche « Lapinou ». Avantage : « Lapinou, allume le salon » tient en une seule phrase. Inconvénient : un peu plus de calcul sur le serveur quand la télé parle. Le modèle entraîné a été retiré du dépôt pour une question de licence : il venait de données non commerciales.

## Mises à jour et sécurité

- **(toi) R38 : les mises à jour viennent des « releases » GitHub**, que le dépôt soit public ou privé, avec un jeton en lecture seule pour un dépôt privé. L'ancienne recherche de mises à jour de Karotz est toujours désactivée, car son adresse est morte.
- **R41, R42 : chaque mise à jour est signée.** La signature Ed25519 couvre la liste des fichiers et leurs empreintes. Sans la vraie clé publique, le lapin et le serveur refusent d'installer.
- **(toi) La clé publique s'appelle `.pub`**, parce que l'extension `.pem` est bloquée par le garde-fou de ton ordinateur.
- **R44 : la version est l'étiquette `vX.Y.Z` de la release.** Revenir à une version plus ancienne est refusé, sauf avec une option explicite.
- **R40 : page du serveur.** Elle ne répond qu'aux adresses connues. La première connexion demande un jeton à usage unique. Changer l'adresse d'un service efface son mot de passe enregistré.
- **R46 : premier mot de passe de la page du lapin.** Pour le créer, il faut appuyer sur la tête du lapin dans les 60 secondes, pendant que sa LED clignote. C'est une preuve qu'on est devant lui.
- **R33, R34 : actions sensibles.** Serrures, alarmes, portes de garage et scripts dont on ne peut pas lire le contenu sont traités comme sensibles. Ils sont désactivés par défaut, à cause des enfants. Le mode de secours Home Assistant (Assist) refuse aussi les phrases qui en parlent.
- **R48 : codes PIN et confirmations.** La réponse à une demande de confirmation n'est jamais envoyée à l'IA, jamais écrite en clair dans les journaux et jamais enregistrée.

## Vie privée

- **R49 : le micro n'envoie le son au serveur que si le serveur est activé ET qu'une adresse est remplie.** Sans serveur, aucun son ne quitte le lapin, sauf en mode sans serveur quand tu appuies longuement sur la tête.
- En mode serveur, seul du texte part sur internet, vers l'IA. La voix reste à la maison.

## L'IA et le budget

- **R35 : mode sans serveur.** Il utilise le modèle rapide (Haiku) par défaut pour rester sous 3 secondes. Opus peut être choisi dans les réglages.
- **R32, R36 : si l'IA refuse de répondre**, Lapinou dit une phrase douce et sa LED passe à l'orange, au lieu d'un son d'erreur.
- **R50 : budget épuisé en pleine réponse.** Lapinou finit par « J'ai atteint mon budget du mois… », sans relancer la demande ailleurs. Ce qui a déjà été consommé est compté.
- **R51 : voix claire.** La voix Piper sonnait caverneuse. Parmi 8 essais écoutés sur le PC puis sur le lapin, le propriétaire a choisi le n° 2 : même hauteur, moins de graves, un peu plus de clarté. Le serveur filtre chaque phrase par ffmpeg (`[voice] clarity`, activé par défaut). Si ffmpeg échoue, la voix d'origine est gardée. Une voix plus expressive (voix en ligne dirigée) reste une piste pour plus tard.

## Le lapin

- **R47 : un lapin = un identifiant**, celui du fichier `uuid.conf`. ⚠️ **Conséquence pour toi :** au prochain démarrage, ton lapin change d'identifiant. Il passe de `lapinlibre` à son uuid. Si le serveur a une section `[rabbits.lapinlibre]`, il faudra la renommer.
- **R27 : un seul réglage d'adresse.** `LAPINOU_SERVER=<adresse>` sur la clé USB règle aussi la plateforme, la voix et la reconnaissance vers le même serveur.
- **R28 : appui long sans serveur ni IA configurés.** Il ne fait rien, comme dans LapinLibre.
- **R26 : phrase « Je n'arrive pas à me connecter. »** Elle est générée avec la voix Piper « siwis » (licence CC BY 4.0), parce que l'autre voix demandait un téléchargement de plusieurs Go.
- **R39 : les notifications de Home Assistant sont prononcées par le lapin lui-même.** Pendant qu'il parle, et une demi-seconde après, dire « Lapinou » ne l'interrompt pas. Appuyer sur sa tête l'interrompt toujours.
- **R37 : le lapin a été flashé avec la dernière version entièrement relue à ce moment-là.** La suite arrive par mise à jour.

## Design

- **(toi) R45 : LapiNou a son propre style, ludique et coloré**, avec un thème clair ou sombre automatique et les animations réduites si l'appareil le demande. Le lapin a une page d'accueil et une page Réglages dans ce style. Les anciens outils LapinLibre restent sous « Avancé ». Le serveur utilise les mêmes couleurs et le même dessin de lapin.

## Serveur et licences

- **R29 : le conteneur Proxmox tourne sous Debian 13.** Par défaut, il a 4 cœurs, 6 Go de mémoire et 20 Go de disque.
- **R30 : la voix Piper (licence GPL) tourne comme un programme séparé**, pour que le serveur LapiNou reste sous licence MIT.
- **R25, R31 : les modèles de reconnaissance et de voix sont téléchargés à l'installation**, à des versions précises vérifiées par empreinte. Ils ne sont pas stockés dans le dépôt.
- **R43 : l'annonce automatique des applications LapinLibre tierces n'est pas dans la v1.**

## À vérifier sur le vrai lapin (essais T27)

- Coupure de courant pendant une mise à jour du noyau ou du système.
- Le programme d'aide de la page web garde-t-il bien ses droits sur la mémoire du lapin ?
- Mémoire et processeur utilisés.
- Délai de réponse de 3 secondes au plus.
- Annulation d'écho réelle.
- Appui réel sur la tête pour le premier mot de passe.
- Format réel de ha-mcp.
- Essai de migration à blanc.
- Délai d'écho après une notification, et attente du micro au démarrage (mesurés seulement en simulation).

## Petits points connus, laissés pour plus tard

- Deux notifications Home Assistant très rapprochées peuvent empêcher « Lapinou » de réveiller le lapin pendant au plus 2 minutes. Le bouton sur la tête marche toujours. À mesurer lors des essais.
- Quand plusieurs lapins se reconnectent en même temps, le journal du serveur peut afficher à tort « identité incohérente ». Ce n'est qu'une ligne de journal.
- Si le détecteur du mot d'activation ne démarre pas pour une autre raison que le modèle, seul le bouton réveille le lapin. Un message le signale dans le journal.
- Seul Anthropic compte le coût d'une réponse coupée en cours de route. OpenAI ne donne ce coût qu'à la fin d'une réponse.
