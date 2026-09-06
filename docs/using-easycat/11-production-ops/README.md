# Chapter 11: Ship and Operate

Production is an operating contract, not a hosting location. The release must
be validated as the artifact you deploy, the process must reject unsafe work,
health and metrics must be meaningful, session records must survive the failure
modes you care about, and shutdown must have an enforced deadline.

This final chapter assembles the features from the ladder into that contract.
Its checkpoint needs no providers, sockets, containers, or credentials.

## Prerequisites

- Complete [chapter 7](../07-observability/) for journals and bundles,
  [chapter 9](../09-multi-caller/) for capacity/draining, and
  [chapter 10](../10-telephony/) if your deployment accepts calls.
- Run `uv sync --group dev` from the repository root.
- No API keys or external services are needed for the checkpoint.

## Run the offline operating checkpoint

```bash
uv run python docs/using-easycat/11-production-ops/main.py
```

Expected output:

```text
PASS policy: public bind has auth, capacity, and bounded drain windows
PASS health: draining fails readiness and raw metric paths are rejected
PASS durability: clean SQLite journal reopened as a read-only postmortem
```

To keep the journal for CLI inspection:

```bash
uv run python docs/using-easycat/11-production-ops/main.py --data-dir .easycat/tutorial/ch11
uv run easycat inspect .easycat/tutorial/ch11/journals/chapter-11-ops-checkpoint.sqlite
uv run easycat inspect .easycat/tutorial/ch11/journals/chapter-11-ops-checkpoint.sqlite --json
```

The checkpoint deliberately checks policy, signals, and persistence without
starting a network server. Provider and ingress validation belong to later,
explicit lanes.

## Treat operations as a contract

Write down the contract before choosing a platform:

- which artifact and configuration revision is running;
- which credentials, extras, providers, regions, and transports it expects;
- what makes the process alive, ready, degraded, and overloaded;
- which latency/error/capacity signals page an operator;
- where sensitive journals live, replicate, expire, and get inspected;
- how new work stops, live work drains, and stragglers are forced;
- which validation evidence permits promotion or rollback.

If one of these answers exists only in a person's memory, the deployment is
not yet repeatable.

## Validation is a ladder, not one command

Use the cheapest lane that can catch the failure, then climb only as far as the
change and release risk require:

| Lane | What it proves | External requirements |
|---|---|---|
| `validate quick` | Deterministic local behavior | None |
| `validate socket` | Localhost transport/server integration | Local sockets |
| `validate stress` | Saturation and local reliability signals | More CPU/time |
| `validate contracts` | Offline provider/protocol/bridge contracts | None |
| `validate live` | Credentialed provider canaries | Keys/network/cost |
| `validate latency --smoke` | Low-sample live integration timing | Keys/network/cost |
| `validate latency --sweep --require-samples` | Tail budgets and compatible baselines | Scheduled samples |
| `validate release` | Built package in a clean environment plus release gates | Build tools and configured gates |

The normal pull-request entry point is:

```bash
uv run easycat validate quick
uv run easycat validate quick --json
```

Every run writes an isolated report below `.easycat/validation/runs/`, updates
`.easycat/validation/latest.json` only after completion, and retains captured
stdout/stderr plus JUnit where supported. Inspect the last result with:

```bash
uv run easycat validate report .easycat/validation/latest.json
uv run easycat validate report .easycat/validation/latest.json --json
```

Machine-readable output is the automation contract. Gate on status and named
checks in the JSON report, not colored console wording. Use `--show-output` in
GitHub Actions so failure logs remain visible without downloading artifacts.

Flaky quarantine is explicit debt, not a retry loop. The repository requires
an issue, owner, and review date, and release-scoped flaky tests remain fatal.

## Build once and run the installed artifact

Source-tree success does not prove the wheel contains models, templates,
entry points, optional metadata, or public imports. `validate release` builds
the sdist/wheel, installs the wheel into a clean temporary environment, clears
`PYTHONPATH`, checks package metadata and CLI/API surfaces outside the checkout,
then runs the configured release gates through that installation.

```bash
uv run easycat validate release
uv run easycat validate release --json
```

Promote the exact artifact that passed. Do not rebuild between staging and
production. Attach its digest, source revision, validation report, dependency
lock, and configuration/manifest revision to the release record.

Before live lanes, preflight the selected extras and credentials:

```bash
uv run easycat doctor
uv run easycat doctor --json
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
```

`doctor` checks availability; it does not replace a canary through the actual
provider, region, network, and ingress path.

## Package configuration separately from secrets

Use environment or secret-manager references for provider keys and bearer
tokens. Never use Docker build arguments for secrets: image history and build
caches preserve them. The repository's `.dockerignore` excludes `.env`, keys,
certificates, caches, reports, and local agent state from the build context.

For the shipped WebSocket container path:

```bash
docker compose --env-file docker/.env -f docker/compose.yaml up --build
docker compose -f docker/compose.yaml down
```

The image runs as a non-root user. Compose binds the host port to loopback by
default and the server requires a token before creating a provider-backed
session. A public deployment needs TLS/WSS ingress, bearer authorization, rate
limits, and the per-process session cap preserved at every proxy layer.

WebRTC and telephony add UDP/media and webhook constraints. Confirm the chosen
load balancer supports the actual protocol; a container being reachable over
HTTP does not prove ICE, RTP, QUIC, or Media Streams are routed correctly.

## Liveness and readiness answer different questions

`/health/live` answers: should the process be restarted? Keep it shallow. A
provider outage should normally make the service not ready or degraded, not
cause a liveness restart loop.

`/health/ready` answers: should new sessions be routed here? It fails when the
server is draining, at capacity, the route stack is unavailable, a configured
manifest failed to load, or provider planning has blocking errors. Readiness also
blocks on a selected backend whose SDK is absent even when that backend has no
pip extra — a commercial backend such as Krisp ships no PyPI package, so nothing
would be reported as a missing extra, yet the session raises on the first
connection.

`/health` returns the stable diagnostic payload with process state, active and
maximum sessions, draining, and safe sub-checks. It must not include session
IDs, IP addresses, raw errors, or tokens.

The checkpoint constructs both a serving and draining `VoiceServerHealth` and
proves only the first is ready. During rollout or shutdown, fail readiness
before closing listeners so the ingress converges away from the instance.

### `warmup` moves cost out of the first call

`warmup=True` (the default) runs provider warmup hooks when a session starts,
so model loading, connection setup, and first-call initialisation happen before
audio flows rather than inside the caller's first turn:

```python
EasyConfig(agent=agent, warmup=True)  # the default; set False to opt out
```

It matters here because readiness and warmup answer adjacent questions. Warmup
is per session, not per process: it makes the *first turn* of each session fast,
which is exactly the turn a caller judges you on. Turn it off only when you have
measured that a warmup path costs more at session start than it saves in the
first turn — a batch worker creating many short-lived sessions is the plausible
case.

## Metrics are bounded; journals are forensic

Server metrics include request count/duration, rejected sessions, active
connections, and the draining gauge. Labels are restricted to enumerated route
templates and low-cardinality states such as `serving`, `draining`, `missing`,
and `invalid`.

The checkpoint proves `/health/ready` is accepted as a metric route and
`/health/ready?token=secret` is rejected before becoming a label. Never label a
metric with `session_id`, `turn_id`, phone number, user ID, transcript, raw URL,
model output, or token.

Use the four observability layers deliberately:

- logs for lossy human/process diagnostics;
- `EventBus` for in-process behavior;
- the execution journal for complete, sensitive forensics;
- OpenTelemetry for bounded metrics and traces.

An OTel exporter/SDK is optional; without one, EasyCat emission is a no-op but
name and attribute validation still runs. Configure and test the exporter in
your deployment rather than assuming emitted calls reach a backend.

Start with alerts for sustained error rate, journal degradation, rejected
sessions, readiness loss, active/max saturation, provider failures, and
speech-to-speech/tail-latency regression. Page on user-impacting symptoms and
runbook-owned conditions, not every individual call failure.

## Durability includes crash recovery and retention

The default `"light"` keeps structured records in an in-memory ring with
in-memory artifacts, so per-frame capture never touches the disk on the live
audio loop; opt into `debug="full"` for a crash-survivable SQLite journal with
filesystem-backed artifacts (production capture), while `"off"` disables the
journal. Choose intentionally because journals contain transcripts, tool data,
agent output, and often PII.

The supported backends are:

- `sqlite` — local WAL-mode journal, simplest single-process choice;
- `sqlite+litestream` — SQLite with a Litestream replication sidecar/binary;
- `libsql` — remote-capable libSQL adapter.

Each append is committed through the production path. Clean close writes a
marker, checkpoints the WAL, closes backend resources, and preserves a
read-only postmortem view. A later startup sweeps abandoned journals into
crash dumps instead of silently treating them as clean sessions.

The checkpoint writes two records, closes the live `SqliteJournal`, reopens it
with `ReadonlySqliteJournal`, and proves the crash sweep leaves it in place.
That is durability across a clean process lifecycle, not proof of disk, node,
zone, or region survival.

For those guarantees, test the configured replication and restore path. Define
recovery point/time objectives, encryption, access control, deletion, storage
quotas, and `journal_retention="archive"` versus `"delete"`. Run restore drills;
an untested backup is only a hypothesis.

Use `record_to="runs"` when every cleanly stopped session should also export a
self-contained bundle. Treat journals and bundles as sensitive data: inspect
and redact before attaching them to issues or sending them to third parties.

## Shutdown order is part of correctness

For a production process:

1. mark draining and fail readiness;
2. stop accepting new WebSocket, WebRTC, HTTP, and telephony work without
   severing established media WebSockets;
3. ask active sessions to `stop()` gracefully while their media connections
   remain live;
4. after `drain_timeout_s`, call `stop(force=True)` for stragglers;
5. bound the forced phase with `force_shutdown_timeout_s`;
6. close any media WebSocket that survived session teardown;
7. finalize/flush journals and provider clients;
8. let the process exit before the orchestrator sends an uncatchable kill.

The orchestrator termination grace period must exceed listener shutdown plus
both EasyCat windows and scheduling margin. If it is shorter, the clean-close
marker and final records cannot be reliable.

`async with session:` uses force-stop on scope exit; it is excellent for scoped
ownership but is not a substitute for a process-level drain that first rejects
new calls. `VoiceServer.stop(force=True)` is the incident/deadline path, not the
normal rolling-deploy path.

## Practice failure before production does it for you

Run controlled drills for:

- invalid/missing auth and an at-capacity server;
- provider timeout, rate limit, and partial streaming failure;
- ingress disconnect during input and output;
- journal disk-full/degraded behavior and crash recovery;
- OTel exporter outage without application failure;
- a session that ignores graceful stop and needs force escalation;
- a bad release that becomes unready and rolls back;
- restoration of an archived/replicated journal under the declared RTO.

For each drill, verify the client outcome, readiness, metrics, journal evidence,
alert, runbook, and cleanup. A failure mode is not operationally covered merely
because an exception is caught.

## A compact promotion checklist

- The wheel/image digest matches the validated artifact.
- Quick, relevant socket/contracts/stress lanes, and configured live/latency
  gates passed with retained JSON reports.
- Secrets are runtime-injected; no secret entered source, build args, logs,
  metrics, bundles, or image layers.
- TLS/auth/capacity/readiness work through the real ingress.
- Journals have a tested storage, retention, redaction, and restore policy.
- Dashboards and alerts use bounded labels and name an owned runbook.
- Rollout fails readiness before drain; outer deadlines exceed EasyCat's.
- Rollback and incident force-stop paths have been exercised.

Continue with [the exercises](./EXERCISES.md) to turn this checklist into a
release record and failure-drill plan for your deployment.

## What you should be able to answer now

> Why should provider health not usually fail liveness?

It would restart a healthy process repeatedly; fail readiness or report
degradation so traffic moves while operators retain diagnostic access.

> Does a local SQLite journal survive node loss?

No. It survives process failure on its storage; node/zone survival needs tested
replication and restore.

> When is shutdown complete?

After admission stops, active work drains or is forced within bounds, and
backend/journal teardown finishes before the outer process deadline.

## Ladder complete

You can now choose an EasyCat runtime mode, wire providers and conversation
policy, expose tools and agent frameworks, own sessions, debug and evaluate
them, serve multiple clients, handle phone calls, and operate the result. Use
the index to revisit a feature by outcome rather than repeating the ladder in
order.
