#!/usr/bin/env python3
"""Appariement des éditeurs CPPAP avec la base SIRENE.

Les fichiers CPPAP ne contiennent aucun numéro SIREN : ils ne portent que la raison
sociale, la forme juridique et le département du siège. Le rapprochement est donc
heuristique, par nom — et c'est le point faible assumé de la chaîne. Trois choix en
découlent :

- le résultat est **mis en cache et versionné** dans data/sirene/cache.json, donc relisible
  en diff et jamais recalculé silencieusement ;
- chaque appariement porte un **niveau de confiance** et conserve ses trois meilleurs
  candidats, pour que l'interface puisse afficher un doute plutôt qu'une fausse certitude ;
- data/sirene/overrides.csv permet de **corriger un appariement à la main**, en PR relue,
  avec priorité absolue sur l'heuristique.

Source interrogée : API Recherche d'entreprises (données SIRENE + RNE, ouverte, sans clé).
Débit plafonné à 7 req/s par IP côté API ; on reste volontairement à 4 req/s.

Usage :
    python scripts/match_sirene.py                 # complète le cache
    python scripts/match_sirene.py --refresh       # réinterroge tout
    python scripts/match_sirene.py --limit 50      # utile pour un premier essai
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import repo
from lib.http import HttpClient, HttpError
from lib.text import fold, normalize_company, token_set_ratio
from normalize import load_all_records

log = logging.getLogger("match_sirene")

CACHE_VERSION = 2

# Tous les champs utiles à la fiche, sauf `matching_etablissements` (inutile ici) et `score`.
INCLUDE_FIELDS = "siege,dirigeants,complements,finances,tva"

# Seuils de confiance. `certain` exige aussi un écart net avec le deuxième candidat :
# deux sociétés homonymes doivent ressortir en doute, pas en certitude.
THRESHOLD_CERTAIN = 0.95
THRESHOLD_PROBABLE = 0.85
THRESHOLD_INCERTAIN = 0.70
CERTAIN_MARGIN = 0.10

MAX_CANDIDATES = 3
MAX_DIRIGEANTS = 12

# Part du score laissée aux signaux secondaires (département, activité, forme juridique).
# La similarité de nom garde les 75 % restants et reste donc le signal déterminant : des
# bonus simplement additionnés, écrêtés à 1.0, mettraient « SOCIETE EDITRICE DU MONDE » et
# « SOCIETE EDITRICE DU MONDE DIPLOMATIQUE » à égalité parfaite — exactement le cas que la
# marge de confiance doit savoir distinguer.
BONUS_WEIGHT = 0.25

BONUS_DEPARTEMENT = 0.15
BONUS_ACTIF = 0.05
BONUS_NATURE = 0.05

# Catégories juridiques INSEE, par préfixe. Bonus faible et volontairement tolérant :
# une forme juridique non reconnue ne pénalise jamais un candidat.
LEGAL_FORM_TO_NATURE_PREFIX = {
    "snc": ("52",),
    "sarl": ("54",),
    "sarlu": ("54",),
    "eurl": ("54",),
    "scs": ("53",),
    "sca": ("53",),
    "sa": ("55",),
    "sas": ("57",),
    "sasu": ("57",),
    "sci": ("65",),
    "scp": ("61", "62", "63", "64"),
    "scm": ("61", "62", "63", "64"),
    "selarl": ("54", "57"),
    "selas": ("57",),
    "scop": ("51", "55", "57"),
    "association": ("92",),
    "fondation": ("93",),
    "ei": ("10",),
    "eirl": ("10",),
    "organisme public": ("7",),
    "etablissement public": ("7",),
    "epic": ("7",),
}


# --------------------------------------------------------------------------------------
# Interrogation de l'API
# --------------------------------------------------------------------------------------


def search(
    client: HttpClient, query: str, departement: str = "", per_page: int = 10
) -> list[dict[str, Any]]:
    """Recherche textuelle. Retourne la liste brute des résultats."""
    params: dict[str, Any] = {
        "q": query,
        "per_page": per_page,
        "minimal": "true",
        "include": INCLUDE_FIELDS,
    }
    if departement:
        params["departement"] = departement
    try:
        payload = client.get_json(f"{repo.RECHERCHE_ENTREPRISES_API}/search", params)
    except HttpError as exc:
        log.warning("Recherche « %s » en échec (%s)", query, exc)
        return []
    return (payload or {}).get("results") or []


def fetch_by_siren(client: HttpClient, siren: str) -> dict[str, Any] | None:
    """Récupère une entreprise par son SIREN — utilisé pour les overrides manuels."""
    results = search(client, f"siren:{siren}", per_page=1)
    return results[0] if results else None


# --------------------------------------------------------------------------------------
# Notation
# --------------------------------------------------------------------------------------


def _nature_prefixes(forme_juridique: str) -> tuple[str, ...]:
    """Préfixes de catégorie juridique INSEE attendus pour une forme juridique CPPAP."""
    folded = fold(forme_juridique)
    if not folded:
        return ()
    prefixes = LEGAL_FORM_TO_NATURE_PREFIX.get(folded)
    if prefixes is None:
        # Certaines sources écrivent « Société anonyme » plutôt que « SA ».
        for key, value in LEGAL_FORM_TO_NATURE_PREFIX.items():
            if len(key) > 3 and key in folded:
                return value
    return prefixes or ()


def name_similarity(editeur_norm: str, candidate: dict[str, Any]) -> float:
    """Meilleure similarité entre la raison sociale CPPAP et les dénominations SIRENE."""
    names = [
        candidate.get("nom_raison_sociale"),
        candidate.get("nom_complet"),
        candidate.get("sigle"),
        (candidate.get("siege") or {}).get("nom_commercial"),
    ]
    best = 0.0
    for raw in names:
        normalized = normalize_company(raw)
        if not normalized:
            continue
        if normalized == editeur_norm:
            return 1.0
        best = max(best, token_set_ratio(editeur_norm, normalized))
    return best


def score_candidate(
    editeur_norm: str, departement: str, forme_juridique: str, candidate: dict[str, Any]
) -> tuple[float, dict[str, float]]:
    """Note un candidat entre 0 et 1, avec le détail des composantes.

    Le score mêle la similarité de nom (75 %) et la fraction des signaux secondaires
    obtenue (25 %). Un signal n'entre au dénominateur que s'il est *vérifiable* : sans
    département dans la source CPPAP, l'absence de concordance géographique ne pénalise
    pas le candidat — c'est la source qui est incomplète, pas l'appariement qui est douteux.
    """
    siege = candidate.get("siege") or {}
    nom = name_similarity(editeur_norm, candidate)

    achievable = BONUS_ACTIF
    obtained = BONUS_ACTIF if candidate.get("etat_administratif") == "A" else 0.0

    if departement:
        achievable += BONUS_DEPARTEMENT
        if siege.get("departement") == departement:
            obtained += BONUS_DEPARTEMENT

    prefixes = _nature_prefixes(forme_juridique)
    nature = str(candidate.get("nature_juridique") or "")
    if prefixes and nature:
        achievable += BONUS_NATURE
        if nature.startswith(prefixes):
            obtained += BONUS_NATURE

    bonus_fraction = (obtained / achievable) if achievable else 1.0
    score = nom * (1 - BONUS_WEIGHT) + bonus_fraction * BONUS_WEIGHT

    parts = {
        "nom": round(nom, 4),
        "bonus_obtenu": round(obtained, 4),
        "bonus_possible": round(achievable, 4),
    }
    return round(min(score, 1.0), 4), parts


def classify(scored: list[tuple[float, dict[str, Any], dict[str, float]]]) -> str:
    """Traduit les scores en niveau de confiance affichable."""
    if not scored:
        return "aucun"
    best = scored[0][0]
    if best < THRESHOLD_INCERTAIN:
        return "aucun"
    if best < THRESHOLD_PROBABLE:
        return "incertain"
    if best < THRESHOLD_CERTAIN:
        return "probable"
    second = scored[1][0] if len(scored) > 1 else 0.0
    return "certain" if (best - second) >= CERTAIN_MARGIN else "probable"


# --------------------------------------------------------------------------------------
# Extraction des champs conservés
# --------------------------------------------------------------------------------------

_ENTREPRISE_FIELDS = (
    "siren",
    "nom_complet",
    "nom_raison_sociale",
    "sigle",
    "activite_principale",
    "activite_principale_naf25",
    "section_activite_principale",
    "nature_juridique",
    "categorie_entreprise",
    "annee_categorie_entreprise",
    "caractere_employeur",
    "date_creation",
    "date_fermeture",
    "date_mise_a_jour",
    "date_mise_a_jour_insee",
    "date_mise_a_jour_rne",
    "etat_administratif",
    "tranche_effectif_salarie",
    "annee_tranche_effectif_salarie",
    "statut_diffusion",
    "nombre_etablissements",
    "nombre_etablissements_ouverts",
    "tva",
)

_SIEGE_FIELDS = (
    "siret",
    "adresse",
    "complement_adresse",
    "numero_voie",
    "indice_repetition",
    "type_voie",
    "libelle_voie",
    "code_postal",
    "cedex",
    "libelle_cedex",
    "commune",
    "libelle_commune",
    "departement",
    "region",
    "latitude",
    "longitude",
    "activite_principale",
    "activite_principale_naf25",
    "date_creation",
    "date_debut_activite",
    "date_fermeture",
    "etat_administratif",
    "tranche_effectif_salarie",
    "caractere_employeur",
    "nom_commercial",
    "liste_enseignes",
    "libelle_commune_etranger",
    "libelle_pays_etranger",
)


def extract_entreprise(candidate: dict[str, Any]) -> dict[str, Any]:
    """Ne conserve que les champs affichés sur la fiche, pour contenir la taille du site."""
    entreprise = {k: candidate.get(k) for k in _ENTREPRISE_FIELDS if candidate.get(k) is not None}

    siege = candidate.get("siege") or {}
    entreprise["siege"] = {k: siege.get(k) for k in _SIEGE_FIELDS if siege.get(k) not in (None, "")}

    dirigeants = candidate.get("dirigeants") or []
    entreprise["dirigeants"] = [
        {k: v for k, v in d.items() if v not in (None, "")} for d in dirigeants[:MAX_DIRIGEANTS]
    ]
    entreprise["dirigeants_total"] = len(dirigeants)

    complements = candidate.get("complements") or {}
    # Les booléens faux n'apportent rien à la fiche et pèsent sur le poids du site.
    entreprise["complements"] = {k: v for k, v in complements.items() if v not in (None, "", False)}

    if candidate.get("finances"):
        entreprise["finances"] = candidate["finances"]

    return entreprise


def candidate_summary(
    candidate: dict[str, Any], score: float, parts: dict[str, float]
) -> dict[str, Any]:
    siege = candidate.get("siege") or {}
    return {
        "siren": candidate.get("siren"),
        "nom": candidate.get("nom_complet") or candidate.get("nom_raison_sociale"),
        "score": score,
        "score_parts": parts,
        "departement": siege.get("departement"),
        "adresse": siege.get("adresse"),
        "etat_administratif": candidate.get("etat_administratif"),
        "nature_juridique": candidate.get("nature_juridique"),
    }


# --------------------------------------------------------------------------------------
# Résolution d'un éditeur
# --------------------------------------------------------------------------------------


def resolve_publisher(
    client: HttpClient,
    editeur: str,
    departement: str,
    forme_juridique: str,
    resolved_at: str,
) -> dict[str, Any]:
    """Résout un éditeur en tentant plusieurs stratégies, de la plus contrainte à la plus large."""
    editeur_norm = normalize_company(editeur)
    base: dict[str, Any] = {
        "editeur": editeur,
        "departement": departement,
        "forme_juridique": forme_juridique,
        "resolved_at": resolved_at,
    }
    if not editeur_norm:
        return {**base, "confidence": "aucun", "score": 0.0, "strategy": "vide", "candidates": []}

    strategies: list[tuple[str, str, str]] = []
    if departement:
        strategies.append(("nom+departement", editeur_norm, departement))
    strategies.append(("nom", editeur_norm, ""))
    # Certaines dénominations SIRENE intègrent la forme juridique (« SA OUEST-FRANCE ») :
    # une dernière tentative sur le libellé brut peut donc mieux coller.
    raw_folded = fold(editeur)
    if raw_folded != editeur_norm:
        strategies.append(("nom brut", raw_folded, ""))

    best_scored: list[tuple[float, dict[str, Any], dict[str, float]]] = []
    used_strategy = "aucune"

    for label, query, dept_filter in strategies:
        results = search(client, query, dept_filter)
        scored: list[tuple[float, dict[str, Any], dict[str, float]]] = []
        for result in results:
            score, parts = score_candidate(editeur_norm, departement, forme_juridique, result)
            scored.append((score, result, parts))
        scored.sort(key=lambda item: item[0], reverse=True)

        if scored and (not best_scored or scored[0][0] > best_scored[0][0]):
            best_scored = scored
            used_strategy = label

        if best_scored and best_scored[0][0] >= THRESHOLD_CERTAIN:
            break

    confidence = classify(best_scored)
    resolution: dict[str, Any] = {
        **base,
        "confidence": confidence,
        "score": best_scored[0][0] if best_scored else 0.0,
        "strategy": used_strategy,
        "candidates": [
            candidate_summary(result, score, parts)
            for score, result, parts in best_scored[:MAX_CANDIDATES]
        ],
    }
    if confidence != "aucun":
        resolution["siren"] = best_scored[0][1].get("siren")
        resolution["entreprise"] = extract_entreprise(best_scored[0][1])
    return resolution


# --------------------------------------------------------------------------------------
# Overrides et cache
# --------------------------------------------------------------------------------------


def load_overrides(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Lit data/sirene/overrides.csv : clé -> {siren, note}.

    La clé peut désigner un identifiant de fiche, un numéro CPPAP ou une clé d'éditeur ;
    la résolution est faite par l'appelant, dans cet ordre de priorité.
    """
    file = path or repo.SIRENE_OVERRIDES_FILE
    if not file.exists():
        return {}
    overrides: dict[str, dict[str, str]] = {}
    with file.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row.get("cle") or "").strip()
            siren = "".join(ch for ch in (row.get("siren") or "") if ch.isdigit())
            if not key or key.startswith("#"):
                continue
            if len(siren) != 9:
                log.warning("Override ignoré pour %r : SIREN %r invalide", key, row.get("siren"))
                continue
            overrides[key] = {"siren": siren, "note": (row.get("note") or "").strip()}
    log.info("%s override(s) manuel(s) chargé(s)", len(overrides))
    return overrides


def resolve_override_targets(
    records: list[dict[str, Any]], overrides: dict[str, dict[str, str]]
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Répartit les overrides selon leur portée.

    Une clé d'override peut viser une fiche précise (identifiant ou n° CPPAP) ou tout un
    éditeur (clé d'éditeur). La première forme est plus fine : un même éditeur peut avoir été
    correctement apparié pour un titre et mal pour un autre. La plus spécifique gagne.
    """
    publisher_overrides: dict[str, dict[str, str]] = {}
    record_overrides: dict[str, dict[str, str]] = {}
    unused = set(overrides)

    for record in records:
        for key in (record["id"], record["cppap"], record["publisher_key"]):
            if not key or key not in overrides:
                continue
            unused.discard(key)
            if key == record["publisher_key"]:
                publisher_overrides[key] = overrides[key]
            else:
                record_overrides[record["id"]] = overrides[key]
            break

    if unused:
        log.warning(
            "%s override(s) ne correspond(ent) à aucune fiche ni éditeur : %s",
            len(unused),
            ", ".join(sorted(unused)),
        )
    return publisher_overrides, record_overrides


def load_cache() -> dict[str, Any]:
    cache = repo.read_json(repo.SIRENE_CACHE_FILE, default=None)
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "entries": {}, "record_entries": {}}
    cache.setdefault("entries", {})
    cache.setdefault("record_entries", {})
    return cache


def save_cache(cache: dict[str, Any]) -> None:
    repo.write_json(repo.SIRENE_CACHE_FILE, cache)


# --------------------------------------------------------------------------------------
# Entrée
# --------------------------------------------------------------------------------------


def collect_publishers(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Regroupe les enregistrements par éditeur : un même éditeur publie plusieurs titres."""
    publishers: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record.get("publisher_key")
        if not key:
            continue
        entry = publishers.setdefault(
            key,
            {
                "editeur": record["editeur"],
                "departement": record["departement"],
                "forme_juridique": record["forme_juridique"],
                "records": 0,
            },
        )
        entry["records"] += 1
        if not entry["forme_juridique"] and record["forme_juridique"]:
            entry["forme_juridique"] = record["forme_juridique"]
    return publishers


def forced_resolution(
    client: HttpClient,
    siren: str,
    note: str,
    publisher: dict[str, Any],
    resolved_at: str,
    label: str,
) -> dict[str, Any]:
    """Construit une résolution à partir d'un SIREN imposé par un override manuel."""
    resolution: dict[str, Any] = {
        "editeur": publisher.get("editeur", ""),
        "departement": publisher.get("departement", ""),
        "forme_juridique": publisher.get("forme_juridique", ""),
        "resolved_at": resolved_at,
        "confidence": "verifie",
        "score": 1.0,
        "strategy": "override manuel",
        "siren": siren,
        "note": note,
        "candidates": [],
    }
    candidate = fetch_by_siren(client, siren)
    if candidate:
        resolution["entreprise"] = extract_entreprise(candidate)
    else:
        log.warning("Override %s : SIREN %s introuvable dans l'API", label, siren)
    return resolution


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Réinterroger même si en cache")
    parser.add_argument("--limit", type=int, default=0, help="Ne résoudre que N éditeurs")
    parser.add_argument("--per-second", type=float, default=4.0, help="Débit (max API : 7)")
    parser.add_argument("--save-every", type=int, default=25, help="Sauvegarde du cache tous les N")
    parser.add_argument("--data-dir", type=Path, help="Répertoire des CSV (défaut : data/latest)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    config = repo.load_config()
    records, _ = load_all_records(config, args.data_dir)
    if not records:
        log.error("Aucun enregistrement — lancez d'abord scripts/ingest.py")
        return 1

    publishers = collect_publishers(records)
    cache = load_cache()
    overrides = load_overrides()
    publisher_overrides, record_overrides = resolve_override_targets(records, overrides)
    resolved_at = datetime.now(UTC).date().isoformat()

    records_by_id = {record["id"]: record for record in records}

    pending = [
        key
        for key in publishers
        if args.refresh or key not in cache["entries"] or key in publisher_overrides
    ]
    pending_records = [
        record_id
        for record_id in record_overrides
        if args.refresh or record_id not in cache["record_entries"]
    ]
    if args.limit:
        pending = pending[: args.limit]

    log.info(
        "%s éditeur(s) unique(s) pour %s fiche(s) — %s à résoudre, %s déjà en cache%s",
        len(publishers),
        len(records),
        len(pending),
        len(publishers) - len(pending),
        f" | {len(pending_records)} override(s) au niveau fiche" if pending_records else "",
    )

    client = HttpClient(user_agent=config["user_agent"], per_second=args.per_second)
    processed = 0
    try:
        for index, key in enumerate(pending, start=1):
            publisher = publishers[key]
            override = publisher_overrides.get(key)
            if override:
                resolution = forced_resolution(
                    client, override["siren"], override["note"], publisher, resolved_at, key
                )
            else:
                resolution = resolve_publisher(
                    client,
                    publisher["editeur"],
                    publisher["departement"],
                    publisher["forme_juridique"],
                    resolved_at,
                )

            cache["entries"][key] = resolution
            processed += 1

            if index % 50 == 0 or index == len(pending):
                log.info("  %s/%s éditeurs résolus", index, len(pending))
            if args.save_every and processed % args.save_every == 0:
                save_cache(cache)

        for record_id in pending_records:
            override = record_overrides[record_id]
            record = records_by_id[record_id]
            cache["record_entries"][record_id] = forced_resolution(
                client, override["siren"], override["note"], record, resolved_at, record_id
            )
            processed += 1
    except KeyboardInterrupt:
        log.warning("Interruption — sauvegarde du cache partiel (%s résolus)", processed)
    finally:
        if processed:
            save_cache(cache)

    counts: dict[str, int] = {}
    for entry in cache["entries"].values():
        level = entry.get("confidence", "aucun")
        counts[level] = counts.get(level, 0) + 1
    total = sum(counts.values()) or 1
    log.info("Confiance d'appariement sur %s éditeur(s) en cache :", total)
    for level in ("verifie", "certain", "probable", "incertain", "aucun"):
        count = counts.get(level, 0)
        log.info("  %-10s %5s  (%4.1f %%)", level, count, 100 * count / total)
    log.info(
        "Les niveaux « incertain » et « aucun » sont à relire ; corrigez-les via %s",
        repo.SIRENE_OVERRIDES_FILE.relative_to(repo.ROOT),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
