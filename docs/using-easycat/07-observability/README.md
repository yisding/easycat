# Chapter 7: See What Happened

Live event callbacks tell the application what is happening now. An execution
journal answers the harder question later: what actually happened across the
whole turn?

This chapter records two deterministic text sessions, exports portable bundles
after teardown, inspects them, replays their captured records, and compares a
baseline with a changed candidate.

## Prerequisites

- Complete [chapter 6](../06-session-control/), or know how a `Session` starts
  and stops.
- Run `uv sync --group dev` from the repository root. No API key, microphone,
  or provider call is required.
- For the optional browser UI, run
  `uv sync --extra debugger --group dev`.
- Treat the generated SQLite journals and bundles as sensitive. They can
  contain transcripts, agent text, tool arguments/results, and audio
  artifacts.

## Record a baseline and candidate

Run:

```bash
uv run python docs/using-easycat/07-observability/main.py pair .easycat/tutorial/ch07
```

The script runs the same two prompts through two deterministic workflow
variants and writes:

```text
.easycat/tutorial/ch07/baseline.bundle
.easycat/tutorial/ch07/candidate.bundle
```

Each run prints its current journal record count and turn count.

To record only one variant at a path you choose:

```bash
uv run python docs/using-easycat/07-observability/main.py record .easycat/tutorial/ch07/one.bundle
uv run python docs/using-easycat/07-observability/main.py record .easycat/tutorial/ch07/changed.bundle --variant candidate
```

The script deliberately calls `session.export_debug_bundle(...)` after the
session stops. That exercises chapter 6's read-only postmortem view and proves
bundle export does not depend on live providers.

## Journal and bundle are different forms

The execution journal is the session's ordered source of truth. It contains
events, spans, metrics, errors, correlation IDs, and references to captured
artifacts. `session.journal.read()` exposes that record stream while the
session runs and through the preserved post-stop view.

A debug bundle is a portable ZIP snapshot containing journal NDJSON, a
manifest, provider/config metadata, and referenced artifact blobs. The CLI can
read either a bundle or a persistent `.sqlite` journal.

The three `debug` modes choose storage behavior:

| Mode | Journal | Artifacts | Use it for |
|---|---|---|---|
| `off` | None | None | Explicit opt-out |
| `light` (default) | Bounded in-memory ring | In memory | Default recording that keeps per-frame capture off the disk and off the live audio loop |
| `full` | Persistent configured backend (SQLite by default) | Filesystem-backed | Durable postmortems and production capture (opt in) |

`debug="full"` does not open a browser by itself. Debugger autolaunch and dev
mode are separate opt-ins.

For always-on capture, `record_to="runs"` on `EasyConfig` or
`create_text_session(...)` automatically exports a timestamped bundle during
clean stop. Explicit export is useful when the application chooses the name or
decides whether a particular run should become an artifact.

## Summarize first

Start with a human summary:

```bash
uv run easycat bundles show .easycat/tutorial/ch07/baseline.bundle
```

Ask for the standard JSON envelope when another program will consume it:

```bash
uv run easycat bundles show .easycat/tutorial/ch07/baseline.bundle --json
```

The JSON includes `schema_version`, `command`, `status`, session/turn counts,
errors, tool calls, record count, duration, per-turn spans/milestones, issue
summary, annotations, provider versions, and artifact count. Test those named
fields rather than parsing Rich terminal output.

`inspect` accepts the same bundle/SQLite inputs. Add `--issues` to focus on the
severity-ranked triage rollup:

```bash
uv run easycat inspect .easycat/tutorial/ch07/baseline.bundle --issues
uv run easycat inspect .easycat/tutorial/ch07/baseline.bundle --issues --json
```

The clean teaching bundle reports zero issues. A real run can surface provider
errors, tool failures, timeouts, empty transcripts, slow milestones,
interruption problems, and supported audio-health signals.

## Replay safely

Replay the captured record stream with explicit safe settings:

```bash
uv run easycat replay .easycat/tutorial/ch07/baseline.bundle --fidelity artifact --tool-policy deny --json
```

This bundle reports the `agent` stage, artifact fidelity, and
`side_effecting: false`. Fast artifact replay masks nondeterministic timing
fields so repeated analysis can be deterministic.

Replay has two independent safety choices:

- Fidelity selects `artifact`, `simulated`, or `live` semantics.
- Tool policy selects `deny`, `stub`, or `allow` and defaults to `deny`.

Keep tools denied until you have reviewed the bundle and the replay target.
Use `stub` when a tool-bearing run should continue without its external
effect. `allow` passes recorded tool frames; the CLI has no application tool
registry and therefore does not invoke external tools. Library callers can
supply a tool executor to `RunBundle.replay(...)`, in which case executed
calls are explicitly reported as side-effecting. You can also restrict a
replay with `--turn`, `--stage`, or committable sequence bounds.

## Diff two runs

Compare the candidate with the baseline:

```bash
uv run easycat diff .easycat/tutorial/ch07/baseline.bundle .easycat/tutorial/ch07/candidate.bundle
uv run easycat diff .easycat/tutorial/ch07/baseline.bundle .easycat/tutorial/ch07/candidate.bundle --json
```

The diff pairs turns by position and compares milestone timing plus user/agent
transcript drift. This pair changes both agent responses, so each turn reports
`transcript.changed: true`.

The JSON command redacts free-form transcript bodies while preserving the
change signal. The bundles themselves are still sensitive and retain the
information required for local forensic work.

Output redaction is not bundle redaction.

Timing differences between these tiny local runs are incidental. Chapter 8
will turn latency observations into explicit, repeatable budgets instead of
treating one diff as a benchmark.

## Open the browser debugger locally

After installing the `debugger` extra, serve the baseline:

```bash
uv run easycat debugger serve .easycat/tutorial/ch07/baseline.bundle --no-open-browser
```

Open the printed loopback URL. The UI provides a session overview, per-turn
waterfalls, transcript/audio views when captured, raw records, issue triage,
and replay controls.

The debugger has no authentication and binds to `127.0.0.1` by default. Keep
that default. Non-loopback binding requires an explicit `--allow-remote`, but
it is only appropriate inside a separately controlled private environment.

For live development across many sessions, `VoiceApp(dev=True)` or
`EASYCAT_DEV=1` adds the process-local session selector and loopback debugger.
That is a developer opt-in, not a production exposure mechanism.

## Search and share with care

Search a large journal without loading every record into a UI:

```bash
uv run easycat journal grep .easycat/tutorial/ch07/baseline.bundle --query support
```

When a coding agent needs a smaller context pack, export the dedicated
redacted form and still review it before sharing:

```bash
uv run easycat bundles export .easycat/tutorial/ch07/baseline.bundle --output .easycat/tutorial/ch07/context --json
```

Normal bundles are PII-bearing by design. Do not attach them to public issues
or send them to third parties merely because a CLI summary redacted its
terminal output.

Continue with [the exercises](./EXERCISES.md) to inspect JSON fields, filter a
replay, and distinguish diagnostic output from the source artifact.

## What you should be able to answer now

> Why keep both EventBus subscriptions and a journal?

Events drive live application behavior; the journal is a durable forensic
record with additional stage detail.

> Does `debug="full"` automatically expose a debugger server?

No. It enables persistent capture; serving or autolaunching the UI is a
separate opt-in.

> Is a bundle safe to share if `easycat diff --json` redacts transcripts?

No. Output redaction does not remove sensitive content from the bundle.

## What's next

Chapter 8 promotes deterministic turns and captured bundles into assertions,
eval cases, and latency budgets that can fail CI usefully.
