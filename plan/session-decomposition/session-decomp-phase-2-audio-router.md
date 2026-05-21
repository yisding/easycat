# Session Decomposition — Phase 2: AudioRouter

> **Historical implementation checklist.** `src/easycat/session/_audio_router.py`
> exists in the current codebase; keep this file as rationale and review
> context. Line numbers and unchecked task boxes are from the original
> extraction plan.
>
> **Part of the session decomposition.** Overview lives in
> `session-decomp-overview.md`. This file is the operational plan
> for Phase 2.
>
> **Predecessors:** Phase 0.
> **Successors:** Phase 5 (TurnRunner) takes a reference to the
> AudioRouter. Phase 2 also wires the real
> `audio_router.reset_speech_detection` callback into the Phase 1
> STTCommitter (replacing the Phase 1 no-op stub).
>
> **Risk:** low. Well-bounded. The ingress loop touches existing
> stage objects through their `execute()` method; outbound playback
> accounting is self-contained.
>
> **Compatibility policy:** internal-only changes. AudioRouter is a
> private collaborator (`session/_audio_router.py`); not exported.

## Goal

Extract transport ingress, outbound audio drain, and
playback-mark accounting into one collaborator. Today this logic
spans 9 methods and 9 instance attrs on Session. After Phase 2,
Session holds one `self._audio_router: AudioRouter` field and
delegates.

## Scope

**In scope:**

- Create `src/easycat/session/_audio_router.py`
- Move 9 methods into the new file
- Migrate 9 session-long attrs (auto-turn detector, replay state,
  playback-mark accounting, outbound queue, outbound task)
- Wire the AudioRouter into Session `__init__`
- Update event-bus subscriptions to target router methods
- Replace the Phase 1 no-op stub
  (`on_speech_detection_reset=lambda: None`) with the real
  `audio_router.reset_speech_detection`

**Out of scope:**

- Any change to transport implementations
  (`src/easycat/transports/*`)
- Any change to `Transport` protocol
- Any change to noise reduction / echo cancellation / VAD modules
- Fixing the WebRTC queue ownership bug (separate workstream)
- Moving `_handle_end_of_speech` (Phase 5)

## Tasks

### T2.0: Architecture freeze

- [ ] Confirm the AudioRouter encompasses both **ingress**
  (transport → audio/vad/stt stages) and **outbound**
  (drain → transport.send_audio). Rationale: both halves share
  the playback-mark accounting and the gated-replay buffer; splitting
  them would force an interface between two halves of one
  conversation.
- [ ] Confirm `replay_gated_audio` (public method on Session today)
  becomes `Session.replay_gated_audio` → delegates to
  `self._audio_router.replay_gated(...)`. The public method name and
  signature on Session do not change.
- [ ] Confirm the `reset_speech_detection` hook signature is
  parameterless. Rationale: the only consumer (`STTCommitter.cancel`)
  doesn't have any context to pass.

### T2.1: Create `_audio_router.py` module

- [ ] Create `src/easycat/session/_audio_router.py`.
- [ ] Define the class skeleton:

```python
"""Owns transport ingress and outbound audio drain for a Session.

Responsibilities:

- **Ingress.** The transport → audio-stage → vad-stage → stt-stage
  receive loop. Handles auto-turn speech-energy detection (the
  "start a turn from raw audio" path used when VAD is off).
- **Outbound.** Drains the outbound queue to ``transport.send_audio``,
  stamps each chunk with the current turn's byte counters, emits
  playback marks at fixed byte intervals, and observes playback acks
  from transports that report them.
- **Gated replay.** Replays buffered audio events through the
  pipeline after a gated transport unblocks.

The router holds the single outbound queue, the playback-mark
accounting (`bytes_interval`, `seq`, `mark_to_bytes`), and the
auto-turn speech-frame counter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from easycat.audio_format import AudioChunk
from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
from easycat.events import (
    AudioIn,
    Error,
    ErrorStage,
    EventBus,
    PlaybackMarkAck,
    TransportAudioDelivered,
)
from easycat.providers import Transport
from easycat.runtime.context import RunContext
from easycat.runtime.capabilities import (
    PlaybackAcknowledgements,
    playback_acknowledgements,
    transport_reports_audio_delivery,
)
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._text import _chunk_has_speech_energy
from easycat.stages.audio import AudioStage
from easycat.stages.stt import STTStage
from easycat.stages.vad import VADStage
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat.session._turn_context import TurnContext

logger = logging.getLogger(__name__)


class AudioRouter:
    """Routes audio between the transport and the pipeline stages."""

    def __init__(
        self,
        *,
        transport: Transport,
        audio_stage: AudioStage,
        vad_stage: VADStage,
        stt_stage: STTStage,
        turn_manager: TurnManager,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        run_ctx: RunContext,
        no_turn: "TurnContext",
        # Capability flags
        enable_noise_reduction: bool,
        enable_aec: bool,
        enable_vad: bool,
        auto_turn_from_stt_final: bool,
        # Callbacks
        emit: Callable[[Any], Any],
        is_running: Callable[[], bool],
        current_turn: Callable[[], "TurnContext | None"],
        is_stt_active: Callable[[], bool],
        # Outbound queue config
        outbound_queue: BoundedAudioQueue | None,
    ) -> None:
        self._transport = transport
        self._audio_stage = audio_stage
        self._vad_stage = vad_stage
        self._stt_stage = stt_stage
        self._turn_manager = turn_manager
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._run_ctx = run_ctx
        self._no_turn = no_turn

        self._enable_noise_reduction = enable_noise_reduction
        self._enable_aec = enable_aec
        self._enable_vad = enable_vad
        self._auto_turn_from_stt_final = auto_turn_from_stt_final

        self._emit = emit
        self._is_running = is_running
        self._current_turn = current_turn
        self._is_stt_active = is_stt_active

        # Auto-turn speech-energy detector state
        self._auto_turn_speech_frames: int = 0

        # Gated replay
        self._replay_chunks_pending: int = 0

        # Playback mark accounting
        self._playback_mark_bytes_interval: int = 4_000
        self._playback_mark_seq: int = 0
        self._playback_ack_transport: PlaybackAcknowledgements | None = (
            playback_acknowledgements(transport)
        )
        self._transport_reports_audio_delivery = transport_reports_audio_delivery(transport)

        # Outbound queue
        self._outbound_queue_external = outbound_queue is not None
        self._outbound_queue = outbound_queue or BoundedAudioQueue(
            max_size=200,
            policy=DropPolicy.DROP_OLDEST,
            name="outbound_audio",
        )
        self._outbound_task: asyncio.Task[None] | None = None
        self._pipeline_task: asyncio.Task[None] | None = None
```

### T2.2: Public API

| Public method | Source (Session method) | Line |
|---|---|---:|
| `start_ingress()` | wraps `_run_pipeline` | 2052 |
| `start_outbound()` | wraps `_drain_outbound_audio` | 2500 |
| `await_drain(timeout)` | `_wait_outbound_drain` | 2483 |
| `on_playback_ack(evt)` | `_on_playback_mark_ack` | 2595 |
| `on_audio_delivered(evt)` | `_on_transport_audio_delivered` | 2607 |
| `gated_replay(events)` | `replay_gated_audio` | 1151 |
| `reset_speech_detection()` | new (sets `_auto_turn_speech_frames = 0`) | — |
| `queue_outbound(chunk)` | new (puts on `_outbound_queue`) | — |
| `stop_ingress()` | new (cancels `_pipeline_task`) | — |
| `stop_outbound()` | new (cancels `_outbound_task`) | — |

Internal helpers (private to AudioRouter):

- `_handle_audio_delivery` (was Session method, 2549)
- `_send_playback_mark` (was Session method, 2576)
- `_stamp_outbound_chunk` (was Session method, 2541)

### T2.3: Port the 9 methods

For each method:

- [ ] Copy body from `_session.py`.
- [ ] Replace `self.transport` → `self._transport`.
- [ ] Replace `self._audio_stage`, `self._vad_stage`, `self._stt_stage`
  → `self._audio_stage`, `self._vad_stage`, `self._stt_stage` (same names).
- [ ] Replace `self._run_ctx` → `self._run_ctx`.
- [ ] Replace `self._turn` reads → `self._current_turn()` (uses
  the callback Session provides; preserves "Session owns the active
  turn pointer" invariant).
- [ ] Replace `self._no_turn` → `self._no_turn`.
- [ ] Replace `self._stt_active` reads → `self._is_stt_active()`.
- [ ] Replace `self._auto_turn_speech_frames` → `self._auto_turn_speech_frames`
  (now local to router).
- [ ] Replace `self._enable_*` flags → `self._enable_*` (same names).
- [ ] Replace `self._turn_manager` → `self._turn_manager`.
- [ ] Replace `self._emit(evt)` → `await self._emit(evt)` (callback).
- [ ] Replace `self._is_running` → `self._is_running()` (callback).
- [ ] Replace `self._outbound_queue` → `self._outbound_queue` (same).
- [ ] Replace `self._playback_mark_*` → `self._playback_mark_*` (now local).
- [ ] Replace `self._replay_chunks_pending` → `self._replay_chunks_pending`.
- [ ] Replace `self._playback_ack_transport` → `self._playback_ack_transport`.
- [ ] Replace `self._transport_reports_audio_delivery` → same name on router.
- [ ] Replace `self._journal_sink` → `self._journal_sink`.

### T2.4: Wire AudioRouter into Session `__init__`

In `_session.py:__init__`, after stages and `_journal_sink` are
constructed, and after Phase 1's STTCommitter is constructed:

```python
self._audio_router = AudioRouter(
    transport=self.transport,
    audio_stage=self._audio_stage,
    vad_stage=self._vad_stage,
    stt_stage=self._stt_stage,
    turn_manager=self._turn_manager,
    event_bus=self.event_bus,
    journal_sink=self._journal_sink,
    run_ctx=self._run_ctx,
    no_turn=self._no_turn,
    enable_noise_reduction=self._enable_noise_reduction,
    enable_aec=self._enable_aec,
    enable_vad=self._enable_vad,
    auto_turn_from_stt_final=self._auto_turn_from_stt_final,
    emit=self._emit,
    is_running=lambda: self._is_running,
    current_turn=lambda: self._turn,
    is_stt_active=lambda: self._stt_committer.is_active,
    outbound_queue=cfg.outbound_queue,
)
```

Update event-bus subscriptions (lines 275–276):

```python
self.event_bus.subscribe(PlaybackMarkAck, self._audio_router.on_playback_ack)
self.event_bus.subscribe(TransportAudioDelivered, self._audio_router.on_audio_delivered)
```

Update the Phase 1 STTCommitter wiring to use the real callback:

```python
self._stt_committer = STTCommitter(
    ...,
    on_speech_detection_reset=self._audio_router.reset_speech_detection,
    ...,
)
```

Delete from Session `__init__`:

- [ ] `self._auto_turn_speech_frames` (line 369)
- [ ] `self._replay_chunks_pending` (line 376)
- [ ] `self._playback_mark_bytes_interval` (line 377)
- [ ] `self._playback_mark_seq` (line 378)
- [ ] `self._playback_ack_transport` (line 380)
- [ ] `self._transport_reports_audio_delivery` (line 383)
- [ ] `self._outbound_queue_external` (line 315)
- [ ] `self._outbound_queue_max_size`, `_outbound_queue_policy`,
  `_outbound_queue_name` (lines 316–318) — pushed into router or
  becomes parameters
- [ ] `self._outbound_queue` (line 319)
- [ ] `self._outbound_task` (line 325)
- [ ] `self._pipeline_task` (line 357) — moves to router

### T2.5: Update Session call sites

Lifecycle methods on Session call into the router instead of the
old method bodies:

- [ ] `start()` (line 1226):
  - `self._outbound_task = asyncio.create_task(self._drain_outbound_audio())`
    → `self._audio_router.start_outbound()`
  - `self._pipeline_task = asyncio.create_task(self._run_pipeline())`
    → `self._audio_router.start_ingress()`
- [ ] `stop()` / `shutdown()`: invoke `self._audio_router.stop_ingress()`,
  `self._audio_router.stop_outbound()`, `self._audio_router.await_drain(...)`.
- [ ] `replay_gated_audio` (public method, line 1151) becomes:
  ```python
  async def replay_gated_audio(self, events: list[Any]) -> None:
      await self._audio_router.gated_replay(events)
  ```
- [ ] `_run_streaming_agent` (line 2271) — calls `_wait_outbound_drain`.
  Becomes `await self._audio_router.await_drain()`. Stays on Session
  in Phase 2; moves to TurnRunner in Phase 5.

### T2.6: Outbound chunk producer hook

TTS synthesis (today in `_synthesize_tts`, line 2424; moves to
TTSScheduler in Phase 3) pushes chunks onto
`self._outbound_queue`. With the queue now living on the router,
TTSScheduler in Phase 3 will need a way to enqueue.

**Decision:** AudioRouter exposes `queue_outbound(chunk)`.
TTSScheduler in Phase 3 takes a reference to the router and calls
`audio_router.queue_outbound(chunk)`. Phase 2 makes the method
public; Phase 3 wires it up.

In Phase 2, the existing `_synthesize_tts` body on Session is
updated to call `self._audio_router.queue_outbound(chunk)` directly
(line-level change inside the method body that stays on Session
until Phase 3).

### T2.7: Delete migrated code from Session

After all callers updated, delete from `_session.py`:

- [ ] `_run_pipeline` (lines 2052–2121)
- [ ] `_drain_outbound_audio` (lines 2500–2540)
- [ ] `_stamp_outbound_chunk` (lines 2541–2548)
- [ ] `_handle_audio_delivery` (lines 2549–2575)
- [ ] `_send_playback_mark` (lines 2576–2594)
- [ ] `_on_playback_mark_ack` (lines 2595–2606)
- [ ] `_on_transport_audio_delivered` (lines 2607–2617)
- [ ] `_wait_outbound_drain` (lines 2483–2499)
- [ ] Body of `replay_gated_audio` minus the one-line delegate.

Total: ~310 lines.

### T2.8: Tests

- [ ] Add `tests/session/test_audio_router.py`. Cover:
  - Ingress: feed three chunks; verify `AudioIn` emitted, audio
    stage executed, VAD stage executed, STT stage called only when
    `is_stt_active()` returns True.
  - Auto-turn detection: when `auto_turn_from_stt_final=True` and VAD
    off, after two chunks with speech energy the router triggers
    `turn_manager.start_turn()`.
  - `reset_speech_detection()` zeroes the counter (regression test
    for the STTCommitter coupling).
  - Outbound drain: chunks queued are pulled and `transport.send_audio`
    called.
  - Playback marks: after `bytes_interval` bytes, a mark is emitted.
  - `on_playback_ack` correctly updates `bytes_since_last_mark`.
  - `gated_replay` replays events through the same path as live audio.
- [ ] Existing transport tests (`tests/transports/`) pass unchanged.
- [ ] WebRTC tests (`tests/transports/test_webrtc.py`) pass unchanged —
  the queue ownership bug is not addressed here.
- [ ] Bundle replay parity.

## Acceptance criteria

- [ ] `uv run pytest` passes unchanged.
- [ ] `uv run ruff check .` passes.
- [ ] `wc -l src/easycat/session/_session.py` drops by ~310.
- [ ] `wc -l src/easycat/session/_audio_router.py` lands at ~380.
- [ ] `grep -c '^    def\|^    async def' src/easycat/session/_session.py`
  drops by 9.
- [ ] Phase 1 STTCommitter's `on_speech_detection_reset` no longer
  uses the no-op stub (verify via `grep 'lambda: None' src/easycat/session/_session.py`).
- [ ] Journal record names unchanged.
- [ ] Perf gate passes (ingress is hot path; this is the most
  perf-sensitive phase).
- [ ] Bundle round-trip parity.

## Risks and rollback

**Risk 1 — Hot path overhead.** `_run_pipeline` runs once per audio
chunk (~20ms at 50fps). Moving it to a collaborator adds two
attribute lookups (`self._audio_router._...`). Verify perf gate
shows no regression. If it does, inline the hottest reads via
local variables in the loop body.

**Risk 2 — `current_turn` callback freshness.** The router reads
`self._current_turn()` per chunk; if Session's `_turn` changes
mid-loop (turn boundary), the router sees the new turn. This is
identical to today's behaviour (where `_run_pipeline` reads
`self._turn` directly). The callback indirection is a closure
read — no measurable cost.

**Risk 3 — Outbound queue ownership during reconnect.** If
`cfg.outbound_queue` is supplied externally (used by supervisor /
external broadcaster), reconnect logic must preserve the same queue
instance. The router takes the queue as a constructor parameter;
verify reconnect path doesn't reconstruct the router.

**Risk 4 — WebRTC queue ownership bug surfaces.** The known
WebRTC `_handle_offer` queue swap bug is unchanged but moves to
under the router. Document that fixing it is out of scope for this
phase. Add a comment in the router source referencing the issue.

**Rollback.** Phase 2 was designed as a single PR. In the current
codebase the later phases have landed, so use this section as original
risk context rather than current rollback instructions.

## Verification commands

```bash
uv run pytest tests/session/ tests/transports/
uv run pytest tests/runtime/test_replay.py
uv run ruff check src/easycat/session/
wc -l src/easycat/session/_session.py src/easycat/session/_audio_router.py
grep -c '^    def\|^    async def' src/easycat/session/_session.py
uv run python perf/turn_latency_bench.py  # or whatever the perf gate command is
```
