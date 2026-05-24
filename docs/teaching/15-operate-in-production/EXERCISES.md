# Chapter 15 — Exercises

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

## 2. Run `easycat doctor` twice

**Task.** Run `easycat doctor` once with `OPENAI_API_KEY` unset,
then again with it set. Which health checks change?

**Hints**

1. The doctor checks five things: Python version, required
   extras (`sounddevice`, `onnxruntime`), optional extras (NR,
   AEC), API keys (`OPENAI_API_KEY` etc.), and provider
   reachability (an actual HTTPS request to each provider's
   health endpoint).
2. With `OPENAI_API_KEY` unset: the key check shows ❌ and the
   reachability check is skipped for OpenAI. Other checks
   unchanged.
3. With `OPENAI_API_KEY` set but *invalid*: the key check shows
   ✓ (it's present) but reachability shows ❌ (auth fails). That
   distinction matters when debugging — "key missing" vs "key
   wrong" are different failure modes.
4. `easycat doctor` is the first command to run on a new machine
   or in a CI container. It's faster than debugging by running
   the actual app.

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

You should be able to: (a) name all four lifecycle methods and
when to use each, (b) explain why `session.journal.read()` still
works after `stop()`, and (c) sketch the `SessionManager`
usage pattern for a WebSocket server in 10 lines without looking
at the file.

## The teaching ladder, complete

If you got here, you've built a voice pipeline from raw PCM to a
multi-session production server. Every remaining EasyCat surface
is either a new provider in the existing factories, a new
transport in the existing config, a new bridge in the existing
shim, or a new telephony deep-cut in the existing executors. The
pattern doesn't change.
