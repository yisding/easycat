# Chapter 2 — Exercises

## 1. Find the moment STT committed to the wrong guess

**Task.** Say a word the STT consistently mishears ("bass" vs
"base", "pear" vs "pair", "their" vs "there"). Re-run
`streaming.py`, then read the bundle and find the exact partial
where the wrong guess stuck. Compare it to the final.

**Hints**

1. The bundle is `runs/*.bundle`. Open it with
   `easycat.debug.testing.load_bundle`.
2. Filter records by `name == "stt.partial"` and
   `name == "stt.final"`. You should see the sequence of guesses
   converging.
3. The interesting case is when the *final* commits to a wrong
   guess — meaning the provider had a better partial at some
   earlier point and threw it away. That's a recall failure (the
   right hypothesis was on the table; the LM-prior overruled it).
4. The opposite case is also interesting: the final is *right* but
   the user saw wrong-looking partials flap by. That's why chapter 6
   reinforces "never commit spoken output from a partial."

**Wider points to check yourself on**

- Does the OpenAI batch-then-stream pattern make this exercise
  easier or harder? (Easier: all partials cluster at the end so
  the sequence is dense. Harder: the timing is misleading — the
  partials don't reflect when the *audio* was uttered.)
- Try the same exercise with both executable paths:

  ```bash
  uv run python docs/teaching/02-transcribe/streaming.py --provider openai
  uv run python docs/teaching/02-transcribe/streaming.py --provider deepgram
  ```

  Deepgram requires `DEEPGRAM_API_KEY`. Mid-speech partials change the feel
  completely; no source edit should be necessary.
- Inspect each bundle's `stt.provider.selected` record before comparing
  `offset_ms`. Confirm the credential *name* is present but its secret value
  is not, and explain why `after_stream_end` is not microphone latency while
  `during_audio` can be.

## 2. Open a bundle in two ways

**Task.** Read the same bundle two ways:

```python
from easycat.debug.testing import load_bundle

b = load_bundle("docs/teaching/02-transcribe/runs/<file>.bundle")

# Way 1: linear iteration
linear = [r for r in b.records() if r["name"] == "stt.partial"]

# Way 2: RunBundle's structured stage filter
structured = [r for r in b.filter_by_stage("stt") if r["name"] == "stt.partial"]

assert [r["sequence"] for r in linear] == [r["sequence"] for r in structured]
for r in structured:
    print(r["sequence"], r["data"]["text"])
```

When does each shape pay off?

**Hints**

1. Linear iteration is good for "I want to see what happened in
   order." Structured query is good for "I want all records of
   one kind, ordered correctly."
2. Chapter 11 leans entirely on the structured query shape because
   real debugging is "all the TTS spans in this turn" not "every
   record from t=0."
3. `load_bundle()` returns a `RunBundle`; its query helpers return
   dictionaries just like `records()`. A live session journal exposes
   a `JournalView` whose query helpers return typed `JournalRecord`
   objects. Chapter 11 compares those two representations explicitly.

## 3. Draw the partial-commit boundary

**Task.** Run the deterministic policy probe:

```bash
uv run python docs/teaching/02-transcribe/partial_policy_probe.py
```

Change the second partial to "cancel my timer" while leaving the final as
"set a timer for fifty minutes." Predict every list before rerunning it. Which
consumers may observe the cancellation hypothesis, and which must wait?

**Hints**

1. A caption can replace text freely; no external state was committed.
2. Speculation is safe only when it is keyed, cancellable, or discardable when
   the hypothesis changes.
3. Tool calls, database writes, agent-history commits, and spoken audio cross
   the irreversible boundary. Dispatch those from `FINAL`, not `PARTIAL`.

## 4. Separate stream end from provider close

**Task.** Run the provider-free ownership probe:

```bash
uv run python docs/teaching/02-transcribe/transcribe_ownership_probe.py
```

Predict the four lifecycle booleans before reading the output. Then remove the
helper's final `close_if_supported(owned_stt)` call temporarily and rerun. Which
contract breaks, and why would a persistent provider make that visible?

**Hints**

1. The logical stream ends in both cases because this helper owns the one-file
   transcription operation.
2. The helper-created STT's final cleanup also belongs to the helper.
3. A caller-supplied STT may be reused for another operation, so closing it
   would violate the caller's ownership.

## 5. Audit recording retention

**Task.** Run `batch.py`, open its newest bundle, and inspect
`recording.complete` plus `recording.cleaned`. Which sensitive data survives,
and which does not?

**Hints**

1. `recording.complete.data` contains the filename, duration, and
   `retention="temporary"`; it does not persist the absolute system-temp path.
2. `recording.cleaned.data.deleted` is `true` because bundle export happens
   after the `TemporaryDirectory` exits. The raw WAV is not a bundle artifact.
3. `stt.final.data.text` does survive. A transcript is PII-bearing even when
   raw audio is gone, so protect and expire the bundle accordingly.
4. Temporarily make `transcribe_file()` raise in a scratch copy. The context
   still removes the WAV, although no success bundle is exported.
5. If you intentionally retain audio, use an explicit project path or
   artifact store with consent and a deletion policy. An untracked temp file
   is not a retention strategy.

## 6. Fail one streaming sibling

**Task.** Run the provider-free lifetime probe:

```bash
uv run python docs/teaching/02-transcribe/stream_lifecycle_probe.py
```

Before reading the JSON, predict which cleanup events appear after a transport
connect failure, a failure after microphone startup, an STT start failure, and
an audio-feed failure. Then replace the `TaskGroup` in a scratch copy of
`streaming.py` with its old `gather()` call. Which ordering guarantee
disappears?

**Hints**

1. The transport and provider objects exist before `connect()` returns, so
   their final cleanup is registered before that fallible await.
2. In `partial_connect_failure`, the input stream starts before output startup
   fails. `transport.input.stop` proves the partially acquired device is still
   released.
3. `end_stream()` is registered only after `start_stream()` succeeds. A start
   failure must close the provider and disconnect the transport, but it did
   not open a logical stream that needs ending.
4. In the feed failure, `TaskGroup` cancels and joins the blocked event
   consumer first. `stt.events.cancelled` must therefore precede every
   resource-cleanup event.
5. The propagated feed error is an `ExceptionGroup` because `TaskGroup`
   preserves concurrent failures. The probe unwraps its first root message for
   compact JSON; production code may handle groups with `except*`.
6. `AsyncExitStack` and `TaskGroup` solve different problems: resource
   ownership versus task ownership. A correct streaming scope needs both.

## Self-check

You should be able to read any bundle from any chapter from now on without
consulting the README that produced it, and explain why observing a partial is
different from committing a side effect from one. You should also be able to
distinguish ending one STT stream from closing the provider that owns it, and
separate raw-audio retention from transcript retention. Finally, you should be
able to switch the Chapter 2 STT provider without editing its consumer loop and
tell a provider wire target from an upstream input-rate restriction. You should
also be able to prove that every concurrent task has stopped before its shared
STT and transport resources begin teardown.
