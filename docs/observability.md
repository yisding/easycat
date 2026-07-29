# Observability in EasyCat

EasyCat exposes four distinct layers for seeing what a voice bot is doing. They
overlap on purpose, but each answers a different question and has different
guarantees. Reach for the wrong one and you will either lose data, leak PII, or
accidentally couple your application logic to a diagnostic sink.

This guide is the "which layer do I use when" map.
From this repository, run `uv run easycat docs --audience operators` to see
the full operator-facing route slice, including deployment, observability, and
journal durability. Use `uv run easycat docs --audience operators --json` when
automation needs that same operator map with command hints.

## The four layers

| Layer | What it is | Use it for | Guarantees |
| --- | --- | --- | --- |
| **A — stdlib logging** | `logging.getLogger("easycat")`, controlled by `EASYCAT_LOG_LEVEL` | Human, ad-hoc diagnostics while developing or tailing a process | Lossy. Off by default in library mode. |
| **B — EventBus** | `easycat.events`, `session.subscribe_event(...)` | Driving application behavior in reaction to session events | Live, in-process. Not a durable record. |
| **C — ExecutionJournal** | `runtime/`, `session.journal.read()`, `export_debug_bundle()`, the `easycat` CLI | Structured record of a session | Full mode is durable and replay-complete. Light mode is bounded, omits per-frame spans, and reports any eviction. PII-bearing by design. |
| **D — OpenTelemetry facade** | `easycat._observability` | Production metrics and traces | PII-scrubbed, low-cardinality. No-op without an SDK. |

### A — stdlib logging

Standard Python logging on the `easycat` logger. All module loggers are
`easycat.*`, so configuring the `easycat` logger configures the whole package.

- **Default is silence.** As a library, EasyCat installs only a
  `logging.NullHandler()` on import and never calls `logging.basicConfig()`.
  Your application owns root logging.
- **Process owners** — the `easycat` CLI, `easycat.run()`, and `debug="light"`/
  `debug="full"` wiring — opt in to console output by attaching exactly one
  tagged handler to the `easycat` logger (never root) via
  `enable_console_logging()`. Enabling it also sets `propagate=False` on the
  `easycat` logger so records do not double-log through root handlers your app
  configured — those handlers stop receiving `easycat` records once console
  logging is enabled. If you want `easycat` records in your own root pipeline,
  do not enable console logging; configure the `easycat` logger yourself.
- Logging is **lossy**: messages are dropped below the configured level, and the
  format is meant for humans, not machines. Do not parse it. Do not depend on a
  specific message appearing — use the journal (C) for that.

### B — EventBus

`easycat.events` defines the event types; `session.subscribe_event(...)` lets you
react to them. The EventBus **drives application behavior** — it is how your app
learns that the user started speaking, a turn ended, the bot produced audio, etc.

- It is **not an observability sink.** Subscribing to events to "log" things is
  fine for app logic, but the bus is live and in-process; it is not a durable
  record and it is not replayable.
- `session.subscribe_event(...)` and `EventBus.subscribe(...)` return an
  idempotent subscription token. Keep that token when a component owns a
  callback lifecycle and call `token.unsubscribe()` during teardown.
- Event handlers run inline in subscription order. By default, handler
  exceptions are logged, counted on the bus, and do not stop later handlers from
  running. Set `EasyConfig(handler_error_policy="raise")` (or the equivalent
  `TextSessionConfig` / `create_text_session` option) in tests or strict app
  code when a handler failure should abort dispatch and propagate to the
  emitter. Direct `EventBus` construction accepts the same option.
- Sessions created from `EasyConfig` or `TextSessionConfig` warn when a callback
  takes at least 5 ms, because slow handlers can stall audio-critical paths.
  Tune this with `slow_handler_threshold_s=...`, or set it to `None` to disable
  the diagnostic. Direct `EventBus` construction leaves it disabled unless
  configured explicitly.
- If you want a durable mirror of what flowed across the bus, that is the
  journal's job (C), which records bus activity (via the session journal sink)
  plus per-stage internal detail the bus never carries.

### C — ExecutionJournal

The journal (`runtime/`) is the durable, structured, replayable record of a
session. Read it live with `session.journal.read()`, export a self-contained
bundle with `export_debug_bundle()`, or inspect a bundle with the `easycat` CLI.

- It is the **single source of truth** for "what actually happened." Full mode
  retains replay-complete stage detail. Light mode keeps turn, event, error,
  and control history while omitting per-frame Audio/VAD/STT spans and their
  artifacts.
- It is **PII-bearing by design**: it records transcripts, agent output, and tool
  arguments so a session can be faithfully replayed and debugged.
- `journal_redaction="secrets"` (the default) preserves that replay content
  while removing credentials. Set `journal_redaction="pii"` to irreversibly
  redact phone numbers, URLs, request IDs, home paths, prompts, transcripts,
  and provider text at write time. Redacted CLI views and coding-agent context
  packs apply their own PII policy regardless; raw journals and debug bundles
  should still be treated as sensitive.
- It is gated by `debug=` (see orthogonality below): `debug="off"` does not
  journal; `debug="light"`/`debug="full"` do.
- The light journal is bounded by `journal_capacity` (default `10_000`
  records). `session.journal.dropped_records`, the bundle manifest's
  `journal_dropped_records`, and `easycat bundles show` make any eviction
  explicit; increase the capacity when an unusually long or event-heavy
  session needs more in-memory history.
- `record_to="runs"` on `EasyConfig` or `create_text_session(...)` exports a
  timestamped debug bundle on clean shutdown when journaling is enabled.
- Error records preserve PEP 678 exception notes in `ErrorInfo.notes`. Runtime
  `Error` events attach `stage`, `provider`, `code`, `session_id`, and
  `turn_id` when known; stage wrappers also attach `elapsed_ms`, `sequence`,
  and `record_key` on re-raised provider failures so a traceback points back
  to the journal record that captured the failing input. When streaming agent
  and TTS branches both fail in one turn, EasyCat emits a pipeline
  `ExceptionGroup` error that preserves both child errors.
- CLI entry points:
  `easycat bundles list`,
  `easycat bundles show <path>`,
  `easycat debugger serve <path>`,
  `easycat inspect <path>`,
  `easycat replay <path>`,
  `easycat latency <path>`, and
  `easycat bundles export <path>`.
  `easycat latency <path>` rolls the critical-path milestone deltas up across a
  bundle's turns and reports `count`/`p50`/`p90`/`p95`/`p99` per segment; see
  [latency](latency.md).
  Add `--json` (`easycat bundles list --json`, `easycat bundles show <path> --json`,
  `easycat inspect <path> --json`, `easycat replay <path> --json`,
  `easycat latency <path> --json`,
  `easycat bundles export <path> --output DIR --json`) for a
  parseable summary. Add `--issues` to `easycat bundles show <path>` /
  `easycat inspect <path>` to render a severity-ranked rollup of detected
  problems (errors, tool failures, timeouts, empty transcripts, slow
  milestones, slow/missed barge-ins, and — when the bundle carries stored PCM
  artifacts — audio-health cards for clipping, near-silent caller capture, and
  dead air); the `issues` key is always present in the `--json` envelope.
- More journal CLI entry points:
  `easycat diff <path> <path>` diffs two bundles or journals turn by turn,
  surfacing milestone and transcript deltas between a baseline ("before")
  and a comparison ("after") run; restrict it with `--turn` and add `--json` for
  a parseable summary.
  `easycat journal grep <path> --query TEXT` runs a redacted full-text search
  over a journal or bundle, `easycat journal follow <path>` live-tails a SQLite
  journal as it grows (redacting every line), and
  `easycat journal promote <path> TURN_ID --out FILE` saves one turn as a
  replayable, self-contained regression bundle.
  `easycat tail <path>` is the short alias for `easycat journal follow <path>`.
- Optional debugger UI:
  install the extra with `uv sync --extra debugger --group dev` from this repo,
  or `uv add 'easycat[debugger]'` in an app. For post-call inspection, launch
  the first-class browser UI directly from the CLI:

  ```bash
  easycat debugger serve PATH --no-open-browser
  uv run easycat debugger serve runs/session.bundle --no-open-browser
  uv run easycat debugger serve .easycat/journals/<session_id>.sqlite
  ```

  The UI gives every captured call a timeline-first forensic workspace: an
  overview dashboard with recommended next steps, Live lanes for during-call
  event flow, per-turn waterfalls, transcript/audio playback, paged raw records,
  deterministic issue triage cards, replay controls, and
  live-session bundle export. You can also import `serve_bundle` for an offline
  bundle or `serve_session` for a live session:

  ```python
  from easycat.debugger import serve_bundle, serve_session

  serve_bundle("runs/session.bundle", port=8765)
  serve_session(session, port=8765, in_thread=True)
  ```

  The debugger is loopback-only by default and has no auth. Keep it on
  `127.0.0.1` unless you have a controlled, private debugging environment and
  explicitly choose `allow_remote=True`.

  **Dev mode (always-available dev timeline).** For the inner development loop,
  opt in to dev mode instead of launching the debugger by hand:

  ```bash
  EASYCAT_DEV=1 easycat serve
  ```

  ```python
  from easycat import VoiceApp

  VoiceApp(agent=agent, dev=True).run("browser")
  ```

  Dev mode (`EASYCAT_DEV=1` / `VoiceApp(dev=True)`) defaults to durable
  debugging when you have not set `debug=` explicitly, registers every live
  session in a process-local registry, and launches ONE loopback debugger UI per
  process. Registration flows through the single `create_session` funnel, so
  **every mode** populates the UI — including the per-connection
  browser/websocket/twilio sessions built downstream — and sessions
  auto-unregister when they stop (weakly held, so a stopped call is never pinned
  alive or left lingering in the list).

  The UI adds a **live session selector** that updates over the WebSocket as
  calls come and go (`GET /api/dev/sessions` lists them; `POST /api/dev/select`
  re-points every panel — switching a session resets the live-follow cursor so
  a session whose journal is "behind" still streams cleanly). It also surfaces a
  **cross-session overview strip** (`GET /api/dev/overview`) with one chip per
  session colored by error count, a "follow newest" toggle, `[`/`]` keyboard
  switching, and a filter box. If the default port (8765) is taken — e.g. a
  second dev process — the UI scans the next few loopback ports automatically
  (override with `EASYCAT_DEV_DEBUGGER_PORT`).

  Dev mode is **purely additive** over the autolaunch guard: it is a separate,
  explicit opt-in. `debug="full"` on its own still keeps a durable journal and
  never opens a browser tab — durable journaling and UI autolaunch remain
  distinct concepts. Dev mode also never opens a tab in CI / non-interactive
  shells (same loopback + interactive-terminal guards as the autolaunch path).

### D — OpenTelemetry facade

`easycat._observability` is a thin facade over the OpenTelemetry API for
production **metrics and traces**.

- It is a **no-op without an SDK**: if `opentelemetry-api` is absent or no SDK is
  configured, every span/metric call does nothing. OTel is an *optional*
  dependency; EasyCat never pulls it in as a hard dependency.
- It is **PII-safe and low-cardinality**: span and metric attributes are
  validated against an explicit allow-list (`easycat.*` and a small set of
  `gen_ai.*` keys). Any attribute that is on the forbidden list, or whose name
  *contains* a high-risk substring (`transcript`, `prompt`, `content`, `text`,
  `body`, `secret`, `token`), is rejected with a `ValueError`. This is
  defense-in-depth so a new PII-bearing attribute cannot silently leak into
  traces.
- Correlation ids (`session_id`/`turn_id`) are deliberately **kept out of OTel**
  attributes — they are logging-only correlation (see below) and would also be
  high-cardinality span attributes.

> **`easycat.server.*` server metrics are registered and emitted (M8).** The
> `easycat.server` process layer registers its five metric names
> (`easycat.server.requests.total`, `easycat.server.request.duration`,
> `easycat.server.sessions.rejected.total`, `easycat.server.connections.active`,
> `easycat.server.draining`) in `METRIC_DEFINITIONS` and its three new labels
> (`easycat.route`, `easycat.server_state`, `easycat.auth_result`) in
> `LOW_CARDINALITY_ATTRIBUTE_KEYS` — in the SAME change that first emits them,
> because emitting an unregistered name/key raises `ValueError`. Emission lives
> in `easycat.server.metrics` and routes through the same `sanitize_attributes`
> path (no bypass); it is a no-op without an SDK but still validates the
> names/labels. `easycat.route` is constrained to an **enumerated set of route
> templates** that is asserted before recording, so a raw path (which can carry
> `?token=` or user content) can never become a label. No token, PII, or raw
> path is ever a server-metric label.

## Golden rules

- **Logs are lossy; the journal is complete.** If you need to be sure something
  was captured, use the journal (C), not logging (A).
- **OTel is PII-safe; the journal is not.** Export OTel data to third parties
  freely. Treat journal bundles as sensitive — they contain transcripts and
  agent output.
- **The EventBus drives behavior; logs only observe.** Put application logic on
  the bus (B). Put human diagnostics in logs (A). Do not invert this.

### Why B and C both exist

The EventBus (B) is the live, in-process channel your application reacts to. The
journal (C) is the durable record. They are not redundant: the journal mirrors
the bus (via the session journal sink) *and* adds per-stage internal detail that
never crosses the bus. So B is for "act on this now," and C is for "reconstruct
exactly what happened later." You generally subscribe to B for behavior and read
C for forensics.

## Configuration and orthogonality

There are three independent knobs, and they control different things:

- **`EASYCAT_LOG_LEVEL`** — controls layer A only (the stdlib `easycat` logger
  level). Accepts `debug`, `info`, `warning`, `warn`, `error`, and `critical`
  (case-insensitive). When a process owner enables console logging, this
  resolves the level; the default is `INFO`, and `DEBUG` is used only when you
  explicitly request it. It has the same single meaning in `easycat.run()` and
  in `debug="light"`/`debug="full"`.
- **`EASYCAT_LOG_FORMAT=json|text|human`** — switches layer A's console handler.
  `json` renders single-line JSON, `text` renders plain non-Rich text for log
  collectors, and `human` renders the Rich-capable interactive formatter.
  This is an **explicit opt-in**: a TTY toggles *color* only for `human`, never
  JSON.

  The JSON field set is a **semi-public UNSTABLE schema** — do not build hard
  dependencies on it yet. Current fields:

  | Field | Meaning |
  | --- | --- |
  | `ts` | ISO-8601 timestamp |
  | `level` | log level name |
  | `logger` | logger name (e.g. `easycat.session._session`) |
  | `msg` | formatted message |
  | `session_id` | bound session id, or `-` |
  | `turn_id` | bound turn id, or `-` |
  | `exc` | formatted traceback (only present when an exception is attached) |

- **`EASYCAT_ENV=dev|prod`** — selects the default layer A renderer when
  `EASYCAT_LOG_FORMAT` is not set. `prod` / `production` uses single-line JSON
  for log pipelines; `dev` / unset keeps the human renderer. An explicit
  `EASYCAT_LOG_FORMAT` always wins.

- **`debug=`** (`"off"` / `"light"` / `"full"` on `EasyConfig`, default
  `"light"`) — controls journal and artifact capture (C).
  Journaling is on by default so sessions are always recorded, but the
  default `"light"` keeps turn/control history in memory and omits per-frame
  Audio/VAD/STT spans and artifacts. Opt into `"full"` for replay-complete,
  crash-survivable on-disk stage detail; set `debug="off"` to skip recording
  entirely.
  Debugger UI launch is an independent opt-in controlled by
  `debugger_autolaunch=True`. `debug=` is **orthogonal to log level**:
  `EASYCAT_LOG_LEVEL` decides how verbose the human console log is. Turning
  one up does not turn the other up.

- **Advanced observability knobs** are direct `EasyConfig` fields, for example
  `warmup=False`. The
  value is validated and preserved in safe debug-bundle config snapshots.
  `warmup=True` runs structural provider/model `warmup()` hooks during
  `Session.start()` before audio ingress and emits `warmup_completed` timing
  records. The bundled providers now implement those hooks — OpenAI TTS primes
  its HTTP pool, Silero VAD and Smart Turn prime their ONNX sessions, the OpenAI
  Realtime STT runs a connect-handshake-close cycle, and the OpenAI Agents
  bridge primes the SDK's shared client — so the first turn does not pay their
  cold-start cost.

### Correlation ids in logs

When a session/turn is active, log records emitted within that async context are
tagged with `session_id` and `turn_id` (via a `contextvars`-backed logging
filter on the console handler). The console formatter shows them as
`[session/turn]`, and the JSON formatter emits them as fields. Unbound records
show `-` in both formats.

The ids are captured at task-creation time: a task inherits the ids bound in the
context that created it. Short-lived per-turn work (agent, TTS) is created after
`bind_turn` and inherits the turn id; the long-lived audio-pipeline tasks are
created at session start, before any turn, so they re-bind the current turn each
loop iteration to stay correlated. `threading.Thread` workers do not inherit the
ids, but EasyCat avoids that boundary.

## Honesty caveats

- **The journal is still sensitive.** EasyCat scrubs safe config/environment
  snapshots, selected agent-bridge metadata, and obvious secret-like journal
  fields through `apply_write_filter`, but normal journal records and bundles
  still preserve transcript text, agent output, and tool-result text for replay.
  A pluggable full `RedactionPolicy` is still planned. Do not attach journal
  bundles to public issues or send them to third parties until you have manually
  scrubbed them. Config snapshots are diagnostic rather than lossless: cycles
  and values beyond fixed depth, item, node, scalar, and final-output safety
  budgets collapse to `...`, `<unavailable>`, or type/length markers so debug
  export cannot recurse forever or grow without bound.
- **Latency is reported, not gated.** Every pipeline stage records its
  `elapsed_ms` to the journal, and each turn emits a `turn_total_latency_ms`
  (voice, once first TTS audio is available) or `text_turn_latency_ms` (text)
  metric record. EasyCat does not reject or alert on slow turns at runtime; use
  the OTel latency histograms (D), the `easycat latency` bundle summary, and the
  `easycat validate latency` regression lane to observe real numbers and catch
  regressions in CI.
- **`gen_ai.*` attributes are development status.** The committed
  `gen_ai.operation.name`, `gen_ai.request.model`, and `gen_ai.system` span keys
  track the OpenTelemetry GenAI semantic conventions, which are themselves still
  evolving. Treat them as subject to change; do not build durable dashboards that
  assume their stability.
