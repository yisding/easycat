# Contributing to EasyCat

Thanks for improving EasyCat. This guide focuses on **testing and validation** —
how to run each test slice, what the markers mean, and how to keep the suite
green. For architecture, see [`CLAUDE.md`](CLAUDE.md) and [`AGENTS.md`](AGENTS.md).

## Quick start

```bash
uv sync --group dev        # install project + dev tools
just                       # list every task (or read the justfile)
just check                 # fmt-check + lint + tests (the pre-PR gauntlet)
```

Run `uv run easycat docs` for the maintained reader-facing map, including
quickstart, CLI and scaffold commands, examples, teaching chapters, public API,
validation, and operations. Use `uv run easycat docs --audience contributors`
to narrow the map to contributor-facing routes. Use
`uv run easycat docs --audience contributors --json` when automation needs
that smaller route map, or `uv run easycat docs --json` when a script or coding
agent needs the full route map with command hints and audience labels; replace
uppercase or angle-bracket placeholders such as `PATH` or `<session_id>` before
running those hints. Use
`uv run easycat explain json-schema` for the standard `--json` envelope and
command-specific fields.
For local audio or provider work, set the relevant environment variables and
run `uv run easycat doctor` before debugging tests or examples. Use
`uv run easycat doctor --env-file .env` when those keys live in a project
`.env`. Use
`uv run easycat doctor --json` when a script or coding agent needs parseable
environment/check rows; use
`uv run easycat doctor --env-file .env --json` when those checks should load
project `.env` keys.

Don't have [`just`](https://github.com/casey/just)? Every recipe is a one-liner
you can copy out of the `justfile`. Install it with `uv tool install rust-just`,
`brew install just`, `cargo install just`, or your distro's package manager.

## The development loop

| Task | `just` recipe | Raw command |
| --- | --- | --- |
| Install dev deps | `just sync` | `uv sync --group dev` |
| Install an extra | `just sync-extra openai` | `uv sync --group dev --extra openai` |
| Full test suite | `just test` | `uv run pytest` |
| Fast parallel run | `just test-fast` | `uv run pytest -n auto --dist loadscope -m "not integration_socket and not integration_live and not slow and not stress and not flaky"` |
| One file / node | `just test-one tests/test_cancel.py` | `uv run pytest tests/test_cancel.py` |
| Lint | `just lint` | `uv run ruff check .` |
| Lint auto-fix | `just lint-fix` | `uv run ruff check --fix .` |
| Format | `just fmt` | `uv run ruff format .` |
| Format check | `just fmt-check` | `uv run ruff format --check .` |
| Type gate (mypy, clean core) | `just typecheck` | `uv run mypy --follow-imports=silent src/easycat/debug` |
| Type report (mypy, whole repo) | `just typecheck-all` | `uv run mypy src/easycat` |
| Fast types (ty, advisory) | `just typecheck-fast` | `uvx ty check src/easycat` |
| Coverage | `just cov` | `uv run pytest -n auto --dist loadscope --cov --cov-report=term-missing -m "not integration_socket and not integration_live and not slow and not stress and not flaky"` |
| Guard root docs routes | `just guard-docs` | `uv run pytest tests/test_quickstart_e2e.py tests/test_command_hints.py tests/test_install_guidance.py tests/test_docs_index.py tests/test_public_api.py tests/cli/test_app.py tests/cli/test_json_schema.py` |
| Guard teaching docs | `just guard-teaching` | `uv run pytest tests/teaching tests/test_docs_index.py::test_teaching_ladder_docs_route_matches_learner_start_commands tests/test_install_guidance.py::test_teaching_ladder_prerequisites_run_doctor_after_setup tests/test_install_guidance.py::test_teaching_chapter_key_prerequisites_run_doctor tests/test_install_guidance.py::test_teaching_provider_key_setup_names_required_extras` |
| Guard examples docs | `just guard-examples` | `uv run pytest tests/test_examples.py tests/test_docs_index.py::test_examples_docs_route_matches_examples_fast_path` |
| Guard scaffold docs | `just guard-templates` | `uv run pytest tests/cli/test_templates.py tests/cli/test_init.py tests/cli/e2e/test_scaffold_smoke.py` |
| Guard contributor docs | `just guard-contributing` | `uv run pytest tests/test_contributing.py tests/test_docs_index.py::test_contributing_docs_route_matches_validation_lane_commands tests/test_validation_plan.py && uv run pytest tests/test_install_guidance.py -k 'agent_guide or agent_guides or claude_'` |
| Guard validation docs | `just guard-validation` | `uv run pytest tests/test_docs_index.py::test_validation_docs_route_matches_validation_workflow_commands tests/test_docs_index.py::test_validation_workflow_command_hints_are_locally_valid tests/test_docs_index.py::test_validation_reference_docs_route_matches_json_commands tests/test_validation_plan.py tests/cli/test_validate.py tests/cli/test_latency_validation.py` |
| Guard provider contracts | `just guard-contracts` | `uv run pytest tests/test_docs_index.py::test_provider_contract_docs_route_matches_contract_commands tests/test_contributing.py::test_contributing_provider_section_points_to_contract_map tests/contracts tests/integration/test_provider_contract_matrix.py` |
| Guard operator docs | `just guard-ops` | `uv run pytest tests/test_docs_index.py::test_deployment_docs_route_matches_docker_commands tests/test_docs_index.py::test_observability_docs_route_matches_journal_cli_entry_points tests/test_docs_index.py::test_journal_durability_docs_route_matches_inspection_commands tests/test_examples.py::test_docker_compose_binds_ws_port_to_loopback_and_requires_token tests/test_examples.py::test_docker_guide_serves_browser_client_from_localhost tests/test_examples.py::test_docker_env_secret_file_is_ignored_but_templates_are_allowed tests/test_examples.py::test_docker_guide_tracks_default_dockerfile_extras tests/test_examples.py::test_dockerfile_default_extras_cover_ws_server_golden_path tests/test_examples.py::test_docker_provider_swap_guidance_uses_known_extras_and_easyconfig tests/test_observability.py tests/cli/test_bundles.py tests/runtime/test_sqlite_journal.py` |
| Guard Markdown links | `just guard-markdown` | `uv run pytest tests/test_markdown_links.py tests/test_docs_index.py::test_cli_docs_routes_resolve_locally tests/cli/test_app.py::test_docs_route_paths_resolve_to_local_sources` |
| Validate (quick) | `just validate-quick` | `uv run easycat validate quick` |
| Validate (socket) | `just validate-socket` | `uv run easycat validate socket` |
| Validate (stress) | `just validate-stress` | `uv run easycat validate stress` |
| Validate (contracts) | `just validate-contracts` | `uv run easycat validate contracts` |
| Validate (latency smoke) | `just validate-latency-smoke` | `uv run easycat validate latency --smoke` |
| Validate (live OpenAI) | `just validate-live-openai` | `uv run easycat validate live --provider openai` |
| Validate (release) | `just validate-release` | `uv run easycat validate release` |
| Validate report | `just validate-report .easycat/validation/latest.json` | `uv run easycat validate report .easycat/validation/latest.json` |
| Pre-PR gauntlet | `just check` | `uv run ruff format --check . && uv run ruff check . && uv run pytest` |
| Pre-commit hooks | `just pre-commit` | `uv run pre-commit run --all-files` |

> `mypy` ships in the `dev` group, so `just typecheck` / `just typecheck-all`
> work right after `uv sync --group dev`. `just typecheck-fast` runs Astral
> `ty` on demand via `uvx` (no install needed; it's advisory, not a gate).
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
| Root README chooser, docs route map, public API docs, or CLI JSON envelopes | `just guard-docs` | Root onboarding links, README e2e coverage, install guidance, command-hint extraction, `easycat docs`, public API import-surface docs, JSON route entries, and shared CLI `--json` envelope contracts |
| Teaching ladder chapters or generated blocks | `just guard-teaching` | Chapter prerequisites, generated auto blocks, diagram alignment, and learner route hints |
| Examples chooser or command matrix | `just guard-examples` | Example README matrix, support files, setup/install/env guidance, script smoke checks, and docs-route hints |
| Scaffold templates or template catalog | `just guard-templates` | Generated README sections, line budgets, init happy paths, overwrite safety, schema rejection paths, catalog text, catalog JSON, next-step commands, generated project smoke, and generated project secret/artifact hygiene |
| Contributor and validation guidance | `just guard-contributing` | `justfile` parity, agent guide command, source-layout, and architecture hints, validation lanes, docs-route hints, and plan current-state evidence |
| Validation workflow, validation reference, or validate CLI behavior | `just guard-validation` | README validation workflow, validation reference route hints, validation plan current state, validate CLI reports, JSON envelopes, latency options, and error handling |
| Provider protocols, cassettes, contract matrix, or bridge event grammar | `just guard-contracts` | Provider contract docs-route hints, contributor provider guidance, offline contract suite, cassette redaction/replay, schema fingerprints, bridge contracts, and provider wiring matrix |
| Operator deployment, observability, or journal durability docs | `just guard-ops` | Docker deployment guide, operator docs-route hints, journal CLI entry points, debugger UI docs, OpenTelemetry facade docs, debug bundle CLI behavior, and SQLite journal durability |
| Markdown links in maintained docs | `just guard-markdown` | Local links, anchors, and docs-route Markdown targets |

If `just` is not installed, use the raw command table in
[the development loop](#the-development-loop) for the equivalent
`uv run pytest ...` command behind each guard.

Then run `uv run easycat validate quick` before a PR, or choose a broader lane
from the validation table below when the change touches transports, provider
contracts, packaging, live canaries, or stress behavior.

## Parallel runs and xdist safety

`just test-fast` and `just cov` use `pytest -n auto --dist loadscope`.
`loadscope` keeps every test in a module on the **same** worker, which matters
for async event-loop tests and any socket/port-binding tests. If you add tests
that bind a **fixed** port (rather than port `0`), keep them in one module and
prefer marking them `integration_socket`. `just test` (serial) is the source of
truth; parallel runs are an opt-in speedup. Always run coverage as
`pytest --cov` — never `coverage run -m pytest -n auto`, which reports 0% under
xdist.

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
| `quick` | `uv run easycat validate quick` | not integration_socket / live / slow / stress / flaky |
| `socket` | `uv run easycat validate socket` | integration_socket, not live, not flaky |
| `stress` | `uv run easycat validate stress` | stress, not live, not flaky |
| `contracts` | `uv run easycat validate contracts` | contract, not live, not flaky |
| `latency` | `uv run easycat validate latency --smoke` | latency probes (live) |
| `live` | `uv run easycat validate live --provider openai` | integration_live + provider/surface |
| `release` | `uv run easycat validate release` | installed-wheel aggregate gate |

`uv run easycat validate report .easycat/validation/latest.json` renders the
latest saved report. Use
`uv run easycat validate quick --json`,
`uv run easycat validate contracts --json`, or
`uv run easycat validate release --json` when a script or coding agent needs
the current validation run inside the standard CLI envelope. Use
`uv run easycat validate report .easycat/validation/latest.json --json` when a
script or coding agent needs a saved report re-emitted inside that envelope.
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
- `integration_live` — needs live API keys and optional provider extras.
- `slow` — long end-to-end tests; opt in with `-m slow`.
- `contract` — provider / protocol / bridge contract tests.
- `latency` — latency measurement or SLO tests.
- `stress` — load / soak / high-volume tests.
- `release` — release-gate validation.
- `flaky` — quarantined intermittent test (see policy below).
- `allow_task_leak` — explicit escape hatch for async tests that intentionally
  leave background tasks alive beyond the test body.
- `provider_openai` / `provider_deepgram` / `provider_elevenlabs` /
  `provider_cartesia` — provider coverage; `provider("name")` is the generic
  form for custom providers.
- `surface_stt` / `surface_tts` / `surface_agent` / `surface_transport` /
  `surface_vad` — which provider surface is exercised.
- `agent_bridge` — agent bridge contract or live coverage.
- `requires_extra("name")` — needs an optional dependency extra.

### Provider / surface pairing (enforced)

`tests/_marker_lint.py` requires that any test marked `contract`,
`integration_live`, or `latency` declares **both** a provider marker
(`provider_*` or `provider("name")`) **and** a surface marker (`surface_*`).
Declaring one without the other fails collection with a pointer to the
missing side. This keeps the validation matrix honest.

## Flaky-quarantine policy

Quarantine a genuinely intermittent test instead of letting it redden CI, but
quarantine is a **debt with an owner and a deadline**. `@pytest.mark.flaky`
requires three keyword fields (enforced by `tests/_marker_lint.py`):

```python
@pytest.mark.flaky(
    issue="https://github.com/yisding/easycat2/issues/123",
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
assertion helpers above accept directly; `assert_latency` budgets turn
latency with the validation percentile code; `assert_llm_judge` (with
`extract_transcript` and the default `JUDGE_RUBRIC`) scores conversational
quality, taking `judge=` for offline CI.

## Adding an STT or TTS provider

EasyCat uses **registries**, not inheritance. To add a provider:

1. **Implement** one provider per file under `src/easycat/stt/` or
   `src/easycat/tts/`, satisfying the `STTProvider` / `TTSProvider` Protocol
   in `src/easycat/providers.py`. Reuse `STTBase` / `TTSBase` plumbing.
2. **Add a config dataclass** for the provider's options.
3. **Register** the `(provider class, config class)` pair:
   - STT: `_PROVIDER_TO_CONFIG` in `src/easycat/stt/factory.py`.
   - TTS: `_PROVIDER_TO_CONFIG` (aliased `_PROVIDERS`) in
     `src/easycat/tts/factory.py`.
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
   `uv run easycat validate contracts`,
   `uv run easycat validate contracts --json` when a script or coding agent
   needs the contract run inside the standard CLI envelope,
   `uv run pytest tests/contracts`, and
   `uv run pytest tests/integration/test_provider_contract_matrix.py`.

## What's expected on a PR

These bullets name the `just` recipes reviewers expect. If `just` is not installed,
use the matching raw command from
[the development loop](#the-development-loop).

- `just check` is green (format + lint + tests).
- New code is typed (Python `>=3.11`, typing-first). `mypy` is the
  authoritative type checker: `just typecheck` gates the clean core
  (`easycat.debug`) and must stay green, while `just typecheck-all` is the
  advisory whole-repo report we ratchet down over time. `just typecheck-fast`
  (Astral `ty`) is faster local feedback but advisory only (beta).
- **Patch coverage**: cover the lines your PR changes (`just cov` locally).
  There is no hard global coverage gate; reviewers look at the diff.
- Tests added/updated for every behavior change.
- Commit subjects follow `<scope>: <imperative summary>`
  (e.g. `stt: normalize partial transcript events`).
- Secrets stay in environment variables; never commit keys or un-redacted
  cassettes.
