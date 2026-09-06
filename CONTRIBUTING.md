# Contributing to EasyCat

Thanks for improving EasyCat. This guide focuses on **testing and validation** —
how to run each test slice, what the markers mean, and how to keep the suite
green. New maintainers should first follow the
[`developer textbook`](docs/development/) for the guided architecture and
source tour. For compact architecture and repository rules, see
[`CLAUDE.md`](CLAUDE.md) and [`AGENTS.md`](AGENTS.md).

## Quick start

```bash
uv sync --group dev        # install project + dev tools
just                       # list every task (or read the justfile)
just check                 # pre-commit + mypy + credential-free tests
```

Run `uv run easycat docs` for the compact reader-facing route index, or
`uv run easycat docs --verbose` to expand every route and command hint. Use
`uv run easycat docs --audience contributors` to show contributor-facing routes, or
`uv run easycat docs --audience contributors --json` when automation needs
that smaller route map.
Coding agent? Use [AGENTS.md](AGENTS.md) for repository coding rules; use
[llms.txt](llms.txt) for machine-readable docs route discovery, or run
`uv run easycat explain json-schema`; `uv run easycat docs --json` emits the
full route map. After editing the docs route map, regenerate the machine docs
with `uv run python scripts/regen_llms_txt.py` (`--check` verifies them in
CI).
For local audio or provider work, set the relevant environment variables and
run `uv run easycat doctor` before debugging tests or examples. Use
`uv run easycat doctor --env-file .env` when those keys live in a project
`.env`; add `--json` (`uv run easycat doctor --json`,
`uv run easycat doctor --env-file .env --json`) for parseable
environment/check rows.

Don't have [`just`](https://github.com/casey/just)? Every recipe has a complete
raw equivalent in the table below. Install it with `uv tool install rust-just`,
`brew install just`, `cargo install just`, or your distro's package manager.

## The development loop

| Task | `just` recipe | Raw command |
| --- | --- | --- |
| Install dev deps | `just sync` | `uv sync --group dev` |
| Install an extra | `just sync-extra openai` | `uv sync --group dev --extra openai` |
| Full local test suite | `just test` | `uv run pytest -n auto --dist loadscope -m "not integration_live and not integration_external and not serial" && uv run pytest -o faulthandler_timeout=0 -o timeout=0 -m "serial and not integration_live and not integration_external"` |
| Live provider suite (may be billable) | `just test-live` | `uv run pytest -m integration_live` |
| Fast parallel run | `just test-fast` | `uv run pytest -n auto --dist load -m "not integration_socket and not integration_live and not integration_external and not contract and not latency and not slow and not stress and not serial and not flaky and not guard"` |
| One file / node | `just test-one tests/core/test_cancel_token.py` | `uv run pytest tests/core/test_cancel_token.py` |
| Lint | `just lint` | `uv run ruff check . && uv run lint-imports` |
| Lint auto-fix | `just lint-fix` | `uv run ruff check --fix .` |
| Format | `just fmt` | `uv run ruff format .` |
| Format check | `just fmt-check` | `uv run ruff format --check .` |
| Type gate (mypy + LangChain smoke script) | `just typecheck` | `uv run mypy src/easycat scripts/smoke_langchain_versions.py` |
| Fast types (ty, advisory) | `just typecheck-fast` | `uvx ty check src/easycat` |
| Coverage | `just cov` | `uv run pytest -n auto --dist load --cov --cov-report=term-missing -m "not integration_socket and not integration_live and not integration_external and not contract and not latency and not slow and not stress and not serial and not flaky and not guard"` |
| Validate (quick) | `just validate-quick` | `uv run easycat validate quick` |
| Validate (socket) | `just validate-socket` | `uv run easycat validate socket` |
| Validate (stress) | `just validate-stress` | `uv run easycat validate stress` |
| Validate (contracts) | `just validate-contracts` | `uv run easycat validate contracts` |
| Validate (latency smoke) | `just validate-latency-smoke` | `uv run easycat validate latency --smoke` |
| Validate (live OpenAI) | `just validate-live-openai` | `uv run easycat validate live --provider openai` |
| Validate (release) | `just validate-release` | `uv run easycat validate release` |
| Validate report | `just validate-report .easycat/validation/latest.json` | `uv run easycat validate report .easycat/validation/latest.json` |
| Pre-commit hooks | `just pre-commit` | `uv run pre-commit run --all-files` |
| Pre-PR gauntlet | `just check` | `uv run pre-commit run --all-files && uv run mypy src/easycat scripts/smoke_langchain_versions.py && uv run pytest -n auto --dist loadscope -m "not integration_live and not integration_external and not serial" && uv run pytest -o faulthandler_timeout=0 -o timeout=0 -m "serial and not integration_live and not integration_external"` |

`just check` mirrors CI's core source-quality gates, but it is not a literal
replay of the workflow: CI also covers the supported Python matrix, minimum
dependency floors, validation artifact lanes, and built distributions. Run the
change-specific validation or release lane below when those surfaces are in
scope.

### Source-enforcement ratchets

`tests/ratchets/` fingerprints grandfathered production call sites for raw
task spawning, cancellation handling, survivor ledgers, inline shield loops,
and generation/epoch fields. The inventory is structural and location-free,
so inserting lines does not require baseline churn; tests and examples remain
free to create raw tasks when orchestrating races. Ruff complexity waivers use
the same rule at function granularity, and `mkdocs.yml` must cover every
maintained Markdown page.

For a recurring bug class, put the fix in the shared primitive or engine. If a
site must remain hand-rolled, add it through an explicit, reviewed baseline
update—never by silently broadening a file-wide waiver:

```bash
uv run pytest -n 0 tests/ratchets tests/test_complexity_ignores.py \
  --update-baseline --baseline-rationale "why this exception remains necessary"
```

Commit the resulting baseline diff with the implementation and rationale.
Ordinary development and CI runs never rewrite baselines.

The docs/onboarding guard table below is generated from the `justfile` by
`uv run python scripts/regen_guard_commands.py`; edit the `guard-*` recipe
in the `justfile`, then re-run the script.

<!-- BEGIN auto:guard-commands format=table -->
| Docs guard | `just` recipe | Raw command |
| --- | --- | --- |
| Guard root onboarding docs, install guidance, docs routes, public API docs, CLI JSON envelopes, and maintained Markdown links and anchors | `just guard-docs` | `uv run pytest tests/test_quickstart_e2e.py tests/install/test_install_guidance.py tests/docs tests/test_public_api.py tests/test_llms_txt.py tests/test_regen_guard_commands.py tests/cli/test_app.py tests/cli/test_json_schema.py tests/test_markdown_links.py` |
| Guard teaching ladder chapters, generated README blocks, and learner route hints | `just guard-teaching` | `uv run pytest tests/teaching tests/docs/test_route_contracts.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/install/test_teaching_prerequisites.py` |
| Guard examples README, support files, script smoke checks, docs-route hints, and scaffold templates, init flows, catalog output, generated project smoke, and secret/artifact hygiene | `just guard-examples` | `uv run pytest tests/examples tests/docs/test_route_contracts.py::test_examples_docs_route_matches_examples_fast_path tests/cli/test_scaffold_schema.py tests/cli/test_templates.py tests/cli/test_init.py tests/cli/e2e/test_scaffold_smoke.py -m 'not integration_external'` |
| Guard contributor guidance, agent guide contracts, validation state, and route hints | `just guard-contributing` | `uv run pytest tests/test_contributing.py tests/docs/test_route_contracts.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_regen_guard_commands.py tests/install/test_agent_guides.py` |
| Guard validation workflow docs, validation reference docs, and validate CLI behavior | `just guard-validation` | `uv run pytest tests/docs/test_route_contracts.py::test_validation_docs_route_matches_validation_workflow_commands tests/docs/test_command_hints.py::test_validation_workflow_command_hints_are_locally_valid tests/docs/test_route_contracts.py::test_validation_reference_docs_route_matches_json_commands tests/cli/test_validate_report_model.py tests/cli/test_validate_live.py tests/cli/test_validate_runner.py tests/cli/test_validate_cli.py tests/cli/test_validate_report_cli.py tests/cli/test_latency_selectors_artifacts.py tests/cli/test_latency_reliability_failures.py tests/cli/test_latency_runner.py tests/cli/test_latency_cli.py tests/cli/test_latency_baseline_budgets.py` |
| Guard provider contract docs, offline contract suite, contract kit, and provider wiring matrix | `just guard-contracts` | `uv run pytest tests/docs/test_route_contracts.py::test_provider_contract_docs_route_matches_contract_commands tests/test_contributing.py::test_contributing_provider_section_points_to_contract_map tests/contracts tests/testing` |
| Guard operator docs, deployment guide, observability docs, journal CLI, and durability | `just guard-ops` | `uv run pytest tests/docs/test_route_contracts.py::test_deployment_docs_route_matches_docker_commands tests/docs/test_route_contracts.py::test_observability_docs_route_matches_journal_cli_entry_points tests/docs/test_route_contracts.py::test_journal_durability_docs_route_matches_inspection_commands tests/examples/test_deploy_and_browser_docs.py tests/observability tests/cli/test_bundles.py tests/runtime/test_sqlite_journal.py` |
<!-- END auto:guard-commands -->

> `mypy` ships in the `dev` group, so `just typecheck` works right after
> `uv sync --group dev`. `just typecheck-fast` runs Astral `ty` on demand
> via `uvx` (no install needed; it's advisory, not a gate).
> `just cov` is plain `pytest --cov` and has no type-checker dependency.

If local tests emit dependency warnings right after a lockfile or Dependabot
merge, refresh the virtualenv against the lock before debugging test behavior:

```bash
uv sync --frozen --group dev
```

`uv sync` removes optional-extra packages that are no longer part of the
selected environment. Add the extras you are actively working on, for example
`uv sync --frozen --group dev --extra openai`.

## Maintaining docs and onboarding maps

When a change updates user-facing docs, run the narrow guard that owns that
surface before the broader validation lane:

| If you change | Run | What it protects |
| --- | --- | --- |
| Root README chooser, docs route map, public API docs, CLI JSON envelopes, or Markdown links | `just guard-docs` | Root onboarding links, README e2e coverage, install guidance, command-hint extraction, `easycat docs`, public API import-surface docs, JSON route entries, shared CLI `--json` envelope contracts, and maintained Markdown links, anchors, and docs-route Markdown targets |
| Teaching ladder chapters or generated blocks | `just guard-teaching` | Chapter prerequisites, generated auto blocks, diagram alignment, and learner route hints |
| Examples chooser or command matrix, scaffold templates, or template catalog | `just guard-examples` | Example README matrix, support files, setup/install/env guidance, script smoke checks, docs-route hints, generated README sections, line budgets, init happy paths, overwrite safety, schema rejection paths, catalog text, catalog JSON, next-step commands, generated project smoke, and generated project secret/artifact hygiene |
| Contributor and validation guidance | `just guard-contributing` | `justfile` parity, agent guide command, source-layout, and architecture hints, validation lanes, and docs-route hints |
| Validation workflow, validation reference, or validate CLI behavior | `just guard-validation` | The `docs/validation.md` workflow, validation reference route hints, validate CLI reports, JSON envelopes, latency options, and error handling |
| Provider protocols, cassettes, contract matrix, or bridge event grammar | `just guard-contracts` | Provider contract docs-route hints, contributor provider guidance, offline contract suite, cassette redaction/replay, schema fingerprints, bridge contracts, and provider wiring matrix |
| Operator deployment, observability, or journal durability docs | `just guard-ops` | Docker deployment guide, operator docs-route hints, journal CLI entry points, debugger UI docs, OpenTelemetry facade docs, debug bundle CLI behavior, and SQLite journal durability |

If `just` is not installed, use the raw command table in
[the development loop](#the-development-loop) for the equivalent
`uv run pytest ...` command behind each guard.

Then run `uv run easycat validate quick` before a PR, or choose a broader lane
from the validation table below when the change touches transports, provider
contracts, packaging, live canaries, or stress behavior.

## Parallel runs and xdist safety

`just test` uses `pytest -n auto --dist loadscope` because it includes the
socket suite; `loadscope` keeps every test in a module on the same worker.
`just test-fast`, `just cov`, and the `quick` validation lane exclude socket
and timing-sensitive tests and use `--dist load`, allowing xdist to balance
individual tests from large modules. The repository caps `-n auto` at eight
workers by default; set `PYTEST_XDIST_AUTO_NUM_WORKERS` to override it.

Tests marked `serial` call process primitives such as `os.fork()` that are
unsafe after xdist has started worker-management threads. Bare pytest and the
parallel command in `just test` exclude them; `just test` then runs that small
slice without xdist and with both watchdog threads (faulthandler and
pytest-timeout) disabled. Direct-fork tests must instead bound their child
with `signal.alarm`, `select`, or an equivalent primitive. Quick and coverage
runs omit them; CI and nightly run them serially.

If you add tests that bind a **fixed** port (rather than port `0`), keep them
in one module and mark them `integration_socket` — the dedicated socket,
stress, and contracts validation lanes stay serial.
Bare `uv run pytest` also excludes the `serial` slice as well as live and
external integrations. This keeps the default run from forking pytest's helper
threads and prevents credentials in a developer's shell from turning a local
run into billable provider traffic. Run provider/external lanes explicitly
with `just test-live` / `uv run pytest -m integration_live` or
`uv run pytest -m integration_external`.
Always run coverage as `pytest --cov` — never
`coverage run -m pytest -n auto`, which reports 0% under xdist.

## Validation slices and the `easycat validate` CLI

CI runs the same slices you can run locally. From a repo checkout, run them
through `uv run` so the command uses the checked-out EasyCat package and its
managed virtualenv. Each slice writes a JSON + JUnit report under
`.easycat/validation/`:

| If your change touches | Run | Why |
| --- | --- | --- |
| Most code, docs, CLI help, unit behavior | `uv run easycat validate quick` | deterministic local PR gate |
| WebSocket, WebRTC, transports, or localhost server behavior | `uv run easycat validate socket` | socket and local integration coverage |
| Provider protocols, cassettes, contract matrix, or agent bridges | `uv run easycat validate contracts` | offline provider/protocol/bridge contracts |
| Queues, load, reliability sampling, or saturation behavior | `uv run easycat validate stress` | local stress and saturation signals |
| Live latency budgets or end-to-end timing | `uv run easycat validate latency --smoke` | low-cost live latency probe |
| Live provider adapters, credentials, or provider/surface canaries | `uv run easycat validate live --provider openai` | live provider behavior |
| Packaging, release workflows, or installed-wheel behavior | `uv run easycat validate release` | strict installed-wheel aggregate gate |
| A saved validation artifact | `uv run easycat validate report .easycat/validation/latest.json` | latest report summary |

| Slice | Command | Marker selection |
| --- | --- | --- |
| `quick` | `uv run easycat validate quick` | not integration_socket / live / external / contract / latency / slow / stress / serial / flaky / guard |
| `socket` | `uv run easycat validate socket` | integration_socket, not live, not flaky |
| `stress` | `uv run easycat validate stress` | stress, not live, not flaky |
| `contracts` | `uv run easycat validate contracts` | contract, not live, not flaky |
| `latency` | `uv run easycat validate latency --smoke` | latency probes (live) |
| `live` | `uv run easycat validate live --provider openai` | integration_live + provider/surface |
| `release` | `uv run easycat validate release` | installed-wheel aggregate gate |

`uv run easycat validate report .easycat/validation/latest.json` renders the
latest saved report. Add `--json` to any lane
(`uv run easycat validate quick --json`,
`uv run easycat validate contracts --json`,
`uv run easycat validate release --json`) for the current validation run
inside the standard CLI envelope, or to the report command
(`uv run easycat validate report .easycat/validation/latest.json --json`) to
re-emit a saved report inside that envelope.
Use `.easycat/validation/runs/<run_id>/report.json` when you need a specific
older run.

## Marker taxonomy

Markers are **strict** (`strict_markers = true`): an unknown marker fails
collection. The full list lives in `pyproject.toml` under
`[tool.pytest.ini_options].markers`. What they mean:

- `integration_local` — local integration tests with no live services; may use
  fake providers, subprocesses, or filesystem state.
- `integration_socket` — needs localhost socket bind/connect (auto-skipped
  where the sandbox forbids binding; see `tests/conftest.py`).
- `integration_live` — needs live provider/API endpoints, API keys, and
  optional provider extras. Excluded from bare `pytest`, `just test`, and
  `just check`; run explicitly and serially with `just test-live` or
  `uv run pytest -m integration_live`. These tests may incur provider charges.
- `integration_external` — needs external local binaries, SDKs, or services
  without live provider API credentials. Excluded from bare `pytest`,
  `just test`, and `just check`; run explicitly with
  `pytest -m integration_external`.
- `serial` — must run outside xdist (currently direct-`os.fork()` process
  lifecycle coverage). `just test` and CI run this slice separately.
- `slow` — long end-to-end tests; opt in with `-m slow`.
- `contract` — provider / protocol / bridge contract tests.
- `latency` — latency measurement or SLO tests.
- `stress` — load / soak / high-volume tests.
- `release` — release-gate validation.
- `flaky` — quarantined intermittent test (see policy below).
- `allow_task_leak` — explicit escape hatch for async tests that intentionally
  leave background tasks alive beyond the test body.
- `guard` — docs/onboarding/prose guard test that scans Markdown, docstrings,
  routes, or generated blocks rather than exercising product runtime. Applied
  by path in `tests/conftest.py` (the `tests/docs`, `tests/install`, and
  `tests/examples` trees plus a short list of prose-only guard modules, minus a
  behavioral exempt list). Excluded from the fast dev loop (`just test-fast`,
  `just cov`, and the `quick` validation lane) but always run in `just test`,
  `just check`, and the `guard-*` lanes. The named guard lanes also include
  behavioral CLI and runtime coverage; use `-m guard` only for the prose
  overlay, not as a replacement for a relevant `just guard-*` command.
- `provider_openai` / `provider_deepgram` / `provider_elevenlabs` /
  `provider_cartesia` — provider coverage; `provider("name")` is the generic
  form for custom providers.
- `surface_stt` / `surface_tts` / `surface_agent` / `surface_transport` /
  `surface_vad` — which provider surface is exercised.
- `agent_bridge` — agent bridge contract or live coverage.

There is no marker for "needs an optional extra": tests that need one use
`pytest.importorskip`, and the nightly extras install matrix re-runs the
offline contract tests with each extra's real SDK installed.

### Provider / surface pairing (enforced)

`tests/_marker_lint.py` requires that provider/surface-scoped tests marked
`contract`, `integration_live`, or `latency` declare **both** a provider marker
(`provider_*` or `provider("name")`) **and** a surface marker (`surface_*`).
Bare `integration_live` also fails collection; use `integration_external` for
local binaries/SDKs/services that do not call live provider APIs. Declaring one
side without the other fails collection with a pointer to the missing side.
This keeps the validation matrix honest.

## Flaky-quarantine policy

Quarantine a genuinely intermittent test instead of letting it redden CI, but
quarantine is a **debt with an owner and a deadline**. `@pytest.mark.flaky`
requires three keyword fields (enforced by `tests/_marker_lint.py`):

```python
@pytest.mark.flaky(
    issue="https://github.com/yisding/easycat/issues/123",
    owner="yi",
    review_by="2026-07-01",  # YYYY-MM-DD; a past date fails collection
)
```

Rules (from `tests/_marker_lint.py`):

- All three of `issue`, `owner`, `review_by` are required and non-empty.
- `review_by` must be a valid ISO date and **must not be in the past** — a
  stale date fails collection, forcing a re-triage.
- A `flaky` test may not also be `release`-scoped.
- Validation slices deselect `flaky`, so quarantined tests never gate a PR.

## Cassettes (`tests/cassettes/`)

Provider protocol tests replay **hand-maintained JSON cassettes** so they run
offline and deterministically. There are three transport flavors:

- `tests/cassettes/http/` — request/response pairs (e.g. `openai-stt.json`).
- `tests/cassettes/ws/` — ordered WebSocket frames
  (e.g. `openai-realtime-stt.json`).
- `tests/cassettes/sse/` — server-sent-event streams
  (e.g. `remote-responses-api.json`).

Replay tests live in `tests/contracts/test_*_cassette_replay.py` and assert the
frame schema and ordering. Cassettes are **redacted** — secrets and volatile
fields are stripped (`tests/contracts/test_http_cassette_redaction.py` guards
this). When a provider's wire protocol changes:

1. Capture the new exchange against the live API in a throwaway script.
2. Redact credentials, account ids, and timestamps.
3. Update the JSON cassette and the expected frame order in the replay test.
4. Run `just test-one tests/contracts/` and confirm the schema-fingerprint
   checks (`tests/contracts/schema_fingerprints.py`) still pass.

Never commit a cassette containing a real key — codespell and the redaction
test are backstops, not a substitute for review.

## RunBundle golden tests (`src/easycat/debug/testing.py`)

A `RunBundle` is a zipped, replayable recording of a full session journal.
`session.export_debug_bundle(path)` writes one; `load_bundle(path)` reads it.
The helpers in `easycat.debug.testing` turn a captured production failure into
a regression test in the same PR that fixes it:

```python
from easycat.debug.testing import load_bundle, assert_turn_completed, assert_no_error


def test_roundtrip_regression():
    bundle = load_bundle("tests/fixtures/roundtrip.zip")
    assert_turn_completed(bundle, turn_id="t1")
    assert_no_error(bundle, turn_id="t1")
```

Available helpers: `load_bundle`; assertion helpers `assert_exact_match`,
`assert_regex`, `assert_turn_completed`, `assert_no_error`, `assert_tool_called`;
and iteration helpers `iter_records`, `turn_records`, `find_record`. To refresh
a golden bundle, re-export it from a session run and re-run the test; review the
bundle diff like any other fixture change.

The same module also covers live text turns and evals (see the
[testing and evals ladder](docs/testing-and-evals.md)):
`run_text_turn` drives one real agent-bridge turn through `send_text` with
Noop audio stages and returns a `TurnResult` — a `RecordSource` the
assertion helpers above accept directly; `run_text_turns` runs a whole
scenario against one session and returns one `TurnResult` per input;
`run_scripted_audio_turn` drives one turn through the real audio pipeline
with scripted stub I/O (no microphone, key or network — it checks pipeline
wiring, not speech quality); `assert_latency` budgets turn
latency with the validation percentile code; `assert_llm_judge` (with
`extract_transcript` and the default `JUDGE_RUBRIC`) scores conversational
quality, taking `judge=` for offline CI.

## Adding an STT or TTS provider

EasyCat uses **registries**, not inheritance. To add a provider:

1. **Implement** one provider per file under `src/easycat/stt/` or
   `src/easycat/tts/`, satisfying the `STTProvider` / `TTSProvider` Protocol
   in `src/easycat/providers.py`. Reuse `STTBase` / `TTSBase` plumbing.
2. **Add a config dataclass** for the provider's options, then add it to the
   built-in `STTConfig` or `TTSConfig` typing union in the matching factory.
   The union is for static typing; runtime extension dispatch remains catalog-based.
3. **Register** one `ProviderSpec` containing the provider/config pair plus catalog
   metadata (credential env var, install extra, API domains — the
   `ProviderCatalog` rejects incomplete entries at import):
   - STT: `_CATALOG` in `src/easycat/stt/factory.py`.
   - TTS: `_CATALOG` in `src/easycat/tts/factory.py`.

   Do not edit `_PROVIDER_TO_CONFIG`, `_PROVIDERS`, or the metadata maps
   directly; they are derived catalog views kept for compatibility.

   `easycat doctor`, `easycat init` scaffolding, validation provider
   markers, and redaction's sensitive-URL policy all derive from the
   catalog, so they pick the new provider up automatically.
4. **Declare the contract row** in
   `tests/contracts/provider_surface_matrix.py` (a `ProviderSurfaceContract`
   with adapter path, protocol, required extra, credential env var, and
   cassette status) — or add an explicit exclusion with a reason. The matrix
   tests fail if a registered provider has no row. Review the
   [provider contract map](tests/contracts/README.md) before changing provider
   adapters, protocol cassettes, schema fingerprints, or bridge event grammar.
5. **Add an extra** in `pyproject.toml` `[project.optional-dependencies]` if
   the provider needs an SDK, and wire it into `all` / `quickstart` as
   appropriate.
6. **Tests**: contract tests under `tests/contracts/` plus unit tests under
   `tests/stt/` or `tests/tts/`. Mark provider/surface pairs correctly (see
   the pairing rule above). If the protocol is replayable, add a cassette. Run
   `uv run easycat validate contracts`
   (add `--json` for the same run inside the standard CLI envelope:
   `uv run easycat validate contracts --json`),
   `uv run pytest tests/contracts`, and
   `uv run pytest tests/contracts/test_provider_session_matrix.py`.

## What's expected on a PR

These bullets name the `just` recipes reviewers expect. If `just` is not installed,
use the matching raw command from
[the development loop](#the-development-loop).

- `just check` is green. It covers the core local source gates: all pre-commit
  hooks, whole-package mypy, and the credential-free local test suite. CI still
  adds version matrices, dependency floors, validation artifacts, and builds.
- New code is typed (Python `>=3.11`, typing-first). `mypy` is the
  authoritative type checker: `just typecheck` gates the whole package at
  zero errors (vendored `vad/_funasr_runtime` excluded), with stricter
  checks layered on the core packages (`easycat.debug`, `easycat.runtime`,
  `easycat.stages`, `easycat.session`, and `easycat.integrations`).
  `just typecheck-fast` (Astral `ty`) is faster local feedback but advisory
  only (beta).
- **Patch coverage**: cover the lines your PR changes (`just cov` locally).
  There is no hard global coverage gate; reviewers look at the diff.
- Tests added/updated for every behavior change.
- Commit subjects follow `<scope>: <imperative summary>`
  (e.g. `stt: normalize partial transcript events`).
- Secrets stay in environment variables; never commit keys or un-redacted
  cassettes.

## Preparing a release

`pyproject.toml` is the package-version source of truth. Before creating a
release candidate:

1. Move completed entries from `Unreleased` into a versioned section in
   [CHANGELOG.md](CHANGELOG.md), and set the same version in `pyproject.toml`.
2. Regenerate and verify the lock with `uv lock` and `uv lock --check`.
3. Run `just check` and `uv run easycat validate release`. The release lane
   builds both distributions, checks their metadata, installs the wheel outside
   the source tree, and runs its configured gates through that installation.
4. Before the first production publish, reserve the `easycat` project name and
   configure pending Trusted Publishers on PyPI and TestPyPI. Manually dispatch
   `.github/workflows/release-validation.yml` until it is green, then rehearse
   an RC such as `0.1.0rc1` against TestPyPI. Do not use the production index as
   the first workflow test.
5. Create an annotated tag whose name is exactly `v` plus the project version
   (for example, version `0.1.0rc1` uses tag `v0.1.0rc1`). The release
   validation workflow rejects a mismatched tag before building.

The tag-triggered workflow publishes through OIDC and the reviewer-gated
`pypi` environment. It must not be changed to use a long-lived upload token.
