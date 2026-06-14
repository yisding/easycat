# Neo Roadmap

Status: active sequencing proposal.

This roadmap converts the phase plans into reviewable PR slices. The ordering is
optimized for early user value, low abstraction risk, and testability.

## Dependency Map

```text
VoiceApp
  ├─ easycat serve migration
  ├─ Twilio server extraction
  └─ feeds VoiceServer.from_app

VoiceServer
  ├─ shared auth
  ├─ health/readiness
  ├─ graceful shutdown
  ├─ manifest loader
  └─ provider planner

Budgets + Evals
  ├─ scenario runner
  ├─ replay promotion
  └─ debugger overlays/dev mode
```

## Milestone 0 — Planning Packet

Deliverables:

- `plan/neo/*` planning assets.
- Agreement on architecture boundaries.
- Agreement on Phase 1 first PR slice.

Exit criteria:

- Maintainers can identify which files to add/change.
- Acceptance criteria are explicit.
- Open questions are recorded.

## Milestone 1 — Minimal VoiceApp

Scope:

- Add `src/easycat/voice_app.py`.
- Support `local`, `browser`, and `websocket` modes.
- Support high-level `agent=...`, static `EasyConfig`, and `config_factory`.
- Add mode alias normalization.
- Add unit tests for config construction and delegation.

Out of scope:

- Twilio mode.
- Manifest loading.
- Dev debugger autolaunch.

Suggested PR title:

```text
app: add VoiceApp run modes
```

## Milestone 2 — Serve Command Uses VoiceApp

Scope:

- Migrate `easycat serve` to construct a `VoiceApp`.
- Add `--mode` with default `browser`.
- Preserve host/port/token behavior.
- Update CLI tests.
- Update README/browser-playground docs.
- Export `VoiceApp` and update public API docs/snapshots.

Suggested PR title:

```text
cli: route serve through VoiceApp
```

## Milestone 3 — Twilio Mode

Scope:

- Extract reusable Twilio server helper from the example shape.
- Add `VoiceApp.run("twilio")`.
- Add TwiML/token/media lifecycle tests with fakes.
- Simplify or add a `VoiceApp` Twilio example.

Suggested PR title:

```text
telephony: add VoiceApp Twilio server mode
```

## Milestone 4 — VoiceServer Skeleton

Scope:

- Add `easycat.server` package.
- Add `VoiceServerConfig`, `VoiceServer`, `VoiceServerHealth`.
- Use `aiohttp`.
- Add `/health/live`, `/health/ready`, `/health`.
- Support a WebSocket route first.
- Use `SessionManager` for lifecycle.
- Add lifecycle/health/auth-free tests.

Suggested PR title:

```text
server: add VoiceServer skeleton
```

## Milestone 5 — Auth, Capacity, Shutdown

Scope:

- Add shared auth policies.
- Add capacity rejection.
- Add draining state.
- Add graceful shutdown and forced escalation.
- Add request/session metrics skeleton.
- Add tests for auth/capacity/shutdown.

Suggested PR title:

```text
server: add auth and graceful draining
```

## Milestone 6 — Manifest and Planner

Scope:

- Add `easycat.project.ProjectManifest`.
- Add manifest discovery/loading/validation.
- Convert profiles to `EasyConfig`.
- Add transport registry metadata.
- Add `ProviderPlan` and missing env/extra detection.
- Add `easycat plan --json`.
- Wire readiness to blocking plan errors.

Suggested PR title:

```text
project: add manifest and provider planning
```

## Milestone 7 — WebRTC in VoiceServer

Scope:

- Mount WebRTC `/offer`, `/config`, `/stats`, and static client through
  `VoiceServer` or shared route internals.
- Preserve existing WebRTC helper API.
- Add integration tests with aiohttp test utilities.

Suggested PR title:

```text
server: mount WebRTC voice sessions
```

## Milestone 8 — Budgets Public API

Scope:

- Add `easycat.budgets` package.
- Re-export/alias `LatencyBudget`.
- Add `CostBudget` model.
- Add shared budget report builder over journal records.
- Preserve `max_session_cost_usd` alias.
- Add tests for serialization, aliases, and report generation.

Suggested PR title:

```text
budgets: add shared budget API
```

## Milestone 9 — Evals Public API

Scope:

- Add `easycat.evals` package.
- Re-export existing debug testing helpers.
- Add `EvalScenario`, `EvalTurn`, `EvalRunner`.
- Implement text-first scenario runner.
- Add CLI `easycat eval run`.
- Add docs and scaffold test pattern.

Suggested PR title:

```text
evals: add scenario runner
```

## Milestone 10 — Replay Promotion

Scope:

- Add promotion library.
- Add `easycat eval promote`.
- Generate pytest skeletons.
- Warn about PII.
- Default to no audio and tool replay denied.
- Add debugger API endpoint for promotion.

Suggested PR title:

```text
evals: promote bundle turns to tests
```

## Milestone 11 — Runtime Budget Coverage

Scope:

- Emit first-token and first-audio latency metrics where available.
- Feed runtime milestones into budget monitor.
- Add budget violations to journal/issue rollups.
- Add tests for metric records and budget exceeded records.

Suggested PR title:

```text
runtime: record first audio budget milestones
```

## Milestone 12 — Dev Debugger Mode

Scope:

- Add dev debugger policy.
- Add session registry.
- Add single debugger per process.
- Add live session selector.
- Add budget overlays.
- Add promote-to-test button.

Suggested PR title:

```text
debugger: add dev session dashboard
```

## Validation Strategy

Each milestone should run targeted tests plus the relevant guard lane:

| Milestone | Targeted tests | Guard lane |
|---|---|---|
| 1–2 | `tests/test_voice_app.py`, `tests/cli/test_serve.py`, public API tests | `just guard-docs` |
| 3 | `tests/telephony/test_voice_app_twilio.py` | `just guard-examples` + targeted telephony |
| 4–7 | `tests/server`, transport tests | `just guard-ops` |
| 8–11 | `tests/budgets`, `tests/evals`, latency/validation tests | `just guard-validation` |
| 12 | `tests/debugger`, `tests/test_dx_helpers.py` | `just guard-ops` |

## Cut Lines

If scope gets too large, cut in this order:

1. Defer Twilio mode from Phase 1 until after WebRTC/WebSocket are stable.
2. Defer WebSocket static browser client.
3. Defer Prometheus text output; keep JSON/OTel-compatible metrics first.
4. Defer audio simulation; ship text scenarios first.
5. Defer full debugger UI polish; ship API + minimal UI affordance first.

## Release Narrative

When Neo is ready, the release story should be:

- `VoiceApp` is the new app-first way to build EasyCat apps.
- `easycat serve` is browser-first and production-capable.
- `easycat.toml` makes projects deployable and inspectable.
- Debugger timelines and evals turn voice bugs into tests.
