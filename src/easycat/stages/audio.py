"""AudioStage — wraps EchoCanceller + NoiseReducer with journal recording."""

from __future__ import annotations

import logging
import time
from typing import Any

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.runtime.context import RunContext
from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    audio_format_fields,
    journal_append_control_signal,
    journal_append_event,
    journal_ctx,
    live_replay_input,
    put_artifact,
    put_artifact_async,
    record_stage_failure,
)

logger = logging.getLogger(__name__)


class AudioStage:
    """Stage wrapper around :class:`EchoCanceller` and :class:`NoiseReducer`.

    ``execute`` feeds the input chunk through the AEC → NR chain: echo
    cancellation runs on the raw mic signal *before* noise reduction
    because NR's nonlinear processing breaks AEC convergence. It records
    the raw input bytes as ``input_ref`` on ``stage_start`` and the
    processed output as ``output_ref`` on ``stage_complete`` so LIVE
    replay can re-drive a fresh NR backend and ARTIFACT replay can skip
    processing entirely.
    """

    name = "audio"

    def __init__(
        self,
        provider: Any,
        *,
        echo_canceller: Any = None,
        journal: Any = None,
    ) -> None:
        self._provider = provider
        self._echo_canceller = echo_canceller
        # Fallback recording sink used only when ``ctx.journal`` is None
        # (see ``_journal_ctx``); recording normally flows through ctx.
        self._journal = journal

    def _journal_ctx(self, ctx: RunContext) -> RunContext:
        """Return *ctx*, substituting the constructor journal as a fallback."""
        return journal_ctx(ctx, self._journal)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        ctx = self._journal_ctx(ctx)
        started = time.perf_counter()
        result_attr = "pass"
        state_before = self.snapshot_state()
        raw_bytes = getattr(input, "data", None) if not isinstance(input, bytes) else input
        input_ref = await put_artifact_async(ctx, raw_bytes)
        start_extra = {
            "audio_bytes": len(raw_bytes) if isinstance(raw_bytes, (bytes, bytearray)) else 0,
        }
        start_extra.update(audio_format_fields(input))
        start_sequence = journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
            input_ref=input_ref,
            data_extra=start_extra,
        )
        # Track which component is in flight so a raised exception is
        # attributed to the provider that actually failed (the noise reducer
        # vs. the echo canceller), not always to ``self._provider``.
        error_provider = type(self._provider).__name__.lower()
        try:
            chunk = input
            # Echo cancellation runs first, on the raw mic signal, because the
            # noise reducer's nonlinear processing would break AEC convergence.
            if self._echo_canceller is not None:
                error_provider = type(self._echo_canceller).__name__.lower()
                chunk = await self._echo_canceller.process(chunk)
            # Noise reduction runs on the echo-cancelled signal.
            error_provider = type(self._provider).__name__.lower()
            chunk = await self._provider.process(chunk)
            result = chunk
        except Exception as exc:
            result_attr = "fail"
            elapsed_ms = (time.perf_counter() - started) * 1000
            record_stage_failure(
                exc,
                ctx,
                stage=self.name,
                # ``error_provider`` tracks the in-flight component (echo
                # canceller vs. noise reducer) so the failure is attributed to
                # the provider that actually raised, not always self._provider.
                provider=error_provider,
                surface="stt",
                elapsed_ms=elapsed_ms,
                sequence=start_sequence,
                turn_id=turn.id,
                state_before=state_before,
            )
            raise
        finally:
            observability.record_histogram(
                "easycat.stage.latency",
                time.perf_counter() - started,
                {"easycat.stage": self.name, "easycat.result": result_attr},
            )
        state_after = self.snapshot_state()
        processed_bytes = (
            getattr(result, "data", None) if not isinstance(result, bytes) else result
        )
        output_ref = await put_artifact_async(ctx, processed_bytes)
        complete_extra = {
            "audio_bytes": (
                len(processed_bytes) if isinstance(processed_bytes, (bytes, bytearray)) else 0
            ),
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }
        complete_extra.update(audio_format_fields(result))
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
            output_ref=output_ref,
            data_extra=complete_extra,
        )
        return result

    def record_reference(
        self,
        chunk: Any,
        ctx: RunContext,
        turn: TurnContext,
    ) -> None:
        """Journal one AEC far-end reference frame (the bot playback fed to AEC).

        The reference (far-end) audio handed to the echo canceller is the one
        leg of the AEC chain that the pipeline never journals on its own: the
        captured mic-in lands as the audio stage's ``stage_start`` ``input_ref``
        and the post-AEC result as its ``stage_complete`` ``output_ref``, but
        the bot's own playback subtracted from the mic is fed straight into the
        canceller as a delivery side effect.  Capturing it under
        :data:`AEC_REFERENCE_FRAME_NAME` (mirroring TTSStage's ``tts_frame``)
        lets the debugger align all three tracks on the timeline and compute
        ERLE / double-talk / self-echo.

        This is best-effort and additive: the caller invokes it only when AEC
        is enabled and an artifact store is present, and when there is nothing
        to store (no store, empty payload, or over the size cap) the frame is
        simply skipped — no record, no raise.  A capture failure must never
        disturb the live audio delivery path.
        """
        ctx = self._journal_ctx(ctx)
        raw_bytes = getattr(chunk, "data", None) if not isinstance(chunk, bytes) else chunk
        ref = put_artifact(ctx, raw_bytes)
        if ref is None:
            return
        extra = {
            "audio_bytes": len(raw_bytes) if isinstance(raw_bytes, (bytes, bytearray)) else 0,
        }
        extra.update(audio_format_fields(chunk))
        duration = getattr(chunk, "duration_ms", None)
        if duration is not None:
            extra["duration_ms"] = duration
        journal_append_event(
            ctx,
            stage=self.name,
            name=AEC_REFERENCE_FRAME_NAME,
            turn_id=turn.id,
            output_ref=ref,
            data_extra=extra,
        )

    def snapshot_state(self) -> StageStateSnapshot:
        fields: dict[str, Any] = {
            "noise_reducer": type(self._provider).__name__,
        }
        if self._echo_canceller is not None:
            fields["echo_canceller"] = type(self._echo_canceller).__name__
        return StageStateSnapshot(stage_name=self.name, fields=fields)

    def replay(
        self,
        spec: ReplaySpec,
        cassette: ReplayCassette | None = None,
    ) -> Any:
        """Replay Audio (NR/AEC) stage.

        ``ARTIFACT`` returns the captured processed audio from the
        cassette's output ref.  ``LIVE`` returns the raw input bytes so
        the caller can re-run the NR/AEC pipeline against a backend at
        the same version.
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            return live_replay_input(spec, cassette)

        if "audio" in overrides or "result" in overrides:
            return overrides.get("audio", overrides.get("result"))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                blob = cassette.blob(record.get("output_ref"))
                if blob is not None:
                    return blob
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("audio", "result"):
                        if key in data:
                            return data[key]
        return None

    async def handle_upstream(
        self,
        signal: ControlSignal,
        ctx: RunContext | None = None,
    ) -> None:
        """Observe and journal an upstream control signal (no cancel here).

        Cancellation is owned by the ``CancelOrchestrator`` / turn runner;
        this method only records that the stage saw the signal.
        """
        logger.debug("AudioStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(self._journal_ctx(ctx), stage=self.name, signal=signal)
