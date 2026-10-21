# Paramètres

Français · [English](Settings-en.md)

Le bouton **Paramètres**, en bas de la barre latérale, ouvre les réglages. Ils s'ouvrent
sur une **Vue d'ensemble** : une ligne par domaine, avec son état. Cliquez sur une ligne
pour la modifier ; une ligne à compléter affiche un bouton **Configurer**, **Préparer** ou
**Corriger**. La recherche en haut du panneau parcourt tous les réglages.

## Les réglages courants

### Mes documents
- Changer le **dossier de vos PDF** (**Changer…**).
- Connecter **Zotero** : **Utiliser ma bibliothèque Zotero**, puis **Détecter Zotero**
  (détection automatique), ou choisir un fichier BibTeX.
- **Lire les pages scannées** : rend consultables les PDF qui sont des photos de pages
  (voir [Premier lancement](Premier-lancement.md)).
- **Changer d'ordinateur** : **Exporter…** enregistre dans un fichier .zip l'index de
  recherche, vos PDF et leurs notices (titres, auteurs, tags, notes), sans vos conversations
  ni vos clés API. Sur l'autre ordinateur, **Importer…** vérifie le fichier et dit ce qu'il
  va faire ; un seul bouton télécharge si besoin le modèle qui manque, copie les PDF absents
  puis installe l'index. Une barre montre l'avancement.
  - Les PDF vont dans votre dossier de PDF. Sans dossier configuré, Bibliobrain crée
    **Documents\Bibliobrain PDF** et l'utilise. Un fichier déjà présent n'est jamais remplacé.
  - Si vos PDF sont dans le dossier de Zotero, ils n'y sont copiés que s'il s'agit de la
    même bibliothèque Zotero synchronisée.
  - Si ce PC n'a ni Zotero ni fichier BibTeX, les notices de l'archive deviennent la source
    de métadonnées, sans rien réindexer. Vous pourrez connecter Zotero plus tard.
  - **Voir le détail** montre la vérification complète. **Options** permet d'exporter sans
    les PDF (fichier bien plus petit), de ne pas télécharger le modèle ou de garder vos
    réglages.
  - Un fichier de plus de 4 Go ne tient pas sur une clé USB formatée en FAT32.

### Assistant IA
**Où l'IA doit-elle fonctionner ?**

- **Sur cet ordinateur** : rien ne quitte votre PC.
- **Un autre ordinateur proche** : une machine de votre réseau, dont vous saisissez l'adresse.
- **Service en ligne** : une API compatible OpenAI (adresse et clé à saisir, puis
  **Enregistrer et connecter**).

Pour les deux premiers choix, indiquez le **Logiciel qui fait tourner les modèles** :
Ollama ou LM Studio. Sur un autre ordinateur, Ollama doit accepter les connexions
du réseau (`OLLAMA_HOST=0.0.0.0`) et LM Studio avoir l'option « Serve on Local Network ».

Ce choix ne concerne que les réponses. L'index de vos documents, la lecture des pages
scannées et l'analyse des figures restent sur Ollama, sur cet ordinateur : ils ne changent
pas quand vous changez de service. Pour les déplacer, voir **Réglages experts > Indexation
et figures** : « Où l'index est calculé » et « Où tournent ces outils » (scans et figures,
qui peuvent être confiés à Ollama sur un autre ordinateur ; les images des pages lui sont
alors envoyées et les modèles doivent y être installés).

Puis choisissez **Quel modèle d'IA répond ?**, et **Lancer un test rapide** pour vérifier
la connexion.

### Trouver des articles en ligne
- **Autoriser la recherche sur le web** : nécessaire pour [Découverte](Decouverte.md), pour
  compléter les métadonnées manquantes et pour les graphes de citations.
- Choix des sources et e-mail Unpaywall (facultatif).

### Apparence et langue
Français ou English ; thème **Sombre**, **Clair** ou **Comme mon ordinateur** ;
**Réinitialiser les panneaux** remet les panneaux à leur largeur d'origine.

Le bouton **Faire le tutoriel**, en bas de la Vue d'ensemble, guide votre première question.

## Réglages experts

Le bouton **Réglages experts** de l'en-tête ouvre les réglages techniques
(**← Réglages courants** pour revenir). Un bandeau rappelle qu'ils peuvent dégrader les
réponses ; chaque valeur a un bouton **Réinitialiser**.

| Section | Pour quoi faire |
|---|---|
| Génération des réponses | Fenêtre de contexte, température, mode *thinking* |
| Qualité de la recherche | Nombre de passages, seuil de similarité, modèle de reclassement (reranker) |
| Indexation et figures | Modèle d'indexation (embedding), où l'index est calculé, analyse des figures, OCR, où tournent ces deux outils, transfert d'index |
| Nettoyage des documents | Texte mal extrait, métadonnées douteuses |
| Instructions d'analyse | Personnaliser le prompt de chaque type d'analyse |
| Clés et connexions | Clés d'API (OpenAlex, Semantic Scholar, Ollama, Hugging Face…) |
| Diagnostic | **Version et mises à jour**, **Relancer l'assistant**, **Revoir la visite des onglets**, mode debug, maintenance, **Quitter Bibliobrain** |

## Clés d'API

Toutes se saisissent dans l'interface, jamais dans un fichier. Sous Windows, elles sont
chiffrées pour votre compte et ne sont jamais renvoyées au navigateur. Un champ laissé vide
conserve la valeur actuelle ; **Effacer** la supprime.

| Clé | Utilité |
|---|---|
| OpenAlex | Recherche, cartographies plus fiables ; indispensable pour les Tendances (gratuite) |
| Semantic Scholar | Quota dédié pour la découverte |
| LLM en ligne | Seulement si vous choisissez **Service en ligne** (à saisir dans **Assistant IA**) |
| Ollama | Seulement pour un serveur Ollama protégé |
| Hugging Face | Quotas de téléchargement des modèles de reclassement |
