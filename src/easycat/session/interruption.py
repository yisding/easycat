"""Audio-byte estimation for barge-in / interruption handling.

Pure functions that map TTS audio byte counts back to estimated spoken
text, used by the Session to tell the agent what the user actually heard
before interrupting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from easycat.integrations.agents._helpers import INTERRUPTION_NOTE
from easycat.integrations.agents.base import CancellationMode
from easycat.session.text import _cleanup_estimation_text, _truncate_partial_text_to_boundary

if TYPE_CHECKING:
    from easycat._turn_context import TurnContext
    from easycat.cancel import CancelToken

logger = logging.getLogger(__name__)


def notify_bridge_interruption(
    agent: Any,
    delivered_text: str,
    mode: str,
    *,
    ctx: Any | None = None,
    turn_id: str | None = None,
) -> bool:
    """Notify a bridge about an end-of-turn interruption.

    ``mode="truncate"`` calls :meth:`ExternalAgentBridge.apply_interruption`
    with :class:`CancellationMode.IMMEDIATE_STOP` so the bridge rewrites
    the last assistant message to what was actually heard.  ``mode="message"``
    calls :meth:`append_interruption_note` instead.  Returns ``True`` on
    success, ``False`` if the bridge raised.

    ``agent`` is normally the :class:`~easycat.stages.agent.AgentStage`, whose
    ``apply_interruption`` / ``append_interruption_note`` accept ``ctx`` /
    ``turn_id`` so the mutation is journaled on the recording boundary.  Plain
    bridges (no journal kwargs) are still supported: the kwargs are only passed
    when a ``ctx`` is supplied and otherwise omitted to keep the bare-bridge
    call shape unchanged.
    """
    journal_kwargs: dict[str, Any] = {}
    if ctx is not None:
        journal_kwargs = {"ctx": ctx, "turn_id": turn_id}
    try:
        if mode == "message":
            agent.append_interruption_note(INTERRUPTION_NOTE, **journal_kwargs)
        else:
            agent.apply_interruption(
                delivered_text, CancellationMode.IMMEDIATE_STOP, **journal_kwargs
            )
        return True
    except Exception:
        logger.debug("bridge interruption notification failed", exc_info=True)
        return False


@dataclass(frozen=True)
class InterruptionNotification:
    """Summary of an interruption notification sent to the agent."""

    mode: str
    text_spoken: str
    notified: bool


class TtsChunk(NamedTuple):
    """A sentence-level TTS call's contribution to the playback timeline.

    Shared shape consumed by both :func:`_estimate_text_spoken` and
    :func:`_all_tts_audio_delivered`.  Producers (``TurnRunner._process_tts``)
    construct these explicitly; being a :class:`NamedTuple` it also stays
    tuple-compatible, so positional unpacking below keeps working.
    """

    text: str
    audio_bytes: int
    completed: bool


def _estimate_text_spoken(
    tts_chunks: list[TtsChunk],
    audio_bytes_sent: int,
) -> str:
    """Estimate what text the user heard based on TTS audio delivered.

    Each entry in *tts_chunks* is a :class:`TtsChunk` (or the equivalent
    ``(text, audio_bytes, completed)`` tuple) for a sentence-level TTS call;
    the ``completed`` flag is ignored here.  *audio_bytes_sent* is the total
    audio bytes that were actually sent to the transport before the barge-in
    flush.

    Walks through the chunks in order, subtracting each chunk's audio from
    the bytes-sent budget.  When the budget runs out mid-chunk, the text is
    proportionally estimated (assumes roughly linear text → audio mapping
    within a single sentence — not perfect, but a practical heuristic).
    """
    if not tts_chunks or audio_bytes_sent <= 0:
        return ""

    remaining = audio_bytes_sent
    spoken = ""
    for chunk_text, chunk_audio, _ in tts_chunks:
        if chunk_audio <= 0:
            # No audio produced for this chunk (e.g. cancelled before any
            # data) — skip.
            continue
        if remaining >= chunk_audio:
            # This entire chunk was delivered.
            spoken += chunk_text
            remaining -= chunk_audio
        else:
            # Partial chunk — estimate by fraction of audio delivered.
            fraction = remaining / chunk_audio
            chars = int(len(chunk_text) * fraction)
            spoken += _truncate_partial_text_to_boundary(chunk_text, chars)
            break
    return spoken


def _all_tts_audio_delivered(
    tts_chunks: list[TtsChunk],
    audio_bytes_delivered: int,
) -> bool:
    """Whether all synthesized TTS audio has been delivered/heard.

    ``audio_bytes_delivered`` should be the estimated bytes actually
    delivered/heard/acknowledged, not merely bytes written to the transport.
    """
    if not tts_chunks:
        return False
    if not all(completed for _, _, completed in tts_chunks):
        return False
    total_audio = sum(max(chunk_audio, 0) for _, chunk_audio, _ in tts_chunks)
    return audio_bytes_delivered >= total_audio


def _audio_bytes_likely_heard(
    send_log: list[tuple[float, int, float]],
    cutoff_time: float | None,
) -> int:
    """Estimate bytes likely heard by ``cutoff_time``.

    ``send_log`` entries are ``(send_time, bytes_sent, chunk_duration_ms)``.
    Chunks are modeled as serial playback with a virtual playout cursor:
    each chunk starts at ``max(send_time, previous_chunk_end)`` and then
    plays linearly over its own duration.
    """
    if not send_log:
        return 0
    if cutoff_time is None:
        return sum(max(size, 0) for _, size, _ in send_log)

    heard = 0
    playout_cursor: float | None = None

    for send_time, size, duration_ms in send_log:
        size = max(size, 0)
        if size == 0:
            continue

        start_time = send_time
        if playout_cursor is not None and playout_cursor > start_time:
            start_time = playout_cursor

        if duration_ms <= 0:
            if start_time <= cutoff_time:
                heard += size
            continue

        duration_s = duration_ms / 1000.0
        end_time = start_time + duration_s
        playout_cursor = end_time

        elapsed_ms = (cutoff_time - start_time) * 1000.0
        if elapsed_ms <= 0:
            continue
        if elapsed_ms >= duration_ms:
            heard += size
            continue
        heard += int(size * (elapsed_ms / duration_ms))
    return heard


def _audio_bytes_acknowledged(
    playback_ack_log: list[tuple[float, int]],
    cutoff_time: float | None,
) -> int:
    """Return acknowledged bytes at or before ``cutoff_time``."""
    if not playback_ack_log:
        return 0
    if cutoff_time is None:
        return max(0, playback_ack_log[-1][1])

    acknowledged = 0
    for ack_time, acked_bytes in playback_ack_log:
        if ack_time > cutoff_time:
            break
        acknowledged = max(acknowledged, max(acked_bytes, 0))
    return acknowledged


def _audio_bytes_per_second_from_send_log(send_log: list[tuple[float, int, float]]) -> float:
    """Estimate playout bytes/second from send-log durations."""
    total_bytes = 0
    total_duration_ms = 0.0
    for _, size, duration_ms in send_log:
        size = max(size, 0)
        if size <= 0 or duration_ms <= 0:
            continue
        total_bytes += size
        total_duration_ms += duration_ms
    if total_bytes <= 0 or total_duration_ms <= 0:
        return 0.0
    return (total_bytes * 1000.0) / total_duration_ms


def _latest_playback_ack_time(
    playback_ack_log: list[tuple[float, int]],
    cutoff_time: float | None,
) -> float | None:
    """Return the latest ack timestamp at or before ``cutoff_time``."""
    latest: float | None = None
    for ack_time, _ in playback_ack_log:
        if cutoff_time is not None and ack_time > cutoff_time:
            break
        latest = ack_time
    return latest


def _audio_bytes_likely_heard_hybrid(
    send_log: list[tuple[float, int, float]],
    playback_ack_log: list[tuple[float, int]],
    cutoff_time: float | None,
    *,
    ack_stale_ms: int,
    ack_tail_cap_ms: int,
) -> int:
    """Estimate heard bytes using playback acks with heuristic stale fallback."""
    heuristic_heard = _audio_bytes_likely_heard(send_log, cutoff_time)
    if cutoff_time is None or not playback_ack_log:
        return heuristic_heard

    acked_heard = _audio_bytes_acknowledged(playback_ack_log, cutoff_time)
    if acked_heard <= 0:
        return heuristic_heard

    # Fresh-ack path: acknowledgements cap the timing estimate.
    heard = min(heuristic_heard, acked_heard)

    latest_ack_time = _latest_playback_ack_time(playback_ack_log, cutoff_time)
    if latest_ack_time is None:
        return heard

    bytes_per_second = _audio_bytes_per_second_from_send_log(send_log)
    if bytes_per_second <= 0:
        return heard

    ack_age_ms = max(0.0, (cutoff_time - latest_ack_time) * 1000.0)
    unacked_tail_bytes = max(0, heuristic_heard - acked_heard)
    unacked_tail_ms = (unacked_tail_bytes / bytes_per_second) * 1000.0
    is_stale = ack_age_ms > ack_stale_ms or unacked_tail_ms > ack_stale_ms
    if not is_stale:
        return heard

    if ack_tail_cap_ms <= 0:
        return heard
    tail_cap_bytes = int(bytes_per_second * (ack_tail_cap_ms / 1000.0))
    if tail_cap_bytes <= 0:
        return heard
    return min(heuristic_heard, acked_heard + tail_cap_bytes)


def estimate_and_notify_interruption(
    agent: Any,
    token: CancelToken | None,
    turn: TurnContext,
    tts_chunks: list[TtsChunk],
    *,
    tts_playback_started: bool,
    tts_playback_cut_short: bool = False,
    interrupted: bool,
    interruption_mode: str,
    latency_compensation_ms: int,
    ack_stale_ms: int,
    ack_tail_cap_ms: int,
    ctx: Any | None = None,
) -> InterruptionNotification | None:
    """Estimate what the user heard and notify the agent of the interruption.

    Called after a streaming turn completes when the user barged in.
    Compares audio bytes sent to the transport against per-chunk TTS
    production to estimate what text was actually heard.

    ``cancelled_during_playback`` requires the playback to have been *cut
    short* by the cancelled token (``tts_playback_cut_short``), not merely
    started. ``token.is_cancelled`` now flips synchronously on
    ``cancel()`` (the asyncio.Event ``set`` is deferred onto the loop), so a
    turn that finished speaking and only afterwards had its token cancelled
    by a *later* turn's barge-in would otherwise be retro-truncated to what
    was "heard" at the (now bogus) cutoff — clobbering its committed history.
    """
    cancelled_during_playback = bool(
        token and token.is_cancelled and tts_playback_started and tts_playback_cut_short
    )
    if not (interrupted or cancelled_during_playback):
        return None

    cutoff_time = token.cancelled_at if token is not None else None
    if cutoff_time is None:
        cutoff_time = turn.last_barge_in_time
    if cutoff_time is not None and latency_compensation_ms > 0:
        cutoff_time -= latency_compensation_ms / 1000.0

    heard_bytes = _audio_bytes_likely_heard_hybrid(
        list(turn.audio_send_log),
        list(turn.playback_ack_log),
        cutoff_time,
        ack_stale_ms=ack_stale_ms,
        ack_tail_cap_ms=ack_tail_cap_ms,
    )

    if not _all_tts_audio_delivered(tts_chunks, heard_bytes):
        text_spoken = _cleanup_estimation_text(_estimate_text_spoken(tts_chunks, heard_bytes))
        notified = notify_bridge_interruption(
            agent,
            text_spoken,
            interruption_mode,
            ctx=ctx,
            turn_id=turn.id,
        )
        return InterruptionNotification(
            mode=interruption_mode,
            text_spoken=text_spoken,
            notified=notified,
        )
    return None
