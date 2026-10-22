"""Tiny check harness for the test scripts.

No pytest: each test file is a script that prints its checks, ends with
``ALL CHECKS PASS``, and exits 1 if anything failed. That convention matters more than
the framework — a suite that can print a pass banner without having run anything is worse
than no suite, so :meth:`Checker.finish` refuses to pass when zero checks ran.
"""

from __future__ import annotations

import traceback
from typing import Any


class Checker:
    def __init__(self, name: str) -> None:
        self.name = name
        self.passed = 0
        self.failures: list[str] = []
        print(f"=== {name} ===")

    def ok(self, label: str, condition: Any, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
            return True
        self.failures.append(f"{label}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")
        return False

    def equal(self, label: str, actual: Any, expected: Any) -> bool:
        return self.ok(
            label, actual == expected, f"expected {expected!r}, got {actual!r}"
        )

    def close(self, label: str, actual: Any, expected: Any, tol: float = 0.01) -> bool:
        try:
            good = abs(float(actual) - float(expected)) <= tol
        except (TypeError, ValueError):
            return self.ok(label, False, f"not numeric: {actual!r} vs {expected!r}")
        return self.ok(
            label, good, f"expected {expected!r} ± {tol}, got {actual!r}"
        )

    def raises(self, label: str, fn, exc: type[BaseException] = Exception) -> bool:
        try:
            fn()
        except exc:
            return self.ok(label, True)
        except BaseException as other:  # noqa: BLE001
            return self.ok(label, False, f"raised {type(other).__name__}, wanted {exc.__name__}")
        return self.ok(label, False, "did not raise")

    def section(self, title: str) -> None:
        print(f"-- {title}")

    def finish(self) -> int:
        print()
        if not self.passed and not self.failures:
            print(f"{self.name}: NO CHECKS RAN — treating as failure")
            return 1
        if self.failures:
            print(f"{self.name}: {len(self.failures)} FAILURE(S) of {self.passed + len(self.failures)} checks")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print(f"{self.name}: {self.passed} checks")
        print("ALL CHECKS PASS")
        return 0


def guard(main_fn) -> int:
    """Run ``main_fn``; an unexpected exception is a failure, not a traceback-and-zero."""
    try:
        return main_fn()
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        print("\nUNCAUGHT EXCEPTION — treating as failure")
        return 1
