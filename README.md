# cppap-recherche

Moteur de recherche permettant de trouver les médias et leurs agréments CPPAP sur la base de
leur nom, **rattachés à leur entreprise éditrice dans la base SIRENE** (siège social, activité,
dirigeants, effectifs), avec **archivage dans GitHub de chaque nouvelle version publiée** des
listes officielles.

Application légère : un site statique sans étape de compilation servi par GitHub Pages, des
scripts Python sans aucune dépendance de production, et un Cloudflare Worker facultatif.

## Ce que fait le projet

1. **Se connecte** aux trois jeux Open Data de la [CPPAP](https://www.cppap.fr) publiés sur
   data.gouv.fr :
   [services de presse en ligne](https://www.data.gouv.fr/datasets/liste-des-services-de-presse-en-ligne-reconnus),
   [publications de presse](https://www.data.gouv.fr/datasets/liste-des-publications-de-presse),
   [agences de presse agréées](https://www.data.gouv.fr/datasets/liste-des-agences-de-presse-agreees).
2. **Archive** chaque nouvelle version : instantané daté immuable dans `data/raw/`, vue
   normalisée dans `data/latest/`, historique dans `data/manifest.json`, et une **Release
   GitHub par version** avec le différentiel ligne à ligne — de quoi citer l'état exact d'une
   liste à une date donnée.
3. **Recherche** par nom de média, d'éditeur ou n° CPPAP, tolérante aux accents et aux fautes
   de frappe, avec filtres par type, département et qualification IPG.
4. **Restitue une carte dédiée** : agrément CPPAP, entreprise déclarée, puis fiche SIRENE
   complète de l'éditeur — siège social avec adresse, code NAF, nature juridique, date de
   création, effectifs, état administratif, dirigeants.

## Comment le rattachement à SIRENE est établi

Les trois listes ne se valent pas sur ce point : **certaines publient le SIRET de l'éditeur,
d'autres non.** Le rattachement emprunte donc quatre chemins, de fiabilité décroissante, et
**chaque fiche affiche celui qui a été emprunté** :

| Niveau affiché | Origine | Fiabilité |
|---|---|---|
| `Vérifié manuellement` | correction humaine dans `data/sirene/overrides.csv`, relue en PR | maximale |
| `SIRET publié par la CPPAP` | la liste déclare elle-même le SIRET → **jointure exacte** | exacte |
| `SIRET déclaré sur une autre liste` | la liste ne publie pas de SIRET, mais le même éditeur en déclare un dans une autre liste CPPAP | exacte par déduction |
| `Certaine` / `Probable` / `Incertaine` / `Aucune` | rapprochement heuristique sur la raison sociale | à vérifier |

Un cinquième niveau, `SIRET publié, entreprise absente`, signale un SIRET officiel dont
l'entreprise ne figure pas dans l'API — non diffusible ou radiée. L'identifiant reste vrai ;
c'est la fiche entreprise qui manque.

Là où le SIRET est absent, le rapprochement se fait sur le nom, et il est faillible. Trois
garde-fous :

- le niveau de confiance est **toujours affiché**, avec les autres candidats envisagés ;
- le résultat est **calculé une fois puis versionné** dans `data/sirene/cache.json`, donc
  relisible en diff — jamais recalculé silencieusement ;
- il est **corrigeable à la main** via `data/sirene/overrides.csv`, prioritaire sur tout le reste.

Un rattachement `probable` ou `incertain` ne doit jamais être présenté comme un fait. Le filtre
« Rattachement SIRENE à vérifier » de l'interface isole précisément ces cas.

L'ordre de priorité vit dans un seul module, `scripts/lib/resolution.py`, partagé par
l'appariement et la génération du site : les statistiques annoncées décrivent donc exactement
ce que les fiches affichent.

## Architecture

```
data.gouv.fr ──┐
               │  scripts/ingest.py        archive + manifeste + détection de changement
               ▼
        data/raw/ · data/latest/ · data/manifest.json
               │
               │  scripts/match_sirene.py  SIRET publié -> jointure exacte, sinon nom
               ▼
        data/sirene/cache.json  (+ overrides.csv)
               │
               │  scripts/build_site.py    index compact + 32 lots de détail
               ▼
        site/  ──> GitHub Pages
               │
               └─ (facultatif) worker/ ──> rafraîchissement SIRENE à la demande
```

| Élément | Rôle |
|---|---|
| `config/sources.json` | Les trois sources et, surtout, la **carte d'alias de colonnes** |
| `config/labels.json` | Libellés NAF, natures juridiques, tranches d'effectifs, niveaux de confiance |
| `config/departements.json` | Codes et libellés de départements |
| `scripts/lib/resolution.py` | **Ordre de priorité du rattachement**, partagé par l'appariement et le site |
| `scripts/lib/` | Normalisation de texte, client HTTP à débit limité, lecture CSV/XLSX |
| `web/` | Site statique, JS vanilla, MiniSearch vendorisé — aucun CDN |
| `worker/` | Relais CORS facultatif vers l'API Recherche d'entreprises |
| `scripts/build_preview.py` | Assemble le site en un fichier HTML unique, consultable hors ligne |

Stack : **Python 3.11, bibliothèque standard uniquement** (`urllib`, `csv`, `unicodedata`,
`difflib`, `zipfile`). Aucune dépendance de production, ni côté scripts, ni côté navigateur.

Le site charge un index compact au démarrage (~350 Ko compressés pour 15 000 fiches) et va
chercher les fiches complètes dans l'un des 32 lots à l'ouverture d'une carte. Le lot est
déduit d'une empreinte de l'identifiant, donc **stable entre deux publications** : le cache du
navigateur survit aux mises à jour de données.

### Tolérance aux changements de schéma

Les en-têtes de ces fichiers ont déjà changé (« IPG » renommé « Qualification » en 2019,
libellé de département précisé en 2020). L'appariement des colonnes est donc **déclaratif** :
`config/sources.json` liste, pour chaque champ canonique, les libellés plausibles, comparés sur
en-tête normalisé (minuscules, accents pliés) — d'abord à l'identique, puis par inclusion au
mot entier pour absorber les intitulés rallongés.

C'est ce mécanisme qui absorbe l'hétérogénéité entre listes : la colonne `SIRET` est déclarée
pour les trois sources, et simplement absente du résultat là où la liste ne la publie pas.

- Toute colonne non reconnue est **conservée** dans l'objet `extra` de la fiche : aucune perte.
- Un champ **requis** introuvable fait **échouer bruyamment** l'ingestion, en affichant les
  en-têtes rencontrés, et le workflow ouvre une issue. Mieux vaut alerter que publier des
  fiches silencieusement vides.

## Mise en route

### Développement local, sans réseau

```sh
pip install -e '.[dev]'              # ruff + pytest ; aucune dépendance de production

ruff check . && ruff format --check .
pytest                               # tests Python, sur fixtures
node --test worker/index.test.mjs    # tests du relais

# Site de démonstration sur données synthétiques
python scripts/build_site.py --from-fixtures
python -m http.server -d site 8000
```

### Aperçu en un seul fichier

```sh
python scripts/build_site.py            # ou --from-fixtures
python scripts/build_preview.py --out apercu.html
```

Produit un HTML autonome — données, styles et scripts embarqués — qui s'ouvre directement dans
un navigateur, sans serveur. Utile pour archiver l'outil tel qu'il était à une date donnée, ou
le consulter hors ligne. Le site publié reste, lui, découpé en index compact plus lots de
détail : cet assemblage n'a pas d'intérêt pour la publication courante.

### Sur données réelles

```sh
python scripts/ingest.py             # télécharge et archive si le contenu a changé
python scripts/match_sirene.py       # complète le cache d'appariement (incrémental)
python scripts/build_site.py         # génère site/
```

`python scripts/normalize.py --source spel --limit 3` affiche les colonnes détectées et
quelques fiches : c'est l'outil à lancer en premier quand un fichier source change de forme.

### Configuration du dépôt

| Réglage | Où | Effet |
|---|---|---|
| Pages | Settings → Pages → Source : **GitHub Actions** | Nécessaire pour publier le site |
| `SIRENE_PROXY_URL` | Settings → Variables | URL du Worker ; absente, les fiches utilisent l'instantané archivé |
| `CLOUDFLARE_API_TOKEN` | Settings → Secrets | Déploiement automatique du Worker ; absent, l'étape est ignorée sans échec |

## Workflows

| Workflow | Déclencheur | Rôle |
|---|---|---|
| `ci.yml` | push, PR | `ruff`, `pytest`, tests du Worker, génération du site sur fixtures |
| `sync.yml` | cron hebdomadaire, manuel | Ingestion, archivage, appariement SIREN, commit, **Release de version** |
| `pages.yml` | après `sync.yml`, push, manuel | Génère et déploie le site |
| `worker.yml` | modification de `worker/` | Déploie le relais si le secret est présent |

`sync.yml` **ne commite rien** si le contenu n'a pas changé — les listes ne bougent qu'à chaque
commission (~4 fois par an), le dépôt reste donc léger malgré un cron hebdomadaire.

Le premier run est le moment de vérité : ses journaux affichent les **en-têtes réellement
rencontrés** dans chaque fichier, ce qui permet de compléter `config/sources.json` si un alias
manque.

## Corriger un rattachement SIREN

1. Ouvrir la fiche, comparer avec la
   [fiche Annuaire des entreprises](https://annuaire-entreprises.data.gouv.fr) liée.
2. Ajouter une ligne à `data/sirene/overrides.csv` :

   ```csv
   cle,siren,note
   societe editrice du monde|75,123456789,Vérifié sur annuaire-entreprises le 2026-07-28
   ```

   `cle` peut désigner une fiche précise (son identifiant ou son n° CPPAP) ou tout un éditeur
   (la clé d'éditeur telle qu'affichée dans `data/sirene/cache.json`) — la plus spécifique gagne.
3. Ouvrir une pull request. L'override a priorité absolue sur l'heuristique et la fiche passe
   au niveau `vérifié`.

## Sources et licences

- Données : listes CPPAP publiées par le ministère de la Culture sur
  [data.gouv.fr](https://www.data.gouv.fr), sous
  [Licence Ouverte v2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/).
- Données entreprises :
  [API Recherche d'entreprises](https://recherche-entreprises.api.gouv.fr/docs/) (DINUM),
  construite sur SIRENE (INSEE) et le RNE. Limite de 7 requêtes/seconde par IP ; les
  entreprises non diffusibles en sont absentes, ce qui explique une partie des fiches sans
  rattachement.
- Code : **GPL-3.0** (voir `LICENSE`). [MiniSearch](https://github.com/lucaong/minisearch) est
  vendorisé sous licence MIT dans `web/vendor/minisearch/`.
