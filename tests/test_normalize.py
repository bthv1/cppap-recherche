"""Appariement des colonnes et construction des enregistrements canoniques.

Le cœur du risque est ici : les en-têtes des fichiers CPPAP ont déjà changé par le passé, et
n'ont pas pu être observés lors de l'écriture du code. Ces tests figent le comportement des
deux passes d'appariement et, surtout, le **mode d'échec** attendu quand un champ requis
disparaît.
"""

import pytest
from lib import repo
from lib.tabular import read_rows
from normalize import (
    SchemaError,
    build_records,
    clean_value,
    is_inscrit,
    is_ipg,
    load_all_records,
    map_columns,
    normalize_cppap,
    normalize_departement,
    normalize_siret,
    normalize_url,
    publisher_key,
    siren_from_siret,
)


@pytest.fixture(scope="module")
def config():
    return repo.load_config()


@pytest.fixture(scope="module")
def sources(config):
    return {source["key"]: source for source in config["sources"]}


def fixture_rows(key):
    raw = (repo.FIXTURES / "published" / f"{key}.csv").read_bytes()
    rows, _ = read_rows(raw, f"{key}.csv")
    return rows


# --------------------------------------------------------------------------------------
# Appariement des colonnes
# --------------------------------------------------------------------------------------


def test_appariement_exact_sur_les_entetes_spel(sources):
    rows = fixture_rows("spel")
    mapping, report = map_columns(rows[0], sources["spel"]["columns"])

    assert rows[0][mapping["cppap"]] == "N° CPPAP"
    assert rows[0][mapping["nom"]] == "Nom du service"
    assert rows[0][mapping["editeur"]] == "Raison sociale"
    assert rows[0][mapping["url"]] == "Adresse du site"
    assert report["unclaimed_columns"] == []
    assert all(how.startswith("exact:") for how in report["resolved"].values())


def test_appariement_par_inclusion_sur_des_entetes_rallonges(sources):
    """Variante historique : en-tête `IPG` et libellés de colonnes allongés."""
    rows = fixture_rows("publications")
    mapping, report = map_columns(rows[0], sources["publications"]["columns"])

    assert rows[0][mapping["cppap"]] == "N° CPPAP de la publication"
    assert report["resolved"]["cppap"].startswith("inclusion:")

    assert rows[0][mapping["departement"]].startswith("Département du siège social")
    assert report["resolved"]["departement"].startswith("inclusion:")

    # L'ancien intitulé « IPG » doit alimenter le champ `qualification`.
    assert rows[0][mapping["qualification"]] == "IPG"
    assert report["resolved"]["qualification"].startswith("exact:")


def test_une_colonne_n_est_jamais_revendiquee_deux_fois(sources):
    header = ["N° CPPAP", "Dénomination sociale", "Titre", "Forme juridique"]
    mapping, _ = map_columns(header, sources["publications"]["columns"])

    assert len(set(mapping.values())) == len(mapping)
    # `editeur` est résolu avant `nom`, il prend donc « Dénomination sociale ».
    assert header[mapping["editeur"]] == "Dénomination sociale"
    assert header[mapping["nom"]] == "Titre"


def test_un_alias_court_ne_capte_pas_un_entete_plus_long(sources):
    """L'alias « nom », trop générique, ne participe pas à la passe d'inclusion.

    « Nom de la commune » revient donc à `commune`, dont l'alias est plus spécifique, et
    `nom` se rabat sur « Titre » — et non l'inverse.
    """
    header = ["N° CPPAP", "Titre", "Nom de la commune"]
    mapping, _ = map_columns(header, sources["publications"]["columns"])

    assert header[mapping["nom"]] == "Titre"
    assert header[mapping["commune"]] == "Nom de la commune"


def test_un_alias_trop_court_ne_resout_pas_par_inclusion(sources):
    """Sans en-tête exploitable pour `nom`, l'appariement doit échouer, pas improviser.

    « Nom du responsable » n'est pas un titre de publication : l'alias « nom » étant exclu de
    la passe d'inclusion, aucun rattrapage hasardeux n'a lieu et l'erreur remonte.
    """
    rows = [["N° CPPAP", "Nom du responsable"], ["0722 C 83260", "Dupont"]]

    with pytest.raises(SchemaError, match="nom"):
        build_records(rows, sources["publications"])


def test_les_colonnes_inconnues_sont_conservees_dans_extra(sources):
    rows = [
        ["N° CPPAP", "Titre", "Raison sociale", "Colonne inédite"],
        ["0722 C 83260", "Le Monde", "SOCIETE EDITRICE DU MONDE", "valeur à garder"],
    ]
    records, report = build_records(rows, sources["publications"])

    assert report["unclaimed_columns"] == ["Colonne inédite"]
    assert records[0]["extra"] == {"Colonne inédite": "valeur à garder"}


# --------------------------------------------------------------------------------------
# Mode d'échec
# --------------------------------------------------------------------------------------


def test_un_champ_requis_manquant_leve_en_affichant_les_entetes(sources):
    rows = [["Intitulé inconnu", "Autre colonne"], ["a", "b"]]

    with pytest.raises(SchemaError) as excinfo:
        build_records(rows, sources["publications"])

    message = str(excinfo.value)
    # Le message doit permettre de corriger config/sources.json sans relancer le workflow.
    assert "cppap" in message
    assert "nom" in message
    assert "Intitulé inconnu" in message
    assert "config/sources.json" in message


def test_les_agences_n_exigent_pas_de_numero_cppap(sources):
    """Les agences reçoivent un numéro d'agrément, pas toujours un n° CPPAP."""
    rows = [["Nom de l'agence", "Raison sociale"], ["Agence X", "AGENCE X SAS"]]
    records, _ = build_records(rows, sources["agences"])

    assert len(records) == 1
    assert records[0]["cppap"] == ""
    # Sans n° CPPAP, l'identifiant vient du nom : une URL lisible et partageable, plutôt
    # qu'une empreinte opaque.
    assert records[0]["id"] == "agence-agence-x"


# --------------------------------------------------------------------------------------
# Valeurs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("75", "75"),
        ("075", "75"),
        ("2A", "2A"),
        ("2b", "2B"),
        ("971", "971"),
        ("49 - Maine-et-Loire", "49"),
        ("08", "08"),
        ("Paris", ""),
        ("", ""),
    ],
)
def test_normalize_departement(raw, expected):
    assert normalize_departement(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0620 W 91234", "0620 W 91234"),
        ("0620W91234", "0620W91234"),
        ("0620-W-91234", "0620 W 91234"),
        ("  0722   c   83260 ", "0722 C 83260"),
        ("", ""),
    ],
)
def test_normalize_cppap(raw, expected):
    assert normalize_cppap(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IPG", True),
        ("ipg", True),
        ("Information politique et générale", True),
        # Piège : la valeur contient le sigle mais le nie.
        ("Non IPG", False),
        ("Sans qualification IPG", False),
        ("", False),
    ],
)
def test_is_ipg(raw, expected):
    assert is_ipg(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("www.mediapart.fr", "https://www.mediapart.fr"),
        ("https://x.fr", "https://x.fr"),
        ("http://x.fr", "http://x.fr"),
        ("", ""),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_publisher_key_regroupe_les_variantes_de_forme_juridique():
    # « SARL LA HULOTTE » et « LA HULOTTE SARL » désignent le même éditeur.
    assert publisher_key("SARL LA HULOTTE", "08") == publisher_key("LA HULOTTE SARL", "08")
    assert publisher_key("", "75") == ""


# --------------------------------------------------------------------------------------
# Chaîne complète
# --------------------------------------------------------------------------------------


def test_identifiants_dupliques_sont_suffixes(sources):
    """Un même n° CPPAP apparaît deux fois dans la fixture : les identifiants restent uniques."""
    records, report = build_records(fixture_rows("publications"), sources["publications"])

    assert report["duplicate_ids"] == 1
    assert len({r["id"] for r in records}) == len(records)
    assert any(r["id"].endswith("-2") for r in records)


def test_load_all_records_sur_les_fixtures(config):
    records, reports = load_all_records(config, repo.FIXTURES / "published")

    assert len(records) == 18
    assert {r["type"] for r in records} == {"spel", "publication", "agence"}
    assert all("error" not in report for report in reports.values())
    # Chaque enregistrement porte un identifiant unique et une clé d'éditeur exploitable.
    assert len({r["id"] for r in records}) == len(records)
    assert all(r["publisher_key"] for r in records)


# --------------------------------------------------------------------------------------
# SIRET publié dans la source
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90000010100017", "90000010100017"),
        # Groupage typographique fréquent dans les exports de tableur.
        ("900 000 109 00021", "90000010900021"),
        ("900.000.109.00021", "90000010900021"),
        # Zéro initial perdu par une colonne numérique : restitué sans ambiguïté.
        ("9000011200015", "09000011200015"),
        # Longueurs inexploitables : rejetées plutôt que devinées.
        ("123", ""),
        ("900000101", ""),
        ("900000101000170", ""),
        ("", ""),
        ("non renseigné", ""),
    ],
)
def test_normalize_siret(raw, expected):
    assert normalize_siret(raw) == expected


def test_siren_from_siret():
    assert siren_from_siret("90000010100017") == "900000101"
    assert siren_from_siret("09000011200015") == "090000112"
    # Une entrée non normalisée ne doit pas produire un SIREN tronqué silencieusement.
    assert siren_from_siret("900000101") == ""
    assert siren_from_siret("") == ""


def test_les_fiches_portent_le_siret_et_le_siren_derive(sources):
    rows = [
        ["N° CPPAP", "Titre", "SIRET", "Raison sociale"],
        ["0722 C 83260", "Le Monde", "900 000 101 00017", "SOCIETE EDITRICE DU MONDE"],
    ]
    records, report = build_records(rows, sources["publications"])

    assert report["resolved"]["siret"].startswith("exact:")
    assert records[0]["siret"] == "90000010100017"
    assert records[0]["siren"] == "900000101"
    # La valeur brute est conservée : elle documente ce que la source a réellement écrit.
    assert records[0]["siret_source"] == "900 000 101 00017"


def test_une_source_sans_colonne_siret_reste_exploitable(sources):
    """Toutes les listes CPPAP ne publient pas de SIRET : l'absence n'est pas une erreur."""
    records, report = build_records(fixture_rows("spel"), sources["spel"])

    assert "siret" in report["unresolved"]
    assert all(r["siret"] == "" and r["siren"] == "" for r in records)


def test_un_siret_illisible_ne_bloque_pas_la_fiche(sources):
    rows = [
        ["N° CPPAP", "Titre", "SIRET", "Raison sociale"],
        ["0722 C 83260", "Le Monde", "à compléter", "SOCIETE EDITRICE DU MONDE"],
    ]
    records, _ = build_records(rows, sources["publications"])

    # La fiche existe, simplement sans jointure exacte : elle repassera par l'heuristique.
    assert len(records) == 1
    assert records[0]["siren"] == ""
    assert records[0]["siret_source"] == "à compléter"


# --------------------------------------------------------------------------------------
# Statut d'inscription
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statut", "implicite", "expected"),
    [
        ("Inscrit", "", True),
        ("inscrit", "", True),
        # Le piège central : quatre publications sur cinq portent cette valeur.
        ("Non Inscrit", "", False),
        ("non inscrit", "", False),
        ("Reconnu", "", True),
        ("Agréée", "", True),
        # Sans colonne de statut, figurer dans une liste de médias reconnus vaut reconnaissance.
        ("", "Reconnu", True),
        ("", "", None),
        ("Valeur imprévue", "", None),
    ],
)
def test_is_inscrit(statut, implicite, expected):
    assert is_inscrit(statut, implicite) is expected


def test_le_statut_implicite_s_applique_aux_listes_sans_colonne(sources):
    """La liste des services reconnus n'a pas de colonne statut : y être suffit."""
    rows = [["Numéro CPPAP", "Service", "Editeur"], ["0330 W 95411", "exemple.fr", "EXEMPLE"]]
    records, _ = build_records(rows, sources["spel"])

    assert records[0]["statut"] == "Reconnu"
    assert records[0]["inscrit"] is True


def test_un_titre_non_inscrit_est_marque_comme_tel(sources):
    rows = [
        ["numero_cppap", "nom_du_titre_de_presse", "editeur", "statut_inscription"],
        ["2588058", "ELETÜNK", "MISSION CATHOLIQUE HONGROISE", "Non Inscrit"],
    ]
    records, _ = build_records(rows, sources["publications"])

    assert records[0]["statut"] == "Non Inscrit"
    assert records[0]["inscrit"] is False


# --------------------------------------------------------------------------------------
# Valeurs de remplissage
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Le cas réel : « Nom commercial » vaut « - » pour la plupart des agences de presse.
        ("-", ""),
        ("--", ""),
        (" / ", ""),
        ("Néant", ""),
        ("non renseigné", ""),
        ("  Le Monde  ", "Le Monde"),
        ("", ""),
        # Volontairement conservés : ce pourrait être un nom de média réel.
        ("NC", "NC"),
        ("SO", "SO"),
    ],
)
def test_clean_value(raw, expected):
    assert clean_value(raw) == expected


def test_l_editeur_retombe_sur_le_nom_pour_une_agence(sources):
    """Pour une agence de presse, la société est le média : une seule colonne les porte."""
    rows = [["IDENTIFICATION (dénomination sociale)", "Nom commercial"], ["17 JUIN MÉDIA", "-"]]
    records, _ = build_records(rows, sources["agences"])

    assert records[0]["nom"] == "17 JUIN MÉDIA"
    assert records[0]["editeur"] == "17 JUIN MÉDIA"
    # Le « - » ne doit pas se retrouver affiché comme nom commercial.
    assert records[0]["nom_commercial"] == ""


def test_les_entetes_reels_observes_sont_tous_reconnus(sources):
    """Verrou de non-régression sur les en-têtes constatés lors du premier run réel."""
    reels = {
        "spel": [
            "Editeur",
            "Forme juridique",
            "Département",
            "Service",
            "url",
            "Qualification",
            "Numéro CPPAP",
        ],
        "publications": [
            "editeur",
            "siret",
            "forme_juridique",
            "departement",
            "type_de_presse",
            "nom_du_titre_de_presse",
            "statut_inscription",
            "demande_en_cours",
            "numero_cppap",
            "date_expiration_inscription",
            "date_derniere_decision",
            "qualification",
            "regime_derogatoire",
            "ajl",
            "url_spel",
        ],
        "agences": [
            "IDENTIFICATION (dénomination sociale)",
            "Nom commercial",
            "Arrêté du",
            "JORF \u2013 Date",  # tiret demi-cadratin : le caractère réel du fichier source
        ],
    }
    attendus = {
        "spel": {
            "cppap",
            "nom",
            "editeur",
            "url",
            "forme_juridique",
            "departement",
            "qualification",
        },
        "publications": {
            "cppap",
            "nom",
            "editeur",
            "siret",
            "statut",
            "date_expiration",
            "type_presse",
            "forme_juridique",
            "departement",
            "qualification",
            "date_decision",
            "url",
        },
        "agences": {"nom", "nom_commercial", "date_decision"},
    }
    for key, header in reels.items():
        mapping, _ = map_columns(header, sources[key]["columns"])
        manquants = attendus[key] - set(mapping)
        assert not manquants, f"[{key}] champs non résolus : {manquants}"

    # Et le nom des agences vient bien de la dénomination, pas de « Nom commercial ».
    mapping, _ = map_columns(reels["agences"], sources["agences"]["columns"])
    assert reels["agences"][mapping["nom"]].startswith("IDENTIFICATION")
