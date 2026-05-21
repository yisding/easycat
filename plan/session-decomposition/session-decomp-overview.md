# Session Decomposition — Overview

> **Current status:** historical design record with current as-landed
> notes. Static inspection on 2026-05-21 found the planned collaborators
> present in `src/easycat/session/`: `AudioRouter`, `STTCommitter`,
> `TTSScheduler`, `CancelOrchestrator`, `TurnRunner`, and
> `SessionJournalSink`. Use this file for rationale and residual
> `Session` ownership cleanup; do not restart the phase plan from the
> unchecked task lists.
>
> **Goal:** lift coordination logic out of
> `src/easycat/session/_session.py` (2,820 lines, 102 methods, 157
> instance attributes) into five focused collaborators, leaving
> `Session` as a thin lifecycle + user-facing surface (~1,770 lines
> as landed — the ~1,250 estimate below proved optimistic; see note
> under the phase table).
>
> **Non-goal:** introducing a new public pipeline-graph API. Stages
> already exist (`AudioStage`, `VADStage`, `STTStage`, `TTSStage`,
> `AgentStage`, `TransportStage`, `TurnStage`); this workstream
> reorganises *Session's coordination of those stages*, not the
> pipeline shape exposed to users. `EasyConfig` + `create_session` +
> `session.on(EventType, …)` remain identical to today.
>
> **Phase docs:**
>
> - `session-decomp-phase-0-turn-context.md` — TurnContext extension,
>   method-signature migration, `_journaled_task` promotion, attribute
>   ownership ledger
> - `session-decomp-phase-1-stt-committer.md` — STT segment commit
>   scheduling
> - `session-decomp-phase-2-audio-router.md` — transport ingress,
>   outbound drain, playback-mark accounting
> - `session-decomp-phase-3-tts-scheduler.md` — TTS payload prep,
>   synthesis, sentence-pipelining hook
> - `session-decomp-phase-4-cancel-orchestrator.md` — control-signal
>   propagation, barge-in policy, interruption notification
> - `session-decomp-phase-5-turn-runner.md` — end-of-speech, streaming
>   agent loop, text-mode turn

## Why this is not the same as Workstream 3

WS3 (`../workstreams/workstream-3-stage-refactor.md`) extracted `TurnContext`,
`InterruptionController`, `VoiceDeliveryLedger`, defined `Stage` /
`RunContext`, ported the eight stages, and reduced Session from
~1,500 to ~2,820 lines (it has since grown back). WS3 was the
**stages** refactor. This workstream is the **coordination** refactor
that follows: it lifts the per-stage orchestration glue out of
Session into per-concern controllers that sit alongside the existing
stages.

The two refactors are complementary. WS3 made each pipeline boundary
journaled, replayable, and testable in isolation. This decomposition
makes the *orchestration* of those boundaries journaled, replayable,
and testable in isolation.

## Constraints

- **Onboarding tax is non-negotiable.** No new concept lands in the
  teaching ladder chapters 0–12. Users still write
  `create_session(EasyConfig(...))`; the collaborators are private
  (`session/_stt_committer.py` etc.). Chapters 13–15 may reference
  collaborators if they need to.
- **Public API frozen.** Every symbol in `easycat/__init__.py`
  keeps its current name and signature. `Session.start()`,
  `Session.stop()`, `Session.on()`, `Session.cancel_turn()`,
  `Session.send_text()`, `Session.export_debug_bundle()`,
  `Session.agent`, `Session.journal`, telephony properties — all
  unchanged.
- **Test suite stays green throughout.** Each phase is an
  independently-shippable PR. After every phase, `uv run pytest`
  passes without modification to any test file. Tests are evidence
  that internal restructuring preserved behaviour.
- **Journal record stability.** No record `name` changes, no
  ordering invariants violated. Bundle compatibility is preserved
  — bundles captured pre-decomposition replay correctly
  post-decomposition.
- **Latency budget honoured.** The essential plan's P50 ≤ 1.0s /
  P90 ≤ 1.6s turn-latency targets do not regress. Run the perf
  gate (`perf/`) before and after each phase.

## Phase summary

| Phase | Module | Lines moved | Lands at | Risk |
|---|---|---:|---:|---|
| 0 | `_turn_context.py` ext + `runtime/scope.py` ext | ~30 | 2,790 | mechanical |
| 1 | `session/_stt_committer.py` | ~280 | 2,510 | low |
| 2 | `session/_audio_router.py` | ~310 | 2,200 | low |
| 3 | `session/_tts_scheduler.py` | ~190 | 2,010 | low |
| 4 | `session/_cancel_orchestrator.py` | ~140 | 1,870 | medium |
| 5 | `session/_turn_runner.py` | ~620 | **~1,250** | high |

> **As-landed note:** Session is **~1,770 lines** after Phase 5, not
> the projected ~1,250. The "Lands at" column was an estimate; the
> ~520-line gap is real Session-resident concern (lifecycle teardown,
> telephony/screening state, the full public event surface, action
> drain, collaborator construction/wiring) that the estimate
> under-counted. The decomposition goal (coordination logic lifted
> into five collaborators, behaviour preserved) still holds.

Phases 0–4 are independently revertible. Phase 5 depends on 0–4.

## Architecture after Phase 5

```
                     ┌──────────────────────────────┐
                     │           Session            │
                     │  (lifecycle, events, agent,  │
                     │   telephony, helpers,        │
                     │   action drain, journal,     │
                     │   bundle export, public      │
                     │   facades)                   │
                     └─┬──────────────────────────┬─┘
                       │                          │
            ┌──────────┴──────────┐    ┌──────────┴──────────┐
            │                     │    │                     │
   ┌────────▼─────────┐  ┌────────▼────▼─────────┐   ┌───────▼──────────┐
   │   TurnRunner     │  │   CancelOrchestrator  │   │   AudioRouter    │
   │ (end-of-speech,  │  │  (signal propagation, │   │ (ingress loop,   │
   │  streaming agent,│  │   barge-in, interrupt │   │  outbound drain, │
   │  text-turn)      │  │   record)             │   │  playback marks) │
   └─┬──────────────┬─┘  └──────────────┬────────┘   └─────────┬────────┘
     │              │                   │                      │
     │   ┌──────────▼─────────┐   ┌─────▼──────────┐   ┌───────▼────────┐
     │   │   STTCommitter     │   │   stages[7]    │   │  (calls into   │
     │   │ (segment commit,   │   │   (existing)   │   │   AudioStage,  │
     │   │  pending futures,  │   └────────────────┘   │   VADStage,    │
     │   │  cancel)           │                        │   STTStage)    │
     │   └────────────────────┘                        └────────────────┘
     │
     └──▶ TTSScheduler (prepare payload, synthesize, sentence
                        pipelining hook, cancel)
```

Solid arrows are construction-time references. Dashed coupling
(STTCommitter ↔ AudioRouter for `reset_speech_detection`) is
documented in Phase 2.

## Sequencing

```
Phase 0 ──┐
          ├─▶ Phase 1 (STTCommitter)  ──┐
          │                             │
          ├─▶ Phase 2 (AudioRouter)   ──┤
          │                             ├──▶ Phase 5 (TurnRunner)
          ├─▶ Phase 3 (TTSScheduler) ──┤
          │                             │
          └─▶ Phase 4 (CancelOrchestr) ┘
```

Phases 1–4 are independent of each other (the cross-coupling hook in
Phase 2 is a small documented interface, not a code dependency on
Phase 1 being merged first — it stubs out with a no-op until Phase 1
lands).

Phase 5 requires all four collaborators to exist because TurnRunner
takes refs to each.

## Acceptance gate per phase

Every phase PR must include:

1. **No public API change.** `git diff origin/main -- src/easycat/__init__.py` is empty.
2. **Test suite green.** `uv run pytest` passes unchanged.
3. **Lint clean.** `uv run ruff check .` passes.
4. **Journal record names unchanged.** `grep -h 'name=' src/easycat/session/*.py src/easycat/runtime/scope.py | sort -u` before/after diff is empty. (The `task_*` records moved from Session to `runtime/scope.py` in Phase 0; scoping the grep to only `session/*.py` yields a false-positive 4-name diff.)
5. **Bundle round-trip parity.** A captured pre-phase bundle replays without errors against the post-phase code (Workstream 4 replay tests).
6. **Perf gate passes.** P50/P90 turn latency unchanged within
   noise band (see `perf/`).
7. **Session method count drops by the planned amount** (verifiable
   via `grep -c '^    def\|^    async def' src/easycat/session/_session.py`).

## Out of scope

- Sentence-level TTS pipelining (planted as hook in Phase 3,
  implemented separately).
- Provider connection pooling / multi-tenant scheduler.
- `LangGraphBridge` and other agent bridges
  (`../peripherals/peripheral-langchain-langgraph-bridge.md`).
- Pipeline-graph builder public API.
- Realtime / voice-to-voice mode (out of scope per the essential plan).
- Anything in the teaching ladder content.

## Open verification items resolved during planning

The pre-planning code audit verified the following so phase
authors can proceed without re-investigating:

- **`_run_ctx` is read-only.** Created once in `_session.py:405`,
  only consumed via `stage.execute()` / `stage.handle_upstream()`.
  Pass as constructor arg to each collaborator. No mutation.
- **`RuntimeScope` is name-keyed.** Each collaborator uses distinct
  task names (`stt_pause_commit`, `stt_segment_commit`,
  `pipeline_heartbeat`, `call_answered_greeting`, etc.). Pass the
  single Session-owned `RuntimeScope` to each collaborator.
- **`_journal_sink` is already extracted** as `SessionJournalSink`
  in `session/_journal_sink.py`. Collaborators take the sink and
  write via `sink.append_record(...)`. No ordering invariants are
  violated by the split (verified record-by-record in the planning
  audit).
- **Event-bus subscription wiring stays in `Session.__init__`** as
  a centralised handler table. Lines 271-302 today. After
  decomposition the subscription targets change from
  `self._method` to `self._collaborator.method` but the table
  itself stays in Session.

## What stays on Session permanently

After Phase 5, Session is ~1,770 lines (see as-landed note above) and owns:

- **Lifecycle.** `__init__`, `start`, `stop`, `shutdown`, `close`,
  `destroy`, `_preserve_*_after_destroy`, `_mark_closed`,
  `wait_closed`, `__aenter__`, `__aexit__`.
- **User event surface.** `subscribe_event`, `subscribe_events`,
  `unsubscribe_event`, `subscribe_agent_events`, `on`,
  `unsubscribe_handlers`, `get_helper`.
- **Telephony state.** Nine properties
  (`outbound_call_manager`, `outbound_call_state_machine`,
  `number_health_monitor`, `call_disposition_tracker`,
  `dnc_list`, `call_identity`, `caller_id_exposure`,
  `_caller_id_system_message`, `_on_stt_final_opt_out`).
- **Greeting.** Four small methods inlined; not extracted (see
  Phase 5 rationale).
- **Action drain.** `register_action_executor`,
  `_drain_session_actions`, `_find_action_executor`.
- **Agent surface.** `agent` getter/setter,
  `_inject_agent_runtime_config`.
- **Introspection.** `turn_state`, `is_running`, `is_speaking`,
  `is_bot_speaking`, `journal`, `cancel_token`, `transport_kind`.
- **Coordination glue.** `_emit`, `_with_correlation`,
  `_journal_turn_id`, `_emit_heartbeats`, `_on_queue_drop`,
  `_on_turn_state_changed`, `_is_gated`, `_maybe_attach_event_bus`,
  `_stop_helpers`, `_reset_turn_state` (delegates to collaborators),
  `export_debug_bundle`, public cancel/turn facades.

The clear story for new contributors: *Session is the
user-facing object and the lifecycle owner; the collaborators
handle per-concern coordination.*
