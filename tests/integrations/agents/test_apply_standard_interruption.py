"""Regression tests for the shared ``apply_standard_interruption`` helper.

Locks in the dedup from REFACTOR item #30: the four bridges whose interruption
behavior is a plain plan -> journal-protocol (langchain, langgraph,
llama_agents, openai_agents) delegate to the module-level helper instead of
inlining ``run_interruption_journal_protocol``.
"""

from __future__ import annotations

from pathlib import Path

from easycat.integrations.agents.base import (
    CancellationMode,
    InterruptionPlan,
    apply_standard_interruption,
)
from easycat.testing import RecordingAgentRecorder

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "src" / "easycat" / "integrations" / "agents"
_DELEGATING_BRIDGES = ("langchain", "langgraph", "llama_agents", "openai_agents")


def _apply_interruption_body(source: str) -> str:
    """Return the body of the ``apply_interruption`` method from ``source``."""
    marker = "def apply_interruption("
    start = source.index(marker)
    # Body ends at the next top-level ``def `` (4-space indent) after the sig.
    tail = source.index("):", start) + 2
    end = source.index("\n    def ", tail)
    return source[start:end]


def test_delegating_bridges_call_shared_helper() -> None:
    for name in _DELEGATING_BRIDGES:
        source = (_AGENTS_DIR / f"{name}.py").read_text()
        body = _apply_interruption_body(source)
        assert "apply_standard_interruption(" in body, name
        assert "run_interruption_journal_protocol(" not in body, name


def test_helper_is_importable() -> None:
    assert callable(apply_standard_interruption)


class _FakeBridge:
    """Minimal structural bridge exercising the four-step protocol."""

    def __init__(self) -> None:
        self.applied: list[InterruptionPlan] = []

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        del delivered_text, mode
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref="pre",
            post_state_ref="post",
        )

    def _serialize_framework_state(self) -> bytes:
        return b"{}"

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        self.applied.append(plan)


def test_apply_standard_interruption_runs_four_step_protocol() -> None:
    bridge = _FakeBridge()
    recorder = RecordingAgentRecorder()

    apply_standard_interruption(
        bridge,
        "hi",
        CancellationMode.IMMEDIATE_STOP,
        recorder,
        "sig-1",
    )

    assert recorder.kinds() == [
        "state_snapshot",
        "state_committed",
        "state_snapshot",
        "cancellation_boundary",
    ]
    assert len(bridge.applied) == 1
    assert bridge.applied[0].mutation_kind == "interrupt_truncate"
