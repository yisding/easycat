# Code Quality — Duplication & Consolidation

The recurring theme: **shared abstractions already exist** (`stages/base.py`, `BridgeTemplate`,
`ServerTransportBase`/`AudioQueueMixin`, `_journal_codec.py`, `run_interruption_journal_protocol`)
but copies were made alongside them and have started to drift. Consolidation therefore *aligns* with
the repo's conventions (protocol-over-inheritance, one-provider-per-file) rather than fighting them.

**Ground rule for every item:** incremental — extract the shared helper, migrate one caller, verify,
repeat. Never a big-bang rewrite. Land the divergent copy's missing behavior *in the shared version*
as you consolidate.

| # | Sev | Duplicated thing | # sites | Shared home |
|---|-----|------------------|---------|-------------|
| 8 | Med | WebRTC signaling (auth/CORS/quota/stats/redirect) | 2 | `server/_webrtc_shared.py` (→ QS6) |
| 9 | Med | Stage journaling boilerplate + `_parse_frame` | 7 (+2) | `stages/base.py`, `_WSTTSBase` |
| 10 | Med | Per-turn cursor/interruption cleanup | 7 | recorder-level context manager |
| 26 | Low | Turn-start bookkeeping | 3 | `TurnManager._begin_turn(...)` |
| 27 | Low | Twilio Media Streams protocol | 2 | `_TwilioProtocolMixin` |
| 28 | Low | Safe-config-snapshot + record serializers | 2+2 | shared debugger serializer module |
| 29 | Low | Transport `version_info()` | 8 | `transports/_base.py` helper |
| 30 | Low | `apply_interruption()` wrapper | 4 | `integrations/agents/base.py` |
| 31 | Low | STT batch-flush prologue | 2 | `STTBase._drain_buffer_to_wav()` |

> **Anchor note:** re-confirm cited lines before editing.

---

## #8 — WebRTC signaling duplicated across transport and routes (Med)

> **This overlaps the maintainability plan's QS6 (WebRTC convergence).** Treat QS6 as the umbrella
> and fold #8 into it — do not do a separate competing extraction. See
> [`../maintainability/02-structural-refactors.md`](../maintainability/02-structural-refactors.md).

- **Sites.** `server/webrtc_routes.py` vs `transports/webrtc.py`, near-verbatim (docstrings literally
  say "byte-identical to the transport"): `_cors_headers` (901-921 / 299-324),
  `_stats_quota_error` (1004-1029 / 573-599), forbidden/quota responses (979-1002 / 550-571),
  `_ice_servers_as_dicts` (875-892 / 470-481), the stats-persist flow, the `handle_root` `?webrtc=`
  sanitizer (1503-1545 / 617-664).
- **Existing/target abstraction.** Lift the config-only helpers into `server/_webrtc_shared.py`
  (matching the already-shared `_sanitize_webrtc_base`/`_is_loopback_host` pattern) and have both
  call them; route `WebRTCTransport._request_authorized` through `server.auth.BearerTokenAuth`.
- **Consolidation steps.**
  1. Extract the stateless helpers one at a time into `_webrtc_shared.py`; migrate both callers.
  2. `_stats_write_permitted` has **already diverged** — reconcile to one canonical version as you
     lift it.
  3. Unify auth (only two-way — routes already delegate to `AuthPolicy`).
- **Known drift to reconcile.** `_stats_write_permitted`.
- **Validation.** `just guard-contracts` + `tests/transports/test_webrtc_*` + `tests/server/test_webrtc_routes.py`.
- **Risk.** Medium (security-relevant surface). Land one helper per PR; see QS6 for the full,
  test-first convergence plan (it also fixes the latent non-ASCII-credential DoS).

---

## #9 — Stage-wrapper journaling boilerplate + `_parse_frame` (Med)

- **Sites.** All 7 stage wrappers (`stages/stt.py:48` and siblings) re-implement an identical
  `_journal_ctx` and the identical failure trio (`annotate_stage_exception` +
  `increment_counter('easycat.provider.errors.total')` + `journal_append_event('stage_error')` +
  `raise`) wrapped by the `record_histogram` finally, plus a repeated LIVE-replay preamble (two
  copies live inside `tts.py` alone). Separately, byte-identical `_parse_frame` in
  `tts/cartesia_tts.py:174` and `tts/elevenlabs_tts.py:417`.
- **Existing/target abstraction.** `stages/base.py` documents itself as the place to "centralise the
  boilerplate every stage used to duplicate" but stopped at `journal_append_event`.
- **Consolidation steps.**
  1. Add module-level `journal_ctx(...)`, `record_stage_failure(..., provider, ...)` (leave `raise`
     at the call site), and `live_replay_input(spec, cassette)` to `stages/base.py`.
  2. Migrate one stage wrapper at a time.
  3. Move `_parse_frame` up to `_WSTTSBase` as a staticmethod (both providers subclass it and feed
     `MultiContextAdapter`).
- **Behavioral nuance to preserve.** AudioStage's `error_provider` threading is **intentional**
  per-component attribution, not harmful drift — the shared helper **must** take a `provider=` arg.
- **Validation.** `just guard-contracts` + per-stage `uv run pytest tests/stages/` / `tests/tts/`.
- **Risk.** Low-medium; one stage per PR keeps blast radius small.

---

## #10 — Per-turn cursor/interruption cleanup copy-pasted 7× (Med)

- **Sites.** `integrations/agents/template.py:100` defines `BridgeTemplate`, but only
  `GenericWorkflowBridge` inherits it; the six shipped bridges each hand-roll the per-turn cursor
  lifecycle, with the `except BaseException` cleanup arm copy-pasted (near-identical comment) 7×.
  `OpenAIAgentsBridge` implements the invariant a *third* way (`finally` + `cursor_exited`, no
  BaseException arm).
- **Existing/target abstraction.** A recorder-level context manager
  (`recorder.turn_cursor(cursor)`) that centralizes the enter/error/BaseException/clean-exit
  ordering. (Full `BridgeTemplate` migration is **infeasible** — bridges have genuinely different
  invoke shapes: multi-cursor stacks, handoff entry, post-loop chain state. Don't force it.)
- **Consolidation steps.**
  1. Add `recorder.turn_cursor(cursor)` context manager centralizing the ordering.
  2. Converge `OpenAIAgentsBridge`'s variant onto it **first** (do this alongside bug #2's fix, which
     already touches this bridge's cancel/cleanup), so the three implementations become one.
  3. Migrate the remaining bridges one at a time.
- **Known drift to reconcile.** `interrupted` is dead in `openai_agents` but live in `responses_api`;
  tool-drain is a dict in one, a set in the other — converge on the context manager's canonical form.
- **Validation.** `uv run pytest tests/integrations/agents/`.
- **Risk.** Medium — interruption ordering is correctness-sensitive; sequence after bug #2/#11.

---

## #26 — Turn-start bookkeeping duplicated 3× (Low)

- **Sites.** `turn_manager.py:399` — the full new-turn sequence (fresh `CancelToken`, counter
  increment, `turn-{counter:04d}-{uuid4().hex[:8]}` id, 5-line pre-roll flush, transition,
  `TurnStarted` emit) is written verbatim in the VAD-IDLE, barge-in, and manual paths, and **already
  drifts** (flush-before vs flush-after transition; only barge-in cancels the old token).
- **Existing/target abstraction.** `_begin_turn(reason, *, cancel_previous_token=False)` on
  `TurnManager`; normalizes flush/transition ordering.
- **Consolidation steps.**
  1. Extract `_begin_turn(...)`, choosing one canonical flush/transition order.
  2. Route all three paths through it.
  3. While here, add `reset(preserve_token=…)` so `TurnRunner` stops writing `manager._cancel_token`
     directly (see architecture #15).
- **Known drift to reconcile.** flush ordering; token-cancel behavior.
- **Validation.** `uv run pytest tests/` (turn_manager / session tests).
- **Risk.** Low-medium. **Sequence after bug #3/#6** (which also touches these paths).

---

## #27 — Twilio dual-class protocol duplication (Low)

- **Sites.** `transports/twilio_media.py:1070` — `TwilioTransport` and `TwilioConnectionTransport`
  each carry a full copy of the Media Streams wire protocol
  (`_handle_message`/`start`/`media`/`stop`/`mark`/`dtmf`, `_emit_call_ended_once`, send/mark/clear
  encoders). Confirmed drift: the connection variant dropped the `connected` branch and the
  unknown-event log.
- **Existing/target abstraction.** A `_TwilioProtocolMixin` for the inbound routing + handlers,
  parameterized on a `_current_ws()` getter and a `_reset_connection_state()` hook — mirrors the
  existing `ServerTransportBase`/`AudioQueueMixin` sharing.
- **Consolidation steps.**
  1. Extract the shared handlers into `_TwilioProtocolMixin`.
  2. Land the missing `connected` branch + unknown-event log in the single shared copy.
  3. Keep the outbound error-path state reset per-class (legitimately different lifecycle models).
- **Behavioral nuance to preserve.** Outbound vs inbound lifecycle differences.
- **Coordination.** The **#19 reconnect-race fix** should land in the single shared copy — fix #19
  first in place, then this dedup carries one correct copy (or do #27 first and put the guard in the
  mixin).
- **Validation.** `uv run pytest tests/transports/`.
- **Risk.** Low.

---

## #28 — Debugger serializers duplicated + divergent (Low)

- **Sites.** `debugger/server.py:603` — two verbatim `safe_config_snapshot` copies (server manifest
  vs export bundle), plus two independent `JournalRecord → dict` serializers that **already
  disagree**: the server's hardcoded attribute tuple drops `tags` and subclass fields
  (framework/direction/latency) that the export's generic dataclass walk includes, so the same record
  renders differently live vs in a bundle.
- **Existing/target abstraction.** `_journal_codec.py` centralizes exactly this for SQL rows; the
  JSON shape has no home. Create one `record_to_dict`/`json_safe_value` + one
  `safe_config_snapshot_from_session` in a shared module; adopt the **generic dataclass-walk** as
  canonical.
- **Consolidation steps.**
  1. Add the shared serializer module.
  2. Point both call sites at it, standardizing on the generic walk (fixes the live-vs-bundle
     divergence).
- **Known drift to reconcile.** The dropped `tags`/subclass fields in the server's serializer.
- **Validation.** `uv run pytest tests/debugger/` (and `just guard-ops`).
- **Risk.** Low. Coordinate with QS3 (debugger/server.py split) — this extraction is a natural QS3
  sub-step.

---

## #29 — Transport `version_info()` copy-pasted 8× (Low)

- **Sites.** `transports/_base.py:297` plus local/twilio×2/websocket×2/webrtc/webtransport×2 —
  identical `importlib.metadata` try/except + 4-key dict; only provider label, SDK package, and
  (rarely) `api_version` vary.
- **Existing/target abstraction.** `_sdk_version(package)` + `make_version_info(provider,
  sdk_package, *, api_version="unknown")` in `_base.py`.
- **Consolidation steps.**
  1. Add the two helpers to `_base.py`.
  2. Collapse each override to a one-line call.
- **Validation.** `uv run pytest tests/transports/`.
- **Risk.** Very low — pure mechanical.

---

## #30 — `apply_interruption()` wrapper duplicated across 4 bridges (Low)

- **Sites.** `integrations/agents/langchain.py:455` + langgraph/llama_agents/openai_agents — the thin
  outer wrapper (`plan = self._plan_interruption(...)` then forward
  `_serialize_framework_state`/`_apply_planned_mutation`) is byte-identical. (`responses_api` and
  `pydantic_ai` legitimately differ.)
- **Existing/target abstraction.** The inner journal protocol is already extracted
  (`run_interruption_journal_protocol`); add a module-level `apply_standard_interruption(bridge,
  delivered_text, mode, recorder, caused_by_signal_id)` in `integrations/agents/base.py`.
- **Consolidation steps.**
  1. Add `apply_standard_interruption(...)` to `base.py`.
  2. Delegate from the 4 matching bridges in one line each.
- **Validation.** `uv run pytest tests/integrations/agents/`.
- **Risk.** Low. Sequence after bug #2/#11 and #10 (same bridge files).

---

## #31 — STT batch-flush prologue duplicated (Low)

- **Sites.** `stt/openai_provider.py:140` (`_flush_buffer`) and
  `stt/elevenlabs_provider.py` (`_flush_batch_buffer`) share a verbatim ~15-line prologue (docstring,
  empty/format guard, `pcm_to_wav`, in-place clear + rationale comment); only the transcribe/emit
  tail differs. Both descend from `STTBase`.
- **Existing/target abstraction.** `STTBase._drain_buffer_to_wav() -> bytes | None` (which already
  hosts `_latch_uniform_format`/`_buffer_batch_audio_or_finalize`).
- **Consolidation steps.**
  1. Add `_drain_buffer_to_wav()` to `STTBase`.
  2. Have both providers call it, keeping their distinct transcribe/emit tails.
- **Validation.** `uv run pytest tests/stt/`.
- **Risk.** Low. Sequence after bug #12 (same ElevenLabs method region).
