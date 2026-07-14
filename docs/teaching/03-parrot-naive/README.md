# Chapter 3 — Parrot, the Naive Way

> A bot that repeats what you said. Except it breaks the instant
> you say "um."

**Wrong-version-first chapter.** The whole point of this chapter
is to fail. Do not skip it. Do not read chapter 4 until you have
personally heard this fail on your own voice.

## Prerequisites

- [Chapter 2](../02-transcribe/)
- `uv sync --extra quickstart --extra deepgram --group dev`
- `OPENAI_API_KEY` (for TTS) and **`DEEPGRAM_API_KEY`** (the
  parrot needs mid-speech partials, which the OpenAI STT default
  does not produce).
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapters 1-2 (Transport + STT
> events). Chapter 0's PCM math isn't needed here.

## Diff from chapter 2

- **Added:** TTS via `easycat.recipes.speak`; a fixed silence-timeout
  turn detector; a conversation loop that keeps running until Ctrl-C;
  `speak_acceptance_probe.py` and `parrot.delivery` records for
  provider-free output-acceptance evidence; correlated `stt.received` and
  `stt.partial`/`stt.final` records that separate provider ingress from queue
  consumption; `parrot_lifecycle_probe.py` for inherited task/resource scope.
- **New requirement:** `DEEPGRAM_API_KEY` — the parrot's silence
  timer keys off STT partials, which OpenAI's default STT only emits
  after the audio uploads.
- **Modified:** STT events drive an action (speak) instead of just
  printing.
- **Preserved:** Chapter 2's `AsyncExitStack` acquisition rollback and
  `TaskGroup` sibling ownership. The timeout policy is deliberately naive;
  cleanup and cancellation are not.

<!-- BEGIN auto:diff prev=02-transcribe prev_src=streaming.py src=main.py trim_blank_context=true -->
<details>
<summary>Full unified diff vs <code>02-transcribe/streaming.py</code> (auto-generated)</summary>

```diff
--- docs/teaching/02-transcribe/streaming.py
+++ docs/teaching/03-parrot-naive/main.py
@@ -1,14 +1,15 @@
-"""Chapter 2 — streaming transcription.
-
-Open a mic transport, stream audio into an STT provider, and print
-partial + final transcripts with timestamps as they arrive. Writes a
-debug bundle to ``runs/``.
+"""Chapter 3 — Parrot, the naive way.
+
+A bot that parrots whatever it thinks you just said. Turn detection
+is a fixed silence timeout on STT partials. Deliberately broken.
+
+Run it and break it — "The capital of France is... uh... Paris" is
+the canonical killer. Chapter 4 replaces this with a real VAD.

 Dependencies:
-    uv sync --extra quickstart --group dev
-    uv sync --extra quickstart --extra deepgram --group dev  # for --provider deepgram
-    export OPENAI_API_KEY=...
-    export DEEPGRAM_API_KEY=...  # for --provider deepgram
+    uv sync --extra quickstart --extra deepgram --group dev
+    export OPENAI_API_KEY=...      # OpenAI TTS
+    export DEEPGRAM_API_KEY=...    # mid-speech STT partials
     uv run easycat doctor
     uv run easycat doctor --env-file .env         # if keys live in .env
     uv run easycat doctor --env-file .env --json  # for parseable checks
@@ -17,7 +18,6 @@

 from __future__ import annotations

-import argparse
 import asyncio
 import os
 import time
@@ -28,167 +28,286 @@
 from easycat import LocalTransportConfig
 from easycat.audio_format import PCM16_MONO_24K
 from easycat.debug.export import export_debug_bundle
-from easycat.events import EventBus, STTEventType
+from easycat.events import Error, EventBus, STTEvent, STTEventType
+from easycat.recipes import speak
 from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
 from easycat.runtime.capabilities import close_if_supported
 from easycat.stt.factory import STTProviderConfig, create_stt_provider
 from easycat.transports.local import LocalTransport

-DURATION_S = 5
+SILENCE_TIMEOUT_S = 0.5  # ← the magic number we will watch break things
 RUNS_DIR = Path(__file__).parent / "runs"
-PROVIDERS = ("openai", "deepgram")
-PROVIDER_ENV_VARS = {
-    "openai": "OPENAI_API_KEY",
-    "deepgram": "DEEPGRAM_API_KEY",
-}
-PROVIDER_TIMING = {
-    "openai": "after_stream_end",
-    "deepgram": "during_audio",
-}
-
-
-def _display_path(path: Path) -> Path:
-    try:
-        return path.relative_to(Path.cwd())
-    except ValueError:
-        return path
-
-
-def build_stt_config(provider: str) -> STTProviderConfig:
-    """Resolve one documented provider without leaking its credential."""
-    if provider not in PROVIDER_ENV_VARS:
-        choices = ", ".join(PROVIDERS)
-        raise ValueError(f"Unknown STT provider {provider!r}; choose one of: {choices}")
-
-    env_var = PROVIDER_ENV_VARS[provider]
-    api_key = os.getenv(env_var)
-    if not api_key:
-        raise SystemExit(f"Set {env_var} in your environment first.")
-
-    params = None
-    if provider == "deepgram":
-        # This is Deepgram's wire target, not a restriction on upstream PCM.
-        # Matching the transport avoids a resample in this comparison; the
-        # provider also accepts other PCM rates and resamples them internally.
-        params = {
-            "sample_rate": PCM16_MONO_24K.sample_rate,
-            "event_bus": EventBus(),
-        }
-
-    return STTProviderConfig(provider=provider, api_key=api_key, params=params)
-
-
-async def run_streaming(
+SESSION_ID = f"ch03-parrot-{int(time.time())}"
+STTQueueItem = tuple[int, STTEvent, float] | None
+
+
+class ParrotEventStreamEndedError(RuntimeError):
+    """Private TaskGroup signal: the STT consumer drained its sentinel."""
+
+
+def record_stt_received(
+    journal: InMemoryRingBuffer,
+    *,
+    event_id: int,
+    event: STTEvent,
+    offset_ms: float,
+    queue_depth: int,
+) -> None:
+    """Record provider ingress before the consumer can be blocked by TTS."""
+    journal.append(
+        kind=JournalRecordKind.EVENT,
+        name="stt.received",
+        session_id=SESSION_ID,
+        data={
+            "stage": "stt",
+            "event_id": event_id,
+            "event_type": event.type.value,
+            "text": event.text,
+            "offset_ms": offset_ms,
+            "queue_depth_before_put": queue_depth,
+        },
+    )
+
+
+def record_stt_consumed(
+    journal: InMemoryRingBuffer,
+    *,
+    event_id: int,
+    event: STTEvent,
+    received_offset_ms: float,
+    consumed_offset_ms: float,
+    queue_depth: int,
+) -> None:
+    """Record when the parrot finally dequeues one provider event."""
+    journal.append(
+        kind=JournalRecordKind.EVENT,
+        name=f"stt.{event.type.value}",
+        session_id=SESSION_ID,
+        data={
+            "stage": "stt",
+            "event_id": event_id,
+            "event_type": event.type.value,
+            "text": event.text,
+            "offset_ms": consumed_offset_ms,
+            "received_offset_ms": received_offset_ms,
+            "consumer_lag_ms": consumed_offset_ms - received_offset_ms,
+            "queue_depth_after_get": queue_depth,
+        },
+    )
+
+
+def record_delivery(
+    journal: InMemoryRingBuffer,
+    *,
+    text: str,
+    accepted_chunks: int,
+    rejected_chunks: int,
+    offset_ms: float,
+) -> None:
+    """Record transport acceptance without claiming speaker playback."""
+    journal.append(
+        kind=JournalRecordKind.EVENT,
+        name="parrot.delivery",
+        session_id=SESSION_ID,
+        data={
+            "stage": "parrot",
+            "committed_text": text,
+            "accepted_chunks": accepted_chunks,
+            "rejected_chunks": rejected_chunks,
+            "offset_ms": offset_ms,
+        },
+    )
+    if rejected_chunks:
+        print(
+            "  transport rejected "
+            f"{rejected_chunks}/{accepted_chunks + rejected_chunks} audio chunks"
+        )
+
+
+async def speak_and_record(
+    transport, journal: InMemoryRingBuffer, text: str, start: float
+) -> None:
+    """Speak once, then preserve every transport acceptance result."""
+    accepted_chunks, rejected_chunks = await speak(transport, text)
+    record_delivery(
+        journal,
+        text=text,
+        accepted_chunks=accepted_chunks,
+        rejected_chunks=rejected_chunks,
+        offset_ms=(time.monotonic() - start) * 1000,
+    )
+
+
+async def feed_audio(stt, transport) -> None:
+    async for chunk in transport.receive_audio():
+        await stt.send_audio(chunk)
+
+
+async def listen_stt(
+    stt,
+    ev_queue: asyncio.Queue[STTQueueItem],
+    journal: InMemoryRingBuffer,
+    start: float,
+    provider_errors: list[BaseException] | None = None,
+) -> None:
+    event_id = 0
+    async for event in stt.events():
+        event_id += 1
+        received_offset_ms = (time.monotonic() - start) * 1000
+        record_stt_received(
+            journal,
+            event_id=event_id,
+            event=event,
+            offset_ms=received_offset_ms,
+            queue_depth=ev_queue.qsize(),
+        )
+        await ev_queue.put((event_id, event, received_offset_ms))
+    # Provider errors are emitted on a task immediately before the terminal
+    # sentinel is queued. Yield once so the synchronous subscriber records an
+    # exhausted-socket failure before we classify stream exhaustion as normal.
+    await asyncio.sleep(0)
+    if provider_errors:
+        raise provider_errors[-1]
+    await ev_queue.put(None)
+
+
+async def parrot_events(
+    transport,
+    ev_queue: asyncio.Queue[STTQueueItem],
+    journal: InMemoryRingBuffer,
+    start: float,
+) -> None:
+    last_text = ""
+    while True:
+        try:
+            # If no new event arrives within SILENCE_TIMEOUT_S, we
+            # interpret silence as "user is done" — the whole bug.
+            item = await asyncio.wait_for(ev_queue.get(), timeout=SILENCE_TIMEOUT_S)
+        except TimeoutError:
+            if last_text:
+                offset_ms = (time.monotonic() - start) * 1000
+                print(f"  t+{offset_ms:6.0f}ms  PARROT → {last_text!r}")
+                journal.append(
+                    kind=JournalRecordKind.EVENT,
+                    name="parrot.fire",
+                    session_id=SESSION_ID,
+                    data={
+                        "stage": "parrot",
+                        "committed_text": last_text,
+                        "silence_timeout_s": SILENCE_TIMEOUT_S,
+                        "offset_ms": offset_ms,
+                    },
+                )
+                await speak_and_record(transport, journal, last_text, start)
+                last_text = ""
+            continue
+        if item is None:
+            break
+        event_id, event, received_offset_ms = item
+        # Deliberately acting on partials — chapter 2's rule, broken
+        # on purpose. Chapter 4 restores it by waiting for a real
+        # turn boundary from the VAD.
+        last_text = event.text
+        kind = "FINAL" if event.type == STTEventType.FINAL else "part "
+        offset_ms = (time.monotonic() - start) * 1000
+        print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
+        record_stt_consumed(
+            journal,
+            event_id=event_id,
+            event=event,
+            received_offset_ms=received_offset_ms,
+            consumed_offset_ms=offset_ms,
+            queue_depth=ev_queue.qsize(),
+        )
+
+
+async def stop_when_parrot_ends(
+    transport,
+    ev_queue: asyncio.Queue[STTQueueItem],
+    journal: InMemoryRingBuffer,
+    start: float,
+) -> None:
+    """Turn normal queue exhaustion into a TaskGroup-wide stop signal."""
+    await parrot_events(transport, ev_queue, journal, start)
+    raise ParrotEventStreamEndedError
+
+
+async def run_parrot(
     stt,
     transport,
     journal: InMemoryRingBuffer,
-    session_id: str,
-    *,
-    duration_s: float = DURATION_S,
-) -> None:
-    """Run one stream with acquisition rollback and joined sibling tasks."""
-    # Register each cleanup as soon as ownership begins. If connect() or
-    # start_stream() raises after partial acquisition, the earlier callbacks
-    # still run. Successful acquisition unwinds as end → close → disconnect.
+    provider_errors: list[BaseException] | None = None,
+) -> None:
+    """Own one parrot stream until cancellation, failure, or STT exhaustion."""
     async with AsyncExitStack() as resources:
+        # These objects exist before connect(), so register final cleanup
+        # before the first fallible acquisition step.
         resources.push_async_callback(transport.disconnect)
         resources.push_async_callback(close_if_supported, stt)
         await transport.connect()

         await stt.start_stream()
+        # A logical stream exists only after start_stream() succeeds.
         resources.push_async_callback(stt.end_stream)

         start = time.monotonic()
-        print(f"Speak for {duration_s:g} seconds...")
-
-        async def feed_audio() -> None:
-            """Push mic chunks into STT until the capture window elapses."""
-            async for chunk in transport.receive_audio():
-                await stt.send_audio(chunk)
-                if time.monotonic() - start >= duration_s:
-                    break
-            # Closing the STT stream is what triggers the upload (for
-            # OpenAI's batch provider) or the final commit (for Deepgram).
-            # For OpenAI this call blocks for the full round-trip: the
-            # partials you see start arriving *after* we get here.
-            await stt.end_stream()
-
-        async def consume_events() -> None:
-            """Print every partial / final as soon as it arrives."""
-            async for event in stt.events():
-                offset_ms = (time.monotonic() - start) * 1000
-                kind = "FINAL" if event.type == STTEventType.FINAL else "part "
-                print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
-                journal.append(
-                    kind=JournalRecordKind.EVENT,
-                    name=f"stt.{event.type.value}",
-                    session_id=session_id,
-                    data={
-                        "stage": "stt",
-                        "event_type": event.type.value,
-                        "text": event.text,
-                        "offset_ms": offset_ms,
-                        # t_ms mirrors the later chapters' field so downstream
-                        # scripts (ch 12's evals.py, etc.) can read this bundle
-                        # without a translator.
-                        "t_ms": time.monotonic() * 1000,
-                    },
+        print("Naive parrot. Talk to it. Ctrl-C when you're sick of it.")
+        ev_queue: asyncio.Queue[STTQueueItem] = asyncio.Queue()
+        observed_provider_errors = provider_errors if provider_errors is not None else []
+
+        try:
+            async with asyncio.TaskGroup() as streams:
+                streams.create_task(feed_audio(stt, transport))
+                streams.create_task(
+                    listen_stt(stt, ev_queue, journal, start, observed_provider_errors)
                 )
-
-        # TaskGroup cancels and joins one sibling before it lets the failure
-        # escape. Resource cleanup therefore never races a live feeder or
-        # event consumer.
-        async with asyncio.TaskGroup() as streams:
-            streams.create_task(feed_audio())
-            streams.create_task(consume_events())
-
-
-async def main(provider: str = "openai") -> None:
-    config = build_stt_config(provider)
-    session_id = f"ch02-streaming-{provider}-{int(time.time())}"
+                streams.create_task(stop_when_parrot_ends(transport, ev_queue, journal, start))
+        except* ParrotEventStreamEndedError:
+            # ``parrot_events`` consumed the listener's None sentinel. Raising
+            # inside its wrapper makes TaskGroup cancel and join the infinite
+            # microphone feeder before resource teardown begins.
+            pass
+
+
+async def main() -> None:
+    oai_key = os.getenv("OPENAI_API_KEY")
+    dg_key = os.getenv("DEEPGRAM_API_KEY")
+    if not oai_key or not dg_key:
+        raise SystemExit("Set OPENAI_API_KEY (for TTS) and DEEPGRAM_API_KEY (for STT).")

     journal = InMemoryRingBuffer(capacity=10_000)
-    # The same STT factory from batch.py — the CLI changes only its config.
-    # The start/send/events consumer below is provider-independent.
-    stt = create_stt_provider(config)
-
-    # LocalTransport's 24 kHz pipeline rate matches chapters 3+.
     transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))

-    journal.append(
-        kind=JournalRecordKind.EVENT,
-        name="stt.provider.selected",
-        session_id=session_id,
-        data={
-            "provider": provider,
-            "credential_env": PROVIDER_ENV_VARS[provider],
-            "event_timing": PROVIDER_TIMING[provider],
-            "input_sample_rate_hz": PCM16_MONO_24K.sample_rate,
-            "provider_target_sample_rate_hz": (
-                PCM16_MONO_24K.sample_rate if provider == "deepgram" else None
-            ),
-        },
-    )
-
-    await run_streaming(stt, transport, journal, session_id)
+    # Deepgram emits partials mid-speech, which is what this chapter needs
+    # to feel break. Its STT factory config takes provider-specific args via
+    # ``params``. ``sample_rate=24000`` matches our LocalTransport's mic
+    # format. Capture terminal provider errors so an exhausted reconnect loop
+    # cannot look like an ordinary end of the STT event stream.
+    provider_errors: list[BaseException] = []
+    event_bus = EventBus()
+    event_bus.subscribe(Error, lambda event: provider_errors.append(event.exception))
+    stt = create_stt_provider(
+        STTProviderConfig(
+            provider="deepgram",
+            api_key=dg_key,
+            params={"sample_rate": 24000, "event_bus": event_bus},
+        )
+    )
+
+    try:
+        await run_parrot(stt, transport, journal, provider_errors)
+    except (KeyboardInterrupt, asyncio.CancelledError):
+        pass

     RUNS_DIR.mkdir(exist_ok=True)
-    bundle_path = RUNS_DIR / f"{session_id}.bundle"
+    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
     session_stub = types.SimpleNamespace(journal=journal)
     export_debug_bundle(session_stub, bundle_path, overwrite=True)
-    print(f"\nWrote bundle → {_display_path(bundle_path)}")
-
-
-def parse_args() -> argparse.Namespace:
-    parser = argparse.ArgumentParser(description=__doc__)
-    parser.add_argument(
-        "--provider",
-        choices=PROVIDERS,
-        default="openai",
-        help="STT provider: OpenAI batches locally; Deepgram emits during speech.",
-    )
-    return parser.parse_args()
+    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")


 if __name__ == "__main__":
-    asyncio.run(main(parse_args().provider))
+    try:
+        asyncio.run(main())
+    except KeyboardInterrupt:
+        pass
```

</details>
<!-- END auto:diff -->

## Run it

```bash
uv run python docs/teaching/03-parrot-naive/main.py
```

Talk. It repeats. Ctrl-C to stop.

## The naive plan

> If no new STT partial has arrived in **500 ms**, the user is
> done. Take the last partial text, hand it to TTS, play it.

Reasonable-sounding. Chapter 2's boundary was "partials may drive
reversible observation or speculation, but irreversible output waits for
`STTFinal`." We are **deliberately** crossing that boundary here by speaking
a partial so you can feel why it exists.

## Architecture

```
 ┌─────┐    ┌─────┐   partials+finals   ┌───────────────┐
 │ Mic │ ──►│ STT │ ──────────────────► │ ingress queue │
 └─────┘    └─────┘                     └───────┬───────┘
                                               ▼
                                      ┌─────────────────┐    ┌─────┐
                                      │ silence-timeout │──► │ TTS │
                                      │     parrot      │    └─────┘
                                      └─────────────────┘
                                      (blocks on speak())
```

## Keep the intended bug isolated

This chapter deliberately breaks one rule: it treats a 500 ms gap in consumed
STT events as permission to speak a partial hypothesis. It does **not** need to
reintroduce unrelated lifetime bugs to make that failure visceral.

The surrounding scaffold is inherited from chapter 2:

- `AsyncExitStack` registers transport disconnect and provider close before
  `connect()`, then registers `end_stream()` only after logical stream startup.
  Connect and start failures therefore unwind only the ownership that exists.
- `TaskGroup` owns the microphone feeder, STT listener, and parrot consumer.
  A failure in any child cancels and joins the other two before resource
  teardown.
- A long-running parrot has one extra case: the STT listener can end normally
  while the microphone feeder is intentionally infinite. Provider `Error`
  events are captured first, so failed WebSocket exhaustion propagates instead
  of sharing this path. After a genuinely normal end, the parrot drains the
  listener's `None` sentinel and a private
  `ParrotEventStreamEndedError` turns that terminal condition into a caught
  TaskGroup stop signal. The feeder is cancelled and joined; the sentinel does
  not escape as an application error.

Run all five paths without credentials or audio hardware:

```bash
uv run python docs/teaching/03-parrot-naive/parrot_lifecycle_probe.py
```

`normal_event_end` shows `transport.receive.cancelled` before `stt.end`,
`stt.close`, and `transport.disconnect`. `failed_event_end` follows the same
ordered teardown but reports the STT failure instead of suppressing it. The two
startup failures never record `stt.end`, while the feed failure cancels the STT
listener before teardown. That leaves the silence timeout as the only
deliberate failure introduced by this chapter.

## Break it, deliberately

Say each of these and watch the parrot commit to the wrong thing:

1. **"The capital of France is... uh... Paris."** The 500 ms
   timeout fires during the "uh." The parrot speaks "The capital
   of France is" while you continue with "Paris." New STT events queue behind
   the blocking TTS call, then may be treated as a second fragment after the
   first audio finishes.
2. **"I was thinking... [long pause] ...we should order pizza."**
   Same story. Thinking pauses indistinguishable from done.
3. **A list: "apples, bananas, pears."** Commas are 300-500 ms
   of silence. Bot fires mid-list.
4. **A yes/no question with rising intonation.** A short, clean
   sentence — works! Sometimes. Until the provider partial
   happens to land late and the timeout fires first.

## Why it breaks

Silence is not a boolean that can be read off the microphone:

| What it looks like | What it is |
|---|---|
| 500 ms no partial | End of turn |
| 500 ms no partial | Thinking pause |
| 500 ms no partial | Breath |
| 500 ms no partial | Provider happened to be slow |

The STT partial layer cannot distinguish these. It's a thresholding
decision on the wrong signal. Whatever number you pick for the
timeout, you will get either **false fires** (low number) or a
**sluggish bot** (high number). There is no good value.

## Read the journal

Open the bundle in `runs/`:

```python
from easycat.debug.testing import load_bundle
b = load_bundle("docs/teaching/03-parrot-naive/runs/<file>.bundle")
for r in b.records():
    if r["name"].startswith(("stt.", "parrot.")):
        print(r["sequence"], r["data"].get("offset_ms"), r["name"],
              r["data"].get("text") or r["data"].get("committed_text"))
```

Find the moment the parrot committed with the maintained analyzer:

```bash
uv run python docs/teaching/03-parrot-naive/inspect_timeout.py \
  docs/teaching/03-parrot-naive/runs/<file>.bundle
```

Each provider event now has one `event_id` across two records:

- `stt.received.offset_ms` is when `listen_stt()` received the provider event
  and queued it. This is the ingress clock.
- `stt.partial` or `stt.final` records when the parrot dequeued that same
  event. Its `received_offset_ms`, `consumer_lag_ms`, and queue depth expose
  head-of-line blocking.

The timeout starts after the latest *consumed* STT event, which can be a
partial or a final. The `parrot.fire` offset will be **at least** 500 ms after
that trigger; event-loop scheduling contributes the reported
`scheduler_overshoot_ms`. For the next partial, the analyzer reports both
`post_fire_ingress_gap_ms` (when the provider event reached the process) and
`post_fire_consumer_gap_ms` (when the parrot finally handled it).
`consumer_backlog_ms` is the difference. A large backlog proves delay behind
`speak()`; it is not provider latency and it is not a dropped event.

### Output acceptance is separate evidence

`recipes.speak()` returns `(accepted_chunks, rejected_chunks)` from the
transport and the parrot writes those counts to `parrot.delivery`. Run the
same contract without a microphone, TTS account, or speaker:

```bash
uv run python docs/teaching/03-parrot-naive/speak_acceptance_probe.py
```

The scripted TTS produces three chunks; the transport accepts two and rejects
one. These counts prove only transport acceptance. They do not prove that the
accepted chunks reached a device or were heard—the playback-evidence lesson in
chapter 9 adds that later boundary.

## Try breaking it

Change `SILENCE_TIMEOUT_S` at the top of `main.py` from `0.5` to
`2.0`. Re-run. Observations:

- Fewer false fires on "um."
- Feels sluggish. Turn latency is now permanently 2 seconds.

Then try `0.2`. It will fire on every breath. Somewhere between
the extremes is *your* personal compromise on *your* voice — and
that is still worse than the real thing.

## What you should feel now

Three failure modes, minimum. You should be actively asking for
VAD — for a signal that is "the microphone is currently carrying
speech" rather than "STT has been quiet."

## What's next

[Chapter 4 — VAD + pre-roll](../04-vad-preroll/) replaces the
silence timeout with a real voice-activity detector and a
pre-roll buffer, then replays your breakers through it.
