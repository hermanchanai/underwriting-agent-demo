"""Mock mode has to carry the whole pipeline, with no key and no spend.

The point of mock mode is not that it returns something. It is that the entire path from
an uploaded file to a set of mappings runs with no API key and no spend, so the parts of
this system that are *not* the model — the parsers, the role rules, the quote check, the
category assertion — can be exercised on every commit for free.


Usage: python server/tests/test_mock_pipeline.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, guard

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def main() -> int:
    c = Checker("test_mock_pipeline")
    os.environ.pop("DEMO_MODEL_MODE", None)  # mock is the default; make that explicit

    from server import classify as cl
    from server import extract_llm as m
    from server.parse_worker import parse_file
    import schema

    c.section("mock is the default and needs no key")
    c.equal("mode", m.mode(), "mock")
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        c.ok("no key is set for this run", not m.have_key())

        # ---- the synthetic set ---------------------------------------------------
        c.section("synthetic offering memorandum")
        om = parse_file(FIXTURES / "om.pdf", "pdf", "om.pdf")
        ledger = m.UsageLedger()
        result = m.om_summary(om["text"], ledger)
        c.equal("a call was recorded", ledger.model_calls, 1)
        c.equal("and it cost nothing", ledger.dollars, 0.0)
        c.equal("every asked-for field is present",
                sorted(result["fields"]), sorted(m.om_field_keys()))
        c.equal("units_total was found", result["fields"]["units_total"]["value"], 24)
        c.equal("year_built was found", result["fields"]["year_built"]["value"], 2021)
        c.equal("nothing was dropped as unverifiable", result["dropped"], [])
        c.ok(
            "the quote it returned really is in the document",
            m.quote_appears(result["fields"]["units_total"]["quote"], om["text"]),
            repr(result["fields"]["units_total"]["quote"]),
        )
        c.ok(
            "and it cites the page the marker says",
            result["fields"]["units_total"]["page"] == 1,
            repr(result["fields"]["units_total"]["page"]),
        )
        missing = [k for k, v in result["fields"].items() if v["value"] == schema.MISSING]
        c.ok(
            "the fields the mock cannot find read MISSING, not a guess",
            len(missing) >= 20,
            f"{len(missing)} of {len(result['fields'])} are MISSING",
        )

        c.section("synthetic T-12")
        t12 = parse_file(FIXTURES / "t12.xlsx", "xlsx", "t12.xlsx")
        rows = t12["sheets"][0]["rows"]
        index, header = cl.find_header_row(rows)
        labels = [str(r[1]) for r in rows[index + 1:] if len(r) > 1 and r[1]]
        ledger = m.UsageLedger()
        mapped = m.map_t12(labels, [str(h) for h in header], ledger)
        c.ok("labels were read off the sheet", len(labels) >= 15, str(len(labels)))
        # `asked` counts DISTINCT unresolved descriptions since an earlier review — a T-12
        # repeats a description across GL rows and paying to map it twice was the
        # defect — so this is no longer occurrences + occurrences. The claim it was
        # making is unchanged and is stated directly instead: nothing was dropped.
        mapped_labels = {x["label"] for x in mapped["mappings"]}
        c.equal("every label is accounted for",
                sorted(set(labels) - mapped_labels), [])
        c.equal("and the model was asked about the distinct ones only",
                mapped["asked"],
                len({label for label in labels if label not in
                     {x["label"] for x in mapped["mappings"][:mapped["deterministic"]]}}))
        # Since 2026-09-03 every operating line is asked of the model (the mock, here),
        # with its section; only the statement's own subtotal rows are settled without a
        # call. The synthetic statement has fifteen operating lines and three subtotals.
        c.equal("every operating line of the synthetic statement is asked",
                mapped["asked"], len(labels) - mapped["deterministic"])
        c.equal("its three subtotal rows are settled by layout, not by a call",
                mapped["deterministic"], 3)
        c.equal("so one mock call was made", ledger.model_calls, 1)
        c.equal("the stated total revenue row was found",
                mapped["subtotals"]["total_revenue"], "Total Revenue")
        c.equal("and net operating income",
                mapped["subtotals"]["net_operating_income"], "Net Operating Income")
        proposed = [x["category"] for x in mapped["mappings"] if x["category"] != m.EXCLUDE]
        c.equal("nothing outside the vendored list was proposed",
                schema.assert_categories(proposed), [])

        c.section("synthetic rent roll")
        roll = parse_file(FIXTURES / "rent-roll.xlsx", "xlsx", "rent-roll.xlsx")
        rows = roll["sheets"][0]["rows"]
        index, header = cl.find_header_row(rows)
        ledger = m.UsageLedger()
        mapping = m.map_rent_roll([str(h) for h in header], rows[index + 1:], ledger)
        columns = mapping["columns"]
        c.equal("unit column", columns["unit"], 0)
        c.equal("plan column", columns["plan"], 1)
        c.equal("sqft column", columns["sqft"], 4)
        c.equal("status column", columns["status"], 5)
        c.equal("lease_start column", columns["lease_start"], 6)
        c.equal("current_rent column", columns["current_rent"], 9)
        c.equal("market_rent column", columns["market_rent"], 10)
        meanings = {v["raw"]: v["meaning"] for v in mapping["status_vocabulary"]}
        c.equal("Occupied is read as occupied", meanings.get("Occupied"), "occupied")
        c.equal("Vacant is read as vacant", meanings.get("Vacant"), "vacant")

        c.section("the notes box")
        ledger = m.UsageLedger()
        out = m.parse_notes(
            "24 homes, as of 2026-08-31. rent-roll.xlsx is the rent roll.",
            ["rent-roll.xlsx", "t12.xlsx"],
            ledger,
        )
        c.equal("unit count read", out["values"].get("units_total"), 24)
        c.equal("as-of date read", out["values"].get("as_of_date"), "2026-08-31")
        c.equal("the role hint is kept",
                [(h["filename"], h["role"]) for h in out["role_hints"]],
                [("rent-roll.xlsx", "rent_roll")])
        c.equal("an empty note makes no call at all",
                m.parse_notes("", ["a.pdf"], ledger)["values"], {})

    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
