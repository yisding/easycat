"""TTSStage — wraps a TTSProvider with journal recording and audio capture."""

from __future__ import annotations

import contextlib
import inspect
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    audio_format_fields,
    journal_append_control_signal,
    journal_append_event,
    journal_ctx,
    live_replay_input,
    put_artifact_async,
    record_stage_failure,
)

logger = logging.getLogger(__name__)


class TTSStage:
    """Stage wrapper around a :class:`TTSProvider`.

    Records one ``stage_start`` at synthesis start and one
    ``stage_complete`` at stream end.  Each audio-bearing TTS event
    emits an additional ``tts_frame`` record whose ``output_ref`` points
    at the chunk's bytes in the artifact store — concatenating those
    blobs in journal order reproduces the outbound stream bit-for-bit.
    """

    name = "tts"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        self._journal = journal

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        ctx = journal_ctx(ctx, self._journal)
        capture_enabled = ctx.journal is not None or ctx.artifact_store is not None
        started = time.perf_counter()
        state_before = self.snapshot_state() if capture_enabled else None
        start_sequence = None
        if capture_enabled:
            start_sequence = journal_append_event(
                ctx,
                stage=self.name,
                name="stage_start",
                turn_id=turn.id,
                state_before=state_before,
            )
        try:
            result = self._provider.synthesize(input)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            record_stage_failure(
                exc,
                ctx,
                stage=self.name,
                provider=type(self._provider).__name__.lower(),
                surface="tts",
                elapsed_ms=elapsed_ms,
                sequence=start_sequence,
                turn_id=turn.id,
                state_before=state_before,
            )
            raise

        if isinstance(result, AsyncIterator) or inspect.isasyncgen(result):
            if not capture_enabled:
                return self._wrap_stream_without_capture(result, ctx, turn.id)
            return self._wrap_stream(result, ctx, turn.id, state_before, start_sequence)

        if capture_enabled:
            state_after = self.snapshot_state()
            elapsed_ms = (time.perf_counter() - started) * 1000
            journal_append_event(
                ctx,
                stage=self.name,
                name="stage_complete",
                turn_id=turn.id,
                state_before=state_before,
                state_after=state_after,
                data_extra={"elapsed_ms": elapsed_ms},
            )
        return result

    async def _wrap_stream_without_capture(
        self,
        stream: Any,
        ctx: RunContext,
        turn_id: str,
    ) -> AsyncIterator[Any]:
        """Stream provider events without constructing replay-only metadata."""
        started = time.perf_counter()
        result_attr = "pass"
        try:
            span = (
                observability.span(
                    "easycat.tts.synthesize",
                    {"easycat.stage": self.name, "easycat.surface": "tts"},
                )
                if observability.tracing_available()
                else contextlib.nullcontext()
            )
            with span:
                async for event in stream:
                    yield event
        except Exception as exc:
            result_attr = "fail"
            elapsed_ms = (time.perf_counter() - started) * 1000
            record_stage_failure(
                exc,
                ctx,
                stage=self.name,
                provider=type(self._provider).__name__.lower(),
                surface="tts",
                elapsed_ms=elapsed_ms,
                sequence=None,
                turn_id=turn_id,
                state_before=None,
            )
            raise
        finally:
            if observability.metrics_available():
                observability.record_histogram(
                    "easycat.stage.latency",
                    time.perf_counter() - started,
                    {"easycat.stage": self.name, "easycat.result": result_attr},
                )

    async def _wrap_stream(
        self,
        stream: Any,
        ctx: RunContext,
        turn_id: str,
        state_before: StageStateSnapshot | None,
        start_sequence: int | None,
    ) -> AsyncIterator[Any]:
        """Iterate *stream*, capture audio bytes per frame, yield each event.

        Each TTSEvent with audio gets a ``tts_frame`` journal record with
        ``output_ref`` pointing at the captured bytes.  Byte-identical
        replay walks these records in order and concatenates the blobs.
        """
        frame_count = 0
        total_bytes = 0
        started = time.perf_counter()
        result_attr = "pass"
        try:
            span = (
                observability.span(
                    "easycat.tts.synthesize",
                    {"easycat.stage": self.name, "easycat.surface": "tts"},
                )
                if observability.tracing_available()
                else contextlib.nullcontext()
            )
            with span:
                async for event in stream:
                    audio = getattr(event, "audio", None)
                    audio_bytes = getattr(audio, "data", None) if audio is not None else None
                    if audio_bytes:
                        output_ref = await put_artifact_async(ctx, audio_bytes)
                        extra = {
                            "audio_bytes": len(audio_bytes),
                            "frame_index": frame_count,
                        }
                        extra.update(audio_format_fields(audio))
                        duration = getattr(audio, "duration_ms", None)
                        if duration is not None:
                            extra["duration_ms"] = duration
                        journal_append_event(
                            ctx,
                            stage=self.name,
                            name="tts_frame",
                            turn_id=turn_id,
                            output_ref=output_ref,
                            data_extra=extra,
                        )
                        frame_count += 1
                        total_bytes += len(audio_bytes)
                    yield event
        except Exception as exc:
            result_attr = "fail"
            elapsed_ms = (time.perf_counter() - started) * 1000
            record_stage_failure(
                exc,
                ctx,
                stage=self.name,
                provider=type(self._provider).__name__.lower(),
                surface="tts",
                elapsed_ms=elapsed_ms,
                sequence=start_sequence,
                turn_id=turn_id,
                state_before=state_before,
            )
            raise
        finally:
            if observability.metrics_available():
                observability.record_histogram(
                    "easycat.stage.latency",
                    time.perf_counter() - started,
                    {"easycat.stage": self.name, "easycat.result": result_attr},
                )
        state_after = self.snapshot_state()
        elapsed_ms = (time.perf_counter() - started) * 1000
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn_id,
            state_before=state_before,
            state_after=state_after,
            data_extra={
                "frame_count": frame_count,
                "total_bytes": total_bytes,
                "elapsed_ms": elapsed_ms,
            },
        )

    def snapshot_state(self) -> StageStateSnapshot:
        return StageStateSnapshot(
            stage_name=self.name,
            fields={"provider": type(self._provider).__name__},
        )

    def replay(
        self,
        spec: ReplaySpec,
        cassette: ReplayCassette | None = None,
    ) -> Any:
        """Replay TTS stage.

        ``ARTIFACT``/``SIMULATED`` returns the captured audio bytes —
        preferring concatenated ``tts_frame`` blobs when the cassette
        has them, falling back to ``spec.overrides["audio"]`` or the
        legacy ``stage_complete`` output ref.  ``LIVE`` returns the
        captured input text so the caller can re-run synthesis on a
        fresh provider.
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            return live_replay_input(spec, cassette, source="data_input")

        if "audio" in overrides or "result" in overrides:
            return overrides.get("audio", overrides.get("result"))
        if cassette is not None:
            frame_records = cassette.records_named("tts_frame")
            if frame_records:
                blobs = [cassette.blob(r.get("output_ref")) for r in frame_records]
                concatenated = b"".join(b for b in blobs if b is not None)
                if concatenated:
                    return concatenated
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                blob = cassette.blob(record.get("output_ref"))
                if blob is not None:
                    return blob
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("audio", "audio_bytes", "result"):
                        if key in data:
                            return data[key]
        return None

    async def handle_upstream(
        self,
        signal: ControlSignal,
        ctx: RunContext | None = None,
    ) -> None:
        """Observe and journal an upstream control signal (no cancel here).

        The real TTS cancel/truncate is driven out-of-band by the turn
        runner / ``CancelOrchestrator`` (``tts.cancel()`` + task cancel).
        This method only records that the stage saw the signal.
        """
        logger.debug("TTSStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(
                journal_ctx(ctx, self._journal), stage=self.name, signal=signal
            )
