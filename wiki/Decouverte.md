# Découverte

Français · [English](Discovery-en.md)

L'onglet **Découverte** cherche de **nouveaux** articles en ligne et vous aide à les
repérer : Semantic Scholar, OpenAlex, arXiv, HAL, Crossref, et Unpaywall pour les versions
en accès libre.

> **Prérequis.** Activez **Autoriser la recherche sur le web** dans
> **Paramètres > Trouver des articles en ligne**, ou cliquez sur **Autoriser la recherche
> en ligne** quand Découverte indique que la recherche en ligne est désactivée. Seule votre
> question (mots-clés, titres, DOI) est envoyée ; vos PDF restent sur votre ordinateur.

<!-- TODO capture : résultats d'une recherche -->

## Trouver des articles

Choisissez le point de départ :

- **Décrire un sujet** : quelques phrases, un résumé, des mots-clés, des auteurs ou un DOI.
- **À partir d'un carnet** : choisissez le **Carnet source** ; ses articles et ses notes
  suffisent, une précision est facultative.

Réglez au besoin le nombre de **Résultats** (de 1 à 50), puis **Rechercher**. Chaque
résultat explique pourquoi il correspond à votre recherche.

## Trier et comparer les résultats

- Filtrez par **Année**, gardez les **PDF en accès libre**, ou séparez les articles
  **Déjà présents** dans votre bibliothèque de ceux **Non retrouvés**.
- **Trier par** pertinence, du plus récent ou du plus ancien.
- Sélectionnez jusqu'à 4 articles puis **Comparer** : un tableau extrait des résumés
  (méthode, résultats…), avec un passage justificatif pour chaque case.
- **Trouver des articles similaires** relance une recherche à partir d'un résultat ;
  **Ajouter à un carnet…** le range dans un carnet (titre et résumé seulement).

## Retrouver une recherche

Vos recherches sont listées sous **Découverte** dans la barre latérale (**Toutes les
recherches** pour les voir toutes). Vous pouvez en relancer une, la renommer ou la
supprimer ; une recherche relancée signale les **nouveaux** articles depuis la dernière
fois. Depuis le chat, `/discover …` fait la même chose (voir [Chat](Chat.md)).

## Tendances

La vue **Tendances** compte, année après année, les publications indexées par OpenAlex
sur un à trois sujets : saisissez les **Sujets à comparer**, la période et les types de
publications, puis **Analyser les tendances**. **Exporter CSV** récupère les chiffres.
Cette vue demande une clé OpenAlex (voir ci-dessous).

## Origines

La vue **Origines** cherche d'où vient une idée. Saisissez un concept (de préférence en
anglais, la langue des bases bibliographiques), puis **Remonter aux origines** :

- **Racines du sujet** : parmi environ 200 publications OpenAlex sur ce concept,
  les références anciennes que beaucoup d'entre elles citent, placées sur une frise
  (année, part des publications qui les citent). La ligne **Racine la plus ancienne**
  donne le plus ancien travail cité par au moins 10 % d'entre elles. Seul le concept est
  envoyé ; il faut l'accès web et une clé OpenAlex.
- **Dans votre bibliothèque** : la même chaîne de citations que `/origine` dans le
  [Chat](Chat.md), construite sur votre ordinateur à partir de vos articles.

Une racine très citée est un travail fondateur probable, pas une preuve d'antériorité.
Pour une phrase précise de votre texte, le bouton **Source d'origine ?** de la
[Rédaction](Redaction.md) est plus fiable.

## Clés d'API (facultatif)

OpenAlex et Semantic Scholar fonctionnent mieux avec une clé gratuite (indispensable pour
les **Tendances** et les racines des **Origines**), à saisir dans
**Paramètres > Réglages experts > Clés et connexions**. Voir [Paramètres](Parametres.md).
