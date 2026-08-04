"""Require every discovered teardown timeout site to have one classification."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from tests.ratchets._teardown_budget_inventory import (
    CLASSIFICATIONS,
    BudgetSite,
    format_delta,
    inventory_delta,
    scan_teardown_budgets,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
MANIFEST_PATH = Path(__file__).with_name("teardown-budget-manifest.json")


def test_teardown_budget_manifest_is_a_classified_source_bijection(
    pytestconfig: pytest.Config,
) -> None:
    findings = scan_teardown_budgets(SOURCE_ROOT)
    actual = {finding.site for finding in findings}
    manifest = _load_manifest() if MANIFEST_PATH.exists() else {"entries": []}
    if pytestconfig.getoption("--update-baseline"):
        rationale = _required_rationale(pytestconfig)
        _write_updated_manifest(actual, manifest=manifest, update_rationale=rationale)
        manifest = _load_manifest()

    entries = [_parse_entry(record) for record in manifest["entries"]]
    expected = {site for site, _classification, _rationale in entries}
    added, removed = inventory_delta(expected, actual)
    assert not added and not removed, (
        format_delta(added, removed, findings=findings)
        + "\nUse --update-baseline --baseline-rationale 'reviewed reason' to refresh "
        "the source skeleton, then classify every new entry."
    )

    unclassified = [
        site.as_record()
        for site, classification, rationale in entries
        if classification not in CLASSIFICATIONS or not rationale.strip()
    ]
    assert not unclassified, (
        "Classify every teardown-budget manifest entry and give its rationale:\n  "
        + "\n  ".join(unclassified)
    )
    actual_counts = dict(sorted(Counter(item[1] for item in entries).items()))
    assert manifest["counts"] == actual_counts


def test_scanner_covers_declarations_defaults_and_lifecycle_calls(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "sample.py",
        """
import asyncio

_CLOSE_TIMEOUT_S = 0.5

class Config:
    drain_timeout_s: float = 10.0

async def close_stream(*, force_timeout_s: float = 2.0):
    await asyncio.wait_for(finish(), timeout=_CLOSE_TIMEOUT_S)

async def ordinary_work(timeout_s: float = 3.0):
    await asyncio.wait_for(work(), timeout=timeout_s)
""",
    )

    findings = scan_teardown_budgets(source_root)
    shapes = {(item.site.kind, item.site.construct) for item in findings}

    assert shapes == {
        ("config_default", "default force_timeout_s"),
        ("config_default", "default timeout_s"),
        ("lifecycle_call", "call asyncio.wait_for"),
        ("named_constant", "constant _CLOSE_TIMEOUT_S"),
        ("named_constant", "constant drain_timeout_s"),
    }


def test_new_lifecycle_timeout_call_changes_inventory(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    _write_module(before_root, "sample.py", "async def close():\n    await finish()\n")
    _write_module(
        after_root,
        "sample.py",
        "import asyncio\nasync def close():\n    await asyncio.wait_for(finish(), timeout=1.0)\n",
    )

    before = {item.site for item in scan_teardown_budgets(before_root)}
    after = {item.site for item in scan_teardown_budgets(after_root)}
    added, removed = inventory_delta(before, after)

    assert [item.construct for item in added] == ["call asyncio.wait_for"]
    assert not removed


def test_lifecycle_control_wait_without_a_budget_is_not_inventoried(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "sample.py",
        """
import asyncio

async def close(tasks, event):
    await asyncio.wait(tasks)
    await event.wait()
""",
    )

    assert scan_teardown_budgets(source_root) == []


def test_line_insertion_does_not_change_budget_fingerprint(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    source = (
        "import asyncio\nasync def close():\n    await asyncio.wait_for(finish(), timeout=1.0)\n"
    )
    _write_module(before_root, "sample.py", source)
    _write_module(after_root, "sample.py", "\n\n" + source)

    before = {item.site for item in scan_teardown_budgets(before_root)}
    after = {item.site for item in scan_teardown_budgets(after_root)}

    assert before == after


def test_changed_timeout_expression_changes_budget_fingerprint(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    before_source = (
        "import asyncio\nasync def close():\n    await asyncio.wait_for(finish(), timeout=1.0)\n"
    )
    after_source = before_source.replace("timeout=1.0", "timeout=2.0")
    _write_module(before_root, "sample.py", before_source)
    _write_module(after_root, "sample.py", after_source)

    before = {item.site for item in scan_teardown_budgets(before_root)}
    after = {item.site for item in scan_teardown_budgets(after_root)}
    added, removed = inventory_delta(before, after)

    assert len(added) == len(removed) == 1
    assert added[0].ast_hash != removed[0].ast_hash


def test_manifest_update_preserves_reviews_and_marks_new_sites(
    tmp_path: Path,
) -> None:
    reviewed = BudgetSite(
        "named_constant",
        "sample.py",
        "<module>",
        "constant CLOSE_TIMEOUT",
        "aaaaaaaaaaaaaaaa",
        0,
    )
    added = BudgetSite(
        "lifecycle_call",
        "sample.py",
        "close",
        "call asyncio.wait_for",
        "bbbbbbbbbbbbbbbb",
        0,
    )
    manifest = {"entries": [f"{reviewed.as_record()}\tlifecycle_budget\tReviewed cleanup bound."]}
    manifest_path = tmp_path / "manifest.json"

    _write_updated_manifest(
        {reviewed, added},
        manifest=manifest,
        update_rationale="Deliberate fixture update",
        manifest_path=manifest_path,
    )

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    parsed = {_parse_entry(record)[0]: _parse_entry(record)[1:] for record in updated["entries"]}
    assert parsed[reviewed] == ("lifecycle_budget", "Reviewed cleanup bound.")
    assert parsed[added] == ("unclassified", "")
    assert updated["counts"] == {"lifecycle_budget": 1, "unclassified": 1}


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _parse_entry(record: object) -> tuple[BudgetSite, str, str]:
    parts = str(record).split("\t", maxsplit=7)
    assert len(parts) == 8, f"Malformed teardown-budget manifest entry: {record!r}"
    site = BudgetSite.from_record("\t".join(parts[:6]))
    return site, parts[6], parts[7]


def _write_updated_manifest(
    actual: set[BudgetSite],
    *,
    manifest: dict[str, object],
    update_rationale: str,
    manifest_path: Path = MANIFEST_PATH,
) -> None:
    prior = {
        site: (classification, rationale)
        for site, classification, rationale in (
            _parse_entry(record) for record in manifest.get("entries", [])
        )
    }
    records: list[str] = []
    counts: Counter[str] = Counter()
    for site in sorted(actual):
        classification, rationale = prior.get(site, ("unclassified", ""))
        counts[classification] += 1
        records.append(f"{site.as_record()}\t{classification}\t{rationale}")
    data = {
        "version": 1,
        "update_rationale": update_rationale,
        "counts": dict(sorted(counts.items())),
        "entries": records,
    }
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _required_rationale(pytestconfig: pytest.Config) -> str:
    rationale = pytestconfig.getoption("--baseline-rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        pytest.fail("--update-baseline requires a non-empty --baseline-rationale")
    return rationale.strip()


def _write_module(source_root: Path, relative_path: str, source: str) -> None:
    path = source_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.lstrip(), encoding="utf-8")
