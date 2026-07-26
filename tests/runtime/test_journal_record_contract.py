from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JOURNAL_REFERENCE = REPO_ROOT / "docs" / "reference" / "journal-records.md"

JOURNAL_RECORD_SNAPSHOT = frozenset(
    {
        "aec_reference_frame",
        "agent_delta",
        "agent_final",
        "agent_request_started",
        "assistant_interruption_notified",
        "audio_queue_drop",
        "bot_started_speaking",
        "bot_stopped_speaking",
        "buffer_overflow",
        "call_answered",
        "call_ended",
        "call_failed",
        "call_screening",
        "cancellation_boundary",
        "control_signal",
        "control_signal_cause",
        "error",
        "framework_error",
        "framework_handoff",
        "interruption",
        "interruption_apply_failed",
        "interruption_note",
        "journal_degraded",
        "markdown_stripped",
        "pipeline_heartbeat",
        "playback_mark_ack",
        "provider_versions",
        "recovered_session",
        "replace_last_assistant_text",
        "session_action_completed",
        "session_action_failed",
        "session_action_requested",
        "session_action_started",
        "stage_complete",
        "stage_error",
        "stage_start",
        "state_committed",
        "state_snapshot",
        "stt_final",
        "stt_partial",
        "stt_segment_commit_requested",
        "stt_segment_commit_result",
        "stt_segment_final",
        "supervisor_listener_attached",
        "supervisor_listener_detached",
        "task_cancelled",
        "task_completed",
        "task_raised",
        "task_scheduled",
        "text_turn_latency_ms",
        "tool_call_delta",
        "tool_call_result",
        "tool_call_started",
        "tool_phase_changed",
        "transport_degraded",
        "tts_audio",
        "tts_frame",
        "tts_markers",
        "tts_payload_prepared",
        "turn_ended",
        "turn_started",
        "turn_state_changed",
        "turn_total_latency_ms",
        "unit_entered",
        "unit_exited",
        "vad_start_speaking",
        "vad_stop_speaking",
        "warmup_completed",
        "warmup_failed",
        "ws_reconnect_attempt",
        "ws_reconnect_failure",
        "ws_reconnect_success",
    }
)

JOURNAL_RECORD_KIND_SNAPSHOT = {
    "EVENT": frozenset(
        """
        aec_reference_frame interruption_note markdown_stripped provider_versions
        replace_last_assistant_text stage_complete stage_error stage_start
        stt_segment_commit_requested stt_segment_commit_result stt_segment_final tts_frame
        tts_payload_prepared agent_delta agent_final agent_request_started bot_started_speaking
        bot_stopped_speaking call_answered call_ended call_failed call_screening error
        playback_mark_ack session_action_completed session_action_failed session_action_requested
        session_action_started stt_final stt_partial supervisor_listener_attached
        supervisor_listener_detached tool_call_delta tool_call_result tool_call_started
        transport_degraded tts_audio tts_markers turn_ended turn_started vad_start_speaking
        vad_stop_speaking ws_reconnect_attempt ws_reconnect_failure ws_reconnect_success
        assistant_interruption_notified audio_queue_drop pipeline_heartbeat task_cancelled
        task_completed task_raised task_scheduled turn_state_changed warmup_completed
        """.split()
    ),
    "METRIC": frozenset("text_turn_latency_ms turn_total_latency_ms".split()),
    "CONTROL": frozenset(
        """
        interruption transport_degraded control_signal control_signal_cause warmup_failed
        buffer_overflow
        """.split()
    ),
    "FRAMEWORK_TRANSITION": frozenset(
        """
        cancellation_boundary framework_error framework_handoff interruption_apply_failed
        state_committed state_snapshot tool_phase_changed unit_entered unit_exited
        """.split()
    ),
    "DEGRADED": frozenset({"journal_degraded"}),
    "RECOVERY": frozenset({"recovered_session"}),
}

JOURNAL_RECORD_REQUIRED_KEYS_SNAPSHOT = {
    "aec_reference_frame": frozenset("stage audio_bytes".split()),
    "interruption_note": frozenset("stage note".split()),
    "markdown_stripped": frozenset("phase changed original_text stripped_text".split()),
    "replace_last_assistant_text": frozenset("stage text".split()),
    "stage_complete": frozenset({"stage"}),
    "stage_error": frozenset("stage error elapsed_ms".split()),
    "stage_start": frozenset({"stage"}),
    "stt_segment_commit_requested": frozenset(
        "segment_index transcript_text pending_commit_bytes".split()
    ),
    "stt_segment_commit_result": frozenset("segment_index committed transcript_text".split()),
    "stt_segment_final": frozenset("segment_index text track transcript_text".split()),
    "text_turn_latency_ms": frozenset("value surface".split()),
    "tts_frame": frozenset("stage audio_bytes frame_index".split()),
    "tts_payload_prepared": frozenset(
        """
        is_streaming is_final changed original_text original_format prepared_text
        prepared_format processors ssml_downgraded
        """.split()
    ),
    "turn_total_latency_ms": frozenset("value from to".split()),
    "agent_final": frozenset({"text"}),
    "call_answered": frozenset({"call_sid"}),
    "call_ended": frozenset({"call_sid"}),
    "call_failed": frozenset("call_sid reason".split()),
    "call_screening": frozenset("call_sid platform".split()),
    "error": frozenset({"stage"}),
    "playback_mark_ack": frozenset({"mark_name"}),
    "session_action_completed": frozenset("action executor result".split()),
    "session_action_failed": frozenset("action error".split()),
    "session_action_requested": frozenset({"action"}),
    "session_action_started": frozenset("action executor".split()),
    "stt_final": frozenset({"text"}),
    "stt_partial": frozenset({"text"}),
    "supervisor_listener_attached": frozenset("listener_id queue_size".split()),
    "supervisor_listener_detached": frozenset("listener_id dropped_frames reason".split()),
    "tool_call_delta": frozenset("call_id delta".split()),
    "tool_call_result": frozenset("call_id result".split()),
    "tool_call_started": frozenset("tool_name call_id".split()),
    "transport_degraded": frozenset("provider reason detail fatal".split()),
    "tts_audio": frozenset(
        "audio_bytes duration_ms sample_rate channels sample_width encoding bypass_gate".split()
    ),
    "tts_markers": frozenset({"markers"}),
    "ws_reconnect_attempt": frozenset("provider attempt".split()),
    "ws_reconnect_failure": frozenset("provider error".split()),
    "ws_reconnect_success": frozenset({"provider"}),
    "assistant_interruption_notified": frozenset("source mode text_spoken notified".split()),
    "audio_queue_drop": frozenset("queue kind queue_len total_drops".split()),
    "control_signal": frozenset(
        "stage observed_stage signal_kind signal_id direction cause".split()
    ),
    "control_signal_cause": frozenset("signal_id cause".split()),
    "pipeline_heartbeat": frozenset(
        "interval_ms loop_lag_ms outbound_queue_len outbound_queue_drops".split()
    ),
    "task_cancelled": frozenset({"task_name"}),
    "task_completed": frozenset({"task_name"}),
    "task_raised": frozenset("task_name exc_type".split()),
    "task_scheduled": frozenset({"task_name"}),
    "turn_state_changed": frozenset("from to reason".split()),
    "warmup_completed": frozenset("elapsed_ms components".split()),
    "warmup_failed": frozenset("component elapsed_ms exc_type".split()),
    "cancellation_boundary": frozenset(
        "run_id cancellation_mode boundary_reason caused_by_signal_id".split()
    ),
    "framework_error": frozenset({"run_id"}),
    "framework_handoff": frozenset("run_id from_unit to_unit handoff_reason".split()),
    "interruption_apply_failed": frozenset(
        "run_id mutation_kind pre_state_ref post_state_ref".split()
    ),
    "state_committed": frozenset(
        "run_id mutation_kind pre_state_ref post_state_ref direction".split()
    ),
    "state_snapshot": frozenset("run_id state_ref".split()),
    "tool_phase_changed": frozenset("run_id phase tool_name args_ref result_ref call_id".split()),
    "unit_entered": frozenset(
        """
        run_id unit_id unit_kind display_name parent_unit_id committable direction
        """.split()
    ),
    "unit_exited": frozenset(
        "run_id unit_id unit_kind display_name committable exit_reason direction".split()
    ),
    "buffer_overflow": frozenset({"dropped_from"}),
    "journal_degraded": frozenset("error_type error_message".split()),
    "recovered_session": frozenset("recovered_record_count original_session_id".split()),
}

_PRODUCER_ROOTS = (
    REPO_ROOT / "src" / "easycat" / "config" / "_factory.py",
    REPO_ROOT / "src" / "easycat" / "integrations" / "agents",
    REPO_ROOT / "src" / "easycat" / "runtime",
    REPO_ROOT / "src" / "easycat" / "session",
    REPO_ROOT / "src" / "easycat" / "stages",
)
_RECORD_CALLS = frozenset({"append", "append_record", "journal_append_event", "_append"})


def _producer_paths() -> list[Path]:
    paths: list[Path] = []
    for root in _PRODUCER_ROOTS:
        paths.extend(root.rglob("*.py") if root.is_dir() else [root])
    return sorted(set(paths))


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _literal_string(
    node: ast.expr | None,
    constants: Mapping[str, str],
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _record_name_from_call(
    call: ast.Call,
    constants: Mapping[str, str],
) -> str | None:
    function_name = _call_name(call)
    if function_name == "_EventRecordSpec" and len(call.args) >= 3:
        return _literal_string(call.args[2], constants)
    if function_name == "_make_event_handler" and len(call.args) >= 2:
        return _literal_string(call.args[1], constants)
    if function_name not in _RECORD_CALLS:
        return None
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    if function_name == "append" and not {"kind", "name", "session_id"} <= keywords.keys():
        return None
    return _literal_string(keywords.get("name"), constants)


def _source_string_constants(paths: list[Path]) -> dict[str, str]:
    constants: dict[str, str] = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if (
                isinstance(target, ast.Name)
                and target.id.isupper()
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value
            ):
                constants[target.id] = value.value
    return constants


def _source_record_names() -> frozenset[str]:
    paths = _producer_paths()
    constants = _source_string_constants(paths)
    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "name"
            ):
                value = _literal_string(node.value, constants)
                if value is not None:
                    names.add(value)
                continue
            if isinstance(node, ast.Call):
                value = _record_name_from_call(node, constants)
                if value is not None:
                    names.add(value)
    return frozenset(names)


def _documented_record_contracts() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    text = JOURNAL_REFERENCE.read_text(encoding="utf-8")
    catalog = text.split("## Pipeline Records", 1)[1].split("## Contract Guard", 1)[0]
    contracts: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    row_pattern = re.compile(
        r"^\| `(?P<name>[a-z][a-z0-9_]*)` \| "
        r"(?P<kind>.*?) \| (?P<required>.*?) \|",
        flags=re.MULTILINE,
    )
    for match in row_pattern.finditer(catalog):
        kinds = frozenset(re.findall(r"`([A-Z_]+)`", match["kind"]))
        required_keys = frozenset(re.findall(r"`([a-z][a-z0-9_]*):", match["required"]))
        contracts[match["name"]] = (kinds, required_keys)
    return contracts


def test_builtin_journal_record_names_match_snapshot() -> None:
    assert _source_record_names() == JOURNAL_RECORD_SNAPSHOT


def test_journal_record_reference_matches_snapshot() -> None:
    documented = _documented_record_contracts()

    assert frozenset(documented) == JOURNAL_RECORD_SNAPSHOT
    for kind, names in JOURNAL_RECORD_KIND_SNAPSHOT.items():
        assert {name for name, (kinds, _) in documented.items() if kind in kinds} == names
    assert {
        name: required for name, (_, required) in documented.items() if required
    } == JOURNAL_RECORD_REQUIRED_KEYS_SNAPSHOT
