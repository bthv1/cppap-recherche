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
      // Une fiche réunissant plusieurs listes CPPAP doit ressortir des filtres de chacune.
      // `types` n'est renseigné que dans ce cas, `type` suffisant sinon.
      record.typeList = record.types ? record.types.split('|') : [record.type];
      // Clés compactes pour retrouver un numéro saisi sans séparateurs, sous n'importe
      // laquelle de ses écritures : « 1026Y90833 » relevé dans un ours, « 2590833 » copié
      // depuis la liste des publications, ou le seul n° d'inscription « 90833 ».
      record.cppapKeys = [record.cppap, ...(record.cppap_alt ? record.cppap_alt.split('|') : [])]
        .map((value) => fold(value).replace(/[^0-9a-z]/g, ''))
        .filter(Boolean);
      return record;
    });

    this.byId = new Map(this.records.map((r) => [r.id, r]));

    this.alphabetical = [...this.records].sort((a, b) => collator.compare(a.nom, b.nom));

    this.mini = new MiniSearch({
      idField: 'id',
      fields: ['nom', 'editeur', 'cppap', 'cppap_alt'],
      storeFields: [],
      tokenize,
      processTerm: (term) => fold(term) || null,
      searchOptions: {
        prefix: true,
        // Pas de tolérance aux fautes sur les termes courts : sur un sigle de trois lettres,
        // une distance de 1 rapproche « AFP » de « ALP » et noie le résultat cherché.
        // Aucune non plus sur un nombre : un chiffre qui diffère dans un n° CPPAP désigne un
        // autre agrément, pas une faute de frappe à pardonner — « 2590066 » ramenait
        // trente-quatre fiches, dont trente-trois sans rapport.
        fuzzy: (term) => (/^\d+$/.test(term) ? 0 : term.length > 4 ? 0.2 : 0),
        combineWith: 'AND',
        boost: { nom: 4, cppap: 2, cppap_alt: 2, editeur: 1.5 },
      },
    });
    this.mini.addAll(this.records);
  }

  /**
   * Retrouve une fiche par son identifiant, y compris un identifiant devenu obsolète.
   *
   * Avant la réunion des listes, chaque liste avait sa propre fiche pour une même
   * inscription, identifiée par l'écriture du numéro qui lui était propre :
   * `spel-1026-y-90833` et `publication-2590833`. Les liens déjà partagés doivent continuer
   * de mener à la fiche, désormais unique.
   */
  resolve(id) {
    const direct = this.byId.get(id);
    if (direct) return direct;

    // « spel-1229-y-90066 » → « 1229y90066 » : le préfixe de liste tombe, le numéro reste.
    const key = fold(id).replace(/^[a-z]+-/, '').replace(/[^0-9a-z]/g, '');
    if (key.length < 4 || !/\d/.test(key)) return null;
    return this.records.find((r) => r.cppapKeys.some((k) => k.includes(key))) ?? null;
  }

  static matches(record, filters) {
    if (filters.type && !record.typeList.includes(filters.type)) return false;
    if (filters.dept && record.dept !== filters.dept) return false;
    if (filters.qual && record.qual !== filters.qual) return false;
    if (filters.doubt && !DOUBTFUL.has(record.confidence)) return false;
    // Actif par défaut : quatre titres sur cinq de la liste des publications ont vu leur
    // inscription expirer, et les afficher d'emblée laisserait croire à un agrément en cours.
    // Le décompte total reste annoncé dans le bandeau, et la case se décoche.
    if (filters.inscrit && record.inscrit !== 1) return false;
    return true;
  }

  /**
   * @param {string} text    requête libre ; vide = parcours alphabétique
   * @param {object} filters {type, dept, qual, inscrit, doubt}
   * @param {number} limit   nombre de résultats retournés
   */
  query(text, filters = {}, limit = 60) {
    const filtered = this.candidates(text).filter((r) => MediaIndex.matches(r, filters));
    return { total: filtered.length, items: filtered.slice(0, limit) };
  }

  /**
   * Décompte des valeurs d'une facette parmi les résultats courants.
   *
   * La facette ignore **son propre** filtre : sinon, choisir « IPG » afficherait « IPG (456) »
   * et zéro partout ailleurs, ce qui rendrait le menu inutilisable. Recompté à chaque rendu,
   * pour qu'un menu n'annonce jamais un nombre que la liste ne montre pas.
   */
  facet(text, filters, field) {
    const counts = new Map();
    const others = { ...filters, [field]: '' };
    for (const record of this.candidates(text)) {
      if (!MediaIndex.matches(record, others)) continue;
      const value = record[field];
      if (value) counts.set(value, (counts.get(value) ?? 0) + 1);
    }
    return counts;
  }

  /** Fiches retenues par la requête, avant application des filtres. */
  candidates(text) {
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
      // peut pas rapprocher : « 0620W91234 » face à « 0620 W 91234 ». La recherche par
      // sous-chaîne fait aussi tout le travail pour le n° d'inscription seul, contenu dans
      // chacune des écritures du numéro.
      const digits = fold(trimmed).replace(/[^0-9a-z]/g, '');
      if (digits.length >= 3) {
        const seen = new Set(candidates.map((r) => r.id));
        for (const record of this.records) {
          if (!seen.has(record.id) && record.cppapKeys.some((key) => key.includes(digits))) {
            candidates.push(record);
          }
        }
      }
    }

    return candidates;
  }
}
