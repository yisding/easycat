"""Per-turn state for a voice session.

Each turn (user speaks → agent responds → bot speaks) gets its own
``TurnContext``.  Session creates one at turn start and discards it at
turn end, replacing the 15+ per-turn instance variables that previously
lived on Session.
"""

from __future__ import annotations

import time
from collections import deque

from easycat.cancel import CancelToken


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
