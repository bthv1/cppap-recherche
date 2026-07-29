"""Lecture d'un numéro CPPAP, écrit différemment selon la liste qui le publie.

Source unique de vérité sur l'anatomie de ce numéro, comme `lib/resolution.py` l'est pour
l'ordre de rattachement à SIRENE.

Un numéro CPPAP complet se compose de trois parties :

```
1026        Y            90833
MMAA        lettre       n° d'inscription (permanent)
expiration  rubrique
```

Les deux listes ne publient pas la même chose (vérifié sur les fichiers réels) :

- **liste des services de presse en ligne** — ressource data.gouv, en-têtes humains : la forme
  complète, `0330 W 95411`, sur ses 1 242 lignes ;
- **liste des publications de presse** — export de base du ministère de la Culture, en-têtes
  `snake_case` : le seul n° d'inscription, précédé d'un préfixe de deux chiffres **constant sur
  ses 26 669 lignes** (`2595411`). Les deux composants abandonnés ne sont pas perdus, ils
  figurent dans des colonnes dédiées (`date_expiration_inscription`, `qualification`).

Ce n'est donc ni une cellule mal formatée ni un zéro initial perdu : c'est un même numéro écrit
de deux façons. Le **n° d'inscription** est la partie permanente — la date d'expiration et la
lettre changent à chaque renouvellement — et c'est donc lui qui sert de clé de rapprochement
entre les listes.

Le préfixe de la liste des publications est *observé*, jamais supposé : `parse_cppap` le renvoie
tel quel et l'appelant vérifie qu'une source n'en présente qu'un seul. S'il devait s'en présenter
plusieurs, le n° d'inscription cesserait d'être une clé sûre et le rapprochement doit être
abandonné plutôt que risqué.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

# Formes reconnues, comparées sur l'écriture débarrassée de tout séparateur : les sources
# comme les lecteurs écrivent indifféremment « 0330 W 95411 », « 0330-W-95411 » ou
# « 0330W95411 », et ces trois-là désignent le même agrément.
_COMPLETE = re.compile(r"^(\d{2})(\d{2})([A-Z])(\d{5})$")
# Forme de la liste des publications : préfixe de 2 chiffres + n° d'inscription de 5 chiffres.
_PREFIXED = re.compile(r"^(\d{2})(\d{5})$")
# Un « n° » en tête de cellule n'appartient pas au numéro. Retiré avant toute lecture pour
# qu'un tel ajout en amont ne rende pas toute une liste soudainement irrapprochable.
_NUMBER_SIGN = re.compile(r"^N(?=\d)")
# Longueur du n° d'inscription, identique dans les deux listes.
SERIE_LENGTH = 5

FORME_COMPLETE = "complete"
FORME_SERIE_PREFIXEE = "serie_prefixee"
FORME_SERIE = "serie"
FORME_INCONNUE = "inconnue"


@dataclass(frozen=True)
class CppapNumber:
    """Décomposition d'un numéro CPPAP. `raw` est toujours l'écriture de la source."""

    raw: str = ""
    serie: str = ""
    lettre: str = ""
    expiration: str = ""
    prefixe: str = ""
    forme: str = FORME_INCONNUE

    @property
    def is_joinable(self) -> bool:
        """Un numéro n'est rapprochable d'une autre liste que s'il livre son n° d'inscription."""
        return bool(self.serie)


def _clean(value: str) -> str:
    """Réduit tout séparateur à une espace simple et passe en majuscules."""
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", (value or "").strip()).strip()
    return re.sub(r"\s+", " ", cleaned).upper()


def month_end(month: int, year: int) -> str:
    """Dernier jour du mois, au format ISO — la CPPAP fait expirer en fin de mois.

    Vérifié : l'expiration ainsi déduite du numéro SPEL égale la colonne
    `date_expiration_inscription` de la liste des publications sur les 1 241 titres communs,
    sans une divergence.
    """
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def parse_cppap(value: str) -> CppapNumber:
    """Décompose un numéro CPPAP, quelle que soit la liste qui l'écrit.

    Ne devine rien : une écriture non reconnue est renvoyée telle quelle, sans n° d'inscription,
    donc simplement non rapprochable. Mieux vaut une fiche isolée qu'un rapprochement inventé.
    """
    raw = _clean(value)
    if not raw:
        return CppapNumber()

    compact = _NUMBER_SIGN.sub("", raw.replace(" ", ""))

    complete = _COMPLETE.match(compact)
    if complete:
        month, year_short, lettre, serie = complete.groups()
        month_num = int(month)
        # Un mois hors 1-12 signale une écriture que l'on ne comprend pas : on garde le
        # n° d'inscription, qui reste lisible, mais on n'affirme aucune date d'expiration.
        expiration = month_end(month_num, 2000 + int(year_short)) if 1 <= month_num <= 12 else ""
        return CppapNumber(
            # Écriture canonique, groupes séparés : c'est celle qui figure dans l'ours d'un
            # journal, et la seule qui se lise sans compter les chiffres.
            raw=f"{month}{year_short} {lettre} {serie}",
            serie=serie,
            lettre=lettre,
            expiration=expiration,
            forme=FORME_COMPLETE,
        )

    prefixed = _PREFIXED.match(compact)
    if prefixed:
        prefixe, serie = prefixed.groups()
        return CppapNumber(raw=compact, serie=serie, prefixe=prefixe, forme=FORME_SERIE_PREFIXEE)

    if re.fullmatch(rf"\d{{{SERIE_LENGTH}}}", compact):
        return CppapNumber(raw=compact, serie=compact, forme=FORME_SERIE)

    return CppapNumber(raw=raw, forme=FORME_INCONNUE)


def writings(number: CppapNumber) -> list[str]:
    """Écritures sous lesquelles un lecteur peut rencontrer ce numéro.

    Sert à ce qu'une recherche aboutisse quelle que soit la source consultée : la forme
    complète relevée dans un ours de journal, l'écriture préfixée du fichier ministériel, ou
    le seul n° d'inscription.
    """
    found = [w for w in (number.raw, number.serie) if w]
    seen: dict[str, None] = {}
    for item in found:
        seen.setdefault(item, None)
    return list(seen)
