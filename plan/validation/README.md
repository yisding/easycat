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

Snapshot: static inspection on 2026-05-21. No tests were run for this
snapshot.

Implemented today:

- `pyproject.toml` registers only these pytest markers:
  `integration_local`, `integration_socket`, `integration_live`, and
  `slow`.
- `.github/workflows/ci.yml` has `lint`, local tests, socket integration
  tests, and manual live-provider tests.
- The public CLI currently registers `init`, `doctor`, `explain`, `bundles`,
  and `inspect`. There is no `easycat validate` command.
- There is no `scripts/validate.py` and no `scripts/` directory.
- The existing provider matrix at
  `tests/integration/test_provider_contract_matrix.py` validates provider
  registry, factory, EventBus injection, and session wiring. It is not a
  protocol cassette suite.
- `tests/e2e/test_plan_7_latency_benchmark.py` already measures voice-loop
  latency and stage breakdowns, but it is marked with the existing
  `integration_socket`, `integration_live`, and `slow` markers. There is no
  `latency` marker or structured validation artifact yet.
- CLI testing already has a focused plan in `tests/cli/TEST_PLANS.md`.
- Broader E2E planning in [../testing/](../testing/README.md) is backed by
  concrete tests under `tests/e2e/`.

Planned but not implemented:

- `easycat validate ...` command group.
- `.easycat/validation/latest.json` validation reports.
- JUnit and validation artifact upload in CI.
- `contract`, `latency`, `stress`, `release`, `flaky`, and provider-specific
  validation markers.
- HTTP/WebSocket provider cassettes and schema drift fingerprints.
- Live provider capability reports.
- Release validation workflow.

## Target Slices

These names are the planned validation vocabulary. Until V0/V1 in
[tasks.md](tasks.md) lands, use the current pytest selectors shown here.

| Slice | Current selector or entry point | Planned command |
|---|---|---|
| quick | `uv run pytest -q -m "not integration_socket and not integration_live"` | `easycat validate quick` |
| socket | `uv run pytest -q -m "integration_socket"` | `easycat validate socket` |
| contracts | existing `integration_local` provider matrix only | `easycat validate contracts` |
| live | manual CI or `uv run pytest -q -m "integration_live"` with credentials | `easycat validate live` |
| latency | `uv run pytest tests/e2e/test_plan_7_latency_benchmark.py -s -v` | `easycat validate latency --smoke/--sweep` |
| stress | selected `tests/e2e/` tests, often `slow` and/or socket-gated | `easycat validate stress` |
| release | manual checklist today | `easycat validate release` |

## First Implementation PR

Start with V0 in [tasks.md](tasks.md):

1. Register planned markers and enable strict marker validation once
   collection is clean.
2. Add a validation report model.
3. Create `scripts/validate.py quick` and `scripts/validate.py socket`.
4. Document the contributor workflow without implying the public CLI already
   exists.

Keep cassettes, live-provider reports, latency rewrites, OpenTelemetry, and
CI reshaping out of the first PR unless implementation forces a small adjacent
change.
