# Phase 3 — Feedback Loop: Debugger, Evals, Replay, Budgets

Status: active implementation plan.

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

Reuse:

- Runtime journal as the source of truth.
- Debugger server and `DebuggerSource` abstraction.
- Existing timeline, waterfall, transcript, audio, cost, replay, annotation, and
  bundle export routes.
- `runtime/replay.py` and `ReplaySpec`.
- `debug/testing.py` text-turn and bundle assertion helpers.
- `LatencyBudget` and validation latency summaries.
- `LatencyBudgetMonitor` runtime records.
- `CostBudgetEnforcer` and cost budget status helpers.
- CLI/debugger bundle export and journal promotion commands.

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

## Workstream B — Native Eval and Simulation APIs

### New Package

```text
src/easycat/evals/
  __init__.py
  assertions.py
  promote.py
  pytest.py
  replay_test.py
  runner.py
  scenario.py
  simulation.py
```

### Public API Sketch

```python
from easycat.evals import (
    EvalRunner,
    EvalScenario,
    EvalTurn,
    assert_budgets_pass,
    assert_no_error,
    assert_tool_called,
    load_bundle,
    promote_turn_to_test,
)
```

### Scenario Model

```python
@dataclass
class EvalScenario:
    name: str
    turns: list[EvalTurn]
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

### CLI

Add:

```bash
easycat eval run tests/evals/refund_flow.yaml
easycat eval run tests/evals --json
easycat eval report .easycat/evals/latest.json
```

## Workstream C — Replay Production Sessions As Tests

### New Files

```text
src/easycat/evals/promote.py
src/easycat/evals/replay_test.py
src/easycat/cli/evals.py
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

Add:

```bash
easycat eval promote PATH TURN_ID --out tests/test_regressions.py
```

Options:

```bash
--redact
--include-audio / --no-audio
--mode record-assertion
--mode artifact-replay
--name test_refund_flow_regression
```

Promotion behavior:

1. Warn that journals/bundles may contain PII.
2. Default to redacted/transcript-oriented fixture where possible.
3. Require explicit opt-in for audio fixture export.
4. Generate pytest skeleton.
5. Suggest committable replay boundaries if the requested range is unsafe.

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

```python
from easycat.budgets import CostBudget, LatencyBudget
```

Compatibility:

```python
from easycat.validation.latency import LatencyBudget  # keep working
```

### Models

```python
@dataclass(frozen=True)
class CostBudget:
    max_session_usd: float
    warn_at: float = 0.8
    action: Literal["warn", "stop"] = "stop"
```

Keep `max_session_cost_usd` as a config alias into `CostBudget`.

### Runtime Metrics To Add

Add budget-compatible metric records for:

- `llm_ttft_ms`
- `tts_ttfb_ms`
- `stt_final_latency_ms`
- `vad_endpointing_ms`
- `first_audio_ms`
- `barge_in_ack_ms`

Feed them through `LatencyBudgetMonitor` so runtime, debugger, validation, and
evals share the same budget vocabulary.

### Shared Report

Add:

```python
build_budget_report(records, budgets) -> BudgetReport
```

Use it from:

- debugger `/api/budgets`,
- `easycat eval run`,
- `easycat latency`,
- validation reports,
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
- `docs/observability.md`
- `docs/testing-and-evals.md`
- `docs/latency.md`
- `docs/validation.md`
- `docs/reference/easyconfig.md`
- teaching chapters for journal/evals/latency

## Acceptance Criteria

- `EASYCAT_DEV=1 easycat serve` opens one loopback debugger UI and registers
  live sessions.
- `debug="full"` without dev/autolaunch remains non-launching.
- Debugger UI can switch among live sessions.
- Debugger exposes budget status and promotion endpoint.
- `easycat.evals` supports text scenario execution without live audio or API-key
  requirements for basic fake agents.
- `easycat eval promote` generates a usable pytest skeleton.
- Promotion warns about PII and does not include audio by default.
- Replay-as-test denies tool execution by default.
- `easycat.budgets.LatencyBudget` and legacy `easycat.validation.latency.LatencyBudget`
  imports both work.
- Runtime emits first-token/first-audio budget records where available.
- Budget reports are shared by debugger, eval, CLI, and validation surfaces.

## Suggested PR Slice

1. Add `easycat.budgets` aliases and shared report model.
2. Add `easycat.evals` package re-exporting current debug testing helpers.
3. Add scenario dataclasses and text runner.
4. Add promotion library and CLI.
5. Add runtime budget metric coverage.
6. Add debugger dev registry and API endpoints.
7. Add UI budget overlays and promote-to-test button.
