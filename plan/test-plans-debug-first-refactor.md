# End-to-End Test Plans: Debug-First Refactor PR

Five true end-to-end test plans that push real voice bytes through the full
EasyCat pipeline (transport → VAD → STT → agent → TTS → transport) and
validate the debug-first contract under stress and at the edges. Every plan
uses real audio in and real audio out — no scripted STT transcripts, no
silence-only inputs. Each plan calls out what it stresses, what corner cases
it covers, and the concrete pass criteria.

---

## Shared Test Infrastructure

All five plans depend on the same fixtures and utilities — define these once
in `tests/e2e/conftest.py`:

1. **Real speech fixtures, generated once per test session**
   ```python
   @pytest.fixture(scope="session")
   def voice_fixtures(tmp_path_factory):
       """Generate a handful of real speech utterances via OpenAI TTS.

       Caches to tmp dir so repeat runs reuse them. Skipped at session scope
       if OPENAI_API_KEY is missing — plans that depend on this fixture
       should pytest.skip if it's not available.
       """
       if not os.environ.get("OPENAI_API_KEY"):
           pytest.skip("OPENAI_API_KEY required for voice fixtures")
       cache = tmp_path_factory.mktemp("voice")
       utterances = {
           "question":    "What is the capital of France?",
           "greeting":    "Hello, how are you today?",
           "short":       "Yes.",
           "long":        "Tell me a story about a robot who learned to paint.",
           "interrupt":   "Stop talking right now.",
           "numbers":     "One two three four five six seven eight nine ten.",
           "quiet":       "barely audible whisper test",  # synthesized at -20dB
           "noisy":       "speech mixed with background hum",
       }
       out = {}
       for name, text in utterances.items():
           pcm24k = _render_tts(text, voice="alloy")
           pcm16k = resample(pcm24k, 24000, 16000)
           path = cache / f"{name}.pcm"
           path.write_bytes(pcm16k)
           out[name] = path
       return out
   ```

2. **Wire-level client for each transport**
   - `WSVoiceClient`: opens a real ws:// connection, negotiates sample rate
     via `{"type":"config"}`, streams binary PCM16 at real-time pace,
     collects outbound binary frames
   - `TwilioVoiceClient`: streams base64 μ-law frames at 8 kHz with
     correct `streamSid` threading, captures outbound `media`/`mark`/`clear`
   - `WebRTCVoiceClient`: aiortc peer with audio track; sends real speech
     frames, subscribes to returned track

3. **Audio verification helpers**
   - `decode_and_asr(pcm_bytes, rate)` → string: runs the outbound bytes
     through a reference STT (e.g. OpenAI) to verify the *audio content*
     matches what the agent said. This is the crucial "voice actually
     reached the user" check — not just "some bytes were sent."
   - `measure_rms(pcm_bytes)` → float: confirms audio has non-trivial energy
   - `detect_clipping(pcm_bytes)` → bool: verifies no saturation
   - `compare_audio(a, b, tolerance_pct=0.01)` → bool: byte-identical or
     within tolerance

4. **Standard assertion helpers**
   - `assert_journal_turn_complete(journal, turn_id)`: verifies the 6+
     expected events are present in correct order for a turn
   - `assert_no_dangling_artifacts(journal, store)`: every journal ref
     resolves in the store, and no orphaned artifacts remain

---

## Test Plan 1: Baseline Voice Round-Trip over WebSocket

### What it tests
One clean voice turn through the complete real stack: a Python WebSocket
client streams a real speech utterance ("What is the capital of France?")
into `WebSocketConnectionTransport` over a localhost connection. The
pipeline uses real OpenAI Whisper STT, real OpenAI Agents SDK agent, real
OpenAI TTS, and `debug="full"`. The client captures the returned PCM16
audio, re-transcribes it via a reference STT, and verifies the answer
("Paris") is actually audible in the outbound audio.

### Why it matters
This is the smoke test. If this doesn't work, nothing else matters. It
proves the entire real wire path works end to end:
- Real speech audio reaches STT and is transcribed correctly
- The real OpenAI Agents bridge produces a real agent response
- Real TTS generates audible speech that reaches the client
- The journal captures every stage with real provider version info and
  real artifact refs that resolve
- The bundle exported after the turn loads and replays

### Stress / corner-case coverage
- Real utterance audio (not synthesized tones), so STT must actually
  transcribe speech — sample-rate resampling (48 kHz client → 16 kHz
  pipeline → 24 kHz TTS → back to client rate) must preserve speech
  intelligibility end to end.

### Steps

1. **Skip guard**
   ```python
   @pytest.mark.integration_live
   @pytest.mark.integration_socket
   async def test_baseline_voice_roundtrip(voice_fixtures, tmp_path):
       if not os.environ.get("OPENAI_API_KEY"):
           pytest.skip("OPENAI_API_KEY required")
   ```

2. **Launch a real WebSocket server on a free port with real providers**
   ```python
   port = find_free_port()
   async def handler(ws):
       transport = WebSocketConnectionTransport(
           ws, WebSocketTransportConfig(sample_rate=16000))
       config = EasyCatConfig(
           stt=STTConfig(provider="openai", model="whisper-1"),
           tts=TTSConfig(provider="openai", model="tts-1", voice="alloy"),
           agent=Agent(name="assistant",
                       instructions="Answer in one short sentence.",
                       model="gpt-4o-mini"),
           transport=transport,
           debug="full",
       )
       session = create_session(config)
       handler.session = session
       await session.start()
       await session.wait_for_close()

   server = await websockets.serve(handler, "127.0.0.1", port)
   ```

3. **Client streams real speech at 48 kHz (simulating browser capture)**
   ```python
   speech_16k = voice_fixtures["question"].read_bytes()
   speech_48k = resample(speech_16k, 16000, 48000)

   async with WSVoiceClient(f"ws://127.0.0.1:{port}") as client:
       await client.negotiate_config(sample_rate=48000)
       await client.send_pcm_realtime(speech_48k, sample_rate=48000)
       await client.send_silence(seconds=1.0, sample_rate=48000)
       outbound_pcm, outbound_rate = await client.collect_response(
           timeout=30.0)
   ```

4. **Verify outbound audio has real speech energy**
   ```python
   assert len(outbound_pcm) > 0
   assert measure_rms(outbound_pcm) > 0.01
   assert not detect_clipping(outbound_pcm)
   ```

5. **Re-transcribe the outbound audio with a reference STT**
   ```python
   transcript = await decode_and_asr(outbound_pcm, rate=outbound_rate)
   assert "paris" in transcript.lower()
   ```

6. **Verify journal captured the full turn with real artifact refs**
   ```python
   session = handler.session
   events = session.journal.slice(kind=JournalRecordKind.EVENT)
   for name in ["turn_started", "stt_final", "agent_final",
                "bot_started_speaking", "bot_stopped_speaking", "turn_ended"]:
       assert any(e.name == name for e in events)

   stt_final = next(e for e in events if e.name == "stt_final")
   assert "france" in stt_final.data["text"].lower() or \
          "capital" in stt_final.data["text"].lower()

   fw = session.journal.slice(kind=JournalRecordKind.FRAMEWORK_TRANSITION)
   assert any(r.data.get("framework") == "openai_agents" for r in fw if r.data)
   ```

7. **Verify provider version info is in the manifest**
   ```python
   bundle_path = tmp_path / "baseline.zip"
   session.export_debug_bundle(bundle_path)
   bundle = RunBundle.load(bundle_path)
   versions = bundle.manifest.provider_versions
   assert versions["stt"]["provider"] == "openai"
   assert versions["tts"]["provider"] == "openai"
   assert "sdk_version" in versions["stt"]
   ```

8. **Teardown**
   ```python
   await session.stop()
   server.close()
   await server.wait_closed()
   ```

### Pass criteria
- Outbound audio RMS > 0.01 (real signal, not silence)
- No clipping in outbound audio
- Reference-STT re-transcription of outbound audio contains "paris"
- Journal contains all 6 turn-lifecycle events plus framework transitions
  from the OpenAI Agents bridge
- Bundle manifest carries real provider version strings
- Server closes cleanly


---

## Test Plan 2: Sustained Multi-Turn Stress + Concurrent Sessions

### What it tests
Long-running and high-concurrency scenarios that exercise resource
management in the new runtime:
- A single session running 50 consecutive voice turns with real audio for
  each turn (no accumulated memory, journal, or file-descriptor leaks)
- 10 concurrent WebSocket sessions each running 5 turns in parallel
- Continuous 10-minute call with periodic turns (checkpointing, retention,
  SQLite WAL growth)
- A session that outlives its journal's in-memory ring buffer capacity
  (overflow sentinels, artifact eviction)

### Why it matters
The debug-first refactor introduces new long-lived state: the SQLite
journal, the filesystem artifact store, per-turn cursor tracking, and
framework transition records. Under real load this state must not leak,
corrupt, or wedge the pipeline. A voice bot that starts dropping turns
after 30 minutes is worse than one that never worked at all.

### Stress / corner-case coverage
- Memory: 50 turns × ~10 artifacts each = 500 artifacts; measure RSS growth
- Journal size: SQLite DB must stay bounded via retention
- Concurrent event-bus subscribers: 10 sessions × ~12 handlers each
- File descriptors: each session opens SQLite, artifact dir, ws socket,
  agent HTTP client, TTS HTTP client
- Ring buffer overflow: verify `BufferOverflow` sentinel appears
- Artifact store eviction: verify in-memory store evicts oldest on cap

### Steps

#### 2a. Single session, 50 turns

1. **Launch a session with `debug="full"` and a mix of short/long utterances**
   ```python
   @pytest.mark.integration_live
   @pytest.mark.integration_socket
   @pytest.mark.slow
   async def test_sustained_fifty_turns(voice_fixtures, tmp_path):
       import resource
       rss_before = resource.getrusage(
           resource.RUSAGE_SELF).ru_maxrss
       utterances = [voice_fixtures[k] for k in
           ["short", "greeting", "question", "numbers", "long"]] * 10
   ```

2. **Start server + client, run 50 turns back-to-back**
   ```python
   async with WSVoiceClient(...) as client:
       for i, utt_path in enumerate(utterances):
           pcm = utt_path.read_bytes()
           await client.send_pcm_realtime(pcm, sample_rate=16000)
           await client.send_silence(seconds=0.8)
           outbound = await client.collect_turn_response(timeout=30.0)
           # Verify each turn produced real audio out
           assert measure_rms(outbound) > 0.01, f"turn {i} silent"
   ```

3. **Verify invariants across turns**
   ```python
   session = handler.session
   records = session.journal.read()
   turns = {r.turn_id for r in records if r.turn_id}
   assert len(turns) == 50

   # Sequence monotonicity across all 50 turns
   seqs = [r.sequence for r in records]
   assert seqs == sorted(seqs)
   assert len(set(seqs)) == len(seqs)

   # No degraded or buffer-overflow markers
   assert not session.journal.degraded
   assert not any(r.name == "buffer_overflow" for r in records)

   # Every artifact ref resolves
   assert_no_dangling_artifacts(session.journal, session._artifact_store)

   # Memory growth bounded (< 100 MB for 50 turns)
   rss_after = resource.getrusage(
       resource.RUSAGE_SELF).ru_maxrss
   assert rss_after - rss_before < 100 * 1024
   ```

4. **Verify SQLite retention kept DB bounded**
   ```python
   sqlite_path = pathlib.Path(".easycat/journals") / f"{session.session_id}.sqlite"
   assert sqlite_path.stat().st_size < 500 * 1024 * 1024  # <500 MB
   ```

#### 2b. 10 concurrent sessions × 5 turns each

1. **Launch 10 parallel server handlers on one shared port**
   (WebSocket server accepts multi-client in `examples/ws_server.py`)

2. **Launch 10 client tasks, each running 5 turns in parallel**
   ```python
   async def run_session(client_id):
       async with WSVoiceClient(f"ws://127.0.0.1:{port}") as client:
           results = []
           for _ in range(5):
               await client.send_pcm_realtime(
                   voice_fixtures["greeting"].read_bytes(),
                   sample_rate=16000)
               await client.send_silence(seconds=0.5)
               results.append(await client.collect_turn_response(timeout=30.0))
           return client_id, results

   results = await asyncio.gather(*[run_session(i) for i in range(10)])
   ```

3. **Verify all 10 sessions completed all 5 turns with real audio**
   ```python
   for cid, turns in results:
       assert len(turns) == 5
       for audio in turns:
           assert measure_rms(audio) > 0.01, f"client {cid} silent"
   ```

4. **Verify per-session journal isolation**
   ```python
   # Each session produced its own journal; no cross-contamination
   for session in active_sessions:
       records = session.journal.read()
       # Every record must carry this session's session_id
       assert all(r.session_id == session.session_id for r in records)
   ```

#### 2c. Ring buffer overflow in `debug="light"`

1. **Configure a tiny ring buffer**
   ```python
   from easycat.runtime.journal import InMemoryRingBuffer
   j = InMemoryRingBuffer(session_id="overflow", capacity=50)
   # Inject into a session built with debug="light"
   ```

2. **Run 20 turns (will produce >50 records)**

3. **Verify overflow sentinel + most-recent retention**
   ```python
   records = j.read()
   assert len(records) == 50
   assert any(r.name == "buffer_overflow" for r in records)
   # Earliest records evicted
   last_turn_id = records[-1].turn_id
   assert sum(1 for r in records if r.turn_id == last_turn_id) >= 3
   ```

### Pass criteria
- All 50 sequential turns produce non-silent outbound audio
- All 50 journal turn_ids are distinct; all sequences strictly monotonic
- No `degraded` or unexpected `buffer_overflow` markers
- Zero dangling artifact refs
- RSS growth < 100 MB across 50 turns
- SQLite DB stays < 500 MB (retention works)
- All 10 concurrent sessions complete 5 turns with real audio
- Per-session journal isolation: no cross-session records
- Ring buffer overflow produces sentinel and retains newest records


---

## Test Plan 3: Adversarial Audio and Transport Corner Cases

### What it tests
The pipeline's response to ugly real-world audio and transport conditions:
- Pure silence for 60 seconds (no false turn-starts)
- Background hum / broadband noise (VAD false-positive rate)
- Very quiet speech (-20 dB, still detected)
- Clipped/saturated audio (doesn't crash STT)
- Sample-rate mismatches (client sends 44.1 kHz when pipeline expects 16k)
- Mid-utterance WebSocket disconnect and reconnect
- Zero-byte binary frames
- Giant frames (10× normal size)
- Out-of-order / duplicate Twilio `media` frames
- Twilio `outbound_track` frames (must be ignored)
- μ-law decoding of non-standard bytes
- Malformed JSON control messages on WebSocket
- Provider failure mid-turn (STT HTTP 500, TTS connection reset)
- Transport disconnect during bot-speaking state

### Why it matters
Real calls contain background noise, network jitter, misconfigured clients,
and transient provider outages. The pipeline must degrade gracefully —
never crash the session loop, always record the error in the journal,
always emit a recoverable state transition.

### Stress / corner-case coverage
- VAD robustness against noise
- STT input validation (clipping, sample rate)
- Transport framing edge cases
- Provider-error handling (new `ErrorInfo` journal records)
- Reconnection without losing session state

### Steps

#### 3a. Silent-call corner case

1. **Stream 60 seconds of pure silence through WebSocket**
2. **Verify NO `turn_started` or `stt_final` records in journal**
3. **Session stays healthy; a subsequent real utterance produces a normal turn**

#### 3b. Background noise

1. **Generate broadband noise (`numpy.random.normal`) at -30 dB for 30 s**
2. **Mix 10 s of real speech (`voice_fixtures["question"]`) at -10 dB into the noise**
3. **Stream through pipeline with real providers**
4. **Verify exactly one `turn_started` + one `stt_final` containing "france" or "capital"**
   - False-positive turn_starts must not have fired during noise-only portion

#### 3c. Quiet speech (-20 dB)

1. **Scale `voice_fixtures["question"]` to 10% amplitude**
2. **Stream through pipeline**
3. **Verify VAD still fires and STT still transcribes (or skip gracefully)**

#### 3d. Sample-rate mismatch

1. **Resample `voice_fixtures["question"]` to 44100 Hz**
2. **Client declares `{"type":"config", "sample_rate":44100}`**
3. **Pipeline runs at 16 kHz internally**
4. **Verify resampler on transport side handles the conversion without
   distortion** (outbound audio re-transcribes correctly)

#### 3e. Mid-utterance disconnect + reconnect

1. **Stream first 500 ms of an utterance**
2. **Client closes ws socket abruptly**
3. **Server-side session observes disconnect, cleans up, marks journal**
   ```python
   events = session.journal.slice(kind=JournalRecordKind.EVENT)
   errors = session.journal.slice(kind=JournalRecordKind.EVENT)
   # Look for a clean "transport_closed" or Error event
   ```
4. **New client connects with same session-id hint; verify new session
   starts clean (does NOT resurrect the old session state)**

#### 3f. Malformed frames

1. **Send zero-byte binary frame** → transport must not crash
2. **Send 4 MB binary frame** → transport must chunk or reject gracefully
3. **Send `{"type":"garbage"}` JSON** → ignored with a warning, no state change
4. **Send invalid JSON `"not json"`** → warning, no state change

#### 3g. Twilio corner cases

1. **Send `media` frame with `track: "outbound_track"`** → ignored (ScriptedSTT
   receives no bytes from that frame)
2. **Send `media` frame with wrong `streamSid`** → ignored or logged
3. **Send base64-decoded-length-mismatch payload** → rejected gracefully
4. **Send 100 `media` frames with the same sequenceNumber** → journal
   doesn't duplicate turn events

#### 3h. Provider failure mid-turn

1. **Monkeypatch STT provider to raise `httpx.HTTPStatusError(500)` on the
   next request**
2. **Stream a real utterance**
3. **Verify session emits an `Error` event with `stage="stt"` and structured
   `ErrorInfo`**
   ```python
   errors = [r for r in session.journal.read() if r.error is not None]
   assert len(errors) >= 1
   assert errors[0].error.type == "HTTPStatusError"
   assert "500" in errors[0].error.message
   ```
4. **Session continues; next turn works normally after provider is unpatched**

#### 3i. Transport disconnect during bot speech

1. **Start a turn with a long agent response (use `voice_fixtures["long"]`
   driving agent output)**
2. **While bot is speaking, client closes ws socket**
3. **Verify:**
   - Session emits `TurnEnded` with interrupted state
   - Journal has a CONTROL `interruption` record with `cause` containing "disconnect"
   - Agent bridge's `apply_interruption` was called
   - No unfinished artifacts remain

### Pass criteria
- 60 s silence produces zero turn_started records
- Broadband noise does not trigger false turns
- Quiet speech either transcribes or fails gracefully (no crash)
- 44.1 kHz → 16 kHz resampling round-trips intelligibly
- Abrupt disconnect produces clean teardown with recorded error
- All malformed frames are rejected without crashing the session loop
- All Twilio corner cases produce correct (or no) journal records
- Provider failures are structured in the journal as `ErrorInfo`
- Session survives every corner case and accepts subsequent normal turns


---

## Test Plan 4: Interruption Matrix Across All Four Bridges

### What it tests
The four-step atomic interruption mutation (plan → commit → apply → record)
across the full matrix of bridges and interruption modes, driven by real
voice barge-in events:

| Bridge                     | Deep vs Shallow | Mutation kind             |
|----------------------------|-----------------|---------------------------|
| OpenAIAgentsBridge         | Deep            | interrupt_truncate        |
| PydanticAIBridge (Agent)   | Deep            | interrupt_truncate        |
| PydanticAIBridge (Graph)   | Deep            | interrupt_truncate_graph  |
| GenericWorkflowBridge shallow | Shallow      | downgrade → end-of-turn   |
| GenericWorkflowBridge deep | Deep            | interrupt_cancel_token    |
| GenericWorkflowBridge deep + `apply_interruption` | Deep | interrupt_workflow_override |
| RemoteResponsesAPIBridge   | Deep            | interrupt_n1_chain        |

For each bridge, drive a real voice turn with a long agent response, then
barge in with a short "stop" utterance at a known time offset. Verify:
- The `InterruptionController` seven-step flow emits the correct journal
  records in the correct order
- `FrameworkStateCommitted` appears BEFORE the mutation is applied
- If the mutation fails, `InterruptionApplyFailed` is emitted
- The next turn starts cleanly with the interrupted state reflected in the
  bridge (e.g., conversation history shows truncated assistant response +
  interruption note)
- Shallow mode emits `ShallowModeInterruptionError` and records a
  `shallow_mode_downgrade` control signal

### Why it matters
Interruption is the hardest coordination problem in real-time voice. It
crosses stage boundaries (user audio → STT → interruption controller →
bridge → agent framework state). The debug-first refactor's four-step
atomic write is the design that makes interruptions observable and
replayable. If any bridge's interruption path is broken, users will get
repeated phrases, duplicate turns, or wedged sessions.

### Stress / corner-case coverage
- Rapid-fire interruptions (3 barge-ins within 2 seconds)
- Interruption during tool-call execution (OpenAI Agents SDK)
- Interruption between graph nodes (PydanticAI Graph)
- Interruption before any delivered text (mutation with empty
  delivered_text)
- Interruption after full delivery (no mid-turn truncation needed)
- Simultaneous interruption + agent failure
- All three `CancellationMode` values (IMMEDIATE_STOP, DRAIN_CURRENT_UNIT,
  DRAIN_TO_COMMIT_POINT) on applicable bridges

### Steps

#### 4a. Single-interruption per bridge (7 sub-tests)

For each bridge in the matrix above:

1. **Construct the bridge with a verbose-responding agent**
   ```python
   # Example: OpenAI Agents SDK
   agent = Agent(
       name="verbose",
       instructions="Respond with a long 5-sentence answer.",
       model="gpt-4o-mini",
   )
   ```

2. **Start a voice session with `debug="full"` and a WebSocket transport**

3. **Stream the question voice fixture and wait for bot to start speaking**
   ```python
   await client.send_pcm_realtime(voice_fixtures["question"].read_bytes())
   await collector.wait_for(BotStartedSpeaking, timeout=10.0)
   # Let bot speak 500ms
   await asyncio.sleep(0.5)
   ```

4. **Barge in with the "interrupt" voice fixture**
   ```python
   await client.send_pcm_realtime(voice_fixtures["interrupt"].read_bytes())
   await client.send_silence(seconds=0.5)
   await collector.wait_for(Interruption, timeout=5.0)
   ```

5. **Verify the seven-step flow in the journal**
   ```python
   records = session.journal.read()

   # Step 1: ControlSignalRecord with cause="barge_in"
   signals = [r for r in records if r.kind == JournalRecordKind.CONTROL]
   barge = next(r for r in signals if "barge_in" in r.data.get("cause", ""))

   # Step 2: FrameworkStateCommitted (before mutation)
   committed = [r for r in records
                if r.name == "framework_state_committed"]
   assert len(committed) >= 1
   commit_seq = committed[0].sequence
   assert commit_seq > barge.sequence

   # Step 3: CancellationBoundary
   boundary = [r for r in records
               if r.name == "cancellation_boundary_reached"]
   assert len(boundary) >= 1
   assert boundary[0].sequence > commit_seq

   # If bridge supports rollback:
   apply_failed = [r for r in records
                   if r.name == "interruption_apply_failed"]
   # Expected: 0 in happy path
   assert len(apply_failed) == 0
   ```

6. **Verify bridge state reflects the interruption in next turn**
   ```python
   # Start a second turn: "what was the last word you said?"
   await client.send_pcm_realtime(voice_fixtures["numbers"].read_bytes())
   await client.send_silence(seconds=0.5)
   response_audio = await client.collect_turn_response()
   transcript = await decode_and_asr(response_audio, rate=24000)
   # Bridge should know the previous response was interrupted
   # (agent should acknowledge being interrupted or continue reasonably)
   ```

7. **Verify the outbound audio actually stopped after barge-in**
   ```python
   # The TTS stream should have been cut mid-sentence
   # Measure duration of outbound audio from the interrupted turn
   interrupted_audio = collect_outbound_between(
       bot_started_ts, interruption_ts)
   duration = len(interrupted_audio) / 2 / 24000  # seconds
   # Agent would normally speak 5+ seconds; we interrupted at 500ms
   assert duration < 2.0, "TTS did not stop promptly on interruption"
   ```

#### 4b. Shallow-mode downgrade

1. **Use a `GenericWorkflowBridge` with a shallow workflow
   (`on_user_turn(text)` signature, no `apply_interruption` method)**

2. **Drive a voice turn with barge-in at 500 ms**

3. **Verify downgrade path**
   ```python
   signals = [r for r in session.journal.read()
              if r.kind == JournalRecordKind.CONTROL]
   downgrades = [s for s in signals
                 if s.data.get("cause") == "shallow_mode_downgrade"]
   assert len(downgrades) == 1
   # Turn continues to completion (downgrade defers interruption to end)
   ```

#### 4c. Rapid-fire interruptions (stress)

1. **Use OpenAI Agents bridge**
2. **Drive 3 barge-ins within 2 seconds during one agent response**
3. **Verify:**
   - Each produces its own `ControlSignalRecord`
   - The bridge's final state reflects the LAST interruption's
     `delivered_text` (not a merge)
   - No wedged state; the next turn works normally
   - No duplicate `FrameworkStateCommitted` records beyond the number of
     actually-applied mutations

#### 4d. Interruption during tool call (OpenAI Agents SDK)

1. **Configure an agent with a slow mock tool (takes 3 s to return)**
2. **Drive a voice turn that triggers the tool**
3. **Barge in while the tool is running**
4. **Verify:**
   - Tool call is recorded with phase="start"
   - `CancellationMode.DRAIN_CURRENT_UNIT` was selected
   - Tool call eventually records phase="result" OR phase="error"
   - `CancellationBoundaryReached` record appears after the tool resolves
   - Next turn does not replay or duplicate the tool call

#### 4e. Pre-delivery interruption

1. **Barge in 100 ms after `turn_started`, BEFORE any `agent_delta`**
2. **Verify `FrameworkStateCommitted` has `delivered_text = ""`**
3. **Verify bridge handles empty-text mutation without error**

### Pass criteria
- For each bridge, the journal contains the seven-step interruption
  record sequence in order: signal → state_committed → mutation apply →
  cancellation_boundary
- `FrameworkStateCommitted` always precedes the actual mutation (atomicity)
- No `InterruptionApplyFailed` records in happy-path runs
- Outbound audio actually stops within 2 s of barge-in start
- Shallow-mode bridge records the downgrade control signal correctly
- Rapid-fire 3× barge-in produces 3 signal records and converges to a
  consistent bridge state
- Tool-call interruption drains cleanly; next turn is unaffected
- Pre-delivery interruption handles `delivered_text=""` without crashing


---

## Test Plan 5: Record → Bundle → Replay with Byte-Identical Voice Reproduction

### What it tests
The ultimate debug-first promise: record a real voice turn with full
providers, export the bundle, load it offline, replay from captured
artifacts, and verify the replayed TTS output is byte-identical (or within
a tiny tolerance window) to the original audio that reached the user. No
live APIs may be called during replay. Tool calls in replay must be blocked
by default policy.

### Why it matters
If a bundle can reproduce the exact bytes a user heard, engineers can debug
any production voice issue offline. This is the deliverable that justifies
the entire refactor. Validates:
- Every byte of audio that reached transport was captured as an artifact
- Every agent decision (text, tool call, structured output) was recorded
- The replay engine walks the journal correctly and substitutes captured
  outputs for live ones
- `ToolReplayPolicy.DENY` actually blocks live tool execution
- `ReplayFidelity.ARTIFACT` produces output matching the recording
- Provider-version mismatches are caught before replay divergence
- Redacted config snapshots do not prevent replay (safe config is
  sufficient to rebuild the pipeline)

### Stress / corner-case coverage
- Bundle with multiple turns (cross-turn journal continuity)
- Bundle with an interruption (partial TTS in a turn)
- Bundle with a tool call (replay must not execute the tool)
- Bundle with an agent error (replay reproduces the error path)
- Bundle exported after a clean stop (via `stop()` → export)
- Bundle exported from a crashed SQLite journal (`from_partial_journal`)
- Tampered bundle (one byte flipped in `journal.ndjson`) — rejected

### Steps

#### 5a. Baseline record → bundle → replay

1. **Record a real voice turn with full providers**
   ```python
   @pytest.mark.integration_live
   async def test_replay_byte_fidelity(voice_fixtures, tmp_path):
       if not os.environ.get("OPENAI_API_KEY"):
           pytest.skip()
       # ... set up session with real OpenAI STT/TTS/Agent, debug="full"
       # ... drive 3 turns with voice_fixtures["question"],
       #     voice_fixtures["greeting"], voice_fixtures["numbers"]
       live_outbound_per_turn = client.outbound_by_turn  # bytes per turn
       live_journal_records = session.journal.read()
   ```

2. **Export bundle after session stop**
   ```python
   bundle_path = tmp_path / "record.zip"
   await session.stop()
   session.export_debug_bundle(bundle_path)
   ```

3. **Load bundle and verify completeness**
   ```python
   bundle = RunBundle.load(bundle_path)
   loaded_records = list(bundle.records())
   assert len(loaded_records) == len(live_journal_records)

   # All artifact refs resolve
   for r in loaded_records:
       if r.get("input_ref"):
           assert r["input_ref"] in bundle.artifact_index
       if r.get("output_ref"):
           assert r["output_ref"] in bundle.artifact_index
   ```

4. **Replay with `ARTIFACT` fidelity**
   ```python
   spec = ReplaySpec(
       fidelity=ReplayFidelity.ARTIFACT,
       tool_policy=ToolReplayPolicy.DENY,
       timing="fast",
   )
   replay_runner = build_replay_runner(bundle, spec)
   replay_outbound_per_turn = await replay_runner.run()
   ```

5. **Verify byte-identical TTS output per turn**
   ```python
   for turn_idx in range(3):
       live_audio = live_outbound_per_turn[turn_idx]
       replay_audio = replay_outbound_per_turn[turn_idx]
       assert live_audio == replay_audio, \
           f"turn {turn_idx} audio diverged: " \
           f"len {len(live_audio)} vs {len(replay_audio)}"
   ```

6. **Verify deterministic journal fields match (ignoring timing fields)**
   ```python
   for live, repl in zip(live_journal_records, loaded_records):
       live_scrub = scrub(live, REPLAY_IGNORE_FIELDS)
       repl_scrub = scrub(repl, REPLAY_IGNORE_FIELDS)
       assert live_scrub["name"] == repl_scrub["name"]
       assert live_scrub["kind"] == repl_scrub["kind"]
       assert live_scrub.get("data", {}).get("text") == \
              repl_scrub.get("data", {}).get("text")
   ```

7. **Verify NO live API calls were made during replay**
   ```python
   # Monitor network; use an HTTP blocker
   with patch("httpx.AsyncClient.request",
              side_effect=AssertionError("live call during replay!")):
       await replay_runner.run()
   ```

#### 5b. Replay a turn that had an interruption

1. **Record a session where turn 2 is interrupted mid-TTS (drive a barge-in)**
2. **Bundle the session**
3. **Replay turn 2 with `ARTIFACT` fidelity**
4. **Verify the replayed outbound audio is byte-identical to the partial
   (interrupted) audio from the live run** — i.e. replay reproduces the
   interruption point, not the full response

#### 5c. Replay a turn with a tool call (DENY policy)

1. **Record a session where the agent calls a tool**
2. **Replay with `ReplaySpec(fidelity=LIVE, tool_policy=DENY)`**
3. **Verify `ReplaySideEffectBlocked` is raised when the tool would execute**
4. **Replay with `ReplaySpec(fidelity=ARTIFACT, tool_policy=DENY)`**
5. **Verify the recorded tool result is returned from artifacts; no exception**

#### 5d. Replay from a crashed SQLite journal

1. **Create a session, run 2 turns with `debug="full"`**
2. **Kill the process (`os.kill(pid, SIGKILL)`) without calling `stop()`**
3. **Restart; find the prior journal in `.easycat/journals/`**
4. **Recover via `RunBundle.from_partial_journal(journal_path, artifacts_dir)`**
5. **Verify recovery produces a valid bundle with the 2 turns present**
6. **Replay turn 1 with `ARTIFACT` fidelity**
7. **Verify `RecoveredSessionMarker` is at sequence=0 of the new session
   spawned after the crash**

#### 5e. Provider-version-mismatch guard

1. **Record a bundle with `openai` STT sdk_version "1.2.3"**
2. **Edit the manifest to declare sdk_version "9.9.9"**
3. **Replay with strict version checking → `ProviderVersionMismatchError`**
4. **Replay with `ReplaySpec(force=True)` → succeeds with a warning**

### Pass criteria
- Replayed TTS outbound audio is byte-identical to live outbound audio
  across all 3 turns (5a)
- All artifact refs in the bundle resolve; no dangling refs
- No live HTTP/HTTPS requests are made during `ARTIFACT` replay
- Interrupted-turn replay reproduces the exact interruption point, not
  the full response (5b)
- Tool-call replay with `DENY` + `LIVE` raises `ReplaySideEffectBlocked`;
  with `DENY` + `ARTIFACT` returns the recorded result (5c)
- Crashed-journal recovery produces a loadable bundle with all recorded
  turns intact (5d)
- `RecoveredSessionMarker` appears at sequence=0 of the post-crash session
- Provider-version-mismatch raises `ProviderVersionMismatchError` unless
  `force=True` (5e)
