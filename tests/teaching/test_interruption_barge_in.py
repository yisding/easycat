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
CLEANUP_FIELDS = {
    COORDINATORS[0]: ("bot_task", "active_cancel"),
    COORDINATORS[1]: ("bot_task", "active_cancel", "active_ledger"),
    COORDINATORS[2]: ("bot_task", "active_cancel"),
    COORDINATORS[3]: ("bot_task", "active_cancel"),
}


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


@pytest.mark.parametrize("path", COORDINATORS, ids=lambda path: path.stem)
def test_completed_bot_tasks_are_reaped_before_barge_in_checks(path: Path) -> None:
    tree = ast.parse(path.read_text())
    coordinator = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "coordinator"
    )
    loop = next(node for node in coordinator.body if isinstance(node, ast.While))
    cleanup_index, cleanup = next(
        (index, node)
        for index, node in enumerate(loop.body)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Await)
        and "_reap_completed_bot_task" in ast.unparse(node.value)
    )
    active_index = next(
        index
        for index, node in enumerate(loop.body)
        if isinstance(node, ast.If) and "not bot_task.done()" in ast.unparse(node.test)
    )

    assert cleanup_index < active_index
    target = cleanup.targets[0]
    assert isinstance(target, ast.Tuple)
    assert tuple(ast.unparse(element) for element in target.elts) == CLEANUP_FIELDS[path]

    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_reap_completed_bot_task"
    )
    helper_source = ast.unparse(helper)
    assert "bot_task.done()" in helper_source
    assert "await bot_task" in helper_source
    assert "name='bot_task.error'" in helper_source
    terminal_return = helper.body[-1]
    assert isinstance(terminal_return, ast.Return)
    assert isinstance(terminal_return.value, ast.Tuple)
    assert all(
        isinstance(value, ast.Constant) and value.value is None
        for value in terminal_return.value.elts
    )
    assert len(terminal_return.value.elts) == len(CLEANUP_FIELDS[path])
