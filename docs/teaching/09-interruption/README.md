# Chapter 9 — Interruption / Barge-in

<!-- BEGIN auto:navigation -->
**Progress: 10 of 16** · [← Chapter 8 — Smart-turn](../08-smart-turn/) · [Ladder index](../) · [Exercises](./EXERCISES.md) · [Chapter 10 — Cleaning the Signal →](../10-cleaning-signal/)
<!-- END auto:navigation -->

> Three versions of the same feature. Each one better. Each one
> teaching something the previous one didn't.

**Wrong-version-first, in triplicate.** Read them in order.

## Prerequisites

- [Chapter 8](../08-smart-turn/)
- `uv sync --extra quickstart --extra deepgram --group dev`
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`
- Running this chapter makes live provider calls that may incur charges.
  Review your provider billing and usage limits first.
- Provider-backed scripts may send audio, transcripts, or prompts to configured
  services. Use non-sensitive test content and review provider data-handling
  policies first.
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.
- **Use headphones.** If you run this on a speaker+mic laptop,
  the bot will interrupt itself every time it hears its own
  voice. Chapter 10 fixes that with AEC; this chapter punts.

> **Minimum to skip the ladder:** chapter 6 (the streaming-agent
> surface). Barge-in is independent of tools (ch 7) and smart-turn
> (ch 8) — drop it on any streaming pipeline.

## Diff from chapter 8

- **Added:** three separate scripts (`ignore.py`, `cancel.py`,
  `estimate.py`); `CancelToken` from `easycat.cancel`;
  `transport.clear_audio()` calls and cancellation-latency records;
  a `bytes_accepted` / sentence ledger in `estimate.py` plus an interruption-estimate formula
  that rewrites conversation history toward what the user could
  actually have heard.
- **Modified:** the pipeline splits into two coroutines
  (mic-producer + coordinator) connected by a queue, so the mic
  side never pauses while TTS runs.
- **Removed:** smart-turn — to isolate the barge-in concept.

## The three scripts

Start with the chapter's canonical entry point. It delegates to version A,
the deliberately limited baseline:

```bash
uv run python docs/teaching/09-interruption/main.py
```

Then run all three named versions in order:

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
4. `transport.clear_audio()` requests an immediate speaker-queue
   flush instead of waiting for the current queue to drain.
5. The same `speech_started` event falls through to the ordinary STT
   branch, so the words that caused the interruption become the next
   user turn instead of being thrown away.

Three places, one token. That's the pattern.

<!-- BEGIN auto:snippet src=cancel.py symbol=run_agent -->
```python
async def run_agent(client, user_text, sentence_queue, cancel: CancelToken):
    """Consume the agent stream until cancelled."""
    stream = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful voice assistant. "
                    "Give a long-ish answer so the reader has something to interrupt."
                ),
            },
            {"role": "user", "content": user_text},
        ],
        stream=True,
    )
    buffer = ""
    async for chunk in stream:
        if cancel.is_cancelled:
            break
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        buffer += delta
        ready, buffer = split_at_sentence_boundaries(buffer)
        if ready.strip():
            spoken = strip_markdown(ready).strip()
            if spoken:
                await sentence_queue.put(spoken)
    if buffer.strip() and not cancel.is_cancelled:
        spoken = strip_markdown(buffer).strip()
        if spoken:
            await sentence_queue.put(spoken)
    await sentence_queue.put(None)
```
<!-- END auto:snippet -->

### Software cancellation is measurable

“Immediate” needs a clock. Versions B and C write
`interruption.start`, then `interruption.cancel_complete` after both
software owners have settled. The completion record preserves:

- `cancel_to_clear_audio_return_ms` — trigger to `clear_audio()` returning.
- `cancel_to_bot_task_return_ms` — trigger to the cooperative bot task exiting.

Run the deterministic provider-free probe:

```bash
uv run python docs/teaching/09-interruption/cancel_latency_probe.py
```

Its scripted transport returns from queue clearing at 30 ms, and the
bot task exits at 80 ms. That proves ordering and software control
latency. It does not prove acoustic silence: transport/device buffers
or sound already in the room can remain after `clear_audio()` returns.
Playback progress evidence later in this chapter answers a different,
stronger question.

**What this still doesn't solve:** the bot's memory. The LLM
thinks it said its whole reply. Next turn it may reference "as I
mentioned before" — but the user never heard it.

## Version C — estimate (`estimate.py`)

Track two things per turn:

- `sentences_sent` — the text dispatched to TTS, in order.
- `bytes_accepted` — audio bytes for which
  `transport.send_audio` returned `True`. Rejected/dropped chunks
  never enter the estimate.

OpenAI TTS emits PCM16 mono at 24 kHz = 48,000 B/s. We estimate
chars-per-byte with a deliberately crude assumption (~15 chars/s
of natural speech), multiply, and truncate the full text to that
character index. Then we rewrite the conversation history:

```python
history.append({"role": "assistant", "content": heard_text})
```

Next turn, the LLM's memory is closer to the user's.

<!-- BEGIN auto:snippet src=estimate.py symbol=TurnLedger -->
```python
@dataclass
class TurnLedger:
    """Per-turn record of what the bot tried to say vs. what was accepted.

    ``sentences_sent`` accumulates the text of each sentence dispatched
    to TTS in order. ``bytes_accepted`` tracks audio bytes for which
    ``transport.send_audio`` returned ``True``. At cancel time we combine
    them to estimate where, in the concatenated text, the user's ear
    fell silent.
    """

    sentences_sent: list[str] = field(default_factory=list)
    bytes_accepted: int = 0

    def heard_text(self) -> str:
        """Estimate the text prefix the user's ear actually reached.

        Audio bytes map directly to playback duration (OpenAI TTS
        emits a fixed-rate stream). Convert duration to characters
        via the expected full-text byte count; clamp to the real
        length so a complete turn returns the whole string.
        """
        if not self.sentences_sent:
            return ""
        full_text = " ".join(self.sentences_sent)
        expected = max(1, _expected_bytes(full_text))
        estimated_chars = int(len(full_text) * self.bytes_accepted / expected)
        estimated_chars = max(0, min(estimated_chars, len(full_text)))
        return full_text[:estimated_chars]
```
<!-- END auto:snippet -->

## Preserving the triggering utterance

The cancel branch must not consume the event that proves the user
started talking. Versions B and C settle the cooperative bot task and
return `consumed=False`; the coordinator then sends that same
`speech_started` event through its ordinary STT branch. While it waits
for the bot task to unwind, the independent mic producer keeps placing
pre-roll and live frames on `mic_queue`. Once STT starts, those queued
frames follow the preserved boundary in their original order.

Version A intentionally does the opposite: `ignore.py` consumes every
mic event while the bot is active. That is the answering-machine
behavior the comparison is meant to expose.

Run the provider-free probe, which calls `cancel.py`'s real barge-in
router:

```bash
uv run python docs/teaching/09-interruption/barge_in_turn_probe.py
```

Look for `event_consumed: false`, followed by `stt.start`, `stt.frame`,
`stt.end`, and `stt.close`. The remaining toy limitation is now stated
accurately: the coordinator awaits cooperative bot shutdown before it
starts STT, so an unresponsive agent can make the unbounded mic queue
grow. Production uses bounded audio buffers and stronger cancellation
deadlines; it does not silently discard the triggering turn.

There are two shutdown owners here. The coordinator owns its active
per-turn STT and background bot task, so its `finally` arm ends/closes
STT and cancels/observes bot work. The outer `AsyncExitStack` owns the
long-lived TTS, client, VAD, and transport and closes them only after
the coordinator has stopped using them.

## Why "bytes accepted" ≠ "bytes heard"

Three reasons, all real:

1. **Playback queues.** A `True` return from `transport.send_audio`
   means the transport accepted the chunk; it does not mean the user
   heard it. `LocalTransport` and PortAudio can still hold queued
   audio that `clear_audio()` drops, so `bytes_accepted` overcounts
   by the unplayed backlog.
2. **Markdown + SSML.** `strip_markdown(text)` is shorter than the
   raw LLM output. TTS synthesises the stripped version. Character
   counts drift.
3. **Variable speech rate.** Our 15-chars/s constant is an
   average. "Hello" is slower than "uhh".

Production `easycat.session.interruption` has a more careful estimator
that handles all three and combines the strongest progress evidence a
transport exposes. Run the provider-free capability probe:

```bash
uv run python docs/teaching/09-interruption/playback_evidence.py
```

Local playback and WebRTC report delivered chunks through
`TransportAudioDelivered`; Twilio supports explicit marks acknowledged
as `PlaybackMarkAck`; transports with neither use a serial-playout
estimate from the send log. These milestones constrain queued backlog,
but none proves sound reached a human ear. The toy remains a single-line
formula: excluding rejected chunks prevents invented audio, while its
queue/rate errors stay visible for the exercise.

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
- `cancel.py` bundle: `interruption.start`, followed by
  `interruption.cancel_complete` with clear-audio and bot-task return
  latency.
- `estimate.py` bundle: `interruption.estimate` with
  `{full_text, heard_text, bytes_accepted}`.

## Try breaking it

1. Run `estimate.py`. Interrupt as close as you can after hearing one
   word, and repeat several times because human reaction time is not an
   exact clock. In each bundle, does `heard_text` end at that word, or
   does it over- or under-shoot?
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
