# Bibliothèque

Français · [English](Library-en.md)

L'onglet **Bibliothèque** liste les documents de votre bibliothèque. Avec Zotero (ou un
fichier BibTeX), il affiche aussi les métadonnées : auteurs, année, revue, DOI, tags,
collections.

<!-- TODO capture : liste et panneau de détail d'un article -->

## Ajouter des articles

Trois façons d'ajouter des PDF (ou d'autres documents) après la première préparation :

- **Glisser-déposer** des fichiers sur la fenêtre de Bibliobrain.
- Bouton **⋯ (Plus d'actions) > Ajouter des fichiers…** en haut de la Bibliothèque.
- Les **copier dans votre dossier de PDF**, ou les **ajouter à Zotero** si votre
  bibliothèque est celle de Zotero.

Les fichiers déposés sont copiés dans votre dossier de PDF. Avec une bibliothèque Zotero,
seuls les PDF sont acceptés : ils sont confiés à Zotero, qui retrouve leur fiche.

**Ensuite, il faut les préparer.** Quand vous revenez sur la fenêtre de Bibliobrain,
l'indicateur en bas de la barre latérale signale **N nouveaux PDF** : cliquez sur
**Ajouter** pour les lire. Pour des PDF modifiés ou retirés, le bouton s'appelle
**Mettre à jour**. Seuls les documents concernés sont lus, pas toute la bibliothèque.

## Parcourir

- **Recherche** : filtre instantané par titre, auteur, tag ou collection.
- **Filtre** : n'afficher qu'un type de document, une collection Zotero, une période, les
  articles avec ou sans analyse…
- **Tri** : cliquez sur les en-têtes de colonne ; **Colonnes** choisit celles qui
  s'affichent.
- Cochez plusieurs articles pour les **Questionner dans le chat** ou les **Ajouter au
  carnet**.
- **⋯ (Plus d'actions) > Exporter les articles affichés** : Markdown ou BibTeX.

## Ouvrir un article

Un clic ouvre un panneau avec trois vues :

| Vue | Contenu |
|---|---|
| **Aperçu** | Métadonnées complètes et carte **Graphe de citations** : qui cite cet article, et ce qu'il cite (voir [Cartographies](Cartographies.md)) |
| **Résumé** | Choisissez un **Type d'analyse** (résumé rapide ou complet, analyse critique, méthodes, conclusions) puis **Générer l'analyse** |
| **Citer** | **Copier APA** ou **Copier BibTeX** pour coller la référence ailleurs |

**Lire ici** affiche le PDF dans Bibliobrain. Le menu **⋯** de l'article propose
**Ouvrir le PDF**, **Afficher dans Zotero**, **Ajouter au
carnet** et **Graphe de citations**.

Les analyses sont **conservées** : elles ne sont pas recalculées à chaque ouverture.
**Modifier le prompt** (dans la vue Résumé) change les instructions d'un type d'analyse ;
on les retrouve dans **Réglages experts > Instructions d'analyse** (voir
[Paramètres](Parametres.md)).

## Poser une question sur un seul article

Le bouton **Interroger cet article** limite le chat à ce PDF, sans créer de carnet.

## Nettoyer ses documents

Quand le texte extrait d'un PDF est abîmé (accents détachés, ligatures) ou que des
métadonnées semblent douteuses (titres étranges, doublons probables), l'outil
**Nettoyage des documents** des réglages experts vous aide à les repérer.

## Sans Zotero

Bibliobrain fonctionne très bien sans Zotero. Les métadonnées manquantes peuvent être
complétées automatiquement (Crossref, puis OpenAlex) si vous autorisez la recherche en
ligne ; seuls titres et DOI sont alors envoyés, jamais le contenu des PDF.
