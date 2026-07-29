#!/usr/bin/env python3
"""Génère le site statique publié sur GitHub Pages.

Trois sorties, dimensionnées pour que la recherche soit instantanée sans télécharger
l'intégralité des données :

- `data/search.json` : index compact au format colonnes (28 000 fiches, ~845 Ko compressés),
  chargé au démarrage et indexé côté navigateur ;
- `data/details/<n>.json` : fiches complètes réparties en 64 lots, chargées à l'ouverture
  d'une carte. Le lot est déduit d'une empreinte de l'identifiant, donc **stable d'une
  publication à l'autre** : le cache du navigateur survit aux mises à jour de données ;
- `data/meta.json` : versions archivées, statistiques d'appariement, libellés, filtres.

Usage :
    python scripts/build_site.py                  # depuis data/
    python scripts/build_site.py --from-fixtures  # depuis tests/fixtures/, sans réseau
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import repo
from lib.resolution import TRUSTED_LEVELS, resolve_record
from normalize import load_all_records

log = logging.getLogger("build_site")

DETAIL_BUCKETS = 64

SEARCH_FIELDS = (
    "id",
    "nom",
    "editeur",
    "cppap",
    "type",
    "dept",
    "ipg",
    "bucket",
    "confidence",
    # 1 inscrit, 0 non inscrit, -1 statut non précisé par la source.
    "inscrit",
)


def detail_bucket(record_id: str) -> int:
    """Lot de détail d'une fiche — dérivé de l'identifiant, donc stable entre versions."""
    digest = hashlib.sha1(record_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % DETAIL_BUCKETS


def load_sirene(from_fixtures: bool) -> dict[str, Any]:
    path = repo.FIXTURES / "sirene_cache.json" if from_fixtures else repo.SIRENE_CACHE_FILE
    cache = repo.read_json(path, default=None) or {}
    loaded = {
        "entries": cache.get("entries") or {},
        "record_entries": cache.get("record_entries") or {},
        "siren_entries": cache.get("siren_entries") or {},
        "publisher_siren": cache.get("publisher_siren") or {},
    }
    if not any(loaded.values()):
        log.warning(
            "Aucun appariement SIREN dans %s — les fiches seront publiées sans volet SIRENE. "
            "Lancez scripts/match_sirene.py pour l'alimenter.",
            path,
        )
    return loaded


def attach_sirene(record: dict[str, Any], sirene: dict[str, Any]) -> dict[str, Any] | None:
    """Rattache la résolution SIRENE retenue pour une fiche.

    L'ordre de priorité vit dans `lib/resolution.py`, partagé avec `match_sirene.py` : ses
    statistiques doivent décrire exactement ce que le site publie.
    """
    return resolve_record(record, sirene)


def source_context(manifest: dict[str, Any], config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pour chaque source : page data.gouv, licence et dernière version archivée."""
    context: dict[str, dict[str, Any]] = {}
    for source in config["sources"]:
        key = source["key"]
        entry = (manifest.get("sources") or {}).get(key) or {}
        versions = entry.get("versions") or []
        latest = versions[-1] if versions else {}
        context[key] = {
            "key": key,
            "type": source["type"],
            "label": source["label"],
            "label_plural": source["label_plural"],
            "dataset_page": entry.get("dataset_page")
            or f"https://www.data.gouv.fr/datasets/{source.get('dataset_slug', '')}",
            "license": entry.get("license"),
            "versions_archived": len(versions),
            "latest": {
                "observed_at": latest.get("observed_at"),
                "resource_last_modified": latest.get("resource_last_modified"),
                "resource_title": latest.get("resource_title"),
                "rows": latest.get("rows"),
                "content_sha8": latest.get("content_sha8"),
                "snapshot": latest.get("snapshot"),
            },
        }
    return context


def build_payloads(
    records: list[dict[str, Any]],
    sirene: dict[str, Any],
    departements: dict[str, str],
    sources: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[str, Any]]:
    """Construit l'index de recherche, les lots de détail et les statistiques."""
    rows: list[list[Any]] = []
    buckets: dict[int, dict[str, Any]] = {i: {} for i in range(DETAIL_BUCKETS)}

    by_type: dict[str, int] = {}
    dept_counts: dict[str, int] = {}
    confidence_counts: dict[str, int] = {}
    statut_counts: dict[str, int] = {}

    for record in records:
        resolution = attach_sirene(record, sirene)
        confidence = (resolution or {}).get("confidence", "aucun")
        bucket = detail_bucket(record["id"])

        rows.append(
            [
                record["id"],
                record["nom"],
                record["editeur"],
                record["cppap"],
                record["type"],
                record["departement"],
                1 if record["ipg"] else 0,
                bucket,
                confidence,
                -1 if record["inscrit"] is None else int(record["inscrit"]),
            ]
        )

        buckets[bucket][record["id"]] = slim_detail(record, resolution)

        by_type[record["type"]] = by_type.get(record["type"], 0) + 1
        statut_key = {True: "inscrit", False: "non_inscrit"}.get(record["inscrit"], "inconnu")
        statut_counts[statut_key] = statut_counts.get(statut_key, 0) + 1
        if record["departement"]:
            dept_counts[record["departement"]] = dept_counts.get(record["departement"], 0) + 1
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1

    search = {"fields": list(SEARCH_FIELDS), "rows": rows}
    stats = {
        "total": len(records),
        "by_type": by_type,
        "by_confidence": confidence_counts,
        "by_statut": statut_counts,
        "ipg": sum(1 for r in records if r["ipg"]),
        "departements": [
            {"code": code, "label": departements.get(code, code), "count": count}
            for code, count in sorted(dept_counts.items())
        ],
    }
    return search, buckets, stats


# Champs internes, ou dérivables côté navigateur depuis `meta.json` : les répéter sur chacune
# des 28 000 fiches représentait à lui seul un quart du poids des lots de détail.
# `type_label` se retrouve depuis `type` via `meta.types`, comme le libellé de département
# depuis `meta.stats.departements` et les liens de source depuis `meta.sources`.
_DROPPED_FIELDS = frozenset({"publisher_key", "type_label"})


def slim_detail(record: dict[str, Any], resolution: dict[str, Any] | None) -> dict[str, Any]:
    """Fiche allégée : ni champ vide, ni valeur redondante.

    Les libellés de type, de département et les liens de source sont reconstitués par
    `web/app.js` à partir de `meta.json`, qui les porte une seule fois.
    """
    detail = {
        key: value
        for key, value in record.items()
        if key not in _DROPPED_FIELDS
        # `False` et `0` sont des valeurs signifiantes : seules les chaînes vides, les listes
        # vides et `None` sont écartées.
        and value not in ("", None, [], {})
    }
    # Le libellé source du département ne sert que s'il n'a pas pu être normalisé en code.
    if detail.get("departement_source") == detail.get("departement"):
        detail.pop("departement_source", None)
    # Idem pour le SIRET brut, utile seulement s'il diffère de la forme normalisée.
    if detail.get("siret_source", "").replace(" ", "") == detail.get("siret", ""):
        detail.pop("siret_source", None)
    if resolution:
        detail["sirene"] = resolution
    return detail


def copy_web(destination: Path) -> None:
    """Recopie web/ dans le répertoire de build (le site n'a pas d'étape de compilation)."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(repo.WEB, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-fixtures",
        action="store_true",
        help="Construire depuis tests/fixtures/ (démonstration hors ligne)",
    )
    parser.add_argument("--out", type=Path, default=repo.SITE, help="Répertoire de sortie")
    parser.add_argument(
        "--sirene-proxy",
        default=os.environ.get("SIRENE_PROXY_URL", ""),
        help="URL du Worker de rafraîchissement SIRENE (facultatif)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    config = repo.load_config()
    data_dir = (repo.FIXTURES / "published") if args.from_fixtures else repo.DATA_LATEST
    if args.from_fixtures:
        log.warning("Mode fixtures : données SYNTHÉTIQUES, ne pas publier tel quel")

    records, reports = load_all_records(config, data_dir)
    if not records:
        log.error("Aucun enregistrement à publier — lancez d'abord scripts/ingest.py")
        return 1

    sirene = load_sirene(args.from_fixtures)
    manifest = repo.read_json(repo.MANIFEST_FILE, default={"sources": {}})
    departements = {
        k: v
        for k, v in repo.read_json(repo.ROOT / "config" / "departements.json").items()
        if not k.startswith("_")
    }
    labels = {
        k: v
        for k, v in repo.read_json(repo.ROOT / "config" / "labels.json").items()
        if not k.startswith("_")
    }

    sources = source_context(manifest, config)
    search, buckets, stats = build_payloads(records, sirene, departements, sources)

    site_config = repo.read_json(repo.WEB / "config.json", default={}) or {}
    proxy = args.sirene_proxy or site_config.get("sireneProxy") or ""

    meta = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fixtures": args.from_fixtures,
        "repository": "bthv1/cppap-recherche",
        "annuaire_base": repo.ANNUAIRE_ENTREPRISES,
        "sirene_proxy": proxy,
        "detail_buckets": DETAIL_BUCKETS,
        # Source unique de vérité pour « rattachement établi » : sans cela l'interface
        # redéfinit sa propre liste et finit par annoncer un taux faux.
        "confidence_trusted": sorted(TRUSTED_LEVELS),
        "sources": sources,
        "stats": stats,
        "labels": labels,
        "types": [
            {
                "key": s["key"],
                "type": s["type"],
                "label": s["label"],
                "label_plural": s["label_plural"],
            }
            for s in config["sources"]
        ],
        "schema_reports": {
            key: {
                "resolved": report.get("resolved"),
                "unresolved": report.get("unresolved"),
                "columns_kept_as_extra": report.get("unclaimed_columns"),
            }
            for key, report in reports.items()
            if "error" not in report
        },
    }

    copy_web(args.out)
    data_out = args.out / "data"
    repo.write_json(data_out / "search.json", search, compact=True)
    repo.write_json(data_out / "meta.json", meta)
    for index, payload in buckets.items():
        repo.write_json(data_out / "details" / f"{index}.json", payload, compact=True)

    search_size = (data_out / "search.json").stat().st_size
    details_size = sum(p.stat().st_size for p in (data_out / "details").glob("*.json"))
    log.info(
        "Site généré dans %s — %s fiches | index %.0f Ko | détails %.0f Ko sur %s lots",
        args.out,
        stats["total"],
        search_size / 1024,
        details_size / 1024,
        DETAIL_BUCKETS,
    )
    log.info(
        "Appariement SIREN : %s",
        ", ".join(f"{k}={v}" for k, v in sorted(stats["by_confidence"].items())) or "aucun",
    )
    if not proxy:
        log.info(
            "Aucun Worker SIRENE configuré — les fiches afficheront l'instantané archivé "
            "(comportement normal, le site reste complet)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
