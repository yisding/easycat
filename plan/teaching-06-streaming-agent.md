# Chapter 6 — Streaming Agent + Sentence-boundary TTS

> Start speaking before the LLM is done thinking. First real
> pipeline overlap.

## Prerequisites

- Chapter 5 (and its bundles — we will compare against them)

## Learning objectives

1. Consume an async agent stream token-by-token without losing the
   mental model.
2. Explain why **sentence boundaries** are the right chunking unit
   for TTS (not tokens, not paragraphs).
3. Build a minimal sentence-streaming consumer using the library's
   promoted `split_at_sentence_boundaries` helper, and then read
   the production `consume_agent_stream` to understand what the
   teaching version leaves out.
4. Diagnose first-audio latency by reading a journal.

## What you build

`docs/teaching/06-streaming-agent/main.py`:

- Starts from a copy of `docs/teaching/05-blocking-agent/main.py`.
- Adds a small `stream_sentences_to_tts(agent, user_text, tts_queue)`
  coroutine — ~40 lines — that iterates the agent's async stream,
  accumulates tokens into a buffer, calls
  `easycat.session.split_at_sentence_boundaries(buffer)` after each
  token, and pushes ready sentences onto a TTS queue.
- A second coroutine drains the TTS queue, synthesises each
  sentence, and sends the audio to the transport.
- Both coroutines run concurrently so sentence N+1 synthesises
  while sentence N is still playing.
- Same journal schema as chapter 5 for direct comparison.
- Bundles land in `docs/teaching/06-streaming-agent/runs/`.

**Why a toy version?** The library's
`easycat.session.consume_agent_stream` is a battle-tested function,
but its signature takes nine arguments — emit callbacks, TTS-payload
factories, `CancelToken`, `TurnContext` — all of which exist because
it runs *inside* Session. Using it in a standalone chapter would
force the reader to construct scaffolding that has nothing to do
with the sentence-chunking concept this chapter is about.  So we
build the minimum that teaches the idea, then point at the
production version.

## Narrative arc

1. **Try one TTS call per token.** Prosody collapses. Each word
   sounds like a separate beat. Quantify how awful.
2. **Try one TTS call for the whole paragraph.** Back to chapter 5
   latency. We've traveled in a circle.
3. **Goldilocks: the sentence.** Short enough to start fast, long
   enough to sound natural. This is not an arbitrary choice — it
   matches the unit TTS models were trained on.
4. **Build `stream_sentences_to_tts`.** ~40 lines. Reader writes
   it themselves using `split_at_sentence_boundaries` and a small
   `asyncio.Queue`.
5. **Read the production `consume_agent_stream`.** Open
   `src/easycat/session/_streaming.py`. For each extra parameter
   it takes that the toy doesn't — `CancelToken`, `emit`,
   `prepare_tts_payload`, `TurnContext`, `strip_md`, etc. — name
   the scenario it is guarding against (interruption mid-stream;
   custom payload envelope; markdown in agent output).
6. **Walk the sentence splitter.** Open
   `src/easycat/session/text_utils.py` and read
   `split_at_sentence_boundaries` end-to-end. Note it's a
   ~15-line pySBD wrapper, not a 500-line NLP module. Why: latency
   and the fact that perfect splitting doesn't matter when TTS
   prosody forgives most seams.
7. **Journal comparison.** Side-by-side chapter 5 and chapter 6
   timelines. First-audio latency should drop by ~3× on most
   prompts.

## Key concepts

- `easycat.session.split_at_sentence_boundaries` — the promoted
  helper the teaching consumer calls
- `easycat.session.has_unclosed_markdown_delimiters` — the "wait
  for the close-backtick before flushing" helper (optional in the
  toy version; required once you feed markdown-heavy agent output)
- `src/easycat/session/_streaming.py::consume_agent_stream` — read
  in this chapter as reference material, not imported
- `easycat.strip_markdown.strip_markdown` — why `say("**bold**")`
  sounds wrong without stripping (see `session/tts_helpers.py`)
- TTS task parallelism — the next sentence synthesises while the
  current one plays

## Exercises

1. Force single-token TTS. Listen to how bad it is. Compare
   first-audio latency against sentence-chunked TTS — is it really
   faster? (Usually marginal; prosody cost is huge.)
2. Modify your copy of `split_at_sentence_boundaries` to only split
   on `.` (not `!`, `?`, `;`). Find a user prompt where this
   sounds wrong.
3. Time "STT-final → first-TTS-audio" across chapters 5 and 6 on
   the same recording. The journal makes this trivial. Report the
   ratio.
4. Read `consume_agent_stream` and list three bugs your toy would
   hit that the production function doesn't.

## Journal highlights

- Multiple parallel `stage.tts.execute` spans, one per sentence
- First TTS audio timestamp relative to first agent token
- Sentence-boundary events interleaved with agent-token events
- Compare against chapter 5: the `agent → tts` span pipeline'd
  away rather than serialised

## Files created

- `docs/teaching/06-streaming-agent/main.py` (~120 lines including
  `stream_sentences_to_tts` + the drain coroutine)
- `docs/teaching/06-streaming-agent/README.md`
- `docs/teaching/06-streaming-agent/latency_comparison.md`
  (fill-in-the-table exercise; auto-populated from chapter 5 + 6
  bundles when the reader runs the script)

## Success criteria

- The reader has measurably cut first-audio latency by >2× vs
  chapter 5 on the same prompts.
- The reader understands why sentences (not tokens, not paragraphs)
  are the TTS unit — and can defend the choice.
- The reader can name three responsibilities `consume_agent_stream`
  handles that their toy ducks.

## Sidebar — Speech-friendly output

Three things that bite every voice agent the moment it shells out
to a real LLM. Cover them before chapter 7 lands tools:

- **Markdown stripping.** The agent says `**bold**`; without
  stripping, TTS reads "asterisks bold asterisks." We already use
  `easycat.strip_markdown` in this chapter; show the before/after
  audibly.
- **Number and date normalisation.** `2024` reads four ways:
  "twenty twenty-four", "two thousand twenty-four", "two oh
  twenty-four", "two zero two four." The TTS provider picks one,
  and it's often wrong. Mention `LLMOutputProcessor` /
  `PhoneticReplacementProcessor` in `easycat.llm_output_processing`
  for fixed corrections.
- **SSML for fine control.** `TTSInput(text=..., format="ssml")`
  accepts `<break time="500ms"/>` and `<phoneme>` tags. Use it
  sparingly — most providers support a subset and prosody is
  brittle across vendors.

## Sidebar — Backpressure when the TTS queue grows

When the agent streams faster than the TTS+playback can drain
(common with a fast model and a slow voice), the queue grows
unboundedly. Production uses `easycat.bounded_queue.BoundedAudioQueue`
with a `DropPolicy`:

- `DropPolicy.OLDEST` — drop stale audio first. Good for live
  conversation: the user wants the latest, not the backlog.
- `DropPolicy.NEWEST` — refuse new audio until queue drains. Good
  for transactional flows where every word matters.
- `DropPolicy.BLOCK` — apply backpressure to the producer. Safest,
  but if the producer can't slow down (e.g., LLM stream), it stalls.

In the toy code we use an unbounded `asyncio.Queue` and ignore
this. Name it explicitly so the reader knows the choice exists.

## Sidebar — Partials can flap; never act on them

Chapter 2 introduced partials but didn't name the trap: STT
partials can revise themselves, sometimes dramatically. A naive
agent that fires on a partial commits to a guess that may turn out
wrong. The rule: agents fire on `STTFinal`, never on `STTPartial`.
The journal in this chapter shows several partials per turn —
inspect them and convince yourself.

## Links forward

Chapter 7 takes a sharp left turn: real agents don't just talk,
they *call tools*. Wiring a tool call into a streaming voice
pipeline raises questions the chat-only world doesn't have.
