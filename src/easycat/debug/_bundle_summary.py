"""Stable summary projections for records and annotation sidecars.

These projections operate on plain decoded mappings so bundle inspection,
export, and future debugger consumers can share the same aggregation rules
without depending on CLI presentation code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from easycat.debug._turn_timeline import record_wall_ns, safe_turn_id


@dataclass(frozen=True, slots=True)
class BundleRecordSummary:
    """High-signal counts and identifiers projected from journal records."""

    session_id: str
    turn_count: int
    errors: int
    error_type: str | None
    failing_turn_id: str | None
    tool_calls: int
    records: int
    duration_ms: float | None

    def to_dict(self) -> dict[str, object]:
        """Return the stable mapping used by CLI JSON envelopes."""
        return {
            "session_id": self.session_id,
            "turn_count": self.turn_count,
            "errors": self.errors,
            "error_type": self.error_type,
            "failing_turn_id": self.failing_turn_id,
            "tool_calls": self.tool_calls,
            "records": self.records,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class AnnotationSummary:
    """Pass/fail counts and failure categories from an annotation sidecar."""

    annotated: int
    passed: int
    failed: int
    failure_types: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        """Return the stable mapping used by CLI JSON envelopes."""
        return {
            "annotated": self.annotated,
            "passed": self.passed,
            "failed": self.failed,
            "failure_types": dict(self.failure_types),
        }


@dataclass(slots=True)
class _BundleRecordAccumulator:
    session_id: str = ""
    turn_ids: set[str] = field(default_factory=set)
    errors: int = 0
    error_type: str | None = None
    failing_turn_id: str | None = None
    tool_calls: int = 0
    records: int = 0
    earliest_wall_ns: int | None = None
    latest_wall_ns: int | None = None
    call_duration_ms: float | None = None

    def observe(self, record: Mapping[str, Any]) -> None:
        """Include one decoded journal record in the projection."""
        self.records += 1
        self._observe_session(record)
        turn_id = safe_turn_id(record.get("turn_id"))
        if turn_id is not None:
            self.turn_ids.add(turn_id)
        self._observe_timing(record)
        self._observe_call_duration(record)
        self._observe_error(record.get("error"), turn_id)
        if record.get("name") == "tool_call_started":
            self.tool_calls += 1

    def _observe_session(self, record: Mapping[str, Any]) -> None:
        if not self.session_id and record.get("session_id"):
            self.session_id = str(record["session_id"])

    def _observe_timing(self, record: Mapping[str, Any]) -> None:
        wall_ns = record_wall_ns(record)
        if wall_ns is None:
            return
        if self.earliest_wall_ns is None or wall_ns < self.earliest_wall_ns:
            self.earliest_wall_ns = wall_ns
        if self.latest_wall_ns is None or wall_ns > self.latest_wall_ns:
            self.latest_wall_ns = wall_ns

    def _observe_call_duration(self, record: Mapping[str, Any]) -> None:
        if record.get("name") != "call_ended":
            return
        data = record.get("data")
        if not isinstance(data, Mapping):
            return
        duration_s = data.get("duration_s")
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            return
        if duration_s < 0:
            return
        self.call_duration_ms = float(duration_s) * 1000.0

    def _observe_error(self, error: object, turn_id: str | None) -> None:
        if not error:
            return
        self.errors += 1
        if self.error_type is not None or not isinstance(error, Mapping):
            return
        error_type = error.get("type")
        if error_type:
            self.error_type = str(error_type)
            self.failing_turn_id = turn_id

    def finish(self) -> BundleRecordSummary:
        """Freeze the accumulated state into the public projection."""
        duration_ms = None
        if self.call_duration_ms is not None:
            duration_ms = self.call_duration_ms
        elif self.earliest_wall_ns is not None and self.latest_wall_ns is not None:
            duration_ms = (self.latest_wall_ns - self.earliest_wall_ns) / 1_000_000
        return BundleRecordSummary(
            session_id=self.session_id,
            turn_count=len(self.turn_ids),
            errors=self.errors,
            error_type=self.error_type,
            failing_turn_id=self.failing_turn_id,
            tool_calls=self.tool_calls,
            records=self.records,
            duration_ms=duration_ms,
        )


def summarise_bundle_records(records: Iterable[Mapping[str, Any]]) -> BundleRecordSummary:
    """Project a decoded record stream into bundle-level statistics."""
    accumulator = _BundleRecordAccumulator()
    for record in records:
        accumulator.observe(record)
    return accumulator.finish()


def summarise_annotations(annotations: Mapping[str, Any]) -> AnnotationSummary:
    """Roll a per-turn annotation map into pass/fail and failure-type counts."""
    passed = 0
    failed = 0
    failure_types: dict[str, int] = {}
    for record in annotations.values():
        if not isinstance(record, Mapping):
            continue
        verdict = record.get("passed")
        if verdict is True:
            passed += 1
        elif verdict is False:
            failed += 1
        failure_type = record.get("failure_type")
        if isinstance(failure_type, str) and failure_type:
            failure_types[failure_type] = failure_types.get(failure_type, 0) + 1
    return AnnotationSummary(
        annotated=len(annotations),
        passed=passed,
        failed=failed,
        failure_types=MappingProxyType(failure_types),
    )
