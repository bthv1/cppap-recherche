/**
 * Tests du relais SIRENE, sans réseau ni environnement Cloudflare.
 *
 * On vérifie la logique de routage, le plafonnement de `per_page`, le filtrage des paramètres
 * transmis et la liste blanche d'origines — c'est là que se jouent les risques (proxy ouvert,
 * paramètres non maîtrisés relayés à l'amont).
 *
 * Exécution : node --test worker/
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import worker, { corsHeaders, isSiren, upstreamUrl } from './index.js';

const req = (url, { method = 'GET', origin } = {}) =>
  new Request(url, { method, headers: origin ? { Origin: origin } : {} });

const ctx = { waitUntil() {} };

test('isSiren n\'accepte que 9 chiffres', () => {
  assert.ok(isSiren('900000101'));
  assert.ok(!isSiren('12345678'));
  assert.ok(!isSiren('1234567890'));
  assert.ok(!isSiren('90000010a'));
  assert.ok(!isSiren(''));
});

test('une route entreprise devient une recherche par SIREN', () => {
  const url = upstreamUrl('/api/entreprise/900000101', new URLSearchParams());
  assert.equal(url.origin + url.pathname, 'https://recherche-entreprises.api.gouv.fr/search');
  assert.equal(url.searchParams.get('q'), 'siren:900000101');
  assert.equal(url.searchParams.get('per_page'), '1');
  assert.equal(url.searchParams.get('minimal'), 'true');
  assert.match(url.searchParams.get('include'), /siege/);
});

test('seuls les paramètres en liste blanche sont transmis', () => {
  const params = new URLSearchParams({
    q: 'le monde',
    departement: '75',
    // Hors liste : ne doit pas atteindre l'amont.
    nom_personne: 'dupont',
    include: 'tout',
    minimal: 'false',
  });
  const url = upstreamUrl('/api/search', params);
  assert.equal(url.searchParams.get('q'), 'le monde');
  assert.equal(url.searchParams.get('departement'), '75');
  assert.equal(url.searchParams.get('nom_personne'), null);
  // Les valeurs imposées par le relais gagnent sur celles de l'appelant.
  assert.equal(url.searchParams.get('minimal'), 'true');
  assert.match(url.searchParams.get('include'), /dirigeants/);
});

test('per_page est plafonné à la limite de l\'amont', () => {
  const cases = [['500', '25'], ['5', '5'], ['0', '10'], ['abc', '10'], [null, '10']];
  for (const [input, expected] of cases) {
    const params = new URLSearchParams({ q: 'x' });
    if (input !== null) params.set('per_page', input);
    assert.equal(upstreamUrl('/api/search', params).searchParams.get('per_page'), expected,
      `per_page=${input}`);
  }
});

test('la liste blanche d\'origines rejette une origine inconnue', () => {
  const env = { ALLOWED_ORIGINS: 'https://bthv1.github.io' };
  assert.equal(corsHeaders(req('https://w.dev/health', { origin: 'https://ailleurs.example' }), env), null);

  const allowed = corsHeaders(req('https://w.dev/health', { origin: 'https://bthv1.github.io' }), env);
  assert.equal(allowed['Access-Control-Allow-Origin'], 'https://bthv1.github.io');
  assert.equal(allowed.Vary, 'Origin');
});

test('sans liste blanche, toute origine est autorisée', () => {
  const headers = corsHeaders(req('https://w.dev/health', { origin: 'https://ailleurs.example' }), {});
  assert.equal(headers['Access-Control-Allow-Origin'], '*');
});

test('origine non autorisée : 403 sans appel amont', async () => {
  const response = await worker.fetch(
    req('https://w.dev/api/entreprise/900000101', { origin: 'https://ailleurs.example' }),
    { ALLOWED_ORIGINS: 'https://bthv1.github.io' },
    ctx,
  );
  assert.equal(response.status, 403);
});

test('préflight OPTIONS répond 204', async () => {
  const response = await worker.fetch(req('https://w.dev/api/search?q=x', { method: 'OPTIONS' }), {}, ctx);
  assert.equal(response.status, 204);
  assert.equal(response.headers.get('Access-Control-Allow-Methods'), 'GET, OPTIONS');
});

test('méthode non GET refusée', async () => {
  const response = await worker.fetch(req('https://w.dev/api/search?q=x', { method: 'POST' }), {}, ctx);
  assert.equal(response.status, 405);
});

test('route inconnue refusée avant tout appel amont', async () => {
  const response = await worker.fetch(req('https://w.dev/autre'), {}, ctx);
  assert.equal(response.status, 404);
});

test('SIREN mal formé refusé avant tout appel amont', async () => {
  const response = await worker.fetch(req('https://w.dev/api/entreprise/123'), {}, ctx);
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /SIREN invalide/);
});

test('recherche sans q refusée', async () => {
  const response = await worker.fetch(req('https://w.dev/api/search'), {}, ctx);
  assert.equal(response.status, 400);
});

test('/health répond sans réseau', async () => {
  const response = await worker.fetch(req('https://w.dev/health'), {}, ctx);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).status, 'ok');
});
