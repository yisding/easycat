# Chapter 10 — Cleaning the Signal

<!-- BEGIN auto:navigation -->
**Progress: 11 of 16** · [← Chapter 9 — Interruption / Barge-in](../09-interruption/) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Exercises](./EXERCISES.md) · [Chapter 11 — The Journal as Mental Model →](../11-journal/)
<!-- END auto:navigation -->

> Two problems often confused as one. **Noise reduction** removes
> uncorrelated background sound (fan, keyboard, baby).
> **Echo cancellation** removes the bot's own voice coming back
> through the microphone. Same pipeline slot; fundamentally
> different techniques.

<!-- BEGIN auto:spaced-retrieval -->
## Recall before reading

> **Following the ladder? Spaced retrieval — Chapter 8 — Smart-turn**
>
> Close earlier chapters and answer from memory before reading further. If this
> chapter is your starting point, skip this block.
>
> **Answer from memory:**
>
> How do 200 ms early silence, 40 ms inference, and 800 ms fallback compare with the 800 ms VAD
> baseline?
>
> After recording your answer, explain one way `endpoint wait decomposition` changes how you
> reason about `NR/AEC replay metrics`. Keep the first answer visible.
>
> **Check only after answering:**
>
> ```bash
> uv run python docs/teaching/08-smart-turn/endpoint_wait_probe.py
> ```
>
> Cite one observed field, measurement, or behavior; repair only the part your
> evidence disproved.
<!-- END auto:spaced-retrieval -->

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `NR/AEC replay metrics` without a microphone,
> speakers, or provider credentials:
>
> **Predict first:** What changes with aligned AEC reference audio, and what should fail when
> reference audio is missing or short?
>
> ```bash
> uv run python docs/teaching/10-cleaning-signal/replay_metrics_probe.py
> ```
>
> **Evidence to find:** aligned reference audio changes RMS by -12.041 dB; missing or short
> references fail.
>
> **Explain the result:** Tie each failure to the missing replay input and explain what aligned
> reference changes.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

## Prerequisites

- [Chapter 9](../09-interruption/)
- For the live pipeline:
  `uv sync --extra quickstart --extra deepgram --extra rnnoise --group dev`.
- For offline replay with `--nr on`:
  `uv sync --extra quickstart --extra rnnoise --group dev`. The checked-in WAV
  pairs need no microphone or API keys.
- RNNoise uses the opt-in `rnnoise` extra; Krisp requires its own SDK.
- For real AEC: `uv sync --extra aec --group dev` (LiveKit APM).
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`.
- Running this chapter makes live provider calls that may incur charges.
  Review your provider billing and usage limits first.
- Provider-backed scripts may send audio, transcripts, or prompts to configured
  services. Use non-sensitive test content and review provider data-handling
  policies first.
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapter 4 (where VAD sits).
> NR / AEC are orthogonal to the agent layer — they belong
> upstream of VAD and don't depend on chapters 5-9. The bonus
> `wrong_order.py` here makes that "where it belongs" tangible.

When a selected backend's deps are missing, the factories
**silently fall back to passthrough**. The script prints and
journals the live backend so you know which one you're actually
hearing.

## Diff from chapter 9

- **Added:** `create_noise_reducer()` + `create_echo_canceller()`
  factories; a `clean_audio_pipeline` stage that runs NR → AEC
  before VAD; an `aec.feed_reference(event.audio)` line in the
  TTS drain (the only dual-input thing in the pipeline); an
  `audio.config` journal record naming the live backend;
  `generate_fixtures.py` + `replay.py` for deterministic offline
  runs; `wrong_order.py` showing what happens with the stages in
  the wrong order.
- **Modified:** pipeline order becomes mic → NR → AEC → VAD → STT
  → agent (and TTS still feeds AEC's reference).
- **Removed:** chapter 9c's `TurnLedger` / history-rewrite — this
  chapter isolates the NR/AEC axis. Merge them yourself in
  exercise 4 if you want both at once.

<!-- BEGIN auto:diff prev=09-interruption prev_src=estimate.py src=main.py -->
<details>
<summary>Full unified diff vs <code>09-interruption/estimate.py</code> (auto-generated)</summary>

```diff
--- docs/teaching/09-interruption/estimate.py
+++ docs/teaching/10-cleaning-signal/main.py
@@ -1,19 +1,33 @@
-"""Chapter 9c — cancel + estimate what the user could have heard.
-
-Same as ``cancel.py`` plus: we track bytes of TTS audio accepted by
-the transport before the cancel fires, compute the character position
-in the bot's *text* reply that corresponds to those bytes, and rewrite
-the assistant turn in the conversation history to end there. Next turn,
-the LLM has a closer picture of what the user could have heard.
-
-The byte-to-char estimate is deliberately simple: accepted bytes ÷
-expected bytes × total chars. The production
-`easycat.session.interruption` estimator is a lot more careful
-about silence, SSML, markdown, and playback-ack fudge factors —
-read it after you understand the toy.
+"""Chapter 10 — Cleaning the signal.
+
+Add noise reduction (NR) and acoustic echo cancellation (AEC) to
+the ch9 pipeline. Toggle each independently via CLI flags, and
+read the journal to see which backend is actually running.
+
+    # Nothing on: the baseline.
+    --nr off --aec off
+
+    # NR alone: fan noise, keyboard clicks get filtered.
+    --nr on  --aec off
+
+    # AEC alone: bot-through-speaker bleed gets subtracted.
+    --nr off --aec on
+
+    # Both: prod-style.
+    --nr on  --aec on
+
+NR is single-input — it only sees the mic. AEC is dual-input —
+it needs both the mic *and* the far-end reference (the TTS audio
+we sent to the speaker). We feed the reference every time the
+transport accepts a complete TTS chunk.
 
 Dependencies:
-    uv sync --extra quickstart --extra deepgram --group dev
+    uv sync --extra quickstart --extra deepgram --extra rnnoise --group dev
+    RNNoise uses its opt-in extra; Krisp requires its own SDK.
+    For real AEC:  uv sync --extra aec --group dev
+    Missing selected backends fall back to passthrough — the
+    journal tells you which backend is live.
+
     export OPENAI_API_KEY=...
     export DEEPGRAM_API_KEY=...
     uv run easycat doctor
@@ -24,21 +38,24 @@
 
 from __future__ import annotations
 
+import argparse
 import asyncio
 import collections
 import os
 import time
 import types
-from collections.abc import Iterator
 from contextlib import AsyncExitStack
-from dataclasses import dataclass, field
 from pathlib import Path
 
 from openai import AsyncOpenAI
 
-from easycat import CancelToken, LocalTransportConfig
+from easycat import (
+    CancelToken,
+    LocalTransportConfig,
+)
 from easycat.audio_format import PCM16_MONO_24K, AudioChunk
 from easycat.debug.export import export_debug_bundle
+from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
 from easycat.events import (
     EventBus,
     STTEventType,
@@ -46,6 +63,7 @@
     VADStartSpeaking,
     VADStopSpeaking,
 )
+from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
 from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
 from easycat.runtime.capabilities import close_if_supported
 from easycat.session import split_at_sentence_boundaries
@@ -60,62 +78,23 @@
 MODEL = "gpt-5.6-luna"
 PREROLL_FRAMES = 15
 RUNS_DIR = Path(__file__).parent / "runs"
-SESSION_ID = f"ch09c-estimate-{int(time.time())}"
-
-# OpenAI TTS emits PCM16 mono at 24 kHz = 48,000 bytes/second.
-TTS_BYTES_PER_SECOND = 24_000 * 2
-LOCAL_OUTPUT_FRAME_MS = 20
-
-
-@dataclass
-class TurnLedger:
-    """Per-turn record of what the bot tried to say vs. what was accepted.
-
-    ``sentences_sent`` accumulates the text of each sentence dispatched
-    to TTS in order. ``bytes_accepted`` tracks audio bytes for which
-    ``transport.send_audio`` returned ``True``. At cancel time we combine
-    them to estimate where, in the concatenated text, the user's ear
-    fell silent.
+
+
+class _Passthrough:
+    """Stand-in for --nr off / --aec off paths: no-op both directions.
+
+    Matches ``replay.py``'s passthrough so both entry points take the
+    same shape when a stage is disabled.
     """
 
-    sentences_sent: list[str] = field(default_factory=list)
-    bytes_accepted: int = 0
-
-    def heard_text(self) -> str:
-        """Estimate the text prefix the user's ear actually reached.
-
-        Audio bytes map directly to playback duration (OpenAI TTS
-        emits a fixed-rate stream). Convert duration to characters
-        via the expected full-text byte count; clamp to the real
-        length so a complete turn returns the whole string.
-        """
-        if not self.sentences_sent:
-            return ""
-        full_text = " ".join(self.sentences_sent)
-        expected = max(1, _expected_bytes(full_text))
-        estimated_chars = int(len(full_text) * self.bytes_accepted / expected)
-        estimated_chars = max(0, min(estimated_chars, len(full_text)))
-        return full_text[:estimated_chars]
-
-
-def _expected_bytes(text: str) -> int:
-    """Very rough ~15 chars/s of speech at 48000 bytes/s of TTS audio."""
-    chars_per_sec = 15
-    seconds = len(text) / chars_per_sec
-    return int(seconds * TTS_BYTES_PER_SECOND)
-
-
-def _local_output_frames(chunk: AudioChunk) -> Iterator[AudioChunk]:
-    """Split TTS audio into all-or-nothing LocalTransport queue writes."""
-    frame_bytes = (
-        chunk.format.sample_rate * chunk.format.frame_size * LOCAL_OUTPUT_FRAME_MS // 1000
-    )
-    for offset in range(0, len(chunk.data), frame_bytes):
-        yield AudioChunk(
-            data=chunk.data[offset : offset + frame_bytes],
-            format=chunk.format,
-            timestamp=chunk.timestamp,
-        )
+    async def process(self, chunk):
+        return chunk
+
+    def feed_reference(self, chunk):
+        pass
+
+    def version_info(self):
+        return {"provider": "off"}
 
 
 class MiniTurnDetector:
@@ -144,16 +123,32 @@
                 self._preroll.append(chunk)
 
 
-async def mic_producer(detector, transport, queue: asyncio.Queue) -> None:
-    async for tag, chunk in detector.frames(transport.receive_audio()):
+async def clean_audio_pipeline(transport, nr, aec):
+    """Pipeline order: transport → NR → AEC → (downstream).
+
+    NR runs first so it sees the rawest noise spectrum possible.
+    AEC then subtracts the bot's own voice (reference-fed elsewhere).
+    VAD and STT live downstream in the detector + coordinator.
+    """
+    async for chunk in transport.receive_audio():
+        chunk = await nr.process(chunk)
+        chunk = await aec.process(chunk)
+        yield chunk
+
+
+async def mic_producer(detector, cleaned_audio, queue: asyncio.Queue) -> None:
+    async for tag, chunk in detector.frames(cleaned_audio):
         await queue.put((tag, chunk))
 
 
-async def run_agent(client, history, sentence_queue, cancel: CancelToken):
+async def run_agent(client, user_text, sentence_queue, cancel: CancelToken):
     stream = await client.chat.completions.create(
         model=MODEL,
         reasoning_effort="none",
-        messages=history,
+        messages=[
+            {"role": "system", "content": "You are a helpful voice assistant. Keep it brief."},
+            {"role": "user", "content": user_text},
+        ],
         stream=True,
     )
     buffer = ""
@@ -176,49 +171,38 @@
     await sentence_queue.put(None)
 
 
-async def drain_to_speaker(tts, transport, sentence_queue, cancel, ledger, journal):
+async def drain_to_speaker(tts, transport, aec, sentence_queue, cancel, session_id, journal):
+    """Emit TTS audio and feed accepted chunks to AEC as the far-end reference."""
     while True:
         sentence = await sentence_queue.get()
         if sentence is None or cancel.is_cancelled:
             break
-        ledger.sentences_sent.append(sentence)
         async for event in tts.synthesize(TTSInput(text=sentence)):
             if cancel.is_cancelled:
                 await tts.cancel()
                 break
-            if event.type == TTSEventType.AUDIO and event.audio is not None:
-                # LocalTransport reports False for a partial fit. Sending one
-                # callback-sized frame at a time makes acceptance atomic, so
-                # the ledger can still credit an accepted head accurately.
-                for frame in _local_output_frames(event.audio):
-                    if cancel.is_cancelled:
-                        await tts.cancel()
-                        break
-                    if await transport.send_audio(frame):
-                        ledger.bytes_accepted += len(frame.data)
-                if cancel.is_cancelled:
-                    break
+            if event.type == TTSEventType.AUDIO and event.audio is not None:  # noqa: SIM102 nested branches preserve decision context
+                if await transport.send_audio(event.audio):
+                    # The crucial dual-input line: AEC needs to know what
+                    # the speaker accepted, so it can subtract that pattern
+                    # from the mic. Rejected or partial writes return False.
+                    aec.feed_reference(event.audio)
         journal.append(
             kind=JournalRecordKind.EVENT,
             name="stage.tts.execute",
-            session_id=SESSION_ID,
-            data={
-                "stage": "tts",
-                "text": sentence,
-                "bytes_accepted_so_far": ledger.bytes_accepted,
-                "cancelled": cancel.is_cancelled,
-            },
+            session_id=session_id,
+            data={"stage": "tts", "text": sentence},
         )
 
 
-async def observe_bot_task(bot_task: asyncio.Task, journal) -> None:
+async def observe_bot_task(bot_task: asyncio.Task, journal, session_id: str) -> None:
     """Retrieve a background result so failures never become orphan warnings."""
     (result,) = await asyncio.gather(bot_task, return_exceptions=True)
     if isinstance(result, Exception):
         journal.append(
             kind=JournalRecordKind.EVENT,
             name="bot_task.error",
-            session_id=SESSION_ID,
+            session_id=session_id,
             data={"stage": "coordinator", "error": repr(result)},
         )
 
@@ -244,7 +228,7 @@
         await close_if_supported(stt)
 
 
-async def shutdown_coordinator(stt, bot_task, active_cancel, journal) -> None:
+async def shutdown_coordinator(stt, bot_task, active_cancel, journal, session_id) -> None:
     """Release both possible in-flight owners before shared providers close."""
     try:
         if stt is not None:
@@ -255,35 +239,35 @@
                 active_cancel.cancel()
             if not bot_task.done():
                 bot_task.cancel()
-            await observe_bot_task(bot_task, journal)
-
-
-async def route_barge_in(tag, bot_task, active_cancel, active_ledger, transport, journal, history):
-    """Cancel output, rewrite history, and preserve speech_started for STT."""
+            await observe_bot_task(bot_task, journal, session_id)
+
+
+async def route_barge_in(tag, bot_task, active_cancel, transport, journal, session_id):
+    """Cancel active output while preserving speech_started for STT below."""
     if bot_task is None:
-        return bot_task, active_cancel, active_ledger, False
+        return bot_task, active_cancel, False
     if bot_task.done():
-        await observe_bot_task(bot_task, journal)
-        return None, None, None, False
-    if tag != "speech_started" or active_cancel is None or active_ledger is None:
-        return bot_task, active_cancel, active_ledger, True
+        await observe_bot_task(bot_task, journal, session_id)
+        return None, None, False
+    if tag != "speech_started" or active_cancel is None:
+        return bot_task, active_cancel, True
 
     started_at = time.monotonic()
     journal.append(
         kind=JournalRecordKind.EVENT,
         name="interruption.start",
-        session_id=SESSION_ID,
+        session_id=session_id,
         data={"stage": "vad", "t_ms": started_at * 1000},
     )
     active_cancel.cancel()
     await transport.clear_audio()
     clear_returned_at = time.monotonic()
-    await observe_bot_task(bot_task, journal)
+    await observe_bot_task(bot_task, journal, session_id)
     bot_returned_at = time.monotonic()
     journal.append(
         kind=JournalRecordKind.EVENT,
         name="interruption.cancel_complete",
-        session_id=SESSION_ID,
+        session_id=session_id,
         data={
             "stage": "interruption",
             "cancel_to_clear_audio_return_ms": (clear_returned_at - started_at) * 1000,
@@ -291,54 +275,20 @@
             "t_ms": bot_returned_at * 1000,
         },
     )
-
-    heard = active_ledger.heard_text()
-    full = " ".join(active_ledger.sentences_sent)
-    journal.append(
-        kind=JournalRecordKind.EVENT,
-        name="interruption.estimate",
-        session_id=SESSION_ID,
-        data={
-            "stage": "interruption",
-            "full_text": full,
-            "heard_text": heard,
-            "bytes_accepted": active_ledger.bytes_accepted,
-        },
-    )
-    history.append({"role": "assistant", "content": heard})
-    print(f"  bot (cut): {heard!r}")
-    return None, None, None, False
-
-
-async def coordinator(mic_queue, stt_factory, client, tts, transport, journal):
-    """Maintain a multi-turn history and rewrite it on cancel."""
-    history: list[dict] = [
-        {
-            "role": "system",
-            "content": (
-                "You are a helpful voice assistant. "
-                "Give a long-ish answer so the reader has something to interrupt."
-            ),
-        }
-    ]
+    return None, None, False
+
+
+async def coordinator(mic_queue, stt_factory, client, tts, transport, aec, session_id, journal):
     stt = None
     bot_task: asyncio.Task | None = None
     active_cancel: CancelToken | None = None
-    active_ledger: TurnLedger | None = None
 
     try:
         while True:
             tag, chunk = await mic_queue.get()
 
-            # Cancel output, but fall through with speech_started intact.
-            bot_task, active_cancel, active_ledger, consumed = await route_barge_in(
-                tag,
-                bot_task,
-                active_cancel,
-                active_ledger,
-                transport,
-                journal,
-                history,
+            bot_task, active_cancel, consumed = await route_barge_in(
+                tag, bot_task, active_cancel, transport, journal, session_id
             )
             if consumed:
                 continue
@@ -356,41 +306,34 @@
                 if not final_text.strip():
                     continue
                 print(f"  user: {final_text!r}")
-                history.append({"role": "user", "content": final_text})
 
                 cancel = CancelToken()
-                ledger = TurnLedger()
                 active_cancel = cancel
-                active_ledger = ledger
-
-                async def _bot(hist=list(history), ct=cancel, led=ledger):
+
+                async def _bot(text=final_text, ct=cancel):
                     q: asyncio.Queue = asyncio.Queue()
                     await asyncio.gather(
-                        run_agent(client, hist, q, ct),
-                        drain_to_speaker(tts, transport, q, ct, led, journal),
+                        run_agent(client, text, q, ct),
+                        drain_to_speaker(tts, transport, aec, q, ct, session_id, journal),
                     )
-                    if not ct.is_cancelled:
-                        # Clean completion: record the full reply in history.
-                        full = " ".join(led.sentences_sent)
-                        history.append({"role": "assistant", "content": full})
-                        print(f"  bot: {full!r}")
 
                 bot_task = asyncio.create_task(_bot())
     finally:
-        await shutdown_coordinator(stt, bot_task, active_cancel, journal)
+        await shutdown_coordinator(stt, bot_task, active_cancel, journal, session_id)
 
 
 async def main() -> None:
+    ap = argparse.ArgumentParser()
+    ap.add_argument("--nr", choices=("on", "off"), default="off")
+    ap.add_argument("--aec", choices=("on", "off"), default="off")
+    args = ap.parse_args()
+
     if not (os.getenv("OPENAI_API_KEY") and os.getenv("DEEPGRAM_API_KEY")):
         raise SystemExit("Set OPENAI_API_KEY and DEEPGRAM_API_KEY.")
 
+    session_id = f"ch10-nr{args.nr}-aec{args.aec}-{int(time.time())}"
     journal = InMemoryRingBuffer(capacity=10_000)
-    transport = LocalTransport(
-        LocalTransportConfig(
-            audio_format=PCM16_MONO_24K,
-            frame_duration_ms=LOCAL_OUTPUT_FRAME_MS,
-        )
-    )
+    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))
 
     def stt_factory():
         return create_stt_provider(
@@ -405,6 +348,32 @@
         resources.push_async_callback(transport.disconnect)
         await transport.connect()
 
+        # Factory-wired stages. NR/AEC both fall back to passthrough if the
+        # optional deps aren't installed; the journal records which one is live.
+        if args.nr == "on":
+            nr = create_noise_reducer(NoiseReducerConfig())
+            nr_backend = nr.version_info().get("provider", "unknown")
+        else:
+            nr = _Passthrough()
+            nr_backend = "off"
+        resources.push_async_callback(close_if_supported, nr)
+
+        if args.aec == "on":
+            aec = create_echo_canceller(EchoCancellationConfig(enabled=True))
+            aec_backend = aec.version_info().get("provider", "unknown")
+        else:
+            aec = _Passthrough()
+            aec_backend = "off"
+        resources.push_async_callback(close_if_supported, aec)
+
+        print(f"NR backend: {nr_backend}    AEC backend: {aec_backend}")
+        journal.append(
+            kind=JournalRecordKind.EVENT,
+            name="audio.config",
+            session_id=session_id,
+            data={"stage": "audio", "nr": nr_backend, "aec": aec_backend},
+        )
+
         vad = create_vad(VADConfig())
         resources.push_async_callback(close_if_supported, vad)
         detector = MiniTurnDetector(vad)
@@ -416,19 +385,22 @@
         )
         resources.push_async_callback(close_if_supported, tts)
 
-        print("Cancel + history rewrite. Interrupt freely. Ctrl-C to stop.\n")
+        print("Talk. Ctrl-C to stop.\n")
 
         mic_queue: asyncio.Queue = asyncio.Queue()
+        cleaned = clean_audio_pipeline(transport, nr, aec)
         try:
             await asyncio.gather(
-                mic_producer(detector, transport, mic_queue),
-                coordinator(mic_queue, stt_factory, client, tts, transport, journal),
+                mic_producer(detector, cleaned, mic_queue),
+                coordinator(
+                    mic_queue, stt_factory, client, tts, transport, aec, session_id, journal
+                ),
             )
         except (KeyboardInterrupt, asyncio.CancelledError):
             pass
 
     RUNS_DIR.mkdir(exist_ok=True)
-    bundle_path = RUNS_DIR / f"{SESSION_ID}.bundle"
+    bundle_path = RUNS_DIR / f"{session_id}.bundle"
     session_stub = types.SimpleNamespace(journal=journal)
     export_debug_bundle(session_stub, bundle_path, overwrite=True)
     print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")
```

</details>
<!-- END auto:diff -->

## The naive predecessor

Before reaching for the right pipeline order, look at the wrong one:

```bash
# Run NR *after* VAD (no-op: VAD already classified the noise as speech)
uv run python docs/teaching/10-cleaning-signal/wrong_order.py --mode nr-after-vad

# Run AEC *without* feeding the reference (silently does nothing)
uv run python docs/teaching/10-cleaning-signal/wrong_order.py --mode aec-no-reference
```

Both modes are technically running NR / AEC, and both produce a
bundle. The journal shows the failure: each `vad.processed_before_nr`
record precedes the matching `nr.applied_after_vad` frame index in
`nr-after-vad`, and AEC's `feed_reference()` counter stays at zero
in `aec-no-reference`. **Wrong-version-
first** for pipeline ordering — the same components, wired
wrong, do nothing.

**Scope note.** This chapter isolates the NR/AEC axis. It drops
chapter 9c's `TurnLedger` / history rewrite (the LLM's memory
goes back to one-shot) so you can focus on the signal cleaning
without extra moving parts. If you want both, merge the two
files — nothing prevents it.

## Two ways to run this chapter

### A — live (speakerphone + real voice)

```bash
uv run python docs/teaching/10-cleaning-signal/main.py --nr off --aec off
uv run python docs/teaching/10-cleaning-signal/main.py --nr on  --aec off
uv run python docs/teaching/10-cleaning-signal/main.py --nr off --aec on
uv run python docs/teaching/10-cleaning-signal/main.py --nr on  --aec on
```

**For the AEC cell you need mic + speaker in the same laptop, no
headphones** — if the bot's audio never reaches the mic, AEC has
nothing to cancel. Chapter 9 asked you to use headphones; for
this chapter's AEC demo, take them off.

### B — offline replay (deterministic fixtures)

The synthetic fixture set is checked in, so you can replay a condition
directly from the repository root without a microphone or API keys:

```bash
uv run python docs/teaching/10-cleaning-signal/replay.py \
    --mic docs/teaching/10-cleaning-signal/recordings/speakerphone_loop.mic.wav \
    --ref docs/teaching/10-cleaning-signal/recordings/speakerphone_loop.ref.wav \
    --nr on --aec on
```

Maintainers intentionally rebuilding the tracked WAV fixtures run
`uv run python docs/teaching/10-cleaning-signal/generate_fixtures.py`
and review the resulting audio diff.

The fixtures are toy signals (sine-wave "voice," deterministic
white noise, a 30 ms echo at -18 dB) — enough to exercise the
lockstep `feed_reference` path and dump bundles the journal can
compare. They are **not** a substitute for a real speech test
set. Replace the WAV pairs with your own recordings for a real
eval.

The replay fails closed when `--aec on` has no `--ref`, or when the
mic and reference contain different numbers of complete 20 ms frames.
Silently running an adaptive echo canceller with a missing or exhausted
reference would recreate `wrong_order.py --mode aec-no-reference` while
pretending to be the correct path.

Run the provider-free metrics probe to exercise that contract and the
successful aligned path without native NR/AEC backends:

```bash
uv run python docs/teaching/10-cleaning-signal/replay_metrics_probe.py
```

The aligned case writes one `replay.frame` record per mic frame with
input/cleaned RMS, reference-feed presence, and VAD starts. The replay
sizes its in-memory journal from the mic frame count, reserving space
for `audio.config` and the summary so long recordings cannot evict
earlier frame evidence. Its
`replay.summary` adds aggregate `input_rms`, `cleaned_rms`,
`rms_change_db`, and `reference_frames_fed`.

**RMS is not a quality score.** A lower cleaned RMS proves that signal
energy changed; it does not prove noise or echo was removed correctly.
An over-aggressive filter can achieve a large reduction by deleting the
near-end speaker too. Pair these measurements with VAD/STT outcomes and
representative listening or perceptual metrics.

The replay path owns the same native-backed stages even though it has
no microphone or network connection. Its `AsyncExitStack` closes VAD,
AEC, and NR in reverse construction order after the last frame—or if
processing raises halfway through a fixture. Offline does not mean
resource-free.

## The pipeline

```mermaid
flowchart LR
    Mic[raw mic] --> NR["NR<br/>(fan,<br/>keyboard,<br/>baby)"]
    NR --> AEC --> VAD --> STT --> Agent[agent]
    Agent --> TTS
    TTS -- "aec.feed_reference<br/>(what we asked<br/>the speaker to play)" --> AEC
```

- **NR** is *single-input*. It sees only the mic and subtracts a
  learned model of stationary noise. It does **not** know what
  the bot is saying. From NR's perspective the bot's voice coming
  back through the speaker is *signal* — real speech.
- **AEC** is *dual-input*. It sees the mic *and* the far-end
  reference — the exact PCM we sent to the speaker. It correlates
  the two and subtracts the echo path's filtered version of the
  reference from the mic. That's why the chapter-10 code has a
  new line in `drain_to_speaker`:

  ```python
  await transport.send_audio(event.audio)
  aec.feed_reference(event.audio)  # ← only AEC needs this
  ```

The `NR → AEC → VAD` stage itself is one short coroutine; the
order *is* the lesson:

<!-- BEGIN auto:snippet src=main.py symbol=clean_audio_pipeline -->
```python
async def clean_audio_pipeline(transport, nr, aec):
    """Pipeline order: transport → NR → AEC → (downstream).

    NR runs first so it sees the rawest noise spectrum possible.
    AEC then subtracts the bot's own voice (reference-fed elsewhere).
    VAD and STT live downstream in the detector + coordinator.
    """
    async for chunk in transport.receive_audio():
        chunk = await nr.process(chunk)
        chunk = await aec.process(chunk)
        yield chunk
```
<!-- END auto:snippet -->

### Why this order

1. **NR before AEC.** AEC's adaptive filter still converges
   because it sees the raw reference on one side and the
   NR-processed mic on the other — it learns the combined
   (echo-path ∘ NR) mapping. NR-first lets NR see the rawest
   possible noise spectrum.
2. **VAD after both.** Before NR, VAD false-triggers on
   stationary noise. Before AEC, VAD false-triggers on the bot's
   own voice. After both, VAD only fires on the user.

Swap either and something specific breaks. Try it.

### Reference-timing caveat

`feed_reference` is called when we *send* a TTS chunk to the
transport, not when the speaker actually radiates it. The physical
echo will arrive at the mic tens of milliseconds later. LiveKit
APM's adaptive filter learns this delay as part of the echo path,
so small misalignments are fine. A large misalignment — e.g. the
TTS stream outruns the mic loop by hundreds of ms — breaks
convergence and you hear audible echo. Production pipelines
compensate with playback-ack marks.

## What's in the journal

Every run writes an `audio.config` record with the live backends:

```python
from pathlib import Path
from easycat.debug.testing import load_bundle

for b in Path("docs/teaching/10-cleaning-signal/runs/").glob("*.bundle"):
    bundle = load_bundle(b)
    for r in bundle.records():
        if r["name"] in ("audio.config", "replay.summary"):
            print(b.name, r["data"])
```

Expect config entries like `{"stage": "audio", "nr": "rnnoise", "aec": "livekit"}`
or `{"stage": "audio", "nr": "passthrough", "aec": "off"}` if the
extras weren't installed — *that* is where you catch the silent
backend fallback. The summary separately proves how many reference
frames were fed and how signal energy changed.

## Half-duplex vs. full-duplex

Speakerphone hardware is not inherently half-duplex. Hands-free
terminals can have full-, partial-, or no-duplex capability; the
[ITU-T P.340](https://www.itu.int/rec/T-REC-P.340/en) categories are
based on what happens during double-talk. Older or weaker systems may
switch gain or heavily attenuate one direction, producing the familiar
clipped, walkie-talkie behavior even when the network carries both
directions.

AEC is a key enabler of usable full-duplex hands-free audio. It models
the acoustic path from speaker output to microphone input and suppresses
that echo while preserving the near-end talker. Real systems also need
delay alignment, double-talk handling, and often nonlinear suppression;
“subtract the speaker” is the useful mental model, not the whole
implementation. Disabling AEC on a speakerphone can therefore make a
full-duplex path unusable, but it does not change the network into a
half-duplex transport.

Headsets reduce the acoustic path dramatically; they do not prove it is
zero, which is why platforms may still expose echo-control settings.

## Double-talk: the AEC failure mode

When the bot and the user speak at the *same time*, AEC's
adaptive filter has a moving target. Mainstream AECs (LiveKit APM
included) have a "double-talk detector" that freezes filter
adaptation during overlap; aggressive tuning clips the user's
voice audibly. This is the same physical problem as chapter 9's
barge-in, viewed from the other side. Tuning is per-deployment.

## Try breaking it

1. Type loudly on your keyboard while saying "hello." Run each of
   the four modes. Where does VAD fire in each?
2. Run on speakerphone (no headphones) with `--aec off`. The bot
   interrupts itself on chapter 9's `cancel.py` style pipeline.
   Then enable AEC. Compare.
3. Set NR to `off` and AEC to `on` with the `livekit` extra
   installed. AEC runs, but the signal it sees still has fan
   noise. Does the bot sound better, worse, or identical compared
   to NR on + AEC off? Why?

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

## What's next

[Chapter 11 — The journal as mental model](../11-journal/). The
ladder stops building and starts reading — teaching you the
single query surface the last ten chapters have been dumping
into.
