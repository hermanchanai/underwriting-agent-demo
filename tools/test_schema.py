"""Tests for the interchange schema and the field inventory.

Validation is checked by *breaking* a valid fixture one way at a time — a validator that
has only ever seen good input proves nothing. Every rule that exists to stop a real
mistake gets its own negative case: a licensed comp source, a tenant-name column, a
per-unit price outside the band a sale price occupies, a unit count that disagrees with
itself, an unknown T-12 category, an empty trade-out block with no stated reason.

Usage: python tools/test_schema.py
"""

from __future__ import annotations

import copy
import json as _json
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import schema
from _testkit import Checker, guard

#: The pre-2026-09-10 sample (fully synthetic, every bed count covered, a synthetic county
#: bill that ties) — the property checks below need that coverage. The PUBLISHED sample is
#: fixture_deal.json, pinned by test_published_sample.py.
FIXTURE = HERE / "fixtures" / "synthetic_full.json"


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def main() -> int:
    c = Checker("test_schema")
    deal = load()

    # ---- the fixture itself ----------------------------------------------------------
    c.section("fixture validity")
    errors = schema.validate(deal)
    c.ok("fixture validates clean", not errors, "; ".join(errors))
    c.equal("all sections present", sorted(deal.keys()), sorted(schema.SECTIONS))
    c.equal("toolkit name is exact", deal["meta"]["toolkit_name"], schema.TOOLKIT_NAME)

    # ---- category vocabulary (I3) ----------------------------------------------------
    c.section("vendored category vocabulary")
    c.equal("11 revenue categories", len(schema.T12_REVENUE_CATEGORIES), 11)
    c.equal("12 expense categories", len(schema.T12_EXPENSE_CATEGORIES), 12)
    c.ok(
        "revenue order puts GSR after GPR and GLL",
        schema.T12_REVENUE_CATEGORIES.index("Gross Scheduled Rent")
        > schema.T12_REVENUE_CATEGORIES.index("Gain / (Loss) to Lease")
        > schema.T12_REVENUE_CATEGORIES.index("Gross Potential Rent"),
    )
    c.ok(
        "Total Revenue is not assignable to a source line",
        "Total Revenue" not in schema.ASSIGNABLE_CATEGORIES,
    )
    c.equal(
        "no drift in the fixture's categories",
        schema.assert_categories(
            r.get("category") for r in deal["t12"]["rows"] if r.get("category")
        ),
        [],
    )
    c.section("alias normalization does not become a licence to drift")
    c.equal(
        "a known synonym normalizes",
        schema.normalize_category("General and Administrative"),
        "General and Administrative",
    )
    c.equal(
        "spacing variant normalizes",
        schema.normalize_category("Gain / (Loss) to Lease"),
        "Gain / (Loss) to Lease",
    )
    c.equal(
        "an unknown label is reported, not silently dropped",
        schema.assert_categories(["Ancillary Widget Revenue"]),
        ["Ancillary Widget Revenue"],
    )

    # ---- negative cases: each rule earns its keep ------------------------------------
    c.section("validation catches the mistakes it exists for")

    def broken(mutate) -> list[str]:
        d = copy.deepcopy(deal)
        mutate(d)
        return schema.validate(d)

    def has(errs: list[str], needle: str) -> bool:
        return any(needle in e for e in errs)

    errs = broken(lambda d: d["comps"].__setitem__("source", "Licensed comp feed"))
    c.ok("rejects a non-own-scrape comp source", has(errs, "comps.source"), str(errs))

    errs = broken(
        lambda d: d["comps"]["rows"][0].__setitem__("source", "Licensed comp feed")
    )
    c.ok("rejects a licensed comp ROW", has(errs, "comps.rows[0].source"), str(errs))

    errs = broken(
        lambda d: d["rent_roll"]["units"][0].__setitem__("tenant_name", "A. Person")
    )
    c.ok("rejects a tenant-name column (I4)", has(errs, "name-like key"), str(errs))

    errs = broken(lambda d: d["pricing"].__setitem__("price_per_unit", 4200))
    c.ok(
        "rejects a per-unit price below the sale-price band",
        has(errs, "outside the 50,000-600,000 band"),
        str(errs),
    )
    errs = broken(lambda d: d["pricing"].__setitem__("price_per_unit", 38_700_000))
    c.ok(
        "rejects a TOTAL price pasted into the per-unit field",
        has(errs, "outside the 50,000-600,000 band"),
        str(errs),
    )
    c.equal(
        "accepts a price inside the band",
        [
            e
            for e in broken(lambda d: d["pricing"].__setitem__("price_per_unit", 215000))
            if "band" in e
        ],
        [],
    )

    errs = broken(lambda d: d["pricing"].__setitem__("units_total", 179))
    c.ok("catches a unit count that disagrees", has(errs, "disagrees"), str(errs))
    errs = broken(lambda d: d["t12"].__setitem__("units", 200))
    c.ok("catches a T-12 unit count that disagrees", has(errs, "disagrees"), str(errs))

    errs = broken(
        lambda d: d["t12"]["rows"][0].__setitem__("category", "Ancillary Widgets")
    )
    c.ok("rejects an unvendored category", has(errs, "not a vendored category"), str(errs))

    errs = broken(lambda d: d["t12"].__setitem__("month_labels", ["2026-01"]))
    c.ok("requires 12 month labels", has(errs, "expected 12 labels"), str(errs))
    errs = broken(
        lambda d: d["t12"]["month_labels"].__setitem__(0, "July 2025")
    )
    c.ok("requires YYYY-MM month labels", has(errs, "expected YYYY-MM"), str(errs))

    errs = broken(lambda d: d["t12"]["rows"][0]["monthly"].pop())
    c.ok("requires 12 monthly values", has(errs, "expected 12 values"), str(errs))

    errs = broken(
        lambda d: d["trade_out"].update({"rows": [], "absent_reason": None})
    )
    c.ok(
        "an empty trade-out block must say why",
        has(errs, "must say why"),
        str(errs),
    )
    errs = broken(
        lambda d: d["trade_out"].update({"absent_reason": "no report supplied"})
    )
    c.ok(
        "rows and an absent_reason cannot both be set",
        has(errs, "one or the other"),
        str(errs),
    )

    errs = broken(
        lambda d: d["rent_roll"]["units"][1].__setitem__(
            "unit_id", d["rent_roll"]["units"][0]["unit_id"]
        )
    )
    c.ok("catches a duplicate unit id", has(errs, "duplicate"), str(errs))

    errs = broken(
        lambda d: d["rent_roll"]["units"][0].__setitem__("lease_type", "Month-to-Month")
    )
    c.ok(
        "rejects a lease type outside New/Renewal/blank",
        has(errs, "lease_type"),
        str(errs),
    )

    errs = broken(lambda d: d["summary"].pop("county"))
    c.ok("catches a missing required field", has(errs, "missing required key"), str(errs))
    errs = broken(lambda d: d["summary"].__setitem__("surprise_field", 1))
    c.ok("catches an unknown key", has(errs, "unknown key"), str(errs))
    # `units_total` takes `(int, str)` since round 19's ruling 2, so that the count nobody
    # supplied can be the MISSING sentinel instead of a 0 every consumer divides by. A
    # numeral in quotes is still refused — by the sentinel check, which says WHY.
    errs = broken(lambda d: d["summary"].__setitem__("units_total", "180"))
    c.ok("catches a count that arrived as prose",
         has(errs, "a count is a positive whole number or"), str(errs))
    errs = broken(lambda d: d["summary"].__setitem__("units_total", [180]))
    c.ok("catches a wrong type", has(errs, "expected int"), str(errs))
    errs = broken(lambda d: d["summary"].__setitem__("units_total", -4))
    c.ok("catches a negative count",
         has(errs, "a count is a positive whole number or"), str(errs))
    # Zero is the one round 20 added: "0 vacant units" read into the wrong field passed
    # the old check, and assembly then published a count every per-unit figure divides by.
    errs = broken(lambda d: d["summary"].__setitem__("units_total", 0))
    c.ok("catches a zero count",
         has(errs, "a count is a positive whole number or"), str(errs))
    # All THREE together, which is what assembly produces: since round 20 the
    # consistency check sees the sentinel, so one count going absent beside two stated
    # ones is the drift it exists to catch.
    def _all_absent(d):
        d["summary"]["units_total"] = schema.MISSING
        d["pricing"]["units_total"] = schema.MISSING
        d["t12"]["units"] = schema.MISSING

    errs = broken(_all_absent)
    c.equal("and accepts the absent sentinel on all three",
            [e for e in errs if "units_total" in e or "t12.units" in e], [])
    errs = broken(lambda d: d["summary"].__setitem__("units_total", schema.MISSING))
    c.ok("but one of them going absent alone is drift",
         any("disagrees" in e for e in errs), str(errs)[:200])
    errs = broken(lambda d: d.pop("supply"))
    c.ok("catches a missing section", has(errs, "missing required section"), str(errs))
    errs = broken(lambda d: d.__setitem__("extra_section", {}))
    c.ok("catches an unknown section", has(errs, "unknown top-level section"), str(errs))
    errs = broken(lambda d: d["meta"].__setitem__("as_of_date", "06/30/2026"))
    c.ok("catches a non-ISO date", has(errs, "expected YYYY-MM-DD"), str(errs))
    errs = broken(lambda d: d["supply"]["rings"][0].__setitem__("stage", "Maybe someday"))
    c.ok("catches an unknown supply stage", has(errs, "expected one of"), str(errs))
    errs = broken(lambda d: d["demand"]["rings"][0].__setitem__("ring", "7 mi"))
    c.ok("catches an unknown demand ring", has(errs, "expected one of"), str(errs))

    c.section("F2: the month sequence must be a real trailing twelve")
    lab = deal["t12"]["month_labels"]

    def with_months(labels):
        return broken(lambda d: d["t12"].__setitem__("month_labels", list(labels)))

    shuffled = [lab[0], lab[11]] + lab[1:11]
    c.ok(
        "rejects a shuffled month sequence",
        has(with_months(shuffled), "consecutive"),
        str(with_months(shuffled)),
    )
    c.ok(
        "rejects duplicated month labels",
        has(with_months([lab[0]] * 12), "consecutive"),
        str(with_months([lab[0]] * 12)),
    )
    gap = lab[:6] + ["2026-08"] + lab[7:]
    c.ok("rejects a gap in the sequence", has(with_months(gap), "consecutive"), str(with_months(gap)))
    c.equal(
        "accepts the ascending fixture sequence",
        [e for e in schema.validate(deal) if "consecutive" in e],
        [],
    )
    c.equal(
        "accepts a strictly descending sequence",
        [e for e in with_months(list(reversed(lab))) if "consecutive" in e],
        [],
    )

    c.section("F5: source subtotals must be numeric-or-null under a known key")
    errs = broken(
        lambda d: d["t12"]["source_subtotals"].__setitem__("Total Revenue", "3,814,904")
    )
    c.ok("rejects a formatted-string subtotal", has(errs, "source_subtotals"), str(errs))
    errs = broken(
        lambda d: d["t12"]["source_subtotals"].__setitem__("Total Revenue", schema.MISSING)
    )
    c.ok("rejects a sentinel subtotal", has(errs, "source_subtotals"), str(errs))
    errs = broken(
        lambda d: d["t12"]["source_subtotals"].__setitem__("Total Revenu", 1.0)
    )
    c.ok(
        "rejects a typo'd key that no cross-tie would ever read",
        has(errs, "source_subtotals"),
        str(errs),
    )
    c.equal(
        "allows an explicit null subtotal",
        [
            e
            for e in broken(
                lambda d: d["t12"]["source_subtotals"].__setitem__("Utilities", None)
            )
            if "source_subtotals" in e
        ],
        [],
    )

    c.section("F4: a source string may not begin with '=' (it would become a live formula)")
    for path, mutate in (
        ("rent_roll.units[0].unit_id",
         lambda d: d["rent_roll"]["units"][0].__setitem__("unit_id", '=HYPERLINK("http://x","c")')),
        ("t12.rows[0].label",
         lambda d: d["t12"]["rows"][0].__setitem__("label", "=1+1")),
        ("comps.rows[0].community",
         lambda d: d["comps"]["rows"][0].__setitem__("community", "=cmd|'/c calc'!A")),
        ("summary.notes",
         lambda d: d["summary"].__setitem__("notes", "=SUM(A1)")),
    ):
        errs = broken(mutate)
        c.ok(f"rejects a leading '=' in {path}", has(errs, "begins with"), str(errs))
    c.equal(
        "an ordinary label is unaffected",
        [e for e in schema.validate(deal) if "begins with" in e],
        [],
    )

    c.section("comps / demand / supply may be absent, but must say why")
    for section, row_key in (("comps", "rows"), ("demand", "rings"), ("supply", "rings")):
        errs = broken(
            lambda d, sec=section, rk=row_key: d[sec].update({rk: [], "absent_reason": None})
        )
        c.ok(
            f"an empty {section} block with no reason is rejected",
            has(errs, "must say why"),
            str(errs),
        )
        c.equal(
            f"an empty {section} block WITH a reason is accepted",
            [
                e
                for e in broken(
                    lambda d, sec=section, rk=row_key: d[sec].update(
                        {rk: [], "absent_reason": "no coverage for this submarket"}
                    )
                )
                if section in e
            ],
            [],
        )
        errs = broken(
            lambda d, sec=section: d[sec].__setitem__("absent_reason", "contradictory")
        )
        c.ok(
            f"{section} rows plus a reason is contradictory",
            has(errs, "one or the other"),
            str(errs),
        )

    c.section("N2: generated_on must be a date a ZIP archive can carry")
    for bad in ("1900-01-01", "1979-12-31", "2200-06-30"):
        errs = broken(lambda d, b=bad: d["meta"].__setitem__("generated_on", b))
        c.ok(
            f"rejects generated_on={bad}",
            has(errs, "generated_on"),
            str(errs),
        )
    for good in ("1980-01-01", "2026-09-01", "2107-12-31"):
        c.equal(
            f"accepts generated_on={good}",
            [
                e
                for e in broken(lambda d, g=good: d["meta"].__setitem__("generated_on", g))
                if "generated_on" in e
            ],
            [],
        )

    c.section("F7: the comp subject rent is the modeled market rent")
    errs = broken(lambda d: d["comps"].__setitem__("subject_market_rent", 1795))
    c.ok(
        "rejects a subject rent that is not the modeled market rent",
        has(errs, "subject_market_rent"),
        str(errs),
    )
    c.equal(
        "fixture's subject rent agrees with summary.market_rent_for_model",
        [e for e in schema.validate(deal) if "subject_market_rent" in e],
        [],
    )

    c.section("closing costs: verified rates only, and a citation even for zero")
    import closing_costs as cc

    c.ok("eight states are vendored", len(cc.states_covered()) == 8, str(cc.states_covered()))
    for st in cc.states_covered():
        block = cc.for_state(st, 60_120_000, loan_amount=39_078_000)
        for line in ("transfer_tax", "mortgage_tax"):
            b = block[line]
            c.ok(
                f"{st} {line} cites an authority",
                bool(str(b["citation"]).strip()) and not str(b["citation"]).startswith("Not asserted"),
                str(b["citation"]),
            )
            c.ok(
                f"{st} {line} zero-when-not-applicable",
                b["applies"] or b["amount"] == 0,
                f"applies={b['applies']} amount={b['amount']}",
            )
    c.section("a state outside the table is NOT ASSERTED, never zero")
    unknown = cc.for_state("ZZ", 1_000_000)
    c.ok(
        "unknown state reports not-asserted",
        unknown["transfer_tax"]["citation"].startswith("Not asserted"),
        unknown["transfer_tax"]["citation"],
    )
    c.ok("and does not claim it applies", unknown["transfer_tax"]["applies"] is False)

    c.section("the rate mechanics that actually bite")
    c.close("AZ transfer is zero", cc.for_state("AZ", 60_120_000)["transfer_tax"]["amount"], 0.0)
    c.close(
        "FL deed stamps at 70c per $100",
        cc.for_state("FL", 1_000_000)["transfer_tax"]["amount"], 7_000.0, 1.0,
    )
    c.close(
        "GA intangible is capped at $25,000",
        cc.for_state("GA", 60_120_000, loan_amount=50_000_000, maturity_months=120)["mortgage_tax"]["amount"],
        25_000.0, 0.01,
    )
    c.equal(
        "GA intangible does not attach at 60 months (2025 rule)",
        cc.for_state("GA", 60_120_000, loan_amount=50_000_000, maturity_months=60)["mortgage_tax"]["applies"],
        False,
    )
    c.ok(
        "GA intangible does attach at 84 months",
        cc.for_state("GA", 60_120_000, loan_amount=50_000_000, maturity_months=84)["mortgage_tax"]["applies"],
    )
    c.close(
        "TN indebtedness exempts the first $2,000",
        cc.for_state("TN", 1_000_000, loan_amount=102_000)["mortgage_tax"]["amount"],
        115.0, 0.5,
    )
    c.ok(
        "per-unit statutes round the consideration UP",
        cc.for_state("NC", 501)["transfer_tax"]["amount"] == 2.0,
        str(cc.for_state("NC", 501)["transfer_tax"]["amount"]),
    )

    c.section("schema refuses a closing-cost line with no citation")
    errs = broken(lambda d: d["closing_costs"]["transfer_tax"].__setitem__("citation", ""))
    c.ok("uncited rate rejected", has(errs, "citation"), str(errs))
    errs = broken(lambda d: d["closing_costs"]["transfer_tax"].update({"applies": False, "amount": 1234}))
    c.ok("a tax that does not apply must be zero", has(errs, "does not apply is zero"), str(errs))

    c.section("R#1: a mortgage tax needs debt terms, not a price")
    no_loan = cc.for_state("FL", 60_120_000)
    c.ok(
        "no loan amount -> mortgage tax NOT ASSERTED",
        not no_loan["mortgage_tax"]["applies"]
        and "not asserted" in no_loan["mortgage_tax"]["citation"].lower(),
        f"applies={no_loan['mortgage_tax']['applies']} amount={no_loan['mortgage_tax']['amount']} "
        f"cite={no_loan['mortgage_tax']['citation'][:60]}",
    )
    c.equal("and reports zero rather than a full-price figure", no_loan["mortgage_tax"]["amount"], 0)
    c.ok(
        "the transfer tax still computes without a loan",
        cc.for_state("FL", 60_120_000)["transfer_tax"]["applies"],
    )
    with_loan = cc.for_state("FL", 60_120_000, loan_amount=39_078_000)
    c.ok("with a loan it computes", with_loan["mortgage_tax"]["applies"])
    c.close(
        "and on the LOAN, not the price",
        with_loan["mortgage_tax"]["amount"], 39_078_000 * 0.0055, 1.0,
    )
    for st in ("GA", "TN", "AL"):
        c.ok(
            f"{st} also refuses to assert without debt terms",
            not cc.for_state(st, 60_120_000)["mortgage_tax"]["applies"],
        )

    c.section("R#6: the closing block must belong to this deal")
    errs = broken(lambda d: d["closing_costs"].__setitem__("state", "FL"))
    c.ok("a state that disagrees with the summary is rejected", has(errs, "closing_costs.state"), str(errs))
    errs = broken(lambda d: d["closing_costs"].__setitem__("price", 999))
    c.ok(
        "a price that is not price_per_unit x units_total is rejected",
        has(errs, "closing_costs.price"),
        str(errs),
    )

    c.section("C#1: amount status is a FIELD, not a substring of the citation")
    fl_noloan = cc.for_state("FL", 60_120_000)
    c.equal("FL with no loan -> not_asserted", fl_noloan["mortgage_tax"]["amount_status"], "not_asserted")
    fl_loan = cc.for_state("FL", 60_120_000, loan_amount=39_078_000)
    c.equal("FL with a loan -> asserted", fl_loan["mortgage_tax"]["amount_status"], "asserted")
    az = cc.for_state("AZ", 60_120_000)
    c.equal("AZ mortgage -> zero_by_law", az["mortgage_tax"]["amount_status"], "zero_by_law")
    c.equal("AZ transfer -> zero_by_law", az["transfer_tax"]["amount_status"], "zero_by_law")
    c.equal("FL transfer with a price -> asserted", fl_noloan["transfer_tax"]["amount_status"], "asserted")
    unknown_st = cc.for_state("ZZ", 1_000_000)
    c.equal("an unvendored state -> not_asserted", unknown_st["transfer_tax"]["amount_status"], "not_asserted")
    c.ok(
        "the status does not depend on how the citation reads",
        "Not asserted" not in fl_noloan["mortgage_tax"]["citation"][:12],
        "the statute comes first now; a substring test would call this asserted",
    )
    for st_name in cc.states_covered():
        for line in ("transfer_tax", "mortgage_tax"):
            b = cc.for_state(st_name, 60_120_000)[line]
            c.ok(
                f"{st_name} {line}: a non-asserted amount is zero",
                b["amount_status"] == "asserted" or b["amount"] == 0,
                f"{b['amount_status']} / {b['amount']}",
            )
    errs = broken(lambda d: d["closing_costs"]["transfer_tax"].__setitem__("amount_status", "probably"))
    c.ok("schema rejects an unknown amount_status", has(errs, "amount_status"), str(errs))
    errs = broken(lambda d: d["closing_costs"]["transfer_tax"].update(
        {"amount_status": "not_asserted", "amount": 1234}))
    c.ok("a not-asserted amount must be zero", has(errs, "amount_status"), str(errs))

    c.section("RR#2: a not-asserted mortgage tax keeps the state's authority")
    fl = cc.for_state("FL", 60_120_000)
    c.ok(
        "citation still names the statute",
        "201.08" in fl["mortgage_tax"]["citation"] or "199.133" in fl["mortgage_tax"]["citation"],
        fl["mortgage_tax"]["citation"],
    )
    c.ok(
        "and still says the amount is not asserted",
        "not asserted" in fl["mortgage_tax"]["citation"].lower()
        or "not asserted" in str(fl["mortgage_tax"]["rate_label"]).lower(),
        f"{fl['mortgage_tax']['rate_label']} | {fl['mortgage_tax']['citation']}",
    )
    c.equal("amount is zero", fl["mortgage_tax"]["amount"], 0)
    c.equal("and applies is False", fl["mortgage_tax"]["applies"], False)
    az = cc.for_state("AZ", 60_120_000)
    c.ok(
        "a state with no such tax keeps its constitutional cite and does not claim not-asserted",
        "art. IX" in az["mortgage_tax"]["citation"]
        and "not asserted" not in az["mortgage_tax"]["citation"].lower(),
        az["mortgage_tax"]["citation"],
    )

    c.section("RR#1: the tax cross-check degrades to NOT CHECKED, never to an error")
    import compute as _cmp

    ok_state = _cmp.tax_check(deal)
    c.equal("a complete deal ties", ok_state["status"], "ties")
    for mutate, label in (
        (lambda d: d["summary"].__setitem__("tax_rate_as_billed", schema.MISSING), "missing rate"),
        (lambda d: d["summary"].__setitem__("assessed_value_basis", schema.MISSING), "missing basis"),
        (lambda d: d["summary"].__setitem__("tax_billed_amount", schema.MISSING), "missing billed"),
        (lambda d: d["summary"].__setitem__("tax_billed_amount", 0), "zero billed"),
    ):
        d2 = copy.deepcopy(deal)
        mutate(d2)
        st = _cmp.tax_check(d2)
        c.equal(f"{label} -> NOT CHECKED", st["status"], "NOT CHECKED")
        c.equal(f"{label} -> no difference claimed", st["difference"], None)

    c.section("R16 F3: the assessment ratio is applied exactly once")
    import compute as _cm

    for state, ratio in (("GA", 0.40), ("TX", 0.25), ("NV", 0.35)):
        reset = copy.deepcopy(deal)
        reset["summary"]["state"] = state
        reset["summary"]["assessment_ratio"] = ratio
        tu = _cm.tax_underwriting(reset)
        c.ok(f"{state} resets on sale", tu["resets_on_sale"] is True)
        c.close(
            f"{state} @ {ratio:.0%}: assumed assessed == price x ratio (not ratio squared)",
            tu["assumed_assessed"], tu["price"] * ratio, 1.0,
        )
        c.close(
            f"{state}: the generic method is not reported as overstating itself",
            tu["generic_overstatement"], 1.0, 0.001,
        )
    az = _cm.tax_underwriting(deal)
    if az["resets_on_sale"] is False:
        c.close("AZ year-one basis is the LPV grown at the cap, a FULL value",
                az["assumed_basis"], az["current_basis"] * (1 + _cm.AZ_LPV_CAP), 1.0)
        c.close("AZ assumed assessed applies the ratio once",
                az["assumed_assessed"], az["assumed_basis"] * az["ratio"], 1.0)
        c.ok("and AZ still shows the generic method overstating", az["generic_overstatement"] > 2)

    c.section("R16 F5: the exemption is applied on every tax surface (I6)")
    ex = copy.deepcopy(deal)
    ex["summary"]["tax_exemption"] = 50_000
    c.close(
        "tax_underwriting agrees with summary_derived",
        _cm.tax_underwriting(ex)["current_computed"],
        _cm.summary_derived(ex)["computed_annual_tax"],
        0.01,
    )
    c.ok(
        "and the exemption actually moved the number",
        _cm.tax_underwriting(ex)["current_computed"]
        < _cm.tax_underwriting(deal)["current_computed"] - 49_000,
    )

    c.section("R16 F6: a missing tax divisor is never invented")
    for bad in (schema.MISSING, None, 0):
        nod = copy.deepcopy(deal)
        nod["summary"]["tax_divisor"] = bad
        tu = _cm.tax_underwriting(nod)
        c.ok(f"divisor {bad!r} -> no computed tax", tu["current_computed"] is None)
        c.ok(f"divisor {bad!r} -> no year-one tax", tu["assumed_tax"] is None)
        c.equal(f"divisor {bad!r} -> divisor reported as None", tu["divisor"], None)

    c.section("R16 F2: standard notes assert nothing about any particular deal")
    import notes as _nt

    # A figure, a vintage or a spread in a STANDARD note is a claim about whichever deal it
    # came from, republished on every other deal. The fixture's Notes tab was asserting the
    # showcase deal's 1981 vintage and its spreads.
    import re as _re

    for i, n in enumerate(_nt.STANDARD):
        body = n["note"]
        years = _re.findall(r"\b(?:18|19|20)\d{2}\b", body)
        c.equal(f"standard note {i} quotes no year", years, [])
        pcts = _re.findall(r"\d+(?:\.\d+)?\s?%", body)
        c.equal(f"standard note {i} quotes no percentage", pcts, [])
        money = _re.findall(r"\$[\d,]+", body)
        c.equal(f"standard note {i} quotes no dollar figure", money, [])

    c.section("R16 F2: each set's derived notes describe ITS OWN subject")
    # `deal` here IS the fixture. The showcase set is a build output (gitignored), so it is
    # compared only when present — and when it is, the two vintages must differ or this
    # check cannot fail and proves nothing.
    fx_notes = deal["meta"]["notes"]
    fx_year = str(deal["summary"]["year_built"])
    c.ok(
        "the fixture's derived vintage note quotes the FIXTURE's own year",
        any(fx_year in n["note"] for n in fx_notes),
        f"expected {fx_year} somewhere in the notes",
    )
    show_path = HERE.parent / "outputs" / "showcase" / "deal_outputs.json"
    if show_path.exists():
        show = _json.loads(show_path.read_text(encoding="utf-8"))
        show_year = str(show["summary"]["year_built"])
        c.ok(
            "the two sets have different vintages, so this check CAN fail",
            show_year != fx_year, f"showcase {show_year} vs fixture {fx_year}",
        )
        if show_year != fx_year:
            c.ok(
                "no fixture note quotes the showcase deal's year",
                not any(show_year in n["note"] for n in fx_notes),
                str([n["topic"] for n in fx_notes if show_year in n["note"]]),
            )
            c.ok(
                "no showcase note quotes the fixture's year",
                not any(fx_year in n["note"] for n in show["meta"]["notes"]),
                str([n["topic"] for n in show["meta"]["notes"] if fx_year in n["note"]]),
            )
        c.ok(
            "the showcase's derived note quotes its own year",
            any(show_year in n["note"] for n in show["meta"]["notes"]),
        )
    # A derived note may only exist when the condition it describes is true of that deal.
    below = [n for n in fx_notes if "BELOW in-place" in n["topic"]]
    fx_tot = _cm.roll_totals(deal)
    if fx_tot["wavg_market_rent_for_model"] >= fx_tot["wavg_in_place_rent"]:
        c.equal("the fixture does not claim market rent is below in-place", below, [])

    c.section("R17 F2: every detailed comp year unknown must not crash the notes")
    import notes as _nt2

    noyear = copy.deepcopy(deal)
    for r in noyear["comps"]["rows"]:
        r["year_built"] = None
        r["year_built_source"] = None
    # The tiering contract explicitly allows this, and a nearby community may still carry a
    # year — which is what made `years` empty while `nearby_years` was not.
    got = _nt2.derived_for(noyear)
    c.ok("derived_for survives an all-unknown detailed set", isinstance(got, list))
    c.ok(
        "and simply omits the vintage note rather than guessing a range",
        not any("vintage-matching" in n["topic"] for n in got),
        str([n["topic"] for n in got]),
    )

    c.section("R17 F3: the vintage note compares vintage, and gets the sign right")
    cm2 = _cm.comps(deal)
    if cm2.get("subject_vs_unrestricted_spread") is not None:
        # NOT "more communities" — that was the confounded design, where the baseline also
        # varied the distance cap. The counterfactual holds the cap fixed, so:
        #   * at the cap, both sides hold the SAME count and differ only in WHICH
        #     communities fill it (one real case: 20 vs 20, six swapped);
        #   * below the cap, removing the vintage test genuinely admits more (the fixture
        #     case: only twelve communities are eligible at all).
        # Both are correct; asserting equality unconditionally would have been wrong.
        cap = (deal["comps"]["tiering"]["top_n"]
               + deal["comps"]["tiering"]["additional_n"])
        n_matched = len(cm2["detailed_communities"])
        n_unr = cm2["unrestricted_communities"] or 0
        c.ok("the counterfactual never exceeds the cap", n_unr <= cap, f"{n_unr} vs {cap}")
        c.ok("and never holds fewer than the matched set", n_unr >= n_matched,
             f"{n_unr} vs {n_matched}")
        if n_matched >= cap:
            c.equal("at the cap, both sides hold the same count", n_unr, n_matched)
        c.ok(
            "but reaches a WIDER vintage span, which is the variable under test",
            (cm2["unrestricted_year_max"] or 0) >= max(
                r["year_built"] for r in cm2["rows"] if r.get("year_built")
            ),
        )
        c.ok(
            "the two spreads are genuinely different populations, not mix-vs-flat",
            abs(cm2["subject_vs_compset_spread"] - cm2["subject_vs_unrestricted_spread"])
            > 1e-9,
        )
        # Orientation: both are subject / comp - 1, so positive means the SUBJECT is above.
        for key, comp_key, subj_key in (
            ("subject_vs_compset_spread", "compset_mix_matched_net_effective",
             "subject_market_rent_matched"),
            ("subject_vs_unrestricted_spread", "unrestricted_mix_matched",
             "unrestricted_subject_matched"),
        ):
            spread, comp_v, subj_v = cm2[key], cm2[comp_key], cm2[subj_key]
            c.ok(
                f"{key} is positive exactly when the subject is above the comp set",
                (spread >= 0) == (subj_v >= comp_v),
                f"spread {spread:+.4f}, subject {subj_v:.2f}, comp {comp_v:.2f}",
            )
        note = [n for n in _nt2.derived_for(deal) if "vintage-matching" in n["topic"]]
        c.ok("the note is emitted", bool(note))
        if note:
            body = note[0]["note"]
            above = "above" if cm2["subject_vs_compset_spread"] >= 0 else "below"
            c.ok(
                f"and states the matched figure as '{above}' the comp set",
                f"{abs(cm2['subject_vs_compset_spread']):.1%} {above}" in body,
                body[:150],
            )
            c.ok(
                "it states the counterfactual's vintage span, so the swap is visible",
                str(cm2["unrestricted_year_max"]) in body,
            )
            c.ok(
                "it names how many communities swapped in",
                str(len(cm2["unrestricted_swapped_in"])) in body,
            )
            c.ok(
                "and no longer claims a flat row average is the unrestricted population",
                "flat average" not in body,
            )
            c.ok(
                "it says every other criterion was held fixed",
                "identical criteria" in body or "same product test" in body,
                body[:160],
            )
            c.ok(
                "and reports the gap in percentage POINTS, not as a fraction",
                "points" in body,
            )
            # The note is a MEASUREMENT of two named sets, not a causal market claim. This
            # framing was a deliberate decision after three consecutive rounds found
            # defects in the machinery supporting the causal version, each moving the
            # number.
            c.ok("both sets are labelled", "Set A" in body and "Set B" in body, body[:120])
            c.ok(
                "it disclaims being a market-level estimate",
                "not an estimate of what vintage is worth" in body,
                body[-160:],
            )
            for banned in ("the vintage effect is", "vintage effect:", "the vintage spread"):
                c.ok(f"no causal phrasing: {banned!r}", banned not in body.lower())
            c.ok(
                "it states the direction observationally, derived from the actual swap",
                "set B reaches" in body or "no vintage shift" in body
                or "cannot be stated" in body,
                body[-200:],
            )
            c.ok(
                "and NOT with the old templated sentence, which was false on the fixture",
                "the newer communities nearby rent above" not in body,
            )
            c.ok("and points the reader at both sets", "Rent Comps section" in body)

    c.section("R19: STANDARD's vintage note makes no market-level claim either")
    std = next(
        (n for n in _nt2.STANDARD if "vintage" in n["topic"].lower()), None
    )
    c.ok("the standard vintage note exists", std is not None)
    if std:
        for banned in ("wearing a rent label", "the more of it is"):
            c.ok(f"standard note drops {banned!r}", banned not in std["note"])
        c.ok(
            "and says the magnitude is submarket-specific rather than a rule",
            "not a rule" in std["note"],
            std["note"][-120:],
        )

    c.section("R17 F6: a non-positive tax divisor is rejected at VALIDATION")
    for bad in (0, -100):
        badd = copy.deepcopy(deal)
        badd["summary"]["tax_divisor"] = bad
        errs = [e for e in schema.validate(badd) if "tax_divisor" in e]
        c.ok(f"divisor {bad} is a validation error", bool(errs), str(errs))
        # And every surface agrees, rather than one showing MISSING and another a negative.
        c.equal(f"divisor {bad}: summary_derived refuses",
                _cm.summary_derived(badd)["computed_annual_tax"], schema.MISSING)
        c.equal(f"divisor {bad}: tax_underwriting refuses",
                _cm.tax_underwriting(badd)["current_computed"], None)
    neg_ex = copy.deepcopy(deal)
    neg_ex["summary"]["tax_exemption"] = -5000
    c.ok(
        "a negative exemption is rejected too",
        any("tax_exemption" in e for e in schema.validate(neg_ex)),
    )

    c.section("R17 F5: the year-one basis fields are labelled for what they hold")
    for state, ratio in (("GA", 0.40), ("AZ", 0.10)):
        x = copy.deepcopy(deal)
        x["summary"]["state"] = state
        x["summary"]["assessment_ratio"] = ratio
        tu = _cm.tax_underwriting(x)
        c.ok(
            f"{state}: full value and assessed value are distinct fields",
            "assumed_full_value" in tu and "assumed_assessed" in tu,
        )
        c.close(
            f"{state}: assessed == full value x ratio",
            tu["assumed_assessed"], tu["assumed_full_value"] * ratio, 1.0,
        )
        c.ok(
            f"{state}: the full-value label does not describe the assessed value",
            "x assessment ratio" not in (tu["assumed_full_value_label"] or ""),
            tu["assumed_full_value_label"],
        )

    c.section("R18 F1: the counterfactual varies VINTAGE and nothing else")
    cm3 = _cm.comps(deal)
    unr = deal["comps"].get("unrestricted") or {}
    tiering3 = deal["comps"]["tiering"]
    c.equal("the block declares what it varied", unr.get("varied"), "vintage")
    for criterion in ("product test", "radius", "mix weighting"):
        c.ok(f"it declares '{criterion}' held fixed",
             criterion in (unr.get("held_fixed") or []), str(unr.get("held_fixed")))
    # Same cap on both sides: a counterfactual with a different population SIZE is varying
    # the distance cap too, which is what the confounded version did.
    cap3 = tiering3["top_n"] + tiering3["additional_n"]
    n_m = len(cm3["detailed_communities"])
    if n_m >= cap3:
        # At the cap the swap must be symmetric: one community in for one out. That is the
        # signature of a held-fixed cap, and its absence is how the confound crept in.
        c.equal(
            "at the cap, the swap is symmetric — one in for one out",
            len(cm3["unrestricted_swapped_in"]),
            len(cm3["unrestricted_swapped_out"]),
        )
    c.ok(
        "the counterfactual swaps at least one community, or it measures nothing",
        len(cm3["unrestricted_swapped_in"]) > 0,
        str(cm3["unrestricted_swapped_in"]),
    )
    c.ok(
        "every swapped-in community is one the vintage band excluded",
        all(
            n not in cm3["detailed_communities"]
            for n in cm3["unrestricted_swapped_in"]
        ),
    )

    c.section("R18 F2: the vintage contrast uses the SAME bed counts on both sides")
    k = cm3["vintage_contrast"]
    if k.get("spread_matched") is not None:
        beds_in_matched = {int(e["beds"]) for e in cm3["by_bed"]}
        beds_in_unr = {int(e["beds"]) for e in unr["by_bed"]}
        for b in k["beds"]:
            c.ok(f"bed count {b} is present in BOTH populations",
                 b in beds_in_matched and b in beds_in_unr)
        c.ok("the contrast reports its own coverage", k["coverage"] is not None)
        c.ok(
            "and that coverage clears the floor",
            (k["coverage"] or 0) >= _cm.MIX_COVERAGE_FLOOR,
            str(k["coverage"]),
        )
        # Both spreads share a subject numerator, so they differ only by the comp side.
        c.close(
            "matched spread is consistent with its own comp figure",
            k["spread_matched"], k["subject"] / k["matched"] - 1, 1e-9,
        )
        c.close(
            "vintage-off spread is consistent with its own comp figure",
            k["spread_unrestricted"], k["subject"] / k["unrestricted"] - 1, 1e-9,
        )
    # Divergent bed coverage must SUPPRESS rather than publish.
    skew = copy.deepcopy(deal)
    if skew["comps"].get("unrestricted", {}).get("by_bed"):
        skew["comps"]["unrestricted"]["by_bed"] = [
            e for e in skew["comps"]["unrestricted"]["by_bed"] if int(e["beds"]) == 1
        ]
        sk = _cm.comps(skew)["vintage_contrast"]
        c.ok(
            "dropping bed counts from one side withholds the contrast",
            sk["withheld_reason"] is not None,
            f"published {sk.get('spread_matched')!r} over beds {sk.get('beds')}",
        )
        import notes as _nt3
        topics = [n["topic"] for n in _nt3.derived_for(skew)]
        c.ok(
            "and the note says it could not be isolated rather than asserting a figure",
            not any("What vintage-matching changed" in x for x in topics),
            str(topics),
        )

    c.section("R18 F3: the counterfactual block is validated, not discovered downstream")
    for mutate, needle in (
        (lambda d: d["comps"]["unrestricted"].__setitem__("by_bed", "nope"), "by_bed"),
        (lambda d: d["comps"]["unrestricted"]["by_bed"][0].__setitem__("beds", 1.5), "whole number"),
        (lambda d: d["comps"]["unrestricted"].__setitem__("varied", "distance"), "varied"),
        (lambda d: d["comps"]["unrestricted"].__setitem__("year_min", 2099), "year_min"),
        (lambda d: d["comps"]["unrestricted"]["by_bed"][0].pop("avg_net_effective"), "avg_net_effective"),
    ):
        bad = copy.deepcopy(deal)
        try:
            mutate(bad)
        except (KeyError, IndexError):
            continue
        errs = [e for e in schema.validate(bad) if "unrestricted" in e]
        c.ok(f"malformed unrestricted block ({needle}) fails validation",
             any(needle in e for e in errs), str(errs)[:150])
    dup = copy.deepcopy(deal)
    if len(dup["comps"].get("unrestricted", {}).get("by_bed", [])) >= 2:
        dup["comps"]["unrestricted"]["by_bed"][1]["beds"] = dup["comps"]["unrestricted"]["by_bed"][0]["beds"]
        c.ok(
            "a duplicated bed count fails validation",
            any("more than once" in e for e in schema.validate(dup)),
        )

    c.section("R20 C#3: the comp-set narrative is derived, not templated")
    note = next((n["note"] for n in deal["meta"]["notes"] if "Two comp sets" in n["note"]), "")
    c.ok("the fixture emits the two-set note", bool(note))
    if note:
        u = deal["comps"]["unrestricted"]
        n_in, n_out = len(u["swapped_in"]), len(u["swapped_out"])
        # The wording must MATCH this deal's own swap. On the fixture the cap does not
        # bind, so nothing is "replaced" — one community is added and none removed, and
        # the templated sentence said otherwise.
        if n_in and not n_out:
            c.ok("a cap that does not bind is not described as a replacement",
                 "replaced" not in note, note[:160])
            c.ok("and it says the cap never binds", "cap never binds" in note)
        if n_in and n_out and n_in == n_out:
            c.ok("a binding cap IS described as a replacement", "replaced by" in note)
        # Direction must be measured against set A's own range, not assumed "newer".
        years = [r["year_built"] for r in deal["comps"]["rows"] if r.get("year_built")]
        nearby_year = {n["community"]: n.get("year_built")
                       for n in deal["comps"]["nearby"]}
        in_years = [y for y in (nearby_year.get(x) for x in u["swapped_in"])
                    if isinstance(y, int)]
        if in_years and years:
            if all(y < min(years) for y in in_years):
                c.ok("older newcomers are described as OLDER",
                     "OLDER product, not newer" in note, note[-200:])
                c.ok("and are not called newer", "newer product than" not in note)
            elif all(y > max(years) for y in in_years):
                c.ok("newer newcomers are described as newer",
                     "newer product than" in note)
        c.ok("no leftover templated direction sentence",
             "the newer communities nearby rent above" not in note)

    show_p = HERE.parent / "outputs" / "showcase" / "deal_outputs.json"
    if show_p.exists():
        sd = _json.loads(show_p.read_text(encoding="utf-8"))
        sn = next((n["note"] for n in sd["meta"]["notes"] if "Two comp sets" in n["note"]), "")
        su = sd["comps"]["unrestricted"]
        c.ok("the two sets' narratives are NOT identical boilerplate", sn != note)
        if len(su["swapped_in"]) == len(su["swapped_out"]) and su["swapped_in"]:
            c.ok("the showcase (binding cap) says replaced", "replaced by" in sn)
        if len(u["swapped_in"]) and not len(u["swapped_out"]):
            c.ok("and the fixture (non-binding cap) does not", "replaced" not in note)

    c.section("R20 #2: tie-outs run against the SOURCE's own stated totals")
    rrc = _cm.rent_roll_checks(deal)
    c.ok("the checks report a summary", bool(rrc["summary"]))
    show_stated = None
    if show_p.exists():
        show_stated = (sd["rent_roll"].get("stated") or {})
        c.ok("the showcase source states per-status buckets",
             bool(show_stated.get("buckets")), str(show_stated.get("buckets"))[:90])
        c.ok("physical occupancy is DERIVED from those buckets, not read off one",
             show_stated.get("occupied_basis") is not None
             and "+" in str(show_stated.get("occupied_basis")),
             str(show_stated.get("occupied_basis")))
        # The source labels one bucket "Occupied"; using it alone would understate.
        b = show_stated["buckets"]
        c.ok("the derived figure exceeds the source's own 'Occupied' bucket",
             (show_stated["occupied_units"] or 0) > b.get("Occupied", 0),
             f"{show_stated['occupied_units']} vs bucket {b.get('Occupied')}")
        srrc = _cm.rent_roll_checks(sd)
        scored = [r for r in srrc["rows"] if r["verdict"] != _cm.INFO]
        c.ok("every showcase tie-out is scored, none NOT CHECKED",
             all(r["verdict"] != _cm.NOT_CHECKED for r in scored),
             str([(r["item"], r["verdict"]) for r in scored]))
        c.ok("and they all tie", all(r["verdict"] == _cm.OK for r in scored),
             str([(r["item"], r["verdict"]) for r in scored]))
        # NOT "validates the de-duplication" — it does not, and this project claimed it
        # did. On this export both rows of every turnover pair carry the same market rent,
        # so the total is identical under either rule. It validates row COMPLETENESS.
        c.ok("the market-rent total ties (row completeness)",
             any(r["item"].startswith("Market rent") and r["verdict"] == _cm.OK
                 for r in srrc["rows"]))
        mk = next(r for r in srrc["rows"] if r["item"].startswith("Market rent"))
        c.ok("and its note does NOT claim to validate the row choice",
             "does NOT validate which row" in (mk["note"] or ""), mk["note"][:120])

    c.section("R21 C#1: a check that IS sensitive to the de-duplication choice")
    import copy as _cp

    # Runs off the TRACKED fixture, not a gitignored build output. The earlier version was
    # guarded on outputs/showcase/deal_outputs.json existing, so a fresh clone skipped its
    # core assertion in silence and exited 0 — the monitors-that-cannot-tell-the-truth
    # shape, in the very test written to stop one.
    st = deal["rent_roll"]["stated"]
    c.ok("the fixture carries duplicate-row detail", bool(st.get("duplicate_detail")))
    c.ok("and the lease status that identifies the in-place row",
         st.get("in_place_lease_status") == "Current")
    shapes = {d["unit_id"] for d in st["duplicate_detail"]}
    for shape, why in (
        ("D-0ZERO", "an incoming lease STATING $0 — the shape that halved the exposure"),
        ("D-LOWER", "an ordinary lower incoming lease"),
        ("D-NOCUR", "no in-place row at all — keeping the other is CORRECT"),
    ):
        c.ok(f"fixture covers {shape}: {why}", shape in shapes, str(sorted(shapes)))

    base = _cm.rent_roll_checks(deal)
    dp = next(r for r in base["rows"] if r["item"].startswith("Duplicate rows"))
    mk = next(r for r in base["rows"] if r["item"].startswith("Market rent"))
    c.equal("with the correct dedup the row-choice check is OK", dp["verdict"], _cm.OK)
    c.ok("a duplicate with NO in-place row is not reported as wrong",
         "had none" in (dp["note"] or ""), dp["note"][:140])

    # The exposure must count a STATED $0 as a value. Pinned as a literal.
    import re as _re3
    m = _re3.search(r"by \$([\d,]+)/mo", dp["note"] or "")
    c.ok("the note publishes an exposure", m is not None, dp["note"][:140])
    if m:
        got = float(m.group(1).replace(",", ""))
        want = __import__("importlib").import_module("fixtures.make_fixture").FIXTURE_DUP_EXPOSURE \
            if False else 1599.0
        c.close("exposure counts the stated $0 (D-0ZERO's full 1,160)", got, want, 0.5)
        c.ok("and is NOT the value you get by dropping stated zeros",
             abs(got - 439.0) > 1.0, f"got {got}")

    # Inject the WRONG dedup and prove which check notices.
    bad = _cp.deepcopy(deal)
    stb = bad["rent_roll"]["stated"]
    swapped = 0
    for det in stb["duplicate_detail"]:
        alt = [x for x in det["dropped"]
               if isinstance(x.get("rent_stated"), (int, float))]
        if det["kept_lease_status"] != stb["in_place_lease_status"] or not alt:
            continue
        det["kept_rent"] = alt[0]["rent"]
        det["kept_rent_stated"] = alt[0]["rent_stated"]
        det["kept_lease_status"] = alt[0]["lease_status"]
        alt[0]["lease_status"] = stb["in_place_lease_status"]
        swapped += 1
    c.ok("the injection swapped at least one duplicate", swapped > 0, str(swapped))
    bc = _cm.rent_roll_checks(bad)
    bdp = next(r for r in bc["rows"] if r["item"].startswith("Duplicate rows"))
    bmk = next(r for r in bc["rows"] if r["item"].startswith("Market rent"))
    c.equal("a WRONG dedup is caught by the row-choice check", bdp["verdict"], _cm.MISMATCH)
    c.equal(
        "and is INVISIBLE to the market-rent tie-out — why the old claim was false",
        bmk["verdict"], _cm.OK,
    )
    c.ok("the MISMATCH names the offending units",
         any(u in (bdp["note"] or "") for u in ("D-0ZERO", "D-LOWER")), bdp["note"][:120])

    c.section("R22 C#3: a malformed duplicate_detail fails validation, not the check")
    for mutate, needle in (
        (lambda d: d["rent_roll"]["stated"].__setitem__("duplicate_detail", [{}]), "missing required key"),
        (lambda d: d["rent_roll"]["stated"]["duplicate_detail"][0].__setitem__("kept_row", 999), "not among candidate_rows"),
        (lambda d: d["rent_roll"]["stated"]["duplicate_detail"][0].__setitem__("dropped", []), "not exactly"),
        (lambda d: d["rent_roll"]["stated"]["duplicate_detail"][0]["dropped"][0].pop("rent_stated"), "rent_stated"),
        (lambda d: d["rent_roll"]["stated"].__setitem__("duplicate_detail", "nope"), "expected a list"),
    ):
        bad2 = copy.deepcopy(deal)
        try:
            mutate(bad2)
        except (KeyError, IndexError):
            continue
        errs = [e for e in schema.validate(bad2) if "duplicate_detail" in e]
        c.ok(f"malformed record ({needle}) is a validation error",
             any(needle in e for e in errs), str(errs)[:130])
    # And the empty-record case specifically must not reach the check as a KeyError.
    empty = copy.deepcopy(deal)
    empty["rent_roll"]["stated"]["duplicate_detail"] = [{}]
    c.ok("[{}] fails validation", bool([e for e in schema.validate(empty) if "duplicate_detail" in e]))
    try:
        _cm.rent_roll_checks(empty)
        c.ok("and rent_roll_checks does not raise on it", True)
    except Exception as exc:
        c.ok("and rent_roll_checks does not raise on it", False, f"{type(exc).__name__}: {exc}")
    dup_id = copy.deepcopy(deal)
    dd = dup_id["rent_roll"]["stated"]["duplicate_detail"]
    if len(dd) >= 2:
        dd[1]["unit_id"] = dd[0]["unit_id"]
        c.ok("a repeated unit_id fails validation",
             any("appears twice" in e for e in schema.validate(dup_id)))

    c.section("R23: malformed duplicate records degrade, they never raise")
    # Each of these is the exact malformed example from its finding.
    for label, mutate in (
        ("dropped: [null]", lambda x: x["dropped"].__setitem__(0, None)),
        ("dropped: null", lambda x: x.__setitem__("dropped", None)),
        ("dropped: 'nope'", lambda x: x.__setitem__("dropped", "nope")),
    ):
        m1 = copy.deepcopy(deal)
        mutate(m1["rent_roll"]["stated"]["duplicate_detail"][0])
        try:
            got = _cm.rent_roll_checks(m1)
            c.ok(f"{label}: no raise", True)
            dpv = next(r for r in got["rows"] if r["item"].startswith("Duplicate rows"))
            c.ok(f"{label}: the row does not claim a clean OK",
                 dpv["verdict"] != _cm.OK, dpv["verdict"])
            c.ok(f"{label}: the market note survives it too",
                 any(r["item"].startswith("Market rent") for r in got["rows"]))
        except Exception as exc:
            c.ok(f"{label}: no raise", False, f"{type(exc).__name__}: {exc}")
    m_null = copy.deepcopy(deal)
    m_null["rent_roll"]["stated"]["duplicate_detail"][0] = None
    try:
        _cm.rent_roll_checks(m_null)
        c.ok("a null RECORD does not raise either", True)
    except Exception as exc:
        c.ok("a null RECORD does not raise either", False, f"{type(exc).__name__}: {exc}")

    c.section("R23: a missing market rent makes the pair dedup-SENSITIVE")
    ms = copy.deepcopy(deal)
    d0m = ms["rent_roll"]["stated"]["duplicate_detail"][0]
    d0m["kept_market_rent"] = 1290.0
    d0m["dropped"][0]["market_rent"] = None
    nm = next(r["note"] for r in _cm.rent_roll_checks(ms)["rows"]
              if r["item"].startswith("Market rent"))
    c.ok("one stated + one blank is NOT called identical",
         "sensitive to row choice" in nm, nm[-150:])
    c.ok("and the note says why — a blank counts as zero in the total",
         "treats as zero" in nm, nm[-150:])
    both = copy.deepcopy(deal)
    for dd in both["rent_roll"]["stated"]["duplicate_detail"]:
        dd["kept_market_rent"] = 1000.0
        for x in dd["dropped"]:
            x["market_rent"] = 1000.0
    nb = next(r["note"] for r in _cm.rent_roll_checks(both)["rows"]
              if r["item"].startswith("Market rent"))
    c.ok("identical on both rows IS called insensitive",
         "does NOT validate which row" in nb, nb[-140:])

    # ---- round-24 hygiene pins -------------------------------------------------------
    c.section("R24 C#1: 'dedup-sensitive' means the TOTAL would move, nothing else")
    # A missing market rent and a stated $0 contribute the SAME zero to the sum. Treating
    # every missing value as a difference marked pairs sensitive whose total cannot move.
    for label, kept_mr, dropped_mr, want in (
        ("both blank",        None,   None,   False),
        ("blank vs stated 0", None,   0.0,    False),
        ("stated 0 vs blank", 0.0,    None,   False),
        ("both stated 0",     0.0,    0.0,    False),
        ("1290 on both",      1290.0, 1290.0, False),
        ("blank vs 1290",     None,   1290.0, True),
        ("stated 0 vs 1290",  0.0,    1290.0, True),
        ("1290 vs 1515",      1290.0, 1515.0, True),
    ):
        s24 = copy.deepcopy(deal)
        recs24 = s24["rent_roll"]["stated"]["duplicate_detail"]
        recs24[0]["kept_market_rent"] = kept_mr
        recs24[0]["dropped"][0]["market_rent"] = dropped_mr
        for rest in recs24[1:]:            # hold every other pair identical
            rest["kept_market_rent"] = 1000.0
            for x in rest["dropped"]:
                x["market_rent"] = 1000.0
        n24 = next(r["note"] for r in _cm.rent_roll_checks(s24)["rows"]
                   if r["item"].startswith("Market rent"))
        c.ok(f"{label}: claimed sensitivity matches whether the total moves",
             ("sensitive to row choice" in n24) == want,
             f"claimed={'sensitive to row choice' in n24} expected={want} :: {n24[-110:]}")

    c.section("R24 C#2: unreadable records are UNVERIFIABLE, never reported as absence")
    allbad = copy.deepcopy(deal)
    allbad["rent_roll"]["stated"]["duplicate_detail"] = [None]
    n_all = next(r["note"] for r in _cm.rent_roll_checks(allbad)["rows"]
                 if r["item"].startswith("Market rent"))
    c.ok("[null] does not assert that no unit is listed twice",
         "no row choice to make" not in n_all, n_all[-130:])
    c.ok("[null] says UNVERIFIABLE instead", "UNVERIFIABLE" in n_all, n_all[-130:])
    c.ok("[null] discloses how many records it could not read",
         "1 duplicate record(s) are malformed" in n_all, n_all[-130:])
    mixed = copy.deepcopy(deal)
    mixed["rent_roll"]["stated"]["duplicate_detail"][0] = None
    n_mix = next(r["note"] for r in _cm.rent_roll_checks(mixed)["rows"]
                 if r["item"].startswith("Market rent"))
    c.ok("a mixed set does not describe the readable subset as complete",
         "1 further record(s) were unreadable" in n_mix, n_mix[-130:])

    c.section("R24 C#3: validate() reports a bad candidate_rows, it does not raise")
    for label, cands24 in (("unhashable [41, []]", [41, []]),
                           ("mixed ints/strings [41, '40']", [41, "40"]),
                           ("bool [41, True]", [41, True]),
                           ("nested mapping [41, {'a': 1}]", [41, {"a": 1}])):
        c24 = copy.deepcopy(deal)
        c24["rent_roll"]["stated"]["duplicate_detail"][0]["candidate_rows"] = cands24
        try:
            # Match the TYPE error specifically. Filtering on the substring
            # "candidate_rows" let the unrelated partition error vouch for this pin: the
            # bool and string cases passed against a deliberately reverted validator.
            errs24 = [e for e in schema.validate(c24)
                      if ".candidate_rows:" in e and "is not a row number" in e]
            c.ok(f"{label}: the bad element itself is reported, without throwing",
                 len(errs24) == 1, errs24[0] if errs24 else "no type error was reported")
        except Exception as exc:                       # noqa: BLE001 - that IS the finding
            c.ok(f"{label}: the bad element itself is reported, without throwing", False,
                 f"{type(exc).__name__}: {exc}")
    c.equal("a clean fixture still passes the same code path",
            [e for e in schema.validate(deal) if "candidate_rows" in e], [])

    c.section("R23: dropped rows must PARTITION candidate_rows")
    for label, mutate, needle in (
        ("duplicated dropped id",
         lambda x: (x["candidate_rows"].append(99), x["dropped"].append(dict(x["dropped"][0]))),
         "contain a duplicate"),
        ("dropped id == kept_row",
         lambda x: x["dropped"][0].__setitem__("row", x["kept_row"]), "also kept_row"),
        ("dropped id not a candidate",
         lambda x: x["dropped"][0].__setitem__("row", 12345), "not exactly"),
        ("repeated candidate_rows",
         lambda x: x.__setitem__("candidate_rows", [x["kept_row"], x["kept_row"]]),
         "repeated row"),
    ):
        pm = copy.deepcopy(deal)
        mutate(pm["rent_roll"]["stated"]["duplicate_detail"][0])
        errs = [e for e in schema.validate(pm) if "duplicate_detail" in e]
        c.ok(f"{label} fails validation", any(needle in e for e in errs), str(errs)[:130])

    c.section("R23: lease statuses are an enumerated vocabulary")
    c.ok("the vocabulary is declared as data", isinstance(schema.LEASE_STATUSES, tuple))
    c.ok("and includes None — a blank Status is a real value",
         None in schema.LEASE_STATUSES, str(schema.LEASE_STATUSES))
    for bad in ("Currnet", "current", "Notice", "CURRENT"):
        vm = copy.deepcopy(deal)
        vm["rent_roll"]["stated"]["duplicate_detail"][0]["kept_lease_status"] = bad
        c.ok(f"kept_lease_status={bad!r} fails validation",
             any("lease_status" in e for e in schema.validate(vm)))
        # And it must not have quietly become the branch that EXCUSES a wrong keep.
        rr = _cm.rent_roll_checks(vm)
        dpx = next(r for r in rr["rows"] if r["item"].startswith("Duplicate rows"))
        c.ok(f"...and {bad!r} does not silently read as a clean OK",
             dpx["verdict"] != _cm.OK or "had none" not in (dpx["note"] or ""),
             f"{dpx['verdict']}: {(dpx['note'] or '')[:90]}")
    vd = copy.deepcopy(deal)
    vd["rent_roll"]["stated"]["duplicate_detail"][0]["dropped"][0]["lease_status"] = "Nwe"
    c.ok("a typo on a DROPPED row fails too",
         any("lease_status" in e for e in schema.validate(vd)))
    for good in schema.LEASE_STATUSES:
        gm = copy.deepcopy(deal)
        gm["rent_roll"]["stated"]["duplicate_detail"][0]["kept_lease_status"] = good
        c.ok(f"{good!r} is accepted",
             not any("kept_lease_status" in e for e in schema.validate(gm)))

    c.section("R22 C#5: the market tie-out note is DERIVED, not hardcoded prose")
    mkn = next(r["note"] for r in _cm.rent_roll_checks(deal)["rows"]
               if r["item"].startswith("Market rent"))
    n_units = len(deal["rent_roll"]["units"])
    c.ok("it states THIS deal's unit count", f"{n_units:,} units" in mkn, mkn[:110])
    c.ok("not a hardcoded 334", "334 units" not in mkn or n_units == 334)
    # The dedup-sensitivity claim must be measured. Make one pair's market rents differ.
    diffp = copy.deepcopy(deal)
    d0 = diffp["rent_roll"]["stated"]["duplicate_detail"][0]
    d0["dropped"][0]["market_rent"] = (d0["kept_market_rent"] or 0) + 250.0
    mkn2 = next(r["note"] for r in _cm.rent_roll_checks(diffp)["rows"]
                if r["item"].startswith("Market rent"))
    c.ok("with differing pair market rents it says the total IS dedup-sensitive",
         "sensitive to row choice" in mkn2, mkn2[-160:])
    c.ok("and with identical ones it says it is NOT",
         "does NOT validate which row" in mkn, mkn[-160:])

    c.section("R21 C#5: zero occupied units is a legitimate measurement")
    allvac = copy.deepcopy(deal)
    st3 = allvac["rent_roll"].setdefault("stated", {})
    st3["buckets"] = {"Occupied": 0, "Vacant Available": int(st3.get("units_total") or 0)}
    st3["occupied_units"] = 0
    st3["occupied_basis"] = "Occupied"
    av = _cm.rent_roll_checks(allvac)
    occ_rows = [r for r in av["rows"] if r["item"] in ("Occupied units (physical)", "Occupancy")]
    c.equal("both occupancy rows are present", len(occ_rows), 2)
    for r in occ_rows:
        c.ok(f"{r['item']} does not read NOT CHECKED on a 0 source",
             r["verdict"] != _cm.NOT_CHECKED, f"{r['item']}={r['verdict']} src={r['source']!r}")
    c.equal("the source-side percentage is 0.0, not None",
            next(r["source"] for r in occ_rows if r["item"] == "Occupancy"), 0.0)

    c.section("R21 C#3: canonical categories normalize case-insensitively")
    for raw_label, want in (
        ("payroll", "Payroll"), ("PAYROLL", "Payroll"),
        ("general and administrative", "General and Administrative"),
        ("GENERAL AND ADMINISTRATIVE", "General and Administrative"),
        ("g&a", "General and Administrative"),
        ("gain / (loss) to lease", "Gain / (Loss) to Lease"),
        ("gain/(loss) to lease", "Gain / (Loss) to Lease"),
        ("  Utilities  ", "Utilities"),
    ):
        c.equal(f"normalize_category({raw_label!r})", schema.normalize_category(raw_label), want)

    c.section("R21 C#2: the cap narrative uses the CONFIGURED cap")
    note2 = next((n["note"] for n in deal["meta"]["notes"] if "Two comp sets" in n["note"]), "")
    tg = deal["comps"]["tiering"]
    cap_cfg = tg["top_n"] + tg["additional_n"]
    if "cap" in note2 and "never binds" in note2:
        c.ok(f"it names the configured cap ({cap_cfg}), not the matched set's size",
             f"fewer than {cap_cfg}" in note2, note2[:200])
        c.ok("and not the detailed-set count",
             f"fewer than {len(_cm.comps(deal)['detailed_communities'])} " not in note2
             or len(_cm.comps(deal)["detailed_communities"]) == cap_cfg)

    c.section("provenance completeness")
    import provenance as pv

    st_n, req_n, pct = pv.coverage(deal["provenance"])
    print(f"  ...  PROVENANCE COVERAGE = {st_n}/{req_n} ({pct:.0f}%)")
    c.equal("every required value is stamped", st_n, req_n)
    c.section("R#5: the new tax and closing-cost values are in the required set")
    for pth in (
        "summary.tax_parcel_id", "summary.tax_area_code", "summary.tax_basis_label",
        "summary.tax_basis_note", "summary.tax_billed_amount", "summary.tax_billed_year",
        "closing_costs.transfer_tax.amount", "closing_costs.mortgage_tax.amount",
    ):
        c.ok(f"{pth} is required", pth in pv.REQUIRED_PATHS)
        errs = broken(lambda d, k=pth: d["provenance"].pop(k, None))
        c.ok("and dropping its stamp is caught", has(errs, "no stamp"), str(errs)[:160])

    c.section("R#7: a closing-cost stamp names every input the number really used")
    inputs = set(deal["provenance"]["closing_costs.transfer_tax.amount"].get("inputs", []))
    for needed in ("pricing.price_per_unit", "pricing.units_total", "summary.state"):
        c.ok(f"transfer-tax stamp cites {needed}", needed in inputs, str(sorted(inputs)))
    m_inputs = set(deal["provenance"]["closing_costs.mortgage_tax.amount"].get("inputs", []))
    c.ok(
        "mortgage-tax stamp cites the loan amount it is computed on",
        "closing_costs.loan_amount" in m_inputs,
        str(sorted(m_inputs)),
    )

    errs = broken(lambda d: d["provenance"].pop("summary.units_total"))
    c.ok("an unstamped required value is rejected", has(errs, "no stamp"), str(errs)[:200])
    errs = broken(
        lambda d: d["provenance"].__setitem__(
            "summary.units_total",
            {"source_doc": "x", "locator": "y", "method": "derived"},
        )
    )
    c.ok(
        "a derived value with no inputs is rejected",
        has(errs, "cite the inputs"),
        str(errs)[:200],
    )
    errs = broken(
        lambda d: d["provenance"]["summary.units_total"].__setitem__("method", "vibes")
    )
    c.ok("an unknown method is rejected", has(errs, "method"), str(errs)[:200])
    c.section("a quote must appear verbatim in the source it cites")
    c.ok(
        "contiguous quote accepted",
        pv.quote_is_contiguous("garden-style multifamily", "a 334-unit garden-style multifamily community"),
    )
    c.ok(
        "paraphrase rejected",
        not pv.quote_is_contiguous("garden style, multi family", "a garden-style multifamily community"),
    )
    c.ok(
        "line-broken source still matches",
        pv.quote_is_contiguous("garden-style multifamily", "a 334-unit garden-style\n  multifamily community"),
    )

    # ---- field inventory / manifest source -------------------------------------------
    c.section("field inventory")
    fields = schema.MANIFEST_FIELDS
    c.ok("inventory is non-trivial", len(fields) >= 90, f"{len(fields)} fields")
    print(f"  ...  FIELD COUNT = {len(fields)}")
    names = [(f.group, f.name) for f in fields]
    c.equal("no duplicate group+field pairs", len(names), len(set(names)))
    c.ok(
        "every field has all seven columns populated",
        all(f.group and f.name and f.type and f.definition and f.source and f.fills for f in fields),
        str([f.name for f in fields if not (f.definition and f.fills)]),
    )
    c.ok(
        "every T-12 revenue category has a manifest row",
        all(
            any(f.name == cat and f.group == "T-12 revenue" for f in fields)
            for cat in schema.T12_REVENUE_CATEGORIES
        ),
    )
    c.ok(
        "every T-12 expense category has a manifest row",
        all(
            any(f.name == cat and f.group == "T-12 expense" for f in fields)
            for cat in schema.T12_EXPENSE_CATEGORIES
        ),
    )
    for group in ("Comps", "Demand", "Supply"):
        c.ok(
            f"{group} rows exist in the inventory",
            any(f.group == group for f in fields),
        )
    c.equal(
        "manifest has the eight specified columns",
        list(schema.MANIFEST_COLUMNS),
        [
            "Group",
            "Field",
            "Type",
            "Definition",
            "Source document",
            "Typically fills (client model input)",
            "Public demo",
            "Example (demo deal)",
        ],
    )
    c.ok("groups are contiguous in manifest order", _groups_contiguous(fields))

    # ---- path resolution -------------------------------------------------------------
    c.section("example-value path resolution")
    c.equal(
        "resolves a nested list path",
        schema.resolve_path(deal, "comps.rows[0].community"),
        deal["comps"]["rows"][0]["community"],
    )
    c.equal(
        "resolves a plain path", schema.resolve_path(deal, "summary.county"), "Example County"
    )
    c.equal("None path resolves to None", schema.resolve_path(deal, None), None)
    c.equal(
        "a bad path resolves to None, it does not raise",
        schema.resolve_path(deal, "summary.nope.deeper"),
        None,
    )
    c.equal(
        "an out-of-range index resolves to None",
        schema.resolve_path(deal, "comps.rows[9999].community"),
        None,
    )
    # An `absent_reason` field is None precisely BECAUSE its section is present, which the
    # fixture's every section is. Excluding them here is not a loosening: the check below
    # asserts they resolve on a deal where the section really is absent, which is the only
    # state in which they carry a value at all.
    pathed = [f for f in fields if f.path and not f.path.endswith(".absent_reason")]
    # The exact set that may not resolve, not a count: two optional keys this fixture leaves
    # out, the optional prior effective rent, and the two trade-out percentages, which
    # compute.trade_out derives and the deal never stores -- they carry a path so the
    # manifest's public-demo column reads them internal-only like the rest of trade-out
    #. A typo'd path anywhere else is a new member and fails here.
    unresolved = sorted(f.path for f in pathed if schema.resolve_path(deal, f.path) is None)
    c.equal(
        "every pathed field resolves against the fixture, bar the known optional/derived set",
        unresolved,
        sorted([
            "pricing.document_price_per_unit",
            "closing_costs.loan_amount",
            "trade_out.rows[0].prior_rent_effective",
            "trade_out.rows[0].gross_change_pct",
            "trade_out.rows[0].effective_change_pct",
        ]),
    )

    absence_fields = [f for f in fields if f.path and f.path.endswith(".absent_reason")]
    c.ok(
        "every absence-capable section has an absent_reason field",
        {f.path.split(".")[0] for f in absence_fields} >= {"t12", "rent_roll", "trade_out"},
        sorted(f.path for f in absence_fields),
    )
    absent = copy.deepcopy(deal)
    # `units` is the MISSING sentinel, not 0: an absent section has no count, and since
    # round 20 zero is refused by the count contract wherever it appears.
    absent["t12"] = {"month_labels": [], "units": schema.MISSING, "rows": [],
                     "source_subtotals": {},
                     "absent_reason": "no statement was supplied"}
    absent["rent_roll"] = {"as_of": "", "lookback_months": 12,
                           "new_tenant_tolerance_days": 31, "units": [], "stated": {},
                           "absent_reason": "no roll was supplied"}
    absent["trade_out"] = {**absent["trade_out"], "rows": [],
                           "absent_reason": "no trade-out report was supplied"}
    c.ok("and that deal validates", not schema.validate(absent),
         "; ".join(schema.validate(absent))[:300])
    for field in absence_fields:
        c.ok(
            f"{field.path} resolves when the section is absent",
            schema.resolve_path(absent, field.path) is not None,
            field.path,
        )

    return c.finish()


def _groups_contiguous(fields) -> bool:
    seen: list[str] = []
    for f in fields:
        if not seen or seen[-1] != f.group:
            if f.group in seen:
                return False
            seen.append(f.group)
    return True


if __name__ == "__main__":
    raise SystemExit(guard(main))
