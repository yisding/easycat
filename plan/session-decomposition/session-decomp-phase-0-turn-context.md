# Session Decomposition — Phase 0: TurnContext, RuntimeScope, attribute ownership

> **Historical implementation checklist.** This phase has landed in the
> current codebase; keep this file as rationale and review context. Line
> numbers and unchecked task boxes are from the original extraction plan.
>
> **Part of the session decomposition.** Overview, rationale, and
> phase sequencing live in `session-decomp-overview.md`. This file is
> the operational plan for Phase 0.
>
> **Predecessors:** none.
> **Successors:** Phases 1–5 all depend on Phase 0.
>
> **Compatibility policy:** internal-only changes. No public API
> change. `TurnContext` is not exported from `easycat/__init__.py`
> today and remains unexported.

## Goal

Establish the contract that makes Phases 1–5 honest: per-turn state
lives on `TurnContext`, session-long state ownership is documented,
and journaled task creation moves to the shared `RuntimeScope`. After
Phase 0, every method slated to move in Phases 1–5 takes its
`TurnContext` explicitly rather than reaching into `Session._turn`.

## Scope

**In scope:**

- Extend `TurnContext` with two per-turn fields currently on Session
- Migrate all reads of those fields in `_session.py`
- Convert 27 methods slated to move to accept `turn: TurnContext`
  as an explicit parameter rather than reaching `self._turn`
- Promote `_journaled_task` from a Session method to
  `RuntimeScope.create_journaled_task(...)`
- Document session-long attribute ownership for Phases 1–5

**Out of scope:**

- Any code moves between files (Phases 1–5)
- Any signature change to public Session methods
- Any change to event-bus subscription wiring

## Tasks

### T0.0: Architecture freeze

- [ ] Confirm the two TurnContext field additions
  (`stt_final_future`, `pending_stt_segment_futures`) are the only
  per-turn fields that need to move. All other Session attrs are
  session-long and remain on Session until claimed by their
  collaborator in Phases 1–5.
- [ ] Confirm `RuntimeScope.create_journaled_task` is the right
  home for `_journaled_task` (vs leaving it as a Session method).
  Rationale: collaborators in Phases 1, 2, 5 will need it; promoting
  it to the shared scope avoids each collaborator taking both
  `RuntimeScope` and `Session.journaled_task` separately.

### T0.1: Extend TurnContext

Target file: `src/easycat/session/_turn_context.py`.

- [ ] Add to `__slots__`:
  ```python
  "stt_final_future",
  "pending_stt_segment_futures",
  ```
- [ ] Initialise in `__init__`:
  ```python
  self.stt_final_future: asyncio.Future[str] | None = None
  self.pending_stt_segment_futures: list[asyncio.Future[str]] = []
  ```
- [ ] Add to top-of-file `from __future__ import annotations` block
  if needed for `asyncio.Future` typing.
- [ ] Update the docstring to reflect the two new fields.

### T0.2: Migrate Session reads to TurnContext

Drop the two now-redundant Session fields:

- `self._stt_final_future: asyncio.Future[str] | None` (line 361)
- `self._stt_pending_segment_futures: list[asyncio.Future[str]]` (line 362)

Update every read/write site to use `turn.stt_final_future` /
`turn.pending_stt_segment_futures` instead. Each site below is
inside a method that already has access to a `TurnContext` (either
via `self._turn` or via T0.3's explicit parameter migration).

**`_stt_final_future` reads/writes** (10 sites identified in
`grep -n 'stt_final_future' src/easycat/session/_session.py`):

- [ ] `_reset_turn_state` (763): clear via `turn.stt_final_future = None`
- [ ] `_handle_end_of_speech` (2157, 2162, 2165, 2175): read,
  await, set, clear via `turn.stt_final_future`
- [ ] `_handle_end_of_speech` (2178): set result via
  `turn.stt_final_future.set_result(transcript)`
- [ ] `_cancel_stt` (2804–2806): clear via
  `turn.stt_final_future = None` (use `self._turn or self._no_turn`
  at call sites where applicable)

**`_stt_pending_segment_futures` reads/writes** (5 sites):

- [ ] `_commit_stt_segment` (1949): append to
  `turn.pending_stt_segment_futures`
- [ ] `_commit_stt_segment` (1969–1970): remove from
  `turn.pending_stt_segment_futures`
- [ ] `_await_pending_stt_segments` (1977, 1994, 1997): read /
  pop via `turn.pending_stt_segment_futures`
- [ ] `_handle_end_of_speech` (2146): append future via
  `turn.pending_stt_segment_futures.append(future)`
- [ ] `_resolve_pending_stt_segment_futures` (1869): iterate /
  set result via `turn.pending_stt_segment_futures`

**Edge case — the `_no_turn` sentinel.** When STT events arrive
between turns, the writer must not crash. Two acceptable patterns:

- **Pattern A (recommended):** writers check
  `turn is None or turn is self._no_turn` and skip. The pending
  list on `_no_turn` is always empty.
- **Pattern B:** allow writes on `_no_turn`; the next
  `_reset_turn_state()` discards them.

Pick Pattern A so debugging is easier (no phantom pending
futures on the no-turn sentinel).

### T0.3: Lift `turn` through method signatures

For each method slated to move in Phases 1–5, convert
`turn = self._turn` (or implicit `self._turn` reads) to an explicit
`turn: TurnContext` parameter. Use `self._no_turn` sentinel at call
sites when no turn is active.

Methods to update (line refs in `_session.py`):

| # | Method | Line | Moves to |
|---|---|---:|---|
| 1 | `_cancel_for_barge_in` | 1664 | CancelOrchestrator (P4) |
| 2 | `_on_stt_final_opt_out` | 1727 | stays (telephony) |
| 3 | `_on_turn_started` | 1765 | TurnRunner (P5) |
| 4 | `_schedule_turn_ended` | 1812 | TurnRunner (P5) |
| 5 | `_on_turn_ended` | 1836 | TurnRunner (P5) |
| 6 | `_cancel_scheduled_stt_segment_commit` | 1857 | STTCommitter (P1) |
| 7 | `_cancel_inflight_stt_segment_commit` | 1863 | STTCommitter (P1) |
| 8 | `_resolve_pending_stt_segment_futures` | 1869 | STTCommitter (P1) |
| 9 | `_schedule_stt_segment_commit` | 1875 | STTCommitter (P1) |
| 10 | `_commit_stt_segment_after` | 1890 | STTCommitter (P1) |
| 11 | `_start_stt_segment_commit` | 1897 | STTCommitter (P1) |
| 12 | `_commit_stt_segment` | 1918 | STTCommitter (P1) |
| 13 | `_await_pending_stt_segments` | 1975 | STTCommitter (P1) |
| 14 | `_start_stt_event_task` | 2000 | STTCommitter (P1) |
| 15 | `_run_pipeline` | 2052 | AudioRouter (P2) |
| 16 | `_handle_end_of_speech` | 2123 | TurnRunner (P5) |
| 17 | `_run_streaming_agent` | 2193 | TurnRunner (P5) |
| 18 | `_prepare_tts_payload` | 2402 | TTSScheduler (P3) |
| 19 | `_synthesize_tts` | 2424 | TTSScheduler (P3) |
| 20 | `_wait_outbound_drain` | 2483 | AudioRouter (P2) |
| 21 | `_drain_outbound_audio` | 2500 | AudioRouter (P2) |
| 22 | `_stamp_outbound_chunk` | 2541 | AudioRouter (P2) |
| 23 | `_handle_audio_delivery` | 2549 | AudioRouter (P2) |
| 24 | `_send_playback_mark` | 2576 | AudioRouter (P2) |
| 25 | `_on_playback_mark_ack` | 2595 | AudioRouter (P2) |
| 26 | `_on_transport_audio_delivered` | 2607 | AudioRouter (P2) |
| 27 | `_execute_text_turn` | 2688 | TurnRunner (P5) |
| 28 | `_cancel_stt` | 2786 | STTCommitter (P1) |
| 29 | `_cancel_tts` | 2808 | TTSScheduler (P3) |

For each:

- [ ] Change `turn = self._turn` to a parameter `turn: TurnContext | None`
  (or `turn: TurnContext` where the caller already guarantees non-None).
- [ ] Update all callers in `_session.py` to pass `self._turn` or
  `self._turn or self._no_turn` as appropriate.
- [ ] Keep the method name and module location for now — the
  movement happens in Phases 1–5. This phase only changes the
  signature.

**Sanity check after T0.3:** `grep -c 'self\._turn\b' src/easycat/session/_session.py`
should drop by ~30–40 references. The remaining `self._turn`
references should be lifecycle / assignment sites
(`self._turn = TurnContext(...)`, `self._turn = None`,
`self._turn is turn`) and stay on Session.

### T0.4: Promote `_journaled_task` to RuntimeScope

Move the body of `Session._journaled_task` (`_session.py:571-632`)
into `RuntimeScope` as a new method.

Target file: `src/easycat/runtime/scope.py`.

- [ ] Add to `RuntimeScope`:
  ```python
  def create_journaled_task(
      self,
      coro: Coroutine[Any, Any, _T],
      *,
      name: str,
      journal_sink: SessionJournalSink,
      turn_id: str | None = None,
  ) -> asyncio.Task[_T]:
      """Create a tracked task that journals scheduled/completed/cancelled/raised."""
  ```
- [ ] Reuse `RuntimeScope.add_task` internally so the task is both
  journaled and tracked under `name`.
- [ ] Move the four record writes (`task_scheduled`,
  `task_completed`, `task_cancelled`, `task_raised`) from
  `Session._journaled_task` into the new RuntimeScope method.
  Same record names, same data shape.
- [ ] Import `SessionJournalSink` lazily or via a `TYPE_CHECKING`
  block to avoid circular imports
  (`runtime/scope.py` → `session/_journal_sink.py` →
  `runtime/journal.py`).

Update Session call sites (5 sites):

- [ ] `_session.py:1699` (`_deliver_call_answered_greeting`)
- [ ] `_session.py:1763` (`_on_stt_final_opt_out`)
- [ ] `_session.py:1829` (`_schedule_turn_ended`)
- [ ] `_session.py:1883` (`_schedule_stt_segment_commit`)
- [ ] `_session.py:1910` (`_start_stt_segment_commit`)

Each call site changes from:

```python
self._journaled_task(coro, name=..., turn_id=...)
```

to:

```python
self._runtime_scope.create_journaled_task(
    coro,
    name=...,
    journal_sink=self._journal_sink,
    turn_id=...,
)
```

- [ ] Delete `Session._journaled_task` (lines 571–632).

### T0.5: Document session-long attribute ownership

Add a one-page reference table to this file (or to
`session-decomp-overview.md` if the team prefers) listing every
Session instance attribute that will migrate to a collaborator in
Phases 1–5, including which phase claims it.

This is a documentation task. No code changes. The table below is
the authoritative reference Phases 1–5 will follow.

| Session attribute | Phase | Collaborator | Note |
|---|---|---|---|
| `_stt_final_future` | 0 | TurnContext | per-turn — already moved in T0.1 |
| `_stt_pending_segment_futures` | 0 | TurnContext | per-turn — already moved in T0.1 |
| `_stt_active` | 1 | STTCommitter | session-long flag |
| `_stt_task` | 1 | STTCommitter | session-long task |
| `_stt_pause_commit_task` | 1 | STTCommitter | session-long task |
| `_stt_segment_commit_task` | 1 | STTCommitter | session-long task |
| `_auto_turn_speech_frames` | 2 | AudioRouter | ingress speech-energy counter |
| `_replay_chunks_pending` | 2 | AudioRouter | replay state |
| `_playback_mark_bytes_interval` | 2 | AudioRouter | mark accounting |
| `_playback_mark_seq` | 2 | AudioRouter | mark accounting |
| `_playback_ack_transport` | 2 | AudioRouter | playback acks |
| `_transport_reports_audio_delivery` | 2 | AudioRouter | transport capability |
| `_outbound_queue*` (4 fields) | 2 | AudioRouter | outbound queueing |
| `_outbound_task` | 2 | AudioRouter | outbound drain task |
| `_current_tts_task` | 3 | TTSScheduler | session-long task |
| `_tts_playback_suppressed` | 3 | TTSScheduler | session-long flag |
| `_tts_synth` | 3 | TTSScheduler | session-long owned object |
| `_strip_markdown` | 3 | TTSScheduler | config flag |
| `_output_processors` | 3 | TTSScheduler | config |
| `_interruption_mode` | 4 | CancelOrchestrator | config |
| `_interruption_latency_compensation_ms` | 4 | CancelOrchestrator | config |
| `_interruption_ack_stale_ms` | 4 | CancelOrchestrator | config |
| `_interruption_ack_tail_cap_ms` | 4 | CancelOrchestrator | config |
| `_turn` | — | Session | the active turn pointer stays on Session |
| `_turn_generation` | — | Session | turn-versioning stays on Session |
| `_no_turn` | — | Session | sentinel stays on Session |
| `_runtime_scope` | — | Session | shared scope passed to collaborators |
| `_run_ctx` | — | Session | read-only handoff passed to collaborators |
| `_journal_sink` | — | Session | passed to collaborators |
| `event_bus` | — | Session | passed to collaborators |
| `_turn_manager` | — | Session | passed to collaborators |
| `_audio_gate` | — | Session | shared |
| `_timeout_config` | — | Session | passed to collaborators that need timeouts |

## Acceptance criteria

- [ ] `uv run pytest` passes unchanged.
- [ ] `uv run ruff check .` passes.
- [ ] `grep -c '^    def\|^    async def' src/easycat/session/_session.py`
  shows one fewer method (`_journaled_task` removed).
- [ ] `wc -l src/easycat/session/_session.py` shows ~30 lines fewer.
- [ ] No new test files added; no test files modified except where
  a test reaches `Session._journaled_task` directly (if any —
  there should be zero).
- [ ] Bundle round-trip parity verified: a bundle captured against
  pre-Phase-0 code replays cleanly against post-Phase-0 code.
- [ ] `grep -c 'self\._turn\b' src/easycat/session/_session.py`
  drops by at least 25 (target: ~30).

## Risks and rollback

**Risk 1 — `_no_turn` sentinel writes.** If T0.1 introduces a code
path that writes to `_no_turn.stt_final_future`, the next turn
starts with stale state. Mitigation: Pattern A in T0.2 (skip writes
when `turn is self._no_turn`). Add a unit test that asserts the
sentinel is never mutated.

**Risk 2 — `_journaled_task` circular import.** `RuntimeScope` is
in `runtime/scope.py`; `SessionJournalSink` is in
`session/_journal_sink.py`; the sink depends on
`runtime/journal.py`. To break the cycle: pass the sink as a
parameter (not via constructor) and import its type lazily via
`TYPE_CHECKING`.

**Rollback.** Each task (T0.1, T0.3, T0.4) is an independent commit.
Revert the offending commit; the previous tasks remain valid.

## Verification commands

```bash
uv run pytest tests/session/
uv run pytest tests/runtime/
uv run ruff check src/easycat/session/ src/easycat/runtime/
grep -c 'self\._turn\b' src/easycat/session/_session.py
wc -l src/easycat/session/_session.py src/easycat/session/_turn_context.py src/easycat/runtime/scope.py
```
