"""Ordre de priorité du rattachement à SIRENE.

Ce module est la source unique de vérité partagée entre `match_sirene.py` et
`build_site.py` : ses règles décident de ce que chaque fiche affiche. Les tests figent donc
l'ordre exact, et surtout les cas où l'on refuse de trancher.
"""

import pytest
from lib.resolution import (
    LEVEL_ORDER,
    REVIEW_LEVELS,
    TRUSTED_LEVELS,
    build_publisher_siren_map,
    confidence_of,
    count_by_confidence,
    empty_cache,
    resolve_record,
)


def record(record_id="f1", publisher_key="ed|75", siren=""):
    return {"id": record_id, "publisher_key": publisher_key, "siren": siren}


@pytest.fixture
def cache():
    return {
        "record_entries": {
            "override-fiche": {"siren": "111111111", "confidence": "verifie"},
        },
        "entries": {
            "ed|75": {"siren": "222222222", "confidence": "certain"},
            "override-ed|75": {"siren": "333333333", "confidence": "verifie"},
        },
        "siren_entries": {
            "444444444": {"siren": "444444444", "confidence": "siret"},
            "555555555": {"siren": "555555555", "confidence": "siret_absent"},
        },
        "publisher_siren": {"override-ed|75": "444444444", "propage|75": "444444444"},
    }


# --------------------------------------------------------------------------------------
# Ordre de priorité
# --------------------------------------------------------------------------------------


def test_un_override_de_fiche_prime_sur_tout(cache):
    fiche = record("override-fiche", "ed|75", siren="444444444")
    assert resolve_record(fiche, cache)["siren"] == "111111111"


def test_un_override_d_editeur_prime_sur_le_siret_publie(cache):
    """Une décision humaine passe devant le SIRET du fichier, qui peut être erroné ou périmé."""
    fiche = record("f1", "override-ed|75", siren="444444444")
    resolution = resolve_record(fiche, cache)

    assert resolution["siren"] == "333333333"
    assert resolution["confidence"] == "verifie"


def test_le_siret_de_la_fiche_prime_sur_le_rapprochement_par_nom(cache):
    fiche = record("f1", "ed|75", siren="444444444")
    resolution = resolve_record(fiche, cache)

    assert resolution["confidence"] == "siret"
    assert resolution["siren"] == "444444444"


def test_la_propagation_prime_sur_le_rapprochement_par_nom(cache):
    """La fiche n'a pas de SIRET, mais son éditeur en déclare un dans une autre liste."""
    resolution = resolve_record(record("f1", "propage|75"), cache)

    assert resolution["siren"] == "444444444"
    assert resolution["confidence"] == "siret_propage"
    assert "autre fiche" in resolution["strategy"]


def test_la_propagation_ne_maquille_pas_un_siret_absent(cache):
    """Propager un SIRET dont l'entreprise est introuvable ne doit pas produire un faux succès."""
    cache["publisher_siren"]["absent|75"] = "555555555"
    resolution = resolve_record(record("f1", "absent|75"), cache)

    assert resolution["confidence"] == "siret_absent"


def test_le_rapprochement_par_nom_reste_le_dernier_recours(cache):
    resolution = resolve_record(record("f1", "ed|75"), cache)
    assert resolution["confidence"] == "certain"


def test_aucune_resolution_disponible(cache):
    assert resolve_record(record("f1", "inconnu|75"), cache) is None
    assert confidence_of(record("f1", "inconnu|75"), cache) == "aucun"


def test_resolve_record_supporte_un_cache_vide():
    assert resolve_record(record(), empty_cache()) is None


def test_un_siren_absent_du_cache_retombe_sur_l_editeur(cache):
    """SIRET publié mais pas encore interrogé : on n'invente pas, on prend ce qu'on a."""
    fiche = record("f1", "ed|75", siren="999999999")
    assert resolve_record(fiche, cache)["confidence"] == "certain"


# --------------------------------------------------------------------------------------
# Carte de propagation
# --------------------------------------------------------------------------------------


def test_build_publisher_siren_map_associe_un_siren_unanime():
    records = [
        {"publisher_key": "a|75", "siren": "111111111"},
        {"publisher_key": "a|75", "siren": "111111111"},
        {"publisher_key": "b|69", "siren": ""},
    ]
    assert build_publisher_siren_map(records) == {"a|75": "111111111"}


def test_build_publisher_siren_map_ecarte_les_siren_divergents():
    """Deux SIREN pour une même clé d'éditeur : on refuse de trancher au hasard.

    Retomber sur l'heuristique est préférable — elle exposera le doute au lecteur, là où un
    choix arbitraire l'aurait masqué.
    """
    records = [
        {"publisher_key": "a|75", "siren": "111111111"},
        {"publisher_key": "a|75", "siren": "222222222"},
    ]
    assert build_publisher_siren_map(records) == {}


def test_build_publisher_siren_map_ignore_les_cles_vides():
    records = [{"publisher_key": "", "siren": "111111111"}]
    assert build_publisher_siren_map(records) == {}


# --------------------------------------------------------------------------------------
# Décompte
# --------------------------------------------------------------------------------------


def test_count_by_confidence_compte_les_fiches_pas_les_editeurs(cache):
    """Un éditeur mal apparié publiant trois titres dégrade trois fiches, pas une."""
    records = [record(f"f{i}", "ed|75") for i in range(3)]
    records.append(record("f4", "propage|75"))

    assert count_by_confidence(records, cache) == {"certain": 3, "siret_propage": 1}


def test_count_by_confidence_sur_liste_vide(cache):
    assert count_by_confidence([], cache) == {}


# --------------------------------------------------------------------------------------
# Cohérence des ensembles de niveaux
# --------------------------------------------------------------------------------------


def test_les_niveaux_sont_classes_et_disjoints():
    assert TRUSTED_LEVELS.isdisjoint(REVIEW_LEVELS)
    # Tout niveau déclaré doit avoir une place dans l'ordre d'affichage.
    assert set(LEVEL_ORDER) >= TRUSTED_LEVELS | REVIEW_LEVELS
    assert len(LEVEL_ORDER) == len(set(LEVEL_ORDER))


def test_chaque_niveau_a_un_libelle_d_affichage():
    """Un niveau sans libellé s'afficherait sous sa clé technique dans l'interface."""
    from lib import repo

    labels = repo.read_json(repo.ROOT / "config" / "labels.json")["confidence"]
    for level in LEVEL_ORDER:
        assert level in labels, f"niveau {level} sans libellé dans config/labels.json"
        assert labels[level]["tone"] in {"ok", "warn", "risk", "none"}
