# Fixtures de test

**Ces données sont synthétiques.** Elles imitent la *forme* des sources réelles pour que la
chaîne complète soit testable hors ligne, mais leur contenu est inventé :

- `published/*.csv` — imitent les fichiers publiés sur data.gouv.fr : séparateur point-virgule,
  en-têtes accentués, et pour `publications.csv` la **variante historique d'en-têtes**
  (colonne `IPG` au lieu de `Qualification`, libellé de département rallongé) afin d'exercer
  les deux passes d'appariement de colonnes de `scripts/normalize.py`.
- `api/*.json` — imitent les réponses de l'API Recherche d'entreprises. **Les numéros SIREN
  qu'elles contiennent sont fictifs** et ne doivent jamais être repris ailleurs.
- `sirene_cache.json` — cache d'appariement pré-rempli, pour construire un site de
  démonstration sans accès réseau (`scripts/build_site.py --from-fixtures`).

Les en-têtes réels des fichiers CPPAP n'ont pas pu être observés au moment de l'écriture :
la carte d'alias de `config/sources.json` est donc à confirmer lors du premier run réel de
`.github/workflows/sync.yml`, qui journalise les en-têtes rencontrés.
