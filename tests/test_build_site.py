"""Génération du site : répartition en lots, index compact, rattachement SIRENE."""

import json

import build_site
import pytest
from build_site import DETAIL_BUCKETS, attach_sirene, build_payloads, detail_bucket, source_context
from lib import repo
from normalize import load_all_records


@pytest.fixture(scope="module")
def config():
    return repo.load_config()


@pytest.fixture(scope="module")
def records(config):
    fiches, _ = load_all_records(config, repo.FIXTURES / "published")
    return fiches


@pytest.fixture(scope="module")
def sirene():
    return build_site.load_sirene(from_fixtures=True)


# --------------------------------------------------------------------------------------
# Répartition en lots
# --------------------------------------------------------------------------------------


def test_detail_bucket_reste_dans_les_bornes(records):
    assert all(0 <= detail_bucket(r["id"]) < DETAIL_BUCKETS for r in records)


def test_detail_bucket_est_deterministe():
    """Le lot dérive de l'identifiant : il ne bouge pas d'une publication à l'autre.

    C'est ce qui permet au cache du navigateur de survivre à une mise à jour de données —
    seuls les lots réellement modifiés sont retéléchargés.
    """
    assert detail_bucket("publication-0722-c-83260") == detail_bucket("publication-0722-c-83260")
    assert detail_bucket("a") != detail_bucket("a ")


# --------------------------------------------------------------------------------------
# Rattachement SIREN
# --------------------------------------------------------------------------------------


def test_attach_sirene_utilise_le_rapprochement_par_nom(sirene):
    """Éditeur sans SIRET nulle part : c'est l'heuristique qui fournit le rattachement."""
    record = {"id": "spel-x", "publisher_key": "societe editrice de mediapart|75"}
    resolution = attach_sirene(record, sirene)

    assert resolution["siren"] == "900000103"
    assert resolution["confidence"] == "certain"


def test_attach_sirene_propage_le_siret_entre_listes(sirene):
    """Sans SIRET sur la fiche, mais l'éditeur en déclare un dans une autre liste CPPAP."""
    record = {"id": "spel-x", "publisher_key": "societe editrice du monde|75", "siren": ""}
    resolution = attach_sirene(record, sirene)

    assert resolution["siren"] == "900000101"
    assert resolution["confidence"] == "siret_propage"


def test_attach_sirene_prefere_le_siret_de_la_fiche(sirene):
    """Quand la fiche porte elle-même un SIRET, le rattachement est direct, pas propagé."""
    record = {
        "id": "publication-x",
        "publisher_key": "societe editrice du monde|75",
        "siren": "900000101",
    }
    assert attach_sirene(record, sirene)["confidence"] == "siret"


def test_un_override_de_fiche_prime_sur_la_cle_editeur():
    sirene = {
        "entries": {"ed|75": {"siren": "900000001", "confidence": "certain"}},
        "record_entries": {"fiche-1": {"siren": "900000002", "confidence": "verifie"}},
    }
    record = {"id": "fiche-1", "publisher_key": "ed|75"}

    assert attach_sirene(record, sirene)["siren"] == "900000002"


def test_attach_sirene_sans_correspondance():
    sirene = {"entries": {}, "record_entries": {}}
    assert attach_sirene({"id": "x", "publisher_key": "inconnu|75"}, sirene) is None


# --------------------------------------------------------------------------------------
# Charges utiles
# --------------------------------------------------------------------------------------


def test_build_payloads_produit_un_index_coherent(config, records, sirene):
    sources = source_context({"sources": {}}, config)
    departements = {"75": "Paris", "69": "Rhône"}
    search, buckets, stats = build_payloads(records, sirene, departements, sources)

    assert len(search["rows"]) == len(records)
    assert len(search["fields"]) == len(search["rows"][0])
    assert stats["total"] == len(records)

    # Chaque fiche de l'index est retrouvable dans le lot que l'index désigne.
    for row in search["rows"]:
        entry = dict(zip(search["fields"], row, strict=True))
        assert entry["id"] in buckets[entry["bucket"]]

    # Toutes les fiches sont réparties, aucune perdue ni dupliquée.
    assert sum(len(payload) for payload in buckets.values()) == len(records)


def test_les_libelles_de_departement_vont_dans_les_statistiques(config, records, sirene):
    """Le libellé n'est publié qu'une fois, dans meta.json, pas sur chaque fiche.

    Répéter « Paris » sur des dizaines de milliers de fiches pesait un quart du poids
    téléchargé ; `web/app.js` le reconstitue à l'ouverture d'une carte.
    """
    sources = source_context({"sources": {}}, config)
    _, buckets, stats = build_payloads(records, sirene, {"75": "Paris"}, sources)

    assert {"code": "75", "label": "Paris", "count": 10} in stats["departements"]

    detail = next(
        d for payload in buckets.values() for d in payload.values() if d.get("departement") == "75"
    )
    assert "departement_label" not in detail


def test_les_fiches_publiees_sont_allegees(config, records, sirene):
    """Ni champ vide, ni valeur reconstituable depuis meta.json."""
    sources = source_context({"sources": {}}, config)
    _, buckets, _ = build_payloads(records, sirene, {"75": "Paris"}, sources)
    fiches = [d for payload in buckets.values() for d in payload.values()]

    for detail in fiches:
        assert "" not in detail.values(), f"champ vide conservé dans {detail['id']}"
        for derivable in (
            "type_label",
            "departement_label",
            "source_page",
            "source_snapshot",
            "source_version",
            "publisher_key",
        ):
            assert derivable not in detail

    # Les valeurs signifiantes qui ressemblent à du vide doivent survivre à l'allègement.
    assert any(d.get("ipg") is False for d in fiches)
    assert any(d.get("inscrit") is True for d in fiches)


def test_build_payloads_retire_la_cle_interne_d_editeur(config, records, sirene):
    """`publisher_key` sert à l'appariement, pas à l'affichage : inutile de la publier."""
    sources = source_context({"sources": {}}, config)
    _, buckets, _ = build_payloads(records, sirene, {}, sources)

    for payload in buckets.values():
        for detail in payload.values():
            assert "publisher_key" not in detail


def test_source_context_retombe_sur_l_url_du_slug_sans_manifeste(config):
    sources = source_context({"sources": {}}, config)

    assert sources["spel"]["versions_archived"] == 0
    assert sources["spel"]["dataset_page"].endswith(
        "liste-des-services-de-presse-en-ligne-reconnus"
    )


def test_source_context_reprend_la_derniere_version_du_manifeste(config):
    manifest = {
        "sources": {
            "spel": {
                "dataset_page": "https://data.gouv.fr/spel",
                "license": "lov2",
                "versions": [
                    {"observed_at": "2026-01-01", "rows": 10, "content_sha8": "aaaaaaaa"},
                    {
                        "observed_at": "2026-07-28",
                        "rows": 12,
                        "content_sha8": "bbbbbbbb",
                        "snapshot": "data/raw/spel/2026-07-28__bbbbbbbb.csv",
                    },
                ],
            }
        }
    }
    sources = source_context(manifest, config)

    assert sources["spel"]["versions_archived"] == 2
    assert sources["spel"]["latest"]["observed_at"] == "2026-07-28"
    assert sources["spel"]["latest"]["rows"] == 12


# --------------------------------------------------------------------------------------
# Exécution complète
# --------------------------------------------------------------------------------------


def test_main_from_fixtures_genere_un_site_complet(tmp_path):
    assert build_site.main(["--from-fixtures", "--out", str(tmp_path)]) == 0

    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "app.js").is_file()
    assert (tmp_path / "vendor" / "minisearch" / "minisearch.js").is_file()

    search = json.loads((tmp_path / "data" / "search.json").read_text(encoding="utf-8"))
    meta = json.loads((tmp_path / "data" / "meta.json").read_text(encoding="utf-8"))

    assert meta["fixtures"] is True
    assert meta["stats"]["total"] == len(search["rows"]) == 18
    assert meta["detail_buckets"] == DETAIL_BUCKETS
    assert len(list((tmp_path / "data" / "details").glob("*.json"))) == DETAIL_BUCKETS

    # Les libellés d'affichage voyagent avec les données : une seule requête de configuration.
    assert meta["labels"]["confidence"]["certain"]["tone"] == "ok"
    assert meta["labels"]["naf"]["58.13Z"] == "Édition de journaux"

    # Le compte rendu d'appariement des colonnes est publié : la dérive de schéma est visible.
    assert "cppap" in meta["schema_reports"]["publications"]["resolved"]


def test_main_signale_l_url_du_relais_sirene(tmp_path):
    build_site.main(["--from-fixtures", "--out", str(tmp_path), "--sirene-proxy", "https://w.dev"])
    meta = json.loads((tmp_path / "data" / "meta.json").read_text(encoding="utf-8"))

    assert meta["sirene_proxy"] == "https://w.dev"


def test_main_echoue_proprement_sans_donnees(monkeypatch, tmp_path, caplog):
    """Sans données ingérées, on sort en erreur explicite plutôt que publier un site vide."""
    vide = tmp_path / "latest-vide"
    vide.mkdir()
    monkeypatch.setattr(build_site.repo, "DATA_LATEST", vide)

    with caplog.at_level("ERROR"):
        code = build_site.main(["--out", str(tmp_path / "site")])

    assert code == 1
    assert "ingest.py" in caplog.text
    # Rien n'a été écrit : pas de site partiel à déployer par accident.
    assert not (tmp_path / "site").exists()
