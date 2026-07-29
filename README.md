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

## Ce que contiennent réellement les listes

Constaté au premier passage sur les données réelles (28 juillet 2026) — et ce n'est pas
intuitif :

| Liste | Lignes | Ce qu'il faut savoir |
|---|---|---|
| Publications de presse | 26 669 | **21 653 (81 %) au statut « Non Inscrit »**. Le fichier recense les titres connus de la CPPAP, pas seulement les titres inscrits. Seule liste à publier un SIRET, et seulement pour 15 % des lignes. |
| Services de presse en ligne | 1 242 | Tous reconnus par construction. Pas de colonne SIRET, ni de date d'expiration. **Les 1 242 figurent aussi dans la liste des publications**, sous le même n° d'inscription écrit autrement. |
| Agences de presse agréées | 180 | Toutes agréées par construction. Ni SIRET, ni n° CPPAP, ni département. |

Soit **26 849 fiches publiées** : les 28 091 lignes moins les 1 242 inscriptions décrites par
deux listes, réunies en une fiche (voir la section suivante).

> **Un numéro CPPAP affiché ne vaut pas agrément en cours.** L'interface porte donc un badge
> `NON INSCRIT` dès la liste de résultats, un avertissement en tête de fiche, la date
> d'expiration de l'inscription, et un filtre « inscrits ou reconnus uniquement ».

Le champ `qualification` n'a pas le même sens partout : `IPG` dans la liste des services en
ligne, mais surtout des régimes postaux et fiscaux (`DISPOSITIF_FISCAL_39_BIS_A`,
`CIBLAGE_POSTAL_D_19_2`) dans celle des publications. Seules les valeurs désignant
explicitement l'information politique et générale alimentent le marqueur IPG. Les deux
vocabulaires sont ramenés à un libellé lisible par `config/labels.json`.

## Anatomie du n° CPPAP, et pourquoi il s'écrit de deux façons

Les deux listes ne publient pas le même morceau du numéro. C'est la principale chausse-trappe
de ces données : **« 1026 Y 90833 » et « 2590833 » désignent le même agrément.**

```
1026        Y            90833
MMAA        lettre       n° d'inscription (permanent)
expiration  rubrique     ← seul élément publié par la liste des publications
```

| | Services de presse en ligne | Publications de presse |
|---|---|---|
| Origine | ressource data.gouv (`static.data.gouv.fr`) | export de base du ministère de la Culture (`ministere-culture.s3…`) |
| En-têtes | intitulés humains | `snake_case` |
| Écriture du numéro | forme complète, `0330 W 95411` | n° d'inscription préfixé, `2595411` |
| Expiration | **aucune colonne** — lisible dans le numéro | colonne `date_expiration_inscription` |

Ce n'est ni une cellule mal formatée ni un zéro initial perdu : le fichier source écrit la
valeur entre guillemets, donc en texte. Le préfixe est **constant sur les 26 669 lignes** de la
liste des publications, il ne porte donc aucune information propre à la ligne.

Mesuré sur les fichiers réels : les 1 242 n° d'inscription de la liste des services de presse
en ligne figurent **tous** dans celle des publications, et le rapprochement est corroboré par
quatre champs indépendants — éditeur identique 1 241/1 241, forme juridique 1 241/1 241, titre
1 240/1 241, et **expiration déduite du numéro égale à la colonne publiée 1 241/1 241, sans une
divergence**.

`scripts/lib/cppap.py` porte cette lecture, et seul lui. Trois conséquences dans le site :

- **une fiche par inscription**, alimentée par les deux listes (26 849 fiches au lieu de
  28 091). La liste des services en ligne apporte la forme complète du numéro, l'URL et la
  qualification ; celle des publications apporte le SIRET, le statut et les dates. Les deux
  sources restent citées et archivées séparément sur la fiche ;
- **la recherche accepte les trois écritures** — `1026 Y 90833`, `2590833`, `90833` ;
- **l'expiration est déduite du numéro** quand la liste ne la publie pas. Sans cela, 126
  reconnaissances déjà expirées s'affichaient comme valides.

La lettre de rubrique n'est **pas** interprétée. Sur les 1 241 titres communs, elle est
pourtant en correspondance stricte avec la qualification (`W` aucune, `X` 39 bis B, `Y` IPG,
`Z` 39 bis A) — mais cette correspondance est observée, non documentée par la CPPAP : elle
n'alimente aucune donnée dérivée.

### Garde-fous du rapprochement

La clé est le couple **n° d'inscription + éditeur**, jamais le seul numéro : un numéro peut
être **réattribué**. Le n° 90135 est ainsi porté par trois titres, dont un dont l'inscription a
expiré en 2015. Deux fiches ne sont réunies que si aucune liste n'en fournit deux, et que les
dates d'expiration *publiées* concordent — une date déduite du numéro ne peut pas s'y opposer,
elle n'est qu'une lecture de ce même numéro. Un numéro ambigu donne des fiches distinctes et
des identifiants explicites (`cppap-90135-nextinteractive`).

Si le préfixe de la liste des publications cessait d'être constant, le n° d'inscription ne
serait plus une clé sûre : **le rapprochement est alors désactivé pour cette source**, avec un
avertissement. Les écritures rencontrées sont comptées par source et publiées dans
`meta.json` (`schema_reports.<source>.cppap_prefixes`), pour qu'une dérive de format en amont
soit visible dans les journaux du workflow plutôt que silencieuse.

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

Résultat mesuré au 29 juillet 2026 sur les 26 849 fiches, jointures exactes seules (le
rapprochement par nom reste à exécuter) :

| | Fiches | Part |
|---|---|---|
| SIRET publié par la CPPAP | 4 014 | 15,0 % |
| SIRET hérité d'une autre liste du même éditeur | 2 402 | 8,9 % |
| SIRET publié mais entreprise absente de l'API | 52 | 0,2 % |
| En attente du rapprochement par le nom | 20 381 | 75,9 % |

Soit **6 416 fiches (24 %) rattachées de façon exacte**, sans aucune heuristique. Sur
2 444 entreprises interrogées par SIREN, 2 404 ont été retrouvées (98 %) — les 40 autres sont
vraisemblablement non diffusibles ou radiées.

> Ces parts ont **baissé** en apparence lors de la réunion des fiches d'une même inscription :
> un service de presse en ligne comptait auparavant pour deux fiches rattachées, une par liste.
> Sur les 1 242 inscriptions concernées, le rattachement est en réalité meilleur — 1 186
> établies contre 1 098 avant, et surtout 1 173 **jointures exactes** là où c'étaient des
> déductions. Le n° d'inscription confirme d'ailleurs les 1 084 déductions antérieures sans un
> seul désaccord.

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
               │  scripts/build_site.py    index compact + 64 lots de détail
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
| `scripts/lib/cppap.py` | **Lecture du n° CPPAP** : ses deux écritures, son n° d'inscription, son expiration |
| `scripts/lib/` | Normalisation de texte, client HTTP à débit limité, lecture CSV/XLSX |
| `web/` | Site statique, JS vanilla, MiniSearch vendorisé — aucun CDN |
| `worker/` | Relais CORS facultatif vers l'API Recherche d'entreprises |
| `scripts/build_preview.py` | Assemble le site en un fichier HTML unique, consultable hors ligne |

Stack : **Python 3.11, bibliothèque standard uniquement** (`urllib`, `csv`, `unicodedata`,
`difflib`, `zipfile`). Aucune dépendance de production, ni côté scripts, ni côté navigateur.

Le site charge un index compact au démarrage (~840 Ko compressés pour 26 849 fiches) puis va
chercher les fiches complètes dans l'un des 64 lots à l'ouverture d'une carte — de l'ordre de
**60 Ko compressés par lot**. Le lot est déduit d'une empreinte de l'identifiant, donc **stable
entre deux publications** : le cache du navigateur survit aux mises à jour de données.

Les fiches publiées ne transportent ni champ vide, ni valeur reconstituable depuis
`meta.json` (libellés de type et de département, liens de source) : `web/app.js` les
recompose. Sans cette diète, un quart du poids téléchargé était de la répétition.

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
python scripts/ingest.py                        # télécharge et archive si le contenu a changé
python scripts/match_sirene.py --skip-fuzzy     # jointures exactes par SIRET : quelques minutes
python scripts/match_sirene.py                  # rapprochement par nom : ~100 min, reprenable
python scripts/build_site.py                    # génère site/
```

L'appariement complet demande environ 100 minutes de requêtes (plafond de l'API : 7 req/s ;
on reste à 4). Le cache est écrit au fil de l'eau : une exécution interrompue laisse un
progrès réutilisable, et la suivante reprend là où elle s'est arrêtée. Le workflow de
synchronisation borne l'étape dans le temps et commite ce progrès partiel quoi qu'il arrive.

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
