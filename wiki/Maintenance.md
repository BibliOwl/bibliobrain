# Sauvegardes et maintenance

Français · [English](Maintenance-en.md)

Les commandes ci-dessous se trouvent dans le dossier d'installation de Bibliobrain.
Avec l'installateur Windows, il s'agit par défaut de `%LOCALAPPDATA%\Programs\Bibliobrain` :
appuyez sur **Win + R**, collez ce chemin et validez avec **Entrée** pour l'ouvrir.
Pour installer ou mettre à jour le logiciel, voir [Installation](Installation.md).

## Sauvegarder et restaurer

`Backup-Bibliobrain.cmd` arrête brièvement l'application et archive la configuration,
les index, les réglages, les historiques et le brouillon de rédaction.
`Restore-Bibliobrain.cmd` valide l'archive, crée d'abord une sauvegarde de retour
arrière, restaure les fichiers puis redémarre l'application.

Les documents placés hors du dossier de données de Bibliobrain ne sont pas inclus
par défaut. Pour les inclure, utilisez la commande PowerShell suivante depuis le
dossier d'installation :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bibliobrain-manager.ps1 -Action backup -IncludeExternalDocuments
```

Les clés API chiffrées avec Windows (DPAPI) ne fonctionnent après restauration
qu'avec le même compte Windows.

## Réparer ou mettre à jour

- `Repair-Bibliobrain.cmd` réinstalle les composants manquants d'une installation abîmée.
- `Update-Bibliobrain.cmd` crée une sauvegarde avant de mettre à jour Bibliobrain.
  Il vous demande de sélectionner le nouvel installateur ou le nouveau ZIP selon
  votre mode d'installation. Une signature invalide ou une version plus ancienne
  est refusée ; une alpha non signée demande une confirmation supplémentaire.

Les données sont séparées du programme et conservées pendant ces opérations.
Si une mise à jour échoue, lancez `Repair-Bibliobrain.cmd`.

## Journaux

Les données de Bibliobrain se trouvent dans `%LOCALAPPDATA%\bibliobrain`
(ou dans le dossier choisi avec `BIBLIOBRAIN_HOME`). Les journaux utiles sont :

| Fichier | Contenu |
|---|---|
| `logs\bibliobrain.log` | Journal de l'application ; rotation de cinq fichiers de 5 Mio |
| `logs\server.out.log`, `logs\server.err.log` | Sortie du dernier démarrage |
| `bibliobrain-install.log`, dans le dossier d'installation | Détail de la dernière installation |
| `%TEMP%\bibliobrain-uninstall.log` | Détail de la désinstallation, conservé après le nettoyage |

Pour signaler un problème, voir [Dépannage](Depannage.md). L'envoi d'un rapport se fait
uniquement après une action explicite dans l'interface. Une valeur vide active
automatiquement le DSN officiel en écriture seule ; pour désactiver complètement
les rapports, définissez `BIBLIOBRAIN_ERROR_REPORTING_ENABLED` à `false` dans
`configuration.json`, dans le dossier de données.

## Désinstaller

1. Si vous souhaitez conserver une sauvegarde, lancez `Backup-Bibliobrain.cmd`.
2. Si vous avez utilisé l'installateur Windows, ouvrez **Paramètres Windows >
   Applications**, sélectionnez **Bibliobrain** puis **Désinstaller**. Avec un ZIP ou
   un clone Git, lancez `Uninstall-Bibliobrain.cmd` et confirmez.
3. Laissez le nettoyage se terminer : la fenêtre affiche l'étape et la progression.

Avec l'installateur Windows, le désinstalleur retire les fichiers de l'application.
Avec un ZIP ou un clone Git, la commande retire l'environnement Python de
Bibliobrain et ses lanceurs ; le dossier source reste présent.
`Uninstall-Bibliobrain.cmd` effectue ce même nettoyage du runtime si vous le lancez
dans une installation Windows ; utilisez le désinstalleur Windows pour retirer
aussi les fichiers de l'application et son entrée dans la liste des applications.

Vos index, réglages, clés API, historiques et brouillons sont conservés par défaut
dans le dossier de données. Les documents de votre bibliothèque et votre
bibliothèque Zotero ne sont pas supprimés. Python installé sur Windows, Ollama
et les modèles téléchargés restent installés.

Si le nettoyage échoue, consultez `%TEMP%\bibliobrain-uninstall.log`, fermez Ollama
si le message le demande, puis relancez la désinstallation.
