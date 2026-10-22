"""Write the bundled sample deal's raw documents into ``sample/inputs/``.

The sample is a wholly invented property, "Wildflower Crossing": a 24-home build-for-rent
community at an invented address. Its T-12 and rent roll are the same synthetic workbooks
the parser tests use (``server/tests/fixtures/make_fixtures.py``); its offering memorandum
is the test memorandum plus an address line, so the tax block has a county and state to
work from. No figure in any of these files comes from a real deal.

    python sample/make_sample.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server" / "tests" / "fixtures"))

import make_fixtures as fx  # noqa: E402

INPUTS = HERE / "inputs"

ADDRESS_LINE = "Address: 100 Example Way, Mesa, Maricopa County, AZ 85204 (an invented address)."


def om_pages() -> list[list[str]]:
    pages = [list(page) for page in fx.OM_PAGES]
    # After the disclaimer line on the cover page.
    pages[0].insert(2, ADDRESS_LINE)
    return pages


def main() -> int:
    INPUTS.mkdir(parents=True, exist_ok=True)
    targets = {
        "Wildflower Crossing Offering Memorandum.pdf": lambda p: p.write_bytes(fx.build_pdf(om_pages())),
        "Wildflower Crossing T-12.xlsx": fx.write_t12_xlsx,
        "Wildflower Crossing Rent Roll.xlsx": fx.write_rent_roll_dated_xlsx,
    }
    for name, build in targets.items():
        build(INPUTS / name)
        print(f"  wrote {INPUTS / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
