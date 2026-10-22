"""Every ``server/`` module has to parse under Python 3.11.

The venv here is 3.12 and the container is ``python:3.11-slim``. There is no 3.11
interpreter on this machine and no Homebrew to install one, so the runtime proof waits for
the container — but a 3.12-only construct is a *syntax* difference, and
``ast.parse(feature_version=(3, 11))`` catches those here, at the moment they are written,
instead of in a deploy log.

What this does NOT cover, stated so nobody reads a green here as more than it is: stdlib
functions added in 3.12, arguments added to existing ones, and any third-party wheel that
does not build on 3.11. Those need the container.

Usage: python server/tests/test_py311_compat.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, REPO_ROOT, guard

TARGET = (3, 11)

#: Names added to the stdlib in 3.12 that a 3.11 runtime would not have. Syntax parsing
#: cannot see these, so they are grepped for by name.
POST_311_IMPORTS: tuple[str, ...] = (
    "from itertools import batched",
    "itertools.batched",
    "from typing import override",
    "typing.override",
    "from typing import TypeAliasType",
    "datetime.UTC",
    "from datetime import UTC",
    "pathlib.Path.walk",
    ".is_junction(",
    "sys.monitoring",
)


def main() -> int:
    c = Checker("test_py311_compat")
    server_dir = REPO_ROOT / "server"
    modules = sorted(server_dir.rglob("*.py"))

    c.section("the file set")
    c.ok("server/ has modules to check", len(modules) >= 3, f"found {len(modules)}")

    c.section(f"parses under Python {TARGET[0]}.{TARGET[1]}")
    for path in modules:
        rel = path.relative_to(REPO_ROOT)
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path), feature_version=TARGET)
            c.ok(f"{rel}", True)
        except SyntaxError as exc:
            c.ok(f"{rel}", False, f"line {exc.lineno}: {exc.msg}")

    c.section("no stdlib names that only exist after 3.11")
    for path in modules:
        rel = path.relative_to(REPO_ROOT)
        source = path.read_text(encoding="utf-8")
        # This module names them all in POST_311_IMPORTS, so it cannot check itself.
        if path.name == Path(__file__).name:
            continue
        hits = [needle for needle in POST_311_IMPORTS if needle in source]
        c.ok(f"{rel} uses no post-3.11 stdlib name", not hits, f"found {hits}")

    c.section("the check can fail")
    # A guard nobody has watched refuse is not a guard: 3.12's PEP 695 type-parameter
    # syntax is a parse error on 3.11, so it must be reported as one here.
    only_312 = "def f[T](x: T) -> T: return x\n"
    try:
        ast.parse(only_312, feature_version=TARGET)
        rejected = False
    except SyntaxError:
        rejected = True
    c.ok("3.12-only syntax is rejected at the 3.11 feature level", rejected)
    c.ok(
        "and the same source parses at this interpreter's own level",
        _parses(only_312, sys.version_info[:2]) or sys.version_info < (3, 12),
        f"running {sys.version_info[0]}.{sys.version_info[1]}",
    )

    return c.finish()


def _parses(source: str, version: tuple[int, int]) -> bool:
    try:
        ast.parse(source, feature_version=version)
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    raise SystemExit(guard(main))
