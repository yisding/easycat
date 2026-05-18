# Session Decomposition — Phase 5: TurnRunner

> **Part of the session decomposition.** Overview lives in
> `session-decomp-overview.md`. This file is the operational plan
> for Phase 5.
>
> **Predecessors:** Phases 0, 1, 2, 3, 4. All four collaborators
> must exist; TurnRunner takes refs to each.
> **Successors:** none. This is the final phase. After Phase 5
> Session lands at ~1,250 lines.
>
> **Risk:** high. Largest single phase (~620 lines moved).
> `_run_streaming_agent` (the biggest single method in the codebase
> at ~200 lines) is the load-bearing piece and touches ~25 Session
> attributes. Plan the extract carefully.
>
> **Compatibility policy:** internal-only changes. TurnRunner is a
> private collaborator (`session/_turn_runner.py`); not exported.
> Public Session methods `send_text`, `start_turn`, `end_turn`
> retain their signatures.

## Goal

Extract the per-turn agent loop into one collaborator. Today this
logic spans 6 methods on Session totalling ~620 lines, with
`_run_streaming_agent` alone reaching ~200 lines and touching ~25
distinct Session attributes. After Phase 5 the hub is one file with
explicit dependencies declared in its constructor.

This is the moment Session becomes a **thin coordinator** rather
than a hub: it holds collaborators and wires them together; the
turn loop lives elsewhere.

## Scope

**In scope:**

- Create `src/easycat/session/_turn_runner.py`
- Move 6 methods (`_on_turn_started`, `_schedule_turn_ended`,
  `_on_turn_ended`, `_handle_end_of_speech`, `_run_streaming_agent`,
  `_execute_text_turn`) into the new file
- Wire the TurnRunner into Session `__init__` with explicit
  dependencies on STTCommitter, TTSScheduler, AudioRouter,
  CancelOrchestrator, TurnManager, AgentStage, EventBus,
  JournalSink, TimeoutConfig
- Reduce Session's public `send_text`, `start_turn`, `end_turn`
  to one-line delegates
- Reduce `_reset_turn_state` to a coordinator that asks each
  collaborator to clear its per-turn state

**Out of scope:**

- Any change to `consume_agent_stream` (`session/_streaming.py`)
- Any change to `estimate_and_notify_interruption`
  (`session/interruption.py`)
- Any change to `AgentStage` or `Bridge` protocol
- Any change to action-drain logic (stays on Session)
- Any change to greeting flow (stays on Session)

## Tasks

### T5.0: Architecture freeze

The TurnRunner is the hub; it cannot pretend otherwise. Confirm the
constructor signature **before** writing any code:

```python
def __init__(
    self,
    *,
    # Collaborators
    stt_committer: STTCommitter,
    tts_scheduler: TTSScheduler,
    audio_router: AudioRouter,
    cancel_orchestrator: CancelOrchestrator,
    turn_manager: TurnManager,
    # Stage
    agent_stage: AgentStage,
    # Context
    run_ctx: RunContext,
    event_bus: EventBus,
    journal_sink: SessionJournalSink,
    runtime_scope: RuntimeScope,
    # Config
    timeout_config: TimeoutConfig,
    # Callbacks (read-only views into Session)
    current_turn: Callable[[], "TurnContext | None"],
    set_current_turn: Callable[["TurnContext | None"], None],
    turn_generation: Callable[[], int],
    bump_turn_generation: Callable[[], int],
    no_turn: "TurnContext",
    is_gated: Callable[[], bool],
    agent: Callable[[], Any],
    drain_session_actions: Callable[[], Awaitable[bool]],
    caller_id_system_message: Callable[[], str | None],
    stop: Callable[[], Awaitable[None]],
    reset_turn_state: Callable[[], None],
    emit: Callable[[Any], Awaitable[None]],
    session_id: str,
    journal_enabled: bool,
) -> None: ...
```

This is ~18 dependencies. That count is high but every entry maps to
a real responsibility the streaming agent loop has today. If the
team finds two or three that can fold (e.g. bundle the four
`turn_generation` / `set_current_turn` / `current_turn` / `no_turn`
callbacks into a single `TurnHandle` protocol), do so — but **do
not collapse for cosmetic reasons**. Each dep should be justified.

- [ ] Decide between explicit callbacks vs. a `TurnHandle` protocol
  for the four turn-pointer callbacks. **Recommendation:** introduce
  `TurnHandle` to keep the ctor under 15 params and make the
  Session↔TurnRunner contract visible.
- [ ] Confirm `_execute_text_turn` lives on the same runner. It
  shares enough state (turn context, agent, emit, journal) that
  splitting it into a separate `TextTurnRunner` is over-engineering.
- [ ] Confirm `_on_turn_started`, `_on_turn_ended`,
  `_schedule_turn_ended` are subscription handlers; their wiring
  in Session `__init__` redirects to runner methods.

### T5.1: Optional — introduce `TurnHandle` protocol

Reduces the callback fanout in the constructor.

- [ ] Add to `session/_turn_context.py`:

```python
class TurnHandle(Protocol):
    """The contract between Session and turn-running collaborators.

    Session is the single authority on the active turn pointer and
    turn generation. Collaborators read and write through this
    handle rather than holding Session refs.
    """

    @property
    def current(self) -> "TurnContext | None": ...

    @property
    def generation(self) -> int: ...

    @property
    def no_turn(self) -> "TurnContext": ...

    def set(self, turn: "TurnContext | None") -> None: ...

    def bump_generation(self) -> int: ...
```

- [ ] Session implements `TurnHandle` (or holds a small adapter)
  and passes itself to TurnRunner.

### T5.2: Create `_turn_runner.py` module

- [ ] Create `src/easycat/session/_turn_runner.py`.
- [ ] Define the class skeleton (using `TurnHandle` if T5.1 chosen):

```python
"""Owns the per-turn agent loop for a Session.

Responsibilities:

- React to ``TurnStarted`` / ``TurnEnded`` events.
- ``_handle_end_of_speech``: drain pending STT segments, fetch the
  final transcript, dispatch to the agent.
- ``_run_streaming_agent``: drive the agent stream through
  ``consume_agent_stream`` and synthesize TTS payloads sentence by
  sentence; track interruption; record the interruption
  notification at the end of the turn.
- ``_execute_text_turn``: same agent flow but with no audio
  pipeline.
- Coordinate with STTCommitter (drain pending segments),
  TTSScheduler (prepare and synthesize payloads), AudioRouter
  (drain outbound audio), CancelOrchestrator (signal propagation),
  and TurnManager (lifecycle state transitions).

TurnRunner is the hub. It depends on every other collaborator. The
constructor signature documents that explicitly — no surprises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from easycat.cancel import CancelToken
from easycat.events import (
    AgentFinal,
    AgentRequestStarted,
    Error,
    ErrorStage,
    EventBus,
    TurnEnded,
    TurnStarted,
)
from easycat.errors import AgentTimeoutError
from easycat.runtime.context import RunContext
from easycat.runtime.scope import RuntimeScope
from easycat.session._interruption import estimate_and_notify_interruption
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._streaming import consume_agent_stream
from easycat.session._tts_helpers import _text_for_estimation_timeline
from easycat.session._turn_context import TurnContext, TurnHandle
from easycat.stages.agent import AgentStage
from easycat.strip_markdown import strip_markdown
from easycat.timeouts import STTTimeoutError, TimeoutConfig, with_agent_timeout
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat.session._audio_router import AudioRouter
    from easycat.session._cancel_orchestrator import CancelOrchestrator
    from easycat.session._stt_committer import STTCommitter
    from easycat.session._tts_scheduler import TTSScheduler

logger = logging.getLogger(__name__)


class TurnRunner:
    """Drives the per-turn agent loop."""

    def __init__(
        self,
        *,
        stt_committer: "STTCommitter",
        tts_scheduler: "TTSScheduler",
        audio_router: "AudioRouter",
        cancel_orchestrator: "CancelOrchestrator",
        turn_manager: TurnManager,
        agent_stage: AgentStage,
        run_ctx: RunContext,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        runtime_scope: RuntimeScope,
        timeout_config: TimeoutConfig,
        turn_handle: TurnHandle,
        is_gated: Callable[[], bool],
        agent: Callable[[], Any],
        drain_session_actions: Callable[[], Awaitable[bool]],
        caller_id_system_message: Callable[[], str | None],
        stop: Callable[[], Awaitable[None]],
        reset_turn_state: Callable[[], None],
        emit: Callable[[Any], Awaitable[None]],
        session_id: str,
        journal_enabled: bool,
    ) -> None:
        self._stt = stt_committer
        self._tts = tts_scheduler
        self._audio = audio_router
        self._cancel = cancel_orchestrator
        self._turn_manager = turn_manager
        self._agent_stage = agent_stage
        self._run_ctx = run_ctx
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._runtime_scope = runtime_scope
        self._timeout_config = timeout_config
        self._turn = turn_handle
        self._is_gated = is_gated
        self._agent = agent
        self._drain_session_actions = drain_session_actions
        self._caller_id_system_message = caller_id_system_message
        self._stop = stop
        self._reset_turn_state = reset_turn_state
        self._emit = emit
        self._session_id = session_id
        self._journal_enabled = journal_enabled

        # Active text-turn tracking (was Session._active_text_turn etc.)
        self._active_text_turn: asyncio.Task[str] | None = None
        self._text_turn_cancel_token: CancelToken | None = None
        self._text_turn_accumulated: str = ""
        self._text_turn_lock = asyncio.Lock()
```

### T5.3: Port the 6 methods

Order of port (least to most invasive):

1. `_on_turn_started` (line 1765) — short, mostly event emission.
2. `_schedule_turn_ended` (line 1812) — schedules `_on_turn_ended`
   via `runtime_scope.create_journaled_task`.
3. `_on_turn_ended` (line 1836) — short cleanup.
4. `_handle_end_of_speech` (line 2123) — orchestrates
   STTCommitter → AgentStage → `_run_streaming_agent`.
5. `_execute_text_turn` (line 2688) — parallel path; shares emit
   patterns with `_run_streaming_agent`.
6. `_run_streaming_agent` (line 2193) — the 200-line hairball.

For each method, the substitutions are extensive. The mapping:

| Today (in Session) | After Phase 5 (in TurnRunner) |
|---|---|
| `self._turn` | `self._turn.current` |
| `self._no_turn` | `self._turn.no_turn` |
| `self._turn = X` | `self._turn.set(X)` |
| `self._turn_generation` | `self._turn.generation` |
| `self._turn_generation = ...` | handled inside `self._turn.set(...)` |
| `self._turn_manager` | `self._turn_manager` |
| `self._tts_synth` | `self._tts._synth` ← scheduler exposes it; **or** scheduler offers a `synthesize` API the runner calls directly without touching `_synth` |
| `self._tts_playback_suppressed` | `self._tts.is_playback_suppressed` |
| `self._is_gated` | `self._is_gated()` |
| `self._stt_active` | `self._stt.is_active` |
| `self._stt_segment_commit_task` | hidden behind `self._stt.await_inflight_commit()` |
| `self._stt_pending_segment_futures` | `turn.pending_stt_segment_futures` |
| `self._stt_final_future` | `turn.stt_final_future` |
| `self._timeout_config` | `self._timeout_config` |
| `self._interruption_mode` | `self._cancel.interruption_mode` |
| `self._interruption_latency_compensation_ms` | `self._cancel.latency_compensation_ms` |
| `self._interruption_ack_stale_ms` | `self._cancel.ack_stale_ms` |
| `self._interruption_ack_tail_cap_ms` | `self._cancel.ack_tail_cap_ms` |
| `self._strip_markdown` | `self._tts._strip_markdown` ← scheduler exposes `should_strip_markdown` property |
| `self._output_processors` | not needed in runner (used inside `_tts.prepare`) |
| `self._auto_turn_speech_frames = 0` | `self._audio.reset_speech_detection()` |
| `self._caller_id_system_message()` | `self._caller_id_system_message()` |
| `self._drain_session_actions()` | `self._drain_session_actions()` |
| `await self._wait_outbound_drain()` | `await self._audio.await_drain()` |
| `await self._cancel_tts()` | `await self._tts.cancel()` |
| `self._reset_turn_state()` | `self._reset_turn_state()` |
| `await self._emit(evt)` | `await self._emit(evt)` |
| `await self.stop()` | `await self._stop()` |
| `self.agent` | `self._agent()` |
| `self.event_bus` | `self._event_bus` |
| `self._prepare_tts_payload(...)` | `self._tts.prepare(...)` |
| `self._record_markdown_strip(...)` | `self._tts.record_markdown_strip(...)` (scheduler exposes) |
| `self._record_interruption_notification(...)` | `self._cancel.record_interruption(...)` |
| `self._current_tts_task = ...` | scheduler tracks its own; remove these assignments |

The mapping is mechanical but voluminous. Plan to spend ~half the
phase budget on `_run_streaming_agent` alone.

### T5.4: Wire TurnRunner into Session `__init__`

In `_session.py:__init__`, after all four collaborators are
constructed:

```python
self._turn_runner = TurnRunner(
    stt_committer=self._stt_committer,
    tts_scheduler=self._tts_scheduler,
    audio_router=self._audio_router,
    cancel_orchestrator=self._cancel,
    turn_manager=self._turn_manager,
    agent_stage=self._agent_stage,
    run_ctx=self._run_ctx,
    event_bus=self.event_bus,
    journal_sink=self._journal_sink,
    runtime_scope=self._runtime_scope,
    timeout_config=self._timeout_config,
    turn_handle=self,  # Session implements TurnHandle
    is_gated=lambda: self._is_gated,
    agent=lambda: self.agent,
    drain_session_actions=self._drain_session_actions,
    caller_id_system_message=self._caller_id_system_message,
    stop=self.stop,
    reset_turn_state=self._reset_turn_state,
    emit=self._emit,
    session_id=self.session_id,
    journal_enabled=self._journal is not None,
)
```

Update event-bus subscriptions (lines 271–272):

```python
self.event_bus.subscribe(TurnStarted, self._turn_runner.on_turn_started)
self.event_bus.subscribe(TurnEnded, self._turn_runner.schedule_turn_ended)
```

### T5.5: Reduce Session public facades

- [ ] `send_text` (line 2637) becomes:
  ```python
  async def send_text(self, text: str) -> str:
      return await self._turn_runner.send_text(text)
  ```
  (Runner exposes `send_text` as the public entry into `_execute_text_turn`.)

- [ ] `start_turn` (line 1654) and `end_turn` (line 1658) become:
  ```python
  async def start_turn(self) -> None:
      await self._turn_manager.start_turn()

  async def end_turn(self) -> None:
      await self._turn_manager.bot_stopped_speaking()
  ```
  (These were already thin; Phase 5 doesn't change them. Keep them on Session
  for the public surface.)

- [ ] `_reset_turn_state` (line 757) reduces to:
  ```python
  def _reset_turn_state(self) -> None:
      turn = self._turn
      self._stt_committer.cancel_scheduled(turn=turn)
      self._stt_committer.cancel_inflight()
      self._stt_committer.resolve_pending(turn, "")
      if turn is not None and turn.stt_final_future is not None and not turn.stt_final_future.done():
          turn.stt_final_future.set_result("")
      if turn is not None:
          turn.stt_final_future = None
      self._turn = None
      self._turn_manager.reset()
  ```
  Exact ordering verified line-by-line against today's
  implementation (lines 757–770).

### T5.6: Delete migrated code from Session

After all callers updated, delete from `_session.py`:

- [ ] `_on_turn_started` (lines 1765–1803)
- [ ] `_schedule_turn_ended` (lines 1812–1835)
- [ ] `_on_turn_ended` (lines 1836–1847)
- [ ] `_handle_end_of_speech` (lines 2123–2190)
- [ ] `_run_streaming_agent` (lines 2193–2401)
- [ ] `_execute_text_turn` (lines 2688–2785)

Total: ~620 lines.

Also delete the now-unused Session attrs (moved to TurnRunner):

- [ ] `self._active_text_turn` (line 387)
- [ ] `self._text_turn_cancel_token` (line 388)
- [ ] `self._text_turn_accumulated` (line 389)
- [ ] `self._text_turn_lock` (line 390)

### T5.7: Tests

The streaming agent loop is the most-tested area of the codebase.
Every existing test in `tests/session/` that exercises a turn flow
must pass unchanged. This is the highest acceptance bar in the
decomposition.

- [ ] Add `tests/session/test_turn_runner.py`. Cover:
  - `on_turn_started` records the turn-start time.
  - `handle_end_of_speech` with empty transcript skips the agent
    dispatch and resets the turn.
  - `handle_end_of_speech` with non-empty transcript invokes
    `_run_streaming_agent`.
  - `run_streaming_agent` drains pending STT, awaits the agent
    stream, synthesizes each sentence, records the interruption
    notification at end.
  - Barge-in mid-stream cancels both agent task and TTS task.
  - Agent timeout emits `AgentTimeoutError` and resets the turn.
  - Action drain returning `True` triggers `stop()` at the end.
  - Text-turn path: `send_text("hello")` runs the agent without
    audio I/O and returns the response string.
- [ ] **Full barge-in matrix.** WS2B's three-cancellation-mode
  matrix (`tests/session/test_interruption_*.py`) is the
  acceptance gate. All three modes
  (`shallow_interruption`, `mid_stream`, `end_of_turn`) must pass
  unchanged.
- [ ] **Perf regression gate.** Run the turn-latency micro-benchmark
  before and after. P50 ≤ 1.0s, P90 ≤ 1.6s, no regression beyond
  noise band.
- [ ] **Replay parity.** A bundle captured pre-Phase-5 replays
  cleanly post-Phase-5.

## Acceptance criteria

- [ ] `uv run pytest` passes unchanged.
- [ ] `uv run pytest tests/session/test_interruption*.py` passes
  unchanged.
- [ ] `uv run pytest tests/session/test_streaming*.py` passes
  unchanged.
- [ ] `uv run ruff check .` passes.
- [ ] `wc -l src/easycat/session/_session.py` drops by ~620,
  landing at ~1,250.
- [ ] `wc -l src/easycat/session/_turn_runner.py` lands at ~700.
- [ ] `grep -c '^    def\|^    async def' src/easycat/session/_session.py`
  drops by 6 (the six moved methods).
- [ ] Public methods `send_text`, `start_turn`, `end_turn`,
  `cancel_turn`, `cancel_tts_playback`, `reset_state`, `agent`
  retain their exact signatures.
- [ ] Journal record names unchanged.
- [ ] Bundle round-trip parity.
- [ ] Perf gate passes.

## Risks and rollback

**Risk 1 — `_run_streaming_agent` correctness.** This is the
load-bearing piece of the codebase. The 200-line body has
non-obvious ordering invariants:

- The agent consumer task is created before the TTS consumer task (faithfully preserved from the pre-decomposition Session implementation).
- `tts_playback_started` is set only on the first non-suppressed,
  non-cancelled payload.
- `tts_should_stop` is read after the action drain.
- `_drain_session_actions` runs *before* `bot_stopped_speaking`.
- Outbound drain runs *after* `bot_stopped_speaking` to flush the
  tail.
- The turn-generation check (`self._turn is turn and ... == turn_gen`)
  guards against a newer turn clobbering state.

Mitigation: **port the method last, in one commit, with no
behaviour changes**. Side-by-side diff the old and new versions for
review. Add a regression test for each invariant above before the
port.

**Risk 2 — Callback overhead.** TurnRunner reads
`self._turn.current` per turn step where Session used `self._turn`
directly. That's a property dereference vs. an attribute load. On a
hot path of ~5 reads per turn, the overhead is negligible — but
verify with the perf gate.

**Risk 3 — `text_turn_lock` ownership.** Today the lock prevents
concurrent `send_text` calls (line 390). The lock moves into
TurnRunner. Verify that `send_text` is not called concurrently from
elsewhere (e.g. action executor).

**Risk 4 — Subscription order.** Today
`self.event_bus.subscribe(TurnStarted, self._on_turn_started)` is at
line 271, **before** stages are constructed (line 413). After Phase 5,
`self._turn_runner` doesn't exist yet at line 271. Either:

- **Option A:** Move the subscription block after the runner is
  constructed.
- **Option B:** Use the journal sink's subscription pattern (lazy
  subscribe via a `subscribe()` method on the runner).

Pick Option A. Document in the runner's docstring that subscriptions
happen after construction.

**Risk 5 — Telephony interaction.** The streaming agent
runs `_caller_id_system_message()` to prefix the agent input with
caller-ID metadata. That callback stays on Session (telephony state
is Session-level). Verify the callback returns the same value after
the move.

**Risk 6 — `_journaled_task` for `_schedule_turn_ended`.** After
Phase 0 this is `runtime_scope.create_journaled_task(...)`. The
runner uses the same pattern. Verify the task name `on_turn_ended`
stays unchanged so journal records are stable.

**Risk 7 — Late discovery of attr access.** Despite Phase 0's
audit, an attr access in `_run_streaming_agent` may have been
missed. Mitigation: run the test suite continuously while porting;
each missed attr surfaces as `AttributeError`.

**Rollback.** Phase 5 is a single large PR. Revert reverts cleanly;
Phases 0–4 remain. If the PR is too large to review, split into
two: T5.3 steps 1–4 (turn-lifecycle handlers) and T5.3 steps 5–6
(streaming agent + text turn). Each is independently revertible.

## Verification commands

```bash
uv run pytest
uv run pytest tests/session/test_interruption_shallow.py
uv run pytest tests/session/test_interruption_mid_stream.py
uv run pytest tests/session/test_interruption_end_of_turn.py
uv run pytest tests/session/test_streaming.py
uv run pytest tests/runtime/test_replay.py
uv run ruff check .
wc -l src/easycat/session/_session.py src/easycat/session/_turn_runner.py
grep -c '^    def\|^    async def' src/easycat/session/_session.py
uv run python perf/turn_latency_bench.py
```

## Done state

After Phase 5 merges, `src/easycat/session/_session.py` is
~1,250 lines and owns lifecycle, the public event surface,
telephony state, action drain, the agent property, introspection
properties, and coordination glue. Each of the four collaborators
plus TurnRunner is independently testable, journaled, and
replaceable. The decomposition is complete.
