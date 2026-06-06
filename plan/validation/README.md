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

Snapshot: maintenance update on 2026-06-05.

Implemented:

- `pyproject.toml` registers the base, validation, provider, surface,
  optional-extra, and flaky/release markers listed in [reference.md](reference.md),
  with `strict_markers = true`.
- `tests/conftest.py` enforces provider/surface metadata when validation tests
  declare either side, and enforces flaky quarantine metadata:
  `@pytest.mark.flaky(issue="...", owner="...", review_by="YYYY-MM-DD")`.
- The public CLI registers `easycat validate` with `quick`, `socket`,
  `stress`, `contracts`, `latency`, `live`, and `report` subcommands.
- `scripts/validate.py` remains as a compatibility shim over
  `easycat.validation.runner` for slice runs.
- Validation runs write isolated artifacts under `.easycat/validation/runs/`
  and update `.easycat/validation/latest.json` after a complete report exists.
- `src/easycat/validation/report.py` defines the validation JSON envelope,
  provider credential states, artifact references, and report-boundary redaction.
- `easycat validate latency` writes structured latency artifacts with samples,
  percentiles, budget checks, reliability signals, and optional baseline
  comparison.
- `easycat validate live` writes redacted provider capability reports and can
  fail missing required live secrets in strict or release mode.
- `easycat validate contracts` runs the offline provider, protocol, cassette,
  and bridge contract suite through the same validation report machinery as
  quick/socket/stress.
- `easycat validate socket` advertises `EASYCAT_WEBRTC_STATS_PATH`; the bundled
  WebRTC browser client posts sanitized `RTCPeerConnection.getStats()`
  snapshots to `/stats`, and produced snapshots are surfaced as a
  `webrtc_stats` validation artifact.
- `easycat validate release` builds distributions, installs the wheel into a
  clean temporary venv, verifies the installed package outside the source tree,
  and aggregates quick, stress, contracts, live, and latency release gates into
  one validation report.
- `.github/workflows/ci.yml` runs `easycat validate quick` and
  `easycat validate socket` with uploaded JSON, JUnit, stdout, and stderr
  artifacts.
- `.github/workflows/nightly-validation.yml` runs quick, socket, stress, live,
  and latency validation lanes with uploaded artifacts.
- `.github/workflows/release-validation.yml` validates an installed package
  through the public CLI before release.
- Provider-surface coverage lives in `tests/contracts/provider_surface_matrix.py`
  and `src/easycat/validation/provider_reports.py`.
- CLI testing covers validation commands in `tests/cli/test_validate.py` and
  `tests/cli/test_latency_validation.py`; CLI test planning lives in
  `tests/cli/TEST_PLANS.md`.
- Broader E2E planning in [../testing/](../testing/README.md) is backed by
  concrete tests under `tests/e2e/`.

Remaining backlog:

- HTTP/WebSocket provider cassettes and schema drift fingerprints are still not
  standardized.
- Browser-driven WebRTC validation is not automated yet; the stats endpoint and
  artifact path exist, but socket validation only reports the artifact when a
  browser/client posts snapshots during the run.
- The release workflow still owns GitHub environment setup and artifact upload,
  but the release gate itself now has a dedicated public CLI wrapper.
- Deep acceptance-bullet auditing for the historical milestones in
  [tasks.md](tasks.md) remains open.

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

These names are the validation vocabulary.

| Slice | Current entry point | Notes |
|---|---|---|
| quick | `easycat validate quick` | deterministic local validation |
| socket | `easycat validate socket` | localhost socket / transport integration |
| stress | `easycat validate stress` | local stress validation and reliability artifacts |
| latency | `easycat validate latency --smoke` or `--sweep` | live latency probes and structured latency artifacts |
| live | `easycat validate live --provider openai` | live provider canaries and capability reports |
| contracts | `easycat validate contracts` | offline provider, protocol, cassette, and bridge contracts |
| release | `easycat validate release` | installed-wheel release gate and aggregate report |

## Historical First Implementation PR

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
