"""Per-turn state for a voice session.

Each turn (user speaks → agent responds → bot speaks) gets its own
``TurnContext``.  Session creates one at turn start and discards it at
turn end, replacing the 15+ per-turn instance variables that previously
lived on Session.

Per-turn STT futures (``stt_final_future`` and
``pending_stt_segment_futures``) also live here so that a stale
callback from the previous turn cannot resolve a future on the next
turn — the futures naturally die when the ``TurnContext`` is replaced.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Protocol, runtime_checkable

from easycat.cancel import CancelToken


@runtime_checkable
class TurnHandle(Protocol):
    """The contract between Session and turn-running collaborators.

    Session is the single authority on the active turn pointer and
    turn generation.  Collaborators read and write through this handle
    rather than holding ``Session`` refs.
    """

    @property
    def current(self) -> TurnContext | None: ...

    @property
    def generation(self) -> int: ...

    @property
    def no_turn(self) -> TurnContext: ...

    def set(self, turn: TurnContext | None) -> None: ...

    def bump_generation(self) -> int: ...


class TurnContext:
    """Holds all mutable state scoped to a single conversational turn."""

    _generation_counter: int = 0

    __slots__ = (
        "id",
        "generation",
        "cancel_token",
        "end_time",
        "stt_final_time",
        "stt_segments",
        "stt_track",
        "stt_has_uncommitted_audio",
        "first_agent_time",
        "first_tts_audio_time",
        "audio_bytes_sent",
        "audio_send_log",
        "playback_mark_to_bytes",
        "playback_ack_log",
        "bytes_since_last_mark",
        "last_barge_in_time",
        "stt_final_future",
        "pending_stt_segment_futures",
    )

    def __init__(self, turn_id: str, cancel_token: CancelToken) -> None:
        TurnContext._generation_counter += 1
        self.id = turn_id
        self.generation = TurnContext._generation_counter
        self.cancel_token = cancel_token

        # Timing markers (set as the turn progresses)
        self.end_time: float | None = None
        self.stt_final_time: float | None = None
        self.stt_segments: list[str] = []
        self.stt_track: str | None = None
        self.stt_has_uncommitted_audio = False
        self.first_agent_time: float | None = None
        self.first_tts_audio_time: float | None = None

        # Audio bytes sent to the transport during this turn.
        # Used to estimate what the user heard before a barge-in.
        self.audio_bytes_sent: int = 0
        self.audio_send_log: deque[tuple[float, int, float]] = deque(maxlen=10_000)

        # Playback mark tracking (maps mark names to cumulative byte positions)
        self.playback_mark_to_bytes: dict[str, int] = {}
        self.playback_ack_log: deque[tuple[float, int]] = deque(maxlen=10_000)
        self.bytes_since_last_mark: int = 0

        self.last_barge_in_time: float | None = None

        # STT future plumbing — per-turn so a stale callback cannot resolve
        # a future on the next turn (the futures die with the TurnContext).
        self.stt_final_future: asyncio.Future[str] | None = None
        self.pending_stt_segment_futures: list[asyncio.Future[str]] = []

    def record_barge_in(self) -> None:
        self.last_barge_in_time = time.monotonic()

    def append_stt_segment(self, text: str, *, track: str | None = None) -> None:
        normalized = " ".join(text.split())
        if normalized:
            self.stt_segments.append(normalized)
        if track is not None:
            self.stt_track = track

    @property
    def transcript_text(self) -> str:
        return " ".join(self.stt_segments).strip()

    def record_audio_sent(self, size: int, duration_ms: float) -> None:
        """Record that audio was sent to the transport."""
        self.audio_bytes_sent += size
        self.audio_send_log.append((time.monotonic(), size, duration_ms))
        self.bytes_since_last_mark += size
