#!/usr/bin/env python3
"""The page-break filter drops header blocks, never an account line that contains a header word.

The showcase export repeats a page header every 50 rows — `Database:`, `ENTITY:`, `Accrual`,
`Account | Description | <months>` — and the reader skips a row when any of those words
appears ANYWHERE in its code-plus-label text. On a real statement  that
substring test dropped `5720-0000 Interest on Bank Accounts` ($260) and `6400-0010 Accounting
Fees` ($3,024) silently: two live runs read revenue $260 short and expenses $3,024 short, the
tie-out said REVIEW, and nothing named the lines. A header word is one that HEADS a cell.

Usage: python server/tests/test_t12_page_noise.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from _harness import Checker, guard  # noqa: E402

import source_readers as raw  # noqa: E402

MONTHS = ["Aug 2025", "Sep 2025", "Oct 2025", "Nov 2025", "Dec 2025", "Jan 2026",
          "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026", "Jul 2026"]
INCOME = [
    ("4250-0000 Market Rent", 170_000, "Gross Potential Rent"),
    ("5700-0000 Miscellaneous Income", 700, "Other Income"),
    ("5720-0000 Interest on Bank Accounts", 21.69, "Other Income"),
    ("5725-0000 Laundry Income", 766, "Other Income"),
]
EXPENSE = [
    ("6010-0010 Electricity", 9_000, "Utilities"),
    ("6400-0005 Association Dues", 57, "General and Administrative"),
    ("6400-0010 Accounting Fees", 252, "Legal and Professional"),
    ("6400-0030 Bank Charges", 111, "General and Administrative"),
    ("6400-0200 Accounts Receivable Service", 40, "General and Administrative"),
    ("6410-0000 Rent Accrual Adjustment", 15, "Expense Adjustments"),
    # Review finding: a label that BEGINS with the header word is still an account.
    ("6400-0210 Account Maintenance Fees", 100, "General and Administrative"),
    ("6420-0000 Accrual Adjustment", 12, "Expense Adjustments"),
]


def header_block() -> list[list]:
    """The page-break rows the showcase export repeats: every one is still noise."""
    return [
        ["Database: SAMPLE", None, "12 Month Profit & Loss Statement"],
        ["ENTITY: SAMPLE", None, "SAMPLE"],
        [None],
        ["Accrual", None, "For 12 Month Period Ending July 31, 2026"],
        ["Account", "Description"] + [None] * 3 + MONTHS,
    ]


def statement() -> list[list]:
    header = ["Account", None, None, None, None] + [f"{m} Actual" for m in MONTHS] + ["Adjusted Total", "Variance"]
    rows: list[list] = [["Sample"], ["Services Co"], ["Trailing Profit And Loss Detail"],
                        ["July 2026 - Accrual - Accounting Book: Default"], ["Printed 8/7/2026"], header, [None, "INCOME"]]

    def line(label, v):
        return [None, None, None, label, None] + [v] * 12 + [v * 12, 0]

    def total(label, v):
        return [label, None, None, None, None] + [v] * 12 + [v * 12, 0]

    for label, v, _ in INCOME:
        rows.append(line(label, v))
    rows.append(total("TOTAL INCOME", sum(v for _, v, _ in INCOME)))
    rows.append([None, "EXPENSE"])
    for label, v, _ in EXPENSE[:3]:
        rows.append(line(label, v))
    rows.extend(header_block())          # a page break in the middle of the expense schedule
    for label, v, _ in EXPENSE[3:]:
        rows.append(line(label, v))
    rows.append(total("TOTAL EXPENSE", sum(v for _, v, _ in EXPENSE)))
    rows.append(total("NET OPERATING INCOME", sum(v for _, v, _ in INCOME) - sum(v for _, v, _ in EXPENSE)))
    rows.append(["© Vendor"])
    return rows


def main() -> int:
    c = Checker("test_t12_page_noise")
    rows = statement()
    header_index = 5
    from server import classify
    code_col, label_col = classify.detect_t12_columns(rows, header_index)
    label_map = {label: cat for label, _, cat in INCOME + EXPENSE}
    subtotal_labels = {"total_revenue": "TOTAL INCOME", "total_expenses": "TOTAL EXPENSE",
                       "net_operating_income": "NET OPERATING INCOME"}
    block, notes = raw.parse_t12(None, 100, sheet=raw.GridSheet(rows), header_row=header_index + 1,
                                 code_col=code_col, label_col=label_col, label_map=label_map,
                                 subtotal_labels=subtotal_labels)
    got = {r["label"]: r for r in block["rows"]}

    c.section("an account line that CONTAINS a header word is an account line")
    for want in ("5720-0000 Interest on Bank Accounts", "6400-0010 Accounting Fees",
                 "6400-0200 Accounts Receivable Service", "6410-0000 Rent Accrual Adjustment",
                 "6400-0210 Account Maintenance Fees", "6420-0000 Accrual Adjustment"):
        c.ok(f"{want!r} is read", want in got, str(sorted(got)))
    c.equal("every mapped line is a row", sorted(got), sorted(label_map))
    inc = sum(v for _, v, _ in INCOME) * 12
    exp = sum(v for _, v, _ in EXPENSE) * 12
    rev_rows = sum(sum(r["monthly"]) for r in block["rows"] if r["category"] in ("Gross Potential Rent", "Other Income"))
    exp_rows = sum(sum(r["monthly"]) for r in block["rows"] if r["category"] not in ("Gross Potential Rent", "Other Income"))
    c.close("revenue rows sum to the statement's TOTAL INCOME", rev_rows, inc, 0.05)
    c.close("expense rows sum to the statement's TOTAL EXPENSE", exp_rows, exp, 0.05)
    c.ok("nothing was reported EXCLUDED", not any("EXCLUDED" in n for n in notes), str(notes))

    c.section("the filter itself: header cells are noise, account labels are not (load-bearing, unlike the row checks below)")
    fn = raw._page_noise
    for cell in ("Account", "Accrual", "Database: SAMPLE", "ENTITY: SAMPLE", "Page: 3", "Date: 8/7/2026", "Time: 10:06"):
        c.ok(f"{cell!r} is a header cell", fn(cell, None))
    for cell in ("6400-0210 Account Maintenance Fees", "Account Maintenance Fees", "Accrual Adjustment",
                 "Accounts Payable", "Date of Service", "Page Fees", "5720-0000 Interest on Bank Accounts"):
        c.ok(f"{cell!r} is an account label", not fn(cell, None))

    c.section("the page-break header block is still dropped")
    c.ok("no row for the Database: line", not any("Database" in k for k in got))
    c.ok("no row for the ENTITY: line", not any("ENTITY" in k for k in got))
    c.ok("no row for the bare Accrual line", "Accrual" not in got)
    c.ok("no row for the repeated Account/Description header", "Account" not in got and "Description" not in got)
    st = block["source_subtotals"]
    c.equal("Total Revenue from the statement's row", st.get("Total Revenue"), inc)
    c.equal("Total Operating Expenses from the statement's row", st.get("Total Operating Expenses"), exp)
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
