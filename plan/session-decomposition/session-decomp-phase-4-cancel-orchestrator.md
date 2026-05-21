# Session Decomposition — Phase 4: CancelOrchestrator

> **Historical implementation checklist.** `src/easycat/session/_cancel_orchestrator.py`
> exists in the current codebase; keep this file as rationale and review
> context. Line numbers and unchecked task boxes are from the original
> extraction plan.
>
> **Part of the session decomposition.** Overview lives in
> `session-decomp-overview.md`. This file is the operational plan
> for Phase 4.
>
> **Predecessors:** Phase 0. Phase 1 (STTCommitter) and Phase 3
> (TTSScheduler) are strongly recommended predecessors because
> `CancelOrchestrator` composes their `.cancel()` methods. If those
> phases have not landed, this phase calls the unmoved Session
> methods directly until they exist.
> **Successors:** Phase 5 (TurnRunner) takes a reference to the
> CancelOrchestrator.
>
> **Risk:** medium. The control-signal propagation path touches all
> 7 stages plus several collaborators; getting the ordering right is
> the load-bearing correctness property.
>
> **Compatibility policy:** internal-only changes.
> CancelOrchestrator is a private collaborator
> (`session/_cancel_orchestrator.py`); not exported. The public
> Session facades (`cancel_turn`, `cancel_tts_playback`,
> `reset_state`) retain their signatures.

## Goal

Extract the barge-in / control-signal propagation policy into one
collaborator. Today this logic spans 4 methods on Session and
references all 7 pipeline stages plus the STTCommitter and
TTSScheduler. After Phase 4, Session holds one
`self._cancel: CancelOrchestrator` field; its public cancel facades
delegate.

## Scope

**In scope:**

- Create `src/easycat/session/_cancel_orchestrator.py`
- Move `_propagate_upstream_signal`, `_cancel_for_barge_in`,
  `_record_interruption_notification` into the new file
- Migrate 4 interruption-config attrs
  (`_interruption_mode`,
  `_interruption_latency_compensation_ms`,
  `_interruption_ack_stale_ms`,
  `_interruption_ack_tail_cap_ms`) to the orchestrator
- Wire the orchestrator into Session `__init__`
- Reduce Session's public `cancel_turn`, `cancel_tts_playback`,
  `reset_state` methods to one-line delegates
- Update `TurnManager`'s `cancel_turn_callback` wiring (line 265)
  to point at the orchestrator

**Out of scope:**

- Any change to `ControlSignal` / `_ControlSignal` types
- Any change to `Stage.handle_upstream` protocol
- Any change to `InterruptionController`
  (from Workstream 3; lives in `_interruption_controller.py`)
- Moving `_cancel_stt` / `_cancel_tts` (already in Phases 1 / 3)
- Moving `_handle_end_of_speech` / `_run_streaming_agent`
  (Phase 5)

## Tasks

### T4.0: Architecture freeze

- [ ] Confirm CancelOrchestrator takes refs to **all 7 stages**:
  `transport_stage`, `tts_stage`, `agent_stage`, `turn_stage`,
  `stt_stage`, `vad_stage`, `audio_stage`. Today
  `_propagate_upstream_signal` walks them in late-to-early order;
  the orchestrator preserves that order.
- [ ] Confirm CancelOrchestrator composes
  `STTCommitter.cancel()` and `TTSScheduler.cancel()` rather than
  reaching into their state. Each subsystem owns its own cancel
  path.
- [ ] Confirm the `_cancel_for_barge_in` return semantics: returns
  `False` if barge-in is suppressed (e.g. a no-interrupt
  `SessionAction` is queued). Wiring through `TurnManager` requires
  this contract to be preserved.
- [ ] Confirm the public facades on Session retain their exact
  signatures:
  ```python
  async def cancel_turn(self, *, barge_in: bool = False) -> None: ...
  async def cancel_tts_playback(self) -> None: ...
  async def reset_state(self) -> None: ...
  ```

### T4.1: Create `_cancel_orchestrator.py` module

- [ ] Create `src/easycat/session/_cancel_orchestrator.py`.
- [ ] Define the class skeleton:

```python
"""Owns control-signal propagation and barge-in policy for a Session.

Responsibilities:

- Walk a control signal through every stage in late-to-early order
  (transport → tts → agent → turn → stt → vad → audio), giving each
  stage a chance to observe and record the signal via
  ``Stage.handle_upstream``.
- Compose ``STTCommitter.cancel()`` and ``TTSScheduler.cancel()``
  during a turn cancel.
- Implement the barge-in suppression policy: if a queued session
  action declares ``no_interrupt=True`` (e.g. an end-call
  announcement), barge-in is suppressed and the orchestrator returns
  ``False`` so the TurnManager does not start a new user turn.
- Write the ``interruption_notification`` journal record so a
  bundle reader can reconstruct what text the user heard at the
  point of barge-in.

The orchestrator does not own the in-progress turn; that lives on
Session and is passed in via the ``current_turn`` callback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from easycat.events import EventBus
from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.session._journal_sink import SessionJournalSink

if TYPE_CHECKING:
    from easycat.session._stt_committer import STTCommitter
    from easycat.session._tts_scheduler import TTSScheduler
    from easycat.session._turn_context import TurnContext
    from easycat.session.actions import SessionActions
    from easycat.stages.base import Stage

logger = logging.getLogger(__name__)


class CancelOrchestrator:
    """Coordinates barge-in, control-signal propagation, and interruption records."""

    def __init__(
        self,
        *,
        # All 7 stages in propagation order
        transport_stage: "Stage",
        tts_stage: "Stage",
        agent_stage: "Stage",
        turn_stage: "Stage",
        stt_stage: "Stage",
        vad_stage: "Stage",
        audio_stage: "Stage",
        # Collaborators whose cancel() this orchestrator composes
        stt_committer: "STTCommitter",
        tts_scheduler: "TTSScheduler",
        # Context
        run_ctx: RunContext,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        # Interruption config (was 4 fields on Session)
        interruption_mode: str,
        interruption_latency_compensation_ms: int,
        interruption_ack_stale_ms: int,
        interruption_ack_tail_cap_ms: int,
        # Callbacks
        current_turn: Callable[[], "TurnContext | None"],
        session_actions: Callable[[], "SessionActions | None"],
        telephony_helpers_present: Callable[[], bool],
        cancel_turn_impl: Callable[..., Any],
    ) -> None:
        self._stages = (
            transport_stage,
            tts_stage,
            agent_stage,
            turn_stage,
            stt_stage,
            vad_stage,
            audio_stage,
        )
        self._stt_committer = stt_committer
        self._tts_scheduler = tts_scheduler
        self._run_ctx = run_ctx
        self._event_bus = event_bus
        self._journal_sink = journal_sink

        self.interruption_mode = interruption_mode
        self.latency_compensation_ms = interruption_latency_compensation_ms
        self.ack_stale_ms = interruption_ack_stale_ms
        self.ack_tail_cap_ms = interruption_ack_tail_cap_ms

        self._current_turn = current_turn
        self._session_actions = session_actions
        self._telephony_helpers_present = telephony_helpers_present
        self._cancel_turn_impl = cancel_turn_impl
```

### T4.2: Public API

| Public method | Source (Session method) | Line |
|---|---|---:|
| `propagate_signal(signal, *, cause=None)` | `_propagate_upstream_signal` | 634 |
| `for_barge_in() -> bool` | `_cancel_for_barge_in` | 1664 |
| `record_interruption(...)` | `_record_interruption_notification` | 735 |

Configuration accessors (read-only properties) expose the four
interruption knobs:

- `interruption_mode`, `latency_compensation_ms`, `ack_stale_ms`,
  `ack_tail_cap_ms`.

### T4.3: Port the 3 methods

For each method:

- [ ] Copy body from `_session.py`.
- [ ] Replace stage refs (`self._transport_stage`, etc.) with the
  `self._stages` tuple iterated in order.
- [ ] Replace `self._run_ctx` → `self._run_ctx`.
- [ ] Replace `self._telephony_helpers` truthy check →
  `self._telephony_helpers_present()`.
- [ ] Replace `self._turn` → `self._current_turn()`.
- [ ] Replace `self._session_actions` (in `_cancel_for_barge_in`) →
  `self._session_actions()`.
- [ ] Replace `self.cancel_turn(barge_in=True)` (in
  `_cancel_for_barge_in`) → `await self._cancel_turn_impl(barge_in=True)`.
- [ ] Replace `self._journal_sink` → `self._journal_sink`.

The control-signal flow (in `propagate_signal`) preserves the
existing telephony-helper journal write at the tail
(`_journal_control_signal(self._run_ctx, stage="telephony", ...)`)
and the cause-annotation record.

### T4.4: `for_barge_in` semantics

The Phase 4 implementation of `for_barge_in()` calls back into
Session via the `cancel_turn_impl` callback:

```python
async def for_barge_in(self) -> bool:
    actions = self._session_actions()
    if actions is not None and actions.no_interrupt:
        logger.debug("Barge-in suppressed: queued action has no_interrupt=True")
        return False
    await self._cancel_turn_impl(barge_in=True)
    return True
```

The callback indirection is necessary because Session's public
`cancel_turn` method is the single entry point that coordinates
everyone (committer cancel, scheduler cancel, signal propagation,
turn-manager reset). The orchestrator can compose the cancel path
internally as well — see T4.6 for the recommended split.

### T4.5: Wire CancelOrchestrator into Session `__init__`

In `_session.py:__init__`, after STTCommitter and TTSScheduler are
constructed:

```python
self._cancel = CancelOrchestrator(
    transport_stage=self._transport_stage,
    tts_stage=self._tts_stage,
    agent_stage=self._agent_stage,
    turn_stage=self._turn_stage,
    stt_stage=self._stt_stage,
    vad_stage=self._vad_stage,
    audio_stage=self._audio_stage,
    stt_committer=self._stt_committer,
    tts_scheduler=self._tts_scheduler,
    run_ctx=self._run_ctx,
    event_bus=self.event_bus,
    journal_sink=self._journal_sink,
    interruption_mode=cfg.interruption_mode,
    interruption_latency_compensation_ms=cfg.interruption_latency_compensation_ms,
    interruption_ack_stale_ms=cfg.interruption_ack_stale_ms,
    interruption_ack_tail_cap_ms=cfg.interruption_ack_tail_cap_ms,
    current_turn=lambda: self._turn,
    session_actions=lambda: self._session_actions,
    telephony_helpers_present=lambda: bool(self._telephony_helpers),
    cancel_turn_impl=self.cancel_turn,
)
```

Update the TurnManager construction (line 262–270) to point its
`cancel_turn_callback` at the orchestrator:

```python
self._turn_manager = cfg.turn_manager or TurnManager(
    ...,
    cancel_turn_callback=self._cancel.for_barge_in,
    ...,
)
```

⚠️ **Construction order.** The TurnManager is currently constructed
before the stages (line 262 vs 413). After Phase 4, the orchestrator
needs all 7 stages to exist. Either:

- **Option A (recommended):** construct TurnManager early without
  the `cancel_turn_callback`; install it after the orchestrator
  exists via `self._turn_manager.set_cancel_callback(self._cancel.for_barge_in)`.
  Requires adding the setter on TurnManager.
- **Option B:** reshuffle `__init__` so stages and orchestrator are
  constructed before TurnManager. May break other construction-order
  invariants.

Pick Option A. Add the setter in Phase 4.

Delete from Session `__init__`:

- [ ] `self._interruption_mode` (line 252)
- [ ] `self._interruption_latency_compensation_ms` (line 253)
- [ ] `self._interruption_ack_stale_ms` (line 256)
- [ ] `self._interruption_ack_tail_cap_ms` (line 257)

These now live on `self._cancel.<...>`. If any code reads them off
Session, expose via property delegate or update the caller.

### T4.6: Reduce Session public facades

The three public Session cancel methods become thin coordinators
that call into the orchestrator. Their internal logic — which
already touches STTCommitter (Phase 1), TTSScheduler (Phase 3), and
signal propagation — gets reduced.

**Today's `cancel_turn`** (lines 1525–1553) does roughly:

1. Cancel the turn's `cancel_token`.
2. Cancel STT (`_cancel_stt`).
3. Cancel TTS (`_cancel_tts`).
4. Propagate the cancel signal upstream
   (`_propagate_upstream_signal(signal, cause=...)`).
5. Reset `_turn_manager` to IDLE.
6. Clear `self._turn`.

After Phase 4, this becomes a coordinator method on Session that
calls:

```python
async def cancel_turn(self, *, barge_in: bool = False) -> None:
    turn = self._turn
    if turn is not None and not turn.cancel_token.is_cancelled:
        turn.cancel_token.cancel()
        if barge_in:
            turn.record_barge_in()
    await self._stt_committer.cancel()
    await self._tts_scheduler.cancel()
    await self._cancel.propagate_signal(_make_cancel_signal(), cause="barge_in" if barge_in else "cancel_turn")
    await self._turn_manager.reset()
    if self._turn is turn:
        self._turn = None
```

The actual fields and ordering must mirror today's `cancel_turn`
exactly — verify line-by-line against lines 1525–1553. The
proposed shape above is illustrative.

**`cancel_tts_playback`** (lines 1554–1572) becomes:

```python
async def cancel_tts_playback(self) -> None:
    await self._tts_scheduler.cancel()
    await self._cancel.propagate_signal(_make_cancel_tts_signal(), cause="cancel_tts_playback")
```

**`reset_state`** (lines 1573–1592) similar reduction.

### T4.7: Delete migrated code from Session

After all callers updated, delete from `_session.py`:

- [ ] `_propagate_upstream_signal` (lines 634–682)
- [ ] `_cancel_for_barge_in` (lines 1664–1678)
- [ ] `_record_interruption_notification` (lines 735–756)

Total: ~140 lines (including the body shrinkage of the three public
facades).

### T4.8: Tests

- [ ] Add `tests/session/test_cancel_orchestrator.py`. Cover:
  - `propagate_signal` walks all 7 stages in late-to-early order;
    each receives `handle_upstream(signal, run_ctx)`.
  - Stage exception in `handle_upstream` does not break propagation
    (other stages still receive the signal).
  - Telephony helper journal record is written when
    `telephony_helpers_present()` returns True.
  - Cause-annotation record is appended when `cause` is supplied.
  - `for_barge_in` returns `False` when a `no_interrupt` action is
    queued.
  - `for_barge_in` returns `True` and invokes `cancel_turn_impl`
    otherwise.
  - `record_interruption` writes the journal record with the same
    `name` and `data` shape as before.
- [ ] Existing barge-in tests in `tests/session/test_interruption*.py`
  pass unchanged. The full WS2B three-cancellation-mode matrix must
  still pass (this is the load-bearing correctness gate for the
  whole decomposition).
- [ ] Bundle replay parity.

## Acceptance criteria

- [ ] `uv run pytest` passes unchanged.
- [ ] `uv run pytest tests/session/test_interruption*.py` passes
  unchanged.
- [ ] `uv run ruff check .` passes.
- [ ] `wc -l src/easycat/session/_session.py` drops by ~140.
- [ ] `wc -l src/easycat/session/_cancel_orchestrator.py` lands
  at ~200.
- [ ] `grep -c '^    def\|^    async def' src/easycat/session/_session.py`
  drops by 3 (the three moved methods).
- [ ] Public facades `cancel_turn`, `cancel_tts_playback`, and
  `reset_state` retain their exact signatures.
- [ ] Journal record names unchanged.
- [ ] Bundle round-trip parity.
- [ ] No regression in the cancellation latency micro-benchmark
  (signal-to-stage-quiescence).

## Risks and rollback

**Risk 1 — TurnManager construction-order coupling.** The
`cancel_turn_callback` is set at TurnManager construction today
(line 265). Phase 4 requires it to be installed after the
orchestrator exists. Option A (setter) is mechanical. If the
TurnManager API resists adding a setter for design reasons, fall
back to Option B (reshuffle `__init__`).

**Risk 2 — Stage iteration order drift.** Today
`_propagate_upstream_signal` hard-codes the order
`(transport, tts, agent, turn, stt, vad, audio)`. The orchestrator
preserves this. Any change requires updating WS3's test
expectations.

**Risk 3 — `cancel_turn_impl` callback indirection.** The
orchestrator's `for_barge_in` calls back into Session's
`cancel_turn`. If Session's `cancel_turn` is reduced too far and
calls back into the orchestrator (`propagate_signal`), there's no
loop — `for_barge_in` only kicks off `cancel_turn`, and
`cancel_turn` calls `propagate_signal` (not `for_barge_in`).
Verify with a unit test that asserts no infinite recursion.

**Risk 4 — `interruption_*` field reads.** Some external test may
read `session._interruption_mode`. Audit and update via property
delegate if needed. **Decision:** add Session-level read-only
properties that delegate to `self._cancel.interruption_mode` etc.
to preserve external test compatibility.

**Rollback.** Phase 4 was designed as a single PR before Phase 5. In
the current codebase Phase 5 has landed, so use this section as original
risk context rather than current rollback instructions.

## Verification commands

```bash
uv run pytest tests/session/ tests/session/test_interruption*.py
uv run pytest tests/runtime/test_replay.py
uv run ruff check src/easycat/session/
wc -l src/easycat/session/_session.py src/easycat/session/_cancel_orchestrator.py
grep -c '^    def\|^    async def' src/easycat/session/_session.py
grep -rn '_interruption_mode\|_propagate_upstream_signal' src/ tests/
```
