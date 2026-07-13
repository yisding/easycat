# Chapter 15 — Exercises

<!-- BEGIN auto:navigation -->
[← Chapter narrative](./README.md) · [Teaching ladder](../)
<!-- END auto:navigation -->

## 1. Probe `SessionManager` without two microphones

**Task.** Run the provider-free manager probe:

```bash
uv run python docs/teaching/15-operate-in-production/manager_probe.py
```

It keeps two fake connection sessions active together, attempts a
duplicate key, injects ordinary failure and task cancellation while two
other sessions start, and then proves that each released key is reusable.
It finishes with a two-session `stop_all()` sweep where one `stop()`
raises. Which guarantees belong to the manager, and which cleanup remains
the session's responsibility?

**Hints**

1. `SessionManager` has no journal and synthesizes no transport events.
   It owns a registry plus `start()` / `stop()` orchestration. Do not
   expect `transport.connected`, `transport.failed`, or `audio.error`
   records from the manager; those are not runtime record names.
2. `add(key, session)` reserves a unique key before awaiting
   `session.start()`. A duplicate raises `ValueError` without starting
   the duplicate. If start raises or the add task is cancelled, the
   manager removes the reserved key before re-raising, so a later
   connection can reuse it. `asyncio.CancelledError` inherits from
   `BaseException`, not `Exception`, which is why cancellation needs
   explicit rollback coverage. The session's own `start()` implementation
   must roll back resources it opened before failing or being cancelled;
   the manager does not call `stop()` on that partially started object.
3. Each `connection(...)` context calls `remove()` in `finally`, which
   removes the slot and awaits graceful `session.stop()`. Do not race
   `remove()` or `stop_all()` against code still running inside an
   overlapping connection block—cancel/finish those handler tasks first,
   then use `stop_all()` as the final sweep.
4. `stop_all()` clears the registry before awaiting every captured
   session's `stop()` concurrently. One stop failure is logged but does
   not prevent the other stop or escape from `stop_all()`. This isolates
   shutdown failures; it does not make a failed session's own cleanup
   successful.
5. The right shape for a real multi-session demo is a *server*
   transport: `WebSocketTransport`, `WebRTCTransport`, or
   `TwilioConnectionTransport`. Each connection gets its own
   transport instance backed by its own socket. That's why
   `SessionManager` is a multi-connection abstraction, not a
   multi-microphone one.
   PortAudio device sharing varies by host API and OS, so opening two
   `LocalTransport` instances is not a portable manager test.

## 2. Run `uv run easycat doctor` twice

**Task.** Compare two scoped, server-oriented JSON reports:

```bash
env -u OPENAI_API_KEY uv run easycat doctor \
  --provider openai --environment production --json

OPENAI_API_KEY=not-a-real-key uv run easycat doctor \
  --provider openai --environment production --json
```

Which rows appear or change? Why does the second command test network
liveness but not credential validity?

**Hints**

1. Doctor's eight check families are Python version, EasyCat version
   (including an informational list of importable integration extras),
   provider environment variables, provider reachability,
   `onnxruntime`, microphone, journal writability, and disk space.
   `--environment production` omits the local-microphone row. Doctor
   does not probe the noise-reduction or echo-cancellation extras.
2. `--provider openai` makes the credential requirement explicit. With
   the key unset, `env_openai` is `fail` with `EASYCAT_E203`, the command
   exits 1, and there is no `reach_openai` row because no probe runs.
   In an unscoped report, missing per-provider rows are `skip` and the
   aggregate `env_any` row fails only when no provider key is configured.
3. With the fake key set, `env_openai` is `ok` and doctor makes an
   **unauthenticated `HEAD`** request to OpenAI's base URL. Any HTTP
   response, including 4xx, produces an `ok` `reach_openai` row; a
   timeout, DNS failure, or connection error produces `EASYCAT_E204`.
   The probe never sends the key, so it cannot distinguish a valid key
   from `not-a-real-key`.
4. Use `--json` for CI and inspect the top-level `status`, process exit
   code, and each check's `name` / `status` / `detail` rather than
   scraping terminal glyphs. Use `--env-file .env` when the real keys
   live in a project dotenv file; doctor loads only recognized provider
   credentials and restores the process environment afterwards.

## 3. Gate a production bundle directly

**Task.** Record at least five voice turns in chapter 13 or 15, replace
`PATH` with the emitted bundle path, and run:

```bash
uv run easycat latency PATH --json \
  | uv run python docs/teaching/15-operate-in-production/latency_gate.py \
      --metric vad->tts --percentile p95 --max-ms 2000 --min-samples 5
```

Then lower `--max-ms` until the gate fails. Finally raise
`--min-samples` above the bundle's count. Why are those two failures
different?

**Hints**

1. `easycat latency` reads production `stage_start` /
   `stage_complete` spans and milestone records directly. Its JSON
   envelope contains one `turns` entry per turn plus `count`, `p50`,
   `p90`, `p95`, and `p99` for five critical-path metrics. Do not
   translate production records into chapter 12's synthetic fixture
   shape before measuring them.
2. `latency_gate.py` consumes that maintained JSON envelope. It exits 0
   only when the selected percentile is at or below your budget *and*
   the selected metric has at least `--min-samples` observations. A
   one-turn "P95" is just that one turn, not useful tail evidence.
3. `--max-ms 2000` is an example policy, not an EasyCat guarantee.
   Choose a threshold from your product SLO and a representative
   baseline. The output distinguishes `over_budget` from
   `insufficient_samples`, so CI tells you whether latency regressed or
   the run simply failed to collect enough evidence.
4. For a live multi-condition provider sweep and stored-baseline drift
   detection, use
   `uv run easycat validate latency --sweep --baseline PATH`. The
   captured-bundle gate here is for replayable call samples you already
   recorded; the validation command owns live canaries.

## 4. Prove the stable postmortem view

**Task.** Run the provider-free full-SQLite probe:

```bash
uv run python docs/teaching/15-operate-in-production/postmortem_probe.py
```

Explain why `same_object_after_stop` and `records_preserved` are true,
why `append_exposed_before_stop` was already false, and why the backend
type changes from `SqliteJournal` to `ReadonlySqliteJournal`.

**Hints**

1. `session.journal` exposes `JournalView`, not the writable runtime
   backend. It supports read, slice, lookup, filtering, and follow; it does
   not expose `append()` at any lifecycle phase.
2. The runtime owns the writable backend while the session is live. Clean
   stop finalizes and closes that backend, then retargets the existing view
   to a preserved read-only backend. Cached view references remain valid.
3. The probe uses `debug="full"` and SQLite so the backend transition is
   visible. A `debug="light"` session follows the same public invariant but
   transitions from `InMemoryRingBuffer` to `FrozenJournalSnapshot`.
4. `session.export_debug_bundle(...)` reads the preserved backend after
   stop. Reloading the emitted bundle and matching its record names proves
   the export is not merely an empty ZIP created after teardown.

## Self-check

You should be able to: (a) explain when to use `async with
session:`, `await session.stop()`, `await session.stop(force=True)`,
(b) explain why `session.journal.read()` still works after `stop()`,
including why the cached view keeps its identity, and (c) sketch the
`SessionManager` usage pattern for a WebSocket server in 10 lines
without looking at the file.

## The teaching ladder, complete

If you got here, you've built a voice pipeline from raw PCM to a
multi-session production server. Every remaining EasyCat surface
is either a new provider in the existing factories, a new
transport in the existing config, a new bridge in the existing
shim, or a new telephony deep-cut in the existing executors. The
pattern doesn't change.
