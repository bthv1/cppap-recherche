"""Différentiel entre deux versions archivées, corps des Releases GitHub.

C'est ce qui rend une version citable : pouvoir dire ce qui a changé depuis la commission
précédente, titre par titre.
"""

import pytest
import release_notes
from lib import repo
from release_notes import MAX_LISTED, bullet_list, describe, diff_records, source_section

V1 = (
    "N° CPPAP;Titre;Raison sociale;Forme juridique;Département du siège social;IPG;Périodicité\n"
    "0722 C 83260;Le Monde;SOCIETE EDITRICE DU MONDE;SA;75;IPG;Quotidien\n"
    "0425 C 79320;Le Canard Enchaîné;LES EDITIONS MARECHAL;SA;75;IPG;Hebdomadaire\n"
    "0823 C 87991;La Hulotte;SARL LA HULOTTE;SARL;08;Non IPG;Trimestriel\n"
)

V2 = (
    "N° CPPAP;Titre;Raison sociale;Forme juridique;Département du siège social;IPG;Périodicité\n"
    # inchangé
    "0722 C 83260;Le Monde;SOCIETE EDITRICE DU MONDE;SA;75;IPG;Quotidien\n"
    # modifié : périodicité et éditeur
    "0425 C 79320;Le Canard Enchaîné;LES EDITIONS MARECHAL SA;SA;75;IPG;Bimensuel\n"
    # « La Hulotte » retirée, un nouveau titre ajouté
    "1126 C 91500;Nouveau Titre;EDITIONS NOUVELLES;SAS;69;Non IPG;Mensuel\n"
)


@pytest.fixture
def source():
    return next(s for s in repo.load_config()["sources"] if s["key"] == "publications")


@pytest.fixture
def archive(monkeypatch, tmp_path, source):
    """Prépare un dépôt factice : un instantané précédent et la version courante."""
    snapshot = tmp_path / "data" / "raw" / "publications" / "2026-04-03__aaaaaaaa.csv"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(V1, encoding="utf-8")

    latest = tmp_path / "data" / "latest" / "publications.csv"
    latest.parent.mkdir(parents=True)
    latest.write_text(V2, encoding="utf-8")

    monkeypatch.setattr(repo, "ROOT", tmp_path)
    monkeypatch.setattr(repo, "DATA_LATEST", latest.parent)
    monkeypatch.setattr(release_notes.repo, "ROOT", tmp_path)
    monkeypatch.setattr(release_notes.repo, "DATA_LATEST", latest.parent)

    return {
        "sources": {
            "publications": {
                "versions": [
                    {
                        "observed_at": "2026-04-03",
                        "rows": 3,
                        "content_sha8": "aaaaaaaa",
                        "snapshot": "data/raw/publications/2026-04-03__aaaaaaaa.csv",
                        "columns": ["N° CPPAP", "Titre"],
                    },
                    {
                        "observed_at": "2026-07-28",
                        "rows": 3,
                        "content_sha8": "bbbbbbbb",
                        "snapshot": "data/raw/publications/2026-07-28__bbbbbbbb.csv",
                        "resource_title": "Liste 2026",
                        "resource_last_modified": "2026-07-12T09:00:00+00:00",
                        "columns": ["N° CPPAP", "Titre"],
                    },
                ]
            }
        }
    }


def test_diff_records_classe_ajouts_retraits_et_modifications():
    previous = {
        "a": {"nom": "A", "editeur": "X", "periodicite": "Quotidien"},
        "b": {"nom": "B", "editeur": "Y", "periodicite": "Mensuel"},
    }
    current = {
        "a": {"nom": "A", "editeur": "X", "periodicite": "Hebdomadaire"},
        "c": {"nom": "C", "editeur": "Z", "periodicite": "Annuel"},
    }

    delta = diff_records(previous, current)

    assert [r["nom"] for r in delta["added"]] == ["C"]
    assert [r["nom"] for r in delta["removed"]] == ["B"]
    assert len(delta["modified"]) == 1
    record, changes = delta["modified"][0]
    assert record["nom"] == "A"
    assert changes == [("periodicite", "Quotidien", "Hebdomadaire")]


def test_diff_records_ignore_les_champs_hors_comparaison():
    """`extra` n'est pas comparé : trop bruyant, et déjà visible dans le diff git du CSV."""
    previous = {"a": {"nom": "A", "extra": {"col": "avant"}}}
    current = {"a": {"nom": "A", "extra": {"col": "après"}}}

    assert diff_records(previous, current)["modified"] == []


def test_bullet_list_annonce_la_troncature():
    items = [f"item {i}" for i in range(MAX_LISTED + 5)]
    rendered = bullet_list("Ajouts", items)

    assert f"Ajouts ({len(items)})" in rendered
    # Une troncature silencieuse laisserait croire à une liste complète.
    assert "Liste tronquée" in rendered
    assert f"sur {len(items)}" in rendered


def test_bullet_list_sans_troncature_quand_la_liste_tient():
    rendered = bullet_list("Ajouts", ["un", "deux"])

    assert "Liste tronquée" not in rendered
    assert "- un" in rendered


def test_bullet_list_vide_ne_produit_rien():
    assert bullet_list("Ajouts", []) == ""


def test_describe_reste_lisible_sans_numero():
    assert "`0722 C 83260`" in describe(
        {"cppap": "0722 C 83260", "nom": "Le Monde", "editeur": "SEM"}
    )
    assert "sans n°" in describe({"cppap": "", "nom": "X", "editeur": ""})


def test_source_section_compte_le_differentiel(archive, source):
    section = source_section(source, archive)

    assert "## Publications de presse" in section
    assert "**1 ajout(s), 1 retrait(s), 1 modification(s)**" in section
    assert "par rapport à la version du 2026-04-03" in section
    assert "Nouveau Titre" in section
    assert "La Hulotte" in section
    assert "`bbbbbbbb`" in section

    # Le détail des champs modifiés doit apparaître, pas seulement leur nombre.
    modifications = section.split("Titres modifiés")[1]
    assert "periodicite : « Hebdomadaire » → « Bimensuel »" in modifications
    assert "editeur : « LES EDITIONS MARECHAL » → « LES EDITIONS MARECHAL SA »" in modifications


def test_source_section_signale_une_premiere_version(source):
    versions = [{"observed_at": "2026-07-28", "rows": 3}]
    manifest = {"sources": {"publications": {"versions": versions}}}
    section = source_section(source, manifest)

    assert "Première version archivée" in section


def test_source_section_sans_version(source):
    section = source_section(source, {"sources": {}})
    assert "Aucune version archivée" in section


def test_source_section_survit_a_un_instantane_manquant(monkeypatch, tmp_path, source):
    """Un instantané supprimé du dépôt ne doit pas faire échouer la Release."""
    monkeypatch.setattr(release_notes.repo, "ROOT", tmp_path)
    monkeypatch.setattr(release_notes.repo, "DATA_LATEST", tmp_path / "absent")
    manifest = {
        "sources": {
            "publications": {
                "versions": [
                    {"observed_at": "a", "snapshot": "data/raw/publications/disparu.csv"},
                    {"observed_at": "b", "snapshot": "data/raw/publications/aussi.csv"},
                ]
            }
        }
    }
    section = source_section(source, manifest)

    assert "Différentiel indisponible" in section


def test_main_ecrit_les_notes(archive, monkeypatch, tmp_path):
    monkeypatch.setattr(release_notes.repo, "MANIFEST_FILE", tmp_path / "manifest.json")
    repo.write_json(tmp_path / "manifest.json", archive)
    monkeypatch.setattr(
        release_notes.repo, "SIRENE_CACHE_FILE", repo.FIXTURES / "sirene_cache.json"
    )

    out = tmp_path / "notes.md"
    assert release_notes.main(["--changed", "publications", "--out", str(out)]) == 0

    body = out.read_text(encoding="utf-8")
    assert "## Publications de presse" in body
    assert "octets publiés par data.gouv.fr" in body
    # Le récapitulatif d'appariement SIREN accompagne chaque version publiée.
    assert "## Rattachement SIRENE" in body
    assert "ne contiennent pas de numéro SIREN" in body
