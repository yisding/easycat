"""Freeze turn-scoped commit effects and their suspension relationship."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.ratchets._turn_commit_inventory import (
    TurnCommitSite,
    format_delta,
    inventory_delta,
    scan_turn_commits,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
MANIFEST_PATH = Path(__file__).with_name("turn-commit-manifest.json")

ROLE_BY_CATEGORY = {
    "activity_commit": "manager_activity",
    "identity_commit": "turn_identity",
    "phase_latch_commit": "phase_latch",
    "provider_commit": "provider_dispatch",
    "public_observation_commit": "public_observation",
    "session_lifecycle_commit": "session_lifecycle",
    "turn_field_commit": "turn_bookkeeping",
}

RATIONALE_BY_CATEGORY = {
    "activity_commit": "Publishes or closes one manager activity phase.",
    "identity_commit": "Publishes, resets, or clears Session turn identity.",
    "phase_latch_commit": "Closes the one-way preemptive take phase for a turn.",
    "provider_commit": "Dispatches work across an agent, STT, or TTS provider boundary.",
    "public_observation_commit": "Publishes an externally observable lifecycle or output event.",
    "session_lifecycle_commit": "Commits a whole-Session lifecycle effect.",
    "turn_field_commit": "Mutates bookkeeping retained on the shared TurnContext.",
}


def test_turn_commit_manifest_is_a_classified_source_bijection(
    pytestconfig: pytest.Config,
) -> None:
    findings = scan_turn_commits(SOURCE_ROOT)
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
        "intentional commit-edge inventory change."
    )
    assert all(
        role == ROLE_BY_CATEGORY[site.category]
        and rationale == RATIONALE_BY_CATEGORY[site.category]
        for site, role, rationale in entries
    )
    assert manifest["counts"] == _counts(entries)


def test_scanner_distinguishes_synchronous_awaited_and_post_await_effects(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "easycat"
    for relative_path in (
        "session/_stt_committer.py",
        "session/_tts_scheduler.py",
        "session/_turn_runner.py",
    ):
        _write_module(
            source_root,
            relative_path,
            """
async def commits(self, turn):
    self._turn.set(turn)
    await self._emit(Started())
    self._reset_turn_state()
    self._preemptive_finalized_generation = max(1, 2)
""",
        )

    counts = Counter(
        (finding.site.category, finding.site.construct.split(" ", 1)[0])
        for finding in scan_turn_commits(source_root)
    )
    assert counts == {
        ("identity_commit", "synchronous"): 3,
        ("public_observation_commit", "awaited"): 3,
        ("identity_commit", "post_await"): 3,
        ("phase_latch_commit", "post_await"): 3,
    }


def test_line_insertion_preserves_fingerprints_but_effect_replacement_does_not(
    tmp_path: Path,
) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    moved_root = tmp_path / "moved" / "src" / "easycat"
    changed_root = tmp_path / "changed" / "src" / "easycat"
    source = """
async def commit(self):
    await checkpoint()
    self._reset_turn_state()
"""
    for root, turn_runner_source in (
        (before_root, source),
        (moved_root, "\n\n" + source),
        (changed_root, source.replace("_reset_turn_state", "_clear_turn")),
    ):
        for relative_path in (
            "session/_stt_committer.py",
            "session/_tts_scheduler.py",
            "session/_turn_runner.py",
        ):
            _write_module(
                root,
                relative_path,
                turn_runner_source if relative_path.endswith("_turn_runner.py") else "",
            )

    before = {finding.site for finding in scan_turn_commits(before_root)}
    moved = {finding.site for finding in scan_turn_commits(moved_root)}
    changed = {finding.site for finding in scan_turn_commits(changed_root)}

    assert before == moved
    added, removed = inventory_delta(before, changed)
    assert len(added) == len(removed) == 1


def test_late_stt_final_during_end_stream_race_contract_is_present() -> None:
    path = REPO_ROOT / "tests" / "session" / "test_turn_runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    contract = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            == "test_trailing_stt_final_after_take_does_not_restart_preemptive_generation"
        ),
        None,
    )
    assert contract is not None
    calls = {ast.unparse(node.func) for node in ast.walk(contract) if isinstance(node, ast.Call)}
    assert "runner.handle_end_of_speech" in calls
    assert "runner.on_stt_final" in calls
    assert "self.calls.append" in calls


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _parse_entry(record: str) -> tuple[TurnCommitSite, str, str]:
    site_record, role, rationale = record.rsplit("\t", 2)
    return TurnCommitSite.from_record(site_record), role, rationale


def _counts(
    entries: list[tuple[TurnCommitSite, str, str]],
) -> dict[str, dict[str, int]]:
    return {
        "categories": dict(sorted(Counter(site.category for site, _, _ in entries).items())),
        "roles": dict(sorted(Counter(role for _, role, _ in entries).items())),
        "suspensions": dict(
            sorted(
                Counter(
                    site.construct.split(" ", 1)[0] for site, _role, _rationale in entries
                ).items()
            )
        ),
    }


def _write_manifest(sites: set[TurnCommitSite], *, update_rationale: str) -> None:
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
