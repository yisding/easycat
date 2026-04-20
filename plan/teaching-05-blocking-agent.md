# Chapter 5 — The Blocking Agent

> Swap parrot for an LLM. The bot falls silent for three seconds.
> This is on purpose.

**"Wrong version first"** chapter. Do not skip.

## Prerequisites

- Chapter 4
- An OpenAI API key (or any LLM reachable via `integrations/agents/`)

## Learning objectives

1. Integrate a non-streaming LLM response into the voice pipeline.
2. Measure the "STT-final → first-TTS-audio" gap and understand
   every millisecond in it.
3. Form the gut intuition that **blocking agents are unshippable**
   in voice. The rest of the ladder exists to close this gap.

## What you build

`docs/teaching/05-blocking-agent/main.py`:

- Starts from a copy of `docs/teaching/04-vad-preroll/main.py`.
- Same pipeline as chapter 4 up through STT + turn-taking.
- On `USER_PAUSED`, call `await agent.run(user_text)` — fully
  blocking, returns the complete response.
- Feed the complete response into TTS as one synthesis job.
- Journal dumps the full timeline to
  `docs/teaching/05-blocking-agent/runs/`.

## Narrative arc

1. **The obvious architecture.** Of course you wait for the LLM to
   finish, then speak. That's how HTTP request/response works.
2. **Run it.** Silence. For 2-4 seconds. The bot feels broken even
   though every stage succeeded.
3. **Look at the journal.** The gap decomposes into three spans:
   - STT final → agent request start (~50ms, fine)
   - Agent request → agent response (~2000-4000ms, most of the pain)
   - Agent response → first TTS audio (~300-600ms, not trivial
     either)
4. **Human baseline.** Human turn-taking gap is 100-300ms. We are
   an order of magnitude worse. This is why voice LLM products
   feel off when they do.
5. **Two axes we'll attack.** We can start the agent *sooner*
   (smart-turn, chapter 8) and start TTS on *partial* agent output
   (streaming, chapter 6).

## Key concepts

- `Agent` protocol in `src/easycat/session/_types.py`
- `AgentRunner` in `src/easycat/integrations/agents/_agent_runner.py`
- Wall-clock latency vs perceived latency (the bot's first syllable
  is what the user feels, not the total response length)
- Journal's `stage.agent.execute` span timing

## The naive bug, visualized

In the chapter README, draw a horizontal timeline:

```
[user speech]---[stt final]...[         agent thinks          ]...[tts synth]---[bot speaks]
                          ^-- 50ms --^                       ^-- 400ms -->
                          ^------------ 2-4s silence -------------^
```

## Exercises

1. Use a faster model (GPT-4o mini vs o3). How much of the pain
   goes away? Which span shrinks?
2. Add an intentional `await asyncio.sleep(0.5)` inside a mock
   agent. Which journal span grows?
3. Tell the LLM to answer in exactly one word. Total latency drops
   — *why*? (Hint: it's not the agent span that shrinks.)

## Journal highlights

- `stage.agent.execute` span with full request and response text
- A large, visible gap between `STTFinal` and the first
  `stage.tts.execute` record
- Total `turn.elapsed` should be uncomfortable

## Files created

- `docs/teaching/05-blocking-agent/main.py`
- `docs/teaching/05-blocking-agent/README.md`

## Success criteria

- The reader can name the three sub-gaps that make up the
  STT-final-to-TTS-start gap, in order, without looking them up.
- The reader *wants* the streaming version. They are asking for it.

## Links forward

Chapter 6 streams the agent response and cuts first-audio latency
by ~3× on the same inputs — attacking the second sub-gap.
