# Chapter 5 — The Blocking Agent

> Swap the parrot for an LLM. The bot falls silent for three
> seconds. This is on purpose.

**Wrong-version-first chapter.** Do not skip. The rest of the
build movement (chapters 6-9) exists to close this gap.

## Prerequisites

- [Chapter 4](../04-vad-preroll/)
- `OPENAI_API_KEY` (LLM + TTS) and `DEEPGRAM_API_KEY` (STT)

> **Minimum to skip the ladder:** chapter 4 alone (VAD-gated
> turns). You can read this chapter without chapter 3's
> wrong-version parrot.

## Diff from chapter 4

- **Added:** an `AsyncOpenAI` client + `blocking_agent` function
  between STT and TTS; three `turn.gap` sub-spans
  (`stt_to_agent_ms`, `agent_ms`, `tts_ms`) journaled per turn.
- **Removed:** the parrot — the bot now answers, instead of
  repeating.

## Run it

```bash
uv run python docs/teaching/05-blocking-agent/main.py
```

Ask it something simple: *"What is the capital of France?"* Keep
your stopwatch handy. There will be a long, empty silence between
your voice and the bot's.

## The obvious architecture

```
  ┌─────┐   ┌─────┐   ┌─────┐   ┌─────┐   ┌─────┐   ┌─────┐
  │ Mic │──►│ VAD │──►│ STT │──►│ LLM │──►│ TTS │──►│ Spkr│
  └─────┘   └─────┘   └─────┘   └─────┘   └─────┘   └─────┘
                                    ▲
                                    └── blocks here for 2-4 seconds
```

The `blocking_agent` function on line ~78 of `main.py` is eight
lines:

```python
async def blocking_agent(client, user_text):
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[..., {"role": "user", "content": user_text}],
    )
    return resp.choices[0].message.content or ""
```

It is also unshippable. This is what most naïve voice demos do.

> **Pointer.** In production, EasyCat wraps an agent like this
> behind the `Agent` protocol and `AgentRunner` (timeouts,
> cancellation, history). We are staying one level below on
> purpose — the point here is "agent is just an async function
> from text to text." [Chapter 7](../07-tools/) introduces tools
> via the real `Agent` surface.

## Decompose the gap

The journal records three sub-spans between STT-final and the bot
speaking. Open the bundle and list them:

```python
from pathlib import Path
from easycat.debug.testing import load_bundle
b = next(iter(Path("docs/teaching/05-blocking-agent/runs/").glob("*.bundle")))
bundle = load_bundle(b)
for r in bundle.records():
    if r["name"] == "turn.gap":
        d = r["data"]
        print(f"  STT final → agent dispatch  {d['stt_to_agent_ms']:6.1f} ms")
        print(f"  agent (LLM call)            {d['agent_ms']:6.1f} ms")
        print(f"  TTS synth + first audio     {d['tts_ms']:6.1f} ms")
        print(f"  TOTAL                       {d['total_gap_ms']:6.1f} ms")
```

You will see something like:

```
  STT final → agent dispatch     0.4 ms
  agent (LLM call)            2134.0 ms
  TTS synth + first audio      812.0 ms
  TOTAL                       2946.4 ms
```

Three sub-gaps, in order:

1. **STT final → agent dispatch** (~0-50 ms): just your own code
   overhead. You can't optimise this; it doesn't matter.
2. **Agent / LLM call** (~1-4 s): most of the silence. The
   dominant term. Model choice dominates.
3. **Agent response → first TTS audio** (~300-800 ms): not
   trivial either. Synth + network + first-chunk playback.

Total `turn.gap` is what the user *feels*. Humans turn-take in
100-300 ms. We are an order of magnitude worse. That is why
voice LLM products feel off when they do.

## The two axes we'll attack

We can't make the LLM faster — but we can start TTS sooner and
start the LLM sooner:

| Chapter | What we attack |
|---|---|
| [6](../06-streaming-agent/) | Stream the LLM's tokens; start TTS on the first sentence instead of the last. The `tts.execute` span moves to overlap the `agent.execute` span. |
| [8](../08-smart-turn/) | Predict end-of-turn earlier than VAD silence. The `turn.gap` starts sooner. |

## Try breaking it

Three experiments, all quick:

1. Change `MODEL = "gpt-4o-mini"` to `"gpt-4o"`. Re-run the same
   question. Which span grew?
2. Add a system prompt: *"Answer in one word."* Total gap drops.
   Which span shrank — and which did *not*?
3. Insert `await asyncio.sleep(0.5)` inside `blocking_agent`. Watch
   `stage.agent.execute` grow by ~500 ms while the others stay
   put.

## Success criteria

You should be able to name the three sub-gaps in order without
looking them up, and you should actively *want* the streaming
version. If you don't yet: try the "uh, what was I going to say"
flow where a user asks a 3-second question and waits 3 seconds
for an answer. Six seconds of one human standing in the room
holding their breath.

## What's next

[Chapter 6 — Streaming agent + sentence TTS](../06-streaming-agent/)
cuts the gap by roughly 3× on the same inputs, by overlapping the
LLM and TTS spans.
