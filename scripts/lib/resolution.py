"""Ordre de priorité du rattachement d'une fiche à une entreprise SIRENE.

Source unique de vérité, partagée par `match_sirene.py` (pour son récapitulatif) et
`build_site.py` (pour ce qui est publié) : deux implémentations divergentes de cet ordre
donneraient des statistiques ne correspondant pas aux fiches réellement affichées.

Du plus fort au plus faible :

1. **override manuel de fiche** — une correction humaine relue en revue de code ;
2. **override manuel d'éditeur** — idem, mais valable pour tous ses titres ;
3. **SIRET publié sur la fiche** — jointure exacte, aucune heuristique : c'est le fichier
   officiel qui l'affirme ;
4. **SIREN propagé depuis une autre fiche du même éditeur** — les trois listes ne portent pas
   toutes un SIRET ; quand un éditeur en publie un dans l'une, on le réutilise pour ses fiches
   des autres listes. C'est une inférence, pas une affirmation de la source : elle a donc son
   propre niveau, distinct du SIRET direct ;
5. **rapprochement par le nom** — heuristique, avec niveau de confiance et candidats.
"""

from __future__ import annotations

from typing import Any

# Niveaux qui n'exigent aucune relecture humaine : décision humaine, SIRET publié,
# SIRET hérité d'une autre liste du même éditeur, ou rapprochement de nom sans ambiguïté.
TRUSTED_LEVELS = frozenset({"verifie", "siret", "siret_propage", "certain"})

# Niveaux à relire avant citation : soit l'heuristique doute, soit l'entreprise est absente.
REVIEW_LEVELS = frozenset({"siret_absent", "siret_non_verifie", "probable", "incertain", "aucun"})

# Ordre d'affichage des statistiques.
LEVEL_ORDER = (
    "verifie",
    "siret",
    "siret_propage",
    "certain",
    "probable",
    "siret_absent",
    "siret_non_verifie",
    "incertain",
    "aucun",
)


def empty_cache() -> dict[str, Any]:
    return {"entries": {}, "record_entries": {}, "siren_entries": {}, "publisher_siren": {}}


def build_publisher_siren_map(records: list[dict[str, Any]]) -> dict[str, str]:
    """Associe une clé d'éditeur au SIREN que ses fiches déclarent, quand il est unanime.

    Sert à faire bénéficier les listes sans colonne SIRET du SIRET publié dans une autre.
    Un éditeur dont les fiches déclarent des SIREN divergents est **écarté** : mieux vaut
    retomber sur l'heuristique, qui exposera le doute, que trancher au hasard.
    """
    seen: dict[str, set[str]] = {}
    for record in records:
        key, siren = record.get("publisher_key"), record.get("siren")
        if key and siren:
            seen.setdefault(key, set()).add(siren)
    return {key: next(iter(sirens)) for key, sirens in seen.items() if len(sirens) == 1}


def resolve_record(record: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any] | None:
    """Retourne la résolution SIRENE retenue pour une fiche, ou None si aucune."""
    forced = (cache.get("record_entries") or {}).get(record["id"])
    if forced:
        return forced

    publisher_key = record.get("publisher_key") or ""
    publisher = (cache.get("entries") or {}).get(publisher_key)
    # Un override d'éditeur est une décision humaine : il passe devant le SIRET publié, qui
    # peut lui-même être erroné ou périmé dans le fichier source.
    if publisher and publisher.get("confidence") == "verifie":
        return publisher

    by_siren = cache.get("siren_entries") or {}

    siren = record.get("siren")
    if siren and siren in by_siren:
        return by_siren[siren]

    # La fiche n'a pas de SIRET, mais l'éditeur en a déclaré un ailleurs.
    propagated = (cache.get("publisher_siren") or {}).get(publisher_key)
    if propagated and propagated in by_siren:
        entry = by_siren[propagated]
        if entry.get("confidence") == "siret":
            return {
                **entry,
                "confidence": "siret_propage",
                "strategy": "SIREN repris d'une autre fiche du même éditeur portant un SIRET",
            }
        return entry

    return publisher


def confidence_of(record: dict[str, Any], cache: dict[str, Any]) -> str:
    resolution = resolve_record(record, cache)
    return (resolution or {}).get("confidence", "aucun")


def count_by_confidence(records: list[dict[str, Any]], cache: dict[str, Any]) -> dict[str, int]:
    """Compte les fiches par niveau de confiance — et non les éditeurs.

    C'est le décompte qui compte pour un lecteur : un éditeur mal apparié qui publie
    quarante titres dégrade quarante fiches, pas une.
    """
    counts: dict[str, int] = {}
    for record in records:
        level = confidence_of(record, cache)
        counts[level] = counts.get(level, 0) + 1
    return counts
