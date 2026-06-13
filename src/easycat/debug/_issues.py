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

from easycat.debug._turn_timeline import record_wall_ns, safe_turn_id, turn_waterfall

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
    # ``barge_in_cutoff_ms`` is how long the bot may keep talking after a
    # barge-in before the cutoff is "slow"; ``missed_barge_in_window_ms`` is how
    # long the bot may keep playing over a user who started speaking before the
    # turn is flagged as a missed barge-in (the bot never stopped).
    barge_in_cutoff_ms: float = 600.0
    missed_barge_in_window_ms: float = 1_500.0
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


# Barge-in record names.  An ``interruption`` (or fanned ``control_signal``
# with ``signal_kind == "interrupt"``) is an acted-on barge-in; the bot goes
# quiet on ``bot_stopped_speaking`` / ``playback_mark_ack``; a user starting to
# talk over the bot is ``vad_start_speaking`` inside a ``bot_started_speaking``
# → bot-stopped window.
_INTERRUPTION = "interruption"
_CONTROL_SIGNAL = "control_signal"
_BOT_STARTED_SPEAKING = "bot_started_speaking"
_VAD_START_SPEAKING = "vad_start_speaking"
_BOT_STOPPED_NAMES = frozenset({"bot_stopped_speaking", "playback_mark_ack"})


def _is_interruption(record: Mapping[str, Any]) -> bool:
    """True when *record* is a barge-in (legacy event or fanned control signal)."""
    name = record.get("name")
    if name == _INTERRUPTION:
        return True
    if name == _CONTROL_SIGNAL:
        data = record.get("data")
        if isinstance(data, dict) and data.get("signal_kind") == "interrupt":
            return True
    return False


def _barge_in_cards(
    records: list[dict[str, Any]], thresholds: IssueThresholds
) -> list[dict[str, Any]]:
    """Flag slow barge-in cutoffs and missed barge-ins per turn.

    Detection is pure wall-clock ordering: an ``interruption`` whose next
    bot-stopped marker lands more than ``barge_in_cutoff_ms`` later is a
    ``slow_barge_in``; a ``vad_start_speaking`` inside an open playback window
    with no interruption and no bot-stop within ``missed_barge_in_window_ms`` is
    a ``missed_barge_in`` (the bot talked over the user).
    """
    by_turn: dict[str, list[tuple[int, str, bool]]] = {}
    for record in records:
        turn_id = safe_turn_id(record.get("turn_id"))
        if turn_id is None:
            continue
        wall = record_wall_ns(record)
        if wall is None:
            continue
        name = record.get("name")
        if _is_interruption(record):
            by_turn.setdefault(turn_id, []).append((wall, _INTERRUPTION, True))
        elif name in (_BOT_STARTED_SPEAKING, _VAD_START_SPEAKING) or name in _BOT_STOPPED_NAMES:
            by_turn.setdefault(turn_id, []).append((wall, str(name), False))

    issues: list[dict[str, Any]] = []
    for turn_id, raw in by_turn.items():
        ordered = sorted(raw, key=lambda item: item[0])
        issues.extend(_slow_barge_in_cards(turn_id, ordered, thresholds))
        issues.extend(_missed_barge_in_cards(turn_id, ordered, thresholds))
    return issues


def _slow_barge_in_cards(
    turn_id: str,
    ordered: list[tuple[int, str, bool]],
    thresholds: IssueThresholds,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, (wall, _name, is_interruption) in enumerate(ordered):
        if not is_interruption:
            continue
        stop_wall = next(
            (w for w, n, _i in ordered[index + 1 :] if n in _BOT_STOPPED_NAMES),
            None,
        )
        if stop_wall is None:
            continue
        delta_ms = (stop_wall - wall) / 1_000_000
        if delta_ms > thresholds.barge_in_cutoff_ms:
            issues.append(
                _issue(
                    code="slow_barge_in",
                    severity="warning",
                    title="Slow barge-in cutoff",
                    detail=(
                        "The bot kept talking after the user interrupted. Tune "
                        "interruption handling so playback stops sooner."
                    ),
                    turn_id=turn_id,
                    stage="tts",
                    metric="barge_in_cutoff_ms",
                    value=delta_ms,
                    threshold=thresholds.barge_in_cutoff_ms,
                )
            )
    return issues


def _missed_barge_in_cards(
    turn_id: str,
    ordered: list[tuple[int, str, bool]],
    thresholds: IssueThresholds,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    bot_speaking = False
    for index, (wall, name, is_interruption) in enumerate(ordered):
        if name == _BOT_STARTED_SPEAKING:
            bot_speaking = True
            continue
        if name in _BOT_STOPPED_NAMES:
            bot_speaking = False
            continue
        if name != _VAD_START_SPEAKING or not bot_speaking:
            continue
        # User started speaking over the bot. A miss is when nothing stopped the
        # bot (no interruption acted and no bot-stop) within the window.
        resolved = False
        for next_wall, next_name, next_interrupt in ordered[index + 1 :]:
            if next_interrupt or next_name in _BOT_STOPPED_NAMES:
                if (next_wall - wall) / 1_000_000 <= thresholds.missed_barge_in_window_ms:
                    resolved = True
                break
        if not resolved:
            issues.append(
                _issue(
                    code="missed_barge_in",
                    severity="warning",
                    title="Missed barge-in",
                    detail=(
                        "The user started speaking while the bot was talking, but "
                        "the bot never stopped. Check interruption detection."
                    ),
                    turn_id=turn_id,
                    stage="vad",
                    metric="missed_barge_in_window_ms",
                    threshold=thresholds.missed_barge_in_window_ms,
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
    issues = (
        _record_issues(records, thresholds)
        + _milestone_issues(records, thresholds)
        + _barge_in_cards(records, thresholds)
    )
    issues.sort(key=_sort_key)
    summary = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        severity = str(issue.get("severity"))
        if severity in summary:
            summary[severity] += 1
    return {"issues": issues, "summary": summary, "total": len(issues)}


__all__ = ["build_issues", "IssueThresholds"]
