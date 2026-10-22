"""Checks for the three-tier comp display rule.

Written from the specification, before the rule existed, against the selection behaviour
that preceded it — every rent-carrying community inside the radius became a detail row,
ordered by distance, with no product test and no vintage band. Each check below names the
part of the spec it pins, so a later reader can tell a deliberate rule change from a
regression.

The spec:

* **Tier 1 "Top comps"** — the 5 CLOSEST communities that are (a) the same product type as
  the subject and (b) within +/-10 years of the subject's year built, where a year is
  known. Full detail.
* **Tier 2 "Additional comps"** — the NEXT 15 meeting the same criteria. Full detail.
* **Tier 3 "Also nearby"** — every remaining tracked community inside the radius, identity
  only, each carrying the reason it is not a detail row.
* An **unknown year does not disqualify**. The tracker has no year-built column, so a
  community whose vintage nobody published stays eligible on proximity and product, and
  the tier header says the vintage filter was applied only where a year was known. The
  alternative — dropping it, or inventing a year — either hides the closest comp in the
  submarket or fabricates the evidence the filter runs on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import comp_tiers as ct
from _testkit import Checker, guard


def cand(name, miles, *, product="mf", year=1981, rent=True, exclusion=None):
    return ct.Candidate(
        community=name,
        distance_mi=miles,
        product=product,
        year_built=year,
        year_built_source="test" if year is not None else None,
        has_rent_data=rent,
        comp_exclusion=exclusion,
        address=f"{name} address",
        units=100,
    )


def _by_name(assigned):
    return {a.community: a for a in assigned}


def main() -> int:
    c = Checker("test_comp_tiers")

    # ---- ordering ---------------------------------------------------------------------
    c.section("tier order is by distance, nearest first")
    pool = [cand(f"C{i}", miles) for i, miles in enumerate([2.4, 0.3, 1.1, 0.9, 2.9, 0.1])]
    a = ct.assign(pool, subject_product="MF", subject_year_built=1981)
    detailed = [x for x in a if x.tier in (ct.TIER_TOP, ct.TIER_ADDITIONAL)]
    c.equal(
        "detailed rows come out nearest-first",
        [x.distance_mi for x in detailed],
        [0.1, 0.3, 0.9, 1.1, 2.4, 2.9],
    )
    c.equal("rank is 1-based over the detailed set", [x.rank for x in detailed], [1, 2, 3, 4, 5, 6])
    c.section("a distance tie breaks on name, so the order is stable across runs")
    tie = [cand("Zed", 1.0), cand("Alpha", 1.0), cand("Mid", 1.0)]
    a = ct.assign(tie, subject_product="MF", subject_year_built=1981)
    c.equal("tie broken alphabetically", [x.community for x in a], ["Alpha", "Mid", "Zed"])

    # ---- the 5 / 15 boundary ----------------------------------------------------------
    c.section("boundary at 5 and at 20")
    pool = [cand(f"C{i:02d}", 0.1 * (i + 1)) for i in range(25)]
    a = ct.assign(pool, subject_product="MF", subject_year_built=1981)
    tiers = [x.tier for x in a]
    c.equal("first 5 are tier 1", tiers[:5], [ct.TIER_TOP] * 5)
    c.equal("next 15 are tier 2", tiers[5:20], [ct.TIER_ADDITIONAL] * 15)
    c.equal("the rest are tier 3", tiers[20:], [ct.TIER_NEARBY] * 5)
    c.equal("exactly 5 in tier 1", sum(1 for x in a if x.tier == ct.TIER_TOP), 5)
    c.equal("exactly 15 in tier 2", sum(1 for x in a if x.tier == ct.TIER_ADDITIONAL), 15)
    c.ok(
        "the 21st eligible community says why it is identity-only",
        "beyond" in a[20].reason.lower(),
        a[20].reason,
    )

    c.section("a short pool does not pad")
    a = ct.assign([cand("A", 0.2), cand("B", 0.4)], subject_product="MF", subject_year_built=1981)
    c.equal("two eligible -> two tier-1 rows, no tier 2", [x.tier for x in a], [ct.TIER_TOP] * 2)

    # ---- the vintage band -------------------------------------------------------------
    c.section("vintage band is +/-10 years INCLUSIVE of the edge")
    # Subject 1981 -> 1971..1991 inclusive. The edges are the whole point: a comp built
    # exactly ten years apart is in, one built eleven is out, and an off-by-one here
    # silently changes which five communities a reader treats as the market.
    pool = [
        cand("Edge-low-in", 0.1, year=1971),
        cand("Edge-low-out", 0.2, year=1970),
        cand("Edge-high-in", 0.3, year=1991),
        cand("Edge-high-out", 0.4, year=1992),
        cand("Same-year", 0.5, year=1981),
    ]
    a = _by_name(ct.assign(pool, subject_product="MF", subject_year_built=1981))
    c.equal("1971 (exactly -10) is detailed", a["Edge-low-in"].tier, ct.TIER_TOP)
    c.equal("1991 (exactly +10) is detailed", a["Edge-high-in"].tier, ct.TIER_TOP)
    c.equal("1970 (-11) drops to tier 3", a["Edge-low-out"].tier, ct.TIER_NEARBY)
    c.equal("1992 (+11) drops to tier 3", a["Edge-high-out"].tier, ct.TIER_NEARBY)
    c.ok(
        "the out-of-band reason names the vintage",
        "vintage" in a["Edge-low-out"].reason.lower(),
        a["Edge-low-out"].reason,
    )
    c.ok(
        "the out-of-band reason quotes the year it rejected",
        "1992" in a["Edge-high-out"].reason,
        a["Edge-high-out"].reason,
    )

    # ---- unknown year ----------------------------------------------------------------
    c.section("an unknown year stays eligible; it is never invented")
    pool = [
        cand("No-year-closest", 0.1, year=None),
        cand("Known-in-band", 0.9, year=1985),
        cand("Known-out-of-band", 0.5, year=2024),
    ]
    a = _by_name(ct.assign(pool, subject_product="MF", subject_year_built=1981))
    c.equal("unknown-year community is tier 1 on proximity+product", a["No-year-closest"].tier, ct.TIER_TOP)
    c.equal("it carries no year", a["No-year-closest"].year_built, None)
    c.ok(
        "and it says the vintage test could not be applied",
        "unknown" in a["No-year-closest"].vintage_note.lower(),
        a["No-year-closest"].vintage_note,
    )
    c.equal("a KNOWN out-of-band year still drops", a["Known-out-of-band"].tier, ct.TIER_NEARBY)
    c.ok(
        "the tier-1 footnote discloses the partial filter",
        "where year" in ct.vintage_footnote(
            ct.assign(pool, subject_product="MF", subject_year_built=1981)
        ).lower(),
        ct.vintage_footnote(ct.assign(pool, subject_product="MF", subject_year_built=1981)),
    )
    c.section("with every year known, the footnote does not claim a partial filter")
    allknown = ct.assign(
        [cand("A", 0.2, year=1980), cand("B", 0.4, year=1982)],
        subject_product="MF",
        subject_year_built=1981,
    )
    c.ok(
        "footnote for an all-known set is unqualified",
        "where year" not in ct.vintage_footnote(allknown).lower(),
        ct.vintage_footnote(allknown),
    )
    c.section("an unknown SUBJECT year cannot filter anything")
    a = _by_name(
        ct.assign(
            [cand("Old", 0.1, year=1950), cand("New", 0.2, year=2025)],
            subject_product="MF",
            subject_year_built=None,
        )
    )
    c.equal("both stay detailed when the subject year is unknown", a["Old"].tier, ct.TIER_TOP)
    c.equal("... including the far-off vintage", a["New"].tier, ct.TIER_TOP)

    # ---- product ---------------------------------------------------------------------
    c.section("product must match, via the mapping")
    pool = [cand("BFR-closest", 0.1, product="bfr"), cand("MF-further", 0.9, product="mf")]
    a = _by_name(ct.assign(pool, subject_product="MF", subject_year_built=1981))
    c.equal("a bfr community is not an MF comp", a["BFR-closest"].tier, ct.TIER_NEARBY)
    c.equal("the mf community is, despite being further", a["MF-further"].tier, ct.TIER_TOP)
    c.ok(
        "the reason names the product mismatch",
        "product" in a["BFR-closest"].reason.lower(),
        a["BFR-closest"].reason,
    )
    c.section("the mapping is data: bfr covers SFD and TH")
    for subject in ("SFD", "TH"):
        a = _by_name(ct.assign(pool, subject_product=subject, subject_year_built=1981))
        c.equal(f"bfr comps an {subject} subject", a["BFR-closest"].tier, ct.TIER_TOP)
        c.equal(f"mf does not comp an {subject} subject", a["MF-further"].tier, ct.TIER_NEARBY)
    c.ok("PRODUCT_MAP is declared as data", isinstance(ct.PRODUCT_MAP, dict))
    c.ok(
        "an unrecognised tracker product matches nothing rather than everything",
        not ct.product_matches("mystery-code", "MF"),
    )

    # ---- other disqualifiers ---------------------------------------------------------
    c.section("no rent data, and tracker comp exclusions")
    pool = [
        cand("No-rents", 0.1, rent=False),
        cand("Student", 0.2, exclusion="student"),
        cand("Income", 0.3, exclusion="income_restricted"),
        cand("Clean", 0.4),
    ]
    a = _by_name(ct.assign(pool, subject_product="MF", subject_year_built=1981))
    c.equal("a community with no rents is identity-only", a["No-rents"].tier, ct.TIER_NEARBY)
    c.ok("and says so", "rent" in a["No-rents"].reason.lower(), a["No-rents"].reason)
    c.equal("a student property is not a market comp", a["Student"].tier, ct.TIER_NEARBY)
    c.equal("nor is an income-restricted one", a["Income"].tier, ct.TIER_NEARBY)
    c.ok(
        "the exclusion reason names the tracker's own flag verbatim",
        "income_restricted" in a["Income"].reason,
        a["Income"].reason,
    )
    c.equal("the clean one is tier 1", a["Clean"].tier, ct.TIER_TOP)

    # ---- the partition invariant -----------------------------------------------------
    c.section("every candidate lands in exactly one tier, and nothing is invented")
    pool = [
        cand("A", 0.1),
        cand("B", 0.2, product="bfr"),
        cand("C", 0.3, year=2024),
        cand("D", 0.4, rent=False),
        cand("E", 0.5, year=None),
    ]
    a = ct.assign(pool, subject_product="MF", subject_year_built=1981)
    c.equal("output count equals input count", len(a), len(pool))
    c.equal("names are preserved exactly", sorted(x.community for x in a), ["A", "B", "C", "D", "E"])
    c.ok("every row has a tier", all(x.tier in (1, 2, 3) for x in a))
    c.ok("every tier-3 row carries a reason", all(x.reason for x in a if x.tier == ct.TIER_NEARBY))
    c.ok(
        "no detailed row carries a tier-3 reason",
        all(not x.reason for x in a if x.tier != ct.TIER_NEARBY),
    )
    c.ok(
        "no row's year was filled in",
        _by_name(a)["E"].year_built is None and _by_name(a)["E"].year_built_source is None,
    )

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
