# Session Decomposition — Phase 3: TTSScheduler

> **Part of the session decomposition.** Overview lives in
> `session-decomp-overview.md`. This file is the operational plan
> for Phase 3.
>
> **Predecessors:** Phase 0. Phase 2 (AudioRouter) is a prerequisite
> if the TTSScheduler is to enqueue outbound chunks through
> `audio_router.queue_outbound(chunk)`. If Phase 2 has not landed,
> TTSScheduler temporarily takes the outbound queue directly (same
> queue Session uses today) and Phase 2's wiring update flips it to
> the router.
> **Successors:** Phase 5 (TurnRunner) takes a reference to the
> TTSScheduler.
>
> **Risk:** low. Smaller surface than STTCommitter or AudioRouter.
> The main win here is staging the codebase for sentence-level TTS
> pipelining (planted as a hook, not implemented).
>
> **Compatibility policy:** internal-only changes. TTSScheduler is a
> private collaborator (`session/_tts_scheduler.py`); not exported.

## Goal

Extract TTS payload preparation, synthesis, and the single-shot
bypass path into one collaborator. Today this logic spans 5 methods
plus `_cancel_tts` and ~150 lines on Session. After Phase 3, Session
holds one `self._tts_scheduler: TTSScheduler` field and delegates.

The second goal: plant the hook for **sentence-level TTS
pipelining** (synthesizing sentence N+1 before sentence N finishes
playing). The implementation is deferred to a separate workstream;
Phase 3 only structures the API so the change is local.

## Scope

**In scope:**

- Create `src/easycat/session/_tts_scheduler.py`
- Move `_prepare_tts_payload`, `_synthesize_tts`, `synthesize_bypass`,
  `_record_markdown_strip`, `_record_tts_payload_prepared`,
  `_cancel_tts` into the new file
- Migrate `_current_tts_task` and `_tts_playback_suppressed` to
  live on the scheduler
- Take ownership of `_tts_synth` (the `TTSSynthesizer` instance)
- Wire the TTSScheduler into Session `__init__`
- Make `synthesize_bypass` a thin delegate on Session
- Plant the `synthesize_sentences(stream)` method as a stub (raises
  `NotImplementedError`) for sentence-level pipelining

**Out of scope:**

- Any change to TTS provider implementations (`src/easycat/tts/*`)
- Any change to `TTSProvider` protocol
- Implementing sentence-level pipelining (separate workstream)
- Moving `_run_streaming_agent` (Phase 5)

## Tasks

### T3.0: Architecture freeze

- [ ] Confirm `_tts_synth` ownership transfers fully to TTSScheduler.
  Today `_tts_synth.bind_stage(...)` is called in Session `__init__`
  with lambdas capturing `self._run_ctx` and
  `self._turn or self._no_turn`. After Phase 3, the scheduler owns
  the synthesizer and the bind call.
- [ ] Confirm sentence-pipelining hook signature. The proposed API:

  ```python
  async def synthesize_sentences(
      self,
      payloads: AsyncIterator[TTSInput],
      cancel_token: CancelToken | None,
      turn: TurnContext,
  ) -> SynthesisResult:
      """Synthesize a stream of payloads with lookahead pipelining.

      Not yet implemented — current behaviour is one-at-a-time
      synthesis via ``synthesize``. The hook exists so the pipelining
      change is a local one when it lands.
      """
      raise NotImplementedError
  ```

  The TurnRunner consumer (today's `_run_streaming_agent`) keeps its
  current sequential `await self._tts_synth.synthesize(payload, …)`
  loop in Phase 5; switching to `synthesize_sentences` happens later.

### T3.1: Create `_tts_scheduler.py` module

- [ ] Create `src/easycat/session/_tts_scheduler.py`.
- [ ] Define the class skeleton:

```python
"""Owns TTS payload preparation and synthesis for a Session.

Responsibilities:

- Apply output processors (markdown stripping, phonetic
  replacement, pauses) to raw agent text and produce a
  ``TTSInput`` payload.
- Drive the underlying ``TTSSynthesizer`` to produce audio chunks
  and feed them to ``AudioRouter.queue_outbound``.
- Provide the single-shot ``synthesize_bypass`` path used by
  greeting / opt-out announcements.
- Track the in-flight synthesis task so cancellation can target it.
- Reserve the future ``synthesize_sentences`` hook for
  sentence-level pipelining (see workstream-tts-pipelining when it
  lands).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from easycat.cancel import CancelToken
from easycat.events import EventBus
from easycat.llm_output_processing import (
    LLMOutputProcessor,
    apply_output_processors,
)
from easycat.providers import TTSProvider
from easycat.runtime.context import RunContext
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._tts_helpers import _text_for_estimation_timeline
from easycat.stages.tts import TTSStage
from easycat.strip_markdown import strip_markdown, strip_ssml_tags
from easycat.tts.input import TTSInput
from easycat.tts_synthesizer import TTSSynthesizer
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat.session._audio_router import AudioRouter
    from easycat.session._turn_context import TurnContext

logger = logging.getLogger(__name__)


class TTSScheduler:
    """Prepares and synthesizes TTS payloads for a Session."""

    def __init__(
        self,
        *,
        tts: TTSProvider,
        tts_stage: TTSStage,
        turn_manager: TurnManager,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        run_ctx: RunContext,
        no_turn: "TurnContext",
        audio_router: "AudioRouter",
        # Config
        output_processors: list[LLMOutputProcessor],
        strip_markdown_enabled: bool,
        # Callbacks
        current_turn: Callable[[], "TurnContext | None"],
        is_gated: Callable[[], bool],
    ) -> None:
        self._tts = tts
        self._tts_stage = tts_stage
        self._turn_manager = turn_manager
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._run_ctx = run_ctx
        self._no_turn = no_turn
        self._audio_router = audio_router

        self._output_processors = output_processors
        self._strip_markdown = strip_markdown_enabled

        self._current_turn = current_turn
        self._is_gated = is_gated

        self._synth = TTSSynthesizer(
            tts=tts,
            event_bus=event_bus,
            journal_sink=journal_sink,
            # TTSSynthesizer's existing constructor signature; verify
            # actual fields when implementing.
        )
        self._synth.bind_stage(
            tts_stage,
            run_ctx_getter=lambda: self._run_ctx,
            turn_getter=lambda: self._current_turn() or self._no_turn,
        )

        self._current_tts_task: asyncio.Task[None] | None = None
        self._playback_suppressed: bool = False
```

### T3.2: Public API

| Public method | Source (Session method) | Line |
|---|---|---:|
| `prepare(text, *, is_streaming, is_final)` | `_prepare_tts_payload` | 2402 |
| `synthesize(payload, token, *, is_active=)` | `_synthesize_tts` | 2424 |
| `synthesize_bypass(text)` | `synthesize_bypass` | 1174 |
| `cancel()` | `_cancel_tts` | 2808 |
| `synthesize_sentences(payloads, ...)` | new stub | — |
| `set_playback_suppressed(value)` | new | — |
| `is_playback_suppressed` (property) | new | — |
| `current_task` (property) | new (returns `_current_tts_task`) | — |

Internal helpers (private):

- `_record_markdown_strip(...)` (was Session, 684)
- `_record_tts_payload_prepared(...)` (was Session, 704)

### T3.3: Port the 5 methods

For each method:

- [ ] Copy body from `_session.py`.
- [ ] Replace `self.tts` → `self._tts`.
- [ ] Replace `self._tts_synth` → `self._synth`.
- [ ] Replace `self._output_processors` → `self._output_processors`.
- [ ] Replace `self._strip_markdown` → `self._strip_markdown`.
- [ ] Replace `self._tts_playback_suppressed` → `self._playback_suppressed`.
- [ ] Replace `self._current_tts_task` → `self._current_tts_task`.
- [ ] Replace `self._turn_manager` → `self._turn_manager`.
- [ ] Replace `self._is_gated` (Session property) → `self._is_gated()` (callback).
- [ ] Replace `self._turn` reads → `self._current_turn()`.
- [ ] Replace `self._record_markdown_strip(...)` → call own internal helper.
- [ ] Replace `self._record_tts_payload_prepared(...)` → call own internal helper.
- [ ] Replace `self._journal_sink` → `self._journal_sink`.

`synthesize_bypass` (today writes to `self._outbound_queue`) becomes:

- [ ] Read the outbound chunk producer pattern from
  `_synthesize_tts` and replace direct queue writes with
  `await self._audio_router.queue_outbound(chunk)`.

### T3.4: Wire TTSScheduler into Session `__init__`

In `_session.py:__init__`, after AudioRouter is constructed:

```python
self._tts_scheduler = TTSScheduler(
    tts=self.tts,
    tts_stage=self._tts_stage,
    turn_manager=self._turn_manager,
    event_bus=self.event_bus,
    journal_sink=self._journal_sink,
    run_ctx=self._run_ctx,
    no_turn=self._no_turn,
    audio_router=self._audio_router,
    output_processors=list(cfg.output_processors),
    strip_markdown_enabled=cfg.strip_markdown,
    current_turn=lambda: self._turn,
    is_gated=lambda: self._is_gated,
)
```

Delete from Session `__init__`:

- [ ] `self._tts_synth = TTSSynthesizer(...)` (line 326)
- [ ] `self._tts_synth.bind_stage(...)` (line 440)
- [ ] `self._output_processors` (line 259) — moves to scheduler
- [ ] `self._strip_markdown` (line 258) — moves to scheduler
- [ ] `self._current_tts_task` (line 359)
- [ ] `self._tts_playback_suppressed` (line 368)

Keep on Session: agent surface, lifecycle. The `_is_gated`
property stays on Session because it composes transport state and
session-level flags.

### T3.5: Update Session call sites

Replace direct calls in Session with scheduler calls:

- [ ] `synthesize_bypass` public method (line 1174) becomes:
  ```python
  async def synthesize_bypass(self, text: str) -> None:
      await self._tts_scheduler.synthesize_bypass(text)
  ```
- [ ] `_run_streaming_agent` (line 2193 — stays on Session until
  Phase 5):
  - `self._prepare_tts_payload(...)` → `self._tts_scheduler.prepare(...)`
  - `self._tts_synth.synthesize(...)` → `self._tts_scheduler.synthesize(...)`
  - `self._tts_playback_suppressed` reads →
    `self._tts_scheduler.is_playback_suppressed`
  - `self._current_tts_task = ...` references → managed inside
    `TTSScheduler.synthesize` (scheduler tracks its own task; callers
    don't assign to it)
  - `await self._cancel_tts()` → `await self._tts_scheduler.cancel()`
  - `self._record_markdown_strip(...)` direct calls inside
    `_run_streaming_agent` → either keep on Session (it's a journal
    helper) or call `self._tts_scheduler._record_markdown_strip(...)`.
    **Decision:** the markdown-strip record describes TTS payload
    preparation; move both record helpers into TTSScheduler and
    have Session callers use scheduler methods.
- [ ] `_execute_text_turn` (line 2688 — stays on Session until
  Phase 5): same call-site updates as above.

### T3.6: Cancel-path correctness

`TTSScheduler.cancel()` must preserve today's `_cancel_tts`
behaviour (line 2808):

1. `await self._synth.cancel()`
2. If `self._current_tts_task` is not the current task and is not
   done, cancel it; await with swallowed exceptions.

Add a regression test (see T3.8).

### T3.7: Delete migrated code from Session

After all callers updated, delete from `_session.py`:

- [ ] `_prepare_tts_payload` (lines 2402–2423)
- [ ] `_synthesize_tts` (lines 2424–2482)
- [ ] `synthesize_bypass` (lines 1174–1183) — body, keep one-line delegate
- [ ] `_record_markdown_strip` (lines 684–703)
- [ ] `_record_tts_payload_prepared` (lines 704–734)
- [ ] `_cancel_tts` (lines 2808–2820)

Total: ~190 lines.

### T3.8: Tests

- [ ] Add `tests/session/test_tts_scheduler.py`. Cover:
  - `prepare` applies output processors in order.
  - `prepare` writes `tts_payload_prepared` journal record.
  - `prepare` strips SSML when provider doesn't support it.
  - `synthesize` calls `audio_router.queue_outbound` for each chunk
    (mock router).
  - `synthesize` returns a result with `audio_bytes` and `completed`
    fields preserved from today's shape.
  - `synthesize_bypass` runs end-to-end and emits chunks.
  - `cancel()` cancels `_synth` and any in-flight task.
  - `set_playback_suppressed(True)` causes subsequent `synthesize`
    calls to short-circuit with zero audio.
  - `synthesize_sentences` raises `NotImplementedError` for now.
- [ ] Existing TTS tests (`tests/tts/`) pass unchanged.
- [ ] Bundle replay parity.

## Acceptance criteria

- [ ] `uv run pytest` passes unchanged.
- [ ] `uv run ruff check .` passes.
- [ ] `wc -l src/easycat/session/_session.py` drops by ~190.
- [ ] `wc -l src/easycat/session/_tts_scheduler.py` lands at ~250.
- [ ] `grep -c '^    def\|^    async def' src/easycat/session/_session.py`
  drops by 6 (5 moved + `_cancel_tts`).
- [ ] Journal record names unchanged (`markdown_stripped`,
  `tts_payload_prepared`).
- [ ] Bundle round-trip parity.
- [ ] `synthesize_sentences` exists as a stub that raises
  `NotImplementedError`.

## Risks and rollback

**Risk 1 — TTSSynthesizer construction order.** Today the synthesizer
is constructed early in Session `__init__` (line 326) and bound to
the stage later (line 440). After Phase 3, the scheduler does both
in its `__init__`. Verify no other code path in Session expects
`self._tts_synth` to exist between those two points (it shouldn't —
the early construction is just bookkeeping).

**Risk 2 — `_is_gated` lambda capture.** The scheduler reads
`is_gated()` via callback. Verify the callback always reflects the
live state, not a stale snapshot. Closure over `self._is_gated`
property should be fine.

**Risk 3 — `current_tts_task` external observation.** Verify no
test or external caller reads `session._current_tts_task` directly.
If found, expose `session._tts_scheduler.current_task` or update
the test.

**Risk 4 — Phase 2 not landed.** If Phase 3 ships before Phase 2,
TTSScheduler takes the outbound queue directly (
`outbound_queue: BoundedAudioQueue`) instead of an
`AudioRouter`. Phase 2's PR then flips the dependency. Document
this conditional path in the scheduler module docstring.

**Rollback.** Phase 3 is a single PR. Independent of Phases 1, 2, 4.

## Verification commands

```bash
uv run pytest tests/session/ tests/tts/
uv run pytest tests/runtime/test_replay.py
uv run ruff check src/easycat/session/
wc -l src/easycat/session/_session.py src/easycat/session/_tts_scheduler.py
grep -c '^    def\|^    async def' src/easycat/session/_session.py
grep -rn '_tts_synth\|_tts_playback_suppressed\|_current_tts_task' src/ tests/
```
