# Chapter 9 — Interruption / Barge-in

> Three versions of the same feature. Each one better. Each one
> teaching something the previous one didn't.

**Wrong-version-first, in triplicate.** Read them in order.

## Prerequisites

- [Chapter 8](../08-smart-turn/)
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`
- **Use headphones.** If you run this on a speaker+mic laptop,
  the bot will interrupt itself every time it hears its own
  voice. Chapter 10 fixes that with AEC; this chapter punts.

## The three scripts

```bash
uv run python docs/teaching/09-interruption/ignore.py    # A: answering-machine
uv run python docs/teaching/09-interruption/cancel.py    # B: cuts off mid-word
uv run python docs/teaching/09-interruption/estimate.py  # C: cuts off + remembers
```

## A vs B vs C

```
  Step        │  ignore.py   │   cancel.py   │  estimate.py
  ────────────┼──────────────┼───────────────┼───────────────────
  barge-in    │  logged      │  cancels bot  │  cancels bot
  audio       │  bot finishes│  clear_audio  │  clear_audio
  history     │  full reply  │  full reply   │  truncated to heard
  next turn   │  bot rambles │  bot rambles  │  coherent
```

The C column is what a production voice bot gets right.

Ask a long-ish question (*"Tell me about the history of Rome."*).
While the bot is talking, try to interrupt. See what happens.

## Version A — ignore (`ignore.py`)

The bot does not listen while it talks. Or rather, it does — the
mic producer runs at all times — but when VAD fires during bot
speech, the coordinator logs `user.barge_in.ignored` and **takes no
action**. You can recite the Gettysburg address over the bot's
answer and it will not care.

Architecturally, the change vs. chapter 6/8 is real: we split
the pipeline into two coroutines connected by a queue, so the mic
side never pauses while TTS runs. That wiring is what versions B
and C act on.

## Version B — cancel (`cancel.py`)

Introduce `CancelToken` (from `easycat.cancel`) — a cooperative
cancellation primitive. Pipeline stages read `token.is_cancelled`
and stop voluntarily. It is **not** an exception — exceptions
unwind stacks, which would wreck the middle of a streamed reply.

On barge-in:

1. The coordinator calls `cancel.cancel()`.
2. `run_agent` sees `is_cancelled` on the next iteration and
   stops pulling tokens.
3. `drain_to_speaker` sees it and calls `tts.cancel()` to drop
   whatever chunk it was synthesising.
4. `transport.clear_audio()` flushes the speaker queue so the bot
   shuts up **now**, not after the current chunk finishes.

Three places, one token. That's the pattern.

**What this still doesn't solve:** the bot's memory. The LLM
thinks it said its whole reply. Next turn it may reference "as I
mentioned before" — but the user never heard it.

## Version C — estimate (`estimate.py`)

Track two things per turn:

- `sentences_sent` — the text dispatched to TTS, in order.
- `bytes_sent` — the audio bytes that reached
  `transport.send_audio`.

OpenAI TTS emits PCM16 mono at 24 kHz = 48,000 B/s. We estimate
chars-per-byte with a deliberately crude assumption (~15 chars/s
of natural speech), multiply, and truncate the full text to that
character index. Then we rewrite the conversation history:

```python
history.append({"role": "assistant", "content": heard_text})
```

Next turn, the LLM's memory matches the user's.

## Honesty note — the triggering utterance

When barge-in fires, the coordinator reads the `speech_started` tag
off the mic queue and dispatches to the cancel branch. That tag
is *consumed* — the user's new utterance starts but its start
boundary never reaches STT. Production pipelines buffer the
triggering audio into the next user turn; the toy here throws it
away. On every real barge-in, the first ~200 ms of what you said
is lost. The second mic event after bot-done picks things up
normally. Exercise 1 nudges you to notice this.

## Why "bytes sent" ≠ "bytes heard"

Three reasons, all real:

1. **OS playback buffer.** `transport.send_audio` enqueues chunks
   on PortAudio. PortAudio holds ~10-100 ms before the speaker
   driver. `clear_audio()` drops those — so "bytes sent" overcounts
   by however much was in the PA buffer.
2. **Markdown + SSML.** `strip_markdown(text)` is shorter than the
   raw LLM output. TTS synthesises the stripped version. Character
   counts drift.
3. **Variable speech rate.** Our 15-chars/s constant is an
   average. "Hello" is slower than "uhh".

Production `easycat.session.interruption` has a 200-line estimator
that handles all three plus playback-ack marks. The toy here is a
single-line formula — accurate enough that the bot's next turn
doesn't claim it said things the user didn't hear. Read the
production version once you understand why each correction exists.

## Read the bundles

```python
from pathlib import Path
from easycat.debug.testing import load_bundle

for b in Path("docs/teaching/09-interruption/runs/").glob("*.bundle"):
    bundle = load_bundle(b)
    print(b.name)
    for r in bundle.records():
        if r["name"].startswith(("interruption.", "user.barge_in")):
            print("  ", r["name"], r["data"])
```

Expect:

- `ignore.py` bundle: only `user.barge_in.ignored` records.
- `cancel.py` bundle: `interruption.start` at barge-in time.
- `estimate.py` bundle: `interruption.estimate` with
  `{full_text, heard_text, bytes_heard}`.

## Try breaking it

1. Run `estimate.py`. Interrupt exactly after one word. Open the
   bundle — does `heard_text` end at that word, or does it over- or
   under-shoot?
2. Have the agent reply with markdown-heavy output (ask it for a
   table). The stripped text fed to TTS is shorter than the
   original. How does this affect `heard_text` vs reality?
3. Run on speakerphone (no headphones). The bot interrupts
   itself. Why does AEC fix this, and why is VAD alone not enough?
   (Preview of chapter 10.)

## What's next

[Chapter 10 — Cleaning the signal](../10-cleaning-signal/). We
close the loop: noise reduction in front of VAD, echo cancellation
so the bot stops hearing itself, and why the pipeline order
matters.
