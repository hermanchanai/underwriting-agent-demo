"""Role detection, on the synthetic set.

The synthetic T-12 buries its header under a property banner and the synthetic rent roll
under a square-footage total, the case a row-1 reader gets wrong. (The rules were also
checked against real exports, which are not part of this repository.)

Usage: python server/tests/test_classify.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, guard

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def main() -> int:
    c = Checker("test_classify")
    from server import classify as cl
    from server.errors import ApiError
    from server.parse_worker import parse_file

    c.section("month headers, in the forms real exports write them")
    for value in (
        dt.datetime(2025, 7, 31),
        dt.date(2025, 7, 31),
        "2025-07-31 00:00:00",
        "Jul-25",
        "jul 2025",
        "07/31/2025",
        "7/2025",
    ):
        c.ok(f"{value!r} reads as a month", cl.is_month_header(value), repr(value))
    for value in ("Account", "Description", "Unit ID", "", None, 1444, "Total"):
        c.ok(f"{value!r} does not", not cl.is_month_header(value), repr(value))

    c.section("the synthetic set")
    cases = [
        ("om.pdf", "pdf", "om"),
        ("om.docx", "docx", "om"),
        ("site-note.docx", "docx", "context"),
        ("t12.xlsx", "xlsx", "t12"),
        ("t12.csv", "csv", "t12"),
        ("rent-roll.xlsx", "xlsx", "rent_roll"),
        ("rent-roll.xls", "xls", "rent_roll"),
        ("trade-out.csv", "csv", "trade_out"),
    ]
    parsed: dict[str, dict] = {}
    for name, kind, expected in cases:
        document = parse_file(FIXTURES / name, kind, name)
        parsed[name] = document
        result = cl.classify(document)
        c.equal(f"{name} -> {expected}", result.role, expected)
        c.ok(f"{name} carries a reason", len(result.reason) > 20, result.reason)

    c.section("the T-12 rule needs twelve months, not six")
    six = {
        "filename": "half-year.xlsx",
        "kind": "xlsx",
        "text": None,
        "sheets": [
            {
                "name": "S",
                "rows": [["Account", "Description"] + [f"Jan-2{n}" for n in range(6)]],
            }
        ],
    }
    result = cl.classify(six)
    c.equal("six month columns is not a T-12", result.role, "context")
    c.ok("and it is flagged unsure rather than settled", not result.confident, result.reason)

    c.section("two files claiming one role is a question, not a guess")
    both = [cl.classify(parsed["t12.xlsx"]), cl.classify(parsed["t12.csv"])]
    try:
        cl.resolve(both)
        c.ok("two T-12s are refused", False, "it picked one")
    except ApiError as exc:
        c.equal("two T-12s -> 400", exc.status, 400)
        c.equal("code", exc.code, "duplicate_role")
        c.ok(
            "and both filenames are named",
            "t12.xlsx" in exc.message and "t12.csv" in exc.message,
            exc.message,
        )

    c.section("two background documents are fine")
    contexts = [cl.classify(parsed["site-note.docx"]), cl.classify(parsed["site-note.docx"])]
    try:
        cl.resolve(contexts)
        c.ok("two context files are allowed through", True)
    except ApiError as exc:
        c.ok("two context files are allowed through", False, exc.message)

    c.section("the documents[] entry the page renders")
    entry = cl.classify(parsed["t12.xlsx"]).as_document()
    c.equal("shape", sorted(entry.keys()), ["filename", "note", "role"])
    c.equal("role", entry["role"], "t12")

    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
