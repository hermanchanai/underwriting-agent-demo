"""Do our vendored T-12 category labels still match the contract, character-exact?

The labels are not decoration — they are the join key. The workbook's P&L sums with
`SUMIF` on the Category text, and a downstream financial model reads the same strings.

A drifted label does not raise. The SUMIF matches nothing, that category's money silently
leaves the total, and every check downstream still passes. This project once shipped two
drifted labels for a long stretch:

    ours 'G&A'                    vs  contract 'General and Administrative'
    ours 'Gain/(Loss) to Lease'   vs  contract 'Gain / (Loss) to Lease'

They were found only by extracting the same deal twice and diffing, which is a slow and
lucky way to find a typo. This test is the fast way: the vendored strings must equal a
frozen expectation, so a future edit to `schema.py` fails here rather than in a comparison
someone happens to run.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import schema
from _testkit import Checker, guard

#: Frozen copy of the skill's contract, character-exact as of 2026-09-01. Sorted the way
#: the skill's SKILL.md lists them.
CONTRACT_REVENUE: tuple[str, ...] = (
    "Gross Potential Rent",
    "Gain / (Loss) to Lease",
    "Gross Scheduled Rent",
    "Vacancy",
    "Concessions",
    "Non-Revenue Units",
    "Delinquency",
    "Utility Reimbursements",
    "Other Income",
    "Revenue Adjustments",
    "Total Revenue",
)

CONTRACT_EXPENSE: tuple[str, ...] = (
    "General and Administrative",
    "Leasing and Marketing",
    "Payroll",
    "Insurance",
    "Property Taxes",
    "Legal and Professional",
    "Management Fee",
    "Landscaping",
    "Repairs and Maintenance",
    "Unit Turns",
    "Utilities",
    "Expense Adjustments",
)

def main() -> int:
    c = Checker("test_category_contract")

    c.section("the vendored lists match the frozen contract, character-exact")
    c.equal("revenue list", list(schema.T12_REVENUE_CATEGORIES), list(CONTRACT_REVENUE))
    c.equal("expense list", list(schema.T12_EXPENSE_CATEGORIES), list(CONTRACT_EXPENSE))
    # Per-label, so a failure names the offender instead of dumping two lists.
    for ours, theirs in zip(schema.T12_REVENUE_CATEGORIES, CONTRACT_REVENUE):
        c.equal(f"revenue label {theirs!r}", ours, theirs)
    for ours, theirs in zip(schema.T12_EXPENSE_CATEGORIES, CONTRACT_EXPENSE):
        c.equal(f"expense label {theirs!r}", ours, theirs)
    c.equal("revenue count", len(schema.T12_REVENUE_CATEGORIES), 11)
    c.equal("expense count", len(schema.T12_EXPENSE_CATEGORIES), 12)

    c.section("no label carries stray whitespace that a SUMIF would not forgive")
    for label in (*schema.T12_REVENUE_CATEGORIES, *schema.T12_EXPENSE_CATEGORIES):
        c.equal(f"{label!r} is already normalized", " ".join(label.split()), label)

    c.section("the I3 membership assertion uses these exact strings")
    # normalize_category / assert_categories are what actually gate an extraction. If they
    # accept a label the skill would never write, the assertion is decorative.
    for label in CONTRACT_REVENUE + CONTRACT_EXPENSE:
        c.equal(f"normalize_category({label!r}) is identity", schema.normalize_category(label), label)
    for bad in ("G&A", "Gain/(Loss) to Lease", "General & Administrative", "Property Tax"):
        if bad in CONTRACT_REVENUE + CONTRACT_EXPENSE:
            continue
        c.ok(
            f"{bad!r} is NOT silently accepted as a category",
            schema.normalize_category(bad) != bad
            or bad not in (*schema.T12_REVENUE_CATEGORIES, *schema.T12_EXPENSE_CATEGORIES),
        )

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
