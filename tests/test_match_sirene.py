"""Appariement éditeur -> SIREN : notation, seuils de confiance et overrides.

Aucun appel réseau : les réponses de l'API Recherche d'entreprises sont rejouées depuis
tests/fixtures/api/. Ce qui compte ici n'est pas de « trouver le bon SIREN » mais que le
niveau de confiance annoncé soit honnête — un homonyme proche doit ressortir en doute.
"""

import json

import pytest
from lib import repo
from lib.text import normalize_company
from match_sirene import (
    THRESHOLD_CERTAIN,
    classify,
    collect_publishers,
    extract_entreprise,
    load_overrides,
    name_similarity,
    resolve_override_targets,
    score_candidate,
)


@pytest.fixture(scope="module")
def candidats():
    payload = json.loads((repo.FIXTURES / "api" / "search_monde.json").read_text(encoding="utf-8"))
    return payload["results"]


def noter(candidats, editeur, departement="75", forme="SA"):
    """Retourne la liste (score, candidat, composantes) triée, comme le fait le résolveur."""
    editeur_norm = normalize_company(editeur)
    scored = []
    for candidate in candidats:
        score, parts = score_candidate(editeur_norm, departement, forme, candidate)
        scored.append((score, candidate, parts))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


# --------------------------------------------------------------------------------------
# Notation
# --------------------------------------------------------------------------------------


def test_name_similarity_reconnait_l_egalite_exacte(candidats):
    assert name_similarity(normalize_company("SOCIETE EDITRICE DU MONDE"), candidats[0]) == 1.0


def test_une_correspondance_exacte_avec_signaux_concordants_est_certaine(candidats):
    scored = noter(candidats, "SOCIETE EDITRICE DU MONDE")

    assert scored[0][1]["siren"] == "900000101"
    assert scored[0][0] >= THRESHOLD_CERTAIN
    assert classify(scored) == "certain"


def test_un_homonyme_proche_ne_doit_pas_etre_ecrase_par_les_bonus(candidats):
    """Le défaut à éviter : des bonus additifs écrêtés à 1.0 mettraient les deux à égalité.

    « SOCIETE EDITRICE DU MONDE » et « ... DU MONDE DIPLOMATIQUE » partagent département,
    état actif et nature juridique. Seule la similarité de nom les sépare : elle doit rester
    déterminante, sinon la marge de confiance ne distingue plus rien.
    """
    scored = noter(candidats, "SOCIETE EDITRICE DU MONDE")

    assert scored[0][0] > scored[1][0], "les deux candidats ne doivent pas être à égalité"
    assert scored[0][0] - scored[1][0] >= 0.10


def test_un_departement_discordant_degrade_la_confiance(candidats):
    """Nom identique mais département différent : probable, jamais certain."""
    scored = noter(candidats, "SOCIETE EDITRICE DU MONDE", departement="69")

    assert scored[0][1]["siren"] == "900000101"
    assert classify(scored) == "probable"


def test_un_departement_absent_de_la_source_ne_penalise_pas_a_tort(candidats):
    """Sans département côté CPPAP, le signal n'entre pas au dénominateur.

    C'est la source qui est incomplète, pas l'appariement qui est douteux : la confiance ne
    doit pas tomber au même niveau qu'un département contredit.
    """
    sans_dept = noter(candidats, "SOCIETE EDITRICE DU MONDE", departement="", forme="SA")
    discordant = noter(candidats, "SOCIETE EDITRICE DU MONDE", departement="69", forme="SA")

    assert sans_dept[0][0] >= discordant[0][0]


def test_un_editeur_sans_rapport_ne_donne_aucune_correspondance(candidats):
    scored = noter(candidats, "EDITIONS FANTOMES DU VAL PERDU", departement="23", forme="SARL")
    assert classify(scored) == "aucun"


def test_classify_sur_liste_vide():
    assert classify([]) == "aucun"


# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------


def test_extract_entreprise_conserve_le_siege_et_les_dirigeants(candidats):
    entreprise = extract_entreprise(candidats[0])

    assert entreprise["siren"] == "900000101"
    assert entreprise["siege"]["code_postal"] == "75013"
    assert entreprise["siege"]["libelle_commune"] == "PARIS 13"
    assert entreprise["dirigeants"][0]["nom"] == "DUPONT"
    assert entreprise["dirigeants_total"] == 1


def test_extract_entreprise_elague_les_valeurs_vides(candidats):
    entreprise = extract_entreprise(candidats[0])

    # `sigle` est nul dans la source : il ne doit pas alourdir le site publié.
    assert "sigle" not in entreprise
    # Les booléens faux de `complements` sont écartés, les vrais conservés.
    assert entreprise["complements"] == {
        "convention_collective_renseignee": True,
        "liste_idcc": ["1895"],
    }


def test_extract_entreprise_supporte_une_reponse_minimale():
    entreprise = extract_entreprise({"siren": "900000999"})

    assert entreprise["siren"] == "900000999"
    assert entreprise["siege"] == {}
    assert entreprise["dirigeants"] == []


# --------------------------------------------------------------------------------------
# Regroupement et overrides
# --------------------------------------------------------------------------------------


def test_collect_publishers_deduplique_les_editeurs():
    records = [
        {"publisher_key": "a|75", "editeur": "A", "departement": "75", "forme_juridique": "SA"},
        {"publisher_key": "a|75", "editeur": "A", "departement": "75", "forme_juridique": "SA"},
        {"publisher_key": "b|69", "editeur": "B", "departement": "69", "forme_juridique": ""},
        {"publisher_key": "", "editeur": "", "departement": "", "forme_juridique": ""},
    ]
    publishers = collect_publishers(records)

    assert set(publishers) == {"a|75", "b|69"}
    assert publishers["a|75"]["records"] == 2


def test_collect_publishers_complete_une_forme_juridique_manquante():
    records = [
        {"publisher_key": "a|75", "editeur": "A", "departement": "75", "forme_juridique": ""},
        {"publisher_key": "a|75", "editeur": "A", "departement": "75", "forme_juridique": "SAS"},
    ]
    assert collect_publishers(records)["a|75"]["forme_juridique"] == "SAS"


def test_load_overrides_rejette_un_siren_invalide(tmp_path):
    path = tmp_path / "overrides.csv"
    path.write_text(
        "cle,siren,note\n"
        "# commentaire ignoré,,\n"
        "bon|75,900 000 101,vérifié\n"
        "mauvais|75,12345,trop court\n",
        encoding="utf-8",
    )
    overrides = load_overrides(path)

    # Les espaces du SIREN sont tolérés, une longueur incorrecte est rejetée.
    assert overrides == {"bon|75": {"siren": "900000101", "note": "vérifié"}}


def test_load_overrides_sur_fichier_absent(tmp_path):
    assert load_overrides(tmp_path / "inexistant.csv") == {}


def test_un_override_de_fiche_prime_sur_un_override_d_editeur():
    """La clé la plus spécifique gagne : un éditeur peut être bien apparié pour un titre
    et mal pour un autre."""
    records = [
        {"id": "publication-x", "cppap": "0722 C 1", "publisher_key": "ed|75"},
        {"id": "publication-y", "cppap": "0722 C 2", "publisher_key": "ed|75"},
    ]
    overrides = {
        "ed|75": {"siren": "900000001", "note": "éditeur"},
        "0722 C 2": {"siren": "900000002", "note": "fiche"},
    }
    publisher_overrides, record_overrides = resolve_override_targets(records, overrides)

    assert publisher_overrides == {"ed|75": {"siren": "900000001", "note": "éditeur"}}
    assert record_overrides == {"publication-y": {"siren": "900000002", "note": "fiche"}}


def test_un_override_orphelin_est_signale(caplog):
    records = [{"id": "a", "cppap": "1", "publisher_key": "k|75"}]
    overrides = {"cle-inconnue": {"siren": "900000001", "note": ""}}

    with caplog.at_level("WARNING"):
        publisher_overrides, record_overrides = resolve_override_targets(records, overrides)

    assert not publisher_overrides and not record_overrides
    assert "cle-inconnue" in caplog.text
