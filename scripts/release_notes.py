#!/usr/bin/env python3
"""Rédige le corps de la Release GitHub d'une nouvelle version de données.

Une Release par version archivée, avec le **différentiel ligne à ligne** : c'est ce qui rend
une version citable dans un article — on peut renvoyer à l'état exact d'une liste à une date,
et voir ce qui a changé depuis la commission précédente.

Le différentiel se fait sur le n° CPPAP, en comparant l'instantané précédent (conservé dans
data/raw/) à la version courante (data/latest/).

Usage :
    python scripts/release_notes.py --changed spel,publications --out notes.md
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import repo
from lib.tabular import read_rows
from normalize import build_records

log = logging.getLogger("release_notes")

# Au-delà, la liste est tronquée — et la troncature est annoncée, jamais silencieuse.
MAX_LISTED = 40

# Champs dont une modification est signalée. `extra` est exclu : trop bruyant, et déjà
# visible dans le diff git du CSV.
COMPARED_FIELDS = (
    "nom",
    "editeur",
    "forme_juridique",
    "departement",
    "qualification",
    "periodicite",
    "url",
    "date_decision",
)


def load_records(path: Path, source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Charge un fichier et indexe ses enregistrements par identifiant."""
    rows, _ = read_rows(path.read_bytes(), path.name)
    records, _ = build_records(rows, source)
    return {record["id"]: record for record in records}


def describe(record: dict[str, Any]) -> str:
    bits = [f"`{record['cppap']}`" if record["cppap"] else "`sans n°`", record["nom"] or "sans nom"]
    if record["editeur"]:
        bits.append(f"({record['editeur']})")
    return " — ".join(bits[:2]) + (f" {bits[2]}" if len(bits) > 2 else "")


def diff_records(
    previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
) -> dict[str, list]:
    added = [current[k] for k in current.keys() - previous.keys()]
    removed = [previous[k] for k in previous.keys() - current.keys()]

    modified = []
    for key in current.keys() & previous.keys():
        changes = [
            (field, previous[key].get(field, ""), current[key].get(field, ""))
            for field in COMPARED_FIELDS
            if previous[key].get(field, "") != current[key].get(field, "")
        ]
        if changes:
            modified.append((current[key], changes))

    added.sort(key=lambda r: r["nom"])
    removed.sort(key=lambda r: r["nom"])
    modified.sort(key=lambda item: item[0]["nom"])
    return {"added": added, "removed": removed, "modified": modified}


def bullet_list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    shown = items[:MAX_LISTED]
    truncated = (
        f"\n\n_Liste tronquée : {len(shown)} entrées affichées sur {len(items)}. "
        "Le détail complet est dans le diff du commit._"
        if len(items) > len(shown)
        else ""
    )
    return (
        f"<details>\n<summary>{title} ({len(items)})</summary>\n\n"
        + "\n".join(f"- {item}" for item in shown)
        + truncated
        + "\n\n</details>\n"
    )


def source_section(source: dict[str, Any], manifest: dict[str, Any]) -> str:
    key = source["key"]
    entry = (manifest.get("sources") or {}).get(key) or {}
    versions = entry.get("versions") or []
    if not versions:
        return f"## {source['label_plural']}\n\n_Aucune version archivée._\n"

    latest = versions[-1]
    header = [
        f"## {source['label_plural']}",
        "",
        f"- **{latest.get('rows', '?')} lignes**, empreinte `{latest.get('content_sha8', '?')}`",
        f"- Ressource data.gouv.fr : {latest.get('resource_title') or 'sans titre'}"
        + (
            f", modifiée le {latest['resource_last_modified'][:10]}"
            if latest.get("resource_last_modified")
            else ""
        ),
        f"- Fichier archivé : `{latest.get('snapshot', '?')}`",
        f"- Colonnes : {', '.join(f'`{c}`' for c in latest.get('columns') or []) or 'inconnues'}",
        "",
    ]

    previous = versions[-2] if len(versions) > 1 else None
    if not previous:
        header.append("**Première version archivée** — pas de différentiel disponible.\n")
        return "\n".join(header)

    previous_path = repo.ROOT / (previous.get("snapshot") or "")
    current_path = repo.DATA_LATEST / f"{key}.csv"
    if not previous_path.exists() or not current_path.exists():
        header.append(
            f"_Différentiel indisponible : fichier manquant "
            f"({previous_path.name if not previous_path.exists() else current_path.name})._\n"
        )
        return "\n".join(header)

    try:
        delta = diff_records(
            load_records(previous_path, source), load_records(current_path, source)
        )
    except Exception as exc:
        log.warning("[%s] Différentiel impossible : %s", key, exc)
        header.append(f"_Différentiel impossible à calculer : {exc}._\n")
        return "\n".join(header)

    counts = (len(delta["added"]), len(delta["removed"]), len(delta["modified"]))
    header.append(
        f"**{counts[0]} ajout(s), {counts[1]} retrait(s), {counts[2]} modification(s)** "
        f"par rapport à la version du {previous.get('observed_at', '?')}.\n"
    )

    header.append(bullet_list("Titres ajoutés", [describe(r) for r in delta["added"]]))
    header.append(bullet_list("Titres retirés", [describe(r) for r in delta["removed"]]))
    header.append(
        bullet_list(
            "Titres modifiés",
            [
                f"{describe(record)}"
                + "".join(
                    f"<br>&nbsp;&nbsp;• {field} : « {before or '∅'} » → « {after or '∅'} »"
                    for field, before, after in changes
                )
                for record, changes in delta["modified"]
            ],
        )
    )
    return "\n".join(header)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", default="", help="Clés de sources modifiées, séparées par ,")
    parser.add_argument("--out", type=Path, help="Fichier de sortie (défaut : stdout)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    config = repo.load_config()
    manifest = repo.read_json(repo.MANIFEST_FILE, default={"sources": {}})

    wanted = {k.strip() for k in args.changed.split(",") if k.strip()}
    sources = [s for s in config["sources"] if not wanted or s["key"] in wanted]

    parts = [
        "Versions des listes CPPAP archivées dans ce dépôt à cette date.",
        "",
        "Les fichiers joints sont les **octets publiés par data.gouv.fr**, non retraités : "
        "c'est cette copie qui fait référence pour citer l'état d'une liste. "
        "`data/latest/<clé>.csv` en donne une vue normalisée (UTF-8, virgule) dont les diffs "
        "git sont lisibles.",
        "",
    ]
    parts.extend(source_section(source, manifest) for source in sources)

    cache = repo.read_json(repo.SIRENE_CACHE_FILE, default={}) or {}
    entries = (cache.get("entries") or {}).values()
    if entries:
        counts: dict[str, int] = {}
        for entry in entries:
            level = entry.get("confidence", "aucun")
            counts[level] = counts.get(level, 0) + 1
        total = sum(counts.values())
        parts.append("## Rattachement SIRENE\n")
        parts.append(
            f"{total} éditeurs uniques appariés à la base SIRENE : "
            + ", ".join(f"{level} = {counts[level]}" for level in sorted(counts))
            + ".\n"
        )
        parts.append(
            "Les fichiers CPPAP ne contiennent pas de numéro SIREN : ce rattachement est "
            "reconstitué par rapprochement de la raison sociale, et son niveau de confiance "
            "est affiché sur chaque fiche. Corrections manuelles possibles via "
            "`data/sirene/overrides.csv`.\n"
        )

    body = "\n".join(parts)
    if args.out:
        args.out.write_text(body, encoding="utf-8")
        log.info("Notes de version écrites dans %s (%s caractères)", args.out, len(body))
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
