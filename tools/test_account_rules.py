"""Tests for the deterministic account-label layer.

Real income statements do not write "Repairs and Maintenance". They write
``5100-000 R & M - HVAC``, ``PAYROLL TAXES``, ``Landscape/Grounds Contract``. The vendored
category list is the vocabulary this project publishes; this layer is the mapping from
what a chart of accounts actually says onto it, so that a model call is needed only for
the genuinely ambiguous remainder.

**Every label in this file is invented.** They mimic the *shapes* real exports use —
ALL-CAPS, dashes, GL prefixes, sub-accounts, ampersands — and none of them is copied from
a deal. A real chart of accounts is a client artifact and does not belong in a repository
that gets published.

Two rules govern what belongs in the mapping layer at all:

* **A wrong category is worse than no category.** An unmapped label goes to the model,
  costs a fraction of a cent, and is usually right. A mis-mapped label silently moves
  money between lines of a P&L that someone underwrites from. So every rule here is
  checked against the near-miss that would break it — ``PAYROLL TAXES`` must not become
  Property Taxes, ``MANAGEMENT FEE`` must not become General and Administrative.
* **Subtotals are recognised, not categorised.** A statement's own ``NET RENTAL INCOME``
  line is a running total of the lines above it; tagging it as revenue double-counts the
  whole rent block.

Usage: python tools/test_account_rules.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import schema
from _testkit import Checker, guard

#: (label, expected category) — every one invented, shapes only.
EXPECTED: tuple[tuple[str, str], ...] = (
    # -- revenue -------------------------------------------------------------------
    ("MARKET RENT", "Gross Potential Rent"),
    ("Gross Potential Rent - Residential", "Gross Potential Rent"),
    ("4010-000 POTENTIAL RENT", "Gross Potential Rent"),
    ("LOSS/GAIN TO LEASE", "Gain / (Loss) to Lease"),
    ("Loss to Lease", "Gain / (Loss) to Lease"),
    ("GAIN (LOSS) TO LEASE", "Gain / (Loss) to Lease"),
    ("VACANCY", "Vacancy"),
    ("Vacancy Loss - Residential", "Vacancy"),
    ("4100 VACANCY/CREDIT LOSS", "Vacancy"),
    ("ONE-TIME CONCESSION", "Concessions"),
    ("Concessions - Renewal", "Concessions"),
    ("MODEL UNITS", "Non-Revenue Units"),
    ("STAFF UNITS", "Non-Revenue Units"),
    ("MANAGEMENT OFFICE/MODEL", "Non-Revenue Units"),
    ("Employee Unit Discount", "Non-Revenue Units"),
    ("BAD DEBT WRITE-OFF - OTHER", "Delinquency"),
    ("Delinquency Expense", "Delinquency"),
    ("4180 COLLECTION LOSS", "Delinquency"),
    ("UTILITIES RECOVERY", "Utility Reimbursements"),
    ("Water/Sewer Reimbursement", "Utility Reimbursements"),
    ("VALET TRASH REIMBURSEMENTS", "Utility Reimbursements"),
    ("RUBS Income - Electric", "Utility Reimbursements"),
    ("LATE CHARGES", "Other Income"),
    ("NSF FEE", "Other Income"),
    ("APPLICATION FEES", "Other Income"),
    ("ADMINISTRATIVE FEE", "Other Income"),
    ("Pet Rent", "Other Income"),
    ("LAUNDRY INCOME", "Other Income"),
    ("VENDING INCOME", "Other Income"),
    ("CABLE INCOME", "Other Income"),
    ("Storage Income", "Other Income"),
    ("Parking Income", "Other Income"),
    ("Month-to-Month Premium", "Other Income"),
    ("Lease Termination Fee", "Other Income"),
    ("Other Income - Miscellaneous", "Other Income"),
    # -- expense -------------------------------------------------------------------
    ("MANAGEMENT FEE", "Management Fee"),
    ("Property Management Fee", "Management Fee"),
    ("PAYROLL - ONSITE", "Payroll"),
    ("PAYROLL TAXES", "Payroll"),
    ("Salaries & Wages", "Payroll"),
    ("Employee Benefits", "Payroll"),
    ("5015 WORKERS COMPENSATION", "Payroll"),
    ("REAL ESTATE TAXES", "Property Taxes"),
    ("Property Tax - County", "Property Taxes"),
    ("Ad Valorem Tax", "Property Taxes"),
    ("PROPERTY INSURANCE", "Insurance"),
    ("General Liability Insurance", "Insurance"),
    ("LEGAL FEES - EVICTION", "Legal and Professional"),
    ("Professional Services", "Legal and Professional"),
    ("Accounting & Audit", "Legal and Professional"),
    ("ADVERTISING & MARKETING", "Leasing and Marketing"),
    ("Locator Fees", "Leasing and Marketing"),
    ("Resident Retention", "Leasing and Marketing"),
    ("LANDSCAPING CONTRACT", "Landscaping"),
    ("Grounds Maintenance", "Landscaping"),
    ("Lawn & Irrigation", "Landscaping"),
    ("R & M - HVAC", "Repairs and Maintenance"),
    ("R&M PLUMBING", "Repairs and Maintenance"),
    ("5100-000 REPAIRS AND MAINTENANCE", "Repairs and Maintenance"),
    ("PEST CONTROL", "Repairs and Maintenance"),
    ("Contract Services - Janitorial", "Repairs and Maintenance"),
    ("Pool Maintenance", "Repairs and Maintenance"),
    ("Elevator Contract", "Repairs and Maintenance"),
    ("UNIT TURNS", "Unit Turns"),
    ("Make Ready - Paint", "Unit Turns"),
    ("MAKE-READY SUPPLIES", "Unit Turns"),
    ("Turnover Carpet Replacement", "Unit Turns"),
    ("ELECTRIC - COMMON AREA", "Utilities"),
    ("Water & Sewer", "Utilities"),
    ("NATURAL GAS", "Utilities"),
    ("TRASH REMOVAL", "Utilities"),
    ("Waste Disposal", "Utilities"),
    ("OFFICE SUPPLIES", "General and Administrative"),
    ("Bank Charges", "General and Administrative"),
    ("Dues & Subscriptions", "General and Administrative"),
    ("6900 LICENSES AND PERMITS", "General and Administrative"),
    ("Telephone & Internet - Office", "General and Administrative"),
)

#: Labels that are a statement's own running totals. Recognised, never categorised.
STRUCTURAL: tuple[str, ...] = (
    "TOTAL REVENUE",
    "Total Income",
    "TOTAL OPERATING EXPENSES",
    "Total Expenses",
    "NET OPERATING INCOME",
    "NOI",
    "NET RENTAL INCOME",
    "Total Payroll",
    "SUBTOTAL - CONTROLLABLE",
    "TOTAL UTILITIES",
)

#: Spend that sits below the operating line. Folding a capital item into operating
#: expenses overstates opex and understates NOI, which is the most consequential mistake
#: this mapping can make — so these are excluded rather than guessed at. An asset
#: management fee is a partnership-level charge and is NOT the property management fee.
BELOW_THE_LINE_LABELS: tuple[str, ...] = (
    "CAPITAL IMPROVEMENTS",
    "Replacement Reserves",
    "Asset Management Fees",
    "DEPRECIATION",
    "Amortization of Loan Costs",
    "INTEREST EXPENSE",
    "Debt Service",
    "Franchise Tax",
)

#: Labels the layer must NOT guess at. Each is a real shape that carries no reliable
#: signal, and each one is cheaper to send to the model than to get wrong.
MUST_NOT_GUESS: tuple[str, ...] = (
    "MISCELLANEOUS",
    "5555-000",
    "Other",
    "ADJUSTMENT",
    "Reclass",
    "SEE ATTACHED SCHEDULE",
    "",
)

#: The near-misses. Each pair is a rule that would fire on the wrong label if the rules
#: were unordered or the keywords were looser.
NEAR_MISSES: tuple[tuple[str, str, str], ...] = (
    ("PAYROLL TAXES", "Payroll", "Property Taxes"),
    ("MANAGEMENT FEE", "Management Fee", "General and Administrative"),
    ("VALET TRASH REIMBURSEMENTS", "Utility Reimbursements", "Utilities"),
    ("UTILITIES RECOVERY", "Utility Reimbursements", "Utilities"),
    ("Grounds Maintenance", "Landscaping", "Repairs and Maintenance"),
    ("Make Ready - Paint", "Unit Turns", "Repairs and Maintenance"),
    ("Lease Termination Fee", "Other Income", "Management Fee"),
    ("Legal Fees - Eviction", "Legal and Professional", "Other Income"),
    # Round 2, item 17 moved this one, deliberately. A claim reimbursement is money the
    # insurer PAYS US, and the same line is written "Insurance Claim Receipts" by the next
    # vendor — which this file already expects to be Other Income. It used to land on
    # Insurance only because the old income-marker list happened not to carry the singular
    # "reimbursement", so two spellings of one line went to two different sides of the
    # P&L. What it must not be is a UTILITY reimbursement, and that is still asserted.
    ("Insurance Claim Reimbursement", "Other Income", "Utility Reimbursements"),
)


def main() -> int:
    c = Checker("test_account_rules")

    c.section("normalisation is case-, punctuation- and prefix-insensitive")
    c.equal(
        "a GL prefix is stripped",
        schema.normalize_account_text("5100-000 REPAIRS AND MAINTENANCE"),
        "repairs and maintenance",
    )
    c.equal(
        "ampersands and spacing collapse",
        schema.normalize_account_text("R & M  -  HVAC"),
        "r m hvac",
    )
    c.equal(
        "slashes become separators",
        schema.normalize_account_text("LOSS/GAIN TO LEASE"),
        "loss gain to lease",
    )
    c.equal("empty stays empty", schema.normalize_account_text(""), "")
    c.equal("None stays empty", schema.normalize_account_text(None), "")

    c.section("structural rows are recognised, not categorised")
    for label in STRUCTURAL:
        result = schema.structural_row(label)
        c.ok(f"{label!r} is structural", result is not None, repr(result))
        c.ok(
            f"{label!r} is not given a category",
            schema.classify_account_label(label) is None,
            repr(schema.classify_account_label(label)),
        )
    c.equal("the three named subtotals are named",
            schema.structural_row("TOTAL REVENUE"), "Total Revenue")
    c.equal("total expenses", schema.structural_row("Total Expenses"),
            "Total Operating Expenses")
    c.equal("noi", schema.structural_row("NOI"), "Net Operating Income")
    c.equal("an unnamed running total is just 'subtotal'",
            schema.structural_row("NET RENTAL INCOME"), "subtotal")
    c.ok("an ordinary line is not structural",
         schema.structural_row("PEST CONTROL") is None)

    c.section("labels map onto the vendored vocabulary")
    wrong: list[str] = []
    for label, expected in EXPECTED:
        got = schema.classify_account_label(label)
        if not c.equal(f"{label!r}", got, expected):
            wrong.append(label)
    c.ok("every mapped category is actually vendored",
         not schema.assert_categories([e for _l, e in EXPECTED]),
         repr(schema.assert_categories([e for _l, e in EXPECTED])))

    c.section("near misses land on the right side")
    for label, right, wrong_one in NEAR_MISSES:
        got = schema.classify_account_label(label)
        c.equal(f"{label!r} is {right}, not {wrong_one}", got, right)

    c.section("below-the-line spend is excluded, not categorised")
    for label in BELOW_THE_LINE_LABELS:
        c.ok(f"{label!r} is below the line", schema.below_the_line(label), label)
        c.ok(f"{label!r} gets no operating category",
             schema.classify_account_label(label) is None,
             repr(schema.classify_account_label(label)))

    c.section("a line that says it is income never lands in an expense category")
    for label, expected in (
        ("RENTER'S INSURANCE INCOME", "Other Income"),
        ("Insurance Claim Receipts", "Other Income"),
        ("COURT COST REIMBURSEMENT", "Other Income"),
        ("Utility Income", "Other Income"),
    ):
        c.equal(f"{label!r}", schema.classify_account_label(label), expected)
    c.equal("but the expense twin still maps",
            schema.classify_account_label("RENTER'S INSURANCE EXPENSE"), "Insurance")

    # -- review finding --------------------------------------------------------
    #
    # The rule above was written as "the label contains the word income", which is not the
    # same claim. A software licence for revenue management is an administrative expense;
    # marketing for income-restricted units is marketing. Both were being moved into the
    # revenue block, where a nonzero expense distorts NOI in the direction that flatters
    # the deal. The income sense has to be the HEAD of the label — what the line IS —
    # rather than a word that happens to appear in it.
    c.section("the income sense must be the head of the label, not a word inside it")
    for label, expected in (
        ("Revenue Management Software", "General and Administrative"),
        ("Income-Restricted Marketing", "Leasing and Marketing"),
        ("Management Fees", "Management Fee"),
        ("Income Restricted Payroll", "Payroll"),
        ("Revenue Cycle Audit", "Legal and Professional"),
    ):
        c.equal(f"{label!r} keeps its expense category", schema.classify_account_label(label),
                expected)
    for label in ("Revenue Management Software", "Income-Restricted Marketing",
                  "Management Fees"):
        c.ok(
            f"{label!r} is not booked as revenue",
            schema.classify_account_label(label) not in schema.T12_REVENUE_CATEGORIES,
            repr(schema.classify_account_label(label)),
        )
    c.section("while a head-of-label income word still wins")
    for label, expected in (
        ("RENTER'S INSURANCE INCOME", "Other Income"),
        ("COURT COST REIMBURSEMENT", "Other Income"),
        ("Utility Income", "Other Income"),
        ("Insurance Claim Receipts", "Other Income"),
        ("Ancillary Widget Revenue", "Other Income"),
        ("VALET TRASH REIMBURSEMENTS", "Utility Reimbursements"),
        ("UTILITIES RECOVERY", "Utility Reimbursements"),
    ):
        c.equal(f"{label!r}", schema.classify_account_label(label), expected)

    # -- review finding --------------------------------------------------------
    #
    # `below_the_line()` matched bare substrings, so any label carrying the word "capital"
    # was dropped out of operating expenses — including a management fee payable to a firm
    # whose name is a market name. Dropping a real operating expense understates opex and
    # overstates NOI, the same direction of error as item 17.
    c.section("below-the-line triggers are phrases where the word is ambiguous")
    for label, expected in (
        ("Capital City Property Management Fee", "Management Fee"),
        ("Capitol Hill Landscaping", "Landscaping"),
        ("Capital One Bank Charges", "General and Administrative"),
    ):
        c.ok(f"{label!r} is NOT below the line", not schema.below_the_line(label), label)
        c.equal(f"{label!r} keeps its operating category",
                schema.classify_account_label(label), expected)
    for label in (
        "Capital Expenditures", "CAPEX - ROOFING", "Capital Improvements",
        "Capital Reserves", "Capital Replacement - HVAC", "Capital Repairs",
        "Capital Projects", "Asset Management Fees", "Replacement Reserves",
        "DEPRECIATION", "INTEREST EXPENSE", "Debt Service",
    ):
        c.ok(f"{label!r} is still below the line", schema.below_the_line(label), label)

    c.section("no guessing where there is no signal")
    for label in MUST_NOT_GUESS:
        got = schema.classify_account_label(label)
        c.ok(f"{label!r} is left to the model", got is None, repr(got))

    c.section("the layer is additive — normalize_category is untouched")
    c.equal("an exact vendored label still normalizes",
            schema.normalize_category("Payroll"), "Payroll")
    c.equal("a known alias still normalizes",
            schema.normalize_category("repairs & maintenance"), "Repairs and Maintenance")
    c.ok(
        "an unknown label STILL fails assert_categories",
        schema.assert_categories(["PEST CONTROL"]) == ["PEST CONTROL"],
        repr(schema.assert_categories(["PEST CONTROL"])),
    )
    c.ok(
        "so the keyword layer cannot loosen the I3 drift check",
        schema.normalize_category("MARKET RENT") == "MARKET RENT",
        schema.normalize_category("MARKET RENT"),
    )

    c.section("the whole synthetic set, as a hit rate")
    labels = ([lab for lab, _ in EXPECTED] + list(STRUCTURAL)
              + list(BELOW_THE_LINE_LABELS) + list(MUST_NOT_GUESS))
    decided = sum(
        1 for lab in labels
        if schema.classify_account_label(lab)
        or schema.structural_row(lab)
        or schema.below_the_line(lab)
    )
    c.ok(
        "the layer reaches a decision on the great majority of realistic shapes",
        decided >= len(labels) - len(MUST_NOT_GUESS),
        f"{decided} of {len(labels)}",
    )
    print(f"    synthetic hit rate: {decided}/{len(labels)} "
          f"({decided / len(labels) * 100:.0f}%), "
          f"{len(MUST_NOT_GUESS)} deliberately left to the model")

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
