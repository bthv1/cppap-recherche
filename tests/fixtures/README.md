# Fixtures de test

**Ces données sont synthétiques.** Elles imitent la *forme* des sources réelles pour que la
chaîne complète soit testable hors ligne, mais leur contenu est inventé :

- `published/*.csv` — imitent les fichiers publiés sur data.gouv.fr : séparateur point-virgule,
  en-têtes accentués. Ils reproduisent volontairement l'**hétérogénéité entre listes** :
  - `publications.csv` porte une colonne **SIRET**, ainsi que la variante historique d'en-têtes
    (colonne `IPG` au lieu de `Qualification`, libellé de département rallongé) — de quoi
    exercer les deux passes d'appariement de colonnes de `scripts/normalize.py` ;
  - `spel.csv` et `agences.csv` n'en portent **pas**, ce qui exerce le repli sur le
    rapprochement par le nom.
- `api/*.json` — imitent les réponses de l'API Recherche d'entreprises. **Les numéros SIREN
  qu'elles contiennent sont fictifs** et ne doivent jamais être repris ailleurs.
- `sirene_cache.json` — cache d'appariement pré-rempli, pour construire un site de
  démonstration sans accès réseau (`scripts/build_site.py --from-fixtures`).

## Cas de figure couverts

Les fixtures sont calibrées pour que chaque chemin de rattachement soit représenté au moins une
fois — voir `scripts/lib/resolution.py` pour l'ordre de priorité :

| Fiche | Niveau obtenu | Ce qu'elle exerce |
|---|---|---|
| Le Monde | `siret` | SIRET publié, jointure exacte |
| Le Canard Enchaîné | `siret` | SIRET écrit avec des espaces de groupage |
| La Hulotte | `siret` | SIRET amputé de son zéro initial par un tableur |
| Numerama (SPEL) | `siret_propage` | l'éditeur HUMANOID déclare son SIRET dans `publications.csv` |
| Journal introuvable en SIRENE | `siret_absent` | SIRET officiel, entreprise absente de l'API |
| Mediapart, AFP… | `certain` | rapprochement par le nom, sans ambiguïté |
| Rue89 Lyon, Le Poulpe | `probable` | rapprochement par le nom, un signal manque |
| Revue sans SIRET renseigné | `aucun` | ni SIRET, ni correspondance de nom |
| Le Monde / Le Monde diplomatique | — | même n° CPPAP répété : unicité des identifiants |

## Limite connue

Les en-têtes réels des fichiers CPPAP n'ont pas pu être observés au moment de l'écriture : la
carte d'alias de `config/sources.json` est donc à confirmer lors du premier run réel de
`.github/workflows/sync.yml`, qui journalise les en-têtes rencontrés. Les intitulés utilisés ici
sont plausibles, pas vérifiés.
