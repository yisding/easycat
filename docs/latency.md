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

### Runtime budget milestones (flat stage names)

The waterfall deltas above are reconstructed *offline* from journal events. At
*runtime*, when a `LatencyBudget` is configured for a stage, the session emits a
matching per-turn metric record under a flat stage name and feeds it through the
`LatencyBudgetMonitor`, so a breach lands in the journal/issue rollups while the
turn is still live. These flat names are the runtime lift of the waterfall
milestones, and `tts_ttfb_ms` / `llm_ttft_ms` are the *same measurement* as the
offline `easycat validate latency` percentile columns — not a duplicate
vocabulary.

| Runtime metric (flat) | Measured from → to | Equivalent waterfall milestone |
| --- | --- | --- |
| `stt_final_latency_ms` | turn ended → STT final | `vad_endpoint_to_stt_final_ms` |
| `llm_ttft_ms` | STT final → agent first token | `agent_request_to_first_token_ms` |
| `tts_ttfb_ms` | agent first token → TTS first byte | `agent_first_token_to_tts_first_byte_ms` |
| `first_audio_ms` | turn ended → TTS first byte | `vad_endpoint_to_tts_first_byte_ms` |
| `total_ms` | turn ended → TTS first byte (turn total) | — |

A budget may target either the flat name or the waterfall milestone name — both
resolve to the same stage. A milestone is only recorded when its budget is
configured (so the journal is not flooded) and only when both timing markers were
observed; an errored or text-only turn skips the milestones it never reached.

## Latency-adding defaults

These are the defaults that *add waiting time* on the response path. Each
value below is asserted against the code by a guard test
(`tests/observability/test_docs.py`), so this table cannot silently drift.

| Default | Value | Where it waits | Tuning guidance |
| --- | --- | --- | --- |
| `TurnManagerConfig.end_of_turn_silence_ms` | `800` | After VAD reports silence, the turn stays open this long before the agent is invoked. Usually the single largest fixed cost in `vad_endpoint_to_stt_final_ms`. | Set via `EasyConfig(turn_taking=TurnManagerConfig(...))`. 600–800 ms feels noticeably snappier; below ~400 ms expect mid-sentence cutoffs unless smart-turn is enabled. With `smart_turn=True` this becomes the *fallback* timer, so a confident endpoint ends the turn well before it expires. |
| `TurnManagerConfig.stt_segment_silence_ms` | `0` | Extra silence budget, after VAD stop, before the current STT segment is finalized. | Already zero — the segment commits as soon as VAD pauses. Raise it only if your STT provider splits sentences too eagerly; every millisecond lands directly on the response path. |
| `VADConfig.min_silence_duration_ms` | `150` | The VAD must observe this much continuous silence before emitting the stop-of-speech event that *starts* the end-of-turn countdown. | Adds directly in front of `end_of_turn_silence_ms`. Lowering makes endpointing twitchier on breaths and pauses; 100–200 ms is the practical range. |
| `VADConfig.min_speech_duration_ms` | `250` | Speech must persist this long before the VAD reports start-of-speech. | Delays turn start and barge-in detection slightly. Lowering increases false triggers from coughs and background noise. |
| `SmartTurnConfig.timeout_s` | `2.0` | Maximum wait to start or finish one smart-turn endpoint inference; on timeout the manager falls back to the silence timer. | Only applies with `smart_turn=True`. The bundled quantized model classifies in tens of milliseconds on CPU, so this ceiling rarely binds; lower it if a slow ONNX runtime should fail fast to the silence timer. |
| `AgentRunnerConfig.timeout` | `30.0` | Ceiling (seconds) on one wrapped agent run before `AgentTimeoutError`. | A safety net, not added per turn — but it bounds your worst case. If your agent should never take 30 s to speak, lower it so failures surface as errors instead of dead air. |
| `SessionConfig.interruption_ack_stale_ms` | `500` | On barge-in, playback acks older than this are treated as stale when estimating what the user actually heard. | Affects truncation accuracy after an interruption, not response speed. Tune together with the tail cap below for transports with infrequent acks. |
| `SessionConfig.interruption_ack_tail_cap_ms` | `500` | Maximum extra playout budget (beyond acked bytes) granted by the timing heuristic when acks are stale. | Larger values assume more audio reached the user before the interruption; keep the default unless transcripts show systematic over- or under-truncation. |

Sources: [`turn_manager.py`](../src/easycat/turn_manager.py),
[`vad/factory.py`](../src/easycat/vad/factory.py),
[`smart_turn.py`](../src/easycat/smart_turn.py),
[`integrations/agents/_agent_runner.py`](../src/easycat/integrations/agents/_agent_runner.py),
and [`session/_types.py`](../src/easycat/session/_types.py).

## What is *not* a knob

- **Provider time** — STT finalization, agent tokens, and TTS synthesis are
  network calls; the waterfall attributes them (`stt`, `agent`, `tts` spans)
  but no EasyCat default adds waiting there. Choose faster providers/models
  or stream more aggressively.
- **Sentence-boundary TTS streaming** — EasyCat starts synthesis at the first
  sentence boundary of the agent stream rather than waiting for the full
  reply; that behavior is structural, not configurable delay.
- **Latency budgets observe, they do not speed up** — `latency_budget=
  LatencyBudget(stage="tts", max_ms=500)` tags over-budget records so slow
  turns are findable; see [observability](observability.md) for the budget
  and alerting story. `LatencyBudget` (and the net-new `CostBudget`) live in
  the shared `easycat.budgets` API, whose `build_budget_report` evaluates the
  same budgets against runtime records, the waterfall `*_to_*_ms` milestones
  below, and the offline `easycat validate latency` percentile columns.

## A worked triage

1. `easycat bundles show .easycat/recordings/<bundle>.zip --json | jq '.turns'`
2. A turn shows `vad_endpoint_to_stt_final_ms: 980` — about 150 ms of VAD
   silence confirmation plus the 800 ms end-of-turn timer plus STT
   finalization. That is the configured floor, not a regression.
3. Enable `smart_turn=True` (or lower `end_of_turn_silence_ms`) and re-run;
   the same delta should drop to roughly the STT finalization cost.
4. If `agent_request_to_first_token_ms` dominates instead, the time is in
   your agent/LLM — no EasyCat default is involved; check the `agent` span
   and your model choice. (A large `stt_final_to_agent_request_ms` instead
   points at dispatch/queueing overhead before the LLM was even called.)
