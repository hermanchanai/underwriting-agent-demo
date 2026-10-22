#!/usr/bin/env python3
"""E2 rule 8: the trailing annualised T-3 revenue series — six figures the model takes.

For each of the six most recent months, (that month + the two before it) x 4. Needs eight
month columns for the full six; fewer months give fewer figures, never a padded zero.
Revenue per month follows the P&L's own rule: printed Gross Scheduled Rent where the
statement has one, else GPR + gain/(loss) to lease, plus the tail through Revenue
Adjustments.

Usage: python tools/test_t3_series.py
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _testkit import Checker, guard  # noqa: E402

import compute  # noqa: E402
import schema  # noqa: E402

FIXTURE = HERE / "fixtures" / "synthetic_full.json"


def _revenue_by_month(deal):
    """Independent: sum the revenue span per month, applying the printed-GSR rule."""
    labels = deal["t12"]["month_labels"]
    cats = list(schema.T12_REVENUE_CATEGORIES)
    tail = cats[cats.index("Gross Scheduled Rent") + 1: cats.index("Revenue Adjustments") + 1]
    printed = compute.gsr_is_printed(deal)
    out = []
    for i in range(len(labels)):
        def cat_month(cat):
            return sum((r["monthly"][i] or 0) for r in deal["t12"]["rows"] if r.get("category") == cat)
        gsr = cat_month("Gross Scheduled Rent") if printed else cat_month("Gross Potential Rent") + cat_month("Gain / (Loss) to Lease")
        out.append(gsr + sum(cat_month(t) for t in tail))
    return out


def main() -> int:
    c = Checker("test_t3_series")
    fn = getattr(compute, "t3_trailing_series", None)
    if not c.ok("compute.t3_trailing_series exists", callable(fn)):
        return c.finish()
    deal = json.loads(FIXTURE.read_text(encoding="utf-8"))
    s = fn(deal)
    c.equal("six figures on a twelve-month statement", len(s), 6)
    c.ok("each carries month and t3_revenue_annualized", all({"month", "t3_revenue_annualized"} <= set(e) for e in s))
    labels = deal["t12"]["month_labels"]
    order = compute.detect_month_order(labels)
    chrono = labels if order == "oldest_first" else list(reversed(labels))
    c.equal("months are the six most recent, oldest to newest", [e["month"] for e in s], chrono[-6:])
    rev = _revenue_by_month(deal)
    rev_chrono = rev if order == "oldest_first" else list(reversed(rev))
    expected = [(rev_chrono[i] + rev_chrono[i - 1] + rev_chrono[i - 2]) * 4 for i in range(6, 12)]
    c.ok("each figure is (month + prior two) x 4, recomputed independently",
         all(abs(a["t3_revenue_annualized"] - b) < 0.01 for a, b in zip(s, expected)),
         str([(round(a["t3_revenue_annualized"]), round(b)) for a, b in zip(s, expected)][:3]))
    c.close("the last figure IS the P&L's T-3 annualised revenue", s[-1]["t3_revenue_annualized"],
            compute.pnl(deal)["total_revenue_t3_annualized"], 0.01)

    c.section("direction and short statements")
    rev_deal = copy.deepcopy(deal)
    rev_deal["t12"]["month_labels"] = list(reversed(labels))
    for r in rev_deal["t12"]["rows"]:
        r["monthly"] = list(reversed(r["monthly"]))
    s2 = fn(rev_deal)
    c.ok("a newest-first statement gives the same series", [e["t3_revenue_annualized"] for e in s2] == [e["t3_revenue_annualized"] for e in s])
    short = copy.deepcopy(deal)
    keep = compute.recent_indices(labels, 7)
    short["t12"]["month_labels"] = [labels[i] for i in keep]
    for r in short["t12"]["rows"]:
        r["monthly"] = [r["monthly"][i] for i in keep]
    c.equal("seven months give five figures", len(fn(short)), 5)
    two = copy.deepcopy(short)
    two["t12"]["month_labels"] = two["t12"]["month_labels"][-2:]
    for r in two["t12"]["rows"]:
        r["monthly"] = r["monthly"][-2:]
    c.equal("two months give none", fn(two), [])
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
