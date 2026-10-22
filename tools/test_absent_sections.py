"""An upload that brings only some of the documents still has to produce a valid file.

The showcase deal has all three inputs. A visitor's upload often will not: a marketing PDF
and nothing else, or a rent roll on its own. The schema has always had this rule for
``trade_out`` — rows XOR a stated reason, never an empty block that says nothing — and
this extends it to ``t12`` and ``rent_roll``.

The rule is worth the ceremony because of what the alternative looks like. A T-12 section
with twelve months of zeroes validates, renders, and reads as a property that earned
nothing. A section that says "no trailing-twelve statement was uploaded" cannot be
mistaken for anything. The whole project's position is that a visible hole beats a
plausible number, and this is that position expressed as a validator.

Checked in both directions, because a rule that only ever sees good input proves nothing:
an absent section validates, an empty one with no reason fails, and a populated one with a
reason set also fails.

Usage: python tools/test_absent_sections.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import schema
from _testkit import Checker, guard

FIXTURE = HERE / "fixtures" / "synthetic_full.json"

UPLOAD_REASON = "No trailing-twelve statement was uploaded."
ROLL_REASON = "No rent roll was uploaded."


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def absent_t12(reason: str = UPLOAD_REASON) -> dict:
    """The shape ``assemble`` writes when no T-12 came in."""
    return {
        "month_labels": [],
        # The MISSING sentinel since an earlier review: an absent section has no unit count,
        # and zero is a number the per-unit column would have divided by.
        "units": schema.MISSING,
        "rows": [],
        "source_subtotals": {},
        "absent_reason": reason,
    }


def absent_rent_roll(reason: str = ROLL_REASON) -> dict:
    return {
        "as_of": "",
        "lookback_months": 12,
        "new_tenant_tolerance_days": 31,
        "units": [],
        "stated": {},
        "absent_reason": reason,
    }


def main() -> int:
    c = Checker("test_absent_sections")

    c.section("the fixture is unaffected")
    deal = load()
    c.ok("a full deal still validates", not schema.validate(deal),
         "; ".join(schema.validate(deal))[:300])
    c.ok("and carries no absent_reason on t12",
         deal["t12"].get("absent_reason") in (None, ""),
         repr(deal["t12"].get("absent_reason")))

    c.section("an absent T-12 validates when it says why")
    d = load()
    d["t12"] = absent_t12()
    errors = schema.validate(d)
    c.ok("validates", not errors, "; ".join(errors)[:400])

    c.section("an absent rent roll validates when it says why")
    d = load()
    d["rent_roll"] = absent_rent_roll()
    errors = schema.validate(d)
    c.ok("validates", not errors, "; ".join(errors)[:400])

    c.section("both absent at once")
    d = load()
    d["t12"] = absent_t12()
    d["rent_roll"] = absent_rent_roll()
    errors = schema.validate(d)
    c.ok("validates", not errors, "; ".join(errors)[:400])

    c.section("an empty section with NO reason is refused")
    d = load()
    d["t12"] = absent_t12(reason=None)
    errors = schema.validate(d)
    c.ok("t12 with no rows and no reason fails", bool(errors), "validate() was silent")
    c.ok("and the message names t12 and absent_reason",
         any("t12" in e and "absent_reason" in e for e in errors),
         "; ".join(errors)[:300])

    d = load()
    d["rent_roll"] = absent_rent_roll(reason=None)
    errors = schema.validate(d)
    c.ok("rent_roll with no units and no reason fails", bool(errors), "validate() was silent")
    c.ok("and the message names rent_roll and absent_reason",
         any("rent_roll" in e and "absent_reason" in e for e in errors),
         "; ".join(errors)[:300])

    c.section("rows AND a reason together are refused — one or the other")
    d = load()
    d["t12"]["absent_reason"] = "there is no statement"
    errors = schema.validate(d)
    c.ok("a populated t12 with a reason fails", bool(errors), "validate() was silent")
    c.ok("and says one or the other",
         any("one or the other" in e for e in errors), "; ".join(errors)[:300])

    d = load()
    d["rent_roll"]["absent_reason"] = "there is no roll"
    errors = schema.validate(d)
    c.ok("a populated rent_roll with a reason fails", bool(errors), "validate() was silent")

    c.section("an absent section is exempt from its own shape rules, and only those")
    # 12 month labels, a positive unit count and a YYYY-MM-DD as_of are requirements of a
    # section that is THERE. Applying them to a section that is declared absent would make
    # the declaration impossible to express.
    d = load()
    d["t12"] = absent_t12()
    c.ok("no complaint about 0 of 12 month labels",
         not any("month_labels" in e for e in schema.validate(d)),
         "; ".join(schema.validate(d))[:200])
    c.ok("no complaint about its absent unit count",
         not any("t12.units" in e for e in schema.validate(d)),
         "; ".join(schema.validate(d))[:200])

    # ... but a section that is present is still held to all of them.
    d = load()
    d["t12"]["month_labels"] = d["t12"]["month_labels"][:6]
    c.ok("a PRESENT t12 with six months still fails",
         any("month_labels" in e for e in schema.validate(d)),
         "; ".join(schema.validate(d))[:200])

    c.section("a blank string is not a reason")
    d = load()
    d["t12"] = absent_t12(reason="   ")
    errors = schema.validate(d)
    c.ok("whitespace does not satisfy the rule", bool(errors), "validate() was silent")

    c.section("the manifest exposes the reason as a field")
    paths = {f.path for f in schema.MANIFEST_FIELDS if f.path}
    c.ok("t12.absent_reason is in the manifest", "t12.absent_reason" in paths,
         "not listed")
    c.ok("rent_roll.absent_reason is in the manifest",
         "rent_roll.absent_reason" in paths, "not listed")

    c.section("section_absent() is the one place consumers ask")
    d = load()
    c.ok("a full deal reports neither absent",
         not schema.section_absent(d, "t12") and not schema.section_absent(d, "rent_roll"))
    d["t12"] = absent_t12()
    c.equal("an absent t12 reports its reason",
            schema.section_absent(d, "t12"), UPLOAD_REASON)
    c.ok("and the other section is unaffected",
         not schema.section_absent(d, "rent_roll"))
    c.ok("an unknown section is not absent", not schema.section_absent(d, "summary"))

    c.section("MISSING is how 'unknown' is said, and only when the file agrees")
    # Zero USED to mean unknown here. an earlier review replaced that: a count is a positive
    # whole number or the sentinel, because "0 vacant units" read into the wrong field
    # passed the old rule and then divided every per-unit figure in the set.
    d = load()
    d["t12"]["units"] = schema.MISSING
    d["summary"]["units_total"] = schema.MISSING
    d["pricing"]["units_total"] = schema.MISSING
    c.ok("all three absent together is allowed",
         not any("units_total" in e or "t12.units" in e for e in schema.validate(d)),
         "; ".join(schema.validate(d))[:200])
    d = load()
    d["t12"]["units"] = 0
    c.ok("and a zero is refused outright, whatever the summary says",
         any("positive whole number" in e for e in schema.validate(d)),
         "; ".join(schema.validate(d))[:200])
    d = load()
    d["t12"]["units"] = schema.MISSING
    c.ok("an absent count against a stated one still fails",
         any("disagrees with summary.units_total" in e for e in schema.validate(d)),
         "; ".join(schema.validate(d))[:200])
    d = load()
    d["rent_roll"]["units"] = []
    c.ok("and a roll that parsed to nothing without saying why still fails",
         any("rent_roll" in e and "absent_reason" in e for e in schema.validate(d)),
         "; ".join(schema.validate(d))[:250])

    c.section("trade_out keeps working exactly as it did")
    d = load()
    d["trade_out"] = {**d["trade_out"], "rows": [], "absent_reason": None}
    c.ok("empty trade_out with no reason still fails", bool(schema.validate(d)))
    d["trade_out"]["absent_reason"] = "no trade-out report was supplied"
    c.ok("and with a reason still passes", not schema.validate(d),
         "; ".join(schema.validate(d))[:200])

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
