# Chapter 6 — Exercises

## 1. Isolate which knob buys you what

**Task.** Change `MODEL = "gpt-4o-mini"` to `"gpt-4o"`. Re-run.
Compare `agent.first_token → tts.first_audio` (the time to *start*
speaking) and the per-sentence TTS spans.

**Hints**

1. The per-sentence TTS spans stay overlapping with subsequent
   agent tokens — that overlap is the chapter's whole win, and
   it survives a slower agent.
2. The *first* sentence now takes longer to complete because the
   first token arrives later from the slower model. The pipeline
   still overlaps; it just starts overlapping later.
3. The point: streaming is a multiplier on whatever model you
   choose. Faster model = faster start; either way you overlap.

## 2. Break markdown stripping deliberately

**Task.** Remove the `strip_markdown(ready)` call so the raw
markdown reaches TTS. Ask the bot for a *bulleted list of three
things*. Listen.

**Hints**

1. You will hear *"asterisk asterisk bold asterisk asterisk"* or
   *"hyphen item one"*. This is the single most common voice-bot
   shipping bug.
2. The agent's history (`messages`) still contains the original
   markdown text — only the TTS pipe gets stripped. Why does the
   chapter wire it this way? (Because the LLM next turn benefits
   from the structured prior; the user does not.)
3. Production wires this through
   `easycat.llm_output_processing.MarkdownStripProcessor` (chapter
   14) — exact same logic, plumbed through `output_processors`.

## 3. Make the unbounded queue bite

**Task.** Have the bot answer a long question ("explain the entire
history of Rome in detail") on a slow speaker — easiest way:
plug in Bluetooth headphones. Watch the per-sentence latency drift
over the answer.

**Hints**

1. `transport.send_audio` returns as soon as the chunk is
   *queued*, not when it plays. Sentence N+1 finishes synth long
   before sentence N finishes playing.
2. Memory usage of the speaker queue rises linearly during the
   answer. Production uses `BoundedAudioQueue` with `DROP_OLDEST`
   to keep this in check during long sessions; the teaching
   version doesn't.
3. This is exactly the failure mode chapter 9c's interruption
   estimator runs into: "what's in the queue" ≠ "what the user
   heard" because the queue holds future audio.

## Self-check

You should be able to: (a) draw the architecture diagram from
memory, (b) explain why sentences (not tokens, not paragraphs) are
the right unit, and (c) point at the production
`consume_agent_stream` and name one parameter without re-reading
the README.
