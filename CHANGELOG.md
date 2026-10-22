# Changelog

Toutes les évolutions notables de Bibliobrain sont consignées ici. Le format suit
[Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et les versions suivent
[SemVer](https://semver.org/lang/fr/).

La version courante est définie une seule fois dans le fichier `VERSION` à la racine du
dépôt. Elle est lue par `src/version.py`, par `packaging/build-installer.ps1` et par
`packaging/Bibliobrain.iss`. Une publication consiste à :

1. lancer `python scripts/release_tools.py prepare X.Y.Z`, qui met à jour `VERSION` et
   transforme la section « Non publié » ci-dessous en section datée ;
2. commiter, puis créer et pousser un tag annoté `vX.Y.Z` ;
3. sur GitHub, `.github/workflows/release.yml` construit l'installateur, le paquet ZIP et
   `latest.json` et publie la version, que la vérification des mises à jour proposera
   ensuite. Ailleurs, `packaging/build-installer.ps1 -ReleaseBaseUrl <adresse des fichiers
   de la version>` produit les trois fichiers à déposer à la main.

## [Non publié]

## [0.8.2] - 2026-10-22

### Corrigé

- Mise à jour depuis l'application : elle refusait tout installateur avec « The selected
  executable is not identified as a Bibliobrain installer ». Inno Setup complète le nom de
  produit de l'exe avec des espaces, que la vérification ne retirait pas. Une installation
  en 0.8.1 ou antérieure doit encore lancer l'installateur 0.8.2 à la main une fois.

### Sécurité

- Téléchargement de PDF depuis la découverte d'articles : la connexion se fait vers l'adresse
  publique déjà vérifiée, ce qui empêche un DNS rebinding de viser une adresse locale.
- Export du chat, rapports de bug et graphe de citations : des expressions régulières au coût
  quadratique sur certains textes ont été remplacées par des traitements linéaires.

## [0.8.1] - 2026-10-22

### Ajouté

- Changer d'ordinateur : l'export et l'import de l'index sont dans Réglages > Documents
  (« Changer d'ordinateur »). Le fichier d'export contient aussi les PDF indexés (case à
  décocher pour les laisser de côté, avec leur taille affichée et un avertissement FAT32 au-delà
  de 4 Go) et la fiche de chaque document (titre, auteurs, étiquettes, collections, notes,
  annotations). À l'import, un plan court n'affiche que ce qui demande une décision ; un
  seul bouton télécharge le modèle d'embedding manquant et installe l'index. Les PDF
  absents sont copiés sans jamais écraser un fichier existant (dans « Documents/Bibliobrain
  PDF » si aucun dossier n'est choisi). Si l'ordinateur n'a pas de Zotero ni de BibTeX, les
  fiches importées servent de source et rien n'est ré-indexé. Une barre de progression suit
  le téléchargement du modèle, l'export, la copie et l'installation. L'assistant du premier
  lancement propose « importez son index » à la fin de l'étape 1.

### Corrigé

- Aperçu du rechargement impossible (erreur 500) : un seul .pptx illisible ou contenant un
  gros objet intégré (tableur collé, vidéo) faisait échouer l'aperçu, les métadonnées et la
  détection des doublons. Seules les parties lues par les analyseurs comptent maintenant dans
  la limite de taille ; objets intégrés, polices, images et miniatures sont ignorés. Un
  document illisible ou trop gros ne bloque plus l'analyse de la bibliothèque.
- Zotero avec ZotMoov et sous-dossiers : charger Zotero ne remplace plus le dossier de PDF
  choisi ; la détection lit `prefs.js` (dossier de données personnalisé, dossier des fichiers
  liés proposé quand la plupart des PDF le sont) ; les documents sont rattachés à leur fiche
  par chemin d'abord, par nom de fichier seulement s'il est unique ; deux « article.pdf » de
  dossiers différents ne fusionnent plus (ils sont relus une fois). Réglages : « Revenir
  au Zotero par défaut » et un décompte des documents trouvés, sous-dossiers compris.
- Chat : « Trouve les articles écrits par <prénom> » retrouve aussi les entrées à initiales
  seules (« Moreau, C. ») quand la bibliothèque montre le même nom avec le prénom complet.
  « Seulement 3 ? » ou « only three? » prolongent la liste précédente au lieu de relancer une
  recherche, et une liste tirée des métadonnées ne prétend plus venir de l'index.
- Chat : « Quels sont les thèmes principaux de ma bibliothèque ? » et les questions de vue
  d'ensemble sans sujet précis s'appuient sur les Cartographies : les 8 plus grands groupes
  de la bibliothèque, chacun avec un passage de texte d'un article représentatif. Avant, la
  recherche ramenait des tables des matières et des index, et les petits modèles répondaient
  que les thèmes étaient introuvables ou listaient des titres au hasard. Les modèles
  `qwen3.5` reçoivent maintenant la consigne de regrouper en 2 à 4 thèmes ; « test » ou
  « merci » ne relancent plus la vue d'ensemble précédente.
- Changer d'ordinateur, disque plein : l'import additionne tout ce qui va sur le même disque
  (PDF et index) avant d'écrire quoi que ce soit, au lieu de répondre « Erreur interne du
  transfert » en cours de route. Si le disque se remplit quand même, le message dit que
  l'index n'a pas été remplacé. L'export refuse une destination trop petite.
- Mise à jour avec le paquet ZIP : une fenêtre annonce la fin de `Update-Bibliobrain.cmd` et
  propose d'ouvrir Bibliobrain ; pendant la mise à jour, le raccourci ne la lance plus.
- Découverte : ouvrir la liste des modèles faisait disparaître le champ de recherche et la
  liste était coupée par le cadre. Elle s'affiche maintenant en entier.

### Sécurité

- Pillow passe en 13.0.0 (huit failles corrigées) et certifi en 2026.10.19. Pillow lit les
  images extraites des PDF.
- pypdf passe en 6.21.2 (cinq failles corrigées). pypdf lit le texte des PDF de la
  bibliothèque.

## [0.8.0] - 2026-10-20

Première version publique.

### Ajouté

- Chat : aide sur le logiciel à partir du wiki fourni avec l'application. `/aide` ou `/help`
  la demande explicitement ; sans commande, seules les questions courtes portant sur
  Bibliobrain lui-même y vont (« Comment créer un carnet ? », « Où sont stockés mes PDF ? »,
  « Zotero n'est pas détecté »). Une question qui nomme un sujet reste une question sur les
  articles. Les réponses d'aide n'ont pas de sources et renvoient aux pages du wiki.
- Installateur compréhensible par tous : une première page propose l'« installation simple »
  (par défaut), qui télécharge l'IA conseillée pour l'ordinateur et le module de recherche en
  le disant clairement, ou l'« installation personnalisée ». Les textes n'emploient plus de
  jargon (Gio, VRAM, runtime, PyTorch, reclassement) et les tailles sont en Go.
- Installateur : trois modèles de conversation plus légers (`gemma4:e2b`, `qwen3.5:2b`,
  `qwen3.5:0.8b`), marqués « qualité réduite » et jamais recommandés. La page des modèles dit ce qui est déjà installé
  (Ollama absent, aucun modèle, ou le nombre de modèles de conversation présents), liste ces
  modèles pour les choisir sans rien télécharger, et les trouve aussi quand Ollama ne tourne
  pas (lecture du dossier de modèles). La liste défile quand il y a beaucoup de modèles.
- Installation : chaque version publie aussi `Bibliobrain-Windows.zip`, les mêmes fichiers
  que l'installateur sans aucun exécutable, pour les PC dont l'antivirus
  met en quarantaine le `Setup.exe` récent et non signé. On l'extrait puis on lance
  `Install-Bibliobrain.cmd`. Une installation faite ainsi porte un fichier `ZIP-PACKAGE.txt`
  et se met à jour avec le ZIP suivant (vérifié par SHA-256 depuis l'application, ou
  sélectionné dans `Update-Bibliobrain.cmd`), jamais avec l'installateur. Au début de
  l'installation, `install.ps1` retire la marque « téléchargé d'Internet » des fichiers
  extraits.
- Chat : une question sur la réponse précédente travaille sur ses sources. Après une liste
  d'articles, « Classe ces articles par approche », « Extrais les sujets et regroupe-les » ou
  « Fais un résumé global » répondent à partir de tous les articles listés (titre, auteurs,
  année, extrait), avec leurs labels [Sx] d'origine, au lieu de relancer une recherche de
  six passages pendant que l'historique ne montrait au modèle que les 8 premières sources.
  Si tout ne tient pas dans la fenêtre du modèle, les extraits partent d'abord, puis la
  réponse dit combien de sources ont été laissées de côté. Un export ou une bibliographie
  de « cette liste » garde son outil.
- Chat plus rapide avec Ollama : le modèle de chat garde une seule fenêtre de contexte pour
  tous ses appels, la plus grande que la mémoire vidéo permet à côté du reranker et du
  modèle d'embedding (mesurée une fois par modèle au premier lancement). Le planificateur
  d'outils, le titre automatique, une question longue ou le bouton Stop ne provoquent plus
  de rechargement du modèle (environ 3 s chacun). Les modèles restent chargés 30 minutes
  (`OLLAMA_KEEP_ALIVE_SECONDS`) et sont préchargés à l'ouverture de l'application et au
  changement de modèle. Le panneau de debug affiche les rechargements, la taille du prompt
  et la vitesse de génération.
- Chat : les réponses des modèles qwen3.5 s'affichent au fil de l'eau, comme celles des
  autres modèles, au lieu d'apparaître d'un bloc une fois terminées (premier mot en environ
  0,2 s au lieu de 1 à 1,5 s avec qwen3.5:4b). Leurs labels de citation sont réparés pendant
  l'affichage, et le bouton Stop les interrompt sans décharger le modèle.
- Chat : les valeurs chiffrées d'une réponse sont contrôlées contre les passages retrouvés,
  sans appel supplémentaire au modèle. Une valeur absente de tous les passages est signalée
  en fin de réponse (« Valeurs à vérifier »), et les labels empilés après la dernière phrase
  sont retirés. Utile surtout avec les petits modèles locaux.
- Installateur sans terminal : `Bibliobrain-Setup.exe` pose toutes ses questions dans des
  pages classiques (Suivant, Suivant…), en français ou en anglais selon Windows. Une page
  « Modèles d'IA » analyse l'ordinateur (RAM, mémoire vidéo, modèles déjà dans Ollama),
  présélectionne le modèle de conversation recommandé et propose EmbeddingGemma. Les
  composants s'installent en arrière-plan : la page de progression affiche l'étape en cours
  et le temps écoulé. À la fin, une case « Lancer Bibliobrain » remplace le démarrage depuis
  la console. Les raccourcis (menu Démarrer, Bureau en option) sont créés et retirés par
  l'installateur. Installation silencieuse : `/CHATMODEL=<modèle|none>` et `/EMBEDDING=1`.
  `install.ps1` reste utilisable à la main et accepte `-EmbeddingDownload ask|yes|no`.
- Vérification des mises à jour, une fois par jour : une requête anonyme vers un fichier
  `latest.json` publié avec chaque version (aucun identifiant, comparaison des versions en
  local). Rien ne part avant l'accord de l'utilisateur : l'assistant de démarrage pose la
  question, et une carte unique la pose aux installations existantes. Réglage et bouton
  « Vérifier maintenant » dans Paramètres › Vue d'ensemble ; adresse remplaçable par
  `BIBLIOBRAIN_UPDATE_MANIFEST_URL`. Nouvelles routes `/api/update/status`,
  `/api/update/check` et `/api/update/start`.
- Mise à jour en un clic : l'installeur est téléchargé puis vérifié par SHA-256 avant
  `bibliobrain-manager.ps1 -Action update -FromApp`, qui sauvegarde les données (deux
  sauvegardes `Bibliobrain-Backup-BeforeUpdate-*` conservées), installe en mode silencieux
  et redémarre le serveur ; la page se recharge seule. Réservé au serveur lancé par son
  raccourci, jamais à un serveur de développement.
- Outils de publication : `scripts/release_tools.py` (`prepare`, `manifest`),
  `build-installer.ps1 -ReleaseBaseUrl` et le workflow GitHub `release.yml` déclenché par un
  tag `v*`.

- `VERSION` devient la source unique de la version applicative ; l'interface affiche la
  version renvoyée par `/api/capabilities` au lieu d'une valeur codée en dur.
- Journalisation applicative dans `%LOCALAPPDATA%\bibliobrain\logs\bibliobrain.log` avec
  rotation, niveau réglable par `BIBLIOBRAIN_LOG_LEVEL`.
- Mesure de couverture des tests avec `pytest-cov` en local et en CI.
- Analyse statique du JavaScript avec ESLint (configuration `eslint.config.js`).
- Ce fichier CHANGELOG.
- Rédaction : option « Inclure les phrases déjà citées » pour leur chercher des sources
  complémentaires. Chaque phrase analysée est validée avec les deux phrases qui la
  précèdent et celle qui la suit, y compris autour d'une sélection ou d'une phrase relancée ;
  une phrase anaphorique (« Ces courants… ») est recherchée avec la phrase précédente.

- Redaction des query strings dans les journaux d'accès Uvicorn.
- Paramètres : « Corriger » rend les PDF scannés consultables en une carte. Elle explique
  les trois étapes et la mémoire disponible, puis enchaîne installation du modèle OCR,
  activation et démarrage ; chaque blocage (Ollama arrêté, téléchargement échoué,
  indexation en cours, bibliothèque non chargée) a sa carte avec la marche à suivre.
  Nouvelle route `/api/ocr/inventory` qui compte les pages scannées sans modèle OCR, et
  codes d'erreur stables (`code`) sur `/api/ocr/start`.
- Index EmbeddingGemma (v1 et v2) : questions et passages sont vectorisés avec les préfixes
  de tâche recommandés par Google. Les index existants restent en texte brut et passent aux
  préfixes à leur prochaine reconstruction complète (champ `embedding_prompts` du manifeste).

### Modifié

- Installateur : le fichier porte sa version dans son nom (`Bibliobrain-Setup-X.Y.Z.exe`),
  pour savoir quelle version un fichier téléchargé installe.
- Installateur : pendant les gros téléchargements (PyTorch, composants, modèles d'IA), la page
  d'installation affiche la quantité téléchargée et le temps restant estimé, avec une vraie
  barre de progression ; la décompression de PyTorch qui suit est annoncée comme une étape à
  part. L'ordinateur ne se met plus en veille pendant l'installation.
- Installateur : une carte NVIDIA trop ancienne pour la version CUDA 13 de PyTorch (avant la
  génération Turing, par exemple une GeForce MX130) reçoit directement la version CUDA 12.6,
  au lieu de télécharger 2 Go pour rien ; avant Maxwell, la version sans carte graphique.
- Réglages : la vérification des mises à jour et le bouton « Vérifier maintenant » passent
  dans Réglages experts › Diagnostic (section « Version et mises à jour »). Le bouton
  « Mise à jour disponible » de la barre latérale y mène directement.
- Listes d'articles complètes. « Liste tous mes articles », « Quels articles ai-je ? » ou
  « Combien d'articles dans ma bibliothèque ? » listent toute la bibliothèque indexée, une
  fois par article même s'il y a plusieurs copies du PDF, du plus récent au plus ancien
  (« … de 2024 » filtre l'année), sans appel au modèle : la liste trouvait 0 article. Une
  liste sur un sujet n'est plus limitée à 12 articles tirés de 32 passages : tous les
  articles dont un passage ressort pour le sujet sont notés par le reranker, puis listés en
  deux groupes, « traitent de » et « peut-être liés ». Un sujet absent ne liste rien. Un
  sujet en français est traduit par le modèle de chat avec le vocabulaire des titres de la
  bibliothèque. Sans reranker, la liste vérifiée par le modèle reste utilisée.
- Recherche du chat plus juste et plus rapide : une question n'est plus
  mêlée à la précédente sauf si c'en est une suite (« Et dans ce cas ? », « Quelles sont ses
  limites ? ») ; après un changement de sujet, 9 papiers sur 10 sont retrouvés au lieu de 6.
  Les deux copies d'un même PDF n'occupent plus deux places parmi les candidats, donc une
  réponse s'appuie de nouveau sur 6 passages distincts. Une question en français est cherchée
  dans les deux langues puis classée une seule fois, et sa traduction se fait pendant la
  recherche. Le score lexical ne recalcule plus la ressemblance des mots pour chaque passage
  d'un même article. Articles trouvés 75 → 79 sur 84 ; temps médian de recherche 854 → 341 ms
  (question en français 1,3 → 0,9 s).
- Génération : la pénalité de répétition recommandée passe de 1,2 à 1,0. Elle pénalisait
  aussi les chiffres et les labels de citation que la réponse doit recopier : sur 54 cas de
  test avec trois modèles locaux, 1,0 n'est jamais moins bon, qwen3.5:4b invente moins de
  valeurs et répond deux fois plus court. Une valeur 1,2 enregistrée
  automatiquement par l'interface est remplacée par la nouvelle ; une valeur choisie dans les
  Paramètres est conservée.
- Installateur : textes de l'assistant réécrits, en français et en anglais. La page « Avant
  l'installation » distingue ce qui est installé d'office, ce qui est proposé et ce qui ne
  sera téléchargé que plus tard sur demande, et indique l'espace disque à prévoir. Les étapes
  de l'installation s'affichent dans la langue de l'assistant au lieu du texte anglais du
  script. La page d'acceptation de la licence disparaît : l'AGPL n'exige aucune acceptation
  pour utiliser le logiciel, et son texte reste livré dans le dossier d'installation.
- Lecture des documents durcie : plafond de taille (768 Mo, `BIBLIOBRAIN_MAX_DOCUMENT_MB`) et
  contrôle de la signature par format avant tout parseur, et extraction du texte dans un
  processus enfant avec délai maximal et plafond mémoire (désactivable avec
  `BIBLIOBRAIN_DOCUMENT_ISOLATION=0`). Une pièce piégée ne peut plus bloquer ni faire tomber
  le serveur ; les déclarations d'entités XML sont refusées dans les archives Office/ODF/EPUB.
- Paramètres réorganisés : une Vue d'ensemble (une ligne d'état par domaine, un bouton
  là où une action est attendue) et quatre domaines aux noms simples — Mes documents,
  Assistant IA, Trouver des articles en ligne, Apparence et langue. Les réglages
  techniques passent dans des Réglages experts ouverts depuis l'en-tête, avec un
  bandeau d'avertissement, la valeur par défaut de chaque réglage et un bouton
  Réinitialiser par réglage. Les anciens noms d'onglets restent acceptés.

- PyTorch passe en 2.14.0, ce qui permet de passer setuptools en 84.0.0 et de retirer
  l'exception d'audit `PYSEC-2026-3447`. PyTorch ne publiant plus de version cu128,
  l'installateur essaie cu130, puis cu126, puis le CPU. Il lit la version du pilote NVIDIA : une
  carte RTX 50 dont le pilote est antérieur à la branche 580 reçoit la version CPU et un
  message l'invitant à mettre à jour le pilote. La vérification du GPU exécute désormais un
  vrai calcul, ce qui détecte une version de PyTorch qui ne gère pas la carte.
- L'état d'exécution du serveur géré (PID, journaux) est déplacé du dossier
  d'installation vers `%LOCALAPPDATA%\bibliobrain\runtime` et `...\logs`. L'ancien dossier
  `.bibliobrain` est migré automatiquement au premier démarrage.
- Les exceptions silencieuses (`except: pass`) sont désormais journalisées au niveau debug.
- Les utilitaires `utc_now_iso` et `env_bool` sont centralisés dans `src/common.py`.
- Les middlewares HTTP et le limiteur de débit sont extraits d'`app_api.py` vers
  `src/http_security.py`.
- L'installateur ne télécharge plus aucun modèle sans le demander. Dès le début, avant les
  étapes longues, il affiche le tableau des modèles de chat (`qwen3.5:9b` validé,
  `granite4.2:8b` et `qwen3.5:4b` avec réserves) avec, pour chacun, son fonctionnement sur la
  machine (GPU, GPU+CPU lent, CPU seul ou trop gros), puis demande lequel télécharger ; il demande
  aussi pour `embeddinggemma`. Sans réponse, rien n'est téléchargé. La sélection automatique de
  `qwen3.5:9b` et l'arrêt de l'installation sur une machine trop modeste disparaissent.
- Découverte : les cartes de résultats affichent la pertinence estimée par le modèle (sur 100)
  sans ouvrir les détails, un DOI cliquable et rien quand l'année ou le DOI manquent. Sans PDF
  en accès libre, « Ouvrir chez l'éditeur » devient l'action principale au lieu d'un bouton de
  téléchargement grisé. Les libellés des cartes sont traduits en anglais. L'historique ne montre
  plus que la date et le nombre de résultats (modèle, durée et sources en infobulle). La
  pastille « Externe désactivé » disparaît au profit de la seule bannière « Activer ».
- Découverte, formulaire de recherche : le mode « Recherche par mots-clés » devient « Décrire
  un sujet », puisque la recherche accepte des phrases. Le menu déroulant du mode est remplacé
  par deux boutons, le nombre de résultats tient sur la même ligne, et « Lancer la recherche »
  passe dans le coin bas droit de la zone de saisie. Le texte indicatif et l'aide s'adaptent
  au mode carnet.

### Corrigé

- Installation simple : sur un ordinateur où aucun modèle conseillé ne tient (carte graphique
  de moins de 5 Go, ou moins de 12 Go de mémoire vive sans carte graphique), elle ne
  téléchargeait aucun modèle de conversation, et l'assistant de démarrage restait bloqué sur
  « aucun modèle de conversation n'est installé ». Elle prend désormais `qwen3.5:2b`, y compris
  sur une petite carte graphique (moins de 3 Go) où il déborde en mémoire vive comme Ollama le
  fait par défaut. L'application prévient alors une fois que ce modèle très petit peut donner
  des réponses fausses, et propose de connecter une API externe. Elle ne le choisit pas par
  défaut quand un autre modèle est installé. Sous la configuration minimale (8 Go de mémoire et
  4 cœurs), l'installation simple le dit avant d'installer sans IA, en proposant un fournisseur
  distant (Annuler revient au choix du type d'installation). Quand l'ordinateur n'a pas pu être
  analysé, elle demande quelle IA utiliser au lieu de n'en prendre aucune, `qwen3.5:2b` coché.
- Paquet ZIP : `Install-Bibliobrain.cmd` propose les mêmes choix que l'installateur. Entrée
  accepte l'installation simple (l'IA conseillée et le module de recherche), `C` ouvre le
  choix personnalisé, avec les mêmes avertissements. Sans réponse, rien n'est téléchargé.
- Préparation des PDF sans modèle de conversation : elle n'en a besoin d'aucun (seul le modèle
  d'indexation sert), mais l'assistant de démarrage refusait de la lancer. Elle se fait
  désormais, avec un avertissement et la commande pour en installer un ; le chat dit ensuite
  que les documents sont prêts et comment ajouter un modèle ou une API externe.
- Assistant de démarrage : « Ollama répond, mais aucun modèle de conversation n'est installé »
  s'affichait aussi quand le modèle d'indexation ne fonctionnait pas ou que la liste des
  modèles n'avait pas pu être lue. Chaque cas a maintenant son propre message, avec le détail
  de l'erreur du modèle d'indexation.
- Installateur : relancé alors qu'une installation précédente tournait encore en arrière-plan
  (par exemple après avoir fermé l'installateur depuis le Gestionnaire des tâches), il
  démarrait une deuxième installation dans le même dossier. Il s'arrête désormais sans rien
  modifier et explique qu'il faut attendre la fin de la première ou redémarrer l'ordinateur.
- Désinstallation : lancée normalement (`unins000.exe`, ou « Applications installées » de
  Windows), elle s'arrêtait dès la confirmation sur « Runtime error: Could not call proc » et
  ne retirait rien. Le script écrivait dans la fenêtre de progression avant qu'elle existe ;
  seule la désinstallation silencieuse fonctionnait.
- Premier lancement : l'assistant pré-remplissait le dossier des PDF avec le dossier de
  données interne de Bibliobrain, qui n'existe pas encore sur une installation neuve ;
  « Continuer » répondait « dossier introuvable ». Le champ reste vide tant qu'aucun
  dossier n'est choisi ou utilisé (`data_path_configured`).
- Sélecteurs de dossier et de fichier (Windows) : la fenêtre pouvait ne jamais apparaître
  et le bouton tourner sans fin. Ils utilisent maintenant la boîte de dialogue de
  l'Explorateur depuis un petit processus Python (`src/native_dialog_worker.py`) au lieu
  d'un PowerShell caché et de l'ancienne arborescence « Rechercher un dossier » ; si
  aucune fenêtre n'est visible après 20 secondes, l'interface l'annonce et propose de
  saisir le chemin. Sans dossier de départ valable, le sélecteur s'ouvre là où
  l'utilisateur se trouvait la dernière fois plutôt que dans le dossier d'installation.
- Chat : un message sans contenu exploitable (vide, ponctuation ou emojis seuls) reçoit une
  demande de précision avant toute initialisation du RAG ; les expressions scientifiques
  courtes (« 77 K », « ∇ », « α ») restent acceptées.
- Chat : une source portant un label explicite n'autorise plus une citation différente
  déduite de sa position (un passage étiqueté S9 validait aussi `[S1]`), et le guide de
  citations ne présente plus S9 comme un label interdit.
- Installeur : une mise à jour laissait en place les modules supprimés ou renommés par la
  nouvelle version, que Python pouvait encore importer ; `src` et `static` sont désormais
  vidés avant la copie (`[InstallDelete]`).
- Installeur : un échec d'`install.ps1` (connexion coupée, disque plein) passait inaperçu,
  l'assistant annonçant une installation réussie et la mise à jour en un clic aussi.
  `Bibliobrain-Setup.exe` lance désormais le script lui-même, affiche l'échec avec le chemin
  du journal et renvoie le code de sortie 20 ; le gestionnaire propose alors
  `Repair-Bibliobrain.cmd`.
- Mise à jour en un clic : la console reposait la question du modèle de chat et attendait
  jusqu'à cinq minutes. Une mise à jour ne touche plus à Ollama ni à ses modèles
  (`-SkipOllamaSetup`), pour un paquet comme pour un clone Git.
- Mise à jour : une version bêta (`0.8.0-beta.1`) était refusée par `Update-Bibliobrain.cmd`
  et par la mise à jour en un clic, et ne pouvait pas être remplacée par la version finale.
  Le gestionnaire compare les versions comme l'application.
- Installeur : relancer `Bibliobrain-Setup.exe` pendant que Bibliobrain tourne remplaçait le
  code sous le serveur ; il est maintenant arrêté avant la copie.
- Désinstallation : le dossier du programme restait en place à cause des `__pycache__` et de
  `bibliobrain-install.log` ; ils sont supprimés.
- Démarrage : si le port 7860 est pris par un autre programme, le message le nomme au lieu
  d'annoncer un délai dépassé ; un serveur encore en cours de chargement est attendu (jusqu'à
  3 minutes) au lieu d'être doublé par un second ; le raccourci du Bureau, qui n'a pas de
  console, affiche l'erreur dans une fenêtre.
- Installeur : Python et le runtime Visual C++ se téléchargent avec les mêmes reprises
  qu'Ollama (`curl` puis PowerShell, trois essais) et sans la barre de progression qui
  ralentissait PowerShell 5.1 ; l'espace disque libre est vérifié avant une première
  installation ; les lignes parasites `System.Management.Automation.RemoteException`
  disparaissent de la console. L'assistant n'annonce plus un modèle de chat « choisi
  automatiquement ».
- Publication : `release.yml` exécute le test du paquet (`test-installer-payload.ps1`) avant
  de publier ; ce test vérifie aussi les arguments et le code de retour d'`install.ps1`.
- Les modèles Ollama sans profil dédié (par exemple `granite4.2`, `ornith-1.5`) raisonnaient
  même quand la réflexion était désactivée, car le champ `think` n'était pas envoyé : réponses
  vides ou très lentes. `think=false` est désormais explicite, et le brouillon affiché avant une
  balise `</think>` orpheline est retiré de la réponse visible.

- Installation impossible sur un poste géré (compte de domaine sans `SeSecurityPrivilege`) :
  `install.ps1` échouait en toute fin d'installation sur `Set-Acl`
  (`PrivilegeNotHeldException`), qui tente de réécrire la SACL. Le durcissement de
  `configuration.json` passe désormais par `icacls` puis par l'API .NET, qui ne modifient
  que la DACL, et un échec devient un avertissement au lieu d'interrompre l'installation.
- Rédaction, fenêtre étroite : les résultats de la recherche de citations étaient coupés sans
  défilement possible, et entre ~820 et 980 px de large le panneau latéral était écrasé à
  quelques pixels. Quand éditeur et panneau sont empilés, la zone défile et un séparateur
  permet d'ajuster la hauteur de l'éditeur (double-clic pour revenir à la taille par défaut).
- Découverte, fenêtre étroite : entre ~820 et 1080 px avec la barre latérale repliée, un grand
  vide séparait l'historique de la recherche. L'empilement suit désormais la largeur réelle
  du panneau. Sur mobile, le tiroir d'historique (Découverte et Discussion) s'affichait en
  bande horizontale de 100 px : c'est de nouveau une liste verticale. Historique replié vers
  1100 px : ses deux boutons ne débordent plus de la barre.

[Non publié]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.2...HEAD
[0.8.2]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/bibliOwl/bibliobrain/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/bibliOwl/bibliobrain/releases/tag/v0.8.0
