"""Replay types and orchestration for WS4.

This module is the single source of truth for replay fidelity, tool
policy, and orchestration primitives.  ``stages.base.ReplaySpec`` used
to be a separate stub; it now re-exports from here so every stage and
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
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from easycat.stages.base import NONDETERMINISTIC_FIELDS

if TYPE_CHECKING:
    from easycat.debug.bundle import CommittableCheckpoint, RunBundle

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class VersionMismatch:
    """One provider's bundle version not matching the installed version."""

    provider: str
    bundle_version: str
    installed_version: str
    code: str  # "MISMATCH" or "UNKNOWN"


class ProviderVersionMismatchError(RuntimeError):
    """Replay bundle captured a provider version that doesn't match installed.

    ``error_code`` is ``"PROVIDER_VERSION_MISMATCH"`` for a plain version
    skew and ``"PROVIDER_VERSION_UNKNOWN"`` when either side reports the
    sentinel ``"unknown"`` string from WS1 ``version_info()``.
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


@dataclass
class ReplayResult:
    """Output of :meth:`ReplayRunner.run`.

    ``fidelity_label`` is the effective fidelity after any downgrades
    (e.g. ``ARTIFACT`` with ``force=True`` and a version mismatch is
    downgraded to ``LIVE`` because determinism is no longer guaranteed).

    ``side_effecting`` is ``True`` when at least one ``ALLOW``-policy
    tool call was observed during the walk.

    ``blocked_tool_calls`` records ``STUB``-policy substitutions and
    ``ALLOW``-policy pass-throughs so callers can tell users exactly
    what was swapped or permitted.
    """

    frames: list[ReplayFrame]
    fidelity_label: ReplayFidelity
    side_effecting: bool = False
    blocked_tool_calls: list[str] = field(default_factory=list)
    stubbed_tool_calls: list[str] = field(default_factory=list)
    allowed_tool_calls: list[str] = field(default_factory=list)


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

    This is the helper used in byte-determinism tests (AC4.18) and by
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
    every bundle version matches the installed version (or when the
    bundle simply doesn't mention a provider the caller is asking about).

    This function never raises.  Callers decide whether a non-empty list
    should abort or warn; :class:`ReplayRunner` runs the T4.2 policy:

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
        installed_str = _stringify_version(installed_version)
        if bundle_version is None:
            # Installed provider not captured in bundle — not a mismatch,
            # just an unknown pairing.  Skip.
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

    ``version_info()`` returns a dict (``{"sdk_version": ..., "model": ...}``)
    in WS1, but bundles may store either the dict or a pre-joined string.
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
    3. Enforces :attr:`ReplaySpec.tool_policy` on any tool-call records
       surfaced by the walk.

    The runner does **not** instantiate stages or call their
    ``execute()`` methods.  That's the caller's responsibility — they
    pull a :class:`ReplayCassette` from the bundle for the stage of
    interest and feed it to a fresh stage instance's
    :meth:`Stage.replay`.
    """

    def __init__(
        self,
        bundle: RunBundle,
        spec: ReplaySpec,
        *,
        installed_versions: dict[str, str] | None = None,
    ) -> None:
        self._bundle = bundle
        self._spec = spec
        self._installed_versions = dict(installed_versions or {})

    def run(self) -> ReplayResult:
        """Produce a :class:`ReplayResult` for the configured bundle+spec."""
        effective_fidelity = self._apply_version_check()
        self._validate_entry_point()

        frames: list[ReplayFrame] = []
        blocked: list[str] = []
        stubbed: list[str] = []
        allowed: list[str] = []
        side_effecting = False

        mask_fields = REPLAY_IGNORE_FIELDS if self._spec.timing == "fast" else frozenset()

        for record in self._iter_records():
            frame_stage = _infer_stage(record)
            if self._spec.stage_filter and frame_stage not in self._spec.stage_filter:
                continue

            name = record.get("name", "") or ""
            is_tool_phase = _is_tool_phase(record)
            frame_side_effecting = False

            if is_tool_phase:
                descriptor = _tool_descriptor(record)
                policy = self._spec.tool_policy
                if policy is ToolReplayPolicy.DENY:
                    blocked.append(descriptor)
                    raise ReplaySideEffectBlocked(
                        f"Tool call {descriptor!r} blocked by "
                        f"ToolReplayPolicy.DENY at sequence "
                        f"{record.get('sequence')}"
                    )
                if policy is ToolReplayPolicy.STUB:
                    stubbed.append(descriptor)
                elif policy is ToolReplayPolicy.ALLOW:
                    allowed.append(descriptor)
                    side_effecting = True
                    frame_side_effecting = True
                    logger.warning(
                        "Replay: ToolReplayPolicy.ALLOW permitted %s; result is side-effecting.",
                        descriptor,
                    )

            masked_data = mask_nondeterministic(record.get("data") or {}, mask_fields)
            masked_error = mask_nondeterministic(record.get("error"), mask_fields)

            input_ref = record.get("input_ref")
            output_ref = record.get("output_ref")
            frame = ReplayFrame(
                sequence=int(record.get("sequence") or 0),
                stage=frame_stage,
                kind=str(record.get("kind") or ""),
                name=name,
                turn_id=record.get("turn_id"),
                data=masked_data,
                input_blob=self._bundle.artifact_blobs.get(input_ref) if input_ref else None,
                output_blob=(self._bundle.artifact_blobs.get(output_ref) if output_ref else None),
                input_ref=input_ref,
                output_ref=output_ref,
                error=masked_error if isinstance(masked_error, dict) else None,
                side_effecting=frame_side_effecting,
            )
            frames.append(frame)

        return ReplayResult(
            frames=frames,
            fidelity_label=effective_fidelity,
            side_effecting=side_effecting,
            blocked_tool_calls=blocked,
            stubbed_tool_calls=stubbed,
            allowed_tool_calls=allowed,
        )

    # ── Internal helpers ─────────────────────────────────────────

    def _iter_records(self) -> Iterable[dict[str, Any]]:
        low = self._spec.from_sequence
        high = self._spec.to_sequence
        for record in self._bundle.records():
            seq = record.get("sequence")
            if seq is None:
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

        unknown = any(m.code == "UNKNOWN" for m in mismatches)
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
        if data.get("stage") in _STAGE_NAMES:
            return str(data["stage"])
        observed = data.get("observed_stage")
        if observed in _STAGE_NAMES:
            return str(observed)
    return ""


def _is_tool_phase(record: dict[str, Any]) -> bool:
    kind = str(record.get("kind") or "")
    if kind == "framework_transition":
        data = record.get("data") or {}
        if isinstance(data, dict) and data.get("phase"):
            return True
    name = record.get("name") or ""
    return isinstance(name, str) and name.startswith("tool_")


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
                sample_rate=int(data.get("sample_rate") or 0),
                channels=int(data.get("channels") or 0),
                sample_width=int(data.get("sample_width") or 0),
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
                sample_rate=int(data.get("sample_rate") or 0),
                channels=int(data.get("channels") or 0),
                sample_width=int(data.get("sample_width") or 0),
                encoding=str(data.get("encoding") or ""),
                duration_ms=float(data.get("duration_ms") or 0.0),
                turn_id=record.get("turn_id"),
                bypass_gate=False,
            )
        )
    return chunks
