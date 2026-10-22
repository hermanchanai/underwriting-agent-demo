#!/usr/bin/env python3
"""Public vs internal is a function on the interchange file, not a fork of the pipeline.

E1 of PLAN_single_engine.md. One engine computes ``deal_outputs.json``;
this module decides what a given SURFACE may carry:

* ``internal`` — nothing withheld, nothing renamed: the deal exactly as the run produced it.
* ``public``  — every value the registry ``schema.INTERNAL_ONLY`` names is MISSING with the
  "Internal pipeline only" stamp *unless a document stated it* (a memorandum's parcel number
  is public; a web-looked-up market value is not); and, when an :class:`Identity` is given,
  the deal is re-identified: name, parties and address swapped, every memorandum quote that
  would place the real asset withheld, pricing marked illustrative, every comp row naming
  the asset dropped, the memorandum's own comparables withheld. A registry ROW column
  (``comps.nearby[].units`` / ``comps.rows[].units``, the tracker's licensed unit counts)
  never keeps its licensed value: a row shows the public-record count with its source where
  one exists and reads MISSING with the label elsewhere, with one stamp for the column. :func:`apply_upload` is the visitor run's entry point:
  the row columns only, since an upload carries no summary-level registry value.

Writers and previews never call this. They read the flags it leaves on the JSON — the
stamps, ``meta.illustrative_pricing``, the absent reasons — and render whatever they find.
That is the whole point: a second caller (the operator's run in internal mode, a visitor run in
public mode) gets the same decisions by calling :func:`apply`, not by copying a script.

``apply`` never mutates its input. It is deterministic on the same inputs, and it preserves
key insertion order so a restaged deal serialises byte for byte (``test_policy.py`` pins the
published sample against it).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Literal

import provenance as pv
import schema

Mode = Literal["public", "internal"]
MODES: tuple[str, ...] = ("public", "internal")

#: The public reason for an absent lease trade-out block (the registry says why it is
#: internal-only; this is the sentence the tab shows).
TRADE_OUT_REASON = (
    "Lease trade-out is an internal-pipeline input: the demo does not ask for a trade-out "
    "report, on the sample or on an upload."
)
SCHOOLS_SOURCE = "Internal underwriting agent — attendance-zone school lookup"
SCHOOLS_NOTE = (
    "Looked up for the illustrative address by the internal underwriting agent; "
    "the public demo does not run this on an uploaded deal."
)
SCHOOL_LEVELS = ("elementary", "middle", "high")
_ABSENT = (None, "", schema.MISSING)


@dataclass(frozen=True)
class Identity:
    """What a public sample says it is, and what must not leak from what it really is."""

    deal_name: str
    deal_slug: str
    address: dict  # street_address, city, state, zip, county, msa
    seller: str
    brokerage: str
    party_note: str
    address_provenance: str
    #: In a MEMORANDUM quote or note: the quote is withheld. Broad on purpose.
    identifying: re.Pattern
    #: ANYWHERE in the set: the row is dropped, and any remaining mention is a watchlist hit.
    asset: re.Pattern
    withheld_note: str
    om_comps_reason: str
    #: Set → pricing is marked illustrative with this note. None → the run's pricing stands.
    pricing_note: str | None = None
    deal_name_note: str = "The deal name as the memorandum states it."
    #: Set → the name is this identity's own invention and is stamped to THIS source, not
    #: to the memorandum. An illustrative name credited to the memorandum is the audit
    #: trail asserting something the document does not say.
    deal_name_source: str | None = None
    address_paths: tuple[str, ...] = field(default=(
        "summary.street_address", "summary.city", "summary.state", "summary.zip",
        "summary.county", "summary.msa", "summary.address_provenance"))


def om_document(deal: dict) -> str | None:
    return next((e["document"] for e in deal["meta"].get("provenance", []) if e.get("kind") == "om"), None)


# ---- the steps ---------------------------------------------------------------------------

def withhold_quotes(deal: dict, om_doc: str, identifying: re.Pattern, note: str) -> list[str]:
    """Replace every memorandum quote that identifies the asset; return the paths touched."""
    touched = []
    for path, st in deal["provenance"].items():
        if not isinstance(st, dict) or st.get("source_doc") != om_doc:
            continue
        text = " ".join(str(st.get(k) or "") for k in ("quote", "note"))
        if identifying.search(text):
            st.pop("quote", None)
            st["note"] = note
            touched.append(path)
    return touched


def substitute_identity(deal: dict, identity: Identity, om_doc: str, log: list[str]) -> list[str]:
    """Name, parties, address; then the memorandum quotes that would give the swap away."""
    summary, meta = deal["summary"], deal["meta"]
    summary["deal_name"] = identity.deal_name
    meta["deal_name"] = identity.deal_name
    meta["deal_slug"] = identity.deal_slug
    deal["provenance"]["summary.deal_name"] = (
        pv.read(identity.deal_name_source, "—", note=identity.deal_name_note)
        if identity.deal_name_source
        else pv.read(om_doc, "p.3", note=identity.deal_name_note)
    )
    summary["seller_entity"] = identity.seller
    summary["listing_brokerage"] = identity.brokerage
    for path in ("summary.seller_entity", "summary.listing_brokerage"):
        deal["provenance"][path] = pv.read("Illustrative name", "—", note=identity.party_note)
    summary.update(identity.address)
    summary["address_provenance"] = identity.address_provenance
    for path in identity.address_paths:
        deal["provenance"][path] = pv.read("Illustrative address", "—", note=identity.address_provenance)
    withheld = withhold_quotes(deal, om_doc, identity.identifying, identity.withheld_note)
    a = identity.address
    log.append(f"identity: name → {identity.deal_name!r}; seller/brokerage withheld; address → "
               f"{a['street_address']}, {a['city']} {a['state']} {a['zip']}; "
               f"{len(withheld)} memorandum quote(s) withheld: {', '.join(withheld)}")
    return withheld


def withhold_internal_only(deal: dict, log: list[str], *, schools: dict | None = None) -> list[str]:
    """Every registry path whose value the run did not read from a document reads MISSING
    with the "Internal pipeline only" stamp. A path already so stamped is left alone; a path
    with no stamp gets none (the registry never invents provenance). Returns the paths
    relabelled. ``schools`` — the internal agent's lookup, shown and stamped as its work."""
    summary, prov = deal["summary"], deal["provenance"]
    relabelled: list[str] = []
    for key in list(summary):
        path = f"summary.{key}"
        if not schema.internal_only(path):
            continue
        st = prov.get(path)
        stated = isinstance(st, dict) and not pv.is_absence(st)
        if stated and summary.get(key) not in _ABSENT:
            continue  # a document stated it: public
        summary[key] = schema.MISSING
        if isinstance(st, dict) and st.get("source_doc") != pv.INTERNAL_ONLY_SOURCE:
            prov[path] = pv.internal_only(path)
            relabelled.append(path)
    if schools:
        for level in SCHOOL_LEVELS:
            entry = (schools.get("schools") or {}).get(level) or {}
            name, rating = entry.get("name"), entry.get("rating")
            summary[f"school_{level}_name"] = name or schema.MISSING
            summary[f"school_{level}_rating"] = rating if rating is not None else schema.MISSING
            prov[f"summary.school_{level}_name"] = pv.read(
                SCHOOLS_SOURCE, f"{schools.get('address', '')} on {schools.get('looked_up', '')}", note=SCHOOLS_NOTE)
        log.append("schools: " + ", ".join(
            f"{lv} {((schools.get('schools') or {}).get(lv) or {}).get('name')} "
            f"({((schools.get('schools') or {}).get(lv) or {}).get('rating')})" for lv in SCHOOL_LEVELS))
    deal["trade_out"] = {"rows": [], "absent_reason": TRADE_OUT_REASON}
    prov["trade_out.rows"] = pv.internal_only("trade_out.rows")
    relabelled.append("trade_out.rows")
    log.append("internal-only: " + ", ".join(relabelled) + " → MISSING with the label")
    relabelled += withhold_internal_only_rows(deal, log)
    return relabelled


def _rows_at(deal: dict, dotted: str):
    node = deal
    for key in dotted.split("."):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, list) else None


def withhold_internal_only_rows(deal: dict, log: list[str]) -> list[str]:
    """Every registry ROW column (``comps.nearby[].units``, ``comps.rows[].units``): the
    licensed value never stays. Where the registry names a PUBLIC stand-in
 and the row carries one, the column
    shows the stand-in's value and ``<column>_source`` names its source and as-of; every
    other row reads MISSING, with ``schema.NO_PUBLIC_RECORD_LABEL`` as its source when a
    stand-in is registered. The rows themselves stay — identity, distance, year built,
    reason. The column carries ONE "Internal pipeline only" stamp (what is held back is the
    licensed figure), also when no row carried one. An empty
    list gets no stamp. Deterministic and idempotent: the stand-in columns are kept, so a
    second pass recomputes the same value. Returns the registry paths withheld."""
    withheld: list[str] = []
    for path, rows_path, column, _reason in schema.internal_only_row_columns():
        rows = _rows_at(deal, rows_path)
        if not rows:
            continue
        sub = schema.public_substitute(path)
        n = shown = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            n += 1
            value = row.get(sub[0]) if sub else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                row[column] = value
                row[f"{column}_source"] = schema.public_record_label(row.get(sub[1]), row.get(sub[2]))
                shown += 1
            else:
                row[column] = schema.MISSING
                if sub:
                    row[f"{column}_source"] = schema.NO_PUBLIC_RECORD_LABEL
        if n:
            deal["provenance"][path] = pv.internal_only(path)
            withheld.append(path)
            log.append(f"internal-only: {path} → licensed value withheld on {n} row(s); "
                       f"{shown} show the public record, {n - shown} MISSING with the label")
    return withheld


def mark_illustrative_pricing(deal: dict, note: str, log: list[str]) -> None:
    meta = deal["meta"]
    meta["illustrative_pricing"] = True
    meta["pricing_note"] = note
    ppu = deal["pricing"]["price_per_unit"]
    deal["summary"]["pricing_guidance"] = f"${ppu:,.0f} per unit (illustrative)"
    deal["provenance"]["summary.pricing_guidance"] = pv.read("Illustrative price", "—", note=note)
    deal["provenance"]["pricing.price_per_unit"] = pv.read(
        "Illustrative price", "—",
        note="Per-unit price chosen for the sample, inside the sale-price band; every cap rate and pricing ladder is computed from it.")
    log.append(f"pricing: ${ppu:,.0f}/unit marked illustrative")


def withhold_asset_rows(comps: dict, asset: re.Pattern) -> int:
    """Drop every comp/nearby row that names the real asset; return the count. A block that
    carries no rows (absent) is left untouched."""
    def names(row: dict) -> str:
        return " ".join(str(row.get(k) or "") for k in ("community", "address", "name"))
    removed = 0
    for block in (comps, comps.get("unrestricted")):
        if not isinstance(block, dict):
            continue
        for key in ("rows", "nearby"):
            if not isinstance(block.get(key), list):
                continue
            kept = [r for r in block[key] if not asset.search(names(r))]
            removed += len(block[key]) - len(kept)
            block[key] = kept
    return removed


def withhold_om_comps(deal: dict, reason: str) -> None:
    deal["om_comps"] = schema.empty_om_comps(reason)


def watchlist_hits(deal: dict, asset: re.Pattern) -> list[str]:
    """Every JSON path whose text still names the real asset. Any line = look."""
    hits: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and asset.search(node):
            hits.append(f"{path}: {node[:120]!r}")
    walk(deal, "")
    return hits


# ---- the one call ------------------------------------------------------------------------

def apply(deal: dict, mode: str, *, identity: Identity | None = None,
          schools: dict | None = None) -> tuple[dict, list[str]]:
    """Return ``(new deal, log)`` for ``mode``. Never mutates ``deal``."""
    if mode not in MODES:
        raise ValueError(f"policy mode must be one of {MODES}, got {mode!r}")
    out = copy.deepcopy(deal)
    log: list[str] = []
    if mode == "internal":
        log.append("internal: nothing withheld, nothing renamed")
        return out, log
    if identity is not None:
        om_doc = om_document(out)
        if not om_doc:
            raise ValueError("identity substitution needs a memorandum in meta.provenance")
        substitute_identity(out, identity, om_doc, log)
    withhold_internal_only(out, log, schools=schools)
    if identity is not None and identity.pricing_note:
        mark_illustrative_pricing(out, identity.pricing_note, log)
    if identity is not None:
        n = withhold_asset_rows(out["comps"], identity.asset)
        log.append(f"comps: {n} row(s) withheld as the real asset")
        withhold_om_comps(out, identity.om_comps_reason)
        log.append("om_comps: the memorandum's comparables withheld")
    return out, log


def apply_upload(deal: dict, mode: str) -> tuple[dict, list[str]]:
    """The visitor's upload run, in ``mode``. Never mutates ``deal``.

    ``internal`` is the identity, as in :func:`apply`. ``public`` withholds the registry's
    ROW columns only: an upload never produces a summary-level registry value (no web
    lookup, no assessor portal, no trade-out report), and the run already stamps each of
    those as an absence with its own reason, which stands. What an upload DOES carry from
    an internal-only source is the market report's rows — the tracker's community unit
    counts."""
    if mode not in MODES:
        raise ValueError(f"policy mode must be one of {MODES}, got {mode!r}")
    out = copy.deepcopy(deal)
    log: list[str] = []
    if mode == "internal":
        log.append("internal: nothing withheld, nothing renamed")
        return out, log
    withhold_internal_only_rows(out, log)
    return out, log
