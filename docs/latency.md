# Why Was That Turn Slow? (Latency in EasyCat)

Voice latency is mostly *configured*, not mysterious. The pipeline waits at
several well-defined points — VAD silence detection, the end-of-turn timer,
the agent, TTS synthesis — and every one of those waits has a default you can
read, measure per turn, and tune. This page is the map: first how to see
where a specific turn spent its time, then the table of latency-adding
defaults and how to change them.

From this repository, run `uv run easycat docs --audience operators` for the
full operator route slice; [observability](observability.md) explains the
journal layers these tools read from.

## Read the per-turn waterfall

Every session journal already records per-stage spans and the milestone
events below. Three surfaces render them:

- **CLI** — `easycat bundles show PATH --json` and
  `easycat inspect PATH --json` return a `turns` array:
  one entry per turn with `wall_ms`,
  per-stage `spans` (`stage`, `offset_ms`, `duration_ms`, `record_count`),
  and `milestones` deltas. The human (no `--json`) output renders the same
  waterfall as a `Per-turn latency` table.
- **CLI percentiles** — `easycat latency PATH` rolls the milestone deltas
  up across all turns and reports `count`/`p50`/`p90`/`p95`/`p99` for the
  five critical-path segments (`vad->stt`, `stt->req`, `req->token`,
  `token->tts`, `vad->tts`). It reuses the same percentile math as
  `easycat validate latency`, so use it when one bundle has enough turns to
  ask "what is my p95 time-to-first-token?" without a full validation run.
  `easycat latency PATH --json` emits `{turns, percentiles}`.
- **Debugger UI** — `serve_bundle(...)` / `serve_session(...)` render the
  interactive waterfall (`uv sync --extra debugger --group dev` from this
  repo). The CLI and the UI share one implementation
  (`easycat.debug._turn_timeline`), so the numbers always agree.
- **Validation** — `easycat validate latency --smoke` aggregates P50/P95
  percentiles across turns when you need a distribution, not one turn.

The milestone chain is the response-latency skeleton of a turn:

```text
VAD endpoint ──► STT final ──► agent request ──► agent first token ──► TTS first byte
   (user stopped     (transcript      (run             (LLM started        (bot started
    speaking)         committed)       dispatched)      answering)           sounding)
```

| Milestone delta | Journal records it is computed from |
| --- | --- |
| `vad_endpoint_to_stt_final_ms` | last `vad_stop_speaking` before the turn's first `stt_final` |
| `stt_final_to_agent_request_ms` | first `stt_final` → first `agent_request_started` (dispatch/queueing overhead) |
| `agent_request_to_first_token_ms` | first `agent_request_started` → first `agent_delta` (or `agent_final`); the raw LLM time-to-first-token |
| `agent_first_token_to_tts_first_byte_ms` | first `agent_delta` → first `tts_frame` / `tts_audio` |
| `vad_endpoint_to_tts_first_byte_ms` | the full voice-to-voice response gap |
| `user_speech_start_to_bot_stopped_ms` | first `vad_start_speaking` inside a playback window → first `bot_stopped_speaking` / `playback_mark_ack`; the barge-in cutoff (how long the bot kept talking after the user spoke over it) |

A delta is `null` when a turn never reached that milestone — text turns have
no VAD endpoint, and a turn that errored before synthesis has no TTS byte. The
`user_speech_start_to_bot_stopped_ms` barge-in delta is `null` for turns the
user never interrupted.

## Latency-adding defaults

These are the defaults that *add waiting time* on the response path. Each
value below is asserted against the code by a guard test
(`tests/observability/test_docs.py`), so this table cannot silently drift.

| Default | Value | Where it waits | Tuning guidance |
| --- | --- | --- | --- |
| `TurnManagerConfig.end_of_turn_silence_ms` | `500` | After VAD reports silence, the turn stays open this long before the agent is invoked. Usually the single largest fixed cost in `vad_endpoint_to_stt_final_ms`. | Set via `EasyConfig(turn_taking=TurnManagerConfig(...))`. 500–800 ms feels noticeably snappier; below ~400 ms expect mid-sentence cutoffs unless smart-turn is enabled. With `smart_turn=True` this becomes the *fallback* timer, so a confident endpoint ends the turn well before it expires. |
| `TurnManagerConfig.punctuated_end_of_turn_silence_ms` | `200` | Shortens the fixed timer only after STT finalizes text ending in terminal punctuation. | Set to `None` to disable. Smart-turn incomplete/error decisions retain the full fallback timer, so punctuation never overrides a semantic incomplete verdict. |
| `TurnManagerConfig.stt_segment_silence_ms` | `0` | Extra silence budget, after VAD stop, before the current STT segment is finalized. | Already zero — the segment commits as soon as VAD pauses. Raise it only if your STT provider splits sentences too eagerly; every millisecond lands directly on the response path. |
| `VADConfig.min_silence_duration_ms` | `50` | The VAD must observe this much continuous silence before emitting the stop-of-speech event that *starts* the end-of-turn countdown. | Adds directly in front of `end_of_turn_silence_ms`. Lowering makes endpointing twitchier on breaths and pauses; 50–200 ms is the practical range. |
| `VADConfig.min_speech_duration_ms` | `250` | Speech must persist this long before the VAD reports start-of-speech. | Delays turn start and barge-in detection slightly. Lowering increases false triggers from coughs and background noise. |
| `SmartTurnConfig.timeout_s` | `2.0` | Maximum wait to start or finish one smart-turn endpoint inference; on timeout the manager falls back to the silence timer. | Applies whenever smart-turn is on — which is the default for the local-microphone transport (`EasyConfig.mic()`) and off for the server/browser/telephony transports; pass `smart_turn=False`/`True` to override. The bundled quantized model classifies in tens of milliseconds on CPU, so this ceiling rarely binds; lower it if a slow ONNX runtime should fail fast to the silence timer. |
| `AgentRunnerConfig.timeout` | `30.0` | Ceiling (seconds) on one wrapped agent run before `AgentTimeoutError`. | A safety net, not added per turn — but it bounds your worst case. If your agent should never take 30 s to speak, lower it so failures surface as errors instead of dead air. |
| `SessionConfig.interruption_ack_stale_ms` | `500` | On barge-in, playback acks older than this are treated as stale when estimating what the user actually heard. | Affects truncation accuracy after an interruption, not response speed. Tune together with the tail cap below for transports with infrequent acks. |
| `SessionConfig.interruption_ack_tail_cap_ms` | `500` | Maximum extra playout budget (beyond acked bytes) granted by the timing heuristic when acks are stale. | Larger values assume more audio reached the user before the interruption; keep the default unless transcripts show systematic over- or under-truncation. |

Sources: [`turn_manager.py`](../src/easycat/turn_manager.py),
[`vad/factory.py`](../src/easycat/vad/factory.py),
[`smart_turn.py`](../src/easycat/smart_turn.py),
[`integrations/agents/_agent_runner.py`](../src/easycat/integrations/agents/_agent_runner.py),
and [`session/_types.py`](../src/easycat/session/_types.py).

Plain `async run(text) -> str` agents can overlap model work with endpoint
confirmation by setting
`AgentRunnerConfig(preemptive_generation=True)`. This is intentionally opt-in:
the agent may be cancelled and retried when speech resumes, so its `run()`
implementation must be replayable and must not perform irreversible side
effects for an unconfirmed transcript.

## Provider-specific tuning

- **OpenAI Realtime STT connection setup** — the provider keeps its
  transcription WebSocket warm across turns by default, using each
  `input_audio_buffer.commit` to delimit and clear a logical turn. Set
  `OpenAIRealtimeSTTConfig.persistent_ws=False` to restore one socket per
  turn. A final-transcript timeout discards the reusable socket before the
  next turn so a late final cannot leak into the replacement transcript queue.
- **Deepgram Nova STT connection setup** — EasyCat keeps Deepgram Nova's STT
  WebSocket warm across turns by default, sends a provider `KeepAlive` while
  idle, and uses `Finalize` to delimit each turn; set
  `DeepgramSTTConfig.persistent_ws=False` to restore one socket per turn.
  Flux keeps the one-socket-per-turn lifecycle because its v2 endpoint does
  not support explicit `Finalize`.

## What is *not* a knob

- **Provider time** — STT finalization, agent tokens, and TTS synthesis are
  network calls; the waterfall attributes them (`stt`, `agent`, `tts` spans)
  but no EasyCat default adds waiting there. Choose faster providers/models
  or stream more aggressively. OpenAI TTS consumes the HTTP response at its
  native cadence, releases the first 20 ms of PCM immediately, then coalesces
  steady-state audio into 100 ms frames; this avoids making first audio wait
  for a full steady-state frame without increasing per-frame overhead for the
  rest of the utterance. The bounded final-transcript knobs are
  `OpenAIRealtimeSTTConfig.final_transcript_timeout_s` (default `0.9` s): the
  bounded wait for OpenAI's end-of-turn `...transcription.completed` before the
  provider promotes its delta-accumulated partial to the turn's final. OpenAI
  occasionally stalls several seconds on that event, so the wait caps the
  worst-case end-of-turn pause; lower it to trade a little tail correction for
  snappier handoff, raise it if you see truncated end-of-turn transcripts.
  `DeepgramSTTConfig.final_transcript_timeout_s` similarly defaults to `2.0`
  seconds for a persistent Nova `Finalize`; on timeout EasyCat drops the stale
  socket (a final already buffered in the close window is still delivered to
  the ending turn), promotes the latest interim only when no final arrived,
  and reconnects next turn so late text cannot leak across the turn boundary.
- **Sentence-boundary TTS streaming** — EasyCat starts synthesis early in the
  agent stream rather than waiting for the full reply. The *first* payload of a
  turn is cut at the first natural clause boundary (comma/semicolon/colon, as
  long as the clause is long enough to not sound clipped) to shave
  time-to-first-audio; every later payload keeps full-sentence granularity.
  That behavior is structural, not configurable delay.
- **Bot-start lifecycle overlap** — on the first TTS payload, EasyCat starts
  the provider request while `BotStartedSpeaking` handlers run. A one-shot
  barrier preserves the public order (`BotStartedSpeaking` before
  `AgentFinal`/`TTSAudio`) and prevents audio release until every lifecycle
  handler completes, so handler latency and provider first-byte latency overlap
  instead of adding together.
- **Latency is reported, not gated** — every stage records its `elapsed_ms` to
  the journal and each turn emits a `turn_total_latency_ms` (voice) /
  `text_turn_latency_ms` (text) metric record, so slow turns are findable; see
  [observability](observability.md). EasyCat does not slow down or reject turns
  to hit a target. For regression gating in CI, use `easycat validate latency`.

## A worked triage

1. `easycat bundles show .easycat/recordings/<bundle>.zip --json | jq '.turns'`
2. A turn shows `vad_endpoint_to_stt_final_ms: 580` — about 50 ms of VAD
   silence confirmation plus the 500 ms end-of-turn timer plus STT
   finalization. That is the configured floor, not a regression.
3. Enable `smart_turn=True` (on by default for the local-mic transport) or
   lower `end_of_turn_silence_ms`, then re-run; the same delta should drop to
   roughly the STT finalization cost.
4. If `agent_request_to_first_token_ms` dominates instead, the time is in
   your agent/LLM — no EasyCat default is involved; check the `agent` span
   and your model choice. (A large `stt_final_to_agent_request_ms` instead
   points at dispatch/queueing overhead before the LLM was even called.)
