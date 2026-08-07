"""Built-in journal record contracts shared by producers and documentation guards."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from easycat.runtime.records import JournalRecordKind


@dataclass(frozen=True, slots=True)
class BuiltinRecordContract:
    """Allowed kinds and always-present payload keys for one built-in record."""

    kinds: frozenset[JournalRecordKind]
    required_data_keys: frozenset[str] = frozenset()


_KIND_NAMES = {
    JournalRecordKind.EVENT: [
        "aec_reference_frame",
        "interruption_note",
        "markdown_stripped",
        "provider_versions",
        "replace_last_assistant_text",
        "stage_complete",
        "stage_error",
        "stage_start",
        "stt_segment_commit_requested",
        "stt_segment_commit_result",
        "stt_segment_final",
        "tts_frame",
        "tts_payload_prepared",
        "agent_delta",
        "agent_failure_fallback",
        "agent_final",
        "agent_request_started",
        "agent_usage",
        "bot_started_speaking",
        "bot_stopped_speaking",
        "call_answered",
        "call_ended",
        "call_failed",
        "call_screening",
        "error",
        "playback_mark_ack",
        "session_action_completed",
        "session_action_failed",
        "session_action_requested",
        "session_action_started",
        "stt_final",
        "stt_partial",
        "supervisor_listener_attached",
        "supervisor_listener_detached",
        "tool_call_delta",
        "tool_call_result",
        "tool_call_started",
        "transport_degraded",
        "tts_audio",
        "tts_markers",
        "turn_ended",
        "turn_started",
        "vad_start_speaking",
        "vad_stop_speaking",
        "ws_reconnect_attempt",
        "ws_reconnect_failure",
        "ws_reconnect_success",
        "assistant_interruption_notified",
        "audio_queue_drop",
        "pipeline_heartbeat",
        "task_cancelled",
        "task_completed",
        "task_raised",
        "task_rejected",
        "task_scheduled",
        "turn_state_changed",
        "warmup_completed",
    ],
    JournalRecordKind.METRIC: ["text_turn_latency_ms", "turn_total_latency_ms"],
    JournalRecordKind.CONTROL: [
        "interruption",
        "transport_degraded",
        "control_signal",
        "control_signal_cause",
        "warmup_failed",
        "buffer_overflow",
    ],
    JournalRecordKind.FRAMEWORK_TRANSITION: [
        "cancellation_boundary",
        "framework_error",
        "framework_handoff",
        "interruption_apply_failed",
        "state_committed",
        "state_snapshot",
        "tool_phase_changed",
        "unit_entered",
        "unit_exited",
    ],
    JournalRecordKind.DEGRADED: ["journal_degraded"],
    JournalRecordKind.RECOVERY: ["recovered_session"],
}

_REQUIRED_KEYS = {
    "aec_reference_frame": "stage audio_bytes",
    "interruption_note": "stage note",
    "markdown_stripped": "phase changed original_text stripped_text",
    "replace_last_assistant_text": "stage text",
    "stage_complete": "stage",
    "stage_error": "stage error elapsed_ms",
    "stage_start": "stage",
    "stt_segment_commit_requested": "segment_index transcript_text pending_commit_bytes",
    "stt_segment_commit_result": "segment_index committed transcript_text",
    "stt_segment_final": "segment_index text track transcript_text",
    "text_turn_latency_ms": "value surface",
    "tts_frame": "stage audio_bytes frame_index",
    "tts_payload_prepared": (
        "is_streaming is_final changed original_text original_format prepared_text "
        "prepared_format processors ssml_downgraded"
    ),
    "turn_total_latency_ms": "value from to",
    "agent_final": "text",
    "agent_usage": "run_id",
    "agent_failure_fallback": "text error_type",
    "call_answered": "call_sid",
    "call_ended": "call_sid",
    "call_failed": "call_sid reason",
    "call_screening": "call_sid platform",
    "error": "stage",
    "session_action_completed": "action executor result",
    "session_action_failed": "action error",
    "session_action_requested": "action",
    "session_action_started": "action executor",
    "stt_final": "text",
    "stt_partial": "text",
    "supervisor_listener_attached": "listener_id queue_size",
    "supervisor_listener_detached": "listener_id dropped_frames reason",
    "tool_call_delta": "call_id delta",
    "tool_call_result": "call_id result",
    "tool_call_started": "tool_name call_id",
    "transport_degraded": "reason detail fatal",
    "tts_audio": "audio_bytes duration_ms sample_rate channels sample_width encoding bypass_gate",
    "tts_markers": "markers",
    "assistant_interruption_notified": "source mode text_spoken notified",
    "audio_queue_drop": "queue kind queue_len total_drops",
    "control_signal": "stage observed_stage signal_kind signal_id direction cause",
    "control_signal_cause": "signal_id cause",
    "pipeline_heartbeat": "interval_ms loop_lag_ms outbound_queue_len outbound_queue_drops",
    "task_cancelled": "task_name",
    "task_completed": "task_name",
    "task_raised": "task_name exc_type",
    "task_rejected": "task_name reason",
    "task_scheduled": "task_name",
    "turn_state_changed": "from to reason",
    "warmup_completed": "elapsed_ms components",
    "warmup_failed": "component elapsed_ms exc_type",
    "cancellation_boundary": "run_id cancellation_mode boundary_reason caused_by_signal_id",
    "framework_error": "run_id",
    "framework_handoff": "run_id from_unit to_unit handoff_reason",
    "interruption_apply_failed": "run_id mutation_kind pre_state_ref post_state_ref",
    "state_committed": "run_id mutation_kind pre_state_ref post_state_ref direction",
    "state_snapshot": "run_id state_ref",
    "tool_phase_changed": "run_id phase tool_name args_ref result_ref call_id",
    "unit_entered": ("run_id unit_id unit_kind display_name parent_unit_id committable direction"),
    "unit_exited": ("run_id unit_id unit_kind display_name committable exit_reason direction"),
    "buffer_overflow": "dropped_from",
    "journal_degraded": "error_type error_message",
    "recovered_session": "recovered_record_count original_session_id",
}


def _build_contracts() -> dict[str, BuiltinRecordContract]:
    kinds_by_name: dict[str, set[JournalRecordKind]] = {}
    for kind, names in _KIND_NAMES.items():
        for name in names:
            kinds_by_name.setdefault(name, set()).add(kind)
    return {
        name: BuiltinRecordContract(
            kinds=frozenset(kinds),
            required_data_keys=frozenset(_REQUIRED_KEYS.get(name, "").split()),
        )
        for name, kinds in kinds_by_name.items()
    }


BUILTIN_JOURNAL_RECORD_CONTRACTS = MappingProxyType(_build_contracts())


def validate_builtin_record(
    *,
    name: str,
    kind: JournalRecordKind,
    data: dict[str, Any] | None,
) -> None:
    """Raise when a known built-in producer violates its pinned contract."""
    contract = BUILTIN_JOURNAL_RECORD_CONTRACTS.get(name)
    if contract is None:
        return
    if kind not in contract.kinds:
        allowed = ", ".join(sorted(candidate.value for candidate in contract.kinds))
        raise ValueError(f"{name!r} must use journal kind(s): {allowed}")
    payload = data or {}
    missing = contract.required_data_keys - payload.keys()
    if missing:
        raise ValueError(f"{name!r} is missing required data keys: {', '.join(sorted(missing))}")


__all__ = [
    "BUILTIN_JOURNAL_RECORD_CONTRACTS",
    "BuiltinRecordContract",
    "validate_builtin_record",
]
