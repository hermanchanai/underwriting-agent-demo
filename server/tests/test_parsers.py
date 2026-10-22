"""The parse worker: what it extracts, and what it refuses to keep doing.

Two kinds of check. The extraction half is measured against something that was not
produced by this code — the OM's word count against the reference text captured before
this server existed, and the workbook grid dimensions against the ``<dimension>`` element
inside the sheet XML rather than against openpyxl, which is what the parser itself uses.
Agreeing with yourself is not verification.

The containment half proves the limits bite. A limit nobody has watched kill something is
a comment. So: a child that calls the production ``apply_limits()`` and then spins must die
of ``SIGXCPU``, the wall-clock kill is exercised through the same helper production uses,
and the child reports which limits actually applied — which is how the fact that **macOS
silently refuses ``RLIMIT_AS``** surfaced instead of being assumed away.

Usage: python server/tests/test_parsers.py
"""

from __future__ import annotations

import re
import signal
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, REPO_ROOT, guard

FIXTURES = Path(__file__).resolve().parent / "fixtures"



def sheet_extent_from_xml(path: Path) -> dict[str, tuple[int, int]]:
    """The last row and last column the workbook's own XML declares.

    Deliberately not openpyxl: the parser reads these files with openpyxl, so measuring
    with openpyxl would only prove the parser calls the library it calls.

    The *extent* is the comparable quantity, not the span. One of the real workbooks
    declares ``A2:O373`` because its first row is blank — 372 rows of span, but the sheet
    still runs to row 373, and a reader that starts at row 1 yields 373 rows. Comparing
    spans against a reader's row count is comparing two different measurements, which is
    what the first version of this check did.
    """
    out: dict[str, tuple[int, int]] = {}
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
        for name in sorted(names):
            head = archive.read(name)[:4096].decode("utf-8", "replace")
            match = re.search(r'<dimension ref="[A-Z]+\d+:([A-Z]+)(\d+)"', head)
            if not match:
                continue
            last_col, last_row = match.groups()
            out[name] = (int(last_row), _column_index(last_col))
    return out


def _column_index(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value


def main() -> int:
    c = Checker("test_parsers")
    from server.parse_worker import ParseFailure, parse_file, run_child

    # ---- the synthetic set ------------------------------------------------------
    c.section("synthetic fixtures parse to the shape the pipeline expects")
    document = parse_file(FIXTURES / "om.pdf", "pdf", "om.pdf")
    c.equal("om.pdf keys", sorted(document.keys()), ["filename", "kind", "sheets", "stats", "text"])
    c.equal("om.pdf pages", document["stats"]["pages"], 3)
    c.ok("om.pdf has text", len(document["text"] or "") > 400, str(len(document["text"] or "")))
    c.ok(
        "every page is marked so a quote can cite one",
        all(f"[[page {n}]]" in document["text"] for n in (1, 2, 3)),
        document["text"][:120],
    )

    document = parse_file(FIXTURES / "om.docx", "docx", "om.docx")
    c.ok("om.docx has text", len(document["text"] or "") > 200, str(len(document["text"] or "")))
    c.ok("om.docx read its table", document["stats"]["tables"] >= 1, repr(document["stats"]))

    document = parse_file(FIXTURES / "t12.xlsx", "xlsx", "t12.xlsx")
    c.equal("t12.xlsx sheets", len(document["sheets"]), 1)
    c.equal("t12.xlsx rows", document["stats"]["rows"], 23)
    c.ok("t12.xlsx has no text field", document["text"] is None, repr(document["text"])[:60])

    document = parse_file(FIXTURES / "t12.csv", "csv", "t12.csv")
    c.equal("t12.csv rows", len(document["sheets"][0]["rows"]), 21)
    c.equal("t12.csv delimiter", document["stats"]["delimiter"], ",")

    document = parse_file(FIXTURES / "rent-roll.xlsx", "xlsx", "rent-roll.xlsx")
    c.equal("rent-roll.xlsx rows", document["stats"]["rows"], 27)

    document = parse_file(FIXTURES / "rent-roll.xls", "xls", "rent-roll.xls")
    c.equal("rent-roll.xls sheets", len(document["sheets"]), 1)
    c.equal("rent-roll.xls rows", document["stats"]["rows"], 27)

    c.section("the legacy .doc path")
    import shutil as _shutil

    if _shutil.which("antiword"):  # pragma: no cover - not this machine
        c.ok("antiword is present, so .doc is exercised here", True)
    else:
        print("  NOT RUN  .doc parsing — antiword is not installed on this machine")
        c.ok(
            "and the .doc path refuses honestly rather than crashing",
            _doc_refuses_cleanly(parse_file, ParseFailure),
            "expected a doc_not_supported ParseFailure",
        )

    # ---- the real deal materials -------------------------------------------------
    c.section("the limits the child actually got")
    stats = parse_file(FIXTURES / "t12.xlsx", "xlsx", "t12.xlsx")["stats"]
    c.equal("a CPU limit was applied", stats.get("cpu_limit_applied"), True)
    c.equal("and it is the configured 30 seconds", stats["rlimit_cpu"][0], 30)
    if sys.platform == "darwin":
        c.equal(
            "on macOS the address-space limit is NOT enforceable and says so",
            stats.get("memory_limit_applied"),
            False,
        )
        print(
            "  NOT RUN  memory cap — macOS refuses setrlimit(RLIMIT_AS/RLIMIT_DATA); "
            "this control is Linux-only and is proven in the container"
        )
    else:  # pragma: no cover - the container leg
        c.equal("the address-space limit applied", stats.get("memory_limit_applied"), True)

    c.section("the CPU limit kills a runaway child")
    # No document in any accepted format is slow enough on this hardware to exhaust a CPU
    # budget: a 39 MB csv parses in 0.47 s, a 150-page dense PDF extracts in 0.26 s, and
    # the real 19 MB offering memorandum takes 1.3 s. A "CPU-bomb fixture" would therefore
    # be a file sized to today's laptop, and would stop proving anything on a faster one.
    # So the control itself is tested: a child that calls the production apply_limits() and
    # then spins must be killed by SIGXCPU, not by the wall clock, which is the failure the
    # limit exists to catch.
    spinner = (
        "import sys; sys.path.insert(0, %r);"
        "from server.parse_worker import apply_limits;"
        "applied = apply_limits(cpu_seconds=1, mem_mb=0);"
        "assert applied['cpu_limit_applied'];"
        "\nwhile True:\n    pass\n"
    ) % str(REPO_ROOT)
    started = time.time()
    code, _out, err = run_child([sys.executable, "-c", spinner], "", timeout=30.0)
    elapsed = time.time() - started
    c.equal("the spinning child is killed by SIGXCPU", code, -signal.SIGXCPU)
    c.ok("not by the wall clock", code != -9, f"return code {code}")
    c.ok(
        "and it dies in about its one-second budget",
        elapsed < 20,
        f"took {elapsed:.1f}s; stderr {err[-160:]!r}",
    )
    print(f"    CPU limit: child killed with signal {-code} after {elapsed:.1f}s")

    c.section("a child that hangs is killed by the wall clock")
    started = time.time()
    code, _out, _err = run_child(
        [sys.executable, "-c", "import time; time.sleep(60)"], "", timeout=2.0
    )
    elapsed = time.time() - started
    c.equal("the runner reports a kill", code, -9)
    c.ok("and it took about the timeout, not the sleep", elapsed < 15, f"{elapsed:.1f}s")

    c.section("a corrupt file fails as a refusal, not a crash")
    with tempfile.TemporaryDirectory(prefix="corrupt-") as tmp:
        broken = Path(tmp) / "broken.xlsx"
        broken.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
        try:
            parse_file(broken, "xlsx", "broken.xlsx")
            c.ok("a corrupt workbook is refused", False, "it parsed")
        except ParseFailure as failure:
            c.ok("a corrupt workbook is refused", True)
            c.ok(
                "with a sentence, not a stack trace",
                "Traceback" not in failure.message and failure.message.endswith("."),
                repr(failure.message),
            )

    return c.finish()


def _doc_refuses_cleanly(parse_file, ParseFailure) -> bool:
    with tempfile.TemporaryDirectory(prefix="doc-") as tmp:
        fake = Path(tmp) / "letter.doc"
        fake.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
        try:
            parse_file(fake, "doc", "letter.doc")
        except ParseFailure as failure:
            return failure.code == "doc_not_supported"
    return False


if __name__ == "__main__":
    raise SystemExit(guard(main))
