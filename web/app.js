/**
 * Orchestration du site : chargement des données, recherche, rendu des résultats et des cartes.
 *
 * L'index compact est chargé au démarrage ; les fiches complètes sont réparties en lots et
 * récupérées à la demande, à l'ouverture d'une carte. Une carte ouverte est reflétée dans
 * l'URL (`#/f/<identifiant>`), de sorte qu'un lien vers une fiche précise soit partageable.
 */

import { MediaIndex } from './search.js';
import { attachCardBehaviour, esc, renderCard } from './card.js';

const RESULT_LIMIT = 60;

const el = {
  form: document.getElementById('search-form'),
  q: document.getElementById('q'),
  clear: document.getElementById('clear'),
  type: document.getElementById('f-type'),
  dept: document.getElementById('f-dept'),
  qual: document.getElementById('f-qual'),
  inscrit: document.getElementById('f-inscrit'),
  inscritHint: document.getElementById('inscrit-hint'),
  doubt: document.getElementById('f-doubt'),
  count: document.getElementById('results-heading'),
  results: document.getElementById('results'),
  more: document.getElementById('results-more'),
  card: document.getElementById('card'),
  cardEmpty: document.getElementById('card-empty'),
  state: document.getElementById('dataset-state'),
  sourceLinks: document.getElementById('source-links'),
  repoLink: document.getElementById('repo-link'),
};

const state = {
  meta: null,
  index: null,
  buckets: new Map(),
  currentId: null,
};

const numberFormat = new Intl.NumberFormat('fr-FR');

function frenchDate(value) {
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? ''));
  return iso ? `${iso[3]}/${iso[2]}/${iso[1]}` : '';
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`${path} : HTTP ${response.status}`);
  return response.json();
}

// --------------------------------------------------------------------------------------
// En-tête et pied de page
// --------------------------------------------------------------------------------------

function describeDataset(meta) {
  const stats = meta.stats ?? {};
  const sources = Object.values(meta.sources ?? {});
  const dates = sources
    .map((source) => source.latest?.observed_at)
    .filter(Boolean)
    .sort();

  // La liste des niveaux considérés comme établis vient de meta.json, pas d'ici : la
  // dupliquer avait fait annoncer « 0 % » alors que le quart des fiches était rattaché.
  const trusted = meta.confidence_trusted ?? ['verifie', 'siret', 'siret_propage', 'certain'];
  const confident = trusted
    .reduce((sum, level) => sum + (stats.by_confidence?.[level] ?? 0), 0);
  const share = stats.total ? Math.round((100 * confident) / stats.total) : 0;

  const inscrits = stats.by_statut?.inscrit ?? 0;
  const merged = stats.multi_listes ?? 0;
  const parts = [
    `${numberFormat.format(stats.total ?? 0)} fiches issues de ${sources.length} listes CPPAP`
      + (merged
        ? ` (${numberFormat.format(merged)} inscriptions figurant dans deux listes, réunies en une fiche)`
        : ''),
    `dont ${numberFormat.format(inscrits)} inscrites ou reconnues`,
    `${numberFormat.format(stats.ipg ?? 0)} qualifiées IPG`,
  ];
  if (dates.length) parts.push(`dernière version archivée le ${frenchDate(dates[dates.length - 1])}`);
  parts.push(`rattachement SIRENE établi pour ${share} % des fiches`);

  const prefix = meta.fixtures
    ? '⚠ DONNÉES DE DÉMONSTRATION (fixtures de test, contenu inventé) — '
    : '';
  return prefix + parts.join(' · ');
}

function renderMeta(meta) {
  el.state.textContent = describeDataset(meta);
  if (meta.fixtures) el.state.classList.add('is-fixtures');

  if (meta.repository) {
    el.repoLink.href = `https://github.com/${meta.repository}`;
    el.repoLink.textContent = meta.repository;
  }

  el.sourceLinks.innerHTML = Object.values(meta.sources ?? {})
    .map((source) => {
      const version = source.latest?.observed_at
        ? ` (version du ${frenchDate(source.latest.observed_at)})`
        : '';
      return source.dataset_page
        ? `<a href="${esc(source.dataset_page)}" target="_blank" rel="noopener noreferrer">${
            esc(source.label_plural ?? source.label)}</a>${esc(version)}`
        : esc(source.label_plural ?? source.label);
    })
    .join(' · ');

  for (const type of meta.types ?? []) {
    const option = document.createElement('option');
    option.value = type.type;
    option.textContent = type.label_plural ?? type.label;
    el.type.append(option);
  }

  fillOptions(el.dept, (meta.stats?.departements ?? []).map(
    (dept) => ({ value: dept.code, label: `${dept.code} — ${dept.label}` }),
  ));
  fillOptions(el.qual, (meta.stats?.qualifications ?? []).map(
    (qual) => ({ value: qual.cle, label: qual.label }),
  ));

  // Le filtre est actif par défaut : il faut dire ce qu'il écarte, et combien.
  const expires = meta.stats?.by_statut?.non_inscrit ?? 0;
  if (expires) {
    el.inscritHint.textContent = `décochez pour inclure les ${numberFormat.format(expires)} `
      + "titres dont l'inscription a expiré";
  }
}

// --------------------------------------------------------------------------------------
// Résultats
// --------------------------------------------------------------------------------------

/** Alimente un menu de filtre. Le libellé est mémorisé, le décompte lui étant ajouté à chaque rendu. */
function fillOptions(select, entries) {
  for (const { value, label } of entries) {
    const option = document.createElement('option');
    option.value = value;
    option.dataset.label = label;
    option.textContent = label;
    select.append(option);
  }
}

/**
 * Met à jour les décomptes des menus de filtre d'après les résultats courants.
 *
 * Un nombre figé serait trompeur dès qu'un autre filtre est actif — et celui des inscriptions
 * en cours l'est par défaut : le menu annoncerait 491 fiches IPG là où la liste en montre 456.
 */
function refreshFacetCounts(query, filters) {
  for (const [select, field] of [[el.qual, 'qual'], [el.dept, 'dept']]) {
    const counts = state.index.facet(query, filters, field);
    for (const option of select.options) {
      if (!option.value) continue;
      option.textContent = `${option.dataset.label} (${
        numberFormat.format(counts.get(option.value) ?? 0)})`;
    }
  }
}

function currentFilters() {
  return {
    type: el.type.value,
    dept: el.dept.value,
    qual: el.qual.value,
    inscrit: el.inscrit.checked,
    doubt: el.doubt.checked,
  };
}

function resultLabel(record, meta) {
  const typeLabels = record.typeList.map(
    (key) => (meta.types ?? []).find((t) => t.type === key)?.label ?? key,
  );
  const dept = (meta.stats?.departements ?? []).find((d) => d.code === record.dept);
  const bits = [
    record.editeur ? esc(record.editeur) : '',
    record.cppap ? `n° ${esc(record.cppap)}` : '',
    dept ? `${esc(dept.code)} — ${esc(dept.label)}` : (record.dept ? esc(record.dept) : ''),
  ].filter(Boolean);

  const tone = meta.labels?.confidence?.[record.confidence]?.tone ?? 'none';
  // Un titre dont l'inscription a expiré doit se voir dans la liste, pas seulement après
  // ouverture de la fiche. `exp` distingue l'inscription expirée du statut simplement absent.
  const statutKey = record.exp ? 'non_inscrit_expire' : 'non_inscrit';
  const statut = record.inscrit === 0
    ? `<span class="badge tone-risk">${
        esc(meta.labels?.statut?.[statutKey]?.label ?? 'NON INSCRIT')}</span>`
    : '';
  const qual = (meta.stats?.qualifications ?? []).find((q) => q.cle === record.qual);
  const badges = [
    ...typeLabels.map((label) => `<span class="badge">${esc(label)}</span>`),
    statut,
    qual ? `<span class="badge${record.ipg ? ' ipg' : ''}">${esc(qual.court)}</span>` : '',
    tone === 'ok' ? '' : `<span class="badge tone-${esc(tone)}">SIREN ?</span>`,
  ].filter(Boolean).join(' ');

  return `<span class="result-name">${esc(record.nom)}</span>
    <span class="result-meta">${badges}${bits.map((b) => `<span>${b}</span>`).join('')}</span>`;
}

function renderResults(query) {
  const filters = currentFilters();
  const { total, items } = state.index.query(query, filters, RESULT_LIMIT);
  refreshFacetCounts(query, filters);

  el.count.textContent = total === 0
    ? 'Aucun résultat'
    : `${numberFormat.format(total)} résultat${total > 1 ? 's' : ''}`;

  el.results.replaceChildren(...items.map((record) => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'result';
    button.dataset.id = record.id;
    button.innerHTML = resultLabel(record, state.meta);
    if (record.id === state.currentId) button.setAttribute('aria-current', 'true');
    li.append(button);
    return li;
  }));

  if (total > items.length) {
    el.more.hidden = false;
    el.more.textContent = `${numberFormat.format(items.length)} premiers résultats affichés `
      + `sur ${numberFormat.format(total)} — précisez la recherche pour affiner.`;
  } else if (total === 0) {
    el.more.hidden = false;
    el.more.textContent = query.trim()
      ? 'Essayez une orthographe différente, le nom de l\'entreprise éditrice, ou un n° CPPAP.'
      : 'Aucune fiche ne correspond à ces filtres.';
  } else {
    el.more.hidden = true;
  }

  el.clear.hidden = !el.q.value;
}

// --------------------------------------------------------------------------------------
// Carte
// --------------------------------------------------------------------------------------

/**
 * Reconstitue les libellés que les fiches ne transportent plus.
 *
 * Les lots de détail omettent tout ce qui est répétitif — libellé de type, de département,
 * liens vers la source — parce que `meta.json` les porte déjà une seule fois. Les répéter sur
 * chacune des dizaines de milliers de fiches pesait un quart du poids téléchargé.
 */
function enrichDetail(detail, meta) {
  const type = (meta.types ?? []).find((t) => t.type === detail.type);
  const dept = (meta.stats?.departements ?? []).find((d) => d.code === detail.departement);

  // Une fiche peut réunir plusieurs listes CPPAP : chacune garde sa page, sa version et son
  // instantané archivé, pour rester citable séparément.
  const sourceList = (detail.sources ?? [detail.source]).map((key) => {
    const source = (meta.sources ?? {})[key] ?? {};
    return {
      key,
      label: source.label_plural ?? source.label ?? key,
      page: source.dataset_page,
      snapshot: source.latest?.snapshot,
      version: source.latest?.observed_at,
    };
  });

  return {
    ...detail,
    type_label: detail.type_label ?? type?.label ?? detail.type,
    departement_label: dept?.label ?? '',
    source_list: sourceList,
  };
}

async function loadDetail(record) {
  const bucket = record.bucket;
  if (!state.buckets.has(bucket)) {
    state.buckets.set(bucket, fetchJson(`data/details/${bucket}.json`));
  }
  const payload = await state.buckets.get(bucket);
  return payload[record.id] ?? null;
}

async function openCard(requestedId, { updateHash = true } = {}) {
  // Un lien partagé avant la réunion des listes désigne une fiche qui n'existe plus sous cet
  // identifiant : on la retrouve par son numéro et l'on remet le lien à jour.
  const record = state.index.resolve(requestedId);
  const id = record?.id ?? requestedId;
  state.currentId = id;
  for (const button of el.results.querySelectorAll('.result')) {
    // `toggleAttribute` poserait aria-current="" , que le sélecteur CSS ne reconnaît pas.
    if (button.dataset.id === id) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }

  el.cardEmpty.hidden = true;
  el.card.hidden = false;
  el.card.innerHTML = '<div class="card-section">Chargement de la fiche…</div>';

  // Un identifiant obsolète est réécrit même à l'ouverture d'un lien reçu : le lecteur
  // repart ainsi avec l'adresse qui restera valable.
  if (updateHash || (record && record.id !== requestedId)) {
    const next = `#/f/${encodeURIComponent(id)}`;
    if (window.location.hash !== next) history.replaceState(null, '', next);
  }

  try {
    const raw = record ? await loadDetail(record) : null;
    if (!raw) {
      el.card.innerHTML = '<div class="card-section">Fiche introuvable.</div>';
      return;
    }
    const detail = enrichDetail(raw, state.meta);
    el.card.innerHTML = renderCard(detail, state.meta);
    attachCardBehaviour(el.card, detail, state.meta);
    if (window.matchMedia('(max-width: 900px)').matches) {
      // En colonne unique la carte s'affiche sous la liste : on y amène le lecteur, en
      // respectant une préférence de mouvement réduit.
      const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      el.card.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
    }
  } catch (error) {
    el.card.innerHTML = `<div class="card-section">Chargement impossible : ${esc(error.message)}</div>`;
  }
}

function idFromHash() {
  const match = /^#\/f\/(.+)$/.exec(window.location.hash);
  return match ? decodeURIComponent(match[1]) : null;
}

// --------------------------------------------------------------------------------------
// Démarrage
// --------------------------------------------------------------------------------------

function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function wireEvents() {
  const rerender = () => renderResults(el.q.value);

  el.form.addEventListener('submit', (event) => event.preventDefault());
  el.q.addEventListener('input', debounce(rerender, 120));
  el.type.addEventListener('change', rerender);
  el.dept.addEventListener('change', rerender);
  el.qual.addEventListener('change', rerender);
  el.inscrit.addEventListener('change', rerender);
  el.doubt.addEventListener('change', rerender);

  el.clear.addEventListener('click', () => {
    el.q.value = '';
    el.q.focus();
    rerender();
  });

  el.results.addEventListener('click', (event) => {
    const button = event.target.closest('.result');
    if (button) openCard(button.dataset.id);
  });

  window.addEventListener('hashchange', () => {
    const id = idFromHash();
    if (id && id !== state.currentId) openCard(id, { updateHash: false });
  });
}

async function start() {
  try {
    const [meta, search] = await Promise.all([
      fetchJson('data/meta.json'),
      fetchJson('data/search.json'),
    ]);
    state.meta = meta;
    state.index = new MediaIndex(search);

    renderMeta(meta);
    wireEvents();
    renderResults('');

    const initial = idFromHash();
    if (initial) await openCard(initial, { updateHash: false });

    el.q.focus();
  } catch (error) {
    el.count.textContent = 'Erreur';
    el.more.hidden = false;
    el.more.textContent = `Impossible de charger les données : ${error.message}. `
      + 'Le site a-t-il été généré par scripts/build_site.py ?';
    el.state.textContent = 'Données indisponibles.';
  }
}

start();
