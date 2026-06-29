# Neo Risk Register

Status: active planning risk log.

> **Stale (runtime budgets removed):** the runtime cost/latency-*budget* code
> some risks below reference as existing — `LatencyBudgetMonitor`
> (`session/_latency_budget.py`), `CostBudgetEnforcer` (`session/_cost_budget.py`),
> `cost_budget_*` records, and the debugger `/api/cost` rollup — was removed as
> undercooked and duplicative with the journal. Those entries are now historical
> / net-new. The offline `easycat.validation.latency.LatencyBudget` (validation
> lane) is a separate, still-existing symbol.

## Summary

Neo touches user-facing APIs, server runtime, observability, evals, and docs.
The risks below should be reviewed before each milestone and updated when a
mitigation lands.

## Risks

### R1 — `VoiceApp` duplicates `EasyConfig`

**Risk:** `VoiceApp` grows provider/audio/telephony fields until it becomes a
parallel config system.

**Impact:** Divergent behavior, duplicated validation, confusing docs.

**Mitigation:** Keep `VoiceApp` as orchestration. It builds or clones
`EasyConfig` and delegates to `create_session`.

**Decision rule:** If a field controls one session’s provider/pipeline behavior,
it belongs in `EasyConfig` or a grouped config dataclass. If it controls how an
app is run, it belongs in `VoiceApp`.

**Construction-input precedence (resolve before coding):** The current
`phase-1-voice-app.md` sketch is internally inconsistent and must be specified
first. The 5-field `@dataclass` sketch cannot accept the documented
`VoiceApp(agent=..., stt="openai/realtime", tts="openai")` call (it has no
`stt=`/`tts=` fields, so the call raises `TypeError`), and the three
construction inputs — `config`, `config_factory`, and the high-level fields
(`agent`/`stt`/`tts`/`vad`/`debug`) — have no precedence or mutual-exclusion
rule, so `VoiceApp(agent=a, config=EasyConfig.browser(agent=b))` is a real,
undefined conflict. Per cross-cutting decision **D3**:

- Define `config` vs `config_factory` vs high-level-fields as **mutually
  exclusive**; supplying more than one raises `ValueError` naming the conflict.
- Define a **field allow-list** of which `EasyConfig` fields `VoiceApp` forwards
  into the chosen preset via `**config_kwargs` (`agent`, `stt`, `tts`, `vad`,
  `debug`, plus mode-appropriate transport/auth fields) vs which `VoiceApp` owns
  (`dev`). The allow-list is enforced by a test.
- **Delete `default_mode`** — it is a dead field (one grep hit repo-wide, never
  read; each method keeps its own default).

See the new open question on construction-input precedence in
`open-questions.md`.

### R2 — `VoiceServer` becomes a new pipeline runtime

**Risk:** Server code starts constructing providers or managing turn/audio logic.

**Impact:** Divergence from `Session`, harder testing, inconsistent behavior.

**Mitigation:** `VoiceServer` accepts factories returning `EasyConfig` or
`Session`. If it gets `EasyConfig`, it calls `create_session`.

### R3 — Multi-session shutdown races

**Risk:** Server calls `SessionManager.stop_all()` while active connection tasks
are adding/removing sessions.

**Impact:** Orphaned sessions, noisy teardown, flaky tests.

**Mitigation:** `VoiceServer` owns connection tasks, marks draining, stops
listeners, waits/cancels handlers, then drains sessions.

### R4 — Auth asymmetry: WebSocket has no non-loopback guard (and query-token default change is a WS-client break)

**Severity:** HIGH (open `0.0.0.0` unauthenticated voice endpoint today).

**Risk:** Two distinct problems sit under "auth defaults":

1. **Asymmetry that closes a real hole.** WebRTC enforces the
   non-loopback-requires-token guard today (`transports/webrtc.py:347-351,924-927`
   raise `ValueError`), but WebSocket does **not** — `auth_token` defaults `None`
   (`transports/websocket.py:84`), host is overridable via `EASYCAT_WS_HOST`
   (`:93`), and `websocket_server_authorized` returns `True` whenever the token
   is `None` (`:100-111`). A `0.0.0.0` unauthenticated voice endpoint is
   reachable today. The plan's prior R4/Q10 mitigation was **unbacked on the WS
   path** and never flagged the asymmetry — so this is closing a real gap, not
   preserving parity.
2. **Query-token default change breaks the WS browser client.** `?token=` query
   auth is **unconditional** today whenever a token is set (WebRTC
   `webrtc.py:826-834`, WebSocket `websocket.py:102-111`). Making
   `allow_query_token` default `False` (the correct default-off posture) is a
   **breaking change** for the bundled WebSocket browser client
   (`examples/ws_browser_client.html:91-93` — browsers cannot set headers on the
   WS handshake). The bundled WebRTC client is **unaffected** (it sends
   `Authorization: Bearer`).

**Impact:** Unauthenticated voice endpoint exposure (1); silent regression of the
WS browser demo (2).

**Mitigation (per D7 + D14):**

- Make the non-loopback-requires-token guard a **property of the unified
  `VoiceServer`/`AuthPolicy` layer applied to BOTH transports**, with an explicit
  structured field `unsafe_allow_no_auth: bool = False` on `AuthPolicy` (and/or
  `VoiceServerConfig`) as the ONLY escape hatch — a structured field, not
  prose-only. Acceptance test: a non-loopback WebSocket bind with no token and
  without `unsafe_allow_no_auth` **raises** (`phase-2-voice-server.md` AuthPolicy
  sketch must add the gate; today it lives only in prose).
- Default `allow_query_token=False`; confirm the new default applies to the
  EXISTING handlers too, document the WS-client break in the plan, and provide
  the `allow_query_token=True` **loopback** opt-in for local browser demos.
- Keep loopback/dev no-auth defaults where safe (loopback binds remain usable
  without a token).

### R5 — Secret leakage in planning/metrics

**Risk:** Provider planning, the project manifest, or server metrics expose
tokens, phone numbers, headers, session IDs, route paths, or transcript text.

**Impact:** Security/privacy regression.

**Mitigation:** Three concrete, testable controls:

1. **Manifest secret rule made testable (per D15).** No loader exists today
   (grep finds no `bearer-env`/`tomllib`/`ProjectManifest`), so this is net-new.
   `auth`/`token` fields MUST match an env-reference grammar (`bearer-env:NAME`
   where NAME is an env-var identifier); the loader MUST RAISE a coded error
   (e.g. `EASYCAT_Exxx`) when it sees a literal-looking secret — reuse
   `redaction._SECRET_RE` / `contains_unredacted_sensitive_text()`. Any
   echoed/dumped manifest (`--json`, `/manifest`) routes through `redact_value`.
   Acceptance tests: (a) a literal secret is rejected; (b) the `--json` /
   `/manifest` dump shows no resolved token value.
2. **Server metric/label registration cannot be bypassed (per D8/MF-6).** New
   `easycat.server.*` metric names MUST be registered in `METRIC_DEFINITIONS`
   and new labels (`easycat.server_state`, `easycat.auth_result`,
   `easycat.route`) in `LOW_CARDINALITY_ATTRIBUTE_KEYS` in `_observability.py`
   **in the same PR that emits them** — `_record_metric` / `sanitize_attributes`
   raise `ValueError` on any unregistered name/key (`_observability.py:199,209-210`).
   `easycat.transport` is ALREADY registered (`:59`) — do not re-add. New
   metrics must flow through the same sanitizer, never a bypass.
3. **`easycat.route` is a latent PII/cardinality hazard (per D8/SEC-5).** The
   key-name allow-list cannot distinguish a route *template* from a raw path, so
   a raw path carrying user content could leak as a label and explode
   cardinality. Constrain `easycat.route` to an ENUMERATED set of route
   **templates** and assert the value is in the template set **before** recording.

Continue to use env-var references in manifests and allow only low-cardinality
safe metric labels everywhere else.

### R6 — Provider planner diverges from provider factories

**Severity:** HIGH — and the divergence surface is **large**.

**Risk:** `easycat plan` says a config is valid, but `create_session` later
fails; or vice versa.

**Why the surface is large:** Only **2 of 7 roles** (STT, TTS) have shared
catalog metadata the planner can reuse (`ProviderCatalog` at
`_provider_catalog.py:1-2,285-353`). The other **5 roles have NO static
catalog** and must be hand-rolled NET-NEW (per D19):

- **VAD** resolves by try/except with extras embedded in an error string
  (`vad/factory.py:91-151`).
- **Transport** is config-type dispatch (`config/_factory.py:110-139`).
- **Noise/echo** have hardcoded extras only (`noise_reduction.py:40,96`,
  `echo_cancellation.py:11,90`).
- **Agent** has no catalog source.
- `ProviderSelection.capabilities` has **no** static source
  (`validation/provider_capabilities.py:2-5` is a live-derived report).

So the plan must STOP saying "prefer extracting/shared catalog metadata"
uniformly: stt/tts reuse the catalog; vad/transport/agent/noise/echo need
NET-NEW declarative metadata, and every one of those is a place the planner can
silently disagree with `create_session`.

**Impact:** Loss of trust in planning/readiness; `/health/ready` (which the M6b
planner backs) reports a config "ready" that then fails to construct.

**Mitigation (per D5):** REQUIRE a planner-vs-`create_session` **parity test**
that the planner verdict matches the `create_session` outcome for **every one of
the 7 roles**, and GATE the manifest/plan readiness checks behind that parity
test passing. The planner reports likely issues, but final creation remains
authoritative. This is why the Manifest+Planner milestone (split into
M6a/M6b per D5) is marked the **highest-risk milestone** in the roadmap and this
register.

### R7 — Dev opt-in must not relax the existing no-autolaunch invariant

**Reframe (per D16):** This is a **guarded invariant**, not new protection work.
The no-autolaunch protection ALREADY EXISTS and is tested: `debug="full"` alone
never opens a browser tab or binds a UI server (`_autolaunch.py:40-51,70`;
`tests/test_dx_helpers.py:603-625`). Do not re-derive it.

**Risk:** The NEW `EASYCAT_DEV=1` / `VoiceApp(dev=True)` opt-in accidentally
**relaxes** the existing guarantee — i.e. some code path makes `debug="full"`
autolaunch on its own once the dev opt-in machinery exists.

**Impact:** Production instability and broken current assumptions if the
durable-debug guarantee regresses.

**Mitigation:** Assert the dev opt-in is **purely additive** — it never weakens
the "`debug='full'` alone never autolaunches" guarantee. Keep autolaunch
explicitly dev-only: `EASYCAT_DEV=1`, `VoiceApp(dev=True)`, or explicit
`debugger_autolaunch=True`. The acceptance criterion "`EASYCAT_DEV=1` opens one
loopback debugger UI" must be made **testable** by asserting the registry/launch
is invoked (not that a browser literally opens — CI is non-interactive), and a
companion test must assert `debug="full"` alone still does NOT autolaunch.

### R8 — Promotion-to-test ships unredacted PII + raw audio TODAY (critical, unsafe path)

**Severity:** CRITICAL (privacy). This is a present defect, not a future risk.

**Ground truth — the existing path is UNSAFE today.** The plan previously
documented "warn + default no-audio + `--redact`" as if the existing
`journal promote` preserved safe behavior. It does NOT. `journal promote`
(`cli/debug/bundles.py:1819-1962`, only `--out/--force/--json`) calls
`slice_bundle_by_turn`, which copies **full raw NDJSON** (transcripts, tool args)
+ **every** referenced audio blob (`debug/export.py:154-170`), and prints the
**verbatim** agent reply into the generated stub
(`cli/debug/bundles.py:1749-1750,1775-1786`). There is **zero redaction**
anywhere in this path. Promotion writes that straight into a committed file.

**Impact:** A maintainer who promotes a real production turn commits raw
transcripts, tool arguments, and audio into the repo — a privacy/security
incident, silently.

**Hardening note on redaction limits:** Redaction is field-name + secret-regex
only — no embedded-PII/NER detection (`validation/redaction.py:22-34,106-117`).
So a transcript whose entire value IS the assertion target cannot be both
redacted and useful — which is why the record-assertion default must be a
hash/regex over the reply, not the embedded text.

**Mitigation (per D6 — this is HARDENING, not preservation):**

- **Extend-vs-fork: FORK.** Add a new `eval promote` command rather than
  silently extending `journal promote` (leaving both unreconciled is itself a
  footgun). Document `journal promote` as the unsafe legacy path and `eval
  promote` as the hardened replacement; reconcile the two verbs in the
  promotion milestone scope.
- `--no-audio` is the **DEFAULT** (mirror `bundles export` at `bundles.py:1160`).
- Records route through `redact_value` **before serialization** (redact-by-default).
- Add a `contains_unredacted_sensitive_text()` **tripwire** gated by
  `--allow-pii` (mirror `_assert_context_pack_redacted`).
- The record-assertion mode **DEFAULTS to assert on a HASH/REGEX** over the reply
  rather than embedding raw text.
- Reuse `_promote_test_stub` / `_validate_promoted_slice` rather than
  re-implementing.

### R9 — Replay executes tools by default

**Risk:** Production replay accidentally calls real tools/APIs.

**Impact:** External side effects, user-impacting actions, cost.

**Mitigation:** Keep tool replay denied by default. Require explicit opt-in and
make tests assert the default.

### R10 — Three existing latency vocabularies must be RECONCILED, not just kept from fragmenting

**Risk (corrected per D11):** This is not merely "prevent future fragmentation."
There are already **THREE distinct, incompatible latency vocabularies** that
`build_budget_report` must unify:

1. **Runtime** emits ONLY `stage="total_ms"`
   (`session/_turn_runner.py:686-696,847-853`) through a single-observation
   push-based monitor (`session/_latency_budget.py:44-64`).
2. **Offline validation** uses percentile columns `tts_ttfb_ms` / `llm_ttft_ms`
   (`validation/latency.py:113-114,308-314`).
3. **Waterfall milestones** use `*_to_*_ms` names
   (`debug/_turn_timeline.py:325-336`, `cli/debug/bundles.py:1448-1452`).

Of the six "Runtime Metrics To Add": `tts_ttfb_ms`/`llm_ttft_ms` are
RENAMES/lifts of existing offline columns + waterfall milestones (NOT net-new);
`stt_final_latency_ms`/`vad_endpointing_ms`/`first_audio_ms`/`barge_in_ack_ms`
are NET-NEW at runtime (zero hits in `src/`) but have equivalent waterfall
milestones to map from.

**Impact:** Confusing reports and brittle tests; a "shared budget report" that
silently excludes the offline percentile path.

**Mitigation (per D11):** Centralize budget models/reporting in `easycat.budgets`.
Concretely: (a) **map the new flat metric names onto `_budget_matches_stage`**;
(b) **retrofit `validation/latency.py` `budget_violations` onto
`build_budget_report`** so the builder covers the offline percentile path, not
just journal records. `build_budget_report`'s "builder over journal records"
wording must be widened to cover the offline path too. Keep aliases for existing
`total_ms`, `tts_ttfb_ms`, and `llm_ttft_ms`.

### R11 — `aiohttp`/FastAPI split confuses server users

**Risk:** WebRTC/debugger use aiohttp, Twilio examples use FastAPI, and
`VoiceServer` chooses one path.

**Impact:** Harder docs and extension story.

**Mitigation:** Use aiohttp internally for `VoiceServer` first because existing
WebRTC/debugger surfaces already use it. Keep FastAPI as an adapter/future
integration rather than a core dependency.

### R12 — Next-major scope balloons

**Risk:** VoiceApp, VoiceServer, manifests, planning, evals, debugger UI, and
budgets all land in one giant branch.

**Impact:** Hard review, high regression risk.

**Mitigation:** Follow `roadmap.md` milestones. Ship additive slices with tests
and docs before deprecating old paths.

### R13 — Public API surface cap exhausted

**Risk:** `easycat.__all__` is at **94/94** (verified;
`tests/test_public_api.py:126` asserts `len(easycat.__all__) <= 94`), and the
plan never mentions the cap. Adding a top-level `VoiceApp` export pushes the
surface to 95 and trips the cap assertion, blocking the Phase-1 export.

**Impact:** Phase-1 stalls on an unexpected test failure; or a maintainer
patches the cap without the matching snapshot/docs updates, leaving the public
API contract inconsistent.

**Mitigation (per D9):** Treat the cap bump as a DELIBERATE, atomic change. In
the SAME PR that adds top-level `VoiceApp`, perform the **triple-lock**:

1. Raise the cap `94 → 95` in `tests/test_public_api.py` and update
   `PUBLIC_API_SNAPSHOT`.
2. Add `VoiceApp` to `easycat.__all__` AND `LAZY_EXPORTS`.
3. Update `docs/public-api.md`.

Add "raise `__all__` cap to 95" to Phase-1 Files-To-Update. **Only top-level
`VoiceApp` counts** against this cap: `CostBudget` / `LatencyBudget` /
`VoiceServer` / `EvalRunner` / `EvalScenario` / `EvalTurn` are SUBMODULE exports
(`easycat.budgets`, `easycat.server`, `easycat.evals`) and do NOT count against
the top-level cap.

## Decision Points To Revisit

- Should `VoiceApp` be top-level only, or also `easycat.app.VoiceApp`?
- Should project manifest be TOML, YAML, or both?
- Should `easycat serve` mean dev server, production server, or both with mode
  flags?
- Should `VoiceServer` expose Prometheus text directly or rely on OpenTelemetry
  first?
- How much Twilio server functionality belongs in Phase 1 versus Phase 2?
