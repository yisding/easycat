# EasyCat CLI — Test Plans

One plan per column below. Each plan names a concern, the high-level
risk, the checks that exercise it, and the test files that back it.

Plans are organized by the lifecycle order users hit them: scaffold
first, debug second, safety net third, and infrastructure last.

| # | Plan | Backing tests |
|---|------|---------------|
| 1 | CLI boot integrity | `test_app.py` + E2E §1 |
| 2 | `explain` catalog completeness | `test_explain.py` + `test_errors.py` |
| 3 | `explain` fuzzy + meta paths | `test_explain.py` |
| 4 | `docs` route map | `test_app.py` + `tests/docs` + `test_json_schema.py` |
| 5 | `init` template rendering | `test_init.py` + `test_templates.py` |
| 6 | `init` schema rejection paths | `test_init.py` |
| 7 | `init` overwrite safety | `test_init.py` |
| 8 | `doctor` check matrix | `test_doctor.py` |
| 9 | `doctor` network isolation | `test_doctor.py` + network stubs |
| 10 | Error-code registry integrity | `test_errors.py` |
| 11 | Exit-code contract stability | `test_errors.py` + `test_exit_codes.py` |
| 12 | JSON envelope stability | `test_json_schema.py` |
| 13 | `validate` command and report rendering | `test_validate_cli.py` + `test_validate_report_cli.py` + `test_validate_runner.py` |
| 14 | Library prereqs — `run()` lifecycle | `test_library_prereqs.py` |
| 15 | Library prereqs — string-keyed providers | `test_library_prereqs.py` |
| 16 | Packaging — wheel and sdist ship template dotfiles, metadata, and clean contents | `test_packaging.py` (integration) |
| 17 | End-to-end scaffold-and-invoke | `tests/cli/e2e/test_scaffold_smoke.py` (integration) + `tests/cli/e2e/test_generated_project_wheel.py` (`integration_external`) |

Plans 1-10 are fast unit tests. Plans 11-15 add coverage for cross-
cutting contracts. Plan 16 and `test_scaffold_smoke.py` are marked
`integration_local` so validation lanes and maintainers can select or filter
them explicitly; bare `pytest` still collects them unless the caller supplies
a marker expression. Plan 17's `test_generated_project_wheel.py` is the
exception: it is `integration_external`, which the default `addopts` and every
`just guard-*` recipe deselect, so **no automated lane runs it**. It is a
maintainer reproduction tool for `.github/workflows/ci.yml`'s
`generated-app-smoke` job — the job is the gate, and
`tests/test_generated_app_smoke_lane.py` pins its load-bearing contents.

---

## Plan 1 — CLI boot integrity

**Concern.** A user with nothing installed must get a sensible
response from `uvx easycat`, `easycat --version`, and `easycat --help`.
If bare invocation errors out, everything else is moot.

**Risks.** Missing entry point; broken Typer callback; import-time
failure in `easycat.cli`; the bare `easycat` invocation silently
producing an empty line.

**Checks.**
- `--version` prints a version containing `easycat`, exit 0.
- `-V` short form works identically.
- `--help` renders, exit 0, and includes every registered top-level
  command/group.
- Bare `easycat` prints the journey menu and includes every registered
  top-level command/group.
- E2E: `uvx easycat --version` works on a clean machine (covered by
  the wheel test at the bottom).

**Backed by.** `tests/cli/test_app.py`.

---

## Plan 2 — `explain` catalog completeness

**Concern.** Every `EASYCAT_Exxx` raised by the library must have a
canonical explanation; a raised code without a doc entry is a
regression that `easycat explain` should catch at test time, not at
runtime.

**Risks.** A contributor adds a new `EASYCAT_E_` factory call without
registering it; the registry gets out of sync with what
`_errors._CODE_TO_EXIT` maps; a headline template uses placeholders
that the rendering path can't supply.

**Checks.**
- Every code in `REGISTRY` renders via `easycat explain <code>`
  without error.
- Every registered code has a non-empty headline/cause/fix.
- Every code listed in `_errors._CODE_TO_EXIT` is in `REGISTRY`.

**Backed by.** `test_explain.py::test_every_registered_code_renders`
and `test_errors.py::test_every_registered_code_has_factory`.

---

## Plan 3 — `explain` fuzzy + meta paths

**Concern.** `explain` is the recovery surface users reach after a
typo; if fuzzy matching is broken or meta topics don't render,
typos become dead ends.

**Risks.** Case sensitivity; prefix handling (`E102` vs
`EASYCAT_E102`); unknown codes not suggesting close matches;
meta topics (`exit-codes`, `init-schema`, `json-schema`) failing to
render.

**Checks.**
- `easycat explain E101`, `easycat explain EASYCAT_E101`,
  `easycat explain e101` all produce identical output.
- `easycat explain E999` exits 2 with a fuzzy suggestion.
- Each meta topic renders with its canonical body.
- `easycat explain --list` includes every code and meta topic.
- `--json` mode for both single code and `--list` emits a valid
  envelope.

**Backed by.** `test_explain.py`.

---

## Plan 4 — `docs` route map

**Concern.** `easycat docs` is the maintained navigation surface for
new users, maintainers, contributors, operators, and coding agents. It
must point to current docs, expose useful command hints, and keep the
human output and JSON route map in sync.

**Risks.** A docs route points at a removed file or stale anchor; the
JSON payload drops route fields, audience filter metadata, `command_note`,
or online URLs; command hints drift away from target pages; placeholders
such as `PATH` are emitted without explaining how to replace them; the
route order stops putting onboarding paths first.

**Checks.**
- Human `easycat docs` output includes every maintained route, audience
  label, description, command hint, online URL, and the machine-readable
  command note.
- `easycat docs --json` emits a standard envelope with every route entry,
  including `label`, `path`, `audience`, `description`, `commands`, and
  `url`, plus top-level `source_url`, `command_note`, `audience_filter`,
  `available_audiences`, `available_audience_filters`, and
  `audience_alias_note`; maintainer and coding-agent routes include
  docs-map, parseable doctor/schema/validation-report commands, and
  onboarding guard commands.
- Audience filters accept exact labels with hyphen/underscore aliases,
  include broad `operators` / `maintainers` role filters for compound
  labels, and reject partial fragments such as `maint` or `agent`.
- Provider contract routes include the focused contract validation and
  factory/session wiring commands that appear in `tests/contracts/README.md`.
- Routes are unique, resolve to local sources, and match GitHub
  heading anchors for fragments.
- Command hints are valid local commands, appear on their target pages,
  and use repo-local `uv run` form where appropriate.
- The route order keeps quickstart, CLI/scaffolds, docs map, teaching,
  first lesson, examples, architecture, and coding-agent routes on the first
  screen.

**Backed by.** `tests/cli/test_app.py`, `tests/docs`, and
`tests/cli/test_json_schema.py`.

---

## Plan 5 — `init` template rendering

**Concern.** The scaffolded project must be runnable — `agent.py`
must be valid Python after substitution, files must be in the right
place, substitutions must not leak raw `$VAR` tokens into user code.

**Risks.** Missing substitution variables; binary/dotfile mishandling
(`.env.example`, `.gitignore` not copied); `agent.py` line budget
regression; README sections dropped during rendering.
OpenAI-key templates missing the `easycat doctor` preflight before the
first run; copied local cache, build, coverage, docs, mutation, package
metadata, bytecode, or secret-key artifacts leaking into generated
projects.

**Checks.**
- For each shipped template: `init` produces agent.py, .env.example,
  .gitignore, pyproject.toml, README.md.
- `agent.py` parses with `ast` after substitution with a representative
  config.
- `agent.py` does not contain `$AGENT_NAME`, `$AGENT_INSTRUCTIONS`, or
  `$PROJECT_NAME` (substitutions must succeed).
- `agent.py` stays within its per-template line budget.
- README contains all five required sections: Install, Configure, Run,
  Check, Next steps.
- README Install section names the rendered base `easycat[...]` package
  requirement from `pyproject.toml`.
- The scaffolded `easycat[...]` version floor tracks the project package
  version, so release bumps do not leave template dependencies stale.
- README tells every OpenAI-key template to run
  `uv run easycat doctor --env-file .env` during setup.
- `easycat init` success output mirrors each template README's run and
  local lint/syntax check commands.
- `easycat init --list-templates` includes per-template metadata
  (mode, transport, framework, best-fit guidance, base package requirement,
  base extras, required environment variables, optional environment knobs,
  generated files, description, and copyable create commands plus
  post-scaffold doctor/check/fix/docs/json-schema/run commands), and `--json`
  exposes the same catalog for tooling.
- `pyproject.toml` pins `easycat[<extra>]`.
- `pyproject.toml` includes a `dev` dependency group with Ruff so
  `uv run ruff check ...` works after the documented `uv sync`.
- `.gitignore` contains no placeholders.
- `.gitignore` covers local env variants, caches, local agent/tool state,
  build and wheel outputs, coverage reports, docs builds, mutation-test
  output, package metadata, bytecode, and local `.pem` / `.key` files.
- The scaffold copier skips the same local artifact directories plus
  coverage files, `.egg-info` package metadata, bytecode suffixes, and
  local secret suffixes.
- `easycat init --json` reports only the clean generated-project manifest,
  with generated/cache/package/secret artifacts omitted while the real
  top-level `.gitignore` remains.

**Backed by.** `test_init.py` (happy paths) and `test_templates.py`
(per-template parametrized checks).

---

## Plan 6 — `init` schema rejection paths

**Concern.** Coding agents (Claude Code, Cursor, Codex) send typos
and mis-shaped JSON; silent acceptance is worse than loud rejection
because the user then debugs a broken scaffold instead of a typo.

**Risks.** Invalid JSON swallowed; unknown keys accepted silently;
missing `schema_version` not detected; wrong `template` value
accepted.

**Checks.**
- Non-JSON `--config` → `EASYCAT_E102`, exit 4.
- `--config` missing `schema_version` → `EASYCAT_E102`, exit 4.
- `--config` with unsupported `schema_version` → `EASYCAT_E102`.
- Unknown key → `EASYCAT_E102` with fuzzy suggestion ("Did you mean
  'template'?").
- Unknown template string → `EASYCAT_E103`, exit 2.

**Backed by.** `test_init.py` schema and error-path tests.

---

## Plan 7 — `init` overwrite safety

**Concern.** `init` must never silently overwrite existing work.

**Risks.** Empty-vs-non-empty directory handling (empty dirs should
be OK to fill); `--force` misbehaving; interactive branch accidentally
overwriting without a confirm.

**Checks.**
- Existing non-empty directory → `EASYCAT_E101`, exit 101.
- Existing empty directory → OK (populates).
- `--force` overrides, scaffolded files land on top; pre-existing
  files not referenced by the template survive (spec: init writes
  into the dir, does not wipe it).

**Backed by.** `test_init.py::test_init_target_exists_without_force`
and `test_init_force_overwrites_existing`.

---

## Plan 8 — `doctor` check matrix

**Concern.** Doctor must produce accurate status for first-run
environment, provider, optional-runtime, journal, and disk checks; the
rendered report must not misrepresent anything as "ok" that failed (or
vice versa).

**Risks.** A check raising an uncaught exception mid-report; a skip
incorrectly counted as a failure; the summary line diverging from
the per-row statuses.

**Checks.**
- With no API keys in a generic project: env_* rows and env_any skip;
  keyless/local/custom setups remain valid and the overall run can pass.
- With one API key: corresponding env_* row passes; reachability
  probe fires (network stubbed), reports network liveness, and explicitly
  does not claim credential validity.
- Obvious example credentials fail without issuing a network probe.
- Generated scaffold metadata makes required env checks project-aware;
  Twilio projects validate the stream URL and auth token as well as the
  provider key.
- With a probe failure (stubbed `httpx.head`): reachability row
  fails with `EASYCAT_E204`, exit 1.
- `--provider openai` filters reachability so other providers are
  not probed.
- `--env-file .env` loads scaffolded project keys without permanently
  mutating the process environment.
- `--environment production` drops the local microphone probe.
- Journal/disk checks probe `.easycat/journals` by default and honor
  `EASYCAT_DATA_DIR`.
- Unknown `--environment` and unknown provider names → exit 2.

**Backed by.** `test_doctor.py`.

---

## Plan 9 — `doctor` network isolation

**Concern.** Doctor probes real provider endpoints. In CI we never
hit the network, and in user-controlled environments network probes
must tolerate offline/captive portals without blowing up the doctor
report.

**Risks.** Tests accidentally issuing real HTTP; `httpx` raising an
exception type not in our handler; the 2s timeout not honored
(tests hanging).

**Checks.**
- All doctor tests use a fixture that patches `httpx.head` to a
  stub — no real network is hit.
- `ConnectError` is caught and rendered as a `fail` with
  `EASYCAT_E204`.
- Timeout flow covered via stub raising `httpx.TimeoutException`.

**Backed by.** `test_doctor.py::no_network` fixture; plus
`test_doctor_reports_httpx_failure`.

---

## Plan 10 — Error-code registry integrity

**Concern.** The registry is the single source of truth for
`easycat explain`, raising code, and CLI exit codes. Any
inconsistency between registration and factories breaks the
contract.

**Risks.** Duplicate registration silently clobbering entries;
placeholder mismatches between factory call sites and headline
templates; factories leaking partial context into error messages.

**Checks.**
- Registering the same code twice raises `RuntimeError`.
- Factory call with all expected kwargs produces a properly tagged
  `EasyCatError` (code + message substitution + context).
- Factory call missing a required placeholder raises `RuntimeError`
  at dev time (never silently formats `{foo}` as text).
- Factory call with unused kwargs stores them in `context` for CLI
  rendering without breaking the substitution.

**Backed by.** `test_errors.py`.

---

## Plan 11 — Exit-code contract stability

**Concern.** Shell scripts and CI pipelines branch on CLI exit
codes; changes here are breaking changes. The mapping between
`EASYCAT_Exxx` and exit codes must be explicit and stable.

**Risks.** A new error code defaulting to exit 1 because no mapping
was added; the `exit-codes` explain doc drifting from the actual
mapping.

**Checks.**
- Every code in `_CODE_TO_EXIT` is documented in the `exit-codes`
  meta entry (the doc must list every non-default mapping).
- Every code in `_CODE_TO_EXIT` is also in `REGISTRY`.
- Unlisted codes fall back to exit 1.
- The documented exit codes (0, 1, 2, 3, 4, 5, 6, 101, 130) all
  appear in the exit-codes meta body.

**Backed by.** `test_errors.py::test_exit_code_mapping` and
`test_exit_codes.py`.

---

## Plan 12 — JSON envelope stability

**Concern.** Every `--json` output shares a versioned envelope:
`{"schema_version": 1, "command": "...", "status": "ok|error",
...}`. Stability matters because coding agents parse it.

**Risks.** A command drifting away from the envelope; `schema_version`
bumping without a migration; stderr content leaking into the JSON on
stdout and corrupting `jq` consumers.

**Checks.**
- `--json` output from `init`, `doctor`, `docs`, `explain` (both
  single-code and `--list`), `bundles list`, `bundles show`,
  `bundles export`, `inspect`, `replay`, `validate quick`,
  `validate contracts`, `validate release`, and `validate report` all include
  `schema_version: 1`, a `command` field matching the command name,
  and a `status` in {ok, error}.
- The JSON payload appears on stdout; stderr is either empty or
  strictly logs/progress (never JSON fragments).
- EasyCat registry errors include `code`, `message`, `fix`, `context`,
  and `exit_code`; command-specific errors still include `message` and
  `exit_code` without inventing a fake `EASYCAT_Exxx` code.
- Early command-specific usage errors, such as `explain` missing `CODE`,
  `init` missing `NAME`, `doctor --env-file` parse failures, and mutually
  exclusive `validate` flags, still emit the envelope in `--json` mode.
- Debug-bundle failures in `bundles show`, `bundles export`, and
  `replay` keep their documented exit codes while emitting parseable
  `--json` error envelopes.

**Backed by.** `test_json_schema.py` for the shared envelope shape, plus
command-specific CLI suites for deeper payload details.

---

## Plan 13 — `validate` command and report rendering

**Concern.** Validation should have one obvious command surface while
preserving the global `--json` stdout envelope contract and keeping
persisted reports under `--report`.

**Risks.** Accidentally exposing raw pytest exit codes; report JSON
becoming unreadable on failure; missing report artifacts not being
called out; `--json` output diverging from the standard envelope.

**Checks.**
- `easycat validate quick --report PATH` writes a report and renders a
  concise human summary.
- `easycat validate quick --json` emits the standard JSON envelope on
  stdout.
- `easycat validate socket` returns the validation exit code, not the
  raw pytest exit code.
- `easycat validate contracts --json` dispatches the contract marker
  slice and emits the standard JSON envelope on stdout.
- `easycat validate release --json` dispatches the installed-wheel
  release gate, preserves release options, and emits the standard JSON
  envelope on stdout.
- `easycat validate report .easycat/validation/latest.json` renders run
  status, checks, git dirty state, expected skips, failures, artifact paths,
  and missing artifact warnings.
- `easycat validate report .easycat/validation/latest.json --json` re-emits
  the saved validation run inside the standard envelope and keeps failed
  reports parseable.
- Missing, invalid, unsupported-schema, and unknown-kind report files
  fail explicitly.

**Backed by.** `test_validate_cli.py`, `test_validate_report_cli.py`,
`test_validate_report_model.py`, and `test_validate_runner.py`.

---

## Plan 14 — Library prereqs — `run()` lifecycle

**Concern.** `easycat.run(config)` is the entry point every template
uses. If lifecycle is broken (async-enter/start/stop ordering), voice
agents will hang on Ctrl-C.

**Risks.** `run()` bypassing the `async with session` teardown path;
signal handlers not wired; TTY-vs-non-TTY feedback attachment
misbehaving; the feedback subscription firing in a pytest session
and polluting stdout.

**Checks.**
- `run()` calls `create_session`, enters the session async context,
  waits for shutdown, and exits through `stop(force=True)` — under a
  mock that swaps the real Session.
- `PYTEST_CURRENT_TEST` env var suppresses the TTY feedback hook.
- `run()` is exposed at `easycat.run` (public attribute).
- Signal handlers are added for SIGINT and SIGTERM.

**Backed by.** `test_library_prereqs.py::TestRun`.

---

## Plan 15 — Library prereqs — string-keyed providers

**Concern.** `EasyConfig(stt="deepgram/flux")` is the headline DX
win the plan promised. If the string parser silently mis-routes or
grabs the wrong env var, templates ship with broken defaults.

**Risks.** `model_id` (ElevenLabs) not mapped from `model`; fuzzy
match suggesting an off-tree provider; empty env var slipping
through as a valid key.

**Checks.**
- `parse_stt_string("deepgram/flux")` with `DEEPGRAM_API_KEY` set →
  `DeepgramSTTConfig(model="flux", api_key=...)`.
- `parse_tts_string("elevenlabs/eleven_flash_v2_5")` with
  `ELEVENLABS_API_KEY` set → `ElevenLabsTTSConfig(model_id=...)`.
- Unknown provider → `EASYCAT_E104` with fuzzy suggestion.
- Missing env var → `EASYCAT_E203`.
- `EasyConfig(stt="...")` resolves in `__post_init__` before any
  downstream check.
- Env autodetect: `EasyConfig(agent=...)` with only
  `OPENAI_API_KEY` in env picks OpenAI STT/TTS.

**Backed by.** `test_library_prereqs.py::TestProviderStrings`.

---

## Plan 16 — Packaging — wheel and sdist ship template dotfiles, metadata, and clean contents

**Concern.** `uvx easycat init my-agent` from a PyPI-installed
`easycat` must get the full template catalog, including `.env.example`
and `.gitignore`. Build backends have been known to strip dotfiles
silently. The release artifact also needs useful package-index
metadata before users read the README, and it must not include local
cache, generated report/build output, package metadata, bytecode, or
secret-key artifacts that happen to exist under `src/`.

**Risks.** `uv_build` / hatchling excluding dotfiles; a templates
subdir missing from the wheel; files copied under a different tree
structure than the source; missing author/project/classifier metadata
on PyPI; ignored cache, coverage, docs, mutation, package metadata,
bytecode, or local secret-key artifacts leaking into release artifacts.

**Checks.**
- `uv build --wheel` and `uv build --sdist` succeed on a clean checkout.
- The built wheel contains
  `easycat/cli/scaffold/templates/<name>/{agent.py, pyproject.toml,
  README.md, .env.example, .gitignore}` for each shipped scaffold
  template.
- Wheel metadata includes the package name, Python requirement, author,
  project URLs, keywords, and core classifiers.
- Wheel and sdist contents reject cache, local-tool, test, build, coverage,
  docs, mutation, VCS, virtualenv, and package metadata artifacts, including
  ignored `.ruff_cache`, `.uv-cache`, `.agents`, `.codex`,
  `.coverage`, `.egg-info`, bytecode, or local `.pem` / `.key` files
  under `src/`.

**Backed by.** `tests/cli/test_packaging.py` (marked
`integration_local` so heavier wheel-build checks can be selected or
filtered explicitly).

---

## Plan 17 — End-to-end scaffold-and-invoke

**Concern.** The scaffolded project itself must be usable. Users
type `cd my-agent && uv sync && uv run --env-file .env python agent.py` —
if any link is broken, the whole onboarding promise evaporates.

**Risks.** `uv sync` failing because the template pins an
unpublished `easycat` version; a scaffolded Python entry point or
support module failing at import time; env var not loading from `.env`.

**Checks.**
- Scaffold each template into a tmpdir.
- Every top-level scaffolded Python file passes `py_compile` without
  actually running the agent.
- Every top-level scaffolded Python file passes `ruff check` with the
  project's own ruff config.
- Resolution-only install smoke: `uv lock` succeeds in the scaffolded
  project with `--easycat-source` pointed at this checkout (the
  pre-launch `[tool.uv.sources]` wiring). Skipped when `uv` is missing
  or PyPI is unreachable. A full `uv sync` round-trip is intentionally
  *not* run — it would download numpy/onnxruntime wheels on every
  guard run; resolution proves the install path without the weight.

**Backed by.** `tests/cli/e2e/test_scaffold_smoke.py` (covers
`py_compile` + `ruff` + `uv lock` resolution per template) and
`tests/cli/e2e/test_generated_project_wheel.py`, which installs the built
wheel plus the agent SDK into a throwaway venv outside this checkout, then
scaffolds and runs a generated project's own tests there with an ambient
credential and outbound provider traffic blocked. That module is
`integration_external` and runs only when a maintainer selects it; the
automated peer is `ci.yml`'s `generated-app-smoke` job.
