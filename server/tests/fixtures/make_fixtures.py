"""Build the synthetic raw uploads the intake and parser tests run against.

These are the only raw deal-shaped files in this repository, and they are the only ones
that will ever leave this machine, so **every number in them is invented** and the
property does not exist. Nothing here is derived from, measured against, or copied out of
a real deal — the point is to exercise a parser, not to describe a building.

Half of them are not documents at all but attacks: a macro-enabled workbook wearing an
``.xlsx`` name, an archive that declares three hundred megabytes of expansion, a
password-protected PDF, and one that is longer than the page cap. A gate is only worth
having if something has been thrown at it.

The generated files are committed. Regenerate with::

    python server/tests/fixtures/make_fixtures.py

``.xls`` needs ``xlwt``, which is a fixture-generation dependency and deliberately NOT in
``requirements.txt`` — the server reads ``.xls`` with ``xlrd`` and never writes one. If it
is missing the ``.xls`` fixture is skipped, loudly, and the committed copy stands.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEAL = "Wildflower Crossing"
UNITS = 24

# --------------------------------------------------------------------------------------
# A minimal PDF, written by hand
# --------------------------------------------------------------------------------------


def _escape_pdf(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(pages: list[list[str]]) -> bytes:
    """A valid PDF with real, extractable text and no third-party writer.

    ``reportlab`` would be one more pinned dependency for the sake of nine fixture pages.
    The format is simple enough to emit directly, and doing so keeps the runtime
    requirements to what the *server* needs.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = None
    content_ids: list[int] = []
    page_ids: list[int] = []

    # 1 catalog, 2 pages tree — reserved first so the ids are stable.
    objects.append(b"")  # 1: catalog, filled at the end
    objects.append(b"")  # 2: pages tree, filled at the end
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for lines in pages:
        stream_lines = ["BT", "/F1 11 Tf", "1 0 0 1 60 740 Tm", "14 TL"]
        for line in lines:
            stream_lines.append(f"({_escape_pdf(line)}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1")
        content_id = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        content_ids.append(content_id)
        page_ids.append(
            add(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 " + str(font_id).encode() + b" 0 R >> >> "
                b"/Contents " + str(content_id).encode() + b" 0 R >>"
            )
        )

    kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
    objects[1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_ids)).encode() + b" >>"
    )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


OM_PAGES = [
    [
        f"{DEAL} - Offering Memorandum",
        "A wholly invented property. Every figure below is made up for testing.",
        "",
        "Executive Summary",
        f"{DEAL} is a {UNITS}-unit build-for-rent community in a market that does not exist.",
        "Year built: 2021.  Occupancy: 93.8%.  Asking price: $7,400,000.",
        "Capitalization rate: 5.35% on in-place income.",
        "",
        "Property Overview",
        "Twenty-four detached homes on eleven acres, individually metered.",
        "Two-car garages, fenced yards, no shared corridors.",
    ],
    [
        "Unit Mix",
        "Plan      Homes   Beds   Baths   Sq Ft   Asking Rent",
        "A1            8      2       2     1150         2,145",
        "B2           10      3       2     1440         2,480",
        "C3            6      4       3     1785         2,910",
        "",
        "Rent Comparables",
        "Three competing communities within four miles, all invented.",
        "Average asking rent across the set: $2,412 per home.",
    ],
    [
        "Submarket",
        "Population within three miles: 41,200 people, growing 2.1% a year.",
        "Median household income: $88,400.",
        "",
        "Confidentiality",
        "This memorandum is a test artifact and describes no real asset.",
        "The broker named in it does not exist.",
    ],
]


def write_om_pdf(path: Path) -> None:
    path.write_bytes(build_pdf(OM_PAGES))


def write_long_pdf(path: Path, pages: int = 151) -> None:
    """One page past the cap, so the refusal is about the cap and not about the file."""
    body = [[f"Page {n} of {pages}. Filler for the page-count cap."] for n in range(1, pages + 1)]
    path.write_bytes(build_pdf(body))


def write_encrypted_pdf(path: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(build_pdf([["This page is behind a password."]])))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("not-the-password-you-have")
    with path.open("wb") as handle:
        writer.write(handle)


# --------------------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------------------


def write_om_docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading(f"{DEAL} - Offering Memorandum", level=1)
    document.add_paragraph(
        "A wholly invented property. Every figure below is made up for testing."
    )
    document.add_heading("Executive Summary", level=2)
    document.add_paragraph(
        f"{DEAL} is a {UNITS}-unit build-for-rent community. Asking price $7,400,000, a "
        f"capitalization rate of 5.35%, and 93.8% occupancy as of the date of this test."
    )
    document.add_heading("Unit Mix", level=2)
    table = document.add_table(rows=1, cols=5)
    for cell, label in zip(table.rows[0].cells, ["Plan", "Homes", "Beds", "Sq Ft", "Rent"]):
        cell.text = label
    for plan, homes, beds, sqft, rent in (
        ("A1", "8", "2", "1150", "2145"),
        ("B2", "10", "3", "1440", "2480"),
        ("C3", "6", "4", "1785", "2910"),
    ):
        cells = table.add_row().cells
        for cell, value in zip(cells, [plan, homes, beds, sqft, rent]):
            cell.text = value
    document.add_heading("Confidentiality", level=2)
    document.add_paragraph("A test artifact describing no real asset. The broker is invented.")
    document.save(str(path))


def write_short_docx(path: Path) -> None:
    """Too short and too plain to be an offering memorandum — should land in context."""
    import docx

    document = docx.Document()
    document.add_paragraph("Site visit is Tuesday at ten. Park on the north side.")
    document.save(str(path))


# --------------------------------------------------------------------------------------
# Spreadsheets
# --------------------------------------------------------------------------------------

T12_ROWS = [
    ("4010", "Gross Potential Rent", 62000),
    ("4020", "Gain / (Loss) to Lease", -2100),
    ("4100", "Vacancy", -4300),
    ("4110", "Concessions", -900),
    ("4200", "Utility Reimbursement", 3100),
    ("4300", "Other Income", 1750),
    ("4999", "Total Revenue", 59550),
    ("5010", "Payroll", 7200),
    ("5100", "Repairs & Maintenance", 4100),
    ("5200", "Contract Services", 2300),
    ("5300", "Marketing", 900),
    ("5400", "Administrative", 1400),
    ("5500", "Utilities", 3600),
    ("5600", "Management Fee", 1780),
    ("5700", "Insurance", 2600),
    ("5800", "Real Estate Taxes", 8900),
    ("5999", "Total Operating Expenses", 32780),
    ("6000", "Net Operating Income", 26770),
]


def _t12_months() -> list[dt.date]:
    return [dt.date(2025, 7, 1) + dt.timedelta(days=31 * n) for n in range(12)]


def write_t12_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "12MONTH"
    # A banner above the header, the way every real export does it.
    sheet["A1"] = "PROPERTY: WILDFLOWER CROSSING"
    sheet["A2"] = "12 Month Profit & Loss"
    sheet["A3"] = "Accrual basis. All figures invented."
    header = ["Account", "Description"] + [m.strftime("%b-%y") for m in _t12_months()]
    sheet.append([])
    sheet.append(header)
    for account, label, base in T12_ROWS:
        drift = [round(base * (1 + (n - 6) * 0.004), 2) for n in range(12)]
        sheet.append([account, label] + drift)
    workbook.save(str(path))


def write_t12_csv(path: Path) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["12 Month Profit & Loss - Wildflower Crossing (invented)"])
        writer.writerow([])
        writer.writerow(["Account", "Description"] + [m.strftime("%b-%y") for m in _t12_months()])
        for account, label, base in T12_ROWS:
            writer.writerow([account, label] + [round(base * (1 + (n - 6) * 0.004), 2) for n in range(12)])


RENT_ROLL_HEADER = [
    "Unit ID", "Floorplan", "Beds", "Baths", "Sqft", "Unit Status",
    "Lease Start", "Expiration", "Move In", "Lease Rent", "Market Rent",
]


def _rent_roll_rows() -> list[list]:
    plans = [("A1", 2, 2.0, 1150, 2145), ("B2", 3, 2.0, 1440, 2480), ("C3", 4, 3.0, 1785, 2910)]
    rows = []
    for index in range(UNITS):
        plan, beds, baths, sqft, market = plans[index % 3]
        occupied = index % 8 != 3
        start = dt.date(2024, 1, 15) + dt.timedelta(days=29 * index)
        rows.append(
            [
                f"{101 + index}",
                plan,
                beds,
                baths,
                sqft,
                "Occupied" if occupied else "Vacant",
                start.isoformat() if occupied else "",
                (start + dt.timedelta(days=365)).isoformat() if occupied else "",
                start.isoformat() if occupied else "",
                market - 55 if occupied else 0,
                market,
            ]
        )
    return rows


def _printed_sqft(rows) -> str:
    """The roll's printed square-footage line, computed from its own unit rows.

    A source that does not foot is a different fixture; this one must, because the chain
    contract uses it as an oracle.
    """
    sqft = RENT_ROLL_HEADER.index("Sqft")
    return f"Total Sqft: {sum(row[sqft] for row in rows):,}"


def write_rent_roll_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "RENTROLL"
    sheet["A1"] = "Wildflower Crossing - One Line Rent Roll (invented)"
    sheet["A2"] = _printed_sqft(_rent_roll_rows())
    sheet.append(RENT_ROLL_HEADER)
    for row in _rent_roll_rows():
        sheet.append(row)
    workbook.save(str(path))


#: The eight-unit roll plus a totals TRAILER, whose plan cell is blank.
#:
#: A csv and a legacy xls write a blank cell as ``""`` rather than None, and the trailer
#: filter asked ``plan is None`` — so "Total Units:" with the aggregate rent was accepted
#: as a ninth unit. The xls has to be a COMMITTED fixture because xlwt is
#: a fixture-generation dependency and is deliberately not in requirements.txt: the
#: container has no xlwt, and that is where this test also has to run.
TRAILER_HEADER = ["Unit ID", "Floorplan", "Unit Status", "Lease Rent"]
TRAILER_ROWS = [
    *[[f"{100 + i}", "A1", "Occupied", 1000 + i] for i in range(1, 9)],
    ["Total Units:", "", "", 8036],
]


#: The same four units and three trailer rows the round-18 three-format test builds as an
#: xlsx and a csv. Committed for the same reason as every other .xls here: xlwt is a
#: fixture-generation dependency and the container has none.
THREE_FORMAT_HEADER = ["Unit ID", "Floorplan", "Beds", "Baths", "Sqft", "Unit Status",
                       "Lease Rent", "Lease Start"]
THREE_FORMAT_ROWS = [
    ["101", "A1", 1, 1.0, 700, "Occupied", 1400, "2026-01-15"],
    ["102", "", 2, 2.0, 950, "Occupied", 1650, "2026-03-01"],
    ["103", "B2", 2, 2.0, 950, "Vacant", 0, ""],
    ["104", "B2", 2, 2.0, 950, "Occupied", 1700, "2025-11-20"],
    ["Total Units:", "", "", "", "", "", 4750, ""],
    ["", "", "", "", "", "", 4750, ""],
    ["", "", "", "", "", "Occupied", 4750, ""],
    ["   ", "  ", "", "", "", "", "", ""],
]


def write_three_format_roll_xls(path: Path) -> bool:
    try:
        import xlwt
    except ImportError:
        return False
    book = xlwt.Workbook()
    sheet = book.add_sheet("RENTROLL")
    for column, label in enumerate(THREE_FORMAT_HEADER):
        sheet.write(0, column, label)
    for index, row in enumerate(THREE_FORMAT_ROWS, start=1):
        for column, value in enumerate(row):
            sheet.write(index, column, value)
    book.save(str(path))
    return True


def write_rent_roll_trailer_xls(path: Path) -> bool:
    try:
        import xlwt
    except ImportError:
        return False
    book = xlwt.Workbook()
    sheet = book.add_sheet("RENTROLL")
    for column, label in enumerate(TRAILER_HEADER):
        sheet.write(0, column, label)
    for index, row in enumerate(TRAILER_ROWS, start=1):
        for column, value in enumerate(row):
            sheet.write(index, column, value)
    book.save(str(path))
    return True


def write_rent_roll_xls(path: Path) -> bool:
    try:
        import xlwt
    except ImportError:
        return False
    book = xlwt.Workbook()
    sheet = book.add_sheet("RENTROLL")
    sheet.write(0, 0, "Wildflower Crossing - One Line Rent Roll (invented)")
    for column, label in enumerate(RENT_ROLL_HEADER):
        sheet.write(2, column, label)
    for index, row in enumerate(_rent_roll_rows(), start=3):
        for column, value in enumerate(row):
            sheet.write(index, column, value)
    book.save(str(path))
    return True


# --------------------------------------------------------------------------------------
# Date-TYPED spreadsheets
#
# The three fixtures above write their dates as ISO text, which is what a CSV export does
# and what a workbook written by a script does. A workbook written by Excel — or by any
# property-management system exporting through it — writes a real date CELL, and the two
# take completely different routes out of a parser: openpyxl hands back a `datetime`,
# xlrd hands back a float plus a cell type. Both used to arrive downstream as something
# `_date()` refused, so lease-start and move-in silently became None on the exact file
# shape a visitor is most likely to upload. These exist so that path has a test.
# --------------------------------------------------------------------------------------

#: Columns 6, 7 and 8 of RENT_ROLL_HEADER: Lease Start, Expiration, Move In.
RENT_ROLL_DATE_COLUMNS = (6, 7, 8)


def _rent_roll_dated_rows() -> list[list]:
    """The same rows, with real ``date`` objects where the text version writes ISO."""
    rows = []
    for row in _rent_roll_rows():
        out = list(row)
        for column in RENT_ROLL_DATE_COLUMNS:
            text = out[column]
            out[column] = dt.date.fromisoformat(text) if text else None
        rows.append(out)
    return rows


def write_rent_roll_dated_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "RENTROLL"
    sheet["A1"] = "Wildflower Crossing - One Line Rent Roll (invented)"
    sheet["A2"] = _printed_sqft(_rent_roll_dated_rows())
    sheet.append(RENT_ROLL_HEADER)
    for row in _rent_roll_dated_rows():
        sheet.append(row)
    for column in RENT_ROLL_DATE_COLUMNS:
        for cell in sheet.iter_cols(min_col=column + 1, max_col=column + 1, min_row=4):
            for one in cell:
                one.number_format = "yyyy-mm-dd"
    workbook.save(str(path))


def write_rent_roll_dated_xls(path: Path) -> bool:
    try:
        import xlwt
    except ImportError:
        return False
    book = xlwt.Workbook()
    sheet = book.add_sheet("RENTROLL")
    dated = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    sheet.write(0, 0, "Wildflower Crossing - One Line Rent Roll (invented)")
    for column, label in enumerate(RENT_ROLL_HEADER):
        sheet.write(2, column, label)
    for index, row in enumerate(_rent_roll_dated_rows(), start=3):
        for column, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, dt.date):
                sheet.write(index, column, value, dated)
            else:
                sheet.write(index, column, value)
    book.save(str(path))
    return True


def write_wide_xls(path: Path) -> bool:
    """A legacy sheet one column past the width cap.

    Committed rather than generated in the test, for the same reason every other ``.xls``
    here is: ``xlwt`` is a fixture-generation dependency and is deliberately NOT in
    ``requirements.txt``, so it does not exist in the deploy image the suite also runs in.
    Small on purpose — the cell-budget test lowers ``DEMO_MAX_CELLS`` rather than shipping
    a four-hundred-thousand-cell file.
    """
    try:
        import xlwt
    except ImportError:
        return False
    book = xlwt.Workbook()
    sheet = book.add_sheet("WIDE")
    for column in range(201):
        sheet.write(0, column, f"c{column}")
    for row in range(1, 11):
        for column in range(201):
            sheet.write(row, column, column % 10)
    book.save(str(path))
    return True


def write_t12_dated_xls(path: Path) -> bool:
    """A legacy statement whose month headings are date cells, not text."""
    try:
        import xlwt
    except ImportError:
        return False
    book = xlwt.Workbook()
    sheet = book.add_sheet("12MONTH")
    dated = xlwt.easyxf(num_format_str="MMM-YY")
    sheet.write(0, 0, "PROPERTY: WILDFLOWER CROSSING")
    sheet.write(1, 0, "12 Month Profit & Loss (invented)")
    sheet.write(3, 0, "Account")
    sheet.write(3, 1, "Description")
    for column, month in enumerate(_t12_months(), start=2):
        sheet.write(3, column, month, dated)
    sheet.write(3, 14, "Total")
    for index, (account, label, base) in enumerate(T12_ROWS, start=4):
        sheet.write(index, 0, account)
        sheet.write(index, 1, label)
        total = 0.0
        for n in range(12):
            value = round(base * (1 + (n - 6) * 0.004), 2)
            sheet.write(index, 2 + n, value)
            total += value
        sheet.write(index, 14, round(total, 2))
    book.save(str(path))
    return True


def write_trade_out_csv(path: Path) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Unit", "Prior Rent", "New Rent", "Increase %", "Lease Type"])
        for index in range(12):
            prior = 2100 + index * 15
            new = prior + 80
            writer.writerow([101 + index, prior, new, round((new / prior - 1) * 100, 2), "Renewal"])


# --------------------------------------------------------------------------------------
# The attacks
# --------------------------------------------------------------------------------------

MACRO_CT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>'
    "</Types>"
)

PLAIN_CT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    "</Types>"
)


def write_macro_workbook_named_xlsx(path: Path) -> None:
    """A macro-enabled package with an ``.xlsx`` name. The rename is the whole attack."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", MACRO_CT)
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/vbaProject.bin", b"\x00" * 512)


def write_zip_bomb(path: Path, megabytes: int = 300) -> None:
    """One sheet part that declares ~300 MB of expansion from a few hundred kilobytes.

    The gate has to refuse this from the central directory alone. If it has to decompress
    to find out how big it is, it has already lost.
    """
    filler = ("<c r=\"A1\"><v>0</v></c>" * 4096).encode("utf-8")
    chunk_count = (megabytes * 1024 * 1024) // len(filler)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", PLAIN_CT)
        archive.writestr("xl/workbook.xml", "<workbook/>")
        info = zipfile.ZipInfo("xl/worksheets/sheet1.xml")
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w") as sheet:
            sheet.write(b"<worksheet><sheetData><row>")
            for _ in range(chunk_count):
                sheet.write(filler)
            sheet.write(b"</row></sheetData></worksheet>")


def write_plain_zip(path: Path) -> None:
    """A bare archive: right magic bytes, no Office manifest, must be refused as a type."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("notes.txt", "just a zip, not a document")


def write_not_a_document(path: Path) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


# --------------------------------------------------------------------------------------

BUILDERS = [
    ("om.pdf", write_om_pdf),
    ("om.docx", write_om_docx),
    ("site-note.docx", write_short_docx),
    ("t12.xlsx", write_t12_xlsx),
    ("t12.csv", write_t12_csv),
    ("rent-roll.xlsx", write_rent_roll_xlsx),
    ("rent-roll-dated.xlsx", write_rent_roll_dated_xlsx),
    ("trade-out.csv", write_trade_out_csv),
    ("macro-workbook-named-xlsx.xlsx", write_macro_workbook_named_xlsx),
    ("zip-bomb.xlsx", write_zip_bomb),
    ("plain.zip", write_plain_zip),
    ("encrypted.pdf", write_encrypted_pdf),
    ("too-many-pages.pdf", write_long_pdf),
    ("not-a-document.png", write_not_a_document),
]


def main() -> int:
    for name, builder in BUILDERS:
        target = HERE / name
        builder(target)
        print(f"  wrote {name:34s} {target.stat().st_size:>10,} bytes")

    legacy = [
        ("rent-roll.xls", write_rent_roll_xls),
        ("rent-roll-dated.xls", write_rent_roll_dated_xls),
        ("t12-dated.xls", write_t12_dated_xls),
        ("wide.xls", write_wide_xls),
        ("rent-roll-trailer.xls", write_rent_roll_trailer_xls),
        ("three-format-roll.xls", write_three_format_roll_xls),
    ]
    for name, builder in legacy:
        target = HERE / name
        if builder(target):
            print(f"  wrote {name:34s} {target.stat().st_size:>10,} bytes")
        elif target.exists():
            print(f"  SKIP  {name} — xlwt not installed; committed copy left in place")
        else:
            print(f"  MISSING {name} — install xlwt and re-run: pip install xlwt")
            return 1

    print(f"\n{len(BUILDERS) + len(legacy)} fixtures in {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
