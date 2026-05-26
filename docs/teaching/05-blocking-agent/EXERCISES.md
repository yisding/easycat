# Chapter 5 — Exercises

## 1. Three controlled experiments on the same question

**Task.** Ask the bot *"What is the capital of France?"* under
each condition below, and for each one record which of the three
`turn.gap` sub-spans changed:

| Change                                         | Which span changes? | By how much? |
|------------------------------------------------|---------------------|--------------|
| `MODEL = "gpt-4o-mini"` → `"gpt-4o"`           | ?                   | ?            |
| Add system prompt: *"Answer in one word."*     | ?                   | ?            |
| Insert `await asyncio.sleep(0.5)` inside agent | ?                   | ?            |

**Hints**

1. Switching to `gpt-4o` mostly affects the `agent_ms` span — same
   prompt, slower model. The other two stay put.
2. The one-word system prompt shrinks `tts_ms` (less text to
   synthesise) and also shrinks `agent_ms` slightly (fewer tokens
   to generate). `stt_to_agent_ms` is unchanged.
3. `asyncio.sleep(0.5)` adds exactly 500 ms to `agent_ms`. Use
   this to verify your understanding of which code lives in which
   span.

## 2. The "uh, what was I going to say" exercise

**Task.** Have a friend (or yourself) ask the bot a 3-second
question, then sit silently for the latency gap, then naturally
continue the conversation. Time the full transaction with a
stopwatch.

Now write down: **how many seconds was the human standing in the
room holding their breath?**

**Hints**

1. 3 s question + ~3 s `turn.gap` + however long the bot speaks =
   6+ seconds of awkward silence per turn.
2. Voice users budget around 100-300 ms for turn-taking. We're an
   order of magnitude over budget — that's why this feels so off.
3. There's no software fix here. The fix is structural: don't make
   the user wait for the entire `agent.complete` event before
   starting to speak. That's chapter 6.

## 3. Decompose somebody else's gap

**Task.** Find a voice product you use (any vendor). Time the gap
between when you stop talking and when it starts talking. Try to
attribute the time to the three sub-spans:

- Stop-talking → STT-final (this is the smart-turn signal of
  chapter 8)
- STT-final → first audio (this is `agent_ms` + `tts_ms` from
  this chapter)
- First audio → end-of-greeting (just speech duration; not in
  scope)

**Hints**

1. The first sub-span is hard to measure from the outside (you
   don't see the STT final). A rough proxy: the moment the
   wave-form indicator (if present) drops.
2. Good products run the gap at 600-900 ms total. Bad ones 2-4 s.
   Excellent ones (OpenAI Realtime API, smart-turn-equipped
   pipelines) target 300-500 ms.

## Self-check

You should be able to name the three sub-gaps in order without
looking them up, predict which one each chapter-6/8 fix attacks,
and have visceral evidence for why both fixes matter.
