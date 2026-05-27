"""Refactor-enforcement test for Phase N2.

This test pins the migration of `tests/e2e/test_plan_2_sustained_stress.py`
from its private `_EventLoopLagSampler` shim to the public
`EventLoopLagSampler` exported by `easycat.validation.reliability`.

It lives in `tests/validation/` (next to `test_reliability.py`) because the
concern is "the stress test consumes the public validation surface" rather
than CI workflow plumbing (which is what `tests/test_ci_workflow.py` covers).

The behavior of the public sampler itself is exercised in
`tests/validation/test_reliability.py`; this file only guards against a
future contributor re-introducing a private inline copy.
"""

from __future__ import annotations

import ast
from pathlib import Path

STRESS_TEST_PATH = (
    Path(__file__).resolve().parent.parent / "e2e" / "test_plan_2_sustained_stress.py"
)


def _parse_stress_module() -> ast.Module:
    source = STRESS_TEST_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(STRESS_TEST_PATH))


def test_stress_test_does_not_define_private_event_loop_lag_sampler() -> None:
    """The stress test must not redefine an inline event-loop-lag sampler.

    Catches the literal `_EventLoopLagSampler` shim as well as a rename
    (e.g. `_LagSampler`, `_LoopLagProbe`) that exposes the same async
    `start`/`stop` shape — anything that looks like a private copy of the
    public `EventLoopLagSampler` should be flagged.
    """
    module = _parse_stress_module()
    offenders: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.ClassDef):
            continue
        method_names = {
            child.name for child in node.body if isinstance(child, ast.AsyncFunctionDef)
        }
        if {"start", "stop"} <= method_names:
            offenders.append(node.name)
    assert not offenders, (
        f"tests/e2e/test_plan_2_sustained_stress.py defines inline async "
        f"start/stop sampler-shaped classes ({offenders!r}); import "
        "EventLoopLagSampler from easycat.validation.reliability instead "
        "of redefining the shape locally."
    )


def test_stress_test_imports_public_event_loop_lag_sampler() -> None:
    """The stress test must import `EventLoopLagSampler` from the public module."""
    module = _parse_stress_module()
    imported_from_reliability: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "easycat.validation.reliability",
            "easycat.validation",
        }:
            imported_from_reliability.update(alias.name for alias in node.names)
    assert "EventLoopLagSampler" in imported_from_reliability, (
        "tests/e2e/test_plan_2_sustained_stress.py must import "
        "EventLoopLagSampler from easycat.validation.reliability "
        "(or easycat.validation) instead of defining its own private copy."
    )
