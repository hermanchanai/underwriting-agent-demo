#!/usr/bin/env python3
"""The engine must import and run where there is no ``resource`` module.

The engine also runs on Windows, where ``import resource`` (POSIX only) would fail before
the parser could start. Rlimits are a Linux/macOS control; on Windows the wall-clock timeout is
the only backstop, and the parse worker has to say so rather than refuse to exist.

Pins (in a subprocess whose ``resource`` import is forced to fail):
  - ``server.parse_worker`` imports
  - ``apply_limits()`` reports both limits NOT applied and names the platform
  - ``current_limits()`` reads back [-1, -1]
  - ``kill_process_group`` returns False when ``os`` has no ``killpg`` (the caller's
    single-process kill is what happens), instead of raising AttributeError

Usage: python server/tests/test_windows_import.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import Checker, guard  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
import json, os, sys
sys.modules["resource"] = None          # an ImportError on `import resource`, as on Windows
sys.modules["pwd"] = None               # and on `import pwd` — the parser-account probe runs at import
os.environ["OPENPYXL_LXML"] = "False"
out = {}
try:
    from server import parse_worker as pw
    out["imported"] = True
    out["apply"] = pw.apply_limits()
    out["current"] = pw.current_limits()
    for name in ("killpg", "getpgid"):
        if hasattr(os, name):
            delattr(os, name)
    class P:  # a child that is not ours to inspect
        pid = 424242
    out["kill"] = pw.kill_process_group(P())
except Exception as e:  # noqa: BLE001
    out["imported"] = False
    out["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
'''


def main() -> int:
    c = Checker("test_windows_import")
    r = subprocess.run([sys.executable, "-c", PROBE], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        out = {"imported": False, "error": (r.stderr or r.stdout)[-600:]}
    c.ok("server.parse_worker imports without the resource module", out.get("imported") is True, str(out.get("error"))[:300])
    if not out.get("imported"):
        return c.finish()
    a = out.get("apply") or {}
    c.equal("apply_limits: CPU limit not applied", a.get("cpu_limit_applied"), False)
    c.equal("apply_limits: memory limit not applied", a.get("memory_limit_applied"), False)
    c.ok("apply_limits names the platform as having no rlimits", "rlimits_unavailable" in a, str(a))
    cur = out.get("current") or {}
    c.equal("current_limits reads back nothing rather than raising", (cur.get("rlimit_cpu"), cur.get("rlimit_as")), ([-1, -1], [-1, -1]))
    c.equal("kill_process_group falls back (False) when os has no killpg", out.get("kill"), False)
    return c.finish()


if __name__ == "__main__":
    raise SystemExit(guard(main))
