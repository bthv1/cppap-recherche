# MiniSearch (vendorisé)

Moteur de recherche plein texte côté navigateur, utilisé par `web/search.js`.

| | |
|---|---|
| Version | **7.2.0** |
| Licence | MIT — voir `LICENSE.txt` (compatible avec la GPL-3.0 du projet) |
| Source | `https://registry.npmjs.org/minisearch/-/minisearch-7.2.0.tgz`, fichier `dist/es/index.js` |
| sha256 amont | `0393b3ba253b809d5e55707c7b0875ef9b518a296a006c6739b28876e154edb3` |
| sha256 vendorisé | `be655f42574cbfc32f7143d8f9ed20d287a98f70d76fe16e7d422918174b5c04` |

**Seule modification apportée** : suppression du commentaire final
`//# sourceMappingURL=index.js.map`, la source map n'étant pas embarquée. Le code lui-même
est identique à l'amont — recalculez l'empreinte ci-dessus pour le vérifier.

## Pourquoi vendoriser plutôt qu'utiliser un CDN

Le site est publié sur GitHub Pages sans étape de compilation et ne doit dépendre d'aucun
tiers à l'exécution : pas de requête sortante vers un CDN, pas de rupture si ce CDN change
ou disparaît, et un archivage du dépôt qui reste fonctionnel tel quel.

## Mise à jour

```sh
curl -sO https://registry.npmjs.org/minisearch/-/minisearch-<version>.tgz
tar xzf minisearch-<version>.tgz
cp package/dist/es/index.js web/vendor/minisearch/minisearch.js
cp package/LICENSE.txt      web/vendor/minisearch/LICENSE.txt
# retirer la dernière ligne //# sourceMappingURL=..., puis mettre ce tableau à jour
```
