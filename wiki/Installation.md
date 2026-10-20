# Installation

Français · [English](Installation-en.md)

Bibliobrain fonctionne sous **Windows 10 ou 11**. Il s'ouvre dans votre navigateur, à
l'adresse `http://localhost:7860`.

## Ce qu'il vous faut

- Windows 10 ou 11 et une connexion Internet pendant l'installation.
- De la place disque : l'installation télécharge des modèles d'IA de plusieurs Go.
- Idéalement une carte graphique NVIDIA, mais ce n'est pas obligatoire.

Python et Ollama (le programme qui fait tourner l'IA sur votre ordinateur) sont
installés automatiquement s'ils manquent.

## Installer

1. Téléchargez `Bibliobrain-Setup-X.Y.Z.exe` (X.Y.Z est le numéro de version) depuis la page **Releases** du dépôt.
   <!-- TODO: lien Releases -->
2. Lancez-le. Les versions alpha ne sont pas signées : Windows SmartScreen peut afficher
   un avertissement.
3. Choisissez le type d'installation :
   - **Installation simple** (conseillée, par défaut) : Bibliobrain installe tout ce qu'il
     lui faut, dont l'IA qui convient à votre ordinateur et le module de recherche dans vos
     documents.
   - **Installation personnalisée** : vous choisissez le dossier, l'IA à télécharger et les
     raccourcis.
4. Laissez l'installation se terminer (c'est long surtout à cause des téléchargements).
   À la fin, la case **Lancer Bibliobrain** ouvre l'application dans le navigateur.

Le dossier d'installation par défaut est `%LOCALAPPDATA%\Programs\Bibliobrain`,
généralement `C:\Users\<nom_utilisateur>\AppData\Local\Programs\Bibliobrain`. Il contient
le programme et les scripts de maintenance. Pour l'ouvrir, appuyez sur **Win + R**,
collez `%LOCALAPPDATA%\Programs\Bibliobrain` et validez avec **Entrée**.

Si l'installation échoue, relancez l'installateur : il reprend là où il s'est arrêté. En
cas de nouvel échec, joignez le fichier `bibliobrain-install.log` (dans le dossier
d'installation) à votre signalement.

### Si votre antivirus bloque l'installateur

Certains antivirus mettent `Bibliobrain-Setup-X.Y.Z.exe` en quarantaine parce qu'il est récent et
non signé. Chaque version propose aussi `Bibliobrain-Windows.zip`, les mêmes fichiers sans
aucun exécutable :

1. Téléchargez `Bibliobrain-Windows.zip` depuis la même page **Releases**.
2. Clic droit sur le ZIP > **Propriétés** > cochez **Débloquer** > **OK**.
3. Extrayez-le dans un dossier que vous garderez (par exemple `Documents`).
4. Double-cliquez sur **`Install-Bibliobrain.cmd`** dans ce dossier et répondez aux
   questions dans la fenêtre.

Une installation faite avec le ZIP se met à jour avec le ZIP suivant, jamais avec
l'installateur.

## Choisir un modèle de conversation

Le modèle de conversation est l'IA qui rédige les réponses. Plus il est gros, meilleur
il est, mais plus il demande de mémoire. L'installateur analyse votre ordinateur, coche
celui qui lui convient et indique pour chacun s'il sera rapide ou lent chez vous.

| Modèle | Taille | Mémoire conseillée |
|---|---|---|
| `qwen3.5:9b` (validé) | 6 Go | 8 Go de VRAM, ou 24 Go de RAM sans GPU |
| `granite4.2:8b` | 5 Go | 6 Go de VRAM, ou 16 Go de RAM sans GPU |
| `qwen3.5:4b` (plus léger) | 3 Go | 5 Go de VRAM, ou 12 Go de RAM sans GPU |

Pour les ordinateurs modestes, l'installateur propose aussi des modèles plus petits,
marqués **moins fiable** : ils fonctionnent, mais leurs réponses et leurs citations
contiennent plus d'erreurs. Ils ne sont jamais conseillés.

Un modèle trop gros pour la mémoire de votre carte graphique fonctionne quand même, mais
beaucoup plus lentement. Les modèles déjà installés dans Ollama sont listés : en choisir
un ne télécharge rien. Vous pourrez changer de modèle plus tard dans
**Paramètres > Assistant IA**.

## Lancer, arrêter

- **Lancer** : double-cliquez sur le raccourci **Bibliobrain** (menu Démarrer, Bureau ou
  dossier du logiciel). Le navigateur s'ouvre après quelques secondes.
- **Arrêter** : fermez le dernier onglet Bibliobrain, ou utilisez **Quitter Bibliobrain**
  en bas de la barre latérale. `Stop-Bibliobrain.cmd` arrête le serveur si besoin.

## Mettre à jour

Au premier lancement, Bibliobrain vous demande s'il peut vérifier une fois par jour la
présence d'une nouvelle version (**Vérifier chaque jour** ou **Non merci**). Rien n'est
envoyé tant que vous n'avez pas dit oui, et rien n'est installé sans votre clic.

- Quand une version est disponible, le bouton **Mise à jour disponible** apparaît dans la
  barre latérale. Il ouvre **Paramètres > Réglages experts > Diagnostic**, section
  **Version et mises à jour**, où **Mettre à jour vers…** télécharge, vérifie, sauvegarde
  vos données, installe puis redémarre Bibliobrain. Cela marche quand Bibliobrain a été
  lancé depuis son raccourci.
- Dans cette même section : la case **Vérifier les mises à jour chaque jour** et le bouton
  **Vérifier maintenant**.
- Sans passer par l'application : lancez `Update-Bibliobrain.cmd` (une sauvegarde est
  faite avant). Il vous demande de choisir le nouveau `Bibliobrain-Setup-X.Y.Z.exe`, ou le
  nouveau `Bibliobrain-Windows.zip` si vous avez installé avec le ZIP.

## Les commandes de maintenance

Dans le dossier d'installation :

| Fichier | Rôle |
|---|---|
| `Update-Bibliobrain.cmd` | Mettre à jour (une sauvegarde est faite avant) |
| `Repair-Bibliobrain.cmd` | Réparer une installation abîmée |
| `Backup-Bibliobrain.cmd` | Sauvegarder réglages, index, historiques et brouillon |
| `Restore-Bibliobrain.cmd` | Restaurer une sauvegarde |
| `Uninstall-Bibliobrain.cmd` | Désinstaller (vos données sont conservées par défaut) |

Vos données (index, réglages, historiques, brouillon) sont rangées dans
`%LOCALAPPDATA%\bibliobrain`, séparées du programme.
Pour les sauvegardes, les journaux et la désinstallation, voir [Maintenance](Maintenance.md).

## Désinstaller

Avec l'installateur Windows, ouvrez **Paramètres Windows > Applications**, sélectionnez
**Bibliobrain** puis **Désinstaller**. Avec un ZIP ou un clone Git, lancez
`Uninstall-Bibliobrain.cmd` dans le dossier d'installation. Vos données sont conservées
par défaut. Le détail et le journal de désinstallation sont décrits dans [Maintenance](Maintenance.md).
