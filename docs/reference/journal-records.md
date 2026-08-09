# Journal Record Reference

This page is the compatibility contract for record names emitted by EasyCat's
built-in runtime. It complements the storage and lifecycle details in the
[session lifecycle reference](session-lifecycle.md).

Record names and the required `data` keys below are stable across compatible
releases. New optional keys may be added. Readers should ignore keys they do not
recognize, and should treat a missing optional key as unknown rather than as a
zero, empty string, or false value. Write-time redaction can replace a value
while preserving its key.

Applications can append namespaced records with `Session.record(...)`. Those
application-defined names are outside this built-in vocabulary.

For the maintained app-builder route list and terminal journal summary, run:

```bash
uv run easycat docs --audience app-builders
uv run easycat explain journal
```

## Record Envelope

Every record has:

| Field | Type | Contract |
|---|---|---|
| `sequence` | `int` | Monotonic within a journal, except the documented `-1` degraded marker and `0` recovery marker. |
| `session_id` | `str` | Session that owns the record. |
| `kind` | `JournalRecordKind` | Discriminator shown in the catalog below. |
| `name` | `str` | Stable built-in name shown below. |
| `timing` | `TimingInfo` | Wall, monotonic, and process CPU clocks in nanoseconds. |
| `turn_id` | `str or null` | Turn correlation when the record belongs to a turn. |
| `data` | `dict[str, Any]` | Record payload. Required and optional keys are cataloged below. |
| `error` | `ErrorInfo or null` | Structured exception snapshot when present. |
| `input_ref` | `str or null` | Artifact reference for captured input. |
| `output_ref` | `str or null` | Artifact reference for captured output. |
| `tags` | `frozenset[str]` | Stable filtering tags. |

`EVENT`, `METRIC`, `CONTROL`, `FRAMEWORK_TRANSITION`, `DEGRADED`, and
`RECOVERY` below are `JournalRecordKind` values. `SPAN_START` and `SPAN_END`
remain available to journal users but EasyCat does not currently emit a named
built-in record with either kind.

## Pipeline Records

Variable stage payloads share one name. The required keys are common to every
variant; the optional column lists the built-in stage-specific keys that a
reader may inspect.

| Name | Kind | Required `data` keys | Optional or variant `data` keys |
|---|---|---|---|
| `aec_reference_frame` | `EVENT` | `stage: str`, `audio_bytes: int` | `sample_rate: int`, `channels: int`, `sample_width: int`, `encoding: str`, `duration_ms: number` |
| `agent_failure_fallback` | `EVENT` | `text: str`, `error_type: str` | - |
| `agent_usage` | `EVENT` | `run_id: str`; at least one token-count key | `provider: str`, `model: str`, `input_tokens: int`, `output_tokens: int`, `cached_input_tokens: int` |
| `interruption_note` | `EVENT` | `stage: str`, `note: str` | - |
| `markdown_stripped` | `EVENT` | `phase: str`, `changed: bool`, `original_text: str`, `stripped_text: str` | - |
| `provider_versions` | `EVENT` | - | Dynamic provider-role keys such as `stt`, `tts`, `transport`, `vad`, `noise_reducer`, `echo_canceller`, and `agent`; each value is a version-info mapping. |
| `replace_last_assistant_text` | `EVENT` | `stage: str`, `text: str` | - |
| `stage_complete` | `EVENT` | `stage: str` | `state_before: str`, `state_after: str`, `elapsed_ms: number`, `response: str`, `audio_bytes: int`, audio-format keys, `delivered: bool`, `events: list`, `prediction: Any`, `probability: number`, `frame_count: int`, `total_bytes: int` |
| `stage_error` | `EVENT` | `stage: str`, `error: str`, `elapsed_ms: number` | `state_before: str`, `input_sequence: int`, `input_record_ref: str` |
| `stage_start` | `EVENT` | `stage: str` | `state_before: str`, `input: Any`, `audio_bytes: int`, `sample_rate: int`, `channels: int`, `sample_width: int`, `encoding: str` |
| `stt_segment_commit_requested` | `EVENT` | `segment_index: int`, `transcript_text: str`, `pending_commit_bytes: int or null` | - |
| `stt_segment_commit_result` | `EVENT` | `segment_index: int`, `committed: bool`, `transcript_text: str` | - |
| `stt_segment_final` | `EVENT` | `segment_index: int`, `text: str`, `track: str or null`, `transcript_text: str` | `confidence: number`, `word_timestamps: list` |
| `text_turn_latency_ms` | `METRIC` | `value: number`, `surface: str` | - |
| `tts_frame` | `EVENT` | `stage: str`, `audio_bytes: int`, `frame_index: int` | `sample_rate: int`, `channels: int`, `sample_width: int`, `encoding: str`, `duration_ms: number` |
| `tts_payload_prepared` | `EVENT` | `is_streaming: bool`, `is_final: bool`, `changed: bool`, `original_text: str`, `original_format: str`, `prepared_text: str`, `prepared_format: str`, `processors: list[str]`, `ssml_downgraded: bool` | - |
| `turn_total_latency_ms` | `METRIC` | `value: number`, `from: str`, `to: str` | - |

## Session Event Records

These records project EasyCat events. The event's `session_id` and `turn_id`
remain top-level envelope fields, not `data` keys.

| Name | Kind | Required `data` keys | Optional or variant `data` keys |
|---|---|---|---|
| `agent_delta` | `EVENT` | - | Text form: `text: str` and optional `type: str`, `part_index: int`, `replacement: bool`; tool form: `type: str`, `tool_name: str`, `call_id: str`, or `result: Any`. |
| `agent_final` | `EVENT` | `text: str` | `structured_output: Any` |
| `agent_request_started` | `EVENT` | - | - |
| `bot_started_speaking` | `EVENT` | - | - |
| `bot_stopped_speaking` | `EVENT` | - | - |
| `call_answered` | `EVENT` | `call_sid: str` | `answered_by: str` |
| `call_ended` | `EVENT` | `call_sid: str` | `duration_s: number`, `disposition: str`, `number: str` |
| `call_failed` | `EVENT` | `call_sid: str`, `reason: str` | `sip_code: int`, `number: str` |
| `call_screening` | `EVENT` | `call_sid: str`, `platform: str` | - |
| `error` | `EVENT` | `stage: str`; top-level `error` is present | `provider: str`, `code: str`, `elapsed_ms: number`, `sequence: int`, `record_ref: str` |
| `interruption` | `CONTROL` | - | - |
| `playback_mark_ack` | `EVENT` | - | `mark_name: str`; older compatible bundles can omit it. |
| `session_action_completed` | `EVENT` | `action: object`, `executor: str`, `result: object` | Sensitive action/result values can be redacted. |
| `session_action_failed` | `EVENT` | `action: object`, `error: str` | `executor: str`; sensitive action values can be redacted. |
| `session_action_requested` | `EVENT` | `action: object` | Sensitive action values can be redacted. |
| `session_action_started` | `EVENT` | `action: object`, `executor: str` | Sensitive action values can be redacted. |
| `stt_final` | `EVENT` | `text: str` | `track: str` |
| `stt_partial` | `EVENT` | `text: str` | `track: str` |
| `supervisor_listener_attached` | `EVENT` | `listener_id: int`, `queue_size: int` | - |
| `supervisor_listener_detached` | `EVENT` | `listener_id: int`, `dropped_frames: int`, `reason: str` | - |
| `tool_call_delta` | `EVENT` | `call_id: str`, `delta: str` | - |
| `tool_call_result` | `EVENT` | `call_id: str`, `result: str` | - |
| `tool_call_started` | `EVENT` | `tool_name: str`, `call_id: str` | - |
| `transport_degraded` | `EVENT` or `CONTROL` | `reason: str`, `detail: str`, `fatal: bool` | `provider: str`; fatal records use `CONTROL`; recoverable records use `EVENT`. |
| `tts_audio` | `EVENT` | `audio_bytes: int`, `duration_ms: number`, `sample_rate: int`, `channels: int`, `sample_width: int`, `encoding: str`, `bypass_gate: bool` | - |
| `tts_markers` | `EVENT` | `markers: list` | - |
| `turn_ended` | `EVENT` | - | - |
| `turn_started` | `EVENT` | - | - |
| `vad_start_speaking` | `EVENT` | - | - |
| `vad_stop_speaking` | `EVENT` | - | - |
| `ws_reconnect_attempt` | `EVENT` | - | `provider: str`, `attempt: int`; older compatible bundles can omit both keys. |
| `ws_reconnect_failure` | `EVENT` | - | `provider: str`, `error: str`; older compatible bundles can omit both keys. |
| `ws_reconnect_success` | `EVENT` | - | `provider: str`; older compatible bundles can omit it. |

## Runtime Control Records

| Name | Kind | Required `data` keys | Optional or variant `data` keys |
|---|---|---|---|
| `assistant_interruption_notified` | `EVENT` | `source: str`, `mode: str`, `text_spoken: str`, `notified: bool` | - |
| `audio_queue_drop` | `EVENT` | `queue: str`, `kind: str`, `queue_len: int`, `total_drops: int` | - |
| `control_signal` | `CONTROL` | `stage: str`, `observed_stage: str`, `signal_kind: str`, `signal_id: str`, `direction: str`, `cause: str or null` | - |
| `control_signal_cause` | `CONTROL` | `signal_id: str`, `cause: str` | - |
| `pipeline_heartbeat` | `EVENT` | `interval_ms: int`, `loop_lag_ms: number`, `outbound_queue_len: int`, `outbound_queue_drops: int` | - |
| `task_cancelled` | `EVENT` | `task_name: str` | - |
| `task_completed` | `EVENT` | `task_name: str` | - |
| `task_raised` | `EVENT` | `task_name: str`, `exc_type: str` | - |
| `task_rejected` | `EVENT` | `task_name: str`, `reason: str` | - |
| `task_scheduled` | `EVENT` | `task_name: str` | - |
| `turn_state_changed` | `EVENT` | `from: str`, `to: str`, `reason: str or null` | - |
| `warmup_completed` | `EVENT` | `elapsed_ms: number`, `components: list[object]` | Each component object contains `component: str` and `elapsed_ms: number`. |
| `warmup_failed` | `CONTROL` | `component: str`, `elapsed_ms: number`, `exc_type: str` | - |

## Agent Bridge Records

Every bridge record includes `run_id: str` in `data`.

| Name | Kind | Required `data` keys | Optional or variant `data` keys |
|---|---|---|---|
| `cancellation_boundary` | `FRAMEWORK_TRANSITION` | `run_id: str`, `cancellation_mode: str`, `boundary_reason: str or null`, `caused_by_signal_id: str or null` | - |
| `framework_error` | `FRAMEWORK_TRANSITION` | `run_id: str`; top-level `error` is present | - |
| `framework_handoff` | `FRAMEWORK_TRANSITION` | `run_id: str`, `from_unit: str or null`, `to_unit: str`, `handoff_reason: str or null` | - |
| `interruption_apply_failed` | `FRAMEWORK_TRANSITION` | `run_id: str`, `mutation_kind: str`, `pre_state_ref: str or null`, `post_state_ref: str or null` | Top-level `error` can be present. |
| `state_committed` | `FRAMEWORK_TRANSITION` | `run_id: str`, `mutation_kind: str`, `pre_state_ref: str or null`, `post_state_ref: str or null`, `direction: str` | - |
| `state_snapshot` | `FRAMEWORK_TRANSITION` | `run_id: str`, `state_ref: str` | `output_ref` points at captured snapshot bytes when stored. |
| `tool_phase_changed` | `FRAMEWORK_TRANSITION` | `run_id: str`, `phase: str`, `tool_name: str`, `args_ref: str or null`, `result_ref: str or null`, `call_id: str or null` | - |
| `unit_entered` | `FRAMEWORK_TRANSITION` | `run_id: str`, `unit_id: str`, `unit_kind: str`, `display_name: str`, `parent_unit_id: str or null`, `committable: bool`, `direction: str` | - |
| `unit_exited` | `FRAMEWORK_TRANSITION` | `run_id: str`, `unit_id: str`, `unit_kind: str`, `display_name: str`, `committable: bool`, `exit_reason: str or null`, `direction: str` | - |

## Backend Marker Records

| Name | Kind | Required `data` keys | Optional or variant `data` keys |
|---|---|---|---|
| `buffer_overflow` | `CONTROL` | `dropped_from: str` | `gap: int` for a lagging `JournalView.follow()` cursor. |
| `journal_degraded` | `DEGRADED` | `error_type: str`, `error_message: str` | Uses sequence `-1`; inspect `JournalView.degraded` for the live health signal. |
| `recovered_session` | `RECOVERY` | `recovered_record_count: int`, `original_session_id: str` | Uses sequence `0` when SQLite recovers an unclean prior session. |

## Application Records

Use `session.record("app.<name>", data={...})` to append application facts to
the same live journal as EasyCat's runtime records. The method forces
`JournalRecordKind.EVENT`, accepts optional `turn_id` and `tags`, and applies
the journal's normal write-time redaction unchanged. Payloads must contain
JSON-native values with finite numbers and are snapshotted at the call
boundary. Tags are canonicalized to a `frozenset`; each tag must be non-empty
and cannot contain commas. An omitted `turn_id` inherits the active turn while
explicit `None` keeps the record session-scoped.

The `app.` namespace is required. Built-in names are rejected, and calls after
`session.stop()` raise `RuntimeError` because the preserved postmortem journal
is read-only. With journaling disabled, a valid call is a no-op.

## Contract Guard

`easycat.runtime.record_contracts` defines the built-in names, allowed kinds,
and required keys enforced at write time.
`tests/runtime/test_journal_record_contract.py` independently extracts producer
names from the full source tree and compares the runtime registry with every
catalog cell on this page. Adding or changing a built-in record therefore
requires an intentional runtime contract and documentation update.
