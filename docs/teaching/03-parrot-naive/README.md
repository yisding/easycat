# Chapter 3 — Parrot, the Naive Way

<!-- BEGIN auto:navigation -->
**Progress: 4 of 16** · [← Chapter 2](../02-transcribe/) · [Ladder index](../) · [Exercises](./EXERCISES.md) · [Chapter 4 →](../04-vad-preroll/)
<!-- END auto:navigation -->

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
- Running this chapter makes live provider calls that may incur charges.
  Review your provider billing and usage limits first.
- Provider-backed scripts may send audio, transcripts, or prompts to configured
  services. Use non-sensitive test content and review provider data-handling
  policies first.
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapters 1-2 (Transport + STT
> events). Chapter 0's PCM math isn't needed here.

## Diff from chapter 2

- **Added:** TTS via `easycat.recipes.speak`; a fixed silence-timeout
  turn detector; a conversation loop that keeps running until Ctrl-C;
  `speak_acceptance_probe.py` and `parrot.delivery` records for
  provider-free output-acceptance evidence.
- **New requirement:** `DEEPGRAM_API_KEY` — the parrot's silence
  timer keys off STT partials, which OpenAI's default STT only emits
  after the audio uploads.
- **Modified:** STT events drive an action (speak) instead of just
  printing.

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
@@ -28,60 +28,63 @@
 from easycat.audio_format import PCM16_MONO_24K
 from easycat.debug.export import export_debug_bundle
 from easycat.events import EventBus, STTEventType
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
+SESSION_ID = f"ch03-parrot-{int(time.time())}"
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
+async def shutdown(stt, transport) -> None:
+    """End the logical STT stream, close its provider, then disconnect."""
     try:
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
-async def shutdown(stt, transport, *, needs_stream_end: bool) -> None:
-    """End an active stream once, then close its provider and transport."""
-    try:
-        if needs_stream_end:
-            await stt.end_stream()
+        await stt.end_stream()
     finally:
         try:
             await close_if_supported(stt)
@@ -89,97 +92,109 @@
             await transport.disconnect()


-async def main(provider: str = "openai") -> None:
-    config = build_stt_config(provider)
-    session_id = f"ch02-streaming-{provider}-{int(time.time())}"
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
+    # Deepgram emits partials mid-speech, which is what this chapter needs
+    # to feel break. Its STT factory config takes provider-specific args via
+    # ``params``. ``sample_rate=24000`` matches our LocalTransport's mic
+    # format; ``event_bus`` is only used by Deepgram for WebSocket-reconnect
+    # telemetry — we wire a fresh bus here with no subscribers to satisfy
+    # the provider's constructor.
+    stt = create_stt_provider(
+        STTProviderConfig(
+            provider="deepgram",
+            api_key=dg_key,
+            params={"sample_rate": 24000, "event_bus": EventBus()},
+        )
     )

     await transport.connect()
     await stt.start_stream()
-    stream_end_started = False
     start = time.monotonic()
-    print(f"Speak for {DURATION_S} seconds...")
+    print("Naive parrot. Talk to it. Ctrl-C when you're sick of it.")
+
+    # Bridge STT events into an asyncio.Queue so the parrot loop can use
+    # ``asyncio.wait_for`` to implement "silence timeout since last event."
+    ev_queue: asyncio.Queue = asyncio.Queue()

     async def feed_audio() -> None:
-        """Push mic chunks into STT until DURATION_S seconds elapse."""
-        nonlocal stream_end_started
         async for chunk in transport.receive_audio():
             await stt.send_audio(chunk)
-            if time.monotonic() - start >= DURATION_S:
+
+    async def listen_stt() -> None:
+        async for event in stt.events():
+            await ev_queue.put(event)
+        await ev_queue.put(None)
+
+    async def parrot() -> None:
+        last_text = ""
+        while True:
+            try:
+                # If no new event arrives within SILENCE_TIMEOUT_S, we
+                # interpret silence as "user is done" — the whole bug.
+                event = await asyncio.wait_for(ev_queue.get(), timeout=SILENCE_TIMEOUT_S)
+            except TimeoutError:
+                if last_text:
+                    offset_ms = (time.monotonic() - start) * 1000
+                    print(f"  t+{offset_ms:6.0f}ms  PARROT → {last_text!r}")
+                    journal.append(
+                        kind=JournalRecordKind.EVENT,
+                        name="parrot.fire",
+                        session_id=SESSION_ID,
+                        data={
+                            "stage": "parrot",
+                            "committed_text": last_text,
+                            "silence_timeout_s": SILENCE_TIMEOUT_S,
+                            "offset_ms": offset_ms,
+                        },
+                    )
+                    await speak_and_record(transport, journal, last_text, start)
+                    last_text = ""
+                continue
+            if event is None:
                 break
-        # Closing the STT stream is what triggers the upload (for
-        # OpenAI's batch provider) or the final commit (for Deepgram).
-        # For OpenAI this call blocks for the full round-trip: the
-        # partials you see start arriving *after* we get here.
-        stream_end_started = True
-        await stt.end_stream()
-
-    async def consume_events() -> None:
-        """Print every partial / final as soon as it arrives."""
-        async for event in stt.events():
+            # Deliberately acting on partials — chapter 2's rule, broken
+            # on purpose. Chapter 4 restores it by waiting for a real
+            # turn boundary from the VAD.
+            last_text = event.text
+            kind = "FINAL" if event.type == STTEventType.FINAL else "part "
             offset_ms = (time.monotonic() - start) * 1000
-            kind = "FINAL" if event.type == STTEventType.FINAL else "part "
             print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
             journal.append(
                 kind=JournalRecordKind.EVENT,
                 name=f"stt.{event.type.value}",
-                session_id=session_id,
+                session_id=SESSION_ID,
                 data={
                     "stage": "stt",
                     "event_type": event.type.value,
                     "text": event.text,
                     "offset_ms": offset_ms,
-                    # t_ms mirrors the later chapters' field so downstream
-                    # scripts (ch 12's evals.py, etc.) can read this bundle
-                    # without a translator.
-                    "t_ms": time.monotonic() * 1000,
                 },
             )

     try:
-        await asyncio.gather(feed_audio(), consume_events())
+        await asyncio.gather(feed_audio(), listen_stt(), parrot())
+    except (KeyboardInterrupt, asyncio.CancelledError):
+        pass
     finally:
-        await shutdown(stt, transport, needs_stream_end=not stream_end_started)
+        await shutdown(stt, transport)

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
 ┌─────┐    ┌─────┐   partials+finals   ┌─────────────────┐    ┌─────┐
 │ Mic │ ──►│ STT │ ──────────────────► │ silence-timeout │──► │ TTS │
 └─────┘    └─────┘                     │     parrot      │    └─────┘
                                        └─────────────────┘
                                        (fires on 500 ms
                                         of no STT events)
```

## Break it, deliberately

Say each of these and watch the parrot commit to the wrong thing:

1. **"The capital of France is... uh... Paris."** The 500 ms
   timeout fires during the "uh." The parrot speaks "The capital
   of France is" and then you say "Paris" to an ignoring bot.
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

The timeout starts after the latest STT event, which can be a partial
or a final. The `parrot.fire` offset will be **at least** 500 ms after
that trigger; event-loop scheduling contributes the reported
`scheduler_overshoot_ms`. The analyzer also separates that silence gap
from the consumer stall while the parrot awaits `speak()`.

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
