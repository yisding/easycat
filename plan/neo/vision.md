# Neo Vision

Status: active proposal.

## Thesis

EasyCat already has many hard pieces of a voice-agent platform: a complete
voice pipeline, provider registries, browser/WebRTC and WebSocket transports,
Twilio primitives, debug journals, replay, bundles, validation, latency/cost
hooks, and a debugger UI. The next major version should make those pieces feel
like one coherent product.

Neo’s thesis:

> EasyCat should be the fastest path from an agent object to a production-grade,
> inspectable, deployable voice application.

That means the default developer experience should not expose a pile of config
and transport details first. It should start with an app, run in a browser, show
what is happening, and provide a clean path to production and CI.

## North-Star API

The first experience should be this small:

```python
from agents import Agent
from easycat import VoiceApp

app = VoiceApp(
    agent=Agent(
        name="assistant",
        instructions="You are a helpful voice assistant.",
    )
)

app.run("browser")
```

Changing surfaces should be a mode switch, not a rewrite:

```python
app.run("local")
app.run("websocket")
app.run("twilio")
```

Production should be one concept above the app:

```python
from easycat.server import VoiceServer

server = VoiceServer.from_manifest("easycat.toml")
server.run()
```

CI should test voice-agent behavior without requiring a human to make calls:

```python
from easycat.evals import EvalRunner, EvalScenario, EvalTurn

scenario = EvalScenario(
    name="refund_flow",
    turns=[EvalTurn(user="I need a refund", expect_response_regex="refund|order")],
)

result = await EvalRunner(agent=agent).run(scenario)
result.assert_passed()
```

The north-star `VoiceApp(agent=Agent(...)).run("browser")` is feasible exactly
as written today (`agent=` accepts a raw OpenAI Agents SDK `Agent`; `import
easycat` is lazy). The server and eval symbols above, however, are **net-new
and not yet in the tree**: `VoiceServer` is imported from `easycat.server`, and
`EvalRunner`/`EvalScenario`/`EvalTurn` from `easycat.evals`. These are
**submodule exports** (`easycat.server`, `easycat.evals`, alongside
`easycat.budgets`) and do **not** count against the top-level `easycat.__all__`
cap — only the new top-level `VoiceApp` does, which forces a deliberate cap bump
(94 → 95; see R13 and Phase 1).

## Product Pillars

### 1. App-first developer experience

Developers should think in terms of a voice app and deployment modes. The
existing session/config/provider details remain available, but they should be
second-page concepts.

### 2. Browser-first iteration

The browser should become the default dev surface because it is easy to share,
works well with WebRTC, and can show the debugger/timeline next to the live
conversation.

### 3. Production is a first-class runtime

A production voice app needs health/readiness, auth, metrics, capacity limits,
graceful shutdown, provider planning, and deployment-friendly manifests. These
belong in a server/process layer, not in every user app.

### 4. Debuggability is the moat

Voice bugs are temporal. EasyCat should make every run inspectable through a
journal-backed timeline, replay, budget overlays, and promotion to regression
tests.

### 5. Tests should look like conversations

The framework should help users test scenarios, not just functions. A
production bundle should become a pytest regression with one command.

## Non-Goals

- Do not rewrite the audio pipeline as part of Phase 1. Reuse `Session`,
  `EasyConfig`, providers, and transports.
- Do not make `Session` multi-client. Keep one `Session` per call/client.
- Do not make durable debugging imply browser autolaunch in production.
- Do not execute external tools during replay by default.
- Do not hide PII risks in journals, bundles, or promoted fixtures. This is
  **net-new work**, not an already-satisfied invariant: the existing
  `journal promote` path (`cli/debug/bundles.py:1819-1962` →
  `slice_bundle_by_turn` → `debug/export.py:154-170`) ships **unredacted
  transcripts plus every raw audio blob by default** and embeds the verbatim
  agent reply into the committed stub. Neo's promotion workstream
  (`eval promote`) MUST redact by default (route records through
  `redact_value`, default to `--no-audio`, assert on a hash/regex over the
  reply rather than embedding raw text) — see R8/Q14 and Phase 3. The non-goal
  is a requirement to harden, not a description of current behavior.
- Do not require live provider API keys for baseline eval/scenario tests.

## Success Criteria

Neo succeeds if a new developer can:

1. Create a voice app with `VoiceApp(agent=...)`.
2. Run it in a browser with one command.
3. See a live timeline/debugger in dev mode.
4. Deploy the same app through a manifest-backed server.
5. Inspect health, readiness, metrics, and provider plan.
6. Promote a bad production turn into a regression test.
7. Run conversation scenarios and budget checks in CI.
