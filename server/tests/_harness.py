"""Shared wiring for the server test scripts.

Same convention as ``tools/test_*.py``: each module is a script that prints its checks,
ends with ``ALL CHECKS PASS``, and exits 1 if anything failed (``tests/test_suites.py``
runs every one of them under pytest). This module only puts the repo and ``tools/`` on
``sys.path``.
"""

from __future__ import annotations

import os

# server/__init__.py pins openpyxl to ElementTree. A test that builds a workbook with
# openpyxl BEFORE it imports anything from ``server`` would bind lxml first, so the pin is
# repeated here, before any other import.
os.environ.setdefault("OPENPYXL_LXML", "False")

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.append(_p)

from _testkit import Checker, guard  # noqa: E402  (path set above)

__all__ = ["Checker", "guard", "REPO_ROOT", "TOOLS_DIR"]
