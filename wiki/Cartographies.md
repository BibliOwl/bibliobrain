# Cartographies

Français · [English](Maps-en.md)

L'onglet **Cartographies** vous aide à vous repérer dans votre corpus et à comprendre
d'où viennent les réponses du chat. Quatre vues se choisissent en haut de l'onglet.

<!-- TODO capture : vue « Depuis des articles » -->

## Depuis des articles

Choisissez un ou plusieurs articles de la bibliothèque, puis **Construire le graphe** : vous
obtenez leur graphe de citations (voir plus bas) directement dans l'onglet.

- **Ajouter un article** cherche dans la bibliothèque par titre, auteur ou année ;
  **Entrée** ajoute le premier résultat.
- Trois boutons ajoutent d'un coup la **Sélection de la bibliothèque**, les **Sources de la
  dernière réponse** du chat ou les **Articles d'un carnet**.
- Jusqu'à 60 articles. **Changer les articles** revient au choix sans le perdre.
- Dans la Bibliothèque, sélectionnez des articles puis **Graphe de citations** dans la
  barre de sélection : l'onglet s'ouvre et le graphe se construit aussitôt.

Cette vue a besoin de la recherche en ligne (OpenAlex, voir plus bas).

## Vue d'ensemble

Une carte de toute votre bibliothèque par thèmes, pour naviguer dans un gros corpus.

- **Rechercher un concept** filtre la carte.
- **Réinitialiser** revient à la vue d'ensemble.
- **Ajouter** crée un carnet à partir des articles affichés.
- Après un gros ajout d'articles, **⋯ (Plus d'actions) > Recalculer la carte** la
  reconstruit.

C'est une aide à la navigation, pas un graphe de faits extraits par l'IA.

## Dernière réponse

Cette vue montre la question posée au chat, les articles retrouvés et les passages qui ont
servi à répondre (page, extrait). Le bouton **Graphe RAG** sous une réponse du chat l'ouvre
directement. C'est le moyen le plus simple de vérifier sur quoi une réponse s'appuie.

## Auteur ou labo

*(bêta)* Cherchez un auteur ou un laboratoire par son nom, son ORCID ou son ROR,
sélectionnez la bonne identité, puis **Construire le graphe** : vous obtenez le graphe de
citations de ses publications.

## Graphes de citations

Ils s'ouvrent pour un article (Bibliothèque, **Graphe de citations**), pour un carnet, pour
des articles choisis (vue **Depuis des articles**), ou pour un auteur ou un laboratoire. Ils utilisent OpenAlex : la recherche en ligne doit être
autorisée. Le temps se lit de gauche à droite :

- **Un article** : à gauche ce qu'il cite (**← Ce qu'il cite**), au centre l'article, à
  droite les travaux qui le citent (**Qui le cite →**). La position donne l'année, la
  taille d'un point le nombre total de citations.
- **Plusieurs articles** (articles choisis, carnet, auteur) : trois bandes, les références citées, les
  articles du corpus et les travaux citants, sur un même axe des années. La taille d'un
  point donne le nombre de liens dans ce graphe.
- Les filtres en haut masquent les références, les citants ou les liens internes ; le
  champ **Auteur ou titre…** retrouve un point.
- Un clic sur un point (**Référence citée**, **Travail citant**…) affiche son résumé et des
  actions : **Ajouter au carnet**, **Voir dans la bibliothèque**, **Explorer ses
  citations**, **Rechercher dans Découverte** ou la page de l'éditeur.

Une note signale les travaux sans date, qui ne sont pas placés sur l'axe du temps, et les
sources sans DOI, absentes du graphe.
