# Code Quality — Architecture & Smells

Layering/hot-path consistency fixes plus small smells and dead-code deletions. Several of these are
deliberately **consistency/tidiness** rather than "audible bug" — that is stated per item so effort
is spent honestly. Two (#16, #40) do touch the realtime audio hot path and are worth doing for
latency-invariant reasons.

> **Anchor note:** re-confirm each cited line before editing.

## Architecture

### #15 — Collaborators reach into other components' private attributes

- **Category / Severity:** architecture / Low (functionally correct today; the coupling is the risk)
- **Location:** `session/_turn_runner.py:366`

**Problem.** `TurnRunner` writes `manager._cancel_token = None` and relies on `reset()`'s internal
cancel-then-clear ordering; `AudioRouter` inspects `EventBus._handlers` (a defaultdict) to count
sibling routers, with a `getattr(..., {})` fallback that would silently yield 0 routers if storage
changes. Both are exactly the cross-collaborator coupling the `session/` decomposition was built to
avoid.

**Fix steps.**
1. Add `TurnManager.reset(preserve_token=False)` (or `detach_token()`) and route `TurnRunner`
   through it (dovetails with duplication #26's `_begin_turn`).
2. Add `EventBus.handlers_for(event_type)` (or a router-registration count) and use it in
   `AudioRouter`. Both changes are additive.

**Validation.** `uv run pytest tests/session/`.

**Risk.** Low — additive API, behavior-preserving.

### #16 — WebRTC `_recv` awaits `EventBus.emit` on the 20 ms RTP pacing path

- **Category / Severity:** architecture / Low (invariant/consistency fix; absolute-PTS pacing
  self-corrects a single slow emit, so not a live audio bug — but it violates a documented invariant)
- **Location:** `transports/webrtc.py:754`; **reference:** `LocalTransport` (schedules the identical
  event as a tracked task)

**Problem.** Per-20 ms-frame `_recv` awaits `event_bus.emit(TransportAudioDelivered)` inline, and
`emit` awaits every handler (journal write) to completion — contradicting `_base.py:126-127`
("Observability must never block a transport hot path"). The emit isn't load-bearing for AEC here
(reference captured synchronously).

**Fix steps.**
1. Mirror `LocalTransport`: fire the event via a tracked `asyncio.create_task` (fire-and-forget with
   a strong ref for lifecycle), not an inline `await`.

**Validation.** `uv run pytest tests/transports/` (webrtc). Overlaps QW9's `ASYNC` findings in this
file.

**Risk.** Low.

### #17 — Debugger export/AEC endpoints run heavy sync work on the loop

- **Category / Severity:** architecture / Low (freezes the *debugger* loop, not the audio pipeline —
  except the full-journal `fetchall` briefly holds the same lock a live append needs)
- **Location:** `debugger/server.py:2382`

**Problem.** The export handler calls `export_fn()`/`export_turn_fn()` synchronously (unlike the
records search at 1992 which uses `asyncio.to_thread`). `export_debug_bundle` reads *every* artifact
into an in-memory dict then DEFLATEs it (contradicting the route's own "we don't have to hold the
bundle bytes in memory" comment); single-turn export triple-buffers (full export → `RunBundle.load`
→ slice → re-write).

**Fix steps (independent).**
1. Wrap the exports + `_aec_diagnostics_for_turn` in `asyncio.to_thread`.
2. Stream artifacts into the zip file-by-file instead of buffering all in memory.
3. Add a journal-slice-based single-turn export (avoid the triple-buffer).

**Validation.** `uv run pytest tests/debugger/` (and `just guard-ops`).

**Risk.** Low-medium. Natural to fold into QS3 (debugger/server.py split). Overlaps QW9 `ASYNC` hits.

### #38 — `_drain_emit_tasks()` applied inconsistently across transports

- **Category / Severity:** resource-leak / Low (cosmetic — the mixin docstring calls the drain
  "lifecycle tidiness, not correctness"; symptom is "Task was destroyed but it is pending" noise)
- **Location:** `transports/webrtc.py:1107`

**Problem.** Only `ServerTransportBase.disconnect` and `TwilioConnectionTransport.disconnect` drain
in-flight `_emit_degraded` tasks; WebRTC, WebSocketConnection, WebTransportConnection, and Local
don't — even though all emit via `_emit_degraded` (WebRTC/WebTransport are the heaviest emitters).

**Fix steps.**
1. `await self._drain_emit_tasks()` at the end of each un-drained `disconnect`, **or** lift it into a
   shared `AudioQueueMixin` teardown helper (preferred — one place).

**Validation.** `uv run pytest tests/transports/`.

**Risk.** Low.

---

## Smells & cleanups

### #39 — Pure-Python mulaw codec blocks the loop

- **Category / Severity:** smell (perf) / Low
- **Location:** `transports/twilio_media.py:864`

**Problem.** Per-sample encode/decode loops burn measurable event-loop CPU (~5–15 ms per second of
audio) on the most latency-sensitive path; at `max_sessions=64` the aggregate is significant.

**Fix steps.**
1. Replace with a table-driven G.711 codec (256-entry decode LUT + segment/exponent encode table) —
   pure stdlib, orders of magnitude faster.

**Validation.** `uv run pytest tests/transports/`; optionally a micro-benchmark under `perf/`.

**Risk.** Low — pin correctness with a round-trip test against the current implementation's outputs.

### #40 — `/stats` handler does sync file I/O on the loop

- **Category / Severity:** smell (perf) / Low (gated behind `stats_path`, inactive in normal prod)
- **Location:** `transports/webrtc.py:1484`

**Problem.** Every POST `stat()`s + re-reads the whole JSONL artifact for the record-count quota,
then does a sync `mkdir`+`open`+`write`, on the loop, up to 120/min.

**Fix steps.**
1. Use an in-memory record counter (the process already tracks `_stats_request_times`).
2. `asyncio.to_thread` the append. Apply to the duplicated routes copy too (see #8/QS6).

**Validation.** `uv run pytest tests/transports/` (webrtc stats tests). Overlaps QW9 `ASYNC` hits.

**Risk.** Low.

### #41 — Twilio media listener omits `compression=None`

- **Category / Severity:** smell / Low
- **Location:** `telephony/server.py:224` (and the scaffold template)

**Problem.** Every other realtime WS `serve` call explicitly disables permessage-deflate; this one
doesn't, so the library negotiates deflate on per-20 ms mulaw frames if a client offers it.

**Fix steps.**
1. Add `compression=None` to match the other three listeners (and the scaffold template).

**Validation.** `uv run pytest tests/telephony/` (and template guard `just guard-templates`).

**Risk.** Trivial one-liner.

### #42 — ElevenLabs realtime final-timeout stale + hardcoded

- **Category / Severity:** smell / Low
- **Location:** `stt/elevenlabs_provider.py:33`

**Problem.** Hardcoded `5.0` with a "Mirrors OpenAIRealtimeSTT" comment — but the sibling was tuned
to `0.9` and promoted to a config field precisely because the wait shows up as user-visible dead air.
On a provider stall this adds ~5 s of turn-to-agent latency and isn't operator-tunable.

**Fix steps.**
1. Add `final_transcript_timeout_s` to `ElevenLabsSTTConfig` (fold in with bug #12's `max_retries` —
   same config class, one PR).
2. Fix or drop the stale comment.

**Validation.** `uv run pytest tests/stt/`.

**Risk.** Low.

### #43 — `validate latency --json` envelope inconsistency

- **Category / Severity:** smell / Low
- **Location:** `cli/validate.py:508`

**Problem.** `command` is `f"validate latency {mode}"` on success but `"validate latency"` on usage
error — three values for one command, breaking `payload["command"] == "validate latency"` dispatch.
Usage errors also route human text to stdout (unlike every other command's stderr default).

**Fix steps.**
1. Make `command` a constant `"validate latency"`; carry `mode` as a separate field.
2. Drop the `human_console=stdout_console` overrides so usage errors go to stderr.

**Lockstep edits.** Updates the 3 tests that pin the current behavior.

**Validation.** `uv run pytest tests/cli/` (latency CLI tests) — `just guard-validation`.

**Risk.** Low; contract change — the guard tests cover it.

### Dead-code deletions (terse)

- **#23 — Dead `_closed` re-check (`session/_session.py:907`).** The inner `if self._closed:
  event.set()` is unreachable (no await between it and the early return; `_mark_closed` is
  sync/loop-bound). Delete it. Validation: `uv run pytest tests/session/`.
- **#24 — Write-only `interrupted` flag (`integrations/agents/openai_agents.py:184`).** Set but never
  read (`if not interrupted: interrupted = True` is a self-referential no-op); residue of the dropped
  chain-state guard. Delete the flag — **with bug #2** (same cluster). **Do not** port the
  `responses_api` guard without verifying the Agents-SDK interruption contract first.
- **#25 — Dead `ReplaySpec` `__getattr__` forwarder (`stages/base.py:169`).** Guards a "would
  deadlock" cycle that doesn't exist — `runtime/replay.py` never imports `easycat.stages`, and all 7
  stage modules already top-level import `ReplaySpec`. Delete the forwarder + false comment; repoint
  the 2 tests that use the alias at `easycat.runtime.replay`. Validation: `uv run pytest tests/stages/`.
