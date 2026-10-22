"""A T-12 tells you its own sections; the upload path has to read them, and has to tie.

The ruling after the first live run: the same real statement read
by the raw path (GL codes) and by the upload path (labels) came out $1.3M apart on NOI,
while the statement's own TOTAL INCOME / TOTAL OPERATING EXPENSES / NET OPERATING INCOME
rows sum to the raw path's figures exactly. The pipeline had already found those rows —
the mapping call names them, `parse_t12` records them as `source_subtotals`, the workbook
prints an Error Check against them — and then used none of it: 141 of 204 labels were
bucketed by a keyword layer that never asked the model, the rest by the model without
being told which section a line sits in, and a $1.3M Error Check discrepancy shipped.

Three rules, all spec-derived, all red here first:

1. **The model is the categoriser, and it is shown the section.** Every operating label
   goes to the model with its section from the statement's own subtotal rows —
   ``income`` above TOTAL INCOME, ``opex`` between the subtotals — and picks a vendored
   category from that section's list. Structural rows and lines below the NOI row are
   excluded by construction and never sent. The keyword layer lives on only in the mock.
2. **A category on the wrong side of its section is refused**, like a short answer:
   retried once, then the run fails rather than reading the statement through it.
3. **The read has to tie.** Where the statement prints its subtotals, the reader's
   revenue, operating expenses and NOI must equal them (to the dollar); otherwise the
   T-12 section is withheld with the reason, never shown wrong. A statement that prints
   no subtotals says so in its notes.

Usage: python server/tests/test_t12_sections.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker  # noqa: E402

MONTHS = ["2025-07-31", "2025-08-31", "2025-09-30", "2025-10-31", "2025-11-30", "2025-12-31",
          "2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30"]
HEADER = ["Account", "Description", *MONTHS]

#: (code, label, monthly amount). Subtotal rows carry no code, like the real export.
STATEMENT = [
    ("4010", "MARKET RENT", 100_000.0),
    ("4020", "LOSS/GAIN TO LEASE", -2_000.0),
    ("4030", "VACANCY", -5_000.0),
    ("3121", "PEST CONTROL FEES", 500.0),          # income; the label reads as an expense
    ("3176", "RENTER'S INSURANCE EXPENSE", 300.0),  # income; the label says "expense"
    (None, "TOTAL INCOME", 93_800.0),
    ("5100", "PAYROLL", 10_000.0),
    ("6070", "ELECTRICAL", 1_000.0),
    ("7050", "RECOVERABLE ELEC/GAS/WATER", 2_000.0),  # opex; the label reads as income
    (None, "TOTAL OPERATING EXPENSES", 13_000.0),
    (None, "NET OPERATING INCOME", 80_800.0),
    ("9103", "HVAC", 4_000.0),
    ("9106", "REFRIGERATORS", 1_500.0),
    (None, "TOTAL CAPITAL", 5_500.0),
]

INCOME = ["MARKET RENT", "LOSS/GAIN TO LEASE", "VACANCY", "PEST CONTROL FEES",
          "RENTER'S INSURANCE EXPENSE"]
OPEX = ["PAYROLL", "ELECTRICAL", "RECOVERABLE ELEC/GAS/WATER"]
BELOW = ["HVAC", "REFRIGERATORS"]

#: What a careful model answers, category from the section's own list.
GOOD = {
    "MARKET RENT": "Gross Potential Rent",
    "LOSS/GAIN TO LEASE": "Gain / (Loss) to Lease",
    "VACANCY": "Vacancy",
    "PEST CONTROL FEES": "Other Income",
    "RENTER'S INSURANCE EXPENSE": "Other Income",
    "PAYROLL": "Payroll",
    "ELECTRICAL": "Repairs and Maintenance",
    "RECOVERABLE ELEC/GAS/WATER": "Utilities",
}


def rows() -> list[list]:
    out = [list(HEADER)]
    for code, label, amount in STATEMENT:
        out.append([code, label, *([amount] * 12)])
    return out


def labels() -> list[str]:
    return [label for _code, label, _amount in STATEMENT]


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 100
    output_tokens = 10
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Response:
    def __init__(self, payload) -> None:
        self.content = [_Block(json.dumps(payload))]
        self.stop_reason = "end_turn"
        self.stop_details = None
        self.usage = _Usage()


class _Count:
    input_tokens = 100


class _Messages:
    """Answers each chunk from ``answers`` (label -> category), in call order."""

    def __init__(self, owner, answers: list[dict]) -> None:
        self.owner = owner
        self.answers = list(answers)

    def create(self, **kwargs):
        self.owner.created += 1
        self.owner.requests.append(kwargs)
        answer = self.answers.pop(0) if self.answers else {}
        user = kwargs["messages"][0]["content"]
        asked = [lab for lab in labels() if f"] {lab}\n" in user + "\n" or f"] {lab}" in user]
        mappings = [{"label": lab, "category": answer.get(lab, "exclude")} for lab in asked]
        return _Response({"mappings": mappings, "subtotals": {
            "total_revenue": None, "total_expenses": None, "net_operating_income": None}})

    def count_tokens(self, **kwargs):
        return _Count()


class SectionClient:
    def __init__(self, answers: list[dict]) -> None:
        self.created = 0
        self.requests: list = []
        self.messages = _Messages(self, answers)
        self.beta = type("Beta", (), {"messages": self.messages})()


def _map(answers: list[dict]):
    from server import extract_llm as m

    client = SectionClient(answers)
    os.environ["DEMO_MODEL_MODE"] = "live"
    try:
        return m.map_t12(labels(), HEADER, m.UsageLedger(cap_dollars=1.5), client), None, client
    except Exception as exc:  # noqa: BLE001
        return None, exc, client
    finally:
        os.environ.pop("DEMO_MODEL_MODE", None)


def item_1(c: Checker) -> None:
    c.section("1 — every operating label is asked of the model, with its section; nothing else is")
    from server import extract_llm as m

    sections = m.statement_sections(labels())
    for lab in INCOME:
        c.equal(f"{lab}: income", sections.get(lab), "income")
    for lab in OPEX:
        c.equal(f"{lab}: opex", sections.get(lab), "opex")
    for lab in BELOW:
        c.equal(f"{lab}: below the NOI row", sections.get(lab), "below")
    c.equal("TOTAL INCOME is structural", sections.get("TOTAL INCOME"), "structural")

    result, exc, client = _map([GOOD])
    c.ok("the mapping is accepted", exc is None, f"{type(exc).__name__}: {exc}" if exc else "")
    if result is None:
        return
    user = client.requests[0]["messages"][0]["content"]
    for lab in INCOME:
        c.ok(f"{lab} was sent tagged [income]", f"[income] {lab}" in user, "not tagged")
    for lab in OPEX:
        c.ok(f"{lab} was sent tagged [opex]", f"[opex] {lab}" in user, "not tagged")
    for lab in BELOW + ["TOTAL INCOME", "TOTAL OPERATING EXPENSES", "NET OPERATING INCOME",
                        "TOTAL CAPITAL"]:
        c.ok(f"{lab} was NOT sent", lab not in user, "sent")
    c.equal("asked = every operating line, keyword-layer knowledge or not",
            result["asked"], len(INCOME) + len(OPEX))
    got = {e["label"]: e["category"] for e in result["mappings"]}
    for lab, cat in GOOD.items():
        c.equal(f"{lab} -> the model's answer", got.get(lab), cat)
    for lab in BELOW:
        c.equal(f"{lab} -> exclude, by construction", got.get(lab), m.EXCLUDE)
    c.equal("the below-NOI lines are listed", sorted(result.get("below_noi") or []), sorted(BELOW))
    c.equal("the subtotal rows were found deterministically",
            result["subtotals"], {"total_revenue": "TOTAL INCOME",
                                  "total_expenses": "TOTAL OPERATING EXPENSES",
                                  "net_operating_income": "NET OPERATING INCOME"})
    c.ok("the task text explains the sections",
         "section" in client.requests[0]["messages"][0]["content"].lower(), "no mention")


def item_2(c: Checker) -> None:
    c.section("2 — a category on the wrong side of the section is refused, retried once")
    from server import extract_llm as m

    wrong = dict(GOOD, **{"PEST CONTROL FEES": "Repairs and Maintenance"})
    result, exc, client = _map([wrong, wrong])
    c.ok("an income line answered with an expense category is refused",
         isinstance(exc, m.MappingIncomplete), f"{type(exc).__name__ if exc else 'accepted'}")
    c.equal("after one retry", client.created, 2)
    c.ok("the exception carries the label, never the log",
         exc is not None and "PEST CONTROL FEES" in (getattr(exc, "labels", None) or []),
         repr(getattr(exc, "labels", None)))

    result, exc, client = _map([wrong, GOOD])
    c.ok("a corrected retry is accepted", exc is None, f"{type(exc).__name__}: {exc}" if exc else "")
    c.equal("in two calls", client.created, 2)

    crossed = dict(GOOD, **{"RECOVERABLE ELEC/GAS/WATER": "Utility Reimbursements"})
    result, exc, client = _map([crossed, crossed])
    c.ok("an opex line answered with a revenue category is refused too",
         isinstance(exc, m.MappingIncomplete), f"{type(exc).__name__ if exc else 'accepted'}")


def item_3(c: Checker) -> None:
    c.section("3 — the reader leaves lines below the NOI row out, and says so")
    import source_readers as raw

    label_map = {lab: cat for lab, cat in GOOD.items()}
    block, notes = raw.parse_t12(None, 100, sheet=raw.GridSheet(rows(), "T12"), header_row=1,
                                 code_col=1, label_col=2, label_map=label_map,
                                 subtotal_labels={"total_revenue": "TOTAL INCOME",
                                                  "total_expenses": "TOTAL OPERATING EXPENSES",
                                                  "net_operating_income": "NET OPERATING INCOME"})
    kept = [r["label"] for r in block["rows"]]
    c.ok("HVAC is not a row", not any(k.startswith("HVAC") for k in kept), repr(kept))
    c.ok("a note counts the below-NOI lines as such",
         any("below" in n.lower() and "net operating income" in n.lower() and "2" in n for n in notes),
         repr(notes))
    c.ok("and does not call them unmapped",
         not any("no category mapping" in n for n in notes), repr(notes))
    c.close("the statement's own NOI was read", block["source_subtotals"].get("Net Operating Income"),
            80_800.0 * 12, tol=0.01)


def item_4(c: Checker) -> None:
    c.section("4 — the read ties to the statement's subtotals, or the section is withheld")
    from server import assemble as asm
    import compute

    document = {"filename": "t12.xlsx", "role": "t12", "sheets": [{"name": "T12", "rows": rows()}]}

    def mapping(answers: dict) -> dict:
        return {
            "mappings": [{"label": lab, "category": cat} for lab, cat in answers.items()]
                        + [{"label": lab, "category": "exclude"} for lab in BELOW],
            "subtotals": {"total_revenue": "TOTAL INCOME",
                          "total_expenses": "TOTAL OPERATING EXPENSES",
                          "net_operating_income": "NET OPERATING INCOME"},
            "header_row_index": 0, "code_col": 1, "label_col": 2,
        }

    block, notes = asm.build_t12(document, mapping(GOOD), 100)
    c.equal("a correct mapping keeps the section", block.get("absent_reason"), None)
    pnl = compute.pnl({"t12": block})
    c.close("revenue ties", pnl["total_revenue"], 93_800.0 * 12, tol=0.01)
    c.close("opex ties", pnl["total_opex"], 13_000.0 * 12, tol=0.01)
    c.close("NOI ties", pnl["noi"], 80_800.0 * 12, tol=0.01)
    c.ok("and a note says it ties", any("tie" in n.lower() for n in notes), repr(notes))

    wrong = dict(GOOD, **{"PEST CONTROL FEES": "Repairs and Maintenance"})
    block, notes = asm.build_t12(document, mapping(wrong), 100)
    c.ok("a mapping that does not tie withholds the section",
         bool(block.get("absent_reason")), "absent_reason is empty")
    c.ok("with the two figures in the reason",
         bool(block.get("absent_reason")) and "1,125,600" in block["absent_reason"]
         and "1,119,600" in block["absent_reason"], str(block.get("absent_reason"))[:300])
    c.equal("and no rows", block.get("rows"), [])


def item_5(c: Checker) -> None:
    c.section("5 — a label printed in two sections: the operating occurrence is the one mapped")
    from server import extract_llm as m
    import source_readers as raw

    # One real statement prints MISCELLANEOUS twice: 5550 among the operating expenses ($44.17 over
    # the year) and 7400 below its NOI row. Keyed by label with last-wins, the operating
    # line inherited "below" and was excluded — the one line keeping the read from tying.
    twice = [
        ("4010", "MARKET RENT", 100_000.0),
        (None, "TOTAL INCOME", 100_000.0),
        ("5100", "PAYROLL", 10_000.0),
        ("5550", "MISCELLANEOUS", 44.17),
        (None, "TOTAL OPERATING EXPENSES", 10_044.17),
        (None, "NET OPERATING INCOME", 89_955.83),
        ("7400", "MISCELLANEOUS", 900.0),
        ("9103", "HVAC", 4_000.0),
    ]
    labs = [label for _c, label, _a in twice]
    sections = m.statement_sections(labs)
    c.equal("MISCELLANEOUS is read as the operating line it also is", sections.get("MISCELLANEOUS"),
            "opex")
    c.equal("HVAC is still below", sections.get("HVAC"), "below")

    both = ["INSURANCE REIMBURSEMENT", "TOTAL INCOME", "INSURANCE REIMBURSEMENT",
            "TOTAL OPERATING EXPENSES", "NET OPERATING INCOME"]
    c.equal("a label printed in BOTH income and opex is left to the model as unknown",
            m.statement_sections(both).get("INSURANCE REIMBURSEMENT"), "unknown")

    grid = [list(HEADER)] + [[code, label, *([amount] * 12)] for code, label, amount in twice]
    label_map = {"MARKET RENT": "Gross Potential Rent", "PAYROLL": "Payroll",
                 "MISCELLANEOUS": "General and Administrative"}
    block, notes = raw.parse_t12(None, 100, sheet=raw.GridSheet(grid, "T12"), header_row=1,
                                 code_col=1, label_col=2, label_map=label_map,
                                 subtotal_labels={"total_revenue": "TOTAL INCOME",
                                                  "total_expenses": "TOTAL OPERATING EXPENSES",
                                                  "net_operating_income": "NET OPERATING INCOME"})
    kept = [r["label"] for r in block["rows"]]
    c.ok("the operating MISCELLANEOUS row is read", "MISCELLANEOUS (5550)" in kept, repr(kept))
    c.ok("the below-NOI MISCELLANEOUS row is not", "MISCELLANEOUS (7400)" not in kept, repr(kept))
    import compute
    pnl = compute.pnl({"t12": block})
    c.close("and opex ties to the statement", pnl["total_opex"], 10_044.17 * 12, tol=0.01)


def item_6(c: Checker) -> None:
    c.section("6 — a revenue total with no expense or NOI row leaves the tail unknown")
    from server import extract_llm as m

    open_ended = ["MARKET RENT", "TOTAL INCOME", "PAYROLL", "HVAC", "ROOF REPLACEMENT",
                  "DEBT SERVICE"]
    sections = m.statement_sections(open_ended)
    c.equal("above the revenue total is still income", sections.get("MARKET RENT"), "income")
    for lab in ("PAYROLL", "HVAC", "ROOF REPLACEMENT", "DEBT SERVICE"):
        c.equal(f"{lab}: unknown — nothing closes the section", sections.get(lab), "unknown")
    closed = ["MARKET RENT", "TOTAL INCOME", "PAYROLL", "TOTAL OPERATING EXPENSES"]
    c.equal("with an expense total the tail is opex again",
            m.statement_sections(closed).get("PAYROLL"), "opex")
    noi_only = ["MARKET RENT", "TOTAL INCOME", "PAYROLL", "NET OPERATING INCOME", "HVAC"]
    s2 = m.statement_sections(noi_only)
    c.equal("with an NOI row and no expense total, before NOI is opex", s2.get("PAYROLL"), "opex")
    c.equal("and after it is below", s2.get("HVAC"), "below")


def item_7(c: Checker) -> None:
    c.section("7 — a summary-first layout: totals printed ABOVE the detail")
    from server import extract_llm as m
    import source_readers as raw
    import compute

    summary_first = [
        (None, "TOTAL INCOME", 93_800.0),
        (None, "TOTAL OPERATING EXPENSES", 13_000.0),
        (None, "NET OPERATING INCOME", 80_800.0),
    ] + STATEMENT
    labs = [label for _c, label, _a in summary_first]
    sections = m.statement_sections(labs)
    for lab in INCOME:
        c.equal(f"{lab}: income, below the summary block", sections.get(lab), "income")
    for lab in OPEX:
        c.equal(f"{lab}: opex", sections.get(lab), "opex")
    for lab in BELOW:
        c.equal(f"{lab}: below the LAST NOI row", sections.get(lab), "below")

    grid = [list(HEADER)] + [[code, label, *([amount] * 12)] for code, label, amount in summary_first]
    block, notes = raw.parse_t12(None, 100, sheet=raw.GridSheet(grid, "T12"), header_row=1,
                                 code_col=1, label_col=2, label_map=dict(GOOD),
                                 subtotal_labels={"total_revenue": "TOTAL INCOME",
                                                  "total_expenses": "TOTAL OPERATING EXPENSES",
                                                  "net_operating_income": "NET OPERATING INCOME"})
    kept = [r["label"] for r in block["rows"]]
    c.equal("the detail below the summary block is read: eight operating lines", len(kept), 8)
    c.ok("HVAC, after the last NOI row, is not", not any(k.startswith("HVAC") for k in kept), repr(kept))
    pnl = compute.pnl({"t12": block})
    c.close("and it ties", pnl["noi"], 80_800.0 * 12, tol=0.01)


def item_8(c: Checker) -> None:
    c.section("8 — a read that keeps no lines is withheld, never an empty present section (P2)")
    from server import assemble as asm
    import schema

    document = {"filename": "t12.xlsx", "role": "t12", "sheets": [{"name": "T12", "rows": rows()}]}
    everything_excluded = {
        "mappings": [{"label": lab, "category": "exclude"} for lab in labels()],
        "subtotals": {"total_revenue": "TOTAL INCOME",
                      "total_expenses": "TOTAL OPERATING EXPENSES",
                      "net_operating_income": "NET OPERATING INCOME"},
        "header_row_index": 0, "code_col": 1, "label_col": 2,
    }
    block, notes = asm.build_t12(document, everything_excluded, 100)
    c.ok("the section is withheld", bool(block.get("absent_reason")), "absent_reason is empty")
    c.ok("with the statement's own revenue in the reason",
         bool(block.get("absent_reason")) and "1,125,600" in block["absent_reason"],
         str(block.get("absent_reason"))[:300])
    c.equal("no rows", block.get("rows"), [])
    deal = {"t12": block}
    c.ok("schema.section_absent sees an absent section, not an empty present one",
         bool(schema.section_absent(deal, "t12")), repr(schema.section_absent(deal, "t12")))


def item_9(c: Checker) -> None:
    c.section("9 — the tie tolerance scales with the cells that were rounded (P2)")
    from server import assemble as asm
    import compute

    # Twenty lines of 100.004999 a month: each cell rounds DOWN half a cent, 240 cells,
    # $1.20 of drift against a subtotal read unrounded — a correct read that a flat $1
    # tolerance withholds.
    per_cell = 100.004999
    fractional = [(f"4{i:03d}", f"INCOME LINE {i}", per_cell) for i in range(20)]
    fractional += [(None, "TOTAL INCOME", per_cell * 20),
                   ("5100", "PAYROLL", 10.0),
                   (None, "TOTAL OPERATING EXPENSES", 10.0),
                   (None, "NET OPERATING INCOME", per_cell * 20 - 10.0)]
    grid = [list(HEADER)] + [[code, label, *([amount] * 12)] for code, label, amount in fractional]
    document = {"filename": "t12.xlsx", "role": "t12", "sheets": [{"name": "T12", "rows": grid}]}
    mapping = {
        "mappings": [{"label": f"INCOME LINE {i}", "category": "Other Income"} for i in range(20)]
                    + [{"label": "PAYROLL", "category": "Payroll"}],
        "subtotals": {"total_revenue": "TOTAL INCOME",
                      "total_expenses": "TOTAL OPERATING EXPENSES",
                      "net_operating_income": "NET OPERATING INCOME"},
        "header_row_index": 0, "code_col": 1, "label_col": 2,
    }
    block, notes = asm.build_t12(document, mapping, 100)
    c.equal("a correctly read statement with rounding drift is NOT withheld",
            block.get("absent_reason"), None)
    if not block.get("absent_reason"):
        pnl = compute.pnl({"t12": block})
        drift = abs(pnl["total_revenue"] - block["source_subtotals"]["Total Revenue"])
        c.ok(f"the drift really exceeded a flat dollar ({drift:.2f})", drift > 1.0, f"{drift:.3f}")
    c.ok("and the tie note still appears", any("tie" in n.lower() for n in notes), repr(notes))


def main() -> int:
    c = Checker("test_t12_sections")
    # 3 and 4 first: they exercise the reader and the tie-out, which exist today, so a RED
    # shows the behaviour that was wrong before it hits the names 1 and 2 introduce.
    item_3(c)
    item_4(c)
    item_1(c)
    item_2(c)
    item_5(c)
    item_6(c)
    item_7(c)
    item_8(c)
    item_9(c)
    return c.finish()


if __name__ == "__main__":
    sys.exit(main())
