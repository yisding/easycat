"""TransportStage — wraps a Transport with journal recording and capture."""

from __future__ import annotations

import dataclasses
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
    put_artifact,
)

logger = logging.getLogger(__name__)


class TransportStage:
    """Stage wrapper around a :class:`Transport`.

    ``execute`` wraps the outbound ``send_audio`` call: the chunk's
    bytes are stored as a ``replay_critical`` artifact and attached to
    ``stage_complete`` via ``output_ref``, so a replay can reconstruct
    exactly what the transport pushed toward the client.
    """

    name = "transport"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        # Fallback recording sink used only when ``ctx.journal`` is None
        # (see ``_journal_ctx``); recording normally flows through ctx.
        self._journal = journal

    def _journal_ctx(self, ctx: RunContext) -> RunContext:
        """Return *ctx*, substituting the constructor journal as a fallback."""
        if ctx.journal is None and self._journal is not None:
            return dataclasses.replace(ctx, journal=self._journal)
        return ctx

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> bool:
        """Send audio through the wrapped transport.

        Returns ``True`` when the transport accepted the chunk for
        delivery and ``False`` when it was silently dropped (for
        example because the peer is disconnected).
        """
        ctx = self._journal_ctx(ctx)
        started = time.perf_counter()
        result_attr = "pass"
        state_before = self.snapshot_state()
        audio_bytes = getattr(input, "data", None) if not isinstance(input, bytes) else input
        output_ref = put_artifact(ctx, audio_bytes)
        extra = {
            "audio_bytes": len(audio_bytes) if isinstance(audio_bytes, (bytes, bytearray)) else 0,
        }
        extra.update(audio_format_fields(input))
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
            data_extra=extra,
        )
        try:
            with observability.span(
                "easycat.transport.send",
                {"easycat.stage": self.name, "easycat.surface": "tts"},
            ):
                if isinstance(audio_bytes, (bytes, bytearray)):
                    observability.increment_counter(
                        "easycat.audio.bytes.total",
                        value=len(audio_bytes),
                        attributes={"easycat.surface": "tts"},
                    )
                    observability.increment_counter(
                        "easycat.audio.frames.total",
                        attributes={"easycat.surface": "tts"},
                    )
                delivered = await self._provider.send_audio(input)
                if not delivered:
                    # A falsy return is a silent drop, not a successful send;
                    # tag the latency sample so drops are distinguishable from
                    # deliveries in easycat.stage.latency.
                    result_attr = "drop"
        except Exception as exc:
            result_attr = "fail"
            observability.increment_counter(
                "easycat.provider.errors.total",
                attributes={
                    "easycat.surface": "tts",
                    "easycat.provider": type(self._provider).__name__.lower(),
                    "easycat.error_type": type(exc).__name__,
                },
            )
            journal_append_event(
                ctx,
                stage=self.name,
                name="stage_error",
                turn_id=turn.id,
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
        result = bool(delivered)
        state_after = self.snapshot_state()
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
            output_ref=output_ref,
            data_extra={**extra, "delivered": result},
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
        """Replay Transport stage.

        Transport replay is a passthrough cassette. The transport stage
        only ever sends *outbound* frames, which ``execute`` captures
        under the ``stage_complete`` ``output_ref``. Both ``ARTIFACT``
        and ``LIVE`` therefore return those captured outbound frames;
        ``LIVE`` exists so the caller can push them through a fresh
        transport (for offline analysis, not a real socket).
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            if "input" in overrides:
                return overrides["input"]
            if cassette is not None:
                record = cassette.last_record("stage_complete") or cassette.last_record()
                if record is not None:
                    blob = cassette.blob(record.get("output_ref"))
                    if blob is not None:
                        return blob
            return None

        if "data" in overrides or "result" in overrides:
            return overrides.get("data", overrides.get("result"))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                blob = cassette.blob(record.get("output_ref"))
                if blob is not None:
                    return blob
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("data", "result"):
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
        logger.debug("TransportStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(self._journal_ctx(ctx), stage=self.name, signal=signal)
