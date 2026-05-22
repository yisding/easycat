# EasyCat Validation Reference

Status: design reference.

This is the supporting reference for EasyCat validation planning. It records
the current repo inventory, target validation vocabulary, artifact shapes,
marker/CI design, provider-contract guidance, observability notes, and
external research links.

Implementation order lives in [tasks.md](tasks.md). The current-state entry
point lives in [README.md](README.md).

## Goal

Make validation cheap enough for daily use, representative enough for live
provider confidence, and structured enough to catch latency and integration
regressions before users do.

The validation surface should answer five questions:

1. Does the library still work locally?
2. Do integration boundaries still compose?
3. Do live providers still behave the way EasyCat adapters expect?
4. Did latency regress, and in which stage?
5. When something fails, is there an artifact that explains the failure
   without rerunning it live?

## Current Repo Inventory

Snapshot: V0 implementation pass on 2026-05-22.

Implemented strengths:

- `pyproject.toml` uses a `src/easycat` layout and registers base,
  validation, provider, surface, optional-extra, flaky, and release pytest
  markers with `strict_markers = true`.
- `.github/workflows/ci.yml` has lint, local tests, socket integration tests,
  and manual live-provider tests.
- `src/easycat/cli/_app.py` registers `init`, `doctor`, `explain`, `bundles`,
  and `inspect`.
- `scripts/validate.py quick` and `scripts/validate.py socket` are the V0
  script-first validation entry points.
- `src/easycat/validation/report.py` defines the V0 validation JSON envelope,
  provider credential states, artifact references, and report-boundary
  redaction.
- Validation artifacts are written under isolated
  `.easycat/validation/runs/<run_id>/` directories, with
  `.easycat/validation/latest.json` updated after a complete report exists.
- `tests/cli/TEST_PLANS.md` documents the current CLI test plan.
- `tests/conftest.py` skips `integration_socket` tests when localhost socket
  bind/connect is unavailable, enforces provider/surface marker pairing for
  provider-scoped validation tests, and enforces flaky quarantine metadata.
- `tests/integration/test_provider_contract_matrix.py` already validates
  STT/TTS registry dispatch, EventBus injection, and session wiring with fake
  providers.
- `tests/e2e/test_plan_7_latency_benchmark.py` is marked `latency` and
  measures user-stops-speaking to first-audio latency and stage breakdowns
  for the live full stack.
- `tests/debug/test_replay_and_bundle.py` verifies replay and debug bundle
  export/load contracts.
- `perf/bench_journal.py` benchmarks journal append latency and sustained
  write rate.
- `easycat doctor` checks local environment, provider keys, reachability, and
  microphone availability.
- `tests/integration/harness.py` provides fake transports, scripted VAD/STT,
  recording TTS, event collection, and lifecycle helpers.

Current gaps:

- There is no `easycat validate` command.
- CI does not upload EasyCat validation JSON, JUnit XML, latency histories,
  debug bundles, or provider compatibility summaries.
- Latency tests print useful results and enforce SLOs, but do not persist a
  stable artifact for comparison.
- Live-provider validation is opt-in and only partially decomposed by provider,
  surface, scenario, cost, and flake risk.
- HTTP record/replay and WebSocket protocol cassette workflows are not
  standardized.
- Browser-side WebRTC network stats are not first-class validation artifacts.

## Principles

1. One obvious path. A contributor should not need to memorize marker
   expressions.
2. Cheap checks first. Fast deterministic checks should run more often than
   live-provider checks.
3. Contracts before live calls. Most provider drift should be caught by local
   protocol contracts.
4. Latency needs distributions. Persist raw samples, eligible summaries, and
   per-stage breakdowns. Do not claim high percentiles when sample counts are
   too low.
5. Failures leave artifacts. Validation failures should point to JSON, JUnit,
   logs, and debug bundles.
6. Secrets never enter artifacts. Reports, cassettes, debug bundles, and
   telemetry attributes need explicit redaction rules.
7. Flaky tests are debt. Quarantine requires owner, issue, and review date.
8. Library telemetry is opt-in. EasyCat may expose OTel API spans/metrics, but
   SDK/exporter setup belongs to an optional extra or host application.
9. Spot checks and release gates are different. Smoke checks may be cheap and
   noisy; release gates should be comprehensive and artifact-rich.
10. Validation should match user workflows: voice turns, interruption,
    provider swaps, transport modes, debug/replay, and deployment boundaries.

## Planned Command Surface

These commands are planned. They do not exist until the V1 tasks land.

```bash
easycat validate quick
easycat validate socket
easycat validate contracts
easycat validate live
easycat validate live --provider openai
easycat validate live --provider deepgram --provider elevenlabs
easycat validate latency --smoke
easycat validate latency --sweep
easycat validate stress
easycat validate release
easycat validate report .easycat/validation/latest.json
```

V0 can start with `uv run python scripts/validate.py quick` and
`uv run python scripts/validate.py socket` before the public CLI exists.

Shared planned options:

```bash
--json
--report PATH
--junit PATH
--artifacts-dir PATH
--provider NAME
--surface NAME
--python VERSION
--extra NAME
--timeout SECONDS
--fail-fast / --no-fail-fast
--strict / --no-strict
```

`--json` must keep the existing EasyCat CLI meaning: emit the standard
machine-readable stdout envelope. Persisted validation reports should use
`--report PATH` or the default artifact path.

## Validation Tiers

### Quick

Planned command:

```bash
easycat validate quick
```

Script-first selector for PR-local quick validation:

```bash
uv run pytest -q --junitxml=<run-dir>/junit.xml \
  -m "not integration_socket and not integration_live and not slow and not flaky"
```

Current nearest selector:

```bash
uv run pytest -q -m "not integration_socket and not integration_live"
```

Expected coverage: unit tests, local integration tests, fake-provider agent
bridge tests, provider config/factory dispatch without live calls, debug
bundle export/load/replay invariants, session lifecycle, cancellation, CLI
tests, and examples that do not bind sockets. If this becomes too slow for
daily use after measurement, add a smaller `unit` command instead of removing
deterministic local coverage from PR CI.

### Socket

Planned command:

```bash
easycat validate socket
```

Script-first selector:

```bash
uv run pytest -q --junitxml=<run-dir>/junit.xml \
  -m "integration_socket and not integration_live and not flaky"
```

Expected base coverage: localhost WebSocket sessions, transports covered by
the installed dev dependencies, reconnect behavior, degraded events, playback
marks, and multi-session contamination checks. WebRTC, WebTransport, Twilio,
and other optional transport-extra checks should be separate lanes unless the
CI job installs those extras.

### Contracts

Planned command:

```bash
easycat validate contracts
```

Contract tests should validate EasyCat's consumer-side assumptions:
request construction, response/event parsing, normalized errors, lifecycle
semantics, capability declarations, and replay fidelity. They should not
assert provider model quality, exact transcript text, or exact generated audio
unless the adapter contract requires it.

Use a strict request, loose response rule:

- Provider-facing requests assert exact endpoint, method, required headers,
  model/API-version fields, audio format, and required body fields.
- Provider responses and streaming events use type, shape, and semantic
  matching. Exact-match only fields EasyCat branches on.
- Unknown additive fields are warnings unless they break parsing or conflict
  with documented behavior.

Keep `tests/integration/test_provider_contract_matrix.py` focused on registry
and session wiring. Put protocol cassettes under a future `tests/contracts/`
suite.

Provider contract coverage should be driven from a provider-surface matrix,
not provider name alone. Each row should include:

| Field | Meaning |
|---|---|
| provider | OpenAI, Deepgram, ElevenLabs, Cartesia, etc. |
| surface | STT, TTS, VAD, transport, or agent bridge |
| adapter | EasyCat adapter class/module |
| protocol | HTTP, WebSocket, SSE, local fake, or framework callback |
| mode | batch, realtime, streaming, Flux, etc. |
| model/API version | default model, API version, or version header EasyCat depends on |
| extra | optional dependency extra needed to import/run the surface |
| credential env | env var name, presence only |
| contract path | offline contract test or explicit exclusion |
| cassette status | required, deferred with reason, or not applicable |
| live status | canary required, optional, or not applicable |

Agent bridge contracts are part of `contract`, but need their own grammar.
They should cover OpenAI Agents, PydanticAI, GenericWorkflow, Remote Responses
API, LangChain, LangGraph, and Llama Agents for text deltas, done events,
tool start/result, handoff triples, framework snapshots, interruption modes,
recorder writes, normalized errors, and optional-extra skips.

### Live

Planned command:

```bash
easycat validate live --provider openai
```

Live canaries should prove that configured providers still satisfy the local
contracts. Secrets must be passed through environment variables only.

Credential states:

- `not_requested`: provider or surface was not selected for this run.
- `skipped_missing_secret`: non-strict exploratory/manual run selected a
  provider but credentials were absent.
- `failed_missing_required_secret`: strict, release, or explicitly required
  run selected a provider but credentials were absent.
- `passed`: live provider check completed successfully.
- `failed`: live provider check ran and failed.

Manual and nightly workflows may allow expected skips. Release validation and
strict manual runs must fail required provider or latency checks that skip.

Failure classes:

- `easycat_regression`
- `provider_drift`
- `provider_outage`
- `auth_or_quota`
- `network`
- `environment`

Provider env vars:

| Provider | Env var |
|---|---|
| OpenAI | `OPENAI_API_KEY` |
| Deepgram | `DEEPGRAM_API_KEY` |
| ElevenLabs | `ELEVENLABS_API_KEY` |
| Cartesia | `CARTESIA_API_KEY` |

Capability reports should be surface-aware. Common fields include provider,
surface, adapter, adapter version, protocol/mode, API version, required extra,
credential env var name, auth presence, contract status, schema status,
latency status, and failure class. Surface-specific fields should include
input/output audio formats, streaming/finalization behavior, alignment or
marker support, SSML support, EventBus requirements, and required API version
headers where applicable.

### Latency

Planned commands:

```bash
easycat validate latency --smoke
easycat validate latency --sweep
```

Current direct entry point:

```bash
OPENAI_API_KEY=... uv run pytest tests/e2e/test_plan_7_latency_benchmark.py -s -v
```

Canonical user-facing SLI: client speech end to first playable client audio.
Stage timings diagnose which subsystem moved.

Canonical sample fields:

- `sample_id`
- `condition_id`
- `warmup`
- `timestamp_source`
- provider/model/transport/debug metadata
- feature metadata
- `detection_ms`
- `stt_ms`
- `stt_finalize_close_ms`
- `agent_request_start_ms`
- `llm_ttft_ms`
- `tts_ttfb_ms`
- `transport_ms`
- `total_ms`
- `missing_stage_reason`
- `failure_class`

Sample-count rules:

- Smoke: raw samples only; no percentile gates.
- Short sweep: p50 is eligible with at least 3 post-warmup samples; p90 is
  informational until at least 10.
- Nightly/release sweep: p50 and p90 gates require at least 10 post-warmup
  samples per condition.
- Soak/stress reporting: p95/p99 require larger samples, preferably 50 to 100
  successful turns per condition.

### Stress

Planned command:

```bash
easycat validate stress
```

Stress classes:

| Class | Scenario | Signal |
|---|---|---|
| sustained single session | 10-50 consecutive turns | leaks, session drift, stream cleanup |
| concurrent sessions | N local scripted sessions | cross-session contamination, queue pressure |
| transport pressure | high frame rate or burst sends | dropped frames, drain behavior |
| interruption storm | repeated barge-ins | cancellation correctness |
| reconnect loop | repeated disconnect/reconnect | transport lifecycle |
| journal pressure | high event volume | write latency, degraded flag |
| provider live soak | low-frequency live turns | provider drift, rate limits |

Report saturation signals: event-loop lag, queue depth, dropped frames,
journal degraded flag, memory growth, CPU, provider timeouts, and rate-limit
counts.

### Release

Planned command:

```bash
easycat validate release
```

Release validation should build distributions, install the wheel into a clean
environment, run import smoke and `easycat doctor --json`, run quick tests
against the installed wheel, run configured live canaries, run latency smoke
or sweep when prerequisites exist, and upload a final validation report. The
installed-wheel check should run outside the source tree, clear `PYTHONPATH`,
and assert `easycat.__file__` resolves under site-packages.

## Artifact Model

Default planned layout:

```text
.easycat/validation/
  latest.json
  runs/
    20260521T120000Z-quick-12345/
      report.json
      junit.xml
      stdout.log
      stderr.log
      debug-bundles/
      latency/
      providers/
  latency/
    smoke-latest.json
    sweep-latest.json
    history/
```

Shared JSON envelope:

```json
{
  "schema_version": 1,
  "redaction_version": 1,
  "kind": "validation_run",
  "run_id": "20260521T120000Z-quick-12345",
  "command": "easycat validate quick",
  "started_at": "2026-05-21T00:00:00Z",
  "finished_at": "2026-05-21T00:01:30Z",
  "duration_s": 90.2,
  "status": "pass",
  "exit_code": 0,
  "tool_exit_codes": {
    "pytest": 0
  },
  "git": {
    "sha": "abc123",
    "branch": "feature/validation",
    "dirty": true
  },
  "environment": {
    "python": "3.12.12",
    "platform": "Linux",
    "ci": false
  },
  "checks": [
    {
      "name": "pytest.quick",
      "status": "pass",
      "duration_s": 72.1,
      "command": "uv run pytest -q -m ...",
      "artifacts": {
        "junit": ".easycat/validation/runs/20260521T120000Z-quick-12345/junit.xml"
      }
    }
  ],
  "extras": [],
  "skips": [
    {
      "name": "webrtc",
      "reason": "aiortc/aiohttp not installed",
      "expected": true
    }
  ],
  "failures": [],
  "latency": null,
  "providers": []
}
```

Rules:

- The envelope should be stable enough for CI dashboards and release notes.
- A failed pytest run should still write `<run-dir>/report.json` and update
  `latest.json` only after a complete report exists.
- `latest.json` is a latest-run convenience copy or pointer; the run
  directory is the durable artifact.
- Reports must not include environment variable values.
- Artifacts should be safe to upload from CI after redaction.
- The public CLI exit code and underlying tool exit code are separate fields.
  Do not expose raw pytest code `5` through `easycat validate` as EasyCat's
  public code `5`, because that code already means bundle missing/corrupt.
- `easycat validate report` should handle missing files, invalid JSON,
  unsupported schema versions, unknown `kind`, failed runs, expected skips,
  and missing artifact paths with explicit messages.

## Marker Plan

Keep existing markers:

- `integration_local`
- `integration_socket`
- `integration_live`
- `slow`

Add planned markers:

- `contract`: provider, protocol, or bridge contract tests
- `latency`: latency measurement or latency SLO tests
- `stress`: load, soak, or high-volume validation tests
- `release`: release-gate validation tests
- `flaky`: quarantined intermittent test with required metadata
- `provider_openai`
- `provider_deepgram`
- `provider_elevenlabs`
- `provider_cartesia`
- `surface_stt`
- `surface_tts`
- `surface_agent`
- `surface_transport`
- `surface_vad`
- `agent_bridge`
- `requires_extra(name)`: optional dependency extra needed for a test
- `provider(name)`: generic provider metadata for future/custom providers

Cost markers and provider markers should stay orthogonal so selectors can
combine them, for example `contract and provider_openai and surface_stt`.
Marker lint should require provider/surface metadata for provider-scoped
`integration_live`, `contract`, and `latency` tests.

Planned pytest config shape:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
strict_markers = true
strict_config = true
markers = [
    "integration_local: in-process end-to-end tests with fake providers",
    "integration_socket: tests requiring localhost socket bind/connect permissions",
    "integration_live: tests requiring live API keys and optional provider extras",
    "slow: long-running end-to-end tests; opt in with '-m slow'",
    "contract: provider, protocol, or bridge contract tests",
    "latency: latency measurement or latency SLO tests",
    "stress: load, soak, or high-volume validation tests",
    "release: release-gate validation tests",
    "flaky: quarantined intermittent test; requires linked issue, owner, and review date",
    "provider_openai: OpenAI provider coverage",
    "provider_deepgram: Deepgram provider coverage",
    "provider_elevenlabs: ElevenLabs provider coverage",
    "provider_cartesia: Cartesia provider coverage",
    "surface_stt: STT surface coverage",
    "surface_tts: TTS surface coverage",
    "surface_agent: agent bridge surface coverage",
    "surface_transport: transport surface coverage",
    "surface_vad: VAD surface coverage",
    "agent_bridge: agent bridge contract or live coverage",
    "requires_extra(name): test requires an optional dependency extra",
    "provider(name): generic provider metadata for custom provider filtering",
]
```

## CI Shape

Current CI:

- `lint`
- `test` on Python 3.12 and 3.14 with
  `not integration_socket and not integration_live`
- `integration-socket` on Python 3.12 and 3.14 with `integration_socket`
- manual `integration-live` on Python 3.12 with `integration_live`

Planned PR-required CI:

- `lint`
- `validate-quick` on Python 3.11, 3.12, and 3.14 while
  `requires-python >=3.11`
- `validate-socket` on Python 3.12
- package build smoke on Python 3.12

Validation CI should remove pytest `-x`, set strategy `fail-fast: false`, add
job-level `timeout-minutes`, and upload reports, JUnit, stdout/stderr logs,
and selected debug bundles with `if: always()`.

Planned manual/nightly/release:

- manual latency smoke
- manual provider-specific live smoke
- nightly full local suite
- nightly socket suite
- nightly live canaries for configured secrets
- nightly latency sweep
- nightly flaky quarantine lane with rerun counts
- release validation against built wheel

Every CI validation job should upload artifacts with `if: always()`, bounded
retention, and names that include job name, Python version, and run attempt.
CI artifact paths should include the validation run id. JUnit should use
suite names or `--junit-prefix` so merged reports keep job context.

## Provider Cassettes And Redaction

HTTP providers can use VCR.py or `pytest-recording` where practical. SSE
providers, such as a streaming Remote Responses bridge path, need explicit
event-stream cassette coverage if EasyCat branches on event names or shapes.

Rules:

- CI/offline record mode defaults to `none`.
- Network calls are blocked for offline contract runs.
- Cassette refresh requires explicit local opt-in.
- Filter `Authorization`, `xi-api-key`, `OpenAI-Organization`,
  `OpenAI-Project`, API keys, tokens, access tokens, signed URLs, request IDs
  when sensitive, and credential-bearing query/body fields.
- Normalize timestamps and volatile IDs when they are not contract-relevant.
- Keep cassettes small and scenario-focused.
- Define cassette scope per provider surface as required, deferred with
  reason, or not applicable before declaring contract coverage complete.

WebSocket providers need a small EasyCat-owned cassette format:

```json
{
  "schema_version": 1,
  "provider": "cartesia",
  "surface": "tts",
  "provider_api_version": "2026-03-01",
  "redaction_version": 1,
  "capabilities_snapshot_ref": "cartesia-tts-2026-03-01",
  "frames": [
    {
      "seq": 1,
      "direction": "send",
      "opcode": "text",
      "kind": "config",
      "payload_assertion": {"schema_ref": "cartesia_tts_config_v1"},
      "redacted_fields": ["Authorization"]
    }
  ]
}
```

Recommended assertions:

- frame order and required lifecycle transitions
- normalized event kind
- required parse fields
- normalized error category
- audio metadata: codec, sample rate, channel count, minimum byte count
- timing class, not exact latency

Schema drift result values:

- `unchanged`
- `additive_warning`
- `breaking_failure`
- `unknown`

Schema fingerprints should be backed by provider-surface schema registry
entries. Each entry should separate required outbound fields, required inbound
event names, optional observed fields, provider-specific enum values EasyCat
branches on, and normalized error fields. Additive unknown fields should warn;
missing required fields, changed content type, changed contractual enum value,
or incompatible error shape should fail.

## Observability Model

Validation artifacts and runtime telemetry should stay separate. Validation
artifacts store raw per-turn samples plus computed summaries. Runtime
telemetry should use histograms, counters, gauges, and traces.

Suggested metrics:

- `easycat.turn.latency`: histogram, seconds
- `easycat.stage.latency`: histogram, seconds, with low-cardinality `stage`
- `easycat.journal.append.latency`: histogram, seconds
- `easycat.sessions.active`: observable gauge
- `easycat.turns.total`: counter
- `easycat.audio.bytes.total`: counter
- `easycat.audio.frames.total`: counter
- `easycat.provider.errors.total`: counter
- `easycat.session.errors.total`: counter
- `easycat.transport.disconnects.total`: counter
- `easycat.validation.failures.total`: counter
- `easycat.queue.depth`: observable gauge
- `easycat.queue.dropped.total`: counter
- `easycat.event_loop.lag`: histogram or observable gauge
- `easycat.journal.degraded`: observable gauge

Allowed low-cardinality attributes:

- `easycat.provider`
- `easycat.provider_family`
- `easycat.surface`
- `easycat.transport`
- `easycat.debug_mode`
- `easycat.stage`
- `easycat.condition_id`
- `easycat.feature_set`
- `easycat.result`
- `easycat.error_type`

Forbidden metric attributes:

- session IDs
- trace IDs and span IDs
- user IDs
- phone numbers
- IP addresses
- provider request IDs
- raw prompts, transcripts, generated text, tool arguments, and model outputs
- file paths or URLs containing credentials or tenant identifiers

Recommended span tree:

```text
easycat.session
  easycat.transport.receive
  easycat.vad.detect
  easycat.stt.stream
  easycat.turn.commit
  easycat.agent.invoke
    easycat.agent.tool
  easycat.tts.synthesize
  easycat.transport.send
  easycat.journal.append
```

EasyCat core should depend on `opentelemetry-api` at most. SDKs/exporters and
example collectors belong in an optional extra or application configuration.

## WebRTC Stats

Browser-facing validation should collect `RTCPeerConnection.getStats()` when
available:

- selected ICE candidate pair
- local/remote candidate types
- round-trip time
- jitter
- packet loss
- bytes sent/received
- concealed samples when available
- available outgoing bitrate when available

Capture snapshots before speech, at client speech end, at first received
audio, and on teardown. Persist WebRTC stats alongside the same latency
`sample_id`.

## Code Paths To Validate

Session lifecycle:

- start -> stop -> postmortem journal read
- start -> shutdown -> postmortem bundle export
- failure during provider start
- cancellation during STT, agent streaming, and TTS
- transport disconnect mid-turn
- stop while provider stream is active

Turn-taking:

- VAD start/stop
- silence timeout
- smart turn early termination
- partial STT then final
- empty transcript
- interruption before first TTS byte
- interruption during TTS playback
- interruption during tool call

Providers:

- STT start/send/end/events/close
- TTS synthesize/stop/close
- EventBus injection
- timeout/auth/rate-limit/malformed-frame mapping

Agent bridges:

- `AgentTurnInput.from_text`
- text delta and done events
- tool call start/result
- handoff triple
- framework snapshot safety
- interruption modes
- recorder writes

Debug/replay:

- light journal
- full SQLite journal
- artifact capture
- bundle export/load
- ARTIFACT/SIMULATED/LIVE fidelity behavior
- missing artifact failure
- provider version mismatch behavior

Transports:

- local transport
- WebSocket
- WebRTC
- WebTransport
- Twilio media stream
- reconnect
- clear audio
- playback marks
- version info

## Research Links

Pytest and test organization:

- Registered custom markers and strict marker handling:
  <https://docs.pytest.org/en/stable/how-to/mark.html>
- Flaky test guidance:
  <https://docs.pytest.org/en/stable/explanation/flaky.html>
- JUnit XML output:
  <https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files>
- Good integration practices:
  <https://docs.pytest.org/en/stable/explanation/goodpractices.html>

GitHub Actions and release:

- Python matrix testing:
  <https://docs.github.com/en/actions/tutorials/build-and-test-code/python>
- Artifact storage:
  <https://docs.github.com/en/actions/tutorials/store-and-share-data>
- Scheduled workflow behavior:
  <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule>
- Secrets handling:
  <https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets>
- PyPA publishing with GitHub Actions:
  <https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/>

Latency, metrics, and observability:

- Google SRE monitoring and four golden signals:
  <https://sre.google/sre-book/monitoring-distributed-systems/>
- Google SRE SLO guidance:
  <https://sre.google/sre-book/service-level-objectives/>
- Prometheus histograms:
  <https://prometheus.io/docs/practices/histograms/>
- Prometheus instrumentation and naming:
  <https://prometheus.io/docs/practices/instrumentation/>
  <https://prometheus.io/docs/practices/naming/>
- OpenTelemetry Python instrumentation:
  <https://opentelemetry.io/docs/languages/python/instrumentation/>
- OpenTelemetry library instrumentation:
  <https://opentelemetry.io/docs/concepts/instrumentation/libraries/>
- OpenTelemetry metrics API:
  <https://opentelemetry.io/docs/specs/otel/metrics/api/>
- OpenTelemetry GenAI semantic conventions:
  <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>

Contracts, cassettes, and load:

- VCR.py:
  <https://vcrpy.readthedocs.io/en/v4.4.0/index.html>
- Pact:
  <https://docs.pact.io/>
- Pact consumer, scope, and matching guidance:
  <https://docs.pact.io/consumer>
  <https://docs.pact.io/getting_started/testing-scope>
  <https://docs.pact.io/getting_started/matching>
- `pytest-recording`:
  <https://pypi.org/project/pytest-recording/>
- Locust quickstart:
  <https://docs.locust.io/en/latest/quickstart.html>
- `pytest-benchmark` comparisons:
  <https://pytest-benchmark.readthedocs.io/en/latest/comparing.html>

WebRTC stats:

- `RTCPeerConnection.getStats()`:
  <https://developer.mozilla.org/en-US/docs/Web/API/RTCPeerConnection/getStats>
- Candidate pair stats:
  <https://developer.mozilla.org/en-US/docs/Web/API/RTCIceCandidatePairStats>
- W3C WebRTC stats:
  <https://www.w3.org/TR/webrtc-stats/>

## Open Decisions

1. Should `pytest-benchmark` become a dev dependency, or should the current
   custom benchmark be wrapped first?
2. Should OpenTelemetry API be a core dependency or an optional extra?
3. Should providers expose a formal capability/version protocol, or should
   validation derive capability reports by duck-typing configs and adapters?
4. Should socket tests be required on every supported Python version, or only
   Python 3.12 with the full matrix nightly?
5. What compatibility promise should the public `easycat validate` JSON make
   after V1? The current recommendation is versioned additive changes only.
6. Should the public command surface add `easycat validate unit`, or is
   `quick` plus measured runtime enough for daily use?

## Done Definition

Validation is "super easy" when all of the following are true:

- A contributor can run one documented quick command without reading marker
  docs.
- A maintainer can run one provider-specific live command before touching
  provider code.
- A release can attach one JSON report that says what ran, what skipped, what
  failed, and where artifacts live.
- A latency regression report shows the stage that regressed.
- Provider drift is usually caught by contract tests before a live canary
  fails.
- Live canary failures leave enough redacted artifacts to debug without
  immediately rerunning.
- The commands are cheap enough that people actually use them.
