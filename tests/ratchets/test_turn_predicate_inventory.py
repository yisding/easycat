"""Freeze synchronous identity, activity, token, phase, and null predicates."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.ratchets._turn_predicate_inventory import (
    TurnPredicateSite,
    format_delta,
    inventory_delta,
    scan_turn_predicates,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
MANIFEST_PATH = Path(__file__).with_name("turn-predicate-manifest.json")

ROLE_BY_CATEGORY = {
    "activity_state_predicate": "activity",
    "identity_generation_predicate": "legacy_identity",
    "identity_pointer_predicate": "identity",
    "null_object_predicate": "null_object",
    "phase_latch_predicate": "phase_latch",
    "token_cancellation_predicate": "cancellation",
    "token_ownership_predicate": "token_ownership",
}

RATIONALE_BY_CATEGORY = {
    "activity_state_predicate": "Guards an effect by the manager activity phase.",
    "identity_generation_predicate": "Legacy generation participates in turn-identity liveness.",
    "identity_pointer_predicate": "Pointer identity participates in turn-identity liveness.",
    "null_object_predicate": (
        "Distinguishes absent or explicit no-turn payloads; not a liveness fence."
    ),
    "phase_latch_predicate": "One-way preemptive take-point latch; not a turn-identity epoch.",
    "token_cancellation_predicate": "Cooperative cancellation participates in effect admission.",
    "token_ownership_predicate": "Token identity guards ownership or cleanup bookkeeping.",
}


def test_turn_predicate_manifest_is_a_classified_source_bijection(
    pytestconfig: pytest.Config,
) -> None:
    findings = scan_turn_predicates(SOURCE_ROOT)
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
        "intentional predicate migration."
    )
    assert all(
        role == ROLE_BY_CATEGORY[site.category]
        and rationale == RATIONALE_BY_CATEGORY[site.category]
        for site, role, rationale in entries
    )
    assert manifest["counts"] == _counts(entries)


def test_scanner_distinguishes_identity_activity_token_phase_and_null(tmp_path: Path) -> None:
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
def predicate(self, turn, generation):
    if self._turn.current is turn:
        pass
    if self._turn.generation == generation:
        pass
    if self._turn_manager.state == PROCESSING:
        pass
    if turn.cancel_token.is_cancelled:
        pass
    if turn is self._no_turn:
        pass
    return turn.generation <= self._preemptive_finalized_generation
""",
        )

    counts = Counter(finding.site.category for finding in scan_turn_predicates(source_root))
    assert counts == {
        "activity_state_predicate": 3,
        "identity_generation_predicate": 3,
        "identity_pointer_predicate": 3,
        "null_object_predicate": 3,
        "phase_latch_predicate": 3,
        "token_cancellation_predicate": 3,
    }


def test_line_insertion_preserves_fingerprints_but_replacement_does_not(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    moved_root = tmp_path / "moved" / "src" / "easycat"
    changed_root = tmp_path / "changed" / "src" / "easycat"
    source = """
def predicate(self, turn):
    return self._turn.current is turn
"""
    for root, turn_runner_source in (
        (before_root, source),
        (moved_root, "\n\n" + source),
        (changed_root, source.replace(" is turn", " is replacement")),
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

    before = {finding.site for finding in scan_turn_predicates(before_root)}
    moved = {finding.site for finding in scan_turn_predicates(moved_root)}
    changed = {finding.site for finding in scan_turn_predicates(changed_root)}
    assert before == moved
    added, removed = inventory_delta(before, changed)
    assert len(added) == len(removed) == 1


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _parse_entry(record: str) -> tuple[TurnPredicateSite, str, str]:
    parts = record.split("\t", 7)
    if len(parts) != 8:
        raise AssertionError(f"Invalid turn-predicate manifest record: {record!r}")
    return TurnPredicateSite.from_record("\t".join(parts[:6])), parts[6], parts[7]


def _counts(entries: list[tuple[TurnPredicateSite, str, str]]) -> dict[str, dict[str, int]]:
    return {
        "categories": dict(
            sorted(Counter(site.category for site, _role, _reason in entries).items())
        ),
        "roles": dict(sorted(Counter(role for _site, role, _reason in entries).items())),
    }


def _required_rationale(pytestconfig: pytest.Config) -> str:
    rationale = str(pytestconfig.getoption("--baseline-rationale") or "").strip()
    if not rationale:
        raise pytest.UsageError("--update-baseline requires a non-empty --baseline-rationale")
    return rationale


def _write_manifest(actual: set[TurnPredicateSite], *, update_rationale: str) -> None:
    entries = [
        (site, ROLE_BY_CATEGORY[site.category], RATIONALE_BY_CATEGORY[site.category])
        for site in sorted(actual)
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


def _write_module(source_root: Path, relative_path: str, source: str) -> None:
    path = source_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
