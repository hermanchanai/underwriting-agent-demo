"""Every subset of {OM, T-12, rent roll} has to produce a file, and the right workbooks.

A visitor uploads what they have. The interesting cases are therefore not the complete
one — they are the seven incomplete ones, where the question is whether the pipeline
produces an honest partial answer or falls over. All seven are run here, end to end:
assemble, validate, write only the applicable workbooks, render all fourteen fragments,
and pass the publish gate.

Usage: python server/tests/test_assemble.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, guard

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def prepared(paths: dict) -> tuple[list[dict], dict, dict, dict]:
    """Parse, classify and map a set of files the way a real run does, in mock mode."""
    from server import classify as cl
    from server import extract_llm as m
    from server.parse_worker import parse_file

    documents: list[dict] = []
    om_fields: dict = {}
    t12_mapping: dict = {}
    roll_mapping: dict = {}
    ledger = m.UsageLedger()

    for role_hint, (path, kind) in paths.items():
        document = parse_file(path, kind, path.name, timeout=180)
        result = cl.classify(document)
        document["role"] = result.role
        document["note"] = result.reason
        documents.append(document)

        if result.role == "om":
            om_fields = m.om_summary(document["text"], ledger)["fields"]
        elif result.role in ("t12", "rent_roll"):
            rows = document["sheets"][0]["rows"]
            index, header = cl.find_header_row(rows)
            if result.role == "t12":
                labels = [str(r[1]) for r in rows[index + 1:] if len(r) > 1 and r[1]]
                t12_mapping = m.map_t12(labels, [str(h) for h in header], ledger)
                t12_mapping["header_row_index"] = index
                t12_mapping["code_col"] = 1
                t12_mapping["label_col"] = 2
            else:
                roll_mapping = m.map_rent_roll(
                    [str(h) for h in header], rows[index + 1:], ledger
                )
                roll_mapping["header_row_index"] = index
        del role_hint
    return documents, om_fields, t12_mapping, roll_mapping


SYNTHETIC = {
    "om": (FIXTURES / "om.pdf", "pdf"),
    "t12": (FIXTURES / "t12.xlsx", "xlsx"),
    "rent_roll": (FIXTURES / "rent-roll.xlsx", "xlsx"),
}

SUBSETS = [
    (),
    ("om",),
    ("t12",),
    ("rent_roll",),
    ("om", "t12"),
    ("om", "rent_roll"),
    ("t12", "rent_roll"),
    ("om", "t12", "rent_roll"),
]


def main() -> int:
    c = Checker("test_assemble")
    os.environ.pop("DEMO_MODEL_MODE", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)

    from server import assemble as asm
    import render_previews
    import schema
    import scrub_check
    import write_deal_summary
    import write_manifest
    import write_pack
    import write_rent_roll
    import write_t12_analysis

    WRITERS = {
        "pack": lambda deal, path: write_pack.build(deal, path),
        "deal_summary": lambda deal, path: write_deal_summary.build(deal, path),
        "t12_analysis": lambda deal, path: write_t12_analysis.build(deal, path),
        "rent_roll": lambda deal, path: write_rent_roll.build(deal, path),
        "manifest": lambda deal, path: write_manifest.build(path, deal),
    }

    c.section("all eight subsets of the synthetic set")
    for subset in SUBSETS:
        label = "+".join(subset) or "(nothing)"
        documents, om_fields, t12_mapping, roll_mapping = prepared(
            {k: SYNTHETIC[k] for k in subset}
        )
        deal, _notes = asm.assemble(
            documents=documents,
            om_fields=om_fields,
            t12_mapping=t12_mapping or None,
            roll_mapping=roll_mapping or None,
            generated_on="2026-09-02",
            as_of="2026-08-31",
        )
        errors = schema.validate(deal)
        c.ok(f"{label}: validates", not errors, "; ".join(errors)[:300])

        expected = ["pack", "deal_summary", "manifest"]  # the pack always applies (2026-09-14)
        if "t12" in subset:
            expected.insert(1, "t12_analysis")
        if "rent_roll" in subset:
            expected.insert(-1, "rent_roll")
        got = asm.applicable_workbooks(deal)
        c.equal(f"{label}: workbooks", sorted(got), sorted(expected))

        c.equal(f"{label}: t12 absent?", bool(schema.section_absent(deal, "t12")),
                "t12" not in subset)
        c.equal(f"{label}: rent_roll absent?",
                bool(schema.section_absent(deal, "rent_roll")), "rent_roll" not in subset)

        with tempfile.TemporaryDirectory(prefix=f"assemble-{label or 'none'}-") as tmp:
            out = Path(tmp)
            for key in got:
                WRITERS[key](deal, out / schema.OUTPUT_FILES[key])
            written = sorted(p.name for p in out.glob("*.xlsx"))
            c.equal(f"{label}: only those workbooks on disk", written,
                    sorted(schema.OUTPUT_FILES[k] for k in expected))
            fragments = render_previews.render_all(deal, out / "previews")
            c.equal(f"{label}: 14 fragments render", len(fragments), 14)
            empty = [n for n, p in fragments.items() if p.stat().st_size < 200]
            c.ok(f"{label}: no fragment is a stub", not empty, repr(empty))
            (out / "deal_outputs.json").write_text(
                json.dumps(deal, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            findings = scrub_check.run([out], scrub_check.load_terms([]))
            c.ok(f"{label}: passes the publish gate", not findings,
                 "; ".join(f.render() for f in findings)[:200])

    c.section("an absent section says so where a reader will see it")
    documents, om_fields, _t, _r = prepared({"om": SYNTHETIC["om"]})
    deal, _notes = asm.assemble(documents=documents, om_fields=om_fields,
                               generated_on="2026-09-02")
    c.ok("the t12 block carries a reason", bool(schema.section_absent(deal, "t12")))
    for name, fragment in (
        ("t12-data", render_previews.t12_data_fragment(deal)),
        ("t12-pnl", render_previews.t12_pnl_fragment(deal)),
        ("rent-roll", render_previews.rent_roll_fragment(deal)),
        ("unit-mix", render_previews.unit_mix_fragment(deal)),
    ):
        c.ok(f"the {name} fragment states the absence",
             "uploaded" in fragment.lower() or "no " in fragment.lower(),
             fragment[:160])
    manifest_html = render_previews.manifest_fragment(deal)
    c.ok("the manifest fragment mentions what was not run",
         "not run in the upload demo" in manifest_html.lower(), "")
    notes_html = render_previews.notes_fragment(deal)
    c.ok("Notes & Disclosures describes the run",
         "upload" in notes_html.lower() and "deleted" in notes_html.lower(), "")

    c.section("the notes say how T-12 categories were assigned")
    # The T-12 Data tab shows a category per row, so a reader can see the assignment and
    # disagree with it. What they cannot see from the tab is HOW it was made — and on an
    # upload it is made from the words in the label, not from an account-code table the
    # way the showcase path does it. Two readers of the same statement legitimately
    # differ on lines like "HVAC"; a reader who is not told the basis has no way to know
    # that is a judgement rather than a lookup.
    with_t12, _notes = asm.assemble(
        documents=prepared({"t12": SYNTHETIC["t12"]})[0],
        t12_mapping=prepared({"t12": SYNTHETIC["t12"]})[2],
        generated_on="2026-09-02",
    )
    note_text = " ".join(n["note"] for n in with_t12["meta"]["notes"])
    c.ok(
        "the note says category assignment is label-based",
        "label" in note_text.lower() and "categor" in note_text.lower(),
        note_text[:400],
    )
    rendered = render_previews.notes_fragment(with_t12)
    c.ok(
        "and a reader sees it on the Notes & Disclosures tab",
        "label" in rendered.lower() and "categor" in rendered.lower(),
        "",
    )
    without_t12, _n2 = asm.assemble(documents=[], generated_on="2026-09-02")
    without_text = " ".join(n["note"] for n in without_t12["meta"]["notes"])
    c.ok(
        "a run with no T-12 does not claim to have categorised one",
        "categor" not in without_text.lower(),
        without_text[:200],
    )

    c.section("no price is invented")
    c.ok("price_per_unit is None with nothing stated", deal["pricing"]["price_per_unit"] is None)
    c.ok("and the deal still validates", not schema.validate(deal))
    priced, _ = asm.assemble(
        documents=documents, om_fields=om_fields,
        notes_values={"price_per_unit": 185000}, generated_on="2026-09-02",
    )
    c.equal("a stated in-band price is taken", priced["pricing"]["price_per_unit"], 185000.0)
    out_of_band, _ = asm.assemble(
        documents=documents, om_fields=om_fields,
        notes_values={"price_per_unit": 7400000}, generated_on="2026-09-02",
    )
    c.ok("an out-of-band figure is refused, not passed through",
         out_of_band["pricing"]["price_per_unit"] is None,
         repr(out_of_band["pricing"]["price_per_unit"]))

    c.section("an upload is never marked synthetic")
    c.equal("meta.synthetic is False", deal["meta"]["synthetic"], False)

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
