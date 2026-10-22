"""Parsed documents plus mappings, into the one interchange file everything else reads.

This is where the upload path rejoins the pipeline. From here on nothing knows or cares
that the deal arrived over HTTP: the writers, the renderer and the manifest read
``deal_outputs.json`` and ``tools/schema.py``, exactly as they do for the showcase set.

The whole design question here is what to do about what is missing, because a visitor
uploads what they have. A T-12 and nothing else. A marketing PDF on its own. The answer
this project has always given is that **a stated absence is a deliverable and a plausible
number is not** — so a section with no input carries ``absent_reason`` and validates,
rather than carrying twelve months of zeroes and validating. That rule already governed
``trade_out``; it is extended to ``t12`` and ``rent_roll``, and this module is its main
customer.

Three consequences worth stating, because each one is a decision someone could reasonably
have made the other way:

* **No price is invented.** ``pricing.price_per_unit`` is ``None`` unless the offering
  memorandum or the notes box states one inside the band a real per-unit sale price
  occupies. Every cap rate downstream then reads MISSING rather than being computed off a
  placeholder — which is the only honest answer, and the one that keeps the demo from
  quietly showing a stranger a return on a number nobody supplied.
* **Comps, demand, supply and closing costs are absent by construction**, not by accident.
  They need data sources the demo does not reach, and they say so in the output.
* **``meta.synthetic`` is False.** The site publish guard refuses anything not marked
  synthetic, and the upload path must never be publishable. The server never calls
  ``publish_to_site.py``, and this makes the guard's answer correct anyway.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.append(str(TOOLS_DIR))

import compute  # noqa: E402
import source_readers as raw  # noqa: E402
import schema  # noqa: E402

log = logging.getLogger("upload_demo.assemble")

NOT_RUN = (
    "Not run in the upload demo. This section needs data sources the demo does not "
    "reach, so it is left empty rather than filled with placeholders."
)

NO_T12 = "No trailing-twelve income statement was uploaded, so there is nothing to analyse."
NO_ROLL = "No rent roll was uploaded, so there is nothing to analyse."
NO_TRADE_OUT = (
    "The upload demo does not ask for a lease trade-out report, so there is no basis for "
    "a new-versus-renewal block."
)

#: Which workbook each section feeds. A workbook whose inputs are absent is not written —
#: an empty T-12 analysis is not a deliverable, it is a file that wastes a click.
WORKBOOK_INPUTS: dict[str, tuple[str, ...]] = {
    "pack": (),  # every applicable tab, in one workbook; absent sections are skipped inside
    "deal_summary": (),          # always written: it can state what is known and what is not
    "t12_analysis": ("t12",),
    "rent_roll": ("rent_roll",),
    "manifest": (),              # always written: the manifest's job is to list the holes
}


def applicable_workbooks(deal: dict) -> list[str]:
    """The workbook keys worth writing for this deal, in ``schema.OUTPUT_FILES`` order."""
    out = []
    for key in schema.OUTPUT_FILES:
        needed = WORKBOOK_INPUTS.get(key, ())
        if all(not schema.section_absent(deal, section) for section in needed):
            out.append(key)
    return out


# --------------------------------------------------------------------------------------
# Section builders
# --------------------------------------------------------------------------------------


def _sheet_of(document: dict, mapping: dict | None = None) -> raw.GridSheet | None:
    """The sheet the classifier actually recognised, not simply the first one.

    A workbook whose first tab is a cover page and whose second holds the statement is
    ordinary. Classification scores every sheet and records which one it chose; reading
    sheets[0] regardless meant such a file was correctly identified and then parsed from
    the wrong tab, producing an absent section from a file that had the data in it.
    """
    sheets = document.get("sheets") or []
    if not sheets:
        return None
    index = (mapping or {}).get("sheet_index")
    if not isinstance(index, int) or not 0 <= index < len(sheets):
        index = 0
    chosen = sheets[index]
    return raw.GridSheet(chosen.get("rows") or [], chosen.get("name") or "Sheet1")


#: What the two rent-roll knobs are when the visitor has not said otherwise.
DEFAULT_LOOKBACK_MONTHS = 12
DEFAULT_TOLERANCE_DAYS = 31

#: How far into a sheet to look for the date it says it was run on. A roll states it in
#: its banner; twelve rows covers every real one seen, and reading further would start
#: finding lease dates in the data.
AS_OF_SEARCH_ROWS = 12

#: The phrases a roll uses to introduce its own reporting date.
AS_OF_PHRASES: tuple[str, ...] = (
    "as of", "as-of", "as at", "report date", "reporting date", "run date",
    "date:", "period ending", "for the period ending", "through",
)

_AS_OF_FORMATS: tuple[str, ...] = (
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%d %B %Y", "%B %d, %Y",
    "%b %d, %Y", "%B %d %Y", "%b %d %Y",
)

_DATE_IN_TEXT = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}-\d{4}|"
    r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})"
)


def _as_iso(text: str) -> str | None:
    """One written date into ISO, or None when it is not a date."""
    candidate = (text or "").strip().rstrip(".")
    if not candidate:
        return None
    for fmt in _AS_OF_FORMATS:
        try:
            return dt.datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


#: How a roll names the MONTH it covers when it does not name a day.
#:
#: The real one writes ``Period: 202608``; the other shape in the wild is
#: ``Period ending 08/2026``. A stated period IS a stated as-of — the roll is telling you
#: which month it closes — so it belongs on the stated rung and not on the inferred one.
#: It resolves to the LAST day of that month, which is what "the period ending" means, and
#: the run's notes say so by name rather than presenting it as a date the file gave.
_PERIOD_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bperiod(?:\s+ending)?\s*:?\s*(\d{4})(\d{2})\b", re.I), "ym"),
    (re.compile(r"\bperiod(?:\s+ending)?\s*:?\s*(\d{1,2})/(\d{4})\b", re.I), "my"),
    (re.compile(r"\bperiod(?:\s+ending)?\s*:?\s*([A-Za-z]{3,9})\.?\s+(\d{4})\b", re.I),
     "name"),
)

_MONTH_NAMES = {
    name.lower(): number
    for number, names in enumerate(
        (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")),
        start=1,
    )
    for name in names
}


def _last_day(year: int, month: int) -> str | None:
    """The last day of that month, ISO, or None when the pair is not a real month."""
    import calendar

    if not (1 <= month <= 12) or not (1900 <= year <= 2200):
        return None
    return dt.date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _stated_period(text: str) -> tuple[str, str] | None:
    """``(iso last day, the period as the document wrote it)``, or None."""
    for pattern, shape in _PERIOD_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        first, second = match.group(1), match.group(2)
        if shape == "ym":
            iso = _last_day(int(first), int(second))
            written = f"{first}{second}"
        elif shape == "my":
            iso = _last_day(int(second), int(first))
            written = f"{first}/{second}"
        else:
            month = _MONTH_NAMES.get(first.lower())
            iso = _last_day(int(second), month) if month else None
            written = f"{first} {second}"
        if iso:
            return iso, written
    return None


def stated_as_of(
    document: dict | None, mapping: dict | None
) -> tuple[str | None, str | None]:
    """What the ROLL says about when it was run, read out of its own banner rows.

    Returns ``(iso date, stated period)``. The period is None when the file named a DAY;
    when it named only a month, the date is that month's last day and the period is the
    text the file used, so the disclosure can quote it.

    A rent roll is a snapshot, and every window this pipeline measures — the move-in and
    lease-signed lookback, annualised turnover — is measured back from the date of that snapshot. Using
    the day of the UPLOAD instead silently reports a 2025 roll's turnover as if the file
    had been produced today, which on a historical roll is not a small error: every
    move-in falls outside the window and the answer is zero.

    Only the rows above and including the header are read — the banner — so a lease date
    in the data can never be mistaken for the report date. A stated DAY always wins over a
    stated month, including when the same banner carries both.
    """
    if not document:
        return None, None
    sheets = document.get("sheets") or []
    index = (mapping or {}).get("sheet_index") or 0
    rows = (sheets[index].get("rows") if index < len(sheets) else None) or []
    header_row = (mapping or {}).get("header_row_index")
    limit = min(len(rows), AS_OF_SEARCH_ROWS)
    if isinstance(header_row, int):
        limit = min(limit, header_row + 1)
    banner = [
        [str(cell).strip() for cell in row if cell not in (None, "")]
        for row in rows[:limit]
    ]

    # Pass one: a stated DAY, anywhere in the banner.
    for cells in banner:
        joined = " ".join(cells)
        if not any(phrase in joined.lower() for phrase in AS_OF_PHRASES):
            continue
        for match in _DATE_IN_TEXT.finditer(joined):
            iso = _as_iso(match.group(1))
            if iso:
                return iso, None
        # A date-typed cell beside the phrase, already normalised to ISO by the parser.
        for cell in cells:
            iso = _as_iso(cell)
            if iso:
                return iso, None

    # Pass two, and only then: a stated MONTH. Second on purpose — a banner carrying both
    # "As of 08/04/2026" and "Period: 202608" has told you the day.
    for cells in banner:
        period = _stated_period(" ".join(cells))
        if period:
            return period
    return None, None


def valid_as_of(value) -> str:
    """One as-of candidate, or ``""`` when it is not a day that exists.

    Every rung of the as-of ladder passes through here, because ``YYYY-MM-DD`` is a shape
    and not a date: ``2026-02-31`` matches every check this pipeline made of it, wins the
    priority, and passes ``schema.validate`` — and then ``compute.parse_date`` returns
    None, so the run publishes an impossible as-of and silently omits every window
    measured from it. Skipping is the answer rather than repairing: the
    next rung is a date some document actually stated, and guessing which of the month or
    the day was mistyped would be inventing one.
    """
    text = str(value or "").strip()
    return text if compute.parse_date(text) is not None else ""


def latest_roll_date(units: list[dict]) -> str | None:
    """The most recent lease-start or move-in date the roll itself carries.

    The documented fallback, and a fallback rather than a reading: a roll cannot have been
    produced before its own newest lease, so this is the EARLIEST date the snapshot can
    have been taken. It is never exact, which is why it arrives with a disclosure note.

    The parser has already normalised every date it could read to ISO, so anything that is
    not ISO here is a cell it could not read as a date and is ignored rather than guessed.
    A date-SHAPED cell is not enough: ``max`` is over strings, so one ``2026-11-31`` on any
    unit outranked every real date on the roll and became the inferred as-of.
    """
    seen = {
        valid_as_of(value)
        for unit in units or []
        for value in (unit.get("lease_start"), unit.get("move_in"))
        if isinstance(value, str) and valid_as_of(value)
    }
    return max(seen) if seen else None


AS_OF_PERIOD_NOTE = (
    "The uploaded rent roll stated the PERIOD it covers rather than a day: "
    "as-of taken as {date}, the last day of the stated period {period}. Every window on "
    "this page (the move-in and lease-signed lookback, and annualised turnover) is measured back from "
    "that date, not from the day of the upload."
)

AS_OF_INFERRED_NOTE = (
    "The uploaded rent roll did not state the date it was run, so the as-of date was "
    "INFERRED as {date} — the most recent lease start or move-in on the roll, which is "
    "the earliest date the snapshot can have been taken. Every window on this page (the "
    "move-in and lease-signed lookback, and annualised turnover) is measured back from that date. It is "
    "not the upload date, and it is not today."
)

AS_OF_INVALID_NOTE = (
    "An as-of date of {value} came from {source}, but there is no such day on the "
    "calendar, so it was not used. The as-of date on this page is the next one a source "
    "actually stated; every window that depends on it (the move-in and lease-signed "
    "lookback, and annualised turnover) is measured back from that date."
)

#: How each rung is named in `AS_OF_INVALID_NOTE`. The visitor is told which of their
#: sources was skipped, in their own terms.
AS_OF_SOURCE_NAMES = {
    "caller": "this run's own settings",
    "notes": "the notes box",
    "stated": "the uploaded rent roll's header",
}

AS_OF_UNKNOWN_NOTE = (
    "The uploaded rent roll stated no as-of date and carries no lease or move-in date to "
    "infer one from, so no as-of date was established. Every window that depends on one — "
    "the move-in and lease-signed lookback, and annualised turnover — reads MISSING rather than being "
    "measured from the day of the upload."
)


STATUS_TRUNCATION_NOTE = (
    "{count} status {noun} on this roll {verb} longer than the {chars} characters the "
    "column mapper is shown, so the mapping was matched on the first {chars} characters "
    "and applied to the roll's full value. Each full value begins with the key it was "
    "matched to; nothing was guessed, and no unit was left unmapped because of it."
)


def expand_truncated_statuses(
    status_map: dict, sheet, status_col: int | None, header_row: int
) -> tuple[dict, int]:
    """Map the roll's FULL status values onto the truncated ones the model answered with.

    The vocabulary a status column contributes to the prompt is truncated
    (``extract_llm.STATUS_VALUE_CHARS``), so a very long status arrives back keyed by its
    first forty characters — and the parser looks the value up by the cell's whole text.
    Without this the lookup misses and the unit falls through to Non-revenue, which is the
    exact failure the bounded vocabulary exists to stop, just at a different length.

    Deterministic and parent-side: the roll's own cells are the authority, and a full value
    is only bound to a key that is a genuine prefix of it.

    Returns ``(map, resolved)``. The COUNT is returned because it has to be disclosed: the
    mapping applied to those units is one the model never saw in full, and a reader is
    entitled to know that before reading an occupancy figure derived from it.
    """
    if not status_map or not status_col:
        return status_map, 0
    from server.extract_llm import STATUS_VALUE_CHARS

    truncated = {
        key: meaning for key, meaning in status_map.items()
        if len(key) >= STATUS_VALUE_CHARS
    }
    if not truncated:
        return status_map, 0
    from server.extract_llm import hashed_status_key

    expanded = dict(status_map)
    resolved = 0
    for row in range(header_row + 1, sheet.max_row + 1):
        value = str(sheet.cell(row, status_col).value or "").strip()
        if not value or value in expanded:
            continue
        # Two candidate keys, both pure functions of this full value: the plain truncation,
        # and the digest-suffixed form the profiler uses when two values would otherwise
        # collide. A collided value is never keyed by the plain form, so
        # a hit on either is exact.
        meaning = (truncated.get(value[:STATUS_VALUE_CHARS])
                   or truncated.get(hashed_status_key(value)))
        if meaning:
            expanded[value] = meaning
            resolved += 1
    return expanded, resolved


def build_rent_roll(
    document: dict | None,
    mapping: dict | None,
    as_of: str,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    new_tenant_tolerance_days: int = DEFAULT_TOLERANCE_DAYS,
) -> tuple[dict, list[str], dict]:
    """The ``rent_roll`` block, or a declared absence.

    ``lookback_months`` and ``new_tenant_tolerance_days`` are the two knobs
    ``parse_notes`` is allowed to set, and they were being parsed, validated, carried all
    the way here — and then ignored in favour of 12 and 31. A visitor who asks for a
    six-month lookback and gets a twelve-month one is reading turnover for a window they
    did not choose, with nothing on the page saying so.
    """
    sheet = _sheet_of(document, mapping) if document else None
    if sheet is None or not mapping:
        return (
            {
                "absent_reason": NO_ROLL,
                "as_of": "",
                "lookback_months": lookback_months,
                "new_tenant_tolerance_days": new_tenant_tolerance_days,
                "units": [],
                "stated": {},
            },
            [],
            {},
        )

    # 0-based from the mapper, 1-based for the parser's worksheet-shaped indexing.
    columns = {
        role: (index + 1)
        for role, index in (mapping.get("columns") or {}).items()
        if isinstance(index, int)
    }
    header_row = (mapping.get("header_row_index") or 0) + 1
    # The mapper worked out what this export's status strings mean; discarding that made
    # every "O"/"V" roll read as Non-revenue.
    status_map = {
        str(entry.get("raw")): str(entry.get("meaning"))
        for entry in (mapping.get("status_vocabulary") or [])
        if entry.get("raw") and entry.get("meaning")
    }
    status_map, resolved_statuses = expand_truncated_statuses(
        status_map, sheet, columns.get("status"), header_row
    )
    units, notes, stats = raw.parse_rent_roll(
        None, as_of, sheet=sheet, header_row=header_row, columns=columns,
        status_map=status_map or None,
    )
    from server.extract_llm import STATUS_VALUE_CHARS

    if resolved_statuses:
        notes.append(
            STATUS_TRUNCATION_NOTE.format(
                count=resolved_statuses,
                noun="value" if resolved_statuses == 1 else "values",
                verb="was" if resolved_statuses == 1 else "were",
                chars=STATUS_VALUE_CHARS,
            )
        )
    if not units:
        return (
            {
                "absent_reason": (
                    "The uploaded rent roll produced no readable unit rows, so the "
                    "section is left empty rather than partly filled."
                ),
                "as_of": "",
                "lookback_months": lookback_months,
                "new_tenant_tolerance_days": new_tenant_tolerance_days,
                "units": [],
                "stated": {},
            },
            notes,
            {},
        )
    return (
        {
            "absent_reason": None,
            "as_of": as_of,
            "lookback_months": lookback_months,
            "new_tenant_tolerance_days": new_tenant_tolerance_days,
            "units": units,
            "stated": raw._roll_stated(stats),
        },
        notes,
        stats,
    )


def build_t12(document: dict | None, mapping: dict | None, units: int) -> tuple[dict, list[str]]:
    """The ``t12`` block, or a declared absence."""
    sheet = _sheet_of(document, mapping) if document else None
    if sheet is None:
        return (
            {
                "absent_reason": NO_T12,
                "month_labels": [],
                # An absent section has no unit count, and 0 is a number rather than an
                # absence — the same rule the summary and the pricing block follow since
                # round 19.
                "units": schema.MISSING,
                "rows": [],
                "source_subtotals": {},
            },
            [],
        )

    mapping = mapping or {}
    label_map = {
        m["label"]: m["category"]
        for m in mapping.get("mappings", [])
        if m.get("category") and m["category"] != "exclude"
    }
    header_row = (mapping.get("header_row_index") or 0) + 1
    try:
        block, notes = raw.parse_t12(
            None,
            units,
            sheet=sheet,
            header_row=header_row,
            code_col=mapping.get("code_col", 1),
            label_col=mapping.get("label_col", 2),
            label_map=label_map,
            # The mapping call names this statement's own Total Revenue / Total Operating
            # Expenses / NOI rows and this is where that answer was being dropped: the
            # rows were identified and then the parser was left to guess from a handful of
            # hardcoded spellings, so a differently worded statement lost its tie-out.
            subtotal_labels=mapping.get("subtotals") or None,
        )
    except ValueError as exc:
        # The TYPE, not the message: `parse_t12` raises with whatever it found in the
        # visitor's statement, and this log outlives their run. The
        # absent-reason below is what they are told, on their own run.
        log.warning("t12: the statement could not be read (%s)", type(exc).__name__)
        return (
            {
                "absent_reason": (
                    f"The uploaded statement could not be read as a trailing twelve "
                    f"months ({exc}), so the section is left empty."
                ),
                "month_labels": [],
                # An absent section has no unit count, and 0 is a number rather than an
                # absence — the same rule the summary and the pricing block follow since
                # round 19.
                "units": schema.MISSING,
                "rows": [],
                "source_subtotals": {},
            },
            [],
        )
    block["absent_reason"] = None
    candidates = set(block.pop("running_subtotals", None) or [])
    tie_note, tie_failure = _tie_out(block)
    if tie_failure and candidates:
        # Rows that equal the lines directly above them. Only the statement's own
        # printed totals may say they are its running subtotals: kept as lines unless
        # reading them as subtotals is what makes the read tie.
        names = "; ".join(row["label"] for i, row in enumerate(block["rows"]) if i in candidates)
        alt = dict(block, rows=[row for i, row in enumerate(block["rows"]) if i not in candidates])
        alt_note, alt_failure = _tie_out(alt)
        found = (f"{len(candidates)} row(s) equal, month by month, the sum of the lines "
                 f"directly above them: {names}.")
        block = alt
        if alt_failure is None:
            tie_note, tie_failure = alt_note, None
            notes.append(found + " Read as the statement's own running subtotals rather than "
                         "as lines, the read ties to its printed totals, so they were.")
        else:
            # Withheld either way; the figures and causes below are the closer reading.
            tie_failure = (alt_failure + " " + found + " The figures above read them as the "
                           "statement's own running subtotals, not as lines.")
    if tie_failure:
        # The statement printed its own totals and ours do not match them. A T-12 shown
        # with the wrong NOI is worse than no T-12: withheld, with
        # both figures, rather than published with an Error Check nobody reads.
        log.warning("t12: the read does not tie to the statement's own subtotals")
        flipped = _positive_deductions(block)
        if flipped:
            tie_failure += (
                f" The statement's own revenue total subtracts {flipped}, printed with the "
                f"opposite sign: read with that sign reversed, its revenue ties. The sign is "
                f"not reversed on your behalf; the section is withheld instead."
            )
        two_sided = [label for label in mapping.get("two_sided") or [] if label]
        if two_sided:
            # The cause the totals alone do not show: the map is keyed by label, so a line
            # printed under income and again under expenses takes ONE category and one of
            # its rows lands on the wrong side. Visitor text, on their own run.
            shown = ", ".join(repr(label) for label in two_sided[:5])
            more = f" and {len(two_sided) - 5} more" if len(two_sided) > 5 else ""
            tie_failure += (
                f" The statement prints {shown}{more} under both its income and its "
                f"operating expenses; one category cannot serve both rows, which is where "
                f"the read and the statement most likely part."
            )
        return (
            {
                "absent_reason": tie_failure,
                "month_labels": [],
                "units": schema.MISSING,
                "rows": [],
                "source_subtotals": block.get("source_subtotals") or {},
            },
            notes,
        )
    if tie_note:
        notes.append(tie_note)
    return block, notes


#: The least slack a subtotal gets. The real bound is half a cent per rounded cell —
#: `parse_t12` rounds every account-month cell to cents while the statement's subtotal is
#: read as printed — so twenty fractional-cent lines over twelve months can drift $1.20
#: on a correct read. The tolerance is that bound, never less than a dollar.
TIE_TOLERANCE = 1.00
ROUNDING_PER_CELL = 0.005
#: Between "ties" and "withheld": a read within this share of the statement's own figure
#: is KEPT, with the gap named in the notes and the audit trail's error check reading
#: REVIEW on the lines that differ. The first real accounting-system statement through the engine read
#: revenue to within $260 of $1.87M and NOI to within 0.25% — two small lines the mapper
#: placed differently — and the $1 rule withheld the whole section .
#: A wildly wrong read (the $1.3M NOI miss the rule was made for) is still withheld.
TIE_REVIEW_BAND = 0.02


#: Revenue categories that are deductions: a statement subtracts them from rent.
_DEDUCTIONS = ("Vacancy", "Concessions", "Non-Revenue Units", "Delinquency")


def _positive_deductions(block: dict) -> str | None:
    """The run of revenue lines a statement printed POSITIVE and subtracted, or None.

    A statement that prints "Bad Debt 113,586" and deducts it reads, line for line, as
    revenue 2 × 113,586 higher than its own total. Named only when the
    arithmetic says so: a run of consecutive revenue lines, at least one a deduction
    category with a positive total, whose sum is exactly half the revenue gap.
    """
    import compute

    stated = (block.get("source_subtotals") or {}).get("Total Revenue")
    rows = block.get("rows") or []
    if not isinstance(stated, (int, float)) or not rows:
        return None
    gap = float(compute.pnl({"t12": block})["total_revenue"]) - float(stated)
    if gap <= 0:
        return None
    cells = sum(len(row.get("monthly") or []) for row in rows)
    tolerance = max(TIE_TOLERANCE, ROUNDING_PER_CELL * cells + 0.01)
    totals = [sum(row.get("monthly") or []) for row in rows]
    for start in range(len(rows)):
        run = 0.0
        for end in range(start, len(rows)):
            if rows[end].get("category") not in schema.T12_REVENUE_CATEGORIES:
                break
            run += totals[end]
            if abs(2 * run - gap) <= tolerance and any(
                rows[i].get("category") in _DEDUCTIONS and totals[i] > 0
                for i in range(start, end + 1)
            ):
                return ", ".join(repr(rows[i]["label"]) for i in range(start, end + 1))
    return None


def _tie_out(block: dict) -> tuple[str | None, str | None]:
    """``(note, failure)``: one of them, or neither when the statement prints no totals.

    The statement's own TOTAL INCOME / TOTAL OPERATING EXPENSES / NET OPERATING INCOME
    rows are the answer key for the read. Where the statement prints them, ours must
    equal them; the comparison is the same one the workbook's Error Check table prints,
    made binding here instead of decorative.
    """
    import compute

    source = block.get("source_subtotals") or {}
    if not block.get("rows"):
        # Nothing was read. Against a statement that prints its totals that is a read
        # that failed to tie by the whole statement; against one that does not, it is a
        # statement with no operating lines. Either way the section is withheld — an
        # empty PRESENT section is what schema validation refuses.
        printed = [
            f"{name} {float(source[key]):,.0f}"
            for name, key in (("revenue", "Total Revenue"),
                              ("operating expenses", schema.PNL_TOTAL_OPEX_LABEL),
                              ("NOI", schema.PNL_NOI_LABEL))
            if isinstance(source.get(key), (int, float))
        ]
        if printed:
            return None, (
                "The reader kept no operating lines from a statement that prints "
                + ", ".join(printed)
                + ", so the trailing-twelve section was withheld rather than shown empty."
            )
        return None, (
            "No operating lines could be read from the statement, so the trailing-twelve "
            "section was withheld."
        )
    ours = compute.pnl({"t12": block})
    # Every cell of a row that carries an amount, as before; a row whose every month is
    # zero is a STATED zero the reader now keeps and carries no rounding, so it
    # does not widen the band — twenty of them turned a $1.10 gap into a tie. Counting nonzero CELLS instead shrank the band for real rows'
    # zero months. Limit: a row whose every month is a sub-cent amount
    # rounds to all zeros and is not counted.
    cells = sum(len(row.get("monthly") or []) for row in block["rows"] if any(row.get("monthly") or []))
    tolerance = max(TIE_TOLERANCE, ROUNDING_PER_CELL * cells + 0.01)
    pairs = (
        ("revenue", "Total Revenue", ours["total_revenue"]),
        ("operating expenses", schema.PNL_TOTAL_OPEX_LABEL, ours["total_opex"]),
        ("NOI", schema.PNL_NOI_LABEL, ours["noi"]),
    )
    checked: list[str] = []
    review: list[str] = []
    failed: list[str] = []
    for name, key, mine in pairs:
        stated = source.get(key)
        if not isinstance(stated, (int, float)):
            continue
        gap = float(mine) - float(stated)
        if abs(gap) <= tolerance:
            checked.append(name)
        elif abs(gap) <= abs(float(stated)) * TIE_REVIEW_BAND:
            review.append(f"{name} {mine:,.0f} against the statement's {stated:,.0f} ({gap:+,.0f})")
        else:
            failed.append(f"{name} {mine:,.0f} against the statement's {stated:,.0f}")
    if failed:
        # Every figure that differs is named, the within-band ones included: a reader of
        # the reason should see the whole read against the whole statement.
        return None, (
            "The reader's totals do not tie to the statement's own subtotals ("
            + "; ".join(failed + review)
            + "), so the trailing-twelve section was withheld rather than shown wrong."
        )
    if review:
        return (
            "The read is within "
            f"{TIE_REVIEW_BAND:.0%} of the statement's own subtotals but does not tie exactly ("
            + "; ".join(review)
            + "): kept, and the audit trail's error check reads REVIEW on the lines that differ "
            "— the mapping placed a line or two differently from the statement's own totals."
            + (f" Ties exactly: {', '.join(checked)}." if checked else ""),
            None,
        )
    if checked:
        return (
            f"Ties to the statement's own printed subtotals: {', '.join(checked)}.", None
        )
    return (
        "The statement prints no subtotal rows this reader recognises, so its categories "
        "could not be tied out against the statement's own totals.", None
    )


def _absent_comps(as_of: str, subject_market_rent: float) -> dict:
    return {
        "as_of": as_of or dt.date.today().isoformat(),
        "source": schema.COMP_SOURCE,
        # Carried even with no comps: the schema cross-checks it against the summary's
        # modelled market rent, and a zero here would claim the subject rents nothing.
        "subject_market_rent": subject_market_rent,
        "rows": [],
        "absent_reason": NOT_RUN,
        "coverage_note": None,
        "nearby": [],
        # The tiering block describes HOW comps were selected. Nothing was selected, so
        # every knob is null rather than a default someone could mistake for a setting
        # that was actually applied.
        "tiering": {
            "subject_product": None,
            "subject_year_built": None,
            "year_band": None,
            "radius_mi": None,
            "top_n": None,
            "additional_n": None,
            "vintage_footnote": NOT_RUN,
        },
    }


def _absent_supply() -> dict:
    return {"rings": [], "projects": [], "ring_stock": None, "absent_reason": NOT_RUN}


def _absent_closing_costs(state: str) -> dict:
    # `applies: False` means "this tax does not exist here", which the schema rightly
    # insists is a zero and not a MISSING. Nothing was looked up, so the honest shape is
    # not_asserted with a zero amount and a citation that says why there is no citation.
    blank = {
        "applies": False,
        "amount_status": "not_asserted",
        "rate_label": "Not looked up",
        "amount": 0,
        "citation": NOT_RUN,
        "as_of": "",
        "caveat": None,
    }
    return {
        "state": state,
        "price": None,
        "transfer_tax": dict(blank),
        "mortgage_tax": dict(blank),
        "loan_amount": None,
        "note": NOT_RUN,
    }


# --------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------

#: Summary keys the OM call is asked for map straight across; everything else is either
#: computed, or MISSING with the demo's standing reason.
_SUMMARY_DEFAULT_MISSING = schema.MISSING


def _summary_skeleton() -> dict:
    """Every summary field at MISSING, including the counts.

    The int-typed fields used to be seeded with 0. A zero unit count is not an absent
    unit count: it is a number, and it is the DIVISOR for every per-unit figure in the
    output set, so it printed as "0 units" on the card and divided the pricing ladder
. Nothing here is a number until a source supplies one.
    """
    return {key: _SUMMARY_DEFAULT_MISSING for key in schema.SUMMARY_KEYS}


#: What the notes box stamps on a field it supplied, and the summary keys it can supply.
#:
#: `build_summary` reads the notes AFTER the document, so a visitor can correct a bad
#: extraction — and the stamp went on citing the document's page and quote regardless, so
#: the audit trail said "240 units across 12 buildings" beside the 312 on the page. A value the visitor typed has a source, and it is not the marketing document.
NOTES_SOURCE = "The notes box on the upload form"
NOTES_OVERRIDE_NOTE = "Supplied by the operator via the notes box, replacing what the uploaded document stated."
NOTES_SUPPLIED_NOTE = "Supplied by the operator via the notes box."

#: notes key -> the summary field it lands in. THE table: `build_summary` applies these
#: values and `build_upload_provenance` stamps them, both through `notes_overrides`.
NOTES_TO_SUMMARY: dict[str, str] = {
    "units_total": "units_total",
    "product_type": "product_type",
    "address": "street_address",
}


def notes_overrides(notes_values: dict | None) -> list[tuple[str, str, object]]:
    """``(notes key, summary field, value)`` for every notes entry that may override.

    One table and one usable-value rule, in one place. Until round 19 `build_summary` and
    `build_upload_provenance` each walked their own copy of the same three keys, and the
    two had to agree — about which field a value lands in, and about which values are
    usable at all — with nothing making them. Round 18's finding was the first drift.

    The usable-value rule is the schema's, not this function's: a field the inventory
    types as ``int`` takes an integer and nothing else (a non-integer used to be written
    into the field and then replaced with zero), and a field it types as text takes the
    value written out.
    """
    out: list[tuple[str, str, object]] = []
    for key, field in NOTES_TO_SUMMARY.items():
        value = (notes_values or {}).get(key)
        if value in (None, ""):
            continue
        if ("summary", field) in schema.COUNT_FIELDS:
            # A COUNT, asked the one question every count is asked. Round 19 widened
            # `summary.units_total` to `(int, str)` so it could hold the sentinel, and the
            # text branch below then stringified 312 into "312" — which `build_summary`
            # dropped a line later. A rule keyed on a type is a rule that changes when the
            # type does.
            if schema.usable_count(value) is None:
                continue
        else:
            types = schema.SUMMARY_KEYS.get(field, (True, (str,)))[1]
            if str in types and not isinstance(value, str):
                value = str(value)
        out.append((key, field, value))
    return out


#: What the run says when a source stated a count that cannot be one.
UNUSABLE_COUNT_NOTE = (
    "{source} states a unit count of {value}, which is not a count this pipeline can "
    "use — a count is a positive whole number. It reads MISSING instead, and so does "
    "every per-unit figure that would have been divided by it."
)


def unusable_count_notes(om_fields: dict | None, notes_values: dict | None) -> list[str]:
    """One note per source that stated a unit count and did not state a usable one.

    Silence would be the wrong answer twice over: the visitor typed something, and the
    output shows MISSING. A zero is the case that named this — "0 vacant units" read into
    the wrong field — and it used to be published as the deal's unit count.
    """
    notes: list[str] = []
    for source, raw in (  # noqa: F402
        ("The uploaded marketing document",
         ((om_fields or {}).get("units_total") or {}).get("value")
         if isinstance((om_fields or {}).get("units_total"), dict) else None),
        ("The notes box on the upload form", (notes_values or {}).get("units_total")),
    ):
        if raw in (None, "", schema.MISSING):
            continue
        if schema.usable_count(raw) is None:
            notes.append(UNUSABLE_COUNT_NOTE.format(source=source, value=raw))
    return notes


def _count_or_missing(*candidates) -> "int | str":
    """The first candidate that is a usable whole count, or the absent sentinel.

    Never 0. `t12.units` is the divisor of the per-unit column, and `or 0` at the end of
    a chain of falsy candidates is how a statement with no stated unit count came to
    carry one.
    """
    for value in candidates:
        if schema.usable_count(value) is not None:
            return value
    return schema.MISSING


def unit_count_source(om_fields: dict | None, notes_values: dict | None,
                      roll_count: int) -> "str | None":
    """Which rung supplied the unit count the summary publishes: roll, notes, om or none.

    The same order `build_summary` resolves them in — document, then notes, then the
    roll's own row count — written once so provenance can name the rung that actually
    won. The notes stamp used to be written unconditionally over the roll's, so a run with
    both published the roll's count under an audit trail crediting the notes box.
    """
    if roll_count:
        return "roll"
    if any(key == "units_total" for key, _field, _value in notes_overrides(notes_values)):
        return "notes"
    entry = (om_fields or {}).get("units_total")
    value = entry.get("value") if isinstance(entry, dict) else entry
    if schema.usable_count(value) is not None:
        return "om"
    return None


def build_summary(om_fields: dict, notes_values: dict, units_total: int, rents: dict) -> dict:
    summary = _summary_skeleton()
    for key, entry in (om_fields or {}).items():
        if key not in summary:
            continue
        value = entry.get("value") if isinstance(entry, dict) else entry
        if value in (None, "", schema.MISSING):
            continue
        summary[key] = value
    # The notes box may state a handful of things the schema already has fields for. It is
    # read after the document, so a visitor can correct a bad extraction, and validated
    # like everything else. The keys, the fields and the usable-value rule all come from
    # `notes_overrides` — one table, read here and by `build_upload_provenance`.
    for _key, field, value in notes_overrides(notes_values):
        summary[field] = value

    if units_total:
        summary["units_total"] = units_total
    # The one question, asked once, whatever rung the value came from — the OM loop above
    # copies whatever the document said, and a document can say 0.
    # MISSING, never 0: a zero count is the divisor of every per-unit figure in the set.
    resolved = schema.usable_count(summary.get("units_total"))
    summary["units_total"] = schema.MISSING if resolved is None else resolved
    for key in ("in_place_rent_avg", "market_rent_for_model"):
        if rents.get(key) is not None:
            summary[key] = rents[key]
    summary["notes"] = (
        "Generated by the upload demo from the documents supplied with this run. Every "
        "field reads MISSING unless a document stated it; nothing here is inferred to "
        "fill a gap. No licensed data source was used and no price is assumed."
    )
    if not isinstance(summary.get("deal_name"), str) or summary["deal_name"] == schema.MISSING:
        summary["deal_name"] = "Uploaded deal"
    return summary


#: The band a per-unit SALE price occupies. Outside it a per-unit dollar figure is income,
#: capital expenditure, a fee or a typo — never a price — and passing one through would
#: put a cap rate on the page computed from a number that is not a price, which is worse
#: than showing MISSING.
PER_UNIT_MIN = 50_000
PER_UNIT_MAX = 600_000


def _banded(value) -> float | None:
    """The number, if it is a number and it is a per-unit sale price."""
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    if PER_UNIT_MIN <= number <= PER_UNIT_MAX:
        return number
    # No figure: it came out of the visitor's own memorandum.
    log.info("pricing: a stated figure is outside the per-unit sale-price band")
    return None


def _verified_unit_count(om_fields: dict) -> tuple[int | None, str | None]:
    """The document's own unit count, but only where a quote survived the check.

    ``verify_fields`` clears the quote on anything it could not confirm, so a surviving
    quote IS the evidence. Dividing a total by an unverified count would manufacture a
    per-unit price out of two numbers, one of which nobody checked.
    """
    entry = (om_fields or {}).get("units_total")
    if not isinstance(entry, dict):
        return None, None
    value = entry.get("value")
    quote = entry.get("quote")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None, None
    if not (isinstance(quote, str) and quote.strip()):
        return None, None
    return value, quote


def _no_price() -> dict:
    return {"value": None, "how": None, "quote": None, "page": None,
            "units_total": None, "units_source": None, "total": None}


def resolve_price(
    om_pricing: dict | None,
    om_fields: dict,
    notes_values: dict,
    roll_units: int | None = None,
    form_values: dict | None = None,
) -> dict:
    """What a per-unit price may be, and on what evidence.

    Four ways in, in this order, and no fifth:

    * the upload form's own price field (required since 2026-09-10; validated inside the
      band at the route, so a value here is already a per-unit sale price);
    * the notes box, where the visitor typed a per-unit figure themselves;
    * a document that states a **per-unit** price;
    * a document that states a **total**, divided by a unit count — and only then, with
      the provenance saying it was derived rather than read.

    The divisor is the **rent roll's own row count** whenever there is a roll. That number
    is counted by code out of the file the visitor sent, which is stronger evidence than a
    sentence in a marketing package; it is also the count the rest of the output already
    uses, so dividing by anything else would put a price per unit beside a unit count it
    was not computed from. With no roll, the marketing document's own count is used, and
    only when the quote check confirmed it. With neither, pricing is MISSING.

    Anything else — a figure whose basis the document never stated, a result outside the
    sale-price band — leaves pricing MISSING too.
    """
    from_form = (form_values or {}).get("price_per_unit")
    if from_form is not None:
        value = _banded(from_form)
        if value is not None:
            return {**_no_price(), "value": value, "how": "form"}
    stated = (notes_values or {}).get("price_per_unit")
    if stated is not None:
        value = _banded(stated)
        if value is not None:
            return {**_no_price(), "value": value, "how": "notes"}

    pricing = om_pricing if isinstance(om_pricing, dict) else {}
    basis = str(pricing.get("basis") or "unstated")
    raw = pricing.get("value")
    if raw in (None, "", "MISSING") or not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return _no_price()

    if basis == "per_unit":
        value = _banded(raw)
        if value is None:
            return _no_price()
        return {**_no_price(), "value": value, "how": "stated",
                "quote": pricing.get("quote"), "page": pricing.get("page")}

    if basis == "total":
        units, source = _divisor(om_fields, roll_units)
        if not units:
            log.info("pricing: a stated total was not divided — neither an uploaded roll "
                     "nor a quote-verified unit count to divide it by")
            return _no_price()
        # Rounded to cents: a price per unit is money, and an unrounded quotient carries a
        # float tail into the closing-cost product and onto the page.
        value = _banded(round(float(raw) / units, 2))
        if value is None:
            return _no_price()
        return {**_no_price(), "value": value, "how": "derived",
                "quote": pricing.get("quote"), "page": pricing.get("page"),
                "units_total": units, "units_source": source, "total": float(raw)}
    return _no_price()


def _divisor(om_fields: dict, roll_units: int | None) -> tuple[int | None, str | None]:
    """The unit count a stated total is divided by, and which document it came from."""
    if isinstance(roll_units, int) and not isinstance(roll_units, bool) and roll_units > 0:
        return roll_units, "rent_roll"
    units, _quote = _verified_unit_count(om_fields)
    return (units, "om") if units else (None, None)


# --------------------------------------------------------------------------------------
# The whole file
# --------------------------------------------------------------------------------------



def _document_clause(document_price: dict | None, used: float | None,
                     om_pricing: dict | None = None) -> str:
    """What the memorandum said about price, beside the price used — or that it said nothing.

    A figure the document states but that could not be used (no basis, outside the band)
    is still repeated here as what the document SAID, so the reader sees it was read and
    set aside rather than missed."""
    if not document_price:
        return ""
    stated = document_price.get("value")
    if stated is None:
        om = om_pricing if isinstance(om_pricing, dict) else {}
        raw = om.get("value")
        quote = str(om.get("quote") or "").strip()
        basis = str(om.get("basis") or "unstated")
        why = ("with no stated basis" if basis == "unstated"
               else f"as a {basis.replace('_', ' ')} figure that could not be used")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return f"; the memorandum states ${raw:,.0f} {why}, so it was not used"
        if quote:
            # The figure was read and set aside (no basis, outside the band): repeat the
            # document's own words so the reader sees it was seen, not missed.
            return f"; the memorandum says “{quote[:160]}” {why}, so it was not used"
        return "; the memorandum states no usable price"
    if document_price.get("how") == "derived":
        basis = (f"a stated total of ${document_price['total']:,.0f} over "
                 f"{document_price['units_total']} units")
    else:
        basis = "stated per unit"
    gap = ""
    if isinstance(used, (int, float)) and used and abs(stated - used) / used > 0.005:
        gap = f", {((stated / used) - 1):+.1%} against the price entered"
    return f"; the memorandum's own figure is ${stated:,.0f} per unit ({basis}{gap})"


def _guidance_sentence(price: dict, document_price: dict | None = None,
                       om_pricing: dict | None = None) -> str:
    """The prose that goes beside a resolved per-unit price, naming how it was reached."""
    value = price["value"]
    if price["how"] == "form":
        return (f"${value:,.0f} per unit (entered on the upload form)"
                + _document_clause(document_price, value, om_pricing))
    if price["how"] == "notes":
        return f"${value:,.0f} per unit (stated in the notes box)"
    if price["how"] == "derived":
        counted = ("the uploaded rent roll counts"
                   if price.get("units_source") == "rent_roll"
                   else "the document states")
        return (
            f"${value:,.0f} per unit — the stated total of ${price['total']:,.0f} "
            f"divided by the {price['units_total']} units {counted}"
        )
    return f"${value:,.0f} per unit (as stated)"


PRICE_DERIVED_FROM_ROLL = (
    "Derived from the stated total and the rent roll's {units} units: the document states "
    "a total price of ${total:,.0f}, the uploaded rent roll counts {units} units, and "
    "this is that total divided by that count. The document did not state a per-unit "
    "price."
)

PRICE_DERIVED_FROM_OM = (
    "Derived from the stated total and the document's own unit count: it states a total "
    "price of ${total:,.0f} and a count of {units} units, both quote-verified, and this "
    "is that total divided by that count. No rent roll was uploaded, and the document did "
    "not state a per-unit price."
)


def _price_stamp(price: dict, om_name: str | None):
    """The provenance stamp for ``pricing.price_per_unit``, or None when nothing was set.

    A derived price has to say it is derived and name what it was built from. A price
    lifted straight off the document cites the page and the quote. A price the visitor
    typed cites the notes box — which is a source, and one the reader should be able to
    tell apart from the document.
    """
    import provenance as pv

    if price.get("value") is None:
        return None
    page = price.get("page")
    locator = f"p.{page}" if isinstance(page, int) else "—"
    if price["how"] == "form":
        return pv.read(
            "The upload form's price field", "—",
            note="The purchase price per unit the visitor entered, inside the sale-price band. "
                 "It outranks a price in the notes box or the memorandum.",
        )
    if price["how"] == "notes":
        return pv.read(
            "The notes box on the upload form", "—",
            note="A per-unit price the visitor typed, inside the sale-price band.",
        )
    if price["how"] == "derived":
        from_roll = price.get("units_source") == "rent_roll"
        template = PRICE_DERIVED_FROM_ROLL if from_roll else PRICE_DERIVED_FROM_OM
        return pv.derived(
            om_name or "Uploaded document", locator,
            ["summary.pricing_guidance",
             "rent_roll.units" if from_roll else "summary.units_total"],
            note=template.format(total=price["total"], units=price["units_total"]),
        )
    return pv.read(
        om_name or "Uploaded document", locator, quote=price.get("quote"),
        note="A per-unit price stated by the document, inside the sale-price band.",
    )



def build_upload_provenance(
    om_fields: dict, documents: list[dict], deal_sections: dict,
    price: dict | None = None, notes_values: dict | None = None,
    sources: dict | None = None,
) -> dict:
    """A provenance stamp per field, from THIS upload's own documents.

    The showcase extractor has a `build_provenance()` that names one real deal's pages, quotes,
    tax parcel and comp sources. Calling it for an uploaded deal put another property's
    citations into a stranger's audit trail — every field claiming a source document the
    visitor never sent, with quotes from a building they have nothing to do with. It is
    the worst kind of wrong output: complete, confident, and about the wrong asset.

    So provenance is built here from what this run actually had. A summary field read from
    the marketing document cites that document by the visitor's own filename, the page the
    model reported, and the quote the verbatim check already confirmed appears in the
    text. Everything else is stamped derived, or as not run.

    ``sources`` is what :func:`assemble` resolved, because several of these values have
    more than one possible origin and only the caller knows which one won:

    ==========================  =============================================
    ``units_total``              ``roll`` / ``notes`` / ``om`` / ``None``
    ``as_of``                   ``caller`` / ``notes`` / ``stated`` /
                                ``period`` / ``inferred`` / ``unknown`` /
                                ``no_roll`` / ``None``
    ``lookback_months``         ``notes`` / ``default``
    ``tolerance_days``          ``notes`` / ``default``
    ``lookback_value``,
    ``tolerance_value``         the numbers, for the default's own note
    ``roll_units``              how many rows the roll actually yielded
    ==========================  =============================================

    Before round 19 this function guessed: a rent roll in the batch meant every required
    ``rent_roll`` and ``trade_out`` path was stamped "read from the uploaded rent roll",
    including an as-of date the notes box supplied or the parser inferred, two knobs that
    are usually this demo's own defaults, and a trade-out block that is always absent
    because the demo never asks for one.
    """
    import provenance as pv

    sources = sources or {}
    by_role = {d.get("role"): d for d in documents if d.get("role")}
    om_name = (by_role.get("om") or {}).get("filename")
    t12_name = (by_role.get("t12") or {}).get("filename")
    roll_name = (by_role.get("rent_roll") or {}).get("filename")

    stamps: dict = {}
    for key, entry in (om_fields or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("value") in (None, "", schema.MISSING):
            continue
        page = entry.get("page")
        locator = f"p.{page}" if isinstance(page, int) else "—"
        stamps[f"summary.{key}"] = pv.read(
            om_name or "Uploaded document", locator, quote=entry.get("quote")
        )

    count_source = sources.get("units_total")
    if roll_name and count_source == "roll":
        stamps["summary.units_total"] = pv.derived(
            roll_name, "—", ["rent_roll.units"],
            note="Counted from the rows of the uploaded rent roll.",
        )
    # Only where the roll actually yielded rows. A roll that parsed to nothing leaves both
    # averages MISSING, and a stamp saying they were weighted across it describes an
    # arithmetic that did not happen.
    if roll_name and sources.get("roll_units"):
        for key in ("summary.in_place_rent_avg", "summary.market_rent_for_model"):
            stamps[key] = pv.derived(
                roll_name, "—", ["rent_roll.units"],
                note="Unit-weighted across the uploaded roll.",
            )
    if t12_name:
        stamps["t12.source_subtotals"] = pv.read(
            t12_name, "header block",
            quote=None,
        )
    # A field the NOTES supplied cites the notes, whatever the document also said about
    # it — written after the document loop above, because that is the order
    # `build_summary` resolves them in and the stamp has to describe the value shown.
    # Same table `build_summary` applies, so a key it skips is a key nothing stamps.
    for key, field, _value in notes_overrides(notes_values):
        # …except where a LATER rung won. The roll's row count beats the notes box, and
        # this loop used to overwrite the roll's stamp anyway — publishing the roll's
        # count under an audit trail crediting the notes.
        if key == "units_total" and count_source != "notes":
            continue
        had_document_value = (
            isinstance((om_fields or {}).get(key), dict)
            and (om_fields[key].get("value") not in (None, "", schema.MISSING))
        )
        stamps[f"summary.{field}"] = pv.read(
            NOTES_SOURCE, "—",
            note=NOTES_OVERRIDE_NOTE if had_document_value else NOTES_SUPPLIED_NOTE,
        )
        # The document's own stamp for that key, if the key and the field differ, would
        # otherwise be left behind citing a value nothing shows.
        if field != key:
            stamps.pop(f"summary.{key}", None)

    price_stamp = _price_stamp(price or {}, om_name)
    if price_stamp is not None:
        stamps["pricing.price_per_unit"] = price_stamp

    # `pricing.units_total` is required and always populated, and nothing above stamped it,
    # so it fell through to "Not stated in the uploaded documents" with a note claiming
    # the value reads MISSING — beside the number the pricing arithmetic divides by
    #. It is the same value `summary.units_total` carries, so it cites it.
    count_stamp_source = {
        "roll": roll_name, "om": om_name, "notes": NOTES_SOURCE,
    }.get(count_source)
    if count_stamp_source:
        stamps["pricing.units_total"] = pv.derived(
            count_stamp_source, "—", ["summary.units_total"],
            note="The same count the summary carries, " + {
                "roll": "counted from the rows of the uploaded rent roll.",
                "om": "read from the uploaded marketing document.",
                "notes": "supplied by the operator via the notes box.",
            }[count_source],
        )
    else:
        # Its OWN sentence, not the generic fall-through. A unit count is the divisor for
        # every per-unit figure in the set, so "nothing stated one" is the most load-
        # bearing absence this trail can record, and the reader is owed the consequence
        # rather than a line saying the field was not in a document (ruling 2).
        stamps["pricing.units_total"] = pv.read(
            NOT_STATED_SOURCE, "—", note=NO_UNIT_COUNT_NOTE)
        stamps["summary.units_total"] = pv.read(
            NOT_STATED_SOURCE, "—", note=NO_UNIT_COUNT_NOTE)

    stamps.update(_roll_stamps(pv, roll_name, sources))

    # Every required path must carry a stamp — the schema will not accept a value whose
    # origin nobody recorded. Whatever is still unstamped after the reads and derivations
    # above genuinely has no origin in this run, and says exactly that.
    #
    # `derived` has to name the inputs it was built from; a section that never ran has
    # none, so those are stamped as a read of nothing with the reason in the note.
    for path in pv.REQUIRED_PATHS:
        if path in stamps:
            continue
        section = path.split(".", 1)[0]
        if section in ("comps", "demand", "supply", "closing_costs"):
            stamps[path] = pv.read("Not run in the upload demo", "—", note=NOT_RUN)
        elif section == "t12" and t12_name:
            stamps[path] = pv.read(t12_name, "statement", note="Read from the uploaded statement.")
        elif section == "rent_roll" and roll_name:
            # Whatever `_roll_stamps` did not speak to. `trade_out` is deliberately not
            # here: the demo never asks for a trade-out report, so no path under it was
            # ever read from anything.
            stamps[path] = pv.read(roll_name, "roll", note="Read from the uploaded rent roll.")
        else:
            stamps[path] = pv.read(
                "Not stated in the uploaded documents", "—",
                note=(
                    "No uploaded document stated this, so the value reads MISSING. "
                    "Nothing was assumed to fill it."
                ),
            )
    return stamps


#: What "no source at all" looks like on a required path.
NOT_STATED_SOURCE = "Not stated in the uploaded documents"

NO_UNIT_COUNT_NOTE = (
    "No uploaded document, and no note, stated a unit count. It reads MISSING, and so "
    "does every per-unit figure that would have been divided by it — nothing here is "
    "computed from an assumed count."
)

NO_AS_OF_NOTE = (
    "No uploaded document or note stated an as-of date and none could be inferred, so "
    "the roll carries none and no window is measured from one."
)


def _roll_stamps(pv, roll_name: "str | None", sources: dict) -> dict:
    """Stamps for the rent-roll and trade-out paths, each from the rung that supplied it.

    Four of these five used to be one blanket "read from the uploaded rent roll" applied
    to every required path under those two sections whenever a roll was in the batch. The
    as-of date is a four-rung ladder whose bottom rung is an inference; the lookback and
    the tolerance are this demo's own defaults unless the notes box set them; and
    `trade_out.rows` is always empty, because the demo never asks for a trade-out report
.
    """
    roll = roll_name or "the uploaded rent roll"
    as_of = sources.get("as_of")
    stamps: dict = {}

    if as_of == "caller":
        stamps["rent_roll.as_of"] = pv.read(
            "Supplied with the run", "—",
            note="The as-of date the run was started with.")
    elif as_of == "notes":
        stamps["rent_roll.as_of"] = pv.read(
            NOTES_SOURCE, "—",
            note="The as-of date the operator typed in the notes box.")
    elif as_of == "stated":
        stamps["rent_roll.as_of"] = pv.read(
            roll, "as-of line", note="Read from the roll's own as-of line.")
    elif as_of == "period":
        stamps["rent_roll.as_of"] = pv.read(
            roll, "as-of line",
            note="Read as the end of the period the roll's own header states.")
    elif as_of == "inferred":
        stamps["rent_roll.as_of"] = pv.derived(
            roll, "—", ["rent_roll.units"],
            note="The roll states no as-of date, so this is inferred from the newest "
                 "lease date on it.")
    else:
        stamps["rent_roll.as_of"] = pv.read(NOT_STATED_SOURCE, "—", note=NO_AS_OF_NOTE)

    for path, source_key, value_key, what in (
        ("rent_roll.lookback_months", "lookback_months", "lookback_value",
         "months of lease history the turnover figures look back over"),
        ("rent_roll.new_tenant_tolerance_days", "tolerance_days", "tolerance_value",
         "days a lease start may sit from a move-in and still count as the same event"),
    ):
        if sources.get(source_key) == "notes":
            stamps[path] = pv.read(NOTES_SOURCE, "—", note=NOTES_SUPPLIED_NOTE)
        else:
            stamps[path] = pv.read(
                NOT_STATED_SOURCE, "—",
                note=f"This demo's default of {sources.get(value_key)} — the {what}. "
                     f"No uploaded document or note set it.")

    if roll_name:
        stamps["rent_roll.units"] = pv.read(
            roll_name, "roll", note="Read from the uploaded rent roll.")

    stamps["trade_out.rows"] = pv.read(
        "Not asked for by the upload demo", "—", note=NO_TRADE_OUT)
    return stamps


def _knob(notes_values: dict | None, key: str, default: int, low: int,
          high: int) -> "tuple[int, str]":
    """One integer knob from the notes box, or the default — and which of the two it is.

    Bounded rather than trusted: the value came out of free text a stranger typed, and a
    lookback of 100,000 months is not a preference, it is a way to make the analysis mean
    nothing. Out of range falls back to the default rather than clamping, because a
    silently clamped 60 reads as a choice the visitor made.

    The SOURCE travels with the value because the audit trail has to say which it was: a
    default stamped "read from the uploaded rent roll" is a read that never happened.
    """
    raw = (notes_values or {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default, "default"
    value = int(raw)
    if value != raw or not (low <= value <= high):
        # The KEY and the bounds, never the value the visitor typed.
        log.warning("notes: %s is outside %d..%d; using the default of %d",
                    key, low, high, default)
        return default, "default"
    return value, "notes"


def assemble(
    *,
    documents: list[dict],
    om_fields: dict | None = None,
    om_pricing: dict | None = None,
    t12_mapping: dict | None = None,
    roll_mapping: dict | None = None,
    notes_values: dict | None = None,
    generated_on: str | None = None,
    as_of: str | None = None,
    market_report: dict | None = None,
    market_failure: str | None = None,
    om_comps: dict | None = None,
    om_comps_failure: str | None = None,
    form_values: dict | None = None,
) -> tuple[dict, list[str]]:
    """Everything a run produced, into one validated interchange dict.

    ``market_report`` is the operator's market-report service's JSON answer, or None
    with ``market_failure`` saying why; the demand, supply and comps blocks are built from
    it HERE, after the summary is final, because the schema cross-checks the comps block
    against the summary's product type and modelled rent.

    ``documents`` are normalized documents from ``parse_worker`` carrying the ``role``
    ``classify`` gave them. Returns ``(deal, notes)``; the caller validates.
    """
    by_role = {d.get("role"): d for d in documents if d.get("role")}
    om_doc = by_role.get("om")
    t12_doc = by_role.get("t12")
    roll_doc = by_role.get("rent_roll")

    today = generated_on or dt.date.today().isoformat()
    # The roll's own as-of date, NEVER the day of the upload. Four rungs, in order: the
    # caller, the visitor's notes box, the roll's own banner, and — with a disclosure note
    # — the newest lease date on the roll itself. `today` is not one of them: a 2025 roll
    # analysed as of today reports every one of its move-ins as outside the lookback and
    # its turnover as zero, which is a confident wrong answer rather than a missing one.
    stated_date, stated_period = stated_as_of(roll_doc, roll_mapping)
    as_of_notes: list[str] = []

    def _rung(value, source: str) -> str:
        """One rung's value, or "" — and a note saying so when it was date-shaped junk.

        A value is skipped rather than repaired, and the ladder simply continues. The
        note carries the value because it is the visitor's own and their run response is
        where they can act on it; the LOG line names the source and never the value.
        """
        text = str(value or "").strip()
        if not text or valid_as_of(text):
            return text
        log.warning("as-of: the date from %s is not a day on the calendar; skipping "
                    "that source", source)
        as_of_notes.append(AS_OF_INVALID_NOTE.format(
            value=text, source=AS_OF_SOURCE_NAMES.get(source, source)
        ))
        return ""

    called = _rung(as_of, "caller")
    noted = _rung((notes_values or {}).get("as_of_date"), "notes")
    stated = _rung(stated_date, "stated")
    roll_as_of = called or noted or stated or ""
    if called:
        as_of_source = "caller"
    elif noted:
        as_of_source = "notes"
    elif stated:
        as_of_source = "period" if stated_period else "stated"
    else:
        as_of_source = None

    lookback, lookback_source = _knob(
        notes_values, "lookback_months", DEFAULT_LOOKBACK_MONTHS, 1, 60)
    tolerance, tolerance_source = _knob(
        notes_values, "new_tenant_tolerance_days", DEFAULT_TOLERANCE_DAYS, 0, 365)
    rent_roll, roll_notes, _stats = build_rent_roll(
        roll_doc,
        roll_mapping,
        roll_as_of,
        lookback_months=lookback,
        new_tenant_tolerance_days=tolerance,
    )
    # The last rung needs the parsed rows, so it runs here rather than above.
    if not roll_as_of and rent_roll["units"]:
        inferred = latest_roll_date(rent_roll["units"])
        if inferred:
            roll_as_of, as_of_source = inferred, "inferred"
        else:
            as_of_source = "unknown"
        rent_roll["as_of"] = roll_as_of
    elif not roll_as_of and not rent_roll["units"]:
        as_of_source = as_of_source or "no_roll"
    units_total = len(rent_roll["units"])

    t12, t12_notes = build_t12(t12_doc, t12_mapping, units_total)
    # A T-12 read on its own knows its own unit count only from what was said about it.
    if not units_total and isinstance((notes_values or {}).get("units_total"), int):
        units_total = notes_values["units_total"]
    if not schema.section_absent({"t12": t12}, "t12"):
        t12["units"] = _count_or_missing(units_total, t12.get("units"))

    rents: dict = {}
    if rent_roll["units"]:
        totals = compute.roll_totals(
            {
                "rent_roll": rent_roll,
                "summary": {"units_total": units_total},
                "pricing": {"price_per_unit": None, "units_total": units_total,
                            "document_price_per_unit": None},
            }
        )
        # A roll where every unit is vacant, or one with no market-rent column, has no
        # weighted average to state. None means MISSING, not zero — a zero average rent
        # would read as a property that charges nothing.
        rents = {
            key: round(value, 2)
            for key, value in (
                ("in_place_rent_avg", totals.get("wavg_in_place_rent")),
                ("market_rent_for_model", totals.get("wavg_market_rent_for_model")),
            )
            if isinstance(value, (int, float))
        }

    summary = build_summary(om_fields or {}, notes_values or {}, units_total, rents)
    # The rent roll is the authority on unit count when there is one; failing that the
    # marketing document or the notes box. Whatever wins, pricing and the summary have to
    # agree — the schema checks it, and a run with an OM but no roll is exactly where they
    # would drift apart.
    units_total = summary["units_total"]
    # The numeric view, for everything that has to do arithmetic. `units_total` itself may
    # be the MISSING sentinel from here on, and every consumer below asks this instead.
    count = units_total if isinstance(units_total, int) else None
    if not schema.section_absent({"t12": t12}, "t12"):
        t12["units"] = _count_or_missing(count, t12.get("units"))
    # The roll's own row count is the divisor when there is a roll — code-counted out of
    # the visitor's own file, and the same number `summary.units_total` already carries.
    price = resolve_price(
        om_pricing, om_fields or {}, notes_values or {},
        roll_units=len(rent_roll["units"]) or None,
        form_values=form_values,
    )
    # What the MEMORANDUM says the price is, resolved by the same rules with the form and
    # the notes box out of the way: the seller's figure, kept beside the one used so the
    # two can be compared (and so a memorandum that states none says so).
    document_price = resolve_price(
        om_pricing, om_fields or {}, {},
        roll_units=len(rent_roll["units"]) or None,
    )
    price_per_unit = price["value"]
    if price_per_unit is not None:
        # The prose field says which of the three ways this number arrived, because
        # "$200,000 per unit" read off a total is a different claim from one the document
        # printed, and the reader is entitled to know which they are looking at.
        summary["pricing_guidance"] = _guidance_sentence(price, document_price, om_pricing)

    provenance_entries = []
    for document in documents:
        role = document.get("role")
        provenance_entries.append(
            {
                "document": document.get("filename", "(unnamed)"),
                "kind": role or "context",
                "note": document.get("note") or f"Uploaded and read as {role or 'context'}.",
            }
        )
    if not provenance_entries:
        provenance_entries.append(
            {"document": "(none)", "kind": "context", "note": "No documents were read."}
        )

    deal = {
        "meta": {
            "toolkit_name": schema.TOOLKIT_NAME,
            "deal_name": summary["deal_name"],
            "deal_slug": "uploaded-deal",
            "generated_on": today,
            # With a roll present this is the roll's own date, or "" when it stated none
            # and none could be inferred. With NO roll nothing on the page is measured
            # from it, so the generation date stands as it always has.
            "as_of_date": roll_as_of or ("" if rent_roll["units"] else today),
            "illustrative_pricing": price_per_unit is None,
            "pricing_note": (
                "No per-unit price was stated in the documents or the notes, so every "
                "price and cap rate in this output set reads MISSING. Nothing here is "
                "computed from an assumed price."
                if price_per_unit is None
                else "Pricing is as stated in the uploaded documents or the notes box, "
                     "not a market opinion."
            ),
            # The publish guard refuses anything not marked synthetic. An upload is a real
            # deal belonging to whoever sent it and must never reach the site; the server
            # never calls the publish step, and this makes the guard right regardless.
            "synthetic": False,
            "provenance": provenance_entries,
            "excluded_sources": [
                "Third-party licensed rent-comp feeds",
                "Subscription market-data providers",
                "Any comparable-rent, demographic or supply source — the upload demo "
                "does not reach them",
            ],
            "notes": [],
        },
        "summary": summary,
        "t12": t12,
        "rent_roll": rent_roll,
        "trade_out": {"rows": [], "absent_reason": NO_TRADE_OUT},
        "pricing": {"price_per_unit": price_per_unit, "units_total": units_total,
                    "document_price_per_unit": document_price["value"]},
        "comps": _absent_comps(
            roll_as_of,
            summary["market_rent_for_model"]
            if isinstance(summary.get("market_rent_for_model"), (int, float))
            else 0,
        ),
        "demand": schema.empty_demand(NOT_RUN),
        "supply": _absent_supply(),
        "closing_costs": _absent_closing_costs(str(summary.get("state") or "").upper()),
        # Which rung won, for every value that has more than one. Provenance cannot work
        # this out from the finished deal — a 240 in `summary.units_total` looks the same
        # whether the roll counted it, the notes box said it or the memorandum did.
        "provenance": build_upload_provenance(
            om_fields or {}, documents, {}, price, notes_values or {},
            sources={
                "units_total": unit_count_source(
                    om_fields or {}, notes_values or {}, len(rent_roll["units"])
                ),
                "as_of": as_of_source,
                "lookback_months": lookback_source,
                "tolerance_days": tolerance_source,
                "lookback_value": rent_roll.get("lookback_months", lookback),
                "tolerance_value": rent_roll.get(
                    "new_tenant_tolerance_days", tolerance),
                "roll_units": len(rent_roll["units"]),
            },
        ),
    }
    # BOTH, or neither. A price times a count nobody supplied is a number with no
    # meaning, and `closing_costs.price` is already None-able for exactly this reason.
    if price_per_unit is not None and count:
        deal["closing_costs"]["price"] = price_per_unit * count

    # The three market blocks. Offline they are always absent, with the reason.
    from server import market as market_mod

    subject_rent = deal["comps"]["subject_market_rent"]
    if market_report is not None:
        # The market-report client is not part of this repository (server/market.py).
        raise ValueError("a market report was passed, but this build has no market-report client")
    market_blocks, market_notes = market_mod.absent_blocks(
        market_failure or market_mod.NOT_CONFIGURED_REASON,
        as_of=roll_as_of, subject_market_rent=subject_rent,
    )
    deal.update(market_blocks)

    # the comparables the memorandum itself states. Always written on the upload
    # path: with rows, or with the reason there are none.
    om_name = (om_doc or {}).get("filename") or "the memorandum"
    if om_doc is None:
        om_block, om_stamps, om_notes = schema.empty_om_comps("no memorandum was read, so there are no stated comparables"), {}, []
    else:
        om_block, om_stamps, om_notes = om_comps_block(om_comps, om_comps_failure, om_name)
    deal["om_comps"] = om_block
    deal["provenance"].update(om_stamps)
    # The memorandum's own price, stamped to its page and quote (or derived from its
    # stated total) so a reader can hold it beside the price the visitor entered.
    if document_price.get("value") is not None:
        doc_stamp = _price_stamp(document_price, om_name)
        if doc_stamp is not None:
            deal["provenance"]["pricing.document_price_per_unit"] = doc_stamp

    # Property tax from the vendored county table: the rate, its unit, the ratio and the
    # reset rule for the stated state and county, the statement's own tax line, and the
    # per-unit band. After pricing, because the estimate and the band read the price.
    tax_note = apply_property_tax(deal, (by_role.get("t12") or {}).get("filename"))

    notes = (list(roll_notes) + list(t12_notes) + as_of_notes
             + unusable_count_notes(om_fields or {}, notes_values or {})
             + list(market_notes) + list(om_notes) + [tax_note])
    if as_of_source == "period":
        notes.append(
            AS_OF_PERIOD_NOTE.format(date=roll_as_of, period=stated_period)
        )
    elif as_of_source == "inferred":
        notes.append(AS_OF_INFERRED_NOTE.format(date=roll_as_of))
    elif as_of_source == "unknown":
        notes.append(AS_OF_UNKNOWN_NOTE)
    notes.append(_run_note(om_doc, t12_doc, roll_doc, om_fields or {}, price_per_unit))
    deal["meta"]["notes"] = [
        {
            "topic": "About this run",
            "applies_to": "Notes & Disclosures",
            "note": note,
        }
        for note in notes
    ]
    return deal, notes


# --------------------------------------------------------------------------------------
# Property tax — the vendored county table
# --------------------------------------------------------------------------------------

NO_STATEMENT_TAX_NOTE = (
    "No uploaded statement carries a line categorised as Property Taxes, so the amount as "
    "billed in the statement reads MISSING. Nothing was assumed to fill it."
)


def apply_property_tax(deal: dict, t12_name: "str | None") -> str:
    """Fill the summary's tax keys from the vendored table, stamp them, and say what was done.

    The upload demo reaches no county portal, so the parcel, tax area code, assessor's
    value and billed amount stay MISSING and their stamps say why. What the table asserts
    it asserts with a citation and a tax year: the representative combined rate for a
    county on file, and for a state on file the divisor (the rate's unit), the legal
    assessment ratio, what the basis IS and whether a sale resets it. A county not on
    file reads MISSING with the honest note; so does a state not on file.

    Two upload-path keys go beside them: the statement's own "Property Taxes" lines over
    its twelve months (as billed in the statement, computed from the T-12 rows) and the
    per-unit sanity band's verdict, which flags and never overwrites. Both are optional
    summary keys the published sets do not carry. Returns the Notes & Disclosures line.
    """
    import property_tax_table as ptt
    import provenance as pv

    summary = deal["summary"]
    found = ptt.lookup(summary.get("state"), summary.get("county"))
    values = ptt.summary_values(found)
    stamps = ptt.provenance_stamps(found)
    if found is not None:
        # The table is a source the Summary tab's "documents read" list should name beside
        # the uploaded files — it supplied the rate, the ratio and the reset rule.
        n_counties, as_of = ptt.coverage()
        deal["meta"]["provenance"].append({
            "document": "Vendored county property-tax rate table", "kind": "tax",
            "note": f"Representative combined rate for {summary.get('county')}, "
                    f"{summary.get('state')} and the state's assessment rules; {n_counties} "
                    f"counties on file as of {as_of}. No county portal is reached.",
        })

    def stated(key):
        value = summary.get(key)
        return None if value in (None, "", schema.MISSING) else value

    # What the uploaded document said is not thrown away. The table WINS on legal facts
    # it asserts — the divisor, the ratio, what the basis is and how it resets are statute,
    # not opinion — and the document's differing figure is recorded beside it. The RATE is
    # different: a document's rate is at least about this parcel, the table's is the county
    # seat's representative stack, so a stated rate is KEPT and the table's is offered as a
    # cross-check. Where the table asserts nothing, whatever the document stated stands and
    # is labelled unverified; where neither has anything, the key reads MISSING.
    remarks: list[str] = []
    #: Document-stated fields a remark depends on — the evidence stamp cites them.
    remark_inputs: list[str] = []
    kept_document_rate = None
    #: Legal-fact fields the DOCUMENT supplied where the table asserted nothing.
    kept_document_fields: list[str] = []
    for key in ("tax_rate_as_billed", "tax_divisor", "assessment_ratio",
                "tax_basis_label", "tax_basis_note"):
        table_value, doc_value = values[key], stated(key)
        label = schema.summary_label(key).lower()
        if table_value == schema.MISSING:
            if doc_value is not None:
                remarks.append(
                    f"The uploaded document stated the {label} ({doc_value}); it is kept as "
                    f"stated and was not verified against a published table.")
                remark_inputs.append(f"summary.{key}")
                if key == "tax_rate_as_billed":
                    kept_document_rate = doc_value
                else:
                    kept_document_fields.append(label)
            else:
                summary[key] = schema.MISSING
                deal["provenance"][f"summary.{key}"] = stamps[f"summary.{key}"]
            continue
        if key == "tax_rate_as_billed" and doc_value is not None:
            kept_document_rate = doc_value
            remark_inputs.append(f"summary.{key}")
            remarks.append(
                f"The uploaded document stated a rate of {doc_value}; it is kept as stated. "
                f"The vendored table's representative {found.entry.county}, {found.state} "
                f"stack is {found.entry.rate:g} {found.rule.rate_unit} for tax year "
                f"{found.entry.tax_year}, offered as a cross-check.")
            continue
        if doc_value is not None and doc_value != table_value and key in ("tax_divisor", "assessment_ratio"):
            remark_inputs.append(f"summary.{key}")
            remarks.append(
                f"The uploaded document stated the {label} as {doc_value}; the table's "
                f"{table_value} ({found.rule.ratio_citation if key == 'assessment_ratio' else found.rule.rate_unit}) "
                f"is used because it is the statute, and the document's figure is recorded here.")
        summary[key] = table_value
        deal["provenance"][f"summary.{key}"] = stamps[f"summary.{key}"]
    # A bill without its tax year cannot be tied to this year's rate, and a "For MISSING"
    # label is not a label. The figure is recorded in the evidence and the field reads
    # MISSING, so the tie-out stays NOT CHECKED.
    if stated("tax_billed_amount") is not None and stated("tax_billed_year") is None:
        amount = summary["tax_billed_amount"]
        shown = f"${amount:,.0f}" if isinstance(amount, (int, float)) else str(amount)
        # The audit path travels with the figure: its page and quote come out of the
        # memorandum's own stamp before that stamp is replaced.
        old_stamp = deal["provenance"].pop("summary.tax_billed_amount", None) or {}
        where = old_stamp.get("locator") or "page not stated"
        quote = old_stamp.get("quote")
        remarks.append(
            f"The offering memorandum states a property-tax bill of {shown} without a tax "
            f"year ({where}" + (f': "{quote}"' if quote else "") + "); it is recorded here and "
            "not used for the tie-out.")
        summary["tax_billed_amount"] = schema.MISSING

    # The rate's own tax year rides beside the rate when it is the table's, so a bill from
    # another year is shown but never compared against it (compute.tax_check).
    if kept_document_rate is None and found.entry is not None:
        summary["tax_rate_year"] = found.entry.tax_year
        deal["provenance"]["summary.tax_rate_year"] = pv.read(
            ptt.SOURCE_DOC, f"property_tax_table.COUNTY_RATES[({found.entry.state!r}, "
            f"{ptt.normalise_county(found.entry.county)!r})]",
            note="The tax year of the rate on file. A bill from another year is displayed, not tied out.")
    else:
        summary["tax_rate_year"] = schema.MISSING
        deal["provenance"]["summary.tax_rate_year"] = pv.read(
            ptt.SOURCE_DOC, "property_tax_table.COUNTY_RATES",
            note="No table rate is in use, so no rate year is asserted.")
    bill_year, rate_year = stated("tax_billed_year"), summary.get("tax_rate_year")
    if (isinstance(bill_year, int) and isinstance(rate_year, int) and bill_year != rate_year):
        remarks.append(
            f"The memorandum's bill is for tax year {bill_year} and the rate on file is for "
            f"{rate_year}, so the two are shown and not compared.")

    # Where the state taxes ONE of the values an assessor prints, a stated figure is the
    # basis only when the label printed against it says so. An Arizona memorandum's full
    # cash value is not its limited property value, and labelling it LPV overstated the
    # year-one tax by a third. What the memorandum said is kept in the evidence,
    # with its page and quote, because the portal loop below replaces the stamp.
    rule = found.rule if found is not None else None
    terms = getattr(rule, "basis_terms", None)
    basis = stated("assessed_value_basis")
    if terms and basis is not None:
        st = deal["provenance"].get("summary.assessed_value_basis") or {}
        labels = ptt.basis_value_labels(basis, st.get("quote") or "")
        if not labels & set(terms):
            label = ", ".join(sorted(labels))
            shown = f"${basis:,.0f}" if isinstance(basis, (int, float)) else str(basis)
            what = f"as its {label}" if label else \
                "with no label beside the figure that says which value it is"
            quote = st.get("quote")
            remarks.append(
                f"The offering memorandum states {shown} {what} "
                f"({st.get('locator') or 'page not stated'}" + (f': "{quote}"' if quote else "")
                + f"); {rule.name} taxes the {terms[0]}, so it is not used as the basis and "
                f"the assessor's value reads MISSING.")
            summary["assessed_value_basis"] = schema.MISSING

    # A parcel, area code, assessor's value or bill the MEMORANDUM stated stays, labelled as
    # its own statement; only what nothing stated is stamped as absent for want of a portal.
    portal_missing: list[str] = []
    portal_kept: list[str] = []
    portal_names = {
        "tax_parcel_id": "the parcel", "tax_area_code": "the tax area code",
        "assessed_value_basis": "the assessor's value", "tax_billed_amount": "the billed amount",
        "tax_billed_year": "the bill's tax year", "tax_exemption": "the exemption",
    }
    for path in ptt.PORTAL_PATHS:
        key = path.split(".", 1)[1]
        if stated(key) is None:
            summary[key] = schema.MISSING
            deal["provenance"][path] = stamps[path]
            portal_missing.append(key)
        else:
            portal_kept.append(key)
            remark_inputs.append(path)
            value = summary[key]
            shown = f"${value:,.0f}" if key in ("assessed_value_basis", "tax_billed_amount") \
                and isinstance(value, (int, float)) else str(value)
            remarks.append(
                f"The offering memorandum stated {portal_names[key]} ({shown}); it is kept as "
                f"stated and not verified against a county portal.")

    summary["tax_rate_evidence"] = values["tax_rate_evidence"] + (
        " " + " ".join(remarks) if remarks else "")
    evidence_stamp = dict(stamps["summary.tax_rate_evidence"])
    if remark_inputs:
        # The remarks quote document-stated values, so the evidence depends on them and
        # the audit trail has to say so — it used to cite the table alone.
        evidence_stamp["inputs"] = list(evidence_stamp.get("inputs", [])) + remark_inputs
        evidence_stamp["note"] = (evidence_stamp.get("note") or "") + (
            " Also records what the uploaded document stated for the field(s) it cites.")
    deal["provenance"]["summary.tax_rate_evidence"] = evidence_stamp

    statement = compute.statement_property_tax(deal)
    summary["tax_statement_amount"] = statement if statement is not None else schema.MISSING
    if statement is not None:
        deal["provenance"]["summary.tax_statement_amount"] = pv.computed(
            t12_name or "Uploaded statement", "Property Taxes lines", ["t12.rows"],
            note="The statement's lines categorised as Property Taxes, summed over its "
                 "twelve months. As billed in the statement, not the county's bill.",
        )
    else:
        deal["provenance"]["summary.tax_statement_amount"] = pv.read(
            NOT_STATED_SOURCE, "—", note=NO_STATEMENT_TAX_NOTE)

    # The band checks the statement's per-unit figure when there is one, else the
    # year-one estimate's. Either way it is a check, and the figures stand as computed.
    count = schema.usable_count(summary.get("units_total"))
    tu = compute.tax_underwriting(deal)
    if statement is not None and count:
        per_unit, source_label, inputs = (
            statement / count, "the statement's line per unit",
            ["summary.tax_statement_amount", "summary.units_total", "summary.state"],
        )
    elif tu.get("assumed_tax_per_unit") is not None:
        per_unit, source_label, inputs = (
            tu["assumed_tax_per_unit"], "the year-one estimate per unit",
            ["pricing.price_per_unit", "summary.assessment_ratio", "summary.tax_rate_as_billed",
             "summary.tax_divisor", "summary.state"],
        )
    else:
        per_unit, source_label, inputs = None, "no per-unit figure", ["summary.state"]
    summary["tax_per_unit_flag"] = ptt.band_flag_text(per_unit, found.state, source_label)
    deal["provenance"]["summary.tax_per_unit_flag"] = pv.derived(
        ptt.SOURCE_DOC, "property_tax_table.band_check", inputs,
        note="A sanity check against the state's typical per-unit range. It flags; it never "
             "changes a figure.",
    )

    n_counties, as_of = ptt.coverage()
    if kept_document_rate is not None and found.rule is not None:
        # The DOCUMENT's rate is the one every figure uses; the table's is a cross-check.
        what = (
            f"Property tax: the rate is the one the uploaded document stated "
            f"({kept_document_rate} {found.rule.rate_unit} of assessed value), kept because it "
            f"is about this parcel; the vendored county rate table's representative stack "
            + (f"for {found.entry.county}, {found.state} ({found.entry.rate:g}, tax year "
               f"{found.entry.tax_year}) is offered beside it as a cross-check"
               if found.entry is not None else
               f"has no entry for {found.county_label} ({n_counties} counties as of {as_of})")
            + f". The {found.state} divisor, assessment ratio and reset-on-sale rule are the "
            f"table's."
        )
    elif kept_document_rate is not None:
        what = (
            f"Property tax: the rate is the one the uploaded document stated "
            f"({kept_document_rate}), kept as stated; no state rules are on file for "
            f"{found.county_label} in the vendored county rate table ({n_counties} counties "
            f"as of {as_of}), so the table asserts nothing"
            + (f" — the {', '.join(kept_document_fields)} also come from the uploaded "
               f"document, unverified, and" if kept_document_fields else "; ")
            + " whatever else it would state reads MISSING."
        )
    elif found.entry is not None:
        what = (
            f"Property tax: the rate is the vendored county rate table's entry for "
            f"{found.entry.county}, {found.state} — {found.entry.rate:g} "
            f"{found.rule.rate_unit} of assessed value, tax year {found.entry.tax_year}, "
            f"{found.entry.jurisdiction} — with the {found.state} assessment ratio and "
            f"reset-on-sale rule from the same table."
        )
    elif found.rule is not None:
        what = (
            f"Property tax: no rate on file for {found.county_label} in the vendored county "
            f"rate table ({n_counties} counties as of {as_of}), so the rate reads MISSING; "
            f"the {found.state} divisor, assessment ratio and reset-on-sale rule are stated."
        )
    else:
        what = (
            f"Property tax: no state rules on file for {found.county_label} in the vendored "
            f"county rate table ({n_counties} counties as of {as_of}), so the table asserts "
            f"nothing"
            + (f"; the {', '.join(kept_document_fields)} come from the uploaded document, "
               f"unverified, and whatever else the table would state reads MISSING."
               if kept_document_fields else
               "; the rate, its unit, the assessment ratio and the reset-on-sale rule read MISSING.")
        )
    names = portal_names
    portal = " No county portal was reached"
    if portal_missing:
        portal += ": " + ", ".join(names[k] for k in portal_missing) + (
            " read MISSING" if len(portal_missing) > 1 else " reads MISSING")
    if portal_kept:
        portal += ("; " if portal_missing else ": ") + ", ".join(names[k] for k in portal_kept) + (
            " come from the uploaded offering memorandum, unverified against a portal"
            if len(portal_kept) > 1 else " comes from the uploaded offering memorandum, unverified against a portal")
    return (
        what + portal + ". The statement's own Property Taxes line is shown as billed in "
        "the statement where a statement was uploaded."
    )


def om_comps_block(om_comps: dict | None, failure: str | None, om_name: str) -> tuple[dict, dict, list[str]]:
    """The OM-comparables block, its provenance stamps and its notes, from what the call read.

    Three absences that must not be confused: the call failed (its own reason); every row
    the model read was REJECTED by verification (that is not "the memorandum states none");
    the memorandum states none. Partial rejection keeps the block live with a note.
    """
    import provenance as _prov

    if failure:
        return schema.empty_om_comps(f"Not run: {failure}"), {}, []
    rent_rows = list((om_comps or {}).get("rent") or [])
    sale_rows = list((om_comps or {}).get("sale") or [])
    dropped = list((om_comps or {}).get("dropped") or [])
    notes: list[str] = []
    if dropped:
        notes.append(
            f"OM comparables: {len(dropped)} stated comparable(s) the model read were withheld "
            f"because a figure or quote could not be verified against the memorandum's own words."
        )
    if not rent_rows and not sale_rows:
        if dropped:
            return schema.empty_om_comps(
                f"{len(dropped)} comparable(s) the model read could not be verified against the "
                f"memorandum's own words, so none is shown"
            ), {}, notes
        return schema.empty_om_comps("the memorandum states no comparables"), {}, notes
    block = {
        "source": schema.OM_COMPS_SOURCE,
        "rent": rent_rows,
        "sale": sale_rows,
        "retail_note": schema.OM_COMPS_RETAIL_NOTE,
        "absent_reason": None,
    }
    # The same disclosure the static path writes (notes.for_deal): the upload path builds its
    # notes here, so it rides here too.
    notes.append(schema.OM_COMPS_APART_NOTE)
    stamps: dict = {}
    for kind, rows in (("rent", rent_rows), ("sale", sale_rows)):
        if rows:
            pages = sorted({r["page"] for r in rows if isinstance(r.get("page"), int)})
            stamps[f"om_comps.{kind}"] = _prov.read(
                om_name,
                "p." + ", ".join(str(pg) for pg in pages) if pages else "page not stated",
                note=f"{len(rows)} {kind} comparable(s) the memorandum states; every figure verified inside its own quote.",
            )
    return block, stamps, notes


def _run_note(om_doc, t12_doc, roll_doc, om_fields: dict, price_per_unit) -> str:
    """The Notes & Disclosures block for an upload run: what was read, and what was not."""
    read = [
        name
        for name, document in (
            ("an offering memorandum", om_doc),
            ("a trailing-twelve statement", t12_doc),
            ("a rent roll", roll_doc),
        )
        if document
    ]
    model_read = sorted(
        key for key, entry in (om_fields or {}).items()
        if isinstance(entry, dict) and entry.get("value") not in (None, "", schema.MISSING)
    )
    parts = [
        "This output set was generated from documents uploaded to a public demo. "
        + ("It read " + ", ".join(read) + "." if read else "No usable documents were read.")
    ]
    if model_read:
        parts.append(
            f"{len(model_read)} summary field(s) were read from the marketing document by "
            f"a language model and each one is carried with the sentence it came from; "
            f"any value whose sentence could not be found in the document was discarded "
            f"rather than shown."
        )
    # Name only the tabs that exist. Claiming the T-12 tab is computed from an uploaded
    # spreadsheet, when no spreadsheet was uploaded, is the same false confidence one
    # sentence further along from the "$0 NOI" the card used to print.
    computed_tabs = [
        name for name, document in (("T-12", t12_doc), ("rent-roll", roll_doc)) if document
    ]
    if computed_tabs:
        which = " and ".join(computed_tabs)
        plural = "tabs are" if len(computed_tabs) > 1 else "tab is"
        parts.append(
            f"Every figure on the {which} {plural} computed by code from the uploaded "
            f"spreadsheets, not read by a model."
        )
    # Built from the actual absent list, with the number agreeing. This used to read
    # "No trailing-twelve statement, no rent roll was provided" — two subjects, a singular
    # verb, and a comma doing the work of a conjunction. It also never mentioned an absent
    # offering memorandum, which is the input most summary fields come from.
    absent = [
        name
        for name, document in (
            ("offering memorandum", om_doc),
            ("trailing-twelve statement", t12_doc),
            ("rent roll", roll_doc),
        )
        if not document
    ]
    if absent:
        listed = "no " + absent[0] if len(absent) == 1 else (
            ", ".join("no " + a for a in absent[:-1]) + " and no " + absent[-1]
        )
        parts.append(
            ("There was " if len(absent) == 1 else "There were ")
            + listed
            + ", so every figure that would have come from "
            + ("it" if len(absent) == 1 else "them")
            + " reads MISSING rather than being estimated."
        )
    if t12_doc:
        # The category per row is visible on the T-12 Data tab, but how it was chosen is
        # not, and on an upload it is chosen from the words in the account label — there
        # is no account-code table for someone else's chart of accounts. Two careful
        # readers of the same statement genuinely differ on lines like a heating repair,
        # which one shop books as maintenance and another as capital. A reader who is not
        # told the basis has no way to know that is a judgement rather than a lookup.
        parts.append(
            "Each T-12 line's category was assigned from the wording of its account "
            "label, not from an account-code table, because account codes mean different "
            "things in different systems. The category chosen for every line is shown "
            "beside it on the T-12 Data tab so you can check it; lines that could not be "
            "placed confidently are excluded rather than guessed at, and are listed above."
        )
    if price_per_unit is None:
        parts.append(
            "No per-unit price was stated, so pricing and cap rates read MISSING rather "
            "than being computed from an assumption."
        )
    parts.append(
        "Comparable rents, demographics, competing supply and closing costs were not run: "
        "they need data sources this demo does not reach."
    )
    parts.append(
        "The uploaded files were deleted as soon as they were parsed, and this output set "
        "is deleted with the rest of the run."
    )
    return " ".join(parts)
