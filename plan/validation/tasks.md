# Validation Implementation Tasks

Status: implementation substantially complete; small followups remain. See
[Post-Implementation Audit](#post-implementation-audit-2026-05-26) below.

This is the execution backlog for [reference.md](reference.md). It is ordered
to make local validation useful before adding live provider, latency, release,
and observability gates.

Current-state caveat: the static inspection note from 2026-05-21 is stale.
As of the 2026-05-26 audit, `easycat validate` shipped with public validation
commands, `scripts/validate.py` became a shim over `easycat.validation.runner`,
and the validation JSON artifact format lives in `easycat.validation.report`.
The 2026-06-05 maintenance update confirmed the CLI surface includes
`quick`, `socket`, `stress`, `contracts`, `latency`, `live`, and `report`. The
per-milestone statuses below have been updated to reflect the audit; see
[Post-Implementation Audit](#post-implementation-audit-2026-05-26) for the
remaining followup list.

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

## Post-Implementation Audit (2026-05-26)

A repository audit on 2026-05-26 found that V1 through V6 are substantively
implemented; the per-milestone statuses below have been updated accordingly.
The audit verified:

- Module and file presence for every milestone's listed files (CLI commands,
  validation modules, contract tests, workflows, observability stubs).
- Test execution: 164 tests pass across `tests/validation/`,
  `tests/cli/test_validate.py`, `tests/cli/test_latency_validation.py`,
  `tests/contracts/`, `tests/test_validation_markers.py`, and
  `tests/test_observability.py`.
- Workflow content for `.github/workflows/ci.yml`,
  `.github/workflows/nightly-validation.yml`, and
  `.github/workflows/release-validation.yml`.

The audit did not exhaustively trace every per-task acceptance bullet against
its implementation, so individual gaps may still exist within shipped
milestones. Any such gaps should be filed as new tasks rather than reopening
a milestone wholesale.

### Closed Followups

- **N1 — Replace nightly latency placeholder.** Closed. `nightly-validation.yml`
  now runs `easycat validate latency --require-samples` and uploads latency
  validation artifacts.
- **N2 — Migrate stress test to public sampler.** Closed.
  `tests/e2e/test_plan_2_sustained_stress.py` imports
  `EventLoopLagSampler` from `easycat.validation.reliability`, guarded by
  `tests/validation/test_stress_uses_public_sampler.py`.

### Outstanding Work

- **N3 — Deep acceptance-bullet audit.** For each shipped milestone,
  spot-check the listed acceptance bullets against actual tests and code to
  surface any gaps the file-presence audit missed. File findings as
  additional N-tasks rather than reopening a milestone.

## V0: Validation Foundation

### V0.1 Register Validation Markers

Status: completed

Current verified state:

- `pyproject.toml` registers `integration_local`, `integration_socket`,
  `integration_live`, `slow`, `contract`, `latency`, `stress`, `release`,
  `flaky`, provider markers, surface markers, `agent_bridge`,
  `requires_extra(name)`, and `provider(name)`, with
  `strict_markers = true`.
- `tests/conftest.py` delegates provider/surface marker checks and flaky
  quarantine metadata checks to `tests/_marker_lint.py`.
- Validation-specific markers are used by the contract, latency, stress,
  release, live, and provider-surface test lanes.

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

Current verified state:

- `src/easycat/validation/report.py` defines the validation JSON report model:
  `ValidationRun`, `ValidationCheck`, `ValidationSkip`, `ValidationFailure`,
  `ArtifactRef`, `GitMetadata`, `ValidationEnvironment`, `ProviderCheck`, and
  `ProviderCheckState`.
- `ValidationRun.to_dict()` / `ValidationRun.to_json()` serialize
  `schema_version`, `redaction_version`, `kind`, `run_id`, `command`,
  timestamps, `duration_s`, `status`, `exit_code`, `tool_exit_codes`, `git`,
  `environment`, `checks`, `skips`, `failures`, `latency`, `reliability`,
  `providers`, `provider_reports`, `extras`, and `artifacts`.
- `ProviderCheckState` represents `not_requested`, `skipped_missing_secret`,
  `failed_missing_required_secret`, `passed`, and `failed`.
- `ValidationEnvironment` serializes environment metadata as env-var presence
  booleans only; it does not serialize environment variable values.
- `src/easycat/validation/redaction.py` owns report-boundary redaction through
  `redact_text`, `redact_runtime_secrets`, `redact_value`, `redact_command`,
  `UNSAFE_TEXT_FIELDS`, and secret-like key detection.
- `tests/cli/test_validate.py` verifies deterministic required-field
  serialization, secret-like and unsafe-value redaction, pass/fail/expected
  skip representation, and the difference between expected missing-secret
  skips and required-secret failures.
- `tests/validation/test_redaction_property.py` verifies redaction idempotence,
  runtime-secret removal, key-based redaction, domain-specific unsafe text
  placeholders, sensitive-pattern detection, and split secret-flag redaction.

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

Current verified state:

- `scripts/validate.py` is a compatibility shim that imports
  `easycat.validation.runner.main` and exits with its return code.
- `src/easycat/validation/runner.py` owns reusable slice execution through
  `run_validation_slice(...)`; `VALIDATION_SELECTORS` currently includes
  `quick`, `socket`, `stress`, and `contracts`.
- The current `quick` selector is
  `not integration_socket and not integration_live and not slow and not stress and not flaky`;
  the current `socket` selector is
  `integration_socket and not integration_live and not flaky`.
- `run_validation_slice(...)` creates isolated
  `.easycat/validation/runs/<run_id>/` directories, writes `junit.xml`,
  `stdout.log`, `stderr.log`, and `report.json`, atomically updates
  `latest.json`, stores public validation `exit_code` separately from
  `tool_exit_codes["pytest"]`, and redacts stdout/stderr/JUnit content.
- `tests/cli/test_validate.py` verifies quick slice command selection,
  report/JUnit/log/latest artifact writes, failed-pytest report writes,
  isolated run directories, and `main(...)` dispatch for socket/stress/contracts
  slices.

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

Current verified state:

- The `flaky` marker is registered in `pyproject.toml`.
- `tests/_marker_lint.py` validates that every flaky test declares `issue`,
  `owner`, and `review_by`, rejects stale review dates, and rejects
  release-scoped flaky tests.
- `tests/conftest.py` runs the flaky metadata lint during collection, and
  validation slice selectors exclude `flaky`.

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

Current verified state:

- `CONTRIBUTING.md` is the contributor entry point. Its quick start uses
  `uv sync --group dev`, `just`, `just check`, `uv run easycat docs`,
  `uv run easycat docs --json`, `uv run easycat explain json-schema`,
  `uv run easycat doctor`, `uv run easycat doctor --json`, and
  `uv run easycat doctor --env-file .env --json`.
- The development-loop table maps every public `justfile` recipe to its raw
  command, including test, lint, format, type, coverage, validation, report,
  pre-PR, and pre-commit tasks.
- The validation-slices table lists the current public `easycat validate` lanes
  with repo-local `uv run easycat validate` commands, including
  `uv run easycat validate quick --json`,
  `uv run easycat validate contracts --json`,
  `uv run easycat validate release --json`,
  `uv run easycat validate report .easycat/validation/latest.json` and
  `uv run easycat validate report .easycat/validation/latest.json --json`.
- The validation chooser table maps common change types to quick, socket,
  contracts, stress, latency, live, release, and report commands so
  contributors can choose the narrowest useful validation lane before a PR.
- The docs/onboarding maintenance map tells contributors which targeted guard
  to run when editing the root README chooser, README command hints,
  command-hint extraction, docs route map, examples matrix, teaching ladder,
  generated teaching blocks, scaffold templates, contributor guide,
  validation docs, or maintained Markdown links; the named `just` recipes are
  `guard-docs`, `guard-teaching`, `guard-examples`, `guard-templates`,
  `guard-contributing`, and `guard-markdown`.
- The marker taxonomy documents strict pytest markers from `pyproject.toml`,
  provider/surface pairing, flaky quarantine metadata, and the rule that
  validation slices deselect `flaky`.
- The RunBundle golden-test section tracks public helpers exported by
  `easycat.debug.testing`.
- `tests/test_contributing.py` verifies quick-start CLI commands, report
  commands, `justfile` parity, public validation lanes, repo-local `uv run`
  validation commands, validation chooser parity, marker taxonomy coverage,
  RunBundle helper coverage, docs/onboarding maintenance commands, and that
  this plan still includes the contributor quick command.
- `tests/test_docs_index.py` verifies the `CONTRIBUTING.md` docs route exposes
  validation report commands with `uv run` prefixes.

Files:

- `CONTRIBUTING.md` if present, otherwise top-level `README.md`
- validation README/reference updates as needed

Historical task scope before V1.1 shipped:

- Document the script-first command used at the time:
  `uv run python scripts/validate.py quick`.
- Label `easycat validate quick` as the planned public replacement until V1.1
  landed. Current repo contributor docs now use
  `uv run easycat validate quick`.
- Document `--report PATH` for persisted validation JSON and reserve `--json`
  for stdout machine-readable CLI envelopes.
- Document when to run socket, live, latency, and release checks.
- Document flaky marker policy.
- Document artifact directory and cleanup expectations.
- Add a provider validation table or link to the validation reference showing
  provider surface, extra, env var, default mode/model, contract status,
  cassette status, and live command.

Acceptance at the time:

- A new contributor can find the quick command from top-level docs.
- The docs do not require live provider credentials for normal PR work.
- The docs did not imply `easycat validate` existed before V1.1.

## V1: First-Class CLI And CI Artifacts

### V1.1 Move Validation Into CLI

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V0.2
- V0.3

Current verified state:

- `src/easycat/cli/_app.py` registers top-level `init`, `doctor`, `docs`,
  `explain`, `inspect`, `replay`, plus the `bundles` and `validate` groups.
- The bare `easycat` journey menu includes `Scaffold`,
  `Debug with the journal`, `Validation`, and `Docs and guidance`; the
  `Validation` section points at `easycat validate`.
- `easycat validate` exposes `quick`, `socket`, `stress`, `contracts`,
  `latency`, `live`, `release`, and `report`.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V1.1, or V0.3 if CI temporarily calls the script

Current verified state:

- `.github/workflows/ci.yml` runs `easycat validate quick` on every Python
  version advertised by the package classifiers: `3.11`, `3.12`, `3.13`,
  and `3.14`.
- Quick validation uses `strategy.fail-fast: false`, a job timeout, isolated
  CI artifact directories, and `--junit-prefix` values that include the Python
  version.
- The socket validation job runs once on Python `3.12` through
  `easycat validate socket`.
- Quick and socket jobs upload validation report JSON, JUnit XML,
  stdout/stderr logs, and socket WebRTC stats when produced, using
  `if: always()`.
- The workflow includes a Python `3.12` package build smoke job.
- The live-provider job remains manual via `workflow_dispatch`.
- CI no longer uses pytest `-x`.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V1.2

Current verified state:

- `.github/workflows/nightly-validation.yml` runs on a schedule and
  `workflow_dispatch`; it has `full-local`, `quick`, `socket`, `stress`,
  `flaky-quarantine`, `live-canaries`, and `latency` jobs.
- Nightly `live-canaries` and `latency` are gated to protected, non-PR runs
  and use the `live-validation` environment.
- Nightly latency is a real `easycat validate latency --require-samples` job,
  receives `OPENAI_API_KEY` only on the validation step, masks it before use,
  and uploads artifacts with `if: always()`.
- `.github/workflows/release-validation.yml` is a manual
  `workflow_dispatch` workflow using the `release-validation` environment.
- Release validation builds the sdist and wheel, installs the wheel into a
  clean temporary venv outside the workspace, runs installed-wheel smoke tests,
  quick validation, stress validation, flaky collection, strict live validation,
  and latency sweep with `--require-samples` when `OPENAI_API_KEY` is present.
- Release validation rejects unexpected skips and uploads distribution plus
  validation artifacts with bounded retention.

Files:

- `.github/workflows/nightly-validation.yml`
- `.github/workflows/release-validation.yml`

Tasks:

- Add nightly scheduled workflow for full local, quick, socket, stress,
  flaky quarantine, live-canary, and latency jobs.
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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `tests/e2e/test_plan_7_latency_benchmark.py` is marked
  `integration_socket`, `integration_live`, `latency`, `provider_openai`,
  `slow`, `surface_agent`, `surface_stt`, `surface_transport`, and
  `surface_tts`.
- `easycat.validation.latency.latency_pytest_args` factors the CLI into a
  smoke selector
  `tests/e2e/test_plan_7_latency_benchmark.py::test_single_full_stack_latency_probe`
  and a sweep selector
  `tests/e2e/test_plan_7_latency_benchmark.py::test_latency_benchmark_by_pipeline_flags`.
- The benchmark appends canonical `LatencySample` JSON when
  `EASYCAT_LATENCY_SAMPLES_PATH` is set; `easycat validate latency` turns
  those samples into structured latency artifacts under the isolated
  validation run directory.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V2.1

Current verified state:

- `LatencySample.to_dict()` serializes `sample_id`, `condition_id`,
  `warmup`, `timestamp_source`, `provider`, `model`, `transport`, `debug`,
  `stages`, `missing_stage_reason`, and `failure_class`.
- `build_latency_artifact(...)` emits `schema_version`, `kind` value
  `latency_validation`, `mode`, `generated_at`, `baseline`, `environment`,
  `clock_source` value `time.monotonic` (`clock_source=time.monotonic`),
  `samples`, `reliability_samples`, `summary`, `percentiles`, and
  `budget_violations`.
- `run_latency_validation(...)` stores raw samples at
  `runs/<run_id>/latency/samples.json`, writes the mode artifact at
  `runs/<run_id>/latency/<mode>.json`, updates
  `latency/<mode>-latest.json`, and attaches a `latency` artifact reference
  to the validation report.
- Smoke artifacts keep percentile and budget gates informational when
  sample-count eligibility is too low; sweep artifacts can include eligible
  summaries, budget checks, and baseline comparison data.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V2.2

Current verified state:

- `compare_latency_baseline(...)` returns `schema_version`, `kind` value
  `latency_baseline_comparison`, aggregate `status`, serialized
  `thresholds`, and per-condition `conditions` built from non-warmup,
  successful `samples`.
- Per-condition comparison requires matching `provider`, `model`, `transport`,
  and `debug` signatures plus a versioned
  `baseline.conditions[<condition_id>].version`; mismatches return
  `provider_api_drift` with reasons `condition_mismatch`,
  `mixed_condition_signature`, or `baseline_version_missing`, and
  `refresh_required=True`.
- Regression failure requires both `relative_regression` and
  `absolute_regression_ms` on the configured `regression_percentile`, plus at
  least `min_samples` current and baseline values. Low sample counts stay
  informational with reason `ineligible_sample_count`.
- Eligible regressions return `easycat_latency_regression` and per-condition
  fields `condition_id`, `baseline_version`, `current_count`,
  `baseline_count`, `percentile`, `current_<percentile>_ms`,
  `baseline_<percentile>_ms`, `delta_ms`, and `relative_delta`.
- `run_latency_validation(..., baseline_path=...)` embeds the comparison in
  the latency artifact, records a separate `latency.baseline` check, and sets
  `latency_baseline` or `latency_baseline_regression` tool exit codes only for
  load failures or failing eligible regressions.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V2.2

Current verified state:

- `ReliabilitySample.to_dict()` serializes `sample_id`, `condition_id`,
  `mode`, `informational`, `eligible`, and nested `signals`;
  `ReliabilitySignals.to_dict()` includes present `event_loop_lag_ms`,
  `queue_depth`, `dropped_frames`, `journal_degraded`, `active_sessions`,
  `memory_growth_kib`, and `unavailable_reason` values.
- Public `capture_reliability_sample(...)` marks `smoke` samples
  informational and ineligible, marks non-smoke modes such as `sweep` and
  `stress` eligible, and sets `unavailable_reason` when no probe returns a
  value.
- `build_latency_artifact(...)` embeds `reliability_samples`;
  `build_reliability_artifact(...)` emits `schema_version`, `kind` value
  `reliability_validation`, `generated_at`, `samples`, `summary`, and
  `budget_violations`.
- `evaluate_reliability_budgets(...)` evaluates only `eligible` samples,
  reports overall and `condition:<condition_id>` scopes, and the default
  budgets cover `event_loop_lag_ms`, `memory_growth_kib`, `dropped_frames`,
  and `journal_degraded`.
- `run_latency_validation(...)` passes `EASYCAT_RELIABILITY_SAMPLES_PATH` to
  the latency benchmark at `runs/<run_id>/latency/reliability.json`, embeds
  parsed samples in the latency artifact, treats malformed reliability JSON as
  `reliability.samples`, and records `reliability.budget` /
  `reliability_budget` for eligible budget violations.
- `run_validation_slice(...)` passes `EASYCAT_RELIABILITY_SAMPLES_PATH` to
  quick/socket/stress slices at `runs/<run_id>/reliability/samples.json`,
  emits a top-level reliability artifact when samples are present, and attaches
  a `reliability` artifact reference to the validation check.
- `tests/e2e/test_plan_2_sustained_stress.py` imports the public
  `EventLoopLagSampler` and appends stress reliability samples with condition
  IDs; those current e2e stress samples are still informational/ineligible, so
  they document saturation signals without gating the legacy stress tests.

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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `tests/contracts/` exists with offline STT, TTS, VAD, transport, agent
  bridge, HTTP/SSE/WebSocket cassette, provider capability report, provider
  report, and provider-surface matrix contract tests.
- Current contract test files are `test_agent_bridge_contracts.py`,
  `test_http_cassette_redaction.py`,
  `test_provider_capability_report_model.py`,
  `test_provider_capability_reports.py`, `test_provider_reports.py`,
  `test_provider_surface_matrix.py`, `test_sse_cassette_replay.py`,
  `test_stt_provider_contracts.py`, `test_transport_contracts.py`,
  `test_tts_provider_contracts.py`, `test_vad_provider_contracts.py`, and
  `test_ws_cassette_replay.py`.
- The original provider contract matrix remains under `tests/integration/` and
  is intentionally focused on factory/session wiring, not protocol cassettes.
- `tests/contracts/provider_surface_matrix.py` defines
  `ProviderSurfaceContract`, `PROVIDER_SURFACE_CONTRACTS`,
  `EXPLICIT_PROVIDER_SURFACE_EXCLUSIONS`, and
  `missing_registered_provider_surfaces()`.
- Each provider-surface row records `provider`, `surface`, `adapter`,
  `protocol`, `mode`, `model_api_version`, `required_extra`,
  `credential_env_var`, `contract_path`, `cassette_path`, `cassette_status`,
  `live_canary_status`, and `expected_skip_reason`.
- `tests/contracts/test_provider_surface_matrix.py` enforces that rows have
  the required report dimensions, no duplicate keys, existing contract paths,
  required cassette files when `cassette_status=required`, and no missing
  registered STT/TTS/VAD/transport provider surfaces unless explicitly
  excluded.
- `tests/contracts/README.md` documents that protocol contracts live under
  `tests/contracts/`, while `tests/integration/test_provider_contract_matrix.py`
  stays scoped to the factory/session wiring seam.

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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `tests/integration/test_provider_contract_matrix.py` is the
  `factory/session wiring seam`, not a protocol cassette suite; its docstring
  explicitly excludes protocol cassette scope.
- The wiring matrix builds `_STT_CONFIG_CLASSES` from
  `easycat.stt.factory._PROVIDER_TO_CONFIG` and `_TTS_CONFIG_CLASSES` from
  `easycat.tts.factory._PROVIDERS`, so newly registered STT/TTS providers are
  auto-parametrized across every STT x TTS session pair.
- Each provider pair runs two phases: real
  `create_stt_provider_from_config` / `create_tts_provider_from_config`
  dispatch plus EventBus injection checks against `_CONFIG_TO_PROVIDER`, then a
  scripted `create_session()` lifecycle smoke with fake STT/TTS/VAD providers.
- `test_registry_covers_every_known_config` guards the known STT configs
  `OpenAISTTConfig`, `OpenAIRealtimeSTTConfig`, `DeepgramSTTConfig`,
  `ElevenLabsSTTConfig`, and `CartesiaSTTConfig`, and the known TTS configs
  `OpenAITTSConfig`, `DeepgramTTSConfig`, `ElevenLabsTTSConfig`, and
  `CartesiaTTSConfig`, so OpenAI realtime and Cartesia cannot silently fall out
  of wiring coverage.
- `tests/contracts/test_provider_surface_matrix.py` keeps protocol coverage
  separate by requiring every registered STT/TTS/VAD/transport surface to have
  a contract row or explicit exclusion through
  `missing_registered_provider_surfaces()`.
- `tests/contracts/README.md` documents the split: the integration matrix
  proves factory/session wiring, while `tests/contracts/` owns provider
  protocol contracts, cassette replay, schema drift fingerprints, and bridge
  event grammar.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V3.1

Current verified state:

- `tests/contracts/test_stt_provider_contracts.py` is marked `contract`,
  `surface_stt`, and provider `matrix`; it verifies STT matrix rows, provider
  protocol conformance, start/send/commit/end lifecycle calls, normalized
  `STTEventType.PARTIAL` and `STTEventType.FINAL` events, `AudioChunk`
  input at `PCM16_MONO_16K`, and repeat `end_stream()` behavior. Current STT
  rows cover `openai`, `openai-realtime`, `deepgram`, `elevenlabs`, and
  `cartesia`.
- `tests/contracts/test_tts_provider_contracts.py` is marked `contract`,
  `surface_tts`, and provider `matrix`; it verifies TTS matrix rows, provider
  protocol conformance, `TTSInput` coercion, normalized `TTSEventType.AUDIO`
  and `TTSEventType.MARKERS` events, `PCM16_MONO_24K` output audio, marker
  passthrough, `supports_ssml=False`, and idempotent `stop()` / `cancel()`.
  Current TTS rows cover `openai`, `deepgram`, `elevenlabs`, and `cartesia`.
- `tests/contracts/test_vad_provider_contracts.py` is marked `contract`,
  `surface_vad`, and provider `offline-fake`; it verifies the VAD matrix rows
  for `silero`, `funasr`, `ten`, and `krisp`, provider protocol conformance,
  configuration passthrough, `PCM16_MONO_16K` input, and normalized
  `VADStartSpeaking` / `VADStopSpeaking` events.
- `tests/contracts/test_transport_contracts.py` is marked `contract`,
  `surface_transport`, and provider `offline-fake`; it verifies transport
  matrix rows for `local`, `websocket`, `twilio`, `webrtc`, and
  `webtransport`, provider protocol conformance, connect/disconnect,
  send/receive delivery semantics, failed sends before connect, and
  idempotent `clear_audio()`.
- `tests/contracts/provider_surface_matrix.py` maps all current STT, TTS, VAD,
  and transport rows to those contract files, while
  `missing_registered_provider_surfaces()` fails if a registered provider
  surface lacks a row or explicit exclusion.
- The current offline contracts validate lifecycle semantics, normalized
  events, audio-format expectations, marker passthrough, EventBus-adjacent
  provider protocol shape, and idempotency behavior; provider error-taxonomy
  and live-output quality checks remain outside these offline surface contract
  files.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V3.1

Current verified state:

- `tests/contracts/test_agent_bridge_contracts.py` is marked `contract`,
  `agent_bridge`, `surface_agent`, and provider `offline-fake`.
- `tests/contracts/provider_surface_matrix.py` maps `openai-agents`,
  `pydantic-ai`, `generic-workflow`, `remote-responses-api`, `langchain`,
  `langgraph`, and `llama-agents` agent bridge rows to
  `tests/contracts/test_agent_bridge_contracts.py`.
- Agent bridge rows record adapter, protocol, mode, `model_api_version`,
  `required_extra`, `credential_env_var`, cassette path/status, live-canary
  status, and `expected_skip_reason`; the importability contract treats a
  missing optional adapter import as an expected skip only when
  `required_extra` and `expected_skip_reason` are set.
- The offline bridge grammar contract asserts that the stream contains
  `text_delta`, `tool_started`, `tool_result`, and `done`, while cursor enter /
  exit, tool `start` / `result`, framework handoff, and state snapshot records
  are written through `AgentRecorder`.
- Interruption contracts assert `CancellationMode.IMMEDIATE_STOP`,
  `record_cancellation_boundary`, pre/post `record_state_snapshot`,
  `record_state_committed`, swallowed post-commit journal failure, skipped
  mutation when commit journaling fails, and JSON-safe `FrameworkStateSnapshot`
  plus `reset()` behavior.
- Current bridge contracts cover recorder writes, handoff records, snapshot
  safety, interruption journal failure modes, and optional-extra metadata; live
  framework behavior and normalized framework error taxonomies remain outside
  this offline fake bridge contract.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V3.1

Current verified state:

- V3.5 is an offline cassette replay/redaction proof built from checked-in
  JSON cassettes; it does not use a live recording harness or
  `pytest-recording`.
- `tests/contracts/test_http_cassette_redaction.py` validates
  `tests/cassettes/http/openai-stt.json` for schema version, redaction
  version, `protocol=http`, redacted authorization headers, and absence of
  unredacted sensitive text via `contains_unredacted_sensitive_text`.
- `tests/contracts/test_sse_cassette_replay.py` validates
  `tests/cassettes/sse/remote-responses-api.json` for schema version,
  redaction version, `protocol=sse`, `provider_api_version=responses-api`,
  redaction, and offline `translate_sse_event` replay of text delta plus
  completion events.
- Both cassette tests inject a fake `Authorization: Bearer sk-testsecret123456`
  value and assert the redaction detector flags it.
- `tests/contracts/provider_surface_matrix.py` is the cassette scope table:
  every provider-surface row has `cassette_path` and `cassette_status`, and
  the current required HTTP/SSE rows are
  `tests/cassettes/http/openai-stt.json` and
  `tests/cassettes/sse/remote-responses-api.json`; other rows are marked
  `deferred` or `not_applicable` with explicit matrix metadata.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V3.1

Current verified state:

- `tests/contracts/test_ws_cassette_replay.py` validates the checked-in
  `tests/cassettes/ws/openai-realtime-stt.json` fixture for
  `schema_version=1`, `redaction_version=1`, `protocol=websocket`, and
  `provider_api_version=realtime`.
- The fixture declares
  `capabilities_ref=tests/contracts/provider_surface_matrix.py` and models
  the `openai-realtime` STT happy-path frame order: client `session.update`,
  server `session.updated`, client `input_audio_buffer.append`, client
  `input_audio_buffer.commit`, then server
  `conversation.item.input_audio_transcription.completed`.
- The replay-order test checks that every frame has `direction`, `opcode`,
  `kind`, and `payload_assertion`; opcodes are limited to `text` / `binary`,
  the session audio input fields are `format`, `transcription`, and
  `turn_detection`, append-before-commit is recorded through
  `requires_prior_append=True`, and the completed frame maps to
  `normalized_event_kind=final_transcript`.
- The fixture stores no generated audio payload; the append frame declares
  `redacted_fields=["audio"]`, and the raw cassette is scanned with
  `contains_unredacted_sensitive_text`.
- `tests/contracts/provider_surface_matrix.py` marks the `openai-realtime`
  STT WebSocket cassette as `cassette_status=required`; other WebSocket
  provider rows are currently `deferred`.
- V3.7 schema fingerprint tests separately pin the inbound OpenAI realtime
  event enum, including `error`; V3.6's checked-in cassette remains a
  happy-path parser-compatibility smoke proof, not an error-frame cassette.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V3.5
- V3.6

Current verified state:

- `tests/contracts/schema_fingerprints.py` provides the schema-drift helper:
  `SchemaDriftStatus`, `DirectionalSchemaRule`, `SchemaFingerprintRule`, and
  `compare_schema_fingerprint` for comparing observed payload dictionaries
  against explicit rules.
- `DirectionalSchemaRule` separates `required_fields`, `optional_fields`,
  `enum_fields`, and `object_required_fields`; `SchemaFingerprintRule` keeps
  `inbound` and `outbound` rules independent.
- The helper reports `unchanged`, `additive_warning`, `breaking_failure`, or
  `unknown`. Missing selected rules or unknown directions return `unknown`;
  additive unknown fields return `additive_warning`.
- Breaking drift reports include `missing_required_fields`, `enum_failures`,
  and `object_shape_failures`, covering missing top-level required fields,
  provider enum changes, `content_type` enum changes, and nested error-object
  shape changes.
- `tests/contracts/test_provider_capability_reports.py` pins the OpenAI
  realtime inbound event enum, including
  `conversation.item.input_audio_transcription.delta`,
  `conversation.item.input_audio_transcription.completed`, `error`,
  `session.created`, `session.updated`, and `transcription_session.updated`.
- The current V3.7 proof is a contract helper plus representative tests over
  observed payload dictionaries; it is not a generated provider schema registry
  for every provider-surface row.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V0.2

Current verified state:

- `src/easycat/validation/provider_capabilities.py` defines the protocol-free
  model types `ProviderCapabilityReport`, `ProviderCapabilities`, and
  `ProviderIdentifier`, and `easycat.validation` exports those types for
  validation callers.
- `ProviderCapabilityStatus` covers `pass`, `expected_skip`, `auth_failure`,
  `quota_failure`, `provider_drift`, and `failure`; `ProviderContractStatus`
  and `ProviderSchemaStatus` carry contract and schema outcomes.
- `ProviderCapabilityReport.to_dict()` emits
  `kind=provider_capability_report`, `schema_version`, `redaction_version`,
  `provider`, `surface`, `adapter`, `protocol`, `mode`, `adapter_version`,
  `required_extra`, `live_checked_at`, `api_version`, nested `auth`,
  `capabilities`, `models`, `voices`, `contract_status`, `schema_status`,
  `latency`, `failure_class`, and `status`.
- `ProviderCapabilities.to_dict()` emits input/output audio formats and
  optional `streaming`, `streaming_behavior`, `finalization_behavior`,
  `markers`, `alignment`, `ssml`, `tts_input_policy`,
  `api_version_header_behavior`, and `provider_options` when present.
- `ProviderIdentifier` preserves safe low-cardinality identifiers through
  `redact_text` and replaces unsafe provider-specific identifiers with
  `[REDACTED_PROVIDER_IDENTIFIER]`; nested capability provider options are
  redacted through `redact_value`.
- `tests/contracts/test_provider_capability_report_model.py` verifies the
  required JSON shape, UTC `live_checked_at` serialization, `redaction_version`,
  TTS input-policy serialization, nested capability redaction, safe model ID
  preservation, unsafe voice ID suppression, `to_json()` round-tripping, and
  pass / expected-skip / auth-failure / quota-failure / provider-drift status
  representation.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V1.1
- V4.1

Current verified state:

- `src/easycat/cli/validate.py` exposes `easycat validate live` with
  repeatable `--provider` and `--surface`, plus `--strict`, `--release`,
  `--json`, `--report`, and `--artifacts-dir`; the command calls
  `run_live_validation` and wraps JSON output with `json_envelope`.
- `src/easycat/validation/provider_reports.py` defines
  `ProviderSurfaceSpec` and `LIVE_PROVIDER_SURFACES` for `stt`, `tts`, and
  `agent_bridge`, with STT/TTS adapters and credential env-var names derived
  from the runtime registries. `select_provider_surfaces`,
  `known_live_providers`, and `known_live_surfaces` provide the selector
  surface for `run_live_validation`.
- `run_live_validation` creates `runs/<run_id>/providers/`, writes
  `report.json`, `latest.json`, `stdout.log`, `stderr.log`, and per-provider
  `provider_capability_report` artifacts keyed as `provider_<provider>_<surface>`.
- Missing credentials in non-strict mode produce `skipped_missing_secret`,
  an expected `ValidationSkip`, and provider report `status=expected_skip`;
  missing credentials for an explicit provider under `--strict`, or any missing
  required live prerequisite under `--release`, produce
  `failed_missing_required_secret`, `failure_class=auth_or_quota`, and provider
  report `status=auth_failure`.
- Configured providers run `_live_pytest_command` with an `integration_live`
  marker expression that includes provider/surface markers and `not flaky`;
  provider commands receive secrets from `env={**os.environ}` rather than CLI
  arguments.
- Runtime output and reports are redacted with `redact_runtime_secrets` and
  `_runtime_secret_values`; selector errors for unknown providers/surfaces are
  classified as `environment`.
- `classify_live_failure` currently emits the live failure classes
  `auth_or_quota`, `provider_quota`, `network`, `provider_drift`,
  `easycat_regression`, and `environment`.
- `tests/cli/test_validate.py` verifies non-strict missing-secret skips,
  strict and release missing-secret failures, configured-provider command
  selection, provider report artifact creation, runtime secret redaction,
  selector failures, quota classification, release command auditing, and the
  standard JSON envelope for the CLI wrapper.

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

Status: completed (verified by 2026-05-26 audit)

Dependencies:

- V4.2

Current verified state:

- `.github/workflows/nightly-validation.yml` has no `pull_request` trigger;
  nightly `live-canaries` and `latency` jobs run only when
  `github.event_name != 'pull_request' && github.ref_protected == true` and use
  the `live-validation` environment.
- Nightly `live-canaries` maps `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`,
  `ELEVENLABS_API_KEY`, and `CARTESIA_API_KEY` from GitHub secrets at job
  scope, masks any non-empty values with `::add-mask::`, then runs
  `easycat validate live --provider openai --surface stt --surface tts`
  without `--strict` or `--release`.
- Because nightly live validation is non-strict, missing live credentials are
  represented by the V4.2 runner as expected provider-check skips and
  redacted provider capability reports instead of failed workflow prerequisites.
- Nightly `latency` uses the same protected non-PR gate and `live-validation`
  environment, scopes `OPENAI_API_KEY` only to the latency validation step,
  masks it before use, and runs
  `easycat validate latency --require-samples`.
- `.github/workflows/release-validation.yml` is manual `workflow_dispatch`,
  uses the `release-validation` environment, maps live provider credential
  names explicitly from GitHub secrets, masks them with `::add-mask::`, and
  runs installed-wheel live validation with
  `"$RELEASE_VENV/bin/easycat" validate live --release --provider openai --surface stt --surface tts`.
- Release validation also verifies installed-package execution outside the
  workspace, runs quick and stress validation from the wheel environment,
  verifies release reports for unexpected skips, and uploads
  `VALIDATION_ARTIFACTS_DIR` plus `dist/**`.
- `tests/test_ci_workflow.py` parses the nightly and release workflows and
  verifies protected non-PR gates, `live-validation` / `release-validation`
  environments, explicit secret env-var mapping, `::add-mask::` masking,
  `actions/upload-artifact@v4` artifact upload with bounded retention, release
  `--release` live validation, and absence of placeholder jobs.

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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `perf/bench_journal.py` remains a standalone benchmark script: its default
  usage is still `uv run python perf/bench_journal.py`, and `main()` writes the
  raw benchmark JSON to `--output`, defaulting to `perf/baseline.json`.
- `run_benchmarks()` exercises both `InMemoryRingBuffer` and `SqliteJournal`
  backends and records `append_latency`, `sustained_rate`, and
  `turn_simulation` metrics for each backend.
- `build_validation_artifact()` wraps a raw run in a validation-compatible
  JSON envelope with `kind=journal_benchmark_validation`, `schema_version=1`,
  `redaction_version=1`, `generated_at`, `summary`, `baseline`, and `raw_run`.
- The summary includes backend-level append latency p50/p90/p99/mean/total,
  sustained-rate actual/dropped/pass state, turn-simulation per-event/total
  timings, run count, and failure entries when sustained-rate targets are not
  met.
- Optional `--artifact`, `--baseline`, and `--max-regression-percent` flags let
  the standalone script write the validation artifact and compare higher-is-worse,
  lower-is-worse, count, and sustained-boolean metrics against a raw or wrapped
  baseline.
- `tests/perf/test_bench_journal.py` verifies raw-run embedding, summary shape,
  baseline regression reporting, and `main()` writing both raw output and the
  validation artifact with `run_benchmarks` monkeypatched.
- V5.1 currently covers a validation-compatible benchmark artifact produced by
  the standalone `perf/bench_journal.py` script; it is not wired into
  `easycat validate stress`.

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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `src/easycat/validation/runner.py` routes `easycat validate stress` through
  marker expression `stress and not integration_live and not flaky`, so local
  stress coverage is separated from live provider soak tests.
- `tests/e2e/test_plan_2_sustained_stress.py` marks the ring-buffer,
  scripted 50-turn, concurrent-session, and live OpenAI stress cases with
  `pytest.mark.stress`.
- The same stress module imports the public `EventLoopLagSampler`, uses
  `append_reliability_sample` to write `ReliabilitySample` records to
  `EASYCAT_RELIABILITY_SAMPLES_PATH`, and emits condition IDs such as
  `fifty_turns_single_session_scripted`,
  `concurrent_sessions_journal_isolation`, and `ten_turns_live_openai`.
- Current stress samples capture available `ReliabilitySignals` values for
  `event_loop_lag_ms`, `queue_depth`, `dropped_frames`, `journal_degraded`,
  `active_sessions`, and `memory_growth_kib` where practical.
- Current E2E stress samples are `informational=True` and `eligible=False`,
  so saturation signals are embedded for diagnosis without newly gating the
  legacy stress tests.
- Public `capture_reliability_sample(...)` treats `stress` mode as eligible,
  and `evaluate_reliability_budgets(...)` reports saturation-threshold
  failures through `reliability.budget` when eligible reliability samples
  exceed budgets.
- `tests/cli/test_validate.py` verifies stress-slice reliability samples are
  loaded from `EASYCAT_RELIABILITY_SAMPLES_PATH`, embedded under top-level
  `reliability`, and linked from the validation check artifact.
- `tests/validation/test_stress_uses_public_sampler.py` guards against
  reintroducing a private event-loop-lag sampler in the stress E2E module.

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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `.github/workflows/release-validation.yml` is a manual `workflow_dispatch`
  workflow using the `release-validation` environment, masking live provider
  secrets before validation commands run.
- The workflow builds release distributions with `uv build --sdist --wheel`,
  creates `RELEASE_VENV` under `$RUNNER_TEMP/easycat-release-venv`, installs
  `easycat[openai,openai-agents]`, and installs release test dependencies
  `pytest`, `pytest-asyncio`, and `hypothesis`.
- Installed-package checks run from `${{ runner.temp }}` with `PYTHONPATH: ""`;
  they assert `easycat.__file__` resolves into `site-packages` and outside the
  GitHub workspace, then run `easycat doctor --json` and
  `tests/cli/test_app.py`.
- The installed wheel runs `validate quick`, `validate stress`, flaky
  collection with `-m flaky`, strict
  `validate live --release --provider openai --surface stt --surface tts`, and
  `validate latency --sweep --require-samples` when `OPENAI_API_KEY` is
  present.
- The workflow verifies generated reports for
  `unexpected release validation skips` and uploads both
  `VALIDATION_ARTIFACTS_DIR` and `dist/**` through
  `actions/upload-artifact@v4` with `if: always()` and `retention-days: 30`.
- `src/easycat/validation/runner.py` provides the local
  `run_release_validation(...)` implementation used by
  `easycat validate release`; it records `release.build`, `release.venv`,
  `release.install`,
  `release.install-test-tools`, `release.import-smoke`, `release.doctor`,
  `release.cli-smoke`, `release.quick`, `release.stress`, `release.contracts`,
  `release.live`, and `release.latency.<mode>` checks.
- The release runner clears `PYTHONPATH`, executes child validation slices from
  an out-of-source working directory, uses the installed wheel pytest command
  through `EASYCAT_VALIDATION_PYTEST_COMMAND`, requires latency samples, and
  fails the release run when any child validation result fails.
- `tests/test_ci_workflow.py` guards the workflow shape, and
  `tests/cli/test_validate.py` verifies the release runner, child-failure
  aggregation, `validate release --json`, and conflicting latency-mode CLI
  errors.

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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `src/easycat/_observability.py` is the OpenTelemetry facade. Core
  `pyproject.toml` has no hard `opentelemetry-api` dependency; `_get_tracer()`
  and `_get_meter()` import OTel opportunistically and return `None` on
  `ImportError`, so `span(...)` and metric helpers are no-ops without a host
  SDK/exporter.
- `SPAN_NAMES` allow-lists `easycat.session`,
  `easycat.transport.receive`, `easycat.vad.detect`, `easycat.stt.stream`,
  `easycat.turn.commit`, `easycat.agent.invoke`, `easycat.agent.tool`,
  `easycat.tts.synthesize`, `easycat.transport.send`, and the reserved
  `easycat.journal.append` span name. Journal append currently emits the
  `easycat.journal.append.latency` metric under V6.2 rather than a runtime span.
- Runtime span wiring is present in `src/easycat/session/_session.py`,
  `src/easycat/session/_audio_router.py`, `src/easycat/session/_turn_runner.py`,
  `src/easycat/stages/vad.py`, `src/easycat/stages/stt.py`,
  `src/easycat/stages/agent.py`, `src/easycat/stages/tts.py`, and
  `src/easycat/stages/transport.py`.
- Span attributes are sanitized through `SPAN_ATTRIBUTE_KEYS`,
  `LOW_CARDINALITY_ATTRIBUTE_KEYS`, `FORBIDDEN_ATTRIBUTE_KEYS`, and
  `_FORBIDDEN_SUBSTRINGS`; the span-only GenAI keys are
  `gen_ai.operation.name`, `gen_ai.request.model`, and `gen_ai.system`.
- `tests/test_observability.py` verifies no-op behavior without OTel, fake
  tracer span creation, sanitized span attributes, a text-turn trace containing
  `easycat.session`, `easycat.agent.invoke`, and `easycat.turn.commit`, and
  representative voice-path spans for transport receive/send, VAD, agent tool,
  and turn commit.
- `docs/observability.md` documents the OpenTelemetry facade as optional,
  no-op without an SDK, PII-scrubbed, and low-cardinality.

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

Status: completed (verified by 2026-05-26 audit)

Current verified state:

- `src/easycat/_observability.py` defines `METRIC_DEFINITIONS` with histogram
  metrics `easycat.turn.latency`, `easycat.stage.latency`,
  `easycat.journal.append.latency`, and `easycat.event_loop.lag`; counter
  metrics `easycat.turns.total`, `easycat.audio.bytes.total`,
  `easycat.audio.frames.total`, `easycat.provider.errors.total`,
  `easycat.session.errors.total`, `easycat.transport.disconnects.total`,
  `easycat.validation.failures.total`, and `easycat.queue.dropped.total`; and
  observable gauges `easycat.sessions.active`, `easycat.queue.depth`, and
  `easycat.journal.degraded`. The stored kind values are `histogram`,
  `counter`, and `observable_gauge`.
- `record_histogram(...)`, `increment_counter(...)`, and `observe_gauge(...)`
  route through `_record_metric(...)`, which enforces each metric kind and
  sanitizes attributes against `LOW_CARDINALITY_ATTRIBUTE_KEYS`.
- Runtime emissions currently cover active sessions, turn latency/counts,
  stage latency, provider/session errors, audio bytes/frames, queue depth,
  queue drops, event-loop lag, journal append latency, and journal degraded
  state.
- The current source tree defines but does not yet emit
  `easycat.transport.disconnects.total` or `easycat.validation.failures.total`.
- Metric wiring is present in `src/easycat/_bounded_queue.py`,
  `src/easycat/session/_session.py`, `src/easycat/session/_turn_runner.py`,
  `src/easycat/session/_audio_router.py`, `src/easycat/runtime/journal.py`,
  and the stage modules under `src/easycat/stages/`.
- `tests/test_observability.py` verifies metric definitions, fake meter
  counter/histogram/gauge behavior, observable-gauge callback behavior,
  queue-depth refreshes, audio counters, provider/session error counters, and
  rejection of span-only GenAI keys on metric attributes.
- `plan/validation/reference.md` lists the same suggested metric names and the
  allowed low-cardinality attribute vocabulary.

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
