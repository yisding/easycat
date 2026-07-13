# Chapter 7 — Tools, Mid-stream

<!-- BEGIN auto:navigation -->
[← Chapter 6 — Streaming Agent + Sentence TTS](../06-streaming-agent/) · [Teaching ladder](../) · [Progress](../PROGRESS.md) · [Exercises](./EXERCISES.md) · [Chapter 8 — Smart-turn →](../08-smart-turn/)
<!-- END auto:navigation -->

> The agent pauses to fetch something. The user hears silence — or
> hears "let me check that for you." That choice is the whole
> chapter.

<!-- BEGIN auto:spaced-retrieval -->
## Recall before reading

> **Following the ladder? Spaced retrieval — Chapter 5 — The Blocking Agent**
>
> Close earlier chapters and answer from memory before reading further. If this
> chapter is your starting point, skip this block.
>
> **Answer from memory:**
>
> Which sub-gap dominates first-audio latency, and does full TTS enqueue define when the user
> first hears audio?
>
> After recording your answer, explain one way `blocking first-audio gap` changes how you
> reason about `tool filler delivery`. Keep the first answer visible.
>
> **Check only after answering:**
>
> ```bash
> uv run python docs/teaching/05-blocking-agent/gap_decomposition_probe.py
> ```
>
> Cite one observed field, measurement, or behavior; repair only the part your
> evidence disproved.
<!-- END auto:spaced-retrieval -->

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `tool filler delivery` without a microphone,
> speakers, or provider credentials:
>
> **Predict first:** Does a fast tool need filler; when slow filler is rejected, which audio
> becomes first?
>
> ```bash
> uv run python docs/teaching/07-tools/filler_delivery_probe.py
> ```
>
> **Evidence to find:** fast tools skip filler; rejected filler has zero accepted chunks and
> reply audio comes first.
>
> **Explain the result:** Explain why filler production alone cannot establish what the caller
> heard first.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

## Prerequisites

- [Chapter 6](../06-streaming-agent/)
- `uv sync --extra quickstart --extra deepgram --group dev`
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`.
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapter 6 (the streaming-agent
> surface). Chapters 7-9 are mutually orthogonal — you can read
> any of them after ch 6 in any order.

## Diff from chapter 6

- **Added:** two demo tools (`get_weather`, `set_timer`); a filler
  heuristic in `should_play_filler`; `tool.call.started` /
  `tool.call.result` journal records; sentence-queue items become
  `(kind, text, tool_call_id)` tuples so filler delivery stays
  attributable; `blocking_tool.py` shows what happens *without* a filler.
- **Modified:** the agent loop now handles `delta.tool_calls` and
  runs a second LLM iteration once tool results return.

<!-- BEGIN auto:diff prev=06-streaming-agent src=main.py -->
<details>
<summary>Full unified diff vs <code>06-streaming-agent/main.py</code> (auto-generated)</summary>

```diff
--- docs/teaching/06-streaming-agent/main.py
+++ docs/teaching/07-tools/main.py
@@ -1,11 +1,13 @@
-"""Chapter 6 — Streaming agent + sentence-boundary TTS.
-
-Instead of waiting for the whole LLM response, stream tokens as
-they arrive, split on sentence boundaries, and hand each sentence
-to TTS as soon as it's complete. Sentence N+1 synthesises while
-sentence N is still playing.
-
-First-audio latency drops by ~3× versus chapter 5.
+"""Chapter 7 — Tools, mid-stream.
+
+Same streaming pipeline as chapter 6, plus two demo tools:
+
+- ``get_weather(city)`` — a slow (~1.5s) async tool.
+- ``set_timer(minutes)`` — a fast (~50ms) tool.
+
+The slow one triggers a filler utterance so the user doesn't hear
+a void. The fast one doesn't — fillers only help when the gap
+would otherwise feel broken.
 
 Dependencies:
     uv sync --extra quickstart --extra deepgram --group dev
@@ -21,7 +23,9 @@
 
 import asyncio
 import collections
+import json
 import os
+import random
 import time
 import types
 from contextlib import AsyncExitStack
@@ -53,11 +57,56 @@
 PREROLL_FRAMES = 15
 MODEL = "gpt-4o-mini"
 RUNS_DIR = Path(__file__).parent / "runs"
-SESSION_ID = f"ch06-streaming-{int(time.time())}"
+SESSION_ID = f"ch07-tools-{int(time.time())}"
+
+# ── Demo tools ────────────────────────────────────────────────────
+
+EXPECTED_LATENCIES_MS = {"get_weather": 1500, "set_timer": 50}
+
+
+async def get_weather(city: str) -> str:
+    await asyncio.sleep(1.5)
+    return f"The weather in {city} is {random.choice(['sunny', 'cloudy', 'rainy'])} and 17°C."
+
+
+async def set_timer(minutes: int) -> str:
+    await asyncio.sleep(0.05)
+    return f"Timer set for {minutes} minutes."
+
+
+TOOLS = [
+    {
+        "type": "function",
+        "function": {
+            "name": "get_weather",
+            "description": "Get the current weather for a city.",
+            "parameters": {
+                "type": "object",
+                "properties": {"city": {"type": "string"}},
+                "required": ["city"],
+            },
+        },
+    },
+    {
+        "type": "function",
+        "function": {
+            "name": "set_timer",
+            "description": "Set a timer for a number of minutes.",
+            "parameters": {
+                "type": "object",
+                "properties": {"minutes": {"type": "integer"}},
+                "required": ["minutes"],
+            },
+        },
+    },
+]
+
+TOOL_IMPLS = {"get_weather": get_weather, "set_timer": set_timer}
+SentenceItem = tuple[str, str, str | None]
 
 
 class MiniTurnDetector:
-    """Same as chapters 4 & 5."""
+    """Same as chapters 4-6."""
 
     def __init__(self, vad, preroll_frames: int = PREROLL_FRAMES) -> None:
         self._vad = vad
@@ -82,84 +131,162 @@
                 self._preroll.append(chunk)
 
 
-async def stream_sentences_to_tts(
+# ── Filler utterance heuristic ────────────────────────────────────
+
+FILLER_PHRASES = {
+    "get_weather": "Let me check the weather for you.",
+    "set_timer": "",
+}
+
+
+def should_play_filler(tool_name: str) -> bool:
+    """Fillers only help for 300 ms–2 s gaps.
+
+    Shorter: the filler ends up racing the result.
+    Longer: one filler alone isn't enough; you'd need periodic updates.
+    """
+    expected_ms = EXPECTED_LATENCIES_MS.get(tool_name, 0)
+    return 300 <= expected_ms <= 2000 and bool(FILLER_PHRASES.get(tool_name))
+
+
+# ── Tool-bearing stream consumer ──────────────────────────────────
+
+
+async def run_agent_streaming(
     client: AsyncOpenAI,
     user_text: str,
-    sentence_queue: asyncio.Queue[str | None],
+    sentence_queue: asyncio.Queue[SentenceItem | None],
     journal: InMemoryRingBuffer,
 ) -> None:
-    """Iterate the LLM's token stream; flush sentence-by-sentence to the queue.
-
-    We accumulate tokens, then after each delta check whether a complete
-    sentence exists at the start of the buffer. If so, push it to the
-    sentence queue so the TTS drain coroutine can start synth immediately.
+    """Run the agent, call tools if requested, push sentences to TTS.
+
+    ``sentence_queue`` carries ``(kind, text, tool_call_id)`` tuples.
+    Replies have no tool-call ID; fillers keep theirs so the drain can
+    attribute transport acceptance to the tool that requested them.
     """
-    stream = await client.chat.completions.create(
-        model=MODEL,
-        messages=[
-            {"role": "system", "content": "You are a helpful voice assistant. Keep it brief."},
-            {"role": "user", "content": user_text},
-        ],
-        stream=True,
-    )
-
-    buffer = ""
-    first_token_t: float | None = None
-    async for chunk in stream:
-        delta = chunk.choices[0].delta.content or ""
-        if not delta:
-            continue
-        if first_token_t is None:
-            first_token_t = time.monotonic()
+    messages = [
+        {"role": "system", "content": "You are a helpful voice assistant. Keep replies brief."},
+        {"role": "user", "content": user_text},
+    ]
+
+    # Up to two iterations: first call may ask for tools, second produces
+    # the final spoken reply.
+    for _ in range(2):
+        stream = await client.chat.completions.create(
+            model=MODEL,
+            messages=messages,
+            tools=TOOLS,
+            stream=True,
+        )
+
+        buffer = ""
+        tool_calls: dict[int, dict] = {}
+
+        async for chunk in stream:
+            choice = chunk.choices[0]
+            delta = choice.delta
+
+            if delta.content:
+                buffer += delta.content
+                ready, buffer = split_at_sentence_boundaries(buffer)
+                if ready.strip():
+                    spoken = strip_markdown(ready).strip()
+                    if spoken:
+                        await sentence_queue.put(("reply", spoken, None))
+
+            for tc in delta.tool_calls or []:
+                entry = tool_calls.setdefault(tc.index, {"id": None, "name": None, "args": ""})
+                if tc.id:
+                    entry["id"] = tc.id
+                if tc.function and tc.function.name:
+                    entry["name"] = tc.function.name
+                if tc.function and tc.function.arguments:
+                    entry["args"] += tc.function.arguments
+
+            # ``stop`` is terminal for the whole turn; anything in
+            # ``tool_calls`` at that point we treat as malformed and ignore.
+            if choice.finish_reason == "stop":
+                if buffer.strip():
+                    spoken = strip_markdown(buffer).strip()
+                    if spoken:
+                        await sentence_queue.put(("reply", spoken, None))
+                await sentence_queue.put(None)
+                return
+
+            if choice.finish_reason == "tool_calls":
+                break
+
+        if not tool_calls:
+            await sentence_queue.put(None)
+            return
+
+        messages.append(
+            {
+                "role": "assistant",
+                "content": buffer or None,
+                "tool_calls": [
+                    {
+                        "id": tc["id"],
+                        "type": "function",
+                        "function": {"name": tc["name"], "arguments": tc["args"]},
+                    }
+                    for tc in tool_calls.values()
+                ],
+            }
+        )
+
+        for tc in tool_calls.values():
+            name = tc["name"]
+            args = json.loads(tc["args"] or "{}")
+
+            filler_enqueued = should_play_filler(name)
+            if filler_enqueued:
+                await sentence_queue.put(("filler", FILLER_PHRASES[name], tc["id"]))
+
             journal.append(
                 kind=JournalRecordKind.EVENT,
-                name="agent.first_token",
+                name="tool.call.started",
                 session_id=SESSION_ID,
-                data={"stage": "agent", "t_ms": first_token_t * 1000},
+                data={
+                    "stage": "tool",
+                    "name": name,
+                    "tool_call_id": tc["id"],
+                    "args": args,
+                    "filler_enqueued": filler_enqueued,
+                },
             )
-        buffer += delta
-
-        # split_at_sentence_boundaries returns (ready, leftover). ``ready``
-        # is a prefix of complete sentences; ``leftover`` is the dangling
-        # tail we keep buffering.
-        ready, buffer = split_at_sentence_boundaries(buffer)
-        if ready.strip():
-            spoken = strip_markdown(ready).strip()
-            if spoken:
-                await sentence_queue.put(spoken)
-                journal.append(
-                    kind=JournalRecordKind.EVENT,
-                    name="agent.sentence",
-                    session_id=SESSION_ID,
-                    data={"stage": "agent", "text": spoken},
-                )
-
-    # Flush any trailing text the LLM ended mid-sentence (no terminal
-    # punctuation). The production consume_agent_stream also guards with
-    # has_unclosed_markdown_delimiters; we keep the toy simple.
-    if buffer.strip():
-        spoken = strip_markdown(buffer).strip()
-        if spoken:
-            await sentence_queue.put(spoken)
+            t0 = time.monotonic()
+            result = await TOOL_IMPLS[name](**args)
+            journal.append(
+                kind=JournalRecordKind.EVENT,
+                name="tool.call.result",
+                session_id=SESSION_ID,
+                data={
+                    "stage": "tool",
+                    "name": name,
+                    "tool_call_id": tc["id"],
+                    "elapsed_ms": (time.monotonic() - t0) * 1000,
+                    "result": result,
+                },
+            )
+            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})
+
     await sentence_queue.put(None)
 
 
 async def drain_sentences_to_speaker(
-    tts, transport, sentence_queue: asyncio.Queue[str | None], journal: InMemoryRingBuffer
+    tts,
+    transport,
+    sentence_queue: asyncio.Queue[SentenceItem | None],
+    journal: InMemoryRingBuffer,
 ) -> tuple[float | None, int, int]:
-    """Take one sentence at a time, synthesise, stream audio to speaker.
-
-    Because ``transport.send_audio`` returns as soon as the chunk is
-    enqueued for playback, the next ``tts.synthesize`` can start while
-    the current sentence is still audible. That is the pipeline overlap.
-    """
     first_audio_t: float | None = None
     accepted_chunks = rejected_chunks = 0
     while True:
-        sentence = await sentence_queue.get()
-        if sentence is None:
+        item = await sentence_queue.get()
+        if item is None:
             break
-
+        kind, sentence, tool_call_id = item
         synth_start = time.monotonic()
         sentence_accepted = sentence_rejected = 0
         async for event in tts.synthesize(TTSInput(text=sentence)):
@@ -174,7 +301,12 @@
                             kind=JournalRecordKind.EVENT,
                             name="tts.first_audio",
                             session_id=SESSION_ID,
-                            data={"stage": "tts", "t_ms": first_audio_t * 1000},
+                            data={
+                                "stage": "tts",
+                                "kind": kind,
+                                "tool_call_id": tool_call_id,
+                                "t_ms": first_audio_t * 1000,
+                            },
                         )
                 else:
                     rejected_chunks += 1
@@ -185,6 +317,8 @@
             session_id=SESSION_ID,
             data={
                 "stage": "tts",
+                "kind": kind,
+                "tool_call_id": tool_call_id,
                 "elapsed_ms": (time.monotonic() - synth_start) * 1000,
                 "accepted_chunks": sentence_accepted,
                 "rejected_chunks": sentence_rejected,
@@ -195,7 +329,6 @@
 
 
 async def run_turn(transport, stt, client, tts, journal) -> None:
-    """STT-final → fan out to LLM-stream → sentence-queue → TTS-drain."""
     final_text = ""
     stt_final_t = None
     async for event in stt.events():
@@ -206,16 +339,10 @@
     if not final_text.strip() or stt_final_t is None:
         return
 
-    journal.append(
-        kind=JournalRecordKind.EVENT,
-        name="stt.final",
-        session_id=SESSION_ID,
-        data={"stage": "stt", "text": final_text, "t_ms": stt_final_t * 1000},
-    )
     print(f"  user: {final_text!r}")
-    sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
+    sentence_queue: asyncio.Queue[SentenceItem | None] = asyncio.Queue()
     _, delivery = await asyncio.gather(
-        stream_sentences_to_tts(client, final_text, sentence_queue, journal),
+        run_agent_streaming(client, final_text, sentence_queue, journal),
         drain_sentences_to_speaker(tts, transport, sentence_queue, journal),
     )
     first_audio_t, accepted_chunks, rejected_chunks = delivery
@@ -305,7 +432,7 @@
         )
         resources.push_async_callback(close_if_supported, tts)
 
-        print("Streaming agent. Ctrl-C to stop.\n")
+        print('Ask me "What is the weather in Tokyo?" or "Set a 5-minute timer."\n')
 
         try:
             await collect_turns(transport, detector, stt_factory, client, tts, journal)
```

</details>
<!-- END auto:diff -->

## The naive predecessor

Before `main.py`, read `blocking_tool.py`:

```bash
uv run python docs/teaching/07-tools/blocking_tool.py
```

It runs a tool synchronously inside the agent and emits no filler
at all. Ask *"What's the weather in Tokyo?"* and listen — the
1.5-second silence in the middle of the turn is exactly what the
filler heuristic in `main.py` is built to mask. **Wrong-version-
first** for tool UX: the technical pipe works, the user
experience does not.

## Run it

```bash
uv run python docs/teaching/07-tools/main.py
```

Ask *"What's the weather in Tokyo?"* and then *"Set a 5-minute
timer."* Listen to the difference.

## A turn with a tool has three phases

```
  [user speech]──►[agent thinks]──►[TOOL runs]──►[agent resumes]──►[TTS]
                        │              │                │
                        ▼              ▼                ▼
                 maybe some      1.5 s silence     final answer
                 pre-tool text   (filler window)
```

Phase (b) — the 1.5 s while the tool runs — is the new problem.
Voice-UX research treats gaps longer than about 800 ms as
broken-feeling. A filler utterance doesn't reduce the latency; it
changes what the user hears during it.

## The filler heuristic

`should_play_filler(tool_name)` in `main.py`:

| Expected tool time | Decision | Why |
|---|---|---|
| < 300 ms | Don't bother | Filler would race the result |
| 300 ms – 2 s | Play a filler | The sweet spot |
| > 2 s | Filler + periodic update | "Still working…" at 2.5 s |

This is a UX decision, not a technical one. The teaching version
implements the middle band. The >2 s case is exercise 1.

<!-- BEGIN auto:snippet src=main.py symbol=should_play_filler -->
```python
def should_play_filler(tool_name: str) -> bool:
    """Fillers only help for 300 ms–2 s gaps.

    Shorter: the filler ends up racing the result.
    Longer: one filler alone isn't enough; you'd need periodic updates.
    """
    expected_ms = EXPECTED_LATENCIES_MS.get(tool_name, 0)
    return 300 <= expected_ms <= 2000 and bool(FILLER_PHRASES.get(tool_name))
```
<!-- END auto:snippet -->

### Enqueued is not delivered

`should_play_filler` decides whether to put a filler on the sentence
queue. The `tool.call.started` record names that decision
`filler_enqueued`; it deliberately does not say “played.” TTS can
produce no chunks, or the transport can reject every chunk after the
decision was made.

Run the provider-free attribution probe:

```bash
uv run python docs/teaching/07-tools/filler_delivery_probe.py
```

The slow-tool case enqueues a filler and then scripts its only audio
chunk to be rejected. The first accepted reply chunk still becomes
`tts.first_audio`. The shared `tool_call_id` connects
`tool.call.started`, `tool.call.result`, and the filler-kind
`stage.tts.execute` record, whose accepted/rejected counts prove what
was scheduled for delivery.

This gives the journal two separate facts:

- `filler_enqueued: true` means the UX policy requested a filler.
- `kind: filler` with `accepted_chunks > 0` means filler audio reached
  the transport's delivery queue.

Neither fact proves that the speaker rendered the audio or that the
user heard it.

## Inline tools vs. session actions

Two different things live in `easycat.session.actions`:

- **Inline tools** (what this chapter ships): run *during* the
  turn. The result is fed back to the LLM, which then speaks an
  answer informed by the tool output.
- **Session actions**: requested by the agent, run *after* the
  turn, and do *not* return data to the LLM. Run the maintained
  [`action_catalog.py`](action_catalog.py) probe to discover the seven
  types currently shipped:

| Action | What it does |
|---|---|
| `EndCallAction` | Terminates the call / session |
| `TransferCallAction` | Hands off to a human or another number |
| `SendDTMFAction` | Plays DTMF tones on a telephony leg |
| `SendSMSAction` | Sends a text-message side effect |
| `AddToDNCAction` | Adds the current caller or a supplied number to the DNC store |
| `RemoveFromDNCAction` | Removes the current caller or a supplied number from the DNC store |
| `CustomAction` | Escape hatch — anything else |

A weather lookup is a tool. "Hang up" is not a tool — there is
nothing to return and nothing to resume. Put it in the action
queue.

> **Journaling.** `ToolCallStarted/Result` are journaled (we write
> them from this script). `SessionActionRequested/Started/Completed/Failed`
> are journaled by default too — `SessionJournalSink` records them as
> `session_action_requested`, `session_action_started`,
> `session_action_completed`, and `session_action_failed`, so you can
> read the action timeline straight from the bundle (failures include
> the `error` reason).

## A common bug: speaking the tool result

Some agent stacks leak JSON back into the response stream. The
tool says `{"temp": 17, "sky": "cloudy"}` and the TTS literally
reads the braces and quote marks. Our `run_agent_streaming` routes
tool deltas to the journal *only* — they never go through
`sentence_queue`, which is the TTS pipe. If you hit a leak in your
own code, walk the stream until you find a `delta.tool_calls`
branch that accidentally accumulates into the same buffer as
`delta.content`.

## Read the journal

```python
from pathlib import Path
from easycat.debug.testing import load_bundle
b = load_bundle(next(Path("docs/teaching/07-tools/runs/").glob("*.bundle")))
for r in b.records():
    if r["name"] in ("tool.call.started", "tool.call.result"):
        print(r["name"], r["data"].get("tool_call_id"), r["data"].get("filler_enqueued"))
    if r["name"] == "stage.tts.execute":
        d = r["data"]
        print(f"  tts [{d['kind']:>6}] call={d['tool_call_id']} "
              f"accepted={d['accepted_chunks']} rejected={d['rejected_chunks']}")
```

You will see `filler`-kind TTS spans interleaved with
`reply`-kind ones. That interleaving is the filler window made
visible.

## Try breaking it

1. Change `get_weather` to sleep 5 s. Listen — one filler is no
   longer enough. Add a "still working on it" at the 2.5 s mark.
2. Open `src/easycat/session/actions.py` and read the seven
   action dataclasses. For each one, answer in one sentence:
   *why is this a session action and not a tool?* (The test is
   whether the LLM has anything useful to do with the return
   value.) The chapter ships no concrete action wiring because
   the executors live at the Session layer, which we don't have
   yet — but the reasoning is the payload.
3. Make a tool that returns a 5 KB JSON blob. Verify none of it
   reaches TTS. If it does, find the leak.

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

## What's next

[Chapter 8 — Smart-turn](../08-smart-turn/) returns to the
latency story: endpoint classification cuts the *user-finished-
speaking* gap the way streaming TTS cut the *agent-finished-
thinking* gap.
