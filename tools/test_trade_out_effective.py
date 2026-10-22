"""Trade-out EFFECTIVE change is measured against the prior EFFECTIVE rent.

Effective change = new effective - prior effective, where the prior
effective is the prior GROSS rent when the report states no prior concession. Before this,
``compute.trade_out`` subtracted the prior gross from the new effective on every row, so a
cohort whose prior leases carried a concession read a worse effective trade-out than the
rent-roll skill printed for the same report (New cohort -$46.67 vs -$30.00 on the parity
fixture, ``tools/test_rentroll_parity.py``).

The row carries the prior effective as the optional ``prior_rent_effective``; absent or
None means no prior concession was stated, so prior gross stands in. Gross change is
untouched: it is always gross vs prior gross.

Usage: python tools/test_trade_out_effective.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _testkit import Checker, guard  # noqa: E402


def _row(**over):
    row = {"cohort": "New", "leases": 3, "prior_rent": 1500.0,
           "new_rent_gross": 1520.0, "new_rent_effective": 1440.0}
    row.update(over)
    return row


def _one(row):
    import compute

    return compute.trade_out({"trade_out": {"rows": [row], "absent_reason": None}})["rows"][0]


def main() -> int:
    import schema

    c = Checker("trade-out effective change vs prior EFFECTIVE")

    c.section("a stated prior effective is the baseline")
    e = _one(_row(prior_rent_effective=1450.0))
    c.close("effective_change = new effective - prior effective", e["effective_change"],
            1440.0 - 1450.0, tol=1e-9)
    c.close("effective_change_pct divides by the prior effective",
            e["effective_change_pct"], (1440.0 - 1450.0) / 1450.0, tol=1e-12)
    c.close("gross_change stays gross vs prior gross", e["gross_change"], 1520.0 - 1500.0,
            tol=1e-9)
    c.close("gross_change_pct stays over prior gross", e["gross_change_pct"],
            (1520.0 - 1500.0) / 1500.0, tol=1e-12)
    c.equal("the prior effective is carried on the computed row",
            e.get("prior_rent_effective"), 1450.0)

    c.section("no prior concession stated -> prior gross stands in")
    for label, row in (("key absent", _row()),
                       ("key None", _row(prior_rent_effective=None))):
        e = _one(row)
        c.close(f"{label}: effective_change = new effective - prior gross",
                e["effective_change"], 1440.0 - 1500.0, tol=1e-9)
        c.close(f"{label}: effective_change_pct over prior gross",
                e["effective_change_pct"], (1440.0 - 1500.0) / 1500.0, tol=1e-12)
        c.equal(f"{label}: the prior effective reads as the prior gross",
                e.get("prior_rent_effective"), 1500.0)

    c.section("the schema accepts the key")
    sample = HERE / "fixtures" / "synthetic_full.json"
    if not sample.exists():
        c.ok("the fixture deal exists to validate against", False, str(sample))
    else:
        deal = json.loads(sample.read_text())
        base_errors = set(schema.validate(copy.deepcopy(deal)))
        deal["trade_out"] = {"rows": [_row(prior_rent_effective=1450.0)],
                             "absent_reason": None}
        new_errors = [x for x in schema.validate(deal) if x not in base_errors
                      and x.startswith("trade_out")]
        c.equal(f"no trade_out schema error for prior_rent_effective ({sample.name})",
                new_errors, [])
        deal["trade_out"]["rows"][0]["prior_rent_effective"] = "1450"
        bad = [x for x in schema.validate(deal) if "prior_rent_effective" in x]
        c.ok("a non-number prior_rent_effective is still rejected", bool(bad), repr(bad))
    return c.finish()


if __name__ == "__main__":
    sys.exit(guard(main))
