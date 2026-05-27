# Chapter 2 — Transcribe

> Speak, see text. Twice — once batch, once streaming. Feel the
> latency difference. And meet the journal.

## Prerequisites

- [Chapter 1](../01-echo/)
- `uv sync --extra quickstart --group dev`
- `export OPENAI_API_KEY=sk-...` (or any other provider from
  `src/easycat/stt/factory.py`).

> **Minimum to skip the ladder:** chapter 1 for the `Transport`
> protocol. You can read this chapter without chapter 0's PCM math.

## Diff from chapter 1

- **Added:** STT provider via `create_stt_provider`; the first
  `RunBundle` written to `runs/`; the partial-vs-final event shape
  (`stt.partial`, `stt.final`).
- **Modified:** the pipeline forks — audio still flows out of the
  transport, but it now goes to STT instead of back to the speaker.
- **Removed:** the speaker output (no echo in this chapter; this is
  one-way mic → STT).

## The two scripts

```bash
uv run python docs/teaching/02-transcribe/batch.py
uv run python docs/teaching/02-transcribe/streaming.py
```

Each records 5 seconds, sends it to STT, prints what came back,
and writes a debug bundle to `docs/teaching/02-transcribe/runs/`.

## Architecture

```
 ┌─────┐    send_audio()    ┌────────────┐    events()    ┌──────────┐
 │ Mic │ ──────────────────►│     STT    │ ─────────────► │ Consumer │
 └─────┘   AudioChunks      └────────────┘  STTEvent      └──────────┘
                                          (PARTIAL | FINAL)
```

Same STT provider, two usage patterns:

- **batch** — record first, transcribe in one call. The helper
  `easycat.quick.transcribe_file(path)` wraps everything in ~30 lines.
- **streaming** — start the STT stream, push audio as it arrives,
  consume events concurrently. When the stream ends, partials and a
  final flow back.

## A note on which provider you run

`streaming.py` defaults to `"openai"`. The OpenAI STT provider
**buffers the audio locally and uploads it on `end_stream()`**,
then streams the *response* back. That means you will see
partials arrive in a burst *after* the 5-second recording ends,
not during it. The partials are real; the timing is misleading.

For truly mid-speech partials — the ones that arrive while you
are still talking — switch to Deepgram and set
`DEEPGRAM_API_KEY`. Deepgram is strict about its input format,
so the factory call carries two provider-specific settings:

```python
stt = create_stt_provider(STTProviderConfig(
    provider="deepgram",
    api_key=dg_key,
    params={"sample_rate": 24000, "event_bus": EventBus()},
))
```

Chapters 3+ use exactly this configuration. The consumer code
(start_stream / send_audio / events) is identical to the OpenAI
path — that's the factory pattern's payoff.

Both providers teach the same concept, below.

## Partial vs final

Every streaming STT is a guesser under time pressure. As more
audio arrives, it revises its guess — producing a sequence of
**partials** that settle, then commit:

```
  (speaking: "go into town")
  t+5100ms  [part ] going to
  t+5140ms  [part ] going to town
  t+5180ms  [part ] go into town
  t+5200ms  [FINAL] go into town
```

For OpenAI (batch audio, streaming response) the timestamps
cluster at the end because they reflect the response stream, not
the speech. For Deepgram (mid-speech partials) the same sequence
spreads across the utterance. The *shape* is what matters: the
provider revises its guess until it is confident, then commits.

The **final** is the provider's commitment. Anything downstream
(your agent, your logic, your database write) should wait for the
final. **Never act on a partial.**

This rule matters. A naive agent that pre-fetches on partials
commits to a guess that may evaporate two partials later, wasting
LLM tokens and producing audio for a sentence the user didn't
actually say. This is the single most common source of "my voice
bot is weirdly confident about things I didn't say." Chapter 6
reinforces the rule when it wires the agent in.

## Why streaming exists

If batch works, why bother? Two reasons:

1. **Lower perceived latency.** Batch waits for the user to stop
   speaking *and then* starts transcribing. Streaming begins the
   moment audio arrives. With a real-time provider, partials
   appear within ~150-300ms of their audio.
2. **Earlier signal for downstream stages.** Turn-end detection,
   smart-turn priming, and barge-in all want a running guess of
   what the user is saying before they stop.

## Your first journal

Both scripts write a `RunBundle` to `runs/`. Open one:

```python
from easycat.debug.testing import load_bundle
b = load_bundle("docs/teaching/02-transcribe/runs/<file>.bundle")
for rec in b.records():
    print(rec["sequence"], rec["name"], rec["data"])
```

You will see one record per partial and per final. Every record
has a sequence number, a monotonic-clock timestamp, and a name
(`stt.partial`, `stt.final`). This is the substrate that
[chapter 11](../11-journal/) teaches in full.

> **One honesty note up front.** Chapters 2-10 emit *composite*
> journal events of the form `stage.<name>.execute` with a single
> `elapsed_ms` field. The production journal in
> `src/easycat/runtime/` instead emits **paired** records
> (`stage_start` + `stage_complete`) that you match on `op_id`.
> The teaching shape keeps the query layer at the surface; the
> paired shape buys you partial-span visibility on crashes.
> Chapter 11 surfaces this difference explicitly — don't be
> surprised when you meet it there.

For now: the journal is the single source of truth for "what just
happened," and every runnable chapter from here on will dump one.

## Try breaking it

Say a word the STT consistently mishears ("bass" vs "base",
"pear" vs "pair"). Re-run `streaming.py`, then read the bundle
and find the exact partial where the wrong guess stuck. Compare
that to the final. Did the revision save it, or did the provider
commit to the wrong word?

## What's next

[Chapter 3 — Parrot, the naive way](../03-parrot-naive/) glues STT
to TTS with the most obvious possible turn detector — a fixed
silence timeout — and watches it break.
