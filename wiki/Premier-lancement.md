# Premier lancement

Français · [English](First-launch-en.md)

Au premier démarrage, un assistant en **deux étapes** prépare Bibliobrain. Comptez de
quelques minutes à quelques heures selon la taille de votre bibliothèque : la préparation
se fait une seule fois, ensuite seuls les nouveaux documents sont lus.

<!-- TODO capture : fenêtre de l'assistant, étape 1 -->

## Étape 1 : où sont vos PDF ?

Choisissez le dossier qui contient vos articles ; les sous-dossiers sont inclus.

- **Si Zotero est installé**, il est détecté et son dossier de PDF est proposé.
- Le lien **Autre source de métadonnées** permet de choisir Zotero, un fichier BibTeX ou
  aucune source (Bibliobrain utilisera alors les informations contenues dans les fichiers).

Formats lus : PDF, DOCX, ODT, EPUB, PPTX et fichiers texte (`.txt`, `.md`, `.rst`, `.tex`,
`.csv`, `.tsv`).

**Bibliobrain est déjà prêt sur un autre ordinateur ?** Exportez-y l'index (Paramètres >
Mes documents > **Changer d'ordinateur**), puis cliquez ici sur **Importer son index** en
bas de l'étape 1. Si le fichier contient vos PDF, inutile de choisir un dossier : ils sont
copiés dans **Documents\Bibliobrain PDF**. L'import remplace l'étape 2.

## Étape 2 : préparation des articles

Bibliobrain vérifie qu'Ollama et le module de recherche dans vos documents sont prêts, puis
lit vos documents. Si tout est installé, vous n'avez rien à régler. En cas de problème,
l'assistant affiche la commande à exécuter.

- **Explorer en attendant** ferme la fenêtre sans interrompre le travail.
- **Plus tard** ferme l'assistant ; il se rouvrira au prochain lancement tant que la
  préparation n'a pas abouti.

## Savoir si c'est prêt

Un indicateur en bas de la barre latérale montre l'état de votre bibliothèque :

| État | Signification |
|---|---|
| Non initialisé | Vos articles n'ont pas encore été préparés |
| Mise à jour de la bibliothèque | La préparation est en cours (avec un pourcentage) |
| Prêt · N articles | Chat, analyses et rédaction sont utilisables |

Tant que la bibliothèque n'est pas prête, les réponses seraient incomplètes : attendez
**Prêt**. Cliquez sur l'indicateur pour voir l'activité en détail.

## Ensuite : poser votre première question

Ouvrez le **Chat** et posez une question sur vos articles (voir [Chat](Chat.md)). Pour être guidé,
cliquez sur **Faire le tutoriel** dans **Paramètres > Vue d'ensemble** : il vous fait poser
une vraie question et vérifier une source, pas à pas. La liste **Premiers pas**, dans la
barre latérale, propose la suite (vérifier une source, insérer une citation, créer un
carnet).

Pour ajouter des articles plus tard, voir [Bibliothèque](Bibliotheque.md) (« Ajouter des
articles »).

## Et mes PDF scannés ?

Un PDF « scanné » est une photo de pages : la recherche ne peut pas le lire. Ce n'est pas
inclus dans le démarrage. Quand vous voudrez les rendre consultables, allez dans
**Paramètres > Mes documents > Lire les pages scannées** : l'outil compte d'abord les pages
concernées sans rien télécharger, puis vous guide (téléchargement unique de 1,1 Go, lecture
sur votre ordinateur).

## Relancer l'assistant

À tout moment : **Paramètres > Réglages experts > Diagnostic > Relancer l'assistant**. Dans
**Paramètres > Vue d'ensemble**, une ligne à compléter affiche aussi un bouton
**Configurer**, **Préparer** ou **Corriger**. La visite des onglets se relance depuis
**Diagnostic > Revoir la visite des onglets**.
