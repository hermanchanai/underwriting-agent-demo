"""The publish gate (I1). Nothing reaches a public surface unless this exits 0.

Scans a file or directory tree for identifiers that must never appear on a public
surface — employer and brand tokens, work-account names, local absolute paths, and any
extra terms a specific deal contributes — and reports every hit with the file, the exact
location, and which term matched.

Coverage per file type:

* ``.xlsx`` — every cell on every sheet (values *and* formula strings), sheet names,
  defined names, and the workbook's core properties, including ``creator`` and
  ``lastModifiedBy``. Authorship metadata is a real leak channel and is invisible on
  screen, which is exactly why it gets checked.
* ``.html`` / ``.json`` / ``.md`` / ``.txt`` / ``.csv`` — full text.
* every path — the file and directory names themselves.

Matching is case-insensitive substring, because a gate should over-report rather than
under-report: a false positive costs a rename, a false negative costs an incident. The one
concession is :data:`BENIGN_CONTEXTS`, which lets ordinary English words that happen to
contain a token through, and is deliberately short and explicit.

The default terms below are placeholders; see :data:`BRAND_TERMS`.

Usage:
    python tools/scrub_check.py outputs/fixture
    python tools/scrub_check.py <paths...> --terms inputs/<deal>/scrub_terms.txt
    python tools/scrub_check.py <paths...> --quiet     # exit code only
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {".html", ".htm", ".json", ".md", ".txt", ".csv", ".js", ".css", ".svg"}
XLSX_SUFFIXES = {".xlsx", ".xlsm"}

#: Employer, brand, and account identifiers. The shipped list is PLACEHOLDERS: a real
#: deployment adds its own employer, account and person names from a private terms file
#: (``--terms``) that is never committed, because a public denylist of your employer's
#: names is itself a leak.
BRAND_TERMS: tuple[str, ...] = (
    "acme",
    "acmerealty",
    "jdoe@",
)

#: Local filesystem paths. A published file that leaks a path leaks the machine layout.
PATH_TERMS: tuple[str, ...] = (
    "/users/",
    "/home/",
    "c:\\users\\",
    "/library/cloudstorage",
)

DEFAULT_TERMS: tuple[str, ...] = BRAND_TERMS + PATH_TERMS

#: Ordinary strings that contain a token but are not a leak. Kept short on purpose: every
#: entry here is a hole in the gate, so each one has to be worth it.
BENIGN_CONTEXTS: tuple[str, ...] = (
    "the acme of",
)


@dataclass(frozen=True)
class Finding:
    path: str
    location: str
    term: str
    excerpt: str

    def render(self) -> str:
        return f"{self.path} :: {self.location} :: matched {self.term!r} :: {self.excerpt}"


def _excerpt(text: str, index: int, term_len: int, width: int = 48) -> str:
    start = max(0, index - width // 2)
    end = min(len(text), index + term_len + width // 2)
    snippet = text[start:end].replace("\n", " ").replace("\r", " ")
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def _scan_text(text: str, terms: tuple[str, ...]) -> list[tuple[str, int, str]]:
    """Return (term, index, excerpt) for every hit that is not a benign context."""
    lowered = text.lower()
    hits: list[tuple[str, int, str]] = []
    for term in terms:
        needle = term.lower()
        start = 0
        while True:
            idx = lowered.find(needle, start)
            if idx == -1:
                break
            start = idx + 1
            if _is_benign(lowered, idx, len(needle)):
                continue
            hits.append((term, idx, _excerpt(text, idx, len(needle))))
    return hits


def has_hit(text: str, terms: tuple[str, ...]) -> bool:
    """Would the gate refuse this string? The one public test of a value BEFORE it is written
    (the market mapper withholds a community identity that would fail the run)."""
    return bool(_scan_text(text or "", terms))


def _is_benign(lowered: str, idx: int, length: int) -> bool:
    for benign in BENIGN_CONTEXTS:
        b = benign.lower()
        window_start = max(0, idx - len(b))
        window = lowered[window_start : idx + length + len(b)]
        if b in window:
            # The match sits inside a known-benign word.
            offset = window.find(b)
            b_start = window_start + offset
            if b_start <= idx and idx + length <= b_start + len(b):
                return True
    return False


def _relative_name(path: Path, root: Path | None) -> str:
    """The part of the path that would travel with the file.

    Only the file's own name and the directories *below the scan root* can leak: the
    absolute prefix above the root is the operator's own machine layout, and it is not
    part of what gets published. Scanning the prefix would make the gate report a hit on
    every single file whenever the project itself happens to live under a denylisted
    path, which is a gate that cannot ever go green — the worst failure mode there is.
    """
    if root is None:
        return path.name
    try:
        return str(path.relative_to(root if root.is_dir() else root.parent))
    except ValueError:
        return path.name


def scan_path_name(
    path: Path, terms: tuple[str, ...], root: Path | None = None
) -> list[Finding]:
    name = _relative_name(path, root)
    findings = []
    for term, _idx, _ex in _scan_text(name, terms):
        findings.append(Finding(str(path), "file path", term, name))
    return findings


def scan_text_file(path: Path, terms: tuple[str, ...]) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover
        return [Finding(str(path), "read error", "-", str(exc))]
    findings = []
    for term, idx, excerpt in _scan_text(text, terms):
        line = text.count("\n", 0, idx) + 1
        findings.append(Finding(str(path), f"line {line}", term, excerpt))
    return findings


def scan_xlsx(path: Path, terms: tuple[str, ...]) -> list[Finding]:
    from openpyxl import load_workbook

    findings: list[Finding] = []
    try:
        wb = load_workbook(path, data_only=False)
    except Exception as exc:  # pragma: no cover
        return [Finding(str(path), "open error", "-", repr(exc))]

    # Core properties: invisible on screen, and a real leak channel.
    props = wb.properties
    for attr in (
        "creator",
        "lastModifiedBy",
        "title",
        "subject",
        "description",
        "keywords",
        "category",
        "company",
        "manager",
    ):
        value = getattr(props, attr, None)
        if not isinstance(value, str):
            continue
        for term, idx, excerpt in _scan_text(value, terms):
            findings.append(
                Finding(str(path), f"document property '{attr}'", term, excerpt)
            )

    for name in wb.sheetnames:
        for term, idx, excerpt in _scan_text(name, terms):
            findings.append(Finding(str(path), f"sheet name {name!r}", term, excerpt))

    try:
        defined = list(wb.defined_names.keys())
    except Exception:  # pragma: no cover - openpyxl version differences
        defined = []
    for dn in defined:
        for term, idx, excerpt in _scan_text(str(dn), terms):
            findings.append(Finding(str(path), f"defined name {dn!r}", term, excerpt))

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                text = str(cell.value)
                for term, idx, excerpt in _scan_text(text, terms):
                    findings.append(
                        Finding(
                            str(path),
                            f"sheet {ws.title!r} cell {cell.coordinate}",
                            term,
                            excerpt,
                        )
                    )
    return findings


def scan_file(
    path: Path, terms: tuple[str, ...], root: Path | None = None
) -> list[Finding]:
    findings = scan_path_name(path, terms, root)
    suffix = path.suffix.lower()
    if suffix in XLSX_SUFFIXES:
        findings.extend(scan_xlsx(path, terms))
    elif suffix in TEXT_SUFFIXES:
        findings.extend(scan_text_file(path, terms))
    return findings


#: Directory names skipped during a *directory* walk. These hold verification evidence —
#: captured red runs, extractor reports, run logs — which by construction quote the very
#: terms the gate exists to find. Scanning them would make a passing gate impossible.
#: This is the only hole in the gate, and it cannot hide a real leak on the publish path:
#: ``publish_to_site.py`` scans an explicit list of staged files, never a directory, so
#: every artifact that actually travels is scanned by name.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({"qa", "run_logs"})


def iter_files(targets: list[Path]) -> list[Path]:
    out: list[Path] = []
    for target in targets:
        if target.is_dir():
            for path in sorted(target.rglob("*")):
                if not path.is_file() or path.name.startswith("~$"):
                    continue
                rel_parts = path.relative_to(target).parts[:-1]
                if any(part in EXCLUDED_DIR_NAMES for part in rel_parts):
                    continue
                out.append(path)
        elif target.is_file():
            # An explicitly named file is always scanned, even inside an excluded dir.
            out.append(target)
    return out


def load_terms(extra_files: list[Path]) -> tuple[str, ...]:
    terms = list(DEFAULT_TERMS)
    for path in extra_files:
        if not path.exists():
            raise FileNotFoundError(f"terms file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    # Longest first so the most specific term is reported when two overlap.
    return tuple(sorted(set(terms), key=len, reverse=True))


def run(targets: list[Path], terms: tuple[str, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for target in targets:
        for path in iter_files([target]):
            findings.extend(scan_file(path, terms, root=target))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("targets", nargs="+", type=Path)
    ap.add_argument(
        "--terms",
        action="append",
        default=[],
        type=Path,
        help="extra denylist file, one term per line ('#' comments allowed)",
    )
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)

    terms = load_terms(args.terms)
    files = iter_files(args.targets)
    findings = run(args.targets, terms)

    if args.as_json:
        print(
            json.dumps(
                {
                    "files_scanned": len(files),
                    "terms": list(terms),
                    "findings": [f.__dict__ for f in findings],
                },
                indent=2,
            )
        )
    elif not args.quiet:
        print(f"scrub_check: {len(files)} file(s), {len(terms)} term(s)")
        for path in files:
            print(f"  scanned {path}")
        if findings:
            print(f"\nFAIL — {len(findings)} finding(s):")
            for f in findings:
                print(f"  {f.render()}")
        else:
            print("\nCLEAN — no denylisted term found")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
