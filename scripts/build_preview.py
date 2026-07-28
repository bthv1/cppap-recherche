#!/usr/bin/env python3
"""Assemble le site en un **fichier HTML unique**, autonome et consultable hors ligne.

Deux usages :

- partager ou archiver l'outil tel qu'il était à une date donnée, sans serveur — un fichier
  qui s'ouvre dans un navigateur suffit ;
- publier un aperçu là où l'on ne peut déposer qu'une seule page.

Le site normal est volontairement découpé (index compact plus 32 lots de détail chargés à la
demande) : cet assemblage embarque tout, il est donc plus lourd et n'a pas d'intérêt pour la
publication habituelle sur GitHub Pages.

Deux mécanismes suffisent, et permettent de ne modifier **aucun** fichier de `web/` :

1. les données JSON sont embarquées et `fetch` est intercepté pour les servir ;
2. les modules ES sont concaténés, chacun dans un bloc `{ }` pour isoler ses déclarations —
   sans quoi les homonymes entre fichiers (`frenchDate` existe dans card.js et dans app.js)
   provoqueraient une erreur de redéclaration.

Usage :
    python scripts/build_site.py --from-fixtures
    python scripts/build_preview.py --out apercu.html
    python scripts/build_preview.py --fragment --out fragment.html
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import repo

log = logging.getLogger("build_preview")

# Ordre de dépendance, et bindings que chaque module doit exposer aux suivants.
MODULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("vendor/minisearch/minisearch.js", ("MiniSearch",)),
    ("search.js", ("MediaIndex",)),
    ("card.js", ("renderCard", "attachCardBehaviour", "esc")),
    ("app.js", ()),
)

# Lignes d'import/export à retirer : le liage se fait par l'objet partagé `__m`.
_IMPORT_LINE = re.compile(r"^\s*import\s.*?;\s*$", re.MULTILINE | re.DOTALL)
_EXPORT_KEYWORD = re.compile(
    r"^export\s+(?=(?:default\s+)?(?:function|class|const|let|var)\b)", re.MULTILINE
)
_EXPORT_STATEMENT = re.compile(r"^\s*export\s*\{[^}]*\}\s*;\s*$", re.MULTILINE)


def strip_module_syntax(source: str) -> str:
    """Retire imports et exports pour rendre le module concaténable."""
    source = _IMPORT_LINE.sub("", source)
    source = _EXPORT_STATEMENT.sub("", source)
    return _EXPORT_KEYWORD.sub("", source)


def bundle_modules(site: Path) -> str:
    """Concatène les modules, chacun dans son bloc, en publiant ses bindings dans `__m`."""
    parts = ["const __m = {};"]
    for relative, exports in MODULES:
        path = site / relative
        if not path.exists():
            raise FileNotFoundError(f"{path} absent — lancez d'abord scripts/build_site.py")

        body = strip_module_syntax(path.read_text(encoding="utf-8"))
        needed = sorted({name for _, names in MODULES for name in names} - set(exports))

        parts.append(f"// ---------- {relative} ----------")
        parts.append("{")
        # Rend disponibles les bindings des modules déjà traités.
        for name in needed:
            parts.append(f"  const {name} = __m.{name};")
        parts.append(body)
        for name in exports:
            parts.append(f"  __m.{name} = {name};")
        parts.append("}")
    return "\n".join(parts)


def embed_data(site: Path) -> dict[str, object]:
    """Rassemble les JSON du site sous les chemins que `fetch` réclamera."""
    data_dir = site / "data"
    embedded: dict[str, object] = {}
    for path in sorted(data_dir.rglob("*.json")):
        key = path.relative_to(site).as_posix()
        embedded[key] = json.loads(path.read_text(encoding="utf-8"))
    return embedded


def extract_body(index_html: str) -> str:
    """Contenu de <body>, débarrassé de la balise de script (les modules sont embarqués)."""
    match = re.search(r"<body[^>]*>(.*)</body>", index_html, re.DOTALL | re.IGNORECASE)
    body = match.group(1) if match else index_html
    return re.sub(
        r"<script\b[^>]*></script>|<script\b[^>]*>.*?</script>", "", body, flags=re.DOTALL
    )


def extract_title(index_html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", index_html, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else "Recherche CPPAP"


def fixtures_banner(meta: dict) -> str:
    """Bandeau inamovible quand l'aperçu est bâti sur des données synthétiques."""
    if not meta.get("fixtures"):
        return ""
    return (
        '<p style="margin:0;padding:0.85rem 1rem;background:#8a1a08;color:#fff;'
        'font-weight:700;text-align:center;line-height:1.4">'
        "APERÇU D'INTERFACE — les données affichées sont inventées&nbsp;: numéros CPPAP, "
        "SIRET, adresses et dirigeants sont des exemples de test, sans aucune valeur "
        "documentaire.</p>"
    )


def build(site: Path, *, fragment: bool) -> str:
    index_html = (site / "index.html").read_text(encoding="utf-8")
    styles = (site / "styles.css").read_text(encoding="utf-8")
    data = embed_data(site)
    meta = data.get("data/meta.json") or {}

    shim = f"""
window.__CPPAP_DATA__ = {json.dumps(data, ensure_ascii=False, separators=(",", ":"))};
// Les données sont embarquées : on intercepte fetch pour les servir, ce qui laisse le code
// de web/ rigoureusement inchangé — un seul comportement à maintenir, pas deux.
(() => {{
  const nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = (input, init) => {{
    const key = String(input && input.url ? input.url : input).replace(/^\\.?\\//, "");
    if (Object.prototype.hasOwnProperty.call(window.__CPPAP_DATA__, key)) {{
      return Promise.resolve(new Response(JSON.stringify(window.__CPPAP_DATA__[key]), {{
        status: 200,
        headers: {{ "Content-Type": "application/json; charset=utf-8" }},
      }}));
    }}
    if (nativeFetch) return nativeFetch(input, init);
    return Promise.reject(new Error(`Ressource non embarquée : ${{key}}`));
  }};
}})();
""".strip()

    content = (
        f"<title>{extract_title(index_html)}</title>\n"
        f"<style>\n{styles}\n</style>\n"
        f"{fixtures_banner(meta)}\n"
        f"{extract_body(index_html)}\n"
        f"<script>\n{shim}\n</script>\n"
        f'<script type="module">\n{bundle_modules(site)}\n</script>\n'
    )

    if fragment:
        return content
    return (
        '<!doctype html>\n<html lang="fr">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"</head>\n<body>\n{content}</body>\n</html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=repo.SITE, help="Répertoire du site généré")
    parser.add_argument("--out", type=Path, required=True, help="Fichier HTML à écrire")
    parser.add_argument(
        "--fragment",
        action="store_true",
        help="Omettre <html>/<head>/<body> (pour un hébergeur qui fournit le squelette)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if not (args.site / "index.html").exists():
        log.error("%s ne contient pas de site — lancez d'abord scripts/build_site.py", args.site)
        return 1

    html = build(args.site, fragment=args.fragment)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")

    log.info("Aperçu écrit dans %s (%.0f Ko)", args.out, len(html.encode("utf-8")) / 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
