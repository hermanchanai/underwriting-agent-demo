"""JSON -> self-contained HTML fragments, one per output tab.

Each fragment is a web preview of one workbook tab. Constraints (I5, I6):

* **Self-contained.** No external assets, no ``http(s)://`` anywhere, no script tags. The
  fragment is inlined into an Astro page with ``set:html``.
* **Token-styled only.** Colors and type come from the host site's CSS custom properties
  (``--fg``, ``--muted``, ``--rule``, ``--accent``, ``--mono``, …), so the demo survives a
  site reskin without being touched.
* **Every wide table scrolls in its own container**, never the page body.
* **Numbers come from the same JSON key the xlsx cell came from**, via ``compute.py`` for
  anything the workbook leaves to Excel. That equality is what
  ``test_render_previews.py`` asserts.
* An ``(illustrative)`` chip renders beside every pricing-derived figure whenever
  ``meta.illustrative_pricing`` is set.

Usage:
    python tools/render_previews.py --deal <deal.json> --out <dir>
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compute
import provenance as pv
import schema

# One style block, emitted once per fragment. Scoped by the .op- prefix so it cannot
# collide with the host page, and built entirely from the site's own tokens.
STYLE = """<style>
.op-wrap { font-family: var(--sans, sans-serif); color: var(--fg, #111); }
.op-wrap h3 {
  font-family: var(--serif, Georgia, serif); font-weight: 500;
  font-size: var(--t-22, 22px); letter-spacing: -0.01em;
  margin: 0 0 var(--s-2, 8px); color: var(--fg, #111);
}
.op-note {
  font-family: var(--serif, Georgia, serif); font-size: var(--t-15, 15px);
  line-height: 1.5; color: var(--muted, #666); margin: 0 0 var(--s-5, 24px);
  max-width: 62ch;
}
.op-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0 0 var(--s-5, 24px); }
.op-table { border-collapse: collapse; width: 100%; min-width: max-content; font-size: var(--t-13, 13px); }
.op-table th, .op-table td {
  padding: 6px 12px; text-align: left; white-space: nowrap;
  border-bottom: 1px solid var(--rule-soft, #e6e6e6);
}
.op-table thead th {
  font-family: var(--mono, monospace); font-size: var(--t-12, 12px);
  text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted, #666); font-weight: 500;
  border-bottom: 1px solid var(--rule, #ccc); background: var(--bg-alt, #f2f2f2);
  position: sticky; top: 0;
}
.op-table td.op-num, .op-table th.op-num { text-align: right; font-family: var(--mono, monospace); font-variant-numeric: tabular-nums; }
.op-table tr.op-total td { font-weight: 600; background: var(--bg-alt, #f2f2f2); border-bottom: 1px solid var(--rule, #ccc); }
.op-table tr.op-sub td { background: var(--bg-alt, #f6f6f6); font-weight: 600; }
.op-table td.op-wrapcell { white-space: normal; min-width: 26ch; max-width: 46ch; }
.op-card { display: grid; gap: 0; margin: 0 0 var(--s-5, 24px); }
.op-sec {
  font-family: var(--mono, monospace); font-size: var(--t-12, 12px);
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted, #666);
  padding: var(--s-4, 16px) 0 var(--s-2, 8px); border-bottom: 1px solid var(--rule, #ccc);
}
.op-row { display: grid; grid-template-columns: minmax(11rem, 15rem) 1fr; gap: var(--s-4, 16px);
  padding: 7px 0; border-bottom: 1px solid var(--rule-soft, #eee); align-items: baseline; }
.op-row dt { font-family: var(--mono, monospace); font-size: var(--t-12, 12px); color: var(--muted, #666); }
.op-row dd { margin: 0; font-size: var(--t-15, 15px); }
.op-row dd .op-prov { display: block; font-size: var(--t-12, 12px); color: var(--muted-2, #999); margin-top: 3px; }
.op-row dd .op-src { display: block; font-size: var(--t-11, 11px); color: var(--muted-2, #999); margin-top: 2px; opacity: .8; font-style: italic; }
td .op-src { display: block; font-size: var(--t-11, 11px); color: var(--muted-2, #999); margin-top: 2px; opacity: .8; font-style: italic; }
.op-chip {
  display: inline-block; font-family: var(--mono, monospace); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.07em; padding: 1px 6px;
  border: 1px solid var(--rule, #ccc); border-radius: 2px;
  color: var(--muted, #666); margin-left: 6px; vertical-align: 1px; white-space: nowrap;
}
.op-flag { color: var(--accent, #14304f); font-weight: 600; }
.op-ok { color: var(--muted, #666); }
.op-missing { font-family: var(--mono, monospace); font-size: var(--t-12, 12px); color: var(--accent, #14304f); }
.op-internal { font-family: var(--mono, monospace); font-size: var(--t-12, 12px); color: var(--muted, #666); border-bottom: 1px dotted var(--muted, #666); cursor: help; }
.op-bar { display: grid; grid-template-columns: 7rem 1fr 5.5rem; gap: 8px; align-items: center;
  margin: 0 0 5px; font-size: var(--t-12, 12px); }
.op-bar__label { font-family: var(--mono, monospace); color: var(--muted, #666); }
.op-bar__track { background: var(--bg-alt, #eee); height: 14px; position: relative;
  display: grid; grid-auto-rows: 1fr; }
.op-bar__track--multi { height: auto; background: transparent; gap: 1px; }
.op-bar__sub { background: var(--bg-alt, #eee); height: 8px; }
.op-bar__fill { background: var(--accent, #14304f); height: 100%; display: block; }
.op-bar__fill--alt { background: var(--muted-2, #999); }
.op-bar__fill--soft { background: var(--rule, #ccc); }
.op-bar__val { font-family: var(--mono, monospace); text-align: right; color: var(--fg, #111);
  font-variant-numeric: tabular-nums; }
.op-legend { display: flex; gap: var(--s-4, 16px); flex-wrap: wrap; margin: 0 0 var(--s-3, 12px);
  font-family: var(--mono, monospace); font-size: 10px; color: var(--muted, #666);
  text-transform: uppercase; letter-spacing: .07em; }
.op-legend i { display: inline-block; width: 10px; height: 10px; margin-right: 5px;
  vertical-align: -1px; }
.op-empty { border: 1px dashed var(--rule, #ccc); padding: var(--s-5, 24px);
  font-family: var(--serif, Georgia, serif); color: var(--muted, #666); max-width: 62ch; }
@media (max-width: 640px) {
  .op-row { grid-template-columns: 1fr; gap: 2px; }
}
</style>"""

class Safe(str):
    """Markup this module produced, and the only thing exempt from escaping.

    Trust is carried by the *type*, not by what the string looks like. An earlier version
    detected "already safe" markup by testing whether a value contained one of the
    renderer's marker substrings — which meant a source label that merely *included*
    ``<span class="op-ok">`` was handed through verbatim, taking whatever followed it with
    it, into a fragment the site inlines with ``set:html``.

    Concatenation of two Safe values stays Safe; mixing in a bare ``str`` is refused, so a
    source value cannot ride along on a trusted one.
    """

    __slots__ = ()

    def __add__(self, other: object) -> "Safe":
        if isinstance(other, Safe):
            return Safe(str.__add__(self, other))
        return NotImplemented

    def __radd__(self, other: object) -> "Safe":
        if isinstance(other, Safe):
            return Safe(str.__add__(str(other), self))
        return NotImplemented


SAFE_EMPTY = Safe("")


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def esc(value: Any) -> Safe:
    """Escape ``value`` and mark the result as safe markup."""
    return Safe(_escape(value))


def _render_cell(value: Any) -> str:
    """A cell is inserted verbatim only when the renderer built it."""
    return str(value) if isinstance(value, Safe) else _escape(value)


CHIP = Safe('<span class="op-chip">illustrative</span>')


class Fragment:
    """Accumulates one fragment's HTML."""

    def __init__(self, title: str, note: str = "") -> None:
        self.parts: list[str] = [STYLE, '<div class="op-wrap">', f"<h3>{esc(title)}</h3>"]
        if note:
            self.parts.append(f'<p class="op-note">{note}</p>')

    def raw(self, html_text: str) -> None:
        self.parts.append(html_text)

    def table(
        self,
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        numeric: Sequence[int] = (),
        wrap_cols: Sequence[int] = (),
        row_classes: Iterable[str | None] | None = None,
    ) -> None:
        num = set(numeric)
        wrapc = set(wrap_cols)
        out = ['<div class="op-scroll"><table class="op-table"><thead><tr>']
        for i, head in enumerate(headers):
            cls = ' class="op-num"' if i in num else ""
            out.append(f"<th{cls}>{esc(head)}</th>")
        out.append("</tr></thead><tbody>")
        classes = list(row_classes) if row_classes is not None else None
        for r_idx, row in enumerate(rows):
            rc = classes[r_idx] if classes and r_idx < len(classes) else None
            out.append(f'<tr class="{rc}">' if rc else "<tr>")
            for i, cell in enumerate(row):
                cls = []
                if i in num:
                    cls.append("op-num")
                if i in wrapc:
                    cls.append("op-wrapcell")
                attr = f' class="{" ".join(cls)}"' if cls else ""
                out.append(f"<td{attr}>{_render_cell(cell)}</td>")
            out.append("</tr>")
        out.append("</tbody></table></div>")
        self.parts.append("".join(out))

    def bars(
        self,
        rows: list[tuple[str, list[tuple[float, str]], str]],
        *,
        legend: list[tuple[str, str]] | None = None,
    ) -> None:
        """Horizontal bars. Each row: (label, [(value, css-modifier), ...], display)."""
        out: list[str] = []
        if legend:
            out.append('<div class="op-legend">')
            for name, mod in legend:
                out.append(
                    f'<span><i class="op-bar__fill {mod}"></i>{_escape(name)}</span>'
                )
            out.append("</div>")
        peak = max(
            (v for _lbl, series, _d in rows for v, _m in series if v is not None),
            default=0,
        ) or 1
        for label, series, display in rows:
            out.append('<div class="op-bar">')
            out.append(f'<span class="op-bar__label">{_escape(label)}</span>')
            # One sub-track per series. A single track with three full-height fills
            # stacked inside it overflows by twice its own height and lands on the rows
            # below; the grid gives each series its own row and sizes the track to fit.
            multi = " op-bar__track--multi" if len(series) > 1 else ""
            out.append(f'<span class="op-bar__track{multi}">')
            for value, mod in series:
                pct = 0 if not value else max(0.0, min(100.0, 100.0 * value / peak))
                out.append(
                    f'<span class="op-bar__sub">'
                    f'<span class="op-bar__fill {mod}" style="width:{pct:.1f}%"></span>'
                    f"</span>"
                )
            out.append("</span>")
            out.append(f'<span class="op-bar__val">{_escape(display)}</span>')
            out.append("</div>")
        self.parts.append("".join(out))

    def card(self, sections: Iterable[tuple[str, Iterable[tuple]]]) -> None:
        """Label / value / note, and optionally the SOURCE, rendered beside the value.

        A four-element row carries provenance inline. That is the whole point of the
        change: a reader deciding whether to trust a number is looking at the number, and
        a source they have to open another tab for is a source most readers never see.
        """
        out = ['<dl class="op-card">']
        for name, rows in sections:
            out.append(f'<div class="op-sec">{esc(name)}</div>')
            for entry in rows:
                label, value, provenance = entry[0], entry[1], entry[2]
                source = entry[3] if len(entry) > 3 else ""
                prov = (
                    f'<span class="op-prov">{_render_cell(provenance)}</span>'
                    if provenance
                    else ""
                )
                src = (
                    f'<span class="op-src">{_render_cell(source)}</span>'
                    if source
                    else ""
                )
                out.append(
                    f'<div class="op-row"><dt>{_escape(label)}</dt>'
                    f"<dd>{_render_cell(value)}{prov}{src}</dd></div>"
                )
        out.append("</dl>")
        self.parts.append("".join(out))

    def render(self) -> str:
        return "\n".join([*self.parts, "</div>"]) + "\n"


def _missing(text: str = schema.MISSING) -> Safe:
    return Safe(f'<span class="op-missing">{_escape(text)}</span>')


INTERNAL_MARKER = "Available in the internal underwriting agent only"


def _internal_marker(reason: str) -> Safe:
    """The label for a value the public demo leaves to the internal pipeline: the reason is
    the hover text, so the cell says what the demo does not do instead of reading as a hole."""
    return Safe(f'<span class="op-internal" title="{_escape(reason)}">{INTERNAL_MARKER}</span>')


def _internal_or(path: str, value, rendered):
    """``rendered`` — unless the value is MISSING and ``schema.INTERNAL_ONLY`` says the demo
    does not produce it, in which case the internal-only marker with its reason."""
    if value in (None, "", schema.MISSING):
        found = schema.internal_only(path)
        if found:
            return _internal_marker(found[1])
    return rendered


def _from_om(deal: dict, path: str) -> bool:
    """Whether the value at ``path`` was read off an uploaded offering memorandum.

    A parcel, tax district, assessed value or bill that came from the memorandum is the
    SELLER'S statement, and every surface that shows it says so; a county record keeps its
    own label. Decided from the stamp's source document against the documents the run
    read as an OM, never from the value.
    """
    om_docs = {d.get("document") for d in (deal.get("meta", {}).get("provenance") or [])
               if d.get("kind") == "om"}
    st = pv.lookup(deal, path)
    return bool(st) and st.get("source_doc") in om_docs


OM_BILL_LABEL = "Tax bill as stated in the offering memorandum"
OM_ROW_NOTE = "As stated in the offering memorandum, not a county record."


def _money(value: Any) -> Safe:
    if isinstance(value, str):
        return _missing(value)
    return esc(compute.fmt_currency(value))


def _pct(value: Any, places: int = 1) -> Safe:
    if isinstance(value, str):
        return _missing(value)
    return esc(compute.fmt_pct(value, places=places))


# --------------------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------------------


def _tax_check(deal: dict) -> Safe:
    """Renders compute.tax_check — one implementation, so preview and workbook agree."""
    st = compute.tax_check(deal)
    if st["status"] == "NOT CHECKED":
        return _missing("NOT CHECKED")
    css = "op-ok" if st["status"] == "ties" else "op-flag"
    return Safe(
        f'<span class="{css}">{_escape(compute.fmt_currency(st["difference"]))} '
        f'({st["pct"]:.2%}) — {st["status"]}</span>'
    )


def _closing_rows(deal: dict) -> list[tuple[str, Safe, str]]:
    cc = deal.get("closing_costs") or {}
    rows: list[tuple[str, Safe, str]] = []
    import closing_costs as _closing

    for key, label in (("transfer_tax", "Transfer tax"), ("mortgage_tax", "Mortgage / recording tax")):
        block = cc.get(key) or {}
        status = block.get("amount_status")
        if status == _closing.ASSERTED:
            value = _money(block.get("amount"))
        elif status == _closing.ZERO_BY_LAW:
            value = Safe(f'<span class="op-ok">{_escape(compute.fmt_currency(0))}</span>')
        else:
            value = _missing("NOT ASSERTED")
        note = block.get("rate_label", "")
        cite = block.get("citation", "")
        caveat = block.get("caveat") or ""
        detail = " · ".join(x for x in (note, cite, caveat) if x)
        rows.append((label, value, detail))
    if not rows:
        rows.append(("Closing costs", _missing("NOT ASSERTED"), ""))
    return rows


#: The three documents this pipeline reads, and what to call them to a reader.
SOURCE_DOCUMENTS: tuple[tuple[str, str, str], ...] = (
    ("om", "Offering memorandum", "summary"),
    ("t12", "Trailing-twelve statement", "t12"),
    ("rent_roll", "Rent roll", "rent_roll"),
)


def provided_documents(deal: dict) -> list[tuple[str, str]]:
    """(label, what happened) for each source document, provided or not.

    Absence was visible on the T-12 tabs and in the manifest and nowhere else, so a reader
    who opened the Deal summary first saw a card with holes in it and no explanation. The
    holes and the reason for them belong on the same page.
    """
    stated: dict[str, str] = {}
    for entry in (deal.get("meta", {}).get("provenance") or []):
        kind = entry.get("kind")
        if kind and kind not in stated:
            stated[kind] = str(entry.get("document") or "")
    out: list[tuple[str, str]] = []
    for kind, label, section in SOURCE_DOCUMENTS:
        reason = schema.section_absent(deal, section) if section != "summary" else None
        if kind in stated and not reason:
            out.append((label, f"Provided — read from {stated[kind]}."))
        elif kind in stated:
            out.append((label, f"Provided as {stated[kind]}, but {reason[0].lower()}{reason[1:]}"))
        else:
            out.append((label, reason or "Not provided."))
    return out


def missing_documents(deal: dict) -> list[str]:
    """Labels of the source documents this deal does NOT have.

    The render condition used to ask only whether the two SPREADSHEET sections were
    absent, so a T-12 plus a rent roll with no offering memorandum showed a card of
    MISSING identification and property fields with nothing beside them saying why. The
    OM is where those fields come from; its absence is the explanation.
    """
    stated = {
        entry.get("kind")
        for entry in (deal.get("meta", {}).get("provenance") or [])
        if entry.get("kind")
    }
    out = []
    for kind, label, section in SOURCE_DOCUMENTS:
        reason = schema.section_absent(deal, section) if section != "summary" else None
        if kind not in stated or reason:
            out.append(label)
    return out


def documents_block(deal: dict) -> Safe:
    """The provided/not-provided list, rendered as its own small card section."""
    rows = "".join(
        f"<tr><th scope=\"row\">{_escape(label)}</th><td>{_escape(state)}</td></tr>"
        for label, state in provided_documents(deal)
    )
    return Safe(
        '<div class="op-card"><h4>Documents read for this run</h4>'
        f'<table class="op-kv">{rows}</table>'
        "<p class=\"op-note\">A value can only be as good as the document it came from. "
        "Anything a missing document would have supplied reads MISSING on this card "
        "rather than being estimated.</p></div>"
    )


def summary_fragment(deal: dict) -> str:
    s = deal["summary"]
    pr = compute.pricing(deal)
    tu = compute.tax_underwriting(deal)
    chip = CHIP if deal["meta"].get("illustrative_pricing") else SAFE_EMPTY

    f = Fragment(
        "Deal summary",
        "One card, built from the deal's own documents. Where a value could not be "
        "established it reads <span class=\"op-missing\">MISSING</span> rather than a "
        "plausible guess — a visible hole is cheaper than a wrong number that survives "
        "into a model.",
    )
    # Only when something is missing — ALL three documents, not just the two spreadsheet
    # sections. A set with all three has nothing to explain, and adding a block there
    # would change the published showcase output for no reader benefit.
    if missing_documents(deal):
        f.raw(str(documents_block(deal)))

    def txt(key: str) -> str:
        return esc(s.get(key, schema.MISSING))

    def src(key: str) -> str:
        """The source for one summary value, rendered beside it rather than in a trail."""
        return pv.compact_for(deal, f"summary.{key}")

    sections: list[tuple[str, list[tuple]]] = [
        (
            "Identification",
            [
                ("Deal name", txt("deal_name"), "", src("deal_name")),
                (
                    "Address",
                    esc(f"{s['street_address']}, {s['city']}, {s['state']} {s['zip']}"),
                    s.get("address_provenance", ""),
                    src("street_address"),
                ),
                ("County", txt("county"), "", src("county")),
                ("Market", txt("msa"), "", src("msa")),
            ],
        ),
        (
            "Property",
            [
                ("Product type", txt("product_type"), ""),
                ("Structure", txt("structure"), ""),
                ("Units", esc(compute.fmt_number(s["units_total"])), ""),
                ("Year built", txt("year_built"), ""),
                ("Detail", txt("product_detail"), ""),
            ],
        ),
        (
            "Parties",
            [
                ("Seller", txt("seller_entity"), ""),
                ("Listing brokerage", txt("listing_brokerage"), ""),
            ],
        ),
        (
            "Pricing",
            [
                ("Guidance", txt("pricing_guidance") + chip, deal["meta"].get("pricing_note", "")),
                ("Total price", _money(pr["total_price"]) + chip, ""),
                (
                    "T-12 NOI",
                    _money(pr["t12_noi"]),
                    "From the T-12 rollup."
                    if not schema.section_absent(deal, "t12")
                    else "No trailing-twelve statement was provided, so there is no rollup.",
                ),
                ("T-12 cap rate", _pct(pr["t12_cap_rate"], 2) + chip, ""),
                (
                    "T-3 cap rate",
                    _pct(pr["t3_cap_rate"], 2) + chip,
                    "Trailing three months annualized.",
                ),
            ],
        ),
        (
            "Rents",
            [
                ("In-place (avg)", _money(s["in_place_rent_avg"]), ""),
                (
                    "Market rent for model",
                    _money(s["market_rent_for_model"]),
                    "New move-in rent (residents who moved in inside the window, by "
                    "move-in date) where a plan has one, otherwise in-place — and in-place "
                    "outright when the roll has no usable lease start or move-in date. "
                    "Never a blend of new and renewal.",
                ),
            ],
        ),
        (
            "Taxes",
            [
                (
                    "Year-one tax (underwriting assumption)",
                    _money(tu["assumed_tax"]),
                    "The figure to carry into the model: " + str(tu.get("approach") or ""),
                    "computed",
                ),
                ("Year-one tax per unit", _money(tu["assumed_tax_per_unit"]), "", "computed"),
                (
                    f"{OM_BILL_LABEL} ({esc(s.get('tax_billed_year'))})"
                    if _from_om(deal, "summary.tax_billed_amount")
                    else (f"Billed by the county ({esc(s.get('tax_billed_year'))})"
                          if s.get("tax_billed_year") not in (None, "", schema.MISSING)
                          else "Billed by the county"),
                    _internal_or("summary.tax_billed_amount", s.get("tax_billed_amount"),
                                 _money(s.get("tax_billed_amount"))),
                    "The seller's figure, not verified against the county."
                    if _from_om(deal, "summary.tax_billed_amount") else "",
                    src("tax_billed_amount"),
                ),
            ],
        ),
        (
            "Closing costs at the illustrative price",
            _closing_rows(deal),
        ),
        (
            "Schools (internal underwriting agent only)",
            [
                (label,
                 _internal_or(f"summary.school_{key}_name", s[f"school_{key}_name"], esc(s[f"school_{key}_name"])),
                 (f"GreatSchools rating {s[f'school_{key}_rating']}/10"
                  if s.get(f"school_{key}_rating") not in (None, "", schema.MISSING) else ""))
                for label, key in (("Elementary", "elementary"), ("Middle", "middle"), ("High", "high"))
            ] + [
                ("Source",
                 esc("Attendance-zone schools and ratings are looked up by the internal underwriting "
                     "agent. The public demo does not run this on an uploaded deal."), ""),
            ],
        ),
    ]
    f.card(sections)
    return f.render()


def _absence_block(reason: str | None, title: str) -> Safe:
    return Safe(
        f'<div class="op-empty"><strong>{_escape(title)}</strong><br>'
        f'{_escape(reason or "No reason recorded.")}<br><br>'
        "An empty block that says why it is empty is a deliverable. A block of "
        "plausible numbers is not.</div>"
    )


#: Tabs whose whole content comes from one section. When that section declares itself
#: absent there is nothing to tabulate, and the honest tab is the reason — not an empty
#: table, which reads as a property with no income or no units.
ABSENT_TAB_SECTION: dict[str, tuple[str, str]] = {
    "t12_data": ("t12", "No trailing-twelve data"),
    "t12_pnl": ("t12", "No profit-and-loss rollup"),
    "t12_checks": ("t12", "No cross-tie or pricing ladder"),
    "rent_roll": ("rent_roll", "No rent roll"),
    "unit_mix": ("rent_roll", "No unit mix"),
    "rent_roll_checks": ("rent_roll", "No rent-roll cross-check"),
}


def _absent_fragment(deal: dict, key: str) -> str | None:
    """The absence tab for a section that declared itself empty, or None to render."""
    section, title = ABSENT_TAB_SECTION[key]
    reason = schema.section_absent(deal, section)
    if not reason:
        return None
    f = Fragment(
        title,
        "This tab is built entirely from a document that was not part of this run.",
    )
    f.raw(str(_absence_block(reason, title)))
    return f.render()


def _n_or_missing(value, fmt) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return fmt(value)
    return _missing()


def _om_comps_sections(f: "Fragment", deal: dict) -> None:
    """The comparables the memorandum itself states — rent, then institutional sales.

    Optional block: the published sample has none and shows nothing here. Retail for-sale
    comps are out of scope by decision; the notice says so on the tab.
    """
    block = deal.get("om_comps")
    if not isinstance(block, dict):
        return
    f.raw("<h3>Stated in the offering memorandum</h3>")
    if block.get("absent_reason"):
        f.raw(str(_absence_block(block["absent_reason"], "No comparables stated")))
    rent = block.get("rent") or []
    if rent:
        f.raw('<p class="op-note">Rent comparables as the memorandum states them — the broker\'s '
              'figures, verified only against the memorandum\'s own words, not against the '
              'operator\'s scrape above.</p>')
        f.table(
            ["Community", "Miles", "Units", "Built", "Rent", "Basis", "Page"],
            [
                [
                    esc(r.get("community")),
                    _n_or_missing(r.get("distance_mi"), lambda v: compute.fmt_number(v, places=1)),
                    _n_or_missing(r.get("units"), compute.fmt_number),
                    _n_or_missing(r.get("year_built"), lambda v: str(int(v))),
                    _n_or_missing(r.get("rent"), compute.fmt_currency),
                    esc(r.get("rent_basis")),
                    esc(r.get("page") if r.get("page") is not None else "—"),
                ]
                for r in rent
            ],
            numeric=(1, 2, 3, 4),
            wrap_cols=(5,),
        )
    sale = block.get("sale") or []
    if sale:
        f.raw("<h3>Sale comparables stated in the offering memorandum (institutional trades)</h3>")
        f.table(
            ["Property", "Units", "Built", "Sold", "Price", "Per unit", "Cap rate", "Buyer", "Seller", "Page"],
            [
                [
                    esc(r.get("property")),
                    _n_or_missing(r.get("units"), compute.fmt_number),
                    _n_or_missing(r.get("year_built"), lambda v: str(int(v))),
                    esc(r.get("sale_date")),
                    _n_or_missing(r.get("price"), compute.fmt_currency),
                    _n_or_missing(r.get("price_per_unit"), compute.fmt_currency),
                    _n_or_missing(r.get("cap_rate"), lambda v: compute.fmt_pct(v)),
                    esc(r.get("buyer")),
                    esc(r.get("seller")),
                    esc(r.get("page") if r.get("page") is not None else "—"),
                ]
                for r in sale
            ],
            numeric=(1, 2, 4, 5, 6),
            wrap_cols=(7, 8),
        )
    f.raw(f'<p class="op-note">{_escape(str(block.get("retail_note") or ""))}</p>')


def comps_fragment(deal: dict) -> str:
    c = compute.comps(deal)
    f = Fragment(
        "Rent comps",
        "Nearby communities, from an operator's own weekly rent scrape. Asking and net "
        "effective are stated separately on every row: a concession-blind comp set is the "
        "most common way a rent assumption goes wrong. No licensed comp feed is used. The "
        "two detail tiers are shown here; every other tracked community inside the radius, "
        "with the reason it is not a comp, is carried in the downloadable interchange file.",
    )
    if not c["rows"]:
        note = deal["comps"].get("coverage_note")
        if note:
            # The measured evidence matters MOST when the block is empty: it is the
            # difference between "we looked and there is nothing" and "we did not look".
            f.raw(f'<p class="op-note"><strong>Coverage.</strong> {_escape(note)}</p>')
        f.raw(str(_absence_block(deal["comps"].get("absent_reason"), "No comparable rent data")))
        _om_comps_sections(f, deal)
        return f.render()

    # ---- the three tiers -------------------------------------------------------------
    # An undifferentiated list of everything nearby invites exactly the averaging this
    # output refuses: across product types, across vintages, across the property that is
    # actually a student lease-up. So the display separates "these five are the market"
    # from "these are also in the radius, and here is why they are not comps".
    tiering = c.get("tiering") or {}
    for tier in (1, 2):
        group = (c.get("tiers") or {}).get(tier)
        if not group or not group["rows"]:
            continue
        n = len(group["communities"])
        f.raw(
            f"<h3>Tier {tier} — {_escape(group['label'])}</h3>"
            f'<p class="op-note">The {"closest" if tier == 1 else "next"} {n} '
            f'{"community" if n == 1 else "communities"} matching the subject\'s product '
            f"and vintage, in full detail.</p>"
        )
        if tier == 1 and tiering.get("vintage_footnote"):
            f.raw(f'<p class="op-note">{_escape(str(tiering["vintage_footnote"]))}</p>')
        f.table(
            [
                "Community", "Miles", "Built", "Plan", "Bd", "Ba", "SF", "Asking",
                "Concession", "Net effective", "Asking $/SF", "Occupancy", "As of",
            ],
            [
                [
                    row["community"],
                    compute.fmt_number(row["distance_mi"], places=1),
                    # A dash, never a guess: the tracker has no year-built column, so an
                    # invented year here would be the filter's own evidence, fabricated.
                    compute.fmt_year(row.get("year_built")),
                    row["floor_plan"],
                    compute.fmt_number(row["beds"], places=0),
                    compute.fmt_number(row["baths"], places=1),
                    compute.fmt_number(row["sqft"]),
                    compute.fmt_currency(row["asking_rent"]),
                    compute.fmt_currency(row["concession"]),
                    compute.fmt_currency(row["net_effective_rent"]),
                    compute.fmt_currency(row["asking_psf"], cents=True),
                    compute.fmt_pct(row["occupancy"]),
                    row["as_of"],
                ]
                for row in group["rows"]
            ],
            numeric=(1, 2, 4, 5, 6, 7, 8, 9, 10, 11),
        )

    # ---- unit count and year built, each with its source -----------
    # A community's own licensed count never reaches a public page; its public-record count
    # does, naming the record and its as-of. The policy decided which is on the row; this
    # table prints it and says where it came from, one line per detail community.
    facts = compute.community_facts(deal)
    if facts:
        f.raw(
            "<h3>Unit count and year built, by community</h3>"
            '<p class="op-note">Each figure names where it came from. A unit count is shown '
            "only where a public record states one (a county property record, a state "
            "apartment licence, a city permit), with that record's as-of date; a community's "
            "count in the licensed registry is never shown. Year built is the public record's "
            "where it has one, the community registry's otherwise.</p>"
        )
        f.table(
            ["Community", "Tier", "Units", "Units from", "Built", "Built from"],
            [
                [
                    r["community"],
                    compute.fmt_number(r["tier"]),
                    _n_or_missing(r["units"], compute.fmt_number) if r["units"] is not None else "—",
                    r["units_source"],
                    compute.fmt_year(r["year_built"]),
                    r["year_built_source"],
                ]
                for r in facts
            ],
            numeric=(1, 2, 4),
            wrap_cols=(3, 5),
        )

    # The identity-only third tier came off the page. On this sample it
    # was ~340 rows — longer than every other tab put together, and nobody read it. The
    # DATA is untouched: every tracked community in the radius, with the reason it is not
    # a comp, still ships in the downloadable interchange file, which is where a reader who
    # wants to audit the comp set should be looking anyway.

    # ---- the two comp sets, side by side ---------------------------------------------
    # The Notes section points here, so both have to be visible and labelled.
    k = c.get("vintage_contrast") or {}
    if k.get("spread_matched") is not None or k.get("withheld_reason"):
        f.raw(
            "<h3>Two comp sets, selected identically except for the vintage test</h3>"
            f'<p class="op-note">Held fixed in both: '
            f"{_escape(', '.join(c.get('unrestricted_held_fixed') or []))}. Varied: the "
            f"+/-{(c.get('tiering') or {}).get('year_band')}-year vintage test only.</p>"
        )
        if k.get("withheld_reason"):
            f.raw(f'<p class="op-note">{_escape(k["withheld_reason"])}</p>')
        else:
            ys = [r.get("year_built") for r in c["rows"] if r.get("year_built")]
            f.table(
                ["Comp set", "Communities", "Vintages", "Comp net effective",
                 "Subject reads"],
                [
                    ["A — vintage test ON",
                     compute.fmt_number(len(c["detailed_communities"])),
                     f"{min(ys)}–{max(ys)}" if ys else "—",
                     compute.fmt_currency(k["matched"]),
                     compute.fmt_pct(k["spread_matched"])],
                    ["B — vintage test OFF",
                     compute.fmt_number(c.get("unrestricted_communities")),
                     f"{c.get('unrestricted_year_min')}–{c.get('unrestricted_year_max')}",
                     compute.fmt_currency(k["unrestricted"]),
                     compute.fmt_pct(k["spread_unrestricted"])],
                    ["Difference between the two sets", "", "",
                     compute.fmt_currency(k["unrestricted"] - k["matched"]),
                     compute.fmt_pct(k["spread_matched"] - k["spread_unrestricted"])],
                ],
                numeric=(1, 3, 4),
                row_classes=[None, None, "op-total"],
            )
            swapped_in = c.get("unrestricted_swapped_in") or []
            swapped_out = c.get("unrestricted_swapped_out") or []
            if swapped_in or swapped_out:
                f.raw(
                    f'<p class="op-note"><strong>In B but not A.</strong> '
                    f"{_escape(', '.join(swapped_in) or '—')}<br>"
                    f"<strong>In A but not B.</strong> "
                    f"{_escape(', '.join(swapped_out) or '—')}</p>"
                )
            f.raw(
                '<p class="op-note">Both figures are weighted by this property\'s own '
                "unit mix and computed over the same bed counts, so the two rows describe "
                "the same product. This is what these two sets measured on this date; it "
                "is not an estimate of what vintage is worth in this market.</p>"
            )

    note = deal["comps"].get("coverage_note")
    if note:
        f.raw(f'<p class="op-note"><strong>Coverage.</strong> {_escape(note)}</p>')
    if c.get("by_bed"):
        f.raw("<h3>By bed count — like for like</h3>")
        f.table(
            ["Beds", "Communities", "Plans", "Avg asking", "Avg net effective"],
            [
                [
                    "Studio" if e["beds"] == 0 else f"{e['beds']} bed",
                    compute.fmt_number(e["communities"]),
                    compute.fmt_number(e["rows"]),
                    compute.fmt_currency(e["avg_asking"]),
                    compute.fmt_currency(e["avg_net_effective"]),
                ]
                for e in c["by_bed"]
            ],
            numeric=(1, 2, 3, 4),
        )

    f.card(
        [
            (
                "Subject versus comp set",
                [
                    ("Subject market rent", _money(c["subject_market_rent"]), ""),
                    (
                        "Comp-set average asking",
                        _money(c["avg_asking"]),
                        f"Over {c.get('basis_label')} — never the whole radius.",
                    ),
                    ("Comp-set average net effective", _money(c["avg_net_effective"]), ""),
                    (
                        "Comp set, mix-matched",
                        _money(c.get("compset_mix_matched_net_effective")),
                        "Each bed count's comp net-effective weighted by the SUBJECT's own "
                        "unit mix, so the comparison is like for like.",
                    ),
                    (
                        "Subject rent, same bed counts",
                        _money(c.get("subject_market_rent_matched")),
                        "The subject's own rent restricted to the bed counts the comp set "
                        "covers, so both sides describe the same product.",
                    ),
                    (
                        "Mix coverage",
                        _pct(c.get("mix_coverage"), 0),
                        f"Share of the subject's units matched by bed count "
                        f"(beds {c.get('mix_matched_beds')}).",
                    ),
                    (
                        "Subject vs comp set (mix-matched)",
                        _pct(c["subject_vs_compset_spread"])
                        if c.get("subject_vs_compset_spread") is not None
                        else _missing("WITHHELD"),
                        c.get("spread_withheld_reason")
                        or "Negative means the subject is underwritten BELOW the market — "
                        "mark-to-market room rather than a stretch.",
                    ),
                    (
                        "Subject vs flat average",
                        _pct(c.get("subject_vs_compset_spread_unmatched")),
                        "Shown for contrast only: a flat average compares the subject "
                        "against whatever bed mix the market happened to list.",
                    ),
                    ("Source", esc(c["source"]), f"scraped {c['as_of']}"),
                ],
            )
        ]
    )
    _om_comps_sections(f, deal)
    return f.render()


_DEMAND_FMT_FN = {
    "count": lambda v: compute.fmt_number(v),
    "currency0": lambda v: compute.fmt_currency(v),
    "pct1": lambda v: compute.fmt_pct(v),
    "pct2": lambda v: compute.fmt_pct(v, places=2),
    "header": lambda v: "",
}


def demand_fragment(deal: dict) -> str:
    block = deal["demand"]
    sections = block.get("sections") or []
    f = Fragment(
        "Demand",
        block.get("engine_note")
        or "Demographics by drive-ring, from public sources only.",
    )
    if not sections and not compute.demand(deal):
        f.raw(str(_absence_block(block.get("absent_reason"), "No demographic data")))
        return f.render()

    caveat = block.get("ring_1mi_caveat")
    if caveat:
        # 🔴 Above the data, not below it. This is a known overcount in the engine, and a
        # reader who scrolls past it will take the 1-mile levels at face value.
        f.raw(
            '<p class="op-note"><strong>Read this before the 1-mile column.</strong> '
            + _escape(caveat)
            + "</p>"
        )

    if not sections:
        rows, classes = [], []
        for ring in compute.demand(deal):
            rows.append([
                ring["ring"],
                compute.fmt_number(ring["population"]),
                compute.fmt_number(ring["households"]),
                compute.fmt_currency(ring["median_income"]),
                compute.fmt_number(ring["jobs"]),
                compute.fmt_pct(ring["pop_growth_5yr"]),
                compute.fmt_pct(ring["renter_share"]),
            ])
            classes.append("op-total" if ring["ring"] == "MSA" else None)
        f.table(
            ["Ring", "Population", "Households", "Median income", "Jobs",
             "5-yr growth", "Renter share"],
            rows, numeric=(1, 2, 3, 4, 5, 6), row_classes=classes,
        )
        return f.render()

    cols = block.get("columns") or []
    for sec in sections:
        f.raw(f"<h3>{_escape(sec['title'])}</h3>")
        if sec.get("note"):
            f.raw(f'<p class="op-note">{_escape(sec["note"])}</p>')
        is_zip = sec.get("kind") == "zip"
        headers = ["", *(["Value"] if is_zip else cols), "Note"]
        rows, classes = [], []
        for r in sec["rows"]:
            if r.get("kind") == "subheader":
                rows.append([r["label"]] + [""] * (len(headers) - 1))
                classes.append("op-sub")
                continue
            fmt = _DEMAND_FMT_FN.get(r["format"], lambda v: str(v))
            indent = "\u00a0" * (4 * int(r.get("indent") or 0))
            vals = [fmt(v) if v is not None else "—" for v in r["values"]]
            pad = (1 if is_zip else len(cols)) - len(vals)
            rows.append([indent + r["label"], *vals, *([""] * max(0, pad)),
                         r.get("note") or ""])
            classes.append(None)
        f.table(
            headers, rows,
            numeric=tuple(range(1, len(headers) - 1)),
            row_classes=classes,
            wrap_cols=(len(headers) - 1,),
        )
        credit = schema.home_values_credit(deal) if is_zip else None
        if credit:
            f.raw(f'<p class="op-note">{_escape(credit)}</p>')
        hist = sec.get("metro_history")
        if hist:
            f.raw(
                f"<h4>Metro nonfarm employment and unemployment — "
                f"{_escape(str(sec.get('metro_label') or 'metro'))}</h4>"
            )
            f.table(
                ["Year", "Nonfarm jobs (000s)", "Job growth", "Unemployment"],
                [
                    [
                        str(h["year"]),
                        compute.fmt_number(h["nonfarm_000s"], places=1),
                        compute.fmt_pct(h["yoy"]),
                        compute.fmt_pct(h["unemployment"]),
                    ]
                    for h in hist
                ],
                numeric=(1, 2, 3),
            )
    return f.render()


#: Mirrors write_deal_summary.SUPPLY_COVERAGE_NOTE. One sentence, three surfaces.
SUPPLY_COVERAGE_NOTE = (
    "These rings and stages are real and unmodified, and a zero is a genuine zero for the "
    "sources covered so far — not a gap left blank. Coverage of the permit and "
    "entitlement pipeline is an area of ongoing work and will be expanded, so read this "
    "section as a floor on competing supply rather than a census of it."
)


def property_tax_fragment(deal: dict) -> str:
    """Property tax on its own, because it is the line most often got wrong."""
    import property_tax_table as ptt

    tu = compute.tax_underwriting(deal)
    chk = compute.tax_check(deal)
    s = deal["summary"]
    def from_om(path: str) -> bool:
        return _from_om(deal, path)

    OM_NOTE = OM_ROW_NOTE
    bill_from_om = from_om("summary.tax_billed_amount")
    year = s.get("tax_billed_year")
    year_text = str(year) if year not in (None, "", schema.MISSING) else "a year the source did not state"
    f = Fragment(
        "Property tax",
        "Rates as actually billed, not statutory or effective. The current year is "
        + ("checked against the bill the offering memorandum states — the seller's figure, "
           "not the county's" if bill_from_om else "checked against the county's own bill")
        + "; the assumption block is what a buyer should carry at the illustrative price.",
    )
    # The rate's UNIT comes from the divisor — per $100 in Texas or Arizona, mills per
    # $1,000 in Georgia or Florida — never assumed. And how the combined rate is built is
    # a state fact: Arizona bills a primary plus a secondary; everywhere else it is the sum
    # of the overlapping jurisdictions' adopted rates.
    unit = ptt.rate_unit(tu["divisor"])
    rule = ptt.STATE_RULES.get(tu["state"])

    def money(value):
        # A figure this block could not compute is a visible hole, not a dash: on the
        # upload path the basis, the bill and (in Arizona, or with no rate) the year-one
        # estimate are None, and "—" reads as "not applicable" where "MISSING" is the truth.
        return _money(schema.MISSING if value is None else value)

    rate_note = rule.rate_note if rule is not None else (
        "Sum of the overlapping jurisdictions' adopted rates, as billed."
    )
    rate_rows = [
        ("Parcel",
         _internal_or("summary.tax_parcel_id", s.get("tax_parcel_id"), esc(str(s.get("tax_parcel_id") or "—"))),
         OM_NOTE if from_om("summary.tax_parcel_id") else "",
         pv.compact_for(deal, "summary.tax_parcel_id")),
        ("Tax area code",
         _internal_or("summary.tax_area_code", s.get("tax_area_code"), esc(str(s.get("tax_area_code") or "—"))),
         OM_NOTE if from_om("summary.tax_area_code") else "",
         pv.compact_for(deal, "summary.tax_area_code")),
        ("Combined rate",
         Safe(f"{_missing()} {_escape(unit)}") if tu["rate"] is None
         else esc(compute.fmt_number(tu["rate"], places=4) + " " + unit),
         rate_note,
         pv.compact_for(deal, "summary.tax_rate_as_billed")),
        ("Assessment ratio", _pct(tu["ratio"], 0),
         "Legal class for a rental, not the owner-occupied rate.",
         pv.compact_for(deal, "summary.assessment_ratio")),
    ]
    evidence = s.get("tax_rate_evidence")
    if isinstance(evidence, str) and evidence and evidence != schema.MISSING:
        rate_rows.append(("Rate evidence", esc(evidence), "",
                          pv.compact_for(deal, "summary.tax_rate_evidence")))
    current_rows = [
        ("Assessed-value basis",
         _internal_or("summary.assessed_value_basis", tu["current_basis"], money(tu["current_basis"])),
         str(s.get("tax_basis_label") or "")
         + (" " + OM_NOTE if from_om("summary.assessed_value_basis") else ""),
         pv.compact_for(deal, "summary.assessed_value_basis")),
        ("Assessed value", money(tu["current_assessed"]),
         "Basis times the legal-class ratio.", ""),
        ("Computed annual tax", money(tu["current_computed"]), "", "computed"),
        (OM_BILL_LABEL if bill_from_om else "County billed amount",
         _internal_or("summary.tax_billed_amount", tu["current_billed"], money(tu["current_billed"])),
         (f"For {year_text}, as the memorandum states it — the seller's figure, "
          f"not verified against the county.") if bill_from_om
         else f"As billed for {year_text}.",
         pv.compact_for(deal, "summary.tax_billed_amount")),
        ("Computed less billed",
         esc(compute.fmt_currency(tu["current_computed"] - tu["current_billed"], cents=True))
         if (tu["current_computed"] is not None and tu["current_billed"] is not None
             and not chk.get("reason"))
         else _missing("NOT CHECKED"),
         f"Status: {chk['status']}." + (f" Not compared: {chk['reason']}." if chk.get("reason") else ""), ""),
    ]
    # Upload-path rows: present only when the key is carried (the published sets do not).
    if s.get("tax_statement_amount") is not None:
        current_rows.append((
            "Property taxes per the statement (T-12)", _money(s["tax_statement_amount"]),
            "As billed in the uploaded statement over its twelve months — the operator's "
            "ledger, not the county's bill, which the demo does not fetch.",
            pv.compact_for(deal, "summary.tax_statement_amount"),
        ))
    if s.get("tax_per_unit_flag") is not None:
        current_rows.append((
            "Per-unit sanity band", esc(str(s["tax_per_unit_flag"])),
            "A check against the state's typical range. It flags; it never changes a figure.",
            pv.compact_for(deal, "summary.tax_per_unit_flag"),
        ))
    f.card([
        ("The rate, as billed", rate_rows),
        ("Current year", current_rows),
        ("Underwriting assumption at the illustrative price", [
            ("Does the basis reset on sale?",
             esc({True: "Yes", False: "No", None: "not vendored"}[tu["resets_on_sale"]]),
             tu["approach"], ""),
            ("Year-one full value", money(tu["assumed_full_value"]),
             tu["assumed_full_value_label"], ""),
            ("Year-one assessed value", money(tu["assumed_assessed"]),
             tu["assumed_assessed_label"], ""),
            ("Year-one tax", money(tu["assumed_tax"]),
             "The figure to carry into the model.", "computed"),
            ("Year-one tax per unit", money(tu["assumed_tax_per_unit"]), "", "computed"),
        ]),
        ("What the generic shortcut would have said", [
            ("Assessed value if it reset to the price", money(tu["generic_assessed"]),
             "Purchase price times the assessment ratio — the pattern that holds in most "
             "states.", ""),
            ("Tax on that basis", money(tu["generic_tax"]), "", ""),
            ("Overstatement versus the correct approach",
             esc(f"{tu['generic_overstatement']:.2f}x") if tu["generic_overstatement"]
             else _missing(),
             "Shown because the size of the error is the point: a tax line this far out "
             "fails a deal that works.", ""),
        ]),
    ])
    f.raw('<p class="op-note">Every line marked <em>Available in the internal underwriting '
          'agent only</em> — the parcel, its tax area, the assessed value and the county\'s bill — '
          'is produced by the internal underwriting agent from the county assessor. The demo '
          'carries the rate, the assessment ratio and the tax it computes from them.</p>')
    return f.render()


def notes_fragment(deal: dict) -> str:
    """The judgment calls, in plain language."""
    notes = deal["meta"].get("notes") or []
    f = Fragment(
        "Notes and disclosures",
        "Every output here is the result of decisions someone made. These are those "
        "decisions — including the ones that make the numbers look worse.",
    )
    if not notes:
        f.raw(str(_absence_block("This output set carries no disclosures.", "No notes")))
        return f.render()
    for n in notes:
        applies = n.get("applies_to")
        f.raw(
            f"<h3>{_escape(str(n.get('topic') or ''))}</h3>"
            + (f'<p class="op-note">Applies to: {_escape(str(applies))}</p>' if applies else "")
            + f"<p>{_escape(str(n.get('note') or ''))}</p>"
        )
    return f.render()


def supply_fragment(deal: dict) -> str:
    sup = compute.supply(deal)
    f = Fragment(
        "Competing supply",
        "Pipeline projects by ring and stage. Stage is stated because entitled units and "
        "delivered units are not the same risk.",
    )
    f.raw(
        '<p class="op-note"><strong>Coverage note.</strong> '
        + _escape(SUPPLY_COVERAGE_NOTE)
        + "</p>"
    )
    if not sup["rows"]:
        f.raw(str(_absence_block(deal["supply"].get("absent_reason"), "No competing-supply data")))
        return f.render()

    f.table(
        ["Ring", "Stage", "Projects", "Units"],
        [
            [r["ring"], r["stage"], compute.fmt_number(r["projects"]), compute.fmt_number(r["units"])]
            for r in sup["rows"]
        ],
        numeric=(2, 3),
    )
    f.table(
        ["Ring", "Pipeline units", "Existing stock", "Units as % of stock"],
        [
            [
                t["ring"],
                compute.fmt_number(t["total_units"]),
                compute.fmt_number(t["ring_stock"]) if t["ring_stock"] else schema.MISSING,
                compute.fmt_pct(t["pct_of_ring_stock"]),
            ]
            for t in sup["totals"]
        ],
        numeric=(1, 2, 3),
        row_classes=["op-total"] * len(sup["totals"]),
    )
    projects = deal["supply"].get("projects") or []
    if projects:
        f.raw(
            f"<h3>Projects within {compute.fmt_number(schema.SUPPLY_PROJECT_RADIUS_MI, places=0)} miles</h3>"
            f'<p class="op-note">One row per development, at the furthest stage the pipeline '
            f"shows; a dash means the source did not state it.</p>"
        )
        f.table(
            ["Project", "Miles", "Stage", "Units", "Type", "Address", "City", "Acres",
             "Developer", "First filed", "Latest activity", "Source layers"],
            [
                [
                    p["name"],
                    compute.fmt_number(p["miles"], places=1),
                    p["stage"],
                    # A project the source gave no unit count for adds zero units to the
                    # ring above and must read as a dash here: a 0 would say it builds
                    # nothing, which is the one thing the record does not say.
                    compute.fmt_number(p.get("units")) or "—",
                    p.get("product") or "—",
                    p.get("address") or "—",
                    p.get("city") or "—",
                    compute.fmt_number(p.get("acres"), places=1) or "—",
                    p.get("developer") or "—",
                    p.get("filed") or "—",
                    p.get("latest") or "—",
                    ", ".join(p.get("layers") or []) or "—",
                ]
                for p in projects
            ],
            numeric=(1, 3, 7),
            wrap_cols=(0, 5, 8),
        )
    return f.render()


def t12_data_fragment(deal: dict) -> str:
    absent = _absent_fragment(deal, "t12_data")
    if absent is not None:
        return absent
    labels = deal["t12"]["month_labels"]
    order = compute.detect_month_order(labels)
    f = Fragment(
        "T-12 data",
        f"The monthly matrix as normalized, with each source line mapped to a category. "
        f"Months run {order.replace('_', '-')}, detected from the labels rather than "
        f"assumed — the trailing-three window depends on it. An uncategorized subtotal "
        f"the statement printed stays visible, and nothing sums it twice.",
    )
    rows, classes = [], []
    for line in compute.t12_row_totals(deal):
        rows.append(
            [
                line["label"],
                line["category"] or "—",
                *[
                    compute.fmt_currency(v)
                    for v in next(
                        r["monthly"] for r in deal["t12"]["rows"] if r["label"] == line["label"]
                    )
                ],
                compute.fmt_currency(line["t12"]),
                compute.fmt_currency(line["t3"]),
            ]
        )
        classes.append("op-sub" if not line["category"] else None)
    f.table(
        ["Line item", "Category", *labels, "T-12", "T-3"],
        rows,
        numeric=tuple(range(2, 16)),
        row_classes=classes,
    )
    return f.render()


def t12_pnl_fragment(deal: dict) -> str:
    absent = _absent_fragment(deal, "t12_pnl")
    if absent is not None:
        return absent
    p = compute.pnl(deal)
    f = Fragment(
        "P&L summary",
        "The rollup. Two rules do the work: gross scheduled rent is whatever the "
        "statement printed, or gross potential rent plus gain/(loss) to lease where it "
        "printed nothing; and total revenue sums from gross scheduled rent down — never "
        "from gross potential rent, which would count loss-to-lease twice.",
    )
    subtotals = {
        "Gross Scheduled Rent",
        "Total Revenue",
        schema.PNL_TOTAL_OPEX_LABEL,
        schema.PNL_NOI_LABEL,
    }
    rows, classes = [], []
    for label in schema.pnl_row_order():
        vals = p["rows"][label]
        rows.append(
            [
                label,
                compute.fmt_currency(vals["t12"]),
                compute.fmt_currency(vals["t3_annualized"]),
                compute.fmt_currency(vals["per_unit"]),
                compute.fmt_pct(vals["pct_of_revenue"]),
            ]
        )
        classes.append("op-total" if label in subtotals else None)
    f.table(
        ["Line item", "T-12", "T-3 annualized", "Per unit", "% of revenue"],
        rows,
        numeric=(1, 2, 3, 4),
        row_classes=classes,
    )
    f.raw(
        f'<p class="op-note">Gross scheduled rent basis: {esc(p["gsr_basis"])}.</p>'
    )

    for cat, data in compute.breakouts(deal).items():
        f.raw(f"<h3>{esc(cat)} — top {data['depth']} plus rest</h3>")
        rows = [
            [e["label"], compute.fmt_currency(e["t12"]), compute.fmt_currency(e["t3_annualized"])]
            for e in data["top"]
        ]
        rows.append(
            [
                f"All other {cat.lower()} ({data['rest_count']} lines)",
                compute.fmt_currency(data["rest_t12"]),
                compute.fmt_currency(data["rest_t3_annualized"]),
            ]
        )
        rows.append(
            [
                f"Total {cat.lower()}",
                compute.fmt_currency(data["total_t12"]),
                compute.fmt_currency(data["total_t3_annualized"]),
            ]
        )
        f.table(
            ["Line item", "T-12", "T-3 annualized"],
            rows,
            numeric=(1, 2),
            row_classes=[None] * len(data["top"]) + [None, "op-total"],
        )

    # ---- trailing annualized T-3 revenue (E2 rule 8) ------------------------------------
    series = compute.t3_trailing_series(deal)
    if series:
        f.raw("<h3>Trailing T-3 annualized revenue</h3>"
              '<p class="op-note">Each figure is that month plus the two before it, times four — '
              "the trend the T-3 cap rate sits at the end of. Fewer than eight months of statement "
              "give fewer figures, never a padded zero.</p>")
        f.table(["Month", "T-3 annualized revenue"],
                [[e["month"], compute.fmt_currency(e["t3_revenue_annualized"])] for e in series],
                numeric=(1,))

    # ---- pricing, on the analysis tab -----------------------------
    pr = compute.pricing(deal)
    chip = CHIP if deal["meta"].get("illustrative_pricing") else SAFE_EMPTY
    f.raw("<h3>Pricing and cap rate</h3>")
    ladder = [
        ("Price per unit", _money(pr["price_per_unit"]) + chip, ""),
        ("Unit count", esc(compute.fmt_number(pr["units_total"])), ""),
        ("Total purchase price", _money(pr["total_price"]) + chip, ""),
        ("T-12 revenue", _money(pr["t12_revenue"]), ""),
        ("T-12 operating expenses", _money(pr["t12_opex"]), ""),
        ("T-12 net operating income", _money(pr["t12_noi"]), ""),
        ("T-12 cap rate", _pct(pr["t12_cap_rate"], 2) + chip, ""),
        ("T-3 revenue (annualized)", _money(pr["t3_revenue_annualized"]), ""),
        ("T-3 operating expenses (annualized)", _money(pr["t3_opex_annualized"]), ""),
        ("T-3 net operating income (annualized)", _money(pr["t3_noi_annualized"]), ""),
        ("T-3 cap rate", _pct(pr["t3_cap_rate"], 2) + chip, ""),
    ]
    sections: list[tuple[str, list[tuple[str, str, str]]]] = [("Pricing and cap rate", ladder)]
    if pr["proforma"]:
        sections.append(
            (
                "Broker pro forma",
                [
                    ("Revenue", _money(pr["proforma"]["revenue"]), ""),
                    ("Operating expenses", _money(pr["proforma"]["opex"]), ""),
                    ("Net operating income", _money(pr["proforma"]["noi"]), ""),
                    (
                        "Cap rate",
                        _pct(pr["proforma"]["cap_rate"], 2) + chip,
                        "Kept in its own block; never merged into the T-12.",
                    ),
                ],
            )
        )
    sections.append(
        (
            "Revenue trend",
            [
                ("T-12 revenue", _money(pr["t12_revenue"]), ""),
                ("T-6 annualized", _money(pr["t6_revenue_annualized"]), ""),
                ("T-3 annualized", _money(pr["t3_revenue_annualized"]), ""),
            ],
        )
    )
    f.card(sections)
    return f.render()


def t12_checks_fragment(deal: dict) -> str:
    absent = _absent_fragment(deal, "t12_checks")
    if absent is not None:
        return absent
    f = Fragment(
        "T-12 audit trail",
        "The cross-tie against the subtotals the source statement printed, then the "
        "statement against the rent roll. A difference over a dollar reads REVIEW rather "
        "than quietly passing; a subtotal the source never printed reads NOT CHECKED, not OK.",
    )
    rows = []
    checks = compute.error_checks(deal)
    # The breakout ties (Other Income, Utility Reimbursements, Utilities) exist only when
    # the statement prints a group subtotal, which most do not. Two or more of them NOT
    # CHECKED collapse into one line that says so, instead of a table that is half
    # "NOT CHECKED" on a perfectly ordinary statement.
    untied = [c for c in checks if not c.get("headline") and c["status"] == "NOT CHECKED"]
    collapse = len(untied) >= 2
    for check in checks:
        if collapse and check in untied:
            continue
        status = check["status"]
        css = "op-ok" if status == "OK" else "op-flag"
        cell = Safe(f'<span class="{css}">{_escape(status)}</span>')
        rows.append(
            [
                check["check"],
                compute.fmt_currency(check["ours"]),
                compute.fmt_currency(check["source"]) if check["source"] is not None else "—",
                compute.fmt_currency(check["difference"]) if check["difference"] is not None else "—",
                cell,
                check.get("reason") or "",
            ]
        )
    if collapse:
        names = ", ".join(c["check"] for c in untied)
        rows.append([
            f"Group subtotals ({names})", "—", "—", "—",
            Safe('<span class="op-flag">NOT CHECKED</span>'),
            f"The statement prints no subtotal for these {len(untied)} groups, so there is nothing "
            f"to tie their category totals to. The P&amp;L still shows each group's own total.",
        ])
    f.table(
        ["Check", "Our total", "Source subtotal", "Difference", "Status", "Why"],
        rows,
        numeric=(1, 2, 3),
        wrap_cols=(5,),
    )

    # ---- statement sanity checks (E2 rules 1, 3, 4, 5) ------------------------------
    guards = compute.statement_guards(deal)
    if guards:
        f.raw("<h3>Statement sanity checks</h3>"
              '<p class="op-note">Four guards on how the statement is printed. A REVIEW is flagged '
              "for a reader to resolve; nothing here changes a number — the P&amp;L is the statement "
              "as categorised, unflipped.</p>")
        f.table(
            ["Guard", "Status", "Lines named", "Detail"],
            [[g["guard"],
              Safe(f'<span class="{"op-ok" if g["status"] == "OK" else "op-flag"}">{_escape(g["status"])}</span>'),
              "; ".join(g["rows"]) or "—", g["detail"]] for g in guards],
            wrap_cols=(2, 3),
        )

    # ---- across the two documents ---------------------------------------------------
    # The ties above are within the statement. These put the statement against the roll —
    # the check an underwriter does in their head — and read REVIEW outside a tenth.
    f.raw("<h3>Across the two documents: statement vs rent roll</h3>"
          '<p class="op-note">Different documents on different bases — twelve months of a '
          f"statement against one day of a roll — so a tie within {compute.CROSS_DOC_BAND:.0%} "
          "is the expected state and a wider gap is the thing to look at, not a rounding "
          "difference.</p>")
    xrows = []
    for c in compute.cross_document_checks(deal):
        status = c["status"]
        css = "op-ok" if status == "OK" else "op-flag"
        xrows.append([
            c["check"],
            compute.fmt_currency(c["ours"]) if c["ours"] is not None else "—",
            compute.fmt_currency(c["source"]) if c["source"] is not None else "—",
            (compute.fmt_pct(c["pct"]) if c["pct"] is not None else "—"),
            Safe(f'<span class="{css}">{_escape(status)}</span>'),
            c.get("reason") or c["basis"],
        ])
    f.table(["Tie", "Statement", "Rent roll", "Gap", "Status", "Basis / why not"], xrows,
            numeric=(1, 2, 3), wrap_cols=(5,))

    return f.render()


def rent_roll_checks_fragment(deal: dict) -> str:
    absent = _absent_fragment(deal, "rent_roll_checks")
    if absent is not None:
        return absent
    f = Fragment(
        "Rent roll audit trail",
        "The normalized roll tied back to the export's own stated totals — unit count, "
        "square footage, occupancy, rent totals — then a seeded readback of unit rents "
        "against the source cells. Every row compares against a figure the SOURCE printed, "
        "never against another number this toolkit derived.",
    )
    # ---- rent-roll tie-outs, on the same surface -------------------------------------
    # The block above ties our P&L to the income statement's subtotals. This ties the
    # normalized rent roll to the ROLL's own stated totals — a different document and a
    # different failure mode. A reader asking "can I trust these numbers?" should not have
    # to find two separate places that answer it.
    rrc = compute.rent_roll_checks(deal)
    def _status(v):
        css = "op-ok" if str(v).startswith("OK") else ("op-note" if v == "INFO" else "op-flag")
        return Safe(f'<span class="{css}">{_escape(str(v))}</span>')

    # ---- the reconciliation verdict (E2 rule 16), first --------------------------------
    vd = rrc["verdict_delta"]
    vcss = "op-ok" if rrc["verdict"] == "PASS" else "op-flag"
    f.raw(f'<p class="op-note"><strong>Reconciliation: <span class="{vcss}">{_escape(rrc["verdict"])}</span></strong>'
          f' — units off by {_escape(compute.fmt_number(vd["units"]) if vd["units"] is not None else "n/a")}, '
          f'market rent off by {_escape(compute.fmt_currency(vd["market_rent"]) if vd["market_rent"] is not None else "n/a")}. '
          f'{_escape(rrc["verdict_basis"])}</p>')
    f.raw("<h3>Rent-roll tie-outs</h3>")
    f.table(
        ["Check", "Our calc", "Source", "Difference", "Status", "Note"],
        [
            [
                c["item"],
                (compute.fmt_pct(c["ours"]) if c.get("format") == "pct"
                 else compute.fmt_number(c["ours"])),
                (compute.fmt_pct(c["source"]) if c.get("format") == "pct"
                 else compute.fmt_number(c["source"])) if c["source"] is not None else "—",
                (compute.fmt_pct(c["diff"]) if c.get("format") == "pct"
                 else compute.fmt_number(c["diff"])) if c["diff"] is not None else "—",
                _status(c["verdict"]),
                c.get("note") or "",
            ]
            for c in rrc["rows"]
        ],
        numeric=(1, 2, 3),
        wrap_cols=(5,),
    )
    f.raw(
        f'<p class="op-note"><strong>{_escape(rrc["summary"])}.</strong> A total the source '
        f"export never printed reads NOT CHECKED and stays in the denominator — excluding "
        f"it would let a check that could not run flatter the ratio. Occupancy here is "
        f"physical: {_escape(rrc['occupancy_basis'])}; down, model and employee units "
        f"count as vacant.</p>"
    )

    # ---- duplicate rows disclosed (E2 rule 14) --------------------------------------
    dup = compute.duplicate_disclosure(deal)
    f.raw(f"<h3>Duplicate rows — {dup['total']} unit(s) listed twice</h3>")
    if dup["rows"]:
        f.table(
            ["Unit", "Kept row", "Kept status", "Kept rent", "Rows not counted", "Why"],
            [[str(d["unit_id"]), str(d["kept_row"]), d["kept_status"] or "—",
              compute.fmt_currency(d["kept_rent"]) if d["kept_rent"] is not None else "—",
              "; ".join(f"row {x['row']}: {x['status'] or 'no status'}, "
                        f"{compute.fmt_currency(x['rent']) if x['rent'] is not None else 'no rent'}"
                        for x in d["dropped"]) or "—",
              d["why"]] for d in dup["rows"]],
            numeric=(3,), wrap_cols=(4, 5),
        )
        if dup["overflow"]:
            f.raw(f'<p class="op-note">… and {dup["overflow"]} more unit(s) not listed here; every one is in the run\'s JSON.</p>')
    else:
        f.raw('<p class="op-note">The export lists no unit twice.</p>')

    f.raw(f"<h3>Rent readback — {len(rrc['readback'])} units</h3>")
    f.table(
        ["Unit", "Plan", "Our in-place rent", "Source cell", "Source locator", "Status"],
        [
            [
                str(rb["unit_id"]),
                str(rb.get("floor_plan") or "—"),
                compute.fmt_currency(rb.get("our_rent")) if rb.get("our_rent") is not None else "—",
                compute.fmt_currency(rb.get("source_rent")) if rb.get("source_rent") is not None else "—",
                str(rb.get("source_locator") or "—"),
                _status(rb["verdict"]),
            ]
            for rb in rrc["readback"]
        ],
        numeric=(2, 3),
    )
    f.raw(
        f'<p class="op-note">{rrc["readback_matched"]} of {len(rrc["readback"])} match '
        f'within ${rrc["readback_tolerance"]:.2f}. The sample is seeded from the roll\'s '
        f'as-of date ({_escape(rrc["readback_seed"])}), so the same roll always checks the '
        f'same units and anyone re-running this sees these rows. '
        f'{_escape(rrc["readback_limit_note"])}</p>'
    )

    return f.render()


def rent_roll_fragment(deal: dict) -> str:
    absent = _absent_fragment(deal, "rent_roll")
    if absent is not None:
        return absent
    rr = deal["rent_roll"]
    tolerance = rr["new_tenant_tolerance_days"]
    f = Fragment(
        "Rent roll",
        f"The normalized per-unit roll. Unit identifiers only — there is no tenant-name "
        f"column in the schema, so there is nothing for a scrub step to miss. Lease type "
        f"is derived from the gap between lease start and move-in ({tolerance}-day "
        f"tolerance); a blank means the source could not support a classification, and a "
        f"date the source gave as text stays text rather than being coerced.",
    )
    rows = []
    for unit in rr["units"]:
        lease_type = compute.classify_lease(unit, tolerance)
        rent = unit.get("in_place_rent")
        rows.append(
            [
                unit["unit_id"],
                unit["floor_plan"],
                compute.fmt_number(unit["beds"]),
                compute.fmt_number(unit["baths"], places=1),
                compute.fmt_number(unit["sqft"]),
                unit["status"],
                unit.get("lease_start") or "—",
                unit.get("move_in") or "—",
                lease_type or "—",
                compute.fmt_currency(rent) if rent is not None else "—",
                compute.fmt_currency(compute.safe_div(rent, unit.get("sqft")), cents=True),
            ]
        )
    f.table(
        ["Unit", "Plan", "Bd", "Ba", "SF", "Status", "Lease start", "Move-in", "Type", "In-place rent", "$/SF"],
        rows,
        numeric=(2, 3, 4, 9, 10),
    )
    return f.render()


def _windowed(rent, count) -> str:
    """A windowed rent as the rent-roll Summary shows it: n/a where the roll cannot
    support the column, a dash where the window held no lease."""
    if count is None:
        return "n/a"
    return compute.fmt_currency(rent) if rent else "—"


def unit_mix_fragment(deal: dict) -> str:
    absent = _absent_fragment(deal, "unit_mix")
    if absent is not None:
        return absent
    plans = compute.plan_summary(deal)
    totals = compute.roll_totals(deal)
    moves = compute.move_in_counts(deal)
    f = Fragment(
        "Unit mix summary",
        "Per-plan rollup, in the rent-roll Summary's own columns. Market rent for model "
        "is the new move-in rent — residents whose move-in date falls inside the lookback "
        "window, whatever their lease type — where the plan has one, and the in-place "
        "average otherwise (in-place outright on a roll with no usable lease start or "
        "move-in date); the basis column says which, per plan. Leases signed is every "
        "classified lease started in the window, new and renewal together, shown for context and "
        "never fed to the model: a blended average is a renewal average wearing a "
        "market-rent label, and it understates trade-out on every deal it touches.",
    )
    rows = [
        [
            p["floor_plan"],
            compute.fmt_number(p["beds"]),
            compute.fmt_number(p["baths"], places=1),
            compute.fmt_number(p["avg_sqft"]),
            compute.fmt_number(p["units"]),
            compute.fmt_number(p["occupied"]),
            compute.fmt_number(p["vacant"]),
            compute.fmt_currency(p["avg_in_place_rent"]),
            _windowed(p["signed_rent"], p["signed_count"]),
            "n/a" if p["signed_count"] is None else compute.fmt_number(p["signed_count"]),
            _windowed(p["movein_rent"], p["movein_count"]),
            "n/a" if p["movein_count"] is None else compute.fmt_number(p["movein_count"]),
            compute.fmt_currency(p["market_rent_for_model"]),
            p["basis"],
        ]
        for p in plans
    ]
    f.table(
        [
            "Plan", "Bd", "Ba", "Avg SF", "Units", "Occ", "Vac", "In-place rent",
            "Leases signed rent", "Signed #", "New move-in rent", "Move-in #",
            "Market rent for model", "Basis",
        ],
        rows,
        numeric=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12),
        wrap_cols=(13,),
    )
    f.raw("<h3>Units by floor plan</h3>")
    f.bars(
        [
            (p_["floor_plan"], [(p_["units"], "")], compute.fmt_number(p_["units"]))
            for p_ in plans
        ]
    )
    f.raw("<h3>In-place vs new move-in vs market rent for model</h3>")
    f.bars(
        [
            (
                p_["floor_plan"],
                [
                    (p_["avg_in_place_rent"], ""),
                    (p_["movein_rent"], "op-bar__fill--alt"),
                    (p_["market_rent_for_model"], "op-bar__fill--soft"),
                ],
                compute.fmt_currency(p_["market_rent_for_model"]),
            )
            for p_ in plans
        ],
        legend=[
            ("In-place", ""),
            ("New move-in", "op-bar__fill--alt"),
            ("Market rent for model", "op-bar__fill--soft"),
        ],
    )

    f.card(
        [
            (
                "Knobs",
                [
                    (
                        "As-of date",
                        esc(totals["as_of"]),
                        "Every window is measured from this date, never from today.",
                    ),
                    ("Lookback months", esc(totals["lookback_months"]), ""),
                    (
                        "New-tenant tolerance",
                        esc(f"{totals['new_tenant_tolerance_days']} days"),
                        "",
                    ),
                ],
            ),
            (
                "Occupancy",
                [
                    ("Total units", esc(compute.fmt_number(totals["total_units"])), ""),
                    ("Occupied", esc(compute.fmt_number(totals["occupied_units"])), ""),
                    ("Vacant", esc(compute.fmt_number(totals["vacant_units"])), ""),
                    (
                        "Non-revenue",
                        esc(compute.fmt_number(totals["non_revenue_units"])),
                        "Models and down units: in the stock, in neither occupancy bucket.",
                    ),
                    ("Occupancy", _pct(totals["occupancy"]), ""),
                    ("Vacancy", _pct(totals["vacancy"]), "The physical-vacancy input."),
                ],
            ),
            (
                "Rents and turnover",
                [
                    (
                        "Weighted in-place rent",
                        _money(totals["wavg_in_place_rent"]),
                        "Weighted by occupied units.",
                    ),
                    (
                        "Weighted market rent for model",
                        _money(totals["wavg_market_rent_for_model"]),
                        "Weighted by total units.",
                    ),
                    ("Move-ins in window", esc(totals["move_ins_in_window"]), ""),
                    (
                        "Annualized turnover",
                        _pct(totals["annualized_turnover"]),
                        "Move-ins in the window, annualized against unit count.",
                    ),
                ],
            ),
            (
                "New move-ins",
                [
                    (
                        "Move-ins in window",
                        esc(moves["move_ins"]),
                        "Counted by MOVE-IN date, never by lease start — a renewal stamps a "
                        "fresh lease start against a resident who moved in years ago.",
                    ),
                    ("Window (months)", esc(moves["window_months"]), ""),
                    ("Share of units", _pct(moves["share"]), ""),
                ],
            ),
        ]
    )
    return f.render()


def audit_trail_fragment(deal: dict) -> str:
    import provenance as pv

    stamps = sorted((deal.get("provenance") or {}).items())
    stamped, required, pct = pv.coverage(deal.get("provenance") or {})
    evidence = [(p, st) for p, st in stamps if not pv.is_absence(st)]
    absent = [(p, st) for p, st in stamps if pv.is_absence(st)]
    f = Fragment(
        "Audit trail",
        f"Where every published value came from, and how. {stamped} of {required} required "
        f"values carry a stamp ({pct:.0f}%); {len(evidence)} stamps point at a document or a "
        f"computation and {len(absent)} record a value the run could not or does not produce, "
        f"listed apart below so they do not stand beside the evidence as if they were some. "
        f"A derived value names the values it was built from — one that could not would not "
        f"have passed validation. Then the lines actually read, per document, and what the "
        f"run itself did.",
    )
    if not stamps:
        f.raw(str(_absence_block(None, "No provenance recorded")))
        return f.render()

    # ---- 1. evidence: every value that points at a document or a computation ----------
    rows = []
    for path, st in evidence:
        quote = st.get("quote") or ""
        note = st.get("note") or ""
        detail = quote if not note else (f"“{quote}” — {note}" if quote else note)
        rows.append(
            [
                path,
                st.get("source_doc", ""),
                st.get("locator", ""),
                st.get("method", ""),
                ", ".join(st.get("inputs", []) or []) or "—",
                detail or "—",
            ]
        )
    f.raw("<h3>Evidence, value by value</h3>")
    f.table(
        ["Value", "Source document", "Where in it", "How", "Built from", "Supporting quote / note"],
        rows,
        wrap_cols=(1, 4, 5),
    )

    # ---- 2. what is NOT here, and why — one block, grouped by the reason -----------------
    if absent:
        groups: dict[str, list[tuple[str, str]]] = {}
        for path, st in absent:
            source = st.get("source_doc", "")
            found = schema.internal_only(path)
            key = f"{source} — {found[0]}" if (source == schema.INTERNAL_ONLY_SOURCE and found) else source
            groups.setdefault(key, []).append((path, st.get("note") or ""))
        f.raw("<h3>Values this run does not carry, and why</h3>"
              '<p class="op-note">A MISSING value with its reason. <em>Available in the internal underwriting agent only</em> '
              "means the operator's pipeline produces it and the public demo deliberately does "
              "not; the other reasons name what the uploaded documents did not state or what the "
              "run did not do.</p>")
        arows = []
        for key, items in groups.items():
            reasons = {note for _p, note in items if note}
            reason = next(iter(reasons)) if len(reasons) == 1 else " · ".join(sorted(reasons)) or "—"
            arows.append([key, str(len(items)), ", ".join(p for p, _n in items), reason])
        f.table(["Reason", "Values", "Which", "Detail"], arows, numeric=(1,), wrap_cols=(2, 3))

    # ---- 3. the lines read, per document -----------------------------------------------
    t12 = deal.get("t12") or {}
    if isinstance(t12, dict) and t12.get("rows") and not schema.section_absent(deal, "t12"):
        lines = []
        for r in t12["rows"]:
            monthly = [v for v in (r.get("monthly") or []) if isinstance(v, (int, float)) and not isinstance(v, bool)]
            lines.append([r.get("label", ""), r.get("category") or "— (not carried into the P&L)",
                          esc(compute.fmt_currency(sum(monthly))) if monthly else "—"])
        f.raw(f"<h3>Statement lines read ({len(lines)})</h3>"
              '<p class="op-note">Every line of the trailing-twelve statement, the category it was '
              "placed under, and its twelve-month total. A line without a category is a subtotal or "
              "a memo line the P&amp;L does not sum.</p>")
        f.table(["Statement line", "Category", "12-month total"], lines, numeric=(2,), wrap_cols=(0, 1))
    roll = deal.get("rent_roll") or {}
    if isinstance(roll, dict) and roll.get("units") and not schema.section_absent(deal, "rent_roll"):
        urows = []
        for u in roll["units"]:
            rent = u.get("in_place_rent")
            urows.append([str(u.get("unit_id", "")), str(u.get("floor_plan") or ""),
                          str(u.get("status") or ""),
                          esc(compute.fmt_currency(rent)) if isinstance(rent, (int, float)) else "—",
                          str(u.get("lease_start") or "—"), str(u.get("source_row") or "—")])
        f.raw(f"<h3>Rent-roll units read ({len(urows)})</h3>"
              '<p class="op-note">Unit identifiers only — there is no tenant-name column in the '
              "schema. The source row is the row of the uploaded roll the unit was read from.</p>")
        f.table(["Unit", "Plan", "Status", "In-place rent", "Lease start", "Source row"], urows, numeric=(3,))
    comps = deal.get("comps") or {}
    if isinstance(comps, dict) and comps.get("rows"):
        seen: dict[str, dict] = {}
        for r in comps["rows"]:
            seen.setdefault(r["community"], r)
        with_units = bool(compute.community_facts(deal))
        crows = []
        for name, r in sorted(seen.items(), key=lambda kv: (kv[1].get("distance_mi") or 0, kv[0])):
            year = r.get("year_built")
            cells = [name, f"{compute.fmt_number(r.get('distance_mi'), places=1)} mi",
                     f"tier {r.get('tier')}", str(year) if year else "not published",
                     str(r.get("year_built_source") or "—"), str(r.get("as_of") or "—"),
                     str(r.get("source") or "—")]
            if with_units:
                units = r.get("units")
                cells[3:3] = [_n_or_missing(units, compute.fmt_number) if units is not None else "—",
                              str(r.get("units_source") or (compute.UNITS_NOT_STATED if units is None else "—"))]
            crows.append(cells)
        f.raw(f"<h3>Comp communities quoted ({len(crows)})</h3>")
        headers = ["Community", "Distance", "Tier", "Year built", "Year built from", "Rents as of", "Source"]
        if with_units:
            headers[3:3] = ["Units", "Units from"]
        f.table(headers, crows, wrap_cols=(4, 6, 8) if with_units else (4, 6))

    # ---- 4. the run itself -------------------------------------------------------------
    run = (deal.get("meta") or {}).get("run")
    if isinstance(run, dict):
        f.raw("<h3>This run</h3>")
        if run.get("kind") == "upload" or run.get("model_calls"):
            models = run.get("models") or {}
            f.table(["Model calls", "Input tokens", "Output tokens", "Cache read", "Cache write",
                     "Cost", "Seconds", "Extraction model", "Classification model"],
                    [[compute.fmt_number(run.get("model_calls")), compute.fmt_number(run.get("input_tokens")),
                      compute.fmt_number(run.get("output_tokens")), compute.fmt_number(run.get("cache_read_tokens")),
                      compute.fmt_number(run.get("cache_write_tokens")),
                      esc(compute.fmt_currency(run.get("dollars"), cents=True)) if isinstance(run.get("dollars"), (int, float)) else "—",
                      compute.fmt_number(run.get("seconds"), places=1),
                      str(models.get("extraction") or "—"), str(models.get("classification") or "—")]],
                    numeric=(0, 1, 2, 3, 4, 5, 6))
        if run.get("note"):
            f.raw(f'<p class="op-note">{esc(run["note"])}</p>')
    return f.render()


#: The boundary sentence's clause for a deal that carries the memorandum's own comparables —
#: the manifest lists them, so "comps come only from the scrape" alone would deny its own rows
#:. Shared with the workbook manifest.
OM_COMPS_BOUNDARY_CLAUSE = (
    " — plus, listed apart and labelled as such, the comparables the offering memorandum itself "
    "states: the seller's figures, verified against the memorandum's own text, not market data"
)


def _has_om_comps(deal: dict | None) -> bool:
    block = (deal or {}).get("om_comps") if isinstance(deal, dict) else None
    return isinstance(block, dict) and bool(block.get("rent") or block.get("sale"))


def manifest_fragment(deal: dict | None) -> str:
    fields = schema.manifest_fields_for(deal)
    f = Fragment(
        "Model-ready outputs",
        f"{len(fields)} fields the pipeline extracts from a deal's raw documents and "
        f"hands to <em>your</em> model. It does not build a model — every shop already "
        f"has one, and theirs is the one their committee trusts. The work worth "
        f"automating is filling it in correctly. Comps come only from an operator's own "
        f"weekly rent scrape{OM_COMPS_BOUNDARY_CLAUSE if _has_om_comps(deal) else ''}; "
        f"licensed sources are excluded by design, so a field one of "
        f"them would have filled reads MISSING rather than being sourced elsewhere.",
    )
    rows, classes = [], []
    current: str | None = None
    for field in fields:
        if field.group != current:
            current = field.group
            rows.append([field.group, "", "", ""])
            classes.append("op-sub")
        internal = schema.internal_only(field.path)
        example = "—"
        if deal is not None and field.path:
            # What the deal ASSERTS, not merely what it holds: a not_asserted closing-cost
            # amount reads MISSING here, never $0 (schema.manifest_value).
            value = schema.manifest_value(deal, field)
            if value is not None:
                if isinstance(value, bool):
                    example = "yes" if value else "no"
                elif isinstance(value, (int, float)):
                    example = (
                        compute.fmt_pct(value)
                        if field.type == "percent"
                        else compute.fmt_currency(value)
                        if field.type == "currency"
                        else compute.fmt_year(value)
                        if schema.is_year_field(field)
                        else compute.fmt_number(value, places=1 if isinstance(value, float) else 0)
                    )
                elif isinstance(value, list):
                    example = f"{len(value)} entries"
                else:
                    text = str(value)
                    example = text if len(text) <= 90 else text[:87] + "…"
        if internal and schema.public_substitute(field.path) and deal is not None:
            # A row column with a public stand-in: the example the policy left on the row,
            # with the row's own label for it — never the licensed figure.
            value_cell: Any = schema.with_source(deal, field.path, example)
        elif internal:
            value_cell = _internal_marker(internal[1])
        else:
            value_cell = example
        rows.append([field.name, value_cell, field.source, field.fills])
        classes.append(None)
    f.table(
        ["Name", "Value", "Source", "Fills in the model"],
        rows,
        wrap_cols=(1, 2, 3),
        row_classes=classes,
    )
    internal_groups = schema.internal_only_groups()
    if internal_groups:
        f.raw("<h3>What the public demo leaves to the internal pipeline</h3>"
              '<p class="op-note">The demo is a deliberately reduced copy of the operator\'s pipeline: '
              "a licensed source, a portal login or a web lookup is not something a public page should "
              "reach. Each of these reads <em>Available in the internal underwriting agent only</em> wherever it appears, with the "
              "reason, instead of reading as a hole the pipeline failed to fill.</p>")
        f.table(["Capability", "Why the demo does not produce it", "Fields"],
                [[label, reason, ", ".join(paths)] for label, reason, paths in internal_groups],
                wrap_cols=(1, 2))
    return f.render()


BUILDERS: dict[str, Any] = {
    "summary.html": summary_fragment,
    "comps.html": comps_fragment,
    "property-tax.html": property_tax_fragment,
    "demand.html": demand_fragment,
    "supply.html": supply_fragment,
    "t12-data.html": t12_data_fragment,
    "t12-pnl.html": t12_pnl_fragment,
    "t12-checks-pricing.html": t12_checks_fragment,
    "rent-roll.html": rent_roll_fragment,
    "unit-mix.html": unit_mix_fragment,
    "rent-roll-checks.html": rent_roll_checks_fragment,
    "notes.html": notes_fragment,
    "manifest.html": manifest_fragment,
    "audit-trail.html": audit_trail_fragment,
}

#: Human labels for the tab bar on the demo page.
TAB_LABELS: dict[str, str] = {
    "summary.html": "Deal summary",
    "comps.html": "Rent comps",
    "property-tax.html": "Property tax",
    "demand.html": "Demand",
    "supply.html": "Supply",
    "t12-data.html": "Raw data",
    "t12-pnl.html": "Analysis",
    "t12-checks-pricing.html": "Audit trail",
    "rent-roll.html": "Raw data",
    "unit-mix.html": "Analysis",
    "rent-roll-checks.html": "Audit trail",
    "notes.html": "Notes & disclosures",
    "manifest.html": "Model-ready outputs",
    "audit-trail.html": "Audit trail",
}


def render_all(deal: dict, out_dir: Path) -> dict[str, Path]:
    errors = schema.validate(deal)
    if errors:
        raise ValueError("deal failed schema validation:\n  - " + "\n  - ".join(errors))
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, builder in BUILDERS.items():
        html_text = builder(deal)
        path = out_dir / name
        path.write_text(html_text, encoding="utf-8")
        written[name] = path
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deal", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    deal = json.loads(args.deal.read_text(encoding="utf-8"))
    written = render_all(deal, args.out)
    for name, path in written.items():
        print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    print(f"{len(written)} fragments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
