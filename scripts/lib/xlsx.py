"""Lecteur XLSX minimal (zipfile + ElementTree), sans dépendance externe.

L'API tabulaire de data.gouv.fr convertit déjà les tableurs en CSV, ce qui est la voie
normale. Ce module n'existe que pour le repli : si une ressource n'est pas indexée par
l'API tabulaire et n'est publiée qu'en XLSX, l'ingestion doit tout de même aboutir plutôt
que d'échouer pour une raison de format.

Couvre ce que produisent les tableurs administratifs : chaînes partagées, valeurs inline,
nombres, et cellules vides implicites (attribut `r` non contigu).
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

_CELL_REF = re.compile(r"^([A-Z]+)")


def _column_index(ref: str) -> int:
    """« A » -> 0, « B » -> 1, « AA » -> 26."""
    match = _CELL_REF.match(ref)
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(raw)
    strings: list[str] = []
    for item in root.findall("main:si", _NS):
        # Une chaîne peut être fragmentée en plusieurs <t> (runs de mise en forme).
        strings.append("".join(node.text or "" for node in item.iter(f"{{{_NS['main']}}}t")))
    return strings


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    names = [n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    if not names:
        raise ValueError("Aucune feuille de calcul trouvée dans le fichier XLSX")
    return sorted(names, key=lambda n: int(re.search(r"(\d+)", n).group(1)))[0]


def read_xlsx_rows(data: bytes) -> list[list[str]]:
    """Retourne les lignes de la première feuille sous forme de listes de chaînes."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        strings = _shared_strings(archive)
        root = ElementTree.fromstring(archive.read(_first_sheet_path(archive)))

    rows: list[list[str]] = []
    for row in root.iter(f"{{{_NS['main']}}}row"):
        cells: list[str] = []
        for cell in row.findall("main:c", _NS):
            position = _column_index(cell.get("r", ""))
            # Comble les cellules vides omises par le producteur du fichier.
            while len(cells) < position:
                cells.append("")

            cell_type = cell.get("t")
            if cell_type == "inlineStr":
                text = "".join(n.text or "" for n in cell.iter(f"{{{_NS['main']}}}t"))
            else:
                value = cell.find("main:v", _NS)
                text = value.text or "" if value is not None else ""
                if cell_type == "s" and text.isdigit():
                    index = int(text)
                    text = strings[index] if index < len(strings) else ""
            cells.append(text.strip())

        if any(cells):
            rows.append(cells)

    return rows
