"""Freeze production-source concurrency and staleness call sites."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ratchets._source_inventory import (
    Fingerprint,
    format_delta,
    inventory_counts,
    inventory_delta,
    scan_production_source,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
BASELINE_PATH = Path(__file__).with_name("source-baseline.json")


def test_production_source_matches_reviewed_baseline(pytestconfig: pytest.Config) -> None:
    findings = scan_production_source(SOURCE_ROOT)
    actual = {item.fingerprint for item in findings}
    if pytestconfig.getoption("--update-baseline"):
        rationale = _required_rationale(pytestconfig)
        _write_baseline(actual, rationale=rationale)
        return

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = {Fingerprint.from_record(item) for item in baseline["entries"]}
    assert baseline["counts"] == inventory_counts(expected)
    added, removed = inventory_delta(expected, actual)
    assert not added and not removed, (
        format_delta(added, removed, actual_findings=findings)
        + "\nUse --update-baseline --baseline-rationale 'reviewed reason' only for an "
        "intentional inventory change."
    )


def test_scanner_resolves_aliases_and_all_guard_shapes(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "sample.py",
        """
import asyncio as aio
from asyncio import CancelledError as Cancel
from asyncio import Task as AsyncTask
from asyncio import create_task as imported_spawn

_JOBS: set[AsyncTask[None]] = set()
_FUTURES: set[aio.Future[None]] = set()
_lifecycle_epoch = 0

class Options:
    bool_generation: bool = False

async def worker(loop):
    local_epoch: int | None = None
    assigned_spawn = aio.ensure_future
    scheduler = aio.get_running_loop()
    imported_spawn(coro())
    assigned_spawn(coro())
    loop.create_task(coro())
    scheduler.create_task(coro())
    try:
        await coro()
    except (Cancel, RuntimeError):
        pass
    await aio.gather(coro(), return_exceptions=True)
    task = imported_spawn(coro())
    while not task.done():
        await aio.shield(task)
    task.cancelling()
    task.uncancel()
""",
    )

    findings = scan_production_source(source_root)
    counts = inventory_counts({item.fingerprint for item in findings})

    assert counts == {
        "cancelled_error_handler": 1,
        "epoch_field": 1,
        "gather_return_exceptions": 1,
        "module_task_set": 2,
        "raw_task_spawn": 5,
        "shield_loop": 1,
        "task_cancelling": 1,
        "task_uncancel": 1,
    }


def test_sanctioned_modules_are_exempt_only_from_their_declared_shapes(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "_concurrency.py",
        """
import asyncio

async def sanctioned(task):
    asyncio.create_task(work())
    task.cancelling()
    task.uncancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.gather(task, return_exceptions=True)
    while not task.done():
        await asyncio.shield(task)
""",
    )

    categories = {item.fingerprint.category for item in scan_production_source(source_root)}

    assert categories == {"gather_return_exceptions"}


def test_new_spawn_in_new_or_grandfathered_file_changes_inventory(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    baseline_source = """
import asyncio

async def grandfathered():
    asyncio.create_task(existing())
"""
    _write_module(before_root, "existing.py", baseline_source)
    _write_module(
        after_root,
        "existing.py",
        baseline_source.replace(
            "asyncio.create_task(existing())",
            "asyncio.create_task(existing())\n    asyncio.create_task(added())",
        ),
    )
    _write_module(
        after_root,
        "new.py",
        "import asyncio\nasyncio.create_task(new_file_work())\n",
    )

    added, removed = _delta(before_root, after_root)

    assert len(added) == 2
    assert not removed
    assert {item.path for item in added} == {"existing.py", "new.py"}


def test_delete_plus_add_replacement_changes_location_free_hash(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    before = "import asyncio\nasync def run():\n    asyncio.create_task(first())\n"
    after = "import asyncio\nasync def run():\n    asyncio.create_task(replacement())\n"
    _write_module(before_root, "sample.py", before)
    _write_module(after_root, "sample.py", after)

    added, removed = _delta(before_root, after_root)

    assert len(added) == 1
    assert len(removed) == 1
    assert added[0].ast_hash != removed[0].ast_hash


def test_insert_before_existing_call_preserves_old_fingerprint(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    _write_module(
        before_root,
        "sample.py",
        "import asyncio\nasync def run():\n    asyncio.create_task(existing())\n",
    )
    _write_module(
        after_root,
        "sample.py",
        "import asyncio\nasync def run():\n    asyncio.create_task(added())\n"
        "    asyncio.create_task(existing())\n",
    )

    added, removed = _delta(before_root, after_root)

    assert len(added) == 1
    assert not removed


def test_new_cancelled_error_alias_is_fingerprinted(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    _write_module(before_root, "sample.py", "async def run():\n    await work()\n")
    _write_module(
        after_root,
        "sample.py",
        """
from asyncio import CancelledError as Stop

async def run():
    try:
        await work()
    except Stop:
        pass
""",
    )

    added, removed = _delta(before_root, after_root)

    assert [item.category for item in added] == ["cancelled_error_handler"]
    assert not removed


def test_test_tree_raw_tasks_are_outside_the_production_inventory(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(source_root, "safe.py", "VALUE = 1\n")
    _write_module(
        tmp_path / "tests",
        "test_race.py",
        "import asyncio\nasyncio.create_task(orchestrate_race())\n",
    )

    assert scan_production_source(source_root) == []


def _delta(
    before_root: Path,
    after_root: Path,
) -> tuple[list[Fingerprint], list[Fingerprint]]:
    before = {item.fingerprint for item in scan_production_source(before_root)}
    after = {item.fingerprint for item in scan_production_source(after_root)}
    return inventory_delta(before, after)


def _write_module(source_root: Path, relative_path: str, source: str) -> None:
    path = source_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.lstrip(), encoding="utf-8")


def _required_rationale(pytestconfig: pytest.Config) -> str:
    rationale = pytestconfig.getoption("--baseline-rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        pytest.fail("--update-baseline requires a non-empty --baseline-rationale")
    return rationale.strip()


def _write_baseline(fingerprints: set[Fingerprint], *, rationale: str) -> None:
    data = {
        "version": 1,
        "rationale": rationale,
        "counts": inventory_counts(fingerprints),
        "entries": [item.as_record() for item in sorted(fingerprints)],
    }
    BASELINE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
