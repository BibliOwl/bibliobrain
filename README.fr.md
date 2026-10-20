<a id="readme-top"></a>

<div align="center">
  <img src="static/logo_bibliobrain_1024.png" alt="Logo Bibliobrain" width="140" height="140">

  <h3>Bibliobrain</h3>

  <p>
    <strong>Interagissez avec votre bibliographie grâce à l'IA.</strong>
    <br />
    Un assistant de recherche local et privé : interrogez vos articles, explorez et enrichissez vos références, et obtenez des réponses avec des citations que vous pouvez vérifier.
  </p>

  <p>
    <strong><a href="https://www.bibliobrain.org/">Site officiel</a></strong>
    &nbsp;·&nbsp; <a href="https://github.com/bibliOwl/bibliobrain/releases/latest">Télécharger</a>
    &nbsp;·&nbsp; <a href="wiki/Home.md">Guide d'utilisation</a>
  </p>

  <p>
    <a href="README.md">English</a> · Français
  </p>
</div>

<hr />

> **Alpha — en développement actif.** Les fonctionnalités et l'interface évoluent vite, et des bugs sont probables. Conservez vos documents d'origine et vérifiez les réponses de l'IA à l'aide des sources citées.

## À quoi ça sert

Bibliobrain vous permet d'**interagir avec votre bibliographie grâce à l'IA**. Il lit vos PDF et autres documents (ainsi que vos métadonnées Zotero si vous utilisez Zotero), les indexe sur votre ordinateur, et vous permet de les interroger, de les cartographier, de les compléter par de nouveaux articles et de les citer dans vos textes, en langage naturel et avec chaque réponse rattachée à ses sources.

- **Interroger sa bibliothèque.** Discutez avec vos documents et ouvrez les passages exacts cités dans chaque réponse. Un panneau de sources et un graphe de provenance montrent sur quoi la réponse s'appuie.
- **Avec ou sans Zotero.** Pointez Bibliobrain vers une bibliothèque Zotero ou un simple dossier. Formats pris en charge : PDF, DOCX, ODT, EPUB, PPTX et fichiers texte (`.txt`, `.md`, `.rst`, `.tex`, `.csv`, `.tsv`). Chaque format garde son unité de citation naturelle (page, section, chapitre ou diapositive).
- **Organiser.** Regroupez vos sources en carnets thématiques ; interrogez un seul article directement depuis la bibliothèque.
- **Explorer.** Carte de la bibliothèque et graphes de citations (OpenAlex), plus un module Discovery pour trouver et récupérer des articles via Semantic Scholar, OpenAlex, arXiv, HAL et Crossref.
- **Rédiger.** Un onglet de rédaction suggère des références pour un passage sélectionné et relit votre brouillon section par section.
- **Lire scans et figures (facultatif).** OCR local pour les pages scannées et modèle de vision pour décrire figures et tableaux.

L'interface existe en français et en anglais. Les commandes `/papers`, `/liste` et `/review` produisent dans le chat des listes d'articles et des revues de littérature structurées.

## Confidentialité

Avec la configuration locale par défaut (Ollama + ChromaDB), vos documents et leur index de recherche ne quittent jamais votre ordinateur.

Certaines fonctions facultatives communiquent avec Internet **uniquement si vous les activez** :

| Fonction | Ce qui est transmis |
| --- | --- |
| Fournisseur d'IA distant (compatible OpenAI) | Vos questions et les extraits de documents nécessaires à la réponse, au fournisseur que vous avez configuré |
| Recherche d'articles (Discovery, OpenAlex, Semantic Scholar…) | Vos requêtes, une fois la recherche sur le web activée dans les paramètres |
| Rapport de bug | Votre description, votre e-mail de contact si vous en donnez un, la version de l'application, plus uniquement ce que vous cochez sur l'écran du rapport : diagnostics, vingt derniers messages du chat, capture d'écran. Rien n'est envoyé sans clic |
| Vérification des mises à jour | Si vous l'acceptez (question au premier lancement, réglable dans Paramètres › Réglages experts › Diagnostic) : une requête par jour vers GitHub pour le fichier `latest.json` de la dernière version. Bibliobrain n'envoie ni identifiant ni version ; GitHub voit votre adresse IP. Rien n'est installé sans votre clic |

Les rapports de bug arrivent sur un serveur administré par le mainteneur ; [PRIVACY.md](PRIVACY.md) indique qui en est responsable, combien de temps ils sont conservés et comment les faire effacer.

Connecter Zotero n'écrit jamais dans sa base ni dans son dossier de stockage ; un document déposé dans Bibliobrain est confié à Zotero lui-même par son connecteur local. Les clés API sont chiffrées avec DPAPI (Windows) et ne sont jamais renvoyées au navigateur. Voir [SECURITY.md](SECURITY.md) et le [guide des paramètres](wiki/Parametres.md).

## Installation

Bibliobrain fonctionne sous **Windows 10 ou 11 (64 bits)**. Une connexion Internet est nécessaire pendant l'installation. Python 3.14 et [Ollama](https://ollama.com) sont installés automatiquement s'ils manquent. Prévoyez plusieurs Go d'espace disque pour les dépendances Python (PyTorch), plus la taille des modèles choisis ci-dessous.

### Option A — installateur (recommandé)

1. Téléchargez `Bibliobrain-Setup-X.Y.Z.exe` (X.Y.Z est le numéro de version) depuis la [dernière version](https://github.com/bibliOwl/bibliobrain/releases/latest).
2. Lancez-le. Les versions alpha **ne sont pas signées numériquement** : Windows SmartScreen peut afficher un avertissement.
3. Suivez les étapes : l'installation prépare les composants locaux et demande quels modèles d'IA télécharger.

Par défaut, l'installateur place les fichiers de l'application et les scripts de maintenance (`Update-Bibliobrain.cmd`, `Backup-Bibliobrain.cmd`, `Restore-Bibliobrain.cmd` et `Uninstall-Bibliobrain.cmd`) dans `%LOCALAPPDATA%\Programs\Bibliobrain` (généralement `C:\Users\<nom_utilisateur>\AppData\Local\Programs\Bibliobrain`). Pour ouvrir ce dossier, appuyez sur **Win + R**, collez `%LOCALAPPDATA%\Programs\Bibliobrain` et validez avec **Entrée**. Les données utilisateur sont stockées séparément dans `%LOCALAPPDATA%\bibliobrain`.

### Option B — paquet ZIP (si votre antivirus bloque l'installateur)

Certains antivirus (Symantec par exemple) mettent `Bibliobrain-Setup-X.Y.Z.exe` en quarantaine parce qu'il est récent et non signé. Le paquet ZIP contient les mêmes fichiers, sans aucun exécutable.

1. Téléchargez `Bibliobrain-Windows.zip` depuis la [dernière version](https://github.com/bibliOwl/bibliobrain/releases/latest).
2. Clic droit sur le ZIP → **Propriétés** → cochez **Débloquer** → **OK**. Windows cesse alors d'avertir avant chaque lanceur.
3. Extrayez-le dans un dossier que vous garderez, par exemple `Documents` : vous obtenez un dossier `Bibliobrain`.
4. Double-cliquez sur `Install-Bibliobrain.cmd` dans ce dossier et répondez aux questions dans la fenêtre.

Cette installation se met à jour avec le `Bibliobrain-Windows.zip` suivant, jamais avec l'installateur.

### Option C — clone Git

Installez [Git](https://git-scm.com), puis dans PowerShell :

```powershell
git clone https://github.com/bibliOwl/bibliobrain.git
cd bibliobrain
.\Install-Bibliobrain.cmd
```

Utilisez `git clone` plutôt que le « Download ZIP » de GitHub (ce n'est pas le paquet ZIP ci-dessus) : les mises à jour ont besoin du dossier `.git`.

### Premier lancement

Ouvrez le raccourci **Bibliobrain** (Bureau ou dossier du projet). Le serveur démarre en arrière-plan et votre navigateur s'ouvre sur <http://localhost:7860>. Un court assistant en deux étapes demande le dossier de vos documents et prépare l'index ; Ollama et le modèle d'indexation sont vérifiés automatiquement. Fermer le dernier onglet Bibliobrain arrête le serveur.

### Mise à jour

Lancez **`Update-Bibliobrain.cmd`**. Avec un clone Git, il récupère la nouvelle version ; avec le paquet ZIP, il vous demande de sélectionner le nouveau `Bibliobrain-Windows.zip`, en contrôle la version et crée d'abord une sauvegarde ; avec l'installateur, il vous demande de sélectionner le nouveau `Bibliobrain-Setup-X.Y.Z.exe`, en contrôle l'identité, la version et la signature, et crée d'abord une sauvegarde. `Backup-Bibliobrain.cmd`, `Restore-Bibliobrain.cmd` et `Uninstall-Bibliobrain.cmd` se trouvent à côté. Vos données vivent dans `%LOCALAPPDATA%\bibliobrain`, séparées du code, et sont conservées à la désinstallation.

Pour les sauvegardes, les journaux et la désinstallation, consultez le [guide de maintenance](wiki/Maintenance.md). Les clés API se configurent dans les [paramètres](wiki/Parametres.md).

## Modèles recommandés

L'installateur ne télécharge aucun modèle sans le demander. Au début, il affiche le tableau ci-dessous en indiquant comment chaque modèle tournerait sur votre machine (entièrement sur GPU, GPU + CPU, CPU seul ou trop gros), puis demande lequel télécharger. Répondre Entrée n'en télécharge aucun. Elle demande ensuite séparément s'il faut télécharger `embeddinggemma`, nécessaire pour indexer les documents.

| # | Modèle | Téléchargement | Statut | Entièrement sur GPU | CPU seul |
| --- | --- | --- | --- | --- | --- |
| 1 | `qwen3.5:9b` | 6,1 Gio | validé | 8 Gio VRAM, 16 Gio RAM, 6 threads | 24 Gio RAM, 8 threads |
| 2 | `granite4.2:8b` | 5,0 Gio | avec réserves | 6 Gio VRAM, 12 Gio RAM, 4 threads | 16 Gio RAM, 8 threads |
| 3 | `qwen3.5:4b` | 3,2 Gio | avec réserves | 5 Gio VRAM, 8 Gio RAM, 4 threads | 12 Gio RAM, 6 threads |
| 4 | `gemma4:e2b` | 6,7 Gio | qualité réduite | 8 Gio VRAM, 12 Gio RAM, 4 threads | 16 Gio RAM, 6 threads |
| 5 | `qwen3.5:2b` | 2,5 Gio | qualité réduite | 3 Gio VRAM, 6 Gio RAM, 4 threads | 8 Gio RAM, 4 threads |
| 6 | `qwen3.5:0.8b` | 1,2 Gio | qualité réduite | 2 Gio VRAM, 4 Gio RAM, 2 threads | 4 Gio RAM, 2 threads |

- **validé** : aucun échec critique au benchmark documentaire interne `library48` et relecture qualitative sans défaut bloquant.
- **avec réserves** : aucun échec critique, mais la relecture a relevé des faiblesses (`granite4.2:8b` : un pourcentage mal calculé ; `qwen3.5:4b` : sorties structurées faibles).
- **qualité réduite** : petits modèles pour ordinateurs modestes. Ils ont échoué à `library48` (`gemma4:e2b` : cinq échecs critiques) ou n'y ont pas été soumis ; réponses et citations sont moins fiables, et l'installateur ne les recommande jamais, sauf `qwen3.5:2b` quand rien de mieux ne tient sur l'ordinateur (l'application prévient alors que les réponses peuvent être mauvaises).

L'installateur liste aussi les modèles de conversation déjà présents dans Ollama : en choisir un ne télécharge rien. Un modèle qui ne tient pas en VRAM est réparti entre GPU et CPU par Ollama : il fonctionne, mais répond beaucoup plus lentement. Les seuils de tous les modèles sauf `qwen3.5:9b` sont estimés d'après leur taille, pas mesurés sur chaque configuration. En installation silencieuse, `-ChatModel <nom>` télécharge directement ce modèle, `-ChatModel none` aucun, et `-SkipModelDownloads` saute toutes les questions.

Vous pouvez aussi utiliser n'importe quel modèle servi par Ollama, ou tout service compatible OpenAI (Paramètres › Assistant IA).

`embeddinggemma` est le **modèle d'embedding** recommandé (celui qui indexe vos documents ; il a obtenu les meilleurs résultats à nos tests, notamment sur le français), mais ce n'est pas le seul choix : d'autres modèles d'embedding Ollama peuvent être sélectionnés dans les paramètres (« Modèle d'indexation »). Bibliobrain conserve un index distinct pour chaque modèle d'embedding : vous pouvez en changer et revenir en arrière sans tout reconstruire. Les embeddings distants d'une API compatible OpenAI sont aussi possibles, mais désactivés par défaut, car des extraits de vos documents seraient alors envoyés à cette API.

Un reranker local reclasse les meilleurs passages après la recherche vectorielle. Il est choisi automatiquement (Qwen3-Reranker 0.6B sur un GPU CUDA avec au moins 3 Gio de VRAM libre, sinon un petit cross-encoder sur CPU) et se change dans les réglages experts. Seuls sont proposés des modèles aux poids Safetensors figés, chargés avec `trust_remote_code=False` et sous licence Apache-2.0 ou MIT.

Les poids des modèles ne font pas partie de Bibliobrain et ne sont pas couverts par sa licence. Chaque modèle est téléchargé sous ses propres conditions : `embeddinggemma`, par exemple, est distribué sous les Gemma Terms of Use de Google. Vérifiez la licence sur la page Ollama du modèle, ou avec `ollama show <modèle> --license`, avant de vous appuyer sur un modèle, en particulier pour un usage commercial.

## Limites et retours

Bibliobrain est une aide à la recherche. Les réponses peuvent être erronées même avec des citations, et la qualité dépend de votre matériel, du modèle choisi et de la qualité de vos documents (surtout les scans). Windows est la seule plateforme prise en charge pour l'instant.

Pour signaler un problème, utilisez le rapport de bug intégré : décrivez ce que vous faisiez, ce que vous attendiez et ce qui s'est passé, puis relisez les pièces jointes avant l'envoi (évitez les documents confidentiels). Vous pouvez aussi ouvrir un [ticket](https://github.com/bibliOwl/bibliobrain/issues). Failles de sécurité : voir [SECURITY.md](SECURITY.md).

## Documentation

- [Guide d'utilisation](wiki/Home.md)
- [Installation et mises à jour](wiki/Installation.md)
- [Sauvegardes, maintenance et désinstallation](wiki/Maintenance.md)
- [Journal des modifications](CHANGELOG.md)

## Politique de signature du code

Signature de code gratuite fournie par [SignPath.io](https://signpath.io), certificat de la [SignPath Foundation](https://signpath.org). La demande auprès de la SignPath Foundation est en cours d'examen : tant qu'elle n'est pas acceptée, les versions publiées ne sont pas signées. Seul l'installateur Windows (`Bibliobrain-Setup-X.Y.Z.exe`) est signé, et uniquement lorsqu'il est construit à partir de ce dépôt par le [workflow de publication](.github/workflows/release.yml) sur GitHub Actions.

Rôles de l'équipe : le projet n'a qu'un seul mainteneur, [bibliOwl](https://github.com/bibliOwl), à la fois auteur des modifications, relecteur et approbateur. Les contributions extérieures sont relues et fusionnées par le mainteneur (voir [CONTRIBUTING.md](CONTRIBUTING.md)).

Confidentialité : voir la [politique de confidentialité](PRIVACY.md). Bibliobrain n'envoie de données hors de votre ordinateur que pour les fonctions qui y sont décrites, et la vérification des mises à jour comme les rapports de bug demandent votre accord.

## Technologies

FastAPI et Uvicorn côté serveur ; LangChain et ChromaDB pour la recherche ; Ollama ou tout service compatible OpenAI pour les modèles (client `openai` officiel avec `instructor` pour les sorties structurées) ; frontend en HTML/CSS/JS.

## Licence et contributions

Copyright (C) 2026 bibliOwl. Bibliobrain est distribué sous la licence **GNU AGPL v3 uniquement** (`AGPL-3.0-only`) : voir [LICENSE](LICENSE). Il est fourni sans aucune garantie. Le nom « Bibliobrain » et le logo ne sont pas concédés sous l'AGPL : voir [TRADEMARKS.md](TRADEMARKS.md). Les composants tiers conservent leurs licences : voir [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Les contributions sont bienvenues : lisez [CONTRIBUTING.md](CONTRIBUTING.md). Les contributions non triviales nécessitent l'acceptation du [CLA](CLA.md), qui reste non exclusif tout en préservant la possibilité d'une double licence open source et commerciale.

<p align="right"><a href="#readme-top">Haut de page</a></p>
