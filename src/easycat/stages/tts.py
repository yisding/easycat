"""TTSStage — wraps a TTSProvider with journal recording and audio capture."""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from easycat import observability
from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    audio_format_fields,
    journal_append_control_signal,
    journal_append_event,
    put_artifact,
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
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
        )
        try:
            result = self._provider.synthesize(input)
        except Exception as exc:
            journal_append_event(
                ctx,
                stage=self.name,
                name="stage_error",
                turn_id=turn.id,
                state_before=state_before,
                error=str(exc),
            )
            raise

        if isinstance(result, AsyncIterator) or inspect.isasyncgen(result):
            return self._wrap_stream(result, ctx, turn.id, state_before)

        state_after = self.snapshot_state()
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
        )
        return result

    async def _wrap_stream(
        self,
        stream: Any,
        ctx: RunContext,
        turn_id: str,
        state_before: StageStateSnapshot,
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
            with observability.span(
                "easycat.tts.synthesize",
                {"easycat.stage": self.name, "easycat.surface": "tts"},
            ):
                async for event in stream:
                    audio = getattr(event, "audio", None)
                    audio_bytes = getattr(audio, "data", None) if audio is not None else None
                    if audio_bytes:
                        output_ref = put_artifact(ctx, audio_bytes)
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
            journal_append_event(
                ctx,
                stage=self.name,
                name="stage_error",
                turn_id=turn_id,
                state_before=state_before,
                error=str(exc),
            )
            raise
        finally:
            observability.record_histogram(
                "easycat.stage.latency",
                time.perf_counter() - started,
                {"easycat.stage": self.name, "easycat.result": result_attr},
            )
        state_after = self.snapshot_state()
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn_id,
            state_before=state_before,
            state_after=state_after,
            data_extra={"frame_count": frame_count, "total_bytes": total_bytes},
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
            if "input" in overrides:
                return overrides["input"]
            if cassette is not None:
                record = cassette.last_record("stage_start") or cassette.last_record()
                if record is not None:
                    data = record.get("data") or {}
                    if isinstance(data, dict) and "input" in data:
                        return data["input"]
            return None

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
        logger.debug("TTSStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(ctx, stage=self.name, signal=signal)
