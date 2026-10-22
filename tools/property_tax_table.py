"""Vendored property-tax rates by county, and the per-state rules that turn a rate into a bill.

The upload demo reaches no county portal. Rather than write "not run" across the property-tax
section, it carries a small table: for each state it works in, how the rate is quoted, the
legal assessment ratio, what the assessed basis IS and whether a sale resets it; and for each
county on file, one representative combined rate — the stack of overlapping jurisdictions a
parcel in the county seat's core pays — with the published table it was read from and the tax
year. Nothing here is a statutory or effective rate; every figure is a rate as billed.

Three rules, each of which has produced a wrong number before:

* **The divisor is the rate's unit.** Texas, North Carolina, Nevada, Arizona and Tennessee
  quote a percent per $100 (``2.15``); Georgia, Alabama and Florida quote mills per $1,000
  (``38.282``). Effective rate = millage / 1000 = (rate per $100) / 100. Dividing by the
  wrong one is a silent 10x error, and it is tested for by name.
* **The ratio comes from this table, never from a keyboard.** Texas 100% of market value,
  Georgia 40%, Arizona 10% of the LIMITED value for a class 4.2 rental, Florida just value,
  Alabama 20% for class II.
* **The basis is a state rule.** Arizona's limited property value rises at most 5% a year and
  does NOT reset on sale, so pricing tax off the purchase price overstates the bill several
  times over; California resets to the price; Texas, Georgia and Florida follow the market.
  The rule is stated on the output.

A county that is not here reads MISSING with a note saying how many are, as of when. A rate
that could not be read from a primary published table was left out rather than guessed:
Denton and Paulding are absent for exactly that reason.

State rules are on file for AZ, TX, GA, FL, AL, NV, NC, TN and CA; county rates for the
twelve counties below.

``tools/test_property_tax_table.py`` pins the ratios, divisors and reset rules as data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import provenance as pv
import schema
from closing_costs import ASSERTED, NOT_ASSERTED

#: When the table below was assembled and each source read.
VERIFIED_AS_OF = "2026-09-04"

#: What the provenance trail calls this table.
SOURCE_DOC = "Vendored county rate table"

#: Per-unit annual tax at or above this is almost certainly a divisor-class error (a
#: millage divided by 100, or a wrong ratio); at or below the low flag it is the mirror
#: image (a per-$100 rate divided by 1,000, or an effective decimal entered as a rate).
#: No real US apartment deal pays either. Same thresholds as the shared pack.
RED_FLAG_PER_UNIT = 10_000
LOW_FLAG_PER_UNIT = 150

@dataclass(frozen=True)
class StateRule:
    """How one state quotes, assesses and re-bases property tax."""

    code: str
    name: str
    divisor: int
    ratio: float
    ratio_label: str
    ratio_citation: str
    basis_label: str
    resets_on_sale: bool
    reset_note: str
    rate_note: str
    band: tuple[int, int] | None
    #: Florida only: assessed (just) value typically lands at this share of the price, and
    #: paying in November earns a 4% discount. Both are the operator's standing convention
    #: and both are stated on the output; everywhere else they are 1.0.
    basis_factor: float = 1.0
    bill_discount: float = 1.0
    convention_note: str | None = None
    #: Where a state taxes ONE of several values an assessor prints, the canonical label(s)
    #: (see ``basis_value_labels``) a stated figure must carry to be the basis. Arizona
    #: prints a full cash value AND a limited property value and taxes the LPV.
    #: None: every full-value label is the basis, as before.
    basis_terms: tuple[str, ...] | None = None

    @property
    def rate_unit(self) -> str:
        return rate_unit(self.divisor)


#: The labels an assessor's page prints against a value, longest first, each mapped to one
#: canonical name. The full-value family is a basis; the post-ratio family never is.
_VALUE_LABELS: tuple[tuple[str, str], ...] = (
    (r"limited\s+property\s+value|\bLPV\b", "limited property value"),
    (r"full\s+cash\s+value|\bFCV\b", "full cash value"),
    (r"net\s+taxable\s+value|taxable\s+value", "taxable value"),
    (r"assessed\s+value", "assessed value"),
    (r"fair\s+(?:and\s+reasonable\s+)?market\s+value", "market value"),
    (r"appraised\s+(?:market\s+|true\s+)?value", "appraised value"),
    (r"market\s+value", "market value"),
    (r"just\s+value", "just value"),
    (r"true\s+value", "true value"),
)
_LABEL_RE = re.compile("|".join(f"(?P<l{i}>{pat})" for i, (pat, _n) in enumerate(_VALUE_LABELS)), re.I)
#: FULL (pre-ratio) values; "assessed" and "taxable" values are post-ratio.
FULL_VALUE_LABELS: frozenset = frozenset(
    name for _p, name in _VALUE_LABELS if name not in ("assessed value", "taxable value"))
#: A figure in a quote: "$5,120,000", "5,120,000", "5120000", "$6.85 million". Only a
#: MONEY-LIKE figure (a $, thousands separators, a "million", or five digits or more) ends
#: one figure's label span; a year ("2025") or a class number ("Class 4") never does.
_AMOUNT_RE = re.compile(r"(?P<d>\$\s?)?(?P<n>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<m>\s*(?:million|mm|m|thousand|k)\b)?", re.I)


def basis_value_labels(value: Any, quote: str) -> frozenset:
    """The canonical labels printed AGAINST ``value`` in ``quote`` — one per occurrence.

    An occurrence's label is the one value label between the previous money figure (or the
    quote's start) and it: "Full Cash Value: $6,850,000  Limited Property Value: $5,120,000"
    labels each figure with its own name. A label elsewhere in the quote is not evidence
    for a figure; two different labels before one figure —
    a table header over its row — are ambiguous and give nothing. Every occurrence counts,
    so an identical FCV printed first does not hide the LPV beside it. Empty when the
    figure is not in the quote or no occurrence has an unambiguous label.
    """
    try:
        # A model or the mock may hand the figure over as printed text ("5,120,000").
        target = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return frozenset()
    found: set[str] = set()
    previous_end = 0
    for match in _AMOUNT_RE.finditer(quote or ""):
        raw = match.group("n")
        try:
            number = float(raw.replace(",", ""))
        except ValueError:
            continue
        suffix = (match.group("m") or "").strip().lower()
        if suffix:
            number *= 1_000 if suffix in ("thousand", "k") else 1_000_000
        if abs(number - target) < 0.5:
            labels = {_VALUE_LABELS[int(next(k for k, v in lab.groupdict().items() if v is not None)[1:])][1]
                      for lab in _LABEL_RE.finditer(quote[previous_end:match.start()])}
            if len(labels) == 1:
                found |= labels
        if match.group("d") or suffix or "," in raw or len(raw.split(".")[0]) >= 5:
            previous_end = match.end()
    return frozenset(found)


_SUM_NOTE = "Sum of the overlapping jurisdictions' adopted rates, as billed."

STATE_RULES: dict[str, StateRule] = {
    "AZ": StateRule(
        code="AZ", name="Arizona", divisor=100, ratio=0.10,
        ratio_label="10% — legal class 4.2, residential rental",
        ratio_citation="A.R.S. sec. 42-15003 (class four assessed at 10%); A.R.S. sec. 42-12004 "
                       "(class four includes residential rental property)",
        basis_label="Limited property value (LPV), per the county assessor",
        basis_terms=("limited property value",),
        resets_on_sale=False,
        reset_note=(
            "Arizona taxes the limited property value, not the price. Under Proposition 117 "
            "(Ariz. Const. art. IX sec. 18; A.R.S. sec. 42-13301) the LPV rises at most 5% a "
            "year and does not reset on sale, so a buyer inherits the seller's basis and tax "
            "priced off the purchase price overstates the bill — roughly threefold on the "
            "showcase parcel. With no assessor value on file, no year-one estimate is "
            "asserted. The rate table's 'approx residential primary' column is the class-3 "
            "owner-occupied rebate (A.R.S. sec. 15-972) and does not apply to a rental; the "
            "full primary plus secondary rate is the one billed."
        ),
        rate_note="Primary plus secondary, as billed.",
        band=(400, 2600),
    ),
    "TX": StateRule(
        code="TX", name="Texas", divisor=100, ratio=1.00,
        ratio_label="100% of appraised market value",
        ratio_citation="Tex. Tax Code sec. 23.01(a) (taxable property appraised at market value "
                       "as of January 1); Tex. Const. art. VIII sec. 1(b)",
        basis_label="Appraised market value, per the county appraisal district",
        resets_on_sale=True,
        reset_note=(
            "Texas reappraises at 100% of market value every January 1 with no cap on "
            "non-homestead property (Tex. Tax Code sec. 23.01; the sec. 23.23 cap is for "
            "homesteads), so after a sale the appraisal district moves the value toward the "
            "price. The year-one estimate prices tax off the stated price."
        ),
        rate_note=_SUM_NOTE,
        band=(2500, 9000),
    ),
    "GA": StateRule(
        code="GA", name="Georgia", divisor=1000, ratio=0.40,
        ratio_label="40% of fair market value",
        ratio_citation="O.C.G.A. sec. 48-5-7(a) (taxable tangible property assessed at 40% of "
                       "fair market value)",
        basis_label="Fair market value, per the county board of assessors",
        resets_on_sale=True,
        reset_note=(
            "Georgia assesses at 40% of fair market value (O.C.G.A. sec. 48-5-7) and the "
            "assessors' value follows the market; the statewide homestead cap (HB 581, 2024) "
            "applies to homesteads only, not to a rental. After a sale the fair market value "
            "moves toward the price, so the year-one estimate prices tax off the stated price."
        ),
        rate_note=_SUM_NOTE,
        band=(2500, 8000),
    ),
    "FL": StateRule(
        code="FL", name="Florida", divisor=1000, ratio=1.00,
        ratio_label="100% of just value",
        ratio_citation="Fla. Stat. sec. 193.011 (just valuation); Fla. Const. art. VII sec. 4",
        basis_label="Just (market) value, per the county property appraiser",
        resets_on_sale=True,
        reset_note=(
            "Florida assesses at just value (Fla. Stat. sec. 193.011). Save Our Homes caps "
            "homesteads only (Fla. Const. art. VII sec. 4(d)); the 10% non-homestead cap "
            "(sec. 4(g)) is lifted when the property changes hands (Fla. Stat. sec. "
            "193.1555(5)), so after a sale the assessed value resets to just value. Two "
            "conventions carried from the operator's practice sit OUTSIDE the ratio and are "
            "stated where applied: assessed value typically lands at about 85% of the price, "
            "and paying in November earns a 4% discount (Fla. Stat. sec. 197.162)."
        ),
        rate_note=_SUM_NOTE,
        band=(2500, 8000),
        basis_factor=0.85,
        bill_discount=0.96,
        convention_note=(
            "Florida convention: assessed value at 85% of the price (a valuation practice, "
            "multiplies the basis) and the 4% November early-payment discount (multiplies "
            "the bill). The legal ratio stays 100% and never absorbs them."
        ),
    ),
    "AL": StateRule(
        code="AL", name="Alabama", divisor=1000, ratio=0.20,
        ratio_label="20% of fair and reasonable market value — class II",
        ratio_citation="Ala. Code sec. 40-8-1(a) (class II property assessed at 20%)",
        basis_label="Fair and reasonable market value, per the county revenue commissioner",
        resets_on_sale=True,
        reset_note=(
            "Alabama assesses rental residential property as class II at 20% of market value "
            "(Ala. Code sec. 40-8-1); the 10% class III rate is for owner-occupied homes. "
            "Values follow the market, so after a sale the assessed value moves toward the "
            "price and the year-one estimate prices tax off the stated price."
        ),
        rate_note=_SUM_NOTE,
        band=(1000, 5000),
    ),
    "NV": StateRule(
        code="NV", name="Nevada", divisor=100, ratio=0.35,
        ratio_label="35% of taxable value",
        ratio_citation="NRS 361.225 (assessed valuation is 35% of taxable value)",
        basis_label="Taxable value (replacement cost less depreciation, plus land), per the county assessor",
        resets_on_sale=True,
        reset_note=(
            "Nevada assesses at 35% of taxable value (NRS 361.225), which the assessor "
            "recomputes each year from replacement cost less depreciation plus land at market; "
            "a sale does not itself reset it, and the partial abatement (NRS 361.4722, an 8% "
            "cap for a rental) travels with the property. The year-one estimate takes the price "
            "as the proxy for taxable value — the toolkit's standing Nevada convention — and new "
            "construction pays the full rate in year one before any cap applies."
        ),
        rate_note=_SUM_NOTE,
        band=(1000, 5000),
    ),
    "NC": StateRule(
        code="NC", name="North Carolina", divisor=100, ratio=1.00,
        ratio_label="100% of appraised true value",
        ratio_citation="N.C. Gen. Stat. sec. 105-283 (appraised at true value in money); "
                       "sec. 105-284 (assessed at 100% of appraised value)",
        basis_label="Appraised true value, per the county assessor",
        resets_on_sale=True,
        reset_note=(
            "North Carolina appraises real property at 100% of true value (N.C. Gen. Stat. "
            "sec. 105-283) on a county reappraisal cycle of at most eight years (sec. 105-286); "
            "a sale does not itself trigger reassessment and there is no cap for a rental, so "
            "the year-one estimate takes the price as the value the next reappraisal reaches."
        ),
        rate_note=_SUM_NOTE,
        band=(1500, 7000),
    ),
    "TN": StateRule(
        code="TN", name="Tennessee", divisor=100, ratio=0.40,
        ratio_label="40% of appraised value — commercial classification (a multi-unit rental)",
        ratio_citation="Tenn. Const. art. II sec. 28 and Tenn. Code Ann. sec. 67-5-801 (industrial "
                       "and commercial real property assessed at 40%); sec. 67-5-501 (residential "
                       "property holds no more than one rental unit, so a multi-unit rental is "
                       "commercial)",
        basis_label="Appraised market value, per the county assessor of property",
        resets_on_sale=True,
        reset_note=(
            "Tennessee assesses commercial real property, which includes a multi-unit rental, at "
            "40% of appraised value (Tenn. Code Ann. sec. 67-5-801) on a county reappraisal cycle "
            "of four to six years (sec. 67-5-1601); a sale does not itself trigger reassessment "
            "and there is no cap, so the year-one estimate takes the price as the value the next "
            "reappraisal reaches."
        ),
        rate_note=_SUM_NOTE,
        band=None,
    ),
    "CA": StateRule(
        code="CA", name="California", divisor=100, ratio=1.00,
        ratio_label="100% of the base-year value (full cash value)",
        ratio_citation="Cal. Const. art. XIII A secs. 1-2 (1% of full cash value plus "
                       "voter-approved debt; full cash value is the purchase price on a change "
                       "of ownership, then at most 2% a year); Cal. Rev. & Tax. Code sec. 110.1",
        basis_label="Base-year value under Proposition 13 — the purchase price on a change of ownership",
        resets_on_sale=True,
        reset_note=(
            "California is the opposite of Arizona: under Proposition 13 a change of ownership "
            "RESETS the base-year value to the purchase price (Cal. Const. art. XIII A sec. 2), "
            "so a long-held seller's small bill is not inherited. The buyer pays about 1% plus "
            "local voter-approved debt on the price, then at most 2% a year growth. Flat direct "
            "charges and special assessments on the bill do not scale with price and sit on top."
        ),
        rate_note="One percent plus local voter-approved debt, as billed.",
        band=None,
    ),
}


#: What the basis does on a sale, per state — DERIVED from the state rules above, so no
#: state can carry a reset status without the divisor, ratio and basis that go with it
#:. ``compute.tax_underwriting`` reads this dict; a state absent here
#: renders "not vendored" and asserts no year-one estimate.
RESETS_ON_SALE: dict[str, bool] = {code: rule.resets_on_sale for code, rule in STATE_RULES.items()}


@dataclass(frozen=True)
class CountyRate:
    """One county's representative combined rate, as billed, with its source."""

    state: str
    county: str
    jurisdiction: str
    tax_year: int
    rate: float
    components: tuple[tuple[str, float], ...]
    citation: str
    source: str
    note: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.state, normalise_county(self.county))


def _entry(*args: Any, **kwargs: Any) -> tuple[tuple[str, str], CountyRate]:
    entry = CountyRate(*args, **kwargs)
    return entry.key, entry


_STATE_NAMES: dict[str, str] = {
    "arizona": "AZ", "texas": "TX", "georgia": "GA", "florida": "FL", "alabama": "AL",
    "california": "CA", "nevada": "NV", "north carolina": "NC", "tennessee": "TN",
}

#: Words a county is written with that are not its name.
_COUNTY_NOISE = re.compile(r"\b(county|co\.?|parish)\b", re.I)


def normalise_state(text: Any) -> str | None:
    """A USPS code for ``text``, or None when it names no state (MISSING included)."""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw or raw == schema.MISSING:
        return None
    if re.fullmatch(r"[A-Za-z]{2}", raw):
        return raw.upper()
    return _STATE_NAMES.get(re.sub(r"\s+", " ", raw.lower()))


def normalise_county(text: Any) -> str:
    """The lookup key for a county: lower-case, without 'County' or punctuation."""
    if not isinstance(text, str):
        return ""
    cleaned = _COUNTY_NOISE.sub(" ", text)
    cleaned = re.sub(r"[^a-z0-9 ]", " ", cleaned.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


#: The rates on file. Each is the sum of the named jurisdictions' adopted rates for the tax
#: year stated, read from the county tax office's, appraisal district's, tax
#: commissioner's or property appraiser's own published table (``source`` is that page or
#: file, without a scheme so the string may travel into an HTML fragment). A parcel's own
#: district decides; every entry says which stack it is.
COUNTY_RATES: dict[tuple[str, str], CountyRate] = dict([
    _entry(
        "AZ", "Maricopa",
        "Tax area code 971300 — City of Phoenix / Deer Valley Unified School District #97",
        2025, 10.2573,
        (("Primary rate", 6.7007), ("Secondary rate", 3.5566)),
        "Maricopa County, '2025 Tax Rates, Maricopa County, Arizona' (the treasurer's rate "
        "table by tax area code), row 971300: primary 6.7007, approx residential primary "
        "5.1401, secondary 3.5566",
        "maricopa.gov/5160/Tax-Levy-and-Rates — Tax Rate 2025 PDF",
        note=(
            "Maricopa publishes rates per tax area code and they vary by district: area "
            "591300 (City of Phoenix / Laveen ESD #59) is 15.7387 in the same table. The "
            "parcel's own tax area decides. The 'approx residential primary' column is the "
            "owner-occupied rebate and does not apply to a rental."
        ),
    ),
    _entry(
        "TX", "Harris",
        "City of Houston / Houston ISD, with every county-wide district",
        2025, 2.125220,
        (
            ("Harris County", 0.380960),
            ("Harris County Flood Control District", 0.049660),
            ("Port of Houston Authority", 0.005900),
            ("Harris Health System (hospital district)", 0.187610),
            ("Harris County Department of Education", 0.004798),
            ("Houston Community College", 0.098802),
            ("Houston ISD", 0.878300),
            ("City of Houston", 0.519190),
        ),
        "Harris County Tax Office, Truth-in-Taxation summary of adopted 2025 rates (the five "
        "county entities); Houston ISD, 'Tax Information', rate adopted 2025-10-15; Houston "
        "Community College, 'Truth in Taxation', rate adopted 2025-09-16; City of Houston, "
        "2025 tax-rate notices, $0.519190 per $100 (no change from 2024)",
        "hctax.net/Property/TaxRates; houstonisd.org (Tax Information); hccs.edu "
        "(Truth in Taxation); houstontx.gov/2025-tax-rate-information.html",
        note="A parcel outside the City of Houston or Houston ISD, or inside a MUD, pays a "
             "different stack.",
    ),
    _entry(
        "TX", "Montgomery",
        "City of Conroe / Conroe ISD, with the county-wide districts",
        2025, 1.9071,
        (
            ("Montgomery County", 0.3770),
            ("Montgomery County Hospital District", 0.0473),
            ("Lone Star College", 0.1060),
            ("City of Conroe", 0.4272),
            ("Conroe ISD", 0.9496),
        ),
        "Montgomery County Tax Office, '2025 Tax Rates - 2025 Exemptions for Entities "
        "Collected by Montgomery County Tax Office'",
        "mctotx.org/tax_rate_history — 2025 Rates-2025 Exemptions PDF",
        note="A parcel in a MUD or outside Conroe pays a different stack; MUD rates in the "
             "same table run from 0.15 to 1.45 per $100 on top.",
    ),
    _entry(
        "TX", "Fort Bend",
        "City of Sugar Land / Fort Bend ISD, with the county and its drainage district",
        2025, 1.837727,
        (
            ("Fort Bend County general fund", 0.412000),
            ("Fort Bend County Drainage District", 0.010000),
            ("City of Sugar Land", 0.358827),
            ("Fort Bend ISD", 1.056900),
        ),
        "Fort Bend County Tax Assessor-Collector, '2025 Tax Rate and Exemption worksheet' "
        "(General Fund 0.412000, Drainage 0.010000, Sugar Land 0.358827); Fort Bend ISD Board "
        "of Trustees, 2025-09-15 meeting agenda — resolution adopting a combined ad valorem tax "
        "rate of $1.0569 per $100 of valuation for the 2025 tax year (M&O 0.7869, I&S 0.2700)",
        "fortbendcountytx.gov — 2025-Tax-Rate-and-Exemption-worksheet.pdf; "
        "fortbendisd.new.swagit.com/videos/355455 (board agenda)",
        note="The county worksheet's school-district rows did not extract cleanly, so the ISD "
             "rate is taken from the district's own adopting resolution. A parcel in a MUD or "
             "LID, or in Katy ISD or Lamar CISD, pays a different stack; Houston Community "
             "College taxes only its Stafford and Missouri City areas.",
    ),
    _entry(
        "TX", "Dallas",
        "City of Dallas / Dallas ISD, with the county-wide districts",
        2025, 2.226710,
        (
            ("Dallas County", 0.215500),
            ("Parkland Hospital District", 0.212000),
            ("Dallas College", 0.106575),
            ("City of Dallas", 0.698800),
            ("Dallas ISD", 0.993835),
        ),
        "Dallas Central Appraisal District, '2025 Ad Valorem Tax Rates for Dallas County', "
        "revised 2025-11-05",
        "dallascad.org — 2025TaxRate.pdf",
    ),
    _entry(
        "TX", "Tarrant",
        "City of Fort Worth / Fort Worth ISD, with the county-wide districts",
        2025, 2.162580,
        (
            ("Tarrant County", 0.186200),
            ("Tarrant County Hospital District (JPS)", 0.165000),
            ("Tarrant County College District", 0.112280),
            ("City of Fort Worth", 0.670000),
            ("Fort Worth ISD", 1.029100),
        ),
        "Tarrant Appraisal District, '2025 Tax Rates per $100 Valuation for Tarrant County'",
        "tad.org/content/rates/2025TaxRates.pdf",
    ),
    _entry(
        "TX", "Collin",
        "City of Plano / Plano ISD, with the county-wide districts",
        2025, 1.707713,
        (
            ("Collin County", 0.149343),
            ("Collin College", 0.081220),
            ("City of Plano", 0.437600),
            ("Plano ISD", 1.039550),
        ),
        "Collin County Tax Assessor-Collector, '2025 Tax Rates and Exemptions by Jurisdictions'",
        "collincountytx.gov — taxratesummary.pdf",
    ),
    _entry(
        "GA", "Gwinnett",
        "Unincorporated Gwinnett County",
        2026, 34.86,
        (
            ("Gwinnett County general fund", 6.95),
            ("Gwinnett County Public Schools M&O", 18.70),
            ("School bond", 1.45),
            ("Recreation", 1.00),
            ("Fire and EMS", 3.20),
            ("Police", 2.90),
            ("Development and code enforcement", 0.36),
            ("Economic development", 0.30),
        ),
        "Gwinnett County Tax Commissioner, 'Millage Rates' — 2026 millage rates, row "
        "'Unincorporated Gwinnett' (page updated 2026-09-04); the 2025 unincorporated total "
        "was also 34.86",
        "gwinnetttaxcommissioner.com/property-tax/billing-information/millage-rates",
        note="A parcel inside a city pays that city's millage instead of the county's fire, "
             "police and development levies.",
    ),
    _entry(
        "GA", "Fulton",
        "City of Atlanta",
        2025, 40.909,
        (
            ("City of Atlanta millage as the tax commissioner's table lists it (city and "
             "Atlanta Public Schools levies are billed together)", 31.870),
            ("Fulton County millage inside the City of Atlanta", 9.039),
        ),
        "Fulton County Tax Commissioner, 'Millage Rates Through 2025', 2025 row for the City "
        "of Atlanta (31.870 city, 9.039 county, 40.909 total)",
        "fultoncountytaxes.org/media/Millage_Rates_Through_2025.pdf",
        note="Other Fulton districts differ: Union City totals 37.645 in the same table "
             "(9.039 county / 17.080 schools / 11.526 city). The tax district on the bill "
             "decides, not the mailing city.",
    ),
    _entry(
        "GA", "Cobb",
        "Unincorporated Cobb County (tax commissioner row 'County (9)')",
        2026, 30.13,
        (
            ("Cobb County general fund", 8.46),
            ("Cobb County fire", 2.97),
            ("Cobb County School District general", 18.70),
        ),
        "Cobb County Tax Commissioner, '2026 Millage Rates of Cobb County Bills', row "
        "'County (9)': 8.46 general, 2.97 fire, 18.70 school, 30.13 total; the 2025 table "
        "shows the same three figures",
        "cobbtax.gov/property/millage_rates.php — 2026 Millage Rates PDF",
        note="Marietta has its own school system (17.97) and its own general levy; the "
             "other cities add a city millage on top of the county bill rate.",
    ),
    _entry(
        "FL", "Hillsborough",
        "Unincorporated Hillsborough County",
        2025, 18.2515,
        (
            ("Hillsborough County general revenue", 5.4608),
            ("Environmentally sensitive lands", 0.0604),
            ("Library services", 0.5583),
            ("County MSTU (unincorporated services)", 4.6163),
            ("School board — state required local effort", 3.0920),
            ("School board — local (discretionary, capital, additional)", 3.2480),
            ("Independent special districts (port, transit, Children's Board, water "
             "management district)", 1.2157),
        ),
        "Hillsborough County Property Appraiser, 'Final 2025' millage by taxing district, "
        "row 'Unincorporated': total millage 18.2515 (City of Tampa 19.8428, Temple Terrace "
        "19.5319, Plant City 18.2926 in the same document)",
        "hcpafl.org — 2025FinalMillage.pdf",
        note="A parcel inside Tampa pays the city's 6.2076 instead of the county MSTU's "
             "4.6163: 19.8428 total.",
    ),
    _entry(
        "FL", "Pasco",
        "Unincorporated Pasco County with fire-rescue service (taxing area 'Uninc. Pasco - Fire')",
        2025, 16.81690,
        (
            ("Pasco County operating", 7.40420),
            ("County voted debt (fire, jail, libraries, parks bonds)", 0.21880),
            ("Mosquito control", 0.20910),
            ("County roads MSTU", 0.40520),
            ("Fire-rescue municipal service district", 2.12250),
            ("School board — state law", 3.02600),
            ("School board — local (additional operating, capital, discretionary)", 3.24800),
            ("Southwest Florida Water Management District", 0.18310),
        ),
        "Pasco County Property Appraiser, 'Taxing Authorities and Districts', 2025 millage "
        "certified 2025-10-06, area 'Uninc. Pasco - Fire' total 16.81690 ('Uninc. Pasco' "
        "without fire service 14.69440)",
        "search.pascopa.com/millage.aspx",
        note="The unincorporated area outside the fire district totals 14.69440; the six "
             "cities run from 17.2782 to 23.0325.",
    ),
])

@dataclass(frozen=True)
class Lookup:
    """What the table knows about one (state, county)."""

    state: str | None
    state_text: str
    county_text: str
    rule: StateRule | None
    entry: CountyRate | None

    @property
    def rate_status(self) -> str:
        return ASSERTED if self.entry is not None else NOT_ASSERTED

    @property
    def county_label(self) -> str:
        county = self.county_text if self.county_text and self.county_text != schema.MISSING else None
        state = self.state or (self.state_text if self.state_text != schema.MISSING else None)
        if county and state:
            return f"{county}, {state}"
        return county or state or "this deal"


def lookup(state: Any, county: Any) -> Lookup:
    code = normalise_state(state)
    rule = STATE_RULES.get(code) if code else None
    entry = COUNTY_RATES.get((code, normalise_county(county))) if code else None
    return Lookup(
        state=code,
        state_text=str(state or "").strip(),
        county_text=str(county or "").strip(),
        rule=rule,
        entry=entry,
    )


def coverage() -> tuple[int, str]:
    """How many counties carry a rate, and as of when."""
    return len(COUNTY_RATES), VERIFIED_AS_OF


# --------------------------------------------------------------------------------------
# Arithmetic
# --------------------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def rate_unit(divisor: Any) -> str:
    """The unit a rate with this divisor is quoted in, for a label."""
    d = _num(divisor)
    if d == 100:
        return "per $100"
    if d == 1000:
        return "mills per $1,000"
    if d is None or d <= 0:
        return "per (divisor MISSING)"
    return f"per ${d:,.0f}"


def effective_rate(rate: Any, divisor: Any) -> float | None:
    """rate / divisor — the same 2.15% whether written 2.15 per $100 or 21.5 mills."""
    r, d = _num(rate), _num(divisor)
    if r is None or d is None or d <= 0:
        return None
    return r / d


def annual_tax(basis: Any, ratio: Any, rate: Any, divisor: Any, exemption: Any = 0.0) -> float | None:
    """basis x ratio x rate / divisor, less an exemption; None when any input is not a number.

    The basis is a FULL value (market value, LPV, price) — never an already-assessed one;
    applying the ratio twice is one of the two bugs the showcase caught. Rounded to cents,
    because it is checked against a bill to the cent.
    """
    b, ra, r, d = _num(basis), _num(ratio), _num(rate), _num(divisor)
    if None in (b, ra, r, d) or d <= 0:
        return None
    ex = _num(exemption) or 0.0
    return round(max(0.0, b * ra * r / d - ex), 2)


def band_check(per_unit: Any, state: Any) -> tuple[str, str]:
    """(severity, message) for an annual per-unit tax: ok / warn / error / unchecked.

    ``error`` is a divisor-class flag (>= $10,000 or <= $150 a unit), ``warn`` is outside the
    state's typical band. Neither changes a number: the band is calibrated on older stock
    and a new-build in Arizona legitimately trips it, so the message asks for a check.
    """
    t = _num(per_unit)
    code = normalise_state(state)
    if t is None:
        return "unchecked", "no per-unit figure to check"
    if t <= 0:
        return "unchecked", f"a per-unit figure of ${t:,.0f} is not a bill"
    rule = STATE_RULES.get(code) if code else None
    band_text = (
        f" The {code} band is ${rule.band[0]:,}–${rule.band[1]:,}."
        if rule is not None and rule.band is not None else ""
    )
    if t >= RED_FLAG_PER_UNIT:
        return "error", (
            f"${t:,.0f} per unit a year is at or above ${RED_FLAG_PER_UNIT:,} — almost certainly "
            f"a divisor error (a millage divided by 100) or a wrong assessment ratio. Check "
            f"the rate, its unit and the ratio against the bill or the statement's tax line."
            + band_text
        )
    if t <= LOW_FLAG_PER_UNIT:
        return "error", (
            f"${t:,.0f} per unit a year is at or below ${LOW_FLAG_PER_UNIT:,} — almost certainly "
            f"a divisor error (a per-$100 rate divided by 1,000) or a rate entered as an "
            f"effective decimal. Check the rate, its unit and the ratio." + band_text
        )
    if rule is None or rule.band is None:
        return "unchecked", (
            f"no per-unit band is on file for {code or 'this state'}; only the "
            f">= ${RED_FLAG_PER_UNIT:,} and <= ${LOW_FLAG_PER_UNIT:,} red flags ran"
        )
    low, high = rule.band
    if low <= t <= high:
        return "ok", f"${t:,.0f} per unit a year is inside the {code} band of ${low:,}–${high:,}"
    return "warn", (
        f"${t:,.0f} per unit a year is outside the {code} band of ${low:,}–${high:,} — "
        f"double-check the rate, its unit and the assessment ratio against the bill or the "
        f"statement. The band is a typical range, not a rule: a new-build can sit above it."
    )


def band_flag_text(per_unit: Any, state: Any, source_label: str) -> str:
    """One sentence for the output: what was checked, against what, with what result."""
    severity, message = band_check(per_unit, state)
    if severity == "unchecked":
        return f"Per-unit sanity band NOT CHECKED — {message}."
    head = {"ok": "In band", "warn": "OUTSIDE the band", "error": "RED FLAG"}[severity]
    return f"{head} ({source_label}): {message}"


# --------------------------------------------------------------------------------------
# What the table puts on a deal
# --------------------------------------------------------------------------------------


NO_PORTAL_NOTE = (
    "The upload demo reaches no county portal: the parcel, tax area code, assessor's value "
    "and billed amount are not verified against one, and read MISSING unless an uploaded "
    "document stated them. The rate on file is a representative district stack for the "
    "county, not the parcel's own."
)

NO_STATE_NOTE = "no state rules on file for {state}"


def evidence_text(found: Lookup) -> str:
    """What was read and from where — or, honestly, what was not."""
    n, as_of = coverage()
    if found.rule is None:
        state = found.state or (found.state_text if found.state_text and found.state_text != schema.MISSING else None)
        where = f"{state}" if state else "this deal (no state was stated)"
        rules = ", ".join(sorted(STATE_RULES))
        return (
            f"No rate on file: {NO_STATE_NOTE.format(state=where)}. The demo carries state rules "
            f"for {rules} and rates for {n} counties as of {as_of}. {NO_PORTAL_NOTE}"
        )
    if found.entry is None:
        county = found.county_text if found.county_text and found.county_text != schema.MISSING else None
        if county:
            head = (f"No rate on file for {county}, {found.state}; the demo carries rates for "
                    f"{n} counties as of {as_of}.")
        else:
            head = (f"No county was stated, so no rate is on file; the demo carries rates for "
                    f"{n} counties as of {as_of}.")
        return (
            f"{head} The {found.state} rules still apply: the rate is quoted {found.rule.rate_unit} "
            f"of assessed value, the ratio is {found.rule.ratio_label} ({found.rule.ratio_citation}). "
            f"{NO_PORTAL_NOTE}"
        )
    e = found.entry
    parts = "; ".join(f"{name} {rate:g}" for name, rate in e.components)
    return (
        f"{SOURCE_DOC}: {e.rate:g} {found.rule.rate_unit} of assessed value, tax year {e.tax_year} — "
        f"{e.jurisdiction}: {parts}. Source: {e.citation} ({e.source}). Verified {VERIFIED_AS_OF}. "
        f"The parcel's own tax district decides; this is the representative stack for the county"
        + (f". {e.note}" if e.note else ".")
        + f" Ratio {found.rule.ratio_label} ({found.rule.ratio_citation}). {NO_PORTAL_NOTE}"
    )


def summary_values(found: Lookup) -> dict[str, Any]:
    """The ``summary.*`` values the table asserts — MISSING for anything it does not."""
    rule, entry = found.rule, found.entry
    values: dict[str, Any] = {
        "tax_rate_as_billed": entry.rate if entry is not None else schema.MISSING,
        "tax_divisor": rule.divisor if rule is not None else schema.MISSING,
        "assessment_ratio": rule.ratio if rule is not None else schema.MISSING,
        "tax_basis_label": rule.basis_label if rule is not None else schema.MISSING,
        "tax_basis_note": (
            rule.reset_note + (f" {rule.convention_note}" if rule.convention_note else "")
            if rule is not None else schema.MISSING
        ),
        "tax_rate_evidence": evidence_text(found),
    }
    return values


#: Summary paths the table speaks to, and the ones a portal would have filled.
TABLE_PATHS: tuple[str, ...] = (
    "summary.tax_rate_as_billed",
    "summary.tax_divisor",
    "summary.assessment_ratio",
    "summary.tax_basis_label",
    "summary.tax_basis_note",
    "summary.tax_rate_evidence",
)
PORTAL_PATHS: tuple[str, ...] = (
    "summary.tax_parcel_id",
    "summary.tax_area_code",
    "summary.assessed_value_basis",
    "summary.tax_billed_amount",
    "summary.tax_billed_year",
    "summary.tax_exemption",
)


def provenance_stamps(found: Lookup) -> dict[str, dict]:
    """A stamp per tax path: read from the table entry, from the state rule, or absent with the reason."""
    stamps: dict[str, dict] = {}
    rule, entry = found.rule, found.entry
    if entry is not None:
        locator = f"property_tax_table.COUNTY_RATES[({entry.state!r}, {normalise_county(entry.county)!r})]"
        stamps["summary.tax_rate_as_billed"] = pv.read(
            SOURCE_DOC, locator,
            note=f"{entry.jurisdiction}, tax year {entry.tax_year}. {entry.citation}. Verified {VERIFIED_AS_OF}.",
        )
    else:
        stamps["summary.tax_rate_as_billed"] = pv.read(
            SOURCE_DOC, "property_tax_table.COUNTY_RATES",
            note=evidence_text(found).split(" The ", 1)[0],
        )
    if rule is not None:
        locator = f"property_tax_table.STATE_RULES[{rule.code!r}]"
        stamps["summary.tax_divisor"] = pv.read(
            SOURCE_DOC, locator, note=f"The rate's unit in {rule.name}: {rule.rate_unit} of assessed value.")
        stamps["summary.assessment_ratio"] = pv.read(
            SOURCE_DOC, locator, note=f"{rule.ratio_label}. {rule.ratio_citation}.")
        stamps["summary.tax_basis_label"] = pv.read(SOURCE_DOC, locator, note="What the assessed basis is in this state.")
        stamps["summary.tax_basis_note"] = pv.read(SOURCE_DOC, locator, note="How the basis behaves on a sale, with the authority.")
    else:
        for path in ("summary.tax_divisor", "summary.assessment_ratio",
                     "summary.tax_basis_label", "summary.tax_basis_note"):
            stamps[path] = pv.read(
                SOURCE_DOC, "property_tax_table.STATE_RULES",
                note=f"MISSING: {NO_STATE_NOTE.format(state=found.state or 'the state stated')}.",
            )
    stamps["summary.tax_rate_evidence"] = pv.derived(
        SOURCE_DOC, "property_tax_table.evidence_text",
        ["summary.state", "summary.county"],
        note="The citation, tax year and jurisdiction stack behind the rate, or the honest note that none is on file.",
    )
    # The parcel, tax district, assessed value, exemption and billed amount are read off the
    # county assessor's portal by the internal pipeline only (schema.INTERNAL_ONLY, 2026-09-10);
    # here they are MISSING under that label, with the reason, on the sample and on an upload.
    for path in PORTAL_PATHS:
        stamps[path] = pv.internal_only(path)
    return stamps


__all__ = [
    "VERIFIED_AS_OF", "SOURCE_DOC", "RED_FLAG_PER_UNIT", "LOW_FLAG_PER_UNIT",
    "RESETS_ON_SALE", "StateRule", "STATE_RULES", "CountyRate", "COUNTY_RATES",
    "Lookup", "lookup", "coverage", "normalise_state", "normalise_county",
    "rate_unit", "effective_rate", "annual_tax", "band_check", "band_flag_text",
    "evidence_text", "summary_values", "provenance_stamps",
    "TABLE_PATHS", "PORTAL_PATHS", "NO_PORTAL_NOTE", "ASSERTED", "NOT_ASSERTED",
]
