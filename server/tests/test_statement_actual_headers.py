#!/usr/bin/env python3
"""One accounting system's trailing P&L writes its month headings as ``Aug 2025 Actual`` — and the first
real deal through the engine (a smoke run) was filed as background reading
because of it: twelve month columns, zero recognised, no T-12 in the pack.

Pins: the suffix (or prefix) ``Actual`` / ``Actuals`` is tolerated by the classifier's
``is_month_header`` and by the reader's ``month_label``; ``Budget`` / ``Variance`` stay
excluded; a synthetic sheet in that layout — titles, a header row with the account
column in A and the month columns from F, account lines in column E with section totals
in A — classifies as a confident T-12 with the label column found in E.

Usage: python server/tests/test_statement_actual_headers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from _harness import Checker, guard  # noqa: E402

from server import classify  # noqa: E402
import source_readers as raw  # noqa: E402

MONTHS = ["Aug 2025", "Sep 2025", "Oct 2025", "Nov 2025", "Dec 2025", "Jan 2026",
          "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026", "Jul 2026"]


def layout_rows() -> list[list]:
    header = ["Account", None, None, None, None] + [f"{m} Actual" for m in MONTHS] + ["Adjusted Total", "Variance"]
    rows: list[list] = [
        ["Sample Property"], ["Property Services Co"], ["Trailing Profit And Loss Detail"],
        ["July 2026 - Accrual - Accounting Book"], ["Printed 8/7/2026"], header,
        [None, "INCOME"], [None, None, "RENTAL REVENUE"], [None, None, None, "ADJUSTED RENT"],
    ]
    lines = [("4250-0000 Market Rent", 170_000), ("4305-0000 Rent Adjustment", 0), ("4310-0000 Loss/Gain to Lease", -3_000),
             ("4710-0000 Less: Concessions", -2_000), ("4720-0000 Less: Vacancy", -8_000), ("4810-0000 Utility Reimbursement", 9_000),
             ("4910-0000 Late Fees", 1_200), ("6110-0000 Payroll", 20_000), ("6210-0000 Repairs", 6_000),
             ("6310-0000 Utilities - Water", 7_000), ("6410-0000 Insurance", 4_000), ("6510-0000 Property Taxes", 11_000),
             ("6610-0000 Management Fee", 5_000), ("6710-0000 Marketing", 1_000)]
    for label, v in lines:
        rows.append([None, None, None, None, label] + [v] * 12 + [v * 12, 0])
    rows.append(["Total ADJUSTED RENT", None, None, None, None] + [167_000] * 12 + [167_000 * 12, 0])
    rows.append(["NET OPERATING INCOME", None, None, None, None] + [100_000] * 12 + [1_200_000, 0])
    return rows


def main() -> int:
    c = Checker("test_statement_actual_headers")
    c.section("the header cell")
    c.ok("'Aug 2025 Actual' is a month header", classify.is_month_header("Aug 2025 Actual"))
    c.ok("'Actual Aug 2025' too", classify.is_month_header("Actual Aug 2025"))
    c.ok("'Aug 2025 (Actual)' too", classify.is_month_header("Aug 2025 (Actual)"))
    c.ok("plain 'Aug 2025' still is", classify.is_month_header("Aug 2025"))
    c.ok("'Aug 2025 Budget' is NOT", not classify.is_month_header("Aug 2025 Budget"))
    c.ok("'Aug 2025 Variance' is NOT", not classify.is_month_header("Aug 2025 Variance"))
    c.ok("'Adjusted Total' is NOT", not classify.is_month_header("Adjusted Total"))
    c.equal("month_label('Aug 2025 Actual') → 2025-08", raw.month_label("Aug 2025 Actual"), "2025-08")
    c.equal("month_label('Jul 2026 Actuals') → 2026-07", raw.month_label("Jul 2026 Actuals"), "2026-07")
    c.equal("month_label('Aug 2025 Budget') → None", raw.month_label("Aug 2025 Budget"), None)

    c.section("that layout as a whole")
    rows = layout_rows()
    doc = {"filename": "Sample T12.xlsx", "kind": "xlsx", "sheets": [{"name": "Sheet", "rows": rows}], "text": ""}
    sig = classify.grid_signals(doc["sheets"])
    c.equal("twelve month columns recognised", sig.month_headers, 12)
    c.equal("header row found at row 6", sig.header_row_index, 5)
    res = classify.classify(doc)
    c.equal("classified as a T-12", res.role, "t12")
    c.ok("confidently", res.confident, res.reason)
    code_col, label_col = classify.detect_t12_columns(rows, 5)
    c.equal("the label column is E (the account lines), not A (the section totals)", label_col, 5)
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
