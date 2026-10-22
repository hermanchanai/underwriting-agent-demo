"""The market blocks (rent comps, demand rings, competing supply) on the offline path.

In the hosted demo these three blocks come from a separate market-report service that
runs a ring-demographics engine, a permit-pipeline supply query and a rent-scrape comp
query. That service, its data and its client are not part of this repository.

Offline, every block is written ABSENT with the reason. The schema refuses an empty block
that does not say why it is empty, so a tab with no rows still tells the reader what did
not run. The writers and previews are exercised on populated market blocks by the
synthetic fixture in ``tools/fixtures/synthetic_full.json``.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import schema  # noqa: E402

#: The radius the hosted report searches. Kept for the tiering block's shape.
REPORT_RADIUS_MI = 10.0

NOT_CONFIGURED_REASON = (
    "the market-report service is not part of this offline build, so demand, supply and "
    "rent comps were not requested"
)
NO_ADDRESS_REASON = "the memorandum states no street address, so there is nothing to report on"
NO_OM_REASON = "no memorandum was read, so there is no address to report on"


def _absent_comps(reason: str, *, as_of: str, subject_market_rent: float) -> dict:
    return {
        "as_of": as_of or dt.date.today().isoformat(),
        "source": schema.COMP_SOURCE,
        "subject_market_rent": subject_market_rent,
        "rows": [],
        "absent_reason": reason,
        "coverage_note": None,
        "nearby": [],
        "tiering": {
            "subject_product": None,
            "subject_year_built": None,
            "year_band": None,
            "radius_mi": None,
            "top_n": None,
            "additional_n": None,
            "vintage_footnote": reason,
        },
    }


def _absent_supply(reason: str) -> dict:
    return {"rings": [], "projects": [], "ring_stock": None, "absent_reason": reason}


def absent_blocks(reason: str, *, as_of: str, subject_market_rent: float) -> tuple[dict, list[str]]:
    """All three blocks absent for ``reason``: the shape a run carries when nothing ran."""
    text = f"Not run: {reason}."
    blocks = {
        "comps": _absent_comps(text, as_of=as_of, subject_market_rent=subject_market_rent),
        "demand": schema.empty_demand(text),
        "supply": _absent_supply(text),
    }
    return blocks, [f"The market report (demand, supply, comps) did not run: {reason}."]
