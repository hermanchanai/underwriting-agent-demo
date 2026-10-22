"""Provenance stamps: where every published number came from.

A stamp answers four questions about one value:

* ``source_doc`` — which document it came out of;
* ``locator`` — *where* in that document. ``Sheet!Cell`` or ``Sheet!Range`` for a
  spreadsheet, ``p.12`` for a PDF page, a URL for a public record, ``—`` for something
  supplied directly;
* ``method`` — ``read`` (lifted straight off the source), ``derived`` (a rule applied to
  other values), or ``computed`` (arithmetic over other values);
* ``inputs`` — for ``derived`` and ``computed``, the paths this value was built from. A
  derived value that cannot name its inputs is not auditable, so the schema refuses one.

``quote`` carries the supporting text for a value read from prose. The rule it follows is
the one a document-extraction contract has to have: **the quote must appear verbatim and
contiguously in the source text**, so a reader can find it. A paraphrase that supports a
number is not evidence of it — it is a second claim about it.

Stamps live under the deal's ``provenance`` section, keyed by the dotted path of the value
they describe, and the four workbooks each render the subset that applies to them.
"""

from __future__ import annotations

from typing import Any, Iterable

METHODS: tuple[str, ...] = ("read", "derived", "computed")

STAMP_KEYS: dict[str, tuple[bool, tuple[type, ...]]] = {
    "source_doc": (True, (str,)),
    "locator": (True, (str,)),
    "method": (True, (str,)),
    "inputs": (False, (list,)),
    "quote": (False, (str, type(None))),
    "note": (False, (str, type(None))),
}

#: Values that must carry a stamp. Anything a reader could act on: every summary field
#: they might quote, the pricing that drives the cap rates, the tax block, the closing
#: costs, and the structural facts about the T-12 and the roll.
REQUIRED_PATHS: tuple[str, ...] = (
    # identification and property
    "summary.deal_name",
    "summary.street_address",
    "summary.city",
    "summary.state",
    "summary.zip",
    "summary.county",
    "summary.msa",
    "summary.product_type",
    "summary.structure",
    "summary.units_total",
    "summary.year_built",
    "summary.product_detail",
    "summary.construction_status",
    "summary.delivery_date",
    # parties and status
    "summary.seller_entity",
    "summary.listing_brokerage",
    "summary.deal_status",
    # pricing
    "summary.pricing_guidance",
    "pricing.price_per_unit",
    "pricing.units_total",
    # rents
    "summary.in_place_rent_avg",
    "summary.market_rent_for_model",
    "summary.fair_market_value",
    # tax — including every row of the evidence trail, which is the whole point of it
    "summary.tax_rate_as_billed",
    "summary.assessed_value_basis",
    "summary.assessment_ratio",
    "summary.tax_divisor",
    "summary.tax_exemption",
    "summary.tax_parcel_id",
    "summary.tax_area_code",
    "summary.tax_basis_label",
    "summary.tax_basis_note",
    "summary.tax_billed_amount",
    "summary.tax_billed_year",
    # closing costs
    "closing_costs.transfer_tax.amount",
    "closing_costs.mortgage_tax.amount",
    # schools
    "summary.school_elementary_name",
    "summary.school_middle_name",
    "summary.school_high_name",
    # structural
    "t12.month_labels",
    "t12.units",
    "t12.source_subtotals",
    "rent_roll.as_of",
    "rent_roll.lookback_months",
    "rent_roll.new_tenant_tolerance_days",
    "rent_roll.units",
    "comps.rows",
    # Which tier a community lands in is DERIVED — from its distance, its product, its
    # vintage and the subject's own — and it decides whether a reader treats that
    # community as the market. A derived value that changes the reader's conclusion has
    # to name its inputs like any other.
    "comps.rows.tier",
    "comps.nearby",
    "demand.rings",
    "supply.rings",
    "trade_out.rows",
)

#: Which workbook shows which stamps. A trail tab that lists everything is a trail nobody
#: reads, so each workbook carries the subset a reader of *that* workbook would question.
WORKBOOK_SCOPES: dict[str, tuple[str, ...]] = {
    "deal_summary": ("summary.", "pricing.", "closing_costs.", "comps.", "demand.", "supply.", "om_comps."),
    "t12_analysis": ("t12.", "pricing."),
    "rent_roll": ("rent_roll.", "trade_out.", "summary.in_place_rent_avg",
                  "summary.market_rent_for_model", "summary.units_total"),
    "manifest": (),  # the manifest shows the whole trail
}


def stamp(
    source_doc: str,
    locator: str,
    method: str,
    *,
    inputs: Iterable[str] | None = None,
    quote: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Build one stamp. Kept tiny on purpose — it is called a hundred times."""
    out: dict[str, Any] = {
        "source_doc": source_doc,
        "locator": locator,
        "method": method,
    }
    if inputs:
        out["inputs"] = list(inputs)
    if quote:
        out["quote"] = quote
    if note:
        out["note"] = note
    return out


def read(source_doc: str, locator: str, *, quote: str | None = None, note: str | None = None):
    return stamp(source_doc, locator, "read", quote=quote, note=note)


#: Stamp sources that record an ABSENCE — the value reads MISSING and the stamp says why —
#: rather than evidence for a value. The audit trail lists these apart, in one block, so a
#: third of the table is not "nothing here" rows standing beside the evidence rows.
#: Defined HERE (schema imports this module, so this module cannot import schema at load);
#: ``schema.INTERNAL_ONLY_SOURCE`` re-exports it.
INTERNAL_ONLY_SOURCE = "Internal pipeline only"
ABSENCE_SOURCES: tuple[str, ...] = (
    "Not stated in the uploaded documents",
    "No county portal reached",
    "Not run in the upload demo",
    "Not asked for by the upload demo",
    INTERNAL_ONLY_SOURCE,
)


def internal_only(path: str) -> dict[str, Any]:
    """The stamp for a value the demo leaves to the internal pipeline: MISSING here, and
    the reason names the capability the demo does not have."""
    import schema  # lazy: schema imports this module

    found = schema.internal_only(path)
    reason = found[1] if found else "Produced by the internal pipeline only."
    return stamp(INTERNAL_ONLY_SOURCE, "—", "read", note=reason)


def is_absence(st: dict | None) -> bool:
    return isinstance(st, dict) and st.get("source_doc") in ABSENCE_SOURCES


def derived(source_doc: str, locator: str, inputs: Iterable[str], *, note: str | None = None):
    return stamp(source_doc, locator, "derived", inputs=inputs, note=note)


def computed(source_doc: str, locator: str, inputs: Iterable[str], *, note: str | None = None):
    return stamp(source_doc, locator, "computed", inputs=inputs, note=note)


def validate_block(provenance: Any, errors: list[str]) -> None:
    """Every stamp well-formed; every required path stamped; derived values cite inputs."""
    if not isinstance(provenance, dict):
        errors.append("provenance: expected an object keyed by dotted value path")
        return

    for path, st in provenance.items():
        where = f"provenance[{path!r}]"
        if not isinstance(st, dict):
            errors.append(f"{where}: expected an object")
            continue
        for key, (required, types) in STAMP_KEYS.items():
            if key not in st:
                if required:
                    errors.append(f"{where}: missing required key '{key}'")
                continue
            if not isinstance(st[key], types):
                names = "/".join(t.__name__ for t in types)
                errors.append(
                    f"{where}.{key}: expected {names}, got {type(st[key]).__name__}"
                )
        for key in st:
            if key not in STAMP_KEYS:
                errors.append(f"{where}: unknown key '{key}'")

        method = st.get("method")
        if method is not None and method not in METHODS:
            errors.append(f"{where}.method: expected one of {METHODS}, got {method!r}")
        if method in ("derived", "computed"):
            inputs = st.get("inputs")
            if not isinstance(inputs, list) or not inputs:
                errors.append(
                    f"{where}: method {method!r} must cite the inputs it was built from — "
                    f"a derived value that cannot name its inputs is not auditable"
                )
            elif any(not isinstance(i, str) or not i.strip() for i in inputs):
                errors.append(f"{where}.inputs: every input must be a non-empty string")
        if method == "read" and not str(st.get("locator", "")).strip():
            errors.append(f"{where}: a value read from a source must say where in it")

    missing = [p for p in REQUIRED_PATHS if p not in provenance]
    if missing:
        errors.append(
            f"provenance: {len(missing)} value(s) with no stamp: "
            + ", ".join(missing[:12])
            + ("…" if len(missing) > 12 else "")
        )


def coverage(provenance: dict) -> tuple[int, int, float]:
    """(stamped, required, percent) over the required set."""
    if not isinstance(provenance, dict):
        return (0, len(REQUIRED_PATHS), 0.0)
    stamped = sum(1 for p in REQUIRED_PATHS if p in provenance)
    total = len(REQUIRED_PATHS)
    return (stamped, total, 100.0 * stamped / total if total else 0.0)


def quote_is_contiguous(quote: str, source_text: str) -> bool:
    """Whether a supporting quote really appears in the source, contiguously.

    Whitespace is normalized on both sides — a PDF extractor breaks lines wherever the
    column ended — but nothing else is. A quote that only matches after paraphrase is not
    a quote.
    """
    if not quote:
        return True
    norm = " ".join(quote.split()).lower()
    hay = " ".join(source_text.split()).lower()
    return norm in hay


def for_workbook(provenance: dict, workbook: str) -> list[tuple[str, dict]]:
    """The stamps one workbook's Audit Trail tab should carry, in path order."""
    scopes = WORKBOOK_SCOPES.get(workbook, ())
    if not scopes:
        return sorted(provenance.items())
    out = [
        (path, st)
        for path, st in provenance.items()
        if any(path.startswith(s) or path == s for s in scopes)
    ]
    return sorted(out)


AUDIT_COLUMNS: tuple[str, ...] = (
    "Value",
    "Value as published",
    "Source document",
    "Where in it",
    "How",
    "Built from",
    "Supporting quote / note",
)


def render_value(value: object) -> str:
    """The published value, short enough to sit on a trail row.

    A trail that lists a path and a source but not the number it describes cannot be
    checked against the output it claims to explain — a reader would have to hold both
    open and match them by eye.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, list):
        return f"{len(value)} entries"
    if isinstance(value, dict):
        return f"{len(value)} keys"
    text = str(value)
    return text if len(text) <= 90 else text[:87] + "…"


def compact(st: dict | None) -> str:
    """One line of provenance, for rendering BESIDE a value rather than in a trail tab.

    Document and locator only. The quote stays in the full trail: inline space is scarce,
    and the question a reader asks next to a number is "where did this come from?", not
    "what were the exact words?". Method is included only when it is not a plain read,
    because "derived" next to a figure changes how it should be treated.
    """
    if not isinstance(st, dict):
        return ""
    doc = str(st.get("source_doc") or "").strip()
    loc = str(st.get("locator") or "").strip()
    method = str(st.get("method") or "").strip()
    head = " — ".join(x for x in (doc, loc) if x and x != "—")
    if method and method != "read":
        head = f"{head} ({method})" if head else f"({method})"
    return head


def lookup(deal: dict, path: str) -> dict | None:
    """The stamp for one value path, or None. Never raises on a malformed block."""
    prov = deal.get("provenance") if isinstance(deal, dict) else None
    if not isinstance(prov, dict):
        return None
    st = prov.get(path)
    return st if isinstance(st, dict) else None


def compact_for(deal: dict, path: str) -> str:
    """`compact()` of whatever stamp covers ``path``, falling back to its parent.

    A per-row value like ``rent_roll.units`` is stamped once for the whole list; a reader
    looking at one row still wants that source. So a miss walks up one dotted level rather
    than rendering nothing — an empty source column reads as "unsourced", which would be
    a stronger claim than the truth.
    """
    st = lookup(deal, path)
    if st is None and "." in path:
        st = lookup(deal, path.rsplit(".", 1)[0])
    return compact(st)


def as_row(path: str, st: dict, value: object = None) -> list[str]:
    quote = st.get("quote") or ""
    note = st.get("note") or ""
    detail = quote if not note else (f"{quote} — {note}" if quote else note)
    return [
        path,
        render_value(value),
        st.get("source_doc", ""),
        st.get("locator", ""),
        st.get("method", ""),
        ", ".join(st.get("inputs", []) or []),
        detail,
    ]


__all__ = [
    "METHODS",
    "REQUIRED_PATHS",
    "WORKBOOK_SCOPES",
    "AUDIT_COLUMNS",
    "stamp",
    "read",
    "derived",
    "computed",
    "validate_block",
    "coverage",
    "quote_is_contiguous",
    "for_workbook",
    "as_row",
    "render_value",
]
