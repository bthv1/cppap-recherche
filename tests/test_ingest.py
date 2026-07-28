"""Ingestion et archivage, avec un client HTTP factice — aucun appel réseau.

Le comportement le plus important à figer est la **détection de changement** : sans nouvelle
version, l'ingestion ne doit rien écrire. C'est ce qui garde le dépôt léger malgré un cron
hebdomadaire sur des listes qui ne bougent que quelques fois par an.
"""

import json

import ingest
import pytest
from lib import repo
from lib.http import HttpError


class FakeClient:
    """Rejoue des réponses figées et compte les appels effectués."""

    def __init__(self, dataset, file_bytes, profile=None):
        self.dataset = dataset
        self.file_bytes = file_bytes
        self.profile = profile
        self.json_calls = []
        self.bytes_calls = []

    def get_json(self, url, params=None):
        self.json_calls.append(url)
        if "/datasets/" in url:
            return self.dataset
        if url.endswith("/profile/"):
            if self.profile is None:
                raise HttpError("profil absent", status=404, url=url)
            return {"profile": self.profile, "indexes": None}
        raise AssertionError(f"URL inattendue : {url}")

    def get_bytes(self, url, params=None):
        self.bytes_calls.append(url)
        return self.file_bytes


PUBLISHED = (
    b"N\xc2\xb0 CPPAP;Titre;Raison sociale\n0722 C 83260;Le Monde;SOCIETE EDITRICE DU MONDE\n"
)


def dataset_meta(**overrides):
    meta = {
        "id": "5bb834e89ce2e7693d2456a5",
        "slug": "liste-des-publications-de-presse",
        "page": "https://www.data.gouv.fr/datasets/liste-des-publications-de-presse",
        "license": "lov2",
        "resources": [
            {
                "id": "aaaaaaaa-1111-bbbb-2222-cccccccccccc",
                "title": "Liste des publications de presse 2026",
                "format": "csv",
                "url": "https://example.invalid/publications-2026.csv",
                "last_modified": "2026-07-12T09:00:00+00:00",
            }
        ],
    }
    meta.update(overrides)
    return meta


@pytest.fixture
def source(monkeypatch, tmp_path):
    """Isole les écritures dans tmp_path pour ne jamais toucher au data/ du dépôt."""
    monkeypatch.setattr(repo, "ROOT", tmp_path)
    monkeypatch.setattr(repo, "DATA_LATEST", tmp_path / "data" / "latest")
    monkeypatch.setattr(repo, "MANIFEST_FILE", tmp_path / "data" / "manifest.json")
    config = repo.load_config()
    return next(s for s in config["sources"] if s["key"] == "publications")


# --------------------------------------------------------------------------------------
# Sélection de la ressource
# --------------------------------------------------------------------------------------


def test_select_resource_prefere_le_csv(source):
    meta = dataset_meta(
        resources=[
            {
                "id": "1",
                "title": "Export XLSX",
                "format": "xlsx",
                "url": "u1",
                "last_modified": "2026-07-20T00:00:00Z",
            },
            {
                "id": "2",
                "title": "Export CSV",
                "format": "csv",
                "url": "u2",
                "last_modified": "2026-01-01T00:00:00Z",
            },
        ]
    )
    # Le CSV gagne même s'il est plus ancien : le format primaire passe avant la fraîcheur.
    assert ingest.select_resource(meta, source)["id"] == "2"


def test_select_resource_prend_la_plus_recente_a_format_egal(source):
    meta = dataset_meta(
        resources=[
            {
                "id": "1",
                "title": "2024",
                "format": "csv",
                "url": "u1",
                "last_modified": "2024-05-01T00:00:00Z",
            },
            {
                "id": "2",
                "title": "2026",
                "format": "csv",
                "url": "u2",
                "last_modified": "2026-05-01T00:00:00Z",
            },
        ]
    )
    assert ingest.select_resource(meta, source)["id"] == "2"


def test_select_resource_ecarte_la_documentation(source):
    meta = dataset_meta(
        resources=[
            {
                "id": "doc",
                "title": "Documentation des colonnes",
                "format": "csv",
                "url": "u1",
                "last_modified": "2026-09-01T00:00:00Z",
            },
            {
                "id": "data",
                "title": "Liste 2026",
                "format": "csv",
                "url": "u2",
                "last_modified": "2026-05-01T00:00:00Z",
            },
        ]
    )
    assert ingest.select_resource(meta, source)["id"] == "data"


def test_select_resource_echoue_sans_ressource(source):
    with pytest.raises(HttpError, match="aucune ressource"):
        ingest.select_resource(dataset_meta(resources=[]), source)


@pytest.mark.parametrize(
    ("resource", "method", "expected"),
    [
        ({"format": "CSV", "url": "https://x/f.csv"}, "resource-url", "csv"),
        ({"format": "xlsx", "url": "https://x/f.xlsx"}, "resource-url", "xlsx"),
        # Format non déclaré : on retombe sur l'extension de l'URL, requête ignorée.
        ({"format": "", "url": "https://x/f.xlsx?v=2"}, "resource-url", "xlsx"),
        ({"format": "", "url": "https://x/f"}, "resource-url", "dat"),
        # Passé par l'API tabulaire, le contenu est du CSV quel que soit le format source.
        ({"format": "xlsx", "url": "https://x/f.xlsx"}, "tabular-csv", "csv"),
    ],
)
def test_resource_extension(resource, method, expected):
    assert ingest.resource_extension(resource, method) == expected


# --------------------------------------------------------------------------------------
# Archivage et détection de changement
# --------------------------------------------------------------------------------------


def test_premiere_ingestion_ecrit_snapshot_latest_et_manifeste(source, tmp_path):
    client = FakeClient(dataset_meta(), PUBLISHED, profile={"total_lines": 1, "encoding": "utf-8"})
    manifest = {"sources": {}}

    report = ingest.ingest_source(client, source, manifest, "2026-07-28", dry_run=False)

    assert report["changed"] is True
    assert report["rows"] == 1

    snapshot = tmp_path / report["snapshot"]
    assert snapshot.exists()
    # L'archive conserve les octets publiés, sans retraitement : c'est elle qui fait référence.
    assert snapshot.read_bytes() == PUBLISHED

    latest = tmp_path / "data" / "latest" / "publications.csv"
    # La vue normalisée est en UTF-8 avec virgule, pour des diffs git lisibles.
    assert latest.read_text(encoding="utf-8").startswith("N° CPPAP,Titre,Raison sociale\n")

    version = manifest["sources"]["publications"]["versions"][0]
    assert version["columns"] == ["N° CPPAP", "Titre", "Raison sociale"]
    assert version["resource_last_modified"] == "2026-07-12T09:00:00+00:00"
    assert version["profile"]["total_lines"] == 1
    assert manifest["sources"]["publications"]["dataset_page"].endswith("publications-de-presse")


def test_un_contenu_inchange_n_ecrit_rien(source, tmp_path):
    client = FakeClient(dataset_meta(), PUBLISHED)
    manifest = {"sources": {}}

    first = ingest.ingest_source(client, source, manifest, "2026-07-28", dry_run=False)
    second = ingest.ingest_source(client, source, manifest, "2026-08-04", dry_run=False)

    assert first["changed"] is True
    assert second["changed"] is False
    assert "snapshot" not in second
    # Un seul instantané, une seule entrée de manifeste, malgré deux exécutions.
    assert len(manifest["sources"]["publications"]["versions"]) == 1
    assert len(list((tmp_path / "data" / "raw" / "publications").glob("*.csv"))) == 1


def test_un_reencodage_sans_changement_de_contenu_ne_cree_pas_de_version(source):
    """data.gouv.fr peut republier le même contenu dans un autre encodage.

    La détection porte sur le contenu normalisé, pas sur les octets : pas de fausse version.
    """
    manifest = {"sources": {}}
    utf8 = "N° CPPAP;Titre;Raison sociale\n0722 C 83260;Le Monde;SOCIETE EDITRICE DU MONDE\n"

    ingest.ingest_source(
        FakeClient(dataset_meta(), utf8.encode("utf-8")),
        source,
        manifest,
        "2026-07-28",
        dry_run=False,
    )
    second = ingest.ingest_source(
        FakeClient(dataset_meta(), utf8.encode("cp1252")),
        source,
        manifest,
        "2026-08-04",
        dry_run=False,
    )

    assert second["changed"] is False


def test_un_contenu_modifie_cree_une_nouvelle_version(source, tmp_path):
    manifest = {"sources": {}}
    ingest.ingest_source(
        FakeClient(dataset_meta(), PUBLISHED), source, manifest, "2026-07-28", dry_run=False
    )

    modifie = PUBLISHED + b"0425 C 79320;Le Canard;LES EDITIONS MARECHAL\n"
    second = ingest.ingest_source(
        FakeClient(dataset_meta(), modifie), source, manifest, "2026-11-03", dry_run=False
    )

    assert second["changed"] is True
    assert second["rows"] == 2
    assert len(manifest["sources"]["publications"]["versions"]) == 2
    assert len(list((tmp_path / "data" / "raw" / "publications").glob("*.csv"))) == 2


def test_dry_run_n_ecrit_rien(source, tmp_path):
    manifest = {"sources": {}}
    report = ingest.ingest_source(
        FakeClient(dataset_meta(), PUBLISHED), source, manifest, "2026-07-28", dry_run=True
    )

    assert report["changed"] is True
    assert not (tmp_path / "data").exists()
    assert manifest["sources"] == {}


def test_un_profil_indisponible_n_est_pas_fatal(source):
    client = FakeClient(dataset_meta(), PUBLISHED, profile=None)
    manifest = {"sources": {}}

    report = ingest.ingest_source(client, source, manifest, "2026-07-28", dry_run=False)

    assert report["changed"] is True
    assert manifest["sources"]["publications"]["versions"][0]["profile"] is None


def test_un_ecart_de_nombre_de_lignes_est_signale(source, caplog):
    """Fichier tronqué ou profil obsolète : l'écart doit apparaître, pas passer inaperçu."""
    client = FakeClient(dataset_meta(), PUBLISHED, profile={"total_lines": 9000})
    manifest = {"sources": {}}

    with caplog.at_level("WARNING"):
        report = ingest.ingest_source(client, source, manifest, "2026-07-28", dry_run=False)

    assert report["row_count_mismatch"] == 9000
    assert "tronqué" in caplog.text


def test_un_fichier_vide_leve(source):
    with pytest.raises(HttpError, match="vide ou illisible"):
        ingest.ingest_source(
            FakeClient(dataset_meta(), b""), source, {"sources": {}}, "2026-07-28", dry_run=False
        )


def test_download_resource_se_replie_sur_l_api_tabulaire(source):
    class FailingDirect(FakeClient):
        def get_bytes(self, url, params=None):
            self.bytes_calls.append(url)
            if "example.invalid" in url:
                raise HttpError("HTTP 404", status=404, url=url)
            return PUBLISHED

    client = FailingDirect(dataset_meta(), PUBLISHED)
    data, method = ingest.download_resource(client, dataset_meta()["resources"][0])

    assert data == PUBLISHED
    assert method == "tabular-csv"
    assert any("tabular-api" in url for url in client.bytes_calls)


# --------------------------------------------------------------------------------------
# Manifeste
# --------------------------------------------------------------------------------------


def test_load_manifest_repare_un_fichier_invalide(monkeypatch, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"autre": 1}', encoding="utf-8")
    monkeypatch.setattr(repo, "MANIFEST_FILE", path)

    assert ingest.load_manifest() == {"sources": {}}


def test_last_version_retourne_la_plus_recente():
    manifest = {"sources": {"spel": {"versions": [{"observed_at": "a"}, {"observed_at": "b"}]}}}

    assert ingest.last_version(manifest, "spel")["observed_at"] == "b"
    assert ingest.last_version(manifest, "inconnue") is None


def test_emit_github_output(monkeypatch, tmp_path):
    output = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    ingest.emit_github_output(
        [
            {"key": "spel", "changed": True},
            {"key": "publications", "changed": False},
            {"key": "agences", "changed": True},
        ]
    )

    content = output.read_text(encoding="utf-8")
    assert "changed=spel,agences" in content
    assert "changed_count=2" in content
    assert "any_changed=true" in content


def test_le_rapport_json_est_serialisable(source):
    report = ingest.ingest_source(
        FakeClient(dataset_meta(), PUBLISHED), source, {"sources": {}}, "2026-07-28", dry_run=True
    )
    # Le workflow relit ce rapport en Python : il doit tenir dans du JSON strict.
    assert json.loads(json.dumps(report))["key"] == "publications"
