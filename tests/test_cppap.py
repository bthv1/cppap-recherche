"""Lecture d'un numéro CPPAP, écrit différemment selon la liste qui le publie.

Le cas qui a motivé ce module : « 1026 Y 90833 » et « 2590833 » sont le même agrément.
"""

from __future__ import annotations

import pytest
from lib.cppap import month_end, parse_cppap, writings

# --------------------------------------------------------------------------------------
# Forme complète : MMAA lettre n° d'inscription
# --------------------------------------------------------------------------------------


def test_forme_complete_est_entierement_decomposee():
    number = parse_cppap("1026 Y 90833")

    assert number.forme == "complete"
    assert number.serie == "90833"
    assert number.lettre == "Y"
    # Le mois et l'année d'ouverture du numéro sont ceux de l'expiration de l'inscription.
    assert number.expiration == "2026-10-31"
    assert number.prefixe == ""
    assert number.is_joinable


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0330 W 95411", "0330 W 95411"),
        ("0330w95411", "0330 W 95411"),
        ("0330-W-95411", "0330 W 95411"),
        ("  0330   w   95411  ", "0330 W 95411"),
        # « n° » en tête de cellule n'appartient pas au numéro.
        ("n° 0330 W 95411", "0330 W 95411"),
    ],
)
def test_separateurs_et_casse_sont_uniformises(raw, expected):
    """Un numéro recopié à la main arrive avec n'importe quel séparateur.

    Ces cinq écritures doivent donner un même numéro rapprochable : sans quoi un simple
    changement de présentation en amont rendrait toute une liste irrapprochable.
    """
    number = parse_cppap(raw)

    assert number.raw == expected
    assert number.serie == "95411"


@pytest.mark.parametrize(
    ("raw", "expiration"),
    [
        ("0224 W 90001", "2024-02-29"),  # année bissextile
        ("0225 W 90001", "2025-02-28"),
        ("1231 Z 90001", "2031-12-31"),
        ("0130 X 90001", "2030-01-31"),
        ("0430 X 90001", "2030-04-30"),
    ],
)
def test_expiration_tombe_le_dernier_jour_du_mois(raw, expiration):
    assert parse_cppap(raw).expiration == expiration


@pytest.mark.parametrize("raw", ["0026 Y 90833", "1326 Y 90833"])
def test_mois_invalide_n_invente_aucune_expiration(raw):
    """Un mois hors 1-12 signale une écriture incomprise : on n'affirme pas de date."""
    number = parse_cppap(raw)

    assert number.expiration == ""
    # Le n° d'inscription, lui, reste exploitable.
    assert number.serie == "90833"


# --------------------------------------------------------------------------------------
# Forme de la liste des publications : préfixe constant + n° d'inscription
# --------------------------------------------------------------------------------------


def test_forme_prefixee_isole_le_numero_d_inscription():
    number = parse_cppap("2590833")

    assert number.forme == "serie_prefixee"
    assert number.serie == "90833"
    assert number.prefixe == "25"
    # Cette écriture ne porte ni lettre de rubrique ni date d'expiration : la liste des
    # publications les publie dans des colonnes distinctes.
    assert number.lettre == ""
    assert number.expiration == ""


def test_les_deux_ecritures_partagent_le_meme_numero_d_inscription():
    """Le fait constaté sur les fichiers réels, et toute la raison d'être du module."""
    assert parse_cppap("1026 Y 90833").serie == parse_cppap("2590833").serie == "90833"


def test_un_zero_initial_du_numero_d_inscription_est_conserve():
    """« 2500013 » porte le n° d'inscription 00013, pas 13."""
    assert parse_cppap("2500013").serie == "00013"


def test_numero_d_inscription_seul():
    number = parse_cppap("90833")

    assert number.forme == "serie"
    assert number.serie == "90833"
    assert number.prefixe == ""


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_valeur_vide(raw):
    number = parse_cppap(raw)

    assert number.raw == ""
    assert not number.is_joinable


@pytest.mark.parametrize(
    "raw",
    [
        "259083",  # six chiffres : ni forme complète, ni forme préfixée
        "25908330",  # huit chiffres
        "ABC",
        "0330 WX 95411",
    ],
)
def test_ecritures_non_reconnues_ne_sont_pas_rapprochables(raw):
    number = parse_cppap(raw)

    assert number.forme == "inconnue"
    assert number.serie == ""
    assert number.raw  # la valeur reste affichable telle que la source l'écrit


# --------------------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------------------


def test_month_end():
    assert month_end(2, 2024) == "2024-02-29"
    assert month_end(12, 2026) == "2026-12-31"


def test_writings_liste_les_ecritures_sans_doublon():
    assert writings(parse_cppap("1026 Y 90833")) == ["1026 Y 90833", "90833"]
    assert writings(parse_cppap("2590833")) == ["2590833", "90833"]
    # Un n° d'inscription seul ne s'écrit que d'une façon.
    assert writings(parse_cppap("90833")) == ["90833"]
