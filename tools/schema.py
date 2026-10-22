"""Interchange schema for the sample-output pipeline.

One `deal_outputs.json` sits between whatever produced a deal's numbers and everything
public: the xlsx writers, the HTML previews, and the model-ready manifest. Nothing
downstream reads anything else, and no writer holds a deal value.

Three things live here:

* the section/key spec plus :func:`validate`, which returns a list of human-readable
  problems (empty list means valid);
* the T-12 category vocabulary, vendored as this module's own constants so a change in
  any upstream tool surfaces as a loud membership failure rather than a silent relabel;
* :data:`FIELD_INVENTORY`, the row source for the manifest workbook. The manifest is
  generated from it, never hand-typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

import closing_costs as _closing
import provenance as _provenance

# --------------------------------------------------------------------------------------
# Public naming
# --------------------------------------------------------------------------------------

TOOLKIT_NAME = "AI Underwriting Pipeline — Sample Outputs"

#: Canonical download filenames. One definition, so the writers, the tests, the preview
#: renderer, and the publish step cannot drift apart on what a file is called.
OUTPUT_FILES: dict[str, str] = {
    "pack": "sample-underwriting-pack.xlsx",
    "deal_summary": "sample-deal-summary.xlsx",
    "t12_analysis": "sample-t12-analysis.xlsx",
    "rent_roll": "sample-rent-roll-analysis.xlsx",
    "manifest": "sample-model-ready-outputs.xlsx",
}

#: Preview fragment names, one per tab shown on the demo page.
PREVIEW_FILES: tuple[str, ...] = (
    "summary.html",
    "comps.html",
    "property-tax.html",
    "demand.html",
    "supply.html",
    "t12-data.html",
    "t12-pnl.html",
    "t12-checks-pricing.html",
    "rent-roll.html",
    "unit-mix.html",
    "rent-roll-checks.html",
    "notes.html",
    "manifest.html",
    "audit-trail.html",
)

# --------------------------------------------------------------------------------------
# T-12 category vocabulary (I3)
#
# Generic commercial-real-estate line-item vocabulary. Order is load-bearing: the P&L
# rollup is written in exactly this sequence, and Total Revenue sums from Gross Scheduled
# Rent down (never from Gross Potential Rent, which double-counts loss-to-lease).
# --------------------------------------------------------------------------------------

T12_REVENUE_CATEGORIES: tuple[str, ...] = (
    "Gross Potential Rent",
    # The skill's contract spelling, spaces around the slash included. Pinned by
    # test_category_contract.py, which also parses the skill's own SKILL.md.
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

T12_EXPENSE_CATEGORIES: tuple[str, ...] = (
    # NOT "G&A". These strings are a JOIN KEY: the skill writes them into a Category
    # column and sums with SUMIF on the text, so a shortened label matches nothing and
    # that category's money silently leaves the total. Shipped drifted for nineteen
    # rounds and found only by extracting one deal twice and diffing.
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

#: Categories a *source* line item may be tagged with. The derived subtotals are computed
#: by formula on the rollup tab, so a raw line should not claim them -- except Gross
#: Scheduled Rent, which many statements do print as its own line. That one is allowed;
#: the rollup's fallback formula handles either case. A source line that *is* the
#: statement's own "Total Revenue" subtotal stays uncategorized and visible, and its value
#: goes into ``t12.source_subtotals`` where the Error Check block cross-ties against it.
ASSIGNABLE_CATEGORIES: tuple[str, ...] = tuple(
    c for c in (*T12_REVENUE_CATEGORIES, *T12_EXPENSE_CATEGORIES) if c != "Total Revenue"
)

#: Upstream wordings that mean one of the vendored categories. The vendored list above is
#: the generic vocabulary this project publishes; real statements and real tooling word
#: several of these differently, and normalizing a *known* synonym is not the same as
#: tolerating drift. An unrecognized label still fails :func:`assert_categories` loudly --
#: which is the point of I3.
CATEGORY_ALIASES: dict[str, str] = {
    # The two labels this project had as canonical until 2026-09-01 are now SYNONYMS, so a
    # config or fixture written against the old spelling still normalizes rather than
    # failing membership.
    "g&a": "General and Administrative",
    "g & a": "General and Administrative",
    "general & administrative": "General and Administrative",
    "gain/(loss) to lease": "Gain / (Loss) to Lease",
    "gain /(loss) to lease": "Gain / (Loss) to Lease",
    "loss to lease": "Gain / (Loss) to Lease",
    "gross potential rent": "Gross Potential Rent",
    "total operating expenses": "Total Operating Expenses",
    "total expenses": "Total Operating Expenses",
    "net operating income": "Net Operating Income",
    "noi": "Net Operating Income",
    "repairs & maintenance": "Repairs and Maintenance",
    "legal & professional": "Legal and Professional",
    "leasing & marketing": "Leasing and Marketing",
}

# --------------------------------------------------------------------------------------
# Deterministic account-label rules
#
# CATEGORY_ALIASES above is an exact-match table of *known wordings*, and
# `normalize_category` / `assert_categories` stay exactly that strict: they are the I3
# drift check, and loosening them would turn an upstream relabel from a loud failure into
# a silent one.
#
# This is a separate, additive layer. Real charts of accounts write
# `5100-000 R & M - HVAC`, `PAYROLL TAXES`, `Landscape/Grounds Contract` — generic
# multifamily vocabulary in whatever shape the accounting system emits. Recognising those
# by keyword costs nothing per run and never drifts, and it exists so the model is asked
# only about the genuinely ambiguous remainder.
#
# Two design rules, both learned the expensive way:
#
#   * **A wrong category is worse than no category.** An unmapped label goes to the model
#     and is usually right; a mis-mapped label silently moves money between lines of a P&L
#     someone underwrites from. Where a label carries no reliable signal this returns None.
#   * **Order is load-bearing.** `PAYROLL TAXES` is Payroll, not Property Taxes;
#     `MANAGEMENT FEE` is its own category, not General and Administrative; a
#     reimbursement is revenue even though it names a utility. The specific rule has to be
#     reached before the general one, so this is a list and not a dict.
# --------------------------------------------------------------------------------------

_GL_PREFIX = re.compile(r"^[\d\-\.]{2,}\s+")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_account_text(label: str | None) -> str:
    """Lowercase, GL-prefix-stripped, punctuation-collapsed text for keyword matching."""
    if not label:
        return ""
    text = str(label).strip().lower()
    text = _GL_PREFIX.sub("", text)
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


#: Running totals a statement prints itself. Recognised so they are never double-counted
#: as source lines. The first three map to the named subtotals the Error Check block
#: cross-ties against; anything else that reads as a running total is just "subtotal".
_NAMED_SUBTOTALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Net Operating Income", ("net operating income", "noi")),
    # "TOTAL Operating Income" is one accounting system's Report1 revenue total; its NOI row reads
    # "TOTAL Net Operating Income", which the NOI entry above claims first.
    ("Total Revenue", ("total revenue", "total income", "total gross income",
                       "total operating income", "effective gross income")),
    (
        "Total Operating Expenses",
        ("total operating expenses", "total expenses", "total operating expense",
         "total expense", "total opex", "total controllable"),
    ),
)

_SUBTOTAL_MARKERS: tuple[str, ...] = ("total", "subtotal", "net rental income")


def structural_row(label: str | None) -> str | None:
    """``"Total Revenue"`` / ``"Total Operating Expenses"`` / ``"Net Operating Income"``
    for the three named subtotals, ``"subtotal"`` for any other running total, else None.
    """
    text = normalize_account_text(label)
    if not text:
        return None
    for name, needles in _NAMED_SUBTOTALS:
        if any(text == n or text.startswith(n + " ") or text.endswith(" " + n) for n in needles):
            return name
    words = text.split()
    if words and (words[0] in ("total", "subtotal") or text in _SUBTOTAL_MARKERS):
        return "subtotal"
    if "net rental income" in text or "net rental revenue" in text:
        return "subtotal"
    # "Controllable Net Oper Income": a net-operating-income subtotal under an abbreviation
    # the NOI entry does not claim, read as an expense line.
    if re.search(r"\bnet oper\b", text) and "income" in words:
        return "subtotal"
    return None


#: Statistics a statement prints among its accounts: occupancy, averages, unit counts.
_STATISTIC = re.compile(
    r"^(average|avg)\b|\b(physical|economic|financial) occupancy\b|\bunits occupied\b|"
    r"\boccupancy (percent|pct|rate)\b|^(number|no|#) of units\b"
)


def statistic_row(label: str | None) -> bool:
    """True for a statistic printed among the accounts ("Physical Occupancy", "Average In
    Place Rent", "Average # of Units Occupied") — a measure, not an amount of money, and
    never an operating line. "Occupancy Tax" is an account."""
    text = normalize_account_text(label)
    return bool(text) and bool(_STATISTIC.search(text))


#: (category, any-of keywords, none-of keywords). Evaluated in order; first hit wins.
ACCOUNT_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # -- revenue, most specific first ---------------------------------------------
    ("Gain / (Loss) to Lease", ("loss to lease", "gain to lease", "loss gain to lease",
                                "gain loss to lease", "lease loss gain"), ()),
    ("Utility Reimbursements",
     ("reimburs", "recovery", "recover", "rubs"),
     ("insurance", "damage", "legal", "tenant damage")),
    ("Non-Revenue Units",
     ("non revenue", "nonrevenue", "employee unit", "model unit", "staff unit",
      "management office", "down unit", "admin unit", "models", "model units"),
     ()),
    ("Delinquency", ("bad debt", "delinquen", "write off", "writeoff", "collection loss",
                     "uncollect"), ()),
    ("Vacancy", ("vacancy", "vacant"), ()),
    ("Concessions", ("concession",), ()),
    ("Gross Potential Rent", ("gross potential", "potential rent", "market rent",
                              "scheduled rent", "gross rent"), ()),
    # -- expense, before the generic revenue "fee"/"income" rule -------------------
    ("Management Fee", ("management fee", "mgmt fee", "management fees"), ()),
    ("Payroll", ("payroll", "salar", "wages", "benefit", "bonus", "workers comp",
                 "personnel", "onsite staff"), ()),
    ("Property Taxes", ("real estate tax", "property tax", "ad valorem", "re taxes",
                        "real property tax"), ()),
    ("Insurance", ("insurance", "liability"), ()),
    ("Legal and Professional", ("legal", "professional", "attorney", "audit",
                                "accounting", "eviction"), ()),
    ("Leasing and Marketing", ("marketing", "advertis", "leasing", "locator", "referral",
                               "resident retention", "promotion"), ()),
    ("Landscaping", ("landscap", "grounds", "lawn", "irrigation"), ()),
    ("Unit Turns", ("unit turn", "turnover", "turns", "make ready", "makeready",
                    "unit prep", "carpet replace"), ()),
    ("Repairs and Maintenance",
     ("repair", "maintenance", "r m ", "hvac", "plumbing", "pest", "pool", "elevator",
      "janitorial", "cleaning", "snow removal", "contract services", "security",
      "fire safety"),
     ()),
    ("Utilities", ("utilit", "electric", "water", "sewer", "natural gas", "trash",
                   "waste", "refuse"), ()),
    # -- generic revenue, last: anything still naming a fee or income is Other Income
    ("Other Income",
     ("late charge", "late fee", "nsf", "application fee", "administrative fee",
      "admin fee", "pet rent", "pet fee", "parking", "storage", "laundry", "vending",
      "cable income", "amenity fee", "damage", "court cost", "month to month",
      "termination fee", "transfer fee", "other income", "miscellaneous income",
      "forfeited deposit"),
     ()),
    ("Revenue Adjustments", ("revenue adjustment", "income adjustment"), ()),
    ("Expense Adjustments", ("expense adjustment",), ()),
    ("General and Administrative",
     ("administrative", "office", "bank charge", "dues", "subscription", "postage",
      "travel", "training", "license", "permit", "telephone", "internet", "computer",
      "software", "general and admin"),
     ()),
)


#: A line that says it is income cannot be an operating expense, whatever else it says.
#: "RENTER'S INSURANCE INCOME" matched the Insurance rule and was booked as an expense;
#: "COURT COST REIMBURSEMENT" matched the utility-reimbursement rule and was booked
#: against utilities. Both were found by diffing the upload path against the raw one.
#:
#: **The income sense has to be the HEAD of the label**, not a word somewhere inside it.
#: The first cut of this rule fired on any occurrence, which turned
#: "Revenue Management Software" into revenue (it is a G&A licence) and
#: "Income-Restricted Marketing" into revenue (it is marketing spend) — a nonzero expense
#: moved into the revenue block distorts NOI in the direction that flatters the deal. The
#: final token is what the line IS; everything before it qualifies it.
_INCOME_HEAD_TOKENS: frozenset[str] = frozenset(
    {
        "income", "revenue", "revenues", "reimbursement", "reimbursements",
        "recovery", "recoveries", "receipts",
    }
)


def _income_headed(text: str) -> bool:
    """Does this normalised label END in a word that names it as income?"""
    words = text.split()
    return bool(words) and words[-1] in _INCOME_HEAD_TOKENS

#: A reimbursement is a UTILITY reimbursement only when it names a utility. Anything else
#: a resident pays back is other income.
_UTILITY_WORDS: tuple[str, ...] = (
    "utilit", "water", "sewer", "electric", "gas", "trash", "waste", "refuse", "rubs",
    "valet", "cam", "common area",
)

#: Vocabulary for spend that sits below the operating line. A capital item folded into
#: operating expenses overstates opex and understates NOI, which is the single most
#: consequential mistake this mapping can make — so these are excluded rather than
#: guessed at.
#:
#: Split in two because the error runs both ways. Bare ``capital`` matched
#: "Capital City Property Management Fee" and dropped a real operating expense, which
#: overstates NOI exactly as folding capex into opex understates it. A word that is only
#: capital-flavoured in context has to appear as a PHRASE; only the words that mean one
#: thing on a property P&L stand alone.
_BELOW_THE_LINE_PHRASES: tuple[str, ...] = (
    "capital expenditure", "capital expenditures", "capital improvement",
    "capital improvements", "capital reserve", "capital reserves",
    "capital replacement", "capital replacements", "capital repair", "capital repairs",
    "capital project", "capital projects", "capital item", "capital items",
    "capital contribution", "capital contributions", "capital lease", "capital cost",
    "capital costs", "capital budget", "capital account", "capital call",
    "replacement reserve", "replacement reserves", "reserve for replacement",
    "interest expense", "debt service", "asset management fee", "asset management fees",
    "franchise tax", "income tax", "non operating", "below the line",
)

#: Single tokens that need no phrase around them: on a property income statement none of
#: these is anything but a below-the-line item. Matched whole-word, never as a substring.
_BELOW_THE_LINE_WORDS: frozenset[str] = frozenset(
    {
        "capex", "depreciation", "amortization", "amortisation", "principal",
        "escrow", "partnership", "acquisition", "disposition",
    }
)


def below_the_line(label: str | None) -> bool:
    """Is this line capital or otherwise outside operating expenses?"""
    text = normalize_account_text(label)
    if not text:
        return False
    padded = f" {text} "
    if any(f" {phrase} " in padded for phrase in _BELOW_THE_LINE_PHRASES):
        return True
    return bool(_BELOW_THE_LINE_WORDS.intersection(text.split()))


def classify_account_label(label: str | None) -> str | None:
    """A vendored category for an account label, or None when there is no clear signal.

    Returns None for structural rows and for below-the-line spend — neither is an
    operating source line, and :func:`structural_row` / :func:`below_the_line` are the
    functions that answer those questions.
    """
    text = normalize_account_text(label)
    if not text or structural_row(label) or below_the_line(label):
        return None
    padded = f" {text} "
    is_income = _income_headed(text)
    for category, positives, negatives in ACCOUNT_KEYWORD_RULES:
        if any(n in padded for n in negatives):
            continue
        if not any(p in padded for p in positives):
            continue
        # A line that names itself income never lands in an expense category.
        if is_income and category in T12_EXPENSE_CATEGORIES:
            return "Other Income"
        if category == "Utility Reimbursements" and not any(
            word in padded for word in _UTILITY_WORDS
        ):
            return "Other Income"
        return category
    if is_income:
        return "Other Income"
    return None


#: The three Top-4-plus-Rest breakout tables on the rollup tab.
BREAKOUT_CATEGORIES: tuple[str, ...] = (
    "Other Income",
    "Utility Reimbursements",
    "Utilities",
)

#: Rollup row order: revenue block, expense block, then the two totals and NOI.
PNL_TOTAL_OPEX_LABEL = "Total Operating Expenses"
PNL_NOI_LABEL = "Net Operating Income"

LEASE_TYPES: tuple[str, ...] = ("New", "Renewal")
TRADE_OUT_COHORTS: tuple[str, ...] = ("New", "Renewal", "First generation")
SUPPLY_STAGES: tuple[str, ...] = (
    "Entitlement",
    "Permitted",
    "Under construction",
    "Delivered (24 mo)",
)
DEMAND_RINGS: tuple[str, ...] = ("1 mi", "3 mi", "5 mi", "10 mi", "MSA")

#: The only comp source this project will accept. A licensed feed cannot be represented.
COMP_SOURCE = "own weekly scrape"

#: Sentinel written into a cell whose value is genuinely unknown. A visible hole beats a
#: plausible-wrong number.
MISSING = "MISSING"

#: A ZIP entry's timestamp field can only represent these years, and the workbooks pin
#: their archive timestamps from ``meta.generated_on``.
ZIP_MIN_YEAR = 1980
ZIP_MAX_YEAR = 2107

# --------------------------------------------------------------------------------------
# Section spec
# --------------------------------------------------------------------------------------

#: Blocks a deal MAY carry. The upload path writes them; the published sample does not.
OPTIONAL_SECTIONS: tuple[str, ...] = ("om_comps",)

SECTIONS: tuple[str, ...] = (
    "meta",
    "summary",
    "t12",
    "rent_roll",
    "trade_out",
    "pricing",
    "comps",
    "demand",
    "supply",
    "closing_costs",
    "provenance",
)

_NUM = (int, float)

# key -> (required, allowed python types). `None` is always allowed for an optional key
# that is present but unknown, except where noted in validate().
META_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "toolkit_name": (True, (str,)),
    "deal_name": (True, (str,)),
    "deal_slug": (True, (str,)),
    "generated_on": (True, (str,)),
    "as_of_date": (True, (str,)),
    "illustrative_pricing": (True, (bool,)),
    "pricing_note": (True, (str,)),
    "synthetic": (True, (bool,)),
    "provenance": (True, (list,)),
    "excluded_sources": (True, (list,)),
    # Plain-language judgment calls a reader is entitled to see: what is illustrative,
    # which choices were made and why, and what a number does NOT mean. Rendered as its
    # own tab and preview section. A judgment that only lives in a commit message is a
    # judgment the reader cannot audit.
    "notes": (True, (list,)),
    # What the RUN did — model calls, tokens, seconds, dollars, the model ids — so the audit
    # trail can show HOW the numbers were produced, not only WHERE they came from. Optional:
    # the prebuilt sample carries a run block saying it made no model calls.
    "run": (False, (dict,)),
    # What "synthetic" means for THIS set, in one sentence the writers and the page reuse.
    # The published sample's figures are fabricated but its LOCATION is real (2026-09-10),
    # so the demand, supply and comp tabs are genuine pulls; the old "no real property is
    # described" line is only right when this key is absent.
    "synthetic_note": (False, (str,)),
}


def synthetic_note(deal: dict | None) -> str:
    """The sentence that says what is fabricated and what is real about a synthetic set."""
    meta = (deal or {}).get("meta") or {}
    note = meta.get("synthetic_note")
    if isinstance(note, str) and note.strip():
        return note.strip()
    return "Every figure is fabricated for demonstration. No real property is described."

#: Values the OPERATOR'S pipeline produces that the public demo deliberately does not — each
#: with the reason a reader can act on. The demo is a watered-down copy of the internal
#: pipeline on purpose (2026-09-10): a licensed source, a portal login or a web lookup is
#: not something a public page should reach. A value at one of these paths that reads
#: MISSING is labelled "internal pipeline only" with the reason, on every surface, instead
#: of reading as a hole the pipeline failed to fill.
#:
#: (path prefix, short label, reason). Prefix match on the dotted value path.
INTERNAL_ONLY_SOURCE = _provenance.INTERNAL_ONLY_SOURCE
_LICENSED_UNITS_REASON = (
    "A community's own unit count comes from a licensed data export, so the public demo holds "
    "it back; where a public record states a count (a county property record, a Florida DBPR "
    "apartment licence, a City of Austin permit), that count is shown with its source and as-of.")
INTERNAL_ONLY: tuple[tuple[str, str, str], ...] = (
    ("summary.school_",
     "School names and ratings",
     "The internal pipeline looks up the attendance-zone elementary, middle and high school "
     "and their public ratings; the demo reads a school name only where the memorandum "
     "states one and never rates it."),
    ("summary.fair_market_value",
     "Fair market value",
     "The internal pipeline pulls a market value by web lookup; the demo does not reach the web."),
    ("summary.tax_parcel_id",
     "Assessor parcel and bill",
     "The internal pipeline reads the parcel, tax district, assessed value, exemption and billed "
     "amount off the county assessor's portal; the demo shows the county's published rate only."),
    ("summary.tax_area_code", "Assessor parcel and bill", "See parcel: read off the county assessor's portal by the internal pipeline only."),
    ("summary.tax_billed_amount", "Assessor parcel and bill", "See parcel: read off the county assessor's portal by the internal pipeline only."),
    ("summary.tax_billed_year", "Assessor parcel and bill", "See parcel: read off the county assessor's portal by the internal pipeline only."),
    ("summary.assessed_value_basis", "Assessor parcel and bill", "See parcel: read off the county assessor's portal by the internal pipeline only."),
    ("summary.tax_exemption", "Assessor parcel and bill", "See parcel: read off the county assessor's portal by the internal pipeline only."),
    ("summary.address_provenance",
     "Address resolution",
     "The internal pipeline geocodes the address and records how it resolved; the demo takes "
     "the address as the memorandum states it."),
    ("trade_out.",
     "Lease trade-out",
     "The internal pipeline reads a lease trade-out report (new versus renewal rents); the "
     "demo does not ask for one."),
    # A ROW column (``<list>[].<column>``): the value on every row of that list. The rent
    # tracker's community list was seeded from licensed vendor exports, and a tracked
    # community's unit count is that export's own figure. Since
    # 2026-10-06 a row column may have a PUBLIC stand-in (``PUBLIC_SUBSTITUTES``): the
    # community's public-record count, shown with its source where one exists. Year built
    # on the same rows stays public; supply-project units (public permits) and the units a
    # memorandum states for its own comparables are other paths and are not touched.
    ("comps.nearby[].units", "Licensed community unit counts", _LICENSED_UNITS_REASON),
    ("comps.rows[].units", "Licensed community unit counts", _LICENSED_UNITS_REASON),
)

#: Registry row columns that have a PUBLIC stand-in: (value column,
#: source column, as-of column) on the same row. In public mode the withheld column shows
#: the stand-in's value, and ``<column>_source`` names its source and as-of; a row with no
#: stand-in reads MISSING with ``NO_PUBLIC_RECORD_LABEL``. The licensed figure never shows.
PUBLIC_RECORD_COLUMNS: tuple[str, str, str] = (
    "public_record_units", "public_record_units_source", "public_record_as_of")
PUBLIC_SUBSTITUTES: dict[str, tuple[str, str, str]] = {
    "comps.nearby[].units": PUBLIC_RECORD_COLUMNS,
    "comps.rows[].units": PUBLIC_RECORD_COLUMNS,
}
NO_PUBLIC_RECORD_LABEL = "No public record; the licensed count is held back"


def public_record_label(source: str | None, as_of: str | None) -> str | None:
    """A public-record value's provenance as every surface prints it:
    "Maricopa County, AZ property record, as of 2026-10-05". None without a source."""
    if not source:
        return None
    return f"{source}, as of {as_of}" if as_of else str(source)


def public_demo_status(path: str | None) -> str:
    """What the public demo does with the value at ``path``, as the manifest's public-demo
    column says it: "yes"; "Internal pipeline only — <reason>"; or, for a registry row
    column with a public stand-in, "Public record where one exists — <reason>"."""
    found = internal_only(path)
    if not found:
        return "yes"
    if public_substitute(path):
        return f"Public record where one exists — {found[1]}"
    return f"Internal pipeline only — {found[1]}"


def with_source(deal: dict | None, path: str | None, example: str) -> str:
    """A manifest example for a registry row column with a public stand-in, with the row's
    own ``<column>_source`` beside it: "306 — Maricopa County, AZ property record, as of
    2026-10-05", or "MISSING — No public record; the licensed count is held back"."""
    src = asserted_value(deal, f"{path}_source") if deal is not None and path else None
    return f"{example} — {src}" if isinstance(src, str) and src else example


def public_substitute(path: str | None) -> tuple[str, str, str] | None:
    """The public stand-in columns for a registry row column (any row index), or None."""
    if not path:
        return None
    return PUBLIC_SUBSTITUTES.get(_ROW_INDEX.sub("[]", path))

_ROW_INDEX = re.compile(r"\[\d+\]")
_ROW_COLUMN = re.compile(r"^(?P<rows>[\w.]+)\[\]\.(?P<column>\w+)$")


def internal_only(path: str | None) -> tuple[str, str] | None:
    """``(label, reason)`` when the value at ``path`` is produced by the internal pipeline
    only; None for anything the demo produces itself. A row index in ``path``
    (``comps.nearby[12].units``) matches the registry's row-column form (``comps.nearby[].units``)."""
    if not path:
        return None
    path = _ROW_INDEX.sub("[]", path)
    for prefix, label, reason in INTERNAL_ONLY:
        if path == prefix or path.startswith(prefix):
            return label, reason
    return None


def internal_only_row_columns() -> list[tuple[str, str, str, str]]:
    """``(registry path, rows path, column, reason)`` for every registry entry naming a row
    column — what :func:`policy.apply` withholds row by row."""
    out = []
    for prefix, _label, reason in INTERNAL_ONLY:
        m = _ROW_COLUMN.match(prefix)
        if m:
            out.append((prefix, m["rows"], m["column"], reason))
    return out


def internal_only_groups() -> list[tuple[str, str, list[str]]]:
    """``(label, reason, [paths])`` — the registry grouped by label, for one block that
    lists what the demo leaves to the internal pipeline."""
    groups: dict[str, tuple[str, list[str]]] = {}
    for prefix, label, reason in INTERNAL_ONLY:
        entry = groups.setdefault(label, (reason, []))
        entry[1].append(prefix)
    return [(label, reason, paths) for label, (reason, paths) in groups.items()]

NOTE_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "topic": (True, (str,)),
    "note": (True, (str,)),
    # Where this bears: a section name, so the reader can tie it to what they are looking
    # at. Free text on purpose.
    "applies_to": (True, (str, type(None))),
}

SUMMARY_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    # identification
    "deal_name": (True, (str,)),
    "street_address": (True, (str,)),
    "city": (True, (str,)),
    "state": (True, (str,)),
    "zip": (True, (str,)),
    "county": (True, (str,)),
    "msa": (True, (str,)),
    "address_provenance": (True, (str,)),
    # property
    "product_type": (True, (str,)),
    "structure": (True, (str,)),
    # ``MISSING`` where no source supplied one. A count is a DIVISOR — for every
    # per-unit figure in the output set — and the skeleton used to seed it with 0, which
    # is a number every consumer will happily divide by and print. The string is the same visible hole every other absent field shows;
    # ``_check_count_sentinel`` refuses any other string.
    "units_total": (True, (int, str)),
    "year_built": (True, (int, str)),
    "product_detail": (True, (str,)),
    "construction_status": (True, (str,)),
    "delivery_date": (True, (str,)),
    # parties
    "seller_entity": (True, (str,)),
    "listing_brokerage": (True, (str,)),
    "deal_status": (True, (str,)),
    # pricing narrative
    "pricing_guidance": (True, (str,)),
    # rents
    "in_place_rent_avg": (True, (*_NUM, str)),
    "market_rent_for_model": (True, (*_NUM, str)),
    "fair_market_value": (True, (*_NUM, str)),
    # taxes
    "tax_rate_as_billed": (True, (*_NUM, str)),
    "tax_rate_evidence": (True, (str,)),
    "assessed_value_basis": (True, (*_NUM, str)),
    "assessment_ratio": (True, (*_NUM, str)),
    "tax_divisor": (True, (*_NUM, str)),
    "tax_exemption": (True, (*_NUM, str)),
    # Evidence trail: the parcel the bill is actually issued against, the rate district,
    # what the basis IS in that state, and the county's own billed figure to check against.
    "tax_parcel_id": (True, (str,)),
    "tax_area_code": (True, (str,)),
    "tax_basis_label": (True, (str,)),
    "tax_basis_note": (True, (str,)),
    "tax_billed_amount": (True, (*_NUM, str)),
    "tax_billed_year": (True, (int, str)),
    # Upload-path annotations (optional; the published sets do not carry them). The
    # statement's own "Property Taxes" line over its twelve months — as billed in the
    # statement, never the county's bill — and the per-unit sanity band's verdict. Both
    # are MISSING-or-a-value when present; a consumer that finds neither key shows nothing.
    "tax_statement_amount": (False, (*_NUM, str, type(None))),
    "tax_per_unit_flag": (False, (str, type(None))),
    # The tax year of the rate in `tax_rate_as_billed` when it is the vendored table's. A
    # bill from another year is displayed but not tied out against it.
    "tax_rate_year": (False, (int, str, type(None))),
    # schools
    "school_elementary_name": (True, (str,)),
    "school_elementary_rating": (True, (*_NUM, str)),
    "school_middle_name": (True, (str,)),
    "school_middle_rating": (True, (*_NUM, str)),
    "school_high_name": (True, (str,)),
    "school_high_rating": (True, (*_NUM, str)),
    # free text
    "notes": (True, (str,)),
}

T12_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    # Optional, and the one-or-the-other rule below is what gives it meaning: a section
    # with no rows must say why, and a section with rows must not claim to be absent.
    "absent_reason": (False, (str, type(None))),
    "month_labels": (True, (list,)),
    # ``MISSING`` where nothing stated a count. The per-unit column divides by it.
    "units": (True, (int, str)),
    "rows": (True, (list,)),
    "source_subtotals": (True, (dict,)),
    "broker_proforma": (False, (dict, type(None))),
}

T12_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "label": (True, (str,)),
    "category": (True, (str, type(None))),
    "monthly": (True, (list,)),
}

#: What the SOURCE export states about itself. Tie-outs compare our numbers against
#: these; comparing our numbers against our own numbers proves nothing. ``None`` means the
#: export did not publish that total, which degrades the check to NOT CHECKED rather than
#: failing it.
ROLL_STATED_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "units_total": (True, (int, type(None))),
    "sqft": (True, (*_NUM, type(None))),
    "occupancy": (True, (*_NUM, type(None))),
    # Sheet name and column letters, so a per-unit readback can cite the exact cell.
    "sheet": (True, (str, type(None))),
    "rent_column": (True, (str, type(None))),
    "sqft_column": (True, (str, type(None))),
    # The source's own per-status unit table, verbatim. Physical occupancy is DERIVED
    # from it rather than read off any single bucket — see occupied_basis.
    "buckets": (False, (dict,)),
    "occupied_units": (False, (int, type(None))),
    "occupied_basis": (False, (str, type(None))),
    "market_rent": (False, (*_NUM, type(None))),
    "current_rent": (False, (*_NUM, type(None))),
    #: Units that appeared more than once in the export. Recorded so a downstream claim
    #: about the de-duplication can be verified against the units it names.
    "duplicate_units": (False, (list,)),
    #: Per duplicated unit: which row was kept, its lease status, and what the discarded
    #: row held. Without this no check can be sensitive to the de-duplication CHOICE.
    "duplicate_detail": (False, (list,)),
    "in_place_lease_status": (False, (str, type(None))),
}

RENT_ROLL_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "absent_reason": (False, (str, type(None))),
    "as_of": (True, (str,)),
    "lookback_months": (True, (int,)),
    "new_tenant_tolerance_days": (True, (int,)),
    "units": (True, (list,)),
    "stated": (True, (dict,)),
}

RENT_ROLL_UNIT_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    # Row this unit was read from in the source export, so the rent readback can name the
    # cell it is comparing against. None where the set is synthetic.
    "source_row": (True, (int, type(None))),
    # That cell's value EXACTLY as the file held it, before coercion. The readback compares
    # the normalized rent against this, which is what makes the check about something.
    "source_rent_raw": (True, (*_NUM, str, type(None))),
    # The source's own market rent for this unit. Tie-out input ONLY — never a rent in
    # any analysis, because market rent is not in-place rent.
    "market_rent": (False, (*_NUM, type(None))),
    "unit_id": (True, (str,)),
    "floor_plan": (True, (str,)),
    "beds": (True, (*_NUM,)),
    "baths": (True, (*_NUM,)),
    "sqft": (True, (*_NUM, type(None))),
    "status": (True, (str,)),
    "lease_start": (True, (str, type(None))),
    "move_in": (True, (str, type(None))),
    "lease_type": (True, (str, type(None))),
    "in_place_rent": (True, (*_NUM, type(None))),
}

TRADE_OUT_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "rows": (True, (list,)),
    "absent_reason": (True, (str, type(None))),
}

TRADE_OUT_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "cohort": (True, (str,)),
    "leases": (True, (int,)),
    "prior_rent": (True, (*_NUM,)),
    # Optional: the prior EFFECTIVE rent when the report stated a prior
    # concession. Absent or None = none stated, and compute.trade_out uses prior_rent.
    "prior_rent_effective": (False, (*_NUM, type(None))),
    "new_rent_gross": (True, (*_NUM,)),
    "new_rent_effective": (True, (*_NUM,)),
}

PRICING_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    # None when no document and no note stated a price. Every consumer already guards on
    # isinstance(_NUM), so MISSING flows through the cap-rate ladder instead of a
    # placeholder being invented to keep the arithmetic tidy.
    "price_per_unit": (True, (*_NUM, type(None))),
    # ``MISSING`` where no source supplied one — see summary.units_total. Never 0.
    "units_total": (True, (int, str)),
    # The per-unit price the MEMORANDUM states or implies (a stated total over the unit
    # count), kept beside the price actually used. On an upload the visitor's own price is
    # required and wins (2026-09-10); this is the seller's figure for comparison. None
    # when the document states no usable price. Optional: the published sample has no
    # memorandum.
    "document_price_per_unit": (False, (*_NUM, type(None))),
}

COMPS_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "as_of": (True, (str,)),
    "source": (True, (str,)),
    "subject_market_rent": (True, (*_NUM,)),
    "rows": (True, (list,)),
    # Coverage is data-driven. Where the operator's own scrape has no rents for the
    # submarket, the block says so instead of padding with something else's numbers.
    "absent_reason": (True, (str, type(None))),
    # What the scrape actually holds for this submarket, MEASURED — a comp block that
    # shows rents must be able to say how much of the market it looked at, and one that
    # shows none must say so on evidence rather than on an assumed status filter.
    "coverage_note": (True, (str, type(None))),
    # Tier 3, "Also nearby": identity only, no rent columns. Every tracked community
    # inside the radius that is NOT a detail row appears here with the reason why, so the
    # question "why isn't the property across the street in the comp set?" is answered on
    # the page rather than left to trust.
    "nearby": (True, (list,)),
    # The pre-tiering population's by-bed rents, measured at selection time. Optional,
    # because a compact comp input has no way to produce it — and a note that depends on it
    # must then simply not be written rather than guessing.
    "unrestricted": (False, (dict, type(None))),
    # The tiering parameters that produced this block, recorded so the display and any
    # later reader agree on what "top comps" meant.
    "tiering": (True, (dict,)),
}

#: The tier a comp row may carry. Tier 3 lives in `nearby`, never in `rows` — a detail row
#: with no rent columns would be a different kind of thing wearing the same shape.
DETAIL_TIERS: tuple[int, ...] = (1, 2)

COMPS_TIERING_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    # Every knob is nullable: a comp selection that never ran has no radius, no year
    # band and no counts, and a default here would read as a setting that was applied.
    "subject_product": (True, (str, type(None))),
    "subject_year_built": (True, (int, type(None))),
    "year_band": (True, (int, type(None))),
    "radius_mi": (True, (*_NUM, type(None))),
    "top_n": (True, (int, type(None))),
    "additional_n": (True, (int, type(None))),
    "vintage_footnote": (True, (str,)),
}

#: Where a row's ``units`` came from, and the community's PUBLIC-RECORD count. Optional, so a set built before the public market carried them still reads.
#: ``units_source`` names the figure ``units`` holds — the registry label for a licensed
#: count, "<record>, as of <date>" for a public-record one, ``NO_PUBLIC_RECORD_LABEL`` beside
#: a MISSING. ``public_record_*`` are the market service's own fields, carried as sent.
_UNITS_PROVENANCE_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "units_source": (False, (str, type(None))),
    "public_record_units": (False, (int, type(None))),
    "public_record_units_source": (False, (str, type(None))),
    "public_record_as_of": (False, (str, type(None))),
}

NEARBY_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "community": (True, (str,)),
    "address": (True, (str, type(None))),
    "distance_mi": (True, (*_NUM,)),
    # The tracker's own figure (vendor-derived), None where it has none — or, on a public
    # surface (``INTERNAL_ONLY`` "comps.nearby[].units"), the public-record count where one
    # exists and MISSING where none does. ``str`` is admitted for that sentinel only;
    # :func:`validate` refuses any other string.
    "units": (True, (int, str, type(None))),
    **_UNITS_PROVENANCE_KEYS,
    "product": (True, (str, type(None))),
    "year_built": (True, (int, type(None))),
    "year_built_source": (True, (str, type(None))),
    "tier": (True, (int,)),
    # Why this community is identity-only. Never blank: a nearby row with no reason is an
    # unexplained omission from the comp set.
    "reason": (True, (str,)),
    # Whether the scrape holds quotable rent for it. Load-bearing for the coverage
    # invariant — a rent-carrying community that is nearby-only must still be counted by
    # the coverage claim, or the claim and the data disagree.
    "has_rent_data": (True, (bool,)),
}

COMP_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "community": (True, (str,)),
    "distance_mi": (True, (*_NUM,)),
    # 1 = top comps, 2 = additional comps. See DETAIL_TIERS.
    "tier": (True, (int,)),
    # Vintage, where a year is known. The tracker has no year-built column — where a year
    # exists it came from a vendor note on the community record — so None is common and
    # means "nobody published one", never "new".
    "year_built": (True, (int, type(None))),
    "year_built_source": (True, (str, type(None))),
    # The community's unit count on every one of its plan rows: the licensed count
    # internally, the public-record count or MISSING on a public surface (``INTERNAL_ONLY``
    # "comps.rows[].units"). Optional: a set built before 2026-10-06 carries none.
    "units": (False, (int, str, type(None))),
    **_UNITS_PROVENANCE_KEYS,
    "floor_plan": (True, (str,)),
    "beds": (True, (*_NUM,)),
    # A scraped listing carries beds but rarely baths. Null means "the source did not
    # say" — writing 0 would assert a studio-with-no-bathroom that does not exist.
    "baths": (True, (*_NUM, type(None))),
    "sqft": (True, (*_NUM, type(None))),
    "asking_rent": (True, (*_NUM,)),
    "concession": (True, (*_NUM,)),
    "net_effective_rent": (True, (*_NUM,)),
    "occupancy": (True, (*_NUM, type(None))),
    "as_of": (True, (str,)),
    "source": (True, (str,)),
}

#: Comparables the memorandum ITSELF states. Optional at the top level so the
#: published sample — which has none — stays byte-identical; the upload path always writes
#: it. Number fields take a number or the MISSING string and nothing else: a formatted
#: "1,840" is a value nobody parsed, and a zero nobody stated is a claim.
OM_COMPS_SOURCE = "Stated in the offering memorandum"
OM_COMPS_RETAIL_NOTE = (
    "Retail for-sale comps are not part of this demo. Sale comparables shown here are the "
    "whole-community trades the memorandum itself cites."
)
#: The disclosure that rides every deal carrying the memorandum's comparables — on the
#: upload path (assemble.om_comps_block) and the static path (notes.for_deal) alike. It
#: promises no quote column: rent rows show theirs, sale rows do not.
OM_COMPS_APART_NOTE = (
    "Beneath the scraped set, the comparables the offering memorandum itself states are listed "
    "under their own heading. They are the seller's figures — each one verified against the "
    "memorandum's own text (rent rows show their quote) — not market data, and they are never "
    "mixed into the scraped tiers."
)
OM_COMPS_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "source": (True, (str,)),
    "rent": (True, (list,)),
    "sale": (True, (list,)),
    "retail_note": (True, (str,)),
    "absent_reason": (True, (str, type(None))),
}
#: A number the document states, or MISSING. ``str`` is admitted only for the sentinel;
#: :func:`validate` refuses any other string.
_NUM_OR_MISSING = (*_NUM, str)
OM_RENT_COMP_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "community": (True, (str,)),
    "distance_mi": (True, _NUM_OR_MISSING),
    "units": (True, _NUM_OR_MISSING),
    "year_built": (True, _NUM_OR_MISSING),
    "rent": (True, _NUM_OR_MISSING),
    "rent_basis": (True, (str,)),
    "page": (True, (int, type(None))),
    "quote": (True, (str,)),
}
OM_SALE_COMP_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "property": (True, (str,)),
    "units": (True, _NUM_OR_MISSING),
    "year_built": (True, _NUM_OR_MISSING),
    "sale_date": (True, (str,)),
    "price": (True, _NUM_OR_MISSING),
    "price_per_unit": (True, _NUM_OR_MISSING),
    "cap_rate": (True, _NUM_OR_MISSING),
    "buyer": (True, (str,)),
    "seller": (True, (str,)),
    "page": (True, (int, type(None))),
    "quote": (True, (str,)),
}
OM_COMPS_NUMBER_FIELDS = {
    "rent": ("distance_mi", "units", "year_built", "rent"),
    "sale": ("units", "year_built", "price", "price_per_unit", "cap_rate"),
}


def empty_om_comps(absent_reason: str) -> dict:
    return {
        "source": OM_COMPS_SOURCE,
        "rent": [],
        "sale": [],
        "retail_note": OM_COMPS_RETAIL_NOTE,
        "absent_reason": absent_reason,
    }


CLOSING_COSTS_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "state": (True, (str,)),
    # None where no price was stated — see pricing.price_per_unit.
    "price": (True, (*_NUM, type(None))),
    "transfer_tax": (True, (dict,)),
    "mortgage_tax": (True, (dict,)),
    "loan_amount": (True, (*_NUM, type(None))),
    "note": (True, (str, type(None))),
}

TAX_LINE_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "applies": (True, (bool,)),
    # How to read `amount`. A field, not a substring of the citation — see closing_costs.
    "amount_status": (True, (str,)),
    "rate_label": (True, (str,)),
    "amount": (True, (*_NUM, str)),
    "citation": (True, (str,)),
    "as_of": (True, (str,)),
    "caveat": (True, (str, type(None))),
}

DEMAND_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    # The compact ring table. Kept because the manifest and the model-ready field list are
    # built off named scalars, and because a five-row summary is what most readers want
    # first.
    "rings": (True, (list,)),
    # The COMPLETE report the engine produces: seven columns across seven sections. Held
    # as sections of labelled rows rather than named scalars, because two of the engine's
    # sections do not share the ring geometry — home value is a single ZIP figure and the
    # metro labor history is a year-by-year table — and a flat field list can only
    # represent those by lying about what the columns mean.
    "columns": (True, (list,)),
    "sections": (True, (list,)),
    "engine_note": (True, (str, type(None))),
    "data_year": (True, (int, type(None))),
    "output_year": (True, (int, type(None))),
    # 🔴 The engine's own measured 1-mile overcount, carried as data. An internal tool can
    # leave a known defect to operator memory; a public artifact cannot.
    "ring_1mi_caveat": (True, (str, type(None))),
    "matched_address": (True, (str, type(None))),
    "level_row_hints": (True, (list,)),
    "absent_reason": (True, (str, type(None))),
}

#: Number formats a demand row may declare. A closed set, because the writers and the
#: preview both switch on it and a typo would otherwise render a percentage as a count.
def empty_demand(absent_reason: str | None) -> dict:
    """A complete, honest absent-demand block.

    Built from DEMAND_KEYS rather than hand-listed, because hand-listing is exactly how it
    broke: `--demand` is the documented default-off path, three new required keys were
    added for the full report, and the omitted-demand branch was not updated — so the
    documented default started failing validation and exiting 1 instead of producing an
    honest absence.
    """
    out: dict = {}
    for key, (_required, types) in DEMAND_KEYS.items():
        if key == "absent_reason":
            out[key] = absent_reason
        elif list in types:
            out[key] = []
        else:
            out[key] = None
    return out


DEMAND_ROW_FORMATS: tuple[str, ...] = (
    "count", "currency0", "pct1", "pct2", "header",
)

DEMAND_SECTION_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "title": (True, (str,)),
    "note": (True, (str, type(None))),
    "kind": (True, (str,)),
    "rows": (True, (list,)),
    # The labor section carries a year-by-year metro table that does NOT share the ring
    # columns. It travels beside the rows rather than inside them, because forcing it into
    # the ring grid would put a year in a column headed "1 mi".
    "metro_history": (False, (list,)),
    "metro_label": (False, (str, type(None))),
    "metro_summary": (False, (dict,)),
}

DEMAND_SECTION_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "label": (True, (str,)),
    "values": (True, (list,)),
    "format": (True, (str,)),
    "kind": (True, (str,)),
    "note": (True, (str, type(None))),
    "indent": (True, (int,)),
}

DEMAND_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "ring": (True, (str,)),
    "population": (True, (int,)),
    "households": (True, (int,)),
    "median_income": (True, (*_NUM,)),
    # None where the source has no ring-level job count for that column (the MSA row).
    # A zero there would read as "no jobs", which is a different and false claim.
    "jobs": (True, (int, type(None))),
    "pop_growth_5yr": (True, (*_NUM, type(None))),
    "renter_share": (True, (*_NUM, type(None))),
}

SUPPLY_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "rings": (True, (list,)),
    "projects": (True, (list,)),
    "ring_stock": (True, (dict, type(None))),
    "absent_reason": (True, (str, type(None))),
}

SUPPLY_ROW_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "ring": (True, (str,)),
    "stage": (True, (str,)),
    "projects": (True, (int,)),
    "units": (True, (int,)),
}

#: The radius the supply block names its projects inside. The producer cuts the list at it
#: and both renderers title the table with it, so it is one number in one place.
SUPPLY_PROJECT_RADIUS_MI = 3.0

#: One reconciled development inside :data:`SUPPLY_PROJECT_RADIUS_MI`
#:. The ring rows say how many projects were counted; these say which
#: ones. Every value the pipeline did not state stays ``None`` — a supply project with no
#: unit count is the case that matters, because a zero there reads as "builds nothing".
SUPPLY_PROJECT_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "name": (True, (str,)),
    "miles": (True, (int, float)),
    "stage": (True, (str,)),
    "units": (True, (int, type(None))),
    "product": (True, (str, type(None))),
    "address": (True, (str, type(None))),
    "city": (True, (str, type(None))),
    "acres": (True, (int, float, type(None))),
    "developer": (True, (str, type(None))),
    "filed": (True, (str, type(None))),
    "latest": (True, (str, type(None))),
    "layers": (True, (list,)),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _check_mapping(
    where: str,
    obj: Any,
    spec: dict[str, tuple[bool, tuple[type, ...]]],
    errors: list[str],
) -> None:
    """Required-key / type / unknown-key check for one mapping."""
    if not isinstance(obj, dict):
        errors.append(f"{where}: expected an object, got {type(obj).__name__}")
        return
    for key, (required, types) in spec.items():
        if key not in obj:
            if required:
                errors.append(f"{where}: missing required key '{key}'")
            continue
        value = obj[key]
        # bool is a subclass of int; keep them from satisfying each other silently.
        if bool in types and not isinstance(value, bool):
            errors.append(f"{where}.{key}: expected bool, got {type(value).__name__}")
            continue
        if bool not in types and isinstance(value, bool):
            errors.append(f"{where}.{key}: unexpected bool")
            continue
        if not isinstance(value, types):
            names = "/".join(t.__name__ for t in types)
            errors.append(
                f"{where}.{key}: expected {names}, got {type(value).__name__}"
            )
    for key in obj:
        if key not in spec:
            errors.append(f"{where}: unknown key '{key}'")


#: The three unit counts, named once. Every one of them is a DIVISOR, and all three have
#: to answer the same question the same way: a POSITIVE whole number, or the absent
#: sentinel. Nothing else — not zero, not prose, not a float. The validator walks this
#: table and `assemble` asks `usable_count` before publishing any of them, so a producer
#: and the check cannot drift.
COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("summary", "units_total"),
    ("pricing", "units_total"),
    ("t12", "units"),
)


def usable_count(value: Any) -> "int | None":
    """The positive whole count ``value`` states, or None when it does not state one.

    Strict on purpose. A zero unit count divides every per-unit figure in the output set
    and prints as a number a reader has no reason to doubt — "0 vacant units" read into
    the wrong field is the case that named this. A float or a numeral in
    quotes is refused here too: `extract_llm._normalise_field_types` converts those at the
    boundary, with a note, and anything still wearing the wrong type by now is a bug
    rather than a value.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _check_count_sentinel(where: str, obj: Any, key: str, errors: list[str]) -> None:
    """A unit count is a POSITIVE whole number or the absent sentinel — nothing else."""
    if not isinstance(obj, dict) or key not in obj:
        return
    value = obj[key]
    if value == MISSING:
        return
    if usable_count(value) is None:
        errors.append(
            f"{where}: a count is a positive whole number or {MISSING!r}, got {value!r}"
        )


def _check_rows(
    where: str,
    rows: Any,
    spec: dict[str, tuple[bool, tuple[type, ...]]],
    errors: list[str],
) -> None:
    if not isinstance(rows, list):
        errors.append(f"{where}: expected a list")
        return
    for i, row in enumerate(rows):
        _check_mapping(f"{where}[{i}]", row, spec, errors)


def _validate_demand_sections(demand: dict, errors: list[str]) -> None:
    """Every section's rows must fit its declared columns, and say what they mean.

    A row with more values than there are columns is the failure mode that matters: it
    renders anyway, shifted, and every figure after the overflow is attributed to the
    wrong ring. Nothing on the page would show it.
    """
    cols = demand.get("columns")
    if not isinstance(cols, list):
        errors.append("demand.columns: expected a list of column labels")
        cols = []
    sections = demand.get("sections")
    if not isinstance(sections, list):
        errors.append("demand.sections: expected a list")
        return
    for i, sec in enumerate(sections):
        where = f"demand.sections[{i}]"
        if not isinstance(sec, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        _check_mapping(where, sec, DEMAND_SECTION_KEYS, errors)
        _check_rows(f"{where}.rows", sec.get("rows"), DEMAND_SECTION_ROW_KEYS, errors)
        if not isinstance(sec.get("rows"), list):
            continue
        for j, row in enumerate(sec["rows"]):
            if not isinstance(row, dict):
                continue
            rwhere = f"{where}.rows[{j}]"
            fmt = row.get("format")
            if fmt not in DEMAND_ROW_FORMATS:
                errors.append(
                    f"{rwhere}.format: expected one of {DEMAND_ROW_FORMATS}, got {fmt!r}"
                )
            vals = row.get("values")
            if not isinstance(vals, list):
                continue
            # A ZIP-geography row carries its own single figure and is not a ring row.
            limit = 1 if row.get("kind") == "zip" else len(cols)
            if len(vals) > limit:
                errors.append(
                    f"{rwhere}: {len(vals)} values against {limit} column(s). An overlong "
                    f"row still renders, shifted, and every figure after the overflow is "
                    f"attributed to the wrong column."
                )
            for v in vals:
                if v is not None and not isinstance(v, _NUM):
                    errors.append(f"{rwhere}: non-numeric value {v!r}")
                    break


UNRESTRICTED_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "communities": (True, (int,)),
    "rows": (True, (int,)),
    "by_bed": (True, (list,)),
    "year_min": (True, (int, type(None))),
    "year_max": (True, (int, type(None))),
    "note": (True, (str, type(None))),
    "swapped_in": (False, (list,)),
    "swapped_out": (False, (list,)),
    "varied": (False, (str,)),
    "held_fixed": (False, (list,)),
}

UNRESTRICTED_BED_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "beds": (True, (*_NUM,)),
    "communities": (True, (int,)),
    "rows": (True, (int,)),
    "avg_asking": (True, (*_NUM, type(None))),
    "avg_net_effective": (True, (*_NUM, type(None))),
}


#: Lease-status values a duplicate record may carry, mirroring the extractor's
#: LEASE_STATUS_VOCABULARY. `None` is a real value — plenty of rows carry a blank Status.
#:
#: Enumerated because an unrecognised status is NOT inert: it routes a unit into the
#: "no in-place row existed" branch of the de-duplication check, which is the branch that
#: EXCUSES a wrong keep. `'Currnet'` therefore turned a MISMATCH into an OK, silently.
#: `""` is how the extractor represents a BLANK Status — it reads the cell through
#: `str(... or "").strip()`, so a blank arrives as an empty string, never as None.
#: Enumerated from what the extractor EMITS, not from what the source cells hold:
#: the first cut listed None and turned every genuinely-blank row unusable.
#: ``Unknown`` is here since an earlier review: a value the extractor does not recognise is
#: RECORDED as unknown rather than relabelled ``New``. It is still not inert — the
#: duplicate check reads it and says the choice was made on lease dates — but it no longer
#: launders a typo into a status the check treats as meaningful.
LEASE_STATUSES: tuple = ("Current", "New", "Renewal", "Unknown", "", None)


DUP_DETAIL_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "unit_id": (True, (str,)),
    "kept_row": (True, (int,)),
    "kept_lease_status": (True, (str, type(None))),
    "kept_rent": (True, (*_NUM, type(None))),
    # What the SOURCE cell says: 0.0 when it states zero, None only when it is empty.
    "kept_rent_stated": (True, (*_NUM, type(None))),
    "kept_market_rent": (True, (*_NUM, type(None))),
    "dropped": (True, (list,)),
    "candidate_rows": (True, (list,)),
}

DUP_DROPPED_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "row": (True, (int,)),
    "lease_status": (True, (str, type(None))),
    "rent": (True, (*_NUM, type(None))),
    "rent_stated": (True, (*_NUM, type(None))),
    "market_rent": (True, (*_NUM, type(None))),
}


def _validate_duplicate_detail(stated: dict, errors: list[str]) -> None:
    """Every de-duplication record well-formed and self-consistent.

    The dedup check reads these to decide whether the right row was kept and what the
    wrong one would have cost. A malformed record used to raise a KeyError inside the
    check rather than failing here with the field named.
    """
    detail = stated.get("duplicate_detail")
    if detail is None:
        return
    if not isinstance(detail, list):
        errors.append("rent_roll.stated.duplicate_detail: expected a list")
        return
    seen: set[str] = set()
    for i, rec in enumerate(detail):
        where = f"rent_roll.stated.duplicate_detail[{i}]"
        if not isinstance(rec, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        _check_mapping(where, rec, DUP_DETAIL_KEYS, errors)
        uid = rec.get("unit_id")
        if isinstance(uid, str):
            if uid in seen:
                errors.append(f"{where}.unit_id: {uid!r} appears twice")
            seen.add(uid)
        if rec.get("kept_lease_status") not in LEASE_STATUSES:
            errors.append(
                f"{where}.kept_lease_status: {rec.get('kept_lease_status')!r} is not a "
                f"known status {LEASE_STATUSES}. An unrecognised value is not inert — it "
                f"routes the unit into the 'no in-place row existed' branch, which is the "
                f"branch that excuses a wrong keep."
            )
        cands = rec.get("candidate_rows")
        kept = rec.get("kept_row")
        # Every candidate must be an int BEFORE anything calls set() or sorted() on the
        # list — and before the kept_row membership test below, which does `in cands`.
        # An unhashable element made validate() raise TypeError, and validate()'s whole
        # contract is that it RETURNS problems rather than throwing them: a validator
        # that crashes on bad input is the one input shape it cannot report.
        cands_ok = isinstance(cands, list) and all(
            isinstance(x, int) and not isinstance(x, bool) for x in cands
        )
        if isinstance(cands, list) and not cands_ok:
            bad = [x for x in cands
                   if not (isinstance(x, int) and not isinstance(x, bool))]
            errors.append(
                f"{where}.candidate_rows: {bad!r} is not a row number. Every candidate "
                f"must be an int."
            )
        if cands_ok and isinstance(kept, int) and kept not in cands:
            errors.append(
                f"{where}: kept_row {kept} is not among candidate_rows {cands} — the "
                f"record describes keeping a row that was never a candidate"
            )
        if cands_ok and len(set(cands)) != len(cands):
            errors.append(f"{where}.candidate_rows: {cands} contains a repeated row")
        dropped = rec.get("dropped")
        if not isinstance(dropped, list):
            errors.append(f"{where}.dropped: expected a list")
            continue
        for j, drop in enumerate(dropped):
            dwhere = f"{where}.dropped[{j}]"
            if not isinstance(drop, dict):
                errors.append(f"{dwhere}: expected a mapping")
                continue
            _check_mapping(dwhere, drop, DUP_DROPPED_KEYS, errors)
            if drop.get("lease_status") not in LEASE_STATUSES:
                errors.append(
                    f"{dwhere}.lease_status: {drop.get('lease_status')!r} is not a known "
                    f"status {LEASE_STATUSES}"
                )
        # The dropped rows must be EXACTLY the candidates minus the kept one — a
        # partition. "Present among the candidates" was too weak: it accepted a duplicated
        # dropped id, and a count check alone accepted a dropped row that WAS the kept row
        # as long as the arithmetic happened to line up.
        drop_ids = [d.get("row") for d in dropped if isinstance(d, dict)]
        if all(isinstance(x, int) for x in drop_ids) and cands_ok \
                and isinstance(kept, int):
            if len(set(drop_ids)) != len(drop_ids):
                errors.append(
                    f"{where}.dropped: row ids {drop_ids} contain a duplicate — the same "
                    f"source row cannot be dropped twice"
                )
            if kept in drop_ids:
                errors.append(
                    f"{where}.dropped: row {kept} is also kept_row — a row cannot be both"
                )
            want = sorted(set(cands) - {kept})
            if sorted(set(drop_ids)) != want:
                errors.append(
                    f"{where}.dropped: row ids {sorted(set(drop_ids))} are not exactly "
                    f"candidate_rows minus kept_row ({want}). The kept row plus the "
                    f"dropped rows must partition the candidates, or the record describes "
                    f"a de-duplication that did not happen."
                )


def _validate_unrestricted(comps: dict, errors: list[str]) -> None:
    """The vintage-only counterfactual's nested shape.

    Validated here rather than discovered downstream: a malformed ``--comps`` input used to
    reach ``notes.for_deal()`` first and raise there, which turned a bad input into a crash
    instead of a validation message naming the field.
    """
    block = comps.get("unrestricted")
    if block is None:
        return
    if not isinstance(block, dict):
        errors.append("comps.unrestricted: expected a mapping or null")
        return
    _check_mapping("comps.unrestricted", block, UNRESTRICTED_KEYS, errors)
    rows = block.get("by_bed")
    if not isinstance(rows, list):
        errors.append("comps.unrestricted.by_bed: expected a list")
        return
    seen: set[int] = set()
    for i, row in enumerate(rows):
        where = f"comps.unrestricted.by_bed[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        _check_mapping(where, row, UNRESTRICTED_BED_KEYS, errors)
        beds = row.get("beds")
        # Bed counts key the mix-matching. A non-integral one silently matches nothing, so
        # the contrast would quietly compare fewer bed counts than it claims.
        if isinstance(beds, _NUM) and not isinstance(beds, bool):
            if float(beds) != int(beds):
                errors.append(f"{where}.beds: {beds!r} is not a whole number of bedrooms")
            elif int(beds) in seen:
                errors.append(f"{where}.beds: {int(beds)} appears more than once")
            else:
                seen.add(int(beds))
    y0, y1 = block.get("year_min"), block.get("year_max")
    if isinstance(y0, int) and isinstance(y1, int) and y0 > y1:
        errors.append(
            f"comps.unrestricted: year_min {y0} is after year_max {y1}"
        )
    # The counterfactual must say what it varied. A block that does not is not a
    # counterfactual, and the note that reads it attributes its gap to vintage.
    if block.get("varied") is not None and block.get("varied") != "vintage":
        errors.append(
            f"comps.unrestricted.varied: expected 'vintage', got {block['varied']!r} — the "
            f"note built from this block attributes the difference to vintage alone"
        )


def _validate_comp_tiers(comps: dict, errors: list[str]) -> None:
    """The three-tier display contract.

    The tiers are not decoration — they are the claim "these five are the market". So the
    structure is validated rather than trusted: only detail tiers in ``rows``, the counts
    the tiering parameters promise, distance monotonic across tier 1 into tier 2 (a tier-2
    community closer than a tier-1 one means the ordering broke), and a reason on every
    nearby row. A year without a source, or a source without a year, is also an error:
    that pair is the entire audit trail for a filter this display applies.
    """
    tiering = comps.get("tiering")
    if not isinstance(tiering, dict):
        errors.append("comps.tiering: expected a mapping of the tiering parameters")
        tiering = {}
    else:
        _check_mapping("comps.tiering", tiering, COMPS_TIERING_KEYS, errors)
    _check_rows("comps.nearby", comps.get("nearby"), NEARBY_ROW_KEYS, errors)

    rows = comps.get("rows")
    if not isinstance(rows, list):
        return

    def _units_pair(where: str, row: dict) -> None:
        units, src = row.get("units"), row.get("units_source")
        if isinstance(units, str) and units != MISSING:
            errors.append(f"{where}.units: a string other than {MISSING!r} ({units!r})")
        pr, pr_src = row.get("public_record_units"), row.get("public_record_units_source")
        if pr is not None and not pr_src:
            errors.append(f"{where}: public_record_units {pr} carries no public_record_units_source")
        if pr is None and pr_src:
            errors.append(f"{where}: public_record_units_source {pr_src!r} but no public_record_units")
        if units == MISSING and src not in (None, NO_PUBLIC_RECORD_LABEL):
            errors.append(f"{where}: units MISSING but units_source {src!r}")

    def _year_pair(where: str, row: dict) -> None:
        year, src = row.get("year_built"), row.get("year_built_source")
        if year is not None and not src:
            errors.append(
                f"{where}: year_built {year} carries no year_built_source. A vintage with "
                f"no stated origin cannot be audited, and the tracker has no year column "
                f"— every year here came from somewhere nameable."
            )
        if year is None and src:
            errors.append(f"{where}: year_built_source {src!r} but no year_built")
        if year is not None and isinstance(year, int) and not 1800 <= year <= 2200:
            errors.append(f"{where}: year_built {year} is not a plausible year")

    per_tier: dict[int, set[str]] = {}
    prev_key: tuple[float, str] | None = None
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        where = f"comps.rows[{i}]"
        tier = row.get("tier")
        if tier not in DETAIL_TIERS:
            errors.append(
                f"{where}.tier: expected one of {DETAIL_TIERS} — tier 3 belongs in "
                f"comps.nearby, which has no rent columns, got {tier!r}"
            )
        else:
            per_tier.setdefault(int(tier), set()).add(str(row.get("community")))
        _year_pair(where, row)
        _units_pair(where, row)
        # Rows are per plan, so a community repeats; order is by (distance, community) and
        # must not go backwards, or tier 1 is not "the five closest".
        key = (float(row.get("distance_mi") or 0.0), str(row.get("community")))
        if prev_key is not None and key < prev_key:
            errors.append(
                f"{where}: out of order — {key} follows {prev_key}. Tiers are assigned by "
                f"distance, so an unsorted block silently mislabels which comps are the "
                f"top five."
            )
        prev_key = key

    top_n, add_n = tiering.get("top_n"), tiering.get("additional_n")
    if isinstance(top_n, int) and len(per_tier.get(1, ())) > top_n:
        errors.append(
            f"comps: tier 1 holds {len(per_tier[1])} communities, more than the "
            f"{top_n} it promises"
        )
    if isinstance(add_n, int) and len(per_tier.get(2, ())) > add_n:
        errors.append(
            f"comps: tier 2 holds {len(per_tier[2])} communities, more than the "
            f"{add_n} it promises"
        )
    if per_tier.get(2) and isinstance(top_n, int) and len(per_tier.get(1, ())) < top_n:
        errors.append(
            f"comps: tier 2 is populated while tier 1 holds only "
            f"{len(per_tier.get(1, ()))} of {top_n} — the additional tier cannot start "
            f"before the top tier is full"
        )
    overlap = per_tier.get(1, set()) & per_tier.get(2, set())
    if overlap:
        errors.append(f"comps: {sorted(overlap)} appear in both detail tiers")

    nearby = comps.get("nearby")
    if isinstance(nearby, list):
        detailed = per_tier.get(1, set()) | per_tier.get(2, set())
        for i, row in enumerate(nearby):
            if not isinstance(row, dict):
                continue
            where = f"comps.nearby[{i}]"
            if row.get("tier") != 3:
                errors.append(f"{where}.tier: expected 3, got {row.get('tier')!r}")
            if not str(row.get("reason") or "").strip():
                errors.append(
                    f"{where}: no reason. A community listed as nearby-only must say why "
                    f"it is not a comp."
                )
            if str(row.get("community")) in detailed:
                errors.append(
                    f"{where}: {row.get('community')!r} is also a detail row — a community "
                    f"is in one tier or the other"
                )
            _year_pair(where, row)
            _units_pair(where, row)
            for rent_key in ("asking_rent", "net_effective_rent", "concession"):
                if rent_key in row:
                    errors.append(
                        f"{where}: carries {rent_key}. Tier 3 is identity only — a rent on "
                        f"a nearby row would be read as a comp without having passed the "
                        f"comp criteria."
                    )


def _month_ordinal(label: str) -> int:
    year, month = label.split("-")
    return int(year) * 12 + int(month) - 1


def _check_month_sequence(labels: list, errors: list[str]) -> None:
    """A trailing twelve is twelve *consecutive* months, in one direction.

    Checking only the count and the format lets a shuffled or duplicated sequence through,
    and everything downstream then quietly reads the wrong window: ``detect_month_order``
    compares the first label against the last, and ``recent_indices`` assumes the recent
    months sit contiguously at one end. The result is a wrong T-3 and T-6, which means a
    wrong annualized NOI and a wrong cap rate, with nothing on the output looking odd.
    """
    if not all(isinstance(lab, str) and re.match(r"^\d{4}-\d{2}$", lab) for lab in labels):
        return  # the per-label format check already reported this
    months = [_month_ordinal(lab) for lab in labels]
    for i, lab in enumerate(labels):
        mm = int(lab.split("-")[1])
        if not 1 <= mm <= 12:
            errors.append(f"t12.month_labels[{i}]: {lab!r} is not a real month")
            return
    if len(months) < 2:
        return
    steps = {b - a for a, b in zip(months, months[1:])}
    if steps != {1} and steps != {-1}:
        errors.append(
            "t12.month_labels: expected twelve consecutive months in one direction "
            f"(ascending or descending); got {labels[0]!r}..{labels[-1]!r} with "
            f"month-to-month steps {sorted(steps)}"
        )


_SUBTOTAL_KEYS_DOC = "Total Revenue / Total Operating Expenses / Net Operating Income / " \
                     "Other Income / Utility Reimbursements / Utilities"


def checkable_subtotal_keys() -> set[str]:
    """The subtotal labels the Error Check block actually cross-ties.

    A key outside this set is dead data: nothing reads it, so a typo would silently mean
    a check that never happens.
    """
    return {
        "Total Revenue",
        PNL_TOTAL_OPEX_LABEL,
        PNL_NOI_LABEL,
        *BREAKOUT_CATEGORIES,
    }


def _walk_strings(node: Any, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(node, str):
        out.append((path, node))
    elif isinstance(node, dict):
        for key, value in node.items():
            _walk_strings(value, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk_strings(value, f"{path}[{i}]", out)


def _check_no_formula_text(deal: dict, errors: list[str]) -> None:
    """No source string may begin with ``=``.

    openpyxl stores a string starting with ``=`` as a *formula*, so a crafted document
    label, unit id, or community name would become live spreadsheet code in a public
    download. Rejecting it here is what lets the writers treat a leading ``=`` as an
    unambiguous marker of their own generated formulas. The two rules are one invariant.
    """
    found: list[tuple[str, str]] = []
    _walk_strings(deal, "", found)
    for path, value in found:
        if value.startswith("="):
            errors.append(
                f"{path}: string value begins with '=' ({value[:40]!r}) — it would be "
                f"stored as a live spreadsheet formula. Source text may not start with '='."
            )


def _check_om_comps(block: Any, errors: list[str]) -> None:
    """The OM-stated comparables: strict rows, numbers-or-MISSING, rows or a reason."""
    _check_mapping("om_comps", block, OM_COMPS_KEYS, errors)
    if not isinstance(block, dict):
        return
    for kind, spec in (("rent", OM_RENT_COMP_ROW_KEYS), ("sale", OM_SALE_COMP_ROW_KEYS)):
        rows = block.get(kind)
        _check_rows(f"om_comps.{kind}", rows, spec, errors)
        if not isinstance(rows, list):
            continue
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for field in OM_COMPS_NUMBER_FIELDS[kind]:
                value = row.get(field)
                if isinstance(value, str) and value != MISSING:
                    errors.append(
                        f"om_comps.{kind}[{i}].{field}: {value!r} is a string that is not "
                        f"{MISSING} -- a number the document states is a number, not a formatted string"
                    )
                if isinstance(value, bool):
                    errors.append(f"om_comps.{kind}[{i}].{field}: a boolean is not a number")
    has_rows = bool(block.get("rent")) or bool(block.get("sale"))
    if has_rows and block.get("absent_reason"):
        errors.append("om_comps: absent_reason is set but rows are present -- one or the other")
    if not has_rows and not block.get("absent_reason"):
        errors.append("om_comps: no rows and no absent_reason -- an empty block must say why")


def validate(deal: dict) -> list[str]:
    """Return a list of problems with ``deal``. An empty list means it is valid."""
    errors: list[str] = []
    if not isinstance(deal, dict):
        return ["deal: expected an object at the top level"]

    for section in SECTIONS:
        if section not in deal:
            errors.append(f"deal: missing required section '{section}'")
    for key in deal:
        if key not in SECTIONS and key not in OPTIONAL_SECTIONS:
            errors.append(f"deal: unknown top-level section '{key}'")
    if errors:
        # Structure is wrong enough that per-section checks would just be noise.
        return errors
    if "om_comps" in deal:
        _check_om_comps(deal["om_comps"], errors)

    _check_mapping("meta", deal["meta"], META_KEYS, errors)
    _check_rows("meta.notes", deal["meta"].get("notes"), NOTE_KEYS, errors)
    if isinstance(deal["meta"].get("notes"), list):
        for i, n in enumerate(deal["meta"]["notes"]):
            if isinstance(n, dict) and not str(n.get("note") or "").strip():
                errors.append(f"meta.notes[{i}]: empty note text")
    _check_mapping("summary", deal["summary"], SUMMARY_KEYS, errors)
    _check_mapping("t12", deal["t12"], T12_KEYS, errors)
    # The three unit counts, from the one table. They take a string ONLY as the absent
    # sentinel — widening them to `(int, str)` is what stops a count nobody supplied being
    # stored as 0 — and they take a number only when it is a positive whole one.
    for _section, _key in COUNT_FIELDS:
        _check_count_sentinel(
            f"{_section}.{_key}", deal.get(_section), _key, errors
        )
    _check_mapping("rent_roll", deal["rent_roll"], RENT_ROLL_KEYS, errors)
    _rr = deal["rent_roll"] if isinstance(deal["rent_roll"], dict) else {}
    if isinstance(_rr.get("stated"), dict) and not _absent_in_validate(
        deal, "rent_roll", "units"
    ):
        # `stated` is what the SOURCE export said about itself. With no export there is
        # nothing for it to hold, so an absent roll carries an empty dict rather than a
        # set of invented nulls.
        _check_mapping("rent_roll.stated", _rr["stated"], ROLL_STATED_KEYS, errors)
        _validate_duplicate_detail(_rr["stated"], errors)
    _check_mapping("trade_out", deal["trade_out"], TRADE_OUT_KEYS, errors)
    _check_mapping("pricing", deal["pricing"], PRICING_KEYS, errors)
    _check_mapping("comps", deal["comps"], COMPS_KEYS, errors)
    _check_mapping("demand", deal["demand"], DEMAND_KEYS, errors)
    _check_mapping("supply", deal["supply"], SUPPLY_KEYS, errors)

    meta = deal["meta"] if isinstance(deal["meta"], dict) else {}
    if meta.get("toolkit_name") not in (None, TOOLKIT_NAME):
        errors.append(
            f"meta.toolkit_name: expected {TOOLKIT_NAME!r}, got {meta.get('toolkit_name')!r}"
        )
    # The assessment ratio is a MULTIPLIER on the property's whole value, so a percentage
    # stored here does not shade the tax line, it multiplies it by a hundred. The range is
    # enforced rather than documented.
    _summary = deal.get("summary") if isinstance(deal, dict) else None
    ratio = _summary.get("assessment_ratio") if isinstance(_summary, dict) else None
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        if not (0 < ratio <= 1):
            errors.append(
                f"summary.assessment_ratio: {ratio} is not a share of value. It must be "
                f"a fraction greater than 0 and at most 1 — 1.0 means 100% of value — "
                f"because compute multiplies the property's value by it."
            )

    for key in ("generated_on", "as_of_date"):
        val = meta.get(key)
        # An EMPTY as_of_date is a legal answer and the only honest one for a source that
        # stated no date and carried nothing to infer one from. It is never a substitute
        # for a date the source did state, and `generated_on` is always a real date.
        if key == "as_of_date" and val == "":
            continue
        if isinstance(val, str) and not _DATE_RE.match(val):
            errors.append(f"meta.{key}: expected YYYY-MM-DD, got {val!r}")
    # meta.generated_on becomes the workbooks' pinned archive timestamp, and a ZIP entry
    # can only carry 1980-2107. Refusing an out-of-range date here — rather than letting
    # it surface as a ValueError from the zip writer, after the file has already been
    # written — keeps the failure at the boundary where it is explainable.
    gen = meta.get("generated_on")
    if isinstance(gen, str) and _DATE_RE.match(gen):
        year = int(gen[:4])
        if not ZIP_MIN_YEAR <= year <= ZIP_MAX_YEAR:
            errors.append(
                f"meta.generated_on: {gen!r} is outside {ZIP_MIN_YEAR}-{ZIP_MAX_YEAR}, "
                f"the range a ZIP archive entry can carry, and the xlsx timestamp is "
                f"pinned from this date."
            )

    # ---- t12 -------------------------------------------------------------------------
    t12 = deal["t12"] if isinstance(deal["t12"], dict) else {}
    # Twelve months and a positive unit count are requirements of a statement that EXISTS.
    # Holding a declared-absent section to them would make the declaration inexpressible.
    t12_absent = _absent_in_validate(deal, "t12", "rows")
    labels = t12.get("month_labels")
    if isinstance(labels, list) and not t12_absent:
        if len(labels) != 12:
            errors.append(f"t12.month_labels: expected 12 labels, got {len(labels)}")
        for i, lab in enumerate(labels):
            if not isinstance(lab, str):
                errors.append(f"t12.month_labels[{i}]: expected str")
            elif not re.match(r"^\d{4}-\d{2}$", lab):
                errors.append(
                    f"t12.month_labels[{i}]: expected YYYY-MM, got {lab!r}"
                )
        _check_month_sequence(labels, errors)
    # Zero means "the unit count is not known", which is the honest state for a
    # trailing-twelve statement uploaded on its own: the P&L is perfectly computable and
    # only the per-unit figures are unknown. It is allowed ONLY when the summary says the
    # same thing — the disagreement check below is what stops a statement quietly
    # disclaiming a unit count the rest of the file states.
    if isinstance(t12.get("units"), int) and t12["units"] < 0 and not t12_absent:
        errors.append("t12.units: expected a unit count of zero or more")
    rows = t12.get("rows")
    _check_rows("t12.rows", rows, T12_ROW_KEYS, errors)
    if isinstance(rows, list):
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            monthly = row.get("monthly")
            if isinstance(monthly, list):
                if len(monthly) != 12:
                    errors.append(
                        f"t12.rows[{i}].monthly: expected 12 values, got {len(monthly)}"
                    )
                for j, v in enumerate(monthly):
                    if v is not None and (
                        isinstance(v, bool) or not isinstance(v, _NUM)
                    ):
                        errors.append(
                            f"t12.rows[{i}].monthly[{j}]: expected a number or null, "
                            f"got {type(v).__name__}"
                        )
    subtotals = t12.get("source_subtotals")
    if isinstance(subtotals, dict):
        allowed = checkable_subtotal_keys()
        for key, value in subtotals.items():
            if key not in allowed:
                errors.append(
                    f"t12.source_subtotals: {key!r} is not a subtotal the Error Check "
                    f"reads, so nothing would ever cross-tie against it. Expected one of: "
                    f"{_SUBTOTAL_KEYS_DOC}"
                )
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, _NUM):
                errors.append(
                    f"t12.source_subtotals[{key!r}]: expected a number or null, got "
                    f"{type(value).__name__} ({value!r}). A non-numeric subtotal reaches "
                    f"the workbook's cross-tie formulas and produces an invalid check "
                    f"while the preview reports NOT CHECKED."
                )

    if isinstance(rows, list):
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            cat = row.get("category")
            if cat is not None and cat not in ASSIGNABLE_CATEGORIES:
                errors.append(
                    f"t12.rows[{i}].category: {cat!r} is not a vendored category "
                    f"(see T12_REVENUE_CATEGORIES / T12_EXPENSE_CATEGORIES)"
                )

    # ---- rent roll -------------------------------------------------------------------
    rr = deal["rent_roll"] if isinstance(deal["rent_roll"], dict) else {}
    roll_absent = _absent_in_validate(deal, "rent_roll", "units")
    if (
        isinstance(rr.get("as_of"), str)
        and rr["as_of"] != ""
        and not _DATE_RE.match(rr["as_of"])
        and not roll_absent
    ):
        errors.append(f"rent_roll.as_of: expected YYYY-MM-DD, got {rr['as_of']!r}")
    _check_rows("rent_roll.units", rr.get("units"), RENT_ROLL_UNIT_KEYS, errors)
    if isinstance(rr.get("units"), list):
        seen: set[str] = set()
        for i, unit in enumerate(rr["units"]):
            if not isinstance(unit, dict):
                continue
            uid = unit.get("unit_id")
            if isinstance(uid, str):
                if uid in seen:
                    errors.append(f"rent_roll.units[{i}].unit_id: duplicate {uid!r}")
                seen.add(uid)
            lt = unit.get("lease_type")
            if lt is not None and lt not in LEASE_TYPES:
                errors.append(
                    f"rent_roll.units[{i}].lease_type: expected one of "
                    f"{LEASE_TYPES} or null, got {lt!r}"
                )
            # I4: a name-like key must not exist at all.
            for banned in ("tenant", "resident", "name", "tenant_name", "resident_name"):
                if banned in unit:
                    errors.append(
                        f"rent_roll.units[{i}]: name-like key '{banned}' is prohibited "
                        f"(I4 -- unit IDs only)"
                    )

    # ---- trade-out -------------------------------------------------------------------
    to = deal["trade_out"] if isinstance(deal["trade_out"], dict) else {}
    _check_rows("trade_out.rows", to.get("rows"), TRADE_OUT_ROW_KEYS, errors)
    if isinstance(to.get("rows"), list) and isinstance(to.get("absent_reason"), str):
        if to["rows"]:
            errors.append(
                "trade_out: absent_reason is set but rows are present -- one or the other"
            )
    if isinstance(to.get("rows"), list) and not to["rows"] and to.get("absent_reason") is None:
        errors.append(
            "trade_out: no rows and no absent_reason -- an empty block must say why"
        )
    if isinstance(to.get("rows"), list):
        for i, row in enumerate(to["rows"]):
            if isinstance(row, dict) and row.get("cohort") not in TRADE_OUT_COHORTS:
                errors.append(
                    f"trade_out.rows[{i}].cohort: expected one of {TRADE_OUT_COHORTS}, "
                    f"got {row.get('cohort')!r}"
                )

    # ---- pricing ---------------------------------------------------------------------
    pricing = deal["pricing"] if isinstance(deal["pricing"], dict) else {}
    ppu = pricing.get("price_per_unit")
    if isinstance(ppu, _NUM) and not isinstance(ppu, bool):
        if not 50_000 <= ppu <= 600_000:
            errors.append(
                f"pricing.price_per_unit: {ppu} is outside the 50,000-600,000 band a "
                f"per-unit sale price occupies -- a figure outside it is income, capex, "
                f"a fee, or a typo, not a price"
            )
    summary = deal["summary"] if isinstance(deal["summary"], dict) else {}
    # Both counts, compared as they are — including the sentinel. Comparing only when
    # BOTH are ints let one of them go MISSING beside a stated number without a word,
    # which is the same drift the check exists for and reads worse on the page: a card
    # saying 180 units above a per-unit column saying MISSING.
    if (
        pricing.get("units_total") != summary.get("units_total")
        and usable_count(pricing.get("units_total")) is not None
        or (pricing.get("units_total") == MISSING) != (summary.get("units_total") == MISSING)
    ):
        errors.append(
            f"pricing.units_total ({pricing.get('units_total')}) disagrees with "
            f"summary.units_total ({summary.get('units_total')})"
        )
    if (
        t12.get("units") != summary.get("units_total")
        and (usable_count(t12.get("units")) is not None
             or t12.get("units") == MISSING)
        and not _absent_in_validate(deal, "t12", "rows")
    ):
        errors.append(
            f"t12.units ({t12['units']}) disagrees with summary.units_total "
            f"({summary['units_total']})"
        )

    # A tax divisor is the rate's UNIT — per $100, per $1,000. A zero divides by zero and
    # a negative one silently flips the sign of every tax figure computed from it. Rejected
    # here so no surface has to decide what to do with one.
    _div = summary.get("tax_divisor")
    if isinstance(_div, _NUM) and not isinstance(_div, bool) and _div <= 0:
        errors.append(
            f"summary.tax_divisor: {_div!r} is not a usable divisor. It is the rate's unit "
            f"(per $100, per $1,000); zero divides by zero and a negative value inverts "
            f"the sign of every tax figure derived from it."
        )
    # An exemption is subtracted from a computed tax on several surfaces. A negative one
    # would ADD to the bill under the name of a relief.
    _ex = summary.get("tax_exemption")
    if isinstance(_ex, _NUM) and not isinstance(_ex, bool) and _ex < 0:
        errors.append(f"summary.tax_exemption: {_ex!r} is negative — an exemption reduces a bill")

    # ---- comps -----------------------------------------------------------------------
    comps = deal["comps"] if isinstance(deal["comps"], dict) else {}
    if comps.get("source") not in (None, COMP_SOURCE):
        errors.append(
            f"comps.source: only {COMP_SOURCE!r} is permitted, got {comps.get('source')!r}"
        )
    subject = comps.get("subject_market_rent")
    modeled = summary.get("market_rent_for_model")
    if (
        isinstance(subject, _NUM)
        and not isinstance(subject, bool)
        and isinstance(modeled, _NUM)
        and not isinstance(modeled, bool)
        and abs(subject - modeled) > 1
    ):
        errors.append(
            f"comps.subject_market_rent ({subject}) is not summary.market_rent_for_model "
            f"({modeled}). The comp block compares the subject's *modeled* market rent "
            f"against the comp set; using in-place rent here understates the spread and "
            f"mislabels which rent is being compared."
        )
    _check_rows("comps.rows", comps.get("rows"), COMP_ROW_KEYS, errors)
    if isinstance(comps.get("rows"), list):
        for i, row in enumerate(comps["rows"]):
            if isinstance(row, dict) and row.get("source") != COMP_SOURCE:
                errors.append(
                    f"comps.rows[{i}].source: only {COMP_SOURCE!r} is permitted, got "
                    f"{row.get('source')!r} -- licensed comp data never travels"
                )
    _validate_comp_tiers(comps, errors)
    _validate_unrestricted(comps, errors)
    # The tiering filtered on a product and a vintage. If either is not the SUBJECT's, the
    # comp set describes a different property — a wrong answer that looks entirely right,
    # since nothing on the page would show the mismatch. Caught in the fixture once, where
    # the band was centred on 2004 while the subject was built in 2019.
    tiering = comps.get("tiering") if isinstance(comps.get("tiering"), dict) else {}
    if comps.get("rows"):
        t_year, s_year = tiering.get("subject_year_built"), summary.get("year_built")
        if isinstance(s_year, int) and t_year != s_year:
            errors.append(
                f"comps.tiering.subject_year_built ({t_year!r}) is not summary.year_built "
                f"({s_year!r}). The vintage band was centred on the wrong year, so the "
                f"comp set is filtered around a property that is not the subject."
            )
        t_prod, s_prod = tiering.get("subject_product"), summary.get("product_type")
        if isinstance(s_prod, str) and t_prod != s_prod:
            errors.append(
                f"comps.tiering.subject_product ({t_prod!r}) is not summary.product_type "
                f"({s_prod!r}) — the comp set was filtered for the wrong product"
            )

    # ---- demand / supply -------------------------------------------------------------
    demand = deal["demand"] if isinstance(deal["demand"], dict) else {}
    _check_rows("demand.rings", demand.get("rings"), DEMAND_ROW_KEYS, errors)
    if isinstance(demand.get("rings"), list):
        for i, row in enumerate(demand["rings"]):
            if isinstance(row, dict) and row.get("ring") not in DEMAND_RINGS:
                errors.append(
                    f"demand.rings[{i}].ring: expected one of {DEMAND_RINGS}, got "
                    f"{row.get('ring')!r}"
                )
    _validate_demand_sections(demand, errors)
    supply = deal["supply"] if isinstance(deal["supply"], dict) else {}
    _check_rows("supply.rings", supply.get("rings"), SUPPLY_ROW_KEYS, errors)
    if isinstance(supply.get("rings"), list):
        for i, row in enumerate(supply["rings"]):
            if not isinstance(row, dict):
                continue
            if row.get("stage") not in SUPPLY_STAGES:
                errors.append(
                    f"supply.rings[{i}].stage: expected one of {SUPPLY_STAGES}, got "
                    f"{row.get('stage')!r}"
                )
            if row.get("ring") not in DEMAND_RINGS:
                errors.append(
                    f"supply.rings[{i}].ring: expected one of {DEMAND_RINGS}, got "
                    f"{row.get('ring')!r}"
                )
    _check_rows("supply.projects", supply.get("projects"), SUPPLY_PROJECT_KEYS, errors)
    if isinstance(supply.get("projects"), list):
        for i, row in enumerate(supply["projects"]):
            if not isinstance(row, dict):
                continue
            if row.get("stage") not in SUPPLY_STAGES:
                errors.append(
                    f"supply.projects[{i}].stage: expected one of {SUPPLY_STAGES}, got "
                    f"{row.get('stage')!r}"
                )
            if not str(row.get("name") or "").strip():
                errors.append(f"supply.projects[{i}].name: a listed project must be named")

    for section, row_key in (
        ("comps", "rows"),
        ("demand", "rings"),
        ("supply", "rings"),
        ("t12", "rows"),
        ("rent_roll", "units"),
    ):
        block = deal[section] if isinstance(deal[section], dict) else {}
        rows_val = block.get(row_key)
        reason = block.get("absent_reason")
        if isinstance(rows_val, list) and not rows_val and not (
            isinstance(reason, str) and reason.strip()
        ):
            errors.append(
                f"{section}: no {row_key} and no absent_reason — an empty block must say "
                f"why it is empty"
            )
        if isinstance(rows_val, list) and rows_val and isinstance(reason, str):
            errors.append(
                f"{section}: absent_reason is set but {row_key} are present — one or the other"
            )

    cc = deal["closing_costs"] if isinstance(deal["closing_costs"], dict) else {}
    _check_mapping("closing_costs", cc, CLOSING_COSTS_KEYS, errors)
    for line in ("transfer_tax", "mortgage_tax"):
        block = cc.get(line)
        if isinstance(block, dict):
            _check_mapping(f"closing_costs.{line}", block, TAX_LINE_KEYS, errors)
            # A rate that does not apply must still cite the authority that says so —
            # "no transfer tax here" is a claim about the law like any other.
            if not str(block.get("citation", "")).strip():
                errors.append(
                    f"closing_costs.{line}: needs a statute or authority citation, "
                    f"including when the answer is that no such tax applies"
                )
            if block.get("applies") is False and block.get("amount") not in (0, 0.0):
                errors.append(
                    f"closing_costs.{line}: applies is False but amount is "
                    f"{block.get('amount')!r} — a tax that does not apply is zero"
                )
            status = block.get("amount_status")
            if status is not None and status not in _closing.AMOUNT_STATUSES:
                errors.append(
                    f"closing_costs.{line}.amount_status: expected one of "
                    f"{_closing.AMOUNT_STATUSES}, got {status!r}"
                )
            if status in (_closing.NOT_ASSERTED, _closing.ZERO_BY_LAW) and block.get(
                "amount"
            ) not in (0, 0.0):
                errors.append(
                    f"closing_costs.{line}.amount_status is {status!r} but amount is "
                    f"{block.get('amount')!r} — neither status may carry a figure"
                )

    # The closing block must describe THIS deal: a stale block for another state or
    # another price validates cleanly and renders as if it were the subject's.
    if isinstance(cc, dict):
        cc_state = cc.get("state")
        s_state = summary.get("state")
        if isinstance(cc_state, str) and isinstance(s_state, str) and cc_state != s_state.upper():
            errors.append(
                f"closing_costs.state ({cc_state!r}) is not summary.state ({s_state!r}) — "
                f"the closing block would render as this deal's while describing another"
            )
        ppu = pricing.get("price_per_unit")
        units = pricing.get("units_total")
        cc_price = cc.get("price")
        if (
            isinstance(ppu, _NUM) and not isinstance(ppu, bool)
            and isinstance(units, int)
            and isinstance(cc_price, _NUM) and not isinstance(cc_price, bool)
            and abs(cc_price - ppu * units) > 0.01
        ):
            errors.append(
                f"closing_costs.price ({cc_price}) is not pricing.price_per_unit x "
                f"pricing.units_total ({ppu * units}) — the closing costs would be computed "
                f"on a price the rest of the output does not use"
            )

    _provenance.validate_block(deal.get("provenance"), errors)

    _check_no_formula_text(deal, errors)

    return errors


# --------------------------------------------------------------------------------------
# Field inventory -- the manifest's row source (I2)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One row of the model-ready manifest.

    ``path`` locates the field's example value inside a deal dict. Dotted keys with
    optional ``[n]`` list indices; ``None`` where the value is a formula result or a
    per-row column with no single example.
    """

    group: str
    name: str
    type: str
    definition: str
    source: str
    fills: str
    path: str | None = None


_DOC_OM = "Offering memorandum / marketing package"
_DOC_T12 = "Trailing-twelve-month income statement"
_DOC_ROLL = "Rent roll export"
_DOC_TRADE = "Lease trade-out report"
_DOC_TAX = "County tax portal"
_DOC_SCHOOL = "Public school-rating source"
_DOC_SCRAPE = "Operator's own weekly rent scrape"
_DOC_CENSUS = "Public demographic data"
_DOC_PERMIT = "Public permit / entitlement records"
_DOC_DERIVED = "Derived by the pipeline"
_DOC_USER = "Supplied by the operator"

_FILL_NONE = "Reference only — not a model input"

FIELD_INVENTORY: tuple[Field, ...] = (
    # ---- Meta ------------------------------------------------------------------------
    Field("Meta", "toolkit_name", "text", "Name of the toolkit that produced the outputs.", _DOC_DERIVED, _FILL_NONE, "meta.toolkit_name"),
    Field("Meta", "deal_name", "text", "Deal name used on every output.", _DOC_OM, "Model cover / deal header", "meta.deal_name"),
    Field("Meta", "generated_on", "date", "Date the output set was produced.", _DOC_DERIVED, "Model cover date", "meta.generated_on"),
    Field("Meta", "as_of_date", "date", "As-of date the financials and roll describe. Every window is measured from this date, never from today.", _DOC_T12, "Model as-of / analysis date", "meta.as_of_date"),
    Field("Meta", "illustrative_pricing", "boolean", "True when every price and cap rate on the output is illustrative rather than a real quote.", _DOC_DERIVED, _FILL_NONE, "meta.illustrative_pricing"),
    Field("Meta", "pricing_note", "text", "One line stating where the pricing figure came from and that it is illustrative.", _DOC_DERIVED, _FILL_NONE, "meta.pricing_note"),
    Field("Meta", "synthetic", "boolean", "True when the deal is entirely fabricated for demonstration.", _DOC_DERIVED, _FILL_NONE, "meta.synthetic"),
    Field("Meta", "provenance", "list", "Per-document record of what was read and what it supplied.", _DOC_DERIVED, _FILL_NONE, None),
    Field("Meta", "excluded_sources", "list", "Data sources deliberately not used, so a reader knows what a MISSING field would have needed.", _DOC_DERIVED, _FILL_NONE, None),
    # ---- Deal identification ---------------------------------------------------------
    Field("Deal identification", "street_address", "text", "Street address as resolved, not as printed in marketing.", _DOC_OM, "Property address", "summary.street_address"),
    Field("Deal identification", "city", "text", "City.", _DOC_OM, "Property address", "summary.city"),
    Field("Deal identification", "state", "text", "Two-letter state code. Drives the per-state tax divisor.", _DOC_OM, "Property address / tax logic", "summary.state"),
    Field("Deal identification", "zip", "text", "ZIP code.", _DOC_OM, "Property address", "summary.zip"),
    Field("Deal identification", "county", "text", "County. Determines which tax portal was queried.", _DOC_OM, "Tax jurisdiction", "summary.county"),
    Field("Deal identification", "msa", "text", "Metropolitan statistical area.", _DOC_DERIVED, "Market label", "summary.msa"),
    Field("Deal identification", "address_provenance", "text", "How the address was resolved and from which document — so a wrong address is traceable rather than mysterious.", _DOC_DERIVED, _FILL_NONE, "summary.address_provenance"),
    # ---- Property --------------------------------------------------------------------
    Field("Property", "product_type", "text", "SFD, TH, or MF. Selects which underwriting shape applies.", _DOC_OM, "Product type / deal mode", "summary.product_type"),
    Field("Property", "structure", "text", "Construction structure and stories.", _DOC_OM, "Property description", "summary.structure"),
    Field("Property", "units_total", "integer", "Total units. Divisor for every per-unit figure in the output set.", _DOC_OM, "Unit count", "summary.units_total"),
    Field("Property", "year_built", "integer", "Year built or year of first delivery.", _DOC_OM, "Year built", "summary.year_built"),
    Field("Property", "product_detail", "text", "Unit mix in prose — bedroom counts, square footage range, notable features.", _DOC_OM, "Property description", "summary.product_detail"),
    Field("Property", "construction_status", "text", "Stabilized, in lease-up, or under construction.", _DOC_OM, "Deal mode (stabilized vs phased)", "summary.construction_status"),
    Field("Property", "delivery_date", "text", "Certificate-of-occupancy or final-delivery date.", _DOC_OM, "Delivery / phasing schedule", "summary.delivery_date"),
    # ---- Parties ---------------------------------------------------------------------
    Field("Parties", "seller_entity", "text", "Selling entity as named in the marketing package.", _DOC_OM, "Seller", "summary.seller_entity"),
    Field("Parties", "listing_brokerage", "text", "Listing brokerage, generically labeled. No firm or individual name appears in a published file.", _DOC_OM, "Broker", "summary.listing_brokerage"),
    Field("Parties", "deal_status", "text", "Where the deal sits — marketed, call for offers, under contract.", _DOC_OM, "Deal status", "summary.deal_status"),
    # ---- Pricing ---------------------------------------------------------------------
    Field("Pricing", "pricing_guidance", "text", "Pricing guidance in prose. Illustrative in every published output.", _DOC_USER, "Purchase price assumption", "summary.pricing_guidance"),
    Field("Pricing", "price_per_unit", "currency", "Price per unit. Sanity-banded to 50,000–600,000; a figure outside that band is income, capex, a fee, or a typo, not a price.", _DOC_USER, "Purchase price per unit", "pricing.price_per_unit"),
    Field("Pricing", "document_price_per_unit", "currency", "The per-unit price the memorandum itself states, or implies as a stated total over the unit count — the seller's figure, kept beside the price entered for comparison. None when it states no usable price.", _DOC_OM, "Pricing guidance (memorandum), for comparison", "pricing.document_price_per_unit"),
    Field("Pricing", "total_price", "currency", "Price per unit times unit count.", _DOC_DERIVED, "Purchase price", None),
    Field("Pricing", "t12_cap_rate", "percent", "Trailing-twelve NOI divided by total price.", _DOC_DERIVED, "Going-in cap rate (T-12 basis)", None),
    Field("Pricing", "t3_cap_rate", "percent", "Annualized trailing-three NOI divided by total price.", _DOC_DERIVED, "Going-in cap rate (T-3 basis)", None),
    Field("Pricing", "broker_proforma_noi", "currency", "NOI from the broker's pro forma, when one was supplied. Kept in its own column, never merged into the T-12.", _DOC_OM, "Broker pro forma column", "t12.broker_proforma.noi"),
    # ---- Rents -----------------------------------------------------------------------
    Field("Rents", "in_place_rent_avg", "currency", "Weighted average in-place rent across occupied units.", _DOC_ROLL, "In-place rent", "summary.in_place_rent_avg"),
    Field("Rents", "market_rent_for_model", "currency", "The rent to underwrite: new move-in rent (residents who moved in inside the lookback window, by move-in date) where the plan has one, otherwise in-place. Requires a usable lease start AND move-in date on the roll; without either it is in-place outright. Never a blend of new and renewal.", _DOC_DERIVED, "Market rent / unit-mix rent", "summary.market_rent_for_model"),
    Field("Rents", "fair_market_value", "currency", "Operator's own fair-market-value estimate for the asset, stated so the pricing assumption has something to be checked against. Supplied deliberately, not scraped — a field with no defined source ships blank forever.", _DOC_USER, "Valuation cross-check", "summary.fair_market_value"),
    # ---- Taxes -----------------------------------------------------------------------
    Field("Taxes", "tax_rate_as_billed", "percent", "Millage as actually billed, not a statutory or blended rate.", _DOC_TAX, "Property tax rate", "summary.tax_rate_as_billed"),
    Field("Taxes", "tax_rate_evidence", "text", "What was read and from where. Reads MISSING when the portal returned nothing — a visible hole beats a plausible guess.", _DOC_TAX, _FILL_NONE, "summary.tax_rate_evidence"),
    Field("Taxes", "assessed_value_basis", "currency", "Value the assessment is computed against.", _DOC_TAX, "Tax basis", "summary.assessed_value_basis"),
    Field("Taxes", "assessment_ratio", "percent", "Share of value that is assessed.", _DOC_TAX, "Tax inputs", "summary.assessment_ratio"),
    Field("Taxes", "tax_divisor", "number", "Per-state divisor converting millage to a rate.", _DOC_TAX, "Tax inputs", "summary.tax_divisor"),
    Field("Taxes", "tax_exemption", "currency", "Exemption applied against the computed bill.", _DOC_TAX, "Tax inputs", "summary.tax_exemption"),
    Field("Taxes", "tax_parcel_id", "text", "Assessor parcel number the bill is issued against. A tax figure that cannot name its parcel cannot be checked.", _DOC_TAX, "Tax evidence", "summary.tax_parcel_id"),
    Field("Taxes", "tax_area_code", "text", "Tax area / district code, which selects the published rate row that applies.", _DOC_TAX, "Tax evidence", "summary.tax_area_code"),
    Field("Taxes", "tax_basis_label", "text", "What the assessed basis IS in that state — a limited value, a market value, an appraised value. The word matters because only some of them reset on sale.", _DOC_TAX, "Tax inputs", "summary.tax_basis_label"),
    Field("Taxes", "tax_basis_note", "text", "How that basis behaves on a sale. Where a state caps growth and does not reset on transfer, pricing tax off the purchase price overstates it badly.", _DOC_TAX, "Tax inputs", "summary.tax_basis_note"),
    Field("Taxes", "tax_billed_amount", "currency", "The tax the county actually billed, for the year stated — the figure the computed tax is checked against.", _DOC_TAX, "Tax evidence", "summary.tax_billed_amount"),
    Field("Taxes", "tax_billed_year", "integer", "Tax year of the billed figure.", _DOC_TAX, "Tax evidence", "summary.tax_billed_year"),
    Field("Taxes", "computed_annual_tax", "currency", "Basis times ratio over divisor times rate, less exemption.", _DOC_DERIVED, "Property tax expense", None),
    Field("Taxes", "computed_tax_per_unit", "currency", "Computed annual tax divided by unit count.", _DOC_DERIVED, "Property tax per unit", None),
    # ---- Closing costs -----------------------------------------------------------------
    Field("Closing costs", "closing_state", "text", "State whose statutes the closing costs are computed under. Cross-checked against the property's own state.", _DOC_DERIVED, "Closing cost assumptions", "closing_costs.state"),
    Field("Closing costs", "closing_price", "currency", "Consideration the transfer tax is computed on: price per unit times unit count.", _DOC_DERIVED, "Closing cost assumptions", "closing_costs.price"),
    Field("Closing costs", "loan_amount", "currency", "Loan the mortgage or recording tax is computed on. Absent, that tax is reported as not asserted rather than inferred from the price.", _DOC_USER, "Debt assumptions", "closing_costs.loan_amount"),
    Field("Closing costs", "transfer_tax_amount", "currency", "State or county real-estate transfer tax at the stated price.", _DOC_DERIVED, "Closing costs", "closing_costs.transfer_tax.amount"),
    Field("Closing costs", "transfer_tax_rate", "text", "The rate applied, as the statute expresses it.", _DOC_DERIVED, "Closing costs", "closing_costs.transfer_tax.rate_label"),
    Field("Closing costs", "transfer_tax_citation", "text", "Statute or revenue-department authority for the rate — including where the authority is that no such tax exists.", _DOC_DERIVED, _FILL_NONE, "closing_costs.transfer_tax.citation"),
    Field("Closing costs", "mortgage_tax_amount", "currency", "Mortgage, intangible or recording tax on the security instrument, computed on the loan.", _DOC_DERIVED, "Closing costs", "closing_costs.mortgage_tax.amount"),
    Field("Closing costs", "mortgage_tax_rate", "text", "The rate applied, as the statute expresses it.", _DOC_DERIVED, "Closing costs", "closing_costs.mortgage_tax.rate_label"),
    Field("Closing costs", "mortgage_tax_citation", "text", "Statute or revenue-department authority for the rate.", _DOC_DERIVED, _FILL_NONE, "closing_costs.mortgage_tax.citation"),
    # ---- Schools ---------------------------------------------------------------------
    Field("Schools", "school_elementary_name", "text", "Zoned elementary school.", _DOC_SCHOOL, _FILL_NONE, "summary.school_elementary_name"),
    Field("Schools", "school_elementary_rating", "number", "Elementary rating on a 1–10 scale.", _DOC_SCHOOL, _FILL_NONE, "summary.school_elementary_rating"),
    Field("Schools", "school_middle_name", "text", "Zoned middle school.", _DOC_SCHOOL, _FILL_NONE, "summary.school_middle_name"),
    Field("Schools", "school_middle_rating", "number", "Middle-school rating on a 1–10 scale.", _DOC_SCHOOL, _FILL_NONE, "summary.school_middle_rating"),
    Field("Schools", "school_high_name", "text", "Zoned high school.", _DOC_SCHOOL, _FILL_NONE, "summary.school_high_name"),
    Field("Schools", "school_high_rating", "number", "High-school rating on a 1–10 scale.", _DOC_SCHOOL, _FILL_NONE, "summary.school_high_rating"),
    # ---- T-12 revenue (generated below) ----------------------------------------------
    # ---- T-12 expense (generated below) ----------------------------------------------
    # ---- T-12 structure --------------------------------------------------------------
    Field("T-12 structure", "month_labels", "list", "Twelve month labels in the source statement's own order.", _DOC_T12, "T-12 column headers", None),
    Field("T-12 structure", "month_order", "text", "Whether the months run oldest-first or newest-first. Detected from the labels, never assumed, because the T-3 window depends on it.", _DOC_DERIVED, _FILL_NONE, None),
    Field("T-12 structure", "line_item_label", "text", "The source statement's own wording for a line, kept verbatim beside the category it was mapped to.", _DOC_T12, "T-12 line item", "t12.rows[0].label"),
    Field("T-12 structure", "line_item_category", "text", "Which vendored category a source line was mapped to. Blank for an uncategorized subtotal, which stays visible rather than being dropped.", _DOC_DERIVED, "T-12 mapping", "t12.rows[0].category"),
    Field("T-12 structure", "line_item_t12_total", "currency", "Sum of a line's twelve months.", _DOC_DERIVED, "T-12 column", None),
    Field("T-12 structure", "line_item_t3_total", "currency", "Sum of a line's three most recent months, taken from whichever end the direction detector found.", _DOC_DERIVED, "T-3 column", None),
    Field("T-12 structure", "gross_scheduled_rent", "currency", "Printed on the statement where it exists, otherwise gross potential rent plus gain/(loss) to lease. Which one fired is visible in the workbook formula.", _DOC_DERIVED, "Gross scheduled rent", None),
    Field("T-12 structure", "total_revenue", "currency", "Sum from gross scheduled rent down through revenue adjustments — never from gross potential rent, which double-counts loss-to-lease.", _DOC_DERIVED, "Total revenue", None),
    Field("T-12 structure", "total_operating_expenses", "currency", "Sum of the twelve expense categories.", _DOC_DERIVED, "Total operating expenses", None),
    Field("T-12 structure", "net_operating_income", "currency", "Total revenue less total operating expenses.", _DOC_DERIVED, "Net operating income", None),
    Field("T-12 structure", "t3_annualized", "currency", "Trailing-three total times four, beside every rollup row.", _DOC_DERIVED, "T-3 annualized column", None),
    Field("T-12 structure", "t6_annualized", "currency", "Trailing-six total times two, so a reader can see whether the trend is up or down.", _DOC_DERIVED, "Trend cross-check", None),
    Field("T-12 structure", "t3_trailing_revenue_annualized", "currency", "Six figures: each of the six most recent months plus the two before it, times four — the trend the T-3 cap rate sits at the end of.", _DOC_DERIVED, "Investment analysis — trailing T-3 revenue series", None),
    Field("T-12 structure", "statement_guards", "text", "Four sanity checks on how the statement is printed — contra-revenue sign, expense sign, recoveries inside expense buckets, a subtotal counted with its members — each OK or REVIEW with the lines named.", _DOC_DERIVED, "Review flags — not a model input", None),
    Field("T-12 structure", "expense_per_unit", "currency", "Each rollup row divided by unit count, blank-guarded so a bad unit count cannot fill the column with errors.", _DOC_DERIVED, "Per-unit expense check", None),
    Field("T-12 structure", "pct_of_revenue", "percent", "Each rollup row as a share of total revenue.", _DOC_DERIVED, "Expense ratio check", None),
    Field("T-12 structure", "breakout_top4", "table", "For other income, utility reimbursements, and utilities: the four largest constituent lines plus a Rest row, tying to the rollup.", _DOC_DERIVED, "Other-income / utility detail", None),
    Field("T-12 structure", "source_subtotal", "currency", "A subtotal as printed on the source statement, kept for the cross-tie.", _DOC_T12, _FILL_NONE, None),
    Field("T-12 structure", "error_check_difference", "currency", "Our category total less the source's printed subtotal. Anything over a dollar reads REVIEW rather than quietly passing.", _DOC_DERIVED, _FILL_NONE, None),
    Field("T-12 structure", "absent_reason", "text", "Why the T-12 section is empty, when no trailing-twelve statement was supplied. Twelve months of zeroes would validate and read as a property that earned nothing; a stated reason cannot be mistaken for one.", _DOC_DERIVED, _FILL_NONE, "t12.absent_reason"),
    # ---- Rent roll: per unit ---------------------------------------------------------
    Field("Rent roll — unit", "unit_id", "text", "Unit identifier. The output carries unit IDs only; no tenant-name column exists in the schema.", _DOC_ROLL, "Unit list", "rent_roll.units[0].unit_id"),
    Field("Rent roll — unit", "floor_plan", "text", "Floor-plan code the unit belongs to.", _DOC_ROLL, "Unit mix", "rent_roll.units[0].floor_plan"),
    Field("Rent roll — unit", "beds", "number", "Bedrooms.", _DOC_ROLL, "Unit mix", "rent_roll.units[0].beds"),
    Field("Rent roll — unit", "baths", "number", "Bathrooms.", _DOC_ROLL, "Unit mix", "rent_roll.units[0].baths"),
    Field("Rent roll — unit", "sqft", "number", "Square footage.", _DOC_ROLL, "Unit mix", "rent_roll.units[0].sqft"),
    Field("Rent roll — unit", "status", "text", "Occupied, vacant, or model/down.", _DOC_ROLL, "Occupancy", "rent_roll.units[0].status"),
    Field("Rent roll — unit", "lease_start", "date", "Lease start. A value the source gave as text stays text — a date that arrives as 'n/a' is not coerced into 1899.", _DOC_ROLL, _FILL_NONE, "rent_roll.units[0].lease_start"),
    Field("Rent roll — unit", "move_in", "date", "Move-in date. Turnover is counted by move-in, not by lease start.", _DOC_ROLL, "Turnover", "rent_roll.units[0].move_in"),
    Field("Rent roll — unit", "lease_type", "text", "New, Renewal, or blank. Blank means the source could not support a classification — never a guess, never a blend.", _DOC_DERIVED, "New-vs-renewal split", "rent_roll.units[0].lease_type"),
    Field("Rent roll — unit", "in_place_rent", "currency", "Contract rent in place.", _DOC_ROLL, "In-place rent", "rent_roll.units[0].in_place_rent"),
    Field("Rent roll — unit", "rent_per_sf", "currency", "In-place rent divided by square footage.", _DOC_DERIVED, "Rent per square foot", None),
    # ---- Rent roll: per plan ---------------------------------------------------------
    Field("Rent roll — plan", "plan_avg_sqft", "number", "Average square footage for the plan.", _DOC_DERIVED, "Unit mix — average SF", None),
    Field("Rent roll — plan", "plan_units", "integer", "Units in the plan.", _DOC_DERIVED, "Unit mix — count", None),
    Field("Rent roll — plan", "plan_occupied", "integer", "Occupied units in the plan.", _DOC_DERIVED, "Occupancy by plan", None),
    Field("Rent roll — plan", "plan_vacant", "integer", "Vacant units in the plan.", _DOC_DERIVED, "Occupancy by plan", None),
    Field("Rent roll — plan", "plan_avg_in_place_rent", "currency", "Weighted average in-place rent for the plan's occupied units.", _DOC_DERIVED, "In-place rent by plan", None),
    Field("Rent roll — plan", "plan_signed_rent", "currency", "Average rent on every priced, CLASSIFIED lease start (new, renewal or month-to-month) inside the lookback window; a lease the roll cannot classify is left out, so this can undercount. Context only: a blend never feeds the model.", _DOC_DERIVED, "Leases signed", None),
    Field("Rent roll — plan", "plan_signed_count", "integer", "How many leases were signed in the window. n/a where the roll has no usable lease-start or move-in date.", _DOC_DERIVED, "Sample size", None),
    Field("Rent roll — plan", "plan_movein_rent", "currency", "Average rent of residents whose move-in date falls inside the lookback window, whatever their lease type.", _DOC_DERIVED, "New move-in rent evidence", None),
    Field("Rent roll — plan", "plan_movein_count", "integer", "How many move-ins the average rests on. A thin count is visible rather than implied.", _DOC_DERIVED, "Sample size", None),
    Field("Rent roll — plan", "plan_market_rent_for_model", "currency", "The plan's rent to underwrite: new move-in rent where one exists, otherwise in-place. This is the column a financial model consumes.", _DOC_DERIVED, "Unit-mix market rent", None),
    Field("Rent roll — plan", "plan_market_rent_basis", "text", "Which of the two fired, stated per plan.", _DOC_DERIVED, _FILL_NONE, None),
    # ---- Rent roll: totals -----------------------------------------------------------
    Field("Rent roll — totals", "reconciliation_verdict", "text", "PASS, FAIL or NOT CHECKED: the unit count (tolerance 0) and the export's stated market-rent total (tolerance $1) against what the export states, every duplicate row retaining the in-place lease, and no rent-readback mismatch.", _DOC_DERIVED, "Gate — the pack is partial until PASS", None),
    Field("Rent roll — totals", "total_units", "integer", "Units on the roll.", _DOC_DERIVED, "Unit count cross-check", None),
    Field("Rent roll — totals", "occupied_units", "integer", "Occupied units.", _DOC_DERIVED, "Occupancy", None),
    Field("Rent roll — totals", "vacant_units", "integer", "Vacant units.", _DOC_DERIVED, "Occupancy", None),
    Field("Rent roll — totals", "occupancy", "percent", "Occupied over total.", _DOC_DERIVED, "Vacancy assumption", None),
    Field("Rent roll — totals", "vacancy", "percent", "One less occupancy. The physical-vacancy input.", _DOC_DERIVED, "Vacancy assumption", None),
    Field("Rent roll — totals", "wavg_in_place_rent", "currency", "Mean contract rent over the occupied units with a stated contract rent (vacant units carry no in-place rent; an occupied unit with no stated rent is left out, not counted as zero).", _DOC_DERIVED, "In-place rent", None),
    Field("Rent roll — totals", "wavg_market_rent_for_model", "currency", "Unit-weighted market rent for model across the property.", _DOC_DERIVED, "Market rent", None),
    Field("Rent roll — totals", "annualized_turnover", "percent", "Move-ins inside the lookback window, annualized against unit count.", _DOC_DERIVED, "Turnover assumption", None),
    Field("Rent roll — totals", "lookback_months", "integer", "Length of the move-in and lease-signed window, measured back from the roll's as-of date.", _DOC_USER, _FILL_NONE, "rent_roll.lookback_months"),
    Field("Rent roll — totals", "new_tenant_tolerance_days", "integer", "How far lease start may sit from move-in before a lease stops counting as a new tenant. A knob on the output, not a constant buried in code.", _DOC_USER, _FILL_NONE, "rent_roll.new_tenant_tolerance_days"),
    Field("Rent roll — totals", "absent_reason", "text", "Why the rent-roll section is empty, when no roll was supplied. An empty block that says why is a deliverable; a block of zeroes is not.", _DOC_DERIVED, _FILL_NONE, "rent_roll.absent_reason"),
    Field("Rent roll — totals", "roll_as_of", "date", "As-of date of the roll. Every window is measured from here.", _DOC_ROLL, "Analysis date", "rent_roll.as_of"),
    # ---- Trade-out -------------------------------------------------------------------
    Field("Trade-out", "cohort", "text", "New, Renewal, or First generation.", _DOC_TRADE, "New-vs-renewal growth evidence", "trade_out.rows[0].cohort"),
    Field("Trade-out", "cohort_leases", "integer", "Leases in the cohort.", _DOC_TRADE, "Sample size", "trade_out.rows[0].leases"),
    Field("Trade-out", "cohort_prior_rent", "currency", "Average rent before the trade.", _DOC_TRADE, "Prior rent", "trade_out.rows[0].prior_rent"),
    Field("Trade-out", "cohort_new_rent_gross", "currency", "Average new rent before concessions.", _DOC_TRADE, "Gross trade-out", "trade_out.rows[0].new_rent_gross"),
    Field("Trade-out", "cohort_new_rent_effective", "currency", "Average new rent after concessions. Quoted separately from gross, because quoting one as the other is how a trade-out number becomes wrong.", _DOC_TRADE, "Effective trade-out", "trade_out.rows[0].new_rent_effective"),
    Field("Trade-out", "cohort_prior_rent_effective", "currency", "Average prior rent after the prior lease's concession. Optional: absent when the report states no prior concession, and the prior gross stands in.", _DOC_TRADE, "Prior effective rent", "trade_out.rows[0].prior_rent_effective"),
    Field("Trade-out", "cohort_gross_change_pct", "percent", "Gross change over prior rent.", _DOC_DERIVED, "Rent growth evidence", "trade_out.rows[0].gross_change_pct"),
    Field("Trade-out", "cohort_effective_change_pct", "percent", "Effective change over prior effective rent (the prior gross when no prior concession is stated).", _DOC_DERIVED, "Rent growth evidence", "trade_out.rows[0].effective_change_pct"),
    Field("Trade-out", "absent_reason", "text", "Why the block is empty, when no trade-out report exists. An empty block that says why is a deliverable; a block of plausible numbers is not.", _DOC_DERIVED, _FILL_NONE, "trade_out.absent_reason"),
    # ---- Comps -----------------------------------------------------------------------
    Field("Comps", "comp_community", "text", "Comparable community name.", _DOC_SCRAPE, "Comp set", "comps.rows[0].community"),
    Field("Comps", "comp_distance_mi", "number", "Straight-line distance from the subject.", _DOC_DERIVED, "Comp set", "comps.rows[0].distance_mi"),
    Field("Comps", "comp_floor_plan", "text", "Comp floor plan the row describes. Comps are stated per plan, not as one blended community number.", _DOC_SCRAPE, "Comp set", "comps.rows[0].floor_plan"),
    Field("Comps", "comp_tier", "integer", "1 = top comps (the closest five matching the subject's product and vintage), 2 = additional comps (the next fifteen). The identity-only third group carries no rent; since 2026-09-28 it is carried in the interchange file only and is not shown as a table.", _DOC_DERIVED, "Comp set", "comps.rows[0].tier"),
    Field("Comps", "comp_year_built", "integer", "Comp vintage where one is published: the public property record's year where it has one, else the community registry's. Blank means nobody published one, never that the community is new.", _DOC_SCRAPE, "Comp set", "comps.rows[0].year_built"),
    Field("Comps", "comp_year_built_source", "text", "Where that year came from: the public record with its as-of, or the community registry (third-party market data). Blank alongside a blank year; a year with no source is rejected at validation.", _DOC_SCRAPE, _FILL_NONE, "comps.rows[0].year_built_source"),
    Field("Comps", "nearby_community", "text", "A tracked community inside the radius that is NOT a comp. Carried in the interchange file; the ~340-row table was taken off the page on 2026-09-28.", _DOC_SCRAPE, "Market context", "comps.nearby[0].community"),
    Field("Comps", "nearby_address", "text", "Its address, so the reader can place it.", _DOC_SCRAPE, "Market context", "comps.nearby[0].address"),
    Field("Comps", "nearby_distance_mi", "number", "Distance from the subject.", _DOC_DERIVED, "Market context", "comps.nearby[0].distance_mi"),
    Field("Comps", "nearby_units", "integer", "Unit count. The registry's own count was seeded from licensed data exports, so the public demo withholds it and shows the public-record count instead where one exists (county property record, Florida DBPR apartment licence, City of Austin permit), with its source and as-of.", _DOC_SCRAPE, "Market context", "comps.nearby[0].units"),
    Field("Comps", "nearby_reason", "text", "Why it is not a comp: wrong product, vintage outside the band, no quoted rent, excluded by the tracker, or simply beyond the closest twenty. The field that answers 'why isn't the property across the street in your comp set?' for every community in the radius.", _DOC_DERIVED, "Market context", "comps.nearby[0].reason"),
    Field("Comps", "compset_vintage_footnote", "text", "The disclosure on the detailed tiers: the band applied, and whether some communities carried no year and so could not be tested.", _DOC_DERIVED, _FILL_NONE, "comps.tiering.vintage_footnote"),
    Field("Comps", "comp_beds", "number", "Bedrooms in the comp plan.", _DOC_SCRAPE, "Comp set", "comps.rows[0].beds"),
    Field("Comps", "comp_baths", "number", "Bathrooms in the comp plan.", _DOC_SCRAPE, "Comp set", "comps.rows[0].baths"),
    Field("Comps", "comp_sqft", "number", "Square footage of the comp plan.", _DOC_SCRAPE, "Comp set", "comps.rows[0].sqft"),
    Field("Comps", "comp_asking_rent", "currency", "Advertised asking rent.", _DOC_SCRAPE, "Market rent evidence", "comps.rows[0].asking_rent"),
    Field("Comps", "comp_concession", "currency", "Monthly value of the advertised concession.", _DOC_SCRAPE, "Concession assumption", "comps.rows[0].concession"),
    Field("Comps", "comp_net_effective_rent", "currency", "Asking rent less the amortized concession. Stated on every row — a concession-blind comp set is the most common way a rent assumption goes wrong.", _DOC_DERIVED, "Market rent evidence", "comps.rows[0].net_effective_rent"),
    Field("Comps", "comp_asking_psf", "currency", "Asking rent per square foot.", _DOC_DERIVED, "Comp set", None),
    Field("Comps", "comp_occupancy", "percent", "Occupancy where the source published it.", _DOC_SCRAPE, "Market context", "comps.rows[0].occupancy"),
    Field("Comps", "comp_as_of", "date", "When the row was scraped.", _DOC_SCRAPE, _FILL_NONE, "comps.rows[0].as_of"),
    Field("Comps", "comp_source", "text", "Always the operator's own weekly scrape. Licensed comp feeds are excluded by design and cannot be represented in this schema.", _DOC_SCRAPE, _FILL_NONE, "comps.rows[0].source"),
    Field("Comps", "compset_avg_asking", "currency", "Average asking rent across the comp set.", _DOC_DERIVED, "Market rent benchmark", None),
    Field("Comps", "compset_avg_net_effective", "currency", "Average net effective rent across the comp set.", _DOC_DERIVED, "Market rent benchmark", None),
    Field("Comps", "subject_vs_compset_spread", "percent", "Subject market rent over comp-set net effective, less one.", _DOC_DERIVED, "Rent positioning", None),
    # ---- Demand ----------------------------------------------------------------------
    Field("Demand", "demand_ring", "text", "Drive-ring the row describes: 1, 3, 5, or 10 miles, or the MSA.", _DOC_DERIVED, "Market summary", "demand.rings[0].ring"),
    Field("Demand", "demand_population", "integer", "Population in the ring.", _DOC_CENSUS, "Market summary", "demand.rings[0].population"),
    Field("Demand", "demand_households", "integer", "Households in the ring.", _DOC_CENSUS, "Market summary", "demand.rings[0].households"),
    Field("Demand", "demand_median_income", "currency", "Median household income in the ring.", _DOC_CENSUS, "Affordability check", "demand.rings[0].median_income"),
    Field("Demand", "demand_jobs", "integer", "Jobs located in the ring.", _DOC_CENSUS, "Market summary", "demand.rings[0].jobs"),
    Field("Demand", "demand_pop_growth_5yr", "percent", "Five-year population growth in the ring.", _DOC_CENSUS, "Growth assumption evidence", "demand.rings[0].pop_growth_5yr"),
    Field("Demand", "demand_renter_share", "percent", "Share of ring households that rent.", _DOC_CENSUS, "Demand context", "demand.rings[0].renter_share"),
    # ---- Supply ----------------------------------------------------------------------
    Field("Supply", "supply_ring", "text", "Ring the supply row describes.", _DOC_DERIVED, "Competing supply", "supply.rings[0].ring"),
    Field("Supply", "supply_stage", "text", "Entitlement, permitted, under construction, or delivered in the last 24 months. Stage is stated because entitled units and delivered units are not the same risk.", _DOC_PERMIT, "Competing supply", "supply.rings[0].stage"),
    Field("Supply", "supply_projects", "integer", "Projects at that ring and stage.", _DOC_PERMIT, "Competing supply", "supply.rings[0].projects"),
    Field("Supply", "supply_units", "integer", "Units at that ring and stage.", _DOC_PERMIT, "Competing supply / absorption", "supply.rings[0].units"),
    Field("Supply", "supply_ring_total_units", "integer", "All pipeline units in the ring across stages.", _DOC_DERIVED, "Competing supply", None),
    Field("Supply", "supply_pct_of_ring_stock", "percent", "Pipeline units as a share of the ring's existing stock, where stock is known.", _DOC_DERIVED, "Supply pressure", None),
    Field("Supply", "supply_project_name", "text", "One development inside three miles, named. The ring rows say how many projects were counted; these say which ones.", _DOC_PERMIT, "Competing supply", "supply.projects[0].name"),
    Field("Supply", "supply_project_miles", "number", "Distance from the subject, at the nearest record any lifecycle layer carried.", _DOC_DERIVED, "Competing supply", "supply.projects[0].miles"),
    Field("Supply", "supply_project_stage", "text", "The furthest tracked stage the pipeline shows for that development — the stage it was counted at.", _DOC_PERMIT, "Competing supply", "supply.projects[0].stage"),
    Field("Supply", "supply_project_units", "integer", "Units where a layer stated them. Blank means the source did not state a count, never zero.", _DOC_PERMIT, "Competing supply / absorption", "supply.projects[0].units"),
    Field("Supply", "supply_project_product", "text", "Product the pipeline records for the development.", _DOC_PERMIT, "Competing supply", "supply.projects[0].product"),
    Field("Supply", "supply_project_address", "text", "Street address where a layer carried one.", _DOC_PERMIT, "Competing supply", "supply.projects[0].address"),
    Field("Supply", "supply_project_city", "text", "City where a layer carried one.", _DOC_PERMIT, "Competing supply", "supply.projects[0].city"),
    Field("Supply", "supply_project_acres", "number", "Site acreage where a layer carried one.", _DOC_PERMIT, "Competing supply", "supply.projects[0].acres"),
    Field("Supply", "supply_project_developer", "text", "Developer where a layer named one.", _DOC_PERMIT, "Competing supply", "supply.projects[0].developer"),
    Field("Supply", "supply_project_filed", "date", "Earliest filing date across the layers the development appears on.", _DOC_PERMIT, "Competing supply", "supply.projects[0].filed"),
    Field("Supply", "supply_project_latest", "date", "Latest activity date across those layers.", _DOC_PERMIT, "Competing supply", "supply.projects[0].latest"),
    Field("Supply", "supply_project_layers", "text", "The pipeline layers the development appeared on. Layers are lifecycle views of the same market, so one development can appear on several and is counted once.", _DOC_PERMIT, _FILL_NONE, "supply.projects[0].layers"),
)


def _category_fields() -> tuple[Field, ...]:
    """One manifest row per vendored T-12 category, generated rather than typed."""
    out: list[Field] = []
    for cat in T12_REVENUE_CATEGORIES:
        out.append(
            Field(
                "T-12 revenue",
                cat,
                "currency",
                f"{cat} — trailing-twelve total, and trailing-three annualized beside it.",
                _DOC_T12,
                "Investment analysis — T-12 revenue column",
            )
        )
    for cat in T12_EXPENSE_CATEGORIES:
        out.append(
            Field(
                "T-12 expense",
                cat,
                "currency",
                f"{cat} — trailing-twelve total, trailing-three annualized, and per unit.",
                _DOC_T12,
                "Investment analysis — T-12 expense column",
            )
        )
    return tuple(out)


def _ordered_inventory() -> tuple[Field, ...]:
    """FIELD_INVENTORY with the generated category rows spliced into group order."""
    generated = _category_fields()
    groups_order: list[str] = []
    for f in (*FIELD_INVENTORY, *generated):
        if f.group not in groups_order:
            groups_order.append(f.group)
    # Keep the hand-written group sequence, but put the two generated T-12 groups
    # immediately before "T-12 structure".
    anchor = "T-12 structure"
    for g in ("T-12 revenue", "T-12 expense"):
        if g in groups_order:
            groups_order.remove(g)
    idx = groups_order.index(anchor) if anchor in groups_order else len(groups_order)
    groups_order[idx:idx] = ["T-12 revenue", "T-12 expense"]

    all_fields = (*FIELD_INVENTORY, *generated)
    out: list[Field] = []
    for g in groups_order:
        out.extend(f for f in all_fields if f.group == g)
    return tuple(out)


#: The manifest's actual row source: hand-written rows plus the generated category rows,
#: in group order. Use this, not FIELD_INVENTORY, when building the manifest.
MANIFEST_FIELDS: tuple[Field, ...] = _ordered_inventory()


#: The credit beside the Demand tab's home-value table, on every surface that shows it.
ZHVI_ATTRIBUTION = "Home values: Zillow Home Value Index (ZHVI), Zillow Group"


def home_values_credit(deal: dict) -> str | None:
    """The source line for the Demand tab's ZIP-versus-metro home values, when the deal's
    own provenance says they are Zillow's ZHVI; None otherwise — a home value from any
    other source (the synthetic fixture's invented ZIP) is never credited to Zillow."""
    st = (deal.get("provenance") or {}).get("demand.zip_home")
    if isinstance(st, dict) and "Zillow ZHVI" in str(st.get("source_doc") or ""):
        return ZHVI_ATTRIBUTION
    return None


def is_year_field(field: "Field") -> bool:
    """A field whose value is a calendar year (``year_built``, ``comp_year_built``,
    ``tax_billed_year``): its example renders "1984", never as a count."""
    return field.type == "integer" and (field.name == "year_built" or field.name.endswith(("_year_built", "_year")))

MANIFEST_COLUMNS: tuple[str, ...] = (
    "Group",
    "Field",
    "Type",
    "Definition",
    "Source document",
    "Typically fills (client model input)",
    "Public demo",
    "Example (demo deal)",
)

_PATH_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def resolve_path(deal: dict, path: str | None) -> Any:
    """Resolve a dotted path with optional list indices, e.g. ``comps.rows[0].community``.

    Returns ``None`` when the path is ``None`` or does not resolve — the manifest prints
    an em dash for those, which is the honest answer for a formula-derived field.
    """
    if not path:
        return None
    cur: Any = deal
    for name, index in _PATH_RE.findall(path):
        if name:
            if not isinstance(cur, dict) or name not in cur:
                return None
            cur = cur[name]
        else:
            i = int(index)
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
    return cur


def asserted_value(deal: dict, path: str | None) -> Any:
    """What the interchange ASSERTS at ``path`` — :func:`resolve_path` answers only what it
    HOLDS.

    A closing-cost line carries an ``amount`` beside an ``amount_status``, and the absent
    shape the upload path writes (``not_asserted``, amount 0) is a zero the pipeline never
    claimed. The deal-summary fragment reads the status; the manifest Example column and
    the Audit trail's "Value as published" resolved the raw amount and printed ``$0`` / ``0``
    (found live, 2026-09-04). Every published-value reader goes through here, so a status
    that is not an assertion reads MISSING everywhere; ``zero_by_law`` and ``asserted`` pass
    the amount through unchanged.
    """
    value = resolve_path(deal, path)
    if path and path.endswith(".amount"):
        parent = resolve_path(deal, path[: -len(".amount")])
        if isinstance(parent, dict) and parent.get("amount_status") == "not_asserted":
            return MISSING
    return value


def manifest_value(deal: dict, field: "Field") -> Any:
    """The value a manifest Example column may show for ``field`` — :func:`asserted_value`."""
    return asserted_value(deal, field.path)


#: Manifest rows for the OM-comparables block — listed only when a deal carries it, so
#: the published sample's manifest (no such block) is unchanged.
OM_COMPS_FIELDS: tuple[Field, ...] = (
    Field("OM comparables", "om_rent_comp_community", "text", "A rent comparable the memorandum states, by name; one row per comparable in the interchange.", _DOC_OM, "Rent comps (broker-stated)", "om_comps.rent[0].community"),
    Field("OM comparables", "om_rent_comp_distance_mi", "number", "Distance from the subject as the memorandum states it.", _DOC_OM, "Rent comps (broker-stated)", "om_comps.rent[0].distance_mi"),
    Field("OM comparables", "om_rent_comp_rent", "currency", "The monthly rent figure the memorandum states for that comparable; its basis is on the row.", _DOC_OM, "Rent comps (broker-stated)", "om_comps.rent[0].rent"),
    Field("OM comparables", "om_sale_comp_property", "text", "A whole-community sale comparable the memorandum cites, by name.", _DOC_OM, "Sale comps (institutional)", "om_comps.sale[0].property"),
    Field("OM comparables", "om_sale_comp_price", "currency", "The stated sale price.", _DOC_OM, "Sale comps (institutional)", "om_comps.sale[0].price"),
    Field("OM comparables", "om_sale_comp_price_per_unit", "currency", "The stated price per unit.", _DOC_OM, "Sale comps (institutional)", "om_comps.sale[0].price_per_unit"),
    Field("OM comparables", "om_sale_comp_cap_rate", "percent", "The stated cap rate, as a fraction.", _DOC_OM, "Sale comps (institutional)", "om_comps.sale[0].cap_rate"),
    Field("OM comparables", "om_sale_comp_date", "text", "The sale date as written.", _DOC_OM, "Sale comps (institutional)", "om_comps.sale[0].sale_date"),
)


def manifest_fields_for(deal: dict | None) -> tuple[Field, ...]:
    """The manifest inventory for THIS deal: the static rows, plus the OM-comparable rows
    when the deal carries that optional block."""
    if isinstance(deal, dict) and isinstance(deal.get("om_comps"), dict):
        return (*MANIFEST_FIELDS, *OM_COMPS_FIELDS)
    return MANIFEST_FIELDS


def manifest_groups() -> tuple[str, ...]:
    """Distinct group names, in manifest order."""
    seen: list[str] = []
    for f in MANIFEST_FIELDS:
        if f.group not in seen:
            seen.append(f.group)
    return tuple(seen)


def normalize_category(label: str | None) -> str | None:
    """Map a known upstream wording onto its vendored category label.

    Returns the label unchanged when it is already vendored, the vendored equivalent when
    it is a known synonym, and the original string otherwise -- so an unknown label
    reaches :func:`assert_categories` and fails there rather than being quietly dropped.
    """
    if label is None:
        return None
    stripped = " ".join(label.split())
    known = _known_category_labels()
    if stripped in known:
        return stripped
    # Case-insensitive on the CANONICALS too. Excel's SUMIF criteria are case-insensitive,
    # so 'payroll' and 'Payroll' genuinely do tie out — and the lowercase alias entries
    # that used to cover this were removed when the canonical spellings changed, silently
    # regressing inputs that had worked.
    lowered = stripped.lower()
    for canon in known:
        if canon.lower() == lowered:
            return canon
    return CATEGORY_ALIASES.get(lowered, stripped)


#: Sections that may declare themselves absent, and the key that holds their rows.
ABSENT_CAPABLE: dict[str, str] = {
    "t12": "rows",
    "rent_roll": "units",
    "trade_out": "rows",
    "comps": "rows",
    "demand": "rings",
    "supply": "rings",
}


def section_absent(deal: dict, section: str) -> str | None:
    """The stated reason a section is empty, or None when it is present.

    One function so a writer, a renderer and the manifest cannot disagree about what
    "absent" means. Absence is *declared*, not inferred: an empty rows list on its own is
    a bug, and the validator says so.
    """
    row_key = ABSENT_CAPABLE.get(section)
    if row_key is None:
        return None
    block = deal.get(section) if isinstance(deal, dict) else None
    if not isinstance(block, dict):
        return None
    reason = block.get("absent_reason")
    rows = block.get(row_key)
    if isinstance(rows, list) and not rows and isinstance(reason, str) and reason.strip():
        return reason
    return None


def _absent_in_validate(deal: dict, section: str, row_key: str) -> bool:
    block = deal.get(section) if isinstance(deal, dict) else None
    if not isinstance(block, dict):
        return False
    reason = block.get("absent_reason")
    rows = block.get(row_key)
    return isinstance(rows, list) and not rows and isinstance(reason, str) and bool(reason.strip())


def _known_category_labels() -> set[str]:
    return set(ASSIGNABLE_CATEGORIES) | {
        "Total Revenue",
        PNL_TOTAL_OPEX_LABEL,
        PNL_NOI_LABEL,
    }


def assert_categories(labels: Iterable[str]) -> list[str]:
    """I3: return the labels that are not members of the vendored category lists.

    The extractor calls this on every category label read out of a real run, so an
    upstream relabel surfaces loudly at showcase time instead of silently. Known
    synonyms are normalized first (:data:`CATEGORY_ALIASES`); anything left over is
    genuine drift and is returned for the caller to fail on.
    """
    known = _known_category_labels()
    return [lab for lab in labels if lab and normalize_category(lab) not in known]


def pnl_row_order() -> tuple[str, ...]:
    """Rollup row sequence: revenue block, expense block, expense total, NOI."""
    return (
        *T12_REVENUE_CATEGORIES,
        *T12_EXPENSE_CATEGORIES,
        PNL_TOTAL_OPEX_LABEL,
        PNL_NOI_LABEL,
    )


def summary_sections() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The deal-summary card's section order and the keys inside each section."""
    return (
        (
            "Identification",
            (
                "deal_name",
                "street_address",
                "city",
                "state",
                "zip",
                "county",
                "msa",
                "address_provenance",
            ),
        ),
        (
            "Property",
            (
                "product_type",
                "structure",
                "units_total",
                "year_built",
                "product_detail",
                "construction_status",
                "delivery_date",
            ),
        ),
        ("Parties", ("seller_entity", "listing_brokerage", "deal_status")),
        ("Pricing (illustrative)", ("pricing_guidance",)),
        (
            "Rents",
            ("in_place_rent_avg", "market_rent_for_model", "fair_market_value"),
        ),
        (
            "Taxes",
            (
                "tax_rate_as_billed",
                "assessed_value_basis",
                "assessment_ratio",
                "tax_divisor",
                "tax_exemption",
            ),
        ),
        (
            "Schools",
            (
                "school_elementary_name",
                "school_elementary_rating",
                "school_middle_name",
                "school_middle_rating",
                "school_high_name",
                "school_high_rating",
            ),
        ),
        ("Notes", ("notes",)),
    )


SUMMARY_LABELS: dict[str, str] = {
    "deal_name": "Deal name",
    "street_address": "Street address",
    "city": "City",
    "state": "State",
    "zip": "ZIP",
    "county": "County",
    "msa": "MSA",
    "address_provenance": "Address resolved from",
    "product_type": "Product type",
    "structure": "Structure",
    "units_total": "Unit count",
    "year_built": "Year built",
    "product_detail": "Product detail",
    "construction_status": "Construction status",
    "delivery_date": "Delivery / CO date",
    "seller_entity": "Seller entity",
    "listing_brokerage": "Listing brokerage",
    "deal_status": "Deal status",
    "pricing_guidance": "Pricing guidance",
    "in_place_rent_avg": "In-place rent (avg)",
    "market_rent_for_model": "Market rent for model",
    "fair_market_value": "Fair market value",
    "tax_rate_as_billed": "Tax rate as billed",
    "assessed_value_basis": "Assessed value basis",
    "assessment_ratio": "Assessment ratio",
    "tax_divisor": "Tax divisor",
    "tax_exemption": "Tax exemption",
    "tax_statement_amount": "Property taxes per the statement (T-12)",
    "tax_per_unit_flag": "Per-unit sanity band",
    "tax_rate_year": "Tax year of the rate",
    "school_elementary_name": "Elementary school",
    "school_elementary_rating": "Elementary rating",
    "school_middle_name": "Middle school",
    "school_middle_rating": "Middle rating",
    "school_high_name": "High school",
    "school_high_rating": "High rating",
    "notes": "Notes",
}


def summary_label(key: str) -> str:
    return SUMMARY_LABELS.get(key, key.replace("_", " ").capitalize())


__all__ = [
    "TOOLKIT_NAME",
    "T12_REVENUE_CATEGORIES",
    "T12_EXPENSE_CATEGORIES",
    "ASSIGNABLE_CATEGORIES",
    "CATEGORY_ALIASES",
    "normalize_category",
    "BREAKOUT_CATEGORIES",
    "PNL_TOTAL_OPEX_LABEL",
    "PNL_NOI_LABEL",
    "LEASE_TYPES",
    "TRADE_OUT_COHORTS",
    "SUPPLY_STAGES",
    "SUPPLY_PROJECT_KEYS",
    "SUPPLY_PROJECT_RADIUS_MI",
    "DEMAND_RINGS",
    "COMP_SOURCE",
    "MISSING",
    "SECTIONS",
    "OPTIONAL_SECTIONS",
    "OM_COMPS_KEYS",
    "OM_RENT_COMP_ROW_KEYS",
    "OM_SALE_COMP_ROW_KEYS",
    "OM_COMPS_SOURCE",
    "OM_COMPS_RETAIL_NOTE",
    "empty_om_comps",
    "Field",
    "FIELD_INVENTORY",
    "MANIFEST_FIELDS",
    "OM_COMPS_FIELDS",
    "manifest_fields_for",
    "manifest_value",
    "asserted_value",
    "MANIFEST_COLUMNS",
    "validate",
    "checkable_subtotal_keys",
    "resolve_path",
    "manifest_groups",
    "assert_categories",
    "pnl_row_order",
    "summary_sections",
    "summary_label",
]
