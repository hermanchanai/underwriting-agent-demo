"""The vendored property-tax table: divisors, ratios, reset rules, coverage, the band.

Property tax is the line most often got wrong on an acquisition, and the two ways it goes
wrong are structural, not arithmetic: the rate's UNIT (a percent per $100 in TX/NC/NV/AZ/TN,
a millage per $1,000 in GA/AL/FL — the ÷100-vs-÷1000 slip is a silent 10x error) and the
BASIS (Arizona taxes a limited value that does not reset on sale; California resets to the
price; Texas, Georgia and Florida follow the market). This suite pins each rule as data,
pins the Maricopa arithmetic to the cent, and checks that an unknown
county or state reads MISSING with an honest note rather than a guessed number.

Usage: python tools/test_property_tax_table.py
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testkit import Checker, guard  # noqa: E402

import compute  # noqa: E402
import render_previews  # noqa: E402
import schema  # noqa: E402

#: The pre-2026-09-10 sample: fully synthetic, TX, with a synthetic county record — the deal
#: these state-by-state checks were written against. The PUBLISHED sample (fixture_deal.json)
#: is AZ/Maricopa from the vendored table since the restage and is pinned by
#: test_published_sample.py.
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_full.json"

#: An illustrative Maricopa County parcel: the county's published 2025 rates for one tax
#: area (primary 6.3268 + secondary 5.6592 per $100), applied to a round, invented limited
#: property value at the 10% ratio for legal class 4.2.
MARICOPA_PRIMARY = 6.3268
MARICOPA_SECONDARY = 5.6592
MARICOPA_LPV = 20_000_000
MARICOPA_COMPUTED = 239_720.00

DOLLAR_ZERO = re.compile(r"\$0(?![\d.,])")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", html, flags=re.S)))


def _text_after(text: str, label: str, width: int = 120) -> str:
    i = text.find(label)
    return text[i + len(label): i + len(label) + width].strip() if i >= 0 else ""


def main() -> int:
    c = Checker("test_property_tax_table")
    try:
        import property_tax_table as ptt
    except ImportError as exc:
        c.ok("tools/property_tax_table.py exists and imports", False, str(exc))
        return c.finish()

    deal = json.loads(FIXTURE.read_text(encoding="utf-8"))

    c.section("the divisor is the rate's unit — per $100 or per $1,000, per state")
    for code, divisor in (("TX", 100), ("AZ", 100), ("CA", 100), ("GA", 1000), ("FL", 1000), ("AL", 1000)):
        rule = ptt.STATE_RULES.get(code)
        c.ok(f"{code} has a state rule", rule is not None)
        if rule is not None:
            c.equal(f"{code} divisor", rule.divisor, divisor)
    c.equal("per $100 reads as such", ptt.rate_unit(100), "per $100")
    c.equal("per $1,000 reads as mills", ptt.rate_unit(1000), "mills per $1,000")
    c.close("2.15 per $100 is 2.15% effective", ptt.effective_rate(2.15, 100), 0.0215, 1e-9)
    c.close("21.5 mills is the SAME 2.15% effective", ptt.effective_rate(21.5, 1000), 0.0215, 1e-9)
    # The recurring silent error, stated as a test: a per-$100 rate divided by 1,000 is
    # ten times too low; a millage divided by 100 is ten times too high.
    right = ptt.annual_tax(1_000_000, 1.0, 2.15, 100)
    wrong = ptt.annual_tax(1_000_000, 1.0, 2.15, 1000)
    c.close("a TX rate through the GA divisor is 10x LOW", wrong * 10, right, 1e-6)
    right_ga = ptt.annual_tax(1_000_000, 0.40, 38.282, 1000)
    wrong_ga = ptt.annual_tax(1_000_000, 0.40, 38.282, 100)
    c.close("a GA millage through the TX divisor is 10x HIGH", wrong_ga, right_ga * 10, 1e-6)
    c.equal("a zero divisor computes nothing", ptt.annual_tax(1_000_000, 1.0, 2.15, 0), None)
    c.equal("a missing rate computes nothing", ptt.annual_tax(1_000_000, 1.0, None, 100), None)
    c.equal("a MISSING basis computes nothing", ptt.annual_tax(schema.MISSING, 1.0, 2.15, 100), None)

    c.section("assessment ratios come from the table, with a citation each")
    for code, ratio in (("TX", 1.00), ("GA", 0.40), ("AZ", 0.10), ("FL", 1.00), ("AL", 0.20), ("CA", 1.00)):
        rule = ptt.STATE_RULES[code]
        c.close(f"{code} ratio {ratio:.0%}", rule.ratio, ratio, 1e-9)
        c.ok(f"{code} ratio cites its authority", len(rule.ratio_citation) > 10, rule.ratio_citation)

    c.section("reset-on-sale is a state rule, stated on the output")
    c.equal("AZ: the LPV does NOT reset on sale", ptt.STATE_RULES["AZ"].resets_on_sale, False)
    c.ok("AZ names Proposition 117 and the 5% cap",
         "117" in ptt.STATE_RULES["AZ"].reset_note and "5%" in ptt.STATE_RULES["AZ"].reset_note)
    c.ok("AZ warns off the owner-occupied rate column",
         "residential primary" in ptt.STATE_RULES["AZ"].reset_note.lower())
    c.equal("CA: Prop 13 DOES reset to the price", ptt.STATE_RULES["CA"].resets_on_sale, True)
    c.ok("CA names Proposition 13", "13" in ptt.STATE_RULES["CA"].reset_note)
    for code in ("TX", "GA", "FL", "AL"):
        c.equal(f"{code}: assessed value follows the market", ptt.STATE_RULES[code].resets_on_sale, True)
    c.ok("compute reads the SAME reset table (one source of truth)",
         compute.RESETS_ON_SALE is ptt.RESETS_ON_SALE)
    for code, rule in ptt.STATE_RULES.items():
        c.equal(f"RESETS_ON_SALE[{code}] agrees with the state rule",
                ptt.RESETS_ON_SALE.get(code), rule.resets_on_sale)
    c.equal("AZ basis is the limited property value",
            "limited" in ptt.STATE_RULES["AZ"].basis_label.lower(), True)
    c.close("FL carries the 0.85 assessed-share convention", ptt.STATE_RULES["FL"].basis_factor, 0.85, 1e-9)
    c.close("FL carries the 0.96 early-payment discount", ptt.STATE_RULES["FL"].bill_discount, 0.96, 1e-9)
    for code in ("TX", "GA", "AZ", "AL", "CA"):
        c.close(f"{code} has no basis factor", ptt.STATE_RULES[code].basis_factor, 1.0, 1e-9)
        c.close(f"{code} has no bill discount", ptt.STATE_RULES[code].bill_discount, 1.0, 1e-9)

    c.section("every county entry is cited, dated, and sums to its components")
    n_counties, as_of = ptt.coverage()
    c.ok("the table carries at least the counties that could be cited", n_counties >= 10, str(n_counties))
    c.equal("coverage reports the verification date", as_of, ptt.VERIFIED_AS_OF)
    for (code, key), entry in ptt.COUNTY_RATES.items():
        label = f"{entry.county}, {code}"
        c.ok(f"{label}: its state has a rule", code in ptt.STATE_RULES)
        c.close(f"{label}: components sum to the rate", sum(r for _n, r in entry.components), entry.rate, 1e-6)
        c.ok(f"{label}: cites a published table", len(entry.citation) > 20 and len(entry.source) > 5)
        c.ok(f"{label}: has a tax year", isinstance(entry.tax_year, int) and 2024 <= entry.tax_year <= 2026,
             repr(entry.tax_year))
        c.ok(f"{label}: no scheme in the source (fragments carry no http)", "http" not in entry.source.lower())
        divisor = ptt.STATE_RULES[code].divisor
        # A rate stored in the wrong unit is the 10x error at rest. Per-$100 rates sit
        # between 0.5 and 5 where the ratio is 100%; Arizona's sit between 5 and 20 because
        # they apply to a 10% assessed value; millages between 5 and 60. Nothing real is
        # outside, and a millage typed into a per-$100 state (38) trips the TX bound.
        if divisor == 100 and code == "AZ":
            c.ok(f"{label}: an AZ per-$100 rate is between 5 and 20", 5.0 <= entry.rate <= 20.0, str(entry.rate))
        elif divisor == 100:
            c.ok(f"{label}: a per-$100 rate is between 0.5 and 5", 0.5 <= entry.rate <= 5.0, str(entry.rate))
        else:
            c.ok(f"{label}: a millage is between 5 and 60", 5.0 <= entry.rate <= 60.0, str(entry.rate))
        c.equal(f"{label}: keyed by its normalised county", ptt.normalise_county(entry.county), key)
    for code, county in (("AZ", "Maricopa"), ("TX", "Harris"), ("TX", "Montgomery"), ("TX", "Dallas"),
                         ("TX", "Tarrant"), ("TX", "Collin"), ("TX", "Fort Bend"), ("GA", "Gwinnett"),
                         ("GA", "Fulton"), ("GA", "Cobb"), ("FL", "Pasco"), ("FL", "Hillsborough")):
        c.ok(f"{county}, {code} is on file", ptt.lookup(code, county).entry is not None)
    fort_bend = ptt.lookup("TX", "Fort Bend County").entry
    c.ok("Fort Bend is found under 'Fort Bend County' too", fort_bend is not None)
    if fort_bend is not None:
        c.close("Fort Bend: the Sugar Land / Fort Bend ISD stack", fort_bend.rate, 1.837727, 1e-9)
        c.ok("Fort Bend: cites the school district's adopted rate and the county tax office",
             "Fort Bend ISD" in fort_bend.citation and "Tax" in fort_bend.citation, fort_bend.citation)
    for code, county in (("TX", "Denton"), ("GA", "Paulding")):
        found = ptt.lookup(code, county)
        c.ok(f"{county}, {code} is NOT asserted (no primary-source rate at build time)",
             found.entry is None and found.rate_status == ptt.NOT_ASSERTED)

    c.section("county names normalise")
    for spelling in ("Harris", "Harris County", "harris", "HARRIS CO.", " Harris county "):
        c.ok(f"{spelling!r} finds Harris", ptt.lookup("TX", spelling).entry is ptt.lookup("TX", "Harris").entry)
    c.ok("'Texas' normalises to TX", ptt.normalise_state("Texas") == "TX")
    c.ok("'tx' normalises to TX", ptt.normalise_state("tx") == "TX")
    c.equal("MISSING is not a state", ptt.normalise_state(schema.MISSING), None)
    c.equal("None is not a state", ptt.normalise_state(None), None)

    c.section("the Maricopa arithmetic, to the cent")
    combined = round(MARICOPA_PRIMARY + MARICOPA_SECONDARY, 4)
    c.close("primary + secondary = 11.9860 per $100", combined, 11.9860, 1e-9)
    computed = ptt.annual_tax(MARICOPA_LPV, ptt.STATE_RULES["AZ"].ratio, combined, ptt.STATE_RULES["AZ"].divisor)
    c.close("LPV x 10% x 11.9860/100 = $239,720.00", computed, MARICOPA_COMPUTED, 0.001)
    # The owner-occupied 'approx residential primary' column (4.7662 in place of 6.3268)
    # understates a rental's bill; the 15% ratio the earlier cut carried overstates it.
    rebated = ptt.annual_tax(MARICOPA_LPV, 0.10, 4.7662 + MARICOPA_SECONDARY, 100)
    c.ok("the 'approx residential primary' column would understate the bill",
         rebated is not None and rebated < MARICOPA_COMPUTED, str(rebated))
    c.ok("by a margin that looks close enough to be wrong (within 20%)",
         rebated is not None and 0.80 < rebated / MARICOPA_COMPUTED < 0.95, f"{rebated / MARICOPA_COMPUTED:.3f}")
    overstated = ptt.annual_tax(MARICOPA_LPV, 0.15, combined, 100)
    c.close("a 15% ratio overstates it by exactly half", overstated / MARICOPA_COMPUTED, 1.5, 1e-6)
    maricopa = ptt.lookup("AZ", "Maricopa").entry
    c.ok("the Maricopa entry is a named tax area code, stated as such",
         maricopa is not None and "971300" in maricopa.jurisdiction)
    c.close("Maricopa rate on file is primary + secondary for that area",
            maricopa.rate, 6.7007 + 3.5566, 1e-9)
    c.ok("and the entry says rates vary by tax area", "tax area" in (maricopa.note or "").lower())

    c.section("an unknown county reads MISSING with an honest note; the state rules still apply")
    found = ptt.lookup("TX", "Travis")
    values = ptt.summary_values(found)
    c.equal("rate is MISSING", values["tax_rate_as_billed"], schema.MISSING)
    c.equal("divisor is still asserted for the state", values["tax_divisor"], 100)
    c.close("ratio is still asserted for the state", values["assessment_ratio"], 1.0, 1e-9)
    c.ok("the basis label is stated", values["tax_basis_label"] != schema.MISSING)
    expected_note = f"No rate on file for Travis, TX; the demo carries rates for {n_counties} counties as of {as_of}."
    c.ok("the evidence carries the honest note", expected_note in values["tax_rate_evidence"],
         values["tax_rate_evidence"])
    c.equal("the lookup says not asserted", found.rate_status, ptt.NOT_ASSERTED)
    found = ptt.lookup("ID", "Ada")
    values = ptt.summary_values(found)
    for key in ("tax_rate_as_billed", "tax_divisor", "assessment_ratio", "tax_basis_label", "tax_basis_note"):
        c.equal(f"unknown state: {key} is MISSING", values[key], schema.MISSING)
    c.ok("unknown state: the note says no state rules are on file",
         "no state rules on file" in values["tax_rate_evidence"].lower(), values["tax_rate_evidence"])
    found = ptt.lookup(schema.MISSING, schema.MISSING)
    c.equal("MISSING state and county: rate MISSING", ptt.summary_values(found)["tax_rate_as_billed"], schema.MISSING)
    c.ok("MISSING state and county: the note does not print the sentinel as a place",
         "MISSING," not in ptt.summary_values(found)["tax_rate_evidence"])

    c.section("a known county fills the rate, cited and dated")
    found = ptt.lookup("TX", "Harris")
    values = ptt.summary_values(found)
    c.close("Harris: the Houston / Houston ISD stack, per $100", values["tax_rate_as_billed"], 2.125220, 1e-9)
    c.equal("Harris: asserted", found.rate_status, ptt.ASSERTED)
    c.ok("Harris: evidence names the source and the tax year",
         "Harris County Tax Office" in values["tax_rate_evidence"] and "2025" in values["tax_rate_evidence"])
    c.ok("Harris: evidence names the jurisdiction stack", "Houston ISD" in values["tax_rate_evidence"])
    c.ok("Harris: evidence says the parcel's own district decides",
         "district" in values["tax_rate_evidence"].lower())
    stamps = ptt.provenance_stamps(found)
    c.equal("the rate stamp names the vendored table", stamps["summary.tax_rate_as_billed"]["source_doc"], ptt.SOURCE_DOC)
    c.ok("the rate stamp's locator is the table entry",
         "COUNTY_RATES" in stamps["summary.tax_rate_as_billed"]["locator"])
    c.equal("the divisor is read from the state rule", stamps["summary.tax_divisor"]["method"], "read")
    for key in ("tax_parcel_id", "tax_area_code", "assessed_value_basis", "tax_billed_amount", "tax_billed_year"):
        st = stamps.get(f"summary.{key}")
        c.ok(f"{key}: stamped as absent because no portal is reached",
             st is not None and "portal" in (st.get("note") or "").lower(), repr(st))
    gwinnett = ptt.summary_values(ptt.lookup("GA", "Gwinnett"))
    c.equal("Gwinnett: millage divisor", gwinnett["tax_divisor"], 1000)
    c.close("Gwinnett: 34.86 mills, unincorporated", gwinnett["tax_rate_as_billed"], 34.86, 1e-9)
    c.close("Gwinnett: 40% ratio", gwinnett["assessment_ratio"], 0.40, 1e-9)
    c.ok("the notes say what state rules are on file even with no county",
         "GA" in ptt.summary_values(ptt.lookup("GA", "Nowhere"))["tax_rate_evidence"])

    c.section("the per-unit band flags and never overwrites")
    c.equal("TX $5,000/unit is in band", ptt.band_check(5_000, "TX")[0], "ok")
    c.equal("TX $12,000/unit is a red flag (divisor-class error)", ptt.band_check(12_000, "TX")[0], "error")
    c.equal("TX $100/unit is a red flag (10x low)", ptt.band_check(100, "TX")[0], "error")
    c.equal("AZ $3,000/unit is outside the band — warn, not error", ptt.band_check(3_000, "AZ")[0], "warn")
    c.equal("an unknown state is unchecked", ptt.band_check(5_000, "ID")[0], "unchecked")
    c.equal("no figure is unchecked", ptt.band_check(None, "TX")[0], "unchecked")
    text = ptt.band_flag_text(12_000, "TX", "the statement")
    c.ok("the flag text names the figure and the band",
         "$12,000" in text and "$2,500" in text and "$9,000" in text, text)
    c.ok("the flag text says to check, not that it changed anything",
         "check" in text.lower() and "overwr" not in text.lower(), text)
    c.ok("an unchecked flag says so", "not checked" in ptt.band_flag_text(None, "TX", "the statement").lower())

    c.section("the preview and the workbook read the divisor, never assume per $100")
    ga = copy.deepcopy(deal)
    ga["summary"]["state"] = "GA"
    ga["summary"]["county"] = "Gwinnett"
    ga["summary"].update(ptt.summary_values(ptt.lookup("GA", "Gwinnett")))
    ga["closing_costs"]["state"] = "GA"
    html = render_previews.property_tax_fragment(ga)
    text = _text(html)
    c.ok("a GA rate renders as mills per $1,000", "34.8600 mills per $1,000" in text, text[:400])
    c.ok("the GA rate note is not Arizona's", "Primary plus secondary" not in text)
    c.ok("the fragment shows the rate evidence", "Rate evidence" in text and "Gwinnett County Tax Commissioner" in text)
    tx = copy.deepcopy(deal)
    html = render_previews.property_tax_fragment(tx)
    text = _text(html)
    c.ok("the fixture's TX rate still reads per $100", "2.1480 per $100" in text)
    c.ok("the fixture's rate note is no longer Arizona's",
         "Primary plus secondary" not in text and "jurisdictions" in text.lower())
    c.ok("the fixture fragment carries its rate evidence row", "Rate evidence" in text)
    c.ok("no upload-only rows on a set that does not carry the keys (the pre-restage synthetic set)",
         "per the statement" not in text.lower() and "sanity band" not in text.lower())
    az = copy.deepcopy(deal)
    az["summary"]["state"] = "AZ"
    az["summary"]["county"] = "Maricopa"
    az["summary"].update(ptt.summary_values(ptt.lookup("AZ", "Maricopa")))
    az["closing_costs"]["state"] = "AZ"
    text = _text(render_previews.property_tax_fragment(az))
    c.ok("the AZ rate note stays primary plus secondary", "Primary plus secondary" in text)
    unknown = copy.deepcopy(deal)
    unknown["summary"]["county"] = "Travis"
    unknown["summary"].update(ptt.summary_values(ptt.lookup("TX", "Travis")))
    # A published set carries its own basis and bill; on the upload path they are MISSING.
    for key in ("assessed_value_basis", "tax_billed_amount", "tax_billed_year"):
        unknown["summary"][key] = schema.MISSING
    unknown["summary"]["tax_statement_amount"] = schema.MISSING
    unknown["summary"]["tax_per_unit_flag"] = ptt.band_flag_text(None, "TX", "the estimate")
    c.equal("the unknown-county deal validates", schema.validate(unknown), [])
    text = _text(render_previews.property_tax_fragment(unknown))
    c.ok("an unknown county's rate reads MISSING on the page", "MISSING per $100" in text or "MISSING" in text)
    c.ok("MISSING never renders as $0", not DOLLAR_ZERO.search(text), text[:300])
    c.ok("the honest note reaches the page", "No rate on file for Travis, TX" in text)
    c.ok("the upload-only statement row renders when present", "per the statement" in text.lower())

    c.section("the FL convention reaches the year-one estimate, stated beside it")
    fl = copy.deepcopy(deal)
    fl["summary"]["state"] = "FL"
    fl["summary"]["county"] = "Hillsborough"
    fl["summary"].update(ptt.summary_values(ptt.lookup("FL", "Hillsborough")))
    fl["closing_costs"]["state"] = "FL"
    tu = compute.tax_underwriting(fl)
    price = deal["pricing"]["price_per_unit"] * deal["pricing"]["units_total"]
    c.close("year-one full value is 85% of the price", tu["assumed_full_value"], price * 0.85, 0.01)
    c.close("year-one tax carries the 4% early-payment discount",
            tu["assumed_tax"], price * 0.85 * 1.0 * 18.2515 / 1000 * 0.96, 0.01)
    c.ok("the label says so", "85%" in tu["assumed_full_value_label"])
    c.ok("the approach says so", "4%" in tu["approach"] or "0.96" in tu["approach"] or "discount" in tu["approach"].lower())
    c.close("the generic shortcut is still the undiscounted price-based figure",
            tu["generic_tax"], price * 1.0 * 18.2515 / 1000, 0.01)
    tu_tx = compute.tax_underwriting(deal)
    c.equal("TX labels are unchanged", tu_tx["assumed_full_value_label"], "Purchase price (the transaction resets the basis)")

    c.section
    spelled = copy.deepcopy(deal)
    spelled["summary"]["state"] = "Texas"
    tu = compute.tax_underwriting(spelled)
    c.equal("compute normalises 'Texas' to TX", tu["state"], "TX")
    c.equal("and finds the reset rule", tu["resets_on_sale"], True)
    c.close("so the year-one tax is computed, not MISSING",
            tu["assumed_tax"], price * 1.0 * 2.148 / 100, 0.01)
    text = _text(render_previews.property_tax_fragment(spelled))
    c.ok("the fragment answers the reset question for 'Texas'", _text_after(text, "Does the basis reset on sale?").startswith("Yes"),
         _text_after(text, "Does the basis reset on sale?"))
    c.ok("and the rate note is Texas's, not the fallback", "jurisdictions" in text.lower())
    html = render_previews.property_tax_fragment(unknown)
    c.ok("an absent combined rate renders a styled MISSING, not a dash",
         re.search(r'Combined rate</dt><dd><span class="op-missing">MISSING</span> per \$100', html) is not None,
         re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[html.find("Combined rate"):html.find("Combined rate") + 200])))

    c.section
    c.equal("RESETS_ON_SALE covers exactly the states with a rule",
            sorted(ptt.RESETS_ON_SALE), sorted(ptt.STATE_RULES))
    for code, ratio, band in (("NV", 0.35, (1000, 5000)), ("NC", 1.00, (1500, 7000)), ("TN", 0.40, None)):
        rule = ptt.STATE_RULES.get(code)
        c.ok(f"{code} has a full state rule", rule is not None)
        if rule is not None:
            c.equal(f"{code} divisor: per $100", rule.divisor, 100)
            c.close(f"{code} ratio {ratio:.0%}", rule.ratio, ratio, 1e-9)
            c.ok(f"{code} cites the ratio's authority", len(rule.ratio_citation) > 10, rule.ratio_citation)
            c.equal(f"{code} band", rule.band, band)
            c.ok(f"{code} states how the basis moves after a sale", len(rule.reset_note) > 40)
    c.ok("a Nevada lookup finds the state rule (no county, but the rules)",
         ptt.lookup("NV", "Clark").rule is not None and ptt.lookup("NV", "Clark").entry is None)
    nv = copy.deepcopy(deal)
    nv["summary"]["state"] = "NV"
    nv["summary"]["assessment_ratio"] = 0.35
    tu = compute.tax_underwriting(nv)
    c.ok("compute and the table agree on Nevada's reset status",
         tu["resets_on_sale"] == ptt.STATE_RULES["NV"].resets_on_sale and tu["resets_on_sale"] is not None)
    for code in ("ID", "OR", "WA"):
        c.equal(f"{code}: no rule, no reset status", ptt.RESETS_ON_SALE.get(code), None)

    c.section("an OM-stated bill is labelled as the memorandum's; a county record keeps its label")
    om_deal = copy.deepcopy(deal)
    om_deal["meta"]["provenance"] = [{"document": "om.pdf", "kind": "om", "note": "test"}]
    for key in ("tax_billed_amount", "tax_billed_year", "assessed_value_basis", "tax_parcel_id", "tax_area_code"):
        om_deal["provenance"][f"summary.{key}"] = {"source_doc": "om.pdf", "locator": "p.7", "method": "read",
                                                   "quote": "stated in the document"}
    text = _text(render_previews.property_tax_fragment(om_deal))
    c.ok("the bill row says it is the memorandum's", "as stated in the offering memorandum" in text.lower(), text[:500])
    c.ok("and the county label is gone", "County billed amount" not in text)
    c.ok("the subtitle says the check is against the memorandum's bill, not the county's",
         "county's own bill" not in text and "memorandum states" in text, text[:400])
    text = _text(render_previews.property_tax_fragment(deal))
    c.ok("the fixture's county record keeps 'County billed amount'", "County billed amount" in text)
    c.ok("and says nothing about a memorandum", "offering memorandum" not in text.lower())
    c.ok("and keeps its county-check subtitle", "county's own bill" in text)

    c.section("the summary schema knows the two upload-path keys, optionally")
    for key in ("tax_statement_amount", "tax_per_unit_flag"):
        spec = schema.SUMMARY_KEYS.get(key)
        c.ok(f"{key} is an OPTIONAL summary key", spec is not None and spec[0] is False, repr(spec))
    c.equal("the published fixture (without them) validates", schema.validate(deal), [])
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
