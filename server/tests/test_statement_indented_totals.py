#!/usr/bin/env python3
"""One accounting system's trailing P&L keeps its account lines in column E and its TOTAL rows in column A.

The engine read labels from the label column only, so the statement's own ``TOTAL INCOME``,
``TOTAL EXPENSE`` and ``NET OPERATING INCOME`` rows never reached the mapper: every line's
section was unknown, the model excluded the rental-revenue block on its wording alone
("Market Rent", "Less: Vacancy"), NOI came out negative, and every tie-out read NOT CHECKED
.

Pins: a row whose label column is empty takes its label from the leftmost text cell among
the columns before the first month column (never the code column) — the export indents its
account lines to D in one section and E in another, and prints its TOTAL rows in A; the
mapper's label list therefore carries every line and the TOTAL rows; ``statement_sections`` bounds income and expenses off them; the reader keeps the
account lines the mapping names, drops the bare section headings, and records the
statement's own three subtotals for the error check.

Usage: python server/tests/test_statement_indented_totals.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from _harness import Checker, guard  # noqa: E402

from server import extract_llm  # noqa: E402
import source_readers as raw  # noqa: E402

MONTHS = ["Aug 2025", "Sep 2025", "Oct 2025", "Nov 2025", "Dec 2025", "Jan 2026",
          "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026", "Jul 2026"]
LINES = [
    ("4250-0000 Market Rent", 170_000, "Gross Potential Rent"),
    ("4310-0000 Loss/Gain to Lease", -3_000, "Gain / (Loss) to Lease"),
    ("4710-0000 Less: Concessions", -2_000, "Concessions"),
    ("4730-0000 Less: Vacancy", -8_000, "Vacancy"),
    ("4750-0000 Less: Down", -1_000, "Non-Revenue Units"),
    ("5850-0000 Utility Reimbursement", 9_000, "Utility Reimbursements"),
    ("5800-0000 Late Charge Fees", 1_200, "Other Income"),
    ("6010-0030 Water/Sewer", 7_000, "Utilities"),
    ("6010-0010 Electricity", 9_000, "Utilities"),
    ("6100-0130 Landscaping Contract", 3_000, "Landscaping"),
    ("6600-0005 Manager", 20_000, "Payroll"),
    ("6600-0020 Clerical/Leasing", 6_000, "Payroll"),
    ("6800-0000 Insurance", 4_000, "Insurance"),
    ("6900-0000 Property Taxes", 11_000, "Property Taxes"),
]


def layout_rows() -> list[list]:
    header = ["Account", None, None, None, None] + [f"{m} Actual" for m in MONTHS] + ["Adjusted Total", "Variance"]
    rows: list[list] = [["Sample"], ["Services Co"], ["Trailing Profit And Loss Detail"], ["July 2026 - Accrual"],
                        ["Printed 8/7/2026"], header, [None, "INCOME"], [None, None, "RENTAL REVENUE"],
                        [None, None, None, "ADJUSTED RENT"]]
    def line(label, v, depth=5):
        return [None] * (depth - 1) + [label] + [None] * (5 - depth) + [v] * 12 + [v * 12, 0]
    def total(label, v):
        return [label, None, None, None, None] + [v] * 12 + [v * 12, 0]
    inc = LINES[:7]; exp = LINES[7:]
    for label, v, _ in inc[:5]:
        rows.append(line(label, v))
    rows.append(total("Total ADJUSTED RENT", sum(v for _, v, _ in inc[:5])))
    rows.append([None, None, "OTHER INCOME"])
    for label, v, _ in inc[5:]:
        rows.append(line(label, v, depth=4))  # the real export indents this block to column D
    rows.append(total("Total OTHER INCOME", sum(v for _, v, _ in inc[5:])))
    rows.append(total("TOTAL INCOME", sum(v for _, v, _ in inc)))
    rows.append([None, "EXPENSE"])
    for label, v, _ in exp:
        rows.append(line(label, v, depth=4))  # expenses too sit in column D
    rows.append(total("TOTAL EXPENSE", sum(v for _, v, _ in exp)))
    rows.append(total("NET OPERATING INCOME", sum(v for _, v, _ in inc) - sum(v for _, v, _ in exp)))
    rows.append([None, None, None, None, "7100-0000 Capital Improvements"] + [5_000] * 12 + [60_000, 0])
    rows.append(["© Vendor"])
    return rows


def main() -> int:
    c = Checker("test_statement_indented_totals")
    rows = layout_rows()
    header_index = 5
    # The pipeline's own choice: the column with the most varied prose — D here, as on the
    # real export, where the rental-revenue block sits one column to the RIGHT of it.
    from server import classify
    code_col, label_col = classify.detect_t12_columns(rows, header_index)
    c.equal("the pipeline picks column D as the label column (most lines)", label_col, 4)

    c.section("labels for the mapper carry the TOTAL rows from column A")
    fn = getattr(raw, "labels_for_mapping", None)
    if not c.ok("raw.labels_for_mapping exists", callable(fn)):
        return c.finish()
    labels = fn(rows, header_index, label_col, code_col)
    for want in ("4250-0000 Market Rent", "TOTAL INCOME", "TOTAL EXPENSE", "NET OPERATING INCOME", "Total ADJUSTED RENT"):
        c.ok(f"{want!r} is in the list", want in labels)
    c.ok("the bare section headings are in the list too (harmless: no numbers, the reader drops them)",
         "INCOME" in labels and "ADJUSTED RENT" in labels)
    c.ok("a line indented to column D is in the list beside the lines in E",
         "5800-0000 Late Charge Fees" in labels and "4250-0000 Market Rent" in labels)

    c.section("sections are bounded by the statement's own totals")
    secs = extract_llm.statement_sections(labels)
    c.equal("Market Rent is income", secs.get("4250-0000 Market Rent"), extract_llm.SECTION_INCOME)
    c.equal("Less: Vacancy is income", secs.get("4730-0000 Less: Vacancy"), extract_llm.SECTION_INCOME)
    c.equal("Manager is an expense", secs.get("6600-0005 Manager"), extract_llm.SECTION_OPEX)
    c.equal("Capital Improvements is below NOI", secs.get("7100-0000 Capital Improvements"), extract_llm.SECTION_BELOW)
    c.equal("TOTAL EXPENSE is structural", secs.get("TOTAL EXPENSE"), extract_llm.SECTION_STRUCTURAL)

    c.section("the reader keeps the mapped lines, drops the headings, records the subtotals")
    label_map = {label: cat for label, _, cat in LINES}
    subtotal_labels = {"total_revenue": "TOTAL INCOME", "total_expenses": "TOTAL EXPENSE",
                       "net_operating_income": "NET OPERATING INCOME"}
    block, notes = raw.parse_t12(None, 100, sheet=raw.GridSheet(rows), header_row=header_index + 1,
                                 code_col=code_col, label_col=label_col, label_map=label_map,
                                 subtotal_labels=subtotal_labels)
    got = {r["label"]: r["category"] for r in block["rows"]}
    c.equal("every mapped account line is a row", sorted(got), sorted(label_map))
    c.equal("Market Rent → Gross Potential Rent", got.get("4250-0000 Market Rent"), "Gross Potential Rent")
    c.ok("no bare heading became a row", not any(k in got for k in ("INCOME", "EXPENSE", "ADJUSTED RENT")))
    c.ok("no bare heading is reported as an excluded line", not any("INCOME" in n and "EXCLUDED" in n for n in notes), str(notes))
    st = block["source_subtotals"]
    inc_total = sum(v for _, v, _ in LINES[:7]) * 12
    exp_total = sum(v for _, v, _ in LINES[7:]) * 12
    c.equal("Total Revenue from the statement's TOTAL INCOME row", st.get("Total Revenue"), inc_total)
    c.equal("Total Operating Expenses from TOTAL EXPENSE", st.get("Total Operating Expenses"), exp_total)
    c.equal("NOI from NET OPERATING INCOME", st.get("Net Operating Income"), inc_total - exp_total)
    c.ok("the capital line below NOI is left out", "7100-0000 Capital Improvements" not in got)
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
