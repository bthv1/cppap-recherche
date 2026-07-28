#!/usr/bin/env python3
"""Ingestion et archivage des jeux Open Data CPPAP publiés sur data.gouv.fr.

Pour chaque source déclarée dans config/sources.json :

1. interroge l'API data.gouv.fr pour retrouver la ressource courante du jeu de données ;
2. télécharge le fichier publié tel quel — ce sont ces octets qui constituent l'archive
   citable, pas une reconstruction ;
3. en dérive une vue CSV canonique (UTF-8, virgule) versionnée dans data/latest/, dont les
   diffs git sont lisibles et que les scripts aval consomment ;
4. ne commite RIEN si le contenu normalisé n'a pas changé — les listes ne bougent qu'à
   chaque commission (~4 fois par an), le dépôt reste donc léger malgré un cron hebdomadaire ;
5. enregistre dans data/manifest.json la ressource retenue, les empreintes, le nombre de
   lignes et les en-têtes observés, ce qui rend toute dérive de schéma visible en diff.

Usage :
    python scripts/ingest.py                     # toutes les sources
    python scripts/ingest.py --source spel       # une seule
    python scripts/ingest.py --dry-run           # sans écriture
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import repo
from lib.http import HttpClient, HttpError
from lib.tabular import read_rows, rows_to_csv

log = logging.getLogger("ingest")

# Ressources annexes à ne jamais confondre avec le jeu de données lui-même.
EXCLUDE_TITLE = re.compile(
    r"documentation|notice|dictionnaire|m[ée]thodolog|licence|readme|sch[ée]ma",
    re.IGNORECASE,
)

PREFERRED_FORMATS = ("csv", "xlsx", "xls", "ods", "json")


# --------------------------------------------------------------------------------------
# data.gouv.fr
# --------------------------------------------------------------------------------------


def fetch_dataset(client: HttpClient, dataset: str) -> dict[str, Any]:
    """Métadonnées d'un jeu de données, par identifiant ou par slug."""
    payload = client.get_json(f"{repo.DATAGOUV_API}/datasets/{dataset}/")
    if not isinstance(payload, dict) or "resources" not in payload:
        raise HttpError(f"Réponse inattendue pour le jeu de données {dataset}")
    return payload


def _resource_sort_key(resource: dict[str, Any]) -> tuple[int, str]:
    fmt = (resource.get("format") or "").lower()
    rank = PREFERRED_FORMATS.index(fmt) if fmt in PREFERRED_FORMATS else len(PREFERRED_FORMATS)
    stamp = resource.get("last_modified") or resource.get("created_at") or ""
    # Format préféré d'abord, puis publication la plus récente.
    return (rank, stamp)


def select_resource(dataset_meta: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Choisit la ressource courante : format exploitable, la plus récemment publiée."""
    resources = [r for r in dataset_meta.get("resources", []) if r.get("url")]
    if not resources:
        raise HttpError(f"[{source['key']}] Le jeu de données ne publie aucune ressource")

    candidates = [r for r in resources if not EXCLUDE_TITLE.search(r.get("title") or "")]
    if not candidates:
        candidates = resources

    include = source.get("resource_include_pattern")
    if include:
        filtered = [r for r in candidates if re.search(include, r.get("title") or "", re.I)]
        if filtered:
            candidates = filtered
        else:
            log.warning(
                "[%s] Aucune ressource ne correspond à resource_include_pattern=%r "
                "— sélection sur l'ensemble des ressources",
                source["key"],
                include,
            )

    best_rank = min(_resource_sort_key(r)[0] for r in candidates)
    same_format = [r for r in candidates if _resource_sort_key(r)[0] == best_rank]
    chosen = max(same_format, key=lambda r: _resource_sort_key(r)[1])

    log.info(
        "[%s] Ressource retenue : %r (format=%s, modifiée=%s, id=%s) — %s candidate(s)",
        source["key"],
        chosen.get("title"),
        chosen.get("format"),
        chosen.get("last_modified"),
        chosen.get("id"),
        len(candidates),
    )
    return chosen


def fetch_profile(client: HttpClient, resource_id: str) -> dict[str, Any] | None:
    """Profil csv_detective de l'API tabulaire. Best effort : l'absence n'est pas fatale."""
    if not resource_id:
        return None
    try:
        payload = client.get_json(f"{repo.TABULAR_API}/resources/{resource_id}/profile/")
    except HttpError as exc:
        log.info("Profil tabulaire indisponible pour %s (%s) — ignoré", resource_id, exc)
        return None
    profile = (payload or {}).get("profile") or {}
    return {
        "total_lines": profile.get("total_lines"),
        "encoding": profile.get("encoding"),
        "separator": profile.get("separator"),
        "nb_duplicates": profile.get("nb_duplicates"),
        "indexes": (payload or {}).get("indexes"),
    }


def download_resource(client: HttpClient, resource: dict[str, Any]) -> tuple[bytes, str]:
    """Télécharge le fichier publié. Repli sur l'export CSV de l'API tabulaire."""
    url = resource["url"]
    try:
        return client.get_bytes(url), "resource-url"
    except HttpError as exc:
        resource_id = resource.get("id")
        if not resource_id:
            raise
        log.warning("Téléchargement direct impossible (%s) — repli sur l'API tabulaire", exc)
        data = client.get_bytes(f"{repo.TABULAR_API}/resources/{resource_id}/data/csv/")
        return data, "tabular-csv"


# --------------------------------------------------------------------------------------
# Archivage
# --------------------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resource_extension(resource: dict[str, Any], method: str) -> str:
    if method == "tabular-csv":
        return "csv"
    fmt = (resource.get("format") or "").lower().strip(". ")
    if fmt in {"csv", "xlsx", "xls", "ods", "json", "tsv"}:
        return fmt
    suffix = Path((resource.get("url") or "").split("?")[0]).suffix.lstrip(".").lower()
    return suffix or "dat"


def load_manifest() -> dict[str, Any]:
    manifest = repo.read_json(repo.MANIFEST_FILE, default=None)
    if not isinstance(manifest, dict) or "sources" not in manifest:
        return {"sources": {}}
    return manifest


def last_version(manifest: dict[str, Any], key: str) -> dict[str, Any] | None:
    versions = (manifest.get("sources", {}).get(key) or {}).get("versions") or []
    return versions[-1] if versions else None


def ingest_source(
    client: HttpClient,
    source: dict[str, Any],
    manifest: dict[str, Any],
    observed_at: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Ingère une source. Retourne un compte rendu, avec `changed` à True si nouvelle version."""
    key = source["key"]
    dataset_meta = fetch_dataset(client, source["dataset"])
    resource = select_resource(dataset_meta, source)

    raw, method = download_resource(client, resource)
    extension = resource_extension(resource, method)
    rows, fmt = read_rows(raw, f"file.{extension}")
    if not rows:
        raise HttpError(f"[{key}] Fichier téléchargé vide ou illisible ({resource.get('url')})")

    canonical_csv = rows_to_csv(rows)
    file_sha = sha256_hex(raw)
    content_sha = sha256_hex(canonical_csv.encode("utf-8"))

    previous = last_version(manifest, key)
    changed = not previous or previous.get("content_sha256") != content_sha

    header = rows[0]
    version = {
        "observed_at": observed_at,
        "resource_id": resource.get("id"),
        "resource_title": resource.get("title"),
        "resource_url": resource.get("url"),
        "resource_format": resource.get("format"),
        "resource_last_modified": resource.get("last_modified"),
        "download_method": method,
        "source_format": fmt,
        "file_sha256": file_sha,
        "content_sha256": content_sha,
        "content_sha8": content_sha[:8],
        "rows": len(rows) - 1,
        "columns": header,
        "profile": fetch_profile(client, resource.get("id") or ""),
    }

    report = {
        "key": key,
        "changed": changed,
        "rows": version["rows"],
        "columns": header,
        "content_sha8": version["content_sha8"],
        "resource_title": resource.get("title"),
        "resource_last_modified": resource.get("last_modified"),
        "dataset_id": dataset_meta.get("id"),
        "dataset_page": dataset_meta.get("page"),
    }

    profile_total = (version["profile"] or {}).get("total_lines")
    if profile_total and abs(profile_total - version["rows"]) > 1:
        log.warning(
            "[%s] %s lignes lues mais le profil tabulaire en annonce %s — "
            "fichier tronqué ou profil obsolète, à vérifier",
            key,
            version["rows"],
            profile_total,
        )
        report["row_count_mismatch"] = profile_total

    if not changed:
        log.info("[%s] Contenu inchangé (%s lignes) — aucune écriture", key, version["rows"])
        return report

    snapshot_rel = f"data/raw/{key}/{observed_at}__{version['content_sha8']}.{extension}"
    version["snapshot"] = snapshot_rel
    report["snapshot"] = snapshot_rel

    log.info(
        "[%s] NOUVELLE VERSION : %s lignes, %s colonnes, empreinte %s",
        key,
        version["rows"],
        len(header),
        version["content_sha8"],
    )

    if dry_run:
        log.info("[%s] --dry-run : écritures ignorées", key)
        return report

    snapshot_path = repo.ROOT / snapshot_rel
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(raw)

    latest_path = repo.DATA_LATEST / f"{key}.csv"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(canonical_csv, encoding="utf-8")

    entry = manifest["sources"].setdefault(key, {})
    entry["type"] = source["type"]
    entry["label"] = source["label"]
    entry["dataset_id"] = dataset_meta.get("id")
    entry["dataset_slug"] = dataset_meta.get("slug") or source.get("dataset_slug")
    entry["dataset_page"] = dataset_meta.get("page")
    entry["license"] = dataset_meta.get("license")
    entry.setdefault("versions", []).append(version)

    return report


# --------------------------------------------------------------------------------------
# Entrée
# --------------------------------------------------------------------------------------


def emit_github_output(reports: list[dict[str, Any]]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    changed = [r["key"] for r in reports if r["changed"]]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"changed={','.join(changed)}\n")
        handle.write(f"changed_count={len(changed)}\n")
        handle.write(f"any_changed={'true' if changed else 'false'}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", help="Limiter à ces clés de source")
    parser.add_argument("--dry-run", action="store_true", help="Ne rien écrire sur disque")
    parser.add_argument(
        "--observed-at",
        default=datetime.now(UTC).date().isoformat(),
        help="Date d'observation (AAAA-MM-JJ), pour des exécutions reproductibles",
    )
    parser.add_argument("--report", type=Path, help="Écrire le compte rendu JSON dans ce fichier")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    config = repo.load_config()
    sources = config["sources"]
    if args.source:
        wanted = set(args.source)
        sources = [s for s in sources if s["key"] in wanted]
        unknown = wanted - {s["key"] for s in config["sources"]}
        if unknown:
            parser.error(f"Sources inconnues : {', '.join(sorted(unknown))}")

    client = HttpClient(user_agent=config["user_agent"], per_second=2.0)
    manifest = load_manifest()

    reports: list[dict[str, Any]] = []
    failures: list[str] = []
    for source in sources:
        try:
            reports.append(
                ingest_source(client, source, manifest, args.observed_at, dry_run=args.dry_run)
            )
        except (HttpError, ValueError) as exc:
            # Une source en échec ne doit pas empêcher les autres d'être archivées.
            log.error("[%s] Échec de l'ingestion : %s", source["key"], exc)
            failures.append(f"{source['key']}: {exc}")

    changed = [r["key"] for r in reports if r["changed"]]
    if changed and not args.dry_run:
        repo.write_json(repo.MANIFEST_FILE, manifest)

    log.info(
        "Terminé — %s source(s) traitée(s), %s nouvelle(s) version(s)%s",
        len(reports),
        len(changed),
        f" : {', '.join(changed)}" if changed else "",
    )

    summary = {"observed_at": args.observed_at, "reports": reports, "failures": failures}
    if args.report:
        repo.write_json(args.report, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    emit_github_output(reports)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
