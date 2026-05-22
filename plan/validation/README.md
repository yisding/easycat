# Validation Plan

Status: active backlog index.

This folder tracks the validation roadmap. It does not describe shipped
commands unless the current-state section says they exist.

## Document Map

- [tasks.md](tasks.md): implementation backlog, dependencies, acceptance
  checks, and first-PR scope.
- [reference.md](reference.md): supporting strategy, repo inventory,
  marker/CI/artifact designs, provider-contract notes, and research links.
- This file: current status and navigation.

## Current State

Snapshot: V0 implementation pass on 2026-05-22.

Implemented:

- `pyproject.toml` registers the base, validation, provider, surface,
  optional-extra, and flaky/release markers listed in [reference.md](reference.md),
  with `strict_markers = true`.
- `tests/conftest.py` enforces provider/surface metadata when validation tests
  declare either side, and enforces flaky quarantine metadata:
  `@pytest.mark.flaky(issue="...", owner="...", review_by="YYYY-MM-DD")`.
- `scripts/validate.py quick` and `scripts/validate.py socket` exist as the V0
  script-first validation entry points.
- Validation runs write isolated artifacts under `.easycat/validation/runs/`
  and update `.easycat/validation/latest.json` after a complete report exists.
- `src/easycat/validation/report.py` defines the V0 validation JSON envelope,
  provider credential states, artifact references, and report-boundary redaction.
- `.github/workflows/ci.yml` has `lint`, local tests, socket integration
  tests, and manual live-provider tests.
- The public CLI currently registers `init`, `doctor`, `explain`, `bundles`,
  and `inspect`. There is no `easycat validate` command.
- The existing provider matrix at
  `tests/integration/test_provider_contract_matrix.py` validates provider
  registry, factory, EventBus injection, and session wiring. It is not a
  protocol cassette suite.
- `tests/e2e/test_plan_7_latency_benchmark.py` already measures voice-loop
  latency and stage breakdowns, and is now marked `latency`, but it does not
  emit a structured latency artifact yet.
- CLI testing already has a focused plan in `tests/cli/TEST_PLANS.md`.
- Broader E2E planning in [../testing/](../testing/README.md) is backed by
  concrete tests under `tests/e2e/`.

Planned but not implemented:

- `easycat validate ...` command group.
- JUnit and validation artifact upload in CI.
- HTTP/WebSocket provider cassettes and schema drift fingerprints.
- a canonical provider-surface matrix for provider, surface, adapter,
  protocol, extra, credential env var, model/API version, contract path,
  cassette status, and live-canary status.
- Live provider capability reports.
- Release validation workflow.

## Recent Review Gaps

Subagent and local review on 2026-05-21 found these plan hardening items:

- Keep CLI `--json` as the existing stdout envelope. Use `--report PATH` or
  `--output PATH` for persisted validation JSON.
- Define validation exit-code mapping before adding public CLI commands. Do
  not leak pytest exit codes directly through `easycat validate`.
- Isolate artifacts by run id so concurrent local runs and CI matrix jobs do
  not overwrite each other.
- Make provider selection enforceable through provider/surface markers and a
  marker lint; current `integration_live` tests are too broad to support
  `--provider` safely.
- Add strict release semantics: explicitly required provider, latency, and
  release checks must fail when skipped.
- Make provider surfaces, agent bridges, optional extras, and cassette scope
  first-class in the contract plan.
- Include Python 3.11 in required quick CI or explicitly change the support
  policy.
- Remove pytest `-x` from validation CI so JUnit/report artifacts describe all
  failures found in a run.

## Target Slices

These names are the validation vocabulary. V0 ships script-first `quick` and
`socket`; the public `easycat validate ...` commands remain planned.

| Slice | Current selector or entry point | Planned command |
|---|---|---|
| quick | `uv run python scripts/validate.py quick` | `easycat validate quick` |
| socket | `uv run python scripts/validate.py socket` | `easycat validate socket` |
| contracts | existing `integration_local` provider matrix only | `easycat validate contracts` |
| live | manual CI or `uv run pytest -q -m "integration_live"` with credentials | `easycat validate live` |
| latency | `uv run pytest tests/e2e/test_plan_7_latency_benchmark.py -s -v` | `easycat validate latency --smoke/--sweep` |
| stress | selected `tests/e2e/` tests, often `slow` and/or socket-gated | `easycat validate stress` |
| release | manual checklist today | `easycat validate release` |

## First Implementation PR

V0 in [tasks.md](tasks.md) now covers:

1. Register planned markers and enable strict marker validation once
   collection is clean.
2. Add a validation report model.
3. Create `scripts/validate.py quick` and `scripts/validate.py socket`.
4. Document the contributor workflow without implying the public CLI already
   exists.

Keep cassettes, live-provider reports, latency rewrites, OpenTelemetry, and
CI reshaping out of the first PR unless implementation forces a small adjacent
change.
