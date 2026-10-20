# Chat

Français · [English](Chat-en.md)

L'onglet **Chat** sert à poser des questions en langage naturel sur vos documents. La
réponse s'appuie sur des **extraits retrouvés** dans votre bibliothèque, pas sur la seule
mémoire du modèle d'IA.

<!-- TODO capture : une réponse avec citations et panneau des sources ouvert -->

## Poser une question

Tapez votre question et validez avec **Entrée**. Quelques exemples :

- « Que disent mes articles sur l'effet de la sédentarité sur la mémoire ? »
- « Quelles méthodes sont utilisées pour mesurer l'attention dans ce corpus ? »
- « Quelles sont les limites des études de plus de dix ans ? »

Si aucun article ne répond, Bibliobrain le dit au lieu d'inventer.

Les boutons à côté de la flèche d'envoi choisissent la profondeur : **Auto** convient
presque toujours, **Rapide** répond en quelques secondes, **Approfondi** cherche plus
largement pour les questions difficiles.

## Lire une réponse

- Les **citations** dans le texte sont cliquables : elles ouvrent l'article dans la
  Bibliothèque.
- **Voir les sources** ouvre un panneau latéral avec les passages retenus et leur page.
  La « proximité » d'un passage ne veut pas dire qu'il prouve la réponse : relisez
  l'extrait.
- Le bouton **Graphe RAG** sous une réponse ouvre **Cartographies > Dernière réponse** :
  quels articles et quels passages ont servi (voir [Cartographies](Cartographies.md)).
- Chaque réponse a un bouton **Copier**.
- Avec les modèles qui détaillent leur raisonnement, un bloc *Thinking* peut s'afficher.

## Restreindre les sources

Si Zotero est connecté, **Options > Filtres Zotero** (sous le champ de saisie) limite le
chat à certains **tags** ou **collections** ; le bouton de réinitialisation du panneau
retire les filtres.

Pour une question sur quelques articles précis, cochez-les dans la
[Bibliothèque](Bibliotheque.md) puis **Questionner dans le chat**. Pour un travail plus
ciblé, créez un [Carnet](Carnets.md) : son chat n'interroge que ses sources.

## Aide sur Bibliobrain

Vous pouvez aussi demander comment utiliser le logiciel : « Comment connecter Zotero ? »,
« Comment créer un carnet ? » ou « Comment utiliser Bibliobrain ? ». Pour demander
explicitement de l'aide, commencez par `/aide` ou `/help`, suivi de votre question.

L'assistant consulte alors les pages pertinentes du wiki fourni avec l'application et
ajoute leurs liens à sa réponse. Cette aide fonctionne sans articles indexés. Les liens
ouvrent le wiki en ligne ; la consultation des extraits par l'assistant reste locale.
L'aide utilise le service IA choisi dans les Paramètres, comme les autres réponses du chat.

## Aide sur Bibliobrain

Vous pouvez aussi demander comment utiliser le logiciel : « Comment connecter Zotero ? »,
« Comment créer un carnet ? » ou « Comment utiliser Bibliobrain ? ». Pour demander
explicitement de l'aide, commencez par `/aide` ou `/help`, suivi de votre question.

L'assistant consulte alors les pages pertinentes du wiki fourni avec l'application et
ajoute leurs liens à sa réponse. Cette aide fonctionne sans articles indexés. Les liens
ouvrent le wiki en ligne ; la consultation des extraits par l'assistant reste locale.

La documentation est chargée à la première demande d'aide, puis conservée en mémoire.
Les questions sur vos articles ne chargent pas cette documentation et n'ajoutent aucun
appel au modèle pour cette fonction. L'aide utilise le service IA choisi dans les
Paramètres, comme les autres réponses du chat.

## Commandes rapides

Vous pouvez écrire en langage naturel (« fais une revue de littérature sur… ») ou taper
une commande au début du message :

| Commande | Ce qu'elle fait |
|---|---|
| `/liste …` ou `/papers …` | Liste tous les articles de votre bibliothèque sur un sujet, avec un court résumé chacun |
| `/review …` ou `/revue …` | Revue de littérature : vue d'ensemble, chronologie, courants, désaccords, synthèse |
| `/compare …` | Compare des articles (méthode, hypothèses, résultats, limites) |
| `/matrice …` ou `/evidence …` | Tableau d'affirmations avec leurs preuves et leurs limites |
| `/factcheck …` ou `/verify …` | Vérifie un passage contre votre bibliothèque, affirmation par affirmation |
| `/biblio …` | Bibliographie des sources déjà citées dans la conversation |
| `/origine …` ou `/origin …` | Cherche à qui les articles de votre bibliothèque attribuent une idée, en remontant leurs citations |
| `/md …` ou `/latex …` | Exporte une réponse ou la conversation en Markdown ou en LaTeX |
| `/carnet …` | Crée un carnet à partir des sources de la dernière réponse |
| `/discover …` | Cherche de nouveaux articles en ligne (voir [Découverte](Decouverte.md)) |
| `/rag …` | Force une réponse classique sur la bibliothèque |

Sur un chat vide, des suggestions (**Trouver**, **Synthétiser**, **Comparer**, **Citer**)
préremplissent ces demandes ; **Plus de suggestions**, ou le bouton **Options** sous le
champ de saisie, en propose d'autres (Lister, Revue, Preuves, Bibliographie, Markdown…).

## Remonter à la source d'origine d'une idée

`/origine` suivi d'une idée (ou une question comme « qui a introduit le modèle de
l'état critique ? ») cherche l'article de votre bibliothèque qui en parle le plus, lit
le passage correspondant et regarde à quelles références il attribue l'idée. Si une
de ces références est aussi dans votre bibliothèque, Bibliobrain la lit à son tour, sur
quelques niveaux. La réponse donne la chaîne de citations et la **plus ancienne source
créditée trouvée** ; chaque article lu apparaît dans les Sources avec le passage lu.

Pour partir d'un article précis, limitez d'abord la question à cet article. Tout se
fait sur votre ordinateur. Le résultat n'est pas une preuve d'originalité : une idée
peut être plus ancienne que les sources citées, et un petit modèle se trompe plus
souvent dans les attributions. Vérifiez toujours les passages.

## Recherche en ligne depuis le chat

Si votre message ressemble à une recherche d'articles (« cherche des articles sur… »),
Bibliobrain peut lancer une recherche en ligne, **seulement si vous l'avez autorisée** dans
les Paramètres. Il vous demande alors si la recherche doit rester locale ou aller sur le
web.

## Conversations

Vos conversations sont listées sous **Chat** dans la barre latérale ; **Toutes les
conversations** les affiche toutes, avec une recherche. Le bouton **+** à côté de **Chat**
(**Nouveau chat**) repart d'une page propre.
