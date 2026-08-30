"""Replay types and orchestration for ExecutionJournal bundles.

This module is the single source of truth for replay fidelity, tool
policy, and orchestration primitives.  ``stages.base.ReplaySpec`` used
to be a separate stub; the class now lives here, re-exported at the
package level as ``easycat.stages.ReplaySpec`` (the ``stages.base``
module itself deliberately no longer forwards it) so every stage and
bundle sees the same type.

The exported surface is:

* :class:`ReplayFidelity`, :class:`ToolReplayPolicy` enums
* :class:`ReplaySpec` — the frozen configuration for one replay run
* :class:`ReplayCassette` — the per-stage slice of a bundle (records
  for one stage plus a resolver for artifact blobs)
* :class:`ReplayFrame`, :class:`ReplayResult` — the output shape
* :class:`ReplayRunner` — the bundle-level walker
* :class:`ReplayError`, :class:`ReplaySideEffectBlocked`,
  :class:`ProviderVersionMismatchError` — error types
* :func:`check_provider_versions`, :func:`mask_nondeterministic`,
  :func:`find_nearest_committable` — pure helpers
* :data:`REPLAY_IGNORE_FIELDS` — the set of journal fields masked in
  ``fast``-timing ARTIFACT replays so byte-determinism is reachable
"""

from __future__ import annotations

import copy
import enum
import hashlib
import inspect
import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from easycat.debug._turn_timeline import safe_turn_id
from easycat.errors import EASYCAT_E403, EasyCatError
from easycat.runtime.nondeterministic import NONDETERMINISTIC_FIELDS

if TYPE_CHECKING:
    from easycat.debug.bundle import CommittableCheckpoint, RunBundle

logger = logging.getLogger(__name__)

# Preserve normal recorded pacing while preventing a malformed bundle from
# suspending one replay step indefinitely.
_MAX_WALL_REPLAY_DELAY_NS = 30_000_000_000


# ── Enums ────────────────────────────────────────────────────────


class ReplayFidelity(enum.Enum):
    """How faithfully a replay reproduces the original run."""

    ARTIFACT = "artifact"
    SIMULATED = "simulated"
    LIVE = "live"


class ToolReplayPolicy(enum.Enum):
    """What a replay is allowed to do when it hits a tool or MCP call."""

    DENY = "deny"
    STUB = "stub"
    ALLOW = "allow"


# ── Errors ───────────────────────────────────────────────────────


class ReplaySideEffectBlocked(RuntimeError):
    """A tool or MCP invocation was blocked by ``ToolReplayPolicy.DENY``."""


class ReplayError(RuntimeError):
    """Replay cannot proceed — e.g. a non-committable entry point.

    Carries the sequence the caller asked for and the nearest committable
    checkpoints so the caller can surface a useful message or adjust the
    replay window.
    """

    def __init__(
        self,
        message: str,
        *,
        requested_sequence: int | None = None,
        nearest_committable_before: int | None = None,
        nearest_committable_after: int | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.requested_sequence = requested_sequence
        self.nearest_committable_before = nearest_committable_before
        self.nearest_committable_after = nearest_committable_after
        self.stage = stage


class ReplayDivergenceError(EasyCatError, ReplayError):
    """A stage replay produced output that differs from its recording."""

    def __init__(
        self,
        detail: str,
        *,
        stage: str,
        turn_id: str | None,
        expected_digest: str,
        actual_digest: str,
        requested_sequence: int | None = None,
    ) -> None:
        coded = EASYCAT_E403(detail=detail)
        super().__init__(coded.code, coded.message, **coded.context)
        self.requested_sequence = requested_sequence
        self.nearest_committable_before = None
        self.nearest_committable_after = None
        self.stage = stage
        self.turn_id = turn_id
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest


@dataclass(frozen=True)
class VersionMismatch:
    """One provider's bundle version not matching the installed version."""

    provider: str
    bundle_version: str
    installed_version: str
    code: str  # "MISMATCH", "UNKNOWN", or "MISSING"


class ProviderVersionMismatchError(RuntimeError):
    """Replay bundle captured a provider version that doesn't match installed.

    ``error_code`` is ``"PROVIDER_VERSION_MISMATCH"`` for a plain version
    skew and ``"PROVIDER_VERSION_UNKNOWN"`` when either side reports the
    sentinel ``"unknown"`` string from ``version_info()``.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "PROVIDER_VERSION_MISMATCH",
        mismatches: Sequence[VersionMismatch] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.mismatches: tuple[VersionMismatch, ...] = tuple(mismatches)


# ── Spec and cassette ────────────────────────────────────────────


@dataclass(frozen=True)
class ReplaySpec:
    """Full replay specification.

    ``fidelity`` is required (no default) so callers can't accidentally
    run a replay at a fidelity they didn't intend.  Every other field has
    a sensible default; in particular, ``tool_policy`` defaults to
    ``DENY`` so a replay never hits a live tool unless the caller opts in.
    """

    fidelity: ReplayFidelity
    from_sequence: int | None = None
    to_sequence: int | None = None
    stage_filter: list[str] | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    timing: Literal["fast", "wall"] = "fast"
    force: bool = False
    tool_policy: ToolReplayPolicy = ToolReplayPolicy.DENY


@dataclass(frozen=True)
class ReplayCassette:
    """Per-stage slice of a bundle handed to ``Stage.replay``.

    A cassette carries the journal records that belong to one stage plus
    a callable that resolves artifact refs to bytes.  Stages walk the
    records and resolve refs as needed; they never open the bundle zip
    themselves.
    """

    stage_name: str
    records: tuple[dict[str, Any], ...] = ()
    _resolver: Callable[[str], bytes | None] = field(
        default=lambda _ref: None, repr=False, compare=False
    )

    def blob(self, ref: str | None) -> bytes | None:
        """Return the bytes for ``ref`` or ``None`` when the ref is missing."""
        if not ref:
            return None
        return self._resolver(ref)

    def last_record(self, name: str | None = None) -> dict[str, Any] | None:
        """Return the last record whose ``name`` matches, or the last record."""
        if name is None:
            return self.records[-1] if self.records else None
        for record in reversed(self.records):
            if record.get("name") == name:
                return record
        return None

    def records_named(self, name: str) -> tuple[dict[str, Any], ...]:
        """Return every record with a matching ``name`` in order."""
        return tuple(r for r in self.records if r.get("name") == name)


# ── Frames and result ────────────────────────────────────────────


@dataclass(frozen=True)
class ReplayFrame:
    """One record rehydrated by the replay runner.

    A frame is a journal record projected through ``REPLAY_IGNORE_FIELDS``
    masking (in ``fast`` timing mode) with any referenced artifact blobs
    attached.  Callers iterate frames to rebuild stage outputs or to
    diff replay results against the original journal.
    """

    sequence: int
    stage: str
    kind: str
    name: str
    turn_id: str | None
    data: dict[str, Any]
    input_blob: bytes | None = None
    output_blob: bytes | None = None
    input_ref: str | None = None
    output_ref: str | None = None
    error: dict[str, Any] | None = None
    side_effecting: bool = False


@dataclass(frozen=True)
class StageReplayResult:
    """One built-in stage replayed from its turn-scoped cassette.

    ``output`` is the value returned by the stage's existing
    :meth:`Stage.replay` implementation. ``output_digest`` is a stable,
    compact comparison value suitable for logs and JSON summaries.
    """

    stage: str
    turn_id: str | None
    from_sequence: int | None
    to_sequence: int | None
    fidelity: ReplayFidelity
    output: Any = field(repr=False)
    output_digest: str
    matches_recording: bool | None


@dataclass
class ReplayResult:
    """Output of :meth:`ReplayRunner.run`.

    ``fidelity_label`` is the effective fidelity after any downgrades
    (e.g. ``ARTIFACT`` with ``force=True`` and a version mismatch is
    downgraded to ``LIVE`` because determinism is no longer guaranteed).

    ``side_effecting`` is ``True`` only when a configured tool executor
    actually ran at least one ``ALLOW``-policy tool invocation. Merely
    permitting recorded tool frames does not claim that a side effect
    happened.

    The tool-call lists let callers distinguish substitutions,
    pass-throughs, and calls that were actually executed.
    """

    frames: list[ReplayFrame]
    fidelity_label: ReplayFidelity
    stage_replays: list[StageReplayResult] = field(default_factory=list)
    side_effecting: bool = False
    blocked_tool_calls: list[str] = field(default_factory=list)
    stubbed_tool_calls: list[str] = field(default_factory=list)
    allowed_tool_calls: list[str] = field(default_factory=list)
    executed_tool_calls: list[str] = field(default_factory=list)


# ── Ignored-field masking ────────────────────────────────────────


# Replay extends the base nondeterminism set from stages/base.py with
# artifact-specific derivations and deadline timestamps.  Masking these
# in ``fast``-timing ARTIFACT replays is what makes byte-determinism
# reachable — every snapshot otherwise embeds a fresh monotonic clock
# reading that changes between captures.
REPLAY_IGNORE_FIELDS: frozenset[str] = NONDETERMINISTIC_FIELDS | frozenset(
    {
        "timing.wall_deadline_ns",
        "artifact_written_at",
        "artifact_hashed_at",
    }
)


def mask_nondeterministic(
    value: Any,
    fields: Iterable[str] = REPLAY_IGNORE_FIELDS,
) -> Any:
    """Return a deep copy of ``value`` with ``fields`` stripped.

    ``fields`` is a set of dotted paths (``"timing.wall_ns"``) or plain
    keys (``"recorded_at_utc"``).  Plain keys match anywhere in the
    structure; dotted paths match from the root.  The masking walks
    dicts, lists, and tuples; scalars pass through unchanged.

    This is the helper used in byte-determinism tests and by
    :class:`ReplayRunner` when ``spec.timing == "fast"``.
    """
    field_set = frozenset(fields)
    plain_keys = {f for f in field_set if "." not in f}
    dotted_paths = tuple(f.split(".") for f in field_set if "." in f)

    def _walk(node: Any, path: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            result: dict[str, Any] = {}
            for k, v in node.items():
                if not isinstance(k, str):
                    result[k] = _walk(v, path)
                    continue
                if k in plain_keys:
                    continue
                new_path = path + (k,)
                if any(len(dp) == len(new_path) and tuple(dp) == new_path for dp in dotted_paths):
                    continue
                result[k] = _walk(v, new_path)
            return result
        if isinstance(node, list):
            return [_walk(item, path) for item in node]
        if isinstance(node, tuple):
            return tuple(_walk(item, path) for item in node)
        return node

    return _walk(copy.deepcopy(value), ())


def _record_sequence(record: dict[str, Any]) -> int | None:
    seq = record.get("sequence")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


# ── Provider version match ───────────────────────────────────────


_UNKNOWN_VERSION = "unknown"


def check_provider_versions(
    bundle: RunBundle,
    installed: dict[str, str],
    *,
    force: bool = False,
) -> list[VersionMismatch]:
    """Compare bundle-captured versions against installed versions.

    Returns a list of :class:`VersionMismatch` records — empty when
    every bundle version matches the installed version.

    An installed provider that the bundle never captured is reported as
    a ``"MISSING"`` mismatch rather than silently skipped: an un-captured
    provider version is exactly the case where determinism cannot be
    guaranteed, so ARTIFACT replays must surface it under the same force
    policy as the explicit ``"UNKNOWN"`` sentinel.

    This function never raises.  Callers decide whether a non-empty list
    should abort or warn; :class:`ReplayRunner` applies the replay
    provider-version policy:

    * ``ARTIFACT`` + ``force=False`` → raise
      :class:`ProviderVersionMismatchError`
    * ``ARTIFACT`` + ``force=True`` → log a warning and downgrade the
      result's fidelity label to :attr:`ReplayFidelity.LIVE`
    * ``LIVE`` / ``SIMULATED`` → log a warning only (LIVE is
      non-deterministic by definition and SIMULATED is documented as
      best-effort)
    """
    _ = force  # the caller applies the policy; we only compare
    mismatches: list[VersionMismatch] = []
    captured = bundle.manifest.provider_versions
    for provider, installed_version in installed.items():
        bundle_version_raw = captured.get(provider)
        bundle_version = _stringify_version(bundle_version_raw)
        # A ``None`` installed version means the provider could not report
        # one — fold it into the explicit ``UNKNOWN`` sentinel policy.
        # Preserve explicit empty-string versions so captured and installed
        # custom providers that report ``""`` still compare equal.
        installed_version_str = _stringify_version(installed_version)
        installed_str = (
            _UNKNOWN_VERSION if installed_version_str is None else installed_version_str
        )
        if bundle_version is None:
            # Installed provider not captured in bundle.  Determinism
            # can't be guaranteed against a version we never recorded, so
            # surface it as MISSING rather than silently treating it as a
            # match.
            mismatches.append(
                VersionMismatch(
                    provider=provider,
                    bundle_version=_UNKNOWN_VERSION,
                    installed_version=installed_str,
                    code="MISSING",
                )
            )
            continue
        if bundle_version == _UNKNOWN_VERSION or installed_str == _UNKNOWN_VERSION:
            mismatches.append(
                VersionMismatch(
                    provider=provider,
                    bundle_version=bundle_version,
                    installed_version=installed_str,
                    code="UNKNOWN",
                )
            )
            continue
        if bundle_version != installed_str:
            mismatches.append(
                VersionMismatch(
                    provider=provider,
                    bundle_version=bundle_version,
                    installed_version=installed_str,
                    code="MISMATCH",
                )
            )
    return mismatches


def _stringify_version(value: Any) -> str | None:
    """Normalize a ``version_info()`` result to a comparable string.

    ``version_info()`` often returns a dict
    (``{"sdk_version": ..., "model": ...}``), but bundles may store either the
    dict or a pre-joined string.
    We stringify via ``repr`` for dicts so equality-comparison is stable
    across captures and installs.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Sort keys so two equivalent dicts compare equal.
        return repr({k: value[k] for k in sorted(value)})
    return str(value)


# ── Committable checkpoint helpers ───────────────────────────────


def find_nearest_committable(
    checkpoints: Sequence[CommittableCheckpoint],
    sequence: int,
) -> tuple[int | None, int | None]:
    """Return ``(before, after)`` — the nearest committable sequences.

    ``before`` is the highest committable sequence ``<= sequence`` or
    ``None`` when no earlier checkpoint exists.  ``after`` is the lowest
    committable sequence ``> sequence`` or ``None`` when no later one
    exists.
    """
    before: int | None = None
    after: int | None = None
    for cp in checkpoints:
        if cp.sequence <= sequence:
            if before is None or cp.sequence > before:
                before = cp.sequence
        else:
            if after is None or cp.sequence < after:
                after = cp.sequence
    return before, after


# ── Bundle-level replay orchestrator ─────────────────────────────


class ReplayRunner:
    """Walk a :class:`RunBundle` under a :class:`ReplaySpec`.

    The runner does three things:

    1. Validates the replay is legal — provider versions match (or are
       force-allowed) and ``spec.from_sequence`` sits on a committable
       boundary when one is required.
    2. Walks the bundle's journal records and produces
       :class:`ReplayFrame` objects with artifact blobs attached.
    3. Invokes the built-in stages' :meth:`Stage.replay` implementations
       with turn-scoped cassettes and verifies their outputs against the
       recording.
    4. Enforces :attr:`ReplaySpec.tool_policy` on any tool-call records
       surfaced by the walk.

    Replay never guesses how to construct credentialed provider clients.
    Built-in ``Stage.replay`` implementations therefore rehydrate
    ARTIFACT/SIMULATED outputs and expose LIVE inputs. Applications that
    own fresh providers may pass ``stage_replayers`` to execute those
    inputs and compare the fresh result with the recording. Likewise,
    ALLOW tool calls run only when ``tool_executor`` is supplied.
    """

    def __init__(
        self,
        bundle: RunBundle,
        spec: ReplaySpec,
        *,
        installed_versions: dict[str, str] | None = None,
        stage_replayers: dict[str, Callable[[ReplaySpec, ReplayCassette], Any]] | None = None,
        tool_executor: Callable[[dict[str, Any]], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bundle = bundle
        self._spec = spec
        self._installed_versions = dict(installed_versions or {})
        self._stage_replayers = dict(stage_replayers or {})
        self._tool_executor = tool_executor
        self._sleep = sleep

    def run(self) -> ReplayResult:
        """Produce a :class:`ReplayResult` for the configured bundle+spec."""
        effective_fidelity = self._apply_version_check()
        self._validate_entry_point()

        replay_records = list(self._iter_records())
        frames: list[ReplayFrame] = []
        blocked: list[str] = []
        stubbed: list[str] = []
        allowed: list[str] = []
        executed: list[str] = []
        side_effecting = False
        previous_timing_ns: int | None = None

        mask_fields = REPLAY_IGNORE_FIELDS if self._spec.timing == "fast" else frozenset()

        for record in replay_records:
            frame_stage = _infer_stage(record)
            if self._spec.stage_filter and frame_stage not in self._spec.stage_filter:
                continue

            previous_timing_ns = self._pace_record(record, previous_timing_ns)

            name = record.get("name", "") or ""
            frame_side_effecting = self._apply_tool_policy(
                record,
                blocked=blocked,
                stubbed=stubbed,
                allowed=allowed,
                executed=executed,
            )
            side_effecting = side_effecting or frame_side_effecting

            masked_data = mask_nondeterministic(record.get("data") or {}, mask_fields)
            masked_error = mask_nondeterministic(record.get("error"), mask_fields)

            input_ref = record.get("input_ref")
            output_ref = record.get("output_ref")
            sequence = _record_sequence(record)
            if sequence is None:
                continue
            frame = ReplayFrame(
                sequence=sequence,
                stage=frame_stage,
                kind=str(record.get("kind") or ""),
                name=name,
                turn_id=safe_turn_id(record.get("turn_id")),
                data=masked_data,
                input_blob=self._bundle.artifact_blobs.get(input_ref) if input_ref else None,
                output_blob=(self._bundle.artifact_blobs.get(output_ref) if output_ref else None),
                input_ref=input_ref,
                output_ref=output_ref,
                error=masked_error if isinstance(masked_error, dict) else None,
                side_effecting=frame_side_effecting,
            )
            frames.append(frame)

        stage_replays = self._run_stage_replays(
            replay_records,
            effective_fidelity=effective_fidelity,
        )
        return ReplayResult(
            frames=frames,
            fidelity_label=effective_fidelity,
            stage_replays=stage_replays,
            side_effecting=side_effecting,
            blocked_tool_calls=blocked,
            stubbed_tool_calls=stubbed,
            allowed_tool_calls=allowed,
            executed_tool_calls=executed,
        )

    # ── Internal helpers ─────────────────────────────────────────

    def _pace_record(
        self,
        record: dict[str, Any],
        previous_timing_ns: int | None,
    ) -> int | None:
        if self._spec.timing != "wall":
            return previous_timing_ns
        current_timing_ns = _record_timing_ns(record)
        if current_timing_ns is None:
            return previous_timing_ns
        if previous_timing_ns is not None and current_timing_ns > previous_timing_ns:
            delay_ns = min(current_timing_ns - previous_timing_ns, _MAX_WALL_REPLAY_DELAY_NS)
            self._sleep(delay_ns / 1_000_000_000)
        return current_timing_ns

    def _apply_tool_policy(
        self,
        record: dict[str, Any],
        *,
        blocked: list[str],
        stubbed: list[str],
        allowed: list[str],
        executed: list[str],
    ) -> bool:
        if not _is_tool_phase(record):
            return False

        descriptor = _tool_descriptor(record)
        policy = self._spec.tool_policy
        if policy is ToolReplayPolicy.DENY:
            blocked.append(descriptor)
            raise ReplaySideEffectBlocked(
                f"Tool call {descriptor!r} blocked by ToolReplayPolicy.DENY "
                f"at sequence {record.get('sequence')}"
            )
        if policy is ToolReplayPolicy.STUB:
            stubbed.append(descriptor)
            return False

        allowed.append(descriptor)
        if self._tool_executor is None:
            logger.warning(
                "Replay: ToolReplayPolicy.ALLOW permitted recorded frame %s, "
                "but no tool executor is configured; no side effect was executed.",
                descriptor,
            )
            return False
        if not _is_tool_invocation(record):
            return False
        sequence = _record_sequence(record)
        if (
            sequence is None
            or (self._spec.from_sequence is not None and sequence < self._spec.from_sequence)
            or self._spec.to_sequence is not None
            and sequence > self._spec.to_sequence
        ):
            logger.warning(
                "Replay: skipped ToolReplayPolicy.ALLOW execution for %s because "
                "its sequence is malformed or outside the requested range.",
                descriptor,
            )
            return False

        result = self._tool_executor(copy.deepcopy(record))
        if inspect.isawaitable(result):
            raise ReplayError(
                "Async tool executors are not supported by synchronous replay; "
                "provide a synchronous executor.",
                requested_sequence=_record_sequence(record),
            )
        executed.append(descriptor)
        logger.warning(
            "Replay: ToolReplayPolicy.ALLOW executed %s; result is side-effecting.",
            descriptor,
        )
        return True

    def _iter_records(self) -> Iterable[dict[str, Any]]:
        low = self._spec.from_sequence
        high = self._spec.to_sequence
        for record in self._bundle.records():
            seq = _record_sequence(record)
            if seq is None:
                if _is_tool_phase(record):
                    yield record
                continue
            if low is not None and seq < low:
                continue
            if high is not None and seq > high:
                continue
            yield record

    def _apply_version_check(self) -> ReplayFidelity:
        if not self._installed_versions:
            return self._spec.fidelity
        mismatches = check_provider_versions(
            self._bundle, self._installed_versions, force=self._spec.force
        )
        if not mismatches:
            return self._spec.fidelity

        unknown = any(m.code in ("UNKNOWN", "MISSING") for m in mismatches)
        message = _format_version_mismatch(mismatches)
        error_code = "PROVIDER_VERSION_UNKNOWN" if unknown else "PROVIDER_VERSION_MISMATCH"

        fidelity = self._spec.fidelity
        if fidelity is ReplayFidelity.ARTIFACT and not self._spec.force:
            raise ProviderVersionMismatchError(
                message, error_code=error_code, mismatches=mismatches
            )
        if fidelity is ReplayFidelity.ARTIFACT and self._spec.force:
            logger.warning(
                "Replay: ARTIFACT fidelity with force=True under version mismatch "
                "— downgrading effective fidelity to LIVE. Details: %s",
                message,
            )
            return ReplayFidelity.LIVE
        # LIVE / SIMULATED — warn only.
        logger.warning("Replay: provider version mismatch under %s: %s", fidelity, message)
        return fidelity

    def _validate_entry_point(self) -> None:
        if self._spec.from_sequence is None:
            return
        checkpoints = self._bundle.replay_entry_points
        if not checkpoints:
            # No committable boundaries declared — nothing to validate.
            return
        seq = self._spec.from_sequence
        committable_seqs = {cp.sequence for cp in checkpoints}
        if seq in committable_seqs:
            return
        before, after = find_nearest_committable(checkpoints, seq)
        raise ReplayError(
            (
                f"Replay start sequence {seq} is not a committable boundary. "
                f"Nearest committable before={before}, after={after}."
            ),
            requested_sequence=seq,
            nearest_committable_before=before,
            nearest_committable_after=after,
        )

    def _run_stage_replays(
        self,
        records: Sequence[dict[str, Any]],
        *,
        effective_fidelity: ReplayFidelity,
    ) -> list[StageReplayResult]:
        built_ins = _built_in_stage_replayers()
        replayers = {**built_ins, **self._stage_replayers}
        grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        for record in records:
            stage = _infer_stage(record)
            if not stage or stage not in replayers:
                continue
            if self._spec.stage_filter and stage not in self._spec.stage_filter:
                continue
            turn_id = safe_turn_id(record.get("turn_id"))
            grouped.setdefault((stage, turn_id), []).append(record)

        replay_spec = replace(self._spec, fidelity=effective_fidelity)
        outcomes: list[StageReplayResult] = []
        blobs = self._bundle.artifact_blobs

        for (stage, turn_id), cassette_records in grouped.items():
            cassette = ReplayCassette(
                stage_name=stage,
                records=tuple(cassette_records),
                _resolver=blobs.get,
            )
            output = replayers[stage](replay_spec, cassette)
            if inspect.isawaitable(output):
                raise ReplayError(
                    "Async stage replayers are not supported by synchronous replay; "
                    "provide a synchronous replayer.",
                    requested_sequence=_first_sequence(cassette_records),
                    stage=stage,
                )

            matches: bool | None = None
            is_custom_replayer = stage in self._stage_replayers
            should_compare = stage in built_ins and (
                effective_fidelity is not ReplayFidelity.LIVE or is_custom_replayer
            )
            if should_compare:
                recorded_fidelity = (
                    ReplayFidelity.ARTIFACT
                    if effective_fidelity is ReplayFidelity.LIVE
                    else effective_fidelity
                )
                recorded_spec = replace(
                    replay_spec,
                    fidelity=recorded_fidelity,
                    overrides={},
                )
                expected = built_ins[stage](recorded_spec, cassette)
                expected_digest = _replay_value_digest(expected)
                actual_digest = _replay_value_digest(output)
                matches = actual_digest == expected_digest
                if not matches:
                    detail = (
                        f"stage={stage!r}, turn_id={turn_id!r}, "
                        f"expected_digest={expected_digest}, actual_digest={actual_digest}"
                    )
                    raise ReplayDivergenceError(
                        detail,
                        stage=stage,
                        turn_id=turn_id,
                        expected_digest=expected_digest,
                        actual_digest=actual_digest,
                        requested_sequence=_first_sequence(cassette_records),
                    )
            else:
                actual_digest = _replay_value_digest(output)

            sequences = [
                seq for record in cassette_records if (seq := _record_sequence(record)) is not None
            ]
            outcomes.append(
                StageReplayResult(
                    stage=stage,
                    turn_id=turn_id,
                    from_sequence=min(sequences) if sequences else None,
                    to_sequence=max(sequences) if sequences else None,
                    fidelity=effective_fidelity,
                    output=output,
                    output_digest=actual_digest,
                    matches_recording=matches,
                )
            )
        return outcomes


# ── Private record helpers ───────────────────────────────────────


_STAGE_NAMES: frozenset[str] = frozenset(
    {"stt", "tts", "vad", "agent", "audio", "transport", "telephony", "turn"}
)


def _infer_stage(record: dict[str, Any]) -> str:
    """Best-effort stage name from a raw journal record.

    Stages stamp their ``name`` attribute into the record's
    ``data["stage"]`` field (see the ``_record`` helpers on each stage).
    Control and framework records live outside a stage and may still
    carry an ``observed_stage`` hint.  Records without either report
    ``""`` — downstream callers treat the empty string as "not a stage
    record" for filtering purposes.
    """
    data = record.get("data") or {}
    if isinstance(data, dict):
        stage = data.get("stage")
        if isinstance(stage, str) and stage:
            return stage
        observed = data.get("observed_stage")
        if isinstance(observed, str) and observed:
            return observed
    return ""


def _record_timing_ns(record: dict[str, Any]) -> int | None:
    """Return a record's monotonic/wall timestamp for wall-paced replay."""
    timing = record.get("timing")
    if isinstance(timing, dict):
        for key in ("mono_ns", "wall_ns"):
            value = timing.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    # Crash-dump projections may flatten the timestamp.
    for key in ("mono_ns", "wall_ns"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _is_tool_phase(record: dict[str, Any]) -> bool:
    kind = str(record.get("kind") or "")
    if kind == "framework_transition":
        data = record.get("data") or {}
        if isinstance(data, dict) and data.get("phase"):
            return True
    name = record.get("name") or ""
    return isinstance(name, str) and name.startswith("tool_")


def _is_tool_invocation(record: dict[str, Any]) -> bool:
    data = record.get("data") or {}
    if isinstance(data, dict):
        phase = str(data.get("phase") or "").lower()
        if phase:
            return phase in {"start", "started", "call", "request"}
    name = str(record.get("name") or "").lower()
    return name in {"tool_call", "tool_call_started", "tool_started"}


def _tool_descriptor(record: dict[str, Any]) -> str:
    data = record.get("data") or {}
    if isinstance(data, dict):
        tool = data.get("tool_name") or data.get("name") or ""
        call_id = data.get("tool_call_id") or data.get("call_id") or ""
        if tool and call_id:
            return f"{tool}({call_id})"
        if tool:
            return str(tool)
        if call_id:
            return f"call_id={call_id}"
    return str(record.get("name") or "<tool>")


def _format_version_mismatch(mismatches: Sequence[VersionMismatch]) -> str:
    parts = []
    for m in mismatches:
        parts.append(
            f"{m.provider}: bundle={m.bundle_version!r} "
            f"installed={m.installed_version!r} ({m.code})"
        )
    return "; ".join(parts)


def _first_sequence(records: Sequence[dict[str, Any]]) -> int | None:
    return next(
        (seq for record in records if (seq := _record_sequence(record)) is not None),
        None,
    )


def _replay_value_digest(value: Any) -> str:
    """Return a stable digest for a stage replay value."""
    if isinstance(value, bytes):
        payload = b"bytes\0" + value
    else:
        try:
            serialized = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                default=repr,
            )
        except (TypeError, ValueError):
            serialized = repr(value)
        payload = f"{type(value).__module__}.{type(value).__qualname__}\0{serialized}".encode()
    return hashlib.sha256(payload).hexdigest()


def _built_in_stage_replayers() -> dict[
    str,
    Callable[[ReplaySpec, ReplayCassette], Any],
]:
    """Return provider-free adapters for every built-in ``Stage.replay``.

    The replay methods are deliberately pure and do not read instance
    state. Constructing the stage normally would require provider
    credentials that a redacted bundle cannot and must not contain, so
    the adapters allocate an uninitialized instance solely to invoke
    the shipped replay implementation.
    """
    from easycat.stages.agent import AgentStage
    from easycat.stages.audio import AudioStage
    from easycat.stages.stt import STTStage
    from easycat.stages.transport import TransportStage
    from easycat.stages.tts import TTSStage
    from easycat.stages.turn import TurnStage
    from easycat.stages.vad import VADStage

    stage_types = {
        "agent": AgentStage,
        "audio": AudioStage,
        "stt": STTStage,
        "transport": TransportStage,
        "tts": TTSStage,
        "turn": TurnStage,
        "vad": VADStage,
    }

    def _adapter(stage_type: type[Any]) -> Callable[[ReplaySpec, ReplayCassette], Any]:
        def replay(spec: ReplaySpec, cassette: ReplayCassette) -> Any:
            stage = object.__new__(stage_type)
            return stage_type.replay(stage, spec, cassette)

        return replay

    return {name: _adapter(stage_type) for name, stage_type in stage_types.items()}


# ── End-to-end audio emitter ─────────────────────────────────────


@dataclass(frozen=True)
class ReplayAudioChunk:
    """One TTS audio chunk reconstructed from a bundle.

    The ``data`` field is bit-equal to what ``Session`` emitted to its
    transport during the live recording; the format fields describe the
    chunk's PCM layout so callers can resample or mix without going
    back to the journal.
    """

    sequence: int
    data: bytes
    sample_rate: int
    channels: int
    sample_width: int
    encoding: str
    duration_ms: float
    turn_id: str | None
    bypass_gate: bool


def _stage_matches(record: dict[str, Any], stage: str) -> bool:
    data = record.get("data") or {}
    if not isinstance(data, dict):
        return False
    return data.get("stage") == stage or data.get("observed_stage") == stage


def _audio_metadata_int(data: dict[str, Any], key: str) -> int:
    """Safely coerce optional integer audio metadata from a bundle."""
    value = data.get(key)
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _audio_metadata_float(data: dict[str, Any], key: str) -> float:
    """Safely coerce optional finite float audio metadata from a bundle."""
    value = data.get(key)
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def replay_stt_audio(
    bundle: RunBundle,
    *,
    turn_id: str | None = None,
    include_preroll: bool = True,
) -> list[ReplayAudioChunk]:
    """Reconstruct the audio the session handed to STT during recording.

    Walks *bundle*'s journal for STTStage ``stage_start`` records (the
    stage stamps one per input chunk with ``input_ref`` pointing at the
    captured bytes).  Pass ``turn_id`` to narrow to one turn.  The
    ``include_preroll`` flag is retained for API stability — stage
    records don't carry a preroll flag today, so it's currently a no-op.

    This is what a LIVE-fidelity replay would feed to a fresh STT
    provider to re-run transcription offline.
    """
    _ = include_preroll
    chunks: list[ReplayAudioChunk] = []
    for record in bundle.records():
        if record.get("name") != "stage_start":
            continue
        if not _stage_matches(record, "stt"):
            continue
        if turn_id is not None and record.get("turn_id") != turn_id:
            continue
        sequence = int(record.get("sequence") or 0)
        input_ref = record.get("input_ref")
        if not input_ref:
            # STT stage_start without input_ref — no audio bytes were
            # captured for this chunk (e.g. artifact store absent).
            # Skip rather than raise: a mix of captured/uncaptured
            # chunks is still a useful subset.
            continue
        blob = bundle.artifact_blobs.get(input_ref)
        if blob is None:
            raise ReplayError(
                f"STT stage_start input_ref {input_ref!r} at sequence {sequence} "
                "is missing from bundle artifacts",
                requested_sequence=sequence,
                stage="stt",
            )
        data = record.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        chunks.append(
            ReplayAudioChunk(
                sequence=sequence,
                data=blob,
                sample_rate=_audio_metadata_int(data, "sample_rate"),
                channels=_audio_metadata_int(data, "channels"),
                sample_width=_audio_metadata_int(data, "sample_width"),
                encoding=str(data.get("encoding") or ""),
                duration_ms=0.0,
                turn_id=record.get("turn_id"),
                bypass_gate=False,
            )
        )
    return chunks


def replay_audio(
    bundle: RunBundle,
    *,
    turn_id: str | None = None,
) -> list[ReplayAudioChunk]:
    """Reconstruct the audio chunks the user heard during the recording.

    Walks *bundle*'s journal for TTSStage ``tts_frame`` records — one
    per audio chunk emitted, carrying ``output_ref`` pointing at the
    captured bytes.  Returns them in journal-sequence order; pass
    ``turn_id`` to narrow to one turn.

    Concatenating ``chunk.data`` for every returned chunk yields the
    byte stream Session pushed to its outbound transport.  No live
    providers involved.

    Raises :class:`ReplayError` when a ``tts_frame`` record has a ref
    but the bundle is missing that artifact — byte-identical replay is
    impossible in that case.  Records with no ``output_ref`` at all are
    skipped (capture was disabled).
    """
    chunks: list[ReplayAudioChunk] = []
    for record in bundle.records():
        if record.get("name") != "tts_frame":
            continue
        if not _stage_matches(record, "tts"):
            continue
        if turn_id is not None and record.get("turn_id") != turn_id:
            continue
        sequence = int(record.get("sequence") or 0)
        output_ref = record.get("output_ref")
        if not output_ref:
            continue
        blob = bundle.artifact_blobs.get(output_ref)
        if blob is None:
            raise ReplayError(
                f"tts_frame output_ref {output_ref!r} at sequence {sequence} "
                "is missing from bundle artifacts",
                requested_sequence=sequence,
                stage="tts",
            )
        data = record.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        chunks.append(
            ReplayAudioChunk(
                sequence=sequence,
                data=blob,
                sample_rate=_audio_metadata_int(data, "sample_rate"),
                channels=_audio_metadata_int(data, "channels"),
                sample_width=_audio_metadata_int(data, "sample_width"),
                encoding=str(data.get("encoding") or ""),
                duration_ms=_audio_metadata_float(data, "duration_ms"),
                turn_id=record.get("turn_id"),
                bypass_gate=False,
            )
        )
    return chunks
