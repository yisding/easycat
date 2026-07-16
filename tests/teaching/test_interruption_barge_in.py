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
ROUTE_FIELDS = {
    COORDINATORS[0]: ("bot_task", "active_cancel", "consumed"),
    COORDINATORS[1]: ("bot_task", "active_cancel", "active_ledger", "consumed"),
    COORDINATORS[2]: ("bot_task", "active_cancel", "consumed"),
    COORDINATORS[3]: ("bot_task", "active_cancel", "consumed"),
}


def _coordinator_loop(coordinator: ast.AsyncFunctionDef) -> ast.While:
    loops = [node for node in ast.walk(coordinator) if isinstance(node, ast.While)]
    assert len(loops) == 1
    return loops[0]


@pytest.mark.parametrize("path", COORDINATORS, ids=lambda path: path.stem)
def test_barge_in_start_marker_falls_through_to_open_stt(path: Path) -> None:
    tree = ast.parse(path.read_text())
    coordinator = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "coordinator"
    )
    loop = _coordinator_loop(coordinator)
    route_index, route = next(
        (index, node)
        for index, node in enumerate(loop.body)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Await)
        and "route_barge_in" in ast.unparse(node.value)
    )

    target = route.targets[0]
    assert isinstance(target, ast.Tuple)
    assert tuple(ast.unparse(element) for element in target.elts) == ROUTE_FIELDS[path]

    consumed_handler = loop.body[route_index + 1]
    assert isinstance(consumed_handler, ast.If)
    assert ast.unparse(consumed_handler.test) == "consumed"
    assert isinstance(consumed_handler.body[-1], ast.Continue)

    start_handler = loop.body[route_index + 2]
    assert isinstance(start_handler, ast.If)
    assert ast.unparse(start_handler.test) == "tag == 'speech_started'"
    assert "await stt.start_stream()" in ast.unparse(start_handler)


@pytest.mark.parametrize("path", COORDINATORS, ids=lambda path: path.stem)
def test_completed_bot_tasks_are_reaped_inside_barge_in_router(path: Path) -> None:
    tree = ast.parse(path.read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "route_barge_in"
    )
    done_index, done_handler = next(
        (index, node)
        for index, node in enumerate(helper.body)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "bot_task.done()"
    )
    tag_index = next(
        index
        for index, node in enumerate(helper.body)
        if isinstance(node, ast.If) and "tag != 'speech_started'" in ast.unparse(node.test)
    )

    assert done_index < tag_index
    assert any(
        isinstance(node, ast.Await) and "observe_bot_task" in ast.unparse(node.value)
        for node in ast.walk(done_handler)
    )
    terminal_return = done_handler.body[-1]
    assert isinstance(terminal_return, ast.Return) and isinstance(terminal_return.value, ast.Tuple)
    values = terminal_return.value.elts
    assert all(isinstance(value, ast.Constant) and value.value is None for value in values[:-1])
    assert isinstance(values[-1], ast.Constant) and values[-1].value is False
    assert len(values) == len(ROUTE_FIELDS[path])
