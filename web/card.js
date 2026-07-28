/**
 * Rendu de la carte détaillée d'un média.
 *
 * Trois volets : l'agrément CPPAP, l'entreprise telle que déclarée à la CPPAP, et la fiche
 * SIRENE de cette entreprise. Ce dernier volet affiche toujours son niveau de confiance :
 * le rattachement est reconstitué par rapprochement de noms, il ne doit jamais se lire
 * comme une donnée d'origine.
 */

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Toutes les valeurs viennent de fichiers de données : elles sont échappées sans exception. */
export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

function has(value) {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) return value.length > 0;
  return String(value).trim() !== '';
}

/** Une ligne de définition, omise si la valeur est vide — pas de « — » décoratif. */
function field(term, value, { raw = false } = {}) {
  if (!has(value)) return '';
  return `<dt>${esc(term)}</dt><dd>${raw ? value : esc(value)}</dd>`;
}

function codeWithLabel(code, table) {
  if (!has(code)) return '';
  const label = table?.[code];
  return label ? `${code} — ${label}` : String(code);
}

/** Les dates SIRENE sont en ISO ; celles de la CPPAP peuvent être dans un autre format. */
function frenchDate(value) {
  const text = String(value ?? '').trim();
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(text);
  if (!iso) return text;
  const [, y, m, d] = iso;
  return `${d}/${m}/${y}`;
}

function badge(text, extra = '') {
  return `<span class="badge ${extra}">${esc(text)}</span>`;
}

/** Groupage lisible d'un SIRET : 3-3-3-5, comme l'écrit l'INSEE. */
function formatSiret(siret) {
  const digits = String(siret ?? '').replace(/\D/g, '');
  if (digits.length !== 14) return '';
  return `${digits.slice(0, 3)} ${digits.slice(3, 6)} ${digits.slice(6, 9)} ${digits.slice(9)}`;
}

/** Groupage lisible d'un SIREN : 3-3-3. */
function formatSiren(siren) {
  const digits = String(siren ?? '').replace(/\D/g, '');
  if (digits.length !== 9) return String(siren ?? '');
  return `${digits.slice(0, 3)} ${digits.slice(3, 6)} ${digits.slice(6)}`;
}

function link(href, text, { external = true } = {}) {
  const rel = external ? ' rel="noopener noreferrer"' : '';
  const target = external ? ' target="_blank"' : '';
  return `<a href="${esc(href)}"${target}${rel}>${esc(text)}</a>`;
}

/**
 * Adresse du siège, sur plusieurs lignes.
 *
 * L'API expose à la fois les composants (`numero_voie`, `type_voie`, `libelle_voie`…) et un
 * champ `adresse` qui est **l'adresse complète déjà agrégée**, code postal et commune inclus.
 * Les deux ne se combinent donc pas : composer à partir des composants quand ils existent,
 * sinon reprendre `adresse` telle quelle — y ajouter la ligne commune la dupliquerait.
 */
function addressBlock(siege) {
  const street = [
    siege.numero_voie,
    siege.indice_repetition,
    siege.type_voie,
    siege.libelle_voie,
  ].filter(has).join(' ');

  const foreign = [siege.libelle_commune_etranger, siege.libelle_pays_etranger].filter(has);
  const lines = [];

  if (street) {
    if (has(siege.complement_adresse)) lines.push(siege.complement_adresse);
    lines.push(street);
    if (has(siege.libelle_cedex)) {
      lines.push(`CEDEX ${siege.cedex ?? ''} ${siege.libelle_cedex}`.replace(/\s+/g, ' ').trim());
    } else {
      const city = [siege.code_postal, siege.libelle_commune].filter(has).join(' ');
      if (city) lines.push(city);
    }
    lines.push(...foreign);
  } else if (has(siege.adresse)) {
    lines.push(siege.adresse);
  } else {
    const city = [siege.code_postal, siege.libelle_commune].filter(has).join(' ');
    if (city) lines.push(city);
    lines.push(...foreign);
  }

  if (!lines.length) return '';
  return `<address class="address">${lines.map(esc).join('<br>')}</address>`;
}

function dirigeantLine(person) {
  if (has(person.denomination)) {
    const parts = [person.denomination];
    if (has(person.siren)) parts.push(`SIREN ${person.siren}`);
    if (has(person.qualite)) parts.push(person.qualite);
    return parts.join(' — ');
  }
  const name = [person.prenoms, person.nom].filter(has).join(' ');
  const parts = [name || 'Nom non diffusé'];
  if (has(person.qualite)) parts.push(person.qualite);
  if (has(person.annee_de_naissance)) parts.push(`né(e) en ${person.annee_de_naissance}`);
  return parts.join(' — ');
}

// --------------------------------------------------------------------------------------
// Volets
// --------------------------------------------------------------------------------------

function cppapSection(detail, labels) {
  const rows = [
    field('N° CPPAP', detail.cppap),
    field('Type', detail.type_label),
    field('Qualification', detail.qualification),
    field('Périodicité', detail.periodicite),
    field('Date de décision', frenchDate(detail.date_decision)),
    detail.url ? field('Site', link(detail.url, detail.url.replace(/^https?:\/\//, '')), { raw: true }) : '',
  ].join('');
  void labels;
  return `<section class="card-section">
    <h3>Agrément CPPAP</h3>
    <dl class="fields">${rows}</dl>
  </section>`;
}

function editeurSection(detail) {
  const departement = has(detail.departement)
    ? [detail.departement, detail.departement_label].filter(has).join(' — ')
    : detail.departement_source;

  const rows = [
    field('Raison sociale', detail.editeur),
    // Le SIRET, quand la liste le publie, est la donnée qui rend le rattachement exact :
    // on l'affiche du côté source, là où il a été déclaré.
    field('SIRET déclaré', formatSiret(detail.siret) || detail.siret_source),
    field('Forme juridique', detail.forme_juridique),
    field('Département du siège', departement),
    field('Commune', detail.commune),
  ].join('');

  if (!rows) return '';
  return `<section class="card-section">
    <h3>Entreprise éditrice, telle que déclarée à la CPPAP</h3>
    <dl class="fields">${rows}</dl>
  </section>`;
}

function confidenceNote(sirene, labels) {
  const level = sirene?.confidence ?? 'aucun';
  const info = labels.confidence?.[level] ?? { label: level, tone: 'none', hint: '' };
  const score = typeof sirene?.score === 'number' ? ` (score ${sirene.score.toFixed(2)})` : '';
  const note = has(sirene?.note) ? ` ${sirene.note}` : '';
  return {
    info,
    html: `<p class="note tone-${esc(info.tone)}"><strong>${esc(info.label)}</strong>${esc(score)} —
      ${esc(info.hint)}${esc(note)}</p>`,
  };
}

function sireneSection(detail, meta) {
  const labels = meta.labels ?? {};
  const sirene = detail.sirene;
  const { info, html: note } = confidenceNote(sirene, labels);

  if (!sirene || !sirene.entreprise) {
    const searchUrl = `https://annuaire-entreprises.data.gouv.fr/rechercher?terme=${
      encodeURIComponent(detail.editeur || detail.nom)}`;

    // Un SIRET publié dont l'entreprise est absente de l'API n'est PAS un échec
    // d'appariement : l'identifiant reste officiel. Le confondre avec « aucune
    // correspondance » ferait douter d'une donnée qui, elle, est fiable.
    const known = sirene?.siren
      ? `<dl class="fields">
           ${field('SIREN', formatSiren(sirene.siren))}
           ${field('SIRET déclaré par la CPPAP', formatSiret(sirene.siret_declare))}
         </dl>
         <p>L'identifiant vient du fichier officiel, mais l'API Recherche d'entreprises ne
         renvoie pas cette entreprise : elle est vraisemblablement non diffusible, ou radiée.</p>
         <p class="card-links">${link(
           `${meta.annuaire_base}/${sirene.siren}`, 'Tenter la fiche Annuaire des entreprises',
         )}</p>`
      : `<p>Aucune entreprise n'a pu être rattachée à cette raison sociale.</p>
         <p class="card-links">${link(
           searchUrl,
           `Chercher « ${detail.editeur || detail.nom} » dans l'Annuaire des entreprises`,
         )}</p>`;

    return `<section class="card-section">
      <h3>Siège social et entreprise (SIRENE)</h3>
      ${note}
      ${known}
      ${candidatesBlock(sirene)}
    </section>`;
  }

  const e = sirene.entreprise;
  const siege = e.siege ?? {};
  const complements = Object.entries(e.complements ?? {})
    .filter(([, v]) => v === true)
    .map(([k]) => labels.complements?.[k] ?? k);

  const coords = has(siege.latitude) && has(siege.longitude)
    ? `${siege.latitude}, ${siege.longitude}`
    : '';

  const identity = [
    field('SIREN', formatSiren(e.siren)),
    field('Dénomination SIRENE', e.nom_complet ?? e.nom_raison_sociale),
    field('Sigle', e.sigle),
    field('Nature juridique', codeWithLabel(e.nature_juridique, labels.nature_juridique)),
    field('Activité principale (NAF)', codeWithLabel(e.activite_principale, labels.naf)),
    field('Date de création', frenchDate(e.date_creation)),
    field('Date de fermeture', frenchDate(e.date_fermeture)),
    field('État', codeWithLabel(e.etat_administratif, labels.etat_administratif)),
    field('Catégorie', codeWithLabel(e.categorie_entreprise, labels.categorie_entreprise)),
    field('Effectif salarié', codeWithLabel(e.tranche_effectif_salarie, labels.tranche_effectif)),
    field('Établissements', has(e.nombre_etablissements)
      ? `${e.nombre_etablissements} au total, ${e.nombre_etablissements_ouverts ?? '?'} ouvert(s)`
      : ''),
    field('N° TVA', Array.isArray(e.tva) ? e.tva.join(', ') : e.tva),
    field('Mise à jour SIRENE', frenchDate(e.date_mise_a_jour_insee ?? e.date_mise_a_jour)),
    complements.length ? field('Caractéristiques', complements.join(' · ')) : '',
  ].join('');

  const siegeRows = [
    field('SIRET du siège', formatSiret(siege.siret) || siege.siret),
    // Le SIRET déclaré à la CPPAP désigne un établissement, pas forcément le siège :
    // le signaler évite de croire à une incohérence entre les deux numéros.
    sirene.siret_declare && sirene.siret_est_siege === false
      ? field(
          'Établissement déclaré',
          `${formatSiret(sirene.siret_declare)} — l'établissement déclaré à la CPPAP `
            + "n'est pas le siège social",
        )
      : '',
    field('Enseigne', Array.isArray(siege.liste_enseignes)
      ? siege.liste_enseignes.join(', ') : siege.liste_enseignes),
    field('Nom commercial', siege.nom_commercial),
    field('Activité du siège (NAF)', codeWithLabel(siege.activite_principale, labels.naf)),
    field('État du siège', codeWithLabel(siege.etat_administratif, labels.etat_administratif)),
    field('Coordonnées GPS', coords),
  ].join('');

  const dirigeants = (e.dirigeants ?? []).filter((d) => d && Object.keys(d).length);
  const dirigeantsBlock = dirigeants.length
    ? `<h3>Dirigeants</h3>
       <ul class="dirigeants">${dirigeants.map((d) => `<li>${esc(dirigeantLine(d))}</li>`).join('')}</ul>
       ${e.dirigeants_total > dirigeants.length
         ? `<p class="results-more">${e.dirigeants_total} dirigeants au total ; ${dirigeants.length} affichés.</p>`
         : ''}`
    : '';

  const refresh = meta.sirene_proxy
    ? `<button type="button" class="refresh" data-siren="${esc(e.siren)}">Rafraîchir depuis SIRENE</button>
       <p class="refresh-state" data-role="refresh-state"></p>`
    : `<p class="refresh-state">Instantané SIRENE archivé le ${esc(frenchDate(sirene.resolved_at))}.</p>`;

  return `<section class="card-section" data-role="sirene">
    <h3>Siège social et entreprise (SIRENE)</h3>
    ${info.tone === 'ok' ? '' : note}
    <dl class="fields">${identity}</dl>
    <h3>Siège social</h3>
    ${addressBlock(siege)}
    ${siegeRows ? `<dl class="fields">${siegeRows}</dl>` : ''}
    ${dirigeantsBlock}
    ${info.tone === 'ok' ? note : ''}
    ${candidatesBlock(sirene)}
    ${refresh}
  </section>`;
}

function candidatesBlock(sirene) {
  const candidates = sirene?.candidates ?? [];
  const alternatives = candidates.filter((c) => c.siren && c.siren !== sirene?.siren);
  if (!alternatives.length) return '';
  return `<details class="raw">
    <summary>Autres entreprises envisagées (${alternatives.length})</summary>
    <ul class="candidates">${alternatives.map((c) => `<li>
      ${link(`https://annuaire-entreprises.data.gouv.fr/entreprise/${c.siren}`, c.nom ?? c.siren)}
      <span class="cand-score">— SIREN ${esc(c.siren)}${
        c.departement ? `, dép. ${esc(c.departement)}` : ''
      }, score ${esc(Number(c.score ?? 0).toFixed(2))}</span>
    </li>`).join('')}</ul>
  </details>`;
}

function extraSection(detail) {
  const entries = Object.entries(detail.extra ?? {});
  if (!entries.length) return '';
  return `<section class="card-section">
    <details class="raw">
      <summary>Colonnes source non normalisées (${entries.length})</summary>
      <dl class="fields">${entries.map(([k, v]) => field(k, v)).join('')}</dl>
    </details>
  </section>`;
}

function sourcesSection(detail, meta) {
  const repo = meta.repository ?? '';
  const links = [];
  if (detail.source_page) links.push(link(detail.source_page, 'Jeu de données sur data.gouv.fr'));
  if (detail.source_snapshot && repo) {
    links.push(link(
      `https://github.com/${repo}/blob/HEAD/${detail.source_snapshot}`,
      `Version archivée${detail.source_version ? ` du ${frenchDate(detail.source_version)}` : ''}`,
    ));
  }
  const siren = detail.sirene?.siren;
  if (siren) links.push(link(`${meta.annuaire_base}/${siren}`, 'Fiche Annuaire des entreprises'));

  if (!links.length) return '';
  return `<section class="card-section">
    <h3>Sources</h3>
    <p class="card-links">${links.join('')}</p>
  </section>`;
}

// --------------------------------------------------------------------------------------
// Carte complète
// --------------------------------------------------------------------------------------

export function renderCard(detail, meta) {
  const labels = meta.labels ?? {};
  const level = detail.sirene?.confidence ?? 'aucun';
  const tone = labels.confidence?.[level]?.tone ?? 'none';
  const confidenceLabel = labels.confidence?.[level]?.label ?? level;

  const tags = [
    badge(detail.type_label ?? detail.type),
    detail.ipg ? badge('IPG', 'ipg') : '',
    badge(confidenceLabel, `tone-${tone}`),
  ].join('');

  return `
    <div class="card-head">
      <h2>${esc(detail.nom)}</h2>
      <div class="card-tags">${tags}</div>
    </div>
    ${cppapSection(detail, labels)}
    ${editeurSection(detail)}
    ${sireneSection(detail, meta)}
    ${extraSection(detail)}
    ${sourcesSection(detail, meta)}
  `;
}

/**
 * Branche le bouton de rafraîchissement, quand un Worker SIRENE est configuré.
 * Un échec est signalé sans casser la carte : l'instantané archivé reste affiché.
 */
export function attachCardBehaviour(root, detail, meta) {
  const button = root.querySelector('.refresh');
  if (!button || !meta.sirene_proxy) return;

  const state = root.querySelector('[data-role="refresh-state"]');
  button.addEventListener('click', async () => {
    button.disabled = true;
    state.textContent = 'Interrogation de SIRENE…';
    try {
      const base = String(meta.sirene_proxy).replace(/\/+$/, '');
      const response = await fetch(`${base}/api/entreprise/${encodeURIComponent(button.dataset.siren)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const fresh = payload?.results?.[0];
      if (!fresh) throw new Error('entreprise absente de la réponse');

      const refreshed = {
        ...detail,
        sirene: { ...detail.sirene, entreprise: { ...detail.sirene.entreprise, ...fresh } },
      };
      root.innerHTML = renderCard(refreshed, meta);
      attachCardBehaviour(root, refreshed, meta);
      const freshState = root.querySelector('[data-role="refresh-state"]');
      if (freshState) freshState.textContent = 'Données SIRENE rafraîchies à l\'instant.';
    } catch (error) {
      state.textContent = `Rafraîchissement impossible (${error.message}). `
        + `Instantané archivé le ${frenchDate(detail.sirene?.resolved_at)} conservé.`;
      button.disabled = false;
    }
  });
}
