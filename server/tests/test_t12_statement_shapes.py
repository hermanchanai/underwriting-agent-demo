#!/usr/bin/env python3
"""Three real statement shapes the upload reader withheld or mis-keyed.

A chain contract ran five sanitized real statements through the pipeline. Two tied; three
were withheld, and one of the three was withheld against the WRONG answer key:

* A statement that prints ``Controllable Net Operating Income`` above its bottom-line
  ``Net Operating Income`` had the controllable row recorded as the statement's NOI. The
  mapper kept the FIRST row its NOI rule matched and the reader kept the first row with that
  label, so the tie gate compared the read against $2.79M while the statement's NOI is $1.75M
  — a read that produced $2.79M would have been published as tied.
* That statement also prints ``Controllable Operating Expenses`` and ``Other Operating
  Expenses`` — running subtotals with no ``Total`` in the label — and they were read as
  expense lines on top of their own detail. And it prints ``Water/Sewer`` under income and
  again under expenses: one category per label cannot serve both, and the withheld reason
  said only that the totals differ.
* One accounting system's Report1 names its revenue total ``TOTAL Operating Income``, which no rule
  recognised, so no income section was bounded and 59 income lines were dropped.
* A calendar-year operations report prints 24 month columns (a full two years, the later
  months empty) and names its period in the title, ``... for April 2026``. The reader
  refused on "expected 12 month columns, found 24".

Pins: the statement's NOI is its LAST net-operating-income row, in the mapping hand-off and
in the reader; a row that equals the lines directly above it is a printed subtotal, not a
line; a withheld reason names a label printed on both sides; ``TOTAL Operating Income`` is the
revenue total; the twelve months ending at a stated as-of month are read out of a wider grid,
and without a stated as-of the refusal says why.

Usage: python server/tests/test_t12_statement_shapes.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["DEMO_MODEL_MODE"] = "mock"
sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from _harness import Checker, guard  # noqa: E402

import schema  # noqa: E402
import source_readers as raw  # noqa: E402
from server import assemble, extract_llm  # noqa: E402

MONTHS = ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026",
          "Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026", "Nov 2026", "Dec 2026"]


def line(label, v, months=12):
    """One row: a label and ``v`` in every month (``v`` may be a list of monthly values)."""
    vals = list(v) if isinstance(v, (list, tuple)) else [v] * months
    return [label] + vals


def run(rows, header_index):
    """The upload path, mock mode: labels → map_t12 → build_t12 (as server/runs.py wires it)."""
    labels = raw.labels_for_mapping(rows, header_index, 1, None)
    mapping = extract_llm.map_t12(labels, [str(h) for h in rows[header_index]],
                                  extract_llm.UsageLedger())
    mapping.update({"header_row_index": header_index, "sheet_index": 0,
                    "code_col": None, "label_col": 1})
    block, notes = assemble.build_t12({"sheets": [{"rows": rows, "name": "T12"}]}, mapping, 10)
    return mapping, block, notes


def controllable_statement():
    """One real statement's shape, rebuilt synthetically: controllable NOI above the bottom line, two unlabelled
    running subtotals, and one label printed under income AND expenses."""
    rows = [["Sample Co"], ["Income Statement"], [None] + MONTHS + ["Total"]]
    rows += [["INCOME"], line("Market Rent", 1000), line("Water/Sewer", 40),
             line("Late Charges", 10), line("Total Income", 1050)]
    rows += [["EXPENSES"], line("Repairs", 100), line("Payroll Salaries", 200),
             line("Water/Sewer", 30), line("Total Utilities", 30),
             line("Controllable Operating Expenses", 330),
             line("Controllable Net Operating Income", 720),
             line("Real Estate Taxes", 150), line("Insurance", 60),
             line("Other Operating Expenses", 210),
             line("Net Operating Income", 510)]
    return rows


def main() -> int:
    c = Checker("test_t12_statement_shapes")

    c.section("the statement's NOI is its LAST net-operating-income row (mapping hand-off)")
    labels = ["Net Operating Income", "Market Rent", "Total Income", "Repairs",
              "Controllable Net Operating Income", "Taxes", "Net Operating Income"]
    got = extract_llm.map_t12(labels, [], extract_llm.UsageLedger())["subtotals"]
    c.equal("summary NOI, then controllable NOI, then the bottom line → the bottom line",
            got.get("net_operating_income"), "Net Operating Income")
    labels = ["Market Rent", "Total Income", "Repairs", "Controllable Net Operating Income",
              "Taxes", "Net Operating Income"]
    got = extract_llm.map_t12(labels, [], extract_llm.UsageLedger())["subtotals"]
    c.equal("controllable NOI above the bottom line → the bottom line",
            got.get("net_operating_income"), "Net Operating Income")

    c.section("the reader keeps the LAST row carrying the named NOI label")
    rows = [[None] + MONTHS, line("Net Operating Income", 999), line("Market Rent", 100),
            line("Total Income", 100), line("Repairs", 40), line("Net Operating Income", 60)]
    block, _ = raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=1, code_col=None,
                             label_col=1, label_map={"Market Rent": "Gross Potential Rent",
                                                     "Repairs": "Repairs and Maintenance"},
                             subtotal_labels={"net_operating_income": "Net Operating Income"})
    c.equal("source_subtotals NOI is the bottom-line row (60 × 12), not the summary row",
            block["source_subtotals"].get(schema.PNL_NOI_LABEL), 720.0)

    c.section("a controllable-NOI statement: the answer key is the printed bottom line")
    mapping, block, notes = run(controllable_statement(), 2)
    c.equal("mapping names the bottom-line NOI row", mapping["subtotals"].get("net_operating_income"),
            "Net Operating Income")
    c.equal("source_subtotals NOI == the printed Net Operating Income row (510 × 12)",
            block["source_subtotals"].get(schema.PNL_NOI_LABEL), 6120.0)
    reason = block.get("absent_reason") or ""
    c.ok("withheld: a label printed under income AND expenses cannot take one category",
         bool(reason), reason or str(notes))
    c.ok("the withheld reason NAMES the two-sided label", "Water/Sewer" in reason, reason)
    c.ok("the withheld reason does not cite the controllable NOI figure (720 × 12 = 8,640)",
         "8,640" not in reason, reason)

    c.section("running subtotals with no 'Total' in the label are not read as lines")
    rows = controllable_statement()
    # Same statement with the two-sided label renamed: nothing else stands between it and a tie.
    rows[11] = line("Utilities - Water", 30)
    rows[5] = line("Utility Reimbursement", 40)
    mapping, block, notes = run(rows, 2)
    kept = [r["label"] for r in block.get("rows") or []]
    c.ok("Controllable Operating Expenses is not an operating line (and lines WERE read)",
         bool(kept) and "Controllable Operating Expenses" not in kept, str(kept))
    c.ok("Other Operating Expenses is not an operating line (and lines WERE read)",
         bool(kept) and "Other Operating Expenses" not in kept, str(kept))
    c.equal("the statement ties (no absent_reason)", block.get("absent_reason"), None)
    c.ok("the notes say which rows were read as the statement's own subtotals",
         any("Controllable Operating Expenses" in n and "subtotal" in n for n in notes), str(notes))

    c.section("a line that merely equals ONE line above it is still a line")
    rows = [[None] + MONTHS, line("Market Rent", 100), line("Parking", 100),
            line("Total Income", 200), line("Repairs", 50), line("Net Operating Income", 150)]
    block, _ = raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=1, code_col=None,
                             label_col=1, label_map={"Market Rent": "Gross Potential Rent",
                                                     "Parking": "Other Income",
                                                     "Repairs": "Repairs and Maintenance"},
                             subtotal_labels={"total_revenue": "Total Income",
                                              "net_operating_income": "Net Operating Income"})
    c.ok("Parking (== Market Rent) is kept", any(r["label"] == "Parking" for r in block["rows"]))

    c.section("amounts that merely add up are not a subtotal")
    rows = [[None] + MONTHS, line("Market Rent", 1000), line("Total Income", 1000),
            line("Repairs", 100), line("Payroll", 200), line("Insurance", 300),
            line("Net Operating Income", 400)]
    block, _ = raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=1, code_col=None,
                             label_col=1, label_map={"Market Rent": "Gross Potential Rent",
                                                     "Repairs": "Repairs and Maintenance",
                                                     "Payroll": "Payroll", "Insurance": "Insurance"},
                             subtotal_labels={"total_revenue": "Total Income",
                                              "net_operating_income": "Net Operating Income"})
    c.ok("Insurance (== Repairs + Payroll every month) is kept as a line",
         any(r["label"] == "Insurance" for r in block["rows"]), str([r["label"] for r in block["rows"]]))
    rows = [[None, None] + MONTHS, ["4000", "Market Rent"] + [1000] * 12,
            ["4100", "Parking Income"] + [30] * 12, ["4200", "Storage Income"] + [20] * 12,
            ["4300", "Other Income"] + [50] * 12]
    block, _ = raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=1, code_col=1,
                             label_col=2, label_map={"Market Rent": "Gross Potential Rent",
                                                     "Parking Income": "Other Income",
                                                     "Storage Income": "Other Income",
                                                     "Other Income": "Other Income"})
    c.ok("a coded account whose label reads as an aggregate is still a line",
         any(r["label"].startswith("Other Income") for r in block["rows"]),
         str([r["label"] for r in block["rows"]]))

    c.section("a codeless account that adds up is kept unless the printed totals say otherwise (re-review P1)")
    rows = [[None] + MONTHS + ["Total"], ["INCOME"], line("Market Rent", 1000),
            line("Parking Income", 30), line("Storage Income", 20), line("Other Income", 50),
            line("Total Income", 1100), ["EXPENSES"], line("Repairs", 200),
            line("Total Operating Expenses", 200), line("Net Operating Income", 900)]
    _m, block, notes = run(rows, 0)
    c.equal("the statement's totals count Other Income as a line: the read ties",
            block.get("absent_reason"), None)
    c.ok("Other Income is a published line",
         any(r["label"] == "Other Income" for r in block.get("rows") or []), str(notes))
    no_totals = [r for r in rows if not (r and str(r[0]).startswith(("Total", "Net")))]
    _m, block, notes = run(no_totals, 0)
    c.ok("with no printed totals to say otherwise, Other Income is still a line",
         any(r["label"] == "Other Income" for r in block.get("rows") or []),
         str(block.get("absent_reason")) + str(notes))

    c.section("'TOTAL Operating Income' is the revenue total")
    c.equal("structural_row('TOTAL Operating Income')", schema.structural_row("TOTAL Operating Income"),
            "Total Revenue")

    c.section("an abbreviated controllable-NOI row is a subtotal, not a line")
    c.equal("structural_row('Controllable Net Oper Income')",
            schema.structural_row("Controllable Net Oper Income"), "subtotal")

    c.section("statistics printed among the accounts are not accounts")
    for label in ("Average Market Rent", "Average In Place Rent", "Physical Occupancy",
                  "Average # of Units Occupied", "Economic Occupancy"):
        c.ok(f"{label!r} is a statistic", schema.statistic_row(label))
    for label in ("Market Rent", "Occupancy Tax", "Vacancy Loss", "Late Charges"):
        c.ok(f"{label!r} is an account", not schema.statistic_row(label))
    got = extract_llm.map_t12(["Market Rent", "Physical Occupancy", "Total Income", "Repairs",
                               "Net Operating Income"], [], extract_llm.UsageLedger())
    c.equal("the mapper excludes a statistic without asking",
            {m["label"]: m["category"] for m in got["mappings"]}.get("Physical Occupancy"),
            extract_llm.EXCLUDE)

    c.section("bad debt printed positive and subtracted by the statement: the reason says so")
    rows = [["Sample"], [None] + MONTHS + ["Total"], ["INCOME"], line("Market Rent", 1000),
            line("Bad Debt", 30), line("Effective Gross Income", 970), ["EXPENSES"],
            line("Repairs", 200), line("Total Operating Expenses", 200),
            line("Net Operating Income", 770)]
    _m, block, _n = run(rows, 1)
    reason = block.get("absent_reason") or ""
    c.ok("withheld (a sign flip is not applied silently)", bool(reason), str(_n))
    c.ok("the reason names the positive-printed deduction", "Bad Debt" in reason and "sign" in reason, reason)

    c.section("24 calendar month columns, as-of named in the title → the twelve ending there")
    cal = [f"{m} {y}" for y in (2025, 2026) for m in
           ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")]
    # Real activity May 2025 – Apr 2026, one stray entry in May 2026, then empty months.
    def cal_line(label, v, stray=0):
        vals = [0] * 4 + [v] * 12 + [stray] + [0] * 7
        return [label] + vals + [sum(vals)]
    rows = [["Operations 12 Month Report for April 2026"], [None] + cal + ["Total"],
            cal_line("Market Rent", 100), cal_line("Late Fees", 5, stray=3),
            cal_line("Total Income", 105, stray=3), cal_line("Repairs", 40),
            cal_line("Net Operating Income", 65, stray=3)]
    block, notes = raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=2,
                                 code_col=None, label_col=1,
                                 label_map={"Market Rent": "Gross Potential Rent",
                                            "Late Fees": "Other Income",
                                            "Repairs": "Repairs and Maintenance"},
                                 subtotal_labels={"total_revenue": "Total Income",
                                                  "net_operating_income": "Net Operating Income"})
    c.equal("month_labels are May 2025 – Apr 2026", (block["month_labels"][0], block["month_labels"][-1]),
            ("2025-05", "2026-04"))
    c.equal("Total Income is the twelve window months (105 × 12), not the Total column",
            block["source_subtotals"].get("Total Revenue"), 1260.0)
    c.equal("NOI is the window's (65 × 12)", block["source_subtotals"].get(schema.PNL_NOI_LABEL), 780.0)
    c.ok("a note says the window was chosen from a wider grid, and why",
         any("April 2026" in n and "24" in n for n in notes), str(notes))

    c.section("a generation timestamp is not the reporting period")
    rows[0] = ["Report generated on 08/15/2026"]
    try:
        raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=2, code_col=None,
                      label_col=1, label_map={"Market Rent": "Gross Potential Rent"})
        c.ok("refused (a print date does not choose the twelve)", False, "no ValueError")
    except ValueError as exc:
        c.ok("refused (a print date does not choose the twelve)", "as-of" in str(exc), str(exc))

    c.section("a stated RANGE ends where it ends, not where it starts (re-review P2)")
    for title in ("For the period 05/01/2025 - 04/30/2026", "For the period May 2025 through April 2026"):
        rows[0] = [title]
        block, _ = raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=2, code_col=None,
                                 label_col=1, label_map={"Market Rent": "Gross Potential Rent"})
        c.equal(f"{title!r} → May 2025 – Apr 2026",
                (block["month_labels"][0], block["month_labels"][-1]), ("2025-05", "2026-04"))

    c.section("24 month columns and no stated as-of → the refusal says what is missing")
    rows[0] = ["Operations Report"]
    try:
        raw.parse_t12(None, 10, sheet=raw.GridSheet(rows), header_row=2, code_col=None,
                      label_col=1, label_map={"Market Rent": "Gross Potential Rent"})
        c.ok("refused", False, "no ValueError")
    except ValueError as exc:
        c.ok("refused, naming the missing as-of month", "as-of" in str(exc) and "24" in str(exc), str(exc))

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
