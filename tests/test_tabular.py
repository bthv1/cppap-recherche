"""Lecture des fichiers publiés : détection d'encodage et de séparateur, parsing XLSX.

Les fichiers de l'administration arrivent souvent en CP1252 avec point-virgule, mais pas
toujours, et l'encodage peut changer d'une publication à l'autre. On détecte au lieu de
supposer, et ces tests figent la détection.
"""

import io
import zipfile

import pytest
from lib.tabular import decode_text, read_csv_rows, read_rows, rows_to_csv, sniff_delimiter
from lib.xlsx import read_xlsx_rows

HEADER = "N° CPPAP;Titre;Éditeur"
LINE = "0722 C 83260;Le Monde;SOCIÉTÉ ÉDITRICE DU MONDE"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252", "latin-1"])
def test_decode_text_retrouve_les_accents(encoding):
    text, detected = decode_text(f"{HEADER}\n{LINE}\n".encode(encoding))

    assert "Éditeur" in text
    assert "SOCIÉTÉ ÉDITRICE DU MONDE" in text
    assert detected in {"utf-8", "utf-8-sig", "cp1252", "latin-1"}


def test_decode_text_reconnait_le_bom():
    _, detected = decode_text("a;b\n".encode("utf-8-sig"))
    assert detected == "utf-8-sig"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("a;b;c", ";"),
        ("a,b,c", ","),
        ("a\tb\tc", "\t"),
        ("a|b|c", "|"),
        # Séparateur ambigu : le plus fréquent gagne (les valeurs contiennent des virgules).
        ("a;b,c;d,e", ";"),
        # Colonne unique : aucun séparateur détecté, on retombe sur la virgule.
        ("colonne", ","),
    ],
)
def test_sniff_delimiter(line, expected):
    assert sniff_delimiter(f"{line}\nx\n") == expected


def test_read_csv_rows_nettoie_et_ignore_les_lignes_vides():
    raw = f"{HEADER}\n\n{LINE}\n;;\n".encode("cp1252")
    rows, fmt = read_csv_rows(raw)

    assert fmt["delimiter"] == ";"
    assert len(rows) == 2
    assert rows[0][0] == "N° CPPAP"
    assert rows[1][1] == "Le Monde"


def test_rows_to_csv_produit_un_csv_canonique():
    rows = [["a", "b"], ["1", "valeur, avec virgule"], ["2"]]
    text = rows_to_csv(rows)

    assert text.endswith("\n")
    assert "\r" not in text
    # Le séparateur canonique étant la virgule, une valeur qui en contient est protégée.
    assert '"valeur, avec virgule"' in text
    # Un point-virgule, en revanche, n'a pas besoin de l'être : pas de citation superflue.
    assert "1;2" in rows_to_csv([["1;2"]])
    # Les lignes courtes sont complétées pour que toutes aient la même largeur.
    assert text.splitlines()[-1] == "2,"


def test_rows_to_csv_est_relisible():
    rows = [["N° CPPAP", "Titre"], ["0722 C 83260", 'Le "Monde"']]
    relu, _ = read_csv_rows(rows_to_csv(rows).encode("utf-8"))
    assert relu == rows


def minimal_xlsx(rows):
    """Fabrique un XLSX réduit, avec chaînes partagées et cellules vides implicites."""
    strings = []
    for row in rows:
        for cell in row:
            if cell and cell not in strings:
                strings.append(cell)

    def sheet_xml():
        parts = [
            '<?xml version="1.0"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            "<sheetData>",
        ]
        for r, row in enumerate(rows, start=1):
            parts.append(f'<row r="{r}">')
            for c, cell in enumerate(row):
                if not cell:
                    continue  # cellule omise : le lecteur doit la recréer depuis l'attribut r
                ref = f"{chr(ord('A') + c)}{r}"
                parts.append(f'<c r="{ref}" t="s"><v>{strings.index(cell)}</v></c>')
            parts.append("</row>")
        parts.append("</sheetData></worksheet>")
        return "".join(parts)

    shared = (
        '<?xml version="1.0"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml())
    return buffer.getvalue()


def test_read_xlsx_rows_lit_les_chaines_partagees():
    rows = [["N° CPPAP", "Titre"], ["0722 C 83260", "Le Monde"]]
    assert read_xlsx_rows(minimal_xlsx(rows)) == rows


def test_read_xlsx_rows_recree_les_cellules_vides_omises():
    """Une cellule vide n'est pas écrite dans le XML : l'alignement vient de l'attribut `r`."""
    rows = [["A", "B", "C"], ["1", "", "3"]]
    parsed = read_xlsx_rows(minimal_xlsx(rows))

    assert parsed[1] == ["1", "", "3"]


def test_read_rows_choisit_le_lecteur_selon_l_extension():
    rows = [["A", "B"], ["1", "2"]]
    parsed, fmt = read_rows(minimal_xlsx(rows), "liste.xlsx")

    assert fmt["parser"] == "xlsx"
    assert parsed == rows

    parsed_csv, fmt_csv = read_rows(b"A;B\n1;2\n", "liste.csv")
    assert fmt_csv["parser"] == "csv"
    assert parsed_csv == rows


def test_read_xlsx_rows_refuse_une_archive_sans_feuille():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("docProps/app.xml", "<x/>")

    with pytest.raises(ValueError, match="feuille"):
        read_xlsx_rows(buffer.getvalue())
