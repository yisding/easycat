# Validation Implementation Tasks

Status: active backlog.

This is the execution backlog for [reference.md](reference.md). It is ordered
to make local validation useful before adding live provider, latency, release,
and observability gates.

Current-state caveat: as of static inspection on 2026-05-21, there is no
`easycat validate` command, no `scripts/validate.py`, and no validation JSON
artifact format in the repo. Tasks below create those surfaces.

## Working Rules

- Keep each task scoped to the listed files unless implementation forces a
  small adjacent change.
- Add or update tests for every behavior change.
- Do not require live provider credentials for PR-required validation.
- Preserve existing test marker behavior until a task explicitly changes it.
- Emit generated validation artifacts under `.easycat/validation/` by default.
- Write each validation run into an isolated run directory; never let matrix
  jobs or concurrent local runs overwrite the same report or JUnit file.
- Treat secrets, transcripts, prompts, phone numbers, and generated provider
  content as unsafe unless redaction is explicit.
- Keep planned public commands documented as planned until the relevant task
  lands.
- Preserve the existing CLI-wide `--json` meaning: stdout machine-readable
  envelope. Use `--report PATH` or `--output PATH` for persisted validation
  report files.

## Milestones

| Milestone | Goal | Expected PR shape |
|---|---|---|
| V0 | Marker, report, and temporary script foundation | one small PR |
| V1 | First-class CLI and CI artifacts | one PR |
| V2 | Structured latency validation | one PR |
| V3 | Provider and protocol contracts | two to three PRs |
| V4 | Live canaries and provider reports | one to two PRs |
| V5 | Stress, benchmarks, and release gates | two PRs |
| V6 | Optional observability API | one PR after validation names settle |

## V0: Validation Foundation

### V0.1 Register Validation Markers

Status: completed

Current state:

- `pyproject.toml` registers only `integration_local`,
  `integration_socket`, `integration_live`, and `slow`.
- Existing tests use `asyncio`, `parametrize`, `skipif`, the registered
  integration markers, and `slow`. No validation-specific markers are present
  yet.

Files:

- `pyproject.toml`
- any tests with currently unregistered markers

Tasks:

- Register `contract`, `latency`, `stress`, `release`, `flaky`,
  `provider_openai`, `provider_deepgram`, `provider_elevenlabs`,
  `provider_cartesia`, `surface_stt`, `surface_tts`, `surface_agent`,
  `surface_transport`, `surface_vad`, `agent_bridge`, `requires_extra`, and
  `provider`.
- Decide and document that `quick` means PR-local validation: deterministic,
  no sockets, no live credentials, no slow/flaky tests. It may include
  `integration_local` tests, but if measured runtime gets too high, split a
  smaller `unit` command later instead of weakening CI coverage.
- Confirm all current custom markers are registered.
- Add marker-lint helpers so any `integration_live`, `contract`, or `latency`
  test that names a provider surface also declares provider and surface scope.
- Add `strict_markers = true` after collection is clean.
- Add `strict_config = true` only after current pytest config emits no
  warnings.

Acceptance:

- `uv run pytest --collect-only -q` completes without unknown marker warnings.
- Current CI marker expressions continue to select the same broad groups until
  CI is intentionally changed in V1.2.
- `uv run pytest --collect-only -q -m "integration_live and provider_openai"`
  collects only OpenAI-scoped live tests once provider markers are assigned.
- Marker lint fails a synthetic live/provider test that omits provider or
  surface metadata.

Verification:

```bash
uv run pytest --collect-only -q
uv run pytest -q -m "not integration_socket and not integration_live and not slow"
```

### V0.2 Define Validation Report Model

Status: completed

Files:

- reusable validation module chosen for V0, or `src/easycat/cli/validate.py`
  if V1 is pulled forward
- `tests/cli/test_validate.py` or a focused report-model test

Tasks:

- Add typed helpers for the validation JSON envelope:
  `ValidationRun`, `ValidationCheck`, `ValidationSkip`,
  `ValidationFailure`, and artifact references.
- Include `schema_version`, `redaction_version`, `run_id`, command,
  timestamps, duration, status, validation exit code, underlying tool exit
  codes such as `pytest_exit_code`, git metadata, Python/platform metadata,
  checks, skips, failures, latency, providers, extras, and artifact paths.
- Represent provider check states as `not_requested`,
  `skipped_missing_secret`, `failed_missing_required_secret`, `passed`, and
  `failed`.
- Make JSON serialization deterministic enough for tests.
- Never serialize environment variable values or secret-like strings. Allowed
  environment metadata is env var presence by name only.
- Define report-boundary redaction for command args, pytest stdout/stderr
  snippets, JUnit paths, failure messages, file paths, URLs, transcripts,
  prompts, generated provider text, phone numbers, and provider request IDs.

Acceptance:

- Unit tests verify required fields and deterministic serialization.
- A test value that looks like a secret does not appear in serialized output.
- The schema can represent pass, fail, and expected skip.
- The schema can represent strict-mode skipped-required failures separately
  from expected missing-secret skips.

Verification:

```bash
uv run pytest tests/cli/test_validate.py -q
```

### V0.3 Create `scripts/validate.py quick/socket`

Status: completed

Files:

- `scripts/validate.py`
- reusable validation runner/report helpers
- tests for report helpers and script dispatch

Tasks:

- Create the `scripts/` directory if it is still absent.
- Keep `scripts/validate.py` as a thin shim over reusable runner/report code
  so V1 can reuse the implementation instead of creating a parallel codepath.
- Implement `quick` with:
  `uv run pytest -q --junitxml=<run-dir>/junit.xml -m "not integration_socket and not integration_live and not slow and not flaky"`.
- Implement `socket` with:
  `uv run pytest -q --junitxml=<run-dir>/junit.xml -m "integration_socket and not integration_live and not flaky"`.
- Create `.easycat/validation/runs/<run_id>/` automatically. Use a run id
  containing UTC timestamp, slice name, and a collision-resistant suffix such
  as pid or CI run attempt.
- Emit `<run-dir>/report.json` and atomically update
  `.easycat/validation/latest.json` as the latest-run report copy or pointer.
- Return validation exit codes, not raw pytest exit codes. Store both values
  in the report.
- Record command duration and artifact paths.
- Capture stdout/stderr logs under the run directory when practical, with the
  same redaction boundary as JSON reports.

Acceptance:

- `uv run python scripts/validate.py quick` runs the planned quick selector.
- `uv run python scripts/validate.py socket` runs the planned socket selector.
- A failed pytest run still writes a validation JSON report.
- The JSON report references JUnit XML when it exists.
- Two concurrent validation runs create separate run directories.
- `latest.json` is updated atomically and never points at a partial report.

Verification:

```bash
uv run python scripts/validate.py quick
uv run python scripts/validate.py socket
```

### V0.4 Add Flaky Quarantine Metadata Check

Status: completed

Current state:

- No `flaky` marker is registered or used.

Files:

- `tests/conftest.py` or a new test utility under `tests/`
- `pyproject.toml`

Tasks:

- Define the accepted flaky metadata format in marker kwargs or a nearby
  helper comment.
- Use `@pytest.mark.flaky(issue="...", owner="...", review_by="YYYY-MM-DD")`
  as the initial metadata shape.
- Validate that every `flaky` test has issue, owner, and review date, and
  that `review_by` is not stale.
- Add release validation behavior that fails stale flaky markers and fails
  any release-required test that is still quarantined.
- Define nightly rerun policy before adding a rerun dependency. If a plugin is
  adopted later, name the plugin and keep it out of V0 unless needed.
- Keep PR quick/socket selectors excluding `flaky`.

Acceptance:

- A synthetic or fixture test proves missing flaky metadata fails the
  validation helper.
- A synthetic or fixture test proves stale `review_by` fails release
  validation.
- Normal test collection remains fast.

Verification:

```bash
uv run pytest --collect-only -q -m flaky
uv run pytest -q -m "not flaky and not integration_live"
```

### V0.5 Document Contributor Workflow

Status: completed

Files:

- `CONTRIBUTING.md` if present, otherwise top-level `README.md`
- validation README/reference updates as needed

Tasks:

- Document the current script-first command:
  `uv run python scripts/validate.py quick`.
- Label `easycat validate quick` as the planned public replacement until V1.1
  lands.
- Document `--report PATH` for persisted validation JSON and reserve `--json`
  for stdout machine-readable CLI envelopes.
- Document when to run socket, live, latency, and release checks.
- Document flaky marker policy.
- Document artifact directory and cleanup expectations.
- Add a provider validation table or link to the validation reference showing
  provider surface, extra, env var, default mode/model, contract status,
  cassette status, and live command.

Acceptance:

- A new contributor can find the quick command from top-level docs.
- The docs do not require live provider credentials for normal PR work.
- The docs do not imply `easycat validate` exists before V1.1.

## V1: First-Class CLI And CI Artifacts

### V1.1 Move Validation Into CLI

Status: pending

Dependencies:

- V0.2
- V0.3

Current state:

- `src/easycat/cli/_app.py` registers `init`, `doctor`, `explain`,
  `inspect`, and the `bundles` group.
- The bare `easycat` journey menu has no validation section.

Files:

- `src/easycat/cli/validate.py`
- `src/easycat/cli/_app.py`
- `tests/cli/test_validate.py`
- `tests/cli/TEST_PLANS.md`
- `src/easycat/cli/_output.py` and `src/easycat/cli/diagnose/_codes.py` if
  validation adds or reserves new public exit-code meanings

Tasks:

- Add a Typer command group: `easycat validate`.
- Implement subcommands: `quick`, `socket`, and `report`.
- Preserve report/JUnit/artifact options from the script.
- Keep `--json` as the existing stdout envelope mode. Use `--report PATH` for
  persisted validation JSON.
- Add a validation exit-code table before implementation. Do not expose raw
  pytest exit code `5` as public CLI exit code `5`, because `easycat` already
  uses `5` for bundle missing/corrupt.
- Store both public `exit_code` and underlying `pytest_exit_code` in reports.
- Keep human output concise and Rich-compatible.
- Add the command to the top-level journey menu.
- Add `validate` and `validate report` to the CLI test plan.

Acceptance:

- `uv run easycat validate quick --report /tmp/easycat-validation.json`
  writes a report, and `uv run easycat validate quick --json` emits the
  standard stdout envelope.
- `uv run easycat validate report .easycat/validation/latest.json` renders a
  concise summary.
- CLI tests cover success, failure, expected skip rendering, missing report,
  invalid JSON, unsupported `schema_version`, unknown `kind`, failed run,
  artifact paths, and git dirty state rendering.
- Bare `easycat` output lists validation only after the command exists.

Verification:

```bash
uv run easycat validate quick --report /tmp/easycat-validation.json
uv run easycat validate quick --json
uv run pytest tests/cli/test_validate.py -q
```

### V1.2 Update CI Required Jobs

Status: pending

Dependencies:

- V1.1, or V0.3 if CI temporarily calls the script

Current state:

- `.github/workflows/ci.yml` has one local test matrix on Python 3.12 and
  3.14 with `-m "not integration_socket and not integration_live"`, but the
  package declares `requires-python >=3.11`.
- The socket integration job also runs on Python 3.12 and 3.14.
- The live-provider job is manual via `workflow_dispatch`.
- CI does not upload validation JSON or JUnit artifacts.
- Current CI uses pytest `-x`, which conflicts with complete JUnit and
  validation reports.

Files:

- `.github/workflows/ci.yml`

Tasks:

- Change quick test selection to exclude `slow` and `flaky`.
- Add JUnit output and validation JSON artifact upload with `if: always()`.
- Keep quick required on Python 3.11, 3.12, and 3.14 unless the project
  changes its advertised Python support.
- Keep socket required on Python 3.12 only.
- Remove pytest `-x` from validation CI, set matrix `fail-fast: false`, and
  add job-level `timeout-minutes`.
- Add package build smoke on Python 3.12.
- Use CI-specific artifact directories and names that include job name, Python
  version, run attempt, and run id.
- Use `--junit-prefix` or suite names so merged CI reports keep job context.
- Split base socket coverage from optional transport-extra jobs, or narrow the
  socket tier's stated coverage to the extras actually installed in CI.

Acceptance:

- PR-required workflow uploads artifacts on pass and failure.
- Socket tests no longer run across every Python version in PR CI.
- Slow and flaky tests are not included in quick CI.
- Quick CI covers the declared minimum Python version.
- A failed matrix job still uploads report, JUnit, and stdout/stderr logs.

Verification:

```bash
uv run pytest -q --junitxml=.easycat/validation/runs/manual-quick/junit.xml -m "not integration_socket and not integration_live and not slow and not flaky"
uv run pytest -q --junitxml=.easycat/validation/runs/manual-socket/junit.xml -m "integration_socket and not integration_live and not flaky"
```

### V1.3 Add Manual And Nightly Workflow Skeletons

Status: pending

Dependencies:

- V1.2

Files:

- `.github/workflows/nightly-validation.yml`
- `.github/workflows/release-validation.yml`

Tasks:

- Add nightly scheduled workflow for full local suite, socket suite, flaky
  quarantine lane, and placeholder live/latency jobs.
- Add manual `workflow_dispatch` workflow for live provider and latency
  validation.
- Protect live jobs with branch/environment conditions.
- Upload artifacts with bounded retention.
- Define strictness per workflow: manual/nightly may allow expected missing
  secrets, but release mode fails when explicitly required providers or
  latency prerequisites are skipped.

Acceptance:

- Workflows can be manually triggered without provider secrets.
- Missing secrets are expected skips in non-strict manual/nightly workflows,
  not failures.
- No live canary runs on untrusted fork PRs.
- Required release checks cannot pass only because every required live or
  latency check skipped.

## V2: Structured Latency Validation

### V2.1 Mark And Factor Latency Tests

Status: pending

Current state:

- `tests/e2e/test_plan_7_latency_benchmark.py` is marked
  `integration_socket`, `integration_live`, and `slow`.
- It logs stage timings and enforces SLO assertions, but does not emit a
  stable validation artifact.

Files:

- `tests/e2e/test_plan_7_latency_benchmark.py`
- `pyproject.toml`

Tasks:

- Add `pytest.mark.latency` to latency tests.
- Add provider and surface markers to latency tests, because current latency
  coverage is live-provider specific.
- Factor reusable latency sample serialization helpers.
- Preserve current test behavior and SLO assertions.
- Separate smoke and sweep collection so smoke can run one low-cost condition
  while sweep runs the broader matrix.

Acceptance:

- `uv run pytest -q -m latency --collect-only` selects latency tests.
- `uv run pytest -q -m "latency and provider_openai" --collect-only`
  selects only OpenAI-backed latency tests.
- Existing direct latency test invocation still works.

### V2.2 Add Canonical Latency Sample JSON

Status: pending

Dependencies:

- V2.1

Files:

- validation CLI/helper module
- `tests/e2e/test_plan_7_latency_benchmark.py`
- test file for latency schema helpers

Tasks:

- Emit sample fields: `sample_id`, `condition_id`, `warmup`,
  `timestamp_source`, provider/model/transport/debug metadata, stage
  durations, `missing_stage_reason`, and `failure_class`.
- Persist latency artifacts inside the isolated validation run directory, and
  update `latency/smoke-latest.json` or `latency/sweep-latest.json` only as a
  convenience pointer/copy.
- Mark p90/p95/p99 as informational unless sample-count eligibility is met.
- Store raw samples, summary eligibility, baseline metadata, environment
  metadata, and clock source in the report.
- Classify provider-side timeout/rate-limit/auth failures separately from
  EasyCat latency regressions.

Acceptance:

- Smoke output contains raw sample and no percentile gate.
- Sweep output contains raw samples plus eligible summaries.
- JSON schema test covers missing-stage handling.
- A smoke run with too few samples reports p90/p95/p99 as ineligible instead
  of failing or claiming a percentile.

Verification:

```bash
uv run easycat validate latency --smoke --report /tmp/latency.json
```

### V2.3 Add Baseline Comparison Helper

Status: pending

Dependencies:

- V2.2

Files:

- validation CLI/helper module
- tests for comparison logic

Tasks:

- Compare only matching provider/model/region/transport/debug conditions.
- Require both relative and absolute regression thresholds.
- Require sample-count eligibility before failing.
- Classify provider/API drift separately from EasyCat regression.
- Keep baselines versioned by condition and require explicit baseline refresh.

Acceptance:

- Unit tests cover pass, relative-only regression, absolute-only regression,
  eligible failure, and ineligible informational status.
- A changed provider/model/region/transport condition refuses to compare with
  a mismatched baseline.

### V2.4 Add Reliability Sampling To Latency/Stress Runs

Status: pending

Dependencies:

- V2.2

Files:

- latency/stress helper module
- `tests/e2e/` latency and stress tests

Tasks:

- Capture event-loop lag, queue depth, dropped frames, journal degraded flag,
  active sessions, and memory growth where practical.
- Attach reliability samples to latency and stress reports with the same
  `sample_id` or `condition_id`.
- Keep reliability signals informational in smoke mode and eligible-gated in
  sweep/stress modes.

Acceptance:

- A stress report contains saturation signals even if all functional
  assertions pass.
- Reliability samples are omitted or marked unavailable with an explicit
  reason when the signal cannot be collected.

## V3: Provider And Protocol Contracts

### V3.1 Create Contract Test Directory

Status: pending

Current state:

- There is no `tests/contracts/` directory.
- The existing provider contract matrix is under `tests/integration/` and is
  focused on wiring, not protocol cassettes.

Files:

- `tests/contracts/`
- `tests/contracts/conftest.py`
- `tests/contracts/provider_surface_matrix.py` or equivalent

Tasks:

- Create shared helpers for contract fixtures.
- Reuse existing fake providers and scripted test harnesses where possible.
- Keep contract tests offline by default.
- Add a canonical provider-surface matrix with provider, surface, adapter,
  protocol, mode, model/API version, required extra, credential env var,
  contract path, cassette path/status, and live-canary status.
- Treat extras as a first-class report dimension, not only an install note.

Acceptance:

- Empty or smoke contract suite runs with `-m contract`.
- A new registered provider surface without a matrix row or explicit exclusion
  fails a local contract validation test.

### V3.2 Preserve Existing Provider Matrix Scope

Status: pending

Files:

- `tests/integration/test_provider_contract_matrix.py`
- `tests/contracts/README.md` if useful

Tasks:

- Clarify that the existing matrix is the factory/session wiring check.
- Do not add protocol cassette logic to that file.
- Ensure every registered STT/TTS config appears in either the wiring matrix,
  the provider-surface contract matrix, or an explicit exclusion list with a
  reason.

Acceptance:

- Future failures distinguish wiring regressions from protocol contract
  failures.
- STT/TTS normalization and contract coverage cannot silently omit a newly
  registered provider such as Cartesia or OpenAI realtime.

### V3.3 Add STT/TTS/VAD/Transport Contract Tests

Status: pending

Dependencies:

- V3.1

Files:

- `tests/contracts/test_stt_provider_contracts.py`
- `tests/contracts/test_tts_provider_contracts.py`
- `tests/contracts/test_vad_provider_contracts.py`
- `tests/contracts/test_transport_contracts.py`

Tasks:

- Validate lifecycle semantics and normalized events.
- Validate stop/close idempotency where required.
- Validate normalized timeout/auth/rate-limit/malformed-frame categories
  with fakes or cassettes.
- Validate provider-surface-specific requirements such as input/output audio
  format, commit/finalization behavior, alignment/marker support, SSML
  support, API version headers, and EventBus requirements where EasyCat
  depends on them.
- Avoid asserting provider output quality.

Acceptance:

- New provider without a contract path fails a local contract test.
- Contracts pass without live credentials.

### V3.4 Add Agent Bridge Contract Tests

Status: pending

Dependencies:

- V3.1

Files:

- `tests/contracts/test_agent_bridge_contracts.py`
- bridge contract helpers or fixtures

Tasks:

- Cover OpenAI Agents, PydanticAI, GenericWorkflow, Remote Responses API,
  LangChain, LangGraph, and Llama Agents.
- Validate the bridge event grammar: text delta, done, tool start/result,
  handoff triple, framework snapshot safety, interruption modes, recorder
  writes, and normalized errors.
- Mark bridge tests with `contract`, `agent_bridge`, provider/bridge metadata,
  and `requires_extra(...)` where optional dependencies are needed.
- Keep optional bridge dependencies as expected skips unless a command or
  release profile explicitly requires that extra.

Acceptance:

- A new bridge without a contract path or explicit exclusion fails contract
  validation.
- Bridge contract tests can report missing optional extras without pretending
  the bridge passed.

### V3.5 Add HTTP/SSE Cassette Proof Of Concept

Status: pending

Dependencies:

- V3.1

Files:

- `tests/contracts/test_http_cassette_redaction.py`
- `tests/contracts/test_sse_cassette_replay.py` if Remote Responses API
  streaming is covered here
- `tests/cassettes/http/`
- `tests/cassettes/sse/`
- dependency updates if adopting `pytest-recording`

Tasks:

- Add one small redacted HTTP cassette.
- Add one small redacted SSE cassette for the Remote Responses API bridge if
  that bridge is in the first cassette scope.
- Configure record mode `none` and network blocking for CI/offline runs.
- Filter authorization headers, provider API keys, tokens, signed URLs,
  timestamps, request IDs, and non-contract IDs.
- Add a test that fails if secret-like values appear in cassettes.
- Define the minimum cassette set per provider surface instead of leaving
  cassette scope as an open-ended question.

Acceptance:

- Contract test can run without network.
- Cassette redaction test fails on injected fake secrets.
- The plan has an explicit cassette scope table for every provider surface:
  required, deferred with reason, or not applicable.

### V3.6 Add WebSocket Cassette Proof Of Concept

Status: pending

Dependencies:

- V3.1

Files:

- `tests/contracts/test_ws_cassette_replay.py`
- `tests/cassettes/ws/`

Tasks:

- Define a schema with provider, surface, provider API version, redaction
  version, capabilities snapshot ref, frames, direction, opcode, kind,
  payload assertion, and redacted fields.
- Add one small replay fixture.
- Assert frame order, lifecycle transitions, normalized event kind, required
  parse fields, normalized error category, and audio metadata.
- Do not store long generated audio.

Acceptance:

- Offline WebSocket cassette replay proves parser compatibility.
- Schema version is validated.

### V3.7 Add Schema Drift Fingerprints

Status: pending

Dependencies:

- V3.5
- V3.6

Files:

- `tests/contracts/test_provider_capability_reports.py`
- helper module for schema fingerprints

Tasks:

- Compute observed schema fingerprints for request payloads, response/event
  payloads, and normalized errors.
- Add provider-surface schema registry entries that separate required
  outbound fields, required inbound event names, optional observed fields,
  and provider-specific enum values EasyCat branches on.
- Report `unchanged`, `additive_warning`, `breaking_failure`, or `unknown`.
- Treat missing required fields, changed enum values used by EasyCat,
  content-type changes, and error-shape changes as failures.

Acceptance:

- Additive unknown field test produces warning.
- Missing required field test fails.
- A provider-specific enum change used by EasyCat fails as
  `breaking_failure`.

## V4: Live Canaries And Provider Reports

### V4.1 Add Provider Capability Report Model

Status: pending

Dependencies:

- V0.2

Files:

- validation CLI/helper module
- tests for provider report serialization

Tasks:

- Implement JSON shape with provider, surface, adapter, `live_checked_at`,
  API version, auth env var presence, capabilities, models/voices where
  applicable, contract status, schema status, latency, and failure class.
- Include protocol/mode, adapter version, required extra, credential env var
  name, input and output audio formats, streaming/finalization behavior,
  marker/alignment/SSML support, and API version header behavior where
  applicable.
- Decide whether providers expose a formal capability/version protocol or
  reports may derive capabilities by duck-typing configs and adapters.
- Redact provider-specific identifiers unless they are safe, low-cardinality
  capability values.

Acceptance:

- Report can represent pass, expected skip, auth/quota failure, and provider
  drift.

### V4.2 Implement `easycat validate live`

Status: pending

Dependencies:

- V1.1
- V4.1

Files:

- `src/easycat/cli/validate.py`
- provider-specific live test wrappers or pytest marker selection

Tasks:

- Add repeatable `--provider`.
- Add optional `--surface` or provider-surface selection before provider live
  checks need to distinguish OpenAI batch STT from realtime STT, or speech
  providers from agent bridges.
- Run provider-specific live smoke only when credentials are present.
- If a provider was explicitly requested, missing credentials fail in strict
  or release mode and skip only in exploratory/manual non-strict mode.
- Pass secrets through environment variables only.
- Classify failures as `easycat_regression`, `provider_drift`,
  `provider_outage`, `auth_or_quota`, `network`, or `environment`.
- Emit provider capability reports.

Acceptance:

- Missing secret produces expected skip.
- Explicitly requested provider in strict mode produces
  `failed_missing_required_secret` when its credential is absent.
- Configured provider produces a capability report.
- No secret values appear in JSON.

### V4.3 Harden Live Canary CI

Status: pending

Dependencies:

- V4.2

Files:

- `.github/workflows/nightly-validation.yml`
- `.github/workflows/release-validation.yml`

Tasks:

- Run live canaries only on protected branches/environments.
- Avoid live provider jobs on untrusted fork PRs.
- Mask derived sensitive values with `::add-mask::`.
- Upload only redacted artifacts.
- Make credential env-var mapping explicit in workflow `env:` blocks or job
  documentation without echoing values.

Acceptance:

- Workflow conditions make the secret exposure path explicit.
- Missing secrets skip provider jobs.
- Release-mode live jobs fail missing required secrets.

## V5: Stress, Benchmarks, And Release Gates

### V5.1 Wrap Journal Benchmark In Validation Artifacts

Status: pending

Current state:

- `perf/bench_journal.py` exists outside pytest.

Files:

- `perf/bench_journal.py`
- validation CLI/helper module

Tasks:

- Keep existing benchmark behavior.
- Add JSON output compatible with validation report artifacts.
- Add optional comparison to baseline.
- Decide later whether to adopt `pytest-benchmark`.

Acceptance:

- `uv run python perf/bench_journal.py` still works.
- Validation artifact includes raw benchmark run and summary.

### V5.2 Add Stress Saturation Signals

Status: pending

Current state:

- Stress-like E2E tests exist under `tests/e2e/`, mostly gated by existing
  `integration_socket`, `integration_live`, and `slow` markers.
- The `stress` marker is registered, but existing stress-like tests are not
  consistently marked with it yet.

Files:

- stress/e2e tests under `tests/e2e/` or `tests/stress/`
- helper module for event-loop lag/queue-depth sampling

Tasks:

- Add `pytest.mark.stress` where appropriate.
- Capture active sessions, queue depths, dropped frames, event-loop lag,
  journal degraded flag, and memory growth where practical.
- Preserve p95/p99 only when sample counts are high enough.
- Add `pytest.mark.stress` to existing stress-like E2E tests and separate
  local stress from live provider soak.
- Define timeout and cancellation cleanup expectations so stress failures do
  not leave sessions, transports, or provider streams running.

Acceptance:

- Stress report includes saturation signals, not only pass/fail.
- Stress teardown verifies no leaked sessions or pending runtime tasks when
  practical.

### V5.3 Add Release Validation Workflow

Status: pending

Dependencies:

- V1.2
- V2.2
- V4.2

Files:

- `.github/workflows/release-validation.yml`

Tasks:

- Build sdist and wheel.
- Install wheel into a clean environment.
- Run import smoke and `easycat doctor --json`.
- Run quick tests against the installed wheel.
- Run live provider smoke for configured credentials.
- Run latency sweep when provider prerequisites exist.
- Upload distribution and validation artifacts.
- Run installed-package checks outside the source tree, clear `PYTHONPATH`,
  and assert `easycat.__file__` points into site-packages.
- Fail strict release validation when required provider, latency, or flaky
  quarantine checks skip unexpectedly.

Acceptance:

- Release validation can be manually triggered.
- It validates installed package behavior, not only editable source.
- A release run cannot pass by skipping every required live/provider/latency
  check.

## V6: Optional Observability API

### V6.1 Add No-Op-Safe OTel Spans

Status: pending

Files:

- session/stage/provider modules around session, STT, agent, TTS, transport,
  and journal boundaries
- optional observability helper module

Tasks:

- Use `opentelemetry-api` only in core, if dependency policy allows.
- Add spans around the documented span tree.
- Emit stable `easycat.*` attributes.
- Add GenAI attributes where appropriate, but do not rely on them for EasyCat
  dashboards.
- Avoid content capture by default.

Acceptance:

- Without SDK/exporter, behavior is unchanged.
- With SDK/exporter configured by a host app, one voice turn produces a
  coherent trace.

### V6.2 Add Low-Cardinality Metrics

Status: pending

Dependencies:

- V6.1

Files:

- observability helper module
- tests for attribute redaction/cardinality policy

Tasks:

- Add histograms for turn/stage/journal latency.
- Add counters for turns, errors, bytes, frames, disconnects, and dropped
  frames.
- Add observable gauges for active sessions, queue depth, event-loop lag, and
  journal degraded flag.
- Enforce forbidden attribute list in tests.

Acceptance:

- Metric names and attributes match [reference.md](reference.md).
- Tests fail if forbidden attributes such as session IDs or transcripts are
  added.

## Dependency Map

```text
V0.1 -> V0.3 -> V1.1 -> V1.2
V0.2 -> V0.3 -> V1.1
V1.1 -> V2.2 -> V2.3
V2.2 -> V2.4
V3.1 -> V3.3
V3.1 -> V3.4
V3.1 -> V3.5 -> V3.7
V3.1 -> V3.6 -> V3.7
V0.2 -> V4.1 -> V4.2 -> V4.3
V1.2 + V2.2 + V4.2 -> V5.3
V6 can start after V1, but should wait until names and artifacts settle.
```

## First PR Checklist

The first PR should include only:

- V0.1 marker registration and strict pytest config where safe.
- V0.2 validation report model.
- V0.3 `scripts/validate.py quick/socket`.
- V0.5 contributor workflow docs that label the public CLI as planned.

Do not include live providers, cassettes, latency rewrites, OpenTelemetry, or
CI workflow splits in the first PR. The goal is to make local validation easy
before making it comprehensive.
