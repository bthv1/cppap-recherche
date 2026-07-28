"""Lecture des fichiers publiés : détection d'encodage, de séparateur, et parsing XLSX.

Les CSV administratifs français arrivent le plus souvent en CP1252 avec un point-virgule
comme séparateur, mais pas toujours — et l'encodage peut changer d'une publication à
l'autre. On détecte plutôt que de supposer.
"""

from __future__ import annotations

import csv
import io
import logging

from .xlsx import read_xlsx_rows

log = logging.getLogger(__name__)

# Ordre volontaire : `latin-1` ne lève jamais, il termine donc la chaîne de replis.
CANDIDATE_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

CANDIDATE_DELIMITERS = (";", ",", "\t", "|")


def decode_text(data: bytes) -> tuple[str, str]:
    """Décode en essayant les encodages plausibles. Retourne (texte, encodage retenu)."""
    for encoding in CANDIDATE_ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # Inatteignable en pratique : latin-1 accepte tous les octets.
    return data.decode("latin-1", errors="replace"), "latin-1"


def sniff_delimiter(text: str) -> str:
    """Choisit le séparateur le plus fréquent sur la première ligne non vide."""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    counts = {d: first_line.count(d) for d in CANDIDATE_DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def read_csv_rows(data: bytes) -> tuple[list[list[str]], dict[str, str]]:
    """Parse un CSV en lignes de chaînes. Retourne (lignes, informations de format)."""
    text, encoding = decode_text(data)
    delimiter = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = [[(cell or "").strip() for cell in row] for row in reader]
    rows = [row for row in rows if any(row)]
    return rows, {"encoding": encoding, "delimiter": delimiter, "parser": "csv"}


def read_rows(data: bytes, filename: str) -> tuple[list[list[str]], dict[str, str]]:
    """Parse un fichier publié (CSV ou tableur) en lignes de chaînes."""
    lowered = filename.lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        rows = read_xlsx_rows(data)
        return rows, {"encoding": "n/a", "delimiter": "n/a", "parser": "xlsx"}
    return read_csv_rows(data)


def rows_to_csv(rows: list[list[str]]) -> str:
    """Sérialise en CSV canonique : UTF-8, virgule, fins de ligne LF.

    Cette forme n'est pas l'archive (l'archive conserve les octets publiés) mais la vue
    normalisée versionnée dans data/latest/, dont les diffs git sont lisibles.
    """
    width = max((len(row) for row in rows), default=0)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=",", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    for row in rows:
        writer.writerow([*row, *[""] * (width - len(row))])
    return buffer.getvalue()
