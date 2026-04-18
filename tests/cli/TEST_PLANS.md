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
| 4 | `init` template rendering | `test_init.py` + `test_templates.py` |
| 5 | `init` schema rejection paths | `test_init.py` |
| 6 | `init` overwrite safety | `test_init.py` |
| 7 | `doctor` check matrix | `test_doctor.py` |
| 8 | `doctor` network isolation | `test_doctor.py` + network stubs |
| 9 | Error-code registry integrity | `test_errors.py` |
| 10 | Exit-code contract stability | `test_errors.py` + `test_exit_codes.py` |
| 11 | JSON envelope stability | `test_json_schema.py` |
| 12 | Library prereqs — `run()` lifecycle | `test_library_prereqs.py` |
| 13 | Library prereqs — string-keyed providers | `test_library_prereqs.py` |
| 14 | Packaging — wheel ships template dotfiles | `test_packaging.py` (integration) |
| 15 | End-to-end scaffold-and-invoke | `test_cli_e2e.py` (integration) |

Plans 1-9 are fast unit tests. Plans 10-13 add coverage for cross-
cutting contracts. Plans 14-15 are marked `integration_local` so they
run in CI but not on every `pytest` invocation.

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
- `--help` renders, exit 0, contains `init`, `doctor`, `explain`.
- Bare `easycat` prints the journey menu (both groups: Scaffold,
  Debug with the journal).
- E2E: `uvx easycat --version` works on a clean machine (covered by
  the wheel test at the bottom).

**Backed by.** `tests/cli/test_app.py` (4 tests).

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

**Backed by.** `test_explain.py` (10 tests).

---

## Plan 4 — `init` template rendering

**Concern.** The scaffolded project must be runnable — `agent.py`
must be valid Python after substitution, files must be in the right
place, substitutions must not leak raw `$VAR` tokens into user code.

**Risks.** Missing substitution variables; binary/dotfile mishandling
(`.env.example`, `.gitignore` not copied); `agent.py` line budget
regression; README sections dropped during rendering.

**Checks.**
- For each of the 3 templates: `init` produces agent.py, .env.example,
  .gitignore, pyproject.toml, README.md.
- `agent.py` parses with `ast` after substitution with a representative
  config.
- `agent.py` does not contain `$AGENT_NAME`, `$AGENT_INSTRUCTIONS`, or
  `$PROJECT_NAME` (substitutions must succeed).
- `agent.py` stays within its per-template line budget.
- README contains all four required sections.
- `pyproject.toml` pins `easycat[<extra>]`.
- `.gitignore` contains no placeholders.

**Backed by.** `test_init.py` (happy paths) and `test_templates.py`
(per-template parametrized checks).

---

## Plan 5 — `init` schema rejection paths

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

**Backed by.** `test_init.py` (5 error-path tests).

---

## Plan 6 — `init` overwrite safety

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

## Plan 7 — `doctor` check matrix

**Concern.** Doctor must produce accurate status for each of the 5
M1 checks, and the rendered report must not misrepresent anything as
"ok" that failed (or vice versa).

**Risks.** A check raising an uncaught exception mid-report; a skip
incorrectly counted as a failure; the summary line diverging from
the per-row statuses.

**Checks.**
- With no API keys: Python/EasyCat pass; env_* rows skip; env_any
  fails with `EASYCAT_E203`; overall exit 1.
- With one API key: corresponding env_* row passes; reachability
  probe fires (network stubbed) and passes.
- With a probe failure (stubbed `httpx.head`): reachability row
  fails with `EASYCAT_E204`, exit 1.
- `--provider openai` filters reachability so other providers are
  not probed.
- Unknown `--environment` → exit 2.

**Backed by.** `test_doctor.py` (7 tests).

---

## Plan 8 — `doctor` network isolation

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

## Plan 9 — Error-code registry integrity

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

**Backed by.** `test_errors.py` (7 tests).

---

## Plan 10 — Exit-code contract stability

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

**Backed by.** `test_errors.py::test_exit_code_mapping` and new
`test_exit_codes.py`.

---

## Plan 11 — JSON envelope stability

**Concern.** Every `--json` output shares a versioned envelope:
`{"schema_version": 1, "command": "...", "status": "ok|error",
...}`. Stability matters because coding agents parse it.

**Risks.** A command drifting away from the envelope; `schema_version`
bumping without a migration; stderr content leaking into the JSON on
stdout and corrupting `jq` consumers.

**Checks.**
- `--json` output from `init`, `doctor`, `explain` (both single-code
  and `--list`) all include `schema_version: 1`, a `command` field
  matching the command name, and a `status` in {ok, error}.
- The JSON payload appears on stdout; stderr is either empty or
  strictly logs/progress (never JSON fragments).
- When a command errors, the envelope includes `code`, `message`,
  `context`, `exit_code`.

**Backed by.** New `test_json_schema.py`.

---

## Plan 12 — Library prereqs — `run()` lifecycle

**Concern.** `easycat.run(config)` is the entry point every template
uses. If lifecycle is broken (start/stop/shutdown ordering), voice
agents will hang on Ctrl-C.

**Risks.** `run()` forgetting `session.shutdown()` on exception;
signal handlers not wired; TTY-vs-non-TTY feedback attachment
misbehaving; the feedback subscription firing in a pytest session
and polluting stdout.

**Checks.**
- `run()` calls `create_session`, `session.start`, waits for
  shutdown, and calls `session.shutdown` — under a mock that swaps
  the real Session.
- `PYTEST_CURRENT_TEST` env var suppresses the TTY feedback hook.
- `run()` is exposed at `easycat.run` (public attribute).
- Signal handlers are added for SIGINT and SIGTERM.

**Backed by.** New `test_library_prereqs.py::TestRun`.

---

## Plan 13 — Library prereqs — string-keyed providers

**Concern.** `EasyCatConfig(stt="deepgram/flux")` is the headline DX
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
- `EasyCatConfig(stt="...")` resolves in `__post_init__` before any
  downstream check.
- Env autodetect: `EasyCatConfig(agent=...)` with only
  `OPENAI_API_KEY` in env picks OpenAI STT/TTS.

**Backed by.** New `test_library_prereqs.py::TestProviderStrings`.

---

## Plan 14 — Packaging — wheel ships template dotfiles

**Concern.** `uvx easycat init my-agent` from a PyPI-installed
`easycat` must get the full template catalog, including `.env.example`
and `.gitignore`. Build backends have been known to strip dotfiles
silently.

**Risks.** `uv_build` / hatchling excluding dotfiles; a templates
subdir missing from the wheel; files copied under a different tree
structure than the source.

**Checks.**
- `uv build --wheel` succeeds on a clean checkout.
- The built wheel contains
  `easycat/cli/scaffold/templates/<name>/{agent.py, pyproject.toml,
  README.md, .env.example, .gitignore}` for each of the three M1
  templates.

**Backed by.** New `tests/cli/test_packaging.py` (marked
`integration_local` to keep the wheel build out of the fast test
suite).

---

## Plan 15 — End-to-end scaffold-and-invoke

**Concern.** The scaffolded project itself must be usable. Users
type `cd my-agent && uv sync && uv run python agent.py` — if any
link is broken, the whole onboarding promise evaporates.

**Risks.** `uv sync` failing because the template pins an
unpublished `easycat` version; the scaffolded `agent.py` failing at
import time; env var not loading from `.env`.

**Checks.**
- Scaffold each template into a tmpdir.
- The scaffolded `agent.py` passes `py_compile` without actually
  running the agent.
- The scaffolded `agent.py` passes `ruff check` with the project's
  own ruff config.
- Optional (gated on PyPI availability or a local index): full
  `uv sync` round-trip. This is marked `integration_local` and
  skipped when `EASYCAT_LOCAL_INDEX` isn't set — we don't publish
  on every test run.

**Backed by.** `tests/cli/e2e/test_scaffold_smoke.py` (already covers
`py_compile` + `ruff` per template; `uv sync` skipped in base CI).
