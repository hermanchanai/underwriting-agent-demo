"""State transfer tax and mortgage/recording tax at an assumed price.

These are financial facts. A wrong rate is worse than an absent one, so this table holds
only rates verified against a primary authority — a state statute, a Department of Revenue
page, or a county recorder's published fee schedule — and every entry carries the citation
and the date it was confirmed. A state that is not in the table is reported as **not
asserted**, never as zero: "no tax here" and "we did not check" are different answers and
the output has to say which one it is.

Two rate shapes cover every state here:

* ``pct`` — a percentage of consideration, expressed as a decimal fraction.
* ``per`` — a dollar amount per unit of consideration, e.g. $1.00 per $500. Rounding is
  the shape that bites: most per-unit statutes round the consideration UP to the next
  whole unit, which is why ``round_up`` exists rather than a plain multiply.
"""

from __future__ import annotations

import math
from typing import Any

VERIFIED_AS_OF = "2026-09-01"

#: How to read the `amount` on a tax line. This is a FIELD because it is a decision, and a
#: decision read out of prose is a decision waiting to break: writers used to detect a
#: not-asserted amount with `citation.startswith("Not asserted")`, so prepending the
#: statute to that same string silently turned "we cannot compute this" into a confident
#: green $0 for a tax that really exists.
ASSERTED = "asserted"            # computed from a verified rate and a real basis
NOT_ASSERTED = "not_asserted"    # the tax may exist; we cannot state the amount
ZERO_BY_LAW = "zero_by_law"      # no such tax here, on cited authority
AMOUNT_STATUSES: tuple[str, ...] = (ASSERTED, NOT_ASSERTED, ZERO_BY_LAW)
NOT_ASSERTED_STATUS = NOT_ASSERTED


class Rate:
    """One tax line for one state."""

    def __init__(
        self,
        applies: bool,
        label: str,
        citation: str,
        *,
        pct: float | None = None,
        per_amount: float | None = None,
        per_unit: float | None = None,
        round_up: bool = True,
        cap: float | None = None,
        exempt_first: float = 0.0,
        min_maturity_months: int | None = None,
        caveat: str | None = None,
        as_of: str = VERIFIED_AS_OF,
    ) -> None:
        self.applies = applies
        self.label = label
        self.citation = citation
        self.pct = pct
        self.per_amount = per_amount
        self.per_unit = per_unit
        self.round_up = round_up
        self.cap = cap
        self.exempt_first = exempt_first
        #: Below this maturity the tax does not attach at all (Georgia's 62-month rule).
        self.min_maturity_months = min_maturity_months
        self.caveat = caveat
        self.as_of = as_of

    def attaches(self, maturity_months: int | None) -> bool:
        if not self.applies:
            return False
        if self.min_maturity_months is None:
            return True
        if maturity_months is None:
            return True  # unknown maturity: assume the tax attaches, the costlier read
        return maturity_months > self.min_maturity_months

    def compute(self, consideration: float, maturity_months: int | None = None) -> float:
        if not self.attaches(maturity_months) or consideration <= 0:
            return 0.0
        base = max(0.0, consideration - self.exempt_first)
        if self.pct is not None:
            amount = base * self.pct
        elif self.per_amount is not None and self.per_unit:
            units = base / self.per_unit
            units = math.ceil(units) if self.round_up else units
            amount = units * self.per_amount
        else:
            return 0.0
        if self.cap is not None:
            amount = min(amount, self.cap)
        return round(amount, 2)

    def as_block(self, consideration: float, maturity_months: int | None = None) -> dict[str, Any]:
        attaches = bool(self.attaches(maturity_months))
        if not self.applies:
            status = NOT_ASSERTED if self.citation == NOT_ASSERTED_TEXT else ZERO_BY_LAW
        elif not attaches:
            # The tax exists but this instrument escapes it (Georgia's 62-month rule).
            status = ZERO_BY_LAW
        else:
            status = ASSERTED
        return {
            "applies": attaches,
            "amount_status": status,
            "rate_label": self.label,
            "amount": self.compute(consideration, maturity_months),
            "citation": self.citation,
            "as_of": self.as_of,
            "caveat": self.caveat,
        }


NOT_ASSERTED_TEXT = "Not asserted — this state is not in the verified rate table."
NO_DEBT_TERMS = (
    "Not asserted — no loan amount was supplied, and this state taxes the debt rather "
    "than the price."
)


def _no_debt_terms(rate: "Rate") -> dict[str, Any]:
    """A mortgage tax that exists, with no loan to compute it on.

    Falling back to the purchase price silently asserts a 100%-LTV acquisition, which is
    not a deal anyone does — on a $60m purchase that overstates Florida by roughly
    $115,000. Same house rule as everywhere else here: say nothing rather than say
    something wrong.

    But the *claim that this state taxes the instrument* is still being made, so the
    authority for it has to travel with the row. Replacing the statute with a generic
    explanation left the workbook asserting a tax with nothing behind it. The citation
    keeps the statute and adds the reason the amount is absent.
    """
    return {
        "applies": False,
        "amount_status": NOT_ASSERTED,
        "rate_label": f"{rate.label} — amount not asserted, no loan supplied",
        "amount": 0,
        "citation": f"{rate.citation} · {NO_DEBT_TERMS}",
        "as_of": rate.as_of,
        "caveat": (
            "This state does impose a tax on the security instrument, on the authority "
            "cited. Supply the loan amount (and, in Georgia, the maturity) to compute it."
            + (f" {rate.caveat}" if rate.caveat else "")
        ),
    }


def _unasserted(kind: str) -> Rate:
    return Rate(
        False,
        f"{kind}: not asserted",
        NOT_ASSERTED_TEXT,
        caveat=(
            "The rate for this state has not been verified against a primary authority, "
            "so nothing is claimed. This is not the same as a rate of zero."
        ),
    )


#: Verified rates, keyed by USPS state code. Every entry was checked against the statute
#: or the state revenue department named in its citation. Nothing here is from a
#: title-company summary or a real-estate blog.
TRANSFER_TAX: dict[str, Rate] = {
    "AZ": Rate(
        False, "No real-estate transfer tax", "Ariz. Const. art. IX, sec. 24 (Prop 100, 2008)",
        caveat="The constitution bars any NEW transfer tax imposed after 2007-12-31; it "
               "does not repeal pre-existing ones, and Arizona had none ad valorem. A flat "
               "$2 deed fee (A.R.S. 11-1132(A)) and a $30-per-instrument recording fee "
               "(A.R.S. 11-475(A)(1)) still apply and are not percentage-based.",
    ),
    "TX": Rate(
        False, "No real-estate transfer tax", "Tex. Const. art. VIII, sec. 29 (added 2015)",
        caveat="The bar is on enacting a transfer tax after 2016-01-01; Texas had none, so "
               "the rate is zero. The same section expressly permits a tax on title "
               "insurance, and Texas title premiums are state-promulgated and material — a "
               "zero here does not mean a cheap closing.",
    ),
    "GA": Rate(
        True, "$1.00 per $1,000 of consideration", "O.C.G.A. sec. 48-6-1",
        per_amount=1.00, per_unit=1000,
        caveat="The seller is statutorily liable, though contracts routinely shift it.",
    ),
    "FL": Rate(
        True, "70c per $100 of consideration", "Fla. Stat. sec. 201.02(1)(a)",
        per_amount=0.70, per_unit=100,
        caveat="Miami-Dade is 60c per $100 plus a 45c surtax on anything that is not a "
               "single-family residence, so commercial there is $1.05 per $100. "
               "Consideration INCLUDES assumed debt, so an assumption is taxed on the full "
               "price, not the equity cheque.",
    ),
    "NV": Rate(
        True, "$2.55 per $500 of value (Clark County)", "NRS 375.020(1), 375.023(1)",
        per_amount=2.55, per_unit=500,
        caveat="Clark County (Las Vegas) rate. Most other counties are $1.95 per $500. "
               "Buyer and seller are JOINTLY AND SEVERALLY liable (NRS 375.030(2)) even "
               "though custom is seller-paid.",
    ),
    "NC": Rate(
        True, "$1.00 per $500 of consideration", "N.C. Gen. Stat. sec. 105-228.30(a)",
        per_amount=1.00, per_unit=500,
        caveat="Paid by the transferor by statute. Seven rural north-eastern coastal "
               "counties add a local land-transfer tax of $1.00 per $100 — five times the "
               "state rate. No metro county (Mecklenburg, Wake, Guilford, Durham) has one.",
    ),
    "TN": Rate(
        True, "$0.37 per $100", "Tenn. Code Ann. sec. 67-4-409(a)",
        per_amount=0.37, per_unit=100,
        caveat="The base is the GREATER of consideration paid or the value of the property, "
               "not simply the price — which matters on a bargain or related-party sale.",
    ),
    "AL": Rate(
        True, "$0.50 per $500 of value", "Ala. Code sec. 40-22-1",
        per_amount=0.50, per_unit=500,
        caveat="Only value in EXCESS of a mortgage already taxed is subject, so the deed "
               "base can be below the headline price on an assumption.",
    ),
}

MORTGAGE_TAX: dict[str, Rate] = {
    "AZ": Rate(
        False, "No mortgage or recording tax on the security instrument",
        "Ariz. Const. art. IX, sec. 24; A.R.S. 11-475(A)(1)",
        caveat="Recording is a flat per-instrument fee, not a tax on the amount borrowed.",
    ),
    "TX": Rate(
        False, "No mortgage tax", "No imposing statute; Tex. Loc. Gov't Code ch. 118",
        caveat="Proven by absence of an imposing statute rather than by an affirmative "
               "constitutional bar, which is a weaker form of proof than Arizona's. "
               "Recording is a flat per-page schedule.",
    ),
    "GA": Rate(
        True, "$1.50 per $500 of the note, capped at $25,000",
        "O.C.G.A. sec. 48-6-60 to 48-6-77; Ga. Comp. R. & Regs. 560-11-8-.02",
        per_amount=1.50, per_unit=500, cap=25_000.0, min_maturity_months=62,
        caveat="HB 586 (2025), effective 2025-07-01, redefined a long-term note as one with "
               "principal due more than 62 months out. A 60-month acquisition loan now "
               "carries NO intangible tax; a 7- or 10-year loan pays 0.30% capped at "
               "$25,000. Maturity is an input here, not a constant.",
    ),
    "FL": Rate(
        True, "35c per $100 doc stamps plus 2 mills intangible = 55c per $100",
        "Fla. Stat. sec. 201.08(1)(b); sec. 199.133(1)",
        pct=0.0055,
        caveat="The $2,450 cap in sec. 201.08(1)(a) applies to the NOTE, not to a recorded "
               "mortgage, which is uncapped. Do not apply a ceiling here.",
    ),
    "NV": Rate(
        False, "No mortgage tax", "NRS 375 taxes conveyances, not security instruments",
        caveat="A deed of trust is a security instrument, not a conveyance; recording is a "
               "flat fee under NRS 247.305.",
    ),
    "NC": Rate(
        False, "No mortgage tax", "No imposing statute; deed-of-trust recording is a flat fee",
    ),
    "TN": Rate(
        True, "11.5c per $100 of indebtedness, first $2,000 exempt",
        "Tenn. Code Ann. sec. 67-4-409(b)",
        per_amount=0.115, per_unit=100, exempt_first=2_000.0,
        caveat="On an INCREASE in indebtedness the $2,000 exclusion is not available. UCC "
               "fixture filings are generally subject to this tax too.",
    ),
    "AL": Rate(
        True, "$0.15 per $100 of indebtedness", "Ala. Code sec. 40-22-2",
        per_amount=0.15, per_unit=100,
        caveat="Alabama charges on both sides: 0.10% on the deed and 0.15% on the loan.",
    ),
}


def for_state(
    state: str,
    price: float,
    loan_amount: float | None = None,
    maturity_months: int | None = None,
) -> dict[str, Any]:
    """The closing-cost block for one state at one price."""
    code = (state or "").strip().upper()
    transfer = TRANSFER_TAX.get(code) or _unasserted("Transfer tax")
    mortgage = MORTGAGE_TAX.get(code) or _unasserted("Mortgage / recording tax")
    # A state with a verified "no such tax" answers $0 whatever the debt looks like. A
    # state that DOES tax the instrument needs the loan; without it, nothing is asserted.
    if mortgage.applies and loan_amount is None:
        mortgage_block = _no_debt_terms(mortgage)
    else:
        mortgage_block = mortgage.as_block(
            loan_amount if loan_amount is not None else 0.0, maturity_months
        )
    return {
        "state": code,
        "price": price,
        "loan_amount": loan_amount,
        "transfer_tax": transfer.as_block(price),
        "mortgage_tax": mortgage_block,
        "note": (
            "Transfer tax is computed on the purchase price. A mortgage or recording tax is "
            "computed on the LOAN AMOUNT, and is reported as not asserted when no loan is "
            "stated — never inferred from the price, which would assume 100% leverage. "
            "Rates are verified against a primary authority and carry the citation and the "
            "date confirmed; a state absent from the table reports as not asserted rather "
            "than as zero."
        ),
    }


def states_covered() -> tuple[str, ...]:
    return tuple(sorted(set(TRANSFER_TAX) | set(MORTGAGE_TAX)))


__all__ = ["Rate", "TRANSFER_TAX", "MORTGAGE_TAX", "for_state", "states_covered",
           "VERIFIED_AS_OF", "NOT_ASSERTED_TEXT", "ASSERTED", "NOT_ASSERTED",
           "ZERO_BY_LAW", "AMOUNT_STATUSES"]
