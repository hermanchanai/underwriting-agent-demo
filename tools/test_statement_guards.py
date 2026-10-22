#!/usr/bin/env python3
"""E2 rules 1, 3, 4, 5 (PLAN_single_engine.md): the statement sanity guards.

The operator's T-12 skill carries four guards the engine did not: a contra-revenue
category printed positive (the RealPage/OneSite convention for bad debt), total operating
expenses that are not positive (NOI would add expenses back), a recovery line filed inside
an expense bucket, and a subtotal row categorised alongside the rows it sums. Each is a
REVIEW row here — flagged, never silently corrected: a flip the engine made on its own
would be a claim about the statement.

Usage: python tools/test_statement_guards.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _testkit import Checker, guard  # noqa: E402

import compute  # noqa: E402

FIXTURE = HERE / "fixtures" / "synthetic_full.json"
LABELS = [f"2025-{m:02d}" for m in range(1, 13)]


def _row(label, category, total):
    return {"label": label, "category": category, "monthly": [total / 12.0] * 12}


def _deal(rows):
    return {"t12": {"units": 100, "month_labels": LABELS, "rows": rows, "source_subtotals": {}}}


BASE = [
    _row("RENT", "Gross Potential Rent", 1_200_000),
    _row("VACANCY", "Vacancy", -60_000),
    _row("BAD DEBT WRITE-OFF", "Delinquency", -12_000),
    _row("PAYROLL", "Payroll", 150_000),
    _row("UTILITIES - WATER", "Utilities", 40_000),
]


def _by_name(guards):
    return {g["guard"]: g for g in guards}


def main() -> int:
    c = Checker("test_statement_guards")
    fn = getattr(compute, "statement_guards", None)
    if not c.ok("compute.statement_guards exists", callable(fn)):
        return c.finish()

    c.section("shape")
    g = fn(_deal(BASE))
    c.equal("four guards", len(g), 4)
    c.ok("every guard has guard/status/detail/rows", all({"guard", "status", "detail", "rows"} <= set(x) for x in g))
    c.ok("statuses are OK or REVIEW", all(x["status"] in ("OK", "REVIEW") for x in g), str([x["status"] for x in g]))
    c.ok("a clean statement is all OK", all(x["status"] == "OK" for x in g), str([(x["guard"], x["status"]) for x in g]))
    names = _by_name(g)

    c.section("rule 1 — a contra-revenue category netting positive is flagged, not flipped")
    d = _deal(BASE + [_row("BAD DEBT DEPOSIT WAIVER", "Delinquency", 20_000)])  # nets +8,000
    r = _by_name(fn(d))["Contra-revenue categories net negative"]
    c.equal("REVIEW", r["status"], "REVIEW")
    c.ok("names the category", "Delinquency" in r["rows"], str(r["rows"]))
    c.ok("the P&L is NOT flipped by the guard", compute.pnl(d)["rows"]["Delinquency"]["t12"] > 0)
    d2 = _deal(BASE + [_row("BAD DEBT COLLECTED", "Delinquency", 5_000)])  # recoveries inside, nets -7,000
    c.equal("recoveries inside a category that still nets negative are fine",
            _by_name(fn(d2))["Contra-revenue categories net negative"]["status"], "OK")

    c.section("rule 3 — total operating expenses must be positive")
    d = _deal([_row("RENT", "Gross Potential Rent", 1_200_000), _row("PAYROLL", "Payroll", -150_000)])
    r = _by_name(fn(d))["Total operating expenses positive"]
    c.equal("REVIEW when expenses net non-positive", r["status"], "REVIEW")
    c.ok("says NOI would add expenses back", "NOI" in r["detail"], r["detail"])
    c.equal("OK on the clean statement", names["Total operating expenses positive"]["status"], "OK")

    c.section("rule 4 — a recovery line inside an expense bucket")
    d = _deal(BASE + [_row("UTILITY REIMBURSEMENTS - WATER", "Utilities", -9_000)])
    r = _by_name(fn(d))["No recovery lines inside expense buckets"]
    c.equal("REVIEW", r["status"], "REVIEW")
    c.ok("names the line", "UTILITY REIMBURSEMENTS - WATER" in r["rows"], str(r["rows"]))
    d = _deal(BASE + [_row("BLUE MOON LICENSE", "General and Administrative", -1_500)])
    c.equal("a negative expense line WITHOUT a recovery keyword is not this guard's business",
            _by_name(fn(d))["No recovery lines inside expense buckets"]["status"], "OK")
    d = _deal(BASE + [_row("UTILITY REIMBURSEMENTS - WATER", "Utility Reimbursements", 9_000)])
    c.equal("a recovery filed under the revenue category is fine",
            _by_name(fn(d))["No recovery lines inside expense buckets"]["status"], "OK")

    c.section("rule 5 — a subtotal row categorised alongside the rows it sums")
    d = _deal(BASE + [_row("PET RENT", "Other Income", 10_000), _row("PARKING", "Other Income", 20_000),
                      _row("TOTAL OTHER INCOME", "Other Income", 30_000)])
    r = _by_name(fn(d))["No subtotal row counted with its members"]
    c.equal("REVIEW", r["status"], "REVIEW")
    c.ok("names the subtotal row", any("TOTAL OTHER INCOME" in x for x in r["rows"]), str(r["rows"]))
    d = _deal(BASE + [_row("PET RENT", "Other Income", 10_000), _row("PARKING", "Other Income", 20_000),
                      _row("TOTAL OTHER INCOME", None, 30_000)])
    c.equal("the same row left uncategorised is exactly right",
            _by_name(fn(d))["No subtotal row counted with its members"]["status"], "OK")
    d = _deal(BASE + [_row("PET RENT", "Other Income", 100), _row("PARKING", "Other Income", 200),
                      _row("TOTAL OTHER INCOME", "Other Income", 300)])
    c.equal("under $500 is below the guard's floor (the skill's gate)",
            _by_name(fn(d))["No subtotal row counted with its members"]["status"], "OK")

    c.section("absent statement")
    c.equal("no guards on a deal with no T-12", fn({"t12": {"absent_reason": "none uploaded", "rows": [],
                                                           "month_labels": [], "units": 0}}), [])

    c.section("the published sample")
    deal = json.loads(FIXTURE.read_text(encoding="utf-8"))
    g = fn(deal)
    for x in g:
        c.equal(f"sample: {x['guard']}", x["status"], "OK")
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
