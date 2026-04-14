"""VADStage — wraps a VADProvider with journal recording."""

from __future__ import annotations

import logging
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, StageStateSnapshot

logger = logging.getLogger(__name__)


class VADStage:
    """Stage wrapper around a :class:`VADProvider`."""

    name = "vad"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    # VAD processes a single audio chunk and yields 0-2 events (speech
    # start/stop).  Cap the collection as a safety net against a misbehaving
    # provider that yields endlessly from a single chunk.
    _MAX_EVENTS_PER_CHUNK: int = 64

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        self._record(ctx, "stage_start", turn_id=turn.id, state_before=state_before)
        try:
            events: list[Any] = []
            async for event in self._provider.process(input):
                events.append(event)
                if len(events) >= self._MAX_EVENTS_PER_CHUNK:
                    logger.warning(
                        "VAD provider yielded too many events for a single chunk; truncating"
                    )
                    break
            result = events
        except Exception as exc:
            self._record(
                ctx, "stage_error", turn_id=turn.id, state_before=state_before, error=str(exc)
            )
            raise
        state_after = self.snapshot_state()
        self._record(
            ctx,
            "stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
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
        """Replay VAD stage.

        ``ARTIFACT`` returns the captured frame events from the
        cassette (or ``spec.overrides["events"]``).  ``LIVE`` returns
        the captured input audio for re-execution against a fresh VAD
        backend.
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            if "input" in overrides:
                return overrides["input"]
            if cassette is not None:
                record = cassette.last_record("stage_start") or cassette.last_record()
                if record is not None:
                    blob = cassette.blob(record.get("input_ref"))
                    if blob is not None:
                        return blob
            return None

        if "events" in overrides or "result" in overrides:
            return overrides.get("events", overrides.get("result", []))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("events", "result"):
                        if key in data:
                            return data[key]
        return []

    def replay_decision(self, snapshot: StageStateSnapshot) -> Any:
        """Replay a VAD decision from a snapshot."""
        return snapshot.fields.get("decision", None)

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("VADStage received upstream signal: %s", signal)

    def _record(
        self, ctx: RunContext, name: str, *, turn_id: str | None = None, **kwargs: Any
    ) -> None:
        if ctx.journal is not None:
            payload = {k: str(v) if v is not None else None for k, v in kwargs.items()}
            payload["stage"] = self.name
            ctx.journal.append(
                kind=JournalRecordKind.EVENT,
                name=name,
                session_id=ctx.session_id,
                turn_id=turn_id,
                data=payload,
            )
