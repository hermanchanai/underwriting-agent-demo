#!/usr/bin/env python3
"""The T-12 tie-out gate has a REVIEW band between "ties" and "withheld".

The rule: a T-12 with the wrong NOI is worse than no T-12, so a read
that does not tie to the statement's own subtotals is withheld. The first real accounting-system
statement through the engine read revenue to within $260 of the
statement's $1.87M and NOI to within 0.25% — a mapping quibble on two small lines — and the
gate withheld the whole section on the $1 tolerance, leaving the output set without its T-12.

The rule now has three outcomes: within the rounding tolerance the section TIES; within
``TIE_REVIEW_BAND`` (a share of the statement's own figure) it is KEPT with a note and the
audit trail's error check reads REVIEW on the lines that differ; beyond it the section is
WITHHELD as before. A wildly wrong read (the $1.3M NOI miss that made the rule) is still
withheld; a small one is shown with its discrepancy on the page.

Usage: python server/tests/test_tie_band.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from _harness import Checker, guard  # noqa: E402

from server import assemble  # noqa: E402
import schema  # noqa: E402

LABELS = [f"2025-{m:02d}" for m in range(1, 13)]


def block(revenue_lines, expense_lines, stated_rev, stated_opex):
    rows = [{"label": lab, "category": cat, "monthly": [v / 12] * 12} for lab, cat, v in revenue_lines + expense_lines]
    return {"month_labels": LABELS, "units": 100, "rows": rows,
            "source_subtotals": {"Total Revenue": stated_rev, schema.PNL_TOTAL_OPEX_LABEL: stated_opex,
                                 schema.PNL_NOI_LABEL: stated_rev - stated_opex}}


REV = [("Rent", "Gross Potential Rent", 1_800_000), ("Vacancy", "Vacancy", -100_000), ("Other", "Other Income", 170_000)]
EXP = [("Payroll", "Payroll", 400_000), ("Taxes", "Property Taxes", 300_000), ("Utilities", "Utilities", 60_000)]


def main() -> int:
    c = Checker("test_tie_band")
    c.ok("assemble.TIE_REVIEW_BAND exists", hasattr(assemble, "TIE_REVIEW_BAND"), "no band constant")

    c.section("exact: ties")
    note, failure = assemble._tie_out(block(REV, EXP, 1_870_000, 760_000))
    c.ok("no failure", failure is None, str(failure))
    c.ok("the note says it ties", note is not None and "Ties" in note, str(note))

    c.section("a small miss: kept, with the discrepancy named")
    # ours: revenue 1,870,000 / opex 760,000; the statement says 1,870,260 / 763,024 (a real statement's gaps)
    note, failure = assemble._tie_out(block(REV, EXP, 1_870_260, 763_024))
    c.ok("NOT withheld", failure is None, str(failure))
    c.ok("the note names the gap and points at the error check", note is not None and "REVIEW" in note and "3,024" in note, str(note))

    c.section("a large miss: withheld, as before")
    note, failure = assemble._tie_out(block(REV, EXP, 3_170_000, 760_000))
    c.ok("withheld", failure is not None and "withheld" in failure, str(failure))
    c.ok("with both figures", failure is not None and "3,170,000" in failure and "1,870,000" in failure, str(failure))

    c.section("the band is a share of the statement's own figure")
    band = getattr(assemble, "TIE_REVIEW_BAND", None)
    c.ok("band is a small fraction (≤ 5%)", isinstance(band, float) and 0 < band <= 0.05, str(band))
    # 1% of a $760k opex is $7,600: a $7,000 miss is kept, an $8,000 miss at 1% would not be — pin the edge for the chosen band
    edge_ok = 760_000 * band * 0.9
    note, failure = assemble._tie_out(block(REV, EXP, 1_870_000, 760_000 + edge_ok))
    c.ok("inside the band: kept", failure is None, str(failure))
    edge_bad = 760_000 * band * 1.1
    note, failure = assemble._tie_out(block(REV, EXP, 1_870_000, 760_000 + edge_bad))
    c.ok("outside the band: withheld", failure is not None, str(note))
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
