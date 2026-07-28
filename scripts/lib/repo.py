"""Chemins du dépôt et chargement de la configuration des sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

CONFIG_FILE = ROOT / "config" / "sources.json"

DATA = ROOT / "data"
DATA_LATEST = DATA / "latest"
DATA_RAW = DATA / "raw"
MANIFEST_FILE = DATA / "manifest.json"
SIRENE_DIR = DATA / "sirene"
SIRENE_CACHE_FILE = SIRENE_DIR / "cache.json"
SIRENE_OVERRIDES_FILE = SIRENE_DIR / "overrides.csv"

WEB = ROOT / "web"
SITE = ROOT / "site"
FIXTURES = ROOT / "tests" / "fixtures"

DATAGOUV_API = "https://www.data.gouv.fr/api/1"
TABULAR_API = "https://tabular-api.data.gouv.fr/api"
RECHERCHE_ENTREPRISES_API = "https://recherche-entreprises.api.gouv.fr"
ANNUAIRE_ENTREPRISES = "https://annuaire-entreprises.data.gouv.fr/entreprise"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Charge config/sources.json en ignorant les clés de commentaire."""
    config = json.loads((path or CONFIG_FILE).read_text(encoding="utf-8"))
    if not config.get("sources"):
        raise ValueError(f"Aucune source déclarée dans {path or CONFIG_FILE}")
    return config


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    """Écrit du JSON UTF-8 déterministe (diffs git lisibles)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
