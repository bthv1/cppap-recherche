/**
 * Index de recherche côté navigateur.
 *
 * L'index compact (`data/search.json`) arrive au format colonnes — un tableau de champs et
 * des lignes de valeurs — ce qui pèse nettement moins qu'autant d'objets JSON répétant
 * leurs clés. On le réhydrate ici, puis on l'indexe avec MiniSearch.
 */

import MiniSearch from './vendor/minisearch/minisearch.js';

/** Replie accents et ligatures, et passe en minuscules : « Arrêt » et « arret » se valent. */
export function fold(value) {
  return String(value ?? '')
    .replace(/œ/g, 'oe').replace(/Œ/g, 'OE')
    .replace(/æ/g, 'ae').replace(/Æ/g, 'AE')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
}

/** Sépare sur tout ce qui n'est ni lettre ni chiffre (tirets, apostrophes, ponctuation). */
const SPLIT = /[^0-9A-Za-z\u00C0-\u024F]+/;

export function tokenize(text) {
  return String(text ?? '').split(SPLIT).filter(Boolean);
}

const collator = new Intl.Collator('fr', { sensitivity: 'base', numeric: true });

/** Niveaux de confiance considérés comme « à vérifier » par le filtre dédié. */
const DOUBTFUL = new Set(['probable', 'incertain', 'aucun']);

export class MediaIndex {
  constructor(payload) {
    const { fields, rows } = payload;

    this.records = rows.map((row) => {
      const record = {};
      fields.forEach((field, i) => { record[field] = row[i]; });
      // Clé compacte pour retrouver « 0620W91234 » saisi sans séparateurs.
      record.cppapKey = fold(record.cppap).replace(/[^0-9a-z]/g, '');
      return record;
    });

    this.byId = new Map(this.records.map((r) => [r.id, r]));

    this.alphabetical = [...this.records].sort((a, b) => collator.compare(a.nom, b.nom));

    this.mini = new MiniSearch({
      idField: 'id',
      fields: ['nom', 'editeur', 'cppap'],
      storeFields: [],
      tokenize,
      processTerm: (term) => fold(term) || null,
      searchOptions: {
        prefix: true,
        // Pas de tolérance aux fautes sur les termes courts : sur un sigle de trois lettres,
        // une distance de 1 rapproche « AFP » de « ALP » et noie le résultat cherché.
        fuzzy: (term) => (term.length > 4 ? 0.2 : 0),
        combineWith: 'AND',
        boost: { nom: 4, cppap: 2, editeur: 1.5 },
      },
    });
    this.mini.addAll(this.records);
  }

  static matches(record, filters) {
    if (filters.type && record.type !== filters.type) return false;
    if (filters.dept && record.dept !== filters.dept) return false;
    if (filters.ipg && record.ipg !== 1) return false;
    if (filters.doubt && !DOUBTFUL.has(record.confidence)) return false;
    // Quatre publications sur cinq ne sont pas inscrites : ce filtre laisse écarter le bruit
    // sans jamais masquer par défaut ce que la source contient.
    if (filters.inscrit && record.inscrit !== 1) return false;
    return true;
  }

  /**
   * @param {string} text    requête libre ; vide = parcours alphabétique
   * @param {object} filters {type, dept, ipg, doubt}
   * @param {number} limit   nombre de résultats retournés
   */
  query(text, filters = {}, limit = 60) {
    const trimmed = String(text ?? '').trim();
    let candidates;

    if (!trimmed) {
      candidates = this.alphabetical;
    } else {
      candidates = this.mini
        .search(trimmed)
        .map((hit) => this.byId.get(hit.id))
        .filter(Boolean);

      // Complément pour les n° CPPAP saisis sans séparateurs, que la tokenisation ne
      // peut pas rapprocher : « 0620W91234 » face à « 0620 W 91234 ».
      const digits = fold(trimmed).replace(/[^0-9a-z]/g, '');
      if (digits.length >= 3) {
        const seen = new Set(candidates.map((r) => r.id));
        for (const record of this.records) {
          if (!seen.has(record.id) && record.cppapKey.includes(digits)) {
            candidates.push(record);
          }
        }
      }
    }

    const filtered = candidates.filter((record) => MediaIndex.matches(record, filters));
    return { total: filtered.length, items: filtered.slice(0, limit) };
  }
}
