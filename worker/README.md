# Relais SIRENE (Cloudflare Worker) — facultatif

L'API ouverte [Recherche d'entreprises](https://recherche-entreprises.api.gouv.fr/docs/)
n'envoie **aucun en-tête CORS** : un navigateur ne peut pas l'appeler directement. Ce Worker
ajoute ces en-têtes et met les réponses en cache 24 h.

## Le site fonctionne sans ce Worker

Le rattachement média → entreprise, la partie difficile, est **pré-calculé dans GitHub
Actions** (`scripts/match_sirene.py`) et versionné dans `data/sirene/cache.json`. Les fiches
affichent donc toujours les données SIRENE, avec la date de l'instantané.

Le Worker n'ajoute qu'une chose : un bouton « Rafraîchir depuis SIRENE » qui va rechercher la
fiche à la seconde, utile parce que SIRENE bouge quotidiennement alors que les listes CPPAP ne
changent qu'à chaque commission. En cas d'indisponibilité, la carte conserve l'instantané et
signale l'échec — jamais d'écran vide.

## Routes

| Route | Effet |
|---|---|
| `GET /api/entreprise/{siren}` | Fiche d'une entreprise (9 chiffres exigés) |
| `GET /api/search?q=…` | Recherche libre, filtres `departement`, `code_postal`, `activite_principale`, `nature_juridique`, `etat_administratif`, `categorie_entreprise`, `page`, `per_page` |
| `GET /health` | Contrôle de disponibilité |

Toute autre route renvoie 404, toute autre méthode 405 : c'est un relais sur liste blanche,
pas un proxy ouvert. `per_page` est plafonné à 25, la limite de l'amont.

## Déploiement

```sh
cd worker
npx wrangler login
npx wrangler deploy
```

Puis renseigner l'URL obtenue, au choix :

- dans `web/config.json`, clé `sireneProxy` ;
- ou via la variable de dépôt `SIRENE_PROXY_URL` (Settings → Variables), que
  `.github/workflows/pages.yml` transmet à `scripts/build_site.py`. C'est la voie
  recommandée : l'URL reste hors du code.

Le workflow `.github/workflows/worker.yml` déploie automatiquement à chaque modification de
`worker/`, **si** le secret `CLOUDFLARE_API_TOKEN` est présent ; sinon il s'arrête sans échouer.

## Limites de l'amont à connaître

- **7 requêtes/seconde par IP**, 30/seconde par ASN. Le Worker ne bride pas les appels : il
  s'appuie sur son cache, chaque fiche ne coûtant qu'une requête par période de 24 h.
- Réponse **429** avec `Retry-After` en cas de dépassement — relayée telle quelle.
- Les entreprises **non diffusibles** sont absentes de l'API : une fiche peut donc rester
  sans rattachement même quand l'entreprise existe.
