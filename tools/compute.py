"""Pure-Python mirror of every formula the writers emit.

openpyxl writes formula *strings* and no cached values, so a workbook straight off the
writer has empty result cells until Excel opens it. That is fine for the download, but the
HTML previews need real numbers, and something has to be able to say what the right answer
is. This module is that something: one implementation of each derivation, used by the
previews, by the tests, and by the round-trip QA that compares Excel's own recalc against
it.

Everything here is a pure function of the deal dict. No I/O, no Excel.
"""

from __future__ import annotations

import itertools

import calendar
import datetime as _dt
import math
import re
from typing import Any, Iterable, Sequence

import property_tax_table
import schema

Number = int | float

#: Below this share of the subject's units matched by bed count, a single headline spread
#: is not published — it would describe the mix rather than the market.
MIX_COVERAGE_FLOOR = 0.80

#: Tier labels, mirrored from comp_tiers so compute stays importable without it. The
#: numbers are the contract; a mismatch here is caught by the writer tests.
COMP_TIER_LABELS: dict[int, str] = {1: "Top comps", 2: "Additional comps", 3: "Also nearby"}

# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------


def fsum(values: Iterable[float]) -> float:
    """A correctly-rounded sum of floats. Use this for money, never ``sum``.

    ``sum()`` is not the same function on every interpreter: CPython 3.12 gave it
    compensated (Neumaier) accumulation for floats, so 3.11 and 3.12 return *different
    values* for the same list. That showed up here as a workbook whose bytes differed
    between the build machine (3.12) and the container (3.11) — a market-rent total of
    342300.5 against 342300.5000000001, and a tie-out difference cell that moved off zero
    as a result.

    Byte-identical output is a claim this project makes and tests, and it cannot be true
    while a total depends on which interpreter ran it. ``math.fsum`` is correctly rounded
    on every version, so it is the same number everywhere.

    Integer counts do not need this and must not use it — it would turn a count into a
    float.
    """
    return math.fsum(v for v in values if v is not None)


def as_number(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not a number.

    A string is never coerced. ``"MISSING"`` and ``"n/a"`` are data, not zero.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_date(value: Any) -> _dt.date | None:
    """Parse ``YYYY-MM-DD``. Anything else -- including ``"n/a"`` -- returns ``None``.

    Deliberately strict: a date column that arrives as text stays text everywhere
    downstream rather than becoming 1899-12-31.
    """
    if isinstance(value, _dt.date):
        return value
    if not isinstance(value, str):
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


def months_between(earlier: _dt.date, later: _dt.date) -> float:
    """Whole-ish months from ``earlier`` to ``later``, fractional on the day part."""
    return (later.year - earlier.year) * 12 + (later.month - earlier.month) + (
        (later.day - earlier.day) / 30.0
    )


def safe_div(numerator: Any, denominator: Any) -> float | None:
    """Mirror of ``=IFERROR(a/b,"")`` -- ``None`` where Excel would show blank."""
    n = as_number(numerator)
    d = as_number(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def mean(values: Iterable[Any]) -> float | None:
    nums = [as_number(v) for v in values]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None
    return fsum(nums) / len(nums)


def total(values: Iterable[Any]) -> float:
    return fsum(n for n in (as_number(v) for v in values) if n is not None)


# --------------------------------------------------------------------------------------
# Month direction
# --------------------------------------------------------------------------------------


def month_key(label: str) -> tuple[int, int]:
    year, month = label.split("-")
    return int(year), int(month)


def detect_month_order(labels: Sequence[str]) -> str:
    """``"oldest_first"`` or ``"newest_first"``, detected from the labels themselves.

    The T-3 window depends on this, and a statement that runs newest-first is common
    enough that assuming a direction is how a T-3 silently becomes the *oldest* three
    months. Ties (a single column) read as oldest-first.
    """
    if len(labels) < 2:
        return "oldest_first"
    return "oldest_first" if month_key(labels[0]) <= month_key(labels[-1]) else "newest_first"


def recent_indices(labels: Sequence[str], n: int) -> list[int]:
    """Indices of the ``n`` most recent months, in the labels' own order.

    Contiguous by construction, which is what lets the workbook express the same window
    as a single ``SUM`` range.
    """
    n = max(0, min(n, len(labels)))
    if detect_month_order(labels) == "oldest_first":
        return list(range(len(labels) - n, len(labels)))
    return list(range(0, n))


def recent_span(labels: Sequence[str], n: int) -> tuple[int, int]:
    """(first_index, last_index) of the ``n`` most recent months, inclusive."""
    idx = recent_indices(labels, n)
    if not idx:
        return (0, -1)
    return (idx[0], idx[-1])


# --------------------------------------------------------------------------------------
# T-12
# --------------------------------------------------------------------------------------


def row_total(row: dict, indices: Sequence[int] | None = None) -> float:
    monthly = row.get("monthly") or []
    if indices is None:
        return total(monthly)
    return total(monthly[i] for i in indices if 0 <= i < len(monthly))


def t12_row_totals(deal: dict) -> list[dict]:
    """Per source line: label, category, T-12 total, T-3 total."""
    t12 = deal["t12"]
    labels = t12["month_labels"]
    t3 = recent_indices(labels, 3)
    out = []
    for row in t12["rows"]:
        out.append(
            {
                "label": row["label"],
                "category": row.get("category"),
                "t12": row_total(row),
                "t3": row_total(row, t3),
            }
        )
    return out


def category_total(deal: dict, category: str, *, window: int = 12) -> float:
    """Mirror of the rollup's ``SUMIF`` over the data tab."""
    labels = deal["t12"]["month_labels"]
    indices = None if window == 12 else recent_indices(labels, window)
    return total(
        row_total(row, indices)
        for row in deal["t12"]["rows"]
        if row.get("category") == category
    )


def _revenue_tail() -> tuple[str, ...]:
    """Revenue categories after Gross Scheduled Rent, through Revenue Adjustments."""
    cats = schema.T12_REVENUE_CATEGORIES
    return cats[cats.index("Gross Scheduled Rent") + 1 : cats.index("Revenue Adjustments") + 1]


def gsr_is_printed(deal: dict) -> bool:
    """Whether the statement carries its own Gross Scheduled Rent line.

    Presence is the **existence of a categorized row**, not a nonzero total. A statement
    can print a gross scheduled rent line whose twelve months net to zero — offsetting
    adjustments, or a mid-year restatement — while its recent months are large. Deciding
    from the aggregate then substitutes gross potential rent plus gain/(loss) to lease
    into the T-3 and T-6 windows, which is wrong by hundreds of thousands of dollars and
    looks like nothing on the output. The workbook makes the same decision with a COUNTIF.
    """
    return any(
        row.get("category") == "Gross Scheduled Rent" for row in deal["t12"]["rows"]
    )


def revenue_for_window(deal: dict, window: int = 12) -> float:
    """Total revenue over the ``window`` most recent months.

    Uses the same printed-else-derived Gross Scheduled Rent rule as :func:`pnl`. Getting
    this wrong is not a rounding problem: summing the rollup's revenue *span* without
    applying the fallback picks up only the contra rows on a statement that prints no GSR
    line, and the total comes out negative.
    """
    labels = deal["t12"]["month_labels"]
    indices = None if window >= len(labels) else recent_indices(labels, window)

    def cat_sum(category: str) -> float:
        return total(
            row_total(row, indices)
            for row in deal["t12"]["rows"]
            if row.get("category") == category
        )

    if gsr_is_printed(deal):
        gsr = cat_sum("Gross Scheduled Rent")
    else:
        gsr = cat_sum("Gross Potential Rent") + cat_sum("Gain / (Loss) to Lease")
    return gsr + fsum(cat_sum(cat) for cat in _revenue_tail())


def pnl(deal: dict) -> dict[str, Any]:
    """The rollup: every category row plus the derived subtotals.

    Two rules carry the weight here. Gross Scheduled Rent falls back to gross potential
    rent plus gain/(loss) to lease when the statement did not print a GSR line. Total
    Revenue sums from GSR down through Revenue Adjustments -- never from gross potential
    rent, which would count loss-to-lease twice.
    """
    units = deal["t12"]["units"]
    rows: dict[str, dict[str, float | None]] = {}
    # A category no line was mapped to is ABSENT, not $0: the P&L rendered every
    # unmapped category — Gross Potential Rent on a statement printing only Gross Scheduled
    # Rent — as a $0 figure. Presence is a categorised row existing, the rule
    # gsr_is_printed() already applies; a mapped line whose months net to zero is a value.
    # The totals below treat an absent row as contributing nothing.
    mapped = {row.get("category") for row in deal["t12"]["rows"]}

    def put(label: str, t12_val: float | None, t3_val: float | None) -> None:
        rows[label] = {
            "t12": t12_val,
            "t3": t3_val,
            "t3_annualized": None if t3_val is None else t3_val * 4,
        }

    def put_category(cat: str) -> None:
        if cat in mapped:
            put(cat, category_total(deal, cat), category_total(deal, cat, window=3))
        else:
            put(cat, None, None)

    for cat in schema.T12_REVENUE_CATEGORIES:
        if cat in ("Gross Scheduled Rent", "Total Revenue"):
            continue
        put_category(cat)

    # Gross Scheduled Rent: printed where it exists, else GPR + GLL. The decision is made
    # once, over all twelve months — not per column — so the T-3 column cannot fall back
    # while the T-12 column does not.
    printed = gsr_is_printed(deal)
    gpr = rows["Gross Potential Rent"]
    gll = rows["Gain / (Loss) to Lease"]
    if printed:
        gsr_12 = category_total(deal, "Gross Scheduled Rent")
        gsr_3 = category_total(deal, "Gross Scheduled Rent", window=3)
    elif gpr["t12"] is None and gll["t12"] is None:
        gsr_12 = gsr_3 = None
    else:
        gsr_12 = (gpr["t12"] or 0) + (gll["t12"] or 0)
        gsr_3 = (gpr["t3"] or 0) + (gll["t3"] or 0)
    put("Gross Scheduled Rent", gsr_12, gsr_3)
    gsr_basis = (
        "printed on the statement"
        if printed
        else "gross potential rent + gain/(loss) to lease"
    )

    # Total Revenue: GSR through Revenue Adjustments.
    rev_span = schema.T12_REVENUE_CATEGORIES[
        schema.T12_REVENUE_CATEGORIES.index("Gross Scheduled Rent") : schema.T12_REVENUE_CATEGORIES.index(
            "Revenue Adjustments"
        )
        + 1
    ]
    total_rev_12 = fsum(rows[c]["t12"] or 0 for c in rev_span)
    total_rev_3 = fsum(rows[c]["t3"] or 0 for c in rev_span)
    put("Total Revenue", total_rev_12, total_rev_3)

    for cat in schema.T12_EXPENSE_CATEGORIES:
        put_category(cat)

    opex_12 = fsum(rows[c]["t12"] or 0 for c in schema.T12_EXPENSE_CATEGORIES)
    opex_3 = fsum(rows[c]["t3"] or 0 for c in schema.T12_EXPENSE_CATEGORIES)
    put(schema.PNL_TOTAL_OPEX_LABEL, opex_12, opex_3)
    put(schema.PNL_NOI_LABEL, total_rev_12 - opex_12, total_rev_3 - opex_3)

    for label, vals in rows.items():
        vals["per_unit"] = safe_div(vals["t12"], units)
        vals["pct_of_revenue"] = safe_div(vals["t12"], total_rev_12)

    return {
        "rows": rows,
        "units": units,
        "gsr_basis": gsr_basis,
        "total_revenue": total_rev_12,
        "total_opex": opex_12,
        "noi": total_rev_12 - opex_12,
        "total_revenue_t3_annualized": total_rev_3 * 4,
        "total_opex_t3_annualized": opex_3 * 4,
        "noi_t3_annualized": (total_rev_3 - opex_3) * 4,
        "revenue_span": tuple(rev_span),
    }


#: How many lines each breakout shows on top. Set by the model's DESTINATION, not by
#: taste (E2 rule 2): the financial model has five Other Income rows and four each for
#: Utilities and Utility Reimbursements, so that is how many the analysis names.
BREAKOUT_DEPTH: dict[str, int] = {"Other Income": 5}
BREAKOUT_DEFAULT_DEPTH = 4


def breakouts(deal: dict) -> dict[str, dict[str, Any]]:
    """Top-N-plus-Rest per breakout category, N from :data:`BREAKOUT_DEPTH`, tying to the rollup row."""
    labels = deal["t12"]["month_labels"]
    t3 = recent_indices(labels, 3)
    out: dict[str, dict[str, Any]] = {}
    for cat in schema.BREAKOUT_CATEGORIES:
        members = [r for r in deal["t12"]["rows"] if r.get("category") == cat]
        scored = sorted(
            (
                {
                    "label": r["label"],
                    "t12": row_total(r),
                    "t3_annualized": row_total(r, t3) * 4,
                }
                for r in members
            ),
            key=lambda d: abs(d["t12"]),
            reverse=True,
        )
        depth = BREAKOUT_DEPTH.get(cat, BREAKOUT_DEFAULT_DEPTH)
        top = scored[:depth]
        rest = scored[depth:]
        # A category no line was mapped to is ABSENT: its breakout totals are None,
        # the way its P&L row is, never a $0 under a "—".
        present = bool(members)
        out[cat] = {
            "depth": depth,
            "top": top,
            "rest_t12": fsum(d["t12"] for d in rest) if present else None,
            "rest_t3_annualized": fsum(d["t3_annualized"] for d in rest) if present else None,
            "rest_count": len(rest),
            "total_t12": fsum(d["t12"] for d in scored) if present else None,
            "total_t3_annualized": fsum(d["t3_annualized"] for d in scored) if present else None,
        }
    return out


def error_checks(deal: dict) -> list[dict[str, Any]]:
    """Cross-tie our category totals against the subtotals printed on the source.

    A difference over a dollar reads REVIEW. A subtotal the source never printed reads
    NOT CHECKED -- not OK, which would claim a tie that was never tested.
    """
    p = pnl(deal)
    source = deal["t12"].get("source_subtotals") or {}
    checks = [
        ("Total Revenue", p["total_revenue"]),
        (schema.PNL_TOTAL_OPEX_LABEL, p["total_opex"]),
        (schema.PNL_NOI_LABEL, p["noi"]),
    ]
    for cat in schema.BREAKOUT_CATEGORIES:
        checks.append((cat, p["rows"][cat]["t12"]))
    out = []
    for label, ours in checks:
        src = as_number(source.get(label))
        if src is None:
            out.append(
                {
                    "check": label,
                    "ours": ours,
                    "source": None,
                    "difference": None,
                    "status": "NOT CHECKED",
                    # WHY, on the row itself (2026-09-10): a bare NOT CHECKED reads as a
                    # failure of the pipeline when it is a fact about the statement.
                    "reason": f"The source statement printed no {label} subtotal to tie to.",
                    "headline": label in HEADLINE_CHECKS,
                }
            )
            continue
        diff = (ours or 0) - src
        out.append(
            {
                "check": label,
                "ours": ours,
                "source": src,
                "difference": diff,
                "status": "OK" if abs(diff) <= 1 else "REVIEW",
                "reason": None,
                "headline": label in HEADLINE_CHECKS,
            }
        )
    return out


#: The three ties every statement can support; the breakout categories below them tie
#: only when the statement prints a group subtotal, which many do not.
HEADLINE_CHECKS: tuple[str, ...] = ("Total Revenue", schema.PNL_TOTAL_OPEX_LABEL, schema.PNL_NOI_LABEL)

#: The band a cross-document tie tolerates. The two documents describe the same property
#: on different bases (a statement's twelve months of collections against a roll's one
#: day of rents), so a tie within a tenth is the expected state and a wider gap is the
#: thing an underwriter should look at — not a rounding difference.
CROSS_DOC_BAND = 0.10


def cross_document_checks(deal: dict) -> list[dict[str, Any]]:
    """Ties ACROSS the two spreadsheets — the check an underwriter does in their head.

    1. The statement's Gross Potential Rent over twelve months against the roll's market
       rent for every unit times twelve: what the property could bill against what the
       roll says it asks.
    2. The statement's Total Revenue against the roll's in-place rent for occupied units
       times twelve: what came in against what the leases in hand collect.

    Each reads OK inside ``CROSS_DOC_BAND``, REVIEW outside it, and NOT CHECKED with the
    reason when either side is not there: no statement, no roll, no line categorised as
    Gross Potential Rent, or a market rent / in-place rent on fewer than 90% of the units
    it is summed over.
    """
    out: list[dict[str, Any]] = []
    t12_absent = schema.section_absent(deal, "t12")
    roll_absent = schema.section_absent(deal, "rent_roll")

    def row(label: str, ours, source, basis: str, reason: str | None) -> dict[str, Any]:
        if reason is None and (ours is None or source in (None, 0)):
            reason = ("the statement carries no figure on this line" if ours is None
                      else "the roll gives nothing to compare against")
        if reason is not None:
            return {"check": label, "ours": ours, "source": source, "difference": None,
                    "pct": None, "status": "NOT CHECKED", "reason": reason, "basis": basis}
        diff = ours - source
        pct = diff / source
        return {"check": label, "ours": ours, "source": source, "difference": diff, "pct": pct,
                "status": "OK" if abs(pct) <= CROSS_DOC_BAND else "REVIEW",
                "reason": None, "basis": basis}

    if t12_absent or roll_absent:
        why = ("no trailing-twelve statement was part of this set" if t12_absent
               else "no rent roll was part of this set")
        out.append(row("Gross Potential Rent vs the roll's market rent x 12", None, None,
                       "statement GPR, twelve months; roll market rent, every unit, x 12", why))
        out.append(row("Total Revenue vs the roll's in-place rent x 12", None, None,
                       "statement total revenue; roll in-place rent, occupied units, x 12", why))
        return out

    def num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def needed(n: int) -> int:
        """At least 90% of n, rounded UP and never below one — ceil(9n/10) in integers.
        int(0.9 * n) floors: 1 of 2 and 8 of 9 passed a gate that says 90%."""
        return max(1, -(-9 * n // 10))

    p = pnl(deal)
    units = deal["rent_roll"].get("units") or []
    market_known = [u["market_rent"] for u in units if num(u.get("market_rent"))]
    # The rollup carries a Gross Potential Rent row for EVERY statement — a SUMIF over a
    # category no line used is 0, not absent — so presence is the existence of a categorised
    # line, the rule gsr_is_printed() already applies. A statement that prints Gross
    # Scheduled Rent and no GPR would otherwise tie $0 to the roll and read REVIEW at -100%
    #. A GPR line whose months net to zero is a stated zero and is tied.
    gpr_printed = any(r.get("category") == "Gross Potential Rent" for r in deal["t12"]["rows"])
    gpr = p["rows"]["Gross Potential Rent"]["t12"] if gpr_printed else None
    gpr_reason = None
    if not gpr_printed:
        gpr_reason = "the statement has no line categorised as Gross Potential Rent"
        if gsr_is_printed(deal):
            gpr_reason += (" — its Gross Scheduled Rent is the in-place figure, not the "
                           "market-rent one this tie needs")
    elif len(market_known) < needed(len(units)):
        gpr_reason = (f"the roll carries a market rent on {len(market_known)} of {len(units)} units; "
                      f"the tie needs at least 90%")
    out.append(row("Gross Potential Rent vs the roll's market rent x 12", gpr,
                   None if gpr_reason else round(sum(market_known) * 12.0, 2),
                   "statement GPR, twelve months; roll market rent, every unit, x 12", gpr_reason))
    # An occupied unit WITHOUT an in-place rent is not a zero-rent unit. The sum over the
    # units that carry one is the roll's figure only when nearly all of them do — the same
    # 90% the market-rent tie needs — and the basis says how many it is over.
    occupied = [u for u in units if str(u.get("status", "")).lower() == "occupied"]
    with_rent = [u for u in occupied if num(u.get("in_place_rent"))]
    rev_reason = None
    if not occupied:
        rev_reason = "the roll has no occupied unit"
    elif len(with_rent) < needed(len(occupied)):
        rev_reason = (f"the roll carries an in-place rent on {len(with_rent)} of {len(occupied)} "
                      f"occupied units; the tie needs at least 90%")
    coverage = ("occupied units" if len(with_rent) == len(occupied)
                else f"{len(with_rent)} of {len(occupied)} occupied units")
    out.append(row("Total Revenue vs the roll's in-place rent x 12", p.get("total_revenue"),
                   None if rev_reason else round(sum(u["in_place_rent"] for u in with_rent) * 12.0, 2),
                   f"statement total revenue; roll in-place rent, {coverage}, x 12", rev_reason))
    return out


def pricing(deal: dict) -> dict[str, Any]:
    """Price inputs and the two cap-rate ladders, plus the trailing-6 trend.

    Anything whose inputs are absent comes back as :data:`schema.MISSING`, not as zero.
    Zero is a claim — it says the property earned nothing, or costs nothing — and it
    looks exactly like every other figure on the card, so a reader has no reason to doubt
    it. Returning the sentinel here is what makes the summary card, the deal-summary
    workbook and the manifest all say MISSING without three separate fixes: every one of
    them already renders a string as MISSING and a number as a number.
    """
    p = pnl(deal)
    t12_absent = bool(schema.section_absent(deal, "t12"))
    ppu = as_number(deal["pricing"]["price_per_unit"])
    no_price = ppu is None
    if no_price:
        ppu = 0.0
    # The COUNT gets the same treatment the price above already gets. It may be the
    # MISSING sentinel — no document and no note stated one — and it is the multiplier
    # here and the divisor of every per-unit figure downstream, so a total price built on
    # a zero count is the "0 is a claim" failure this docstring is about (round 19,
    # ruling 2).
    units = deal["pricing"]["units_total"]
    count = as_number(units)
    no_count = count is None
    total_price = ppu * (count or 0.0)

    t6_revenue_annualized = revenue_for_window(deal, 6) * 2

    out: dict[str, Any] = {
        "price_per_unit": ppu,
        "units_total": units,
        "total_price": total_price,
        "t12_revenue": p["total_revenue"],
        "t12_opex": p["total_opex"],
        "t12_noi": p["noi"],
        "t12_cap_rate": safe_div(p["noi"], total_price),
        "t3_revenue_annualized": p["total_revenue_t3_annualized"],
        "t3_opex_annualized": p["total_opex_t3_annualized"],
        "t3_noi_annualized": p["noi_t3_annualized"],
        "t3_cap_rate": safe_div(p["noi_t3_annualized"], total_price),
        "t6_revenue_annualized": t6_revenue_annualized,
        "proforma": None,
    }

    # A cap rate needs both halves. It is MISSING if either the income or the price is.
    if no_price:
        for key in ("price_per_unit", "total_price", "t12_cap_rate", "t3_cap_rate"):
            out[key] = schema.MISSING
    # And a TOTAL price needs a count. The per-unit price survives — the document stated
    # it, and it is still true — but nothing multiplied by a count nobody supplied does.
    if no_count:
        for key in ("total_price", "t12_cap_rate", "t3_cap_rate"):
            out[key] = schema.MISSING
    if t12_absent:
        for key in (
            "t12_revenue", "t12_opex", "t12_noi", "t12_cap_rate",
            "t3_revenue_annualized", "t3_opex_annualized", "t3_noi_annualized",
            "t3_cap_rate", "t6_revenue_annualized",
        ):
            out[key] = schema.MISSING
    proforma = deal["t12"].get("broker_proforma")
    if proforma:
        noi = as_number(proforma.get("noi"))
        out["proforma"] = {
            "revenue": as_number(proforma.get("revenue")),
            "opex": as_number(proforma.get("opex")),
            "noi": noi,
            "cap_rate": (schema.MISSING if no_count or no_price
                         else safe_div(noi, total_price)),
        }
    return out


# --------------------------------------------------------------------------------------
# Rent roll
# --------------------------------------------------------------------------------------


def classify_lease(unit: dict, tolerance_days: int) -> str | None:
    """``"New"``, ``"Renewal"``, or ``None``.

    Derived from the gap between lease start and move-in when both dates parse: a new
    tenant signs at move-in, a renewal signs years later. Where a date is unreadable the
    stated ``lease_type`` is used, and where neither is available the answer is ``None``
    -- blank, not a guess, and never a blend of the two.
    """
    start = parse_date(unit.get("lease_start"))
    move_in = parse_date(unit.get("move_in"))
    if start and move_in:
        return "New" if abs((start - move_in).days) <= tolerance_days else "Renewal"
    stated = unit.get("lease_type")
    return stated if stated in schema.LEASE_TYPES else None


def in_lookback(unit: dict, as_of: _dt.date, lookback_months: int, key: str) -> bool:
    d = parse_date(unit.get(key))
    if d is None or d > as_of:
        return False
    return months_between(d, as_of) <= lookback_months


def window_start(as_of: _dt.date, lookback_months: Any) -> _dt.date:
    """The rent-roll Summary's E2: ``EDATE(as_of, -B1)``, end-of-month clamped.

    B1 is truncated to a whole month and floored at 1, as the workbook's own formulas
    are. The move-in and signed windows below are bounded by THIS date and the as-of
    day, which is what the Summary's H/I and F/G columns count; ``in_lookback``'s
    day/30 approximation can disagree with it at the window edge.
    """
    months = max(1, int(lookback_months or 0))
    shift, month0 = divmod(as_of.month - 1 - months, 12)
    year, month = as_of.year + shift, month0 + 1
    return _dt.date(year, month, min(as_of.day, calendar.monthrange(year, month)[1]))


def _in_window(unit: dict, key: str, start: _dt.date, as_of: _dt.date) -> bool:
    d = parse_date(unit.get(key))
    return d is not None and start <= d <= as_of


def _priced(unit: dict) -> bool:
    rent = as_number(unit.get("in_place_rent"))
    return rent is not None and rent > 0


def plan_summary(deal: dict) -> list[dict[str, Any]]:
    """Per-floor-plan rollup, in first-appearance order.

    ``market_rent_for_model`` mirrors the rent-roll Summary's L, ``=IF(H>0,H,E)``
    the internal rent-roll tool's rule: the NEW MOVE-IN rent -- every priced row whose
    MOVE-IN date falls in the window, whatever its lease type -- where the plan has one,
    and the in-place average otherwise. Never the signed pair: ``signed_*`` (F/G) is every
    classified lease START in the window, new + renewal, and a blend of the two must not
    reach the model. A roll with no lease-start column reads in-place outright, as the
    Summary does when its split is n/a.
    """
    rr = deal["rent_roll"]
    as_of = parse_date(rr["as_of"])
    lookback = rr["lookback_months"]
    tolerance = rr["new_tenant_tolerance_days"]

    # A PARSEABLE date, not a non-empty cell: a Lease Start column of "n/a" placeholders
    # is the no-lease-start shape, which the Summary prices at in-place outright.
    has_lease_starts = any(parse_date(u.get("lease_start")) for u in rr["units"])
    # The Summary's lease_type_live is the move-in COLUMN being mapped, not dated: with
    # no move-in column F/G and H/I are n/a (None here), never measured zeros.
    has_move_in_col = any(u.get("move_in") not in (None, "") for u in rr["units"])
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for unit in rr["units"]:
        plan = unit["floor_plan"]
        if plan not in groups:
            groups[plan] = []
            order.append(plan)
        groups[plan].append(unit)

    out = []
    for plan in order:
        units = groups[plan]
        occupied = [u for u in units if u.get("status") == "Occupied"]
        vacant = [u for u in units if u.get("status") == "Vacant"]
        start = window_start(as_of, lookback) if as_of is not None else None
        moved_in = [u for u in units if start is not None and _priced(u)
                    and _in_window(u, "move_in", start, as_of)]
        signed = [u for u in units if start is not None and _priced(u)
                  and _in_window(u, "lease_start", start, as_of)
                  and classify_lease(u, tolerance) is not None]
        in_place = mean(u.get("in_place_rent") for u in occupied)
        movein_rent = mean(u.get("in_place_rent") for u in moved_in)
        signed_rent = mean(u.get("in_place_rent") for u in signed)
        # A roll with NO lease-start column cannot have "0 leases signed" (E2 rule 11):
        # the signed figures are n/a, the model rent is in-place rent outright, and the
        # basis says why — a zero here would read as a fact about the property.
        movein_count: int | None = len(moved_in) if start is not None else None
        signed_count: int | None = len(signed) if start is not None else None
        if not has_move_in_col:
            movein_rent, movein_count = None, None
            signed_rent, signed_count = None, None
        if not has_lease_starts or not has_move_in_col:
            signed_rent, signed_count = None, None
            market = in_place
            basis = ("In-place — the roll has no lease-start column" if not has_lease_starts
                     else "In-place — the roll has no move-in column")
        elif movein_rent is not None:
            market = movein_rent
            basis = f"New move-ins (n={len(moved_in)})"
        else:
            market = in_place
            basis = "In-place — no new move-ins in window"
        out.append(
            {
                "floor_plan": plan,
                "beds": units[0].get("beds"),
                "baths": units[0].get("baths"),
                "avg_sqft": mean(u.get("sqft") for u in units),
                "units": len(units),
                "occupied": len(occupied),
                "vacant": len(vacant),
                "avg_in_place_rent": in_place,
                "movein_rent": movein_rent,
                "movein_count": movein_count,
                "signed_rent": signed_rent,
                "signed_count": signed_count,
                "market_rent_for_model": market,
                "basis": basis,
            }
        )
    # Bedroom count, then plan code — the order a reader scans a unit mix in.
    out.sort(key=lambda p: (p["beds"] if p["beds"] is not None else 99, p["floor_plan"]))
    return out


def move_in_counts(deal: dict) -> dict[str, Any]:
    """Move-ins inside the window, counted by MOVE-IN date, never by lease start.

    A renewal stamps a fresh lease start against a resident who moved in years ago, so a
    lease-start count reports turnover that never happened.
    """
    rr = deal["rent_roll"]
    as_of = parse_date(rr["as_of"])
    lookback = rr["lookback_months"]
    units = rr["units"]
    # The Summary's own move-in window: the SAME cutoff the plan rollup's
    # move-in rent uses, so the turnover count and the model rent cannot disagree.
    in_window = [
        u for u in units
        if as_of is not None
        and _in_window(u, "move_in", window_start(as_of, lookback), as_of)
    ]
    unreadable = [u for u in units if u.get("move_in") and parse_date(u.get("move_in")) is None]
    # With no as-of date there is no window, and a count of zero move-ins inside a window
    # that does not exist is a confident wrong answer. MISSING, and the run's notes say
    # why the source could not supply one.
    return {
        "window_months": lookback,
        "as_of": rr["as_of"],
        "move_ins": None if as_of is None else len(in_window),
        "of_total": len(units),
        "share": None if as_of is None else safe_div(len(in_window), len(units)),
        "unreadable_dates": len(unreadable),
    }


def roll_totals(deal: dict) -> dict[str, Any]:
    rr = deal["rent_roll"]
    units = rr["units"]
    as_of = parse_date(rr["as_of"])
    lookback = rr["lookback_months"]
    occupied = [u for u in units if u.get("status") == "Occupied"]
    vacant = [u for u in units if u.get("status") == "Vacant"]
    non_revenue = [
        u for u in units if u.get("status") not in ("Occupied", "Vacant")
    ]
    plans = plan_summary(deal)

    weighted_market_num = fsum(
        p["units"] * p["market_rent_for_model"]
        for p in plans
        if p["market_rent_for_model"] is not None
    )
    weighted_market_den = fsum(
        p["units"] for p in plans if p["market_rent_for_model"] is not None
    )
    move_ins = [
        u
        for u in units
        if as_of is not None
        and _in_window(u, "move_in", window_start(as_of, lookback), as_of)
    ]
    turnover = safe_div(len(move_ins), len(units))
    return {
        "as_of": rr["as_of"],
        "lookback_months": lookback,
        "new_tenant_tolerance_days": rr["new_tenant_tolerance_days"],
        "total_units": len(units),
        "occupied_units": len(occupied),
        "vacant_units": len(vacant),
        "non_revenue_units": len(non_revenue),
        "occupancy": safe_div(len(occupied), len(units)),
        "vacancy": None if not units else 1 - (len(occupied) / len(units)),
        "wavg_in_place_rent": mean(u.get("in_place_rent") for u in occupied),
        "wavg_market_rent_for_model": safe_div(
            weighted_market_num, weighted_market_den
        ),
        "move_ins_in_window": None if as_of is None else len(move_ins),
        "annualized_turnover": None
        if as_of is None or turnover is None or not lookback
        else turnover * (12 / lookback),
    }


def trade_out(deal: dict) -> dict[str, Any]:
    """Per-cohort trade-out, gross and effective stated separately.

    Gross is new gross vs prior gross. Effective is new effective vs prior EFFECTIVE
: ``prior_rent_effective`` when the report stated a prior
    concession, else the prior gross -- no stated concession means the prior lease paid
    its gross. Subtracting the prior gross from a concession-net new rent overstated the
    loss on every cohort whose prior leases carried a concession.
    """
    block = deal["trade_out"]
    rows = []
    for row in block.get("rows") or []:
        prior = as_number(row["prior_rent"])
        stated = as_number(row.get("prior_rent_effective"))
        prior_eff = prior if stated is None else stated
        gross = as_number(row["new_rent_gross"])
        effective = as_number(row["new_rent_effective"])
        rows.append(
            {
                "cohort": row["cohort"],
                "leases": row["leases"],
                "prior_rent": prior,
                "prior_rent_effective": prior_eff,
                "new_rent_gross": gross,
                "new_rent_effective": effective,
                "gross_change": None if None in (gross, prior) else gross - prior,
                "gross_change_pct": safe_div(
                    None if None in (gross, prior) else gross - prior, prior
                ),
                "effective_change": None
                if None in (effective, prior_eff)
                else effective - prior_eff,
                "effective_change_pct": safe_div(
                    None if None in (effective, prior_eff) else effective - prior_eff,
                    prior_eff,
                ),
            }
        )
    return {"rows": rows, "absent_reason": block.get("absent_reason")}


# --------------------------------------------------------------------------------------
# Comps, demand, supply
# --------------------------------------------------------------------------------------


#: What a unit-count cell says when no count was stated at all (internal, no record).
UNITS_NOT_STATED = "not stated"


def community_facts(deal: dict) -> list[dict[str, Any]]:
    """One line per detail-tier community: its unit count and year built, each with WHERE
    it came from. The comps preview and the comps workbook tab both
    read this, so the two cannot disagree (I6). ``units`` is whatever the row holds after
    the policy ran — a public-record count, MISSING, or (internal) the licensed count — and
    ``units_source`` is the row's own label for it. Empty when the rows carry no unit
    column at all (a set built before the column existed)."""
    rows = [r for r in ((deal.get("comps") or {}).get("rows") or []) if isinstance(r, dict)]
    if not any("units" in r or "public_record_units" in r for r in rows):
        return []
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(r["community"], r)
    out = []
    for name, r in sorted(seen.items(), key=lambda kv: (kv[1].get("distance_mi") or 0, kv[0])):
        units = r.get("units")
        source = r.get("units_source")
        if units is None:
            source = source or UNITS_NOT_STATED
        out.append({
            "community": name,
            "tier": r.get("tier"),
            "distance_mi": r.get("distance_mi"),
            "units": units,
            "units_source": source or "—",
            "year_built": r.get("year_built"),
            "year_built_source": r.get("year_built_source") or ("not published" if not r.get("year_built") else "—"),
        })
    return out


def comps(deal: dict) -> dict[str, Any]:
    block = deal["comps"]
    rows = []
    for row in block["rows"]:
        rows.append(
            {
                **row,
                "asking_psf": safe_div(row.get("asking_rent"), row.get("sqft")),
            }
        )
    # `rows` IS the detailed set: tiers 1 and 2 only, by construction — tier 3 carries no
    # rents and lives in `nearby`. Every average below is therefore over the up-to-20
    # communities the display calls comps, and the labels say so. Averaging the whole
    # radius instead would quote a market the comp criteria rejected.
    by_tier: dict[int, list[dict]] = {}
    for r in rows:
        by_tier.setdefault(int(r.get("tier") or 0), []).append(r)
    tier_communities = {
        tier: sorted({r["community"] for r in group}) for tier, group in by_tier.items()
    }
    nearby = list(block.get("nearby") or [])
    tiering = dict(block.get("tiering") or {})

    avg_asking = mean(r["asking_rent"] for r in rows)
    avg_effective = mean(r["net_effective_rent"] for r in rows)
    subject = as_number(block.get("subject_market_rent"))

    # By bed count, because a comp set's bed mix is never the subject's. A flat average
    # over every row compares the subject's 1- and 2-bedroom weighting against whatever
    # the market happened to have listed, which is the same blending mistake the rest of
    # this output refuses to make.
    by_bed: dict[int, list[dict]] = {}
    for r in rows:
        b = as_number(r.get("beds"))
        if b is None:
            continue
        by_bed.setdefault(int(b), []).append(r)
    bed_table = []
    for beds in sorted(by_bed):
        group = by_bed[beds]
        bed_table.append(
            {
                "beds": beds,
                "communities": len({g["community"] for g in group}),
                "rows": len(group),
                "avg_asking": mean(g["asking_rent"] for g in group),
                "avg_net_effective": mean(g["net_effective_rent"] for g in group),
            }
        )

    # Mix-matched: weight each bed count's comp net-effective by the SUBJECT's own unit
    # mix, so the single headline number compares like with like.
    subject_mix: dict[int, int] = {}
    for p_ in plan_summary(deal):
        b = as_number(p_.get("beds"))
        if b is None:
            continue
        subject_mix[int(b)] = subject_mix.get(int(b), 0) + int(p_["units"])
    # Both sides restricted to the SAME bed counts. Weighting the comp side over matched
    # beds while leaving the subject side as the whole property compares a 1-and-2-bedroom
    # market against a rent that includes the subject's 3-bedrooms — the spread then
    # measures the mix difference, not the market.
    subject_plan_rent: dict[int, tuple[float, float]] = {}
    for p_ in plan_summary(deal):
        b = as_number(p_.get("beds"))
        rent = as_number(p_.get("market_rent_for_model"))
        if b is None or rent is None:
            continue
        acc_n, acc_d = subject_plan_rent.get(int(b), (0.0, 0.0))
        subject_plan_rent[int(b)] = (acc_n + rent * p_["units"], acc_d + p_["units"])

    comp_num = comp_den = subj_num = subj_den = 0.0
    matched_beds: list[int] = []
    for beds in subject_mix:
        entry = next((e for e in bed_table if e["beds"] == beds), None)
        if entry is None or entry["avg_net_effective"] is None:
            continue
        if beds not in subject_plan_rent or not subject_plan_rent[beds][1]:
            continue
        s_num, s_den = subject_plan_rent[beds]
        # Weight BOTH sides by the units that actually carry a rent. Using every unit in
        # the bed count on the comp side while the subject side counts only priced plans
        # compares two different populations and inflates the coverage figure the floor
        # is tested against.
        comp_num += s_den * entry["avg_net_effective"]
        comp_den += s_den
        subj_num += s_num
        subj_den += s_den
        matched_beds.append(beds)

    mix_matched = (comp_num / comp_den) if comp_den else None
    subject_matched = (subj_num / subj_den) if subj_den else None
    # Coverage is units actually compared over all units in the property, so an unpriced
    # plan lowers it exactly as a missing comp bed count does.
    mix_coverage = safe_div(comp_den, sum(subject_mix.values()))

    spread = None
    withheld = None
    if subject_matched is not None and mix_matched:
        if (mix_coverage or 0) < MIX_COVERAGE_FLOOR:
            withheld = (
                f"Spread withheld: the comp set covers only "
                f"{(mix_coverage or 0):.0%} of the subject's units by bed count, below the "
                f"{MIX_COVERAGE_FLOOR:.0%} floor. A headline number off that little of the "
                f"property would describe the mix, not the market."
            )
        else:
            spread = subject_matched / mix_matched - 1
    flat_spread = None
    if subject is not None and avg_effective:
        flat_spread = subject / avg_effective - 1

    # The vintage-only counterfactual, mix-matched by the SAME arithmetic — and, for the
    # contrast, restricted to the bed counts BOTH populations cover.
    #
    # Two populations can each be 100% mix-covered and still cover different bed counts:
    # swapping six communities can drop a 3-bedroom plan out of one side. Comparing them
    # then measures the bed mix, not the vintage. So the contrast runs over the
    # intersection, and where that intersection is materially thinner than the headline
    # figure's own coverage the contrast is WITHHELD with a reason rather than published
    # with a caveat nobody reads.
    unrestricted = block.get("unrestricted") or {}
    unr_matched = unr_subject = unr_coverage = None
    unr_spread = None
    contrast: dict[str, Any] = {
        "beds": [],
        "coverage": None,
        "matched": None,
        "unrestricted": None,
        "subject": None,
        "spread_matched": None,
        "spread_unrestricted": None,
        "withheld_reason": None,
    }
    if unrestricted.get("by_bed"):
        u_num = u_den = us_num = us_den = 0.0
        for beds in subject_mix:
            entry = next(
                (e for e in unrestricted["by_bed"] if int(e["beds"]) == beds), None
            )
            if entry is None or entry.get("avg_net_effective") is None:
                continue
            if beds not in subject_plan_rent or not subject_plan_rent[beds][1]:
                continue
            s_num, s_den = subject_plan_rent[beds]
            u_num += s_den * entry["avg_net_effective"]
            u_den += s_den
            us_num += s_num
            us_den += s_den
        unr_matched = (u_num / u_den) if u_den else None
        unr_subject = (us_num / us_den) if us_den else None
        unr_coverage = safe_div(u_den, sum(subject_mix.values()))
        if unr_matched and unr_subject is not None:
            unr_spread = unr_subject / unr_matched - 1

        # ---- the like-for-like contrast ---------------------------------------------
        shared = sorted(
            beds for beds in subject_mix
            if beds in subject_plan_rent and subject_plan_rent[beds][1]
            and any(int(e["beds"]) == beds and e.get("avg_net_effective") is not None
                    for e in bed_table)
            and any(int(e["beds"]) == beds and e.get("avg_net_effective") is not None
                    for e in unrestricted["by_bed"])
        )
        c_m = c_u = c_s = c_d = 0.0
        for beds in shared:
            m_entry = next(e for e in bed_table if int(e["beds"]) == beds)
            u_entry = next(e for e in unrestricted["by_bed"] if int(e["beds"]) == beds)
            s_num, s_den = subject_plan_rent[beds]
            c_m += s_den * m_entry["avg_net_effective"]
            c_u += s_den * u_entry["avg_net_effective"]
            c_s += s_num
            c_d += s_den
        c_cov = safe_div(c_d, sum(subject_mix.values()))
        contrast["beds"] = shared
        contrast["coverage"] = c_cov
        if not shared or not c_d:
            contrast["withheld_reason"] = (
                "The two comp sets share no bed count that the subject also prices, so "
                "there is no like-for-like basis on which to compare them."
            )
        elif (c_cov or 0) < MIX_COVERAGE_FLOOR:
            contrast["withheld_reason"] = (
                f"Withheld: the two comp sets share bed counts covering only "
                f"{(c_cov or 0):.0%} of the subject's units, below the "
                f"{MIX_COVERAGE_FLOOR:.0%} floor. A vintage effect measured off that "
                f"little of the property would describe the mix instead."
            )
        elif mix_coverage is not None and abs((c_cov or 0) - mix_coverage) > 0.05:
            contrast["withheld_reason"] = (
                f"Withheld: the shared bed counts cover {(c_cov or 0):.0%} of the "
                f"subject's units against the headline figure's {mix_coverage:.0%}. The "
                f"two populations do not cover the same product, so their difference "
                f"would be a bed-mix effect wearing a vintage label."
            )
        else:
            contrast["matched"] = c_m / c_d
            contrast["unrestricted"] = c_u / c_d
            contrast["subject"] = c_s / c_d
            contrast["spread_matched"] = (c_s / c_d) / (c_m / c_d) - 1
            contrast["spread_unrestricted"] = (c_s / c_d) / (c_u / c_d) - 1

    return {
        "rows": rows,
        "as_of": block["as_of"],
        "source": block["source"],
        "subject_market_rent": subject,
        "avg_asking": avg_asking,
        "avg_net_effective": avg_effective,
        "by_bed": bed_table,
        "compset_mix_matched_net_effective": mix_matched,
        "subject_market_rent_matched": subject_matched,
        "mix_matched_beds": sorted(matched_beds),
        "mix_coverage": mix_coverage,
        "spread_withheld_reason": withheld,
        "subject_vs_compset_spread": spread,
        "subject_vs_compset_spread_unmatched": flat_spread,
        # ---- tiering ----------------------------------------------------------------
        "tiers": {
            tier: {
                "label": COMP_TIER_LABELS.get(tier, f"tier {tier}"),
                "communities": tier_communities.get(tier, []),
                "rows": by_tier.get(tier, []),
            }
            for tier in sorted(by_tier)
        },
        "nearby": nearby,
        "tiering": tiering,
        "detailed_communities": sorted({r["community"] for r in rows}),
        "nearby_count": len(nearby),
        "nearby_with_rent_data": sum(1 for n in nearby if n.get("has_rent_data")),
        # The one sentence that keeps the workbook, the preview and the audit trail from
        # each describing a different population.
        # Same arithmetic, unrestricted population. `None` where no baseline was measured.
        "unrestricted_mix_matched": unr_matched,
        "unrestricted_subject_matched": unr_subject,
        "unrestricted_mix_coverage": unr_coverage,
        "subject_vs_unrestricted_spread": unr_spread,
        "unrestricted_communities": unrestricted.get("communities"),
        "unrestricted_year_min": unrestricted.get("year_min"),
        "unrestricted_year_max": unrestricted.get("year_max"),
        "unrestricted_swapped_in": unrestricted.get("swapped_in") or [],
        "unrestricted_swapped_out": unrestricted.get("swapped_out") or [],
        "unrestricted_held_fixed": unrestricted.get("held_fixed") or [],
        # The like-for-like vintage contrast: same bed counts on both sides, or withheld.
        "vintage_contrast": contrast,
        "basis_label": (
            f"{len({r['community'] for r in rows})} communities in the detailed tiers "
            f"(top {tiering.get('top_n', '?')} plus next {tiering.get('additional_n', '?')})"
        ),
    }


def demand(deal: dict) -> list[dict[str, Any]]:
    return list(deal["demand"]["rings"])


def supply(deal: dict) -> dict[str, Any]:
    """Supply rows plus a per-ring total and share of existing stock where known."""
    block = deal["supply"]
    rows = list(block["rings"])
    stock = block.get("ring_stock") or {}
    ring_totals: dict[str, int] = {}
    for row in rows:
        ring_totals[row["ring"]] = ring_totals.get(row["ring"], 0) + int(row["units"])
    totals = []
    for ring, units in ring_totals.items():
        totals.append(
            {
                "ring": ring,
                "total_units": units,
                "ring_stock": stock.get(ring),
                "pct_of_ring_stock": safe_div(units, stock.get(ring)),
            }
        )
    return {"rows": rows, "totals": totals}


# --------------------------------------------------------------------------------------
# Deal summary derivations
# --------------------------------------------------------------------------------------


def tax_check(deal: dict) -> dict[str, Any]:
    """Computed annual tax against the county's own billed figure.

    Returns ``NOT CHECKED`` whenever either side is not a number — a MISSING tax input or
    a missing bill makes the comparison unanswerable, and an unanswerable comparison must
    not render as a difference of zero or as an error. The workbook formula makes the same
    decision with ISNUMBER guards (I6).
    """
    computed = summary_derived(deal)["computed_annual_tax"]
    billed = as_number(deal["summary"].get("tax_billed_amount"))
    comp = as_number(computed)
    if comp is None or billed is None or billed == 0:
        return {"status": "NOT CHECKED", "computed": computed, "billed": billed,
                "difference": None, "pct": None, "reason": None}
    # A bill from one tax year against a rate from another is not a check: rates change
    # every year, so a false tie or a false REVIEW is the likely answer. When the
    # rate's year is known (the vendored table's) and the bill's year differs, the bill
    # stays on display and the comparison is withheld with the reason.
    bill_year = deal["summary"].get("tax_billed_year")
    rate_year = deal["summary"].get("tax_rate_year")
    if (isinstance(bill_year, int) and not isinstance(bill_year, bool)
            and isinstance(rate_year, int) and not isinstance(rate_year, bool)
            and bill_year != rate_year):
        return {"status": "NOT CHECKED", "computed": comp, "billed": billed,
                "difference": None, "pct": None,
                "reason": f"the bill is for tax year {bill_year} and the rate for {rate_year}"}
    diff = comp - billed
    pct = abs(diff) / billed
    return {
        "status": "ties" if pct <= 0.02 else "REVIEW",
        "computed": comp,
        "billed": billed,
        "difference": diff,
        "pct": pct,
        "reason": None,
    }


#: Statuses that count as physically occupied. Physical, not economic: a resident on
#: notice is still in the unit, and a down or model unit is not. The source skill's
#: convention, and the reason it matters is that "occupancy" with no qualifier is the most
#: over-loaded word in a rent roll.
OCCUPIED_FOR_OCCUPANCY = ("Occupied",)

#: Verdict vocabulary, borrowed verbatim from the operator's own error-check block so the
#: two read the same. NOT CHECKED is the load-bearing one.
OK, MISMATCH, NOT_CHECKED, INFO = "OK", "MISMATCH", "NOT CHECKED", "INFO"

#: How many units the rent readback samples.
SPOT_CHECK_SIZE = 10

#: Tolerance on a single unit's rent, in dollars.
SPOT_CHECK_TOL = 0.50


def _verdict(ours, src, tol: float) -> tuple[str, Any]:
    """``(verdict, difference)`` for one tie-out.

    A missing side is NOT CHECKED and claims no difference. It is never OK: a check that
    could not run must not spend the credibility of one that did.
    """
    a, b = as_number(ours), as_number(src)
    if a is None or b is None:
        return NOT_CHECKED, None
    diff = a - b
    return (OK if abs(diff) <= tol else MISMATCH), diff


def _coerce_rent(raw):
    """A raw rent cell to a number, or None where it means "vacant".

    Vendor exports write a vacant unit's rent as blank, numeric zero, or one of a family
    of text placeholders — ``"$0"``, ``"$0.00"``, ``"-"``, ``"N/A"``, ``"(0)"``. All of
    them mean the same thing and none of them is a rent, so all of them become None. A
    text ``"$0"`` read as the number zero drags every average it touches.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    text = str(raw).strip()
    if not text or text.upper() in {"N/A", "NA", "-", "--", "", "VACANT"}:
        return None
    neg = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("$", "").replace(",", "").strip()
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if neg:
        val = -val
    return val if val > 0 else None


def spot_check_sample(deal: dict, size: int = SPOT_CHECK_SIZE) -> list[dict]:
    """A reproducible sample of units for the rent readback.

    Seeded from the roll's own as-of date, so the same roll always samples the same units
    and a reviewer can re-run the check and see the same rows. The operator's own skill
    leaves this unseeded on purpose — a fresh sample each run eventually covers the
    property — which is the better choice for a tool someone runs weekly. Here the output
    is a published artifact that has to be reproducible from its inputs, so determinism
    wins. Same mechanism, opposite trade-off, and worth naming rather than silently
    diverging.
    """
    import random

    rr = deal["rent_roll"]
    units = [u for u in rr.get("units", []) if u.get("source_row") is not None]
    if not units:
        return []
    rng = random.Random(f"{rr.get('as_of')}|{len(rr.get('units', []))}")
    picked = rng.sample(units, min(size, len(units)))
    picked.sort(key=lambda u: str(u.get("unit_id")))
    stated = rr.get("stated") or {}
    sheet = stated.get("sheet")
    col = stated.get("rent_column")
    out = []
    for u in picked:
        locator = (
            f"{sheet}!{col}{u['source_row']}" if (sheet and col and u.get("source_row"))
            else None
        )
        raw = u.get("source_rent_raw")
        out.append(
            {
                "unit_id": u.get("unit_id"),
                "floor_plan": u.get("floor_plan"),
                "status": u.get("status"),
                "our_rent": u.get("in_place_rent"),
                "source_raw": raw,
                # A raw cell can be a number, a currency-formatted string, or a text
                # placeholder for vacancy. Coerce it HERE, at check time, so the
                # comparison exercises the same interpretation the extractor made.
                "source_rent": _coerce_rent(raw),
                "source_locator": locator,
                "source_row": u.get("source_row"),
            }
        )
    return out


#: Lease statuses this module will act on. Mirrors schema.LEASE_STATUSES; kept here so
#: compute does not import schema for one tuple.
#: `""` is how the extractor represents a BLANK Status — it reads the cell through
#: `str(... or "").strip()`, so a blank arrives as an empty string, never as None.
#: Enumerated from what the extractor EMITS, not from what the source cells hold:
#: the first cut listed None and turned every genuinely-blank row unusable.
KNOWN_LEASE_STATUSES: tuple = ("Current", "New", "Renewal", "", None)


def _dup_record_usable(rec) -> bool:
    """Is this de-duplication record shaped well enough to ACT on?

    ONE predicate, used by both the check and the market-rent note. They had separate
    filters and the looser one let a `dropped: [null]` through to a `.get` — the shape of
    bug that comes from two places deciding the same thing.

    An UNRECOGNISED lease status makes a record unusable, not merely odd. The check's
    "no in-place row existed" branch excuses a wrong keep, and a typo lands there: with
    `'Currnet'` the row read a clean OK while validation was rejecting the same data.
    Defence in depth — validation names the field, and this refuses to act on it.
    """
    if not (
        isinstance(rec, dict)
        and isinstance(rec.get("dropped"), list)
        and all(isinstance(x, dict) for x in rec["dropped"])
    ):
        return False
    statuses = [rec.get("kept_lease_status")] + [
        x.get("lease_status") for x in rec["dropped"]
    ]
    return all(s in KNOWN_LEASE_STATUSES for s in statuses)


def _market_tieout_note(our_market, units_total: int, stated: dict) -> str:
    """What the market-rent tie-out actually proves, FOR THIS DEAL.

    Both halves used to be hardcoded prose — a unit count and the claim that every
    turnover pair carries the same market rent. The first is a fact about one file; the
    second decides whether this tie-out is dedup-sensitive at all, and on a roll whose
    pairs DO differ it becomes a real check on row choice. Saying either without
    measuring it is how a note outlives the thing it describes.
    """
    if our_market is None:
        return "The parse did not capture a per-unit market rent, so this cannot be tied."
    # Filter BEFORE touching anything nested. This ran ahead of the check's own defensive
    # pass and called .get on a non-mapping, so `dropped: [null]` raised instead of
    # degrading — a note that raises produces no report at all.
    raw_detail = stated.get("duplicate_detail") or []
    detail = [d for d in raw_detail if _dup_record_usable(d)]
    unreadable = len(raw_detail) - len(detail)
    base = (
        f"Validates row COMPLETENESS — {units_total:,} units counted once each, none "
        f"dropped or double-counted."
    )
    if not detail:
        # Absence and illegibility are different claims. Saying "no unit is listed twice"
        # off records that could not be read asserts a fact about the roll from a failure
        # to parse it.
        if unreadable:
            return (
                base + f" Whether it is ALSO sensitive to row choice is UNVERIFIABLE: all "
                f"{unreadable} duplicate record(s) are malformed and could not be read."
            )
        return base + " No unit is listed twice here, so there is no row choice to make."

    pairs_same, pairs_diff = [], []
    for d in detail:
        vals = [d.get("kept_market_rent")] + [
            x.get("market_rent") for x in (d.get("dropped") or [])
        ]
        # Sensitivity is exactly one question: WOULD THE TOTAL MOVE? The total sums this
        # column with a missing value contributing zero, so compare the EFFECTIVE
        # contributions. Treating every missing value as a difference over-claimed on the
        # pairs where both rows contribute the same zero — both blank, or one blank and
        # one stated zero. Stated-zero vs missing matters when the DESTINATION
        # distinguishes them; here the destination does not.
        effective = {(m if isinstance(m, (int, float)) else 0.0) for m in vals}
        (pairs_diff if len(effective) > 1 else pairs_same).append(d.get("unit_id"))
    if pairs_diff:
        return (
            base + f" It is ALSO sensitive to row choice on this deal: "
            f"{len(pairs_diff)} of {len(detail)} duplicate pair(s) carry market rents "
            f"that are not identical across their two rows — a different figure, or one "
            f"stated and one blank, which this total treats as zero "
            f"({', '.join(str(u) for u in pairs_diff)}). Keeping the wrong row would move "
            f"this total too."
            + (
                f" A further {unreadable} record(s) were unreadable and are not included "
                f"in that count."
                if unreadable else ""
            )
        )
    return (
        base + f" It does NOT validate which row was kept on a duplicate: "
        f"{'all ' if not unreadable else ''}{len(detail)} readable turnover pair(s) carry "
        f"the same market rent on both rows, so "
        f"this total is identical under either de-duplication rule while in-place rent is "
        f"not. The row CHOICE is checked separately below."
        + (
            f" {unreadable} further record(s) were unreadable, so that statement covers "
            f"only the {len(detail)} that could be read."
            if unreadable else ""
        )
    )


def rent_roll_checks(deal: dict) -> dict[str, Any]:
    """Tie the normalized rent roll back to the source export's own stated totals.

    Every row reports OK, MISMATCH or NOT CHECKED against a figure the SOURCE published —
    never against another number this toolkit derived. A tie-out between two of our own
    computations proves only that we are consistent, which is not the question.

    The rent readback is the one check here that is honest about its own limit: both sides
    read the same column, so it validates TRANSCRIPTION and cannot detect a wrong column
    choice. That limitation is stated on the block rather than left for a reader to work
    out, because a check whose scope is misunderstood is worse than no check.
    """
    rr = deal["rent_roll"]
    stated = rr.get("stated") or {}
    units = rr.get("units", [])
    totals = roll_totals(deal)

    our_units = len(units)
    occupied = [u for u in units if u.get("status") in OCCUPIED_FOR_OCCUPANCY]
    our_occ_units = len(occupied)
    our_occupancy = safe_div(our_occ_units, our_units)
    our_sqft = fsum(as_number(u.get("sqft")) or 0 for u in units) or None

    rows: list[dict] = []

    v, d = _verdict(our_units, stated.get("units_total"), 0)
    rows.append({
        "item": "Total units",
        "ours": our_units,
        "source": stated.get("units_total"),
        "diff": d,
        "verdict": v,
        "note": "Unit count after de-duplicating rows for units listed twice mid-turnover.",
    })

    v, d = _verdict(our_sqft, stated.get("sqft"), 1)
    rows.append({
        "item": "Total square feet",
        "ours": our_sqft,
        "source": stated.get("sqft"),
        "diff": d,
        "verdict": v,
        "note": (
            "This project's own addition — the operator's rent-roll skill has no square-"
            "footage tie-out. It earns its place because square footage is what the "
            "de-duplication is checked against: dropping the wrong duplicate row changes "
            "the total."
        ),
    })

    # Occupancy against the source's OWN per-status buckets. The source states seven
    # statuses and labels one of them "Occupied"; tying our physical occupancy against
    # that single bucket would report a 21-unit MISMATCH on a number that is right. The
    # comparison is against the sum of the buckets where a resident is in the unit, and
    # the basis string names which those are, so the definition is on the page.
    src_occ = stated.get("occupied_units")
    v, d = _verdict(our_occ_units, src_occ, 0)
    rows.append({
        "item": "Occupied units (physical)",
        "ours": our_occ_units,
        "source": src_occ,
        "diff": d,
        "verdict": v,
        "note": (
            f"Against the source's own status buckets: {stated.get('occupied_basis')}. "
            f"A resident on notice has not moved out, so the source's 'Occupied' bucket "
            f"alone is a LEASING status, not physical occupancy."
            if src_occ is not None else
            "The source states no per-status unit table, so physical occupancy cannot be "
            "tied to it."
        ),
    })
    # `is not None`, not truthiness: a fully vacant property legitimately states 0
    # occupied, and truthiness turned that into NOT CHECKED on the percentage row while
    # the unit row read OK — two surfaces disagreeing about whether a check ran.
    src_occ_pct = (
        safe_div(src_occ, stated.get("units_total")) if src_occ is not None else None
    )
    v, d = _verdict(our_occupancy, src_occ_pct, 0.0001)
    rows.append({
        "item": "Occupancy",
        "ours": our_occupancy,
        "source": src_occ_pct,
        "diff": d,
        "verdict": v,
        "format": "pct",
        "note": (
            f"PHYSICAL occupancy: {our_occ_units} of {our_units} units. Residents on "
            f"notice count as occupied; down, model and employee units do not. Economic "
            f"occupancy is a different number and is not this one."
        ),
    })

    # The source states a market-rent total. It reconciles to the sum of the source's own
    # market column over OUR kept rows — which makes it a check on the DE-DUPLICATION, the
    # one decision in this parse with real money behind it. It is the only tie-out here
    # that would fail if the wrong row were kept on a turnover pair.
    our_market = fsum(
        as_number(u.get("market_rent")) or 0 for u in units
    ) or None
    v, d = _verdict(our_market, stated.get("market_rent"), 1.0)
    rows.append({
        "item": "Market rent (monthly total)",
        "ours": our_market,
        "source": stated.get("market_rent"),
        "diff": d,
        "verdict": v,
        "note": _market_tieout_note(our_market, our_units, stated),
    })

    # The gate's second half (E2 rule 16): our in-place rent total against the export's
    # own stated current-rent total, within a dollar. Vacant units carry no rent on
    # either side.
    in_place_rents = [as_number(u.get("in_place_rent")) for u in units]
    our_in_place = fsum(r for r in in_place_rents if r is not None) if any(r is not None for r in in_place_rents) else None
    src_current = as_number(stated.get("current_rent"))
    rows.append({
        "item": "In-place rent (monthly total)",
        "ours": our_in_place,
        "source": src_current,
        "diff": None if None in (our_in_place, src_current) else our_in_place - src_current,
        # INFO, not a scored tie: the export's "current rent" comes from its per-status
        # summary, which carries a rent on model, unrentable and vacant units; in-place
        # rent covers occupied units only. Same words, different basis — a MISMATCH here
        # would be an artefact of the definitions, and an OK would be luck.
        "verdict": INFO,
        "note": (
            "Shown for the reader, not scored: the export's current-rent total is its "
            "per-status summary, which carries a rent on model, unrentable and vacant units; "
            "in-place rent is occupied units only. The gate's rent half is the market-rent "
            "total above, which the export states on the same per-unit basis."
            if src_current is not None else
            "The export states no current-rent total."
        ),
    })
    # DEDUP-SENSITIVE. The market-rent tie-out above passes under either rule, so on its
    # own it corroborates nothing about row choice — a claim this project made and had to
    # retract. This one reads which row was actually retained on every duplicate and fails
    # when it is not the lease in place on the as-of date.
    # Only well-formed records reach the logic. schema.validate() rejects a malformed one
    # by name, but this check must still be able to REPORT when handed junk — a check that
    # raises produces no verdict at all, which is worse than a NOT CHECKED.
    detail = [d for d in (stated.get("duplicate_detail") or []) if _dup_record_usable(d)]
    malformed = len(stated.get("duplicate_detail") or []) - len(detail)
    want = stated.get("in_place_lease_status")
    if not detail:
        rows.append({
            "item": "Duplicate rows: kept the in-place lease",
            "ours": None, "source": None, "diff": None, "verdict": NOT_CHECKED,
            "note": (
                f"{malformed} duplicate record(s) are malformed and were not readable, so "
                f"the row choice cannot be verified. schema.validate() names the fields."
                if malformed else
                "The export lists no unit twice, so there is no row choice to make."
            ),
        })
    elif not want:
        rows.append({
            "item": "Duplicate rows: kept the in-place lease",
            "ours": len(detail), "source": None, "diff": None, "verdict": NOT_CHECKED,
            "note": (
                f"{len(detail)} unit(s) appear twice, but the source states no lease "
                f"status to identify the in-place row, so the choice cannot be verified."
            ),
        })
    else:
        # A unit is only WRONG when an in-place row was available and a different one was
        # kept. Where no candidate carries the in-place status the choice was not
        # available to get wrong — unit 2107 here has one row with no status at all and a
        # $0 rent, so keeping the other is right and flagging it would be a false MISMATCH.
        def had_in_place(d):
            statuses = [d.get("kept_lease_status")] + [
                x.get("lease_status") for x in (d.get("dropped") or [])
            ]
            return want in statuses

        wrong = [d for d in detail
                 if had_in_place(d) and d.get("kept_lease_status") != want]
        no_in_place = [d for d in detail if not had_in_place(d)]
        # Exposure uses the STATED rents. A source that says $0 is stating a value; only an
        # empty cell is an absence. Filtering on the MODELLED rent (None for both) dropped
        # the two largest swings on this deal and published $1,151/mo instead of $4,594.
        exposure = 0.0
        for d in detail:
            if not had_in_place(d):
                continue  # no in-place row existed, so there was no choice to get wrong
            alt = [
                x.get("rent_stated") for x in (d.get("dropped") or [])
                if isinstance(x.get("rent_stated"), (int, float))
            ]
            kept = d.get("kept_rent_stated")
            if isinstance(kept, (int, float)) and alt:
                exposure += kept - max(alt)
        rows.append({
            "item": "Duplicate rows: kept the in-place lease",
            "ours": len(detail) - len(wrong),
            "source": len(detail),
            "diff": -len(wrong) if wrong else 0,
            # A malformed record is a unit that could NOT be verified, so the row cannot
            # claim a clean OK over "every duplicate". schema.validate() rejects these by
            # name; this is the defensive path, and it must not launder them.
            "verdict": (MISMATCH if wrong else (NOT_CHECKED if malformed else OK)),
            "note": (
                f"{len(detail)} unit(s) listed twice; every one where an in-place row was "
                f"available retained it"
                + (
                    f" ({len(no_in_place)} had none — "
                    f"{', '.join(str(d.get('unit_id', '?')) for d in no_in_place)}, where the "
                    f"alternative row carries no status and no rent, so keeping the other "
                    f"is correct)"
                    if no_in_place else ""
                )
                + f". Keeping the other row instead would move in-place rent by "
                f"{fmt_currency(exposure)}/mo ({fmt_currency(exposure * 12)}/yr) — which "
                f"the market-rent tie-out above cannot see."
                + (
                    f" {malformed} further record(s) were malformed and could NOT be "
                    f"verified, so this row is NOT CHECKED rather than OK."
                    if malformed else ""
                )
                if not wrong else
                f"{len(wrong)} of {len(detail)} duplicate unit(s) retained a row that is "
                f"NOT the in-place lease ({want!r}): "
                f"{', '.join(str(d.get('unit_id', '?')) for d in wrong)}. In-place rent is wrong by "
                f"about {fmt_currency(exposure)}/mo."
            ),
        })

    rows.append({
        "item": "Occupied units",
        "ours": our_occ_units,
        "source": None,
        "diff": None,
        "verdict": INFO,
        "note": "The numerator above, stated separately so the ratio can be re-derived.",
    })

    sample = spot_check_sample(deal)
    readback = list(sample)
    for r in readback:
        v, d = _verdict(r.get("our_rent"), r.get("source_rent"), SPOT_CHECK_TOL)
        # A vacant unit with no rent on either side is a legitimate OK, not an unchecked
        # row: both sides agree there is no rent.
        if r.get("our_rent") is None and r.get("source_rent") is None:
            v, d = "OK (vacant)", 0.0
        r["verdict"], r["diff"] = v, d
    matched = sum(1 for r in readback if str(r["verdict"]).startswith("OK"))
    unchecked = sum(1 for r in readback if r["verdict"] == NOT_CHECKED)
    mismatched = sum(1 for r in readback if r["verdict"] == MISMATCH)

    scored = [r for r in rows if r["verdict"] != INFO]
    ok_n = sum(1 for r in scored if r["verdict"] == OK)
    # The reconciliation verdict (E2 rule 16), computed HERE as a value. The operator's
    # skill wrote it into a cell and read it back; the engine never reads a cell.
    units_row = next(r for r in rows if r["item"] == "Total units")
    market_row = next(r for r in rows if r["item"] == "Market rent (monthly total)")
    dup_row = next(r for r in rows if r["item"].startswith("Duplicate rows"))
    gate = (units_row["verdict"], market_row["verdict"])
    if MISMATCH in gate or dup_row["verdict"] == MISMATCH or mismatched:
        verdict = "FAIL"
    elif NOT_CHECKED in gate:
        verdict = "NOT CHECKED"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "verdict_delta": {"units": units_row["diff"], "market_rent": market_row["diff"]},
        "verdict_basis": (
            "Unit count (tolerance 0) and the export's stated market-rent total (tolerance $1), "
            "plus every duplicate row retaining the in-place lease and no rent-readback "
            "mismatch. FAIL on any; NOT CHECKED when the export states no count or no total to "
            "tie to; PASS only when everything ran and tied."
        ),
        "rows": rows,
        "readback": readback,
        "readback_matched": matched,
        "readback_unchecked": unchecked,
        "readback_mismatched": mismatched,
        "readback_tolerance": SPOT_CHECK_TOL,
        "readback_seed": f"{rr.get('as_of')}|{len(units)}",
        "readback_limit_note": (
            "Both sides of the readback read the same rent column, so it validates "
            "TRANSCRIPTION — that the number was carried across correctly — and cannot "
            "detect the wrong column having been chosen in the first place. The column "
            "choice is evidenced in the audit trail instead."
        ),
        # NOT CHECKED stays in the denominator. Excluding it would let a check that could
        # not run flatter the ratio.
        "scored": len(scored),
        "ok": ok_n,
        "summary": f"{ok_n}/{len(scored)} tie-outs OK" + (
            f"; {sum(1 for r in scored if r['verdict'] == NOT_CHECKED)} NOT CHECKED"
            if any(r["verdict"] == NOT_CHECKED for r in scored) else ""
        ),
        "occupancy_basis": f"{our_occ_units}/{our_units} units, physical",
        "totals": totals,
    }


def usable_divisor(raw) -> float | None:
    """The rate's divisor, or None if it cannot be used.

    ONE rule, called from every tax path. `summary_derived()` rejected only zero and would
    happily compute a NEGATIVE annual tax from a negative divisor, while
    `tax_underwriting()` refused it — so the same deal produced MISSING on one tab and a
    negative amount on another. A divisor is a unit; a non-positive one is not a unit.
    """
    v = as_number(raw)
    if v is None or v <= 0:
        return None
    return v


#: Arizona's Proposition 117 caps annual growth in the limited property value at 5%.
AZ_LPV_CAP = 0.05

#: States where the assessed value resets toward the purchase price on a sale. The generic
#: underwriting shortcut — assessed value is some percentage of what you paid — only holds
#: here. AZ is the counter-example this toolkit was built around. The table itself lives
#: with the rest of the vendored tax rules (one source of truth); this is the same dict.
RESETS_ON_SALE: dict[str, bool] = property_tax_table.RESETS_ON_SALE


def statement_property_tax(deal: dict) -> float | None:
    """The statement's own "Property Taxes" lines over its twelve months, or None.

    As billed IN THE STATEMENT — the operator's ledger, not the county's bill — which is
    the one property-tax figure an upload can state without reaching a portal. None when
    there is no statement or it carries no line in that category; never 0 for "absent".
    """
    t12 = deal.get("t12") if isinstance(deal.get("t12"), dict) else None
    if not t12 or not t12.get("rows"):
        return None
    rows = [r for r in t12["rows"] if r.get("category") == "Property Taxes"]
    if not rows:
        return None
    return round(sum(row_total(r) for r in rows), 2)


def tax_underwriting(deal: dict) -> dict[str, Any]:
    """What a buyer should actually assume for year-one property tax.

    The generic shortcut is that assessed value follows the price you paid. In Arizona it
    does not: Proposition 117 caps growth in the limited property value at 5% a year and
    the LPV does NOT reset on sale, so a buyer inherits the seller's basis. Applying the
    generic pattern here does not produce a slightly wrong number — on this parcel it
    produces roughly three times the real bill, which is enough to fail a deal that works.

    So both are computed and both are shown: the state-correct assumption, and what the
    generic pattern would have said. Showing only the correct one hides the size of the
    trap.
    """
    s = deal["summary"]
    # The USPS code, whatever the document wrote: "Texas" and "TX" are the same state and
    # the rule tables are keyed by code.
    state = property_tax_table.normalise_state(s.get("state")) or str(s.get("state") or "").upper()
    rate = as_number(s.get("tax_rate_as_billed"))
    ratio = as_number(s.get("assessment_ratio"))
    # The divisor IS the rate's unit — per $100, per $1,000, or something else. Inventing
    # one when it is missing does not shade a figure, it changes what the rate means, and
    # the summary path already returns MISSING for the same input. A visible hole beats a
    # plausible-wrong number, and it beats two surfaces disagreeing even more.
    divisor = usable_divisor(s.get("tax_divisor"))
    basis = as_number(s.get("assessed_value_basis"))
    billed = as_number(s.get("tax_billed_amount"))
    exemption = as_number(s.get("tax_exemption")) or 0.0
    pr = pricing(deal)
    price = as_number(pr.get("total_price"))
    units = as_number(deal["pricing"].get("units_total"))
    resets = RESETS_ON_SALE.get(state)
    # Florida's two conventions sit OUTSIDE the ratio: assessed value typically lands at
    # 85% of the price (multiplies the basis) and paying in November earns 4% off the bill
    # (multiplies the tax). Everywhere else both factors are 1.0 and nothing below changes.
    rule = property_tax_table.STATE_RULES.get(state)
    basis_factor = rule.basis_factor if rule is not None else 1.0
    bill_discount = rule.bill_discount if rule is not None else 1.0

    def assess(full_value):
        """Full property value -> assessed value. The ratio is never optional."""
        if full_value is None or ratio is None:
            return None
        return full_value * ratio

    def tax_on(assessed):
        """Assessed value -> annual tax, less any exemption.

        The exemption is subtracted HERE so every surface that shows a tax figure shows the
        same one. `summary_derived()` subtracts it and this function did not, so a parcel
        with an exemption reported two different computed taxes depending on which tab you
        opened — and the tie-out status was calculated from the third.
        """
        if assessed is None or rate is None or divisor is None:
            return None
        return max(0.0, assessed * (rate / divisor) - exemption)

    # THREE figures, ONE path, and the path takes a FULL PROPERTY VALUE — never an
    # already-assessed one. Two bugs have now come from confusing the two:
    #   * the ratio applied to the price but not the LPV -> current-year tax 10x the bill;
    #   * `assumed_basis = generic_assessed` for a reset-on-sale state -> the ratio applied
    #     TWICE, so a 40% Georgia ratio assessed 16% of the price, understated year-one tax
    #     by 60%, and then reported the generic method as a 2.5x overstatement of itself.
    # So the variable names now say which is which, and `assumed_full_value` is always a
    # full value.
    current_assessed = assess(basis)
    current = tax_on(current_assessed)
    generic_assessed = assess(price)
    generic = tax_on(generic_assessed)

    if resets is False:
        assumed_full_value = basis * (1 + AZ_LPV_CAP) if basis is not None else None
        approach = (
            f"{state} does not reset the assessed value on sale. Proposition 117 caps "
            f"growth in the limited property value at {AZ_LPV_CAP:.0%} a year, so year-one "
            f"tax is the current basis grown at the cap — NOT the purchase price times the "
            f"assessment ratio."
        )
        basis_label = f"Current LPV grown {AZ_LPV_CAP:.0%} (the Prop 117 cap)"
    elif resets is True:
        # The PRICE, not `generic_assessed` — the latter has already had the ratio applied.
        assumed_full_value = price * basis_factor if price is not None else None
        approach = (
            f"{state} reassesses toward the transaction price, so the generic pattern "
            f"applies: assessed value is the purchase price times the statutory "
            f"assessment ratio."
        )
        # The FULL value is the price; the ratio is applied one step later. The label used
        # to say "purchase price x assessment ratio" while the field held the bare price,
        # so the tab showed the full price beside a note describing the smaller number.
        basis_label = "Purchase price (the transaction resets the basis)"
        if basis_factor != 1.0 or bill_discount != 1.0:
            approach += (
                f" {state} convention, stated because it is a practice and not a statute: "
                f"assessed value taken at {basis_factor:.0%} of the price, and the bill "
                f"taken at {bill_discount:.0%} for the early-payment discount."
            )
            basis_label = (
                f"Purchase price x {basis_factor:.0%} (assessed value typically lands below "
                f"the price in {state}; the transaction resets the basis)"
            )
    else:
        assumed_full_value = None
        approach = (
            f"No reset rule is vendored for {state or 'this state'}, so no year-one "
            f"assumption is asserted. The generic pattern is shown for reference only and "
            f"must not be used until the state's rule is confirmed."
        )
        basis_label = "not asserted"

    assumed_assessed = assess(assumed_full_value)
    assumed = tax_on(assumed_assessed)
    if assumed is not None and bill_discount != 1.0:
        assumed = assumed * bill_discount
    return {
        "state": state,
        "resets_on_sale": resets,
        "approach": approach,
        "rate": rate,
        "divisor": divisor,
        "ratio": ratio,
        "current_basis": basis,
        "current_basis_label": s.get("tax_basis_label"),
        "current_assessed": current_assessed,
        "current_computed": current,
        "exemption": exemption,
        "current_billed": billed,
        # Two DISTINCT fields, each labelled for what it is. Collapsing them into one
        # "basis" is what let a label drift away from its value.
        "assumed_full_value": assumed_full_value,
        "assumed_full_value_label": basis_label,
        "assumed_assessed": assumed_assessed,
        "assumed_assessed_label": "Assessed value = full value x assessment ratio",
        # Kept as an alias so nothing downstream breaks; prefer assumed_full_value.
        "assumed_basis": assumed_full_value,
        "assumed_basis_label": basis_label,
        "assumed_tax": assumed,
        "assumed_tax_per_unit": safe_div(assumed, units),
        "generic_assessed": generic_assessed,
        "generic_tax": generic,
        "generic_overstatement": (
            safe_div(generic, assumed) if (generic and assumed) else None
        ),
        "price": price,
        "units": units,
    }


def summary_derived(deal: dict) -> dict[str, Any]:
    """Everything on the summary card that is computed rather than read."""
    s = deal["summary"]
    pr = pricing(deal)
    basis = as_number(s.get("assessed_value_basis"))
    ratio = as_number(s.get("assessment_ratio"))
    # Same rule as tax_underwriting(), from the same helper. These two paths disagreeing
    # on what counts as a usable divisor produced MISSING on one tab and a negative tax on
    # another for the same deal.
    divisor = usable_divisor(s.get("tax_divisor"))
    rate = as_number(s.get("tax_rate_as_billed"))
    exemption = as_number(s.get("tax_exemption")) or 0.0
    if None in (basis, ratio, divisor, rate):
        annual_tax: float | str = schema.MISSING
        tax_per_unit: float | str = schema.MISSING
    else:
        annual_tax = basis * ratio / divisor * rate - exemption
        # `as_number`, not truthiness: since round 19 the count may be the MISSING
        # sentinel, which is a non-empty string and therefore truthy — and dividing by it
        # is a TypeError rather than a MISSING.
        _units = as_number(s.get("units_total"))
        tax_per_unit = annual_tax / _units if _units else schema.MISSING
    return {
        "price_per_unit": pr["price_per_unit"],
        "total_price": pr["total_price"],
        "approx_project_value": pr["total_price"],
        "t12_cap_rate": pr["t12_cap_rate"],
        "t3_cap_rate": pr["t3_cap_rate"],
        "t12_noi": pr["t12_noi"],
        "computed_annual_tax": annual_tax,
        "computed_tax_per_unit": tax_per_unit,
    }


# --------------------------------------------------------------------------------------
# Display formatting -- one implementation, so a preview and a test agree on the string
# --------------------------------------------------------------------------------------


def fmt_currency(value: Any, *, cents: bool = False) -> str:
    n = as_number(value)
    if n is None:
        return str(value) if isinstance(value, str) else "—"
    if cents:
        return f"${n:,.2f}" if n >= 0 else f"(${abs(n):,.2f})"
    return f"${n:,.0f}" if n >= 0 else f"(${abs(n):,.0f})"


def fmt_number(value: Any, *, places: int = 0) -> str:
    n = as_number(value)
    if n is None:
        return str(value) if isinstance(value, str) else "—"
    return f"{n:,.{places}f}"


def fmt_year(value: Any) -> str:
    """A year as a year — "1984", never the count formatter's "1,984". A dash when absent."""
    n = as_number(value)
    if n is None:
        return str(value) if isinstance(value, str) else "—"
    return str(int(n))


def fmt_pct(value: Any, *, places: int = 1) -> str:
    n = as_number(value)
    if n is None:
        return str(value) if isinstance(value, str) else "—"
    return f"{n * 100:,.{places}f}%"


def fmt_text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


__all__ = [
    "as_number",
    "parse_date",
    "safe_div",
    "mean",
    "total",
    "detect_month_order",
    "recent_indices",
    "recent_span",
    "row_total",
    "t12_row_totals",
    "category_total",
    "gsr_is_printed",
    "revenue_for_window",
    "pnl",
    "breakouts",
    "error_checks",
    "pricing",
    "classify_lease",
    "plan_summary",
    "move_in_counts",
    "roll_totals",
    "trade_out",
    "comps",
    "demand",
    "supply",
    "summary_derived",
    "tax_check",
    "fmt_currency",
    "fmt_number",
    "fmt_pct",
    "fmt_text",
]


# --------------------------------------------------------------------------------------
# Statement sanity guards (E2 rules 1, 3, 4, 5) — flagged, never silently corrected
# --------------------------------------------------------------------------------------

#: Categories that can only ever reduce revenue. One netting POSITIVE is the RealPage /
#: OneSite convention of printing bad debt as a positive figure — or a mis-filed line.
CONTRA_REVENUE_CATEGORIES: tuple[str, ...] = ("Vacancy", "Concessions", "Non-Revenue Units", "Delinquency")
RECOVERY_WORDS = re.compile(r"rebill|reimburs|recover|recaptur", re.I)
SUBTOTAL_WORDS = re.compile(r"\btotal\b|\bsubtotal\b|\bsum\b", re.I)
DOUBLE_COUNT_FLOOR = 500.0   # |value| below this is noise, not a subtotal
DOUBLE_COUNT_SPAN = 25       # a subtotal sits near the rows it sums
DOUBLE_COUNT_TOL = 1.0


def statement_guards(deal: dict) -> list[dict[str, Any]]:
    """Four sanity checks on the categorised statement. Each row is OK or REVIEW with the
    lines it names; nothing here changes a number — an engine that flipped a sign on its
    own would be making a claim about the statement, so it flags and the reader decides.
    Empty when the deal carries no statement."""
    if schema.section_absent(deal, "t12"):
        return []
    rows = deal["t12"]["rows"]
    out: list[dict[str, Any]] = []

    positive = [cat for cat in CONTRA_REVENUE_CATEGORIES if (category_total(deal, cat) or 0) > 0]
    out.append({
        "guard": "Contra-revenue categories net negative",
        "status": "REVIEW" if positive else "OK",
        "rows": positive,
        "detail": (
            f"{', '.join(positive)} net POSITIVE over twelve months. A contra-revenue "
            f"category can only reduce revenue; a positive net is usually a statement that "
            f"prints bad debt as a positive figure (the RealPage / OneSite convention). "
            f"Treated as printed, not flipped — confirm the sign before the P&L is used."
            if positive else
            f"Every contra-revenue category ({', '.join(CONTRA_REVENUE_CATEGORIES)}) nets "
            f"negative, as it must."
        ),
    })

    opex = pnl(deal)["total_opex"]
    out.append({
        "guard": "Total operating expenses positive",
        "status": "OK" if (opex or 0) > 0 else "REVIEW",
        "rows": [],
        "detail": (
            f"Total operating expenses come to {fmt_currency(opex)}. A non-positive total "
            f"means the expense lines are printed as negatives; NOI would add them back and "
            f"double, and so would the cap rate. Not flipped here — confirm the convention."
            if not (opex or 0) > 0 else
            f"Total operating expenses are {fmt_currency(opex)} — positive, as an expense "
            f"total must be."
        ),
    })

    recoveries = [r["label"] for r in rows
                  if r.get("category") in schema.T12_EXPENSE_CATEGORIES
                  and row_total(r) < 0 and RECOVERY_WORDS.search(str(r.get("label") or ""))]
    out.append({
        "guard": "No recovery lines inside expense buckets",
        "status": "REVIEW" if recoveries else "OK",
        "rows": recoveries,
        "detail": (
            f"{len(recoveries)} negative line(s) inside an expense category read like a "
            f"recovery ({', '.join(recoveries)}). A rebill or reimbursement is revenue "
            f"(Utility Reimbursements); netted inside the expense it understates both "
            f"sides. The sign is the tell — a positive line with the same words is a genuine "
            f"expense."
            if recoveries else
            "No negative line inside an expense category carries a recovery word "
            "(rebill, reimburse, recover, recapture)."
        ),
    })

    categorised = [(i, r) for i, r in enumerate(rows) if r.get("category")]
    doubles: list[str] = []
    for i, r in categorised:
        v = row_total(r)
        if abs(v) < DOUBLE_COUNT_FLOOR or not SUBTOTAL_WORDS.search(str(r.get("label") or "")):
            continue
        near = [(j, x) for j, x in categorised if j != i and abs(j - i) <= DOUBLE_COUNT_SPAN]
        found = None
        for k in (1, 2, 3):
            for combo in itertools.combinations(near, k):
                if abs(fsum(row_total(x) for _, x in combo) - v) <= DOUBLE_COUNT_TOL:
                    found = combo
                    break
            if found:
                break
        if found:
            doubles.append(f"{r['label']} = " + " + ".join(str(x.get("label")) for _, x in found))
    out.append({
        "guard": "No subtotal row counted with its members",
        "status": "REVIEW" if doubles else "OK",
        "rows": doubles,
        "detail": (
            f"{len(doubles)} categorised row(s) named like a subtotal equal the sum of one to "
            f"three nearby categorised rows within a dollar: {'; '.join(doubles)}. Counted "
            f"with its members, a subtotal doubles that money. Leave the subtotal row "
            f"uncategorised."
            if doubles else
            f"No categorised row named like a subtotal (|value| ≥ {fmt_currency(DOUBLE_COUNT_FLOOR)}) "
            f"equals the sum of one to three rows within {DOUBLE_COUNT_SPAN} lines of it."
        ),
    })
    return out


# --------------------------------------------------------------------------------------
# Trailing annualised T-3 revenue (E2 rule 8)
# --------------------------------------------------------------------------------------

def revenue_by_month(deal: dict) -> list[tuple[str, float]]:
    """``(label, revenue)`` per month, OLDEST FIRST whatever order the statement runs,
    under the P&L's own rule: printed Gross Scheduled Rent where the statement has one,
    else GPR + gain/(loss) to lease, plus the tail through Revenue Adjustments."""
    labels = deal["t12"]["month_labels"]
    idx = list(range(len(labels)))
    if detect_month_order(labels) == "newest_first":
        idx.reverse()
    printed = gsr_is_printed(deal)
    tail = _revenue_tail()

    def cat_month(category: str, i: int) -> float:
        return total(row_total(row, [i]) for row in deal["t12"]["rows"] if row.get("category") == category)

    out = []
    for i in idx:
        gsr = (cat_month("Gross Scheduled Rent", i) if printed
               else cat_month("Gross Potential Rent", i) + cat_month("Gain / (Loss) to Lease", i))
        out.append((labels[i], gsr + fsum(cat_month(cat, i) for cat in tail)))
    return out


def t3_trailing_series(deal: dict) -> list[dict[str, Any]]:
    """The six figures the model takes: for each of the six most recent months, that month
    plus the two before it, times four. Fewer than eight months give fewer figures — never
    a padded zero; fewer than three give none."""
    if schema.section_absent(deal, "t12"):
        return []
    months = revenue_by_month(deal)
    n = len(months)
    out = []
    for k in range(max(2, n - 6), n):
        label = months[k][0]
        t3 = months[k][1] + months[k - 1][1] + months[k - 2][1]
        out.append({"month": label, "t3_revenue_annualized": t3 * 4})
    return out


# --------------------------------------------------------------------------------------
# Dedup disclosure (E2 rule 14)
# --------------------------------------------------------------------------------------

DUPLICATE_DISCLOSURE_CAP = 200


def duplicate_disclosure(deal: dict) -> dict[str, Any]:
    """Every unit the export listed twice: the row kept, the rows not counted, and why —
    capped at :data:`DUPLICATE_DISCLOSURE_CAP` rows with the overflow counted, so a roll
    with hundreds of turnover pairs still discloses itself without swamping the tab."""
    stated = (deal.get("rent_roll") or {}).get("stated") or {}
    detail = [d for d in (stated.get("duplicate_detail") or []) if _dup_record_usable(d)]
    want = stated.get("in_place_lease_status")
    rows = []
    for d in detail[:DUPLICATE_DISCLOSURE_CAP]:
        kept_status = d.get("kept_lease_status")
        if want and kept_status == want:
            why = f"Kept the row whose lease status is the in-place one ({want!r}); the other row is the incoming lease."
        elif want:
            why = (f"No row carries the in-place status ({want!r}); kept the row with a stated rent "
                   f"and a status, which is the lease in place on the as-of date.")
        else:
            why = "The export states no lease status; kept the row with a stated rent."
        rows.append({
            "unit_id": d.get("unit_id"),
            "kept_row": d.get("kept_row"),
            "kept_status": kept_status,
            "kept_rent": d.get("kept_rent_stated"),
            "dropped": [{"row": x.get("row"), "status": x.get("lease_status"), "rent": x.get("rent_stated")}
                        for x in (d.get("dropped") or [])],
            "why": why,
        })
    return {"rows": rows, "total": len(detail), "overflow": max(0, len(detail) - DUPLICATE_DISCLOSURE_CAP)}

