"""Heuristic issue detection for debug bundles and live sessions.

``build_issues`` scans a sequence of plain journal-record dicts (as yielded
by :meth:`RunBundle.records` or a live ``JournalView``) and returns a
severity-ranked rollup answering "what went wrong, and where?" without
opening the debugger UI.  It powers the debugger ``/api/issues`` tab and the
``easycat bundles show --issues`` / ``easycat inspect --issues`` CLI surface.

Two kinds of finding are produced:

- **Record-level** — an individual journal record carries an error, names a
  tool/timeout failure, or commits an empty transcript.  These reference the
  offending ``turn_id`` + ``sequence``.
- **Milestone-level** — a per-turn latency delta (computed by
  :func:`easycat.debug._turn_timeline.turn_waterfall`) exceeds its threshold,
  or the turn's total wall time is slow.  These reference the ``turn_id`` and
  the metric/value/threshold that tripped.

All numeric thresholds live in the frozen :class:`IssueThresholds` dataclass
so they are tunable from one place and never scatter as magic numbers.  The
output dict shape is stable so the (generic, no-innerHTML) debugger renderer
and the CLI table can both consume it without per-code special-casing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from easycat.debug._turn_timeline import turn_waterfall

# Severity rank for sorting (higher = more urgent).  Used to surface errors
# before warnings before info in both the SPA cards and the CLI table.
_SEVERITY_RANK = {"error": 2, "warning": 1, "info": 0}


@dataclass(frozen=True)
class IssueThresholds:
    """All numeric heuristics ``build_issues`` applies, in one tunable place.

    ``latency_checks`` pairs each milestone key with its warn threshold (ms)
    and the stage to blame; the milestone keys mirror the WP3 split chain
    (``stt_final_to_agent_request_ms`` is dispatch overhead and
    ``agent_request_to_first_token_ms`` is raw LLM TTFT).  ``tool_failure_names``
    and ``timeout_names`` are the record ``name`` sets that flag a failed tool
    call or a timeout even when the record carries no structured ``error``.
    """

    slow_turn_wall_ms: float = 10_000.0
    latency_checks: tuple[tuple[str, float, str], ...] = (
        ("vad_endpoint_to_stt_final_ms", 1_500.0, "stt"),
        ("stt_final_to_agent_request_ms", 2_500.0, "agent"),
        ("agent_request_to_first_token_ms", 2_500.0, "agent"),
        ("agent_first_token_to_tts_first_byte_ms", 1_500.0, "tts"),
    )
    tool_failure_names: frozenset[str] = field(
        default_factory=lambda: frozenset({"tool_call_failed", "tool_error", "tool_call_error"})
    )
    timeout_names: frozenset[str] = field(
        default_factory=lambda: frozenset({"timeout", "stage_timeout", "provider_timeout"})
    )


_THRESHOLDS = IssueThresholds()


def _issue(
    *,
    code: str,
    severity: str,
    title: str,
    detail: str,
    turn_id: str | None = None,
    sequence: int | None = None,
    stage: str | None = None,
    metric: str | None = None,
    value: float | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Build one issue card with the stable shape the renderers expect.

    Every field is always present so the generic debugger renderer and the
    CLI table never branch on ``code``; optional context is ``None`` when the
    finding has no turn/sequence/metric to point at.
    """
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "turn_id": turn_id,
        "sequence": sequence,
        "stage": stage,
        "metric": metric,
        "value": value,
        "threshold": threshold,
    }


def _error_type(error: Any) -> str | None:
    if isinstance(error, Mapping):
        etype = error.get("type")
        if etype:
            return str(etype)
    return None


def _record_issues(
    records: list[dict[str, Any]], thresholds: IssueThresholds
) -> list[dict[str, Any]]:
    """Flag per-record failures: errors, tool failures, timeouts, empty STT."""
    issues: list[dict[str, Any]] = []
    for record in records:
        name = record.get("name") or ""
        turn_id = record.get("turn_id")
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
        seq = record.get("sequence")
        seq = seq if isinstance(seq, int) else None
        data = record.get("data")
        data = data if isinstance(data, dict) else {}
        stage = data.get("stage") or data.get("observed_stage")
        stage = stage if isinstance(stage, str) else None

        error = record.get("error")
        if error:
            etype = _error_type(error) or "error"
            issues.append(
                _issue(
                    code="record_error",
                    severity="error",
                    title=f"{etype} during {name or 'record'}",
                    detail=(
                        f"A journal record raised {etype}. Open the turn in the "
                        "debugger or replay it to see the failing stage."
                    ),
                    turn_id=turn_id,
                    sequence=seq,
                    stage=stage,
                )
            )
            continue

        if name in thresholds.tool_failure_names:
            tool = data.get("tool_name")
            tool = tool if isinstance(tool, str) and tool else "a tool call"
            issues.append(
                _issue(
                    code="tool_call_failed",
                    severity="error",
                    title=f"Tool call failed ({name})",
                    detail=f"{tool} did not complete successfully.",
                    turn_id=turn_id,
                    sequence=seq,
                    stage=stage or "agent",
                )
            )
        elif name in thresholds.timeout_names:
            issues.append(
                _issue(
                    code="timeout",
                    severity="error",
                    title=f"Timeout ({name})",
                    detail="A stage or provider timed out before completing.",
                    turn_id=turn_id,
                    sequence=seq,
                    stage=stage,
                )
            )
        elif name == "stt_final":
            text = data.get("text") or data.get("transcript")
            if not (isinstance(text, str) and text.strip()):
                issues.append(
                    _issue(
                        code="empty_stt_final",
                        severity="warning",
                        title="Empty transcript committed",
                        detail=(
                            "An stt_final committed with no text. The agent ran on "
                            "an empty utterance — check VAD endpointing or STT input."
                        ),
                        turn_id=turn_id,
                        sequence=seq,
                        stage=stage or "stt",
                    )
                )
    return issues


def _milestone_issues(
    records: list[dict[str, Any]], thresholds: IssueThresholds
) -> list[dict[str, Any]]:
    """Flag slow turns and milestone deltas that exceed their thresholds."""
    issues: list[dict[str, Any]] = []
    for turn in turn_waterfall(records):
        turn_id = turn.get("turn_id")
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
        wall_ms = turn.get("wall_ms")
        if isinstance(wall_ms, int | float) and wall_ms > thresholds.slow_turn_wall_ms:
            issues.append(
                _issue(
                    code="slow_turn",
                    severity="warning",
                    title="Slow turn",
                    detail=(
                        "This turn's total wall time exceeded the slow-turn budget. "
                        "Check the latency waterfall to see which stage dominated."
                    ),
                    turn_id=turn_id,
                    metric="wall_ms",
                    value=float(wall_ms),
                    threshold=thresholds.slow_turn_wall_ms,
                )
            )
        milestones = turn.get("milestones") or {}
        for metric, limit, stage in thresholds.latency_checks:
            value = milestones.get(metric)
            if isinstance(value, int | float) and value > limit:
                issues.append(
                    _issue(
                        code="slow_milestone",
                        severity="warning",
                        title=f"Slow {stage} milestone",
                        detail=(
                            f"{metric} took longer than its {limit:.0f}ms budget. "
                            f"This is on the {stage} critical path."
                        ),
                        turn_id=turn_id,
                        stage=stage,
                        metric=metric,
                        value=float(value),
                        threshold=limit,
                    )
                )
    return issues


def _sort_key(issue: Mapping[str, Any]) -> tuple[int, str, int, str]:
    """Errors before warnings before info, then stable by turn/sequence/code."""
    rank = _SEVERITY_RANK.get(str(issue.get("severity")), 0)
    turn_id = issue.get("turn_id")
    turn_id = turn_id if isinstance(turn_id, str) else ""
    sequence = issue.get("sequence")
    sequence = sequence if isinstance(sequence, int) else -1
    code = str(issue.get("code") or "")
    return (-rank, turn_id, sequence, code)


def build_issues(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Scan journal records and return a severity-ranked issue rollup.

    Returns ``{"issues": [...], "summary": {"error": int, "warning": int,
    "info": int}, "total": int}``.  ``issues`` is sorted error-before-warning
    (then by turn id, sequence, and code) so the most urgent findings render
    first.  The shape is stable across an empty journal (no issues, all-zero
    summary) so callers never special-case the no-findings path.
    """
    thresholds = _THRESHOLDS
    issues = _record_issues(records, thresholds) + _milestone_issues(records, thresholds)
    issues.sort(key=_sort_key)
    summary = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        severity = str(issue.get("severity"))
        if severity in summary:
            summary[severity] += 1
    return {"issues": issues, "summary": summary, "total": len(issues)}


__all__ = ["build_issues", "IssueThresholds"]
