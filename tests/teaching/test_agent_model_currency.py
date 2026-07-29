"""Keep latency-sensitive teaching and CLI examples on one current model tier."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from easycat.cli import console, serve
from easycat.debug.testing import assert_llm_judge

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_MODEL = "gpt-5.6-luna"
TEACHING_MODEL_SOURCES = (
    "docs/teaching/05-blocking-agent/main.py",
    "docs/teaching/06-streaming-agent/main.py",
    "docs/teaching/07-tools/main.py",
    "docs/teaching/07-tools/blocking_tool.py",
    "docs/teaching/08-smart-turn/main.py",
    "docs/teaching/09-interruption/cancel.py",
    "docs/teaching/09-interruption/estimate.py",
    "docs/teaching/09-interruption/ignore.py",
    "docs/teaching/10-cleaning-signal/main.py",
    "docs/teaching/10-cleaning-signal/wrong_order.py",
    "docs/teaching/12-evals-and-latency/llm_judge.py",
    "docs/teaching/14-bring-your-own-agent/main.py",
)


def _model_assignments(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id in {"MODEL", "JUDGE_MODEL"}
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def test_latency_sensitive_agent_defaults_share_current_model_tier() -> None:
    assert console._LIVE_AGENT_MODEL == EXPECTED_MODEL
    assert serve._DEFAULT_AGENT_MODEL == EXPECTED_MODEL
    assert inspect.signature(assert_llm_judge).parameters["model"].default == EXPECTED_MODEL

    for relative_path in TEACHING_MODEL_SOURCES:
        assert _model_assignments(ROOT / relative_path) == {EXPECTED_MODEL}, relative_path


def test_teaching_chat_completions_disable_reasoning_for_voice_latency() -> None:
    for relative_path in TEACHING_MODEL_SOURCES:
        source = (ROOT / relative_path).read_text()
        if "chat.completions.create(" in source:
            assert 'reasoning_effort="none"' in source, relative_path
