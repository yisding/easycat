# Chapter 2 — Transcribe

<!-- BEGIN auto:navigation -->
[← Chapter 1 — Echo](../01-echo/) · [Teaching ladder](../) · [Exercises](./EXERCISES.md) · [Chapter 3 — Parrot, the Naive Way →](../03-parrot-naive/)
<!-- END auto:navigation -->

> Speak, see text. Twice — once batch, once streaming. Feel the
> latency difference. And meet the journal.

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `partial vs final commitment` without a microphone,
> speakers, or provider credentials:
>
> ```bash
> uv run python docs/teaching/02-transcribe/partial_policy_probe.py
> ```
>
> **Evidence to find:** revised partials cancel speculation; only the final `fifty` commits the
> safe action.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

## Prerequisites

- [Chapter 1](../01-echo/)
- `uv sync --extra quickstart --group dev` for the default OpenAI path.
- To run the mid-speech comparison, add Deepgram with
  `uv sync --extra quickstart --extra deepgram --group dev`.
- Export the selected provider's key: `OPENAI_API_KEY` for the default or
  `DEEPGRAM_API_KEY` for `--provider deepgram`.
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
  logical-stream versus provider-resource ownership; an executable
  `--provider` switch for comparing response-stream and realtime STT;
  `stream_lifecycle_probe.py` for acquisition rollback and sibling-task
  cancellation.
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
@@ -1,57 +1,194 @@
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
+    uv sync --extra quickstart --group dev
+    uv sync --extra quickstart --extra deepgram --group dev  # for --provider deepgram
+    export OPENAI_API_KEY=...
+    export DEEPGRAM_API_KEY=...  # for --provider deepgram
+    uv run easycat doctor
+    uv run easycat doctor --env-file .env         # if keys live in .env
+    uv run easycat doctor --env-file .env --json  # for parseable checks
+    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
 """

 from __future__ import annotations

+import argparse
 import asyncio
+import os
+import time
+import types
+from contextlib import AsyncExitStack
+from pathlib import Path

 from easycat import LocalTransportConfig
+from easycat.audio_format import PCM16_MONO_24K
+from easycat.debug.export import export_debug_bundle
+from easycat.events import EventBus, STTEventType
+from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
+from easycat.runtime.capabilities import close_if_supported
+from easycat.stt.factory import STTProviderConfig, create_stt_provider
 from easycat.transports.local import LocalTransport

-
-async def echo(transport) -> tuple[int, int]:
-    """Pipe every inbound audio chunk straight to the outbound side.
-
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
+DURATION_S = 5
+RUNS_DIR = Path(__file__).parent / "runs"
+PROVIDERS = ("openai", "deepgram")
+PROVIDER_ENV_VARS = {
+    "openai": "OPENAI_API_KEY",
+    "deepgram": "DEEPGRAM_API_KEY",
+}
+PROVIDER_TIMING = {
+    "openai": "after_stream_end",
+    "deepgram": "during_audio",
+}


-async def main() -> None:
-    transport = LocalTransport(LocalTransportConfig())
-    await transport.connect()
-    print("Echoing mic to speakers. Ctrl-C to stop.")
+def _display_path(path: Path) -> Path:
     try:
-        accepted, rejected = await echo(transport)
-        print(f"Echo stream ended: accepted={accepted}, rejected={rejected}")
-    finally:
-        await transport.disconnect()
+        return path.relative_to(Path.cwd())
+    except ValueError:
+        return path
+
+
+def build_stt_config(provider: str) -> STTProviderConfig:
+    """Resolve one documented provider without leaking its credential."""
+    if provider not in PROVIDER_ENV_VARS:
+        choices = ", ".join(PROVIDERS)
+        raise ValueError(f"Unknown STT provider {provider!r}; choose one of: {choices}")
+
+    env_var = PROVIDER_ENV_VARS[provider]
+    api_key = os.getenv(env_var)
+    if not api_key:
+        raise SystemExit(f"Set {env_var} in your environment first.")
+
+    params = None
+    if provider == "deepgram":
+        # This is Deepgram's wire target, not a restriction on upstream PCM.
+        # Matching the transport avoids a resample in this comparison; the
+        # provider also accepts other PCM rates and resamples them internally.
+        params = {
+            "sample_rate": PCM16_MONO_24K.sample_rate,
+            "event_bus": EventBus(),
+        }
+
+    return STTProviderConfig(provider=provider, api_key=api_key, params=params)
+
+
+async def run_streaming(
+    stt,
+    transport,
+    journal: InMemoryRingBuffer,
+    session_id: str,
+    *,
+    duration_s: float = DURATION_S,
+) -> None:
+    """Run one stream with acquisition rollback and joined sibling tasks."""
+    # Register each cleanup as soon as ownership begins. If connect() or
+    # start_stream() raises after partial acquisition, the earlier callbacks
+    # still run. Successful acquisition unwinds as end → close → disconnect.
+    async with AsyncExitStack() as resources:
+        resources.push_async_callback(transport.disconnect)
+        resources.push_async_callback(close_if_supported, stt)
+        await transport.connect()
+
+        await stt.start_stream()
+        resources.push_async_callback(stt.end_stream)
+
+        start = time.monotonic()
+        print(f"Speak for {duration_s:g} seconds...")
+
+        async def feed_audio() -> None:
+            """Push mic chunks into STT until the capture window elapses."""
+            async for chunk in transport.receive_audio():
+                await stt.send_audio(chunk)
+                if time.monotonic() - start >= duration_s:
+                    break
+            # Closing the STT stream is what triggers the upload (for
+            # OpenAI's batch provider) or the final commit (for Deepgram).
+            # For OpenAI this call blocks for the full round-trip: the
+            # partials you see start arriving *after* we get here.
+            await stt.end_stream()
+
+        async def consume_events() -> None:
+            """Print every partial / final as soon as it arrives."""
+            async for event in stt.events():
+                offset_ms = (time.monotonic() - start) * 1000
+                kind = "FINAL" if event.type == STTEventType.FINAL else "part "
+                print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
+                journal.append(
+                    kind=JournalRecordKind.EVENT,
+                    name=f"stt.{event.type.value}",
+                    session_id=session_id,
+                    data={
+                        "stage": "stt",
+                        "event_type": event.type.value,
+                        "text": event.text,
+                        "offset_ms": offset_ms,
+                        # t_ms mirrors the later chapters' field so downstream
+                        # scripts (ch 12's evals.py, etc.) can read this bundle
+                        # without a translator.
+                        "t_ms": time.monotonic() * 1000,
+                    },
+                )
+
+        # TaskGroup cancels and joins one sibling before it lets the failure
+        # escape. Resource cleanup therefore never races a live feeder or
+        # event consumer.
+        async with asyncio.TaskGroup() as streams:
+            streams.create_task(feed_audio())
+            streams.create_task(consume_events())
+
+
+async def main(provider: str = "openai") -> None:
+    config = build_stt_config(provider)
+    session_id = f"ch02-streaming-{provider}-{int(time.time())}"
+
+    journal = InMemoryRingBuffer(capacity=10_000)
+    # The same STT factory from batch.py — the CLI changes only its config.
+    # The start/send/events consumer below is provider-independent.
+    stt = create_stt_provider(config)
+
+    # LocalTransport's 24 kHz pipeline rate matches chapters 3+.
+    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
+
+    journal.append(
+        kind=JournalRecordKind.EVENT,
+        name="stt.provider.selected",
+        session_id=session_id,
+        data={
+            "provider": provider,
+            "credential_env": PROVIDER_ENV_VARS[provider],
+            "event_timing": PROVIDER_TIMING[provider],
+            "input_sample_rate_hz": PCM16_MONO_24K.sample_rate,
+            "provider_target_sample_rate_hz": (
+                PCM16_MONO_24K.sample_rate if provider == "deepgram" else None
+            ),
+        },
+    )
+
+    await run_streaming(stt, transport, journal, session_id)
+
+    RUNS_DIR.mkdir(exist_ok=True)
+    bundle_path = RUNS_DIR / f"{session_id}.bundle"
+    session_stub = types.SimpleNamespace(journal=journal)
+    export_debug_bundle(session_stub, bundle_path, overwrite=True)
+    print(f"\nWrote bundle → {_display_path(bundle_path)}")
+
+
+def parse_args() -> argparse.Namespace:
+    parser = argparse.ArgumentParser(description=__doc__)
+    parser.add_argument(
+        "--provider",
+        choices=PROVIDERS,
+        default="openai",
+        help="STT provider: OpenAI batches locally; Deepgram emits during speech.",
+    )
+    return parser.parse_args()


 if __name__ == "__main__":
-    try:
-        asyncio.run(main())
-    except KeyboardInterrupt:
-        pass
+    asyncio.run(main(parse_args().provider))
```

</details>
<!-- END auto:diff -->

## The two scripts

```bash
uv run python docs/teaching/02-transcribe/batch.py
uv run python docs/teaching/02-transcribe/streaming.py --provider openai
uv run python docs/teaching/02-transcribe/streaming.py --provider deepgram
```

Each command records 5 seconds, sends it to STT, prints what came back,
and writes a debug bundle to `docs/teaching/02-transcribe/runs/`. The streaming
bundle name includes the selected provider so back-to-back comparisons do not
hide which timing contract produced them.

### What survives the run?

The two scripts deliberately retain **text evidence, not raw microphone
audio**:

- `batch.py` records into a scoped `TemporaryDirectory`. The WAV exists
  while `transcribe_file()` reads it and is deleted before bundle export,
  including when transcription raises. Its `recording.complete` record keeps
  only the filename, duration, and `retention="temporary"`—not an absolute
  host path. A successful run also records `recording.cleaned` with
  `deleted=true`.
- `streaming.py` sends live chunks directly to STT and never creates a WAV.
- Both debug bundles contain transcript hypotheses, timings, and other
  journal data. Transcripts can contain names, account details, or other PII,
  so the bundle is still sensitive even though these chapter scripts do not
  attach raw audio.

If an experiment needs retained audio, choose an explicit project path or
artifact store, document consent and retention, and delete it deliberately.
Do not rely on an abandoned system-temp file as an accidental recording
archive.

## Architecture

```
 ┌─────┐    send_audio()    ┌────────────┐    events()    ┌──────────┐
 │ Mic │ ──────────────────►│     STT    │ ─────────────► │ Consumer │
 └─────┘   AudioChunks      └────────────┘  STTEvent      └──────────┘
                                          (PARTIAL | FINAL)
```

With the default OpenAI selection, the same STT provider appears in two usage
patterns:

- **batch** — record first, transcribe in one call. The helper
  `easycat.recipes.transcribe_file(path)` wraps everything in ~30 lines.
- **streaming** — start the STT stream, push audio as it arrives,
  consume events concurrently. When the stream ends, partials and a
  final flow back.

### One stream, two concurrent tasks

The streaming path owns two lifetimes at once:

1. **Resources:** the transport and STT provider are acquired in order and
   must unwind even if `connect()` or `start_stream()` raises. An
   `AsyncExitStack` registers each cleanup when the script takes ownership.
   A completed stream unwinds as `end_stream()` → provider `close()` →
   transport `disconnect()`. If startup never completes, only the resources
   that exist are closed; there is no logical stream to end.
2. **Sibling tasks:** one task feeds microphone chunks while another consumes
   STT events. `asyncio.TaskGroup` treats them as one scope. If either fails,
   it cancels and joins the other before the exception reaches resource
   cleanup. Cleanup therefore cannot race a still-running producer or
   consumer.

`asyncio.gather()` is not an equivalent failure boundary here: by default it
propagates the first exception without cancelling and joining every other
awaitable. The old shape could begin closing STT while its event consumer was
still blocked inside `events()`.

Run the actual helper through deterministic failure paths without credentials,
a microphone, or a provider SDK:

```bash
uv run python docs/teaching/02-transcribe/stream_lifecycle_probe.py
```

In `feed_failure.events`, `stt.events.cancelled` appears before `stt.end`,
`stt.close`, and `transport.disconnect`. In `start_failure`, the provider and
transport still close, but `stt.end` is correctly absent because
`start_stream()` never succeeded. The main script exports a success bundle
only after this whole scope exits cleanly.

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

For truly mid-speech partials — the ones that arrive while you are still
talking — set `DEEPGRAM_API_KEY` and run:

```bash
uv run python docs/teaching/02-transcribe/streaming.py --provider deepgram
```

The selector changes only the factory configuration. Deepgram's
`sample_rate` is its provider-side wire target, not a required upstream
pipeline rate:

```python
stt = create_stt_provider(STTProviderConfig(
    provider="deepgram",
    api_key=dg_key,
    params={"sample_rate": 24000, "event_bus": EventBus()},
))
```

Chapters 3+ use exactly this configuration. The consumer code
(start_stream / send_audio / events) is identical to the OpenAI
path — that's the factory pattern's payoff. This chapter chooses a 24 kHz
Deepgram target to match `LocalTransport` and avoid resampling during the
comparison. If upstream PCM uses another rate, the bundled Deepgram provider
resamples it to the configured target rather than rejecting it.

Every streaming bundle starts with `stt.provider.selected`. It records the
provider, credential environment-variable *name* (never its value), input and
provider target rates, and one of these timing contracts:

| `event_timing` | What the offsets mean |
|---|---|
| `after_stream_end` | OpenAI buffered the microphone audio and streamed transcription events after `end_stream()`. |
| `during_audio` | Deepgram emitted hypotheses while microphone audio was still arriving. |

That record makes a bundle self-describing: a dense cluster of OpenAI offsets
cannot be mistaken for mid-speech latency later. OpenAI's
`provider_target_sample_rate_hz` is `null` because this provider wraps the
incoming PCM using its actual format instead of imposing a separate wire
target.

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

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

## What's next

[Chapter 3 — Parrot, the naive way](../03-parrot-naive/) glues STT
to TTS with the most obvious possible turn detector — a fixed
silence timeout — and watches it break.
