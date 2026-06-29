"""Two-source per-turn diff for ``easycat diff A B``.

Compares two recorded bundles / crash journals turn-by-turn so a developer
can answer "what changed between this run and the baseline?" — which
milestone regressed and whether the transcript drifted — without opening the
debugger UI.  Everything here is pure: it operates on plain journal-record
dicts (as yielded by ``RunBundle.records()``) and reuses the shared rollups in
:mod:`easycat.debug._turn_timeline` so the diff never reimplements milestone
or transcript math.

Turns are aligned **positionally** by index (turn 0 of A vs turn 0 of B),
which is what matters for a before/after comparison of the same scripted
call.  When both sides expose the same ``turn_id`` at a position the diff
surfaces both ids; ragged turn counts pad the missing side with ``None`` and
mark the pair ``unmatched`` so a dropped or extra turn is obvious rather than
silently shifting every later comparison.

Milestone keys are read **dynamically** from the union of both sides'
milestone dicts, so a future milestone-chain change (the WP3/WP5 keys) flows
through without silently dropping a segment from the comparison.
"""

from __future__ import annotations

from typing import Any

from easycat.debug._turn_timeline import (
    extract_turn_transcripts,
    turn_milestones,
    turn_waterfall,
)

# A milestone counts as a regression only when it is meaningfully slower on the
# B side: more than 10% AND more than 5 ms worse.  The relative gate ignores
# noise on large deltas; the absolute gate ignores noise on tiny ones.
_REGRESSION_PCT = 10.0
_REGRESSION_ABS_MS = 5.0


def _milestone_delta(a: float | None, b: float | None) -> dict[str, Any]:
    """One milestone's ``{a, b, delta_ms, pct, regressed}`` cell.

    ``delta_ms`` is ``b - a`` (positive = slower on B).  ``pct`` is the
    percentage change relative to A, or ``None`` when A is missing/zero (no
    baseline to divide by).  ``regressed`` requires both endpoints present and
    a delta past both the relative and absolute gates.
    """
    delta_ms: float | None = None
    pct: float | None = None
    regressed = False
    if a is not None and b is not None:
        delta_ms = b - a
        if a:
            pct = (delta_ms / a) * 100.0
        # Regression needs a real slowdown past both gates.  When A is zero we
        # have no percentage baseline, so fall back to the absolute gate alone.
        past_pct = pct is None or pct > _REGRESSION_PCT
        regressed = delta_ms > _REGRESSION_ABS_MS and past_pct
    return {"a": a, "b": b, "delta_ms": delta_ms, "pct": pct, "regressed": regressed}


def _diff_milestones(
    milestones_a: dict[str, float | None] | None,
    milestones_b: dict[str, float | None] | None,
) -> dict[str, dict[str, Any]]:
    """Compare every milestone present on either side, keyed dynamically."""
    a = milestones_a or {}
    b = milestones_b or {}
    # Preserve A's key order first (pipeline order), then append any B-only keys.
    names: list[str] = list(a)
    names += [name for name in b if name not in a]
    return {name: _milestone_delta(a.get(name), b.get(name)) for name in names}


def _transcript_cell(
    transcript_a: dict[str, Any] | None,
    transcript_b: dict[str, Any] | None,
) -> dict[str, Any]:
    """One turn's transcript comparison with a ``changed`` flag.

    ``changed`` is true when either the user transcript or the agent response
    differs between the two sides (a dropped turn counts as a change).
    """
    user_a = (transcript_a or {}).get("user", "")
    user_b = (transcript_b or {}).get("user", "")
    agent_a = (transcript_a or {}).get("agent", "")
    agent_b = (transcript_b or {}).get("agent", "")
    changed = user_a != user_b or agent_a != agent_b
    return {
        "user_a": user_a,
        "user_b": user_b,
        "agent_a": agent_a,
        "agent_b": agent_b,
        "changed": changed,
    }


def diff_bundles(
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
) -> dict[str, Any]:
    """Diff two journals' records turn-by-turn.

    Returns ``{"turns": [...], "summary": {...}}`` where each turn carries the
    per-milestone deltas and the transcript comparison.  Turns are aligned
    positionally; ragged counts pad ``None`` and mark the pair ``unmatched``.
    The summary names the single worst regression across all turns/milestones.
    """
    waterfall_a = turn_waterfall(records_a)
    waterfall_b = turn_waterfall(records_b)
    milestones_a = turn_milestones(records_a)
    milestones_b = turn_milestones(records_b)
    transcripts_a = {t["turn_id"]: t for t in extract_turn_transcripts(records_a)}
    transcripts_b = {t["turn_id"]: t for t in extract_turn_transcripts(records_b)}

    turns: list[dict[str, Any]] = []
    worst: dict[str, Any] | None = None

    for index in range(max(len(waterfall_a), len(waterfall_b))):
        turn_a = waterfall_a[index] if index < len(waterfall_a) else None
        turn_b = waterfall_b[index] if index < len(waterfall_b) else None
        turn_id_a = turn_a["turn_id"] if turn_a else None
        turn_id_b = turn_b["turn_id"] if turn_b else None

        ms_a = milestones_a.get(turn_id_a) if turn_id_a else None
        ms_b = milestones_b.get(turn_id_b) if turn_id_b else None
        milestone_cells = _diff_milestones(ms_a, ms_b)

        transcript = _transcript_cell(
            transcripts_a.get(turn_id_a) if turn_id_a else None,
            transcripts_b.get(turn_id_b) if turn_id_b else None,
        )

        for name, cell in milestone_cells.items():
            if not cell["regressed"]:
                continue
            if worst is None or cell["delta_ms"] > worst["delta_ms"]:
                worst = {
                    "index": index,
                    "turn_id_a": turn_id_a,
                    "turn_id_b": turn_id_b,
                    "milestone": name,
                    "delta_ms": cell["delta_ms"],
                    "pct": cell["pct"],
                }

        turns.append(
            {
                "index": index,
                "turn_id_a": turn_id_a,
                "turn_id_b": turn_id_b,
                "unmatched": turn_a is None or turn_b is None,
                "milestones": milestone_cells,
                "transcript": transcript,
            }
        )

    return {
        "turns": turns,
        "summary": {
            "worst_regression": worst,
        },
    }


__all__ = ["diff_bundles"]
