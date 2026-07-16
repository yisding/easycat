# Chapter 15 — Exercises

[← Back to chapter](./README.md) · [Ladder index](../)

## 1. Two sessions fighting for one mic

**Task.** Add a second session to the manager before the first one
stops. Two local-transport sessions on the same mic will fight for
input — what does the journal show for each?

**Hints**

1. `LocalTransport` claims the PortAudio device exclusively (on
   most platforms). The second `connect()` either fails (good —
   you see the error in the journal) or succeeds and the OS
   round-robins audio between the two sessions (bad — both
   journals show partial audio).
2. The right shape for a multi-session demo is a *server*
   transport: `WebSocketTransport`, `WebRTCTransport`, or
   `TwilioConnectionTransport`. Each connection gets its own
   transport instance backed by its own socket. That's why
   `SessionManager` is a multi-connection abstraction, not a
   multi-microphone one.
3. The journal events to watch for: `transport.connected` /
   `transport.failed` on each session, and any
   `audio.error` records during the fight.

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

## 3. Translate a ch-13 bundle into a ch-12 eval input

**Task.** Run `translate.py` against a ch-13 bundle; pipe the
output into `evals.py` via a small adapter. Do the P50/P95 numbers
look right?

**Hints**

1. `translate.py` reads a ch-13 production-shape bundle (`stage_start`
   / `stage_complete` pairs) and emits NDJSON of teaching-shape
   composite records (`stage.X.execute` with `elapsed_ms`).
2. `evals.py` consumes `.bundle` files, not NDJSON. You'll need a
   small wrapper: build a fresh `InMemoryRingBuffer`, append each
   NDJSON record, then `export_debug_bundle` to a temp file, then
   point `evals.py` at the directory.
3. The numbers won't match chapter 12's hand-tuned fixtures
   exactly (your ch-13 turns are real, not synthetic), but the
   shape will: `agent` dominates, `tts_synth` is sub-second,
   `total_gap` is in the 800-2000 ms range.
4. This pipeline (production-shape bundle → translator →
   teaching-shape evals) is also how you'd build a CI gate:
   record N production turns nightly, translate, run evals,
   alert if P95 regresses.

## Self-check

You should be able to: (a) explain when to use `async with
session:`, `await session.stop()`, `await session.stop(force=True)`,
(b) explain why `session.journal.read()` still works after `stop()`,
and (c) sketch the `SessionManager` usage pattern for a WebSocket
server in 10 lines without looking at the file.

## The teaching ladder, complete

If you got here, you've built a voice pipeline from raw PCM to a
multi-session production server. Every remaining EasyCat surface
is either a new provider in the existing factories, a new
transport in the existing config, a new bridge in the existing
shim, or a new telephony deep-cut in the existing executors. The
pattern doesn't change.
