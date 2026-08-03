"""Freeze pause-generation writers, readers, and future correlation."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.ratchets._pause_generation_inventory import (
    PauseGenerationSite,
    format_delta,
    inventory_delta,
    scan_pause_generation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
MANIFEST_PATH = Path(__file__).with_name("pause-generation-manifest.json")

ROLE_BY_CATEGORY = {
    "api_read": "borrower",
    "future_map_owner": "correlation_owner",
    "future_map_take": "correlation_owner",
    "future_map_write": "correlation_owner",
    "generation_handoff": "carrier",
    "generation_receiver": "carrier",
    "owner_read": "pause_owner",
    "owner_write": "pause_owner",
}

RATIONALE_BY_CATEGORY = {
    "api_read": "Borrows the current pause generation through TurnManager's public seam.",
    "future_map_owner": "Owns future-to-pause correlation storage in STTCommitter.",
    "future_map_take": "Consumes one future's pause correlation at completion or cleanup.",
    "future_map_write": "Binds one pending STT future to its originating pause.",
    "generation_handoff": "Carries pause identity across one call boundary.",
    "generation_receiver": "Receives pause identity for a timer, provider commit, or final.",
    "owner_read": "Reads TurnManager's private integer pause identity.",
    "owner_write": "Initializes or advances TurnManager's private pause identity.",
}


def test_pause_generation_manifest_is_a_classified_source_bijection(
    pytestconfig: pytest.Config,
) -> None:
    findings = scan_pause_generation(SOURCE_ROOT)
    actual = {finding.site for finding in findings}
    if pytestconfig.getoption("--update-baseline"):
        _write_manifest(actual, update_rationale=_required_rationale(pytestconfig))

    manifest = _load_manifest()
    entries = [_parse_entry(record) for record in manifest["entries"]]
    expected = {site for site, _role, _rationale in entries}
    added, removed = inventory_delta(expected, actual)
    assert not added and not removed, (
        format_delta(added, removed, findings=findings)
        + "\nUse --update-baseline --baseline-rationale 'reviewed reason' only for an "
        "intentional pause-generation migration."
    )
    assert all(
        role == ROLE_BY_CATEGORY[site.category]
        and rationale == RATIONALE_BY_CATEGORY[site.category]
        for site, role, rationale in entries
    )
    assert manifest["counts"] == _counts(entries)


def test_scanner_classifies_owner_carrier_and_future_map_sites(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "turn_manager.py",
        """
class TurnManager:
    def check(self, pause_generation):
        self._pause_generation = 0
        self._pause_generation += 1
        return pause_generation == self._pause_generation
""",
    )
    _write_module(
        source_root,
        "session/_stt_committer.py",
        """
async def commit(self, pause_generation):
    self._pause_generation_by_future = {}
    self._pause_generation_by_future[future] = pause_generation
    value = self._pause_generation_by_future.pop(future, None)
    sink(pause_generation=value)
    return self._turn_manager.pause_generation
""",
    )

    counts = Counter(finding.site.category for finding in scan_pause_generation(source_root))
    assert counts == {
        "api_read": 1,
        "future_map_owner": 1,
        "future_map_take": 1,
        "future_map_write": 1,
        "generation_handoff": 1,
        "generation_receiver": 2,
        "owner_read": 1,
        "owner_write": 2,
    }


def test_line_insertion_preserves_fingerprints_but_owner_replacement_does_not(
    tmp_path: Path,
) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    moved_root = tmp_path / "moved" / "src" / "easycat"
    changed_root = tmp_path / "changed" / "src" / "easycat"
    source = """
class TurnManager:
    def __init__(self):
        self._pause_generation = 0
"""
    for root, manager_source in (
        (before_root, source),
        (moved_root, "\n\n" + source),
        (changed_root, source.replace("_pause_generation", "_pause_epoch")),
    ):
        _write_module(root, "turn_manager.py", manager_source)
        _write_module(root, "session/_stt_committer.py", "")

    before = {finding.site for finding in scan_pause_generation(before_root)}
    moved = {finding.site for finding in scan_pause_generation(moved_root)}
    changed = {finding.site for finding in scan_pause_generation(changed_root)}

    assert before == moved
    added, removed = inventory_delta(before, changed)
    assert added == []
    assert len(removed) == 1


def test_stale_pause_race_contracts_are_present() -> None:
    contracts = {
        REPO_ROOT / "tests" / "turns" / "test_turn_manager.py": (
            "test_stale_smart_turn_timer_cannot_end_a_later_pause",
            {"manager.on_vad_event", "detector.release[0].set"},
        ),
        REPO_ROOT / "tests" / "session" / "test_stt_committer.py": (
            "test_delayed_final_cannot_shorten_a_later_pause",
            {"committer.schedule", "stt._queue.put", "tm.on_vad_event"},
        ),
    }
    for path, (name, required_calls) in contracts.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        contract = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
            ),
            None,
        )
        assert contract is not None
        calls = {
            ast.unparse(node.func) for node in ast.walk(contract) if isinstance(node, ast.Call)
        }
        assert required_calls <= calls


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _parse_entry(record: str) -> tuple[PauseGenerationSite, str, str]:
    site_record, role, rationale = record.rsplit("\t", 2)
    return PauseGenerationSite.from_record(site_record), role, rationale


def _counts(
    entries: list[tuple[PauseGenerationSite, str, str]],
) -> dict[str, dict[str, int]]:
    return {
        "categories": dict(sorted(Counter(site.category for site, _, _ in entries).items())),
        "roles": dict(sorted(Counter(role for _, role, _ in entries).items())),
    }


def _write_manifest(sites: set[PauseGenerationSite], *, update_rationale: str) -> None:
    entries = [
        (
            site,
            ROLE_BY_CATEGORY[site.category],
            RATIONALE_BY_CATEGORY[site.category],
        )
        for site in sorted(sites)
    ]
    payload = {
        "version": 1,
        "update_rationale": update_rationale,
        "counts": _counts(entries),
        "entries": [
            f"{site.as_record()}\t{role}\t{rationale}" for site, role, rationale in entries
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _required_rationale(pytestconfig: pytest.Config) -> str:
    rationale = pytestconfig.getoption("--baseline-rationale")
    if not rationale:
        pytest.fail("--update-baseline requires --baseline-rationale 'reviewed reason'")
    return str(rationale)


def _write_module(root: Path, relative_path: str, source: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
