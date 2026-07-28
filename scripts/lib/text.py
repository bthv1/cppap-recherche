"""Normalisation de texte et mesure de similarité, bibliothèque standard uniquement.

Ce module porte tout ce qui permet de rapprocher une raison sociale CPPAP d'une
dénomination SIRENE : les deux sources écrivent les mêmes entreprises différemment
(accents, ponctuation, place de la forme juridique, abréviations).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# NFKD ne décompose pas ces caractères : on les traite explicitement avant.
#
# Rien ici pour les apostrophes typographiques ni l'espace insécable : `fold` réduit déjà
# tout caractère non alphanumérique à une espace, les lister serait redondant — et deux
# apostrophes courbes se réduisent trop facilement à une seule clé au fil des éditions.
_LIGATURES = {
    "\u0153": "oe",
    "\u0152": "OE",
    "\u00e6": "ae",
    "\u00c6": "AE",
    "\u00df": "ss",
    "\ufb01": "fi",
    "\ufb02": "fl",
}

_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# Marqueurs de forme juridique retirés uniquement en début ou fin de dénomination :
# « SARL LE MONDE » et « LE MONDE SARL » désignent « LE MONDE », mais un token isolé
# au milieu d'un nom peut être significatif, on ne le touche pas.
LEGAL_FORM_TOKENS = frozenset(
    {
        "sa",
        "sas",
        "sasu",
        "sarl",
        "sarlu",
        "eurl",
        "snc",
        "sci",
        "scs",
        "sca",
        "scp",
        "scm",
        "scop",
        "scic",
        "scea",
        "sel",
        "selarl",
        "selas",
        "selafa",
        "sem",
        "spl",
        "spla",
        "gie",
        "geie",
        "ei",
        "eirl",
        "earl",
        "epic",
        "association",
        "assoc",
        "fondation",
        "cooperative",
    }
)


def strip_accents(value: str) -> str:
    """Retire les diacritiques et déplie les ligatures latines."""
    for src, dst in _LIGATURES.items():
        value = value.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold(value: str | None) -> str:
    """Forme canonique comparable : sans accent, minuscule, séparateurs réduits.

    « N° CPPAP » -> « n cppap » ; « Département du siège » -> « departement du siege ».
    """
    if not value:
        return ""
    folded = strip_accents(str(value)).lower()
    return _NON_ALNUM.sub(" ", folded).strip()


def normalize_header(value: str | None) -> str:
    """Alias explicite de `fold`, utilisé pour l'appariement des en-têtes de colonnes."""
    return fold(value)


def strip_legal_form(value: str) -> str:
    """Retire les marqueurs de forme juridique aux extrémités, de façon répétée.

    Attend une chaîne déjà passée par `fold`.
    """
    tokens = value.split()
    changed = True
    while changed and tokens:
        changed = False
        if tokens and tokens[0] in LEGAL_FORM_TOKENS:
            tokens.pop(0)
            changed = True
        if tokens and tokens[-1] in LEGAL_FORM_TOKENS:
            tokens.pop()
            changed = True
    # Si la dénomination n'était QUE la forme juridique, mieux vaut garder l'original.
    return " ".join(tokens) if tokens else value


def normalize_company(value: str | None) -> str:
    """Clé de comparaison d'une dénomination d'entreprise."""
    return strip_legal_form(fold(value))


def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _sorted_tokens(value: str) -> str:
    return " ".join(sorted(value.split()))


def token_set_ratio(a: str, b: str) -> float:
    """Similarité tolérante à l'ordre des mots, mais pénalisant les tokens en trop.

    Mélange délibéré de deux mesures :

    - `sort_ratio` compare les tokens triés : symétrique, pénalise un nom plus long ;
    - `set_ratio` compare via l'intersection : tolère qu'un nom soit préfixe de l'autre.

    La pondération évite le piège classique du `token_set_ratio` pur, qui donnerait 1.0
    à « LE MONDE » vs « LE MONDE INTERACTIF » — deux entités juridiques distinctes qui
    doivent ressortir en appariement douteux, pas en correspondance certaine.
    """
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0

    inter = sorted(ta & tb)
    only_a = sorted(ta - tb)
    only_b = sorted(tb - ta)

    s0 = " ".join(inter)
    s1 = " ".join(inter + only_a).strip()
    s2 = " ".join(inter + only_b).strip()
    set_ratio = max(_ratio(s0, s1), _ratio(s0, s2), _ratio(s1, s2))

    sort_ratio = _ratio(_sorted_tokens(a), _sorted_tokens(b))

    return round(0.65 * sort_ratio + 0.35 * set_ratio, 4)


def slugify(value: str | None, fallback: str = "sans-nom") -> str:
    """Identifiant sûr pour une URL ou un nom de fichier."""
    folded = fold(value)
    slug = re.sub(r"\s+", "-", folded).strip("-")
    return slug or fallback
