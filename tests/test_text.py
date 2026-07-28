"""Normalisation de texte et similarité — la base du rapprochement CPPAP <-> SIRENE."""

import pytest
from lib.text import (
    fold,
    normalize_company,
    normalize_header,
    slugify,
    strip_accents,
    strip_legal_form,
    token_set_ratio,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Arrêt sur images", "arret sur images"),
        ("N° CPPAP", "n cppap"),
        ("Département du siège", "departement du siege"),
        ("Côtes-d'Armor", "cotes d armor"),
        ("Éditions MARÉCHAL", "editions marechal"),
        ("  espaces   multiples  ", "espaces multiples"),
        ("", ""),
        (None, ""),
    ],
)
def test_fold(raw, expected):
    assert fold(raw) == expected


def test_strip_accents_deplie_les_ligatures():
    # NFKD ne décompose pas œ/æ : le module les traite explicitement.
    assert strip_accents("Œuvre") == "OEuvre"
    assert strip_accents("Cæsar") == "Caesar"
    assert strip_accents("Straße") == "Strasse"


def test_normalize_header_est_un_alias_de_fold():
    assert normalize_header("Périodicité") == fold("Périodicité")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sarl le monde", "le monde"),
        ("le monde sarl", "le monde"),
        ("sa ouest france", "ouest france"),
        ("association le poulpe", "le poulpe"),
        ("telerama sa", "telerama"),
        # Marqueur au milieu : conservé, il peut être significatif.
        ("groupe sa presse", "groupe sa presse"),
        # Chaîne entièrement composée d'un marqueur : on garde l'original plutôt que rien.
        ("sarl", "sarl"),
    ],
)
def test_strip_legal_form(raw, expected):
    assert strip_legal_form(raw) == expected


def test_normalize_company_enchaine_pliage_et_forme_juridique():
    assert normalize_company("SARL LA HULOTTE") == "la hulotte"
    assert normalize_company("LES ÉDITIONS MARÉCHAL - LE CANARD ENCHAÎNÉ") == (
        "les editions marechal le canard enchaine"
    )
    assert normalize_company(None) == ""


def test_token_set_ratio_identiques_et_ordre_libre():
    assert token_set_ratio("le monde", "le monde") == 1.0
    assert token_set_ratio("monde le", "le monde") == 1.0


def test_token_set_ratio_penalise_les_tokens_en_trop():
    """Le piège que la mesure doit éviter : « X » et « X Y » ne sont pas la même société.

    Un `token_set_ratio` classique renverrait 1.0 ici, ce qui ferait passer une filiale
    homonyme pour une correspondance certaine.
    """
    score = token_set_ratio("le monde", "le monde interactif")
    assert 0.6 < score < 0.85


def test_token_set_ratio_reste_eleve_sur_un_mot_manquant():
    assert token_set_ratio("mediacites lyon", "mediacites") > 0.8


def test_token_set_ratio_bas_sur_des_noms_etrangers():
    assert token_set_ratio("editions fantomes du val perdu", "societe editrice du monde") < 0.6


@pytest.mark.parametrize(("raw", "expected"), [("", ""), ("x", ""), (None, "")])
def test_token_set_ratio_gere_le_vide(raw, expected):
    assert token_set_ratio(fold(raw), "") == 0.0


def test_slugify():
    assert slugify("0620 W 91234") == "0620-w-91234"
    assert slugify("Arrêt sur images") == "arret-sur-images"
    assert slugify("", fallback="vide") == "vide"
