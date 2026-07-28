#!/usr/bin/env python3
"""Transformation des CSV CPPAP en enregistrements canoniques.

Les en-têtes de ces fichiers ne sont pas stables dans le temps : la CPPAP a déjà renommé
« IPG » en « Qualification » (2019) et précisé la situation géographique de l'éditeur (2020).
L'appariement des colonnes est donc déclaratif — config/sources.json liste, pour chaque champ
canonique, les libellés plausibles — et se fait en deux passes sur en-tête normalisé :

1. égalité stricte de l'en-tête normalisé avec un alias ;
2. inclusion de l'alias dans l'en-tête, au mot entier, pour absorber les libellés rallongés
   du type « Département du siège social de l'entreprise éditrice ».

Toute colonne non reconnue est conservée dans `extra` : aucune donnée source n'est perdue,
même sans alias correspondant. À l'inverse, l'absence d'un champ requis lève une erreur
affichant les en-têtes observés — mieux vaut alerter que publier des fiches vides.

Usage :
    python scripts/normalize.py --source spel --limit 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import repo
from lib.tabular import read_rows
from lib.text import fold, normalize_company, normalize_header, slugify

log = logging.getLogger("normalize")

# Ordre de résolution volontaire : `editeur` avant `nom` pour que « Dénomination sociale »
# soit revendiqué par l'éditeur, et `nom` en dernier pour qu'il capte les libellés
# génériques restants (« Titre », « Nom »).
FIELD_ORDER = (
    # `siret` en premier : c'est l'identifiant le plus fort du fichier, et son alias
    # (« SIRET ») ne peut être confondu avec aucun autre champ.
    "siret",
    "cppap",
    "editeur",
    "forme_juridique",
    "departement",
    "commune",
    "qualification",
    "periodicite",
    "url",
    "date_decision",
    "nom",
)

_DEPARTEMENT = re.compile(r"^(2[ab]|\d{2,3})")
# Appliqués à une valeur déjà passée par `fold` : « Information politique et générale »
# devient « information politique et generale ».
_IPG = re.compile(r"\bipg\b|information politique et generale")
_IPG_NEGATION = re.compile(r"\b(non|sans|hors|pas|aucune)\b")


class SchemaError(RuntimeError):
    """Un champ requis n'a pu être associé à aucune colonne du fichier."""


# --------------------------------------------------------------------------------------
# Appariement des colonnes
# --------------------------------------------------------------------------------------


def _eligible_for_containment(alias: str) -> bool:
    """Un alias trop court ou trop générique ne doit pas servir en passe d'inclusion.

    Sans ce garde-fou, l'alias « nom » capterait « Nom de la commune ».
    """
    return len(alias.split()) >= 2 or len(alias) >= 6


def map_columns(
    header: list[str], columns_config: dict[str, list[str]]
) -> tuple[dict[str, int], dict[str, Any]]:
    """Associe chaque champ canonique à un index de colonne.

    Retourne (mapping, rapport). Le rapport documente comment chaque champ a été résolu,
    ce qui rend l'appariement vérifiable dans les journaux du workflow.
    """
    normalized = [normalize_header(h) for h in header]
    claimed: set[int] = set()
    mapping: dict[str, int] = {}
    how: dict[str, str] = {}

    fields = [f for f in FIELD_ORDER if f in columns_config]
    fields += [f for f in columns_config if f not in FIELD_ORDER]

    # Passe 1 : égalité stricte.
    for field in fields:
        for alias in columns_config[field]:
            match = next(
                (i for i, value in enumerate(normalized) if i not in claimed and value == alias),
                None,
            )
            if match is not None:
                mapping[field] = match
                claimed.add(match)
                how[field] = f"exact:{header[match]!r}"
                break

    # Passe 2 : inclusion au mot entier, alias les plus spécifiques d'abord.
    for field in fields:
        if field in mapping:
            continue
        aliases = sorted(
            (a for a in columns_config[field] if _eligible_for_containment(a)),
            key=len,
            reverse=True,
        )
        for alias in aliases:
            needle = f" {alias} "
            match = next(
                (
                    i
                    for i, value in enumerate(normalized)
                    if i not in claimed and needle in f" {value} "
                ),
                None,
            )
            if match is not None:
                mapping[field] = match
                claimed.add(match)
                how[field] = f"inclusion:{header[match]!r} (alias {alias!r})"
                break

    report = {
        "resolved": how,
        "unresolved": [f for f in fields if f not in mapping],
        "unclaimed_columns": [header[i] for i in range(len(header)) if i not in claimed],
        "header": header,
    }
    return mapping, report


# --------------------------------------------------------------------------------------
# Valeurs
# --------------------------------------------------------------------------------------


def normalize_departement(value: str) -> str:
    """Ramène un département à son code INSEE (« 075 » -> « 75 », « 2a » -> « 2A »)."""
    folded = fold(value).replace(" ", "")
    match = _DEPARTEMENT.match(folded)
    if not match:
        return ""
    code = match.group(1).upper()
    if len(code) == 3 and code.startswith("0"):
        return code[1:]
    return code


def normalize_siret(value: str) -> str:
    """Ramène un SIRET à ses 14 chiffres, ou chaîne vide s'il est inexploitable.

    Les tableurs mutilent volontiers ces numéros : espaces de groupage, points, et surtout
    zéro initial perdu lors d'un passage par une colonne numérique. On restitue ce zéro
    quand il ne manque qu'un chiffre — le cas est fréquent et sans ambiguïté.
    """
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if len(digits) == 13:
        digits = f"0{digits}"
    return digits if len(digits) == 14 else ""


def siren_from_siret(siret: str) -> str:
    """Les 9 premiers chiffres d'un SIRET identifient l'unité légale."""
    return siret[:9] if len(siret) == 14 else ""


def normalize_cppap(value: str) -> str:
    """Uniformise l'écriture d'un numéro CPPAP en séparant ses groupes par une espace."""
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", (value or "").strip()).strip()
    return re.sub(r"\s+", " ", cleaned).upper()


def is_ipg(qualification: str) -> bool:
    """Vrai si la qualification correspond à « information politique et générale ».

    Deux pièges que la comparaison naïve rate : la valeur est accentuée dans la source
    (« générale »), et « Non IPG » contient le sigle tout en signifiant l'inverse.
    """
    folded = fold(qualification)
    if not folded or _IPG_NEGATION.search(folded):
        return False
    return bool(_IPG.search(folded))


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = f"https://{value.lstrip('/')}"
    return value


# --------------------------------------------------------------------------------------
# Enregistrements
# --------------------------------------------------------------------------------------


def _record_id(source_type: str, cppap: str, nom: str, editeur: str) -> str:
    if cppap:
        return f"{source_type}-{slugify(cppap)}"
    digest = hashlib.sha1(f"{nom}|{editeur}".encode()).hexdigest()[:10]
    return f"{source_type}-h{digest}"


def build_records(
    rows: list[list[str]], source: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Construit les enregistrements canoniques d'une source à partir des lignes brutes."""
    if not rows:
        raise SchemaError(f"[{source['key']}] Fichier vide")

    header, data_rows = rows[0], rows[1:]
    mapping, report = map_columns(header, source["columns"])

    missing = [field for field in source.get("required", []) if field not in mapping]
    if missing:
        raise SchemaError(
            f"[{source['key']}] Champ(s) requis introuvable(s) : {', '.join(missing)}.\n"
            f"En-têtes observés dans le fichier : {header}\n"
            f"Alias attendus : "
            f"{ {f: source['columns'].get(f, []) for f in missing} }\n"
            "Corrigez config/sources.json en ajoutant l'alias manquant."
        )

    def cell(row: list[str], field: str) -> str:
        index = mapping.get(field)
        if index is None or index >= len(row):
            return ""
        return (row[index] or "").strip()

    unclaimed_indexes = [
        i for i in range(len(header)) if i not in set(mapping.values()) and header[i]
    ]

    records: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for row in data_rows:
        if not any(row):
            continue

        cppap = normalize_cppap(cell(row, "cppap"))
        nom = cell(row, "nom")
        editeur = cell(row, "editeur")
        if not nom and not editeur:
            continue

        qualification = cell(row, "qualification")
        departement = normalize_departement(cell(row, "departement"))
        siret = normalize_siret(cell(row, "siret"))

        record_id = _record_id(source["type"], cppap, nom, editeur)
        count = seen_ids.get(record_id, 0) + 1
        seen_ids[record_id] = count
        if count > 1:
            record_id = f"{record_id}-{count}"

        records.append(
            {
                "id": record_id,
                "type": source["type"],
                "source": source["key"],
                "type_label": source["label"],
                "cppap": cppap,
                "nom": nom,
                "editeur": editeur,
                # SIRET déclaré dans le fichier officiel, quand la source le fournit : il
                # remplace le rapprochement par nom par une jointure exacte sur SIREN.
                "siret": siret,
                "siret_source": cell(row, "siret"),
                "siren": siren_from_siret(siret),
                "forme_juridique": cell(row, "forme_juridique"),
                "departement": departement,
                "departement_source": cell(row, "departement"),
                "commune": cell(row, "commune"),
                "qualification": qualification,
                "ipg": is_ipg(qualification),
                "periodicite": cell(row, "periodicite"),
                "url": normalize_url(cell(row, "url")),
                "date_decision": cell(row, "date_decision"),
                "publisher_key": publisher_key(editeur, departement),
                "extra": {
                    header[i]: (row[i] or "").strip()
                    for i in unclaimed_indexes
                    if i < len(row) and (row[i] or "").strip()
                },
            }
        )

    duplicates = sum(count - 1 for count in seen_ids.values() if count > 1)
    if duplicates:
        log.warning(
            "[%s] %s identifiant(s) dupliqué(s) suffixé(s) — n° CPPAP répété dans la source",
            source["key"],
            duplicates,
        )

    report["records"] = len(records)
    report["duplicate_ids"] = duplicates
    return records, report


def publisher_key(editeur: str, departement: str) -> str:
    """Clé de déduplication pour l'appariement SIREN.

    Un même éditeur publie souvent plusieurs titres : regrouper sur cette clé réduit
    fortement le nombre d'appels à l'API Recherche d'entreprises.
    """
    normalized = normalize_company(editeur)
    if not normalized:
        return ""
    return f"{normalized}|{departement}"


def load_source_records(source: dict[str, Any], csv_path: Path | None = None) -> tuple:
    """Charge et normalise une source depuis data/latest/<clé>.csv.

    Passe par `read_rows` plutôt que par `csv.reader` directement : le fichier canonique est
    en UTF-8 avec virgule, mais les fixtures et les fichiers publiés utilisent le
    point-virgule et d'autres encodages. La détection évite un chemin de lecture par cas.
    """
    path = csv_path or (repo.DATA_LATEST / f"{source['key']}.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"[{source['key']}] {path} absent — lancez d'abord scripts/ingest.py"
        )
    rows, _ = read_rows(path.read_bytes(), path.name)
    return build_records(rows, source)


def load_all_records(
    config: dict[str, Any], data_dir: Path | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalise toutes les sources disponibles. Une source absente est signalée, non fatale."""
    records: list[dict[str, Any]] = []
    reports: dict[str, Any] = {}
    for source in config["sources"]:
        path = (data_dir / f"{source['key']}.csv") if data_dir else None
        try:
            source_records, report = load_source_records(source, path)
        except FileNotFoundError as exc:
            log.warning("%s", exc)
            reports[source["key"]] = {"error": str(exc)}
            continue
        records.extend(source_records)
        reports[source["key"]] = report
        log.info(
            "[%s] %s enregistrement(s) — champs résolus : %s%s",
            source["key"],
            len(source_records),
            ", ".join(sorted(report["resolved"])),
            f" | non résolus : {', '.join(report['unresolved'])}" if report["unresolved"] else "",
        )
        if report["unclaimed_columns"]:
            log.info(
                "[%s] Colonnes conservées dans `extra` : %s",
                source["key"],
                ", ".join(report["unclaimed_columns"]),
            )
    return records, reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", help="Limiter à ces clés de source")
    parser.add_argument("--data-dir", type=Path, help="Répertoire des CSV (défaut : data/latest)")
    parser.add_argument("--limit", type=int, default=0, help="Afficher N enregistrements")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    config = repo.load_config()
    if args.source:
        wanted = set(args.source)
        config = {**config, "sources": [s for s in config["sources"] if s["key"] in wanted]}

    records, reports = load_all_records(config, args.data_dir)
    print(json.dumps({"total": len(records), "reports": reports}, ensure_ascii=False, indent=2))
    for record in records[: args.limit]:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
