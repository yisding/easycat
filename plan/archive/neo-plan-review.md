# Neo Next-Major Plan Review

## Executive Summary

**Overall recommendation: PROCEED WITH CHANGES.**

The Neo plan is fundamentally sound and, despite its own self-declared staleness caveat (drafted from a static read of branch `work`@18cbf07 with no `origin` remote), its code assumptions are **overwhelmingly accurate against the current tree**. Nearly every named symbol — `EasyConfig` presets, `create_session`/`create_text_session`/`run_text_turn`, the per-transport `serve_*_config_sessions` helpers, `SessionManager`, `TwilioConnectionTransport`/`TwilioStreamTokenStore`/`twiml_connect_stream`, `DTMFAggregator`/`VoicemailDetector`, `ReplaySpec`/`ReplayFidelity`/`ToolReplayPolicy`, `LatencyBudget`/`LatencyBudgetMonitor`/`CostBudgetEnforcer`, the observability allow-list, the `DebuggerSource` abstraction, and the `debug='full'` no-autolaunch guard — exists where the plan says, with compatible signatures. The layering discipline (Session / EasyConfig / VoiceApp / VoiceServer) is the correct decomposition, and the strongest security guarantees the plan leans on (replay tool-DENY-by-default, the PII firewall in `_observability.py`, constant-time `hmac.compare_digest`) are real and enforced today.

The plan is **not** safe to implement as-written, however. There is one **critical** privacy defect (promotion-to-test extends an existing command that ships unredacted transcripts + raw audio by default, while the plan documents the opposite as if preserving it), one **high** security gap (the WebSocket server path has no non-loopback auth guard that the WebRTC path enforces), and a cluster of high-severity design/sizing issues: the central VoiceApp construction sketch cannot accept the inputs it documents, the `ConnectionContext` factory seam is undefined and self-inconsistent, capacity/draining is mis-attributed to `SessionManager`, the provider planner is treated as catalog-reuse when 5 of 7 roles have no catalog, and Milestone 6 (Manifest+Planner) is badly under-sized and is the trust anchor for `/health/ready`. Fix the items in "Must fix before implementation," tighten the medium items, and the investment is well-targeted.

---

## Per-Phase Verdict

- **Phase 1 — VoiceApp:** PROCEED WITH CHANGES. Building blocks are real and the north-star `VoiceApp(agent=Agent(...)).run("browser")` is feasible, but the public API sketch is internally inconsistent (cannot accept `stt=`/`tts=`, dead `default_mode`, undefined multi-input precedence, undefined `session()` semantics, undefined per-connection clone path) and must be specified before coding.
- **Phase 2 — VoiceServer:** REWORK THE SPEC (then proceed). The reuse narrative is materially wrong in three places (capacity/draining is not in `SessionManager`; `ConnectionContext` does not exist; `/twilio/media` cannot be an aiohttp route), the planner reuse is overstated, and M6 is under-sized. None block the concept, but the spec needs correction before it can guide implementation.
- **Phase 3 — Feedback Loop:** PROCEED WITH CHANGES. Text-first evals genuinely build on real primitives and the no-autolaunch invariant is already satisfied, but the promotion workstream carries a critical PII default, mislabels net-new helpers as re-exports, under-scopes the three-vocabulary latency reconciliation, and the dev-autolaunch acceptance criterion is untestable as worded.

---

## Corrections to Plan Code-Assumptions

This is the most important section: every place ground truth **contradicts** or finds **missing** something the plan assumes. Where the plan is grounded, that is stated explicitly.

### Contradicted (plan assumes X; code does the opposite)

1. **Capacity & draining are NOT in `SessionManager`.** Plan: `phase-2-voice-server.md:49,51,250-255` treats `SessionManager` as the registry that owns "session limits" and routes draining "through `SessionManager`." Reality: `src/easycat/session_manager.py:18-105` is a bare `add/remove/stop_all/connection` registry with no `max_sessions`, no `__len__`, no draining state. Capacity + draining live inline in the serve helpers: `transports/webrtc.py:354,356-357,377-390,422-434,475-482` and `transports/websocket.py:146,154-156,177-179`. (ARCH-2)

2. **`enable_dtmf_aggregator` / `enable_voicemail_detector` already exist on `TelephonyConfig`, default `False`.** Plan: `phase-1-voice-app.md:177-178` puts these on a new `TwilioVoiceServerConfig` defaulting **True**. Reality: `config/easy.py:405-406` already declares both on `TelephonyConfig` (default `False`); both example apps set them per-connection via `TelephonyConfig(...)` (`examples/twilio_app.py:74-77`). Putting them on a server config duplicates/shadows the existing fields and inverts the default. (Telephony ground truth; SEC-adjacent)

3. **The provider planner cannot "reuse catalog metadata" for 5 of 7 roles.** Plan: `phase-2-voice-server.md:336-371` applies `extra`/`required_env`/`capabilities` uniformly across stt/tts/vad/transport/agent/noise_reducer/echo_canceller and says "prefer extracting/shared catalog metadata." Reality: only STT/TTS have a `ProviderCatalog` (`_provider_catalog.py:1-2,285-353`). VAD resolves by try/except with extras in an error string (`vad/factory.py:91-151`); transport is config-type dispatch (`config/_factory.py:110-139`); noise/echo have hardcoded extras only (`noise_reduction.py:40,96`, `echo_cancellation.py:11,90`); `ProviderSelection.capabilities` has **no** static source (`validation/provider_capabilities.py:2-5` is a live-derived report). (ARCH-5, SEQ-2, SEQ-7)

4. **The six "Runtime Metrics To Add" do not exist at runtime; the plan implies aliases.** Plan: `phase-3-feedback-loop.md:299-311` treats `llm_ttft_ms`, `tts_ttfb_ms`, `stt_final_latency_ms`, `vad_endpointing_ms`, `first_audio_ms`, `barge_in_ack_ms` as a clean additive set "sharing the budget vocabulary." Reality: runtime emits **only** `stage="total_ms"` (`session/_turn_runner.py:686-696,847-853`); `tts_ttfb_ms`/`llm_ttft_ms` are offline-validation columns (`validation/latency.py:113-114,308-314`); the other four have zero hits in `src/`. Equivalent measurements exist under different names as waterfall milestones (`debug/_turn_timeline.py:325-336`, `cli/debug/bundles.py:1448-1452`). Three latency vocabularies must be reconciled, not added. (CONS-7, SEQ-3, TEST-4)

5. **`SEC-1` (critical): the existing `journal promote` ships unredacted PII + raw audio by default.** Plan: `risk-register.md:86-94` (R8) and `open-questions.md:128-134` (Q14) specify "warn + default to no audio + `--redact`" for a new `eval promote` as if preserving safe behavior. Reality: the command it extends, `promote_turn` (`cli/debug/bundles.py:1819-1962`), has only `--out/--force/--json`, calls `slice_bundle_by_turn` which copies full raw NDJSON (transcripts/tool args) and **every** referenced audio blob (`debug/export.py:154-170`), and prints the verbatim agent reply into the stub (`bundles.py:1749-1750,1775-1786`). No redaction anywhere in the path. The plan documents the safe behavior as existing; it does not. (SEC-1, SEC-2, TEST-2, SEQ-4)

6. **WebSocket has no non-loopback auth guard.** Plan: `risk-register.md:46-53` (R4) / `open-questions.md:93-98` (Q10) state "require auth for non-loopback." Reality: WebRTC enforces it (`webrtc.py:347-351,924-927` raise `ValueError`), but `transports/websocket.py` has **no** `_is_loopback_host` check — `auth_token` defaults `None` (`:84`), host is overridable via `EASYCAT_WS_HOST` (`:93`), and `websocket_server_authorized` returns `True` whenever token is `None` (`:100-111`). A `0.0.0.0` unauthenticated voice endpoint is reachable today. (SEC-3)

7. **`ConnectionContext` does not exist and the seam type is self-inconsistent.** Plan references `Callable[[ConnectionContext], ...]` at `architecture-boundaries.md:87` and `phase-2-voice-server.md:91`, but grep finds it only there. The real helpers take transport-specific args: `WebRTCTransport` (`webrtc.py:328,403`), `WebSocketConnectionTransport` (`websocket.py:183,201`), `WebTransportConnectionTransport` (`webtransport.py:1467,1509`), `TwilioConnectionTransport` (`phase-1:181`). Phase-1 itself types it `Callable[[Any], EasyConfig]` (`phase-1:68`). (ARCH-1)

8. **`/twilio/media` cannot be an aiohttp route.** Plan: `phase-2-voice-server.md:140-147,170` decides aiohttp-for-all-routes and lists `GET /twilio/media` in the unified table. Reality: Twilio media is raw `websockets.serve` on a separate port (`examples/twilio_app.py:128`; template `server.py:64`), and `TwilioConnectionTransport` consumes a `ServerConnection` (`twilio_media.py:918`), not an aiohttp request. Note the same is true for `/ws` (`websocket.py:162` is also raw websockets) — so the endpoint table is a logical surface listing, not an aiohttp manifest, and should say so. (ARCH-4)

### Missing (plan assumes a symbol/helper exists; it does not)

9. **`run_session` and the websocket config helpers are NOT top-level exports.** Plan lists them as a uniform "Existing Building Blocks" surface (`phase-1:42-46,128,151`). Reality: `run_session` lives in `easycat.helpers` (`helpers.py:145`; `hasattr(easycat,'run_session')` is `False`); `run_websocket_config_server`/`serve_websocket_config_sessions` are absent from `_public_api.py` while the WebRTC equivalents are present (`_public_api.py:135-141`). Internal import is fine; any reader-facing `from easycat import run_session` fails the public-import gate (`tests/test_public_api.py:379-398`). (ARCH-8, DX-5)

10. **No clone / `with_transport` / `replace_transport` helper exists in `config/`.** Plan: `phase-1:103-104` and `acceptance-matrix.md:16` present "clone the config with a new transport" as a settled primitive. Reality: only `dataclasses.replace` works, and it shares the grouped `observability`/`audio_processing`/`session_policy` sub-config instances (verified empirically — mutating a replaced config's `observability.debug` flips the original), so a naive replace does NOT isolate concurrent sessions. The safe existing path is the per-transport `config_factory`. (ARCH-6, CONS-8)

11. **`CostBudget` value object, `build_budget_report`/`BudgetReport`, `easycat.evals`/`easycat.budgets`/`easycat.project`/`easycat.planning`/`easycat.server` packages, `easycat plan`/`easycat eval` CLIs, `promote_turn_to_test` library fn, `assert_budgets_pass`, the `python:module:function` agent resolver, transport string shortcuts, and `src/easycat/telephony/server.py` — all absent.** These are correctly net-new in the plan, but several are referenced as if existing in examples/sketches: `CostBudget` in `EvalScenario` (`phase-3:147`), `assert_budgets_pass` and `promote_turn_to_test` in the public-API sketch (`phase-3:128-138`). (Multiple subsystems; TEST-3, CONS-6)

12. **The public-API cap is fully exhausted at 94/94, not 91/94.** Plan never mentions the cap. Verified: `len(easycat.__all__) == 94`, `PUBLIC_API_SNAPSHOT` has 94 entries, `tests/test_public_api.py:126` asserts `<= 94`. Adding top-level `VoiceApp` (`phase-1:54`, `roadmap.md:74`) forces a deliberate cap bump (`94 → 95`) plus the triple-lock (`__all__` + `LAZY_EXPORTS` + `docs/public-api.md`). Note: `CostBudget`/`LatencyBudget`/`VoiceServer`/`EvalRunner` are submodule exports and do NOT count against this cap. (CONS-6, SEQ-8)

### Grounded (plan is correct — preserve these assumptions)

- `EasyConfig.mic()/.browser()/.phone()` presets, `agent=` accepting a raw OpenAI Agents SDK `Agent`, lazy `__init__` (`import easycat` pulls no heavy SDK), `create_text_session`/`run_text_turn`, the WebRTC config-server helpers, `SessionManager` as a registry, all Twilio primitives, `ReplaySpec.tool_policy=DENY` default, the `_observability.py` allow-list firewall, `hmac.compare_digest` usage, the `debug='full'` no-autolaunch guard, and every docs path in "Files To Update" — all confirmed at the cited locations. The stale-baseline risk **did not bite** for symbol existence; the corrections above are conceptual (where logic lives) and net-new-vs-existing mislabeling, not symbol drift.

---

## Must Fix Before Implementation

Ordered by severity. Deduped across review dimensions.

### MF-1 (CRITICAL — privacy) — Promotion-to-test must redact by default; the path it extends does not

- **Lenses:** SEC-1 (critical, upheld), SEC-2 (medium), TEST-2 (medium), TEST-7 (medium).
- **Problem:** `eval promote` is specified to "warn + default no-audio + `--redact`" (`phase-3:232-261`, R8, Q14) but extends `journal promote`, which copies full raw NDJSON + every audio blob and embeds verbatim transcript text into a committed file with zero warning (`debug/export.py:154-170`, `cli/debug/bundles.py:1749-1750,1775-1786`). Redaction is field-name + secret-regex only — no embedded-PII/NER detection (`validation/redaction.py:22-34,106-117`), so a transcript whose entire value is the assertion target cannot be both redacted and useful.
- **Fix:** State plainly in `phase-3` and R8/Q14 that the existing path is **unsafe** and this is hardening, not preservation. Make `--no-audio` the default (mirror `bundles export` at `bundles.py:1160`); route records through `redact_value` before serialization; add a `contains_unredacted_sensitive_text()` tripwire gated by `--allow-pii` (mirror `_assert_context_pack_redacted`). Default the record-assertion mode to assert on a **hash/regex** over the reply rather than embedding raw text. Decide explicitly: fix `journal promote` in place vs fork `eval promote` (leaving both is itself a footgun). Reuse `_promote_test_stub`/`_validate_promoted_slice` rather than re-implementing.

### MF-2 (HIGH — security) — WebSocket needs the non-loopback auth guard WebRTC already has

- **Lenses:** SEC-3 (high, upheld), DX-8/CONS-5 (env-var naming, below).
- **Problem:** `0.0.0.0` WebSocket binds with no token are accepted today (`websocket.py:84,93,100-111`); the plan's R4/Q10 mitigation is unbacked on this path and never flags the asymmetry.
- **Fix:** Make the non-loopback-requires-token guard a property of the unified `VoiceServer`/`AuthPolicy` layer applied to **both** transports, with `unsafe_allow_no_auth` as the only escape hatch. Add an `AuthPolicy` field for it (the sketch at `phase-2:179-195` has no loopback gate — it lives only in prose at `:203`). Add an acceptance test that a non-loopback WebSocket bind without a token and without `unsafe_allow_no_auth` raises.

### MF-3 (HIGH — API coherence) — `VoiceApp` sketch is internally inconsistent and must be specified

- **Lenses:** DX-1 (cannot accept `stt=`/`tts=`), DX-2 (no multi-input precedence — **upheld high**), DX-3 (dead `default_mode`), DX-7 (`session()` semantics), DX-4 (event-loop ownership), ARCH-6 (clone path).
- **Problem cluster** (all in `phase-1-voice-app.md:62-118`):
  - The 5-field `@dataclass` sketch (`:64-75`) cannot accept Construction Style #1's `VoiceApp(agent=..., stt="openai/realtime", tts="openai")` (`:92`) — `TypeError`.
  - Three construction inputs (`agent`/`config`/`config_factory` + high-level kwargs) have **no** precedence/mutual-exclusion/validation rule. `VoiceApp(agent=a, config=EasyConfig.browser(agent=b))` is a real, undefined conflict (`EasyConfig.browser(agent=...)` is valid via `easy.py:891-916`).
  - `default_mode` (`:69`) is never read by any method; `session()`/`serve()`/`run()` hardcode their own defaults (`:72-74`). Grep confirms one occurrence repo-wide.
  - `session(mode="browser")` returns a single `Session` but browser is multi-session-by-default (`:142`) — undefined.
  - Both `VoiceApp.run()` and `VoiceServer.run()` are sync `asyncio.run()` blockers with no documented loop-ownership rule and asymmetric async verbs (`serve` vs `serve_forever`).
- **Fix:** Add a **field allow-list** (which `EasyConfig` fields `VoiceApp` forwards vs owns) threaded via `**config_kwargs` into the chosen preset, enforced by a test. Add a precedence/mutual-exclusion section (recommend: `config`/`config_factory`/high-level-fields mutually exclusive, raise `ValueError` naming the conflict). Either wire `default_mode` into all three methods (`mode: VoiceMode | None = None` → `mode or self.default_mode`) or delete it. Constrain `session()` to single-session modes and raise for server modes; document the returned `Session` is un-started/caller-owned (matching `_factory.py:351`). Add an "Event loop ownership" subsection: `run()` is the only `asyncio.run()` caller, align the async verb on both objects, and state `VoiceServer` composes apps via the factory, never by calling `VoiceApp.run()`.

### MF-4 (HIGH — architecture spec) — Correct the Phase-2 reuse narrative

- **Lenses:** ARCH-1 (`ConnectionContext` seam, medium), ARCH-2 (`SessionManager` capacity, medium), ARCH-3 (route handlers / mount, medium), ARCH-4 (`/twilio/media` aiohttp, medium), ARCH-7 (from_app layering, medium).
- **Problem:** Several Phase-2 "reuse" claims are conceptually wrong (capacity/draining not in `SessionManager`; `ConnectionContext` undefined; WebRTC handlers require a throwaway `shim = WebRTCTransport(settings)` at `webrtc.py:359` and hardcode flat routes; `/twilio/media` cannot be an aiohttp route; `VoiceApp.serve()` and `VoiceServer` are two overlapping server entry points with divergent defaults — `VoiceServerConfig.max_sessions=64`/`port=8080` vs `WebSocketSessionServerConfig` `max_sessions=10`/`port=8765`).
- **Fix:** (a) Define `ConnectionContext` concretely (a small dataclass `{transport, transport_kind, peer, headers}`) OR standardize on per-transport factories — make `phase-1:68`, `phase-2:91`, `architecture-boundaries.md:87` use one identical signature. (b) State capacity/draining is net-new at the shared layer and add a work item to lift `Semaphore`/active-set/draining out of the two serve helpers into shared internals. (c) Add a sub-task to extract WebRTC handlers off `WebRTCTransport` and decide flat-vs-namespaced paths + the bundled-client/`?token=` migration cost (the client targets flat `/offer`,`/config`,`/stats` at `webrtc_client.html:301,425,455`). (d) Reframe Twilio media as a co-hosted raw-websockets listener (as `phase-1`'s two-port `TwilioVoiceServerConfig` already models); define how `health()`/draining span both listeners. (e) Define a single ownership rule: a mounted `VoiceApp` contributes only its `config_factory`; `VoiceServerConfig` owns all process policy.

### MF-5 (HIGH — sizing/risk) — Split M6 (Manifest+Planner) and gate readiness on planner parity

- **Lenses:** SEQ-2 (under-sized, high upheld), SEQ-7 (riskiest milestone, high upheld), SEQ-1 (readiness backward dependency, medium), ARCH-5 (planner reuse, medium).
- **Problem:** M6 bundles ≥4 PRs of mostly net-new work (3 new packages, manifest discovery/TOML/relative-path resolution, the net-new `python:` resolver, transport string shortcuts, profile→EasyConfig, secret redaction, AND a planner that must hand-roll vad/transport/agent/noise/echo resolution). M4's `/health/ready` contract (`phase-2:213-219`) depends on the M6 planner; R6 warns the planner can diverge from `create_session` and destroy readiness trust — with the divergence surface large precisely because 5 of 7 roles have no shared metadata.
- **Fix:** Split into **M6a** (manifest loader + `python:` resolver + transport-registry metadata + `easycat.toml→EasyConfig`) and **M6b** (`ProviderPlan`: stt/tts via catalog; vad/transport/agent/noise/echo as net-new declarative metadata + `easycat plan --json` + readiness wiring). Scope M4's `/health/ready` to draining/capacity/route-ready only and defer manifest+plan readiness checks to M6b. Mark M6 the highest-risk milestone in the roadmap and risk register. **Require an acceptance test that the planner verdict matches `create_session` outcome for every role**, and gate the manifest/plan readiness checks behind that parity test passing.

### MF-6 (HIGH — completeness) — Server metrics & the `/metrics`,`/manifest`,`/plan`,`/capabilities` endpoints have no completing milestone and require frozen-allow-list edits

- **Lenses:** CONS-2 (high upheld, adjusted medium), SEC-5 (metrics cardinality/PII, medium), TEST-6 (test depends on unbuilt scaffolding, low).
- **Problem:** Server metrics `easycat.server.*` are not in `METRIC_DEFINITIONS` and labels `easycat.route`/`server_state`/`auth_result` are not in `LOW_CARDINALITY_ATTRIBUTE_KEYS` (`_observability.py:34-67`); `_record_metric`/`sanitize_attributes` raise `ValueError` on any unregistered name/key (`:199,209-210`). M5 only adds a "skeleton"; no milestone completes metrics; the four endpoints have zero acceptance rows. `easycat.route` is also a latent PII/cardinality hazard — the key-name allow-list cannot distinguish a route template from a raw path.
- **Fix:** Add a dedicated milestone (or fold into M5/M7) that registers the new metric names and four labels in the same PR, with a test that the server emits through `sanitize_attributes`. Constrain `easycat.route` to an enumerated set of route **templates** (assert the value before recording). Add acceptance rows (200 + JSON shape) for `/metrics`, `/manifest`, `/plan`, `/capabilities`. Note `easycat.transport` is already registered (`:59`).

---

## Should Fix / Worth Reconsidering (upheld medium)

- **SEC-6 — Manifest secret rule is documented but unenforced.** No loader exists (grep: no `bearer-env`/`tomllib`/`ProjectManifest`). Specify a testable contract: `auth`/`token` fields MUST match an env-reference grammar and the loader MUST raise `EASYCAT_Exxx` on a literal-looking secret (reuse `redaction._SECRET_RE`/`contains_unredacted_sensitive_text`); route any echoed manifest through `redact_value`; add acceptance tests (literal secret rejected; `--json` dump shows no resolved token). (`risk-register.md:55-63`, `open-questions.md:69-74`, `phase-2:302,328`)

- **SEC-4 — `?token=` query auth is unconditional today; `allow_query_token=False` is a breaking change.** WebRTC (`webrtc.py:826-834`) and WebSocket (`websocket.py:102-111`) accept query tokens whenever a token is set. The default-off posture is correct but breaks the **WebSocket** browser client (`examples/ws_browser_client.html:91-93` — browsers cannot set headers on the WS handshake). The bundled WebRTC client is unaffected (it sends Bearer). Confirm the new default applies to existing handlers too, document the WS-client break, and provide the `allow_query_token=True` loopback opt-in. (`phase-2:191-194,201`)

- **CONS-1 — `evals/promote.py`/`evals/replay_test.py`/`cli/evals.py` are double-listed** across Workstream B's package tree (`phase-3:117,119`) and Workstream C's New Files (`:201-203`). Assign one owner per file (recommend: B scaffolds, C implements; relabel C as "Files To Implement (created in B)"). (downgraded to low by verifier, but worth a one-line cleanup)

- **CONS-3 — `easycat eval report` is specified once (`phase-3:193`) but tracked in no acceptance row and no milestone.** Add it to M9 scope + an acceptance row ("emits JSON report envelope, `schema_version=1`"), or mark it out-of-scope-for-v1. It mirrors `validate report` (`cli/validate.py:676,833`).

- **CONS-5 / DX-8 — Env-var naming collision `EASYCAT_SERVE_TOKEN` (shipped, `cli/serve.py:36,106`) vs `EASYCAT_SERVER_TOKEN` (plan, `phase-2:302`, `open-questions.md:74`).** One letter apart; the plan migrates `serve` through `VoiceApp`/`VoiceServer` without saying which wins. Standardize on the existing `EASYCAT_SERVE_TOKEN` (or document a deliberate rename) and record the decision. (Note: in the manifest `bearer-env:NAME` model the name is user-chosen, so this is a naming-hygiene nit, not a silent auth bypass — SEC-7 correctly rejected the bypass framing.)

- **CONS-7 — "Shared budget report" claims validation integration that no milestone scopes**, and `build_budget_report` must unify three incompatible latency evaluators (offline percentile columns in `validation/latency.py`, single-observation runtime monitor in `session/_latency_budget.py`, waterfall `*_to_*_ms` milestones). M8's "builder over journal records" wording structurally excludes the offline path. Add explicit roadmap items: (a) map new flat metric names to the `_budget_matches_stage` matcher; (b) retrofit `validation/latency.py` `budget_violations` onto `build_budget_report`. Strengthen R10 to note it is reconciling existing schemas.

- **TEST-3 — Public-API sketch mislabels net-new helpers as re-exports.** `assert_budgets_pass` and `promote_turn_to_test` do NOT exist (`phase-3:128-138`); only `assert_latency` (different signature) and the CLI `promote_turn` do. Separate "pure re-exports of `easycat.debug.testing`" (`load_bundle`, `assert_no_error`, `assert_tool_called`, `assert_regex`, `assert_exact_match`, `assert_latency`, `assert_turn_completed`) from "net-new, needs full unit tests" (`assert_budgets_pass`, `EvalRunner`/`EvalScenario`/`EvalTurn`, `promote_turn_to_test`, `ScenarioResult`). The import/public-API test should assert both sets explicitly.

- **TEST-2 — Promotion command divergence splits the test surface.** Decide extend-vs-fork (see MF-1). If new, add a test that the generated `.py` actually imports and runs (`pytest` on it in `tmp_path`) and that `easycat.evals` re-exports every symbol the generated file imports.

- **SEQ-4 — M10 promotion duplicates the existing `journal promote`** (`cli/debug/bundles.py:1820`, `--out` writes `.zip` today vs `.py` in the plan). Add an M10 scope line to reconcile; prefer a single `promote` verb.

- **TEST-4 — Text-mode latency budgets can only assert `total_ms`.** Provider-stage budgets (`tts_ttfb_ms`, `llm_ttft_ms`) evaluate against zero samples in the only no-API-key mode and pass vacuously (push-based `violations()` in `session/_latency_budget.py:44-64`; text emits only `total_ms` at `_turn_runner.py:846-853`). Document that text scenarios assert only turn-total + cost budgets, and add a check that a provider-stage budget in a text scenario raises a clear "no samples for stage X" error rather than passing silently.

- **TEST-5 — New `--json` commands won't be covered by the envelope guard** (`tests/cli/test_json_schema.py` is hand-written, no registry walk; `tests/cli/test_app.py:49-115` walks commands but only checks `--help`). Add `test_json_schema.py` to Files-To-Update for Phase 2/3, add explicit cases for `plan`/`eval run`/`eval report`/`eval promote`, and ideally a coverage test that fails when a `--json` command lacks an envelope assertion.

---

## Missing Risks & Open Questions to Add

1. **Add R13 "Public API surface cap exhausted."** `__all__` is at 94/94 (verified). Adding top-level `VoiceApp` forces a deliberate cap bump in `tests/test_public_api.py:126` plus the triple-lock (`__all__` + `LAZY_EXPORTS` + `docs/public-api.md`). Add "raise `__all__` cap" to Phase-1 Files-To-Update. (CONS-6)

2. **Add an open question for the per-connection config strategy.** "How does `VoiceApp` produce a fresh `EasyConfig` per connection — a dedicated `with_transport()` helper, `dataclasses.replace`, or mandate `config_factory`?" with the InitVar/proxy shared-state footgun noted. Recommend mandating `config_factory` (the only mechanism the serve helpers already require safely). (ARCH-6, CONS-8)

3. **Resolve the `default_mode` vs per-method-default and the construction-input precedence questions in Q1's vicinity** — currently neither `open-questions.md` nor `risk-register.md` records them. (DX-2, DX-3)

4. **Reframe R7 (debug autolaunch) as a guarded invariant, not new work.** The protection already exists and is tested (`_autolaunch.py:40-51,70`; `tests/test_dx_helpers.py:603-625`); the real risk is the new `EASYCAT_DEV`/`VoiceApp(dev=True)` opt-in accidentally relaxing it. Assert the dev opt-in is purely additive. (SEC-8)

5. **Fix the validation guard-lane mapping** (`roadmap.md:252-258`): `guard-validation` does not run `tests/evals`/`tests/budgets`, `guard-ops` does not run `tests/debugger`/`tests/transports`, and `guard-docs` (home of `test_public_api.py`/`test_json_schema.py`) is mapped only to M1-2. Map `guard-docs` to every milestone adding a public export or `--json` command (M2, M6, M9, M10); extend `guard-validation`/add `guard-evals` and regenerate via `scripts/regen_guard_commands.py`. (CONS-4, TEST-8)

6. **Add a precedence note for the readiness contract split** so `/health/ready` semantics are annotated per-milestone (M4 = serving/draining/capacity; M6 = manifest-loaded + plan-no-blocking-errors). (SEQ-1)

---

## Strengths to Preserve

- **North-star API is genuinely achievable.** `agent=` accepts a raw OpenAI Agents SDK `Agent` (`config/_factory.py:407` → `integrations/agents/_factory.py:187-196`); `import easycat` is empirically lazy (PEP 562 `__getattr__` at `__init__.py:150-159`); `VoiceApp(agent=Agent(...)).run("browser")` is feasible exactly as written.
- **Layering discipline (Session / EasyConfig / VoiceApp / VoiceServer)** matches the real code split and R1's field-ownership decision rule is the right guardrail.
- **Replay tool-DENY-by-default is real and enforced** (`runtime/replay.py:141,478-484`; `cli/debug/bundles.py:1730-1735`); `easycat.evals` inherits it for free. The strongest code-backed security guarantee — keep verbatim.
- **Observability allow-list is an enforced PII firewall** (`_observability.py:78-111,199,209-210`); new server metrics must go through the same sanitizer, never a bypass.
- **Constant-time `hmac.compare_digest`** is already the standard across all five auth surfaces — reuse, don't reinvent.
- **`debug='full'` alone never autolaunches** — already implemented and tested; preserving it as an acceptance gate is correct.
- **Text-first evals build on real primitives** (`create_text_session`/`run_text_turn`/`assert_*` operating on a journal-backed `TurnResult`), so the no-API-key acceptance criterion is testable today.
- **Redaction is mature for structured artifacts** (`validation/redaction.py`, `REDACTION_VERSION=1`); `bundles export` (`include_audio=False` default + `_assert_context_pack_redacted`) is the exemplary model for `eval promote` to copy.
- **Phase-3 PR-slice ordering (budgets → evals → promotion → runtime metrics → dev registry → UI)** maps cleanly to M8-M12 and respects the real `budgets-before-evals` dependency.

---

## Sequencing Note

The roadmap's overall ordering is sound (VoiceApp before VoiceServer.from_app; budgets before evals; Twilio and dev-debugger correctly deferred), and the low-risk re-export shims are correctly front-loaded. The sizing, however, is uneven and concentrated in Phase 2: **M6 (Manifest+Planner) is both the most under-sized milestone (≥4 PRs as one) and the single riskiest** (it is the trust anchor for `/health/ready`, and R6 planner-divergence is amplified because 5 of 7 roles have no catalog metadata to read) — split it into M6a/M6b and gate readiness on a planner-vs-`create_session` parity test. **M7 (WebRTC-in-VoiceServer) is the second under-sized milestone**, hiding a route-handler decoupling + bundled-client migration behind one paragraph. M4's readiness contract has a soft backward dependency on M6 that should be made explicit. Cut Line 1 ("defer Twilio from Phase 1") is a no-op against the existing M1→M3 sequence and should be retargeted to "defer all of M3." Fix M6 sizing and the Phase-2 reuse-narrative corrections first; the rest of the sequence holds.
