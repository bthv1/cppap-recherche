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
from functools import lru_cache
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import repo
from lib.cppap import CppapNumber, parse_cppap
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
    "statut",
    "date_expiration",
    "type_presse",
    "editeur",
    "forme_juridique",
    "departement",
    "commune",
    "qualification",
    "periodicite",
    "url",
    "date_decision",
    "nom_commercial",
    # `nom` en dernier : il capte les libellés génériques que rien de plus précis n'a pris.
    "nom",
)

# Valeurs de remplissage tenant lieu de case vide, sous leur forme pliée. Volontairement
# restrictif : « NA », « NC » ou « SO » pourraient être un nom de média réel, on n'y touche pas.
_PLACEHOLDERS = frozenset({"neant", "sans objet", "non renseigne", "non communique", "inconnu"})

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


def clean_value(value: str) -> str:
    """Vide les valeurs qui tiennent lieu de case vide.

    Deux cas : une valeur sans aucun caractère alphanumérique (« - », « -- », « / »), et une
    mention explicite d'absence. Sans cela, la colonne « Nom commercial » des agences de
    presse afficherait « - » pour la plupart d'entre elles.

    Les blancs internes sont réduits au passage : quelques cellules source portent un saut de
    ligne au milieu du titre (« Whart - Application\\nplication »), qui n'apporte rien et
    parasite l'indexation.
    """
    stripped = re.sub(r"\s+", " ", value or "").strip()
    if not stripped:
        return ""
    folded = fold(stripped)
    if not folded or folded in _PLACEHOLDERS:
        return ""
    return stripped


def is_inscrit(statut: str, implicite: str = "") -> bool | None:
    """Le média est-il effectivement inscrit ou reconnu ?

    Question décisive : la liste des publications de presse contient **quatre titres sur
    cinq qui ne sont pas inscrits**. Les présenter comme les autres laisserait croire à un
    agrément qui n'existe pas.

    Les listes des services de presse en ligne et des agences ne portent pas de colonne de
    statut : y figurer vaut reconnaissance, d'où le statut implicite déclaré par la source.
    Retourne None quand rien ne permet de conclure.
    """
    folded = fold(statut)
    if folded:
        if folded.startswith("non") or folded.startswith("pas "):
            return False
        if folded.startswith(("inscrit", "reconnu", "agree", "actif", "valide", "oui")):
            return True
        return None
    return True if implicite else None


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


@lru_cache(maxsize=1)
def _qualification_table() -> dict[str, Any]:
    """Table des qualifications, lue une fois — elle est consultée à chaque ligne."""
    return repo.load_labels().get("qualification", {})


def qualification_key(qualification: str, table: dict[str, Any] | None = None) -> str:
    """Ramène une qualification à une clé unique, quelle que soit la liste qui l'écrit.

    Les deux listes désignent la même chose autrement — « 39bisA » ici,
    « DISPOSITIF_FISCAL_39_BIS_A » là. Sans cette clé, un filtre par qualification
    proposerait deux entrées pour un même régime.

    Une écriture inconnue de `config/labels.json` reçoit une clé dérivée d'elle-même : elle
    reste filtrable et s'affiche telle quelle, plutôt que de disparaître silencieusement.
    """
    value = (qualification or "").strip()
    if not value:
        return ""

    folded = fold(value)
    for key, entry in (_qualification_table() if table is None else table).items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        if any(fold(candidate) == folded for candidate in entry.get("valeurs", ())):
            return key

    # « Non IPG », « Sans qualification », « Aucune » : la source énonce une **absence** de
    # qualification. En faire une clé ajouterait au filtre une qualification qui n'existe pas.
    if _IPG_NEGATION.search(folded):
        return ""
    # Repli : `is_ipg` reconnaît « Information politique et générale » sous ses variantes.
    if is_ipg(value):
        return "ipg"
    return slugify(value, fallback="")


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


def _record_id(source_type: str, number: CppapNumber, nom: str, editeur: str) -> str:
    """Identifiant stable et lisible, puisqu'il sert de lien partageable vers la fiche.

    Fondé sur le **n° d'inscription** et non sur l'écriture de la source : les deux listes
    écrivent le même numéro différemment (`1026 Y 90833` et `2590833`), et c'est ce qui
    permet à leurs deux fiches de porter d'emblée le même identifiant, donc de fusionner.
    Le n° d'inscription est aussi la partie permanente du numéro : contrairement à la forme
    complète, il ne change pas au renouvellement, donc le lien partagé ne se périme pas.
    """
    if number.serie:
        return f"cppap-{number.serie}"
    if number.raw:
        return _fallback_id(source_type, number.raw)
    # Les agences de presse n'ont pas de n° CPPAP : leur nom donne une URL bien plus
    # utilisable qu'une empreinte, et reste stable tant que le nom ne change pas.
    slug = slugify(nom or editeur, fallback="")
    if slug:
        return f"{source_type}-{slug[:80]}"
    digest = hashlib.sha1(f"{nom}|{editeur}".encode()).hexdigest()[:10]
    return f"{source_type}-h{digest}"


def _fallback_id(source_type: str, cppap: str) -> str:
    """Identifiant préfixé par la liste d'origine, donc unique même en cas de n° réattribué."""
    return f"{source_type}-{slugify(cppap)}"


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
        return clean_value(row[index])

    unclaimed_indexes = [
        i for i in range(len(header)) if i not in set(mapping.values()) and header[i]
    ]

    records: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    prefixes: dict[str, int] = {}
    formes: dict[str, int] = {}
    for row in data_rows:
        if not any(row):
            continue

        number = parse_cppap(cell(row, "cppap"))
        cppap = number.raw
        formes[number.forme] = formes.get(number.forme, 0) + 1
        if number.prefixe:
            prefixes[number.prefixe] = prefixes.get(number.prefixe, 0) + 1
        nom = cell(row, "nom")
        editeur = cell(row, "editeur")
        # Pour une agence de presse, la société *est* le média : une seule colonne porte les
        # deux, et une colonne ne peut être revendiquée qu'une fois.
        if not editeur and source.get("editeur_defaults_to_nom"):
            editeur = nom
        if not nom and not editeur:
            continue

        qualification = cell(row, "qualification")
        departement = normalize_departement(cell(row, "departement"))
        siret = normalize_siret(cell(row, "siret"))
        statut = cell(row, "statut") or source.get("statut_implicite", "")

        # La liste des services de presse en ligne ne porte aucune colonne d'expiration —
        # mais son numéro commence par le mois et l'année d'expiration. Sans ce décodage,
        # 126 reconnaissances déjà expirées s'affichent comme valides.
        date_expiration = cell(row, "date_expiration")
        date_expiration_origine = ""
        if not date_expiration and number.expiration:
            date_expiration = number.expiration
            date_expiration_origine = "cppap"

        record_id = _record_id(source["type"], number, nom, editeur)
        count = seen_ids.get(record_id, 0) + 1
        seen_ids[record_id] = count
        if count > 1:
            record_id = f"{record_id}-{count}"

        records.append(
            {
                "id": record_id,
                "type": source["type"],
                "types": [source["type"]],
                "source": source["key"],
                "sources": [source["key"]],
                "type_label": source["label"],
                # Écriture de la source, conservée telle quelle, et ses composants décodés :
                # les deux listes écrivent le même numéro différemment.
                "cppap": cppap,
                "cppap_serie": number.serie,
                "cppap_lettre": number.lettre,
                "cppap_prefixe": number.prefixe,
                "cppap_forme": number.forme,
                "cppap_ecritures": {source["key"]: cppap} if cppap else {},
                "nom": nom,
                "nom_commercial": cell(row, "nom_commercial"),
                "editeur": editeur,
                # Statut d'inscription : décisif, quatre publications sur cinq ne sont pas
                # inscrites. `inscrit` vaut None quand la source ne permet pas de conclure.
                "statut": statut,
                "inscrit": is_inscrit(statut, source.get("statut_implicite", "")),
                "date_expiration": date_expiration,
                # Vide quand la source publie la date ; « cppap » quand elle a été lue dans
                # le numéro lui-même, ce que la fiche signale au lecteur.
                "date_expiration_origine": date_expiration_origine,
                "type_presse": cell(row, "type_presse"),
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
                # Clé commune aux deux vocabulaires, sur laquelle porte le filtre.
                "qualification_cle": qualification_key(qualification),
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
    # Écritures du n° CPPAP rencontrées : c'est ce qui rend visible dans les journaux du
    # workflow un changement de format en amont, plutôt que de le laisser fausser les
    # rapprochements en silence.
    report["cppap_formes"] = dict(sorted(formes.items()))
    report["cppap_prefixes"] = dict(sorted(prefixes.items()))
    if len(prefixes) > 1:
        log.warning(
            "[%s] Plusieurs préfixes de n° CPPAP observés (%s) — le n° d'inscription n'est "
            "plus une clé sûre, le rapprochement entre listes est désactivé pour cette source.",
            source["key"],
            ", ".join(f"{k} ({v} fois)" for k, v in sorted(prefixes.items())),
        )
    return records, report


# --------------------------------------------------------------------------------------
# Fusion des fiches partageant un n° d'inscription
# --------------------------------------------------------------------------------------

# Champs dont la provenance est indissociable : le compagnon est repris du même membre que
# le champ meneur, faute de quoi une fiche annoncerait une date venue d'une liste et sa
# provenance venue d'une autre.
_COUPLED_FIELDS = {
    "date_expiration": ("date_expiration_origine",),
    "siret": ("siret_source", "siren"),
    "departement": ("departement_source",),
    "statut": ("inscrit",),
    "qualification": ("qualification_cle",),
}

# Champs recalculés par la fusion elle-même, jamais repris d'un membre.
_COMPUTED_FIELDS = frozenset(
    {
        "id",
        "type",
        "types",
        "source",
        "sources",
        "type_label",
        "cppap",
        "cppap_ecritures",
        "ipg",
        "extra",
    }
)

_EMPTY = ("", None, [], {})


def _rank(order: tuple[str, ...], key: str) -> int:
    return order.index(key) if key in order else len(order)


def _first(members: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    """Premier membre renseignant ce champ, dans l'ordre déjà trié des membres."""
    for member in members:
        if member.get(field) not in _EMPTY:
            return member
    return None


def _incoherence(members: list[dict[str, Any]]) -> str:
    """Raison de ne pas fusionner ce groupe, ou chaîne vide s'il est cohérent.

    Le groupe partage déjà un n° d'inscription **et** un éditeur : restent deux façons pour
    lui de ne pas décrire une seule inscription.
    """
    per_source: dict[str, int] = {}
    for member in members:
        per_source[member["source"]] = per_source.get(member["source"], 0) + 1
    repeated = [key for key, count in per_source.items() if count > 1]
    if repeated:
        return f"n° répété dans une même liste pour un même éditeur ({', '.join(sorted(repeated))})"

    # Seules les dates **publiées** par une source font autorité. Celle déduite du numéro est
    # une lecture du même numéro : la laisser opposer son veto priverait le lecteur d'une
    # fiche complète pour une divergence que la source elle-même n'affirme pas.
    expirations = {
        m["date_expiration"]
        for m in members
        if m["date_expiration"] and not m["date_expiration_origine"]
    }
    if len(expirations) > 1:
        return f"dates d'expiration différentes ({', '.join(sorted(expirations))})"

    return ""


def _merge_group(
    members: list[dict[str, Any]],
    value_priority: tuple[str, ...],
    display_priority: tuple[str, ...],
) -> dict[str, Any]:
    """Fusionne des fiches décrivant la même inscription, vue par plusieurs listes."""
    by_value = sorted(members, key=lambda r: _rank(value_priority, r["source"]))
    by_display = sorted(members, key=lambda r: _rank(display_priority, r["source"]))
    primary = by_display[0]

    merged: dict[str, Any] = {}
    fields = {key for member in members for key in member} - _COMPUTED_FIELDS
    companions = {c for group in _COUPLED_FIELDS.values() for c in group}

    for field in fields - companions:
        donor = _first(by_value, field)
        if donor is None:
            # Aucun membre ne renseigne ce champ : on garde la valeur du membre principal
            # pour que le schéma de la fiche reste celui des fiches non fusionnées.
            merged[field] = primary.get(field)
        else:
            merged[field] = donor[field]
        for companion in _COUPLED_FIELDS.get(field, ()):
            source_of_truth = donor or primary
            merged[companion] = source_of_truth.get(companion)

    merged["types"] = list(dict.fromkeys(m["type"] for m in by_display))
    merged["sources"] = list(dict.fromkeys(m["source"] for m in by_display))
    merged["type"] = merged["types"][0]
    merged["source"] = merged["sources"][0]
    merged["type_label"] = primary["type_label"]

    # La forme complète du numéro l'emporte pour l'affichage — c'est celle qui figure dans
    # l'ours d'un journal — mais les deux écritures sont conservées, pour que la recherche
    # aboutisse quelle que soit la source consultée par le lecteur.
    complete = next((m for m in by_display if m["cppap_forme"] == "complete"), None)
    reference = complete or _first(by_display, "cppap") or primary
    merged["cppap"] = reference["cppap"]
    merged["cppap_lettre"] = reference["cppap_lettre"]
    merged["cppap_forme"] = reference["cppap_forme"]
    merged["cppap_ecritures"] = {
        key: value for m in by_display for key, value in m["cppap_ecritures"].items()
    }

    # Une qualification portée par une seule des deux listes reste vraie : on la conserve.
    merged["ipg"] = any(m["ipg"] for m in members)

    merged["extra"] = {
        f"{m['source']} · {key}": value for m in by_display for key, value in m["extra"].items()
    }
    return merged


def merge_by_serie(
    records: list[dict[str, Any]], config: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Réunit en une fiche les inscriptions décrites par plusieurs listes CPPAP.

    Les 1 242 services de presse en ligne reconnus figurent **aussi** dans la liste des
    publications de presse, sous le même n° d'inscription écrit autrement. Publier les deux
    donnait deux fiches sans lien pour un même média, chacune amputée de ce que l'autre
    porte : le n° complet et l'URL d'un côté, le SIRET, le statut et les dates de l'autre.

    La clé de rapprochement est le couple **n° d'inscription + éditeur**, et non le seul
    numéro : un n° d'inscription **réattribué** existe dans les données réelles — le n° 90135
    est porté par trois titres, dont un dont l'inscription a expiré en 2015. L'éditeur dans la
    clé sépare naturellement ces cas, là où un contrôle a posteriori aurait dû renoncer à
    fusionner tout le groupe. Vérifié : l'éditeur est écrit identiquement des deux côtés sur
    les 1 241 titres communs, ce rapprochement n'en perd donc aucun.
    """
    merge_config = (config or {}).get("merge") or {}
    value_priority = tuple(merge_config.get("value_priority") or ())
    display_priority = tuple(merge_config.get("display_priority") or ())

    # Une source dont les numéros présentent plusieurs préfixes n'offre plus de clé sûre :
    # rien ne dit alors que deux n° d'inscription identiques désignent la même inscription.
    prefixes: dict[str, set[str]] = {}
    for record in records:
        if record["cppap_prefixe"]:
            prefixes.setdefault(record["source"], set()).add(record["cppap_prefixe"])
    blocked = {source for source, seen in prefixes.items() if len(seen) > 1}

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        serie = record["cppap_serie"]
        if serie and record["source"] not in blocked:
            groups.setdefault((serie, normalize_company(record["editeur"])), []).append(record)

    # Un n° d'inscription réparti sur plusieurs éditeurs est ambigu : ses fiches ne peuvent
    # pas se contenter du numéro comme identifiant.
    per_serie: dict[str, int] = {}
    for serie, _ in groups:
        per_serie[serie] = per_serie.get(serie, 0) + 1

    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    identity: dict[tuple[str, str], str] = {}
    rejected: list[dict[str, Any]] = []
    for key, members in groups.items():
        serie, editeur_key = key
        identity[key] = (
            f"cppap-{serie}"
            if per_serie[serie] == 1
            else f"cppap-{serie}-{slugify(editeur_key, fallback='x')[:40]}"
        )
        if len({m["source"] for m in members}) < 2:
            continue
        reason = _incoherence(members)
        if reason:
            rejected.append(
                {"serie": serie, "editeur": editeur_key, "raison": reason, "fiches": len(members)}
            )
            continue
        merged = _merge_group(members, value_priority, display_priority)
        merged["id"] = identity[key]
        resolved[key] = merged

    # Reconstruction dans l'ordre d'origine : la fiche fusionnée prend la place du premier
    # de ses membres rencontré, pour que la sortie reste déterministe entre deux exécutions.
    output: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()
    rejected_keys = {(r["serie"], r["editeur"]) for r in rejected}
    for record in records:
        key = (record["cppap_serie"], normalize_company(record["editeur"]))
        if key in resolved:
            if key not in emitted:
                output.append(resolved[key])
                emitted.add(key)
            continue
        if key in rejected_keys:
            # Le numéro ne peut plus servir d'identifiant : on retombe sur un identifiant
            # préfixé par la liste d'origine.
            record = {**record, "id": _fallback_id(record["type"], record["cppap"])}
        elif key in identity and len(groups[key]) == 1:
            record = {**record, "id": identity[key]}
        output.append(record)

    output, collisions = _deduplicate_ids(output)

    report = {
        "fusionnees": len(resolved),
        "fiches_avant": len(records),
        "fiches_apres": len(output),
        "groupes_rejetes": len(rejected),
        "rejets": rejected[:20],
        "numeros_ambigus": sorted({s for s, n in per_serie.items() if n > 1}),
        "sources_ecartees": sorted(blocked),
        "identifiants_desambigues": collisions,
    }
    if resolved:
        log.info(
            "Fusion par n° d'inscription : %s inscription(s) réunie(s), %s fiche(s) publiée(s) "
            "au lieu de %s",
            len(resolved),
            len(output),
            len(records),
        )
    for entry in rejected:
        log.warning(
            "Fusion refusée pour le n° %s / éditeur %r (%s fiches) : %s",
            entry["serie"],
            entry["editeur"],
            entry["fiches"],
            entry["raison"],
        )
    return output, report


def _deduplicate_ids(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Garantit l'unicité des identifiants sur l'ensemble des listes réunies.

    `build_records` ne voyait qu'une source à la fois ; depuis que l'identifiant dérive du
    n° d'inscription, une collision peut naître entre deux listes. Deux fiches partageant
    un identifiant se recouvriraient silencieusement dans les lots de détail du site.
    """
    seen: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    collisions = 0
    for record in records:
        count = seen.get(record["id"], 0) + 1
        seen[record["id"]] = count
        if count > 1:
            collisions += 1
            record = {**record, "id": f"{record['id']}-{count}"}
        output.append(record)
    if collisions:
        log.warning("%s identifiant(s) suffixé(s) pour rester unique(s)", collisions)
    return output, collisions


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
    config: dict[str, Any], data_dir: Path | None = None, *, merge: bool = True
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalise toutes les sources disponibles. Une source absente est signalée, non fatale.

    Les fiches d'une même inscription vues par plusieurs listes sont réunies (`merge_by_serie`)
    avant d'être rendues : `build_site.py` et `match_sirene.py` travaillent ainsi sur exactement
    les mêmes fiches, donc les statistiques d'appariement décrivent bien ce qui est publié.
    Le rapport de fusion est déposé sous la clé `_fusion`.
    """
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
    if merge:
        records, reports["_fusion"] = merge_by_serie(records, config)
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
