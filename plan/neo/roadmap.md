# Neo Roadmap

Status: active sequencing proposal.

This roadmap converts the phase plans into reviewable PR slices. The ordering is
optimized for early user value, low abstraction risk, and testability.

## Dependency Map

Milestone numbering note (read this before cross-referencing siblings):
Milestone 6 is split into **M6a** (manifest loader) and **M6b** (provider
planner). The previously-implicit "server metrics completion" work is now its
own **owning milestone (M8)**, inserted immediately after **M7
(WebRTC-in-VoiceServer)** — the phase-2 plan calls this milestone **M8** ("Server
Metrics + Endpoints"). Inserting M8 shifts everything that followed the old M6 by
one: old M8 (Budgets)→M9, old M9 (Evals)→M10, old M10 (Promotion)→M11, old M11
(Runtime Budget)→M12, old M12 (Dev Debugger)→M13. WebRTC keeps the number 7
(it is the second-most under-sized milestone). The result is a contiguous
M1–M5, M6a, M6b, M7–M13. The map, the per-milestone sections, the Validation
Strategy table, and every README/phase cross-reference use these numbers.

```text
VoiceApp (M1–M3)
  ├─ easycat serve migration (M2: top-level VoiceApp export → __all__ cap 94→95)
  ├─ Twilio server extraction (M3)
  └─ feeds VoiceServer config_factory (NOT VoiceApp.run); see D4 loop-ownership

VoiceServer (M4–M8)
  ├─ health/readiness (M4 owns serving/draining/capacity/route-ready ONLY)
  ├─ shared auth (M5: unified AuthPolicy + non-loopback guard for WS *and* WebRTC)
  ├─ graceful shutdown + capacity (M5: lifted out of serve helpers, net-new shared layer)
  ├─ manifest loader (M6a: highest-risk)
  ├─ provider planner (M6b: highest-risk; readiness=manifest-loaded + plan-no-blocking-errors)
  │     └─ GATE: planner-verdict-matches-create_session parity test (all 7 roles)
  ├─ WebRTC-in-VoiceServer (M7: second most under-sized; route decouple + client migration)
  └─ server metrics + /metrics,/manifest,/plan,/capabilities (M8: registers
        easycat.server.* names + labels in _observability.py allow-lists)

Budgets + Evals (M9–M13)
  ├─ budgets public API (M9: reconciles THREE latency vocabularies, not additive)
  ├─ scenario runner (M10: eval run + eval report)
  ├─ replay promotion (M11: redact-by-default; fork `eval promote`, legacy `journal promote` unsafe)
  ├─ runtime budget coverage (M12: map flat metric names → _budget_matches_stage)
  └─ debugger overlays/dev mode (M13: EASYCAT_DEV opt-in is purely additive)
```

Readiness-contract split (D5 / SEQ-1): `/health/ready` is owned in two pieces.
**M4** asserts serving + draining + capacity + route-ready. **M6b** adds
manifest-loaded + plan-has-no-blocking-errors, gated behind the planner parity
test. M4 must not import the planner; the M6b checks are layered on later.

Capacity sub-dependency (D5 / D8): M4's "below capacity" readiness check reads a
**minimal capacity counter** (active count vs `max_sessions`) that M4 ships
itself; the full `Semaphore`/active-set/draining **machinery is lifted into the
shared layer in M5**, not M4. M5 replaces the counter with the lifted
collaborator without changing the readiness contract, so the M4 check must not be
implemented against the M5 collaborator (which does not exist until M5).

## Milestone 0 — Planning Packet

Deliverables:

- `plan/neo/*` planning assets.
- Agreement on architecture boundaries.
- Agreement on Phase 1 first PR slice.

Exit criteria:

- Maintainers can identify which files to add/change.
- Acceptance criteria are explicit.
- Open questions are recorded.

## Milestone 1 — Minimal VoiceApp

Scope:

- Add `src/easycat/voice_app.py`.
- Support `local`, `browser`, and `websocket` modes.
- Support high-level `agent=...`, static `EasyConfig`, and `config_factory`.
  **`config_factory` is the ONLY safe per-connection config mechanism** (D1);
  its canonical signature is the per-transport factory shape
  `Callable[[TransportT], EasyConfig]` (`TransportT` is the concrete transport:
  `WebRTCTransport`, `WebSocketConnectionTransport`,
  `WebTransportConnectionTransport`, `TwilioConnectionTransport`). There is **no**
  `ConnectionContext` type and **no** `with_transport`/`dataclasses.replace`
  clone path — grouped sub-configs (`observability`/`audio_processing`/
  `session_policy`) are shared by reference, so naive cloning flips the original
  and does NOT isolate concurrent sessions.
- Apply the **construction rules** (D3): delete the dead `default_mode` field
  (one grep hit repo-wide, never read); enforce a `config` vs `config_factory`
  vs high-level-fields **mutual-exclusion** rule that raises `ValueError` naming
  the conflict; enforce a **field allow-list** (which `EasyConfig` fields
  `VoiceApp` forwards via `**config_kwargs` — `agent`, `stt`, `tts`, `vad`,
  `debug`, plus mode-appropriate transport/auth — vs which `VoiceApp` owns:
  `dev`). The 5-field `@dataclass` sketch is replaced because it cannot accept
  `stt=`/`tts=` (TypeError). Add a test that enforces the allow-list.
- Apply the **event-loop ownership rule** (D4): `run()` is the only method that
  calls `asyncio.run()`; use `serve()` as the async verb (drop the asymmetric
  `serve_forever`); `session(mode=...)` returns an un-started, caller-owned
  `Session` and is constrained to single-session modes (`local`), raising for
  server/multi-session modes (`browser`/`websocket`/`twilio`).
- Add mode alias normalization.
- Add unit tests for config construction, the mutual-exclusion/allow-list rules,
  and delegation.

Out of scope:

- Twilio mode.
- Manifest loading.
- Dev debugger autolaunch.

Suggested PR title:

```text
app: add VoiceApp run modes
```

## Milestone 2 — Serve Command Uses VoiceApp

Scope:

- Migrate `easycat serve` to construct a `VoiceApp`.
- Add `--mode` with default `browser`.
- Preserve host/port/token behavior. The shipped auth env var is
  `EASYCAT_SERVE_TOKEN` (`cli/serve.py:36,106`) — keep this exact name through
  the VoiceApp migration; do **not** introduce `EASYCAT_SERVER_TOKEN` (D2).
- Update CLI tests.
- Update README/browser-playground docs.
- Export top-level `VoiceApp` and apply the **public-API triple-lock + cap
  bump** in the *same* PR (D9, R13): the top-level `easycat.__all__` is at 94/94
  and `tests/test_public_api.py:126` asserts `len(__all__) <= 94`, so this
  milestone must raise the cap 94→95 **and** update all three locks together —
  `__all__`, `LAZY_EXPORTS`, and `docs/public-api.md` (refresh
  `PUBLIC_API_SNAPSHOT`). Note `VoiceServer`/`EvalRunner`/`CostBudget` are
  *submodule* exports (`easycat.server`, `easycat.evals`, `easycat.budgets`) and
  do **not** count against this top-level cap — only top-level `VoiceApp` does.

Guard lane: `just guard-docs` is **required** for this milestone (it is the home
of `test_public_api.py` and `test_json_schema.py`); the targeted lane below is
not sufficient on its own.

Suggested PR title:

```text
cli: route serve through VoiceApp
```

## Milestone 3 — Twilio Mode

Scope:

- Extract reusable Twilio server helper from the example shape.
- Add `VoiceApp.run("twilio")`.
- Add TwiML/token/media lifecycle tests with fakes.
- Simplify or add a `VoiceApp` Twilio example.

Suggested PR title:

```text
telephony: add VoiceApp Twilio server mode
```

## Milestone 4 — VoiceServer Skeleton

Scope:

- Add `easycat.server` package (net-new submodule).
- Add `VoiceServerConfig`, `VoiceServer`, `VoiceServerHealth`.
- Use `aiohttp`.
- Add `/health/live`, `/health/ready`, `/health`. **M4 owns the
  serving/draining/capacity/route-ready half of `/health/ready` ONLY** (D5 /
  SEQ-1). It must NOT import the planner; the manifest-loaded +
  plan-has-no-blocking-errors half is layered on in M6b behind the parity gate.
- Support a WebSocket route first.
- Use `SessionManager` for the bare session registry only.
  **`SessionManager` is `add/remove/stop_all/connection` with no capacity/
  draining** (`session_manager.py:18-105`); do not attribute session-limit or
  draining behavior to it (D18). Capacity/draining are lifted into shared
  internals in M5.
- Note the unified endpoint table is a **logical surface listing, not an aiohttp
  route manifest** (D17): `/twilio/media` and `/ws` are raw `websockets.serve`
  listeners, not aiohttp routes.
- Add lifecycle/health/auth-free tests.

Suggested PR title:

```text
server: add VoiceServer skeleton
```

## Milestone 5 — Auth, Capacity, Shutdown

Scope:

- Add a unified `AuthPolicy` layer shared by **both** WebSocket and WebRTC
  transports (D7). Add an explicit `unsafe_allow_no_auth: bool = False` field
  on `AuthPolicy` (and/or `VoiceServerConfig`) as the **only** escape hatch —
  the non-loopback-requires-token guard must be a structured field, not prose.
  This **closes a real `0.0.0.0` unauthenticated voice endpoint**: today
  WebSocket has no loopback guard (`websocket.py:84,93,100-111`) while WebRTC
  does (`webrtc.py:347-351,924-927`). Acceptance: a non-loopback WebSocket bind
  with no token and without `unsafe_allow_no_auth` **raises**.
- Add `allow_query_token: bool = False` to the auth layer (D14). `?token=` is
  unconditional today whenever a token is set (`webrtc.py:826-834`,
  `websocket.py:102-111`), so default-off is a **breaking change** for the
  WebSocket browser client (`examples/ws_browser_client.html:91-93` — browsers
  cannot set headers on the WS handshake); the bundled WebRTC client is
  unaffected (it sends `Authorization: Bearer`). Document the WS-client break and
  ship the `allow_query_token=True` loopback opt-in.
- Add capacity rejection and draining state. **Net-new at the shared layer**:
  capacity/draining are NOT in `SessionManager` (a bare
  `add/remove/stop_all/connection` registry, `session_manager.py:18-105`); they
  live inline in the serve helpers (`webrtc.py:354,356-357,377-390,422-434,475-482`;
  `websocket.py:146,154-156,177-179`) and must be **lifted** (Semaphore /
  active-set / draining state) into shared VoiceServer internals (D18).
- Add graceful shutdown and forced escalation.
- Add request/session metrics **skeleton only** — the metric names and labels
  are NOT registered here. Registration of `easycat.server.*` in
  `METRIC_DEFINITIONS` and the new labels in `LOW_CARDINALITY_ATTRIBUTE_KEYS`
  lands in **M8** (D8), in the same PR that first emits them, because
  `_record_metric`/`sanitize_attributes` raise `ValueError` on any unregistered
  name/key (`_observability.py:199,209-210`). Do not emit unregistered names from M5.
- Add tests for auth/capacity/shutdown (including the non-loopback-raises and
  `allow_query_token` default-off cases).

Suggested PR title:

```text
server: add auth and graceful draining
```

## Milestone 6a — Manifest Loader (HIGHEST RISK)

> M6 is split (D5 / MF-5 / SEQ-2): the original single milestone bundled ≥4 PRs
> of mostly net-new work. M6a and M6b are jointly the **highest-risk
> milestones** in this roadmap (see R6). Nothing here may land before its
> acceptance gate in M6b passes — the readiness wiring is deferred to M6b.

Scope:

- Add `easycat.project.ProjectManifest` (net-new package; no loader exists today
  — grep finds no `bearer-env`/`tomllib`/`ProjectManifest`).
- Add manifest discovery/loading/validation via `tomllib`.
- Add the net-new `python:module:function` agent resolver and transport string
  shortcuts (both net-new; D18).
- Add transport-registry metadata (net-new declarative source for the transport
  role; see M6b — transport has no static catalog).
- Convert `easycat.toml` profiles to `EasyConfig` profiles.
- **Secret redaction is a testable contract (D15 / SEC-6):** `auth`/`token`
  fields MUST match an env-reference grammar (`bearer-env:NAME`, NAME a chosen
  env identifier; the manifest examples use `bearer-env:EASYCAT_SERVE_TOKEN` for
  consistency but the loader accepts any env name). The loader MUST **raise a
  coded error** (`EASYCAT_Exxx`) when it sees a literal-looking secret (reuse
  `redaction._SECRET_RE` / `contains_unredacted_sensitive_text`); any
  echoed/dumped manifest routes through `redact_value`.

Acceptance (M6a-owned, also tracked in the acceptance matrix):

- A manifest with a literal secret in `auth`/`token` is **rejected** with the
  coded error.
- The `--json` / `/manifest` dump shows no resolved token value.

Guard lane: `just guard-ops` plus targeted `tests/project` (manifest discovery
touches no public export or `--json` surface yet — those land in M6b, which then
requires `just guard-docs`).

Suggested PR title:

```text
project: add manifest loader and secret-redaction contract
```

## Milestone 6b — Provider Planner + Readiness Wiring (HIGHEST RISK)

Scope:

- Add `ProviderPlan` and missing env/extra detection.
- **Catalog reuse is STT/TTS ONLY (D19 / ARCH-5).** Only STT/TTS have a
  `ProviderCatalog` (`_provider_catalog.py:1-2,285-353`). The other five roles
  need **net-new declarative metadata** because they have no static catalog:
  VAD resolves by try/except with extras in an error string
  (`vad/factory.py:91-151`); transport is config-type dispatch
  (`config/_factory.py:110-139`); noise/echo have hardcoded extras only
  (`noise_reduction.py:40,96`, `echo_cancellation.py:11,90`); agent has no
  catalog; `ProviderSelection.capabilities` has no static source
  (`validation/provider_capabilities.py:2-5` is a live-derived report). Do NOT
  describe this as uniform "extract shared catalog metadata."
- Add `easycat plan --json`.
- Wire `/health/ready` to **manifest-loaded + plan-has-no-blocking-errors**
  ONLY (the serving/draining/capacity/route-ready half is owned by M4; D5).

Required acceptance gate (blocks the readiness wiring):

- **Planner-vs-`create_session` parity test:** the planner verdict must match
  the `create_session` outcome for every one of the **7 roles** (stt, tts, vad,
  transport, agent, noise_reducer, echo_canceller). Because five roles have no
  shared metadata, the divergence surface is large (this is why R6 is amplified).
  The manifest/plan readiness checks in `/health/ready` are **gated behind this
  parity test passing** — they may not ship until parity is green.

Guard lane: `just guard-docs` is **required** (adds the `easycat plan --json`
envelope, covered by `tests/cli/test_json_schema.py`).

Suggested PR title:

```text
project: add provider planning and readiness wiring
```

## Milestone 7 — WebRTC in VoiceServer (SECOND MOST UNDER-SIZED)

> This milestone hides real decoupling work behind one paragraph (SEQ /
> Sequencing Note). Today the WebRTC handlers are bound to a throwaway
> `shim = WebRTCTransport(settings)` (`webrtc.py:359`) and hardcode flat routes;
> the bundled client targets the flat paths
> (`webrtc_client.html:301,425,455`). Expanding the scope explicitly:

Scope:

- **Decouple the route handlers off `WebRTCTransport`** — extract the
  `/offer`, `/config`, `/stats` handler logic so it does not require constructing
  a per-request `WebRTCTransport` shim, so it can be mounted by the shared
  `VoiceServer` route internals.
- Decide flat-vs-namespaced paths and **own the bundled-client migration cost**:
  if routes move under a namespace, migrate `webrtc_client.html:301,425,455`
  (and the `?token=`/`allow_query_token` handling from M5) in the same change.
- Mount WebRTC `/offer`, `/config`, `/stats`, and the static client through
  `VoiceServer` or shared route internals.
- Preserve the existing WebRTC helper API (out-of-tree callers).
- Apply the unified `AuthPolicy` (M5) to the mounted WebRTC routes.
- Add integration tests with aiohttp test utilities.

Suggested PR title:

```text
server: mount WebRTC voice sessions
```

## Milestone 8 — Server Metrics + Endpoints

> Renumbered metrics-completion milestone (D8 / MF-6). M5 added only a metrics
> *skeleton*; this milestone is the **owning milestone** that makes server
> metrics emittable and completes the four read-only endpoints. None of the
> four endpoints exist today.

Scope:

- **Register in the same PR that first emits them** (D8): add the new
  `easycat.server.*` names to `METRIC_DEFINITIONS` and the new labels
  `easycat.server_state`, `easycat.auth_result`, `easycat.route` to
  `LOW_CARDINALITY_ATTRIBUTE_KEYS` in `_observability.py`. Unregistered
  names/keys raise `ValueError` (`_observability.py:199,209-210`).
  `easycat.transport` is **already registered** (`:59`) — do not re-add it.
- **Constrain `easycat.route` to an enumerated set of route TEMPLATES** (assert
  the value is in the template set before recording) so a raw path with user
  content can never be emitted (PII/cardinality hazard; SEC-5).
- Complete the read-only endpoints: `GET /metrics`, `GET /manifest`,
  `GET /plan`, `GET /capabilities`.
- Add a test that the server emits through `sanitize_attributes` (never a
  bypass of the PII firewall).

Acceptance rows (add to the acceptance matrix; none exist today): `/metrics`,
`/manifest`, `/plan`, `/capabilities` each return 200 + the documented JSON
shape; `/manifest` and `/plan` dumps show no resolved token value.

Guard lane: `just guard-ops` (server/observability) plus `just guard-docs` for
the `--json` envelopes on `/plan` and `easycat plan`.

Suggested PR title:

```text
server: register metrics and complete read-only endpoints
```

## Milestone 9 — Budgets Public API

> Renumbered from old M8.

Scope:

- Add `easycat.budgets` package (net-new submodule).
- Re-export/alias `LatencyBudget`.
- Add `CostBudget` model (**net-new value object** — referenced in the
  `EvalScenario` sketch as if existing; it does not exist today; D18).
- Add `build_budget_report` / `BudgetReport` (**net-new**; D18). Its scope is
  NOT only "builder over journal records": it must **reconcile three distinct
  latency vocabularies, not naively add** (D11 / CONS-7):
  1. runtime emits only `stage="total_ms"`
     (`session/_turn_runner.py:686-696,847-853`);
  2. offline-validation columns `tts_ttfb_ms` / `llm_ttft_ms`
     (`validation/latency.py:113-114,308-314`);
  3. waterfall milestone names `*_to_*_ms`
     (`debug/_turn_timeline.py:325-336`, `cli/debug/bundles.py:1448-1452`).
  Widen the "builder over journal records" wording to also cover the offline
  percentile path, and **retrofit `validation/latency.py` `budget_violations`
  onto `build_budget_report`** so the offline path is not structurally excluded.
- Preserve `max_session_cost_usd` alias.
- Add tests for serialization, aliases, and report generation across all three
  latency sources.

Guard lane: `just guard-docs` is **required** (new `easycat.budgets` submodule
exports + serialization assertions live alongside the public-API guard).

Suggested PR title:

```text
budgets: add shared budget API
```

## Milestone 10 — Evals Public API

> Renumbered from old M9.

Scope:

- Add `easycat.evals` package (net-new submodule).
- **Re-export existing debug testing helpers** (pure re-exports, no new units):
  `load_bundle`, `assert_no_error`, `assert_tool_called`, `assert_regex`,
  `assert_exact_match`, `assert_latency`, `assert_turn_completed` (D13).
- Add **net-new** symbols needing full unit tests: `EvalScenario`, `EvalTurn`,
  `EvalRunner`, `ScenarioResult`, `assert_budgets_pass` (does not exist today —
  only `assert_latency` with a different signature does; D13).
- Implement text-first scenario runner. **Text scenarios emit only `total_ms`**
  (`_turn_runner.py:846-853`), so they may assert turn-total + cost budgets
  only; a provider-stage budget (`tts_ttfb_ms`, `llm_ttft_ms`) in a text
  scenario must **raise a clear "no samples for stage X" error**, not pass
  vacuously (D21 / TEST-4).
- Add CLI `easycat eval run` (net-new).
- Add CLI `easycat eval report` — mirrors `validate report`
  (`cli/validate.py:676,833`); it must emit a JSON report envelope
  (`schema_version=1`) covered by `tests/cli/test_json_schema.py` (D20 / CONS-3).
- Add docs and scaffold test pattern.

Guard lane: `just guard-docs` is **required** (new submodule exports + the
`eval run` / `eval report` `--json` envelopes); add explicit
`tests/cli/test_json_schema.py` cases for `eval run` and `eval report`.

Suggested PR title:

```text
evals: add scenario runner
```

## Milestone 11 — Replay Promotion

> Renumbered from old M10. This milestone is **hardening, not preservation**
> (D6 / MF-1, CRITICAL privacy). The existing path
> (`journal promote` → `slice_bundle_by_turn` → `debug/export.py:154-170`) copies
> full raw NDJSON + every audio blob + the verbatim transcript into a committed
> file with **zero** redaction. State this plainly here, in R8, and in Q14.

Scope:

- Add a `promote_turn_to_test` promotion library (net-new) plus
  `eval promote` — **fork a new `eval promote` command** rather than silently
  extending `journal promote` (leaving both unreconciled is a footgun). Reconcile
  the two verbs: document `journal promote` as the **unsafe legacy path** and
  `eval promote` as the hardened replacement. Note the existing `journal promote`
  writes a `.zip` via `--out` today (`cli/debug/bundles.py:1820`) vs the `.py`
  stub the plan targets — state the extend-vs-fork decision explicitly.
- Reuse `_promote_test_stub` / `_validate_promoted_slice` rather than
  re-implementing.
- Generate pytest skeletons. The record-assertion mode **defaults to assert on a
  hash/regex** over the reply rather than embedding raw transcript text (D6),
  because redaction is field-name + secret-regex only (no NER), so a transcript
  that is itself the assertion target cannot be both redacted and useful.
- **Redact by default + `--allow-pii` tripwire** (replaces "warn about PII"):
  route records through `redact_value` before serialization (redact-by-default);
  add a `contains_unredacted_sensitive_text()` tripwire gated by `--allow-pii`
  (mirror `_assert_context_pack_redacted`).
- **`--no-audio` is the DEFAULT** (mirror `bundles export` at `bundles.py:1160`);
  tool replay denied by default (`ReplaySpec.tool_policy=DENY`).
- Add debugger API endpoint for promotion.

Guard lane: `just guard-docs` is **required** (new `eval promote` `--json`
envelope + `easycat.evals` re-exports); add a `tests/cli/test_json_schema.py`
case for `eval promote`, and a test that the generated `.py` actually imports and
runs (`pytest` on it in `tmp_path`).

Suggested PR title:

```text
evals: promote bundle turns to tests
```

## Milestone 12 — Runtime Budget Coverage

> Renumbered from old M11.

Scope:

- Emit first-token and first-audio latency metrics where available. **These are
  not a clean additive set** (D11 / correction-4): `tts_ttfb_ms` / `llm_ttft_ms`
  are renames/lifts of existing offline columns + waterfall milestones, while
  `stt_final_latency_ms` / `vad_endpointing_ms` / `first_audio_ms` /
  `barge_in_ack_ms` are net-new at runtime (zero hits in `src/`) but have
  equivalent waterfall milestones to map from.
- **Map the new flat metric names to `_budget_matches_stage`** so the runtime
  monitor recognizes them (D11). Confirm the offline percentile path retrofit
  from M9 (`build_budget_report`) covers the same names.
- Feed runtime milestones into the budget monitor.
- Add budget violations to journal/issue rollups.
- Add tests for metric records, the stage-name mapping, and budget-exceeded
  records.

Suggested PR title:

```text
runtime: record first audio budget milestones
```

## Milestone 13 — Dev Debugger Mode

> Renumbered from old M12.

Scope:

- Add dev debugger policy. The new `EASYCAT_DEV` / `VoiceApp(dev=True)` opt-in
  must be **purely additive** (D16 / R7): the no-autolaunch protection already
  exists and is tested (`_autolaunch.py:40-51,70`; `tests/test_dx_helpers.py:603-625`).
  The opt-in must never weaken the `debug='full'`-alone-never-autolaunches
  guarantee.
- Add session registry.
- Add single debugger per process.
- Add live session selector.
- Add budget overlays.
- Add promote-to-test button.
- **Make the dev acceptance criterion testable** (D16): assert the
  registry/launch is invoked (not that a browser literally opens, since CI is
  non-interactive).

Suggested PR title:

```text
debugger: add dev session dashboard
```

## Validation Strategy

Each milestone runs targeted tests plus the relevant guard lane. The mapping
below is corrected against what each lane actually runs (D12 / missing-risk-5 /
CONS-4 / TEST-8). Two facts drive the corrections:

- `just guard-docs` is the **only** lane that runs `tests/test_public_api.py`
  and `tests/cli/test_json_schema.py`, so it is **required for every milestone
  that adds a public export or a `--json` command** — not just M1–M2.
- `just guard-validation` does **NOT** run `tests/evals` or `tests/budgets`, and
  `just guard-ops` does **NOT** run `tests/debugger` or `tests/transports`. Map
  those test dirs to a lane that actually runs them, or add a new lane.

Any guard-lane membership change (e.g. extending `guard-validation` to include
`tests/evals`/`tests/budgets`, or adding a new `guard-evals` lane) must be
regenerated via `uv run python scripts/regen_guard_commands.py`.

| Milestone | Targeted tests | Guard lane |
|---|---|---|
| 1 | `tests/test_voice_app.py` | `just guard-docs` (public-API allow-list) |
| 2 | `tests/cli/test_serve.py`, `tests/test_public_api.py` | `just guard-docs` (**required** — `__all__` cap bump + triple-lock) |
| 3 | `tests/telephony/test_voice_app_twilio.py` | `just guard-examples` + targeted telephony |
| 4 | `tests/server`, `tests/transports` | `just guard-ops` + targeted `tests/transports` |
| 5 | `tests/server` (auth/capacity/shutdown) | `just guard-ops` + targeted `tests/transports` (WS/WebRTC auth) |
| 6a | `tests/project` (manifest loader, redaction contract) | `just guard-ops` + targeted `tests/project` |
| 6b | `tests/project` (planner parity), `tests/cli/test_json_schema.py` | `just guard-docs` (**required** — `easycat plan --json`) |
| 7 | `tests/server`, `tests/transports` (WebRTC mount) | `just guard-ops` + targeted `tests/transports` |
| 8 | `tests/server`, `tests/observability`, `tests/cli/test_json_schema.py` | `just guard-ops` + `just guard-docs` (`/plan` envelope) |
| 9 | `tests/budgets`, `tests/validation` (latency) | `just guard-docs` (**required** — submodule exports) + extend `guard-validation`/add `guard-evals` to run `tests/budgets` |
| 10 | `tests/evals`, `tests/cli/test_json_schema.py` | `just guard-docs` (**required** — `eval run`/`eval report` envelopes) + lane that runs `tests/evals` |
| 11 | `tests/evals` (promotion), `tests/cli/test_json_schema.py` | `just guard-docs` (**required** — `eval promote` envelope) + lane that runs `tests/evals` |
| 12 | `tests/validation` (latency), `tests/runtime` | `just guard-validation` (latency/validation) |
| 13 | `tests/debugger`, `tests/test_dx_helpers.py` | lane that runs `tests/debugger` (`guard-ops` does NOT — map/extend accordingly) |

## Cut Lines

If scope gets too large, cut in this order:

1. Defer all of M3 (Twilio mode) until after WebRTC/WebSocket are stable.
   (Retargeted per the Sequencing Note: "defer Twilio from Phase 1" was a no-op
   against the existing M1→M3 sequence, since M1/M2 already exclude Twilio.)
2. Defer WebSocket static browser client.
3. Defer Prometheus text output; keep JSON/OTel-compatible metrics first.
4. Defer audio simulation; ship text scenarios first.
5. Defer full debugger UI polish; ship API + minimal UI affordance first.

## Release Narrative

When Neo is ready, the release story should be:

- `VoiceApp` is the new app-first way to build EasyCat apps.
- `easycat serve` is browser-first and production-capable, and the server layer
  closes the `0.0.0.0` unauthenticated WebSocket gap that exists today.
- `easycat.toml` makes projects deployable and inspectable, with secrets kept as
  env references and never resolved into dumps.
- Debugger timelines and evals turn voice bugs into tests — and replay promotion
  is **redact-by-default** (a hardening of the existing unsafe `journal promote`
  path, which ships raw transcripts + audio today), not a like-for-like lift.
