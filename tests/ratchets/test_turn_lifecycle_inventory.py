"""Freeze turn identity writers, activity transitions, and event topology."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.ratchets._turn_lifecycle_inventory import (
    TurnLifecycleSite,
    format_delta,
    inventory_delta,
    scan_turn_lifecycle,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
MANIFEST_PATH = Path(__file__).with_name("turn-lifecycle-manifest.json")

ROLES = frozenset(
    {
        "activity_initialize",
        "activity_reset",
        "activity_transition",
        "identity_carrier",
        "identity_clear",
        "identity_command",
        "identity_initialize",
        "identity_publish",
        "identity_publish_or_clear",
        "observation",
    }
)


def test_turn_lifecycle_manifest_is_a_classified_source_bijection(
    pytestconfig: pytest.Config,
) -> None:
    findings = scan_turn_lifecycle(SOURCE_ROOT)
    actual = {finding.site for finding in findings}
    manifest = _load_manifest()
    if pytestconfig.getoption("--update-baseline"):
        rationale = _required_rationale(pytestconfig)
        _write_updated_manifest(actual, manifest=manifest, update_rationale=rationale)
        manifest = _load_manifest()

    entries = [_parse_entry(record) for record in manifest["entries"]]
    expected = {site for site, _role, _rationale in entries}
    added, removed = inventory_delta(expected, actual)
    assert not added and not removed, (
        format_delta(added, removed, findings=findings)
        + "\nUse --update-baseline --baseline-rationale 'reviewed reason' to refresh "
        "the source skeleton, then classify every new entry."
    )

    unclassified = [
        site.as_record()
        for site, role, rationale in entries
        if role not in ROLES or not rationale.strip()
    ]
    assert not unclassified, (
        "Classify every turn-lifecycle manifest entry and give its rationale:\n  "
        + "\n  ".join(unclassified)
    )
    assert manifest["counts"] == _counts(entries)


def test_turn_started_topology_has_one_command_path() -> None:
    entries = [_parse_entry(record) for record in _load_manifest()["entries"]]
    producers = [
        role for site, role, _rationale in entries if site.category == "turn_started_producer"
    ]
    subscriptions = [
        role for site, role, _rationale in entries if site.category == "turn_started_subscription"
    ]

    assert producers.count("identity_command") == 1
    assert producers.count("observation") == 2
    assert subscriptions.count("identity_command") == 1
    assert subscriptions.count("observation") == 1


def test_turn_manager_activity_epoch_has_one_owner_and_writer() -> None:
    entries = [_parse_entry(record) for record in _load_manifest()["entries"]]
    owners = [
        site for site, _role, _rationale in entries if site.category == "activity_owner_assignment"
    ]
    writers = [
        site for site, _role, _rationale in entries if site.category == "activity_epoch_bump"
    ]
    direct_state_writers = [
        site for site, _role, _rationale in entries if site.category == "activity_state_assignment"
    ]

    assert [(site.path, site.qualname) for site in owners] == [
        ("turn_manager.py", "TurnManager.__init__")
    ]
    assert [(site.path, site.qualname) for site in writers] == [
        ("turn_manager.py", "TurnManager._transition")
    ]
    assert direct_state_writers == []


def test_scanner_covers_direct_writes_transitions_and_event_topology(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "session/_session.py",
        """
class Session:
    def __init__(self):
        self._turn = None
        self._turn_generation = 0

    def clear(self):
        self._reset_turn_state()
""",
    )
    _write_module(
        source_root,
        "turn_manager.py",
        """
class TurnManager:
    def __init__(self):
        self._activity = Epoch(IDLE)
        self._state = IDLE

    def _transition(self, state, *, reason):
        self._activity.bump(state)

    def publish(self):
        self._transition(ACTIVE, reason='test')
""",
    )
    _write_module(
        source_root,
        "session/_builder.py",
        "session._subscribe_owned(TurnStarted, runner.on_turn_started)\n",
    )
    _write_module(
        source_root,
        "producer.py",
        "event = TurnStarted(turn_id='turn-1')\n",
    )

    counts = Counter(finding.site.category for finding in scan_turn_lifecycle(source_root))

    assert counts == {
        "activity_epoch_bump": 1,
        "activity_owner_assignment": 1,
        "activity_state_assignment": 1,
        "activity_transition_call": 1,
        "identity_carrier_assignment": 1,
        "identity_clear_call": 1,
        "identity_pointer_assignment": 1,
        "turn_started_producer": 1,
        "turn_started_subscription": 1,
    }


def test_line_insertion_preserves_fingerprints_but_writer_replacement_does_not(
    tmp_path: Path,
) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    moved_root = tmp_path / "moved" / "src" / "easycat"
    changed_root = tmp_path / "changed" / "src" / "easycat"
    source = """
class Session:
    def publish(self, turn):
        self._turn = turn
"""
    _write_module(before_root, "session/_session.py", source)
    _write_module(moved_root, "session/_session.py", "\n\n" + source)
    _write_module(
        changed_root,
        "session/_session.py",
        source.replace("self._turn = turn", "self._turn = replacement"),
    )

    before = {finding.site for finding in scan_turn_lifecycle(before_root)}
    moved = {finding.site for finding in scan_turn_lifecycle(moved_root)}
    changed = {finding.site for finding in scan_turn_lifecycle(changed_root)}

    assert before == moved
    added, removed = inventory_delta(before, changed)
    assert len(added) == len(removed) == 1


def test_new_module_cannot_bypass_session_or_manager_writer_inventory(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "new_writer.py",
        """
def bypass(session, turn_manager, turn):
    session._turn = turn
    turn_manager._activity = Epoch(IDLE)
    turn_manager._activity.bump(ACTIVE)
    turn_manager._state = ACTIVE
""",
    )

    categories = [finding.site.category for finding in scan_turn_lifecycle(source_root)]

    assert categories == [
        "activity_epoch_bump",
        "activity_owner_assignment",
        "activity_state_assignment",
        "identity_pointer_assignment",
    ]


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _parse_entry(record: str) -> tuple[TurnLifecycleSite, str, str]:
    parts = record.split("\t", 7)
    if len(parts) != 8:
        raise AssertionError(f"Invalid turn-lifecycle manifest record: {record!r}")
    return TurnLifecycleSite.from_record("\t".join(parts[:6])), parts[6], parts[7]


def _counts(entries: list[tuple[TurnLifecycleSite, str, str]]) -> dict[str, dict[str, int]]:
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


def _write_updated_manifest(
    actual: set[TurnLifecycleSite],
    *,
    manifest: dict[str, Any],
    update_rationale: str,
) -> None:
    existing: dict[TurnLifecycleSite, tuple[str, str]] = {}
    for record in manifest.get("entries", []):
        site, role, rationale = _parse_entry(str(record))
        existing[site] = (role, rationale)

    entries: list[tuple[TurnLifecycleSite, str, str]] = []
    for site in sorted(actual):
        role, rationale = existing.get(site, ("unclassified", ""))
        entries.append((site, role, rationale))
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
