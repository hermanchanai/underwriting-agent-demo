"""Generate `fixture_deal.json` — a fully synthetic deal, deterministic and committed.

The fixture is not a plausible-looking blob. It is built to exercise the edges the writers
and the previews have to survive:

* an **uncategorized subtotal line** on the T-12 (the statement's own "Total Revenue"),
  which must stay visible rather than being dropped or double-counted;
* **contra-revenue rows carried negative** (loss to lease, vacancy, concessions, bad debt)
  with no writer flipping a sign;
* **no printed Gross Scheduled Rent line**, so the rollup's GPR + gain/(loss) fallback is
  the path under test;
* **a plan with zero new leases in the lookback window**, so `market_rent_for_model` falls
  back to in-place and says so;
* **a unit whose lease-start date is the string "n/a"**, which must stay text — a date
  coerced from unreadable input is worse than a blank;
* **a vacant unit and a non-revenue model unit**, so occupancy arithmetic is checkable;
* **six Other Income lines and five Utilities lines**, so both Top-4-plus-Rest tables have
  a non-empty Rest row;
* **one source subtotal deliberately absent**, so the Error Check has a NOT CHECKED row
  rather than reporting OK on a tie it never tested.

Run: `python tools/fixtures/make_fixture.py`
Deterministic: no RNG, no clock. Re-running produces a byte-identical file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notes as _notes  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "synthetic_full.json"

AS_OF = "2026-06-30"
MONTHS = [
    "2025-07",
    "2025-08",
    "2025-09",
    "2025-10",
    "2025-11",
    "2025-12",
    "2026-01",
    "2026-02",
    "2026-03",
    "2026-04",
    "2026-05",
    "2026-06",
]

# plan -> (beds, baths, sqft, units, base_rent, new_lease_share)
# "Cypress TH" is the plan with no new leases inside the lookback window.
PLANS: list[tuple[str, float, float, int, int, int, float]] = [
    ("A1", 1, 1.0, 720, 34, 1445, 0.35),
    ("A2", 1, 1.0, 795, 28, 1520, 0.30),
    ("B1", 2, 2.0, 1050, 42, 1775, 0.28),
    ("B2", 2, 2.0, 1140, 36, 1850, 0.25),
    ("C1", 3, 2.0, 1380, 26, 2095, 0.22),
    ("Cypress TH", 3, 2.5, 1620, 14, 2340, 0.0),
]

UNITS_TOTAL = sum(p[4] for p in PLANS)  # 180


def _wobble(seed: int, span: int) -> int:
    """Deterministic small offset in [-span, span]. No RNG: reproducible forever."""
    return ((seed * 2654435761) % (2 * span + 1)) - span


def build_units() -> list[dict]:
    """One row per unit, with the edge cases placed at known positions."""
    units: list[dict] = []
    n = 0
    for plan, beds, baths, sqft, count, base_rent, new_share in PLANS:
        new_target = int(round(count * new_share))
        for i in range(count):
            n += 1
            unit_id = f"{plan.split()[0]}-{100 + i + 1}"
            rent = base_rent + _wobble(n, 45)

            is_new = i < new_target
            if is_new:
                # New lease inside the lookback: signs at move-in.
                month = 7 + (i % 11)  # 2025-07 .. 2026-05 window
                year = 2025 if month <= 12 else 2026
                mm = month if month <= 12 else month - 12
                day = 3 + (i % 25)
                lease_start = f"{year if month <= 12 else 2026:04d}-{mm:02d}-{day:02d}"
                move_in = lease_start
                rent = base_rent + 60 + _wobble(n, 40)  # new leases trade up
            else:
                # Renewal: moved in years ago, re-signed recently.
                lease_start = f"2026-0{1 + (i % 5)}-{1 + (i % 27):02d}"
                move_in = f"{2021 + (i % 3)}-{1 + (i % 12):02d}-{1 + (i % 26):02d}"

            status = "Occupied"
            unit: dict = {
                # Synthetic source coordinates, so the rent readback has a locator to cite
                # and a raw value to compare against. On the real deal these are the actual
                # row and cell contents; here they are consistent fabrications, which is
                # what lets the readback logic be exercised at all.
                "source_row": len(units) + 4,
                "source_rent_raw": rent,
                # Tie-out input only — never a rent in any analysis. Present so the
                # market-rent completeness check actually RUNS on the fixture; without it
                # the check reported NOT CHECKED and the test that depends on its verdict
                # could not distinguish "ran and passed" from "never ran".
                "market_rent": round(rent * 1.06, 2) if rent else 0.0,
                "unit_id": unit_id,
                "floor_plan": plan,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "status": status,
                "lease_start": lease_start,
                "move_in": move_in,
                "lease_type": "New" if is_new else "Renewal",
                "in_place_rent": rent,
            }
            units.append(unit)

    # ---- edge cases, placed deliberately -------------------------------------------
    # A vacant unit: no lease, no rent.
    vacant = units[5]
    vacant.update(
        status="Vacant",
        lease_start=None,
        move_in=None,
        lease_type=None,
        in_place_rent=None,
    )
    # A second vacant, in a different plan.
    vacant2 = units[70]
    vacant2.update(
        status="Vacant",
        lease_start=None,
        move_in=None,
        lease_type=None,
        in_place_rent=None,
    )
    # A non-revenue model unit: occupied by nobody, counted in stock.
    model = units[120]
    model.update(
        status="Model",
        lease_start=None,
        move_in=None,
        lease_type=None,
        in_place_rent=None,
    )
    # A unit whose lease-start date the source gave as text. It must survive as text,
    # and its lease type must come out blank rather than guessed.
    textdate = units[40]
    textdate.update(lease_start="n/a", lease_type=None)

    # The townhome plan: every lease is a renewal signed before the lookback window
    # opened, so the plan has no new-lease evidence at all.
    for unit in units:
        if unit["floor_plan"] == "Cypress TH":
            unit["lease_type"] = "Renewal"
            unit["lease_start"] = "2024-11-15"
            unit["move_in"] = "2021-06-01"
    return units


def month_factor(i: int) -> float:
    """Mild seasonality plus a gentle upward trend, so T-3 differs from T-12."""
    seasonal = (0.995, 0.997, 1.0, 1.002, 0.999, 0.996, 1.001, 1.004, 1.007, 1.01, 1.013, 1.016)
    return seasonal[i]


def build_t12(units: list[dict]) -> dict:
    """Monthly matrix consistent with the roll: GPR tracks the plans' market rents."""
    gpr_monthly_base = sum(p[4] * p[5] for p in PLANS)  # units * base rent

    def series(base: float, *, drift: float = 1.0, seed: int = 0) -> list[float]:
        out = []
        for i in range(12):
            val = base * month_factor(i) * (1 + drift * i / 400.0)
            val += _wobble(seed + i, max(1, int(abs(base) * 0.01)))
            out.append(round(val, 2))
        return out

    rows: list[dict] = [
        {"label": "Gross Potential Rent", "category": "Gross Potential Rent",
         "monthly": series(gpr_monthly_base, seed=11)},
        {"label": "Loss to Lease", "category": "Gain / (Loss) to Lease",
         "monthly": series(-gpr_monthly_base * 0.021, seed=23)},
        {"label": "Vacancy Loss", "category": "Vacancy",
         "monthly": series(-gpr_monthly_base * 0.058, seed=37)},
        {"label": "Concessions — Move-in", "category": "Concessions",
         "monthly": series(-gpr_monthly_base * 0.011, seed=41)},
        {"label": "Non-Revenue Units", "category": "Non-Revenue Units",
         "monthly": series(-gpr_monthly_base * 0.004, seed=53)},
        {"label": "Bad Debt", "category": "Delinquency",
         "monthly": series(-gpr_monthly_base * 0.007, seed=59)},
        {"label": "RUBS — Water/Sewer", "category": "Utility Reimbursements",
         "monthly": series(9800, seed=61)},
        {"label": "RUBS — Trash", "category": "Utility Reimbursements",
         "monthly": series(4200, seed=67)},
        {"label": "Pet Rent", "category": "Other Income",
         "monthly": series(3900, seed=71)},
        {"label": "Parking", "category": "Other Income",
         "monthly": series(3100, seed=73)},
        {"label": "Late Fees", "category": "Other Income",
         "monthly": series(1450, seed=79)},
        {"label": "Application Fees", "category": "Other Income",
         "monthly": series(1180, seed=83)},
        {"label": "Storage", "category": "Other Income",
         "monthly": series(760, seed=89)},
        {"label": "Miscellaneous Income", "category": "Other Income",
         "monthly": series(420, seed=97)},
        {"label": "Administrative", "category": "General and Administrative",
         "monthly": series(5200, seed=101)},
        {"label": "Marketing & Locator Fees", "category": "Leasing and Marketing",
         "monthly": series(4000, seed=103)},
        {"label": "On-site Payroll", "category": "Payroll",
         "monthly": series(21000, seed=107)},
        {"label": "Insurance", "category": "Insurance",
         "monthly": series(11500, seed=109)},
        {"label": "Real Estate Taxes", "category": "Property Taxes",
         "monthly": series(41200, seed=113)},
        {"label": "Legal & Audit", "category": "Legal and Professional",
         "monthly": series(1900, seed=127)},
        {"label": "Management Fee", "category": "Management Fee",
         "monthly": series(10900, seed=131)},
        {"label": "Landscaping & Grounds", "category": "Landscaping",
         "monthly": series(3400, seed=137)},
        {"label": "R&M — Interior", "category": "Repairs and Maintenance",
         "monthly": series(7200, seed=139)},
        {"label": "R&M — Exterior", "category": "Repairs and Maintenance",
         "monthly": series(3900, seed=149)},
        {"label": "Unit Turns", "category": "Unit Turns",
         "monthly": series(5600, seed=151)},
        {"label": "Utilities — Water/Sewer", "category": "Utilities",
         "monthly": series(9400, seed=157)},
        {"label": "Utilities — Electric (common)", "category": "Utilities",
         "monthly": series(4200, seed=163)},
        {"label": "Utilities — Trash", "category": "Utilities",
         "monthly": series(4100, seed=167)},
        {"label": "Utilities — Gas", "category": "Utilities",
         "monthly": series(2300, seed=173)},
        {"label": "Utilities — Cable/Internet", "category": "Utilities",
         "monthly": series(1600, seed=179)},
        {"label": "Expense Adjustments", "category": "Expense Adjustments",
         "monthly": series(900, seed=181)},
    ]

    # The statement's own Total Revenue subtotal line: present, visible, and deliberately
    # NOT categorized, so nothing sums it twice.
    revenue_categories = {
        "Gross Potential Rent",
        "Gain / (Loss) to Lease",
        "Vacancy",
        "Concessions",
        "Non-Revenue Units",
        "Delinquency",
        "Utility Reimbursements",
        "Other Income",
        "Revenue Adjustments",
    }
    subtotal_monthly = []
    for i in range(12):
        subtotal_monthly.append(
            round(
                sum(
                    r["monthly"][i]
                    for r in rows
                    if r["category"] in revenue_categories
                ),
                2,
            )
        )
    rows.append(
        {
            "label": "Total Revenue (per statement)",
            "category": None,
            "monthly": subtotal_monthly,
        }
    )

    expense_categories = {
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
    }
    total_revenue = round(sum(subtotal_monthly), 2)
    total_opex = round(
        sum(sum(r["monthly"]) for r in rows if r["category"] in expense_categories), 2
    )
    other_income = round(
        sum(sum(r["monthly"]) for r in rows if r["category"] == "Other Income"), 2
    )
    utilities = round(
        sum(sum(r["monthly"]) for r in rows if r["category"] == "Utilities"), 2
    )

    return {
        "month_labels": MONTHS,
        "units": UNITS_TOTAL,
        "rows": rows,
        # "Utility Reimbursements" is deliberately absent: its Error Check row must read
        # NOT CHECKED rather than claiming a tie it never tested.
        "source_subtotals": {
            "Total Revenue": total_revenue,
            "Total Operating Expenses": total_opex,
            "Net Operating Income": round(total_revenue - total_opex, 2),
            "Other Income": other_income,
            "Utilities": utilities,
        },
        "broker_proforma": {
            "revenue": round(total_revenue * 1.062, 2),
            "opex": round(total_opex * 1.018, 2),
            "noi": round(total_revenue * 1.062 - total_opex * 1.018, 2),
        },
    }


#: Subject vintage for the fixture, so the +/-10 band has a centre. This MUST equal
#: summary.year_built — a comp set filtered around a different vintage than the subject's
#: is a wrong answer that looks right, so schema.validate() now cross-checks the pair
#: rather than trusting this comment. (It caught this constant drifting to 2004 while the
#: summary said 2019.)
FIXTURE_YEAR_BUILT = 2019

#: Synthetic communities inside the radius. Deliberately shaped to exercise every branch
#: of the tiering rule at once, because a fixture that only shows the happy path lets the
#: display drift on exactly the rows a reader would question:
#:
#:   (name, distance, product, year_built, has_rents, comp_exclusion, units)
#:
#: * five in-band rentals fill tier 1, one of them with **no published year** — the case
#:   the tracker actually produces, and the one that decides whether the vintage footnote
#:   is honest;
#: * six more fill part of tier 2 (a partly-filled second tier is legal and worth
#:   showing — real submarkets run out of comps);
#: * four land in tier 3, one per reason: wrong product, vintage outside the band, no
#:   quoted rent, and the tracker's own comp exclusion.
FIXTURE_COMMUNITIES: list[tuple[str, float, str, int | None, bool, str | None, int]] = [
    ("Alderwood Crossing", 0.4, "mf", 2021, True, None, 288),
    ("Rowan Yard", 0.6, "mf", None, True, None, 164),
    ("Sable Point", 0.9, "mf", 2014, True, None, 312),
    ("Harper Station", 1.1, "mf", 2026, True, None, 240),
    ("Kestrel Ridge", 1.3, "mf", 2019, True, None, 196),
    ("Linden Mill", 1.5, "mf", 2011, True, None, 220),
    # Both band edges, exactly: 2009 is -10 and 2029 is +10 from the subject's 2019. They
    # must stay IN. An off-by-one in the band silently changes which comps are the market.
    ("Pinegate Commons", 1.6, "mf", 2009, True, None, 180),
    ("Quarry Flats", 1.8, "mf", 2029, True, None, 152),
    ("Marrow Bend", 2.0, "mf", 2028, True, None, 176),
    ("Nyland Court", 2.2, "mf", 2016, True, None, 208),
    ("Orchard Vale", 2.4, "mf", 2023, True, None, 264),
    # ---- tier 3, one per reason ----
    ("Rushmere Townhomes", 0.5, "bfr", 2022, True, None, 96),
    ("Stonecrest Lofts", 0.8, "mf", 1998, True, None, 132),
    ("Tallow Creek", 1.0, "mf", 2018, False, None, 144),
    ("Underhill Residences", 1.2, "mf", 2020, True, "income_restricted", 88),
]

#: Plan shapes by bed count: (label, baths, sqft, base asking). Rents are derived from the
#: base plus a per-community offset, so the fixture is deterministic without 40 hand-typed
#: numbers — and the arithmetic is visible rather than pasted.
FIXTURE_PLANS: list[tuple[int, str, float, int, int]] = [
    (1, "1BR-A", 1.0, 735, 1440),
    (2, "2BR-B", 2.0, 1075, 1795),
    (3, "3BR-C", 2.5, 1355, 2140),
]


def _fixture_candidates():
    sys.path.insert(0, str(HERE.parent))
    import comp_tiers  # noqa: E402

    return comp_tiers, [
        comp_tiers.Candidate(
            community=name,
            distance_mi=distance,
            product=product,
            year_built=year,
            year_built_source=(
                "vendor note on the tracked community record" if year is not None else None
            ),
            has_rent_data=has_rents,
            comp_exclusion=exclusion,
            address=f"{100 + i * 25} Sample Street, Demo City",
            units=units,
        )
        for i, (name, distance, product, year, has_rents, exclusion, units) in enumerate(
            FIXTURE_COMMUNITIES
        )
    ]


def build_comps() -> dict:
    """The comp block, tiered by the REAL tiering module.

    Hand-assembling the tiers here would let the fixture and the rule disagree — and the
    fixture is what the writer tests and the published previews are built from, so that
    disagreement would be invisible until someone read a live comp set.
    """
    comp_tiers, candidates = _fixture_candidates()
    assigned = comp_tiers.assign(
        candidates,
        subject_product="MF",
        subject_year_built=FIXTURE_YEAR_BUILT,
    )

    rows = []
    for i, a in enumerate(a for a in assigned if a.tier != comp_tiers.TIER_NEARBY):
        # Two plans for most, three for every third community: a comp set where every
        # community happens to publish the same bed counts hides the mix-matching the
        # spread depends on.
        plans = FIXTURE_PLANS if i % 3 == 2 else FIXTURE_PLANS[:2]
        for beds, plan, baths, sqft, base in plans:
            asking = base + 5 * i + 10 * beds
            concession = 45 if i % 2 == 0 else 0
            rows.append(
                {
                    "community": a.community,
                    "distance_mi": a.distance_mi,
                    "tier": a.tier,
                    "year_built": a.year_built,
                    "year_built_source": a.year_built_source,
                    "floor_plan": plan,
                    "beds": beds,
                    "baths": baths,
                    "sqft": sqft,
                    "asking_rent": asking,
                    "concession": concession,
                    "net_effective_rent": asking - concession,
                    "occupancy": 0.94 if concession else 0.96,
                    "as_of": "2026-06-22",
                    "source": "own weekly scrape",
                }
            )
    rows.sort(key=lambda r: (r["distance_mi"], r["community"], r["beds"]))

    nearby = [
        {
            "community": a.community,
            "address": a.address,
            "distance_mi": a.distance_mi,
            "units": a.units,
            "product": a.product,
            "year_built": a.year_built,
            "year_built_source": a.year_built_source,
            "tier": comp_tiers.TIER_NEARBY,
            "reason": a.reason,
            "has_rent_data": next(
                c.has_rent_data for c in candidates if c.community == a.community
            ),
        }
        for a in assigned
        if a.tier == comp_tiers.TIER_NEARBY
    ]

    detailed = len({r["community"] for r in rows})
    with_rent = detailed + sum(1 for n in nearby if n["has_rent_data"])
    # The pre-tiering population, synthetic but shaped like the real one: MORE communities,
    # a WIDER vintage span, and higher rents — because the newer product a vintage filter
    # removes is what was pulling the average up. Without this the fixture cannot exercise
    # the derived vintage note at all, and that note is the one that quotes numbers.
    # The counterfactual, run through the REAL tiering with the vintage test off — so the
    # fixture exercises the same single-variable path the deal does, including a genuine
    # community swap. Hardcoding an empty swap made the fixture pass a check it should not
    # have: a counterfactual that swaps nothing measures nothing.
    novintage = comp_tiers.assign(
        candidates, subject_product="MF", subject_year_built=FIXTURE_YEAR_BUILT, band=None,
    )
    nv_names = sorted(
        a.community for a in novintage if a.tier != comp_tiers.TIER_NEARBY
    )
    matched_names = sorted({r["community"] for r in rows})
    nv_years = [a.year_built for a in novintage
                if a.tier != comp_tiers.TIER_NEARBY and a.year_built is not None]
    unrestricted_by_bed = [
        {
            "beds": float(beds),
            "communities": len(nv_names),
            "rows": len(nv_names),
            # Above the matched set, because the newer product a vintage filter removes is
            # what pulls the average up.
            "avg_asking": round(base * 1.10, 2),
            "avg_net_effective": round(base * 1.09, 2),
        }
        for beds, base in ((1, 1478.0), (2, 1843.0), (3, 2198.0))
    ]
    return {
        "as_of": "2026-06-22",
        "source": "own weekly scrape",
        "subject_market_rent": 1795,
        "rows": rows,
        "nearby": nearby,
        "unrestricted": {
            "communities": len(nv_names),
            "rows": len(nv_names) * 2,
            "by_bed": unrestricted_by_bed,
            "varied": "vintage",
            "held_fixed": [
                "candidate pool", "product test", "tracker comp exclusions", "radius",
                "closest-20 cap", "per-community rent rules", "mix weighting",
            ],
            "swapped_in": sorted(set(nv_names) - set(matched_names)),
            "swapped_out": sorted(set(matched_names) - set(nv_names)),
            # DERIVED, not hardcoded: the unrestricted population is a superset of the
            # detailed tiers, so its span must contain theirs. A hand-written 1994-2026
            # sat inside the matched 2009-2029 span, which is an impossible superset.
            "year_min": min(nv_years) if nv_years else None,
            "year_max": max(nv_years) if nv_years else None,
            "note": (
                "Synthetic pre-tiering population: every community in the radius with "
                "quoted rent, no product or vintage filter. On a real deal this is "
                "measured at selection time, because the nearby tier publishes no rents."
            ),
        },
        "tiering": {
            "subject_product": "MF",
            "subject_year_built": FIXTURE_YEAR_BUILT,
            "year_band": comp_tiers.YEAR_BAND,
            "radius_mi": 3.0,
            "top_n": comp_tiers.TOP_N,
            "additional_n": comp_tiers.ADDITIONAL_N,
            "vintage_footnote": comp_tiers.vintage_footnote(assigned),
        },
        "absent_reason": None,
        "coverage_note": (
            f"Synthetic coverage statement. {with_rent} of {len(candidates)} tracked "
            f"communities within 3 miles carry quoted rent; {detailed} of those are shown "
            f"in the detailed tiers and the rest are listed as nearby only. On a real deal "
            f"this sentence is measured from the operator's own scrape by "
            f"tools/comp_coverage_probe.py, using the scraper's own status taxonomy rather "
            f"than an assumed one."
        ),
    }


#: Seven columns, matching the real engine: four rings, the metro, the state, the nation.
DEMAND_COLUMNS: tuple[str, ...] = (
    "1 mi", "3 mi", "5 mi", "10 mi", "Demo MSA", "Demo State", "U.S.",
)

#: The 1-mile caveat, restated for the synthetic set. It is carried here too because the
#: fixture is what the PUBLISHED previews are built from, and a reader of those has the
#: same right to know the limit as a reader of the real deal's.
FIXTURE_1MI_CAVEAT = (
    "The 1-mile column overstates levels. Ring membership is decided by each census block "
    "group's internal point and then the whole block group is counted, so at 1 mile a "
    "single large block group can sit mostly outside the ring and still be counted whole. "
    "Measured against a commercial source, 1-mile population reads roughly 2.3x and "
    "households roughly 1.9x too high; the 3-, 5- and 10-mile columns do not have this "
    "problem. Read the 1-mile column for mix and shares, which are ratios and survive the "
    "overcount, and read levels off the 3-mile column."
)


def _dr(label, values, fmt, *, kind="ring", note=None, indent=0):
    return {"label": label, "values": list(values), "format": fmt, "kind": kind,
            "note": note, "indent": indent}


def _demand_sections() -> list[dict]:
    """Synthetic equivalents of all seven sections the real engine emits.

    Deliberately exercises the two awkward shapes as well as the easy ones: the ZIP
    home-value section, which carries ONE value and not seven, and the labor section's
    year-by-year metro table, which does not share the ring columns at all. A fixture that
    only covered the rectangular sections would let those two rot unnoticed.
    """
    scale = [0.021, 0.114, 0.276, 0.815, 4.30, 6.50, 300.0]
    def spread(base, mult=1.0):
        return [round(base * s * mult) for s in scale]

    hh20 = spread(180_000)
    hh24 = spread(191_000)
    pop20 = spread(455_000)
    pop24 = spread(477_000)
    return [
        {
            "title": "Households", "kind": "ring",
            "note": "2020 Census baseline, 2024 ACS actual, and a 2026 projection.",
            "rows": [
                _dr("Households (2020 Census)", hh20, "count"),
                _dr("Households (2024 actual)", hh24, "count"),
                _dr("Growth 2020 to 2024", [round(a / b - 1, 4) for a, b in zip(hh24, hh20)], "pct1"),
                _dr("Est. annual growth rate (to 2026)", [0.0225] + [None] * 6, "pct2",
                    note="An assumption, not a measurement."),
                _dr("Households (2026 est.)", [round(v * 1.0225 ** 2) for v in hh24], "count"),
            ],
        },
        {
            "title": "Population", "kind": "ring", "note": None,
            "rows": [
                _dr("Population (2020 Census)", pop20, "count"),
                _dr("Population (2024 actual)", pop24, "count"),
                _dr("Growth 2020 to 2024", [round(a / b - 1, 4) for a, b in zip(pop24, pop20)], "pct1"),
                _dr("Est. annual growth rate (to 2026)", [0.011] + [None] * 6, "pct2",
                    note="An assumption, not a measurement."),
                _dr("Population (2026 est.)", [round(v * 1.011 ** 2) for v in pop24], "count"),
            ],
        },
        {
            "title": "Households by income (2024 actual)", "kind": "ring",
            "note": "Eight brackets, with the two affordability subtotals underneath.",
            "rows": [
                _dr("Less than $25,000", spread(191_000, 0.118), "count", indent=1),
                _dr("$25,000 - $49,999", spread(191_000, 0.146), "count", indent=1),
                _dr("$50,000 - $74,999", spread(191_000, 0.152), "count", indent=1),
                _dr("$75,000 - $99,999", spread(191_000, 0.139), "count", indent=1),
                _dr("$100,000 - $124,999", spread(191_000, 0.121), "count", indent=1),
                _dr("$125,000 - $149,999", spread(191_000, 0.092), "count", indent=1),
                _dr("$150,000 - $199,999", spread(191_000, 0.114), "count", indent=1),
                _dr("$200,000 and over", spread(191_000, 0.118), "count", indent=1),
                _dr("Subtotal $50,000 and over", spread(191_000, 0.736), "count"),
                _dr("   share of households", [0.736] * 7, "pct1"),
                _dr("Subtotal $100,000 and over", spread(191_000, 0.445), "count"),
                _dr("   share of households", [0.445] * 7, "pct1"),
            ],
        },
        {
            "title": "Income and affordability", "kind": "ring",
            "note": "Median interpolated from the brackets above, so the two cannot disagree.",
            "rows": [
                _dr("Average household income (2024)",
                    [81_200, 103_400, 107_900, 112_600, 118_800, 108_100, 113_900], "currency0"),
                _dr("Median household income (2024)",
                    [66_500, 81_900, 83_100, 83_400, 88_900, 80_600, 81_400], "currency0"),
                _dr("Est. annual income growth (to 2026)", [0.062] + [None] * 6, "pct2",
                    note="An assumption, not a measurement."),
                _dr("Average household income (2026 est.)",
                    [round(v * 1.062 ** 2) for v in
                     [81_200, 103_400, 107_900, 112_600, 118_800, 108_100, 113_900]], "currency0"),
                _dr("Renter share", [0.654, 0.512, 0.518, 0.481, 0.334, 0.331, 0.352], "pct1"),
                _dr("Rent-to-income at $1,795/mo",
                    [0.265, 0.208, 0.200, 0.191, 0.181, 0.199, 0.189], "pct1",
                    note="Against projected 2026 income, so it is a forward ratio."),
            ],
        },
        {
            # ONE value, not seven. The section a rectangular renderer breaks on.
            "title": "Home value — ZIP 00000 vs Demo MSA", "kind": "zip",
            "note": (
                "ZIP geography, not a drive ring — the only section that does not share "
                "the columns above, so it is shown as its own three figures rather than "
                "stretched across headers it does not describe."
            ),
            "rows": [
                _dr("ZIP home value", [451_600], "currency0", kind="zip"),
                _dr("Metro home value", [446_200], "currency0", kind="zip"),
                _dr("ZIP vs metro", [0.0121], "pct1", kind="zip"),
            ],
        },
        {
            "title": "Labor market", "kind": "ring",
            "note": (
                "Ring job counts are WORKPLACE counts — jobs located inside the ring, not "
                "residents who hold jobs. The unemployment row is the opposite: residents."
            ),
            "rows": [
                _dr("Unemployment rate (residents, ACS)",
                    [0.066, 0.052, 0.048, 0.046, 0.047, 0.051, 0.052], "pct1"),
                _dr("Jobs located inside the ring — LODES workplace counts", [], "header",
                    kind="subheader"),
                _dr("2021", [10_370, 122_100, 352_300, 903_200, None, None, None], "count", indent=1),
                _dr("2022", [11_300, 127_600, 361_400, 921_800, None, None, None], "count", indent=1),
                _dr("2023  (latest)", [11_644, 140_800, 382_500, 990_700, None, None, None],
                    "count", indent=1),
                _dr("Job growth 2021 to 2023", [0.1229, 0.1531, 0.0858, 0.0968, None, None, None],
                    "pct1", indent=1),
            ],
            "metro_history": [
                {"year": y, "nonfarm_000s": n, "yoy": g, "unemployment": u}
                for y, n, g, u in [
                    (2025, 2462.5, 0.0026, 0.0423), (2024, 2456.0, 0.0224, 0.0367),
                    (2023, 2402.2, 0.0311, 0.0338), (2022, 2329.7, 0.0512, 0.0311),
                ]
            ],
            "metro_label": "Demo County, Demo State",
            "metro_summary": {
                "nonfarm_year": 2025, "nonfarm_000s": 2462.5, "nonfarm_cagr5": 0.0218,
                "msa_unemp": 0.0423, "msa_unemp_year": 2025,
                "county_unemp": 0.0417, "county_unemp_year": 2025,
                "county_name": "Demo County, Demo State",
            },
        },
        {
            "title": "Jobs by industry (2023)", "kind": "ring",
            "note": (
                "Share of jobs by sector, sorted by the metro's own concentration. Ring, "
                "MSA and state shares come from LODES workplace counts; the U.S. column "
                "is QCEW, a different survey, so read it as a benchmark rather than the "
                "same measurement."
            ),
            "rows": [
                _dr(label, vals, "pct1",
                    note=f"Location quotient vs U.S.: {vals[4] / vals[6]:.2f}x — "
                         + ("more" if vals[4] > vals[6] else "less") + " concentrated here.")
                for label, vals in [
                    ("Health Care/Social Asst", [0.079, 0.089, 0.104, 0.127, 0.144, 0.148, 0.152]),
                    ("Retail Trade", [0.091, 0.091, 0.086, 0.083, 0.109, 0.110, 0.102]),
                    ("Accommodation/Food Svcs", [0.049, 0.068, 0.058, 0.076, 0.092, 0.098, 0.092]),
                    ("Professional/Scientific/Tech", [0.061, 0.074, 0.081, 0.072, 0.077, 0.070, 0.086]),
                    ("Educational Services", [0.207, 0.118, 0.092, 0.070, 0.075, 0.081, 0.092]),
                    ("Admin/Support/Waste", [0.038, 0.052, 0.061, 0.058, 0.070, 0.066, 0.058]),
                    ("Finance/Insurance", [0.021, 0.038, 0.049, 0.043, 0.061, 0.055, 0.045]),
                    ("Construction", [0.031, 0.041, 0.047, 0.052, 0.058, 0.060, 0.052]),
                    ("Manufacturing", [0.018, 0.029, 0.038, 0.049, 0.052, 0.055, 0.086]),
                    ("Transportation/Warehousing", [0.014, 0.024, 0.031, 0.041, 0.046, 0.044, 0.042]),
                ]
            ],
        },
    ]


#: Duplicate-row shapes the de-duplication check must handle, carried in the TRACKED
#: fixture so the check is exercised on a fresh clone. Built from the real export's own
#: shapes, because a synthetic case invented from the spec cannot falsify the spec:
#:
#:   D-0ZERO   incoming lease states $0        -> the shape that halved the exposure when
#:                                                a stated zero was read as an absence
#:   D-LOWER   incoming lease states less      -> the ordinary turnover pair
#:   D-NOCUR   no row carries the in-place     -> keeping the other row is CORRECT and
#:             status, alternative is blank $0    must NOT report as a wrong choice
FIXTURE_DUPLICATE_DETAIL: list[dict] = [
    {
        "unit_id": "D-0ZERO", "kept_row": 41, "kept_lease_status": "Current",
        "kept_rent": 1160.0, "kept_rent_stated": 1160.0, "kept_market_rent": 1290.0,
        "dropped": [{"row": 40, "lease_status": "New", "rent": None,
                     "rent_stated": 0.0, "market_rent": 1290.0}],
        "candidate_rows": [40, 41],
    },
    {
        "unit_id": "D-LOWER", "kept_row": 51, "kept_lease_status": "Current",
        "kept_rent": 1969.0, "kept_rent_stated": 1969.0, "kept_market_rent": 1515.0,
        "dropped": [{"row": 50, "lease_status": "New", "rent": 1530.0,
                     "rent_stated": 1530.0, "market_rent": 1515.0}],
        "candidate_rows": [50, 51],
    },
    {
        "unit_id": "D-NOCUR", "kept_row": 60, "kept_lease_status": "New",
        "kept_rent": 1310.0, "kept_rent_stated": 1310.0, "kept_market_rent": 1310.0,
        "dropped": [{"row": 61, "lease_status": None, "rent": None,
                     "rent_stated": 0.0, "market_rent": 1310.0}],
        "candidate_rows": [60, 61],
    },
]

#: The exposure the three shapes above must produce: (1160-0) + (1969-1530), with D-NOCUR
#: excluded because no in-place row existed to choose. Pinned as a literal so a change to
#: the arithmetic has to be deliberate.
FIXTURE_DUP_EXPOSURE = 1599.0

#: What the synthetic source export "states" about itself, so the tie-outs have a source
#: side. Occupancy is left None on purpose: a real export very often states no occupancy
#: figure, and the fixture has to exercise the NOT CHECKED path as well as the OK one.
FIXTURE_ROLL_STATED = {
    "units_total": 180,
    "sqft": 178_020.0,
    "occupancy": None,
    "sheet": "Rent Roll (source)",
    "rent_column": "F",
    "sqft_column": "D",
    "duplicate_units": ["D-0ZERO", "D-LOWER", "D-NOCUR"],
    "duplicate_detail": FIXTURE_DUPLICATE_DETAIL,
    "in_place_lease_status": "Current",
}


def build_demand() -> dict:
    return {
        "rings": [
            {"ring": "1 mi", "population": 9840, "households": 4020, "median_income": 78400,
             "jobs": 3150, "pop_growth_5yr": 0.061, "renter_share": 0.44},
            {"ring": "3 mi", "population": 61250, "households": 24100, "median_income": 84900,
             "jobs": 27400, "pop_growth_5yr": 0.078, "renter_share": 0.39},
            {"ring": "5 mi", "population": 148300, "households": 57600, "median_income": 89200,
             "jobs": 71800, "pop_growth_5yr": 0.084, "renter_share": 0.36},
            {"ring": "10 mi", "population": 402700, "households": 154900, "median_income": 92600,
             "jobs": 214500, "pop_growth_5yr": 0.091, "renter_share": 0.34},
            {"ring": "MSA", "population": 2418000, "households": 903400, "median_income": 86100,
             "jobs": None, "pop_growth_5yr": 0.073, "renter_share": 0.37},
        ],
        "columns": list(DEMAND_COLUMNS),
        "sections": _demand_sections(),
        "engine_note": (
            "Synthetic ring demographics in the shape the real engine produces: ACS "
            "5-year, a 2020 decennial baseline, LODES workplace counts and BLS metro "
            "series. All public sources; no licensed provider."
        ),
        "data_year": 2024,
        "output_year": 2026,
        "ring_1mi_caveat": FIXTURE_1MI_CAVEAT,
        "matched_address": "1400 Sample Parkway, Demo City",
        "level_row_hints": ["households", "population", "jobs"],
        "absent_reason": None,
    }


def build_supply() -> dict:
    rings = []
    data = {
        "1 mi": {"Entitlement": (1, 220), "Permitted": (1, 148), "Under construction": (0, 0), "Delivered (24 mo)": (1, 96)},
        "3 mi": {"Entitlement": (3, 742), "Permitted": (2, 410), "Under construction": (2, 508), "Delivered (24 mo)": (3, 624)},
        "5 mi": {"Entitlement": (6, 1485), "Permitted": (4, 903), "Under construction": (5, 1240), "Delivered (24 mo)": (7, 1608)},
    }
    for ring, stages in data.items():
        for stage, (projects, units) in stages.items():
            rings.append({"ring": ring, "stage": stage, "projects": projects, "units": units})
    return {
        "rings": rings,
        "projects": build_supply_projects(),
        "ring_stock": {"1 mi": 3140, "3 mi": 18600, "5 mi": 41200},
        "absent_reason": None,
    }


def build_supply_projects() -> list[dict]:
    """The ten developments inside three miles, nearest first.

    They ARE the 1 mi and 3 mi ring rows above: counted by ring and stage they reproduce
    them exactly, units included. A synthetic fixture whose list and counts disagreed would
    be teaching the writers a shape the real producer never emits.
    """
    rows = [
        ("Cedar Row", 0.3, "Delivered (24 mo)", 96, "610 Cedar St", 4.1, "Cedar Row Partners",
         "2023-02-14", "2024-06-30", ["Permit"]),
        ("Northgate Flats", 0.6, "Entitlement", 220, None, 8.4, None,
         "2026-01-09", "2026-05-22", ["Entitlement"]),
        ("Willow Bend Phase II", 0.9, "Permitted", 148, "1802 Willow Bend Dr", 6.0, "Willow Bend Development",
         "2025-11-03", "2026-04-18", ["Entitlement", "Permit"]),
        ("Maple & Seventh", 1.4, "Under construction", 268, "700 Maple Ave", 5.2, None,
         "2025-04-21", "2026-08-01", ["Permit"]),
        ("Sample Creek Crossing", 1.7, "Entitlement", 330, None, 12.6, "Creek Crossing Holdings",
         "2026-03-30", "2026-07-15", ["Entitlement"]),
        ("Harbor Lofts", 1.9, "Delivered (24 mo)", 240, "55 Harbor Way", 3.4, None,
         "2022-08-08", "2024-11-12", ["Permit"]),
        ("Vista Terrace", 2.2, "Under construction", 240, "3400 Vista Pkwy", 9.8, "Terrace Residential",
         "2025-01-27", "2026-09-02", ["Permit"]),
        ("Old Mill Yards", 2.5, "Entitlement", 192, None, 7.1, None,
         "2026-02-17", "2026-06-05", ["Entitlement"]),
        ("Depot Square", 2.7, "Permitted", 262, "12 Depot Sq", 5.5, "Depot Square LLC",
         "2025-09-15", "2026-08-28", ["Entitlement", "Permit"]),
        ("Brickline Apartments", 2.9, "Delivered (24 mo)", 288, "980 Brickline Rd", 6.7, None,
         "2022-05-02", "2024-09-19", ["Permit"]),
    ]
    return [
        {
            "name": name, "miles": miles, "stage": stage, "units": units,
            "product": "multifamily", "address": address,
            "city": None if address is None else "Demo City",
            "acres": acres, "developer": developer, "filed": filed, "latest": latest,
            "layers": layers,
        }
        for name, miles, stage, units, address, acres, developer, filed, latest, layers in rows
    ]


SYN_OM = "Synthetic offering memorandum"
SYN_T12 = "Synthetic trailing-twelve income statement"
SYN_ROLL = "Synthetic rent roll export"
SYN_TAX = "Synthetic county tax record"
SYN_TRADE = "Synthetic lease trade-out report"
SYN_SCRAPE = "Synthetic weekly rent scrape"
SYN_PUBLIC = "Synthetic public data extract"
SYN_OP = "Supplied by the operator"


def build_provenance() -> dict:
    """A stamp for every required path. Synthetic, but well-formed and complete —
    the fixture has to exercise the same validation the real deal does."""
    import sys as _sys
    _sys.path.insert(0, str(HERE.parent))
    import provenance as pv

    P: dict = {}
    for key, loc, quote in (
        ("deal_name", "p.1", "Fixture Park"),
        ("street_address", "p.4", "1400 Example Parkway"),
        ("city", "p.4", None),
        ("state", "p.4", None),
        ("zip", "p.4", None),
        ("county", "p.4", None),
        ("product_type", "p.6", None),
        ("structure", "p.6", "Three-story garden, surface parked"),
        ("year_built", "p.6", None),
        ("product_detail", "p.7", None),
        ("construction_status", "p.6", None),
        ("delivery_date", "p.6", None),
        ("seller_entity", "p.2", None),
        ("deal_status", "p.2", None),
    ):
        P[f"summary.{key}"] = pv.read(SYN_OM, loc, quote=quote)

    P["summary.msa"] = pv.derived(
        "Derived by the pipeline", "—", ["summary.city", "summary.state"],
        note="Metro assigned from the city/state pair.")
    P["summary.listing_brokerage"] = pv.derived(
        "Derived by the pipeline", "—", ["summary.seller_entity"],
        note="Generic label; no firm or individual is named in a published file.")
    P["summary.units_total"] = pv.derived(
        SYN_ROLL, "Rent Roll!A:A", ["rent_roll.units"],
        note="Distinct unit identifiers on the roll.")

    P["summary.pricing_guidance"] = pv.read(SYN_OP, "—",
        note="Illustrative figure supplied directly; not a quote or an offer.")
    P["pricing.price_per_unit"] = pv.read(SYN_OP, "—",
        note="Illustrative. Inside the band a per-unit sale price occupies.")
    P["pricing.units_total"] = pv.derived(
        SYN_ROLL, "Rent Roll!A:A", ["summary.units_total"])

    import write_rent_roll as _wrr

    P["summary.in_place_rent_avg"] = pv.computed(
        "Derived by the pipeline", _wrr.mix_locator("in_place_rent"), ["rent_roll.units"],
        note="Occupied units, straight off the roll.")
    P["summary.market_rent_for_model"] = pv.computed(
        "Derived by the pipeline", _wrr.mix_locator("market_rent_for_model"),
        ["rent_roll.units"],
        note="New move-in rent where a plan has one in the window, else in-place. "
             "Never a blend.")
    P["summary.fair_market_value"] = pv.read(SYN_OP, "—",
        note="Operator's own estimate.")

    for key, loc in (
        ("tax_rate_as_billed", "Rate table, synthetic district"),
        ("assessed_value_basis", "Valuation notice"),
        ("assessment_ratio", "Rate table"),
        ("tax_divisor", "Rate table"),
        ("tax_exemption", "Valuation notice"),
        ("tax_parcel_id", "Parcel record"),
        ("tax_area_code", "Rate table"),
        ("tax_billed_amount", "Tax bill"),
        ("tax_billed_year", "Tax bill"),
    ):
        P[f"summary.{key}"] = pv.read(SYN_TAX, loc)
    P["summary.tax_basis_label"] = pv.read(SYN_TAX, "Valuation notice")
    P["summary.tax_basis_note"] = pv.read(SYN_TAX, "Valuation notice")

    for lvl in ("elementary", "middle", "high"):
        P[f"summary.school_{lvl}_name"] = pv.read(SYN_PUBLIC, "School attendance lookup")

    P["t12.month_labels"] = pv.read(SYN_T12, "T-12!C8:N8",
        note="Twelve consecutive month-end columns; direction detected from the labels.")
    P["t12.units"] = pv.derived(SYN_ROLL, "—", ["summary.units_total"])
    P["t12.source_subtotals"] = pv.read(SYN_T12, "T-12!O",
        note="The statement's own printed subtotals, kept for the cross-tie.")
    P["rent_roll.as_of"] = pv.read(SYN_ROLL, "Rent Roll!A2")
    P["rent_roll.lookback_months"] = pv.read(SYN_OP, "—", note="Analysis knob.")
    P["rent_roll.new_tenant_tolerance_days"] = pv.read(SYN_OP, "—", note="Analysis knob.")
    P["rent_roll.units"] = pv.read(SYN_ROLL, "Rent Roll!A4:K183",
        note="Unit identifiers only; the source's resident-name column is never read.")
    P["comps.rows"] = pv.read(SYN_SCRAPE, "—",
        note="Own weekly scrape; asking and net effective stated separately.")
    P["comps.rows.tier"] = pv.derived(
        "Tracked-community registry + the scrape",
        "comp_tiers.assign",
        ["comps.rows.distance_mi", "comps.tiering.subject_product",
         "comps.tiering.subject_year_built", "comps.tiering.year_band",
         "comps.tiering.top_n", "comps.tiering.additional_n"],
        note="Tier 1 = the closest 5 matching product and vintage; tier 2 = the next 15; "
             "everything else is tier 3, identity only. An unknown year does not "
             "disqualify — it is disclosed on the tier header instead.")
    P["comps.nearby"] = pv.derived(
        "Tracked-community registry",
        "rents.communities within the radius",
        ["comps.rows.tier"],
        note="Every tracked community inside the radius that is not a detail row, with "
             "the reason it is not. No rent columns.")
    P["demand.rings"] = pv.read(SYN_PUBLIC, "—", note="Ring demographics.")
    P["supply.rings"] = pv.read(SYN_PUBLIC, "—", note="Permit and entitlement records.")
    P["trade_out.rows"] = pv.read(SYN_TRADE, "—", note="Cohort averages.")
    P["closing_costs.transfer_tax.amount"] = pv.computed(
        "Vendored state rate table", "closing_costs.TRANSFER_TAX",
        ["pricing.price_per_unit", "pricing.units_total", "summary.state",
         "closing_costs.price"],
        note="Rate applied to price_per_unit x units_total. Per-unit statutes round the "
             "consideration up.")
    P["closing_costs.mortgage_tax.amount"] = pv.computed(
        "Vendored state rate table", "closing_costs.MORTGAGE_TAX",
        ["closing_costs.loan_amount", "summary.state"],
        note="Computed on the LOAN, never on the price. With no loan amount the line is "
             "reported as not asserted rather than inferred at 100% leverage.")
    return P


def _closing_costs() -> dict:
    import sys as _sys
    _sys.path.insert(0, str(HERE.parent))
    import closing_costs as cc
    return cc.for_state("TX", 215000 * UNITS_TOTAL)


def build_fixture() -> dict:
    units = build_units()
    t12 = build_t12(units)

    # Rent figures on the summary card are the roll's own answers, so the card and the
    # rent-roll workbook cannot disagree.
    sys.path.insert(0, str(HERE.parent))
    import compute  # noqa: E402  (path set above)

    partial = {
        "meta": {},
        "summary": {"units_total": UNITS_TOTAL},
        "t12": t12,
        "rent_roll": {
            "stated": {
                **FIXTURE_ROLL_STATED,
                # Summed from the units themselves, so the tie-out is a real comparison
                # rather than a constant echoed back.
                "market_rent": round(
                    sum(u.get("market_rent") or 0 for u in units), 2
                ),
            },
            "as_of": AS_OF,
            "lookback_months": 12,
            "new_tenant_tolerance_days": 31,
            "units": units,
        },
        "trade_out": {"rows": [], "absent_reason": None},
        "pricing": {"price_per_unit": 215000, "units_total": UNITS_TOTAL},
        "comps": build_comps(),
        "demand": build_demand(),
        "supply": build_supply(),
    }
    totals = compute.roll_totals(partial)

    # The comp block compares the subject's MODELED market rent against the comp set --
    # not its in-place rent. On this fixture those differ by ~$39/unit, which was enough
    # to halve the reported spread and mislabel which rent was being compared.
    comps = build_comps()
    comps["subject_market_rent"] = round(totals["wavg_market_rent_for_model"], 2)

    trade_out = {
        "rows": [
            {"cohort": "New", "leases": 46, "prior_rent": 1706, "new_rent_gross": 1812,
             "new_rent_effective": 1774},
            {"cohort": "Renewal", "leases": 88, "prior_rent": 1698, "new_rent_gross": 1745,
             "new_rent_effective": 1745},
            {"cohort": "First generation", "leases": 12, "prior_rent": 0,
             "new_rent_gross": 1836, "new_rent_effective": 1790},
        ],
        "absent_reason": None,
    }

    return {
        "meta": {
            "toolkit_name": "AI Underwriting Pipeline — Sample Outputs",
            "deal_name": "Fixture Park",
            "deal_slug": "fixture-park",
            "generated_on": "2026-09-01",
            "as_of_date": AS_OF,
            "illustrative_pricing": True,
            "pricing_note": (
                "Every price and cap rate below is illustrative. The per-unit price is a "
                "round number chosen to sit inside the band a real per-unit sale price "
                "occupies; it is not a quote, an offer, or a broker's guidance."
            ),
            "synthetic": True,
            "provenance": [
                {"document": "Synthetic offering memorandum", "kind": "om",
                 "note": "Fabricated for demonstration. No real property is described."},
                {"document": "Synthetic trailing-twelve income statement", "kind": "t12",
                 "note": "Monthly matrix generated to exercise the rollup's edge cases."},
                {"document": "Synthetic rent roll export", "kind": "rent_roll",
                 "note": "180 units across 6 plans, including vacancies and a model unit."},
                {"document": "Synthetic lease trade-out report", "kind": "trade_out",
                 "note": "Cohort averages, gross and effective stated separately."},
            ],
            "excluded_sources": [
                "Third-party licensed rent-comp feeds",
                "Subscription market-data providers",
                "Any employer-owned deal data or template",
            ],
            # Derived after the dict is built, so the numeric notes describe THIS deal.
            "notes": [],
        },
        "summary": {
            "deal_name": "Fixture Park",
            "street_address": "1400 Example Parkway",
            "city": "Sample City",
            "state": "TX",
            "zip": "75000",
            "county": "Example County",
            "msa": "Sample City MSA",
            "address_provenance": (
                "Street address read from the synthetic marketing package; city/state/ZIP "
                "confirmed against the same document."
            ),
            "product_type": "MF",
            "structure": "Three-story garden, surface parked",
            "units_total": UNITS_TOTAL,
            "year_built": 2019,
            "product_detail": (
                "180 units across six plans: 62 one-bedroom (720–795 SF), 78 two-bedroom "
                "(1,050–1,140 SF), 26 three-bedroom flats (1,380 SF), 14 three-bedroom "
                "townhomes (1,620 SF). Washer/dryer in unit; detached garages on 24 units."
            ),
            "construction_status": "Stabilized",
            "delivery_date": "Delivered 2019; fully stabilized since 2021",
            "seller_entity": "Sample City Residential Holdings LLC",
            "listing_brokerage": "Listing brokerage",
            "deal_status": "On the market",
            "pricing_guidance": "$215,000 per unit (illustrative)",
            "in_place_rent_avg": round(totals["wavg_in_place_rent"], 2),
            "market_rent_for_model": round(totals["wavg_market_rent_for_model"], 2),
            "fair_market_value": 39600000,
            "tax_rate_as_billed": 2.148,
            "tax_rate_evidence": (
                "Rate as billed, 2.148 per $100 of assessed value, read from the synthetic "
                "county bill for the 2025 tax year."
            ),
            "assessed_value_basis": 23000000,
            "assessment_ratio": 1.0,
            "tax_divisor": 100,
            "tax_exemption": 0,
            "tax_parcel_id": "SYN-000-00-000",
            "tax_area_code": "SYN-0000",
            "tax_basis_label": "Assessed value (synthetic)",
            "tax_basis_note": (
                "Synthetic basis. On a real deal this row states what the basis IS in that "
                "state and whether it resets on sale."
            ),
            "tax_billed_amount": 494040,
            "tax_billed_year": 2025,
            "school_elementary_name": "Example Creek Elementary",
            "school_elementary_rating": 7,
            "school_middle_name": "Example Ridge Middle",
            "school_middle_rating": 6,
            "school_high_name": "Sample City High",
            "school_high_rating": 7,
            "notes": (
                "Synthetic deal built to exercise the output formats. Pricing is "
                "illustrative and no licensed data source was used. Comps come only from "
                "an operator's own weekly rent scrape."
            ),
        },
        "t12": t12,
        "rent_roll": {
            "stated": {
                **FIXTURE_ROLL_STATED,
                # Summed from the units themselves, so the tie-out is a real comparison
                # rather than a constant echoed back.
                "market_rent": round(
                    sum(u.get("market_rent") or 0 for u in units), 2
                ),
            },
            "as_of": AS_OF,
            "lookback_months": 12,
            "new_tenant_tolerance_days": 31,
            "units": units,
        },
        "trade_out": trade_out,
        "pricing": {"price_per_unit": 215000, "units_total": UNITS_TOTAL},
        "comps": comps,
        "demand": build_demand(),
        "supply": build_supply(),
        "closing_costs": _closing_costs(),
        "provenance": build_provenance(),
    }


#: The fixture output set. Its interchange file is the one publish_to_site.py inspects, so
#: it has to sit beside the workbooks rather than only in tools/fixtures/ — an external
#: file cannot vouch for artifacts it does not sit beside.
SET_DIR = HERE.parent.parent / "outputs" / "fixture"
SET_JSON_NAME = "deal_outputs.json"


#: Fixture-specific notes that cannot be derived from its numbers.
FIXTURE_NOTES: list[dict] = [
    {
        "topic": "This deal does not exist",
        "applies_to": "throughout",
        "note": (
            "Fixture Park is fabricated end to end — the property, the operating "
            "statement, the rent roll, the comps and the demographics. It exists to "
            "exercise every edge the writers handle, so some of its figures are "
            "deliberately awkward: a plan with no priced units, a comp community with no "
            "published year, a revenue line the source statement never subtotalled."
        ),
    },
    {
        "topic": "The 1-mile demographic ring overstates levels",
        "applies_to": "Demand",
        "note": FIXTURE_1MI_CAVEAT,
    },
    {
        "topic": "The rent readback checks transcription, not column choice",
        "applies_to": "Error Checks & Pricing",
        "note": (
            "Ten units are sampled and their normalized in-place rent is compared against "
            "the raw cell it came from, which catches a coercion or transcription error. "
            "It cannot catch the wrong rent column having been chosen, because both sides "
            "read the same column. The sample is seeded from the roll's as-of date, so "
            "the same roll always checks the same units."
        ),
    },
]


def main() -> int:
    deal = build_fixture()
    sys.path.insert(0, str(HERE.parent))
    import schema  # noqa: E402

    # Validate before deriving notes: the derivation reads the comps block, so a malformed
    # one must surface as a validation error rather than a traceback inside note-building.
    pre = schema.validate(deal)
    if pre:
        print("fixture FAILED validation (before note derivation):")
        for err in pre:
            print(f"  - {err}")
        return 1
    deal["meta"]["notes"] = _notes.for_deal(deal, FIXTURE_NOTES)

    errors = schema.validate(deal)
    if errors:
        print("fixture FAILED validation:")
        for err in errors:
            print(f"  - {err}")
        return 1
    payload = json.dumps(deal, indent=2, ensure_ascii=False) + "\n"
    OUT.write_text(payload, encoding="utf-8")
    print(f"wrote {OUT}")
    # Byte-identical copy into the output set, so the publish guard reads the same file the
    # workbooks were built from and the two can never disagree.
    if SET_DIR.exists():
        set_json = SET_DIR / SET_JSON_NAME
        set_json.write_text(payload, encoding="utf-8")
        print(f"wrote {set_json}")
    print(f"  units={len(deal['rent_roll']['units'])} plans={len({u['floor_plan'] for u in deal['rent_roll']['units']})}")
    print(f"  t12 rows={len(deal['t12']['rows'])} months={len(deal['t12']['month_labels'])}")
    print(f"  comp rows={len(deal['comps']['rows'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
