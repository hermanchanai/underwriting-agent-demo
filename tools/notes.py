"""The judgment calls, written down where the reader can see them.

Every output in this set is the result of decisions someone made: which comps count, what
a rent means, which number is real and which is illustrative. A model that hides those
decisions is not neutral — it just moves the argument somewhere the reader cannot reach.
So they live here, in plain language, and render as their own tab and preview section.

Two rules for anything added to this file:

* **Say what the number does NOT mean.** That is usually the useful half. "Market rent is
  not a blend of new and renewal leases" tells a reader more than any definition of market
  rent.
* **Name the choice, not just the outcome.** A note that says "comps are within 3 miles"
  is a fact; one that says "3 miles because that is where the operator's own scrape has
  coverage, and a wider ring would trade real rents for guesses" is a decision the reader
  can disagree with.
"""

from __future__ import annotations

import schema

#: Notes that hold for any deal this toolkit produces.
STANDARD: list[dict] = [
    {
        "topic": "Pricing is illustrative",
        "applies_to": "Deal Summary — Pricing",
        "note": (
            "Every price, per-unit figure and cap rate in this set is illustrative. The "
            "per-unit price is a round number chosen to sit inside the band a real "
            "per-unit sale price occupies, so the cap-rate arithmetic is realistic. It is "
            "not a quote, an offer, a broker's guidance, or a valuation. Cap rates move "
            "inversely with the price, so read them as a demonstration of the mechanics "
            "rather than a view on the asset."
        ),
    },
    {
        "topic": "Licensed data is excluded by design, not by omission",
        "applies_to": "Rent Comps",
        "note": (
            "No subscription or licensed market-data feed appears anywhere in this set. "
            "Comps come only from an operator's own weekly scrape of public leasing "
            "pages. That is a deliberate constraint, not a gap: licensed data cannot "
            "travel outside the licence, so a portfolio piece built on it could not be "
            "shown at all. The cost is coverage, and the coverage is measured and stated "
            "rather than assumed."
        ),
    },
    {
        "topic": "Comp radius is stated, and coverage inside it is measured",
        "applies_to": "Rent Comps",
        "note": (
            "The comp block states the radius it searched and how many tracked communities "
            "inside it carry live rents; that sentence is counted from the operator's own "
            "scrape, never written by hand. The detail tiers are the closest same-product "
            "communities within that radius, so a wide radius adds candidates without "
            "letting a distant one outrank a close one. A community the scrape tracks but "
            "could not quote is not dropped: it is carried, with the reason it is not a "
            "comp, in the downloadable interchange file."
        ),
    },
    {
        "topic": "Comps are matched on vintage",
        "applies_to": "Rent Comps — tiers",
        "note": (
            "The detailed tiers hold only communities within ten years of the subject's "
            "year built. Widening that range changes which communities are in the set, "
            "and a set that includes materially newer product will generally report a "
            "different rent — how much, and in which direction, is a property of the "
            "particular submarket and date, not a rule. Where this output set has "
            "measured the difference between two otherwise-identical comp sets on its "
            "own subject, the figures appear in the note below."
        ),
    },
    {
        "topic": "Year built comes from a free-text note, where it exists at all",
        "applies_to": "Rent Comps — tiers",
        "note": (
            "The rent tracker has no year-built column. Where a vintage appears here it "
            "was parsed out of a free-text note on the community record, and every row "
            "carries that as its source. A community with no such note keeps its place in "
            "the comp set on distance and product, shows a dash for the year, and the "
            "tier header says how many rows the vintage filter could not be applied to. "
            "Inventing a year would fabricate the evidence the filter runs on; dropping "
            "the community would hide the closest comps for want of a field the source "
            "never had."
        ),
    },
    {
        "topic": "Market rent is never a blend of new and renewal",
        "applies_to": "Rent Roll — Summary",
        "note": (
            "Market rent for the model is the new move-in rent — residents whose move-in "
            "date falls inside the lookback window, whatever their lease type — where a "
            "plan has one, and in-place rent where it does not. It is never the average of "
            "every lease signed, new and renewal together. A blended figure "
            "reads as market rent and is not one: renewals are priced off the existing "
            "rent, so blending them in drags the number toward where rents used to be and "
            "understates the gap the underwriting is trying to measure. On an asset whose "
            "in-place rents sit above what the market is currently signing, the rule "
            "produces a market rent BELOW in-place — which is the correct and "
            "uncomfortable answer, and the one a blend would hide."
        ),
    },
    {
        "topic": "Estimated values are not published as facts",
        "applies_to": "throughout",
        "note": (
            "Where a figure is derived rather than read, the audit trail says so and names "
            "the inputs. Where a figure is unknown, the cell shows a visible hole rather "
            "than a plausible-looking number. A blank that reads as a zero is the most "
            "expensive kind of missing data, because nothing downstream flags it."
        ),
    },
    {
        "topic": "What the public demo leaves to the internal pipeline",
        "applies_to": "throughout",
        "note": (
            "This demo is a deliberately reduced copy of the operator's pipeline. A licensed "
            "data source, a county-portal login, a school-rating lookup, a web valuation or a "
            "lease trade-out report is not something a public page should reach or ask for, so "
            "the values those produce — the assessor's parcel and bill, school ratings, a fair "
            "market value, the new-versus-renewal trade-out — read 'Internal pipeline only' "
            "with the reason wherever they appear. The field manifest lists every one of them. "
            "Everything else on these tabs is produced the same way for the sample and for an "
            "uploaded deal, from the same sources."
        ),
    },
    {
        "topic": "Supply coverage is an area of ongoing work",
        "applies_to": "Supply",
        "note": (
            "The supply section is real and unmodified — the rings, stages and unit counts "
            "come from a public permit and entitlement pipeline, and a zero is a genuine "
            "zero rather than a gap left blank. Coverage of that pipeline is an area of "
            "ongoing work: a ring showing no active supply means nothing was matched in "
            "the sources covered so far, which is a weaker statement than nothing being "
            "built. This section will be expanded; read it as a floor on competing "
            "supply, not a census of it."
        ),
    },
    {
        "topic": "Checks degrade to NOT CHECKED rather than passing quietly",
        "applies_to": "Error Checks & Pricing",
        "note": (
            "Every cross-tie on the checks tab reports OK, FAIL, or NOT CHECKED. A check "
            "missing one of its inputs reports NOT CHECKED and claims no difference — it "
            "never reports OK. A green result that only means 'this check could not run' "
            "is worse than a red one, because it buys confidence it has not earned."
        ),
    },
]


def derived_for(deal: dict) -> list[dict]:
    """Deal-specific notes COMPUTED from this deal's own numbers.

    The vintage note is the reason this function exists. It used to sit in STANDARD stating
    "a 1981 asset ... 9% below ... 1.5% above" — true of the showcase deal and false of
    every other, including the synthetic fixture, whose published Notes tab therefore made
    factual claims about a different property. A note that quotes numbers has to be built
    from the numbers it quotes.
    """
    import compute

    out: list[dict] = []
    comps = deal.get("comps") or {}
    if comps.get("rows"):
        c = compute.comps(deal)
        tiering = c.get("tiering") or {}
        subject_year = tiering.get("subject_year_built")
        k = c.get("vintage_contrast") or {}
        years = [r["year_built"] for r in c["rows"] if r.get("year_built")]
        # Four preconditions, each earned by a bug:
        #   * the CONTRAST must be published, not withheld. It is withheld when the two
        #     comp sets do not cover the same bed counts — comparing them then measures the
        #     bed mix, not the vintage.
        #   * `years` must be non-empty. The tiering contract allows every detailed comp to
        #     have an unknown year, and min() on an empty list raised.
        #   * the subject's own year must be known, or there is no band to describe.
        #   * the counterfactual must have varied VINTAGE ONLY. An earlier version averaged
        #     every rent-carrying community in the radius and called the difference the
        #     vintage spread; on this deal 11 of the 28 extra communities differed by
        #     distance cap or product instead, so the label was wrong by construction.
        if (
            k.get("spread_matched") is not None
            and k.get("spread_unrestricted") is not None
            and subject_year
            and years
            and (c.get("unrestricted_held_fixed") or [])
        ):
            band = tiering.get("year_band")
            matched, off = k["spread_matched"], k["spread_unrestricted"]

            def phrase(x):
                return f"{abs(x):.1%} {'above' if x >= 0 else 'below'}"

            # The membership wording is DERIVED, not templated. An earlier version said
            # "N of those communities are replaced" on every deal; on the fixture, where
            # the closest-N cap is not binding, one community was ADDED and none removed,
            # so the sentence was simply false. It also asserted the newcomers were
            # "newer" — false there too, since the one that joins is OLDER than set A's
            # whole range. Both now come from the swap itself.
            ins = list(c.get("unrestricted_swapped_in") or [])
            outs = list(c.get("unrestricted_swapped_out") or [])
            # The CONFIGURED cap, not the matched set's size. Using the latter made the
            # fixture say "fewer than 11 qualify either way" when the cap is 20 and set B
            # holds 12 — a sentence that described its own output rather than the rule.
            cap = int(tiering.get("top_n") or 0) + int(tiering.get("additional_n") or 0)
            if ins and outs and len(ins) == len(outs):
                membership = (
                    f"{len(ins)} of those communities are replaced by {len(outs)} others "
                    f"— the closest-{cap} cap binds, so one comes in for each that goes out"
                )
            elif ins and outs:
                membership = f"{len(ins)} join and {len(outs)} drop out"
            elif ins and not outs:
                membership = (
                    f"{len(ins)} more "
                    f"{'community joins' if len(ins) == 1 else 'communities join'} and "
                    f"none drops out — fewer than {cap} qualify either way, so the cap "
                    f"never binds"
                )
            elif outs and not ins:
                membership = (
                    f"{len(outs)} drop out and none joins"
                )
            else:
                membership = "membership is unchanged"

            # Direction of the newcomers' vintages, MEASURED against set A's own range.
            nearby_year = {
                n["community"]: n.get("year_built")
                for n in (deal.get("comps", {}).get("nearby") or [])
            }
            in_years = [y for y in (nearby_year.get(x) for x in ins) if isinstance(y, int)]
            lo, hi = min(years), max(years)
            if in_years and all(y > hi for y in in_years):
                direction = (
                    f"Every community that joins was built after {hi}, set A's newest, so "
                    f"set B reaches newer product than the vintage test allowed."
                )
            elif in_years and all(y < lo for y in in_years):
                direction = (
                    f"Every community that joins was built before {lo}, set A's oldest, "
                    f"so set B reaches OLDER product, not newer."
                )
            elif in_years:
                direction = (
                    f"The communities that join span {min(in_years)} to {max(in_years)}, "
                    f"on both sides of set A's {lo}-{hi} range."
                )
            elif ins:
                direction = (
                    "The communities that join carry no published vintage, so the "
                    "direction of the change cannot be stated."
                )
            else:
                direction = "No community joins set B, so there is no vintage shift to describe."

            detail = (
                f"This subject was built {subject_year}. "
                f"Two comp sets were selected by identical criteria — same candidate "
                f"pool, same product test, same tracker exclusions, same radius, same "
                f"closest-{cap} cap, same rent rules, same mix "
                f"weighting — differing only in whether the +/-{band}-year vintage test "
                f"was applied. Set A, vintage test ON: "
                f"{len(c['detailed_communities'])} communities, vintages {min(years)} to "
                f"{max(years)}; this subject's rent reads {phrase(matched)} it. Set B, "
                f"vintage test OFF: {membership} (vintages "
                f"{c.get('unrestricted_year_min')} to {c.get('unrestricted_year_max')}); "
                f"the same rent reads {phrase(off)} it. Both figures are weighted by this "
                f"property's own unit mix and computed over the same bed counts "
                f"({k['coverage']:.0%} of its units), so the two sets differ by "
                f"{abs(matched - off) * 100:.1f} points. {direction} Both sets are listed "
                f"on the Rent Comps section — this is a measurement of these two sets on "
                f"this date, not an estimate of what vintage is worth in this market."
            )
            out.append({
                "topic": "What vintage-matching changed on this deal",
                "applies_to": "Rent Comps — tiers",
                "note": detail,
            })
        elif k.get("withheld_reason"):
            out.append({
                "topic": "The vintage effect could not be isolated on this deal",
                "applies_to": "Rent Comps — tiers",
                "note": (
                    f"{k['withheld_reason']} No vintage figure is asserted here rather "
                    f"than one computed off populations that are not comparable."
                ),
            })
    rr = deal.get("rent_roll") or {}
    if rr.get("units"):
        totals = compute.roll_totals(deal)
        in_place = totals.get("wavg_in_place_rent")
        market = totals.get("wavg_market_rent_for_model")
        if in_place and market and market < in_place:
            out.append({
                "topic": "Market rent is BELOW in-place on this deal",
                "applies_to": "Rent Roll — Summary",
                "note": (
                    f"Unit-weighted in-place rent is {compute.fmt_currency(in_place)} and "
                    f"market rent for the model is {compute.fmt_currency(market)} — "
                    f"{compute.fmt_pct(market / in_place - 1)}. That is not an error. New "
                    f"leases are signing below what residents in place are paying, so the "
                    f"no-blend rule surfaces a downward mark the model has to carry. A "
                    f"blended new-and-renewal average would have hidden it."
                ),
            })
    return out


def for_deal(deal: dict | None = None, extra: list[dict] | None = None) -> list[dict]:
    """Standard methodology notes, then notes derived from THIS deal, then explicit extras.

    ``STANDARD`` is deal-independent by contract — it describes how the pipeline works and
    must contain no figure, vintage or spread belonging to any particular property. Anything
    that quotes a number comes from :func:`derived_for` or from ``extra``.
    """
    out = [dict(n) for n in STANDARD]
    if deal is not None:
        out += derived_for(deal)
        block = deal.get("om_comps") if isinstance(deal.get("om_comps"), dict) else None
        if block and (block.get("rent") or block.get("sale")):
            out.append({
                "topic": "The memorandum's own comparables are shown apart",
                "applies_to": "Rent Comps",
                "note": schema.OM_COMPS_APART_NOTE,
            })
    return out + [dict(n) for n in (extra or [])]
