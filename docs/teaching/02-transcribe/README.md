# Chapter 2 — Transcribe

<!-- BEGIN auto:navigation -->
**Progress: 3 of 16** · [← Chapter 1](../01-echo/) · [Ladder index](../) · [Exercises](./EXERCISES.md) · [Chapter 3 →](../03-parrot-naive/)
<!-- END auto:navigation -->

> Speak, see text. Twice — once batch, once streaming. Feel the
> latency difference. And meet the journal.

## Prerequisites

- [Chapter 1](../01-echo/)
- `uv sync --extra quickstart --group dev`
- `export OPENAI_API_KEY=sk-...` (or any other provider from
  `src/easycat/stt/factory.py`; add that provider's extra, such as
  `--extra deepgram`, and its API key, such as `DEEPGRAM_API_KEY`,
  when you switch).
- Running this chapter makes live provider calls that may incur charges.
  Review your provider billing and usage limits first.
- Provider-backed scripts may send audio, transcripts, or prompts to configured
  services. Use non-sensitive test content and review provider data-handling
  policies first.
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapter 1 for the `Transport`
> protocol. You can read this chapter without chapter 0's PCM math.

## Diff from chapter 1

- **Added:** STT provider via `create_stt_provider`; the first
  `RunBundle` written to `runs/`; the partial-vs-final event shape
  (`stt.partial`, `stt.final`); `partial_policy_probe.py` for the
  reversible-speculation boundary; `transcribe_ownership_probe.py` for
  logical-stream versus provider-resource ownership.
- **Modified:** the pipeline forks — audio still flows out of the
  transport, but it now goes to STT instead of back to the speaker.
- **Removed:** the speaker output (no echo in this chapter; this is
  one-way mic → STT).

<!-- BEGIN auto:diff prev=01-echo prev_src=main.py src=streaming.py trim_blank_context=true -->
<details>
<summary>Full unified diff vs <code>01-echo/main.py</code> (auto-generated)</summary>

```diff
--- docs/teaching/01-echo/main.py
+++ docs/teaching/02-transcribe/streaming.py
@@ -1,57 +1,120 @@
-"""Chapter 1 — Echo.
+"""Chapter 2 — streaming transcription.

-Mic → speaker, continuously, through EasyCat's ``Transport`` protocol.
-Runs until Ctrl-C.
+Open a mic transport, stream audio into an STT provider, and print
+partial + final transcripts with timestamps as they arrive. Writes a
+debug bundle to ``runs/``.

-Dependency:
-    uv sync --extra local --group dev
+Dependencies:
+    uv sync --extra quickstart --group dev  # add --extra deepgram for Deepgram partials
+    export OPENAI_API_KEY=...   # or DEEPGRAM_API_KEY for mid-speech partials
+    uv run easycat doctor
+    uv run easycat doctor --env-file .env         # if keys live in .env
+    uv run easycat doctor --env-file .env --json  # for parseable checks
+    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
 """

 from __future__ import annotations

 import asyncio
+import os
+import time
+import types
+from pathlib import Path

 from easycat import LocalTransportConfig
+from easycat.audio_format import PCM16_MONO_24K
+from easycat.debug.export import export_debug_bundle
+from easycat.events import STTEventType
+from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
+from easycat.runtime.capabilities import close_if_supported
+from easycat.stt.factory import STTProviderConfig, create_stt_provider
 from easycat.transports.local import LocalTransport

+DURATION_S = 5
+RUNS_DIR = Path(__file__).parent / "runs"
+SESSION_ID = f"ch02-streaming-{int(time.time())}"

-async def echo(transport) -> tuple[int, int]:
-    """Pipe every inbound audio chunk straight to the outbound side.

-    ``transport`` is deliberately untyped. Any object that matches
-    the inbound/outbound audio shape of ``easycat.providers.Transport``
-    will work — that is the whole point of duck-typed protocols.
-    Chapter 13 swaps in a different transport without changing this
-    function.
-
-    ``transport.receive_audio()`` is an *async generator* of audio
-    chunks. ``await transport.send_audio(chunk)`` returns whether the
-    transport accepted each chunk for delivery; it does not prove speaker
-    playback. No turn detection or STT — the point of this chapter is the
-    shape of the loop itself.
-    """
-    accepted = rejected = 0
-    async for chunk in transport.receive_audio():
-        if await transport.send_audio(chunk):
-            accepted += 1
-        else:
-            rejected += 1
-    return accepted, rejected
+async def shutdown(stt, transport, *, needs_stream_end: bool) -> None:
+    """End an active stream once, then close its provider and transport."""
+    try:
+        if needs_stream_end:
+            await stt.end_stream()
+    finally:
+        try:
+            await close_if_supported(stt)
+        finally:
+            await transport.disconnect()


 async def main() -> None:
-    transport = LocalTransport(LocalTransportConfig())
+    api_key = os.getenv("OPENAI_API_KEY")
+    if not api_key:
+        raise SystemExit("Set OPENAI_API_KEY in your environment first.")
+
+    journal = InMemoryRingBuffer(capacity=10_000)
+    # The same STT factory from batch.py — we just hand it a config
+    # instead of calling the `transcribe_file` shortcut. No consumer
+    # code would change if we swapped "openai" for "deepgram".
+    stt = create_stt_provider(STTProviderConfig(provider="openai", api_key=api_key))
+
+    # LocalTransport's default 24 kHz matches chapters 3+. OpenAI STT
+    # ingests WAV at whatever sample rate it's given, so this is fine.
+    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
+
     await transport.connect()
-    print("Echoing mic to speakers. Ctrl-C to stop.")
+    await stt.start_stream()
+    stream_end_started = False
+    start = time.monotonic()
+    print(f"Speak for {DURATION_S} seconds...")
+
+    async def feed_audio() -> None:
+        """Push mic chunks into STT until DURATION_S seconds elapse."""
+        nonlocal stream_end_started
+        async for chunk in transport.receive_audio():
+            await stt.send_audio(chunk)
+            if time.monotonic() - start >= DURATION_S:
+                break
+        # Closing the STT stream is what triggers the upload (for
+        # OpenAI's batch provider) or the final commit (for Deepgram).
+        # For OpenAI this call blocks for the full round-trip: the
+        # partials you see start arriving *after* we get here.
+        stream_end_started = True
+        await stt.end_stream()
+
+    async def consume_events() -> None:
+        """Print every partial / final as soon as it arrives."""
+        async for event in stt.events():
+            offset_ms = (time.monotonic() - start) * 1000
+            kind = "FINAL" if event.type == STTEventType.FINAL else "part "
+            print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
+            journal.append(
+                kind=JournalRecordKind.EVENT,
+                name=f"stt.{event.type.value}",
+                session_id=SESSION_ID,
+                data={
+                    "stage": "stt",
+                    "event_type": event.type.value,
+                    "text": event.text,
+                    "offset_ms": offset_ms,
+                    # t_ms mirrors the later chapters' field so downstream
+                    # scripts (ch 12's evals.py, etc.) can read this bundle
+                    # without a translator.
+                    "t_ms": time.monotonic() * 1000,
+                },
+            )
+
     try:
-        accepted, rejected = await echo(transport)
-        print(f"Echo stream ended: accepted={accepted}, rejected={rejected}")
+        await asyncio.gather(feed_audio(), consume_events())
     finally:
-        await transport.disconnect()
+        await shutdown(stt, transport, needs_stream_end=not stream_end_started)
+
+    RUNS_DIR.mkdir(exist_ok=True)
+    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
+    session_stub = types.SimpleNamespace(journal=journal)
+    export_debug_bundle(session_stub, bundle_path, overwrite=True)
+    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")


 if __name__ == "__main__":
-    try:
-        asyncio.run(main())
-    except KeyboardInterrupt:
-        pass
+    asyncio.run(main())
```

</details>
<!-- END auto:diff -->

## The two scripts

Start with the chapter's canonical entry point. It delegates to the
streaming version:

```bash
uv run python docs/teaching/02-transcribe/main.py
```

Then run the two named scripts directly to compare batch and streaming
STT side by side:

```bash
uv run python docs/teaching/02-transcribe/batch.py
uv run python docs/teaching/02-transcribe/streaming.py
```

Each records 5 seconds, sends it to STT, prints what came back,
and writes a debug bundle to `docs/teaching/02-transcribe/runs/`.

## Architecture

```
 ┌─────┐    send_audio()    ┌────────────┐    events()    ┌──────────┐
 │ Mic │ ──────────────────►│     STT    │ ─────────────► │ Consumer │
 └─────┘   AudioChunks      └────────────┘  STTEvent      └──────────┘
                                          (PARTIAL | FINAL)
```

Same STT provider, two usage patterns:

- **batch** — record first, transcribe in one call. The helper
  `easycat.recipes.transcribe_file(path)` wraps everything in ~30 lines.
- **streaming** — start the STT stream, push audio as it arrives,
  consume events concurrently. When the stream ends, partials and a
  final flow back.

### Ending a stream is not the same as closing a provider

`start_stream()` / `end_stream()` delimit one logical utterance. A provider may
still own a persistent WebSocket, HTTP client, or background task after that
utterance. `transcribe_file()` therefore follows the same ownership rule as
the Chapter 3 `speak()` recipe:

| Provider came from | Logical stream | Final resource cleanup |
|---|---|---|
| `transcribe_file(path, provider=...)` | Helper starts and ends it | Helper closes the helper-created STT |
| `transcribe_file(path, stt=my_stt)` | Helper starts and ends it | Caller closes the caller-supplied STT |

Run both paths without credentials:

```bash
uv run python docs/teaching/02-transcribe/transcribe_ownership_probe.py
```

The helper-created provider reports `owned_provider_closed: true`; the
caller-supplied provider reports `caller_provider_closed: false`. The logical
stream ends in both cases. This distinction matters for persistent providers
such as OpenAI Realtime: ending a turn intentionally keeps its warmed socket
available, while final provider cleanup closes it. The manual `streaming.py`
path follows the same rule in its `finally`: end the logical stream, close the
provider it created, then disconnect the transport.

## A note on which provider you run

`streaming.py` defaults to `"openai"`. The OpenAI STT provider
**buffers the audio locally and uploads it on `end_stream()`**,
then streams the *response* back. That means you will see
partials arrive in a burst *after* the 5-second recording ends,
not during it. The partials are real; the timing is misleading.

For truly mid-speech partials — the ones that arrive while you
are still talking — switch to Deepgram and set
`DEEPGRAM_API_KEY`. Deepgram is strict about its input format,
so the factory call carries two provider-specific settings:

```python
stt = create_stt_provider(STTProviderConfig(
    provider="deepgram",
    api_key=dg_key,
    params={"sample_rate": 24000, "event_bus": EventBus()},
))
```

Chapters 3+ use exactly this configuration. The consumer code
(start_stream / send_audio / events) is identical to the OpenAI
path — that's the factory pattern's payoff.

Both providers teach the same concept, below.

## Partial vs final

Every streaming STT is a guesser under time pressure. As more
audio arrives, it revises its guess — producing a sequence of
**partials** that settle, then commit:

```
  (speaking: "go into town")
  t+5100ms  [part ] going to
  t+5140ms  [part ] going to town
  t+5180ms  [part ] go into town
  t+5200ms  [FINAL] go into town
```

For OpenAI (batch audio, streaming response) the timestamps
cluster at the end because they reflect the response stream, not
the speech. For Deepgram (mid-speech partials) the same sequence
spreads across the utterance. The *shape* is what matters: the
provider revises its guess until it is confident, then commits.

The **final** is the provider's commitment. The safe rule is narrower than
"ignore partials": **never commit irreversible work from a partial.** A partial
may update captions, inform endpointing, or start cancellable speculation, but
each newer hypothesis supersedes the previous one.

| Consumer | Partial is useful? | Safe policy |
|---|:---:|---|
| Live transcript UI | Yes | Replace the tentative text. |
| Endpoint/smart-turn signal | Yes | Treat it as evidence, not a committed user turn. |
| Cache lookup or prefetch | Yes | Key it to the hypothesis; cancel or discard stale work. |
| Agent turn, tool side effect, database write, TTS | No commitment | Wait for `FINAL`. |

A naive consumer that starts a timer, writes state, or speaks for every partial
commits to guesses that may evaporate moments later. Cancellable prefetch is
different: it may spend some work early, but it must never leak a stale result
into user-visible state.

Run the provider-free probe:

```bash
uv run python docs/teaching/02-transcribe/partial_policy_probe.py
```

It revises "fifteen minutes" to "fifty minutes." The unsafe policy produces
three irreversible actions. The revision-aware policy cancels the stale
fifteen-minute speculation and commits exactly one fifty-minute action on the
final. Chapter 6 applies the same boundary to the most visible side effect:
spoken bot audio.

## Why streaming exists

If batch works, why bother? Two reasons:

1. **Lower perceived latency.** Batch waits for the user to stop
   speaking *and then* starts transcribing. Streaming begins the
   moment audio arrives. With a real-time provider, partials
   appear within ~150-300ms of their audio.
2. **Earlier signal for downstream stages.** Turn-end detection,
   smart-turn priming, and barge-in all want a running guess of
   what the user is saying before they stop.

## Your first journal

Both scripts write a `RunBundle` to `runs/`. Open one:

```python
from easycat.debug.testing import load_bundle
b = load_bundle("docs/teaching/02-transcribe/runs/<file>.bundle")
for rec in b.records():
    print(rec["sequence"], rec["name"], rec["data"])
```

You will see one record per partial and per final. Every record
has a sequence number, a monotonic-clock timestamp, and a name
(`stt.partial`, `stt.final`). This is the substrate that
[chapter 11](../11-journal/) teaches in full.

> **One honesty note up front.** Chapters 2-10 emit *composite*
> journal events of the form `stage.<name>.execute` with a single
> `elapsed_ms` field. The production journal in
> `src/easycat/runtime/` instead emits **paired** records
> (`stage_start` + `stage_complete`) that you match on a span
> correlation id in the record `data`.
> The teaching shape keeps the query layer at the surface; the
> paired shape buys you partial-span visibility on crashes.
> Chapter 11 surfaces this difference explicitly — don't be
> surprised when you meet it there.

For now: the journal is the single source of truth for "what just
happened," and every runnable chapter from here on will dump one.

## Try breaking it

Say a word the STT consistently mishears ("bass" vs "base",
"pear" vs "pair"). Re-run `streaming.py`, then read the bundle
and find the exact partial where the wrong guess stuck. Compare
that to the final. Did the revision save it, or did the provider
commit to the wrong word?

## What's next

[Chapter 3 — Parrot, the naive way](../03-parrot-naive/) glues STT
to TTS with the most obvious possible turn detector — a fixed
silence timeout — and watches it break.
