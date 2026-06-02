"""Number health monitoring and call disposition tracking."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from easycat.events import CallEnded, CallFailed, CallInitiated, CallStateChanged, EventBus
from easycat.telephony.call_state import OutboundCallState
from easycat.telephony.outbound import BLOCK_REASONS

logger = logging.getLogger(__name__)

_TERMINAL_DISPOSITIONS: dict[OutboundCallState, str] = {
    OutboundCallState.HUMAN: "human",
    OutboundCallState.VOICEMAIL: "voicemail",
    OutboundCallState.IVR: "ivr",
    OutboundCallState.ENDED: "ended",
    OutboundCallState.UNKNOWN: "unknown",
}


@dataclass
class _CallRecord:
    """Internal record of a call from a specific number."""

    timestamp: float
    answered: bool
    duration_s: float = 0.0
    blocked: bool = False
    disposition: str = ""


class NumberHealthMonitor:
    """Tracks per-number health metrics for outbound calling.

    Monitors answer rate, block count, average duration, and enforces
    call pacing limits.
    """

    _MAX_RECORDS_PER_NUMBER = 500
    _MAX_TRACKED_NUMBERS = 10_000

    def __init__(
        self,
        event_bus: EventBus,
        *,
        answer_rate_threshold: float = 0.4,
        block_count_threshold: int = 5,
        max_calls_per_minute: int = 10,
        min_inter_call_delay_s: float = 2.0,
        max_concurrent_per_number: int = 3,
        record_ttl_s: float = 86400.0,
    ) -> None:
        self._event_bus = event_bus
        self._answer_rate_threshold = answer_rate_threshold
        self._block_count_threshold = block_count_threshold
        self._max_calls_per_minute = max_calls_per_minute
        self._min_inter_call_delay_s = min_inter_call_delay_s
        self._max_concurrent_per_number = max_concurrent_per_number
        self._record_ttl_s = record_ttl_s

        self._records: dict[str, list[_CallRecord]] = defaultdict(list)
        self._concurrent: dict[str, int] = defaultdict(int)
        self._last_call_time: dict[str, float] = {}
        self._call_sid_to_number: dict[str, str] = {}
        self._terminal_call_sids: dict[str, None] = {}
        self._max_sid_tracking = 10_000
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(CallInitiated, self._on_call_initiated)
        self._event_bus.subscribe(CallFailed, self._on_call_failed)
        self._event_bus.subscribe(CallEnded, self._on_call_ended)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(CallInitiated, self._on_call_initiated)
            self._event_bus.unsubscribe(CallFailed, self._on_call_failed)
            self._event_bus.unsubscribe(CallEnded, self._on_call_ended)
        self._started = False

    def record_call(
        self,
        number: str,
        answered: bool,
        duration_s: float = 0.0,
        blocked: bool = False,
        disposition: str = "",
    ) -> None:
        """Record a call outcome for a number."""
        if not number:
            # Defense-in-depth: never file analytics under an empty key.
            return
        now = time.monotonic()
        self._ensure_number_capacity(number)
        records = self._records[number]
        records.append(
            _CallRecord(
                timestamp=now,
                answered=answered,
                duration_s=duration_s,
                blocked=blocked,
                disposition=disposition,
            )
        )
        # Cap per-number records to prevent unbounded growth.
        if len(records) > self._MAX_RECORDS_PER_NUMBER:
            self._records[number] = records[-self._MAX_RECORDS_PER_NUMBER :]
        self._last_call_time[number] = now

    def _ensure_number_capacity(self, number: str) -> None:
        """Keep per-number analytics bounded by evicting oldest inactive numbers."""
        # Common path: already tracked, or still under cap. Both are O(1) — no
        # set-union of every tracking map on each call.
        if number in self._last_call_time or len(self._last_call_time) < self._MAX_TRACKED_NUMBERS:
            return

        # At cap. ``_last_call_time`` sees every initiated and completed call, so
        # its insertion order approximates oldest-first. Prefer evicting a
        # completed/inactive number (no in-flight concurrency).
        for candidate in self._last_call_time:
            if candidate == number or self._concurrent.get(candidate, 0) > 0:
                continue
            self._drop_number(candidate)
            return

        # All tracked numbers appear active. Drop the oldest bucket anyway rather
        # than let untrusted status-callback values grow memory without bound.
        candidate = next(iter(self._last_call_time))
        if candidate != number:
            self._drop_number(candidate)

    def _drop_number(self, number: str) -> None:
        self._records.pop(number, None)
        self._last_call_time.pop(number, None)
        self._concurrent.pop(number, None)
        # Purge any lingering SID→number mappings so a later terminal event for a
        # force-dropped number short-circuits via ``_resolve_number() is None``
        # instead of decrementing a phantom concurrency bucket.
        stale = [sid for sid, n in self._call_sid_to_number.items() if n == number]
        for sid in stale:
            self._call_sid_to_number.pop(sid, None)

    def answer_rate(self, number: str) -> float:
        """Return the answer rate for a number (0.0-1.0)."""
        records = self._active_records(number)
        if not records:
            return 1.0
        answered = sum(1 for r in records if r.answered)
        return answered / len(records)

    def avg_duration(self, number: str) -> float:
        """Return average call duration for a number."""
        records = self._active_records(number)
        durations = [r.duration_s for r in records if r.duration_s > 0]
        return sum(durations) / len(durations) if durations else 0.0

    def block_count(self, number: str) -> int:
        """Return number of times this number has been blocked."""
        records = self._active_records(number)
        return sum(1 for r in records if r.blocked)

    def can_place_call(self, number: str) -> bool:
        """Check if rate limits allow placing another call from this number."""
        now = time.monotonic()

        if self._concurrent.get(number, 0) >= self._max_concurrent_per_number:
            return False

        last = self._last_call_time.get(number)
        if last and (now - last) < self._min_inter_call_delay_s:
            return False

        # Completed records + in-flight attempts.
        one_minute_ago = now - 60.0
        recent = [r for r in self._records.get(number, []) if r.timestamp > one_minute_ago]
        in_flight = self._concurrent.get(number, 0)
        if len(recent) + in_flight >= self._max_calls_per_minute:
            return False

        return True

    def _active_records(self, number: str) -> list[_CallRecord]:
        """Return records within TTL for a number, pruning expired entries."""
        now = time.monotonic()
        cutoff = now - self._record_ttl_s
        records = self._records.get(number, [])
        active = [r for r in records if r.timestamp > cutoff]
        if len(active) < len(records):
            self._records[number] = active
        return active

    def _resolve_number(self, call_sid: str, event_number: str | None) -> str | None:
        """Resolve the outbound caller ID for a call.

        Prefer the number tracked from ``CallInitiated`` for this ``call_sid``
        so terminal callbacks with callee-oriented fields cannot decrement a
        different bucket than the one incremented at call start. Fall back to
        the terminal event's number for events that have no tracked SID.
        Returns ``None`` when neither is available so callers can skip recording
        rather than filing analytics under an empty or SID-shaped phantom key
        (e.g. placement failures emit an empty SID).
        """
        tracked_number = self._call_sid_to_number.get(call_sid)
        if tracked_number:
            return tracked_number
        if event_number:
            return event_number
        return None

    async def _on_call_initiated(self, event: CallInitiated) -> None:
        # Guard against duplicate CallInitiated for the same call_sid
        # (place_call() emits one, and the Twilio "initiated" status callback
        # emits another via emit_call_status).
        if event.call_sid in self._call_sid_to_number:
            return
        number = event.from_
        self._call_sid_to_number[event.call_sid] = number
        self._ensure_number_capacity(number)
        self._concurrent[number] = self._concurrent.get(number, 0) + 1
        self._last_call_time[number] = time.monotonic()

        # Evict stale SID mappings (zombie calls that never ended).
        if len(self._call_sid_to_number) > self._max_sid_tracking:
            evict_count = self._max_sid_tracking // 2
            logger.warning(
                "SID tracking limit exceeded (%d > %d), evicting %d oldest entries",
                len(self._call_sid_to_number),
                self._max_sid_tracking,
                evict_count,
            )
            oldest = list(self._call_sid_to_number.keys())[:evict_count]
            for sid in oldest:
                # Decrement concurrent count for evicted calls.
                evicted_number = self._call_sid_to_number.pop(sid, None)
                if evicted_number:
                    self._decrement_concurrent(evicted_number)

    def _decrement_concurrent(self, number: str) -> None:
        prev = self._concurrent.get(number, 0)
        if prev <= 0:
            logger.debug(
                "Concurrent count already 0 for %s — possible unbalanced init/end events",
                number,
            )
            self._concurrent.pop(number, None)
            return
        if prev == 1:
            self._concurrent.pop(number, None)
            return
        self._concurrent[number] = prev - 1

    def _mark_terminal(self, call_sid: str) -> bool:
        """Return False when this call SID was already recorded as terminal."""
        if not call_sid:
            return True
        if call_sid in self._terminal_call_sids:
            return False
        self._terminal_call_sids[call_sid] = None
        if len(self._terminal_call_sids) > self._max_sid_tracking:
            oldest = next(iter(self._terminal_call_sids))
            self._terminal_call_sids.pop(oldest, None)
        return True

    async def _on_call_failed(self, event: CallFailed) -> None:
        if not self._mark_terminal(event.call_sid):
            return
        number = self._resolve_number(event.call_sid, event.number)
        if number is None:
            # Unresolvable number (e.g. placement-failure CallFailed with an
            # empty SID): skip recording rather than poison analytics.
            return
        self._decrement_concurrent(number)
        is_blocked = event.reason in BLOCK_REASONS
        self.record_call(number, answered=False, blocked=is_blocked)
        self._call_sid_to_number.pop(event.call_sid, None)

    async def _on_call_ended(self, event: CallEnded) -> None:
        if not self._mark_terminal(event.call_sid):
            return
        number = self._resolve_number(event.call_sid, event.number)
        if number is None:
            return
        self._decrement_concurrent(number)
        duration = event.duration_s or 0.0
        self.record_call(
            number,
            answered=True,
            duration_s=duration,
            disposition=event.disposition or "",
        )
        self._call_sid_to_number.pop(event.call_sid, None)


class CallDispositionTracker:
    """Tracks call dispositions for analytics.

    Records the final disposition of each call (human, voicemail, screening,
    IVR, busy, failed) and provides breakdown statistics.

    Note: Uses ``time.time()`` (wall-clock) for disposition timestamps because
    :meth:`disposition_by_hour` needs real calendar hours. This differs from
    :class:`NumberHealthMonitor` which uses ``time.monotonic()`` for TTL.
    """

    _MAX_DISPOSITIONS = 10_000
    _MAX_CALL_TRACKING = 50_000

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._dispositions: list[tuple[float, str, str]] = []  # (timestamp, disposition, call_sid)
        self._call_dispositions: dict[str, str] = {}
        self._failure_reasons: dict[str, str] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(CallFailed, self._on_call_failed)
        self._event_bus.subscribe(CallStateChanged, self._on_state_changed)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(CallFailed, self._on_call_failed)
            self._event_bus.unsubscribe(CallStateChanged, self._on_state_changed)
        self._started = False

    def record_disposition(self, disposition: str, call_sid: str = "") -> None:
        self._dispositions.append((time.time(), disposition, call_sid))
        if len(self._dispositions) > self._MAX_DISPOSITIONS:
            self._dispositions = self._dispositions[-self._MAX_DISPOSITIONS :]

    def _replace_disposition(self, call_sid: str, new_disposition: str) -> None:
        """Replace the recorded disposition for a call (e.g. late voicemail)."""
        if call_sid not in self._call_dispositions:
            return
        # Walk backwards to find the entry for this specific call_sid.
        for i in range(len(self._dispositions) - 1, -1, -1):
            if self._dispositions[i][2] == call_sid:
                self._dispositions[i] = (self._dispositions[i][0], new_disposition, call_sid)
                break
        self._call_dispositions[call_sid] = new_disposition

    def disposition_rates(self) -> dict[str, float]:
        """Return disposition breakdown as rates (0.0-1.0)."""
        if not self._dispositions:
            return {}
        counts: dict[str, int] = defaultdict(int)
        for _, disp, _ in self._dispositions:
            counts[disp] += 1
        total = len(self._dispositions)
        return {k: v / total for k, v in counts.items()}

    def disposition_by_hour(self) -> dict[int, dict[str, int]]:
        """Return disposition breakdown by hour of day (UTC)."""
        from datetime import UTC, datetime

        by_hour: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for ts, disp, _ in self._dispositions:
            hour = datetime.fromtimestamp(ts, tz=UTC).hour
            by_hour[hour][disp] += 1
        return dict(by_hour)

    def _evict_call_tracking(self) -> None:
        """Keep per-call tracking dictionaries bounded independently of event order.

        Each dict is trimmed only when *it* exceeds the cap, so a flood of
        failure-only callbacks does not evict under-cap completed-call SIDs (the
        dedupe/reclassification guard relied on by ``_on_state_changed``).
        """
        from itertools import islice

        if len(self._call_dispositions) > self._MAX_CALL_TRACKING:
            disposition_evict_count = len(self._call_dispositions) - self._MAX_CALL_TRACKING // 2
            for sid in list(islice(self._call_dispositions, disposition_evict_count)):
                self._call_dispositions.pop(sid, None)
                self._failure_reasons.pop(sid, None)

        if len(self._failure_reasons) > self._MAX_CALL_TRACKING:
            failure_evict_count = len(self._failure_reasons) - self._MAX_CALL_TRACKING // 2
            for sid in list(islice(self._failure_reasons, failure_evict_count)):
                self._failure_reasons.pop(sid, None)

    async def _on_call_failed(self, event: CallFailed) -> None:
        """Stash failure reason so ENDED disposition preserves it."""
        if event.call_sid:
            self._failure_reasons[event.call_sid] = event.reason
            self._evict_call_tracking()

    async def _on_state_changed(self, event: CallStateChanged) -> None:
        """Auto-record disposition when call reaches terminal state.

        Late voicemail reclassification (HUMAN → VOICEMAIL) overwrites the
        earlier disposition so analytics reflect the corrected outcome.
        """
        if event.new in _TERMINAL_DISPOSITIONS:
            call_sid = event.call_sid
            disposition = _TERMINAL_DISPOSITIONS[event.new]
            # Preserve the specific failure reason (busy, no-answer, etc.)
            # instead of collapsing everything to generic "ended".
            if event.new == OutboundCallState.ENDED and call_sid in self._failure_reasons:
                disposition = self._failure_reasons.pop(call_sid)

            # Allow reclassification: late voicemail (HUMAN→VOICEMAIL) and
            # voicemail pickup (VOICEMAIL→HUMAN) overwrite the earlier disposition.
            if call_sid in self._call_dispositions:
                if event.new in {OutboundCallState.VOICEMAIL, OutboundCallState.HUMAN}:
                    self._replace_disposition(call_sid, disposition)
                return

            self._call_dispositions[call_sid] = disposition
            self.record_disposition(disposition, call_sid=call_sid)

            self._evict_call_tracking()
