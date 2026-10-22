"""Which nearby communities are actually comps, and which are just nearby.

A flat list of every rent-carrying community inside a radius is not a comp set. It mixes
the property across the street with one two miles away, a 1971 garden walk-up with a 2026
lease-up, and apartments with townhomes — and then averages them. This module is the rule
that separates the three, expressed once, as data, so the display, the workbook and the
spread all read the same set.

Three tiers:

* **Tier 1, "Top comps"** — the 5 CLOSEST eligible communities. Full detail.
* **Tier 2, "Additional comps"** — the NEXT 15 eligible. Full detail.
* **Tier 3, "Also nearby"** — everything else tracked inside the radius, identity only,
  each row carrying the reason it is not a comp. That reason column is the point of the
  tier: "why isn't the property across the street in your comp set?" is the first question
  anyone asks, and a set that cannot answer it is asking to be trusted rather than read.

Eligibility for tiers 1 and 2, in the order the reasons are reported:

1. the tracker's own comp-exclusion flag is unset (a student or income-restricted property
   is not a market comp, and the tracker already knows which those are);
2. the product matches the subject through :data:`PRODUCT_MAP`;
3. the vintage is within :data:`YEAR_BAND` years of the subject's, **where a year is
   known**;
4. the community actually carries quoted rents.

**An unknown year does not disqualify.** The tracker has no year-built column at all —
where a vintage exists it came from a vendor note on the community record — so requiring
one would drop the closest comps in the submarket for want of a field the source never
had, and filling one in would fabricate the evidence the filter runs on. Instead the
community stays eligible on proximity and product, its year cell shows a dash, and
:func:`vintage_footnote` discloses on the tier header that the filter was applied only
where a year was known. Saying which rows a filter could not be applied to is the whole
difference between a partial filter and a silent one.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Tier numbers. Values are load-bearing — they are written into the interchange file, the
#: workbook and the previews.
TIER_TOP = 1
TIER_ADDITIONAL = 2
TIER_NEARBY = 3

TIER_LABELS: dict[int, str] = {
    TIER_TOP: "Top comps",
    TIER_ADDITIONAL: "Additional comps",
    TIER_NEARBY: "Also nearby",
}

#: How many communities each detailed tier holds.
TOP_N = 5
ADDITIONAL_N = 15

#: Half-width of the vintage band, in years, INCLUSIVE of the edge. A comp built exactly
#: ten years from the subject is in; eleven is out.
YEAR_BAND = 10

#: Tracker `product` code -> the subject `product_type` values it can serve as a comp for.
#:
#: The mapping is data rather than an `if` because the two vocabularies are genuinely
#: different sizes: the tracker splits rentals into apartments and everything built as a
#: rental house, while underwriting distinguishes detached from attached. `bfr` therefore
#: covers BOTH SFD and TH — a build-for-rent community is the comp set for either, and the
#: tracker does not record which one a given community is. If that ever changes, this dict
#: is the only place that needs to know.
#: The two disqualifiers that describe an ABSENCE rather than a finding. A caller that knows
#: why the thing is absent (the market mapper knows the producer never carried the product
#: code, or why nothing was quotable) may replace these with its own reason; the findings —
#: excluded by the tracker, a different product, outside the vintage band — it may not.
NO_RENT_REASON = "no quoted rent in the latest scrape of this community"
PRODUCT_UNKNOWN_REASON = ("product unknown: no registry record for this community, so it cannot be "
                          "matched to the subject's product type")
ABSENCE_REASONS = (NO_RENT_REASON, PRODUCT_UNKNOWN_REASON)

PRODUCT_MAP: dict[str, tuple[str, ...]] = {
    "mf": ("MF",),
    "bfr": ("SFD", "TH"),
}


@dataclass
class Candidate:
    """One tracked community inside the radius, before any tier is assigned."""

    community: str
    distance_mi: float
    product: str | None
    year_built: int | None
    year_built_source: str | None
    has_rent_data: bool
    comp_exclusion: str | None
    address: str | None
    units: int | None


@dataclass
class Assignment:
    """A candidate with its tier, and — for tier 3 — why it is not a comp."""

    community: str
    distance_mi: float
    product: str | None
    year_built: int | None
    year_built_source: str | None
    address: str | None
    units: int | None
    tier: int
    #: 1-based position within the detailed set; ``None`` for tier 3.
    rank: int | None
    #: Empty for tiers 1 and 2. Tier 3 always carries one.
    reason: str
    #: What the vintage test could conclude about this row, always populated.
    vintage_note: str


def product_matches(tracker_product: str | None, subject_product: str | None) -> bool:
    """Does a tracker product code serve as a comp for this subject product type?

    An unrecognised code matches NOTHING. Defaulting an unknown product to "yes" would
    quietly widen the comp set every time the tracker adds a category.
    """
    if not tracker_product or not subject_product:
        return False
    allowed = PRODUCT_MAP.get(str(tracker_product).strip().lower())
    if not allowed:
        return False
    return str(subject_product).strip().upper() in allowed


def vintage_verdict(
    year_built: int | None, subject_year_built: int | None, band: int | None = YEAR_BAND
) -> tuple[bool, str]:
    """``(passes, note)`` for one community's vintage.

    Both unknowns pass — with a note saying which unknown it was, because "we did not
    filter this row" and "this row passed the filter" are different claims.

    ``band=None`` disables the test entirely. That exists for ONE purpose: building the
    vintage-only counterfactual, where every other criterion is held fixed and only this
    test is removed. Keeping it here rather than in the caller means the counterfactual
    runs through the same selection code, which is the whole point of it.
    """
    if band is None:
        return True, "vintage test not applied in this comparison"
    if subject_year_built is None:
        return True, "vintage not tested: the subject's own year built is unknown"
    if year_built is None:
        return True, "vintage unknown for this community; not tested"
    delta = abs(int(year_built) - int(subject_year_built))
    if delta <= band:
        return True, f"built {year_built}, within {band} years of the subject's {subject_year_built}"
    return (
        False,
        f"vintage {year_built} is {delta} years from the subject's {subject_year_built}, "
        f"outside the +/-{band}-year band",
    )


def _disqualifier(
    cand: Candidate, subject_product: str | None, subject_year_built: int | None, band: int
) -> tuple[str, str]:
    """``(reason, vintage_note)`` — reason empty if the candidate is eligible."""
    passes_vintage, vintage_note = vintage_verdict(cand.year_built, subject_year_built, band)
    if cand.comp_exclusion:
        return (
            f"excluded from comp sets by the tracker: {cand.comp_exclusion}",
            vintage_note,
        )
    if not product_matches(cand.product, subject_product):
        # "Unknown" and "different" are different findings. A community with no registry
        # record has no product at all — reporting that as a mismatch would send a reader
        # looking for a product difference that was never observed.
        if not cand.product:
            return PRODUCT_UNKNOWN_REASON, vintage_note
        return (
            f"different product: tracked as {cand.product}, subject is "
            f"{subject_product or 'unknown'}",
            vintage_note,
        )
    if not passes_vintage:
        return vintage_note, vintage_note
    if not cand.has_rent_data:
        return NO_RENT_REASON, vintage_note
    return "", vintage_note


def assign(
    candidates,
    subject_product: str | None,
    subject_year_built: int | None,
    top_n: int = TOP_N,
    additional_n: int = ADDITIONAL_N,
    band: int | None = YEAR_BAND,
) -> list[Assignment]:
    """Tier every candidate. Total in equals total out; nothing is dropped or invented.

    Ordering is distance ascending, ties broken on name so the output is stable across
    runs — a comp set that reshuffles between two builds of the same data cannot be
    reviewed.
    """
    ordered = sorted(candidates, key=lambda x: (float(x.distance_mi), str(x.community)))
    out: list[Assignment] = []
    rank = 0
    for cand in ordered:
        reason, vintage_note = _disqualifier(cand, subject_product, subject_year_built, band)
        if reason:
            tier, this_rank = TIER_NEARBY, None
        else:
            rank += 1
            this_rank = rank
            if rank <= top_n:
                tier = TIER_TOP
            elif rank <= top_n + additional_n:
                tier = TIER_ADDITIONAL
            else:
                tier = TIER_NEARBY
                this_rank = None
                reason = (
                    f"eligible, but beyond the closest {top_n + additional_n} by distance"
                )
        out.append(
            Assignment(
                community=cand.community,
                distance_mi=float(cand.distance_mi),
                product=cand.product,
                year_built=cand.year_built,
                year_built_source=cand.year_built_source,
                address=cand.address,
                units=cand.units,
                tier=tier,
                rank=this_rank,
                reason=reason,
                vintage_note=vintage_note,
            )
        )
    return out


def vintage_footnote(assigned, band: int | None = YEAR_BAND) -> str:
    """The disclosure that belongs on the detailed tiers' header.

    Names the band, and says so explicitly when some rows could not be tested — the
    difference between "filtered" and "filtered where we could" is exactly the thing a
    reader cannot infer from the table.
    """
    detailed = [a for a in assigned if a.tier in (TIER_TOP, TIER_ADDITIONAL)]
    if not detailed:
        return f"Vintage filter: +/-{band} years of the subject, where a year is known."
    unknown = sum(1 for a in detailed if a.year_built is None)
    if not unknown:
        return f"Vintage filter: +/-{band} years of the subject's year built."
    return (
        f"Vintage filter applied where year known: +/-{band} years of the subject's year "
        f"built. {unknown} of {len(detailed)} communities shown carry no published year "
        f"and were matched on distance and product only."
    )
