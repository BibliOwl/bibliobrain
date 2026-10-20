# Politique de sécurité

## Signaler une vulnérabilité

N’ouvrez pas d’issue publique pour un problème de sécurité. Utilisez le signalement privé de
GitHub : dans le dépôt [`bibliOwl/bibliobrain`](https://github.com/bibliOwl/bibliobrain), onglet
**Security**, puis **Report a vulnerability**. Le rapport n’est visible que du mainteneur.

Indiquez la version de Bibliobrain, les étapes pour reproduire le problème et son impact. Ne
joignez ni document confidentiel ni clé API.

## Exception temporaire de l’audit des dépendances

### ChromaDB — avis serveur et configuration de collection

- **Statut :** acceptée temporairement
- **Version utilisée :** `chromadb==1.5.7`
- **Échéance de révision :** 31 octobre 2026, et avant chaque publication
- **Dernière révision :** 11 octobre 2026 — aucune version corrigée publiée
- **Condition de retrait :** adopter la première version corrigée compatible, puis retirer
  l’exception de la CI

Les exceptions couvrent actuellement les avis suivants :

| Avis | Alias | Scénario amont | Applicabilité à Bibliobrain |
|---|---|---|---|
| `PYSEC-2026-311` | `CVE-2026-45829` | création de collection serveur avec fonction d’embedding contrôlée par un attaquant | non exposé : aucun serveur Chroma |
| `PYSEC-2026-3813` | `CVE-2026-45830` | accès inter-tenant par un utilisateur authentifié | non exposé : mode embarqué mono-utilisateur |
| `PYSEC-2026-3814` | `CVE-2026-45833` | exécution de code après modification serveur d’une collection avec `trust_remote_code` | non exposé : aucune API Chroma ni modification de configuration |
| `PYSEC-2026-3815` | `CVE-2026-45831` | `SimpleRBACAuthorizationProvider` ignore la ressource tenant/base/collection | non exposé : aucun fournisseur d’authentification Chroma |

Au 11 octobre 2026, les quatre fiches OSV donnent `chromadb` 1.5.9 comme dernière version
affectée, et 1.5.9 (5 mai 2026) est la dernière version publiée : aucune version corrigée
n’existe. `langchain-chroma==1.1.0`, toujours la dernière version, exige `chromadb>=1.3.5,<2`.
`pip-audit` sur `requirements.lock` ne signale aucun autre avis que ceux listés ici. Cette acceptation ne vaut que pour
l’architecture locale décrite ci-dessous ; elle ne s’applique pas à un déploiement du serveur
Python Chroma.

Bibliobrain réduit actuellement l’exposition avec les contrôles suivants :

- Chroma est utilisé en mode embarqué avec un répertoire local ;
- l’application ne monte pas les routes du serveur Python Chroma ;
- aucun `chromadb.HttpClient` ou `chromadb.AsyncHttpClient` n’est utilisé ;
- aucun fournisseur `chromadb.auth`, notamment `SimpleRBACAuthorizationProvider`, n’est utilisé ;
- aucune collection Chroma n’est créée ou modifiée à partir d’une configuration fournie par
  une requête utilisateur ;
- le code applicatif interdit `trust_remote_code=True` ;
- chaque construction de `langchain_chroma.Chroma` fournit explicitement la fonction
  d’embedding gérée par Bibliobrain ;
- le serveur Bibliobrain écoute sur `127.0.0.1` par défaut.

Ces invariants sont contrôlés par `tests/test_chromadb_security.py`. Toute évolution vers un
serveur Chroma, un client Chroma distant, un fournisseur d’authentification Chroma, une
modification de configuration de collection ou une fonction d’embedding issue de cette
configuration doit bloquer la publication jusqu’à réévaluation de cette exception.

L’exception CI ne signifie pas que la vulnérabilité est corrigée. Elle documente un risque
résiduel accepté en attendant une correction amont.

Sources de révision : fiches OSV
[`PYSEC-2026-3813`](https://osv.dev/vulnerability/PYSEC-2026-3813),
[`PYSEC-2026-3814`](https://osv.dev/vulnerability/PYSEC-2026-3814) et
[`PYSEC-2026-3815`](https://osv.dev/vulnerability/PYSEC-2026-3815),
[signalement amont `chroma-core/chroma#7588`](https://github.com/chroma-core/chroma/issues/7588)
et [avis HiddenLayer `2026-06-chromadb-5`](https://www.hiddenlayer.com/sai-security-advisory/2026-06-chromadb-5).

## Contrôles obligatoires avant publication

Les audits Python, JavaScript et de secrets ne sont pas optionnels dans la CI. Une publication
doit provenir d’un commit pour lequel les trois tâches ont été exécutées avec succès :

- `pip-audit` sur `requirements.lock`, avec uniquement les exceptions documentées ci-dessus ;
- `npm audit` sur `package-lock.json`, après installation reproductible avec `npm ci` et sans
  scripts de cycle de vie ;
- Gitleaks sur l’historique complet du dépôt.

Une nouvelle exception doit être ajoutée simultanément à ce document, à la commande CI et aux
tests de garde-fou qui démontrent pourquoi le scénario n’est pas exposé.
