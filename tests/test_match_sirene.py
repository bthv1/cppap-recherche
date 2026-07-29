"""Appariement éditeur -> SIREN : notation, seuils de confiance et overrides.

Aucun appel réseau : les réponses de l'API Recherche d'entreprises sont rejouées depuis
tests/fixtures/api/. Ce qui compte ici n'est pas de « trouver le bon SIREN » mais que le
niveau de confiance annoncé soit honnête — un homonyme proche doit ressortir en doute.
"""

import json

import pytest
from lib import repo
from lib.http import HttpError
from lib.text import normalize_company
from match_sirene import (
    IMPLAUSIBLE_SAMPLE,
    THRESHOLD_CERTAIN,
    SearchFailed,
    classify,
    collect_publishers,
    collect_sirens,
    extract_entreprise,
    fetch_by_siren,
    load_overrides,
    name_similarity,
    resolve_by_siren,
    resolve_override_targets,
    score_candidate,
    search,
    search_or_empty,
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


# --------------------------------------------------------------------------------------
# Jointure exacte par SIRET
# --------------------------------------------------------------------------------------


def test_collect_sirens_deduplique_par_entreprise():
    """Un SIREN = un appel d'API, même s'il couvre plusieurs titres."""
    records = [
        {"siren": "900000101", "siret": "90000010100017"},
        {"siren": "900000101", "siret": "90000010100017"},
        {"siren": "900000107", "siret": "90000010700014"},
        {"siren": "", "siret": ""},
    ]
    sirens = collect_sirens(records)

    assert set(sirens) == {"900000101", "900000107"}
    assert sirens["900000101"]["records"] == 2
    assert sirens["900000101"]["siret"] == "90000010100017"


def test_collect_sirens_sur_une_source_sans_siret():
    assert collect_sirens([{"siren": "", "siret": ""}]) == {}


def test_resolve_by_siren_marque_un_siret_absent_de_l_api():
    """L'entreprise est absente de l'API, mais le SIRET reste officiel.

    Ce cas doit se distinguer d'un échec d'appariement : confondre les deux ferait douter
    d'une donnée qui, elle, vient du fichier de la CPPAP.
    """

    class ClientVide:
        def get_json(self, url, params=None):
            return {"results": []}

    resolution = resolve_by_siren(ClientVide(), "999000000", "99900000000018", "2026-07-28")

    assert resolution["confidence"] == "siret_absent"
    assert resolution["siren"] == "999000000"
    assert resolution["siret_declare"] == "99900000000018"
    assert "entreprise" not in resolution


def test_resolve_by_siren_signale_un_etablissement_qui_n_est_pas_le_siege(candidats):
    """Le SIRET déclaré à la CPPAP désigne un établissement, pas forcément le siège."""

    class ClientFixe:
        def __init__(self, result):
            self.result = result

        def get_json(self, url, params=None):
            return {"results": [self.result]}

    siege_siret = candidats[0]["siege"]["siret"]
    au_siege = resolve_by_siren(ClientFixe(candidats[0]), "900000101", siege_siret, "2026-07-28")
    ailleurs = resolve_by_siren(
        ClientFixe(candidats[0]), "900000101", "90000010199999", "2026-07-28"
    )

    assert au_siege["confidence"] == "siret"
    assert au_siege["siret_est_siege"] is True
    assert ailleurs["siret_est_siege"] is False


# --------------------------------------------------------------------------------------
# Interrogation par SIREN
#
# Le bug le plus coûteux de ce projet : `q=siren:123456789` renvoyait zéro résultat sur les
# 2 444 entreprises du premier passage réel, alors que `q=123456789` renvoie la bonne. Rien
# ne l'avait signalé. Ces tests figent les deux garde-fous mis en place.
# --------------------------------------------------------------------------------------


class ApiRealiste:
    """Imite le comportement constaté : seul le SIREN brut donne un résultat.

    La forme préfixée est traitée comme du texte et ne renvoie rien.
    """

    def __init__(self, siren, *, prefixe_repond=False):
        self.siren = siren
        self.prefixe_repond = prefixe_repond
        self.queries = []

    def get_json(self, url, params=None):
        query = (params or {}).get("q", "")
        self.queries.append(query)
        if query == self.siren:
            return {"results": [{"siren": self.siren, "nom_complet": "EDITEUR RÉEL"}]}
        if query.startswith("siren:") and self.prefixe_repond:
            return {"results": [{"siren": self.siren, "nom_complet": "EDITEUR RÉEL"}]}
        return {"results": []}


def test_fetch_by_siren_essaie_la_forme_brute():
    client = ApiRealiste("312408784")
    resultat = fetch_by_siren(client, "312408784")

    assert resultat["siren"] == "312408784"
    # La forme brute est tentée en premier : inutile d'appeler la seconde.
    assert client.queries == ["312408784"]


def test_fetch_by_siren_se_replie_sur_la_forme_prefixee():
    """Si l'API redevient un jour préfixée, le repli doit fonctionner sans nouveau correctif."""

    class SeulPrefixe(ApiRealiste):
        def get_json(self, url, params=None):
            query = (params or {}).get("q", "")
            self.queries.append(query)
            if query == f"siren:{self.siren}":
                return {"results": [{"siren": self.siren}]}
            return {"results": []}

    client = SeulPrefixe("312408784")
    assert fetch_by_siren(client, "312408784")["siren"] == "312408784"
    assert client.queries == ["312408784", "siren:312408784"]


def test_fetch_by_siren_rejette_un_resultat_qui_n_est_pas_le_bon():
    """Une réponse textuelle sans rapport ne doit jamais être acceptée.

    L'accepter rattacherait un média à la mauvaise entreprise — le pire résultat possible
    pour un outil dont la fonction est de vérifier qui édite quoi.
    """

    class ToujoursParasite:
        """Aucune requête ne trouve le bon SIREN, mais toutes répondent quelque chose."""

        def __init__(self):
            self.queries = []

        def get_json(self, url, params=None):
            self.queries.append((params or {}).get("q", ""))
            return {"results": [{"siren": "999999999", "nom_complet": "SANS RAPPORT"}]}

    client = ToujoursParasite()
    assert fetch_by_siren(client, "312408784") is None
    # Les deux formes ont bien été tentées avant de renoncer.
    assert client.queries == ["312408784", "siren:312408784"]


def test_une_panne_ne_devient_pas_une_absence():
    """Distinguer « rien n'a pu être vérifié » de « l'entreprise n'est pas dans la base »."""

    class ClientEnPanne:
        def get_json(self, url, params=None):
            raise HttpError("HTTP 503 sur /search", status=503)

    resolution = resolve_by_siren(ClientEnPanne(), "312408784", "31240878400030", "2026-07-29")

    assert resolution["confidence"] == "siret_non_verifie"
    assert "entreprise" not in resolution
    assert resolution["erreur"]


def test_search_leve_sur_panne_et_search_or_empty_tolere():
    class ClientEnPanne:
        def get_json(self, url, params=None):
            raise HttpError("HTTP 503", status=503)

    with pytest.raises(SearchFailed):
        search(ClientEnPanne(), "le monde")

    # Le rapprochement par nom, lui, ne doit pas s'arrêter sur une requête ratée.
    assert search_or_empty(ClientEnPanne(), "le monde") == []


def test_le_seuil_d_invraisemblance_est_atteignable():
    """Le garde-fou ne sert à rien s'il exige plus d'entrées qu'un run n'en produit."""
    assert 1 < IMPLAUSIBLE_SAMPLE <= 100
