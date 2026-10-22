"""JSON -> `sample-deal-summary.xlsx`.

Four tabs: the summary card, then the market context the pipeline attaches to it —
rent comps from the operator's own weekly scrape, demand by ring, supply by ring and
stage. Pure openpyxl; formulas are written as strings for Excel to compute on open.

Usage:
    python tools/write_deal_summary.py --deal <deal.json> --out <path.xlsx>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import Workbook

import closing_costs as _closing
import compute
import provenance as pv
import schema
from _xl import (
    BOLD,
    FMT_COUNT,
    FMT_CURRENCY,
    FMT_DATE,
    FMT_NUM1,
    FMT_PCT,
    FMT_PCT2,
    FMT_SF,
    ILLUSTRATIVE_MARK,
    SUBTLE,
    SUBTOTAL_FILL,
    WRAP,
    Block,
    autosize,
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

SHEET_SUMMARY = "Deal Summary"
SHEET_COMPS = "Rent Comps"
SHEET_DEMAND = "Demand"
SHEET_SUPPLY = "Supply"
SHEET_TAX = "Property Tax"

#: Shown on the supply tab, in the supply preview, and again in Notes & Disclosures. The
#: figures are real and unmodified; what is still being extended is how much of the permit
#: pipeline the sources cover.
SUPPLY_COVERAGE_NOTE = (
    "Coverage note: these rings and stages are real and unmodified, and a zero is a "
    "genuine zero for the sources covered so far — not a gap left blank. Coverage of the "
    "permit and entitlement pipeline is an area of ongoing work and will be expanded, so "
    "read this section as a FLOOR on competing supply rather than a census of it."
)
SHEET_NOTES = "Notes & Disclosures"

OM_BILL_LABEL = "Tax bill as stated in the offering memorandum"
OM_ROW_NOTE = "As stated in the offering memorandum, not a county record."


def _from_om(deal: dict, path: str) -> bool:
    """Whether the value at ``path`` was read off an uploaded offering memorandum.

    The seller's statement is labelled as such on every tab that shows it; a county
    record keeps its own label. Decided from the stamp's source document against the
    documents the run read as an OM, never from the value.
    """
    om_docs = {d.get("document") for d in (deal["meta"].get("provenance") or [])
               if d.get("kind") == "om"}
    st = pv.lookup(deal, path)
    return bool(st) and st.get("source_doc") in om_docs

#: The section whose block carries the derived pricing rows. Compared by identity against
#: schema.summary_sections() rather than by a prefix of its display title, so renaming the
#: heading cannot silently drop the pricing block.
PRICING_SECTION = "Pricing (illustrative)"  # the public sample's heading; see pricing_section()


def pricing_section(deal: dict) -> str:
    """The pricing block's heading: marked illustrative only when the run's pricing is."""
    return PRICING_SECTION if deal["meta"].get("illustrative_pricing", False) else "Pricing"

# Number format per summary key, where the value is numeric.
_SUMMARY_FORMATS: dict[str, str] = {
    "units_total": FMT_COUNT,
    "year_built": "0",
    "in_place_rent_avg": FMT_CURRENCY,
    "market_rent_for_model": FMT_CURRENCY,
    "fair_market_value": FMT_CURRENCY,
    "tax_rate_as_billed": FMT_NUM1,
    "assessed_value_basis": FMT_CURRENCY,
    "assessment_ratio": FMT_PCT,
    "tax_divisor": FMT_COUNT,
    "tax_exemption": FMT_CURRENCY,
    "school_elementary_rating": "0",
    "school_middle_rating": "0",
    "school_high_rating": "0",
}

_SUMMARY_NOTES: dict[str, str] = {
    "address_provenance": "How the address was resolved, and from which document.",
    "tax_rate_as_billed": "Rate as actually billed — not a statutory or effective rate.",
    "market_rent_for_model": (
        "New move-in rent (residents who moved in inside the lookback window, by move-in "
        "date) where the plan has one, otherwise in-place (in-place outright when the roll has "
        "no usable lease start or move-in date). Never a blend of new and renewal."
    ),
    "units_total": "Divisor for every per-unit figure in the output set.",
}


def _write_summary_tab(wb: Workbook, deal: dict) -> None:
    ws = wb.active
    ws.title = SHEET_SUMMARY
    s = deal["summary"]
    derived = compute.summary_derived(deal)
    illustrative = deal["meta"].get("illustrative_pricing", False)
    mark = f" {ILLUSTRATIVE_MARK}" if illustrative else ""

    row = sheet_header(
        ws,
        "Deal summary",
        deal["meta"]["deal_name"],
        deal["meta"]["as_of_date"],
        note=schema.TOOLKIT_NAME,
    )
    block = Block()

    for section, keys in schema.summary_sections():
        row = section_bar(ws, row, pricing_section(deal) if section == PRICING_SECTION else section)
        for key in keys:
            value = s.get(key, schema.MISSING)
            note = _SUMMARY_NOTES.get(key)
            # A value the public demo leaves to the internal pipeline reads as exactly that,
            # with the reason — the same marker the preview shows (schema.INTERNAL_ONLY).
            internal = schema.internal_only(f"summary.{key}") if value in (None, "", schema.MISSING) else None
            if internal:
                value, note = schema.INTERNAL_ONLY_SOURCE, internal[1]
            row = label_value(
                ws,
                row,
                schema.summary_label(key),
                value,
                fmt=_SUMMARY_FORMATS.get(key) if not internal else None,
                note=note,
                source=pv.compact_for(deal, f"summary.{key}"),
                block=block,
            )

        if section == PRICING_SECTION:
            units_row = block.row("Unit count")
            row = label_value(
                ws, row, f"Price per unit{mark}", deal["pricing"]["price_per_unit"],
                fmt=FMT_CURRENCY, note=deal["meta"].get("pricing_note"), block=block,
            )
            ppu_row = block.row(f"Price per unit{mark}")
            # Both operands are CELLS and either can hold the MISSING sentinel, which is
            # text — so the product came out #VALUE! rather than MISSING. `derived`
            # already knows whether there is a total to state (round 19, ruling 2).
            total = derived.get("total_price")
            row = label_value(
                ws, row, f"Total price{mark}",
                f"=B{ppu_row}*B{units_row}" if isinstance(total, (int, float)) else total,
                fmt=FMT_CURRENCY, note="Price per unit times unit count.", block=block,
            )
            price_row = block.row(f"Total price{mark}")
            # Every cell BELOW here reads the total-price cell, which may hold the
            # sentinel — text. `=B32` returns the text, and `IFERROR(B34/B32,"")` turns
            # the resulting #VALUE! into a BLANK, so the two cap rates came out empty
            # while the contract says an absent figure reads MISSING.
            # `derived` has already resolved each of them, so the sentinel is written in.
            row = label_value(
                ws, row, f"Approximate project value{mark}",
                f"=B{price_row}" if isinstance(total, (int, float)) else total,
                fmt=FMT_CURRENCY, block=block,
            )
            row = label_value(
                ws, row, "T-12 net operating income", derived["t12_noi"],
                fmt=FMT_CURRENCY,
                note="From the T-12 analysis workbook's P&L rollup.", block=block,
            )
            noi_row = block.row("T-12 net operating income")
            t12_cap = derived.get("t12_cap_rate")
            row = label_value(
                ws, row, f"T-12 cap rate{mark}",
                f'=IFERROR(B{noi_row}/B{price_row},"")'
                if isinstance(t12_cap, (int, float)) else t12_cap,
                fmt=FMT_PCT2, block=block,
            )
            t3_noi = compute.pricing(deal)["t3_noi_annualized"]
            row = label_value(
                ws, row, "T-3 net operating income (annualized)", t3_noi,
                fmt=FMT_CURRENCY,
                note="Trailing three months times four.", block=block,
            )
            t3_row = block.row("T-3 net operating income (annualized)")
            t3_cap = compute.pricing(deal)["t3_cap_rate"]
            row = label_value(
                ws, row, f"T-3 cap rate{mark}",
                f'=IFERROR(B{t3_row}/B{price_row},"")'
                if isinstance(t3_cap, (int, float)) else t3_cap,
                fmt=FMT_PCT2, block=block,
            )
            row += 1

        if section == "Taxes":
            # The evidence rows, in the same order the preview shows them.
            row = label_value(
                ws, row, "Parcel / APN", s.get("tax_parcel_id"),
                note=(OM_ROW_NOTE if _from_om(deal, "summary.tax_parcel_id") else "The parcel the bill is issued against."), block=block,
            )
            row = label_value(
                ws, row, "Tax area code", s.get("tax_area_code"),
                note=OM_ROW_NOTE if _from_om(deal, "summary.tax_area_code")
                else "Determines which published rate row applies.", block=block,
            )
            row = label_value(
                ws, row, "Basis label", s.get("tax_basis_label"),
                note=s.get("tax_basis_note"), block=block,
            )
            basis = block.row(schema.summary_label("assessed_value_basis"))
            ratio = block.row(schema.summary_label("assessment_ratio"))
            divisor = block.row(schema.summary_label("tax_divisor"))
            rate = block.row(schema.summary_label("tax_rate_as_billed"))
            exemption = block.row(schema.summary_label("tax_exemption"))
            row = label_value(
                ws,
                row,
                "Computed annual tax",
                f'=IFERROR(B{basis}*B{ratio}/B{divisor}*B{rate}-B{exemption},"{schema.MISSING}")',
                fmt=FMT_CURRENCY,
                note=s.get("tax_rate_evidence"),
                block=block,
            )
            tax_row = block.row("Computed annual tax")
            units_row = block.row("Unit count")
            row = label_value(
                ws,
                row,
                "Computed tax per unit",
                f'=IFERROR(B{tax_row}/B{units_row},"{schema.MISSING}")',
                fmt=FMT_CURRENCY,
                block=block,
            )
            bill_from_om = _from_om(deal, "summary.tax_billed_amount")
            bill_label = (f"{OM_BILL_LABEL} ({s.get('tax_billed_year')})" if bill_from_om
                          else f"Billed by the county ({s.get('tax_billed_year')})")
            row = label_value(
                ws, row, bill_label,
                s.get("tax_billed_amount"), fmt=FMT_CURRENCY,
                note=("The seller's figure, not verified against the county; for the "
                      "cross-check below." if bill_from_om
                      else "The county's own figure, for the cross-check below."), block=block,
            )
            billed_row = block.row(bill_label)
            _chk = compute.tax_check(deal)
            row = label_value(
                ws, row, "Check: computed vs billed",
                # ISNUMBER on BOTH cells: a MISSING tax input or a missing bill leaves
                # text in one of them, and ABS("MISSING"-number) is #VALUE!, not an
                # answer. Same decision compute.tax_check makes (I6). When the check is
                # withheld for a REASON the formula cannot see (the bill's year is not the
                # rate's), the status is written as text so the two surfaces agree.
                "NOT CHECKED" if _chk.get("reason") else
                f'=IF(OR(NOT(ISNUMBER(B{tax_row})),NOT(ISNUMBER(B{billed_row})),'
                f'B{billed_row}=0),"NOT CHECKED",'
                f'IF(ABS(B{tax_row}-B{billed_row})/B{billed_row}<=0.02,"ties","REVIEW"))',
                note=(
                    f"Not compared: {_chk['reason']}." if _chk.get("reason") else
                    "A computed tax that cannot be tied to the actual bill is an estimate "
                    "wearing the clothes of a fact."
                ),
                block=block,
            )
            row = label_value(
                ws, row, "Check: difference",
                "NOT CHECKED" if _chk.get("reason") else f'=IFERROR(B{tax_row}-B{billed_row},"")',
                fmt=FMT_CURRENCY, block=block,
            )
            row += 1

            # ---- closing costs ---------------------------------------------------------
            cc = deal.get("closing_costs") or {}
            row = section_bar(ws, row, "Closing costs at the illustrative price")
            for key, label in (
                ("transfer_tax", "Transfer tax"),
                ("mortgage_tax", "Mortgage / recording tax"),
            ):
                line = cc.get(key) or {}
                # Read the status FIELD. Sniffing the citation for "Not asserted" is what
                # turned an uncomputable tax into a confident $0 the moment the statute
                # was prepended to that same string.
                status = line.get("amount_status")
                if status == _closing.NOT_ASSERTED:
                    shown = "NOT ASSERTED"
                else:
                    shown = line.get("amount")
                row = label_value(
                    ws, row, label, shown,
                    fmt=FMT_CURRENCY if not isinstance(shown, str) else None,
                    note=" · ".join(
                        x for x in (line.get("rate_label"), line.get("citation"),
                                    line.get("caveat")) if x
                    ),
                    block=block,
                )
            row = label_value(
                ws, row, "Closing-cost basis", cc.get("price"), fmt=FMT_CURRENCY,
                note=cc.get("note"), block=block,
            )
            row += 1
        elif section != PRICING_SECTION:
            row += 1

    row = section_bar(ws, row, "Boundaries")
    for line in deal["meta"].get("excluded_sources", []):
        ws.cell(row=row, column=1, value="Excluded by design").font = BOLD
        ws.cell(row=row, column=2, value=line)
        row += 1
    if deal["meta"].get("synthetic"):
        ws.cell(row=row, column=1, value="Synthetic deal").font = BOLD
        ws.cell(row=row, column=2, value=schema.synthetic_note(deal))
        row += 1

    set_widths(ws, {"A": 34, "B": 46, "C": 62})
    freeze(ws, "A5")


def _absence(ws, row: int, title: str, reason: str | None, width: int = 100) -> None:
    """State why a block is empty. An empty block that says why is a deliverable."""
    ws.cell(row=row, column=1, value=title).font = BOLD
    ws.cell(row=row + 1, column=1, value=reason or "No reason recorded.")
    ws.cell(
        row=row + 3,
        column=1,
        value=(
            "An empty block that says why it is empty is a deliverable. A block of "
            "plausible numbers is not."
        ),
    ).font = SUBTLE
    set_widths(ws, {"A": width})


def _write_comps_tab(wb: Workbook, deal: dict) -> None:
    ws = wb.create_sheet(SHEET_COMPS)
    c = compute.comps(deal)
    row = sheet_header(
        ws,
        "Rent comps",
        deal["meta"]["deal_name"],
        c["as_of"],
        note=(
            "Source: the operator's own weekly rent scrape. Asking and net effective are "
            "stated separately on every row — a concession-blind comp set is the most "
            "common way a rent assumption goes wrong. No licensed comp feed is used."
        ),
    )
    if not c["rows"]:
        _absence(ws, row, "No comparable rent data", deal["comps"].get("absent_reason"))
        _write_om_comps(ws, row + 3, deal)
        return

    # ---- the three tiers -------------------------------------------------------------
    # Tiers 1 and 2 carry full detail; tier 3 is identity only. Splitting them is the
    # whole point of the display: an undifferentiated list of everything nearby invites
    # the reader to average across product types and vintages, which is the mistake the
    # rest of this output refuses to make.
    headers = [
        "Community", "Distance (mi)", "Year built", "Floor plan", "Beds", "Baths", "SF",
        "Asking rent", "Concession", "Net effective rent", "Asking $/SF",
        "Occupancy", "As-of date", "Source",
    ]
    formats = [
        None, FMT_NUM1, "0", None, "0.0", "0.0", FMT_SF,
        FMT_CURRENCY, FMT_CURRENCY, FMT_CURRENCY, "#,##0.00",
        FMT_PCT, FMT_DATE, None,
    ]
    tiering = c.get("tiering") or {}
    spans: list[tuple[int, int]] = []
    for tier in (1, 2):
        group = (c.get("tiers") or {}).get(tier)
        if not group or not group["rows"]:
            continue
        n = len(group["communities"])
        caption = (
            f"Tier {tier} — {group['label']}: "
            f"the {'closest' if tier == 1 else 'next'} {n} "
            f"{'community' if n == 1 else 'communities'} matching the subject's product "
            f"and vintage"
        )
        row = section_bar(ws, row, caption, width=6)
        if tier == 1 and tiering.get("vintage_footnote"):
            write_cells(ws, row, [tiering["vintage_footnote"]])
            row += 1
        row = header_row(ws, row, headers)
        span_first = row
        for comp in group["rows"]:
            as_of = compute.parse_date(comp["as_of"]) or comp["as_of"]
            write_cells(
                ws,
                row,
                [
                    comp["community"], comp["distance_mi"], comp.get("year_built"),
                    comp["floor_plan"], comp["beds"], comp["baths"], comp["sqft"],
                    comp["asking_rent"], comp["concession"], comp["net_effective_rent"],
                    f'=IFERROR(H{row}/G{row},"")',
                    comp["occupancy"], as_of, comp["source"],
                ],
                formats=formats,
            )
            row += 1
        spans.append((span_first, row - 1))
        row += 1

    # Multiple ranges rather than one span across the tier break: an AVERAGE over the
    # whole region would silently depend on the section bars being text.
    def _rng(col: str) -> str:
        return ",".join(f"{col}{a}:{col}{b}" for a, b in spans)

    # ---- unit count and year built, each with its source -----------
    # The same lines as the preview's table (compute.community_facts, I6): a public-record
    # count names its record and as-of; a community without one reads MISSING with the
    # held-back label; the licensed registry count is never written on a public set.
    facts = compute.community_facts(deal)
    if facts:
        row = section_bar(ws, row, "Unit count and year built, by community — each with its source", width=6)
        row = header_row(ws, row, ["Community", "Tier", "Units", "Units from", "Year built", "Year built from"])
        for r in facts:
            write_cells(
                ws, row,
                [r["community"], r["tier"], r["units"] if r["units"] is not None else "—",
                 r["units_source"], r["year_built"] if r["year_built"] is not None else "—",
                 r["year_built_source"]],
                formats=[None, "0", FMT_COUNT, None, "0", None],
            )
            row += 1
        row += 1

    # The identity-only third tier came off the workbook and the page.
    # It was ~340 rows on the published sample. The data is unchanged — every tracked
    # community in the radius, with its reason, still ships in the interchange file.

    # ---- by bed count, then the mix-matched comparison -------------------------------
    if c.get("by_bed"):
        row = section_bar(ws, row, "By bed count — like for like", width=5)
        row = header_row(
            ws, row, ["Beds", "Communities", "Plans", "Avg asking", "Avg net effective"]
        )
        for e in c["by_bed"]:
            write_cells(
                ws,
                row,
                [
                    "Studio" if e["beds"] == 0 else f"{e['beds']} bed",
                    e["communities"], e["rows"],
                    e["avg_asking"], e["avg_net_effective"],
                ],
                formats=[None, FMT_COUNT, FMT_COUNT, FMT_CURRENCY, FMT_CURRENCY],
            )
            row += 1
        row += 1

    row = section_bar(ws, row, "Subject versus comp set — detailed tiers only", width=4)
    block = Block()
    row = label_value(
        ws, row, "Subject market rent", c["subject_market_rent"],
        fmt=FMT_CURRENCY,
        note="Unit-weighted market rent for model, whole property.",
        block=block,
    )
    row = label_value(
        ws, row, "Comp-set average asking", f'=IFERROR(AVERAGE({_rng("H")}),"")',
        fmt=FMT_CURRENCY,
        note=f"Over {c.get('basis_label')} — never the whole radius.",
        block=block,
    )
    row = label_value(
        ws, row, "Comp-set average net effective",
        f'=IFERROR(AVERAGE({_rng("J")}),"")', fmt=FMT_CURRENCY, block=block,
    )
    row = label_value(
        ws, row, "Comp set, mix-matched",
        c.get("compset_mix_matched_net_effective"), fmt=FMT_CURRENCY,
        note=(
            "Each bed count's comp net-effective weighted by the subject's own unit mix, "
            "so both sides describe the same product."
        ),
        block=block,
    )
    row = label_value(
        ws, row, "Subject rent, same bed counts",
        c.get("subject_market_rent_matched"), fmt=FMT_CURRENCY,
        note=f"Restricted to the bed counts the comp set covers: {c.get('mix_matched_beds')}.",
        block=block,
    )
    row = label_value(
        ws, row, "Mix coverage", c.get("mix_coverage"), fmt=FMT_PCT,
        note="Share of the subject's units actually compared, by bed count and priced plan.",
        block=block,
    )
    # ONE decision, made in compute and rendered here. The workbook used to divide a flat
    # average and publish a spread the preview was withholding — the same figure reading
    # two different ways depending on which surface you opened.
    if c.get("subject_vs_compset_spread") is None:
        row = label_value(
            ws, row, "Subject vs comp set (mix-matched)", "WITHHELD",
            note=c.get("spread_withheld_reason") or "Coverage too thin to publish a spread.",
            block=block,
        )
    else:
        matched_subj = block.row("Subject rent, same bed counts")
        matched_comp = block.row("Comp set, mix-matched")
        row = label_value(
            ws, row, "Subject vs comp set (mix-matched)",
            f'=IFERROR(B{matched_subj}/B{matched_comp}-1,"")', fmt=FMT_PCT,
            note=(
                "Negative means the subject is underwritten BELOW the market — "
                "mark-to-market room rather than a stretch."
            ),
            block=block,
        )

    # ---- the two comp sets, side by side ---------------------------------------------
    # The Notes section points here, so both sets have to be visible and labelled. This is
    # a measurement of two selections, not an estimate of what vintage is worth: the only
    # difference between them is whether the vintage test ran.
    k = c.get("vintage_contrast") or {}
    if k.get("spread_matched") is not None or k.get("withheld_reason"):
        row += 1
        row = section_bar(
            ws, row,
            "Two comp sets, selected identically except for the vintage test", width=5,
        )
        held = ", ".join(c.get("unrestricted_held_fixed") or [])
        cell = ws.cell(
            row=row, column=1,
            value=(
                f"Held fixed in both: {held}. Varied: the "
                f"+/-{(c.get('tiering') or {}).get('year_band')}-year vintage test only."
            ),
        )
        cell.font = SUBTLE
        cell.alignment = WRAP
        row += 1
        if k.get("withheld_reason"):
            row = label_value(
                ws, row, "Comparison", "WITHHELD", note=k["withheld_reason"],
            )
        else:
            ys = [r.get("year_built") for r in c["rows"] if r.get("year_built")]
            row = header_row(
                ws, row,
                ["Comp set", "Communities", "Vintages", "Comp net effective",
                 "Subject reads"],
            )
            write_cells(
                ws, row,
                ["A — vintage test ON", len(c["detailed_communities"]),
                 f"{min(ys)}–{max(ys)}" if ys else "—",
                 k["matched"], k["spread_matched"]],
                formats=[None, FMT_COUNT, None, FMT_CURRENCY, FMT_PCT],
            )
            row += 1
            write_cells(
                ws, row,
                ["B — vintage test OFF", c.get("unrestricted_communities"),
                 f"{c.get('unrestricted_year_min')}–{c.get('unrestricted_year_max')}",
                 k["unrestricted"], k["spread_unrestricted"]],
                formats=[None, FMT_COUNT, None, FMT_CURRENCY, FMT_PCT],
            )
            row += 1
            write_cells(
                ws, row,
                ["Difference between the two sets", None, None,
                 k["unrestricted"] - k["matched"],
                 k["spread_matched"] - k["spread_unrestricted"]],
                formats=[None, None, None, FMT_CURRENCY, FMT_PCT],
                bold=True, fill=SUBTOTAL_FILL,
            )
            row += 2
            swapped_in = c.get("unrestricted_swapped_in") or []
            swapped_out = c.get("unrestricted_swapped_out") or []
            if swapped_in or swapped_out:
                row = label_value(
                    ws, row, "In B but not A", ", ".join(swapped_in) or "—",
                )
                row = label_value(
                    ws, row, "In A but not B", ", ".join(swapped_out) or "—",
                )
            cell = ws.cell(
                row=row, column=1,
                value=(
                    "Both figures are weighted by this property's own unit mix and "
                    "computed over the same bed counts, so the two rows describe the same "
                    "product. This is what these two sets measured on this date; it is "
                    "not an estimate of what vintage is worth in this market."
                ),
            )
            cell.font = SUBTLE
            cell.alignment = WRAP
            row += 1

    row = _write_om_comps(ws, row + 1, deal)
    autosize(ws, 1, len(headers))
    ws.column_dimensions["A"].width = 30
    freeze(ws, "A6")


def _cell_or_missing(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return schema.MISSING


def _write_om_comps(ws, row: int, deal: dict) -> int:
    """The comparables the memorandum states, below the scraped comps. Returns the next row."""
    block = deal.get("om_comps")
    if not isinstance(block, dict):
        return row
    row = section_bar(ws, row, "Stated in the offering memorandum", width=10)
    if block.get("absent_reason"):
        ws.cell(row=row, column=1, value=block["absent_reason"]).font = SUBTLE
        row += 1
    rent = block.get("rent") or []
    if rent:
        row = header_row(ws, row, ["Community", "Distance (mi)", "Units", "Year built", "Rent", "Basis", "Page", "Quote"])
        for r in rent:
            write_cells(ws, row, [
                r.get("community"), _cell_or_missing(r.get("distance_mi")), _cell_or_missing(r.get("units")),
                _cell_or_missing(r.get("year_built")), _cell_or_missing(r.get("rent")), r.get("rent_basis"),
                r.get("page") if r.get("page") is not None else "—", r.get("quote"),
            ])
            for col_idx, fmt in ((2, FMT_NUM1), (3, FMT_COUNT), (4, "0"), (5, FMT_CURRENCY)):
                if isinstance(ws.cell(row=row, column=col_idx).value, (int, float)):
                    ws.cell(row=row, column=col_idx).number_format = fmt
            ws.cell(row=row, column=8).alignment = WRAP
            row += 1
        row += 1
    sale = block.get("sale") or []
    if sale:
        row = section_bar(ws, row, "Sale comparables stated in the offering memorandum (institutional trades)", width=10)
        row = header_row(ws, row, ["Property", "Units", "Year built", "Sold", "Price", "Per unit", "Cap rate", "Buyer", "Seller", "Page"])
        for r in sale:
            write_cells(ws, row, [
                r.get("property"), _cell_or_missing(r.get("units")), _cell_or_missing(r.get("year_built")), r.get("sale_date"),
                _cell_or_missing(r.get("price")), _cell_or_missing(r.get("price_per_unit")), _cell_or_missing(r.get("cap_rate")),
                r.get("buyer"), r.get("seller"), r.get("page") if r.get("page") is not None else "—",
            ])
            for col_idx, fmt in ((2, FMT_COUNT), (3, "0"), (5, FMT_CURRENCY), (6, FMT_CURRENCY), (7, FMT_PCT2)):
                if isinstance(ws.cell(row=row, column=col_idx).value, (int, float)):
                    ws.cell(row=row, column=col_idx).number_format = fmt
            row += 1
        row += 1
    cell = ws.cell(row=row, column=1, value=block.get("retail_note") or "")
    cell.font = SUBTLE
    cell.alignment = WRAP
    return row + 1


#: Number format per demand row format token.
_DEMAND_FMT = {
    "count": FMT_COUNT,
    "currency0": FMT_CURRENCY,
    "pct1": FMT_PCT,
    "pct2": "0.00%",
    "header": None,
}


def _write_demand_tab(wb: Workbook, deal: dict) -> None:
    """The complete ring-demographics report, not a five-row summary of it.

    Seven columns across seven sections. Two of those sections do not share the ring
    geometry — home value is a single ZIP figure, and the metro labor history is a
    year-by-year table — so both are rendered as their own small blocks rather than
    stretched across headers that would mislabel them.
    """
    ws = wb.create_sheet(SHEET_DEMAND)
    block = deal["demand"]
    row = sheet_header(
        ws,
        "Demand",
        deal["meta"]["deal_name"],
        deal["meta"]["as_of_date"],
        note=block.get("engine_note") or "Public demographic data by drive-ring.",
    )
    sections = block.get("sections") or []
    cols = block.get("columns") or []
    if not sections and not compute.demand(deal):
        _absence(ws, row, "No demographic data", block.get("absent_reason"))
        return

    caveat = block.get("ring_1mi_caveat")
    if caveat:
        # 🔴 On the tab, not in a footnote nobody reaches. The engine's 1-mile ring
        # overstates levels by a known factor; an internal tool can leave that to operator
        # memory, a published artifact cannot.
        c = ws.cell(row=row, column=1, value=f"Read this before the 1-mile column: {caveat}")
        c.font = SUBTLE
        c.alignment = WRAP
        ws.row_dimensions[row].height = 46
        row += 2

    if not sections:
        # Fall back to the compact table when only the summary was pulled.
        headers = ["Ring", "Population", "Households", "Median household income",
                   "Total jobs", "Population growth", "Renter share"]
        row = header_row(ws, row, headers)
        for ring in compute.demand(deal):
            write_cells(
                ws, row,
                [ring["ring"], ring["population"], ring["households"], ring["median_income"],
                 ring["jobs"], ring["pop_growth_5yr"], ring["renter_share"]],
                formats=[None, FMT_COUNT, FMT_COUNT, FMT_CURRENCY, FMT_COUNT, FMT_PCT, FMT_PCT],
                bold=ring["ring"] == "MSA",
                fill=SUBTOTAL_FILL if ring["ring"] == "MSA" else None,
            )
            row += 1
        autosize(ws, 1, len(headers))
        return

    for sec in sections:
        row = section_bar(ws, row, sec["title"], width=len(cols) + 1)
        if sec.get("note"):
            c = ws.cell(row=row, column=1, value=sec["note"])
            c.font = SUBTLE
            c.alignment = WRAP
            row += 1
        is_zip = sec.get("kind") == "zip"
        row = header_row(ws, row, ["", *( ["Value"] if is_zip else cols)])
        for r in sec["rows"]:
            if r.get("kind") == "subheader":
                c = ws.cell(row=row, column=1, value=r["label"])
                c.font = BOLD
                row += 1
                continue
            label = ("    " * int(r.get("indent") or 0)) + r["label"]
            fmt = _DEMAND_FMT.get(r["format"])
            write_cells(
                ws, row, [label, *r["values"]],
                formats=[None] + [fmt] * len(r["values"]),
            )
            if r.get("note"):
                n = ws.cell(row=row, column=len(cols) + 2, value=r["note"])
                n.font = SUBTLE
            row += 1
        credit = schema.home_values_credit(deal) if is_zip else None
        if credit:
            c = ws.cell(row=row, column=1, value=credit)
            c.font = SUBTLE
            row += 1
        hist = sec.get("metro_history")
        if hist:
            row += 1
            c = ws.cell(row=row, column=1,
                        value=f"Metro nonfarm employment and unemployment — "
                              f"{sec.get('metro_label') or 'metro'}")
            c.font = BOLD
            row += 1
            row = header_row(ws, row, ["Year", "Nonfarm jobs (000s)", "Job growth",
                                       "Unemployment"])
            for h in hist:
                write_cells(
                    ws, row, [h["year"], h["nonfarm_000s"], h["yoy"], h["unemployment"]],
                    formats=[None, "#,##0.0", FMT_PCT, FMT_PCT],
                )
                row += 1
        row += 1

    ws.column_dimensions["A"].width = 42
    for i in range(2, len(cols) + 2):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 14
    ws.column_dimensions[ws.cell(row=1, column=len(cols) + 2).column_letter].width = 46
    freeze(ws, "B6")


def _write_supply_tab(wb: Workbook, deal: dict) -> None:
    ws = wb.create_sheet(SHEET_SUPPLY)
    sup = compute.supply(deal)
    row = sheet_header(
        ws,
        "Competing supply",
        deal["meta"]["deal_name"],
        deal["meta"]["as_of_date"],
        note=(
            "Pipeline projects by ring and stage. Stage is stated because entitled units "
            "and delivered units are not the same risk. Public permit records."
        ),
    )
    # An empty ring here means "nothing matched in the sources covered so far", which is a
    # weaker claim than "nothing is being built". Saying so on the tab keeps a zero from
    # being read as a census.
    c = ws.cell(row=row, column=1, value=SUPPLY_COVERAGE_NOTE)
    c.font = SUBTLE
    c.alignment = WRAP
    ws.row_dimensions[row].height = 32
    row += 2
    if not sup["rows"]:
        _absence(ws, row, "No competing-supply data", deal["supply"].get("absent_reason"))
        return

    headers = ["Ring", "Stage", "Projects", "Units"]
    row = header_row(ws, row, headers)
    first = row
    for r in sup["rows"]:
        write_cells(
            ws, row, [r["ring"], r["stage"], r["projects"], r["units"]],
            formats=[None, None, FMT_COUNT, FMT_COUNT],
        )
        row += 1
    last = row - 1
    row += 1

    row = section_bar(ws, row, "Ring totals", width=4)
    row = header_row(
        ws, row, ["Ring", "Pipeline units", "Existing stock", "Units as % of stock"]
    )
    for t in sup["totals"]:
        ring = t["ring"]
        write_cells(
            ws,
            row,
            [
                ring,
                f'=SUMIF($A${first}:$A${last},$A{row},$D${first}:$D${last})',
                t["ring_stock"] if t["ring_stock"] is not None else schema.MISSING,
                f'=IFERROR(B{row}/C{row},"")',
            ],
            formats=[None, FMT_COUNT, FMT_COUNT, FMT_PCT],
        )
        row += 1

    projects = deal["supply"].get("projects") or []
    n_cols = len(headers)
    if projects:
        row += 1
        radius = f"{schema.SUPPLY_PROJECT_RADIUS_MI:g}"
        row = section_bar(ws, row, f"Projects within {radius} miles", width=6)
        write_cells(ws, row, [
            "One row per development, at the furthest stage the pipeline shows; a dash "
            "means the source did not state it."
        ])
        row += 1
        p_headers = [
            "Project", "Distance (mi)", "Stage", "Units", "Type", "Address", "City",
            "Acres", "Developer", "First filed", "Latest activity", "Source layers",
        ]
        row = header_row(ws, row, p_headers)
        for p in projects:
            write_cells(
                ws,
                row,
                [
                    p["name"], p["miles"], p["stage"],
                    # A dash, never a zero: the pipeline not stating a unit count is not
                    # the same claim as the development having none.
                    p["units"] if p.get("units") is not None else "—",
                    p.get("product") or "—",
                    p.get("address") or "—",
                    p.get("city") or "—",
                    p["acres"] if p.get("acres") is not None else "—",
                    p.get("developer") or "—",
                    p.get("filed") or "—",
                    p.get("latest") or "—",
                    ", ".join(p.get("layers") or []) or "—",
                ],
                formats=[None, FMT_NUM1, None, FMT_COUNT, None, None, None, FMT_NUM1,
                         None, None, None, None],
            )
            row += 1
        n_cols = len(p_headers)

    autosize(ws, 1, n_cols)
    freeze(ws, "A6")


def _write_tax_tab(wb: Workbook, deal: dict) -> None:
    """Property tax: what is billed today, and what a buyer should assume.

    Its own tab because it is the single line most often got wrong on an acquisition, and
    the reason is structural rather than arithmetic: the shortcut everyone carries in their
    head — assessed value follows the price you paid — is false in some states, and
    Arizona is one of them.
    """
    ws = wb.create_sheet(SHEET_TAX)
    tu = compute.tax_underwriting(deal)
    chk = compute.tax_check(deal)
    s = deal["summary"]

    def from_om(path: str) -> bool:
        return _from_om(deal, path)

    om_note = OM_ROW_NOTE
    bill_from_om = from_om("summary.tax_billed_amount")
    year = s.get("tax_billed_year")
    year_text = str(year) if year not in (None, "", schema.MISSING) else "a year the source did not state"
    row = sheet_header(
        ws,
        "Property tax",
        deal["meta"]["deal_name"],
        deal["meta"]["as_of_date"],
        note=(
            "Rates as actually billed, not statutory or effective. The current-year block "
            + ("is checked against the bill the offering memorandum states — the seller's "
               "figure, not the county's" if bill_from_om
               else "is checked against the county's own bill")
            + "; the assumption block is what a buyer should carry at the illustrative price."
        ),
    )
    block = Block()

    import property_tax_table as ptt

    row = section_bar(ws, row, "The rate, as billed", width=4)
    # The unit in the label comes from the divisor, never assumed: per $100 in Texas or
    # Arizona, mills per $1,000 in Georgia or Florida.
    for label, key, fmt in (
        ("Parcel", "tax_parcel_id", None),
        ("Tax area code", "tax_area_code", None),
        (f"Combined rate ({ptt.rate_unit(tu['divisor'])} of assessed value)", "tax_rate_as_billed", FMT_NUM1),
        ("Assessment ratio (legal class)", "assessment_ratio", FMT_PCT),
    ):
        value = s.get(key, schema.MISSING)
        note = (om_note if (key in ("tax_parcel_id", "tax_area_code") and from_om(f"summary.{key}"))
                else _SUMMARY_NOTES.get(key))
        internal = schema.internal_only(f"summary.{key}") if value in (None, "", schema.MISSING) else None
        if internal:
            value, note, fmt = schema.INTERNAL_ONLY_SOURCE, internal[1], None
        row = label_value(
            ws, row, label, value, fmt=fmt, note=note,
            source=pv.compact_for(deal, f"summary.{key}"), block=block,
        )
    if s.get("tax_rate_evidence"):
        row = label_value(
            ws, row, "Rate evidence", None, note=str(s["tax_rate_evidence"]), block=block,
        )

    row += 1
    row = section_bar(ws, row, "Current year — computed, then checked against the bill", width=4)
    row = label_value(
        ws, row, "Assessed-value basis", tu["current_basis"], fmt=FMT_CURRENCY,
        note=str(s.get("tax_basis_label") or "")
        + (" " + om_note if from_om("summary.assessed_value_basis") else ""),
        source=pv.compact_for(deal, "summary.assessed_value_basis"), block=block,
    )
    basis_row = block.row("Assessed-value basis")
    row = label_value(
        ws, row, "Assessment ratio", tu["ratio"], fmt=FMT_PCT, block=block,
    )
    ratio_row = block.row("Assessment ratio")
    row = label_value(
        ws, row, "Assessed value", f"=B{basis_row}*B{ratio_row}", fmt=FMT_CURRENCY,
        note="Basis times the legal-class ratio.", block=block,
    )
    assessed_row = block.row("Assessed value")
    row = label_value(
        ws, row, "Combined rate", tu["rate"], fmt=FMT_NUM1, block=block,
    )
    rate_row = block.row("Combined rate")
    # The formula carries the exemption and the divisor as CELLS, so the workbook computes
    # what compute.py computes (I6). An earlier cut inlined the divisor and omitted the
    # exemption, which made this tab disagree with the summary tab on any parcel that has
    # one — and the tie-out status was then read off a third number.
    row = label_value(
        ws, row, "Rate divisor (per)", tu["divisor"] if tu["divisor"] is not None
        else schema.MISSING, fmt=FMT_COUNT,
        note="The rate's unit. Never defaulted: it decides whether the rate is per $100 "
             "or per $1,000.", block=block,
    )
    div_row = block.row("Rate divisor (per)")
    # The NORMALIZED exemption compute used, not the raw summary value. A MISSING there is
    # normalized to 0.0 in Python, and writing the text through instead made the formula
    # subtract a string — Excel returned #VALUE! while Python reported a number (I6).
    row = label_value(
        ws, row, "Exemption", tu["exemption"], fmt=FMT_CURRENCY,
        note=(
            "Normalized: an unstated exemption is zero here, which is what the computed "
            "figure assumes."
            if not isinstance(s.get("tax_exemption"), (int, float))
            else None
        ),
        source=pv.compact_for(deal, "summary.tax_exemption"), block=block,
    )
    exempt_row = block.row("Exemption")
    row = label_value(
        ws, row, "Computed annual tax",
        # ISNUMBER alone is sufficient, and a `<>0` clause here would be an aggregate
        # presence gate — the GSR bug's exact shape, which the N1 sweep correctly flagged.
        # compute.tax_underwriting() refuses a non-positive divisor, so the cell above holds
        # either a positive number or the text MISSING; a numeric zero never reaches it.
        f'=IF(AND(ISNUMBER(B{assessed_row}),ISNUMBER(B{rate_row}),ISNUMBER(B{div_row})),'
        f'MAX(0,B{assessed_row}*B{rate_row}/B{div_row}-B{exempt_row}),'
        f'"{schema.MISSING}")',
        fmt=FMT_CURRENCY,
        note="Assessed value times rate over the divisor, less any exemption. A missing "
             "divisor reads MISSING rather than assuming one.",
        block=block,
    )
    computed_row = block.row("Computed annual tax")
    bill_label = OM_BILL_LABEL if bill_from_om else "County billed amount"
    row = label_value(
        ws, row, bill_label, s.get("tax_billed_amount", schema.MISSING),
        fmt=FMT_CURRENCY,
        note=(f"For {year_text}, as the memorandum states it — the seller's "
              f"figure, not verified against the county.") if bill_from_om
        else f"As billed for {year_text}.",
        source=pv.compact_for(deal, "summary.tax_billed_amount"), block=block,
    )
    billed_row = block.row(bill_label)
    row = label_value(
        ws, row, "Computed less billed",
        # Withheld for a reason the formula cannot see (the bill's year is not the rate's):
        # text, so no cross-year delta is displayed beside "Not compared".
        "NOT CHECKED" if chk.get("reason") else
        f'=IF(AND(ISNUMBER(B{computed_row}),ISNUMBER(B{billed_row})),'
        f'B{computed_row}-B{billed_row},"NOT CHECKED")',
        fmt=FMT_CURRENCY,
        note=(
            f"Not compared: {chk['reason']}." if chk.get("reason") else
            "Guarded: a missing input renders NOT CHECKED rather than a difference of "
            "zero."
        ),
        block=block,
    )
    row = label_value(
        ws, row, "Check", chk["status"],
        note=("Ties within 2% of the billed figure." if chk["status"] == "ties"
              else (f"Not compared: {chk['reason']}." if chk.get("reason") else None)),
        block=block,
    )
    # Upload-path rows: present only when the key is carried (the published sets do not).
    if s.get("tax_statement_amount") is not None:
        row = label_value(
            ws, row, "Property taxes per the statement (T-12)", s["tax_statement_amount"],
            fmt=FMT_CURRENCY,
            note="As billed in the uploaded statement over its twelve months — the operator's "
                 "ledger, not the county's bill, which the demo does not fetch.",
            source=pv.compact_for(deal, "summary.tax_statement_amount"), block=block,
        )
    if s.get("tax_per_unit_flag") is not None:
        row = label_value(
            ws, row, "Per-unit sanity band", None, note=str(s["tax_per_unit_flag"]),
            source=pv.compact_for(deal, "summary.tax_per_unit_flag"), block=block,
        )

    row += 1
    row = section_bar(ws, row, "Underwriting assumption at the illustrative price", width=4)
    row = label_value(
        ws, row, "State", tu["state"], block=block,
    )
    row = label_value(
        ws, row, "Does the basis reset on sale?",
        {True: "Yes", False: "No", None: "not vendored"}[tu["resets_on_sale"]],
        note=tu["approach"], block=block,
    )
    illustrative = deal["meta"].get("illustrative_pricing", False)
    tax_mark = f" {ILLUSTRATIVE_MARK}" if illustrative else ""
    row = label_value(
        ws, row, ("Illustrative purchase price" + tax_mark) if illustrative else "Purchase price", tu["price"],
        fmt=FMT_CURRENCY, block=block,
    )
    row = label_value(
        ws, row, "Year-one full value", tu["assumed_full_value"], fmt=FMT_CURRENCY,
        note=tu["assumed_full_value_label"], block=block,
    )
    row = label_value(
        ws, row, "Year-one assessed value", tu["assumed_assessed"], fmt=FMT_CURRENCY,
        note=tu["assumed_assessed_label"], block=block,
    )
    row = label_value(
        ws, row, f"Year-one tax{tax_mark}", tu["assumed_tax"], fmt=FMT_CURRENCY,
        note="The figure to carry into the model.", block=block,
    )
    row = label_value(
        ws, row, f"Year-one tax per unit{tax_mark}", tu["assumed_tax_per_unit"],
        fmt=FMT_CURRENCY, block=block,
    )

    row += 1
    row = section_bar(ws, row, "What the generic shortcut would have said", width=4)
    row = label_value(
        ws, row, "Assessed value if it reset to the price", tu["generic_assessed"],
        fmt=FMT_CURRENCY,
        note="Purchase price times the assessment ratio — the pattern that holds in most "
             "states.", block=block,
    )
    row = label_value(
        ws, row, "Tax on that basis", tu["generic_tax"], fmt=FMT_CURRENCY, block=block,
    )
    if tu["generic_overstatement"]:
        row = label_value(
            ws, row, "Overstatement versus the correct approach",
            tu["generic_overstatement"], fmt="0.00\"x\"",
            note=(
                "Shown because the size of the error is the point. Carrying the generic "
                "pattern into an Arizona model does not shade the answer, it changes it: "
                "a tax line this far out fails a deal that works."
            ),
            block=block,
        )
    autosize(ws, 1, 4)


def _write_notes_tab(wb: Workbook, deal: dict) -> None:
    """The judgment calls, in plain language, on their own tab."""
    ws = wb.create_sheet(SHEET_NOTES)
    notes = deal["meta"].get("notes") or []
    row = sheet_header(
        ws,
        "Notes and disclosures",
        deal["meta"]["deal_name"],
        deal["meta"]["as_of_date"],
        note=(
            "Every output here is the result of decisions someone made. These are those "
            "decisions, including the ones that make the numbers look worse."
        ),
    )
    if not notes:
        _absence(ws, row, "No notes recorded", "This output set carries no disclosures.")
        return
    row = header_row(ws, row, ["Topic", "Applies to", "Note"])
    for n in notes:
        write_cells(ws, row, [n.get("topic"), n.get("applies_to"), n.get("note")])
        ws.cell(row=row, column=3).alignment = WRAP
        row += 1
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 26
    ws.column_dimensions["C"].width = 110


def build(deal: dict, out_path: Path) -> Path:
    errors = schema.validate(deal)
    if errors:
        raise ValueError(
            "deal failed schema validation:\n  - " + "\n  - ".join(errors)
        )
    wb = Workbook()
    _write_summary_tab(wb, deal)
    _write_comps_tab(wb, deal)
    _write_tax_tab(wb, deal)
    _write_demand_tab(wb, deal)
    _write_supply_tab(wb, deal)
    _write_notes_tab(wb, deal)
    write_audit_trail(wb, deal, "deal_summary", title="Deal summary, comps, demand and supply")
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
