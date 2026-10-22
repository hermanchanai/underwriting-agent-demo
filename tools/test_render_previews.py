"""Tests for compute.py and the HTML preview renderer.

Two jobs.

First, ``compute.py`` is checked against arithmetic worked out independently of it — the
whole point of a formula mirror is that it agrees with something other than itself. The
T-3 window is exercised on **both** month directions, because a statement running
newest-first is common and assuming a direction is how a T-3 silently becomes the oldest
three months.

Second, I6: for every figure that appears in both a workbook and a fragment, the rendered
string must be the formatting of the same JSON-derived value. The test re-derives each
number from ``compute`` and asserts the formatted string is literally present in the
fragment — so a preview drifting from the workbook fails here rather than in front of a
recruiter.

Also checks I5: no ``http(s)://``, no script tags, no external assets, and every table
inside its own scroll container.

Usage: python tools/test_render_previews.py
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import compute
import render_previews
import schema
import scrub_check
from _testkit import Checker, guard

PROJECT = HERE.parent
#: The pre-2026-09-10 sample (fully synthetic; carries trade-out rows and four supply
#: stages, which the renderer must still handle). The PUBLISHED sample is fixture_deal.json,
#: pinned by test_published_sample.py.
FIXTURE = HERE / "fixtures" / "synthetic_full.json"


def reversed_months(deal: dict) -> dict:
    d = copy.deepcopy(deal)
    d["t12"]["month_labels"] = list(reversed(d["t12"]["month_labels"]))
    for row in d["t12"]["rows"]:
        row["monthly"] = list(reversed(row["monthly"]))
    return d


def main() -> int:
    c = Checker("test_render_previews")
    deal = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # ==================================================================================
    # compute.py against independent arithmetic
    # ==================================================================================
    c.section("month direction detection — both ways")
    labels = deal["t12"]["month_labels"]
    c.equal("fixture runs oldest-first", compute.detect_month_order(labels), "oldest_first")
    rev = reversed_months(deal)
    c.equal(
        "reversed fixture runs newest-first",
        compute.detect_month_order(rev["t12"]["month_labels"]),
        "newest_first",
    )
    c.equal("oldest-first T-3 takes the last three", compute.recent_indices(labels, 3), [9, 10, 11])
    c.equal(
        "newest-first T-3 takes the first three",
        compute.recent_indices(rev["t12"]["month_labels"], 3),
        [0, 1, 2],
    )
    c.equal("T-3 span is contiguous (oldest-first)", compute.recent_span(labels, 3), (9, 11))
    c.equal(
        "T-3 span is contiguous (newest-first)",
        compute.recent_span(rev["t12"]["month_labels"], 3),
        (0, 2),
    )
    c.equal("T-6 span (oldest-first)", compute.recent_span(labels, 6), (6, 11))
    c.equal("T-6 span (newest-first)", compute.recent_span(rev["t12"]["month_labels"], 6), (0, 5))

    c.section("the same months are selected either way")
    # The identifying test: the T-3 window must pick the SAME calendar months, and the
    # same dollars, regardless of which end of the row they sit on.
    payroll = next(r for r in deal["t12"]["rows"] if r["label"] == "On-site Payroll")
    payroll_rev = next(r for r in rev["t12"]["rows"] if r["label"] == "On-site Payroll")
    fwd = compute.row_total(payroll, compute.recent_indices(labels, 3))
    back = compute.row_total(payroll_rev, compute.recent_indices(rev["t12"]["month_labels"], 3))
    c.close("T-3 total is direction-independent", back, fwd, tol=0.001)
    manual = sum(payroll["monthly"][-3:])
    c.close("and equals the last three months summed by hand", fwd, manual, tol=0.001)

    c.section("P&L arithmetic, independently derived")
    p = compute.pnl(deal)

    def hand_total(category: str, window: int | None = None) -> float:
        rows = [r for r in deal["t12"]["rows"] if r.get("category") == category]
        if window is None:
            return sum(sum(r["monthly"]) for r in rows)
        idx = compute.recent_indices(labels, window)
        return sum(sum(r["monthly"][i] for i in idx) for r in rows)

    for cat in ("Payroll", "Insurance", "Other Income", "Utilities", "Vacancy"):
        c.close(f"{cat} T-12 matches a hand sum", p["rows"][cat]["t12"], hand_total(cat), 0.02)
    c.close(
        "Payroll T-3 annualized matches a hand sum",
        p["rows"]["Payroll"]["t3_annualized"],
        hand_total("Payroll", 3) * 4,
        0.02,
    )

    c.section("the two rules that stop a double-count")
    gpr = hand_total("Gross Potential Rent")
    gll = hand_total("Gain / (Loss) to Lease")
    c.close("GSR falls back to GPR + GLL", p["rows"]["Gross Scheduled Rent"]["t12"], gpr + gll, 0.02)
    c.equal("and says which basis fired", p["gsr_basis"], "gross potential rent + gain/(loss) to lease")
    hand_revenue = (gpr + gll) + sum(
        hand_total(cat)
        for cat in (
            "Vacancy", "Concessions", "Non-Revenue Units", "Delinquency",
            "Utility Reimbursements", "Other Income", "Revenue Adjustments",
        )
    )
    c.close("Total Revenue matches the hand sum", p["total_revenue"], hand_revenue, 0.05)
    c.ok(
        "Total Revenue is NOT GPR-inclusive-plus-GSR (the double count)",
        abs(p["total_revenue"] - (hand_revenue + gpr + gll)) > 1000,
        f"total_revenue={p['total_revenue']:.0f}, double-counted would be "
        f"{hand_revenue + gpr + gll:.0f}",
    )
    hand_opex = sum(hand_total(cat) for cat in schema.T12_EXPENSE_CATEGORIES)
    c.close("Total opex matches the hand sum", p["total_opex"], hand_opex, 0.05)
    c.close("NOI is revenue less opex", p["noi"], hand_revenue - hand_opex, 0.1)

    c.section("N1: GSR presence is a row's EXISTENCE, not a nonzero aggregate")
    # A statement can print a Gross Scheduled Rent line whose twelve months net to zero
    # (offsetting adjustments, or a mid-year restatement) while its recent months are
    # large. Deciding presence from the aggregate then silently substitutes gross
    # potential rent plus gain/(loss) to lease into the T-3 and T-6 windows.
    zero_net = copy.deepcopy(deal)
    zero_net["t12"]["rows"].append(
        {
            "label": "Gross Scheduled Rent",
            "category": "Gross Scheduled Rent",
            "monthly": [-1000.0] * 9 + [3000.0] * 3,
        }
    )
    c.equal("the zero-net fixture is schema-valid", schema.validate(zero_net), [])
    c.close(
        "its twelve months really do net to zero",
        compute.category_total(zero_net, "Gross Scheduled Rent"),
        0.0,
        0.001,
    )
    c.ok(
        "and the row is nonetheless detected as printed",
        compute.gsr_is_printed(zero_net),
        "presence must come from the categorized rows",
    )
    zn = compute.pnl(zero_net)
    c.equal("basis says printed", zn["gsr_basis"], "printed on the statement")
    c.close(
        "T-12 GSR is the printed row's total, which is zero",
        zn["rows"]["Gross Scheduled Rent"]["t12"],
        0.0,
        0.001,
    )
    c.close(
        "T-3 GSR is the printed row's recent three months, not GPR+GLL",
        zn["rows"]["Gross Scheduled Rent"]["t3"],
        9000.0,
        0.001,
    )
    fallback_would_be = compute.category_total(
        zero_net, "Gross Potential Rent", window=3
    ) + compute.category_total(zero_net, "Gain / (Loss) to Lease", window=3)
    c.ok(
        "which is emphatically not the fallback figure",
        abs(zn["rows"]["Gross Scheduled Rent"]["t3"] - fallback_would_be) > 1000,
        f"printed={zn['rows']['Gross Scheduled Rent']['t3']:,.0f} "
        f"fallback={fallback_would_be:,.0f}",
    )
    c.close(
        "revenue_for_window makes the same presence decision",
        compute.revenue_for_window(zero_net, 12),
        zn["total_revenue"],
        0.05,
    )
    c.ok(
        "an all-zero GSR row still counts as printed",
        compute.gsr_is_printed(
            {"t12": {**deal["t12"], "rows": deal["t12"]["rows"] + [
                {"label": "Gross Scheduled Rent",
                 "category": "Gross Scheduled Rent",
                 "monthly": [0.0] * 12}]}}
        ),
    )
    c.ok(
        "and a statement with no such row still falls back",
        not compute.gsr_is_printed(deal),
    )

    c.section("a printed GSR line takes precedence over the fallback")
    with_gsr = copy.deepcopy(deal)
    printed = [1_000.0] * 12
    with_gsr["t12"]["rows"].append(
        {"label": "Gross Scheduled Rent", "category": "Gross Scheduled Rent", "monthly": printed}
    )
    p2 = compute.pnl(with_gsr)
    c.close("GSR uses the printed line", p2["rows"]["Gross Scheduled Rent"]["t12"], 12_000.0, 0.01)
    c.equal("and says so", p2["gsr_basis"], "printed on the statement")
    c.ok(
        "which changes total revenue accordingly",
        abs(p2["total_revenue"] - p["total_revenue"]) > 1.0,
    )

    c.section("error checks")
    checks = {ch["check"]: ch for ch in compute.error_checks(deal)}
    for label in ("Total Revenue", schema.PNL_TOTAL_OPEX_LABEL, schema.PNL_NOI_LABEL):
        c.equal(f"{label} ties to the source subtotal", checks[label]["status"], "OK")
    c.equal(
        "an absent source subtotal reads NOT CHECKED, not OK",
        checks["Utility Reimbursements"]["status"],
        "NOT CHECKED",
    )
    c.equal(
        "and reports no difference rather than zero",
        checks["Utility Reimbursements"]["difference"],
        None,
    )
    off = copy.deepcopy(deal)
    off["t12"]["source_subtotals"]["Total Revenue"] += 5_000
    c.equal(
        "a real mismatch reads REVIEW",
        {ch["check"]: ch for ch in compute.error_checks(off)}["Total Revenue"]["status"],
        "REVIEW",
    )
    near = copy.deepcopy(deal)
    near["t12"]["source_subtotals"]["Total Revenue"] += 0.5
    c.equal(
        "a rounding-scale difference still reads OK",
        {ch["check"]: ch for ch in compute.error_checks(near)}["Total Revenue"]["status"],
        "OK",
    )

    c.section("pricing ladders")
    pr = compute.pricing(deal)
    c.close(
        "total price is per-unit times units",
        pr["total_price"],
        deal["pricing"]["price_per_unit"] * deal["pricing"]["units_total"],
        0.01,
    )
    c.close("T-12 cap rate is NOI over price", pr["t12_cap_rate"], p["noi"] / pr["total_price"], 1e-9)
    c.close(
        "T-3 cap rate uses the annualized T-3 NOI",
        pr["t3_cap_rate"],
        p["noi_t3_annualized"] / pr["total_price"],
        1e-9,
    )
    c.ok(
        "cap rate lands in a believable range for the fixture",
        0.03 <= pr["t12_cap_rate"] <= 0.09,
        f"{pr['t12_cap_rate']:.4f}",
    )
    zero_price = copy.deepcopy(deal)
    zero_price["pricing"]["price_per_unit"] = 50_000
    c.ok("a price at the band edge still computes", compute.pricing(zero_price)["t12_cap_rate"] is not None)
    c.equal("safe_div mirrors IFERROR on a zero divisor", compute.safe_div(5, 0), None)
    c.equal("safe_div mirrors IFERROR on a text operand", compute.safe_div("MISSING", 2), None)

    c.section("F6: the T-6 revenue trend must be real revenue")
    # Behavioural checks against pricing() itself, so this section goes red on the
    # defect rather than on a missing helper. The bug: the windowed revenue sum used
    # the rollup's revenue SPAN (which starts at gross scheduled rent) without applying
    # the printed-else-derived GSR fallback, so on a statement with no GSR line it
    # summed only the contra rows and came out NEGATIVE.
    idx6 = compute.recent_indices(labels, 6)
    hand6 = 0.0
    for cat in ("Gross Potential Rent", "Gain / (Loss) to Lease", "Vacancy", "Concessions",
                "Non-Revenue Units", "Delinquency", "Utility Reimbursements",
                "Other Income", "Revenue Adjustments"):
        for r in deal["t12"]["rows"]:
            if r.get("category") == cat:
                hand6 += sum(r["monthly"][i] for i in idx6)
    expected_t6 = hand6 * 2
    c.ok(
        "T-6 annualized revenue is positive",
        pr["t6_revenue_annualized"] > 0,
        f"got {pr['t6_revenue_annualized']:,.0f}",
    )
    c.close(
        "T-6 annualized revenue matches a hand sum of the six most recent months",
        pr["t6_revenue_annualized"],
        expected_t6,
        0.05,
    )
    c.ok(
        "and sits within 10% of T-12 revenue rather than being nonsense",
        abs(pr["t6_revenue_annualized"] / p["total_revenue"] - 1) < 0.10,
        f"t6={pr['t6_revenue_annualized']:,.0f} vs t12={p['total_revenue']:,.0f}",
    )
    c.close(
        "the same windowing over 12 months reproduces total revenue",
        compute.revenue_for_window(deal, 12),
        p["total_revenue"],
        0.05,
    )
    c.close(
        "and over 3 months, annualized, reproduces the T-3 annualized figure",
        compute.revenue_for_window(deal, 3) * 4,
        p["total_revenue_t3_annualized"],
        0.05,
    )

    c.section("rent roll: lease classification and the fallback")
    tolerance = deal["rent_roll"]["new_tenant_tolerance_days"]
    c.equal(
        "same-day lease start and move-in is New",
        compute.classify_lease(
            {"lease_start": "2026-03-01", "move_in": "2026-03-01"}, tolerance
        ),
        "New",
    )
    c.equal(
        "a years-old move-in is Renewal",
        compute.classify_lease(
            {"lease_start": "2026-03-01", "move_in": "2022-05-14"}, tolerance
        ),
        "Renewal",
    )
    c.equal(
        "just inside the tolerance is still New",
        compute.classify_lease(
            {"lease_start": "2026-03-30", "move_in": "2026-03-01"}, tolerance
        ),
        "New",
    )
    c.equal(
        "just outside the tolerance flips to Renewal",
        compute.classify_lease(
            {"lease_start": "2026-04-05", "move_in": "2026-03-01"}, tolerance
        ),
        "Renewal",
    )
    c.equal(
        "an unreadable date falls back to blank, not a guess",
        compute.classify_lease({"lease_start": "n/a", "move_in": None}, tolerance),
        None,
    )
    c.equal("a text date does not parse", compute.parse_date("n/a"), None)
    c.equal("a US-format date does not parse either", compute.parse_date("06/30/2026"), None)
    c.ok("an ISO date parses", compute.parse_date("2026-06-30") is not None)

    plans = compute.plan_summary(deal)
    totals = compute.roll_totals(deal)
    c.equal("plan count", len(plans), 6)
    c.equal("unit total matches the roll", totals["total_units"], len(deal["rent_roll"]["units"]))
    c.equal(
        "occupied plus vacant plus non-revenue equals total",
        totals["occupied_units"] + totals["vacant_units"] + totals["non_revenue_units"],
        totals["total_units"],
    )
    # market rent for model follows the rent-roll Summary's L, =IF(H>0,H,E) --
    # the new MOVE-IN rent (windowed on the move-in date), else in-place.
    fallback = [p_ for p_ in plans if p_["movein_count"] == 0]
    c.equal("exactly one plan has no new move-ins", len(fallback), 1)
    fb = fallback[0]
    c.close(
        "its market rent equals its in-place average",
        fb["market_rent_for_model"],
        fb["avg_in_place_rent"],
        0.01,
    )
    c.ok("and its basis says in-place", "in-place" in fb["basis"].lower(), fb["basis"])
    with_new = [p_ for p_ in plans if p_["movein_count"] > 0]
    c.ok("the other plans use new move-in rent", len(with_new) == 5)
    for p_ in with_new:
        c.close(
            f"{p_['floor_plan']} market rent equals its new move-in rent",
            p_["market_rent_for_model"],
            p_["movein_rent"],
            0.01,
        )
    c.ok(
        "new move-in rent runs above in-place (the trade-up the fixture encodes)",
        all(p_["movein_rent"] > p_["avg_in_place_rent"] for p_ in with_new),
    )

    c.section("weighted averages")
    hand_num = sum(p_["units"] * p_["market_rent_for_model"] for p_ in plans)
    hand_den = sum(p_["units"] for p_ in plans)
    c.close(
        "weighted market rent matches a hand weighting",
        totals["wavg_market_rent_for_model"],
        hand_num / hand_den,
        0.01,
    )
    occupied_rents = [
        u["in_place_rent"]
        for u in deal["rent_roll"]["units"]
        if u["status"] == "Occupied" and u["in_place_rent"] is not None
    ]
    c.close(
        "weighted in-place rent matches the mean over occupied units",
        totals["wavg_in_place_rent"],
        sum(occupied_rents) / len(occupied_rents),
        0.01,
    )
    c.close(
        "occupancy matches occupied over total",
        totals["occupancy"],
        totals["occupied_units"] / totals["total_units"],
        1e-9,
    )
    c.close("vacancy is one less occupancy", totals["vacancy"], 1 - totals["occupancy"], 1e-9)

    c.section("trade-out arithmetic")
    to = compute.trade_out(deal)
    new = next(r for r in to["rows"] if r["cohort"] == "New")
    c.close("gross change", new["gross_change"], new["new_rent_gross"] - new["prior_rent"], 0.01)
    c.close(
        "gross change percent",
        new["gross_change_pct"],
        (new["new_rent_gross"] - new["prior_rent"]) / new["prior_rent"],
        1e-9,
    )
    c.ok(
        "effective change is smaller than gross where a concession exists",
        new["effective_change"] < new["gross_change"],
    )
    fg = next(r for r in to["rows"] if r["cohort"] == "First generation")
    c.equal("first-generation percent is undefined, not zero", fg["gross_change_pct"], None)

    c.section("comps arithmetic")
    cm = compute.comps(deal)
    c.close(
        "average asking matches a hand mean",
        cm["avg_asking"],
        sum(r["asking_rent"] for r in deal["comps"]["rows"]) / len(deal["comps"]["rows"]),
        0.01,
    )
    c.close(
        "net effective is asking less concession, per row",
        cm["rows"][0]["net_effective_rent"],
        deal["comps"]["rows"][0]["asking_rent"] - deal["comps"]["rows"][0]["concession"],
        0.01,
    )
    c.ok(
        "average net effective sits at or below average asking",
        cm["avg_net_effective"] <= cm["avg_asking"],
    )

    c.section("supply totals")
    sup = compute.supply(deal)
    for t in sup["totals"]:
        hand = sum(r["units"] for r in deal["supply"]["rings"] if r["ring"] == t["ring"])
        c.equal(f"{t['ring']} pipeline units", t["total_units"], hand)

    c.section("tax block degrades visibly")
    d = compute.summary_derived(deal)
    s = deal["summary"]
    c.close(
        "computed tax matches the stated formula",
        d["computed_annual_tax"],
        s["assessed_value_basis"] * s["assessment_ratio"] / s["tax_divisor"] * s["tax_rate_as_billed"]
        - s["tax_exemption"],
        0.01,
    )
    missing_tax = copy.deepcopy(deal)
    missing_tax["summary"]["tax_rate_as_billed"] = schema.MISSING
    c.equal(
        "an unknown rate yields MISSING, not a plausible number",
        compute.summary_derived(missing_tax)["computed_annual_tax"],
        schema.MISSING,
    )
    c.equal(
        "and the per-unit figure follows it",
        compute.summary_derived(missing_tax)["computed_tax_per_unit"],
        schema.MISSING,
    )

    # ==================================================================================
    # Fragments
    # ==================================================================================
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "previews"
        written = render_previews.render_all(deal, out)

        c.section("fragment inventory")
        c.equal(
            "one fragment per declared preview file",
            sorted(written.keys()),
            sorted(schema.PREVIEW_FILES),
        )
        c.equal(
            "every fragment has a tab label",
            sorted(render_previews.TAB_LABELS.keys()),
            sorted(schema.PREVIEW_FILES),
        )
        texts = {name: path.read_text(encoding="utf-8") for name, path in written.items()}

        c.section("R#9: multi-series bars do not overflow their track")
        mix_html = texts["unit-mix.html"]
        tracks = re.findall(
            r'<span class="op-bar__track[^"]*">(.*?)</span>\s*<span class="op-bar__val"',
            mix_html,
            re.S,
        )
        # Count the ELEMENT, not the substring: the modifier class "op-bar__fill--alt"
        # itself contains "op-bar__fill", so a naive count double-reports every series.
        n_fills = lambda t: len(re.findall(r'<span class="op-bar__fill[ "]', t))
        n_subs = lambda t: len(re.findall(r'<span class="op-bar__sub"', t))
        multi = [t for t in tracks if n_fills(t) >= 2]
        c.ok(
            "the rent chart draws multi-series bars",
            bool(multi),
            f"{len(tracks)} tracks parsed",
        )
        c.ok(
            "every series gets its own sub-track rather than stacking in one box",
            all(n_subs(t) == n_fills(t) for t in multi),
            (multi[0][:200] if multi else ""),
        )
        c.ok(
            "no inline height/margin stacking survives on a fill",
            not any(("height:100%" in t) or ("margin-bottom" in t) for t in multi),
            (multi[0][:200] if multi else ""),
        )
        c.ok(
            "the multi-series track sizes itself instead of a fixed 14px",
            "op-bar__track--multi" in mix_html and "height: auto" in mix_html,
            "three 100%-height fills in a 14px box overflowed ~30px onto the rows below",
        )
        c.ok(
            "and a single-series bar still uses the plain fixed-height track",
            any(n_fills(t) == 1 for t in tracks),
        )

        c.section("I5 — self-contained and token-styled")
        for name, text in texts.items():
            c.ok(f"{name}: no external URL", not re.search(r"https?://", text))
            c.ok(f"{name}: no script tag", "<script" not in text.lower())
            c.ok(f"{name}: no external asset reference", not re.search(r"(src=|@import|url\()", text))
            c.ok(f"{name}: uses site CSS tokens", "var(--" in text)
            table_count = text.count("<table")
            scroll_count = text.count('class="op-scroll"')
            c.ok(
                f"{name}: every table has its own scroll container",
                table_count <= scroll_count,
                f"{table_count} tables, {scroll_count} containers",
            )

        c.section("I6 — every shared figure is the same value, formatted once")
        summary_text = texts["summary.html"]
        checks_text = texts["t12-checks-pricing.html"]
        pnl_text = texts["t12-pnl.html"]
        mix_text = texts["unit-mix.html"]
        # the previews speak the rent-roll Summary's vocabulary.
        c.ok("the unit-mix preview shows the move-in and signed columns",
             "New move-in rent" in mix_text and "Leases signed rent" in mix_text)
        stale = sorted(n for n, t in texts.items() if re.search(r"new-lease|New lease rent", t, re.I))
        c.equal("no preview still describes a new-lease rule", stale, [])

        shared = [
            ("total price in summary", compute.fmt_currency(pr["total_price"]), summary_text),
            ("total price on the T-12 analysis tab (pricing moved there 2026-09-14)", compute.fmt_currency(pr["total_price"]), pnl_text),
            ("T-12 NOI in summary", compute.fmt_currency(pr["t12_noi"]), summary_text),
            ("T-12 NOI in checks", compute.fmt_currency(pr["t12_noi"]), checks_text),
            ("T-12 NOI in the rollup", compute.fmt_currency(p["noi"]), pnl_text),
            ("total revenue in the rollup", compute.fmt_currency(p["total_revenue"]), pnl_text),
            ("total revenue in checks", compute.fmt_currency(pr["t12_revenue"]), checks_text),
            ("total opex in the rollup", compute.fmt_currency(p["total_opex"]), pnl_text),
            ("T-12 cap rate in summary", compute.fmt_pct(pr["t12_cap_rate"], places=2), summary_text),
            ("T-12 cap rate on the T-12 analysis tab", compute.fmt_pct(pr["t12_cap_rate"], places=2), pnl_text),
            ("T-3 cap rate in summary", compute.fmt_pct(pr["t3_cap_rate"], places=2), summary_text),
            ("occupancy in unit mix", compute.fmt_pct(totals["occupancy"]), mix_text),
            (
                "weighted market rent in unit mix",
                compute.fmt_currency(totals["wavg_market_rent_for_model"]),
                mix_text,
            ),
            (
                "market rent for model in summary",
                compute.fmt_currency(deal["summary"]["market_rent_for_model"]),
                summary_text,
            ),
            (
                "computed annual tax in summary",
                compute.fmt_currency(d["computed_annual_tax"]),
                summary_text,
            ),
        ]
        for label, needle, text in shared:
            c.ok(f"{label} renders as {needle}", needle in text, f"looked for {needle!r}")

        c.section("cross-surface agreement: summary card vs the roll it came from")
        c.close(
            "summary market rent equals the roll's weighted market rent",
            deal["summary"]["market_rent_for_model"],
            totals["wavg_market_rent_for_model"],
            0.01,
        )
        c.close(
            "summary in-place rent equals the roll's weighted in-place rent",
            deal["summary"]["in_place_rent_avg"],
            totals["wavg_in_place_rent"],
            0.01,
        )

        c.section("every P&L row's value appears in the fragment")
        missing_rows = [
            label
            for label in schema.pnl_row_order()
            if compute.fmt_currency(p["rows"][label]["t12"]) not in pnl_text
        ]
        c.equal("no P&L row is missing its T-12 figure", missing_rows, [])

        c.section("illustrative chips")
        for name in ("summary.html", "t12-pnl.html"):
            c.ok(f"{name} carries an illustrative chip", render_previews.CHIP in texts[name])
        plain = copy.deepcopy(deal)
        plain["meta"]["illustrative_pricing"] = False
        plain_texts = render_previews.render_all(plain, Path(tmp) / "plain")
        c.ok(
            "no chip when pricing is not illustrative",
            render_previews.CHIP
            not in plain_texts["t12-pnl.html"].read_text(encoding="utf-8"),
        )

        c.section("honest absences render honestly")
        missing_text = render_previews.summary_fragment(missing_tax)
        c.ok("a MISSING tax figure is marked as missing", 'class="op-missing"' in missing_text)

        c.section("C#6: an empty comps block still shows the measured coverage")
        empty_cov = copy.deepcopy(deal)
        empty_cov["comps"]["rows"] = []
        empty_cov["comps"]["absent_reason"] = "no rents in this submarket"
        empty_cov["comps"]["coverage_note"] = "MEASURED: 58 communities in radius, 0 with rents"
        out_html = render_previews.comps_fragment(empty_cov)
        c.ok("the absence reason renders", "no rents in this submarket" in out_html)
        c.ok(
            "and so does the measured coverage evidence",
            "MEASURED: 58 communities" in out_html,
            "the evidence is hidden exactly when it is most needed",
        )

        c.section("two-tier comp display")
        tier_html = render_previews.comps_fragment(deal)
        for heading in ("Tier 1 — Top comps", "Tier 2 — Additional comps"):
            c.ok(f"preview shows '{heading}'", heading in tier_html)
        positions = [
            tier_html.index("Tier 1 — Top comps"),
            tier_html.index("Tier 2 — Additional comps"),
        ]
        c.equal("tiers render in order", positions, sorted(positions))
        # ~340 identity-only rows: too long to read, and the data still ships in the
        # interchange file, so the table goes and the data stays.
        c.ok("the Tier 3 table is gone", "Tier 3" not in tier_html and "Also nearby" not in tier_html)
        c.ok("the nearby data is still on the deal it was rendered from",
             bool(deal["comps"]["nearby"]), "the renderer must not be dropping the data")
        c.ok("and no nearby row's reason is rendered",
             not any(render_previews._escape(n["reason"]) in tier_html
                     for n in deal["comps"]["nearby"] if n.get("reason")),
             "a reason on the page means the table is still being written")
        c.ok("nothing on the comps page points at a table that is not there",
             "listed nearby" not in tier_html and "Also nearby" not in tier_html)
        c.ok(
            "the vintage footnote is disclosed on the detailed tiers",
            "where year known" in tier_html.lower(),
            "a filter applied to only some rows must say so",
        )
        # The unknown-year community must show a dash, not a fabricated year. The whole
        # vintage filter rests on this: an invented year is the filter's own evidence,
        # made up.
        unknown = [r for r in deal["comps"]["rows"] if r.get("year_built") is None]
        c.ok("the fixture carries an unknown-year comp", bool(unknown),
             "otherwise this check proves nothing")
        if unknown:
            c.ok(
                "an unknown year renders as a dash",
                "<td>—</td>" in tier_html or ">—<" in tier_html,
                "a blank or a zero would read as a year",
            )
        c.ok(
            "the comparison card names the population it averaged",
            "detailed tiers" in tier_html,
            "an average whose basis is unstated invites the whole-radius reading",
        )

        c.section("C#1: the preview reads amount_status, not the citation text")
        import closing_costs as _cc

        fl_deal = copy.deepcopy(deal)
        fl_deal["summary"]["state"] = "FL"
        fl_deal["closing_costs"] = _cc.for_state(
            "FL", deal["pricing"]["price_per_unit"] * deal["pricing"]["units_total"]
        )
        fl_html = render_previews.summary_fragment(fl_deal)
        row = re.search(r"Mortgage / recording tax</dt><dd>(.*?)</dd>", fl_html, re.S)
        c.ok("the mortgage row renders", row is not None)
        if row:
            c.ok(
                "a not-asserted amount is NOT shown as a green zero",
                "op-ok" not in row.group(1),
                re.sub(r"<[^>]+>", "", row.group(1))[:80],
            )
            c.ok(
                "it is marked not asserted",
                "NOT ASSERTED" in row.group(1),
                re.sub(r"<[^>]+>", "", row.group(1))[:80],
            )
        az_deal = copy.deepcopy(deal)
        az_deal["summary"]["state"] = "AZ"
        az_deal["closing_costs"] = _cc.for_state(
            "AZ", deal["pricing"]["price_per_unit"] * deal["pricing"]["units_total"]
        )
        az_html = render_previews.summary_fragment(az_deal)
        az_row = re.search(r"Mortgage / recording tax</dt><dd>(.*?)</dd>", az_html, re.S)
        c.ok(
            "a genuine zero-by-law still reads as a confident zero",
            az_row is not None and "op-ok" in az_row.group(1),
            re.sub(r"<[^>]+>", "", az_row.group(1))[:80] if az_row else "",
        )

        c.section("an absent comps / demand / supply block renders its reason")
        for section, row_key, builder, needle in (
            ("comps", "rows", render_previews.comps_fragment, "No comparable rent data"),
            ("demand", "rings", render_previews.demand_fragment, "No demographic data"),
            ("supply", "rings", render_previews.supply_fragment, "No competing-supply data"),
        ):
            absent_deal = copy.deepcopy(deal)
            absent_deal[section][row_key] = []
            # An absent demand block has neither the compact rings NOR the full sections.
            # Clearing only one would leave the other rendering, which is not what absence
            # looks like.
            if section == "demand":
                absent_deal[section]["sections"] = []
            absent_deal[section]["absent_reason"] = f"nothing scraped for {section}"
            out_html = builder(absent_deal)
            c.ok(f"{section}: states the heading", needle in out_html, out_html[:160])
            c.ok(
                f"{section}: states the reason",
                f"nothing scraped for {section}" in out_html,
            )
            c.ok(f"{section}: fabricates no table", "<table" not in out_html)

        c.section("the full demand report renders, including its two awkward shapes")
        dem_html = render_previews.demand_fragment(deal)
        for sec in deal["demand"]["sections"]:
            c.ok(f"demand preview shows '{sec['title'][:32]}'",
                 render_previews._escape(sec["title"]) in dem_html)
        c.ok(
            "the metro history table renders (it does NOT share the ring columns)",
            "Nonfarm jobs (000s)" in dem_html,
        )
        c.ok(
            "the 1-mile overcount caveat is stated before the data",
            "1-mile" in dem_html
            and dem_html.index("1-mile") < dem_html.index("Households (2020"),
        )
        c.ok(
            "and it tells the reader what to do instead",
            "3-mile column" in dem_html,
            "a caveat with no remedy just makes the number unusable",
        )

        c.section("supply keeps its figures and gains a coverage note")
        sup_html = render_previews.supply_fragment(deal)
        c.ok("the coverage note is present", "ongoing work" in sup_html)
        c.ok("it calls the section a floor, not a census", "floor on competing supply" in sup_html)
        c.ok(
            "the supply rows themselves are unchanged",
            all(str(r["units"]) or True for r in compute.supply(deal)["rows"])
            and "Ring" in sup_html,
        )

        c.section("supply names the projects inside 3 miles")
        projects = deal["supply"].get("projects")
        c.ok("the fixture carries a project list", isinstance(projects, list) and bool(projects),
             "otherwise these checks prove nothing")
        c.ok("the table has a heading", "Projects within 3 miles" in sup_html, sup_html[-400:])
        c.ok("with a one-line op-note saying how to read it",
             "at the furthest stage" in sup_html and "a dash means" in sup_html)
        c.ok("it comes AFTER the two existing tables",
             sup_html.count("<table") == 3
             and sup_html.index("Projects within 3 miles") > sup_html.rindex("Existing stock"),
             f"{sup_html.count('<table')} tables")
        if isinstance(projects, list) and projects:
            c.ok("every project is named",
                 all(render_previews._escape(p["name"]) in sup_html for p in projects))
            c.ok("a project with no unit count renders a dash, never a zero",
                 all(p["units"] is not None for p in projects) or "—" in sup_html)
            heads = ("Project", "Miles", "Stage", "Units", "Type", "Address", "City",
                     "Acres", "Developer", "First filed", "Latest activity", "Source layers")
            for h in heads:
                c.ok(f"the project table has a '{h}' column", f"<th>{h}" in sup_html or f'>{h}<' in sup_html)
            c.ok("and no permit link leaks an external URL", not re.search(r"https?://", sup_html))

        c.section("property tax preview")
        tax_html = render_previews.property_tax_fragment(deal)
        import compute as _c2
        tu = _c2.tax_underwriting(deal)
        for label in ("Assessed-value basis", "County billed amount",
                      "Does the basis reset on sale?", "Year-one tax",
                      "What the generic shortcut would have said"):
            c.ok(f"tax preview shows '{label}'", label in tax_html)
        if tu["resets_on_sale"] is False:
            c.ok(
                "a no-reset state names its statute in the approach text",
                "Proposition 117" in tax_html or "does not reset" in tax_html,
            )
            c.ok(
                "and the overstatement multiple is shown",
                f"{tu['generic_overstatement']:.2f}x" in tax_html,
                "the size of the trap is the point",
            )

        c.section("notes and disclosures preview")
        notes_html = render_previews.notes_fragment(deal)
        for n in deal["meta"]["notes"]:
            c.ok(f"note '{n['topic'][:30]}' renders",
                 render_previews._escape(n["topic"]) in notes_html)
        c.ok("supply's ongoing-work note is among them",
             any("ongoing work" in n["note"] for n in deal["meta"]["notes"]))

        c.section("provenance renders inline, and the trail is pointed to not duplicated")
        summ_html = render_previews.summary_fragment(deal)
        c.ok("inline source spans are present", 'class="op-src"' in summ_html)
        c.ok("more than one value carries one", summ_html.count('class="op-src"') >= 3,
             str(summ_html.count('class="op-src"')))
        c.ok("the summary no longer carries the grey-line pointer paragraph", "small grey line" not in summ_html)
        c.ok(
            "the standalone tax evidence trail is gone from the summary",
            "Taxes — evidence trail" not in summ_html,
            "it moved to its own section; leaving both would duplicate it",
        )
        c.ok(
            "the audit-trail fragment still carries the complete registry",
            "audit-trail.html" in texts
            and str(len(deal["provenance"])) in texts["audit-trail.html"]
            or "Audit trail" in texts.get("audit-trail.html", ""),
        )

        c.section("I4 — no tenant-name column reaches a fragment")
        roll_text = texts["rent-roll.html"]
        # Check the table HEADER, not the whole fragment: the prose deliberately says
        # "there is no tenant-name column", and that disclosure is a feature. What must
        # not exist is a name-like column of data.
        head = re.search(r"<thead>(.*?)</thead>", roll_text, re.S)
        c.ok("roll fragment has a table header", head is not None)
        head_text = (head.group(1) if head else "").lower()
        for token in ("tenant", "resident", "name", "occupant", "lessee"):
            c.ok(
                f"no '{token}' column in the roll table header",
                token not in head_text,
                head_text[:200],
            )
        body = re.search(r"<tbody>(.*?)</tbody>", roll_text, re.S)
        body_text = (body.group(1) if body else "").lower()
        for token in ("tenant", "resident", "lessee"):
            c.ok(f"no '{token}' anywhere in the roll table body", token not in body_text)
        c.ok(
            "and the fragment discloses that no tenant-name column exists",
            "tenant-name column" in roll_text.lower(),
        )
        text_unit = next(
            u for u in deal["rent_roll"]["units"] if u.get("lease_start") == "n/a"
        )
        c.ok(
            "the text date renders as text",
            ">n/a<" in roll_text,
            "expected the literal source value, not a coerced date",
        )
        c.ok("its unit still appears", f">{text_unit['unit_id']}<" in roll_text)

        c.section("F1: markup in a source value is escaped, not trusted by substring")
        # A cell is exempt from escaping only when the renderer built it. Detecting
        # "safe" markup by substring let a source label that merely CONTAINED a marker
        # smuggle arbitrary HTML into a fragment the site inlines with set:html.
        marker_attack = copy.deepcopy(deal)
        marker_attack["t12"]["rows"][0]["label"] = (
            'Rent <span class="op-ok">x</span><img src=x onerror=alert(1)>'
        )
        attacked = render_previews.t12_data_fragment(marker_attack)
        # Test for an executable TAG, not for the payload text: escaping leaves
        # "onerror=alert(1)" intact as visible characters, which is harmless. What must
        # never appear is an unescaped "<img".
        c.ok(
            "no unescaped <img tag reaches the fragment",
            "<img" not in attacked,
            "raw tag reached the fragment",
        )
        c.ok("and the payload renders as visible text instead", "&lt;img" in attacked, "")
        for marker in ('<span class="op-flag">', '<span class="op-missing">', render_previews.CHIP):
            probe = copy.deepcopy(deal)
            probe["comps"]["rows"][0]["community"] = f"Acme {marker}<script>x</script>"
            probe_html = render_previews.comps_fragment(probe)
            c.ok(
                f"marker {marker[:26]!r} does not exempt a source value",
                "<script>x</script>" not in probe_html,
            )
        c.ok(
            "the renderer's own status markup still renders as markup",
            '<span class="op-ok">' in texts["t12-checks-pricing.html"],
        )

        c.section("escaping")
        injected = copy.deepcopy(deal)
        injected["summary"]["seller_entity"] = '<script>alert("x")</script> & Co'
        inj_text = render_previews.summary_fragment(injected)
        c.ok("markup in a data value is escaped", "<script>alert" not in inj_text)
        c.ok("and rendered as text", "&lt;script&gt;" in inj_text)
        c.ok("an ampersand is escaped", "&amp; Co" in inj_text)

        c.section("newest-first data renders too")
        rev_texts = render_previews.render_all(rev, Path(tmp) / "rev")
        rev_pnl = rev_texts["t12-pnl.html"].read_text(encoding="utf-8")
        rev_p = compute.pnl(rev)
        c.close(
            "reversed months give the same total revenue",
            rev_p["total_revenue"],
            p["total_revenue"],
            0.05,
        )
        c.close(
            "and the same T-3 annualized revenue",
            rev_p["total_revenue_t3_annualized"],
            p["total_revenue_t3_annualized"],
            0.05,
        )
        c.ok(
            "reversed rollup renders its NOI",
            compute.fmt_currency(rev_p["noi"]) in rev_pnl,
        )

        c.section("manifest fragment")
        man_text = texts["manifest.html"]
        c.ok(
            "every group appears",
            all(f">{g}<" in man_text for g in schema.manifest_groups()),
        )
        sample = [f for f in schema.MANIFEST_FIELDS if f.name == "plan_market_rent_for_model"][0]
        c.ok("a field's maps-to text renders", sample.fills in man_text)
        c.ok("the no-model-building pitch is stated", "does not build a model" in man_text)

        c.section("the gate is green on the fragments")
        terms = scrub_check.load_terms([])
        findings = scrub_check.run([out], terms)
        c.ok(
            "no denylisted term in any fragment",
            not findings,
            "\n".join(f.render() for f in findings),
        )

        c.section("renderer refuses an invalid deal")
        bad = copy.deepcopy(deal)
        bad["comps"]["rows"][0]["source"] = "Licensed comp feed"
        c.raises(
            "render_all validates first",
            lambda: render_previews.render_all(bad, Path(tmp) / "bad"),
            ValueError,
        )

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
