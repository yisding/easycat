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
- Treat secrets, transcripts, prompts, phone numbers, and generated provider
  content as unsafe unless redaction is explicit.
- Keep planned public commands documented as planned until the relevant task
  lands.

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

Status: pending

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
  `provider_openai`, `provider_deepgram`, `provider_elevenlabs`, and
  `provider_cartesia`.
- Confirm all current custom markers are registered.
- Add `strict_markers = true` after collection is clean.
- Add `strict_config = true` only after current pytest config emits no
  warnings.

Acceptance:

- `uv run pytest --collect-only -q` completes without unknown marker warnings.
- Current CI marker expressions continue to select the same broad groups until
  CI is intentionally changed in V1.2.

Verification:

```bash
uv run pytest --collect-only -q
uv run pytest -q -m "not integration_socket and not integration_live and not slow"
```

### V0.2 Define Validation Report Model

Status: pending

Files:

- temporary script/helper module chosen for V0, or `src/easycat/cli/validate.py`
  if V1 is pulled forward
- `tests/cli/test_validate.py` or a focused report-model test

Tasks:

- Add typed helpers for the validation JSON envelope:
  `ValidationRun`, `ValidationCheck`, `ValidationSkip`,
  `ValidationFailure`, and artifact references.
- Include `schema_version`, command, timestamps, duration, status, git
  metadata, Python/platform metadata, checks, skips, failures, latency, and
  providers.
- Make JSON serialization deterministic enough for tests.
- Never serialize environment variable values or secret-like strings.

Acceptance:

- Unit tests verify required fields and deterministic serialization.
- A test value that looks like a secret does not appear in serialized output.
- The schema can represent pass, fail, and expected skip.

Verification:

```bash
uv run pytest tests/cli/test_validate.py -q
```

### V0.3 Create `scripts/validate.py quick/socket`

Status: pending

Files:

- `scripts/validate.py`
- tests for report helpers, if helpers live outside the script

Tasks:

- Create the `scripts/` directory if it is still absent.
- Implement `quick` with:
  `uv run pytest -q --junitxml=.easycat/validation/junit.xml -m "not integration_socket and not integration_live and not slow and not flaky"`.
- Implement `socket` with:
  `uv run pytest -q --junitxml=.easycat/validation/junit.xml -m "integration_socket and not integration_live and not flaky"`.
- Create `.easycat/validation/` automatically.
- Emit `.easycat/validation/latest.json`.
- Return the pytest exit code.
- Record command duration and artifact paths.

Acceptance:

- `uv run python scripts/validate.py quick` runs the planned quick selector.
- `uv run python scripts/validate.py socket` runs the planned socket selector.
- A failed pytest run still writes a validation JSON report.
- The JSON report references JUnit XML when it exists.

Verification:

```bash
uv run python scripts/validate.py quick
uv run python scripts/validate.py socket
```

### V0.4 Add Flaky Quarantine Metadata Check

Status: pending

Current state:

- No `flaky` marker is registered or used.

Files:

- `tests/conftest.py` or a new test utility under `tests/`
- `pyproject.toml`

Tasks:

- Define the accepted flaky metadata format in marker kwargs or a nearby
  helper comment.
- Validate that every `flaky` test has issue, owner, and review date.
- Add release validation behavior that fails stale flaky markers.
- Keep PR quick/socket selectors excluding `flaky`.

Acceptance:

- A synthetic or fixture test proves missing flaky metadata fails the
  validation helper.
- Normal test collection remains fast.

Verification:

```bash
uv run pytest --collect-only -q -m flaky
uv run pytest -q -m "not flaky and not integration_live"
```

### V0.5 Document Contributor Workflow

Status: pending

Files:

- `CONTRIBUTING.md` if present, otherwise top-level `README.md`
- validation README/reference updates as needed

Tasks:

- Document the current script-first command:
  `uv run python scripts/validate.py quick`.
- Label `easycat validate quick` as the planned public replacement until V1.1
  lands.
- Document when to run socket, live, latency, and release checks.
- Document flaky marker policy.
- Document artifact directory and cleanup expectations.

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

Tasks:

- Add a Typer command group: `easycat validate`.
- Implement subcommands: `quick`, `socket`, and `report`.
- Preserve JSON/JUnit/artifact options from the script.
- Keep human output concise and Rich-compatible.
- Add the command to the top-level journey menu.

Acceptance:

- `uv run easycat validate quick --json /tmp/easycat-validation.json` works.
- `uv run easycat validate report .easycat/validation/latest.json` renders a
  concise summary.
- CLI tests cover success, failure, and expected skip rendering.
- Bare `easycat` output lists validation only after the command exists.

Verification:

```bash
uv run easycat validate quick --json /tmp/easycat-validation.json
uv run pytest tests/cli/test_validate.py -q
```

### V1.2 Update CI Required Jobs

Status: pending

Dependencies:

- V1.1, or V0.3 if CI temporarily calls the script

Current state:

- `.github/workflows/ci.yml` has one local test matrix on Python 3.12 and
  3.14 with `-m "not integration_socket and not integration_live"`.
- The socket integration job also runs on Python 3.12 and 3.14.
- The live-provider job is manual via `workflow_dispatch`.
- CI does not upload validation JSON or JUnit artifacts.

Files:

- `.github/workflows/ci.yml`

Tasks:

- Change quick test selection to exclude `slow` and `flaky`.
- Add JUnit output and validation JSON artifact upload with `if: always()`.
- Keep quick required on Python 3.12 and 3.14.
- Keep socket required on Python 3.12 only.
- Add package build smoke on Python 3.12.
- Use artifact names that include job name, Python version, and run attempt.

Acceptance:

- PR-required workflow uploads artifacts on pass and failure.
- Socket tests no longer run across every Python version in PR CI.
- Slow and flaky tests are not included in quick CI.

Verification:

```bash
uv run pytest -q --junitxml=.easycat/validation/junit.xml -m "not integration_socket and not integration_live and not slow and not flaky"
uv run pytest -q --junitxml=.easycat/validation/junit.xml -m "integration_socket and not integration_live and not flaky"
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

Acceptance:

- Workflows can be manually triggered without provider secrets.
- Missing secrets are expected skips, not failures.
- No live canary runs on untrusted fork PRs.

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
- Factor reusable latency sample serialization helpers.
- Preserve current test behavior and SLO assertions.

Acceptance:

- `uv run pytest -q -m latency --collect-only` selects latency tests.
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
- Persist `smoke-latest.json` and `sweep-latest.json`.
- Mark p90/p95/p99 as informational unless sample-count eligibility is met.

Acceptance:

- Smoke output contains raw sample and no percentile gate.
- Sweep output contains raw samples plus eligible summaries.
- JSON schema test covers missing-stage handling.

Verification:

```bash
uv run easycat validate latency --smoke --json /tmp/latency.json
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

Acceptance:

- Unit tests cover pass, relative-only regression, absolute-only regression,
  eligible failure, and ineligible informational status.

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

Tasks:

- Create shared helpers for contract fixtures.
- Reuse existing fake providers and scripted test harnesses where possible.
- Keep contract tests offline by default.

Acceptance:

- Empty or smoke contract suite runs with `-m contract`.

### V3.2 Preserve Existing Provider Matrix Scope

Status: pending

Files:

- `tests/integration/test_provider_contract_matrix.py`
- `tests/contracts/README.md` if useful

Tasks:

- Clarify that the existing matrix is the factory/session wiring check.
- Do not add protocol cassette logic to that file.

Acceptance:

- Future failures distinguish wiring regressions from protocol contract
  failures.

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
- Avoid asserting provider output quality.

Acceptance:

- New provider without a contract path fails a local contract test.
- Contracts pass without live credentials.

### V3.4 Add HTTP Cassette Proof Of Concept

Status: pending

Dependencies:

- V3.1

Files:

- `tests/contracts/test_http_cassette_redaction.py`
- `tests/cassettes/http/`
- dependency updates if adopting `pytest-recording`

Tasks:

- Add one small redacted HTTP cassette.
- Configure record mode `none` and network blocking for CI/offline runs.
- Filter authorization headers, provider API keys, tokens, signed URLs,
  timestamps, request IDs, and non-contract IDs.
- Add a test that fails if secret-like values appear in cassettes.

Acceptance:

- Contract test can run without network.
- Cassette redaction test fails on injected fake secrets.

### V3.5 Add WebSocket Cassette Proof Of Concept

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

### V3.6 Add Schema Drift Fingerprints

Status: pending

Dependencies:

- V3.4
- V3.5

Files:

- `tests/contracts/test_provider_capability_reports.py`
- helper module for schema fingerprints

Tasks:

- Compute observed schema fingerprints for request payloads, response/event
  payloads, and normalized errors.
- Report `unchanged`, `additive_warning`, `breaking_failure`, or `unknown`.
- Treat missing required fields, changed enum values used by EasyCat,
  content-type changes, and error-shape changes as failures.

Acceptance:

- Additive unknown field test produces warning.
- Missing required field test fails.

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
- Run provider-specific live smoke only when credentials are present.
- Pass secrets through environment variables only.
- Classify failures as `easycat_regression`, `provider_drift`,
  `provider_outage`, `auth_or_quota`, `network`, or `environment`.
- Emit provider capability reports.

Acceptance:

- Missing secret produces expected skip.
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

Acceptance:

- Workflow conditions make the secret exposure path explicit.
- Missing secrets skip provider jobs.

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
- There is no `stress` marker yet.

Files:

- stress/e2e tests under `tests/e2e/` or `tests/stress/`
- helper module for event-loop lag/queue-depth sampling

Tasks:

- Add `pytest.mark.stress` where appropriate.
- Capture active sessions, queue depths, dropped frames, event-loop lag,
  journal degraded flag, and memory growth where practical.
- Preserve p95/p99 only when sample counts are high enough.

Acceptance:

- Stress report includes saturation signals, not only pass/fail.

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

Acceptance:

- Release validation can be manually triggered.
- It validates installed package behavior, not only editable source.

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
V3.1 -> V3.3
V3.1 -> V3.4 -> V3.6
V3.1 -> V3.5 -> V3.6
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
