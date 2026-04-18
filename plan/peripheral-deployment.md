# Peripheral: Deployment

> **Peripheral follow-up to `essential-debug-first-runtime.md`.**
> The essential plan pins the deployment constraints (non-negotiable
> requirements, tier assignments, and the two shipped journal
> backend adapters). This file is the concrete per-platform
> runbook, SQLite-vs-alternative decision tree, and tuning guide.
>
> **Scope status:** peripheral but **not deferred**. Deployment
> guidance should land alongside WS1 (the journal backend adapters
> here depend on WS1 T1.4's adapter plug-point) so early adopters
> have a supported deployment story from the first alpha.
>
> **Sibling peripherals:**
>
> - `peripheral-dx-onboarding.md`
> - `peripheral-eval-and-debugger-ui.md`
> - `peripheral-observability-and-cost.md`
> - `peripheral-provider-ecosystem.md`
> - `peripheral-redaction.md`

## Context

EasyCat's debug-first runtime wraps a latency-sensitive audio
pipeline (P50 turn <1.0s, P90 <1.6s) inside a long-lived WebSocket
session with in-memory per-session state and an optional crash-
durable journal. The set of platforms that can host this shape
without workarounds is narrower than "any platform that runs
Python" and much narrower than "any serverless platform." This
document enumerates the supported platforms, the caveats for each,
and the journal backend choice per platform.

The tier assignments are committed in `essential-debug-first-
runtime.md` under **Deployment Targets**. This file owns the
execution details.

## Decision tree (for docs/deployment.md)

```text
Is this a hobby project, internal demo, or prod?
├── Hobby / demo
│   └── Fly Machines, single region, auto_stop=suspend, no Volume
│       Journal: in-memory ring buffer (debug="light")
│
├── Internal prod, <50 concurrent sessions
│   └── Fly Machines with Volume + Litestream→S3  (DEFAULT)
│       Journal: debug="full", backend="sqlite+litestream"
│       min_machines_running=1 per region, auto_stop=suspend
│
└── Production, >=50 concurrent sessions
    ├── Already on AWS
    │   └── ECS Fargate (or EC2 ASG) behind NLB
    │       Instance family: c6i.xlarge (4c/8GB, LiveKit-sized)
    │       Journal: debug="full", backend="sqlite+litestream"
    │                on EBS, Litestream → S3
    │       Sizing: 10-25 concurrent sessions per 4c/8GB worker
    │
    ├── Already on GCP and calls always <60 minutes
    │   └── Cloud Run with min-instances=1
    │       Flags: --timeout=3600 --session-affinity
    │              --execution-environment=gen2 --cpu-boost
    │              --no-cpu-throttling
    │       Journal: debug="full", backend="libsql"
    │                (Turso embedded replica)
    │
    ├── Pay-as-you-go, function shape OK
    │   └── Modal with @modal.asgi_app() on @app.cls
    │       min_containers=1, buffer_containers>=1
    │       timeout=3600, scaledown_window=600
    │       Model weights prewarmed in @modal.enter()
    │       Journal: debug="full", backend="libsql"
    │
    └── Need density and custom routing
        └── EC2 ASG behind NLB, LiveKit-style worker pool
            Journal: debug="full", backend="sqlite+litestream"
```

## Tier 1 runbooks

### Fly.io Machines (default)

**Why it's the default.** Fly is the only platform that combines
native unbounded WebSockets, sub-second `auto_stop_machines=
"suspend"` / resume from memory snapshot, persistent Volumes for
the SQLite journal, first-class Litestream and LiteFS support,
global regions for latency, real Linux containers for native audio
deps, and a documented spawn-per-session pattern via the Machines
API (`api.machines.dev/v1`). The Pipecat project uses it as its
reference self-host target.

**`fly.toml` sketch** (one app, one Machine per session, spawned
via API):

```toml
app = "my-voice-agent"
primary_region = "iad"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "suspend"
  auto_start_machines = true
  min_machines_running = 1

[mounts]
  source = "easycat_data"
  destination = "/data"

[env]
  EASYCAT_DATA_DIR = "/data/.easycat"
  EASYCAT_JOURNAL_BACKEND = "sqlite+litestream"
  EASYCAT_JOURNAL_LITESTREAM_REPLICA = "s3://voice-agent-journals/{hostname}"

[[vm]]
  cpu_kind = "shared"
  cpus = 2
  memory_mb = 1024
```

**Sizing.** 2 vCPU shared / 1 GB RAM for the simple chained
pipeline (Deepgram + OpenAI + ElevenLabs, no local VAD/Turn
models). Bump to **4 vCPU / 4 GB** when Silero VAD, SmartTurn
ONNX, and Krisp are all enabled — these load ONNX models into
process memory and benefit from real cores.

**Spawn-per-session pattern (optional, for high concurrency).** A
long-running "runner" Machine receives `/start_bot` POSTs and
calls the Machines API to spawn a short-lived per-session Machine
with `auto_destroy=True`, identical to Pipecat's documented
pattern. Use this when sessions are long and you want strict
isolation; skip it for shared-Machine deployments.

**Region pinning.** Keep the session on the same region as the
user for the full call — don't load-balance across regions. Fly's
`fly-prefer-region` header lets the client influence placement.

**Journal backend:** `sqlite+litestream` on the mounted Volume.
Litestream restore happens at Machine boot via the app's start
command before the Python process launches:

```bash
litestream restore -if-replica-exists -config /etc/litestream.yml /data/.easycat/journals/current.sqlite
exec uvicorn my_voice_agent:app --host 0.0.0.0 --port 8080
```

### AWS EC2 / ECS Fargate + EBS + Litestream

**When to choose this.** Already on AWS. Need VPC/IAM/compliance
posture. Want LiveKit-style density.

**Sizing (from LiveKit Agents docs).** 4 cores / 8 GB RAM per
agent worker handles **10–25 concurrent sessions**. Use compute-
optimized instance families:

- `c6i.xlarge` (4 vCPU / 8 GB) — baseline
- `c7i.xlarge` (4 vCPU / 8 GB) — if available in the region
- `c6i.2xlarge` (8 vCPU / 16 GB) — if doubling per-worker density

**Do not use burstable instances** (`t3`, `t4g`, `t3a`). CPU
credit starvation causes Smart Turn ONNX inference to time out
unpredictably, which manifests as missed endpointing and delayed
interruption. The LiveKit docs call this out explicitly; EasyCat
inherits the same constraint because it runs the same Smart Turn
model shape.

**Storage.** EBS (gp3 or io2) for the SQLite journal on EC2. On
Fargate, attach an EBS volume at `/data` via the Fargate EBS
integration; **do not** use EFS for the journal — NFS fsync
semantics kill WAL throughput and SQLite-over-NFS is officially
not recommended by the SQLite project.

**Journal backend:** `sqlite+litestream`. Litestream replicates
WAL segments to S3 with sub-second RPO.

**Networking.** Put an NLB in front (not an ALB) — NLB gives you
raw TCP pass-through for WebSockets without ALB's idle timeout
quirks. Session stickiness handled by NLB target-group source-IP
affinity or by sticky sessions on the application layer.

### Modal

**When to choose this.** Pay-as-you-go, don't want to manage
infrastructure, acceptable to structure the app as a Modal class.

**App skeleton:**

```python
import modal
from fastapi import FastAPI, WebSocket

image = (
    modal.Image.debian_slim()
    .pip_install("easycat", "fastapi", "uvicorn")
)

app = modal.App("voice-agent", image=image)

@app.cls(
    min_containers=1,        # at least one warm container
    buffer_containers=1,     # plus one ready to accept a burst
    scaledown_window=600,    # keep warm 10min after last call
    timeout=3600,            # allow full-hour calls
    cpu=4,
    memory=4096,
)
class VoiceAgent:
    @modal.enter()
    def warmup(self):
        """Runs once per container at startup.

        Preload ONNX models, open the libSQL connection,
        and warm the SQLite file-open cost.
        """
        from easycat import create_session
        self.session_factory = ...  # build configured factory

    @modal.asgi_app()
    def web(self):
        api = FastAPI()

        @api.websocket("/voice")
        async def voice(ws: WebSocket):
            await ws.accept()
            session = self.session_factory()
            await session.run_websocket(ws)

        return api
```

**Required settings explained.**

- `min_containers=1` — without this, every new call pays the
  cold-start cost of loading ONNX models (~2–5s), blowing the
  latency budget for the first turn.
- `buffer_containers=1` — keeps a second container ready so
  simultaneous calls don't serialize on warmup.
- `scaledown_window=600` — default is too aggressive; voice apps
  have bursty traffic patterns and benefit from keeping warm
  containers around.
- `timeout=3600` — Modal's default function timeout is 600s (10
  minutes); long voice calls will be killed without this.

**Journal backend:** `libsql` with a Turso remote primary. Modal
Volumes are tuned for model weights, not hot transactional fsync
loops, so the journal goes to an embedded libSQL replica syncing
to Turso's edge network every 10 seconds.

## Tier 2 runbooks (with caveats)

### Google Cloud Run

**Caveat:** Cloud Run's maximum request timeout is **60 minutes**
(`--timeout=3600`). Any voice call that exceeds 60 minutes is
terminated mid-session. If your use case includes long support
calls, skip Cloud Run and use a Tier 1 option.

**Deployment:**

```bash
gcloud run deploy voice-agent \
  --image gcr.io/PROJECT/voice-agent \
  --timeout=3600 \
  --min-instances=1 \
  --session-affinity \
  --execution-environment=gen2 \
  --cpu-boost \
  --no-cpu-throttling \
  --cpu=2 \
  --memory=2Gi \
  --region=us-central1
```

- `--execution-environment=gen2` is **mandatory**. Gen1 does not
  support all Linux syscalls native audio deps need.
- `--cpu-boost` + `--no-cpu-throttling` — keeps CPU at full
  speed during idle moments between turns, which is when Smart
  Turn ONNX runs. Without these flags, inference drifts.
- `--session-affinity` — routes all messages from a WebSocket to
  the same instance. Without it, `TurnContext` state is lost on
  reconnect.
- `--min-instances=1` — avoids cold starts on the first call of
  the day.

**Journal backend:** `libsql` with a Turso primary. Cloud Run's
local FS is tmpfs (RAM), so Litestream cannot reliably ship WAL
segments before container exit.

### Railway / Render / DigitalOcean App Platform

**Caveat:** instances are replaced during deploys and maintenance.
Render explicitly documents this: "you should add retry logic to
handle brief disconnects that can happen when our platform
replaces your web service instances." Clients must implement
reconnect logic that reopens the WebSocket and resumes the turn
from the last journal record.

**Journal backend:**

- **Railway** — supports volumes, use `sqlite+litestream` on the
  volume. Volume is region-pinned; moving the service between
  regions causes a migration with downtime.
- **Render** — persistent disks are region-pinned and scoped to a
  single instance. Use `libsql` with a Turso primary so multiple
  Render instances can share the journal if you scale out.
- **DO App Platform** — no native volumes for app services. Use
  `libsql` with a Turso primary or managed Postgres.

## Tier 3 (why these fail)

Detailed failure modes so users don't waste time investigating
whether workarounds are possible.

### AWS Lambda (any variant)

- **API Gateway WebSocket**: 2-hour max connection, 10-minute
  idle timeout, charges per message, request-response execution
  model. Every inbound WebSocket frame fires a fresh Lambda
  invocation with no in-memory state from previous frames — so
  `TurnContext`, `VoiceDeliveryLedger`, and the journal would all
  have to round-trip to an external store (DynamoDB, Redis) on
  every message. Sub-second turn latency is impossible under
  this constraint.
- **Function URL RESPONSE_STREAM via Lambda Web Adapter**: only
  supports one-way server→client streaming (SSE-style). No
  bidirectional WebSocket. Incompatible with the audio pipeline.
- **Lambda with Provisioned Concurrency**: even with warm
  containers, the per-invocation execution model still breaks the
  in-memory state model. Not a workaround.

### Azure Container Apps

- **240-second HTTP request timeout** on the consumption plan
  (`learn.microsoft.com/en-us/azure/container-apps/ingress-
  overview`, updated 2026-03-25). WebSockets inherit the timeout.
  Any voice call longer than 4 minutes is terminated.
- **Dedicated plan** does not lift the timeout either; it's a
  platform constraint, not a pricing tier.
- Verdict: disqualifier. Use Azure Kubernetes Service (AKS) or
  Azure Virtual Machines if you're committed to Azure.

### Cloudflare Workers Python

- Runtime is Pyodide/WASM, not native CPython.
- `onnxruntime` — no Pyodide build (as of April 2026). Silero
  VAD and Smart Turn cannot run.
- `webrtcvad` — not in the Pyodide package set. Fallback VAD
  cannot run.
- `librosa` — pulls `numba`/`llvmlite`, not supported in
  Pyodide. Audio feature extraction cannot run.
- `sounddevice` / PortAudio — requires host audio hardware,
  impossible on Workers at all.
- Architecturally Cloudflare is a dream fit: hibernatable
  WebSockets, Durable Objects with embedded SQLite, global
  edge, pay-per-request. EasyCat should revisit in 6–12 months
  if the Pyodide package set grows to include `onnxruntime`.

### Vercel

- Python runtime is Lambda under the hood with a 15-minute
  function cap.
- No bidirectional WebSocket server in the Python runtime.
- Vercel Edge is JavaScript/WASM only.
- Verdict: wrong platform shape entirely.

### Cloud Run Jobs

- Batch execution model, not an HTTP service.
- Wrong shape for long-lived WebSocket sessions.
- Use Cloud Run (not Cloud Run Jobs) if you're on GCP.

## SQLite tuning reference

These settings are already baked into `SqliteJournal` and
`LitestreamSqliteJournal` in WS1, but documented here for users
who want to understand the tradeoffs.

- **`PRAGMA journal_mode=WAL`** — mandatory. WAL allows
  concurrent readers while a writer appends, and groups commits.
- **`PRAGMA synchronous=NORMAL`** (not `FULL`). `NORMAL` only
  fsyncs at WAL checkpoints, not every commit. Drops commit
  overhead from ~5–15ms (`FULL`) to ~10–100µs (`NORMAL`). You
  lose at most the last WAL segment on power loss; WAL is still
  crash-safe. This matches the Litestream-recommended settings.
- **Batched per-turn commits.** The journal accumulates records
  in a queue during a turn and flushes in one transaction at
  turn boundary. Per-record commits are ~1s per turn from
  `fsync` alone; per-turn commits are ~1ms on NVMe.
- **`aiosqlite` threading**: one `aiosqlite.Connection` per
  session or per writer. Do not share one connection across
  sessions — `aiosqlite` marshals operations onto a dedicated
  per-connection thread, and sharing causes serialization.
- **File-open cost**: ~1ms warm (OS cache hot), up to ~50ms cold
  on serverless first turn. Mitigate by opening the DB in the
  container startup hook (`@modal.enter()`, Fly start command).

## Litestream vs LiteFS

Both are by Ben Johnson. They solve different problems.

- **Litestream** — single-writer WAL shipping to object storage.
  The primary use case is disaster recovery: the DB lives on
  local disk, WAL segments ship to S3 on an interval (sub-second
  RPO), and restore-on-startup rebuilds the DB from the latest
  segment chain. EasyCat's journal is single-writer per session,
  so Litestream is the right tool.
- **LiteFS** — FUSE-based distributed SQLite with a primary and
  read replicas. Useful if you want multiple machines to *read*
  the journal (e.g., a debugger UI on a different Machine
  tailing the live journal of another Machine). Not needed for
  durability alone; EasyCat's Tier 1 Fly deployment uses
  Litestream by default and users can opt into LiteFS if they
  want multi-machine read replicas.

## libSQL / Turso tuning reference

For `LibsqlJournal`:

- `sync_interval` default 10s. Lower values increase sync
  frequency and reduce RPO but add latency on turn-boundary
  flushes. Higher values increase RPO but reduce sync overhead.
  10s is a reasonable default for the journal use case where
  the worst case is losing ~10s of tail records on container
  kill.
- Explicit `conn.sync()` at turn boundary is a no-op if the
  background syncer has already flushed; adding it as a belt-and-
  suspenders call adds ~1–2ms per turn and guarantees the turn's
  records are durable before the next turn begins.
- Credentials (`EASYCAT_LIBSQL_URL`, `EASYCAT_LIBSQL_AUTH_TOKEN`)
  are in the WS1 T1.5 safe-default env var allowlist as
  **presence-only** — the values are dropped before they reach
  any journal record or artifact.

## CI coverage

The essential plan commits WS1 AC1.17 and AC1.18 covering:

- batched per-turn commit behavior (`fsync` count assertion)
- Litestream adapter round-trip (gated on the Litestream binary)
- libSQL adapter round-trip (gated on `sqld`)
- credential redaction for the new backend env vars

Platform-specific deployment smoke tests are out of scope for
WS1 CI but the peripheral recommends that production users run a
canary against their chosen platform at least weekly, and that
the canary include a forced process kill + journal recovery
round-trip via `RunBundle.from_partial_journal()` (WS4 T4.5.5).

## Dependencies on other peripherals

- `peripheral-dx-onboarding.md` wraps `easycat doctor` with per-
  platform health checks (Fly Machine detection, Modal container
  detection, Cloud Run env detection) and adjusts its suggestions
  based on the detected host.
- `peripheral-observability-and-cost.md` exports OTel/metrics to
  cloud providers' native monitoring (CloudWatch, Stackdriver,
  Azure Monitor) where appropriate; per-platform exporter config
  lives here.
- `peripheral-redaction.md` doesn't directly depend on this
  peripheral but uses the same safe-default allowlist for the
  new Litestream/libSQL env vars.

## Open questions (to be resolved during implementation)

- **Fly + LiteFS multi-reader pattern.** Is it worth shipping as
  a documented Tier 1 variant, or is single-writer Litestream
  enough for the debug-first thesis? Revisit once the debugger
  UI peripheral has a concrete shape.
- **Kubernetes (AKS, EKS, GKE) deployment.** Not explicitly
  tiered above because the user's K8s-specific concerns
  (Ingress, StatefulSet vs Deployment, PVC types) dominate the
  platform-generic guidance. Revisit if early adopters request
  it.
- **WebRTC transports on serverless.** Fly and EC2/Fargate work
  fine. Cloud Run's cold-start behavior may cause the ICE
  negotiation to time out on a cold container; needs empirical
  testing.
