"""What an absent section and an unstated price must look like to a reader.

From the page lane's browser run at 68bcc6a: a rent-roll-only upload printed
``T-12 NOI $0`` on the Deal summary card, sourced "From the T-12 rollup." — a rollup that
never ran, over a statement nobody uploaded. Alongside it, ``Price per unit $0`` and
``Total price $0`` for a deal with no stated price.

That is this project's own stated failure mode, in its own output: a plausible wrong
number where MISSING belongs. Zero is a claim. It says the property earned nothing and
costs nothing, and it is the kind of claim a reader has no reason to doubt because it
looks like every other figure on the card.

So every subset gets checked, not the one that happened to be noticed — and on every
surface a reader actually meets: the summary card, the pricing ladders, closing costs, the
manifest, and the Notes & Disclosures block.

Usage: python server/tests/test_absent_rendering.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, guard
from test_assemble import SUBSETS, SYNTHETIC, prepared


def text_of(html: str) -> str:
    """Rendered text with the tags gone, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def main() -> int:
    c = Checker("test_absent_rendering")
    os.environ.pop("DEMO_MODEL_MODE", None)

    from server import assemble as asm
    import compute
    import render_previews
    import schema
    import write_deal_summary
    import write_manifest

    #: Figures on the summary card that exist only because a T-12 was read.
    T12_DERIVED = ("t12_revenue", "t12_opex", "t12_noi", "t12_cap_rate",
                   "t3_noi_annualized", "t3_cap_rate")
    #: Figures that exist only because somebody stated a price.
    PRICE_DERIVED = ("price_per_unit", "total_price", "t12_cap_rate", "t3_cap_rate")

    for subset in SUBSETS:
        label = "+".join(subset) or "(nothing)"
        documents, om_fields, t12_mapping, roll_mapping = prepared(
            {k: SYNTHETIC[k] for k in subset}
        )
        deal, _notes = asm.assemble(
            documents=documents, om_fields=om_fields,
            t12_mapping=t12_mapping or None, roll_mapping=roll_mapping or None,
            generated_on="2026-09-02", as_of="2026-08-31",
        )
        c.ok(f"{label}: validates", not schema.validate(deal),
             "; ".join(schema.validate(deal))[:200])

        t12_absent = bool(schema.section_absent(deal, "t12"))
        roll_absent = bool(schema.section_absent(deal, "rent_roll"))
        no_price = deal["pricing"]["price_per_unit"] is None

        c.section(f"{label} — nothing derived from an absent input is a number")
        priced = compute.pricing(deal)
        if t12_absent:
            for key in T12_DERIVED:
                c.equal(f"{label}: pricing[{key}] is MISSING", priced[key], schema.MISSING)
        if no_price:
            for key in PRICE_DERIVED:
                c.equal(f"{label}: pricing[{key}] is MISSING", priced[key], schema.MISSING)

        summary_html = render_previews.summary_fragment(deal)
        summary_text = text_of(summary_html)
        # "$0" on a card is the specific thing the browser run caught. There is no figure
        # in this output set for which a bare zero dollars is the right answer.
        zeros = re.findall(r"\$0(?![\d.,])", summary_text)
        c.ok(f"{label}: no '$0' anywhere on the summary card", not zeros,
             f"{len(zeros)} found: ...{summary_text[:200]}")
        if t12_absent:
            c.ok(f"{label}: the card does not claim a T-12 rollup",
                 "from the t-12 rollup" not in summary_text.lower(),
                 summary_text[:200])

        c.section(f"{label} — provenance claims no source that was not provided")
        blob = " ".join(
            f"{st.get('source_doc','')} {st.get('locator','')} {st.get('note','') or ''}"
            for st in deal["provenance"].values()
        ).lower()
        if t12_absent:
            c.ok(f"{label}: no provenance mentions a T-12 rollup",
                 "t-12 rollup" not in blob and "trailing-twelve statement" not in blob,
                 blob[:200])
        for document in documents:
            c.ok(
                f"{label}: {document['filename']} is cited",
                document["filename"].lower() in blob,
                "",
            )

        c.section(f"{label} — both tabs say what was and was not provided")
        for name, html in (("summary", summary_html),
                           ("notes", render_previews.notes_fragment(deal))):
            body = text_of(html).lower()
            if t12_absent:
                c.ok(f"{label}: the {name} tab says no T-12 was provided",
                     "trailing-twelve" in body or "t-12" in body, body[:160])
                c.ok(f"{label}: and says it was not provided",
                     "not provided" in body or "no trailing-twelve" in body
                     or "was not uploaded" in body or "no t-12" in body,
                     body[:200])
            if roll_absent:
                c.ok(f"{label}: the {name} tab says no rent roll was provided",
                     "rent roll" in body, body[:160])

        notes_text = text_of(render_previews.notes_fragment(deal)).lower()
        claim = "computed by code from the uploaded spreadsheets"
        if claim in notes_text:
            # The sentence must name only tabs that exist. Claiming the T-12 tab is
            # computed from an uploaded spreadsheet, when no spreadsheet was uploaded, is
            # the same false-confidence problem one sentence further along.
            index = notes_text.index(claim)
            sentence = notes_text[max(0, index - 200):index]
            if t12_absent:
                c.ok(f"{label}: the computed-tabs sentence does not claim the T-12 tab",
                     "t-12" not in sentence, sentence[-160:])
            if roll_absent:
                c.ok(f"{label}: it does not claim the rent-roll tab",
                     "rent-roll" not in sentence and "rent roll" not in sentence,
                     sentence[-160:])
        else:
            c.ok(f"{label}: the computed-tabs claim is absent when nothing was computed",
                 t12_absent and roll_absent, notes_text[:200])

        c.section(f"{label} — the workbooks and the manifest agree with the card")
        with tempfile.TemporaryDirectory(prefix=f"absent-{label or 'none'}-") as tmp:
            out = Path(tmp)
            summary_path = out / "summary.xlsx"
            write_deal_summary.build(deal, summary_path)
            manifest_path = out / "manifest.xlsx"
            write_manifest.build(manifest_path, deal)

            from openpyxl import load_workbook

            for path in (summary_path, manifest_path):
                workbook = load_workbook(path, data_only=True)
                offenders = []
                for worksheet in workbook.worksheets:
                    rows = list(worksheet.iter_rows(values_only=True))
                    for row in rows:
                        cells = [c for c in row if c is not None]
                        if not cells:
                            continue
                        head = str(cells[0])
                        watched = (
                            (t12_absent and head in ("T-12 NOI", "T-12 revenue",
                                                     "T-12 operating expenses"))
                            or (no_price and head in ("Price per unit", "Total price"))
                        )
                        if watched and any(
                            isinstance(v, (int, float)) and not isinstance(v, bool)
                            for v in cells[1:]
                        ):
                            offenders.append((path.name, head, cells[1:3]))
                workbook.close()
                c.ok(f"{label}: {path.name} has no number where the input is absent",
                     not offenders, repr(offenders)[:220])

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
