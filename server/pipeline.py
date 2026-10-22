"""Run the whole pipeline offline: raw deal documents in, workbooks and previews out.

    python -m server.pipeline sample/inputs --out .tmp/run

Stages, in order (the hosted demo runs the same stages behind an upload page):

1. **intake**: size and type gate on every file (``server/intake.py``)
2. **parse**: each file in a sandboxed subprocess (``server/parse_worker.py``)
3. **classify**: which file is the memorandum, the T-12, the rent roll (``server/classify.py``)
4. **extract**: memorandum fields with verbatim quotes, the T-12 line-to-category mapping,
   the rent-roll column profile (``server/extract_llm.py``; mock mode by default, so no
   API key and no spend)
5. **assemble**: one interchange dict, validated against ``tools/schema.py``
6. **policy**: public-versus-internal decisions (``tools/policy.py``)
7. **write**: the workbooks and the HTML previews
8. **scrub**: the publishing check over everything written (``tools/scrub_check.py``)

The market blocks (comps, demand, supply) are written absent with a reason; see
``server/market.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import server  # noqa: F401  (sets OPENPYXL_LXML=False before openpyxl is imported)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.append(str(TOOLS_DIR))

import source_readers as raw_mod  # noqa: E402
import policy  # noqa: E402
import render_previews  # noqa: E402
import schema  # noqa: E402
import scrub_check  # noqa: E402
import write_deal_summary  # noqa: E402
import write_manifest  # noqa: E402
import write_pack  # noqa: E402
import write_rent_roll  # noqa: E402
import write_t12_analysis  # noqa: E402

from server import assemble as assemble_mod  # noqa: E402
from server import classify as classify_mod  # noqa: E402
from server import extract_llm as model  # noqa: E402
from server import intake as intake_mod  # noqa: E402
from server import market as market_mod  # noqa: E402
from server import parse_worker  # noqa: E402

#: Which writer produces which workbook.
WORKBOOK_WRITERS = {
    "pack": lambda deal, path: write_pack.build(deal, path),
    "deal_summary": lambda deal, path: write_deal_summary.build(deal, path),
    "t12_analysis": lambda deal, path: write_t12_analysis.build(deal, path),
    "rent_roll": lambda deal, path: write_rent_roll.build(deal, path),
    "manifest": lambda deal, path: write_manifest.build(path, deal),
}

FILENAMES: dict[str, str] = {
    "pack": "underwriting-pack.xlsx",
    "deal_summary": "deal-summary.xlsx",
    "t12_analysis": "t12-analysis.xlsx",
    "rent_roll": "rent-roll-analysis.xlsx",
    "manifest": "model-ready-outputs.xlsx",
    "interchange": "deal-outputs.json",
}


class PipelineError(RuntimeError):
    """A stage refused; the message says which and why."""


def _read_inputs(paths: list[Path]) -> list[tuple[str, bytes]]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(q for q in p.iterdir() if q.is_file() and not q.name.startswith(".")))
        else:
            files.append(p)
    return [(f.name, f.read_bytes()) for f in files]


def extract(documents: list[dict], classified, ledger) -> dict:
    """Stage 4: the model layer over each classified document."""
    out: dict = {"om_fields": {}, "om_pricing": None, "om_comps": None,
                 "om_comps_failure": None, "t12_mapping": None, "roll_mapping": None}
    for document, item in zip(documents, classified):
        document["role"] = item.role
        document["note"] = item.reason
        if item.role == "om" and document.get("text"):
            extracted = model.om_summary(document["text"], ledger)
            out["om_fields"] = extracted["fields"]
            out["om_pricing"] = extracted.get("pricing")
            try:
                out["om_comps"] = model.om_comps(document["text"], ledger)
            except Exception as exc:  # noqa: BLE001 - optional block; the reason travels
                out["om_comps_failure"] = (
                    f"the memorandum's comparables could not be read ({type(exc).__name__})"
                )
        elif item.role in ("t12", "rent_roll"):
            sheet_index = item.signals.sheet_index or 0
            sheets = document.get("sheets") or []
            rows = sheets[sheet_index]["rows"] if sheet_index < len(sheets) else []
            header_index = item.signals.header_row_index or 0
            header = rows[header_index] if header_index < len(rows) else []
            if item.role == "t12":
                code_col, label_col = classify_mod.detect_t12_columns(rows, header_index)
                labels = raw_mod.labels_for_mapping(rows, header_index, label_col, code_col)
                mapping = model.map_t12(labels, [str(h) for h in header], ledger)
                mapping.update({"header_row_index": header_index, "sheet_index": sheet_index,
                                "code_col": code_col, "label_col": label_col})
                out["t12_mapping"] = mapping
            else:
                mapping = model.map_rent_roll(
                    [str(h) for h in header], rows[header_index + 1:], ledger
                )
                mapping.update({"header_row_index": header_index, "sheet_index": sheet_index})
                out["roll_mapping"] = mapping
    return out


def run(
    inputs: list[Path],
    out_dir: Path,
    *,
    notes: str = "",
    price_per_unit: float | None = None,
    generated_on: str | None = None,
    policy_mode: str = "public",
) -> dict:
    """Run every stage; return ``{"deal": ..., "written": [...], "ledger": {...}}``."""
    started = time.time()
    payload = _read_inputs(inputs)
    accepted = intake_mod.accept_batch(payload, notes)

    documents: list[dict] = []
    scratch = parse_worker.raw_scratch("upload")
    try:
        for index, ((filename, data), meta) in enumerate(zip(payload, accepted)):
            target = scratch / f"{index}.{meta.kind}"
            target.write_bytes(data)
            if meta.kind == "pdf":
                report = parse_worker.parse_file(target, "probe_pdf", filename)["stats"]
                intake_mod.check_pdf_report(report, filename)
            documents.append(parse_worker.parse_file(target, meta.kind, filename))
            target.unlink(missing_ok=True)
    finally:
        parse_worker.remove_raw_scratch(scratch)

    ledger = model.UsageLedger()
    notes_read = model.parse_notes(notes, [d.get("filename", "") for d in documents], ledger)
    classified = classify_mod.classify_batch(
        documents, ledger=ledger, role_hints=notes_read.get("role_hints")
    )
    extracted = extract(documents, classified, ledger)

    om_doc = next((d for d in documents if d.get("role") == "om"), None)
    deal, _notes = assemble_mod.assemble(
        documents=documents,
        om_fields=extracted["om_fields"],
        om_pricing=extracted["om_pricing"],
        t12_mapping=extracted["t12_mapping"],
        roll_mapping=extracted["roll_mapping"],
        notes_values=notes_read.get("values"),
        generated_on=generated_on,
        market_failure=None if om_doc else market_mod.NO_OM_REASON,
        om_comps=extracted["om_comps"],
        om_comps_failure=extracted["om_comps_failure"],
        form_values=({"price_per_unit": price_per_unit} if price_per_unit is not None else None),
    )
    totals = ledger.as_dict()
    deal["meta"]["run"] = {
        "kind": "upload",
        "model_calls": int(totals.get("model_calls") or 0),
        "input_tokens": int(totals.get("input_tokens") or 0),
        "output_tokens": int(totals.get("output_tokens") or 0),
        "cache_read_tokens": int(totals.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(totals.get("cache_write_tokens") or 0),
        "dollars": round(float(totals.get("dollars") or 0.0), 4),
        "seconds": 0.0,
        "models": {"extraction": model.EXTRACT_MODEL, "classification": model.CLASSIFY_MODEL},
        "note": (f"Model mode: {model.mode()}. Every number above is the run's own ledger. "
                 "Generation of the workbooks and previews makes no model calls."),
    }
    deal, _policy_log = policy.apply_upload(deal, policy_mode)
    errors = schema.validate(deal)
    if errors:
        raise PipelineError("the assembled deal failed validation: " + "; ".join(errors[:5]))

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / FILENAMES["interchange"]).write_text(
        json.dumps(deal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    written = [FILENAMES["interchange"]]
    for key in assemble_mod.applicable_workbooks(deal):
        WORKBOOK_WRITERS[key](deal, out_dir / FILENAMES[key])
        written.append(FILENAMES[key])
    render_previews.render_all(deal, out_dir / "previews")

    findings = scrub_check.run([out_dir], scrub_check.load_terms([]))
    if findings:
        raise PipelineError(f"the publishing check found {len(findings)} term(s) in the outputs")
    return {"deal": deal, "written": written, "ledger": totals,
            "seconds": round(time.time() - started, 2)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("inputs", nargs="+", type=Path, help="files, or a directory of files")
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--notes", default="", help="free-text notes, as the upload form takes them")
    ap.add_argument("--price-per-unit", type=float, default=None,
                    help="illustrative price per unit (drives the cap-rate ladders)")
    ap.add_argument("--generated-on", default=None, help="pin the generated date (YYYY-MM-DD)")
    a = ap.parse_args(argv)
    try:
        result = run(a.inputs, a.out, notes=a.notes, price_per_unit=a.price_per_unit,
                     generated_on=a.generated_on)
    except (PipelineError, parse_worker.ParseFailure) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    deal = result["deal"]
    print(f"deal: {deal['meta']['deal_name']}  (mode: {model.mode()}, "
          f"{result['ledger'].get('model_calls', 0)} model call(s), "
          f"${float(result['ledger'].get('dollars') or 0):.2f})")
    for name in result["written"]:
        print(f"  wrote {a.out / name}")
    print(f"  wrote {a.out / 'previews'}/ ({len(list((a.out / 'previews').glob('*.html')))} fragments)")
    print("scrub check: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
