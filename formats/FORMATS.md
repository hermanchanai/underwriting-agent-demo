# FORMATS.md — layout spec for the four generic outputs

Toolkit name on every public surface: **AI Underwriting Pipeline — Sample Outputs**.

These layouts are this project's own. No employer template was copied, opened-and-saved-as, or
measured cell-by-cell. What carries over is the *information architecture* — which fields exist,
what the T-12 category vocabulary is, what a per-plan rent summary must contain — because that
is generic commercial-real-estate practice, not anyone's intellectual property. The geometry
below is deliberately different from any template in production use: the deal summary is a
single vertical sectioned card rather than a two-column grid, and the T-12 is split across three
tabs rather than living on one analysis tab.

Everything in every output comes from one `deal_outputs.json` (schema: `tools/schema.py`).
No writer holds a deal value.

Common conventions across all four workbooks:

- One header block at the top of every sheet: sheet title (row 1), the deal name and as-of date
  (row 2), a thin rule (row 3). Data starts at row 5 so a reader always finds the same offset.
- **Every block is located by its LABEL, never by a hardcoded row.** Writers record the row they
  wrote each label on and emit formulas against those recorded rows; tests read the file back by
  searching for the label. This is what makes the layout safe to change.
- Fonts: Calibri 11 body, Calibri 11 bold for labels and headers. No fill colors except a single
  neutral grey (`D9D9D9`) on header rows and a lighter grey (`F2F2F2`) on subtotal rows. No
  brand palette.
- Number formats: currency `#,##0`; per-unit currency `#,##0`; rent `#,##0`; percentages
  `0.0%`; cap rates `0.00%`; counts `#,##0`; dates `mm/dd/yyyy`; square feet `#,##0`.
- Negative expense/contra rows are stored as written in the source (a contra-revenue row such as
  Vacancy stays negative); no writer flips a sign.
- Formulas are emitted as strings for Excel to compute on open. `tools/compute.py` mirrors every
  one of them in Python, and that mirror is what the HTML previews and the tests use.
- Where a value is genuinely unknown, the cell says `MISSING` rather than a plausible guess. A
  visible hole beats a plausible-wrong number.
- Pricing-derived figures carry an `(illustrative)` marker whenever `meta.illustrative_pricing`
  is true.

File names (all four are downloadable from the demo page):

| Output | File |
|---|---|
| Deal summary | `sample-deal-summary.xlsx` |
| T-12 analysis | `sample-t12-analysis.xlsx` |
| Rent roll analysis | `sample-rent-roll-analysis.xlsx` |
| Model-ready outputs manifest | `sample-model-ready-outputs.xlsx` |

---

## 1. Deal summary — `sample-deal-summary.xlsx`

Four tabs. The first is the summary card; the other three are the market context the pipeline
attaches to it.

### Tab `Deal Summary`

A single vertical column of labelled sections, label in column A, value in column B, provenance
or note in column C. Sections in order, each introduced by a bold section bar:

1. **Identification** — deal name · street address · city · state · ZIP · county · MSA ·
   address-resolution provenance (how the address was resolved, and from which document).
2. **Property** — product type (SFD / TH / MF) · structure · unit count · year built ·
   product detail · construction/delivery status · certificate-of-occupancy or delivery date.
3. **Parties** — seller entity · listing brokerage (generic; never a firm or person name in a
   published file) · deal status.
4. **Pricing (illustrative)** — pricing guidance · price per unit · approximate project value ·
   T-12 cap rate · T-3 cap rate. Every row in this section carries the `(illustrative)` marker
   and column C names the assumption.
5. **Rents** — in-place rent (per-unit average, from the roll) · market rent for model ·
   fair market value estimate.
6. **Taxes** — tax rate as billed · assessed-value basis · assessment ratio · divisor ·
   exemption · computed annual tax · computed tax per unit. Column C carries the evidence
   string for the rate (what was read, from where). `MISSING` where the portal gave nothing.
7. **Schools** — elementary / middle / high: name and rating, one row each.
8. **Notes** — free text, and the standing disclaimer that pricing is illustrative and no
   licensed data source was used.

Formulas on this tab: price per unit `=B{price}/B{units}`, approximate project value
`=B{ppu}*B{units}`, cap rates `=IFERROR(NOI/value,"")` referencing the T-12 workbook's values
carried into the JSON, computed annual tax
`=IFERROR(B{basis}*B{ratio}/B{divisor}*B{rate}-B{exemption},"MISSING")`, tax per unit
`=IFERROR(B{tax}/B{units},"")`.

### Tab `Rent Comps`

Nearby rental communities from the operator's **own weekly rent scrape** — the only comp source
this project will ever use. Header row, then one row per floor plan per community:

`Community · Distance (mi) · Floor plan · Beds · Baths · SF · Asking rent · Concession ·
Net effective rent · Asking $/SF · Occupancy · As-of date · Source`

Then a subject-versus-comps block: subject market rent, comp-set average asking, comp-set
average net effective, and the spread as a percentage. Net effective is stated separately from
asking on every row — a concession-blind comp set is the single most common way a rent
assumption goes wrong. `Source` reads `own weekly scrape` on every row; a comp row from a
licensed feed cannot exist in this schema.

### Tab `Demand`

Compact demographics by drive-ring, one row per ring (1 / 3 / 5 / 10 mile) plus an MSA row:

`Ring · Population · Households · Median household income · Total jobs · Population growth
(5-yr) · Renter share`

Summary table only — the full demographic pull is a separate product and does not belong in a
sample workbook.

### Tab `Supply`

Competing pipeline supply by ring and stage, one row per ring/stage pair:

`Ring · Stage · Projects · Units`

Stages: `Entitlement` · `Permitted` · `Under construction` · `Delivered (24 mo)`. Then a total
row per ring, and a `Units as % of ring stock` figure where ring stock is known.

---

## 2. T-12 analysis — `sample-t12-analysis.xlsx`

Three tabs. The split is the layout: data, rollup, and checks each get their own surface.

### Tab `T-12 Data`

The monthly matrix as normalized, plus the two sum columns.

Columns: `Line item (A) · Category (B) · Jan..Dec — twelve month columns in the source's own
order (C..N) · T-12 total (O) · T-3 total (P)`.

- Month order is **direction-detected** from the month labels, not assumed. The T-3 column sums
  the three most recent months whichever end of the row they sit on; `compute.py` implements
  both directions and the tests exercise both.
- `Category` is one of the vendored category labels (`tools/schema.py`), or blank for a source
  subtotal line the pipeline chose not to categorize. An uncategorized line is visible, not
  dropped.
- `T-12 total` is `=SUM(C{r}:N{r})`. `T-3 total` is a `SUM` over the three most-recent month
  columns — a contiguous range on whichever side the direction detector found.

### Tab `P&L Summary`

The rollup, aggregated from `T-12 Data` by category with `SUMIF`, so the arithmetic is auditable
in the workbook rather than baked.

Columns: `Line item (A) · T-12 (B) · T-3 annualized (C) · Per unit — T-12 (D) · % of revenue (E)`.

Row order — revenue block, in this exact sequence:

`Gross Potential Rent · Gain/(Loss) to Lease · Gross Scheduled Rent · Vacancy · Concessions ·
Non-Revenue Units · Delinquency · Utility Reimbursements · Other Income · Revenue Adjustments ·
Total Revenue`

Then the expense block:

`G&A · Leasing and Marketing · Payroll · Insurance · Property Taxes · Legal and Professional ·
Management Fee · Landscaping · Repairs and Maintenance · Unit Turns · Utilities ·
Expense Adjustments · Total Operating Expenses`

Then `Net Operating Income`.

Formula rules, which are the substance of this tab:

- Each category row is `=SUMIF('T-12 Data'!$B:$B,$A{r},'T-12 Data'!O:O)` for T-12 and the same
  over column P, times 4, for T-3 annualized.
- **Gross Scheduled Rent is a derived subtotal with a fallback**:
  `=IF(SUMIF(...)<>0,SUMIF(...),B{gpr}+B{gll})`. Many statements carry GSR as its own line;
  many others expect it to be GPR plus loss-to-lease. Both are handled, and which one fired is
  visible in the formula.
- **Total Revenue sums from Gross Scheduled Rent down to Revenue Adjustments** —
  `=SUM(B{gsr}:B{revadj})` — never from Gross Potential Rent, which would double-count the
  loss-to-lease line. This is the one arithmetic trap in the whole layout that a reader should
  be able to check by eye.
- `Total Operating Expenses` is `=SUM(B{first_exp}:B{last_exp})`.
- `Net Operating Income` is `=B{total_rev}-B{total_opex}`.
- Per unit is `=IFERROR(B{r}/{units},"")` — the blank guard matters because a zero unit count
  from a bad extraction would otherwise fill the column with `#DIV/0!`.
- `% of revenue` is `=IFERROR(B{r}/B{total_rev},"")`.

Then three **Top-4-plus-Rest** tables — `Other Income`, `Utility Reimbursements`, `Utilities` —
each listing the four largest constituent line items by T-12 total, a `Rest` row summing the
remainder, and a total row that ties to the corresponding P&L category row.

### Tab `Checks & Pricing`

Two blocks.

**Error Check** — cross-ties this project's `SUMIF` totals against the subtotals printed on the
source statement. One row per checked subtotal: `Check · Our total · Source subtotal ·
Difference · Status`. `Status` is `=IF(ABS(D{r})<=1,"OK","REVIEW")` — a dollar of tolerance for
rounding, and anything else says REVIEW rather than quietly passing.

**Pricing & Cap Rate (illustrative)** — inputs then ladders:

- Inputs: `Price per unit` · `Unit count` · `Total price` (`=B{ppu}*B{units}`).
- T-12 ladder: revenue · operating expenses · NOI · cap rate (`=IFERROR(NOI/price,"")`).
- T-3 annualized ladder: the same four rows off the annualized T-3 column.
- Optional broker pro-forma ladder, present only when the source supplied one.
- A trailing-6 annualized trend row for context: the T-6 sum times two, beside T-12 and T-3, so
  a reader can see whether the trailing trend is up or down.

Every row in this block carries the `(illustrative)` marker.

---

## 3. Rent roll analysis — `sample-rent-roll-analysis.xlsx`

Three tabs.

### Tab `Rent Roll`

The normalized per-unit roll. Columns:

`Unit ID · Floor plan · Beds · Baths · SF · Status · Lease start · Move-in · Lease type ·
In-place rent · Rent per SF`

- **Unit IDs only. No tenant name column exists in this schema**, so there is nothing for a
  scrub step to miss.
- `Lease type` is `New`, `Renewal`, or blank. Blank means the source did not carry enough
  information to classify the lease — never a guess, and never a blended value.
- `New` versus `Renewal` is decided by comparing lease start against move-in within a stated
  tolerance (`new_tenant_tolerance_days`), which is written onto the summary tab as a knob, not
  hidden in code.
- `Rent per SF` is `=IFERROR(J{r}/E{r},"")`.
- A text value in a date column is refused by the writer rather than coerced — a date that
  arrives as the string `n/a` reads as `n/a` in the output, not as 1899-12-31.

### Tab `Unit Mix Summary`

Knobs first, in a small block at the top so the reader knows what produced the numbers below:
`As-of date` · `Lookback months` · `New-tenant tolerance (days)`. **Every window is measured
from the roll's as-of date, never from today** — a summary that silently re-windows when reopened
next quarter is worse than no summary.

Then the per-plan table, one row per floor plan:

`Bedroom · Floor plan · Avg SF · Unit count · In-place rent · LEASES SIGNED rent · Signed # ·
NEW MOVE-IN rent · Move-In # · Market rent for model · Basis` (columns A–K — the internal rent-roll
tool's Summary vocabulary)

- `Market rent for model` is the NEW MOVE-IN rent — residents whose move-in date falls inside
  the lookback window, whatever their lease type — when the plan has one, otherwise the
  in-place average; on a roll with no usable lease start or move-in date it is in-place
  outright. `Basis` states which fired, per row.
- `LEASES SIGNED` is every priced, classified lease start in the window (new, renewal or
  month-to-month); a lease the roll cannot classify is left out. It is context only: **a blend
  of new and renewal rents never reaches the model** — a blended "recent leased rent" is a
  renewal average wearing a market-rent label, and it understates trade-out on every deal it
  touches.
- A pair the roll cannot support reads the TEXT `n/a`, never a numeric 0; an empty window reads
  blank. The TOTAL row carries each pair's count-weighted average and summed count, or `n/a`.
- Averages are weighted: `=IFERROR(SUMPRODUCT(...)/SUM(...),"")` over the plan's unit rows.
- `Market rent for model` is the column a financial model consumes; the manifest says so.

Then a totals block: total units · occupied · vacant · occupancy `=IFERROR(occ/total,"")` ·
vacancy · mean in-place rent over the occupied units with a stated contract rent · weighted average market rent for model · annualized
turnover (move-ins inside the lookback, annualized against unit count).

### No trade-out tab

The demo never ingests a lease trade-out report (an internal-pipeline input), so the workbook
has no trade-out tab and the previews no trade-out fragment. The `trade_out` block stays
in the schema as absent by design: no rows, and an `absent_reason` that says why. An empty block
that says why it is empty is a deliverable; a block of plausible numbers is not.

**The same rule applies to `Rent Comps`, `Demand` and `Supply`.** Each carries an
`absent_reason`, and the schema refuses an empty block that does not say why it is empty.
Coverage is data-driven: where the operator's own weekly scrape has no rent observations for
a submarket, or a demographic pull was not run, the block states that rather than being
padded from a source this project excludes by design.

---

## 4. Model-ready outputs manifest — `sample-model-ready-outputs.xlsx`

The fourth deliverable, and the argument of the whole demo: the pipeline does not build a
financial model, because every shop already has one. It fills one.

Single tab `Model-Ready Outputs`. A cover block at the top:

- Toolkit name.
- The pitch, in one sentence: these are the values the pipeline extracts from a deal's raw
  documents and hands to *your* model.
- The honest boundary: comps come only from the operator's own weekly rent scrape; licensed
  data sources are excluded by design, so a field they would have filled reads `MISSING` rather
  than being sourced elsewhere.
- Field count, generated date, and a note that the Example column is a synthetic or illustrative
  deal.

Then the table, one row per field, generated from `schema.FIELD_INVENTORY` — never hand-typed:

`Group · Field · Type · Definition · Source document · Typically fills (client model input) ·
Example (demo deal)`

Group headers separate the sections (Deal identification, Property, Parties, Pricing, Rents,
Taxes, Schools, T-12 revenue, T-12 expense, T-12 derived, Rent roll unit, Rent roll plan, Rent
roll totals, Trade-out, Comps, Demand, Supply, Meta). `Example (demo deal)` is filled from a
deal JSON when one is supplied and reads `—` otherwise.

`Typically fills (client model input)` is the column that does the work: it names the input a
generic underwriting model would want that value in — unit mix and rents, the T-12 column of an
investment-analysis tab, tax inputs, the vacancy assumption, the growth evidence behind a
new-versus-renewal spread.

---

## HTML previews

`tools/render_previews.py` emits one self-contained fragment per tab:
`summary.html` · `comps.html` · `demand.html` · `supply.html` · `t12-data.html` ·
`t12-pnl.html` · `t12-checks-pricing.html` · `rent-roll.html` · `unit-mix.html` ·
`manifest.html`.

Rules (I5, I6): styling only via the site's CSS custom properties; no external assets and no
`http(s)://` anywhere in a fragment; every table wrapped in its own `overflow-x:auto` container;
each figure rendered from the same JSON key its xlsx cell came from, with `compute.py` supplying
any value the xlsx leaves to Excel; an `(illustrative)` chip beside every pricing-derived number.
