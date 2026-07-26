from __future__ import annotations

import ast
import re
from pathlib import Path

from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME

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


def _literal_string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    return None


def _record_name_from_call(call: ast.Call) -> str | None:
    function_name = _call_name(call)
    if function_name == "_EventRecordSpec" and len(call.args) >= 3:
        return _literal_string(call.args[2])
    if function_name == "_make_event_handler" and len(call.args) >= 2:
        return _literal_string(call.args[1])
    if function_name not in _RECORD_CALLS:
        return None
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    if function_name == "append" and not {"kind", "name", "session_id"} <= keywords.keys():
        return None
    return _literal_string(keywords.get("name"))


def _source_record_names() -> frozenset[str]:
    names = {AEC_REFERENCE_FRAME_NAME}
    for path in _producer_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "name"
            ):
                value = _literal_string(node.value)
                if value is not None:
                    names.add(value)
                continue
            if isinstance(node, ast.Call):
                value = _record_name_from_call(node)
                if value is not None:
                    names.add(value)
    return frozenset(names)


def _documented_record_names() -> frozenset[str]:
    text = JOURNAL_REFERENCE.read_text(encoding="utf-8")
    catalog = text.split("## Pipeline Records", 1)[1].split("## Contract Guard", 1)[0]
    return frozenset(re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", catalog, flags=re.MULTILINE))


def test_builtin_journal_record_names_match_snapshot() -> None:
    assert _source_record_names() == JOURNAL_RECORD_SNAPSHOT


def test_journal_record_reference_matches_snapshot() -> None:
    assert _documented_record_names() == JOURNAL_RECORD_SNAPSHOT
