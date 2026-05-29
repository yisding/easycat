# Observability in EasyCat

EasyCat exposes four distinct layers for seeing what a voice bot is doing. They
overlap on purpose, but each answers a different question and has different
guarantees. Reach for the wrong one and you will either lose data, leak PII, or
accidentally couple your application logic to a diagnostic sink.

This guide is the "which layer do I use when" map.

## The four layers

| Layer | What it is | Use it for | Guarantees |
| --- | --- | --- | --- |
| **A — stdlib logging** | `logging.getLogger("easycat")`, controlled by `EASYCAT_LOG_LEVEL` | Human, ad-hoc diagnostics while developing or tailing a process | Lossy. Off by default in library mode. |
| **B — EventBus** | `easycat.events`, `session.subscribe_event(...)` | Driving application behavior in reaction to session events | Live, in-process. Not a durable record. |
| **C — ExecutionJournal** | `runtime/`, `session.journal.read()`, `export_debug_bundle()`, the `easycat` CLI | Durable, structured, replayable record of a whole session | Complete single source of truth. PII-bearing by design. |
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
- If you want a durable mirror of what flowed across the bus, that is the
  journal's job (C), which records bus activity (via the session journal sink)
  plus per-stage internal detail the bus never carries.

### C — ExecutionJournal

The journal (`runtime/`) is the durable, structured, replayable record of a
session. Read it live with `session.journal.read()`, export a self-contained
bundle with `export_debug_bundle()`, or inspect a bundle with the `easycat` CLI.

- It is the **single source of truth** for "what actually happened" — complete
  where logs are lossy.
- It is **PII-bearing by design**: it records transcripts, agent output, and tool
  arguments so a session can be faithfully replayed and debugged.
- It is gated by `debug=` (see orthogonality below): `debug="off"` does not
  journal; `debug="light"`/`debug="full"` do.

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

There are two independent knobs, and they control different things:

- **`EASYCAT_LOG_LEVEL`** — controls layer A only (the stdlib `easycat` logger
  level). Accepts `debug`, `info`, `warning`, `error` (case-insensitive). When a
  process owner enables console logging, this resolves the level; the default is
  `INFO`, and `DEBUG` is used only when you explicitly request it. It has the
  same single meaning in `easycat.run()` and in `debug="light"`/`debug="full"`.
- **`EASYCAT_LOG_FORMAT=json`** — switches layer A's console handler from the
  human/Rich format to single-line JSON. This is an **explicit opt-in**: a TTY
  toggles *color* only, never JSON.

  The JSON field set is a **semi-public UNSTABLE schema** — do not build hard
  dependencies on it yet. Current fields:

  | Field | Meaning |
  | --- | --- |
  | `ts` | ISO-8601 timestamp |
  | `level` | log level name |
  | `logger` | logger name (e.g. `easycat.session._session`) |
  | `msg` | formatted message |
  | `session_id` | bound session id, or `null` |
  | `turn_id` | bound turn id, or `null` |
  | `exc` | formatted traceback (only present when an exception is attached) |

- **`debug=`** (`"off"` / `"light"` / `"full"` on `EasyConfig`) — controls the
  journal (C) and the optional debugger UI. It is **orthogonal to log level**:
  `debug=` decides whether and how much is journaled (and, for `"full"`, whether
  the debugger UI launches); `EASYCAT_LOG_LEVEL` decides how verbose the human
  console log is. Turning one up does not turn the other up.

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

- **The journal is currently UNREDACTED.** Bundles contain transcripts, agent
  output, and tool arguments verbatim. A pluggable `RedactionPolicy` is
  **planned but not yet implemented** — today the redaction hook is a no-op. Do
  not attach journal bundles to public issues or send them to third parties
  until you have manually scrubbed them.
- **Per-stage latency budgets are guidance, not enforcement.** Any latency
  targets you see documented elsewhere are advisory; nothing in the pipeline
  rejects or alerts on a stage that exceeds them. Use the OTel latency
  histograms (D) to observe real numbers.
- **`gen_ai.*` attributes are development status.** The committed
  `gen_ai.operation.name`, `gen_ai.request.model`, and `gen_ai.system` span keys
  track the OpenTelemetry GenAI semantic conventions, which are themselves still
  evolving. Treat them as subject to change; do not build durable dashboards that
  assume their stability.
