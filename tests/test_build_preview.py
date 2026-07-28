"""Assemblage du site en un fichier HTML unique, autonome.

L'enjeu du test : l'assemblage doit rester fidèle au site normal sans exiger de modifier
`web/`. Deux mécanismes fragiles à figer — l'interception de `fetch` pour servir les données
embarquées, et l'isolation par bloc des modules concaténés, sans laquelle les fonctions
homonymes entre fichiers (`frenchDate` dans card.js et dans app.js) casseraient la page.
"""

import re

import build_preview
import build_site
import pytest
from build_preview import MODULES, bundle_modules, embed_data, extract_body, strip_module_syntax


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    assert build_site.main(["--from-fixtures", "--out", str(out)]) == 0
    return out


def test_strip_module_syntax_retire_imports_et_exports():
    source = (
        "import MiniSearch from './vendor/minisearch/minisearch.js';\n"
        "import { a, b } from './card.js';\n"
        "export function f() { return 1; }\n"
        "export class C {}\n"
        "const local = 2;\n"
        "export { C as default };\n"
    )
    stripped = strip_module_syntax(source)

    assert "import" not in stripped
    assert "export" not in stripped
    # Les déclarations elles-mêmes survivent : seul le mot-clé disparaît.
    assert "function f()" in stripped
    assert "class C {}" in stripped
    assert "const local = 2;" in stripped


def test_bundle_modules_isole_chaque_module_dans_un_bloc(site):
    bundle = bundle_modules(site)

    assert bundle.count("const __m = {}") == 1
    for relative, _ in MODULES:
        assert f"// ---------- {relative} ----------" in bundle

    # Autant de blocs ouvrants que de modules, chacun refermé.
    assert bundle.count("\n{\n") == len(MODULES)
    assert bundle.count("\n}") >= len(MODULES)

    # Les bindings partagés sont publiés puis réinjectés dans les modules suivants.
    assert "__m.MiniSearch = MiniSearch;" in bundle
    assert "const MiniSearch = __m.MiniSearch;" in bundle
    assert "__m.MediaIndex = MediaIndex;" in bundle
    assert "const renderCard = __m.renderCard;" in bundle


def test_bundle_modules_ne_laisse_aucune_syntaxe_de_module(site):
    """Un `import` résiduel dans un script concaténé ferait échouer toute la page."""
    bundle = bundle_modules(site)

    assert not re.search(r"^\s*import\s", bundle, re.MULTILINE)
    assert not re.search(r"^\s*export\s", bundle, re.MULTILINE)


def test_les_homonymes_entre_modules_ne_collisionnent_pas(site):
    """`frenchDate` est défini dans card.js ET dans app.js : deux blocs, deux portées."""
    bundle = bundle_modules(site)

    assert bundle.count("function frenchDate(") == 2


def test_embed_data_couvre_index_meta_et_tous_les_lots(site):
    data = embed_data(site)

    assert "data/search.json" in data
    assert "data/meta.json" in data
    lots = [key for key in data if key.startswith("data/details/")]
    assert len(lots) == build_site.DETAIL_BUCKETS
    # Les clés doivent être exactement les chemins que `fetch` demandera.
    assert all(not key.startswith("/") for key in data)


def test_extract_body_retire_les_balises_de_script(site):
    body = extract_body((site / "index.html").read_text(encoding="utf-8"))

    assert "<script" not in body
    assert 'id="results"' in body
    assert "<body" not in body


def test_build_produit_un_document_autonome(site):
    html = build_preview.build(site, fragment=False)

    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    # Aucune ressource externe : ni feuille de style liée, ni script distant.
    assert 'rel="stylesheet"' not in html
    assert 'src="' not in html
    assert "window.__CPPAP_DATA__" in html
    assert "<title>" in html


def test_build_en_fragment_omet_le_squelette(site):
    fragment = build_preview.build(site, fragment=True)

    assert "<!doctype" not in fragment.lower()
    assert "<html" not in fragment.lower()
    assert "<title>" in fragment
    assert "window.__CPPAP_DATA__" in fragment


def test_un_apercu_sur_fixtures_porte_un_bandeau_inamovible(site):
    """Des données inventées ne doivent jamais pouvoir passer pour documentaires."""
    html = build_preview.build(site, fragment=False)

    assert "APERÇU D'INTERFACE" in html
    assert "inventées" in html


def test_pas_de_bandeau_sur_des_donnees_reelles():
    assert build_preview.fixtures_banner({"fixtures": False}) == ""
    assert build_preview.fixtures_banner({}) == ""


def test_main_echoue_proprement_sans_site(tmp_path, caplog):
    with caplog.at_level("ERROR"):
        code = build_preview.main(["--site", str(tmp_path), "--out", str(tmp_path / "a.html")])

    assert code == 1
    assert "build_site.py" in caplog.text
    assert not (tmp_path / "a.html").exists()


def test_main_ecrit_le_fichier(site, tmp_path):
    out = tmp_path / "apercu.html"
    assert build_preview.main(["--site", str(site), "--out", str(out)]) == 0
    assert out.stat().st_size > 50_000
