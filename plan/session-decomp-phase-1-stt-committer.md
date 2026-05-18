# Session Decomposition — Phase 1: STTCommitter

> **Part of the session decomposition.** Overview lives in
> `session-decomp-overview.md`. This file is the operational plan
> for Phase 1.
>
> **Predecessors:** Phase 0 (`session-decomp-phase-0-turn-context.md`).
> **Successors:** none direct. Phase 5 (TurnRunner) takes a reference
> to the STTCommitter constructed here. Phase 2 (AudioRouter) has a
> small documented coupling: `STTCommitter.cancel()` calls
> `audio_router.reset_speech_detection()`. The hook stubs to a no-op
> until Phase 2 lands.
>
> **Risk:** low. Self-contained subsystem with its own state.
> Cleanest extract in the sequence.
>
> **Compatibility policy:** internal-only changes. STTCommitter is a
> private collaborator (`session/_stt_committer.py`); not exported.

## Goal

Extract STT segment commit scheduling into a single-file
collaborator that owns its own state (active flag, in-flight tasks,
provider invocation) and exposes a small, typed API to Session.
Today this logic is 9 methods scattered across Session with state on
4 instance attrs. After Phase 1, Session holds one
`self._stt_committer: STTCommitter` field and delegates.

## Scope

**In scope:**

- Create `src/easycat/session/_stt_committer.py`
- Move 9 methods + `_cancel_stt` into the new file
- Migrate 4 session-long attrs (`_stt_active`, `_stt_task`,
  `_stt_pause_commit_task`, `_stt_segment_commit_task`) to live on
  the committer
- Wire the committer into Session `__init__`
- Update event-bus subscription wiring to target committer methods
- Update callers in `_handle_end_of_speech` (stays on Session in
  Phase 1; moves to TurnRunner in Phase 5) to use the committer's
  public API

**Out of scope:**

- Any change to STT provider implementations
  (`src/easycat/stt/*`)
- Any change to `STTProvider` protocol or `STTBase` abstract class
- Any change to journal record names or shapes
- Moving `_handle_end_of_speech` (deferred to Phase 5)

## Tasks

### T1.0: Architecture freeze

- [ ] Confirm the public API surface below is what TurnRunner /
  Session need. Specifically: TurnRunner's `_handle_end_of_speech`
  needs (a) commit-on-end, (b) await-pending-with-timeout, (c)
  transcript-via-final-future. STTCommitter exposes these as
  `commit_now`, `await_pending`, `await_final(timeout)`.
- [ ] Confirm `_cancel_stt` belongs on STTCommitter (yes — most of
  its state is the committer's own).
- [ ] Confirm the AudioRouter coupling: Phase 1 introduces a
  module-level no-op `reset_speech_detection` callback that Session
  passes in. Phase 2 replaces it with the real
  `audio_router.reset_speech_detection`. No interface change between
  the two phases.

### T1.1: Create `_stt_committer.py` module

- [ ] Create `src/easycat/session/_stt_committer.py`.
- [ ] Define the class skeleton:

```python
"""Owns STT segment commit scheduling for a Session.

A Session feeds audio to an STT provider; periodically (driven by
VAD pause events or end-of-speech), the committer asks the provider
to flush its buffered segment and resolves the resulting transcript
future. The committer is the single owner of:

- the "STT is currently consuming audio" flag (`_active`)
- the in-flight segment commit task
- the scheduled commit task (delayed after VAD pause)
- the background STT event consumer task

Session delegates to one ``STTCommitter`` instance per session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from easycat.cancel import CancelToken
from easycat.events import (
    Error,
    ErrorStage,
    EventBus,
    STTFinal,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.providers import STTProvider
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink
from easycat.timeouts import TimeoutConfig, STTTimeoutError

if TYPE_CHECKING:
    from easycat.session._turn_context import TurnContext

logger = logging.getLogger(__name__)


class STTCommitter:
    """Schedules and commits STT segments for a Session."""

    def __init__(
        self,
        *,
        stt: STTProvider,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        runtime_scope: RuntimeScope,
        timeout_config: TimeoutConfig,
        segment_silence_ms: int,
        on_speech_detection_reset: Callable[[], None] = lambda: None,
    ) -> None:
        self._stt = stt
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._runtime_scope = runtime_scope
        self._timeout_config = timeout_config
        self._segment_silence_ms = segment_silence_ms
        self._on_speech_detection_reset = on_speech_detection_reset

        self._active: bool = False
        self._stt_task: asyncio.Task[None] | None = None
        self._pause_commit_task: asyncio.Task[None] | None = None
        self._segment_commit_task: asyncio.Task[None] | None = None
```

### T1.2: Public API

The committer exposes the following methods. Each is a direct port
of an existing Session method but operates on its own state and
takes `TurnContext` explicitly:

| Public method | Source (Session method) | Line |
|---|---|---:|
| `start_event_loop()` | `_start_stt_event_task` | 2000 |
| `schedule(turn, _evt)` | `_schedule_stt_segment_commit` | 1875 |
| `cancel_scheduled(turn=None, _evt=None)` | `_cancel_scheduled_stt_segment_commit` | 1857 |
| `cancel_inflight()` | `_cancel_inflight_stt_segment_commit` | 1863 |
| `resolve_pending(turn, value)` | `_resolve_pending_stt_segment_futures` | 1869 |
| `commit_now(turn)` | `_commit_stt_segment` | 1918 |
| `await_pending(turn) -> bool` | `_await_pending_stt_segments` | 1975 |
| `cancel()` | `_cancel_stt` | 2786 |
| `mark_active()` | new (sets `self._active = True`) | — |
| `mark_inactive()` | new (sets `self._active = False`) | — |
| `is_active` (property) | new (returns `self._active`) | — |

Internal helpers (move alongside but keep underscored):

- `_commit_segment_after` (was `_commit_stt_segment_after`, 1890)
- `_start_segment_commit` (was `_start_stt_segment_commit`, 1897)

### T1.3: Port the 9 methods

For each method in the table above (T1.2), the port is mechanical:

- [ ] Copy the body from `_session.py`.
- [ ] Replace `self.stt` → `self._stt`.
- [ ] Replace `self._stt_active` → `self._active`.
- [ ] Replace `self._stt_task` → `self._stt_task` (same name on committer).
- [ ] Replace `self._stt_pause_commit_task` → `self._pause_commit_task`.
- [ ] Replace `self._stt_segment_commit_task` → `self._segment_commit_task`.
- [ ] Replace `self._stt_segment_silence_ms` → `self._segment_silence_ms`.
- [ ] Replace `self._runtime_scope` → `self._runtime_scope`.
- [ ] Replace `self._journal_sink` → `self._journal_sink`.
- [ ] Replace `self._timeout_config` → `self._timeout_config`.
- [ ] Replace `self._emit(evt)` → `await self._event_bus.emit(evt)`
  (committer does not own correlation stamping; if needed, take a
  `Callable[[Any], Awaitable[None]]` for emit at construction).
  **Decision:** committer takes `emit: Callable[[Any], Awaitable[None]]`
  in `__init__` and uses it. Session passes `self._emit` so
  correlation stamping is preserved.
- [ ] Replace `self._auto_turn_speech_frames = 0` (in `_cancel_stt`,
  line 2796) with `self._on_speech_detection_reset()` callback.
- [ ] Replace `turn = self._turn` with the `turn: TurnContext`
  parameter introduced in Phase 0 T0.3.
- [ ] Replace `self._stt_final_future`, `self._stt_pending_segment_futures`
  with the Phase 0 TurnContext fields
  (`turn.stt_final_future`, `turn.pending_stt_segment_futures`).

### T1.4: Wire STTCommitter into Session `__init__`

In `_session.py:__init__`, after `self._journal_sink` is created
(around line 392):

```python
self._stt_committer = STTCommitter(
    stt=self.stt,
    event_bus=self.event_bus,
    journal_sink=self._journal_sink,
    runtime_scope=self._runtime_scope,
    timeout_config=self._timeout_config,
    segment_silence_ms=self._stt_segment_silence_ms,
    on_speech_detection_reset=lambda: None,  # Phase 2 replaces with audio_router.reset_speech_detection
    emit=self._emit,
)
```

Update event-bus subscriptions (lines 273–274):

```python
self.event_bus.subscribe(VADStopSpeaking, self._stt_committer.schedule)
self.event_bus.subscribe(VADStartSpeaking, self._stt_committer.cancel_scheduled)
```

Delete from Session `__init__`:

- [ ] `self._stt_task` (line 358)
- [ ] `self._stt_pause_commit_task` (line 363)
- [ ] `self._stt_segment_commit_task` (line 364)
- [ ] `self._stt_active` (line 367)
- [ ] `self._stt_segment_silence_ms` (line 304) — moves into committer

Keep on Session (read via property if needed):

- [ ] `self._stt_committer.is_active` accessor — used by
  `_run_pipeline` to gate audio forwarding.

### T1.5: Update Session call sites

Replace direct method calls in Session with committer calls:

- [ ] `_handle_end_of_speech` (line 2123):
  - `self._cancel_scheduled_stt_segment_commit()` → `self._stt_committer.cancel_scheduled(turn=self._turn)`
  - `self._stt_active` reads → `self._stt_committer.is_active`
  - `self._stt_active = False` → `self._stt_committer.mark_inactive()`
  - `self._stt_segment_commit_task` → query via
    `self._stt_committer._segment_commit_task` is internal; instead
    expose `await self._stt_committer.await_inflight_commit()` for
    the await at line 2135. Or have `await_pending` swallow this.
  - `self._await_pending_stt_segments()` → `self._stt_committer.await_pending(self._turn)`
  - `self.stt.end_stream()` → STT end-stream is still called directly
    in `_handle_end_of_speech`; consider whether to move into
    committer as `committer.end_stream()`. **Decision:** add
    `STTCommitter.end_stream()` that calls `self._stt.end_stream()`
    and appends the future when there's uncommitted audio. Caller
    just awaits the committer.
- [ ] `_run_pipeline` (line 2052): `self._stt_active` →
  `self._stt_committer.is_active`. Audio chunk forwarding stays in
  AudioRouter's territory; Phase 2 takes this over.
- [ ] `start()` (line 1226): the `_stt_active = True` write → call
  `self._stt_committer.mark_active()` and
  `self._stt_committer.start_event_loop()`. Identify the exact line
  during implementation.
- [ ] `_reset_turn_state` (line 757):
  `self._cancel_scheduled_stt_segment_commit()` →
  `self._stt_committer.cancel_scheduled(turn=self._turn)`;
  `self._cancel_inflight_stt_segment_commit()` →
  `self._stt_committer.cancel_inflight()`;
  `self._resolve_pending_stt_segment_futures("")` →
  `self._stt_committer.resolve_pending(self._turn, "")`.
- [ ] Wherever `_cancel_stt()` is called today (search:
  `grep -n 'self\._cancel_stt' src/easycat/session/_session.py`) →
  `self._stt_committer.cancel()`.

### T1.6: Cancel-path correctness

Verify that the committer's `cancel()` preserves the exact
sequencing of today's `_cancel_stt`:

1. `cancel_and_drain("stt_pause_commit")`
2. `cancel_and_drain("stt_segment_commit")`
3. Clear task handles
4. `await self._stt.end_stream()` (swallowed exception)
5. `self._active = False`
6. `self._on_speech_detection_reset()` — was
   `self._auto_turn_speech_frames = 0`
7. Cancel `self._stt_task`
8. `await self._stt_task` (swallow exceptions)
9. `resolve_pending(turn, "")` — clears `pending_stt_segment_futures`
10. Resolve `turn.stt_final_future` if not done; set to `None`

Add a regression test (see T1.8) that asserts the exact ordering
via journal records.

### T1.7: Delete migrated code from Session

After all callers are updated, delete from `_session.py`:

- [ ] Method bodies (lines 1857–1875, 1890–1973, 1975–1998,
  2000–2050, 2786–2806) — total ~280 lines.
- [ ] Local imports that are no longer needed (e.g.,
  `STTTimeoutError` import if only used by the moved methods).

### T1.8: Tests

- [ ] Add `tests/session/test_stt_committer.py`. Cover:
  - `schedule` then immediate `cancel_scheduled` cancels the task.
  - `schedule` then wait → `commit_now` runs and emits the two
    journal records (`stt_segment_commit_requested`,
    `stt_segment_commit_result`).
  - `commit_now` when `turn.cancel_token.is_cancelled` skips the
    provider call.
  - `commit_now` when `commit_segment` returns False reinstates
    `turn.stt_has_uncommitted_audio = True` and resolves the future
    with empty string.
  - `await_pending` returns False on STT timeout and emits
    `Error(stage=ErrorStage.STT)`.
  - `cancel()` invokes `on_speech_detection_reset`.
- [ ] Existing tests in `tests/session/` must pass unchanged. If any
  test reaches `session._stt_active` directly, update it to use
  `session._stt_committer.is_active`. (Audit:
  `grep -rn '_stt_active\|_stt_task\|_stt_pause_commit_task\|_stt_segment_commit_task' tests/`)
- [ ] Replay parity: a bundle captured pre-Phase-1 replays cleanly
  post-Phase-1. Run `tests/runtime/test_replay.py`.

## Acceptance criteria

- [ ] `uv run pytest` passes unchanged.
- [ ] `uv run ruff check .` passes.
- [ ] `wc -l src/easycat/session/_session.py` drops by ~280.
- [ ] `wc -l src/easycat/session/_stt_committer.py` lands at ~320
  (the methods plus ~40 lines of class scaffolding).
- [ ] `grep -c '^    def\|^    async def' src/easycat/session/_session.py`
  drops by 10 (9 moved + `_cancel_stt`).
- [ ] Journal record names unchanged (verify with
  `grep -h 'name=' src/easycat/session/_session.py src/easycat/session/_stt_committer.py | sort -u`
  → no `stt_*` records lost, no new ones added).
- [ ] Bundle round-trip parity.
- [ ] No regression in P50/P90 turn latency (perf gate).

## Risks and rollback

**Risk 1 — STT end-stream sequencing.** Today
`_handle_end_of_speech` calls `self.stt.end_stream()` directly
between two `_await_pending_stt_segments` calls (lines 2147–2152).
The first await blocks on segment commit; the end-stream may
generate one more segment; the second await blocks on that. If
`end_stream` moves into the committer, this ordering must be
preserved by the caller (`_handle_end_of_speech`). Document the
contract in the committer's docstring.

**Risk 2 — `_stt_active` race.** Today `_run_pipeline` reads
`self._stt_active` without synchronization on every audio chunk.
Reading `self._stt_committer.is_active` adds an attribute hop. Verify
the cost is negligible (one Python attribute load — yes).

**Risk 3 — Test reliance on internal Session attrs.** Some tests may
reach into Session's private STT state. Audit and update.

**Rollback.** Phase 1 is a single PR. Revert reverts cleanly because
no other phase depends on it.

## Verification commands

```bash
uv run pytest tests/session/ tests/stt/
uv run pytest tests/runtime/test_replay.py
uv run ruff check src/easycat/session/
wc -l src/easycat/session/_session.py src/easycat/session/_stt_committer.py
grep -c '^    def\|^    async def' src/easycat/session/_session.py
grep -rn '_stt_active\|_stt_pause_commit_task\|_stt_segment_commit_task' src/ tests/
```
