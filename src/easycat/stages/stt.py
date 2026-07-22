"""STTStage — wraps an STTProvider with journal recording and input capture."""

from __future__ import annotations

import logging
import time
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


class STTStage:
    """Stage wrapper around an :class:`STTProvider`.

    ``execute`` accepts one audio chunk at a time, hands it to the
    provider's ``send_audio``, and journals a ``stage_start`` /
    ``stage_complete`` pair.  The chunk's bytes are stored as a
    ``replay_critical`` artifact and attached to ``stage_start`` via
    ``input_ref`` so a LIVE-fidelity replay can re-drive a fresh STT
    provider from the original audio.
    """

    name = "stt"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        # Fallback recording sink: ``execute`` / ``handle_upstream`` record
        # through ``ctx.journal``; this constructor journal is only used
        # when the RunContext was created without one (see ``_with_journal``).
        self._journal = journal

    def _journal_ctx(self, ctx: RunContext) -> RunContext:
        """Return *ctx*, substituting the constructor journal as a fallback.

        Recording flows through ``ctx.journal``; when the RunContext was
        built without a journal but the stage was handed one directly, we
        record into the stage's journal instead so it is never silently
        dead.
        """
        return journal_ctx(ctx, self._journal)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        ctx = self._journal_ctx(ctx)
        started = time.perf_counter()
        result_attr = "pass"
        state_before = self.snapshot_state()
        with observability.span(
            "easycat.stt.stream",
            {"easycat.stage": self.name, "easycat.surface": "stt"},
        ):
            data_bytes = getattr(input, "data", None) if not isinstance(input, bytes) else input
            input_ref = await put_artifact_async(ctx, data_bytes)
            extra = {
                "audio_bytes": len(data_bytes)
                if isinstance(data_bytes, (bytes, bytearray))
                else 0,
            }
            extra.update(audio_format_fields(input))
            start_sequence = journal_append_event(
                ctx,
                stage=self.name,
                name="stage_start",
                turn_id=turn.id,
                state_before=state_before,
                input_ref=input_ref,
                data_extra=extra,
            )
            try:
                await self._provider.send_audio(input)
                result = input
            except Exception as exc:
                result_attr = "fail"
                elapsed_ms = (time.perf_counter() - started) * 1000
                record_stage_failure(
                    exc,
                    ctx,
                    stage=self.name,
                    provider=type(self._provider).__name__.lower(),
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
        """Replay STT stage.

        Precedence: explicit ``spec.overrides["transcript"]`` wins (the
        escape hatch for unit tests), otherwise the cassette's last
        ``stage_complete`` record supplies the transcript. For ``LIVE``
        fidelity, the stage returns the captured input bytes so the caller
        can re-run ``send_audio`` on a fresh provider.

        Caveat: this stage's own ``execute`` does not journal the
        transcript (STT results arrive asynchronously via the event bus,
        ``STTFinal``, outside ``execute``), so a real recording's
        ``stage_complete`` may not carry one — the cassette read below
        primarily serves ``SIMULATED`` and synthetic cassettes that do.
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            return live_replay_input(spec, cassette)

        # ARTIFACT / SIMULATED
        if "transcript" in overrides or "result" in overrides:
            return overrides.get("transcript", overrides.get("result"))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("transcript", "text", "result"):
                        if key in data:
                            return data[key]
                blob = cassette.blob(record.get("output_ref"))
                if blob is not None:
                    return blob
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
        logger.debug("STTStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(self._journal_ctx(ctx), stage=self.name, signal=signal)
