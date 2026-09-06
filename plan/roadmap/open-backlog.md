# Open backlog

Status: active backlog.
Date: 2026-08-03. Every evidence line below was re-verified against the working
tree at `958b0c18` by reading the cited file, grepping the cited symbol, or
executing the cited expression. Claims that could not be confirmed are marked.

This is the single consolidated queue for work **outside** the bug-resistant
refactor program. That program has its own backlog in
[2026-08-02-bug-resistant-refactor-plan.md](2026-08-02-bug-resistant-refactor-plan.md);
separable feature work lives in [../peripherals/README.md](../peripherals/README.md).
Items here are not tier-gated and can be picked up independently, but several
carry a stated sequencing constraint — honour it.

Nothing in `../archive/` or `../critique/` is a queue. Where an item originated
there, the origin is cited so the rationale can be read, not re-executed.

2026-09-05 addition: [§8](#8-next-level-developer-experience) sequences the next
developer-experience milestone against source at `2c597601`. Sections 1–7
retain their original evidence date; re-verify their claims before starting
work. This addition does not reclassify old findings as still open.

---

## 1. Security and privacy, now

The highest-severity live leak is **not** in this file: WS5.2 in the refactor
plan covers `WebTransportTransportConfig.auth_token`
(`src/easycat/transports/webtransport.py:372` declares
`auth_token: str | None = None` with no `repr=False`, against ~60 `repr=False`
sites elsewhere in `src/`). It is marked "immediately, independent of the peer
decision". Do that first.

### 1.1 Harden `journal promote` against committing production data

`src/easycat/cli/debug/promote.py:192-217` exposes only `--out`, `--force`, and
`--json`. `slice_bundle_by_turn` at `src/easycat/debug/export.py:591-598` copies
every referenced artifact blob into the promoted bundle with no record-level
redaction pass. Promoting a real production turn therefore commits raw audio and
unredacted NDJSON into a repository.

The stub-echo half of this was fixed independently — `_promoted_agent_text` at
`promote.py:101-128` refuses to echo an `agent_final` value the redaction policy
would modify. The bundle half was not.

Work: redact bundle contents on promote, default to `--no-audio`, and add an
`--allow-pii` tripwire so including sensitive material is an explicit act.
Origin: the neo risk register R8 and open question Q14, both now summarized in
[../archive/neo-milestone-ledger.md](../archive/neo-milestone-ledger.md).

### 1.2 Export-time second redaction pass

Owned by [../peripherals/README.md](../peripherals/README.md), listed here
because it is the same defect class. `export_debug_bundle` at
`src/easycat/session/_session.py:989-993` takes only
`(path, *, inline_artifacts, overwrite)` — there is no `redaction=` parameter.
`src/easycat/cli/debug/bundles.py:27-29` gates raw mode "until the full
redaction-policy layer lands", and no `--raw` option is registered in that
module. 1.1 and this item should share one redaction entry point rather than
growing two.

---

## 2. Contract gaps — the shipped surface promises behavior the runtime lacks

Each item here is a public promise with no runtime backing. Either implement the
promise or retract it; leaving them is worse than either.

### 2.1 Wire the drain cancellation modes, or delete them

`CancellationMode.DRAIN_CURRENT_UNIT` and `DRAIN_TO_COMMIT_POINT` appear in
`src/` **only** as their enum definition at
`src/easycat/integrations/agents/base.py:40-41`. The single production call site,
`src/easycat/session/interruption.py:56`, hard-codes `IMMEDIATE_STOP`, and no
config field has type `CancellationMode`. Meanwhile `docs/public-api.md:209`
advertises `CancellationMode` as "supported interruption and drain strategies",
and every bridge implements drain semantics under a full test matrix
(`tests/integrations/agents/test_bridge_lifecycle_*.py`,
`test_ws2b_interruption_and_mcp.py:367-368,448`). The bridge layer is built; the
runtime cannot select the modes.

Design source: the WS2B workstream record, summarized in
[../archive/debug-first-runtime-workstreams.md](../archive/debug-first-runtime-workstreams.md).

### 2.2 Remote-agent capability discovery (T2C.4), or stop leaking metadata

`supports_interruption`, `supports_drain`, and `easycat.framework` return **zero**
hits across both `src/` and `tests/`. `responses_api.py:527-535` builds
`easycat.*` turn metadata unconditionally and `:785-789` merges it into every
request body, so EasyCat pushes proprietary metadata to third-party Responses API
endpoints — the exact "plain server" case the design said to detect and stay
quiet for.

**Sequencing: 2.1 before 2.2.** Negotiating drain support with a server is
pointless until the runtime can select a drain mode.

### 2.3 Shallow-mode downgrade runtime path, or retract the contract

`shallow_mode_downgrade` returns **zero** hits across `src/` and `tests/`.
`ShallowModeInterruptionError` is raised at
`src/easycat/integrations/agents/generic_workflow.py:148`, but
`src/easycat/session/interruption.py:59-61` swallows every exception into
`logger.debug` and returns `False` — the user gets no journal record and no
diagnostic. No `shallow` reference exists anywhere in `src/easycat/cli/`, so
`doctor` cannot warn either. Work: a `ControlSignalRecord` on downgrade plus a
doctor warning.

### 2.4 A real MCP round-trip test

MCP is plumbed config-to-bridge (`config/easy.py:119-242`,
`session/_types.py:233`, `stages/agent.py:160-170`,
`integrations/agents/base.py:273`), but
`tests/integrations/agents/test_ws2b_interruption_and_mcp.py:690-712` only
asserts `bridge._mcp_servers == ["stdio://test"]` — attribute storage, not a
round trip. No mock MCP server exists in the repo and no CI job sets
`MCP_FILESYSTEM_SERVER_PATH`. Work: a mock MCP server fixture plus one
end-to-end tool-call test.

---

## 3. Evals and promote hardening

### 3.1 Text-first evals: `easycat.evals` plus `easycat eval run|report`

`ls src/easycat/evals` → no such directory. `EvalScenario`, `EvalTurn`,
`EvalRunner`, `ScenarioResult`, and `assert_budgets_pass` each return zero hits
across `src/`.

Three constraints travel with this item and must not be rediscovered:

| Constraint | Statement |
|---|---|
| Q12 | Core `easycat.evals` stays pytest-free; pytest fixtures live in `easycat.evals.pytest`. |
| TEST-4 | A provider-stage budget applied to a text-only scenario must **raise** "no samples for stage X", never pass vacuously. |
| Acceptance | `eval run`/`eval report` emit `schema_version=1` envelopes; a promoted `.py` must import and run under pytest in `tmp_path`. |

**Branch pointer.** A working but unmerged implementation sits at
`8aed223c..32daebd5` on `origin/neo/phase-3-feedback-loop`. Verified:
`git rev-list --count origin/neo/phase-3-feedback-loop..main` = **2065**, so the
branch is 2065 commits behind `main`. Treat it as a design reference to read, not
a branch to merge; rebasing it is almost certainly more expensive than
reimplementing against current `main`.

### 3.2 Decide the promotion surface (Q13)

Unmade architectural call. `promote_turn_to_test` returns zero hits in `src/`;
only the bundle-slicing `promote_turn` exists. Two open sub-decisions, recorded
nowhere but the retired neo packet:

- extend `journal promote` versus fork a separate `eval promote`;
- what a promotion generates — `--mode record-assertion` versus
  `--mode artifact-replay`.

Sequence after 1.1: the redaction contract determines what a promoted artifact
is even allowed to contain.

---

## 4. Critique residue (API-DX and packaging)

From [../critique/2026-07-26-full-critique.md](../critique/2026-07-26-full-critique.md).
Nineteen of its twenty HIGH findings are closed; these are the survivors, each
re-verified today.

| # | Item | Verified evidence |
|---|---|---|
| 4.1 | `EasyConfig.mic()` is a duplicate | Executed: `EasyConfig() == EasyConfig.mic()` → `True`. `config/easy.py:758` already defaults transport to `LocalTransportConfig`; `:1020` `mic()` only `setdefault`s the same value, while docs teach it as canonical. |
| 4.2 | `from easycat import Error` is not an exception | Executed: `easycat.Error` is `easycat.events.Error`; `issubclass(easycat.Error, BaseException)` → `False`. `except easycat.Error` is a `TypeError` at runtime. |
| 4.3 | `run_session` is not exported | Executed: `'run_session' in easycat.__all__` → `False`, while the function exists at `src/easycat/helpers.py:163` and is the documented path for long-running apps. |
| 4.4 | `require_env` raises `SystemExit` | `src/easycat/helpers.py:24-33`. Uncatchable by `except Exception`; terminates an embedding application from a top-level-exported library helper. |
| 4.5 | Bundle export ignores third-party artifact stores | `src/easycat/debug/export.py:255-264` isinstance-dispatches on exactly `_MemoryArtifactStore` and `_FilesystemArtifactStore` and reads their private `_store` (`:271`) and `_dir` (`:264`). A custom `ArtifactStore` falls through both branches and silently exports zero artifacts. |
| 4.6 | ONNX weights ship in the base wheel | `pyproject.toml:203-204` source-excludes `plan`/`plan/**` but not `src/easycat/models`, which holds `silero_vad.onnx` (2.3 MB), `smart-turn-v3.2-cpu.onnx` (8.3 MB), and `funasr_fsmn_vad/model.onnx` (1.7 MB) in a 13 MB tree — measured with `du`, regardless of extras. |
| 4.7 | No bridge capability matrix | `ls docs/extending/` → `README.md`, `agent-bridge.md`, `stt.md`, `transport.md`, `tts.md`, `vad.md`. None documents how the six bridges differ on tool visibility, structured output, or tool-call identity, so users cannot choose on evidence. |
| 4.8 | The release path has never run | `git tag` is empty and `pyproject.toml` is at `0.1.0`, so `release.yml`, `release-validation.yml`, and `scripts/check_release_tag.py` have never executed against a real tag. This also moots the "missing migration guide" cross-referenced from four retired workstream files: there is no released version to migrate from. |
| 4.9 | `src/easycat/validation` ships to users | 5,541 LOC (`find … \| xargs wc -l`), absent from the `pyproject.toml` source-exclude list. `validation/provider_reports.py:122` hard-codes `live_pytest_target="tests/stt/test_stt_openai.py::test_live_openai_stt"` and the lane shells out to `uv run pytest` in an installed user's cwd. |
| 4.10 | The bind guard is not a primitive | `src/easycat/transports/webrtc.py:301` and `src/easycat/transports/twilio_media.py:1212` each hand-roll `is_loopback_host`; `enforce_bind_guard` appears only in `server/{webrtc_routes,websocket,voice_server,auth}.py` and `transports/webtransport.py`. A new transport can still open a socket without passing the check. **Overlaps WS5.1** — reconcile, do not execute blind. |
| 4.11 | No audio-path benchmark | `perf/` holds 14 benches; none references `resample`. soxr is a base dependency and spectral alias tests exist, so only the benchmark half of the T2 fix is untouched. |
| 4.12 | No published latency numbers | **Narrowed after verification.** `docs/latency.md:61-105` already carries the comparison method, dispatch boundaries, the runnable `perf/bench_framework_latency.py` command, and explicit positioning against LiveKit Agents and Pipecat. The survey's "no comparative positioning" claim is false. Only committed result numbers are missing. |

### 4.13 Decide the `easycat.__all__` target — CONTESTED

Executed: `len(easycat.__all__)` = **121**, against a documented target of ≤70 in
the April audit. **Do not execute the cull without resolving the counter-argument
first.** [../archive/onramp-zen-dx-plan.md](../archive/onramp-zen-dx-plan.md)
section 7 records a deliberate decision *not* to shrink `__all__`: it is a
curated release contract, it is pinned by `tests/test_public_api.py`, and `dir()`
re-sorts, so a tiered `__all__` changes nothing a user sees. Neither
`docs/public-api.md` nor the auto-ratcheting ≤121 ceiling in
`tests/test_public_api.py:273-274` records that reasoning. The deliverable here
is a written decision either way, not a diff.

---

## 5. Structural cleanup residue (April 2026 audit)

Extracted from [../archive/2026-04-combined-cleanup-audit.md](../archive/2026-04-combined-cleanup-audit.md).
That document is ~85% completed-work log with April premises that no longer
describe the tree; these are the items verified genuinely absent today. Where an
item overlaps an active refactor slice, the cross-reference is binding — check
the slice before starting so the work is not done twice or in conflict.

| # | Item | Verified evidence | Cross-reference |
|---|---|---|---|
| 5.1 | Shared `ConnectionPolicy` for transports (origin, path, token, max payload, compression, ping/close timeouts) | `grep -rn ConnectionPolicy src/` → zero hits | Overlaps WS5.1 `authorized_bind` and item 4.10 |
| 5.2 | `record_schema_version` on `JournalRecord`; promote control-signal fields to first-class record fields | `grep -rn record_schema_version src/` → zero hits | The journal is the declared single source of truth for observability, so an unversioned record schema is a durability gap |
| 5.3 | Shared `JournalSlice` helper; collapse parallel journal serialization into one module | `grep -rn 'class JournalSlice' src/` → zero hits; `src/easycat/runtime/serialization.py` does not exist. `JournalView` and `RunBundle` still slice independently | Touches the same code as 1.1 and 1.2 |
| 5.4 | Reduce stage boilerplate — eight handwritten wrappers repeating `snapshot_state`/`execute`/`handle_upstream`/`replay` | `cat src/easycat/stages/*.py \| wc -l` = **2,853** against the audit's 1,573; the cost grew 81% since it was flagged. `stages/base.py` alone is 793 lines. Includes removing or inlining `src/easycat/runtime/nondeterministic.py`, which still exists | Overlaps WS3 |
| 5.5 | Unify `easycat.debug` and `easycat.debugger` into one product namespace | No `src/easycat/inspect` module or package exists; both namespaces remain, with low-level code mixed into the user-facing surface | — |
| 5.6 | Normalize public provider config field names: `model_id` → `model`, `ws_url` → `base_url` | `model_id` at `tts/elevenlabs_tts.py:82` and `tts/cartesia_tts.py:55`; `ws_url` at `stt/openai_realtime_provider.py:78` and `stt/elevenlabs_provider.py:68`. The `MODEL_FIELD` workaround in the TTS factory can be deleted once done | Breaking change; sequence with 4.8 |
| 5.7 | Shared `EventTranslator` base; PydanticAI tool deltas must retain tool names | `grep -rn 'class .*EventTranslator' src/` → zero hits. Interleaved tool-call journals stay unreadable without pending-call tracking | — |
| 5.8 | Telephony compliance as policy | `telephony/compliance.py:209` `CallBlocked` carries only `(number, reason)` — no rule id, jurisdiction, local time, DNC source, or audit metadata. `CallPreflightResult` and `DisclosureRequired` return zero hits. Outbound blocking is still a raw `ValueError`. **The largest genuinely-unstarted block in the audit and the only one with regulatory weight** | — |
| 5.9 | Rename Twilio-only generic telephony names, or introduce a real `TelephonyProvider` protocol | `telephony/outbound.py:204` is still `class OutboundCallManager` despite being Twilio-specific | Sequence with 5.8 |
| 5.10 | Adopt pyright/basedpyright and add `pyright --verifytypes easycat` | `grep -rn 'pyright\|basedpyright' pyproject.toml .github/workflows/` → zero hits, although the package ships `py.typed` | — |
| 5.11 | Non-inline `EventBus` dispatch policies (task, queue, best-effort) | Subscription tokens and `handler_error_policy` shipped (`src/easycat/events.py:693`), but only inline dispatch exists, so a slow user callback can still stall audio-critical paths | — |

---

## 6. Validation and guard residue

### 6.1 Close the provider-surface cassette gap and build the schema registry

`tests/contracts/provider_surface_matrix.py` has exactly **3** rows at
`cassette_status="required"` against **22** at `"deferred"`, and
`tests/cassettes` holds exactly 3 files
(`http/openai-stt.json`, `sse/remote-responses-api.json`,
`ws/openai-realtime-stt.json`). `tests/contracts/schema_fingerprints.py` is a
comparison helper, not a provider-keyed registry — every rule is constructed
inline in the test file. The cassette JSON format and the schema-registry design
travel with this item; they were carried out of the retired validation reference
and now sit alongside the shipped vocabulary in
[../../docs/reference/validation-vocabulary.md](../../docs/reference/validation-vocabulary.md).

### 6.2 Automate browser-driven WebRTC validation

Zero `playwright`/`puppeteer`/`selenium` references across `pyproject.toml`,
`.github/`, `scripts/`, and `tests/`. The consuming half exists and is unfed: the
stats env var at `validation/_slice_runner.py:271` and the artifact path at
`transports/_webrtc_stats.py:77`. The WebRTC stats capture protocol is the spec
for the missing driver.

### 6.3 Make `release-validation.yml` call `easycat validate release`

`release-validation.yml:130,138,152,161` invoke `quick`, `stress`, `live`, and
`latency` individually and never call `validate release`, while
`src/easycat/validation/_release_runner.py:40` defines
`RELEASE_SLICES = ("quick", "guard", "stress", "contracts")`. The workflow
therefore silently skips the **guard** and **contracts** gates. Two
implementations of one gate that can drift — collapse to one.

### 6.4 Resolve Open Decision 6: should `easycat validate unit` exist?

No `unit` command exists in `src/easycat/cli/validate.py`, which ships
`quick`/`socket`/`stress`/`contracts`/`latency`/`live`/`release`/`report`. The
other five open decisions in the retired validation reference are all resolved in
code; this is the only survivor. The deliverable is a decision, not necessarily a
command.

### 6.5 Make `tests/cli/test_json_schema.py` walk the command registry

The file is **32** hand-written per-command test functions with zero iteration
over the Typer command registry (`grep -c 'registered_commands\|for cmd in'` → 0).
Adding a `--json` command therefore produces no envelope assertion and nothing
fails. Origin: TEST-5 in
[../archive/neo-plan-review.md](../archive/neo-plan-review.md), recorded nowhere
else in the repo.

### 6.6 Delete or regenerate `perf/ws3-final.json`

Byte-identical to `perf/baseline.json` (`md5` `44fd1433c29185c64ca3db08a8abfb01`
for both) and carrying the pre-workstream `git_sha 13051d2fd5f5`, so it asserts a
post-workstream measurement that was never taken. Already adjudicated as a
housekeeping nit, not a finding — carry it as cleanup, never as a gate.

### 6.7 Guard source and docs citations of `plan/` filenames

`plan` and `plan/**` are source-excluded at `pyproject.toml:203-204`, so a
`plan/` filename cited from shipped code or a published docs page is a path no
installed user can follow. Seven such citations existed and were fixed by the
same reorganization that created this file — including
`src/easycat/tts/cartesia_tts.py`, which emitted a planning filename in a
runtime message shown to end users.

What remains is the recurrence guard: **no test scans source docstrings,
comments, or docs pages for `.md` citations**, so this class rots silently and
will come back. `tests/test_markdown_links.py` only validates Markdown link
syntax in maintained Markdown files; a bare filename in a Python docstring is
invisible to it.

Work: add a guard that fails when shipped source or a published docs page names
a `plan/` document, by path or by bare basename.
---

## 7. Decided: do not revive

Recorded so nobody rebuilds these from an archived document.

| Decision | Basis |
|---|---|
| **Cost and latency budgets.** Do not reintroduce `cost_budget`, `max_session_cost_usd`, `CostRecord`, or `easycat.budgets`. | Removed deliberately by `db3ca9cc` ("session: remove runtime cost and latency-budget features"); all symbols return zero hits in `src/` and `tests/`. The only written removal rationale is [../archive/peripheral-observability-and-cost.md](../archive/peripheral-observability-and-cost.md). `easycat cost` is a standing CLI non-goal. |
| **Voice-to-voice.** The pipeline stays chained. | A permanent architectural guardrail; the reasoning now lives in the non-goals section of `docs/architecture.md`, not in `plan/`. |
| **Prometheus.** "Defer Prometheus, keep JSON/OTel first" is the shipped state, not an open decision. | Zero `prometheus` hits repo-wide; the OTel facade landed as `_observability.py` and is documented as Layer D in `docs/observability.md`. |
| **N3, the validation self-audit item.** Closed as superseded by this reorganization. | It was a self-referential audit of a document that has been retired. |
| **The `__all__` cull is not decided.** | See 4.13. It is listed as a live decision, not a live task, precisely because the counter-argument in `../archive/onramp-zen-dx-plan.md` section 7 is real. |
| **M13's dev-debugger mode.** Do not rebuild it. | It shipped independently on `main` as `2d159801`, not via the closed neo branch. See [../archive/neo-milestone-ledger.md](../archive/neo-milestone-ledger.md). |

---

## 8. Next-level developer experience

Follow [the 2026-09-05 delivery plan](2026-09-05-next-level-developer-experience.md)
for the next product and maintainability milestone. It owns the new DX1–DX7
slices: shared configuration resolution, selected-app diagnostics, tests of the
generated application, narrower session interfaces, portable failure-to-test
workflows, the external provider journey, and contributor feedback.

Start with DX1–DX3. The plan supplies source evidence, dependencies, PR
boundaries, acceptance criteria, compatibility constraints, and focused checks.
Its references to sections 1–6 and the bug-resistant program schedule existing
work under its original owner; they do not replace those specifications or
bypass their structural prerequisites. Keep this entry as the intake route
and update delivery status in the linked plan.

## Where the rest went

| Looking for | Now in |
|---|---|
| The active refactor slices and structural prerequisites | [2026-08-02-bug-resistant-refactor-plan.md](2026-08-02-bug-resistant-refactor-plan.md) |
| The peer-set decision and its twelve obligation rows | [2026-08-03-peer-set-adr.md](2026-08-03-peer-set-adr.md) |
| Outcome-observation definitions and the frozen cohorts | [../metrics/README.md](../metrics/README.md) and [../metrics/refactor-families.json](../metrics/refactor-families.json) |
| Separable feature work (mu-law, Smart Turn, evals UI, CLI flags) | [../peripherals/README.md](../peripherals/README.md) |
| Per-platform deployment runbooks | [../peripherals/peripheral-deployment.md](../peripherals/peripheral-deployment.md) |
| The 2026-07-26 audit in full, including [T5 on the meta-layer](../critique/2026-07-26-full-critique.md#t5-—-the-meta-layer-became-a-second-product-competing-for-the-same-maintenance-budget) | [../critique/2026-07-26-full-critique.md](../critique/2026-07-26-full-critique.md) |
| Why a retired plan said what it said | [../archive/](../archive/) — historical only, never a queue |
