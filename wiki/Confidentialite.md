# Confidentialité

Français · [English](Privacy-en.md)

**Par défaut, vos documents et leur index ne quittent pas votre ordinateur.** Voici ce qui
change si vous activez certaines options.

## Ce qui reste toujours local

- Vos PDF, leur texte et leur index.
- Les questions du chat et les réponses, tant que l'assistant IA est réglé sur
  **Sur cet ordinateur**.
- Zotero : Bibliobrain lit sa base sans jamais la modifier. Un PDF que vous déposez dans
  Bibliobrain est confié à Zotero lui-même, qui crée sa fiche.

## Ce qui peut sortir, uniquement si vous l'activez

| Option | Ce qui est envoyé | Où l'activer |
|---|---|---|
| **Service en ligne** (IA distante) | Vos questions et les extraits nécessaires à la réponse, au fournisseur que vous avez choisi | Paramètres > Assistant IA |
| **Un autre ordinateur proche** | Les mêmes informations, à cette machine de votre réseau | Paramètres > Assistant IA |
| **Autoriser la recherche sur le web** | Mots-clés, titres et DOI vers OpenAlex, Crossref, Semantic Scholar, arXiv, HAL… Jamais vos PDF | Paramètres > Trouver des articles en ligne |
| **Vérifier les mises à jour chaque jour** | Une requête par jour vers la page des versions publiées sur GitHub, sans identifiant ni rien sur vos documents ; GitHub voit votre adresse IP | Paramètres > Réglages experts > Diagnostic |
| Rapport de bug | Votre description, votre e-mail de contact si vous en donnez un et la version de Bibliobrain ; les diagnostics, les vingt derniers messages du chat et une capture d'écran **seulement si vous les cochez**. L'écran liste exactement ce qui sera joint. Le nom de l'ordinateur n'est pas envoyé | Bouton **Signaler un bug** |

Les rapports de bug arrivent sur un serveur administré par le mainteneur du projet. Qui en
est responsable, combien de temps ils sont conservés et comment les faire effacer : voir la
[politique de confidentialité](../PRIVACY.md).

La cartographie **Auteur ou labo** envoie le nom ou l'identifiant recherché à OpenAlex.

## Vos clés et vos données

- Les clés d'API sont chiffrées pour votre compte Windows.
- Les journaux (`%LOCALAPPDATA%\bibliobrain\logs`) ne contiennent ni clé ni adresse
  complète. Vous pouvez les joindre à un signalement.
- Bibliobrain n'écoute que sur votre ordinateur.

## Fiabilité des réponses

Une réponse d'IA peut être fausse ou incomplète. Bibliobrain fait vérifier les citations
par rapport au texte réel de vos documents et retire celles qui n'y figurent pas, mais la
relecture du passage cité reste de votre responsabilité.
