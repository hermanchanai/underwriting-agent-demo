"""Run every check script in the repository under pytest.

The suites are plain scripts (``tools/test_*.py``, ``server/tests/test_*.py``,
``contract/test_chain_contract.py``): each prints its checks, ends with
``ALL CHECKS PASS`` and exits 1 on any failure. Each runs here in its own interpreter with
network sockets disabled, so a suite that reached for the network would fail rather than
quietly pass on a connected machine.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = sorted(
    [*REPO.glob("tools/test_*.py"), *REPO.glob("server/tests/test_*.py"), REPO / "contract" / "test_chain_contract.py"]
)

RUNNER = (
    "import runpy, sys, pytest_socket; "
    "pytest_socket.disable_socket(allow_unix_socket=True); "
    "path = sys.argv[1]; sys.argv = [path]; sys.path.insert(0, str(__import__('pathlib').Path(path).parent)); "
    "runpy.run_path(path, run_name='__main__')"
)


@pytest.mark.parametrize("script", SCRIPTS, ids=[str(p.relative_to(REPO)) for p in SCRIPTS])
def test_suite(script: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER, str(script)],
        cwd=REPO, capture_output=True, text=True, timeout=900,
        env={**__import__("os").environ, "OPENPYXL_LXML": "False"},
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
    assert proc.returncode == 0, f"{script.name} exited {proc.returncode}\n{tail}"
    assert "ALL CHECKS PASS" in proc.stdout, f"{script.name} did not report ALL CHECKS PASS\n{tail}"
