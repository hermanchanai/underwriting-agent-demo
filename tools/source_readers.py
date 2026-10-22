"""Deterministic readers for the raw source exports: the T-12 statement and the rent roll.

The model layer (``server/extract_llm.py``) decides WHICH column is the unit id and WHICH
account line is "Payroll". Everything numeric is read here, by code, out of the cells:

* ``parse_t12`` reads a 12-month property P&L, skipping the page-break header blocks some
  accounting exports repeat every ~50 rows, and maps every account line onto a vendored
  category (``schema.T12_*_CATEGORIES``);
* ``parse_rent_roll`` reads a one-line rent roll, **dropping any resident-name column
  outright** and de-duplicating units an export lists twice;
* ``parse_source_totals`` reads the totals the export itself prints, so every downstream
  number can be tied back to the source's own arithmetic.

No value is ever re-typed by a model, so a mislabelled column produces a visibly wrong row
rather than a plausible invented one.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import load_workbook

import schema

# --------------------------------------------------------------------------------------
# T-12: account -> vendored category
#
# Keyed on the export's account code, which is stable, rather than on its wording. A code
# absent from this map is reported by the equality check rather than silently dropped.
# --------------------------------------------------------------------------------------

T12_ACCOUNT_CATEGORY: dict[str, str] = {
    # ---- rental income ---------------------------------------------------------------
    "1016": "Gross Potential Rent",
    "1017": "Gain / (Loss) to Lease",
    "1020": "Vacancy",
    "1030": "Non-Revenue Units",
    "1040": "Non-Revenue Units",
    "1052": "Concessions",
    # ---- utility reimbursements ------------------------------------------------------
    "3001": "Utility Reimbursements",
    "3050": "Utility Reimbursements",
    "3183": "Utility Reimbursements",
    "3186": "Utility Reimbursements",
    "3187": "Utility Reimbursements",
    "3198": "Utility Reimbursements",
    # ---- delinquency (bad debt) ------------------------------------------------------
    "3086": "Delinquency",
    "3081": "Delinquency",
    "3084": "Delinquency",
    "3085": "Delinquency",
    "3087": "Delinquency",
    "3088": "Delinquency",
    "3089": "Delinquency",
    "3091": "Delinquency",
    # ---- other income ----------------------------------------------------------------
    **{
        code: "Other Income"
        for code in (
            "3011", "3012", "3030", "3036", "3040", "3053", "3060", "3066", "3070",
            "3080", "3112", "3117", "3118", "3119", "3121", "3156", "3157", "3175",
            "3176", "3179", "3180", "3192",
        )
    },
    # ---- leasing and marketing --------------------------------------------------------
    **{c: "Leasing and Marketing" for c in ("5010", "5020", "5070", "5090", "5099", "5120", "5160")},
    # ---- G&A / legal -----------------------------------------------------------------
    **{
        c: "General and Administrative"
        for c in (
            "5410", "5430", "5440", "5455", "5460", "5470", "5475", "5480", "5515",
            "5530", "5531", "5535", "5550", "5590", "5514",
        )
    },
    "5510": "Legal and Professional",
    "5514": "Legal and Professional",
    "5525": "General and Administrative",
    # MRI is the accounting platform: a software fee, not a professional service.
    "5514_unused": "General and Administrative",
    # ---- management fee ---------------------------------------------------------------
    "5700": "Management Fee",
    "5740": "Management Fee",
    # ---- payroll ----------------------------------------------------------------------
    **{
        c: "Payroll"
        for c in (
            "5810", "5815", "5820", "5830", "5835", "5840", "5855", "5860", "5870",
            "5875", "5880", "5882", "5885",
        )
    },
    # ---- landscaping ------------------------------------------------------------------
    "6010": "Landscaping",
    "6510": "Landscaping",
    # ---- unit turns -------------------------------------------------------------------
    "6041": "Unit Turns",
    "6160": "Unit Turns",
    "6200": "Unit Turns",
    "6300": "Unit Turns",
    "6700": "Unit Turns",
    # ---- repairs and maintenance ------------------------------------------------------
    **{
        c: "Repairs and Maintenance"
        for c in (
            "6030", "6031", "6035", "6040", "6060", "6070", "6100", "6105", "6130",
            "6140", "6161", "6190", "6220", "6290", "6310",
            "6520", "6540", "6550", "6560", "6570", "6610", "6640", "6660", "6670",
            "6720", "6730", "6760", "6770", "6830", "6870",
        )
    },
    # ---- utilities ---------------------------------------------------------------------
    **{c: "Utilities" for c in ("7010", "7025", "7026", "7030", "7040", "7050", "7060", "7070", "7075")},
    # ---- taxes / insurance -------------------------------------------------------------
    "7110": "Property Taxes",
    "7130": "Property Taxes",
    "7210": "Insurance",
    "7220": "Insurance",
    "5514_legal": "Legal and Professional",
    "5510_legal": "Legal and Professional",
    "5709": "Management Fee",
    "5745": "Management Fee",
    "7747": "Legal and Professional",
}

#: Accounts below the NOI line: improvements, reserves, debt service, partnership costs.
#: Deliberately excluded from operating expense — including them would understate NOI and
#: is the single most common way a T-12 rollup goes wrong.
BELOW_THE_LINE_PREFIXES = ("73", "74", "77", "78", "79", "91", "92", "08", "04", "09", "0")
BELOW_THE_LINE_EXACT = {
    "7325", "7330", "7340", "7345", "7350", "7360", "7370", "7376", "7398", "7399",
    "7400", "7680", "7710", "7720", "7723", "7747", "7748", "7749",
    "9103", "9106", "9108", "9109", "9110", "9111", "9113", "9115", "9116", "9123",
    "9200", "9207", "9233", "9240", "9246",
    "0815", "0824", "0867", "0897", "0400", "0949", "0951",
}

#: The page-break header block the showcase export repeats every ~50 rows: `Database:`,
#: `ENTITY:`, a bare `Accrual`, the `Account | Description | months` header. A header
#: word is one that HEADS a cell. This was a substring test over the row's code-plus-label
#: text, and on a real statement it dropped `5720-0000 Interest on Bank Accounts` and
#: `6400-0010 Accounting Fees` — two live runs read revenue $260 and expenses $3,024
#: short, the tie-out said REVIEW, and nothing named the lines .
#: Two shapes: a labelled field (`Database: X`, `Page: 3`) heads the cell; a bare column or
#: basis word (`Account`, `Accrual`) IS the whole cell. `Account\b` at the head was not
#: enough — it still took `Account Maintenance Fees` and `Accrual Adjustment` for headers
#:. A header cell says nothing else; an account label always does.
_PAGE_NOISE = re.compile(r"^\s*(?:Database:|ENTITY:|Page:|Date:|Time:)|^\s*(?:Account|Accrual)\s*$")


def _page_noise(*cells) -> bool:
    """Is any of these cells a page-break header cell? A labelled field at the head, or the
    bare word alone — never a word inside, or at the head of, an account label."""
    return any(isinstance(v, str) and _PAGE_NOISE.match(v) for v in cells)


def _num(value) -> float | None:
    """A cell as a finite float, or None. NaN and Infinity are ABSENT, not values.

    ``float("NaN")`` and ``float("Infinity")`` both succeed, so a spreadsheet with either
    word in a cell produced a number that passed every "is it present?" test downstream,
    summed into monthly rows and stated totals, and then made the interchange file
    unserialisable — ``json.dumps`` writes a bare ``NaN``, which is not JSON, and every
    comparison against a NaN is False so no check downstream could catch it either
. A cell that says NaN is a cell that says nothing.
    """
    if isinstance(value, bool) or value is None:
        return None
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        try:
            number = float(cleaned)
        except ValueError:
            return None
    if number is None or not math.isfinite(number):
        return None
    return number


def _date(value) -> str | None:
    """A parseable date as ISO; anything else (including "n/a") stays unparsed -> None.

    A spreadsheet's date cells reach the upload path as text, and the text may carry a
    time: ``2026-01-15 00:00:00`` is what ``str()`` makes of a ``datetime``. Accepting the
    ISO forms as well as the bare date is what stops a whole column of lease starts
    reading as absent because it arrived with a midnight attached.
    """
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        try:
            return dt.datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            pass
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y-%m-%d %H:%M:%S",
                    "%m/%d/%Y %H:%M:%S", "%m/%d/%y %H:%M:%S"):
            try:
                return dt.datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------------------
# T-12
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# A worksheet-shaped view over an already-parsed grid
#
# The upload path never has the file: `server/parse_worker.py` parses in a sandboxed
# subprocess and the upload is deleted the moment it returns, so all that survives is a
# list of rows. The raw path here still opens the workbook.
#
# Rather than write the row logic twice — which is how the two paths would quietly drift
# apart, and the equality report would stop being a pin on anything the upload path does
# — the grid gets a read-only object shaped like the small part of a worksheet this module
# uses: 1-based `cell(r, c).value`, `max_row`, `max_column`, `title`. Every line of the
# parsing logic below is then literally unchanged, whichever entry point was used.
# --------------------------------------------------------------------------------------


class _GridCell:
    __slots__ = ("value",)

    def __init__(self, value) -> None:
        self.value = value


class GridSheet:
    """Read-only, worksheet-shaped, 1-indexed view over a list of rows."""

    def __init__(self, rows: list[list], title: str = "Sheet1") -> None:
        self._rows = [list(r) for r in rows]
        self.title = title

    @property
    def max_row(self) -> int:
        return len(self._rows)

    @property
    def max_column(self) -> int:
        return max((len(r) for r in self._rows), default=0)

    def cell(self, row: int, column: int) -> _GridCell:
        if row < 1 or column < 1 or row > len(self._rows):
            return _GridCell(None)
        line = self._rows[row - 1]
        return _GridCell(line[column - 1] if column <= len(line) else None)


def _sheet(path: Path | None, sheet: "GridSheet | None"):
    if sheet is not None:
        return sheet
    if path is None:
        raise ValueError("one of path or sheet is required")
    return load_workbook(path, data_only=True).worksheets[0]


#: Role -> the header text the raw export happens to use. The upload path supplies
#: role -> column index instead (from `server.extract_llm.map_rent_roll`), and this table
#: is what lets the same indexing code serve both.
ROLL_ROLE_TO_HEADER: dict[str, str] = {
    "unit": "Unit ID",
    "plan": "Floorplan",
    # The raw export has no beds/baths columns and derives them from the plan code, but an
    # uploaded roll usually does have them. Omitting the roles here meant a mapped column
    # was silently ignored and a plan named "A1" reported zero bedrooms.
    "beds": "Beds",
    "baths": "Baths",
    "sqft": "Sqft",
    "status": "Unit Status",
    "lease_status": "Status",
    "current_rent": "Lease Rent",
    "market_rent": "LRO Market",
    "lease_start": "Lease Start",
    "move_in": "Occupied",
}


_MONTH_WORDS = ("jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec")


#: One accounting system writes its month headings as ``Aug 2025 Actual`` (a budget-comparison report also
#: carries ``Aug 2025 Budget``, which stays excluded). The word is stripped BEFORE the
#: month patterns run; the classifier applies the same rule (a smoke run: a
#: real trailing P&L was filed as background reading because of it).
ACTUAL_WORD = re.compile(r"^\s*actuals?\b[\s:\-]*|[\s\-:]*\(?\bactuals?\)?\s*$", re.I)


def strip_actual(text: str) -> str:
    return ACTUAL_WORD.sub("", text or "").strip()


def month_label(value) -> str | None:
    """``YYYY-MM`` for a header cell that names a month, else None.

    A raw export writes real datetimes and the original path relied on that. An uploaded
    workbook may write `Jul-25`, `2025-07-31` or `07/31/2025` as text — the same column,
    typed by a different system — and refusing those would send a perfectly readable
    statement down the "could not be read" branch. Only unambiguous forms are accepted;
    anything else is not a month, which keeps a stray text header out of the month set.
    """
    if isinstance(value, (dt.datetime, dt.date)):
        return f"{value.year:04d}-{value.month:02d}"
    text = str(value or "").strip()
    if not text or len(text) > 24:
        return None
    # A header that names a budget, a forecast or a variance is not a month, whatever date
    # it also carries. `2025-01 Budget` matched the ISO branch on its PREFIX and twelve of
    # them were read as a consecutive T-12 and published as actual results; on a sheet carrying both, the 24 columns simply failed instead.
    if _ANNUAL_NOT_ACTUAL.search(text):
        return None
    text = strip_actual(text)
    # Anchored, with the time an export sometimes writes after the date. An unanchored
    # match is what let a suffix ride along.
    m = re.match(r"^(\d{4})-(\d{1,2})(?:-\d{1,2})?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$",
                 text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.match(r"^([A-Za-z]{3,9})[\s\-/,']+(\d{2,4})$", text)
    if m and m.group(1)[:3].lower() in _MONTH_WORDS:
        year = int(m.group(2))
        year = year + 2000 if year < 100 else year
        return f"{year:04d}-{_MONTH_WORDS.index(m.group(1)[:3].lower()) + 1:02d}"
    m = re.match(r"^(\d{1,2})[/\-](?:\d{1,2}[/\-])?(\d{4})$", text)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    return None


#: The three subtotal keys ``extract_llm.map_t12`` reports, and the schema's names for
#: them. One table so the mapping call and the Error Check block cannot disagree.
_SUBTOTAL_KEY_TO_SCHEMA: dict[str, str] = {
    "total_revenue": "Total Revenue",
    "total_expenses": schema.PNL_TOTAL_OPEX_LABEL,
    "net_operating_income": schema.PNL_NOI_LABEL,
}


def _canon_label(label: str | None) -> str:
    """A label reduced enough that two spellings of one row match."""
    return re.sub(r"[^a-z0-9]+", " ", str(label or "").lower()).strip()


#: Headers that make the column after the last month an ANNUAL ACTUAL total.
#:
#: Every other thing a statement prints there — Budget, Variance, Forecast, the next
#: period, a percentage — is a different quantity, and reading it as the year turns the
#: Error Check into a generator of discrepancies that do not exist.
_ANNUAL_TOTAL_HEADER = re.compile(
    r"\b(total|totals|annual|annualized|annualised|ytd|y-t-d|t-?12|ttm|"
    r"12\s*month|twelve\s*month|year|yr)\b",
    re.I,
)

#: Headers that are NOT actuals, whatever else they also say. Checked FIRST and it wins.
#:
#: The accept pattern above matches a period word, and every one of those words also
#: appears in the header of a column that is not the year's actuals: "YTD Budget" matches
#: on ``ytd``, "Year Variance" on ``year``, "T-12 Forecast" on ``t-12``. an earlier review —
#: the tie-out then compared twelve months of actuals against a budget and published a
#: discrepancy that exists only in this code. Being wrong in this direction is free: the
#: twelve months are summed and the choice is disclosed, which is the same answer the
#: parser gives a statement with no annual column at all.
_ANNUAL_NOT_ACTUAL = re.compile(
    r"\b(budget|budgeted|bud|variance|variances|var|forecast|forecasted|fcst|"
    r"projection|projections|projected|proj|proforma|pro\s*forma|plan|planned|"
    r"estimate|estimated|budgt)\b",
    re.I,
)


def _annual_of(ws, row: int, annual_col: "int | None", month_cols) -> "float | None":
    """The year for one line: the stated annual column, or the twelve months summed.

    ``annual_col`` is None whenever the adjacent column's header does not name an annual
    total, so the sum is the answer rather than the fallback.
    """
    if annual_col is not None:
        stated = _num(ws.cell(row, annual_col).value)
        if stated is not None:
            return stated
    monthly = [_num(ws.cell(row, c).value) for c, _lab in month_cols]
    if any(v is not None for v in monthly):
        return round(math.fsum(v or 0.0 for v in monthly), 2)
    return None


def _text_cell(v) -> str | None:
    """The cell as label text — a string with a letter in it — else None."""
    if v is None or isinstance(v, (int, float)):
        return None
    t = str(v).strip()
    return t if t and any(ch.isalpha() for ch in t) else None


def row_label_cells(cells: list, label_col: int, code_col: int | None,
                    first_month_col: int | None = None) -> str | None:
    """The label of one row: the label column's text, else the LEFTMOST text cell among
    the columns before the first month column that is not the code column.

    One accounting system's statement indents its account lines by section — column E under ADJUSTED RENT,
    column D under OTHER INCOME and the expenses — and prints ``Total ...`` /
    ``TOTAL INCOME`` / ``NET OPERATING INCOME`` in column A. One "label column" reads one
    depth; the rental-revenue block and every total were invisible and the statement's NOI
    came out negative . Columns are 1-based; ``cells`` is the row."""
    def at(col: int):
        return cells[col - 1] if 0 < col <= len(cells) else None
    label = _text_cell(at(label_col))
    if label:
        return label
    stop = first_month_col if first_month_col else label_col
    for col in range(1, stop):
        if code_col and col == code_col:
            continue
        t = _text_cell(at(col))
        if t:
            return t
    return None


def row_label(ws, r: int, label_col: int, code_col: int | None, first_month_col: int | None = None) -> str | None:
    stop = max(label_col, first_month_col or 0)
    cells = [ws.cell(r, col).value for col in range(1, stop + 1)]
    return row_label_cells(cells, label_col, code_col, first_month_col)


def first_month_column(header: list) -> int | None:
    """1-based index of the first month heading on a header row, or None."""
    for i, cell in enumerate(header):
        if month_label(cell):
            return i + 1
    return None


def labels_for_mapping(rows: list[list], header_index: int, label_col: int, code_col: int | None) -> list[str]:
    """The upload path's label list for the mapper, one entry per row below the header,
    through :func:`row_label_cells` — so every account line, whatever its indent, and the
    statement's own TOTAL rows reach ``statement_sections`` wherever the export printed them."""
    header = rows[header_index] if 0 <= header_index < len(rows) else []
    fm = first_month_column(list(header))
    out: list[str] = []
    for r in rows[header_index + 1:]:
        label = row_label_cells(list(r), label_col, code_col, fm)
        if label:
            out.append(label)
    return out


#: A stated reporting period in a statement's title rows: "for April 2026", "As of Date:
#: 06/30/2026", "Period Ending 04/30/2026". Only after words that name a PERIOD: a bare date
#: is as likely the print or generation stamp ("Report generated on 08/15/2026"), and reading
#: that as the period picks the wrong twelve and ties them to themselves.
_PERIOD_WORDS = (r"(?:as\s+of(?:\s+date)?|for(?:\s+the)?(?:\s+(?:period|month|year))?"
                 r"(?:\s+(?:ending|ended))?|period\s+end(?:ing|ed)?|ending|ended|through|thru|"
                 r"month\s+of)\s*:?\s*")
_STATED_DATE = (r"(?:(?P<{p}w>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s,]+"
                r"(?P<{p}wy>\d{{4}})|(?P<{p}m>\d{{1,2}})/\d{{1,2}}/(?P<{p}y>\d{{4}}))\b")
#: The period phrase, and a range's END when it states one: "For the period 05/01/2025 -
#: 04/30/2026" ends in April 2026, not May 2025.
_STATED_PERIOD = re.compile(
    _PERIOD_WORDS + r"\b" + _STATED_DATE.format(p="a")
    + r"(?:\s*(?:-|\u2013|\u2014|to|through|thru)\s*" + _STATED_DATE.format(p="b") + r")?",
    re.I)


def _stated_month(m: "re.Match") -> str | None:
    """``YYYY-MM`` a :data:`_STATED_PERIOD` match ends at: the range's end, else its date."""
    p = "b" if (m.group("bw") or m.group("bm")) else "a"
    if m.group(p + "w"):
        return f"{int(m.group(p + 'wy')):04d}-{_MONTH_WORDS.index(m.group(p + 'w')[:3].lower()) + 1:02d}"
    month = int(m.group(p + "m"))
    return f"{int(m.group(p + 'y')):04d}-{month:02d}" if 1 <= month <= 12 else None


def _window_at_stated_month(ws, header_row: int, month_cols: list[tuple[int, str]]):
    """``(twelve month columns, note)`` ending at the one month the title rows state, or
    ``(None, None)`` when they state none, several, or one the grid does not hold twelve
    consecutive months up to."""
    stated: dict[str, str] = {}
    for r in range(1, header_row):
        for c in range(1, ws.max_column + 1):
            text = ws.cell(r, c).value
            if not isinstance(text, str):
                continue
            for m in _STATED_PERIOD.finditer(text):
                month = _stated_month(m)
                if month:
                    stated.setdefault(month, m.group(0))
    if len(stated) != 1:
        return None, None
    (end, phrase), = stated.items()
    year, month = int(end[:4]), int(end[5:])
    wanted = []
    for _ in range(12):
        wanted.append(f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    by_label = {lab: (c, lab) for c, lab in month_cols}
    if len(by_label) != len(month_cols) or not all(w in by_label for w in wanted):
        return None, None
    window = [pair for pair in month_cols if pair[1] in set(wanted)]
    note = (
        f"The statement prints {len(month_cols)} month columns; the twelve ending "
        f"{end} were read, the period its heading states ({phrase!r}). Every stated "
        f"subtotal below is the sum of those twelve, not the sheet's own total column."
    )
    return window, note


#: A label that reads as an aggregate of lines, not one account: "Controllable Operating
#: Expenses", "Other Income", "Non-Controllable Costs".
_AGGREGATE_LABEL = re.compile(r"\b(expenses|costs|income|revenues?|controllable)\b")

#: The longest run of lines a printed running subtotal is looked for over.
_RUNNING_SUBTOTAL_SPAN = 400


def _is_running_subtotal(monthly: list[float], kept: list[dict]) -> bool:
    """True when ``monthly`` equals, month by month, the sum of the last k kept lines, for
    some k with at least two of those lines carrying an amount. One line matching one
    line is a coincidence of values, not a subtotal; a stated-zero row adds nothing and
    is not counted toward the two."""
    acc = [0.0] * len(monthly)
    carrying = 0
    for k, prior in enumerate(reversed(kept[-_RUNNING_SUBTOTAL_SPAN:]), start=1):
        values = prior["monthly"]
        acc = [a + b for a, b in zip(acc, values)]
        if any(values):
            carrying += 1
        # Never less than a dollar a month, the tie gate's own floor: a statement whose
        # cells were rounded independently of its subtotals drifts by cents per line.
        slack = max(1.00, ROUNDING_PER_CELL * k + 0.01)
        if carrying >= 2 and all(abs(a - m) <= slack for a, m in zip(acc, monthly)):
            return True
    return False


#: Half a cent per rounded cell (every account-month cell is rounded to cents).
ROUNDING_PER_CELL = 0.005


def parse_t12(
    path: Path | None,
    units: int,
    *,
    sheet: "GridSheet | None" = None,
    header_row: int | None = None,
    code_col: int | None = 1,
    label_col: int = 2,
    label_map: dict[str, str] | None = None,
    subtotal_labels: dict[str, str] | None = None,
) -> tuple[dict, list[str]]:
    """One trailing-twelve statement into the schema's ``t12`` block.

    ``path`` is the raw path; ``sheet`` is a :class:`GridSheet` for the upload path.
    ``header_row``, ``code_col`` and ``label_col`` come from the caller's own header
    detection when the export does not write "Account" in column A, and ``label_map``
    supplies a category for labels this module's own table does not know — that is the
    mapped-labels hand-off from the model layer.

    ``code_col=None`` means the statement has no account-code column at all, which is
    ordinary outside this one export. The code is then neither required nor used to
    recognise a source line; the label is.

    ``subtotal_labels`` names this statement's own Total Revenue / Total Operating
    Expenses / NOI rows, as identified upstream. Without it only a handful of hardcoded
    spellings were recognised, so a statement saying "Total Income", "Total Expenses" and
    "NOI" produced no ``source_subtotals`` — and the workbook's Error Check block, whose
    whole job is to tie our arithmetic back to the statement's own, had nothing to check
    against even though those rows had been found.
    """
    ws = _sheet(path, sheet)
    notes: list[str] = []

    # Month columns come from the header row's dates, so the direction is read, not assumed.
    if header_row is None:
        for r in range(1, 30):
            if str(ws.cell(r, code_col or 1).value or "").strip() == "Account":
                header_row = r
                break
    if header_row is None:
        raise ValueError("could not locate the T-12 header row (column A == 'Account')")

    subtotal_lookup = {
        _canon_label(name): key
        for key, name in (subtotal_labels or {}).items()
        if name and key in _SUBTOTAL_KEY_TO_SCHEMA
    }

    month_cols: list[tuple[int, str]] = []
    for c in range(label_col + 1, ws.max_column + 1):
        label = month_label(ws.cell(header_row, c).value)
        if label:
            month_cols.append((c, label))
    window_note: str | None = None
    if len(month_cols) > 12:
        # A calendar-year operations report prints every month of the year(s) it spans,
        # the later ones empty or carrying a stray entry, and names its period in the
        # title ("... 12 Month Report for April 2026"). Which twelve are the T-12 is read
        # from that stated month, never guessed from where the numbers stop: a stray
        # May entry would move a "last populated" window by one.
        found = len(month_cols)
        month_cols, window_note = _window_at_stated_month(ws, header_row, month_cols)
        if month_cols is None:
            raise ValueError(
                f"expected 12 month columns, found {found}, and no single as-of month is "
                f"stated above the header to choose the twelve from"
            )
    if len(month_cols) != 12:
        raise ValueError(f"expected 12 month columns, found {len(month_cols)}")
    labels = [lab for _c, lab in month_cols]
    if window_note:
        notes.append(window_note)

    # Is the column after the last month the YEAR, or just the next column along?
    #
    # It was read as the annual total on position alone. A statement that prints Budget,
    # Variance or the next period there had that unrelated figure recorded as its own
    # stated subtotal, and the Error Check then compared the twelve monthly actuals
    # against it and published a discrepancy that exists only in this code. Position is
    # not a claim; the header is.
    annual_col = month_cols[-1][0] + 1
    # A window cut from a wider grid has no annual column of its own: whatever sits after
    # its last month is the next month or the grid's total over a different period.
    annual_header = "" if window_note else str(ws.cell(header_row, annual_col).value or "").strip()
    not_actual = bool(annual_header) and _ANNUAL_NOT_ACTUAL.search(annual_header)
    annual_is_total = (
        bool(annual_header)
        and not not_actual
        and bool(_ANNUAL_TOTAL_HEADER.search(annual_header))
    )
    if not_actual:
        notes.append(
            f"The column after the last month is headed {annual_header!r}, which names a "
            f"budget, variance or forecast rather than actual results, so it was NOT read "
            f"as the annual total: every stated subtotal below is the sum of the twelve "
            f"detected month columns instead."
        )
    elif annual_header and not annual_is_total:
        notes.append(
            f"The column after the last month is headed {annual_header!r}, which does not "
            f"name an annual total, so it was NOT read as one: every stated subtotal below "
            f"is the sum of the twelve detected month columns instead."
        )

    rows: list[dict] = []
    subtotals_seen: dict[str, float] = {}
    unmapped: list[str] = []
    below_noi = 0
    # The LAST row that reads as net operating income, found before the pass: a
    # statement printing a summary block above its account schedule has an NOI row at
    # the top, and cutting at the first one dropped the whole schedule. Only the
    # upload path uses this; the raw showcase path keeps its code-table filter.
    last_noi_row: int | None = None
    if label_map is not None:
        for scan in range(header_row + 1, ws.max_row + 1):
            text = row_label(ws, scan, label_col, code_col, month_cols[0][0]) or ""
            if not text:
                continue
            if subtotal_lookup.get(_canon_label(text)) == "net_operating_income" or (
                text.upper() == "NET OPERATING INCOME"
            ):
                last_noi_row = scan

    named_subtotals: dict[str, float] = {}
    running: list[int] = []
    for r in range(header_row + 1, ws.max_row + 1):
        acct = ws.cell(r, code_col).value if code_col else None
        # The label column's text, else the leftmost text cell to its left (one accounting system's
        # statement's TOTAL rows sit in column A while its lines are indented to E).
        desc = (row_label(ws, r, label_col, code_col, month_cols[0][0]) if label_map is not None
                else ws.cell(r, label_col).value)
        if _page_noise(acct, desc):
            continue

        label = str(desc).strip() if desc is not None else None
        code = str(acct).strip() if acct is not None else None

        # A subtotal this statement was identified as printing, under whatever wording it
        # uses. Checked first, and only when the caller supplied the labels, so the raw
        # showcase path below is bit-for-bit the code it always was.
        key = subtotal_lookup.get(_canon_label(label)) if label else None
        if key:
            total = _annual_of(ws, r, annual_col if annual_is_total else None, month_cols)
            if total is not None:
                # The LAST row with the label: a summary block above the schedule repeats
                # it, and the bottom line is the statement's answer.
                named_subtotals[_SUBTOTAL_KEY_TO_SCHEMA[key]] = round(total, 2)
            continue

        # A printed subtotal line: no account code, a TOTAL/NOI description.
        if code is None and label and re.match(r"^(TOTAL|NET )", label, re.I):
            total = _annual_of(ws, r, annual_col if annual_is_total else None, month_cols)
            if total is not None:
                subtotals_seen[label.upper()] = total
            continue
        if code_col is None:
            # No account-code column: a source line is one with a label and a number.
            if not label:
                continue
            if schema.structural_row(label):
                continue
        elif label_map is None:
            # The RAW showcase path. Its account codes are three or four digits and that
            # shape is part of how this one export is read; keep it exactly.
            if code is None or not re.match(r"^\d{3,4}$", code):
                continue
            # BELOW_THE_LINE_EXACT is a list of THIS export's account codes. On an upload
            # the codes belong to somebody else's chart of accounts and mean nothing, so
            # the filter would discard valid lines by coincidence of numbering.
            if code in BELOW_THE_LINE_EXACT:
                continue
        elif not label:
            # An UPLOAD. Somebody else's codes are 5100-000, or five digits, or absent
            # entirely, and `_CODE_LIKE` upstream already accepts all of those — so
            # demanding three or four digits here threw away every row of a statement
            # that had just been classified and mapped successfully. The label_map
            # decides what a row is; the code is not ours to judge.
            continue

        printed = [_num(ws.cell(r, c).value) for c, _lab in month_cols]
        monthly = [v or 0.0 for v in printed]
        # An all-zero row was dropped here unconditionally, so a GPR the statement PRINTS as
        # twelve zeros vanished and the roll tie said "no line categorised as Gross Potential
        # Rent". On an upload a stated zero is a value: the row is kept when at least
        # one month cell holds an actual number. Blank or "N/A" cells are coerced to 0.0 just
        # above, so they are NOT evidence — keying on the category alone would manufacture a
        # stated zero from missing data. The raw showcase
        # path keeps its old rule. A kept zero row that is below NOI or unmapped is dropped
        # silently, as before, rather than counted in those notes.
        all_zero = all(v == 0.0 for v in monthly)
        if all_zero and (label_map is None or all(v is None for v in printed)):
            continue

        # Whoever supplied a label_map is the authority. T12_ACCOUNT_CATEGORY is a table
        # of THIS export's GL codes; on an upload the same number means something else
        # entirely — 5700 is Management Fee here and could be anything there — so letting
        # the code table win silently overrode the caller's mapping with a foreign one.
        # The raw showcase path supplies no label_map and is unaffected.
        # `is not None`, not truthiness: an EMPTY mapping is an upload whose every label
        # was deliberately excluded, and falling back to this export's own code table
        # there hands a stranger's statement categories its mapping explicitly refused.
        if label_map is not None and last_noi_row is not None and r > last_noi_row:
            # An UPLOAD, and the statement has already printed its net operating income:
            # what follows is capital, reserves, debt — not operating, whatever the label
            # says. Left out of the operating results and counted as such, separately from
            # a line the mapping could not place. The raw showcase
            # path supplies no label_map and keeps its code-table filter untouched.
            if not all_zero:
                below_noi += 1
            continue
        if label_map is not None:
            category = label_map.get(label) or label_map.get(str(label).strip())
        elif code is not None:
            category = T12_ACCOUNT_CATEGORY.get(code)
        else:
            category = None
        if category is None:
            if not all_zero:
                unmapped.append(f"{code} {label}" if code else str(label))
            continue
        if (label_map is not None and not all_zero and code is None
                and _AGGREGATE_LABEL.search(schema.normalize_account_text(label))
                and _is_running_subtotal(monthly, rows)):
            # "Controllable Operating Expenses", "Other Operating Expenses": the statement's
            # own running totals, printed with no "Total" in the label, were read as lines
            # on top of their own detail. Arithmetic and wording are not proof — a
            # codeless "Other Income" account can equal Parking + Storage by coincidence
            # — so the row stays a line here and is only a CANDIDATE: the
            # tie gate drops it when the statement's own printed totals say it is one.
            running.append(len(rows))
        rows.append(
            {
                "label": (f"{label} ({code})" if code else label) if label else code,
                "category": category,
                "monthly": [round(v, 2) for v in monthly],
            }
        )

    if unmapped:
        notes.append(
            f"{len(unmapped)} account line(s) had no category mapping and were EXCLUDED: "
            + "; ".join(unmapped[:8])
        )
    if below_noi:
        notes.append(
            f"{below_noi} account line(s) below the statement's NET OPERATING INCOME row "
            f"were left out of operating results (capital, reserves or other non-operating "
            f"items, by the statement's own layout)."
        )

    # Only the subtotals the Error Check actually reads, and only where the statement's
    # definition matches ours. TOTAL OTHER INCOME is deliberately omitted: the statement
    # groups reimbursements and bad debt inside it, we break them out, so claiming a tie
    # would be claiming a tie between two different things.
    source_subtotals: dict[str, float | None] = {}
    if "TOTAL INCOME" in subtotals_seen:
        source_subtotals["Total Revenue"] = round(subtotals_seen["TOTAL INCOME"], 2)
    if "TOTAL OPERATING EXPENSES" in subtotals_seen:
        source_subtotals[schema.PNL_TOTAL_OPEX_LABEL] = round(
            subtotals_seen["TOTAL OPERATING EXPENSES"], 2
        )
    if "NET OPERATING INCOME" in subtotals_seen:
        source_subtotals[schema.PNL_NOI_LABEL] = round(
            subtotals_seen["NET OPERATING INCOME"], 2
        )
    if "TOTAL UTILITIES" in subtotals_seen:
        source_subtotals["Utilities"] = round(subtotals_seen["TOTAL UTILITIES"], 2)
    # The rows identified upstream win where the two disagree: they were matched by label
    # on this statement, while the block above matches a spelling this one export uses.
    source_subtotals.update(named_subtotals)

    return (
        {
            "month_labels": labels,
            "units": units,
            "rows": rows,
            "source_subtotals": source_subtotals,
            "broker_proforma": None,
            # Positions in ``rows`` that may be the statement's own running subtotals; the
            # upload path's tie gate decides and removes the key (server/assemble.py).
            **({"running_subtotals": running} if running else {}),
        },
        notes,
    )


# --------------------------------------------------------------------------------------
# Rent roll
# --------------------------------------------------------------------------------------

OCCUPIED_STATUSES = {"Occupied", "Notice Available", "Notice Rented"}
VACANT_STATUSES = {"Vacant Available", "Vacant Rented"}


#: How a mapped meaning becomes one of the three statuses this schema carries. A resident
#: on notice is still in place and still paying, so they count as occupied; a model or a
#: down unit is neither occupied nor available.
_MEANING_TO_STATUS: dict[str, str] = {
    "occupied": "Occupied",
    "notice": "Occupied",
    "vacant": "Vacant",
    "model": "Non-revenue",
    "down": "Non-revenue",
    "other": "Non-revenue",
}


#: How every export this pipeline has met spells "this is the lease actually in place on
#: the as-of date". Matched case- and punctuation-insensitively.
#:
#: The de-duplication used to compare against the literal string ``"Current"``, which is
#: what THIS export writes. A roll writing ``C``, ``Active`` or ``In Place`` therefore had
#: no in-place row as far as the code was concerned, so the first row seen won — and on a
#: turning-over unit the first row is usually the INCOMING lease, at the new rent. In-place
#: and market rent totals move silently, which is the whole point of reading a rent roll.
_LEASE_IN_PLACE: frozenset[str] = frozenset(
    {
        "current", "c", "active", "in place", "inplace", "existing", "occupied",
        "month to month", "mtm", "m2m", "holdover", "hold over", "transfer in",
    }
)

#: Values that name a lease not yet in place. Listed rather than inferred so that anything
#: unrecognised stays unrecognised — "not in place" and "we do not know" are different
#: answers and only the second should lose to an incumbent that IS recognised.
_LEASE_INCOMING: frozenset[str] = frozenset(
    {"new", "future", "pending", "applicant", "approved", "reserved", "notice to vacate"}
)


def _canon_lease_status(raw: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(raw or "").lower()).strip()


#: What a status that names neither an in-place lease nor an incoming one is recorded as.
UNKNOWN_LEASE_STATUS = "Unknown"


def lease_recognised(raw: str | None) -> bool:
    """Does this value name either side of the turnover — in place, or not yet?"""
    canon = _canon_lease_status(raw)
    return bool(canon) and (
        canon in _LEASE_IN_PLACE
        or canon in _LEASE_INCOMING
        or canon in ("renewal", "renewed", "renew")
    )


def lease_in_place(raw: str | None) -> bool:
    """Does this lease-status value name the lease in place on the as-of date?"""
    return _canon_lease_status(raw) in _LEASE_IN_PLACE


def normalized_lease_status(raw: str | None) -> str:
    """The value recorded in ``duplicate_detail``, in this schema's own vocabulary.

    Emitted rather than passed through, so ``schema.LEASE_STATUSES`` can stay an
    enumeration — an unrecognised status there is not inert, it routes a unit into the
    branch that EXCUSES a wrong keep. On the showcase export the raw values already ARE
    these values, so this changes nothing about the raw path.
    """
    canon = _canon_lease_status(raw)
    if not canon:
        return ""
    if canon in _LEASE_IN_PLACE:
        return IN_PLACE_LEASE_STATUS
    if canon in ("renewal", "renewed", "renew"):
        return "Renewal"
    if canon in _LEASE_INCOMING:
        return "New"
    # UNKNOWN, never New. "We do not recognise this" and "this lease has not started" are
    # different answers, and calling the first the second is how the typo `Currnet` became
    # a well-formed record that the duplicate check then read as "no in-place row existed"
    # — the branch that EXCUSES a wrong keep. An unknown says so and stays visible.
    return UNKNOWN_LEASE_STATUS


def _status_of(raw: str, status_map: dict[str, str] | None) -> str:
    """The unit's status, from the caller's vocabulary when there is one."""
    if status_map:
        meaning = status_map.get(raw) or status_map.get(raw.strip().lower())
        if meaning:
            return _MEANING_TO_STATUS.get(meaning, "Non-revenue")
    if raw in OCCUPIED_STATUSES:
        return "Occupied"
    if raw in VACANT_STATUSES:
        return "Vacant"
    return "Non-revenue"


#: What a unit's status is when the roll carries no physical-status column at all.
UNKNOWN_STATUS = "Unknown"


def _inferred_status(rent, lease_start, move_in, as_of: "str | None" = None) -> str:
    """Occupancy from the evidence the roll DOES carry, judged ON the as-of date.

    ``status: null`` is a legal answer from the mapper — plenty of exports identify a unit,
    a rent and a lease and never name a status — and every one of those rows used to get
    an empty ``status_raw``, fall through ``_status_of`` and land on ``Non-revenue``. A
    roll of 300 occupied homes then reported zero occupancy, zero vacancy and 300
    non-revenue units, confidently, with nothing anywhere saying the number was a
    fallback. The same inference now serves a blank status cell and a code the
    vocabulary can only call `other`.

    Paying rent under a lease IN PLACE on the as-of date is occupancy. A lease whose
    move-in and start both fall after that date is a home nobody lives in yet — vacant,
    whatever rent it will carry. No rent and no lease is a vacancy. Anything else
    is genuinely unknown and says so — which still leaves it out of the occupied count,
    but as a stated absence rather than as a category it does not belong to.

    Dates are ISO strings from `_date`, so they compare as text; an ``as_of`` that is
    not one imposes no cutoff, which is the rule as it stood.
    """
    paying = (rent or 0) > 0
    cutoff = as_of if isinstance(as_of, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", as_of) else None

    def on_or_before(d) -> bool:
        return isinstance(d, str) and bool(d) and (cutoff is None or d <= cutoff)

    # An explicit move-in DECIDES physical presence; the lease start is the fallback for a
    # roll that has no move-in column. A lease begun in July with a move-in keyed for
    # August is not a July resident.
    in_place = on_or_before(move_in) if move_in else on_or_before(lease_start)
    if paying and in_place:
        return "Occupied"
    if not paying and not lease_start and not move_in:
        return "Vacant"
    if not paying and (lease_start or move_in) and not in_place:
        return "Vacant"  # pre-leased and empty: a lease not yet started, no rent yet
    # Rent with only a future date (a renewal starting next month on a roll with no
    # move-in column; a move-in keyed a day late) is ambiguous — a resident, most likely,
    # but not evidence of one in place. Unknown, never Vacant.
    return UNKNOWN_STATUS


#: Words in a raw status that name a NON-REVENUE unit whatever meaning the vocabulary
#: gave the code. A `Model` the model called `other` must not be inferred into Vacant
#: or Occupied from its rent and dates.
_NON_REVENUE_WORDS: tuple[str, ...] = ("model", "down", "unrentable", "employee", "admin", "office", "staff")


#: Columns whose value is a statement about ONE unit. A rent is deliberately not among
#: them: a roll's own totals line carries a rent (one real export puts $439,669 in the
#: rent column of its "Total Units:" row), so a rent alone cannot tell a unit from a total.
_UNIT_STATE_HEADERS: tuple[str, ...] = (
    "Unit Status", "Status", "Sqft", "Beds", "Baths", "Lease Start", "Occupied",
)

#: Unit identifiers that are labels for a group of rows rather than names of a unit.
_TOTALS_LABEL = re.compile(r"^\s*(total|totals|subtotal|sum|grand|average|avg)\b", re.I)


def _blank_to_none(value):
    """A cell that holds nothing, as None — whatever flavour of nothing it is.

    An xlsx gives an empty cell as ``None``; a csv and a legacy xls give ``""``, and a
    roll typed by hand gives whitespace. The trailer filter below asked ``plan is None``,
    so for those two formats it never ran at all and a "Total Units:" row carrying the
    aggregate rent was accepted as a unit — inflating the count, the rent and everything
    derived from them, silently.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _cell(ws, row: int, column: "int | None"):
    """One cell of the roll, with every flavour of blank normalised to None.

    **The single door every roll read goes through.** An xlsx gives an empty cell as
    ``None``; a csv and a legacy xls give ``""``; a roll typed by hand gives whitespace.
    Three rounds found the same defect in three different comparisons — the plan cell
    (round 17), the unit identifier and the trailer test (round 18) — because each site
    asked ``is None`` of a value that was only sometimes None. Reading through here is what
    makes the same roll parse the same way whatever its file format, which
    the parser tests assert unit for unit.

    A missing COLUMN answers None too, so an optional column needs no caller-side guard.
    """
    if not column:
        return None
    return _blank_to_none(ws.cell(row, column).value)


def _states_a_unit(ws, row: int, unit_id, state_cols: list[int], rent_col: "int | None") -> bool:
    """Is this blank-plan row a UNIT, or part of the roll's trailer?

    Only asked of a row whose plan cell is empty, because that is the only case where the
    answer used to be assumed. Three tests, in order:

    * an identifier that reads as a totals label is not a unit, whatever else the row has;
    * a value in any column that describes one unit — status, size, beds, baths, a lease
      date — is a unit;
    * and where the sheet carries NONE of those columns, a rent is the only evidence
      available, so it is what counts. On a sheet that does have them, a rent alone is not
      enough: that is exactly the shape of a totals line.
    """
    if _TOTALS_LABEL.match(str(unit_id)):
        return False
    if state_cols:
        return any(str(_cell(ws, row, c) or "").strip() for c in state_cols)
    return _cell(ws, row, rent_col) is not None


def _derived_plan(beds, baths) -> str:
    """A neutral grouping for a roll that names no floor plan.

    The unit-mix tab groups by this string, so it has to be a string and it has to group
    like units together. Beds and baths do that honestly; MISSING is the answer when the
    roll carries neither, because inventing "A1" would look like a plan code the export
    does not have.
    """
    if beds is None and baths is None:
        return schema.MISSING
    bed = f"{int(beds)}" if beds is not None and float(beds).is_integer() else (
        f"{beds:g}" if beds is not None else "?"
    )
    bath = f"{baths:g}" if baths is not None else "?"
    return f"{bed}BR/{bath}BA"


def _plan_beds_baths(plan: str) -> tuple[float, float]:
    m = re.match(r"^(\d+)X(\d+)", plan.upper())
    if not m:
        return (0.0, 0.0)
    return (float(m.group(1)), float(m.group(2)))


def parse_rent_roll(
    path: Path | None,
    as_of: str,
    *,
    sheet: "GridSheet | None" = None,
    header_row: int | None = None,
    columns: dict[str, int] | None = None,
    status_map: dict[str, str] | None = None,
) -> tuple[list[dict], list[str], dict]:
    """One rent roll into per-unit rows, plus the notes and the source's own stated totals.

    ``columns`` maps a ROLE (``unit``, ``current_rent``, ``lease_start``, ...) to a
    1-based column index, which is how the upload path hands over what
    ``server.extract_llm.map_rent_roll`` worked out. Omitted, the header row is read the
    way the raw export writes it. Either way the indexing below is the same code.

    ``status_map`` maps a raw status string to ``occupied`` / ``vacant`` / ``notice`` /
    ``model`` / ``down`` / ``other``. Without it only this export's own spellings are
    recognised, so an upload writing ``O`` and ``V`` had every unit fall through to
    Non-revenue and its occupancy, vacancy and occupied-unit rents were all wrong.
    """
    ws = _sheet(path, sheet)
    notes: list[str] = []

    if header_row is None:
        for r in range(1, 12):
            if str(ws.cell(r, 2).value or "").strip() == "Unit ID":
                header_row = r
                break
    if header_row is None:
        raise ValueError("could not locate the rent-roll header row (column B == 'Unit ID')")
    if columns:
        # The PLAN column is not required. A roll carrying unit, rent, dates, beds and
        # baths is a perfectly ordinary roll — the mapping schema says so, `plan: null` is
        # a legal answer — and it was being classified confidently and then refused here,
        # failing the whole run. Where there is no plan column the grouping is derived
        # from beds and baths instead, which is what a floor plan mostly encodes anyway.
        missing = [
            role for role in ("unit", "current_rent") if not columns.get(role)
        ]
        if missing:
            raise ValueError(
                "the rent roll's column mapping is missing " + ", ".join(missing)
            )
        col = {
            ROLL_ROLE_TO_HEADER[role]: index
            for role, index in columns.items()
            if role in ROLL_ROLE_TO_HEADER and index
        }
    else:
        col = {
            str(ws.cell(header_row, c).value).strip(): c
            for c in range(1, ws.max_column + 1)
            if ws.cell(header_row, c).value
        }

    # The export states its own totals above the header. Reading them is what makes a
    # tie-out possible: comparing our numbers to our numbers proves nothing.
    stated: dict[str, float | None] = {"sqft": None, "units": None, "occupancy": None}
    _STATED_LABELS = {
        "total sqft": "sqft",
        "total units": "units",
        "unit count": "units",
        "occupancy": "occupancy",
        "occupancy %": "occupancy",
    }
    for r in range(1, header_row):
        label = str(ws.cell(r, 1).value or "").strip().rstrip(":").lower()
        key = _STATED_LABELS.get(label)
        if key and stated[key] is None:
            stated[key] = _num(ws.cell(r, 2).value)
    stated_sqft = stated["sqft"]

    has_plan_column = "Floorplan" in col
    # A roll whose mapping named no physical-status column. Decided once, from the
    # columns, rather than per row from an empty string that means nothing.
    has_status_column = bool(col.get("Unit Status"))
    # Columns that say something about a UNIT rather than about a group of them. What a
    # blank-plan row is judged on — see `_states_a_unit`.
    state_cols = [col[name] for name in _UNIT_STATE_HEADERS if col.get(name)]
    rent_col = col["Lease Rent"] if col.get("Lease Rent") else None
    raw: list[dict] = []
    # Counted so the run can SAY what it dropped. Both of these skips are judgements about
    # rows the file did contain — a totals line, a summary footer — and a judgement that
    # cannot be wrong is not one worth making. If a real unit is ever dropped by them it
    # shows up here as a number a reader can check against their own roll, rather than as
    # a unit count quietly one short (the coordinator's ruling on round 18 item 2).
    skipped_unnamed = 0
    skipped_trailer = 0
    for r in range(header_row + 1, ws.max_row + 1):
        unit_id = _cell(ws, r, col["Unit ID"])
        plan = _cell(ws, r, col["Floorplan"]) if has_plan_column else None
        # A row with no identifier is not a unit. It used to be caught only when the cell
        # was None, which is the xlsx spelling — a csv and an xls give "", so a footer
        # carrying a number in some other column became a unit with an EMPTY id, and two
        # of them deduplicated into one phantom.
        if unit_id is None:
            # A row with something in it but no identifier — otherwise it is just an
            # empty line, and an empty line is not a decision.
            if any(_cell(ws, r, c) is not None for c in range(1, ws.max_column + 1)):
                skipped_unnamed += 1
            continue
        # A blank plan CELL is not a blank row. It used to skip the whole unit whenever
        # the export had a plan column, discarding a row that named a unit, a rent, a
        # status, beds and baths — understating the unit count, the occupancy and the
        # rents, silently. It is now handled the way a missing plan
        # COLUMN is: kept, and grouped by a neutral plan derived from beds and baths.
        #
        # What the blank cell WAS doing is the reason there is a test here rather than
        # nothing: a real roll's trailer — its own totals line, a charge-code summary —
        # has a blank plan cell on every row, and those rows are not units.
        # Whether the plan column is MISSING or merely blank, the row has no plan — and a
        # roll with no plan column at all skipped this test entirely, so "Total Units:"
        # with the property's total rent was another unit. The test is
        # the same one either way.
        if plan is None and not _states_a_unit(ws, r, unit_id, state_cols, rent_col):
            skipped_trailer += 1
            continue
        status_raw = str(_cell(ws, r, col.get("Unit Status")) or "").strip()
        lease_status = str(_cell(ws, r, col.get("Status")) or "").strip()
        rent = _num(_cell(ws, r, col.get("Lease Rent")))
        raw.append(
            {
                "_row": r,
                "_lease_status": lease_status,
                "unit_id": str(unit_id).strip(),
                "floor_plan": str(plan).strip() if plan is not None else "",
                "sqft": _num(_cell(ws, r, col.get("Sqft"))),
                "_beds": _num(_cell(ws, r, col.get("Beds"))),
                "_baths": _num(_cell(ws, r, col.get("Baths"))),
                "status_raw": status_raw,
                "lease_start": _date(_cell(ws, r, col.get("Lease Start"))),
                "move_in": _date(_cell(ws, r, col.get("Occupied"))),
                "in_place_rent": rent if (rent or 0) > 0 else None,
                # The row this unit was read from, so a spot check can cite the cell it
                # is reading back rather than asserting agreement with itself.
                "_source_row": r,
                # The rent cell EXACTLY as the file holds it, before any coercion. A
                # single-pass extractor cannot do a truly independent second read, but it
                # can keep the raw value and let the check compare the normalized number
                # against it — which is what catches the real class of bug here: a
                # currency-formatted string, a text "$0", a rounding step. Comparing our
                # normalized value against our own normalized value would prove nothing.
                "_source_rent_raw": _cell(ws, r, col.get("Lease Rent")),
                # The source's own market rent for the unit. Carried ONLY so the
                # source-stated market total can be tied against the rows we kept —
                # it is never used as a rent in any analysis. Market rent is not
                # in-place rent, and this project does not blend the two.
                "market_rent": _num(_cell(ws, r, col.get("LRO Market"))),
            }
        )
        # Column "Name" is never read into the output. I4: unit identifiers only.

    # The export lists a unit twice while a lease is turning over: an incoming lease
    # (Status "New", a future start) and the resident actually in place (Status
    # "Current", carrying a move-out date). The lease in place on the as-of date is the
    # one this analysis is about, so that is the row kept. Dropping the other also
    # reconciles the roll to its own stated square footage.
    by_unit: dict[str, dict] = {}
    duplicates: list[str] = []
    unrecognised: set[str] = set()
    for entry in raw:
        uid = entry["unit_id"]
        if uid not in by_unit:
            by_unit[uid] = entry
            continue
        duplicates.append(uid)
        incumbent = by_unit[uid]
        if lease_in_place(entry["_lease_status"]) and not lease_in_place(
            incumbent["_lease_status"]
        ):
            by_unit[uid] = entry
            continue
        if lease_in_place(incumbent["_lease_status"]):
            continue
        # NEITHER row is known to be the lease in place — because one status is
        # unrecognised, or because both name an incoming lease. The status cannot decide
        # it, so the lease DATES do: on a turning-over unit the lease in place is the one
        # that STARTED EARLIER, and the incoming one starts later or in the future.
        # Keeping the first row seen instead is keeping whichever way the export happened
        # to sort, which on this shape is usually the incoming lease at the new rent.
        for value in (entry["_lease_status"], incumbent["_lease_status"]):
            text = str(value or "").strip()
            if text and not lease_recognised(text):
                unrecognised.add(text)
        challenger = _date(entry.get("lease_start"))
        holder = _date(incumbent.get("lease_start"))
        if challenger and (not holder or challenger < holder):
            by_unit[uid] = entry
    # A STATED $0 is a value; only an empty cell is an absence. `in_place_rent` is the
    # MODELLED rent and is None for both, which is right for averages and wrong for an
    # exposure calculation: it silently dropped the two largest swings on this deal and
    # halved the published figure. Both are carried, and every consumer says which it
    # means. Same MISSING-vs-zero discipline as the tax divisor and the occupancy count.
    def _stated(entry):
        raw_cell = entry.get("_source_rent_raw")
        return _num(raw_cell) if raw_cell is not None else None

    dup_detail = []
    for uid in sorted(set(duplicates)):
        cands = [e for e in raw if e["unit_id"] == uid]
        keptrow = by_unit[uid]
        dropped = [e for e in cands if e["_source_row"] != keptrow["_source_row"]]
        dup_detail.append({
            "unit_id": uid,
            "kept_row": keptrow["_source_row"],
            "kept_lease_status": normalized_lease_status(keptrow.get("_lease_status")),
            "kept_rent": keptrow.get("in_place_rent"),
            "kept_rent_stated": _stated(keptrow),
            # Carried so the market tie-out can MEASURE whether it is dedup-sensitive on
            # this deal rather than asserting it.
            "kept_market_rent": keptrow.get("market_rent"),
            "dropped": [
                {
                    "row": e["_source_row"],
                    "lease_status": normalized_lease_status(e.get("_lease_status")),
                    "rent": e.get("in_place_rent"),
                    "rent_stated": _stated(e),
                    "market_rent": e.get("market_rent"),
                }
                for e in dropped
            ],
            "candidate_rows": sorted(e["_source_row"] for e in cands),
        })
    if not has_status_column:
        notes.append(
            "This rent roll has no unit-status column, so occupancy was INFERRED from "
            "the evidence each row does carry: a unit paying rent under a lease is "
            "counted occupied, a unit with neither is counted vacant, and anything else "
            "is left Unknown rather than guessed. Occupancy, vacancy and every figure "
            "derived from them are that inference, not a number the source stated."
        )
    if skipped_unnamed or skipped_trailer:
        parts = []
        if skipped_trailer:
            parts.append(
                f"{skipped_trailer} read as a trailer (a totals or summary line, or a "
                f"row carrying nothing that describes one unit)"
            )
        if skipped_unnamed:
            parts.append(f"{skipped_unnamed} carrying no unit identifier")
        notes.append(
            f"{skipped_unnamed + skipped_trailer} row(s) below the header were SKIPPED "
            f"rather than counted as units: " + "; ".join(parts) + ". Check that number "
            "against your own roll — a real unit dropped by that test would be in it."
        )
    if duplicates:
        notes.append(
            f"{len(duplicates)} unit(s) appeared twice in the export "
            f"({', '.join(sorted(set(duplicates)))}); kept the lease in place on the "
            f"as-of date and dropped the incoming lease."
        )
    if unrecognised:
        notes.append(
            "On at least one duplicated unit NEITHER row carried a lease status this "
            "pipeline recognises (" + ", ".join(sorted(repr(v) for v in unrecognised))
            + "), so the row kept was chosen by lease date — the earlier-starting lease "
            "is the one in place. The unrecognised value is recorded as Unknown rather "
            "than being read as a new lease."
        )

    units: list[dict] = []
    # Rows whose status CELL is empty on a roll that has a status column. A blank is not
    # a code the vocabulary could name — ResMan leaves its lease-code column empty on a
    # vacant unit and writes "Vacant Unit" where the resident's name goes — so it used to
    # fall through `_status_of` to Non-revenue: 11 vacant homes read as non-revenue and
    # occupancy was 88.7% against the export's own 91.1%. A blank says nothing; the
    # unit's own evidence decides, the way it does for a roll with no status column.
    # Likewise a code the vocabulary can only call `other`: six meanings exist and `other`
    # is "none of them" — not a physical state, the absence of one. The same export's
    # lease-code column had `R` (a renewal: a resident, paying) called `other`, which
    # `_MEANING_TO_STATUS` read as Non-revenue, and two paying homes left the occupied
    # count. An explicit vacant / model / down meaning is still the model's call.
    inferred_blank = 0
    for entry in by_unit.values():
        status_raw = entry["status_raw"]
        meaning = (status_map or {}).get(status_raw) or (status_map or {}).get(status_raw.strip().lower())
        if has_status_column and status_raw and meaning != "other":
            status = _status_of(status_raw, status_map)
        elif status_raw and any(w in status_raw.lower() for w in _NON_REVENUE_WORDS):
            status = "Non-revenue"
        else:
            if has_status_column:
                inferred_blank += 1
            status = _inferred_status(
                entry.get("in_place_rent"), entry.get("lease_start"), entry.get("move_in"), as_of
            )
        if not entry["floor_plan"]:
            entry["floor_plan"] = _derived_plan(entry.get("_beds"), entry.get("_baths"))
        # A mapped column beats a guess from the plan code. `_plan_beds_baths` only
        # understands shapes like "2X2"; a roll whose plans are called A1 and B2 reported
        # zero for every unit while the real numbers sat in columns nobody read.
        beds, baths = _plan_beds_baths(entry["floor_plan"])
        if entry.get("_beds") is not None:
            beds = entry["_beds"]
        if entry.get("_baths") is not None:
            baths = entry["_baths"]
        units.append(
            {
                "unit_id": entry["unit_id"],
                "floor_plan": entry["floor_plan"],
                "beds": beds,
                "baths": baths,
                "sqft": entry["sqft"],
                "status": status,
                "lease_start": entry["lease_start"],
                "move_in": entry["move_in"],
                "lease_type": None,  # derived from the dates by compute.classify_lease
                "in_place_rent": entry["in_place_rent"],
                "source_row": entry["_source_row"],
                "source_rent_raw": (
                    entry["_source_rent_raw"]
                    if isinstance(entry["_source_rent_raw"], (int, float, str))
                    else None
                ),
                "market_rent": entry["market_rent"],
            }
        )
    units.sort(key=lambda u: u["unit_id"])

    if inferred_blank:
        notes.append(
            f"{inferred_blank} unit(s) carried no value in the status column, or a code "
            f"the column's vocabulary could not place as a physical state; their status "
            f"was inferred from the unit's own rent and lease dates (no rent and no lease "
            f"reads Vacant, a paying lease reads Occupied, anything else Unknown) rather "
            f"than counted as non-revenue."
        )

    occupied_no_rent = [
        u["unit_id"]
        for u in units
        if u["status"] == "Occupied" and u["in_place_rent"] is None
    ]
    if occupied_no_rent:
        notes.append(
            f"{len(occupied_no_rent)} occupied unit(s) carried no contract rent "
            f"({', '.join(occupied_no_rent)}); left blank rather than recorded as zero, "
            f"so the averages skip them instead of being dragged down."
        )

    from openpyxl.utils import get_column_letter

    # No rows means the mapping pointed at the wrong sheet or the wrong columns. That is
    # a caller error to report, not a ValueError from max() on an empty sequence.
    if not raw:
        return [], notes + ["no unit rows were found under the header row"], {
            "rows_in_export": 0, "units_after_dedupe": 0, "sqft_sum": 0.0,
            "stated_sqft": stated_sqft or 0.0, "sheet": ws.title,
        }
    src_totals = parse_source_totals(ws, header_row, max(e["_source_row"] for e in raw))
    stats = {
        "source_totals": src_totals,
        "duplicate_units": sorted(set(duplicates)),
        "duplicate_detail": dup_detail,
        "rows_in_export": len(raw),
        "units_after_dedupe": len(units),
        "sqft_sum": round(sum(u["sqft"] or 0 for u in units), 0),
        "stated_sqft": stated_sqft,
        "stated_units": stated["units"],
        "stated_occupancy": stated["occupancy"],
        "sheet": ws.title,
        "rent_column": get_column_letter(col["Lease Rent"]),
        # Guarded like every other Sqft read above it. The row loop already handled a
        # roll with no square-footage column; this one lookup did not, so a perfectly
        # valid roll — the schema explicitly allows `sqft_column: None` — died here with
        # a KeyError and the visitor got "generation_failed".
        "sqft_column": get_column_letter(col["Sqft"]) if col.get("Sqft") else None,
    }
    return units, notes, stats


#: Unit-status values that mean a RESIDENT IS PHYSICALLY IN THE UNIT.
#:
#: A resident on notice has not moved out — they are in the unit, paying, until their
#: move-out date. The source's own summary block treats "Occupied" as one bucket of seven
#: and lists the notice buckets separately, so its 292 is a LEASING status, not physical
#: occupancy. Reading it as occupancy would understate this property by 21 units.
PHYSICALLY_OCCUPIED_BUCKETS = ("Occupied", "Notice Available", "Notice Rented")

#: On a turnover pair the export lists the lease IN PLACE on the as-of date with this
#: lease status, and the incoming lease as "New". Keeping the wrong one costs real rent.
IN_PLACE_LEASE_STATUS = "Current"

#: Every lease-status value this extractor emits, enumerated so validation can reject a
#: typo. `None` is a real value here — 20 rows on the showcase export carry a blank Status.
#:
#: This matters more than it looks: an unrecognised status silently routes a unit into the
#: "no in-place row existed" branch, which is the branch that EXCUSES a wrong keep. A
#: one-character typo therefore turns a MISMATCH into an OK.
#: `""` is how the extractor represents a BLANK Status — it reads the cell through
#: `str(... or "").strip()`, so a blank arrives as an empty string, never as None.
#: Enumerated from what the extractor EMITS, not from what the source cells hold:
#: the first cut listed None and turned every genuinely-blank row unusable.
LEASE_STATUS_VOCABULARY: tuple = ("Current", "New", "Renewal", "", None)

#: Buckets that are neither occupied nor rentable-vacant. Kept separate because the source
#: splits Model out of Unrentable in its summary while the per-unit rows do not.
NON_REVENUE_BUCKETS = ("Model", "Unrentable")


def parse_source_totals(ws, header_row: int, last_data_row: int) -> dict:
    """The totals the SOURCE states about itself, found by label.

    A tie-out against a figure this project computed is not a tie-out. This reads the
    export's own `Occupancy and Rents Summary` block — a per-status table BELOW the unit
    rows, whose column headers name what each column holds — so the checks compare our
    numbers against the vendor's.

    Both extraction paths previously reported NOT CHECKED on every unit-count and
    occupancy tie-out for this file *while the file stated all of them*, because one only
    looked above the header and below the data for a label/value pair in columns A and B,
    and the other's vendor arm did not match this block's shape. Two independent
    extractions, the same blind spot.

    Absent or unparseable returns empty — an honest NOT CHECKED, never an assumed zero.

    Two vendor shapes, tried in turn; the first whose title row is found answers:

    * RealPage — ``Occupancy and Rents Summary``: a ``Unit Status`` header row naming
      the columns, one row per status, a labelled ``Totals:`` row.
    * ResMan — ``Property Occupancy``: a ``Type | Status | Market Rent | Units | Square
      Footage`` header, a ``Total / Occupied`` and a ``Total / Vacant`` row, then a totals
      row that carries NO label at all — numbers only. One filed defect (the block was there,
      the reader had no arm for it, and a roll that stated every total read NOT CHECKED).
    """
    out: dict = {"buckets": {}, "units_total": None, "market_rent": None,
                 "current_rent": None, "sqft": None, "block_row": None, "columns": {}}
    for arm in (_totals_realpage, _totals_resman):
        found = arm(ws, last_data_row)
        if found is not None:
            out.update(found)
            return out
    return out


#: How far below the last unit row a summary block may sit. ResMan puts a per-column
#: totals row and a charge-code table between the units and its `Property Occupancy`
#: block; RealPage puts one `Total Units:` line.
_TOTALS_BLOCK_REACH = 120


def _totals_realpage(ws, last_data_row: int) -> "dict | None":
    """The RealPage `Occupancy and Rents Summary` block, or None when there is none."""
    out: dict = {"buckets": {}, "units_total": None, "market_rent": None,
                 "current_rent": None, "block_row": None, "columns": {}}
    head = None
    for r in range(last_data_row + 1, min(last_data_row + _TOTALS_BLOCK_REACH, ws.max_row) + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and "occupancy" in v.lower() and "summary" in v.lower():
            head = r
            break
    if head is None:
        return None
    out["block_row"] = head

    # The column a number lives in comes from the block's OWN header row, never a fixed
    # letter: this block puts unit counts in D and two different rent measures in C and E.
    hdr = None
    for r in range(head + 1, head + 4):
        if isinstance(ws.cell(r, 1).value, str) and "unit status" in str(ws.cell(r, 1).value).lower():
            hdr = r
            break
    if hdr is None:
        return out
    cols: dict[str, int] = {}
    for c in range(2, 12):
        label = ws.cell(hdr, c).value
        if isinstance(label, str) and label.strip():
            cols[" ".join(label.split()).lower()] = c
    out["columns"] = cols
    c_units = cols.get("# units") or cols.get("units")
    c_market = next((v for k, v in cols.items() if "market" in k), None)
    c_current = next((v for k, v in cols.items() if "current" in k), None)

    for r in range(hdr + 1, hdr + 14):
        label = ws.cell(r, 1).value
        if not isinstance(label, str) or not label.strip():
            continue
        name = label.strip().rstrip(":")
        units = ws.cell(r, c_units).value if c_units else None
        if name.lower().startswith("total"):
            out["units_total"] = int(units) if isinstance(units, (int, float)) else None
            if c_market:
                out["market_rent"] = _num(ws.cell(r, c_market).value)
            if c_current:
                out["current_rent"] = _num(ws.cell(r, c_current).value)
            break
        if isinstance(units, (int, float)):
            out["buckets"][name] = int(units)
    return out


def _totals_resman(ws, last_data_row: int) -> "dict | None":
    """The ResMan `Property Occupancy` block, or None when there is none.

    The block's own header names its columns — status, units, market rent, square
    footage — in whatever letters the export chose (D/H/P/V on the file that found this;
    the per-unit columns are elsewhere). Each labelled row is a status bucket; the row
    with a unit count and NO text in the type or status column is the total. The
    `Unit Type Occupancy` block that follows repeats the shape per floor plan and is not
    read: the property block above it is the one that states the roll's totals.
    """
    out: dict = {"buckets": {}, "units_total": None, "market_rent": None,
                 "current_rent": None, "sqft": None, "block_row": None, "columns": {}}
    head = None
    for r in range(last_data_row + 1, min(last_data_row + _TOTALS_BLOCK_REACH, ws.max_row) + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and "property occupancy" in v.lower():
            head = r
            break
    if head is None:
        return None
    out["block_row"] = head
    hdr = None
    cols: dict[str, int] = {}
    for r in range(head + 1, head + 4):
        labels = {
            " ".join(str(ws.cell(r, c).value).split()).lower(): c
            for c in range(1, ws.max_column + 1)
            if isinstance(ws.cell(r, c).value, str) and ws.cell(r, c).value.strip()
        }
        if "status" in labels and ("units" in labels or "# units" in labels):
            hdr, cols = r, labels
            break
    if hdr is None:
        return out
    out["columns"] = cols
    c_status = cols["status"]
    c_units = cols.get("units") or cols.get("# units")
    c_market = next((v for k, v in cols.items() if "market" in k), None)
    c_sqft = next((v for k, v in cols.items() if "square" in k or "sq" in k.split()), None)
    # The block ENDS at its totals row or at the next table — whichever comes first.
    # Reading on into the `Unit Type Occupancy` table that follows overwrote the
    # property's buckets with a floor plan's and took that table's total for the
    # property's. Blank rows are skipped, not stops: the real export
    # has one after the header and a variant may have one between buckets. The next table announces itself by a title in column A that is not a totals
    # label, or by repeating the header word `Status` in the status column — whether or
    # not the two share a row. The totals row is the first row with a unit count and no
    # status, OR a status cell that itself reads as a totals label; column A may say
    # `Total` on it.
    # Fourteen NON-BLANK rows: a spaced export must not spend the window on empties
    #. A totals row is recognised BEFORE the next-table test, so a row
    # titled `Total Occupancy` with `Total` in the status column is the property's total
    # and not the per-plan table's title.
    seen = 0
    for r in range(hdr + 1, ws.max_row + 1):
        status = ws.cell(r, c_status).value
        units = ws.cell(r, c_units).value
        first = ws.cell(r, 1).value
        has_status = isinstance(status, str) and bool(status.strip())
        has_units = isinstance(units, (int, float)) and not isinstance(units, bool)
        first_text = first.strip() if isinstance(first, str) else ""
        if not (has_status or has_units or first_text):
            continue  # a blank row: skipped, never a stop, never counted
        seen += 1
        if seen > 14:
            break
        is_total = has_units and (not has_status or bool(_TOTALS_LABEL.match(status)))
        if is_total:
            out["units_total"] = int(units)
            if c_market:
                out["market_rent"] = _num(ws.cell(r, c_market).value)
            if c_sqft:
                out["sqft"] = _num(ws.cell(r, c_sqft).value)
            break
        next_table = (
            (has_status and " ".join(status.split()).lower() == "status")
            or (first_text and "occupancy" in first_text.lower())
            or (first_text and not _TOTALS_LABEL.match(first_text) and not has_status and not has_units)
        )
        if next_table:
            break
        if has_status and has_units:
            out["buckets"][status.strip()] = int(units)
    return out


def _roll_stated(stats: dict) -> dict:
    """The source export's OWN stated totals, plus where its rent column lives.

    Anything the export did not publish stays None, which degrades that tie-out to NOT
    CHECKED rather than failing it. A missing source total is a fact about the export, not
    a defect in the parse.
    """
    def _int(v):
        return int(v) if isinstance(v, (int, float)) else None

    st = stats.get("source_totals") or {}
    # Recorded so any downstream claim of the form "this difference is the dedup" can be
    # CHECKED against the units it names, rather than believed.
    buckets = st.get("buckets") or {}
    # Physical occupancy DERIVED from the source's own buckets, not from a bucket the
    # source happens to label "Occupied". That label is one of seven statuses here; the
    # notice buckets hold residents who have not moved out.
    occ = None
    if buckets:
        occ = sum(buckets.get(b, 0) for b in PHYSICALLY_OCCUPIED_BUCKETS)
    return {
        # The source's own stated unit total beats a count we derived.
        "units_total": st.get("units_total") or _int(stats.get("stated_units")),
        # A `Total Sqft:` line above the header first; else the summary block's own
        # square-footage total (ResMan states it there and nowhere else).
        "sqft": stats.get("stated_sqft") if stats.get("stated_sqft") is not None else st.get("sqft"),
        "occupancy": (
            float(stats["stated_occupancy"])
            if isinstance(stats.get("stated_occupancy"), (int, float)) else None
        ),
        "sheet": stats.get("sheet"),
        "rent_column": stats.get("rent_column"),
        "sqft_column": stats.get("sqft_column"),
        # --- from the source's own summary block ---
        "buckets": {k: int(v) for k, v in buckets.items()},
        "occupied_units": occ,
        "occupied_basis": (
            " + ".join(b for b in PHYSICALLY_OCCUPIED_BUCKETS if b in buckets)
            if buckets else None
        ),
        "market_rent": st.get("market_rent"),
        "current_rent": st.get("current_rent"),
        "duplicate_units": sorted(stats.get("duplicate_units") or []),
        "duplicate_detail": stats.get("duplicate_detail") or [],
        # The lease status a retained duplicate row MUST carry. Recorded as data so the
        # check reads the rule rather than restating it.
        "in_place_lease_status": IN_PLACE_LEASE_STATUS,
    }
