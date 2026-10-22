"""Decide what each uploaded document *is*, deterministically wherever possible.

A T-12 with twelve dated columns and a rent roll with a unit column and a lease-start
column are not judgement calls — they are recognisable from the header row, and a rule
that recognises them costs nothing per run, never drifts, and can be read by a person who
wants to know why a file was treated the way it was. The model is the fallback for the
genuinely ambiguous case, not the first resort.

Every decision carries its ``reason``, which is what the page shows next to the filename
and what a maintainer reads when a file is misfiled.

The header row is searched for rather than assumed: real exports bury it. One real
T-12 puts its header on row 8 under five rows of entity banner, and the rent roll puts its
on row 3 under a title and a square-footage total. A classifier that reads row 1 would
call both of them "context".
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from server.errors import ApiError

log = logging.getLogger("upload_demo.classify")

#: Roles the pipeline knows how to use. ``context`` is the honest "kept, but nothing in
#: the schema is filled from it" answer.
ROLES: tuple[str, ...] = ("om", "t12", "rent_roll", "trade_out", "context")

#: How far down a sheet to look for the header row.
HEADER_SEARCH_ROWS = 25

#: A T-12 has twelve of them. Eleven is a T-11 and still reads as a T-12 candidate; the
#: assembly step is what insists on twelve.
MIN_MONTH_HEADERS = 12

MONTH_WORDS = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)

#: A header naming a budget, a forecast or a variance is not a month column, whatever date
#: it carries. Kept in step with `source_readers._ANNUAL_NOT_ACTUAL`, which
#: applies the same rule one layer down, when the months are actually read.
NOT_ACTUAL_HEADER = re.compile(
    r"\b(budget|budgeted|bud|variance|variances|var|forecast|forecasted|fcst|"
    r"projection|projections|projected|proj|proforma|pro\s*forma|plan|planned|"
    r"estimate|estimated|budgt)\b",
    re.I,
)

MONTH_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\s*(" + "|".join(MONTH_WORDS) + r")[a-z]*[\s\-/,']*\d{2,4}\s*$", re.I),
    # Anchored, and with the time an export sometimes writes after the date. Unanchored,
    # this matched `2025-01 Budget` on its prefix and a sheet of budget columns scored as
    # a T-12.
    re.compile(r"^\s*\d{4}-\d{2}(-\d{2})?([ T]\d{1,2}:\d{2}(:\d{2})?)?\s*$", re.I),
    re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$"),
    re.compile(r"^\s*\d{1,2}/\d{4}\s*$"),
)

#: Headers are matched WORD for word (see ``_contains_any``), so a word only found inside
#: a longer one — ``unit`` in ``Community``, ``rent`` in ``Current Status`` — is not
#: evidence. That makes the vocabulary itself load-bearing: a synonym an export actually
#: uses has to be listed rather than caught by accident. ``rental`` is here for that
#: reason — ``Rental Rate`` is a real column and is not the word ``rent``.
UNIT_HEADERS = ("unit", "unit id", "unit no", "unit number", "apt", "apartment", "space")
RENT_HEADERS = (
    "rent", "rental", "lease rent", "current rent", "market rent", "actual rent",
    "charge", "scheduled rent",
)
LEASE_DATE_HEADERS = (
    "lease start", "lease begin", "lease from", "lease end", "expiration", "expires",
    "lease expiration", "move in", "move-in", "moveout", "move out", "move-out",
)
#: A trade-out report is recognised by a PAIR, not by one column.
#:
#: "New Rent" and "Rent Change" appear on perfectly ordinary rent rolls, and a single one
#: of them used to classify the whole file as a trade-out — a role assembly does not
#: consume, so the run silently lost its rent-roll analysis. The evidence has to be
#: something only a trade-out report carries: a BEFORE rent beside an AFTER rent, or a
#: phrase that names the thing outright.
#: Only headers a rent roll does NOT carry. "Current Rent" and "Asking Rent" are two of
#: the commonest columns on an ordinary roll, and "Rent Change" and "Increase %" appear on
#: any roll with a renewal column — pairing those made a normal rent roll a trade-out
#: report, a role assembly does not consume, so the run lost its rent-roll analysis
#: entirely.
TRADE_OUT_BEFORE = ("prior rent", "previous rent", "old rent", "prior lease",
                    "expiring rent", "former rent")
TRADE_OUT_AFTER = ("new rent", "renewal rent", "signed rent", "executed rent")
TRADE_OUT_EXPLICIT = ("trade out", "trade-out", "tradeout", "lease trade",
                      "renewal increase", "trade out %")

#: Kept as the union so anything scoring "does this row look like a header" still sees
#: every one of these words.
TRADE_OUT_HEADERS = TRADE_OUT_BEFORE + TRADE_OUT_AFTER + TRADE_OUT_EXPLICIT
T12_LABEL_HEADERS = ("account", "description", "gl", "account name", "category")

#: Words that make a text document an offering memorandum rather than a stray letter.
OM_WORDS = (
    "offering memorandum", "investment summary", "executive summary", "property overview",
    "unit mix", "rent comparables", "confidentiality", "broker", "asking price",
    "capitalization rate", "submarket",
)


@dataclass
class Signals:
    """Everything the rules noticed, kept so a decision can be explained."""

    month_headers: int = 0
    #: Which sheet of the workbook this decision was made on. Assembly reads THIS sheet;
    #: before it existed, a workbook with a cover tab first was classified from sheet 2
    #: and then parsed from sheet 1, which produced an absent section from a file that
    #: plainly had the data in it.
    sheet_index: int = 0
    header_row_index: int | None = None
    header_row: list[str] = field(default_factory=list)
    has_unit: bool = False
    has_rent: bool = False
    has_lease_date: bool = False
    has_trade_out: bool = False
    has_t12_label: bool = False
    om_words: int = 0
    words: int = 0

    def as_dict(self) -> dict:
        return {
            "month_headers": self.month_headers,
            "sheet_index": self.sheet_index,
            "header_row_index": self.header_row_index,
            "has_unit": self.has_unit,
            "has_rent": self.has_rent,
            "has_lease_date": self.has_lease_date,
            "has_trade_out": self.has_trade_out,
            "has_t12_label": self.has_t12_label,
            "om_words": self.om_words,
            "words": self.words,
        }


@dataclass
class Classification:
    filename: str
    kind: str
    role: str
    confident: bool
    reason: str
    signals: Signals

    def as_document(self) -> dict:
        """The ``documents[]`` entry the status route returns."""
        return {"filename": self.filename, "role": self.role, "note": self.reason}


# --------------------------------------------------------------------------------------
# Header sniffing
# --------------------------------------------------------------------------------------


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value).strip()


def is_month_header(value: object) -> bool:
    """Is this cell a month column heading?

    Dates come through as real ``datetime`` objects from openpyxl and as strings from csv,
    and the same export writes ``2025-07-31 00:00:00`` where another writes ``Jul-25``.
    """
    if isinstance(value, (dt.datetime, dt.date)):
        return True
    text = _as_text(value)
    if not text or len(text) > 24:
        return False
    # Budget, forecast, variance, plan and projection columns are not months however they
    # are dated — the same rule the T-12 parser applies, in the place that decides whether
    # a SHEET is a T-12 at all.
    if NOT_ACTUAL_HEADER.search(text):
        return False
    # `Aug 2025 Actual` — that system's heading; the same rule month_label applies.
    from source_readers import strip_actual  # tools/ — the reader owns the rule
    text = strip_actual(text)
    return any(pattern.match(text) for pattern in MONTH_PATTERNS)


#: Everything that separates one word of a header from the next. ``Unit #``, ``Rent/SF``,
#: ``Move-Out``, ``LEASE_START`` and ``Unit No.`` are the same five columns however the
#: export punctuated them.
_HEADER_SPLIT = re.compile(r"[^a-z0-9]+")


def header_words(cell: str) -> list[str]:
    """One header cell, as the words it is made of."""
    return [word for word in _HEADER_SPLIT.split((cell or "").lower()) if word]


def _word_matches(word: str, needle: str) -> bool:
    """One header word against one needle word — exact, or the plain plural of it.

    The plural is the only latitude taken. ``Units`` and ``Rents`` are the same columns as
    ``Unit`` and ``Rent``, and nothing else is: a rule that matched any word CONTAINING
    the needle is what read ``Community`` as a unit column.
    """
    return word == needle or word == needle + "s"


def _phrase_in(words: list[str], needle_words: list[str]) -> bool:
    """Does ``needle_words`` appear as consecutive words of ``words``?"""
    span = len(needle_words)
    if not span or span > len(words):
        return False
    return any(
        all(_word_matches(words[start + offset], needle_words[offset])
            for offset in range(span))
        for start in range(len(words) - span + 1)
    )


def _contains_any(cells: list[str], needles: tuple[str, ...]) -> bool:
    """Do any of these header cells NAME one of these things?

    Word-for-word, not substring. Substring matching found ``unit`` inside ``Community``
    and ``rent`` inside ``Current Status``, so a rent-comps or leasing-status export
    carried unit, rent and lease-date evidence and was confidently read as a rent roll —
    parsed as units when it arrived alone, and refused as a duplicate role when it arrived
    beside a real one. A needle of several words has to appear as consecutive words.
    """
    cell_words = [header_words(cell) for cell in cells]
    needle_words = [header_words(needle) for needle in needles]
    return any(
        _phrase_in(words, needle)
        for words in cell_words
        for needle in needle_words
    )


#: A month-header row is *mostly* months. A rent roll's data rows each carry three or
#: four real dates — lease start, expiration, move-in — and those dates are indentical in
#: shape to a T-12's column headings. Counting them without this ratio picks a data row
#: 20 lines into the rent roll as "the header", which is exactly what it used to do.
MIN_MONTH_FRACTION = 0.5


def find_header_row(rows: list[list]) -> tuple[int | None, list]:
    """The row that looks most like a header, searched not assumed.

    Scored rather than matched: month columns count only when they dominate the row, a
    row that names a unit or a rent or an account scores on that, and a tie goes to the
    earlier row — which is where a real header sits relative to its data.
    """
    best_index: int | None = None
    best_score = 0
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        text_cells = [_as_text(c) for c in row]
        filled = [cell for cell in text_cells if cell]
        if not filled:
            continue
        months = sum(1 for cell in row if is_month_header(cell))
        dominated = months / len(filled) >= MIN_MONTH_FRACTION
        # Count how many *families* of column the row names, not merely whether it names
        # one. A title line like "One Line Rent Roll" names exactly one and would
        # otherwise tie with the real header three rows below it and win on being first.
        families = sum(
            2 * _contains_any(text_cells, group)
            for group in (UNIT_HEADERS, RENT_HEADERS, LEASE_DATE_HEADERS, T12_LABEL_HEADERS)
        )
        families += 3 * _contains_any(text_cells, TRADE_OUT_HEADERS)
        score = (months * 2 if dominated else 0) + families
        if score > best_score:
            best_score, best_index = score, index
    if best_index is None:
        return (0, rows[0]) if rows else (None, [])
    return best_index, rows[best_index]


def grid_signals(sheets: list[dict]) -> Signals:
    """Read the strongest sheet in a workbook. A one-sheet export is the common case."""
    signals = Signals()
    for position, sheet in enumerate(sheets):
        rows = sheet.get("rows") or []
        if not rows:
            continue
        index, header = find_header_row(rows)
        text_cells = [_as_text(c) for c in header]
        filled = [cell for cell in text_cells if cell] or [""]
        months = sum(1 for cell in header if is_month_header(cell))
        if months / len(filled) < MIN_MONTH_FRACTION:
            # Dates scattered through a row of unit data are not month columns.
            months = 0
        candidate = Signals(
            month_headers=months,
            sheet_index=position,
            header_row_index=index,
            header_row=text_cells,
            has_unit=_contains_any(text_cells, UNIT_HEADERS),
            has_rent=_contains_any(text_cells, RENT_HEADERS),
            has_lease_date=_contains_any(text_cells, LEASE_DATE_HEADERS),
            has_trade_out=is_trade_out_header(text_cells),
            has_t12_label=_contains_any(text_cells, T12_LABEL_HEADERS),
        )
        if _strength(candidate) > _strength(signals):
            signals = candidate
    return signals


def detect_t12_columns(rows: list[list], header_index: int) -> tuple[int | None, int]:
    """``(code_col, label_col)``, 1-based, for a statement's header row.

    Extraction used to declare column A the account code and column B the label, full
    stop, while classification was happy to recognise a T-12 from month headings anywhere.
    A statement with its descriptions in column A, or with a department column inserted
    between the code and the description, was therefore classified correctly and then read
    from the wrong columns — producing rows labelled "Residential", or no rows at all.

    The rule is the one a person uses: everything left of the first month column is
    bookkeeping, and the LABEL column is the one holding varied prose. Varied matters —
    an inserted "Department" column is text too, but it repeats a handful of values, so
    columns are scored on text density times distinctness. A code column is optional: it
    is a column left of the label whose values are mostly bare numbers.
    """
    header = rows[header_index] if 0 <= header_index < len(rows) else []
    first_month = next(
        (i for i, cell in enumerate(header) if is_month_header(cell)), len(header)
    )
    if first_month <= 0:
        return (None, 1)

    body = rows[header_index + 1: header_index + 1 + T12_SCAN_ROWS]
    scores: list[tuple[float, int]] = []
    numeric: dict[int, float] = {}
    for column in range(first_month):
        values = [
            str(row[column]).strip()
            for row in body
            if column < len(row) and row[column] is not None and str(row[column]).strip()
        ]
        if not values:
            continue
        codes = sum(1 for v in values if _CODE_LIKE.match(v))
        wordy = [v for v in values if not _CODE_LIKE.match(v) and any(c.isalpha() for c in v)]
        numeric[column] = codes / len(values)
        if wordy:
            distinctness = len(set(wordy)) / len(wordy)
            scores.append((len(wordy) * distinctness, column))
    if not scores:
        # No prose anywhere to the left of the months: fall back to the last column
        # before them, which is where a label sits when the body is empty.
        return (None, first_month)

    best = max(scores, key=lambda pair: (pair[0], pair[1]))
    label_col = best[1]
    code_col = next(
        (c for c in range(label_col) if numeric.get(c, 0.0) >= T12_CODE_FRACTION), None
    )
    return (None if code_col is None else code_col + 1, label_col + 1)


#: Rows of the statement body read to decide which column is which. A T-12 declares itself
#: in its first few lines; reading the whole sheet would only make a big upload slower.
T12_SCAN_ROWS = 200

#: How much of a column has to be bare digits before it is an account-code column.
T12_CODE_FRACTION = 0.6

_CODE_LIKE = re.compile(r"^[\d][\d\-\.]*$")


def is_trade_out_header(cells: list[str]) -> bool:
    """Trade-out-specific evidence, and no rent-roll evidence at all.

    Two rules, and the second is the one that matters. A trade-out report needs a BEFORE
    rent beside an AFTER rent, or a phrase that names the thing outright. And a sheet
    that ALSO looks like a rent roll — units, rents, lease dates — is a rent roll: the
    rent-roll reading is the one this pipeline can actually use, so where the evidence
    supports both, it wins rather than silently costing the visitor their analysis.
    """
    paired = (
        _contains_any(cells, TRADE_OUT_BEFORE)
        and _contains_any(cells, TRADE_OUT_AFTER)
    )
    if not (paired or _contains_any(cells, TRADE_OUT_EXPLICIT)):
        return False
    looks_like_a_roll = (
        _contains_any(cells, UNIT_HEADERS)
        and _contains_any(cells, RENT_HEADERS)
        and _contains_any(cells, LEASE_DATE_HEADERS)
    )
    return not looks_like_a_roll


def _strength(signals: Signals) -> int:
    return (
        signals.month_headers * 2
        + (2 if signals.has_unit else 0)
        + (2 if signals.has_rent else 0)
        + (1 if signals.has_lease_date else 0)
        + (3 if signals.has_trade_out else 0)
        + (1 if signals.has_t12_label else 0)
    )


def text_signals(text: str) -> Signals:
    lowered = (text or "").lower()
    return Signals(
        om_words=sum(1 for word in OM_WORDS if word in lowered),
        words=len((text or "").split()),
    )


# --------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------


def classify(document: dict) -> Classification:
    """One normalized document in, one role out, always with a reason."""
    filename = document.get("filename", "(unnamed)")
    kind = document.get("kind", "")
    sheets = document.get("sheets") or []
    text = document.get("text") or ""

    if sheets:
        signals = grid_signals(sheets)

        if signals.has_trade_out:
            return Classification(
                filename, kind, "trade_out", True,
                "Lease trade-out: the header names a prior and a new rent.", signals,
            )
        if signals.month_headers >= MIN_MONTH_HEADERS:
            return Classification(
                filename, kind, "t12", True,
                f"Trailing-twelve statement: {signals.month_headers} month columns on the "
                f"header row.", signals,
            )
        if signals.has_unit and signals.has_rent and signals.has_lease_date:
            return Classification(
                filename, kind, "rent_roll", True,
                "Rent roll: the header names units, rents and lease dates.", signals,
            )
        # Enough shape to be one of the two, not enough to say which.
        if signals.month_headers >= 6 or (signals.has_unit and signals.has_rent):
            return Classification(
                filename, kind, "context", False,
                "A spreadsheet with some of the marks of a T-12 or a rent roll, but not "
                "enough to be sure.", signals,
            )
        return Classification(
            filename, kind, "context", True,
            "A spreadsheet with no T-12 or rent-roll header.", signals,
        )

    signals = text_signals(text)
    if kind in ("pdf", "docx", "doc"):
        if signals.om_words >= 2:
            return Classification(
                filename, kind, "om", True,
                f"Offering memorandum: {signals.om_words} of the phrases a marketing "
                f"package uses.", signals,
            )
        if signals.words >= 400:
            return Classification(
                filename, kind, "om", False,
                "A long document that reads like a marketing package, but it does not use "
                "the usual phrases.", signals,
            )
        return Classification(
            filename, kind, "context", True,
            "A short document with none of the marks of an offering memorandum.", signals,
        )

    return Classification(
        filename, kind, "context", True, "Kept as background reading.", signals
    )


#: Roles that only one document may hold. Two "context" files are fine; two rent rolls are
#: a question only the person who uploaded them can answer.
EXCLUSIVE_ROLES: frozenset[str] = frozenset({"om", "t12", "rent_roll", "trade_out"})


def resolve(classifications: list[Classification]) -> list[Classification]:
    """Refuse a batch where two files claim the same job, naming both.

    Guessing between them is the wrong move: picking the wrong T-12 produces a complete,
    confident, wrong answer, and the person who uploaded both knows in one second which is
    which.
    """
    seen: dict[str, list[str]] = {}
    for item in classifications:
        if item.role in EXCLUSIVE_ROLES:
            seen.setdefault(item.role, []).append(item.filename)

    for role, names in seen.items():
        if len(names) > 1:
            label = ROLE_LABELS[role]
            raise ApiError(
                400,
                "duplicate_role",
                f"Two of those files look like the {label}: {names[0]} and {names[1]}. "
                f"Send one of them and try again.",
            )
    # Roles and signals, never filenames — same reason as the intake logger. The
    # filename is still returned to the caller and shown to the visitor on their own run
    # page; it just does not go to a log that outlives the run.
    log.info(
        "classify: %d document(s) -> %s",
        len(classifications),
        ", ".join(
            f"{item.role}{'' if item.confident else '?'}" for item in classifications
        ),
    )
    for item in classifications:
        log.debug("classify: %s signals %s", item.role, item.signals.as_dict())
    return classifications


ROLE_LABELS: dict[str, str] = {
    "om": "offering memorandum",
    "t12": "trailing-twelve statement",
    "rent_roll": "rent roll",
    "trade_out": "lease trade-out report",
    "context": "background document",
}


#: Rows scanned to PROFILE a spreadsheet for classification. More than the mapping call
#: needs is pointless here — the question is only what kind of file this is — and the rows
#: themselves never travel, only the shape they imply.
PROFILE_ROWS = 200


def excerpt_for(document: dict) -> str:
    """What the model is shown when the deterministic rules could not decide.

    A text document contributes its opening; a spreadsheet contributes **column profiles**
    and no row text at all.

    The first cut sent three raw rows with the people-named columns masked, which is the
    same mistake the mapping prompt had already been fixed for: a ``Comments``, ``Notes``
    or ``Mailing Address`` column is free text about a resident and its header matches no
    blacklist. Deciding "is this a rent roll" needs the column NAMES and their shapes —
    never a cell of prose — so it now uses exactly the design the mapping call uses, and
    the two cannot drift because they are the same function.
    """
    text = document.get("text") or ""
    if text:
        return text[:4000]
    from server.extract_llm import column_profiles, render_profiles

    lines: list[str] = []
    for sheet in (document.get("sheets") or [])[:2]:
        rows = sheet.get("rows") or []
        if not rows:
            continue
        index, header = find_header_row(rows)
        profiles = column_profiles(
            [_as_text(h) for h in header], rows[index + 1: index + 1 + PROFILE_ROWS]
        )
        lines.append(f"[sheet {sheet.get('name', '')}]")
        lines.append(render_profiles(profiles))
    return "\n".join(lines)[:4000]


def classify_batch(
    documents: list[dict],
    *,
    ledger=None,
    role_hints: list[dict] | None = None,
    model=None,
) -> list[Classification]:
    """Every document's role: deterministic first, the model where that was not enough.

    Two things this used to skip, both from an earlier review.

    **The model arm was dead.** An unconfident result was resolved straight through as
    ``context``, so a long offering memorandum that happens not to use two of the marketing
    phrases, and any spreadsheet with only half the marks of a T-12, were quietly dropped
    from the run — while ``extract_llm.classify()`` sat there implemented and never called.

    **Role hints were parsed after this ran** and thrown away. "The second file is the rent
    roll" is the one thing a visitor can tell us that no header row can, and it is exactly
    what resolves the duplicate-role refusal below.
    """
    results = [classify(document) for document in documents]

    if ledger is not None:
        if model is None:
            from server import extract_llm as model  # noqa: PLW0127 (late import: cycle)
        for position, (document, item) in enumerate(zip(documents, results)):
            if item.confident:
                continue
            excerpt = excerpt_for(document)
            if not excerpt.strip():
                continue
            try:
                answer = model.classify(excerpt, ledger)
            except getattr(model, "BudgetExhausted", ()):
                # A run that has hit its own spending cap is a stop, not a hiccup.
                raise
            except Exception:  # noqa: BLE001
                # Everything else: a classification fallback is not worth failing a run
                # over. The deterministic answer stands and the log says it happened.
                log.warning("classify: the model arm failed for a %s document; keeping "
                            "the deterministic answer", item.kind)
                continue
            role = str((answer or {}).get("role") or "")
            if role not in ROLES:
                # Not echoed: it is a model-authored string, produced while reading a
                # stranger's document.
                log.warning("classify: the model returned a role that is not one of the "
                            "%d known ones; keeping the deterministic answer", len(ROLES))
                continue
            reason = str((answer or {}).get("reason") or "").strip() or "no reason given"
            results[position] = Classification(
                item.filename, item.kind, role, True,
                f"Read by the model, which the rules could not decide: {reason}",
                item.signals,
            )

    return resolve(apply_role_hints(results, role_hints))


def apply_role_hints(
    classifications: list[Classification], role_hints: list[dict] | None
) -> list[Classification]:
    """Let the visitor's own note override what the rules decided.

    A hint is authoritative for the file it names — the person who uploaded it knows —
    and it also DEMOTES any other file holding the same exclusive role, which is how a
    note resolves the duplicate that :func:`resolve` would otherwise refuse. Hints naming
    a file nobody uploaded are dropped upstream, in ``extract_llm.parse_notes``.
    """
    if not role_hints:
        return classifications
    by_name = {item.filename: position for position, item in enumerate(classifications)}
    out = list(classifications)
    for hint in role_hints:
        name = str((hint or {}).get("filename") or "")
        role = str((hint or {}).get("role") or "")
        if role not in ROLES or name not in by_name:
            continue
        position = by_name[name]
        if role in EXCLUSIVE_ROLES:
            for other, item in enumerate(out):
                if other != position and item.role == role:
                    out[other] = Classification(
                        item.filename, item.kind, "context", True,
                        f"Kept as background reading: you said {name} is the "
                        f"{ROLE_LABELS[role]}.",
                        item.signals,
                    )
        current = out[position]
        out[position] = Classification(
            current.filename, current.kind, role, True,
            f"You said this file is the {ROLE_LABELS[role]}.", current.signals,
        )
        log.info("classify: a note set one document's role to %s", role)
    return out
