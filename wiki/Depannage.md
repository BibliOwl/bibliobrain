# Dépannage

Français · [English](Troubleshooting-en.md)

## L'application ne s'ouvre pas

- Double-cliquez à nouveau sur le raccourci et patientez jusqu'à une minute.
- Essayez `http://localhost:7860` dans votre navigateur.
- Lancez `Repair-Bibliobrain.cmd` (réinstalle les composants manquants ; vos données
  sont séparées du programme, sous `%LOCALAPPDATA%\bibliobrain`).
- Si rien n'y fait, regardez `%LOCALAPPDATA%\bibliobrain\logs\bibliobrain.log`.

## L'installateur est bloqué par l'antivirus

Utilisez le paquet `Bibliobrain-Windows.zip` de la même version (voir [Installation](Installation.md),
« Si votre antivirus bloque l'installateur »).

## L'indicateur ne passe pas à « Prêt »

- La préparation d'une grosse bibliothèque peut durer longtemps la première fois. Elle
  continue en arrière-plan : cliquez sur l'indicateur pour voir où elle en est.
- Vérifiez qu'Ollama est démarré : si le détail de l'indicateur affiche **Moteur IA
  arrêté**, cliquez sur **Démarrer**.
- Dans **Paramètres > Vue d'ensemble**, cliquez sur le bouton de la ligne à compléter
  (**Préparer**, **Configurer** ou **Corriger**) : Bibliobrain dit ce qui manque et la
  commande à lancer.

## Mes nouveaux PDF n'apparaissent pas

Bibliobrain ne lit pas les nouveaux fichiers tout seul. Revenez sur sa fenêtre :
l'indicateur affiche **N nouveaux PDF**, cliquez sur **Ajouter** (voir
[Bibliothèque](Bibliotheque.md), « Ajouter des articles »).

## Le chat répond « je n'ai rien trouvé » ou répond à côté

- Attendez que l'indicateur affiche **Prêt**.
- Reformulez avec des termes qui figurent dans vos articles.
- Retirez les filtres de tags ou de collections du chat (**Options > Filtres Zotero**).
- Ouvrez **Cartographies > Dernière réponse** : vous y voyez ce qui a été retrouvé.
- Un modèle trop petit répond moins bien : essayez-en un plus gros (voir [Installation](Installation.md)).

## Les réponses sont très lentes

Le modèle de conversation est probablement trop gros pour la mémoire de votre carte
graphique et déborde sur le processeur. Choisissez un modèle plus léger dans
**Paramètres > Assistant IA**, ou le bouton **Rapide** à côté de la flèche d'envoi du chat.

## Des PDF n'apparaissent pas dans les recherches

Ce sont sans doute des **PDF scannés** (des photos de pages). Allez dans
**Paramètres > Mes documents > Lire les pages scannées**.

## La Découverte ne trouve rien

Vérifiez que **Autoriser la recherche sur le web** est activé dans
**Paramètres > Trouver des articles en ligne**. Si la recherche en ligne est désactivée,
Découverte l'indique et propose **Autoriser la recherche en ligne**.

## Le serveur reste actif après la fermeture du navigateur

Si le navigateur s'est fermé brutalement, utilisez `Stop-Bibliobrain.cmd`.

## Signaler un problème

Joignez toujours `logs\bibliobrain.log` (il ne contient ni clé ni mot de passe). Le bouton
**Signaler un bug** de l'application vous montre précisément ce qui sera envoyé avant tout
envoi. Vous pouvez aussi ouvrir un ticket sur le dépôt.
<!-- TODO: lien vers les tickets du dépôt -->

## Sauvegarde et restauration

`Backup-Bibliobrain.cmd` archive configuration, index, réglages, historiques et brouillon ;
`Restore-Bibliobrain.cmd` les restaure (une sauvegarde de retour arrière est faite avant).
Les clés d'API chiffrées ne fonctionnent que sur le même compte Windows.
Pour le détail des sauvegardes et des journaux, voir [Maintenance](Maintenance.md).
