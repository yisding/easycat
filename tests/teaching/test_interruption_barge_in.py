"""Regression coverage for the teaching-ladder barge-in coordinators."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
COORDINATORS = (
    ROOT / "docs/teaching/09-interruption/cancel.py",
    ROOT / "docs/teaching/09-interruption/estimate.py",
    ROOT / "docs/teaching/10-cleaning-signal/main.py",
    ROOT / "docs/teaching/10-cleaning-signal/wrong_order.py",
)


@pytest.mark.parametrize("path", COORDINATORS, ids=lambda path: path.stem)
def test_barge_in_start_marker_falls_through_to_open_stt(path: Path) -> None:
    tree = ast.parse(path.read_text())
    coordinator = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "coordinator"
    )
    loop = next(node for node in coordinator.body if isinstance(node, ast.While))
    active_bot_index, active_bot = next(
        (index, node)
        for index, node in enumerate(loop.body)
        if isinstance(node, ast.If) and "not bot_task.done()" in ast.unparse(node.test)
    )

    assert any(
        isinstance(node, ast.Await) and ast.unparse(node.value) == "bot_task"
        for node in ast.walk(active_bot)
    )
    assert not isinstance(active_bot.body[-1], ast.Continue)

    start_handler = loop.body[active_bot_index + 1]
    assert isinstance(start_handler, ast.If)
    assert ast.unparse(start_handler.test) == "tag == 'speech_started'"
    assert "await stt.start_stream()" in ast.unparse(start_handler)
