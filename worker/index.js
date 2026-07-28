/**
 * Relais CORS vers l'API Recherche d'entreprises (données SIRENE + RNE).
 *
 * Raison d'être : cette API n'envoie aucun en-tête CORS, un navigateur ne peut donc pas
 * l'appeler directement. Ce Worker ajoute ces en-têtes et met les réponses en cache.
 *
 * Il est **facultatif**. Sans lui, le site affiche l'instantané SIRENE archivé dans le
 * dépôt, avec sa date : le rattachement média → entreprise est de toute façon pré-calculé
 * côté GitHub Actions. Le Worker sert uniquement à rafraîchir une fiche à la demande.
 *
 * Deux routes seulement, en lecture, sur liste blanche — pas un proxy ouvert :
 *   GET /api/entreprise/{siren}   fiche d'une entreprise par son SIREN
 *   GET /api/search?q=…           recherche libre (éditeur non apparié automatiquement)
 */

const UPSTREAM = 'https://recherche-entreprises.api.gouv.fr';

// L'amont recommande un User-Agent explicite et descriptif.
const USER_AGENT = 'cppap-recherche-worker/1.0 (+https://github.com/bthv1/cppap-recherche)';

const CACHE_SECONDS = 86400; // 24 h : SIRENE est mis à jour quotidiennement.

const INCLUDE_FIELDS = 'siege,dirigeants,complements,finances,tva';

const MAX_PER_PAGE = 25; // Plafond imposé par l'amont.

/** Champs de filtre transmis tels quels à /search. Tout le reste est ignoré. */
const FORWARDED_PARAMS = new Set([
  'q',
  'departement',
  'code_postal',
  'code_commune',
  'activite_principale',
  'section_activite_principale',
  'nature_juridique',
  'etat_administratif',
  'categorie_entreprise',
  'page',
  'per_page',
]);

export function corsHeaders(request, env) {
  const allowed = (env.ALLOWED_ORIGINS ?? '').trim();
  const origin = request.headers.get('Origin') ?? '';

  let allowOrigin = '*';
  if (allowed && allowed !== '*') {
    const list = allowed.split(',').map((value) => value.trim()).filter(Boolean);
    if (!list.includes(origin)) return null;
    allowOrigin = origin;
  }

  return {
    'Access-Control-Allow-Origin': allowOrigin,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    Vary: 'Origin',
  };
}

function json(body, status, headers) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...headers },
  });
}

export function isSiren(value) {
  return /^\d{9}$/.test(value);
}

/** Construit l'URL amont pour une requête entrante déjà validée. */
export function upstreamUrl(pathname, searchParams) {
  const url = new URL(`${UPSTREAM}/search`);

  const sirenMatch = /^\/api\/entreprise\/(\d{9})\/?$/.exec(pathname);
  if (sirenMatch) {
    url.searchParams.set('q', `siren:${sirenMatch[1]}`);
    url.searchParams.set('per_page', '1');
  } else {
    for (const [key, value] of searchParams) {
      if (FORWARDED_PARAMS.has(key) && value) url.searchParams.set(key, value);
    }
    const perPage = Number(url.searchParams.get('per_page') ?? 10);
    url.searchParams.set(
      'per_page',
      String(Math.min(Number.isFinite(perPage) && perPage > 0 ? perPage : 10, MAX_PER_PAGE)),
    );
  }

  url.searchParams.set('minimal', 'true');
  url.searchParams.set('include', INCLUDE_FIELDS);
  return url;
}

export default {
  async fetch(request, env, ctx) {
    const cors = corsHeaders(request, env);
    if (cors === null) {
      return json({ error: 'Origine non autorisée' }, 403, {
        'Access-Control-Allow-Origin': 'null',
      });
    }

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors });
    }
    if (request.method !== 'GET') {
      return json({ error: 'Méthode non autorisée' }, 405, { ...cors, Allow: 'GET, OPTIONS' });
    }

    const url = new URL(request.url);
    const { pathname } = url;

    if (pathname === '/health' || pathname === '/health/') {
      return json({ status: 'ok', upstream: UPSTREAM }, 200, cors);
    }

    const isEntreprise = pathname.startsWith('/api/entreprise/');
    const isSearch = pathname === '/api/search' || pathname === '/api/search/';

    if (!isEntreprise && !isSearch) {
      return json({ error: 'Route inconnue', routes: ['/api/entreprise/{siren}', '/api/search'] },
        404, cors);
    }

    if (isEntreprise) {
      const siren = pathname.slice('/api/entreprise/'.length).replace(/\/$/, '');
      if (!isSiren(siren)) {
        return json({ error: 'SIREN invalide : 9 chiffres attendus' }, 400, cors);
      }
    } else if (!url.searchParams.get('q')) {
      return json({ error: 'Paramètre « q » requis' }, 400, cors);
    }

    const target = upstreamUrl(pathname, url.searchParams);

    // Le cache est indexé sur l'URL amont normalisée : deux requêtes entrantes différentes
    // qui aboutissent à la même interrogation partagent la même entrée.
    const cache = caches.default;
    const cacheKey = new Request(target.toString(), { method: 'GET' });
    const cached = await cache.match(cacheKey);
    if (cached) {
      const body = await cached.text();
      return new Response(body, {
        status: cached.status,
        headers: { ...Object.fromEntries(cached.headers), ...cors, 'X-Cache': 'HIT' },
      });
    }

    let upstream;
    try {
      upstream = await fetch(target.toString(), {
        headers: { 'User-Agent': USER_AGENT, Accept: 'application/json' },
      });
    } catch (error) {
      return json({ error: `Amont injoignable : ${error.message}` }, 502, cors);
    }

    if (upstream.status === 429) {
      return json(
        { error: 'Limite de débit de l\'API atteinte, réessayez dans quelques instants' },
        429,
        { ...cors, 'Retry-After': upstream.headers.get('Retry-After') ?? '2' },
      );
    }
    if (!upstream.ok) {
      return json({ error: `Amont en erreur (HTTP ${upstream.status})` }, 502, cors);
    }

    const body = await upstream.text();
    const headers = {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': `public, max-age=${CACHE_SECONDS}`,
    };

    // Mise en cache hors du chemin critique : la réponse part sans attendre l'écriture.
    ctx.waitUntil(cache.put(cacheKey, new Response(body, { headers })));

    return new Response(body, { status: 200, headers: { ...headers, ...cors, 'X-Cache': 'MISS' } });
  },
};
