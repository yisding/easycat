# Chapter 6 — Streaming Agent + Sentence TTS

> Start speaking before the LLM is done thinking. First real
> pipeline overlap.

## Prerequisites

- [Chapter 5](../05-blocking-agent/) and its bundles — we will
  diff against them.
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`.

## Run it

```bash
uv run python docs/teaching/06-streaming-agent/main.py
```

Ask the same question you asked chapter 5. The first syllable
arrives *seconds* earlier.

## The sentence is the right unit

You have three choices for when to hand text to TTS:

| Unit | First-audio latency | Prosody |
|---|---|---|
| **Token** | Near-zero | Terrible — each word is its own breath |
| **Sentence** | ~1× sentence duration | Natural, matches what TTS was trained on |
| **Paragraph** | Back to chapter 5 | Fine, but we defeated the point |

Goldilocks: the **sentence**. Short enough to start speaking fast,
long enough to sound like a human who thought before opening
their mouth.

## Architecture

```
                  tokens              sentences              audio
  ┌─────┐  stream   ┌──────────┐  queue   ┌─────────┐   ┌────────┐
  │ LLM │──────────►│ sentence │─────────►│  TTS    │──►│ Spkr   │
  └─────┘           │ splitter │          │ drain   │   └────────┘
                    └──────────┘          └─────────┘
                         │                     ▲
                         │  ←── concurrent ──  │
                         ▼                     │
                   (split next token) ── (synth next sentence)
```

Two coroutines. The splitter accumulates tokens and calls
`split_at_sentence_boundaries(buffer)` after every delta. When
pySBD finds a complete sentence prefix, it's pushed to an
`asyncio.Queue`. The drain coroutine pulls sentences and streams
TTS audio to the transport. Because `transport.send_audio`
returns as soon as the chunk is enqueued on the speaker, sentence
N+1 can begin synthesising while sentence N is **still playing**
from the speaker queue. (Only one TTS synth runs at a time — but
playback and the next synth overlap, and so does the next token
arriving at the splitter.)

## The toy vs. the production version

About 40 lines for `stream_sentences_to_tts`, another 20 for the
drain coroutine. EasyCat's real implementation lives in
`src/easycat/session/_streaming.py::consume_agent_stream`. Read
it once. It takes nine parameters: `CancelToken` (for chapter 9),
`TurnContext` (per-turn timing), `emit` (EasyCat event bus),
`prepare_tts_payload` (custom envelopes), `strip_md`, `voice`,
and more. Every parameter is defending against something the toy
ducks. When you can look at a parameter and name the scenario —
"ah, `CancelToken` is there so `await cancel_token.check()`
inside the stream loop can abort a reply mid-sentence on
barge-in" — you understand the production code.

## Measure the win

Same bundle format as chapter 5. Compare first-audio latency on
the same prompt:

```python
from pathlib import Path
from easycat.debug.testing import load_bundle

def first_audio_gap_ms(bundle_path):
    b = load_bundle(bundle_path)
    stt_t = next(
        (r["data"]["t_ms"] for r in b.records() if r["name"] == "stt.final"),
        None,
    )
    tts_t = next(
        (r["data"]["t_ms"] for r in b.records() if r["name"] == "tts.first_audio"),
        None,
    )
    return None if stt_t is None or tts_t is None else tts_t - stt_t

for b in Path("docs/teaching/06-streaming-agent/runs/").glob("*.bundle"):
    print(b.name, f"first-audio gap = {first_audio_gap_ms(b):.0f} ms")
```

On a typical 3-sentence reply with `gpt-4o-mini`, expect
first-audio to drop from ~3000 ms (blocking) to ~800-1200 ms
(streaming) — roughly 3×.

## Sidebar — speech-friendly output

Three things bite every voice agent the instant it ships:

1. **Markdown.** The agent says `**bold**`. Without stripping,
   TTS literally reads *"asterisks bold asterisks."* We apply
   `easycat.strip_markdown.strip_markdown` to every sentence
   before enqueuing it. Try removing that call and hear the
   damage.
2. **Numbers and dates.** `2024` reads as "twenty twenty-four",
   "two thousand twenty-four", "two oh twenty-four"… the
   provider picks one, and it's often wrong for your domain.
   Production uses `easycat.llm_output_processing` with
   `PhoneticReplacementProcessor` for fixed corrections.
3. **SSML.** `TTSInput(text=..., format="ssml")` accepts
   `<break time="500ms"/>` and `<phoneme>` tags when the
   provider supports SSML. Useful for phone numbers ("1-800-..."),
   acronyms, and deliberate pauses. Use sparingly — prosody is
   brittle across vendors.

## Sidebar — backpressure

Our `asyncio.Queue` is unbounded. If the agent streams faster
than TTS+playback drains it, the queue grows without limit —
fine for short exchanges, a slow leak in a long-running session.
Production uses `easycat._bounded_queue.BoundedAudioQueue` with a
`DropPolicy`:

- `DROP_OLDEST` — shed stale audio first. Good for live
  conversation.
- `DROP_NEWEST` — refuse new audio until the queue drains. Good
  for transactional flows.
- `BLOCK` — apply backpressure to the producer. Safest, but if
  the producer can't slow down (an LLM stream doesn't negotiate),
  it stalls.

## Sidebar — partials can flap (reprise)

Chapter 2 named the rule: agents fire on `STTFinal`, never on
`STTPartial`. This is the chapter where it bites: we are finally
wiring the agent in. `run_turn` only drains `STTEventType.FINAL`
from the STT event stream. A naïve implementation that kicked
off `stream_sentences_to_tts` on a partial would commit — in
audio, audibly — to a guess the provider may have revised away
by the time the final arrived.

## Try breaking it

Add `MODEL = "gpt-4o"` (bigger, slower). Re-run. The per-sentence
synth stays overlapping, but the *first* sentence now takes
longer to complete because the first token arrives later. The
`agent.first_token → tts.first_audio` span in the journal grows;
everything downstream stays overlapping. This isolates which
knob buys you what.

## What's next

[Chapter 7 — Tools, mid-stream](../07-tools/) adds tool calls
into the same streaming surface. A tool call is a new kind of
sentence boundary — one that triggers work instead of speech.
