# Phase 3 — Feedback Loop: Debugger, Evals, Replay, Budgets

Status: active implementation plan.

> **Stale (runtime budgets removed):** the runtime budget code this plan treats
> as *existing* foundations — `LatencyBudgetMonitor`
> (`session/_latency_budget.py`), `CostBudgetEnforcer` (`session/_cost_budget.py`),
> `cost_budget_status` / `runtime/costs.py`, the debugger `/api/cost` rollup +
> Cost tab, and the `_budget_matches_stage` stage-record tagging — was removed.
> `LatencyBudget` (`easycat.validation.latency`) still exists. Treat every
> reference below to the removed symbols as net-new (rebuild from scratch), and
> note the overall budgets direction (especially cost budgets and
> `max_session_cost_usd`) needs re-scoping against the "report latency, no
> runtime/cost budgets" decision before this workstream is implemented.

## Goal

Make EasyCat’s journal/replay/debugger stack the default development and CI
feedback loop:

1. Run a voice app locally.
2. See a live timeline in the browser debugger.
3. Promote bad turns into regression tests.
4. Run conversation scenarios in CI.
5. Enforce latency and cost budgets consistently.

## Why This Matters

Voice bugs are temporal. A traditional log line rarely explains why a bot
interrupted a user, why first audio was late, why a tool call caused silence, or
which provider caused a latency spike. EasyCat already records rich runtime
journals; Neo should turn those journals into a product advantage.

## Existing Building Blocks

Reuse (all confirmed to exist at the cited locations):

- Runtime journal as the source of truth.
- Debugger server and `DebuggerSource` abstraction.
- Existing timeline, waterfall, transcript, audio, cost, replay, annotation, and
  bundle export routes.
- `runtime/replay.py` and `ReplaySpec` (tool policy DENY-by-default — inherited
  for free by `easycat.evals`).
- `debug/testing.py` text-turn and bundle assertion helpers (`load_bundle`,
  `assert_no_error`, `assert_tool_called`, `assert_regex`, `assert_exact_match`,
  `assert_latency`, `assert_turn_completed` — these are the pure re-export set).
- `LatencyBudget` (`easycat.validation.latency.LatencyBudget`) and validation
  latency summaries.
- `LatencyBudgetMonitor` runtime records.
- `CostBudgetEnforcer` and cost budget status helpers.
- CLI/debugger bundle export and the existing (unsafe-by-default) `journal
  promote` command — see Workstream C for why promotion is HARDENED, not reused
  as-is.
- The `_autolaunch.py` guard and `redact_value` /
  `contains_unredacted_sensitive_text` / `_assert_context_pack_redacted`
  redaction helpers.

**Net-new (do NOT treat as existing):** `CostBudget`, `build_budget_report`,
`BudgetReport`, `assert_budgets_pass`, `promote_turn_to_test`, `EvalRunner`,
`EvalScenario`, `EvalTurn`, `ScenarioResult`, and the `easycat.evals` /
`easycat.budgets` packages and `easycat eval` CLI. These have zero hits in
`src/` today.

## Workstream A — Always-Available Dev Timeline

### New Files

```text
src/easycat/debugger/dev.py
src/easycat/debugger/session_registry.py
tests/debugger/test_dev_autolaunch.py
```

### Behavior

Add explicit dev debugger activation:

```bash
EASYCAT_DEV=1 easycat serve
```

Programmatic:

```python
VoiceApp(agent=agent, dev=True).run("browser")
```

Dev mode should:

- default to durable debugging when no explicit debug config is supplied,
- register live sessions with a process-local debugger registry,
- launch a loopback debugger UI once per process,
- expose a session selector in the UI,
- show budget badges and timeline overlays,
- expose a “promote turn to test” action.

Do **not** make `debug="full"` alone open a browser. Durable journaling and UI
autolaunch should remain separate concepts.

**Guarded invariant (not new protection — reframed per R7).** The
no-autolaunch protection ALREADY EXISTS and is tested: `_autolaunch.py:40-51,70`
implements `_autolaunch_opted_in()` / `maybe_launch_debugger_ui()` such that
`debug="full"` alone never arms a browser tab or a port bind (only an explicit
`EASYCAT_DEBUGGER_AUTOLAUNCH` env truthy value or the
`observability.debugger_autolaunch` config knob arms it), and
`tests/test_dx_helpers.py:603-625` covers it. The real risk Workstream A
introduces is the NEW `EASYCAT_DEV` / `VoiceApp(dev=True)` opt-in accidentally
RELAXING that guarantee. Therefore the dev opt-in MUST be **purely additive**:
it may add a fresh autolaunch trigger but must NOT weaken the existing
`debug="full"`-alone-never-autolaunches behavior. A regression test must assert
that `debug="full"` with neither `EASYCAT_DEV` nor `dev=True` still does not
launch.

### API Additions

Debugger server additions:

```text
GET  /api/dev/sessions
GET  /api/budgets
POST /api/dev/promote
```

Internal registry:

```python
register_session(session: Session, *, label: str | None = None) -> str
unregister_session(session_id: str) -> None
list_sessions() -> list[LiveSessionSummary]
```

### UI Additions

Update debugger static assets to add:

- live session selector,
- active-session status,
- budget overlays on waterfall/timeline,
- promotion button,
- clearer dev/prod source labeling.

### Safety

- Loopback bind by default.
- No public debugger bind without explicit auth/design review.
- No auto-open in non-interactive/CI environments.
- No PII export without warning.
- The `EASYCAT_DEV` / `dev=True` opt-in is additive over the existing
  `_autolaunch.py` guard: it never relaxes the
  `debug="full"`-alone-never-autolaunches invariant (R7). Enforced by a test
  that asserts the registry/launch hook is invoked for the dev opt-in path and
  is NOT invoked for `debug="full"` alone.

## Workstream B — Native Eval and Simulation APIs

### New Package

Workstream B **SCAFFOLDS** the whole `src/easycat/evals/` package (stub modules
+ `__init__` exports). The three files that also appear in Workstream C
(`promote.py`, `replay_test.py`, and the sibling `cli/evals.py`) are CREATED
here and IMPLEMENTED in Workstream C — each file has exactly one owner per phase
(B scaffolds, C implements); they are not duplicated work.

```text
src/easycat/evals/
  __init__.py
  assertions.py
  promote.py        # scaffolded here; implemented in Workstream C
  pytest.py
  replay_test.py    # scaffolded here; implemented in Workstream C
  runner.py
  scenario.py
  simulation.py
```

### Public API Sketch

The `easycat.evals` surface is two distinct sets that the public-API test must
assert **explicitly and separately**, because they carry different testing
obligations:

**(A) Pure re-exports of `easycat.debug.testing`** (already exist at the cited
locations — no new behavior, just an import surface; covered by an
`from easycat.evals import ...` smoke test):

```python
from easycat.evals import (
    load_bundle,  # debug/testing.py:93
    assert_no_error,  # debug/testing.py:314
    assert_tool_called,  # debug/testing.py:333
    assert_regex,  # debug/testing.py:279
    assert_exact_match,  # debug/testing.py:256
    assert_latency,  # debug/testing.py:360 (NOTE: existing signature differs
    # from any budget-aware helper; do NOT conflate with
    # assert_budgets_pass below)
    assert_turn_completed,  # debug/testing.py:300
)
```

**(B) NET-NEW — require full unit tests** (do NOT exist today; the sketch must
not present them as re-exports):

```python
from easycat.evals import (
    assert_budgets_pass,  # NET-NEW (no such symbol today)
    EvalRunner,  # NET-NEW
    EvalScenario,  # NET-NEW
    EvalTurn,  # NET-NEW
    promote_turn_to_test,  # NET-NEW (only the CLI `promote_turn` exists today,
    # at cli/debug/bundles.py:1820 — no library fn)
    ScenarioResult,  # NET-NEW
)
```

`assert_budgets_pass` and `promote_turn_to_test` have **zero** hits in `src/`
today; the only adjacent existing symbols are `assert_latency` (set A, different
signature) and the CLI `promote_turn` command. The import/public-API test must
assert BOTH set (A) and set (B) explicitly so the net-new symbols cannot
silently regress to missing.

### Scenario Model

`CostBudget` is **NET-NEW** (defined in Workstream D below — see
"Models") and MUST be defined before `EvalScenario` references it; do not treat
it as an existing symbol. `LatencyBudget` already exists at
`easycat.validation.latency.LatencyBudget` and is re-exported by Workstream D.

```python
@dataclass
class EvalScenario:
    name: str
    turns: list[EvalTurn]
    # CostBudget is NET-NEW (Workstream D); LatencyBudget is existing.
    budgets: list[LatencyBudget | CostBudget] = field(default_factory=list)


@dataclass
class EvalTurn:
    user: str
    expect_response_regex: str | None = None
    expect_tools: list[str] = field(default_factory=list)
```

YAML/JSON schema example:

```yaml
name: refund_flow
budgets:
  latency:
    - stage: total_ms
      max_ms: 1500
  cost:
    max_session_cost_usd: 0.05
turns:
  - user: "I need a refund"
    expect:
      response_regex: "refund|order"
      tools:
        - lookup_order
```

### Runner Strategy

Start with text-first simulation:

- Build on `create_text_session` and existing `run_text_turn` behavior.
- Return a journal-backed `ScenarioResult`.
- Make `ScenarioResult.records()` compatible with existing assertion helpers.
- Keep LLM-as-judge optional and dependency-free by default.

Audio simulation and synthetic callers can follow after text scenarios are
stable.

#### Text-mode budget constraint (TEST-4)

Text scenarios are the ONLY no-API-key execution mode, and the text turn path
emits ONLY `stage="total_ms"` (`_turn_runner.py:846-853`). It emits no
provider-stage latency records. Because the runtime budget monitor is
push-based — `violations()` only fires when a sample is recorded for a stage
(`session/_latency_budget.py:44-64`) — a provider-stage budget such as
`tts_ttfb_ms` or `llm_ttft_ms` in a text scenario evaluates against ZERO
samples and would pass **vacuously**, silently green-lighting a regression.

Therefore:

- Text scenarios may assert only **turn-total** (`total_ms`) latency budgets
  plus **cost** budgets.
- The runner MUST detect a provider-stage latency budget attached to a
  text-mode scenario and RAISE a clear `"no samples for stage X"` error rather
  than passing silently. Add a unit test for this raise.
- Provider-stage latency budgets only become meaningful once the
  audio-simulation runner (and the net-new runtime metrics in Workstream D)
  land; document that ordering.

### CLI

Add (all three are IN SCOPE for the evals-runner milestone — `eval report` is
not optional):

```bash
easycat eval run tests/evals/refund_flow.yaml
easycat eval run tests/evals --json
easycat eval report .easycat/evals/latest.json
easycat eval report .easycat/evals/latest.json --json
```

`easycat eval report` mirrors `easycat validate report`
(`cli/validate.py:676,833`): it loads a persisted result file and re-emits a
summary. Its `--json` form MUST emit a JSON report envelope with
`schema_version=1`, matching the envelope contract enforced by
`tests/cli/test_json_schema.py`. Track it with its own acceptance row
("`easycat eval report --json` emits JSON report envelope, `schema_version=1`")
in the acceptance matrix.

## Workstream C — Replay Production Sessions As Tests

### Files To Implement (created in B)

These files are SCAFFOLDED in Workstream B's package tree; Workstream C provides
their implementation. They are listed here only to mark Workstream C as the
owner of the logic, not to re-create the files:

```text
src/easycat/evals/promote.py      # scaffolded in Workstream B
src/easycat/evals/replay_test.py  # scaffolded in Workstream B
src/easycat/cli/evals.py          # scaffolded in Workstream B
```

### Modes

#### Record assertion mode

Inspect a bundle/journal without re-executing stages:

```python
from easycat.evals import load_bundle, assert_no_error, assert_regex, assert_tool_called


def test_refund_flow_regression():
    bundle = load_bundle("tests/fixtures/refund_flow.bundle")
    assert_no_error(bundle)
    assert_tool_called(bundle, tool_name="lookup_order")
    assert_regex(bundle, pattern="refund|order")
```

**Generated file must import and run (TEST-2).** Whatever symbols the generated
`.py` imports, `easycat.evals` MUST re-export — the existing
`_promote_test_stub` emits `from easycat.debug.testing import ...`
(`cli/debug/bundles.py:1755-1762`), so if promotion is forked under
`easycat.evals` the stub's import path must be updated and every imported symbol
(`load_bundle`, `assert_no_error`, `assert_turn_completed`, `assert_exact_match`,
plus any budget/regex helper used) must be present in `easycat.evals`.
Add a promotion test that writes the stub into `tmp_path` and then runs `pytest`
on it (importable + passes against the source fixture), mirroring
`_validate_promoted_slice`.

**Output-format reconciliation (SEQ-4).** The existing `journal promote`
command (`cli/debug/bundles.py:1820`) writes a `.zip` slice via `--out` today;
the plan's `eval promote` writes a `.py` regression test. These are two
different output contracts. Decide explicitly (see "Promotion CLI" below):
prefer a SINGLE `promote` verb. The recommendation is to FORK a new hardened
`eval promote` that owns the `.py` regression-test output and leave `journal
promote` as the documented legacy `.zip` slice path; reconcile the two verbs in
the M11 (renumbered) promotion milestone so we never ship two unreconciled,
divergently-defaulted promote commands.

#### Artifact replay mode

Use `ReplaySpec` with safe side-effect defaults:

```python
ReplaySpec(
    fidelity=ReplayFidelity.ARTIFACT,
    tool_policy=ToolReplayPolicy.DENY,
)
```

Never execute external tools during promoted production replay unless the user
explicitly opts in.

### Promotion CLI

**This workstream HARDENS an unsafe path — it does NOT preserve safe behavior.**
State this plainly. The path `eval promote` extends today is:

```
journal promote  ->  slice_bundle_by_turn  ->  debug/export.py:154-170
```

That path copies the **full raw NDJSON** for the sliced turn (transcripts and
tool arguments, `export.py:169-170`) AND **every referenced audio blob**
(`export.py:159-167`) into the committed slice, and the stub generator embeds
the **verbatim agent reply** into the test file via
`assert_exact_match(bundle, expected=<raw text>)`
(`cli/debug/bundles.py:1749-1750,1775-1786`). The existing command exposes only
`--out/--force/--json` and performs ZERO redaction anywhere in this path. The
plan previously documented the *opposite* (warn + no-audio + `--redact`) as if
it were existing behavior; it is not. Promotion is a NET-NEW hardening effort.

Add (FORK a new `eval promote` verb — see reconciliation below; do not silently
extend `journal promote`):

```bash
easycat eval promote PATH TURN_ID --out tests/test_regressions.py
```

Options (note the safe-by-default flags):

```bash
--no-audio / --include-audio   # --no-audio is the DEFAULT (mirror
                               # `bundles export` include_audio=False at
                               # cli/debug/bundles.py:1160)
--allow-pii                    # the ONLY gate that disables the redaction
                               # tripwire; off by default
--mode record-assertion        # default mode
--mode artifact-replay
--name test_refund_flow_regression
--assert-on hash|regex|exact   # default: hash (see below)
```

Promotion behavior (hardened):

1. **Redact by default.** Every promoted record is routed through `redact_value`
   before serialization (redact-by-default), reusing the structured redaction
   in `validation/redaction.py` (`UNSAFE_TEXT_FIELDS` covers `transcript`,
   `generated_text`, `provider_output`, `phone_number`, etc.).
2. **`--no-audio` is the DEFAULT.** Audio blobs are excluded unless
   `--include-audio` is passed, mirroring `bundles export`'s
   `include_audio=False` default (`bundles.py:1160`).
3. **Redaction tripwire gated by `--allow-pii`.** Before writing the committed
   file, run a `contains_unredacted_sensitive_text()` tripwire over the
   serialized output (mirroring `_assert_context_pack_redacted`) and RAISE
   unless `--allow-pii` is explicitly set. This makes "I committed a customer's
   transcript" an error, not a default.
4. **Record-assertion default asserts on a HASH/REGEX, not raw text.** Because
   redaction is field-name + secret-regex only — there is NO embedded-PII/NER
   detection (`validation/redaction.py:22-34,106-117`) — a transcript whose
   entire value IS the assertion target cannot be both redacted and useful. So
   the default `--assert-on` mode is `hash` (assert a stable hash of the reply),
   with `regex` as the redaction-safe alternative; `exact` (the current
   `assert_exact_match` behavior that embeds the verbatim reply) is opt-in and
   warns. Reuse `_promote_test_stub`/`_validate_promoted_slice` rather than
   re-implementing the stub/validation.
5. Generate the pytest skeleton (importable in `tmp_path`; see "Record assertion
   mode" above).
6. Suggest committable replay boundaries if the requested range is unsafe.

#### Extend-vs-fork decision (explicit)

**Decision: FORK `eval promote`** as the hardened replacement.
`journal promote` (`cli/debug/bundles.py:1820`) is documented as the **unsafe
legacy** path (full raw NDJSON + every audio blob + verbatim reply, no
redaction). Leaving both unreconciled is itself a footgun, so the M11
(renumbered) promotion milestone MUST reconcile the two verbs: `eval promote` is
the redact-by-default, `--no-audio`-default, tripwire-guarded path; `journal
promote` is retained only as the labeled legacy `.zip`-slice export. Do not
inherit the legacy defaults into the new command.

## Workstream D — Budgets Everywhere

### New Package

```text
src/easycat/budgets/
  __init__.py
  models.py
  report.py
  runtime.py
```

### Public API

`CostBudget`, `build_budget_report`, and `BudgetReport` are **NET-NEW** symbols
(zero hits in `src/` today) and require full unit tests. `LatencyBudget` is
EXISTING (`easycat.validation.latency.LatencyBudget`) and is re-exported from the
new package for ergonomics. All of `easycat.budgets` is a NET-NEW submodule
package (does NOT count against the top-level `easycat.__all__` cap).

```python
from easycat.budgets import CostBudget, LatencyBudget  # CostBudget NET-NEW
```

Compatibility:

```python
from easycat.validation.latency import LatencyBudget  # existing; keep working
```

### Models

`CostBudget` is defined here (Workstream D) and is the symbol referenced by
`EvalScenario.budgets` in Workstream B — it must be defined BEFORE the scenario
model imports it.

```python
@dataclass(frozen=True)
class CostBudget:  # NET-NEW
    max_session_usd: float
    warn_at: float = 0.8
    action: Literal["warn", "stop"] = "stop"
```

Keep `max_session_cost_usd` as a config alias into `CostBudget`.

### Runtime Metrics To Add

This is **NOT a clean additive set** — there are THREE distinct latency
vocabularies that must be RECONCILED, not naively added:

1. **Runtime** emits ONLY `stage="total_ms"` (`session/_turn_runner.py:686-696`
   for audio turns, `:847-853` for text turns). No provider-stage records exist
   at runtime today.
2. **Offline validation** columns `tts_ttfb_ms` / `llm_ttft_ms` already exist as
   `LatencyRow` fields and `DEFAULT_BUDGETS` stages in
   `validation/latency.py:113-114,308-314`.
3. **Waterfall milestones** use `*_to_*_ms` names
   (`debug/_turn_timeline.py:325-336`; `cli/debug/bundles.py:1448-1452`, e.g.
   `vad_endpoint_to_stt_final_ms`, `agent_request_to_first_token_ms`,
   `agent_first_token_to_tts_first_byte_ms`).

Classifying the six metrics against this reality:

- `tts_ttfb_ms`, `llm_ttft_ms` — **RENAMES/lifts** of existing
  offline-validation columns and waterfall milestones (NOT net-new). Wire them
  so the runtime flat name and the offline column refer to the same measurement.
- `stt_final_latency_ms`, `vad_endpointing_ms`, `first_audio_ms`,
  `barge_in_ack_ms` — **NET-NEW at runtime** (zero hits in `src/`), but
  equivalent measurements already exist as waterfall milestones to map FROM
  (e.g. `vad_endpoint_to_stt_final_ms` ↦ `stt_final_latency_ms`,
  `agent_first_token_to_tts_first_byte_ms` / `vad_endpoint_to_tts_first_byte_ms`
  inform `first_audio_ms`).

Explicit work items:

- (a) **Map the new flat metric names to `_budget_matches_stage`** so a
  `LatencyBudget(stage="tts_ttfb_ms")` matches the corresponding runtime record.
- (b) **Retrofit `validation/latency.py` `budget_violations` onto
  `build_budget_report`** so the offline percentile path and the runtime path
  evaluate budgets through one builder (see "Shared Report").

Feed the new runtime records through `LatencyBudgetMonitor` so runtime,
debugger, validation, and evals converge on ONE reconciled budget vocabulary
rather than three parallel ones.

### Shared Report

`build_budget_report` and `BudgetReport` are **NET-NEW** (zero hits in `src/`).
The builder must NOT be "a builder over journal records" only — that wording
structurally excludes the offline percentile path and would re-fragment the
vocabulary it is meant to unify (CONS-7). It must cover BOTH:

- the **runtime / journal-record** path (single-observation push budgets in
  `session/_latency_budget.py`), and
- the **offline percentile** path (`validation/latency.py` percentile columns
  evaluated by `budget_violations` against `DEFAULT_BUDGETS`).

```python
# Accepts runtime journal records AND offline percentile rows; both evaluate
# the same LatencyBudget/CostBudget set through one report.
build_budget_report(records, budgets) -> BudgetReport  # NET-NEW
```

Use it from:

- debugger `/api/budgets`,
- `easycat eval run`,
- `easycat latency`,
- validation reports (retrofit `validation/latency.py` `budget_violations` onto
  `build_budget_report`),
- promoted regression tests.

## Files To Add

- `src/easycat/debugger/dev.py`
- `src/easycat/debugger/session_registry.py`
- `src/easycat/evals/*`
- `src/easycat/budgets/*`
- `src/easycat/cli/evals.py`
- `tests/debugger/test_dev_autolaunch.py`
- `tests/evals/*`
- `tests/budgets/*`

## Files To Update

- `src/easycat/debugger/server.py`
- `src/easycat/debugger/static/index.html`
- `src/easycat/config/easy.py`
- `src/easycat/session/_latency_budget.py`
- `src/easycat/session/_cost_budget.py`
- `src/easycat/session/_journal_sink.py`
- `src/easycat/validation/latency.py`
- `src/easycat/debug/testing.py`
- `src/easycat/cli/_app.py`
- `tests/cli/test_json_schema.py` — add explicit envelope cases for
  `eval run`, `eval report`, and `eval promote` (`schema_version=1`). NOTE: this
  guard is hand-written with no registry walk (and `tests/cli/test_app.py:49-115`
  only checks `--help`), so new `--json` commands are NOT auto-covered. Add a
  coverage test that FAILS when a `--json` command lacks an envelope assertion.
- `tests/test_public_api.py` — assert BOTH the pure-re-export set (A) and the
  net-new set (B) of `easycat.evals` symbols explicitly (see "Public API
  Sketch").
- `docs/observability.md`
- `docs/testing-and-evals.md`
- `docs/latency.md`
- `docs/validation.md`
- `docs/reference/easyconfig.md`
- teaching chapters for journal/evals/latency

## Acceptance Criteria

- `EASYCAT_DEV=1` / `VoiceApp(dev=True)` invokes the debugger registry/launch
  hook exactly once per process and registers live sessions. Test asserts the
  launch hook is INVOKED (not that a browser literally opens — CI is
  non-interactive).
- `debug="full"` with neither `EASYCAT_DEV` nor `dev=True` does NOT invoke the
  launch hook (the dev opt-in is purely additive over the existing
  `_autolaunch.py` guard; R7).
- Debugger UI can switch among live sessions.
- Debugger exposes budget status and promotion endpoint.
- `easycat.evals` re-exports BOTH the pure-re-export set (A) and the net-new set
  (B); `tests/test_public_api.py` asserts both explicitly.
- `easycat.evals` supports text scenario execution without live audio or API-key
  requirements for basic fake agents.
- A provider-stage latency budget (`tts_ttfb_ms`/`llm_ttft_ms`) attached to a
  text-mode scenario RAISES a clear "no samples for stage X" error rather than
  passing vacuously (TEST-4).
- `easycat eval promote` generates a usable pytest skeleton that imports and
  runs under `pytest` in `tmp_path`.
- `easycat eval promote` redacts records by default (routes through
  `redact_value`), excludes audio by default (`--no-audio`), and RAISES on
  unredacted sensitive text unless `--allow-pii` is set; the default
  record-assertion mode asserts on a hash/regex of the reply, not the verbatim
  text. (This HARDENS the unsafe `journal promote` path; it does not preserve
  existing behavior.)
- `easycat eval report --json` emits a JSON report envelope with
  `schema_version=1` (mirrors `validate report`); covered by
  `tests/cli/test_json_schema.py`.
- Replay-as-test denies tool execution by default.
- `easycat.budgets.LatencyBudget` and legacy `easycat.validation.latency.LatencyBudget`
  imports both work; `CostBudget`/`build_budget_report`/`BudgetReport` are
  net-new and unit-tested.
- Runtime emits first-token/first-audio budget records where available, mapped
  onto `_budget_matches_stage` and reconciled with the offline
  `validation/latency.py` columns and the waterfall `*_to_*_ms` milestones.
- `build_budget_report` covers BOTH the runtime journal-record path and the
  offline percentile path; budget reports are shared by debugger, eval, CLI, and
  validation surfaces.

## Suggested PR Slice

1. Add `easycat.budgets` — net-new `CostBudget`, `build_budget_report`,
   `BudgetReport` plus the `LatencyBudget` re-export and the dual-path shared
   report model (runtime + offline percentile).
2. Scaffold the `easycat.evals` package: pure re-export set (A) of
   `debug/testing.py` helpers AND net-new symbols (B) as stubs, with the
   public-API test asserting both sets.
3. Add scenario dataclasses (`EvalScenario`/`EvalTurn`/`ScenarioResult`) and the
   text runner, including the text-mode provider-stage-budget RAISE (TEST-4).
4. Add the hardened, FORKED `eval promote` library + CLI: redact-by-default,
   `--no-audio` default, `--allow-pii`-gated tripwire, hash/regex default
   assertion; reconcile with legacy `journal promote`; add `eval report`
   (`--json`, `schema_version=1`).
5. Add runtime budget metric coverage, reconciling the three latency
   vocabularies and mapping flat names onto `_budget_matches_stage`.
6. Add debugger dev registry and API endpoints (dev opt-in additive over the
   `_autolaunch.py` guard).
7. Add UI budget overlays and promote-to-test button.
