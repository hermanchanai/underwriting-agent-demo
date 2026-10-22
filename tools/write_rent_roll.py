"""JSON -> `sample-rent-roll-analysis.xlsx`.

Two tabs: the normalized per-unit roll and the per-plan rollup. No trade-out tab: the demo
never ingests a lease trade-out report, so the section is absent by design.

Two deliberate choices worth knowing before reading the code.

**Unit IDs only.** There is no tenant-name column in the schema, so there is nothing for
a scrub step to miss.

**Window-dependent columns are values, not formulas.** Unit counts, occupancy and
in-place averages are emitted as COUNTIF/AVERAGEIF formulas, because Excel does those
robustly and a reader can audit them. The signed and new move-in rents, their counts,
and market rent for model are written as values, because they depend on a lookback window
measured from the roll's as-of date — and a formula that re-derives them would silently re-window the
day someone reopens the file next quarter. The `Basis` column states which rule fired for
each plan.

Usage:
    python tools/write_rent_roll.py --deal <deal.json> --out <path.xlsx>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference

import compute
import schema
from _xl import (
    BOLD,
    FMT_COUNT,
    FMT_CURRENCY,
    FMT_CURRENCY_CENTS,
    FMT_DATE,
    FMT_PCT,
    FMT_SF,
    SUBTLE,
    WRAP,
    SUBTOTAL_FILL,
    Block,
    save_workbook,
    write_audit_trail,
    freeze,
    header_row,
    label_value,
    section_bar,
    set_widths,
    sheet_header,
    write_cells,
)

SHEET_ROLL = "Rent Roll"
SHEET_MIX = "Unit Mix Summary"

ROLL_HEADERS = [
    "Unit ID", "Floor plan", "Beds", "Baths", "SF", "Status",
    "Lease start", "Move-in", "Lease type", "In-place rent", "Rent per SF",
]

# Column letters on the roll tab, referenced by the summary's formulas.
#: The Unit Mix Summary's column layout. The writer builds its headers from this and the
#: provenance stamps take their locators from it, so a stamp cannot point at a column the
#: writer never wrote — which is exactly how in-place rent came to be stamped at the market
#: rent column, and market rent at a column that did not exist.
MIX_COLUMNS: dict[str, str] = {
    "bedroom": "A",
    "floor_plan": "B",
    "avg_sqft": "C",
    "units_total": "D",
    "in_place_rent": "E",
    # the internal rent-roll tool's Summary layout (rentroll_rules SUMMARY_HEADERS).
    "signed_rent": "F",
    "signed_count": "G",
    "movein_rent": "H",
    "movein_count": "I",
    "market_rent_for_model": "J",
    "basis": "K",
}
MIX_HEADERS: dict[str, str] = {
    "bedroom": "Bedroom",
    "floor_plan": "Floor plan",
    "avg_sqft": "Avg SF",
    "units_total": "Unit count",
    "in_place_rent": "In-place rent",
    "signed_rent": "LEASES SIGNED rent",
    "signed_count": "Signed #",
    "movein_rent": "NEW MOVE-IN rent",
    "movein_count": "Move-In #",
    "market_rent_for_model": "Market rent for model",
    "basis": "Basis",
}


def _na(value, count=0):
    """A windowed cell as the rent-roll Summary writes it: the TEXT "n/a" where the
    roll cannot support the column (count None) — never a numeric 0, which would read as a
    measurement — and blank where the window simply held no lease."""
    if count is None:
        return "n/a"
    return "" if value is None else value


def _windowed_total(rent_col: str, count_col: str, first: int, last: int, na: bool) -> list:
    """TOTAL cells for a windowed rent + count pair: the property-level
    average weighted by each plan's COUNT (every lease in the window counts once), and the
    summed count — or "n/a" on both when the roll cannot support the pair, never a SUM that
    turns n/a into a measured 0."""
    if na:
        return ["n/a", "n/a"]
    rents = f"${rent_col}${first}:${rent_col}${last}"
    counts = f"${count_col}${first}:${count_col}${last}"
    return [f'=IFERROR(SUMPRODUCT({rents},{counts})/SUM({counts}),"")',
            f"=SUM({count_col}{first}:{count_col}{last})"]


def mix_locator(key: str) -> str:
    """`Unit Mix Summary!<col>` for a metric, derived rather than retyped."""
    return f"{SHEET_MIX}!{MIX_COLUMNS[key]}"


C_PLAN = "B"
C_SF = "E"
C_STATUS = "F"
C_RENT = "J"


def _date_or_text(value):
    """A parseable date becomes a real date; anything else stays exactly as it came.

    A rent roll that carries "n/a" in a date column means the source could not say. That
    is information. Coercing it to 1899-12-31 destroys it.
    """
    parsed = compute.parse_date(value)
    return parsed if parsed is not None else value


def _write_roll_tab(wb: Workbook, deal: dict) -> dict:
    ws = wb.active
    ws.title = SHEET_ROLL
    rr = deal["rent_roll"]
    tolerance = rr["new_tenant_tolerance_days"]

    row = sheet_header(
        ws,
        "Rent roll",
        deal["meta"]["deal_name"],
        rr["as_of"],
        note=(
            "Unit identifiers only — no tenant names anywhere in this output. Lease type "
            f"is derived from the gap between lease start and move-in ({tolerance}-day "
            "tolerance); a blank means the source could not support a classification."
        ),
    )
    row = header_row(ws, row, ROLL_HEADERS)
    first = row
    formats = [
        None, None, "0.0", "0.0", FMT_SF, None,
        FMT_DATE, FMT_DATE, None, FMT_CURRENCY, FMT_CURRENCY_CENTS,
    ]
    for unit in rr["units"]:
        lease_type = compute.classify_lease(unit, tolerance)
        write_cells(
            ws,
            row,
            [
                unit["unit_id"], unit["floor_plan"], unit["beds"], unit["baths"],
                unit["sqft"], unit["status"],
                _date_or_text(unit.get("lease_start")),
                _date_or_text(unit.get("move_in")),
                lease_type or "",
                unit.get("in_place_rent"),
                f'=IFERROR({C_RENT}{row}/{C_SF}{row},"")',
            ],
            formats=formats,
        )
        row += 1
    last = row - 1

    set_widths(
        ws,
        {"A": 12, "B": 14, "C": 8, "D": 8, "E": 9, "F": 11,
         "G": 12, "H": 12, "I": 12, "J": 13, "K": 12},
    )
    freeze(ws, "A6")
    return {"first": first, "last": last}


def _write_mix_tab(wb: Workbook, deal: dict, ref: dict) -> None:
    ws = wb.create_sheet(SHEET_MIX)
    rr = deal["rent_roll"]
    plans = compute.plan_summary(deal)
    totals = compute.roll_totals(deal)
    moves = compute.move_in_counts(deal)

    row = sheet_header(
        ws,
        "Unit mix summary",
        deal["meta"]["deal_name"],
        rr["as_of"],
        note=schema.TOOLKIT_NAME,
    )

    # ---- knobs, on one row, where a reader meets them before the numbers -------------
    knobs = Block()
    ws.cell(row=row, column=1, value="Months lookback").font = BOLD
    ws.cell(row=row, column=2, value=rr["lookback_months"]).number_format = FMT_COUNT
    ws.cell(row=row, column=4, value="As-of date").font = BOLD
    c = ws.cell(row=row, column=5, value=compute.parse_date(rr["as_of"]) or rr["as_of"])
    c.number_format = FMT_DATE
    ws.cell(row=row, column=7, value="New-tenant tol (days)").font = BOLD
    ws.cell(row=row, column=8, value=rr["new_tenant_tolerance_days"]).number_format = FMT_COUNT
    knobs.mark("Months lookback", row)
    knobs.mark("As-of date", row)
    row += 1
    ws.cell(
        row=row,
        column=1,
        value=(
            "Every window below is measured from the as-of date, never from today — a "
            "summary that silently re-windows when reopened next quarter is worse than none."
        ),
    ).font = SUBTLE
    row += 2

    # ---- per-plan table, bedroom then plan code --------------------------------------
    headers = [MIX_HEADERS[k] for k in MIX_COLUMNS]
    row = header_row(ws, row, headers)
    plan_first = row
    roll = f"'{SHEET_ROLL}'"
    span_plan = f"{roll}!${C_PLAN}${ref['first']}:${C_PLAN}${ref['last']}"
    span_sf = f"{roll}!${C_SF}${ref['first']}:${C_SF}${ref['last']}"
    span_status = f"{roll}!${C_STATUS}${ref['first']}:${C_STATUS}${ref['last']}"
    span_rent = f"{roll}!${C_RENT}${ref['first']}:${C_RENT}${ref['last']}"

    for plan in plans:
        write_cells(
            ws,
            row,
            [
                plan["beds"],
                plan["floor_plan"],
                f'=IFERROR(AVERAGEIF({span_plan},$B{row},{span_sf}),"")',
                f"=COUNTIF({span_plan},$B{row})",
                f'=IFERROR(AVERAGEIFS({span_rent},{span_plan},$B{row},'
                f'{span_status},"Occupied"),"")',
                _na(plan["signed_rent"], plan["signed_count"]),
                _na(plan["signed_count"], plan["signed_count"]),
                _na(plan["movein_rent"], plan["movein_count"]),
                _na(plan["movein_count"], plan["movein_count"]),
                plan["market_rent_for_model"],
                plan["basis"],
            ],
            formats=[
                "0.0", None, FMT_SF, FMT_COUNT, FMT_CURRENCY,
                FMT_CURRENCY, FMT_COUNT, FMT_CURRENCY, FMT_COUNT, FMT_CURRENCY, None,
            ],
        )
        row += 1
    plan_last = row - 1
    row += 1  # the TOTAL row sits two below the last plan

    write_cells(
        ws,
        row,
        [
            "", "TOTAL",
            f'=IFERROR(SUMPRODUCT($D${plan_first}:$D${plan_last},'
            f'$C${plan_first}:$C${plan_last})/SUM($D${plan_first}:$D${plan_last}),"")',
            f"=SUM(D{plan_first}:D{plan_last})",
            # Straight off the roll. Weighting each plan's OCCUPIED-unit average by its
            # TOTAL unit count is a different number — it over-weights plans with
            # vacancies — and it disagreed with the preview by $1.43 on the fixture.
            f'=IFERROR(AVERAGEIFS({span_rent},{span_status},"Occupied"),"")',
            *_windowed_total("F", "G", plan_first, plan_last,
                             any(p_["signed_count"] is None for p_ in plans)),
            *_windowed_total("H", "I", plan_first, plan_last,
                             any(p_["movein_count"] is None for p_ in plans)),
            f'=IFERROR(SUMPRODUCT($D${plan_first}:$D${plan_last},'
            f'$J${plan_first}:$J${plan_last})/SUM($D${plan_first}:$D${plan_last}),"")',
            "Weighted by unit count.",
        ],
        formats=[None, None, FMT_SF, FMT_COUNT, FMT_CURRENCY, FMT_CURRENCY, FMT_COUNT,
                 FMT_CURRENCY, FMT_COUNT, FMT_CURRENCY, None],
        bold=True,
        fill=SUBTOTAL_FILL,
    )
    total_row = row
    row += 2

    # ---- charts: the mix, and the three rents side by side ---------------------------
    mix_chart = BarChart()
    mix_chart.type = "col"
    mix_chart.title = "Units by floor plan"
    mix_chart.y_axis.title = "Units"
    mix_chart.height, mix_chart.width = 7.5, 16
    mix_chart.add_data(
        Reference(ws, min_col=4, min_row=plan_first - 1, max_row=plan_last), titles_from_data=True
    )
    mix_chart.set_categories(Reference(ws, min_col=2, min_row=plan_first, max_row=plan_last))
    mix_chart.legend = None
    ws.add_chart(mix_chart, f"M{plan_first}")

    rent_chart = BarChart()
    rent_chart.type = "col"
    rent_chart.grouping = "clustered"
    rent_chart.title = "In-place vs new move-in vs market rent for model"
    rent_chart.y_axis.title = "Rent"
    rent_chart.height, rent_chart.width = 7.5, 16
    rent_chart.add_data(
        Reference(ws, min_col=5, max_col=5, min_row=plan_first - 1, max_row=plan_last),
        titles_from_data=True,
    )
    rent_chart.add_data(
        Reference(ws, min_col=8, max_col=8, min_row=plan_first - 1, max_row=plan_last),
        titles_from_data=True,
    )
    rent_chart.add_data(
        Reference(ws, min_col=10, max_col=10, min_row=plan_first - 1, max_row=plan_last),
        titles_from_data=True,
    )
    rent_chart.set_categories(Reference(ws, min_col=2, min_row=plan_first, max_row=plan_last))
    ws.add_chart(rent_chart, f"M{plan_first + 16}")

    # ---- occupancy and vacancy --------------------------------------------------------
    row = section_bar(ws, row, "Occupancy & vacancy", width=3)
    occ = Block()
    row = label_value(ws, row, "Total units", f"=D{total_row}", fmt=FMT_COUNT, block=occ)
    row = label_value(
        ws, row, "Occupied units", totals["occupied_units"], fmt=FMT_COUNT, block=occ
    )
    row = label_value(
        ws, row, "Vacant units", totals["vacant_units"], fmt=FMT_COUNT, block=occ
    )
    row = label_value(
        ws, row, "Non-revenue units", totals["non_revenue_units"], fmt=FMT_COUNT,
        note="Models and down units: in the stock, in neither occupancy bucket.", block=occ,
    )
    tot = occ.row("Total units")
    occupied = occ.row("Occupied units")
    row = label_value(
        ws, row, "Occupancy", f'=IFERROR(B{occupied}/B{tot},"")', fmt=FMT_PCT, block=occ
    )
    row = label_value(
        ws, row, "Vacancy", f'=IFERROR(1-B{occupied}/B{tot},"")', fmt=FMT_PCT,
        note="The physical-vacancy input a model consumes.", block=occ,
    )
    row += 1

    # ---- turnover and move-ins ---------------------------------------------------------
    row = section_bar(ws, row, "Annualised turnover rate", width=3)
    row = label_value(
        ws, row, "Annualised turnover", totals["annualized_turnover"], fmt=FMT_PCT,
        note=f"Move-ins in the trailing {rr['lookback_months']} months, annualised "
             f"against unit count.",
        block=occ,
    )
    row += 1
    row = section_bar(ws, row, "New move-ins", width=3)
    row = label_value(
        ws, row, "Move-ins in window", moves["move_ins"], fmt=FMT_COUNT,
        note="Counted by MOVE-IN date, never by lease start — a renewal stamps a fresh "
             "lease start against a resident who moved in years ago.",
        block=occ,
    )
    row = label_value(
        ws, row, "Window (months)", moves["window_months"], fmt=FMT_COUNT, block=occ
    )
    row = label_value(
        ws, row, "Share of units", moves["share"], fmt=FMT_PCT, block=occ
    )
    if moves["unreadable_dates"]:
        row = label_value(
            ws, row, "Unreadable move-in dates", moves["unreadable_dates"], fmt=FMT_COUNT,
            note="These make the count UNDER-read; they are surfaced rather than absorbed.",
            block=occ,
        )
    row += 1

    # ---- notes --------------------------------------------------------------------------
    row = section_bar(ws, row, "Notes", width=3)
    ws.cell(
        row=row,
        column=1,
        value=(
            "Market rent for model is the NEW MOVE-IN rent — residents whose move-in date "
            "falls inside the lookback window, whatever their lease type — where the plan "
            "has one, and the in-place average otherwise; the Basis column says which, per "
            "plan; a roll with no usable lease start or move-in date reads in-place "
            "outright. LEASES SIGNED is every classified lease started in the window, new and renewal "
            "together, and is shown for context only: a blend of new and renewal rents is "
            "a renewal average wearing a market-rent label, and on a stabilised property "
            "renewals outnumber new leases roughly three to two over twelve months."
        ),
    ).alignment = WRAP
    ws.row_dimensions[row].height = 46
    row += 1
    ws.cell(
        row=row,
        column=1,
        value=(
            "The signed and move-in rents, their counts and market rent for model are values rather than "
            "formulas on purpose: they depend on the window above, and a formula would "
            "re-window itself the day this file is reopened. Unit count, average SF and "
            "in-place rent are live formulas over the Rent Roll tab."
        ),
    ).alignment = WRAP
    ws.row_dimensions[row].height = 32

    set_widths(
        ws,
        {"A": 10, "B": 16, "C": 10, "D": 11, "E": 14, "F": 15, "G": 9, "H": 15, "I": 10,
         "J": 21, "K": 34},
    )
    freeze(ws, "A5")


def build(deal: dict, out_path: Path) -> Path:
    errors = schema.validate(deal)
    if errors:
        raise ValueError(
            "deal failed schema validation:\n  - " + "\n  - ".join(errors)
        )
    wb = Workbook()
    ref = _write_roll_tab(wb, deal)
    _write_mix_tab(wb, deal, ref)
    # No trade-out tab (2026-09-10): the lease trade-out report is an internal-pipeline input
    # the demo does not ask for, on the sample and on an upload alike. The section stays in
    # the schema as absent-by-design and the manifest labels its fields "internal only".
    write_audit_trail(wb, deal, "rent_roll", title="The rent roll and unit mix")
    return save_workbook(wb, out_path, deal["meta"].get("generated_on"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deal", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    deal = json.loads(args.deal.read_text(encoding="utf-8"))
    path = build(deal, args.out)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
