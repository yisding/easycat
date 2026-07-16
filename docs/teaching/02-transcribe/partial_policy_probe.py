"""Compare unsafe partial commits with revision-aware speculation.

uv run python docs/teaching/02-transcribe/partial_policy_probe.py
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from easycat.events import STTEvent, STTEventType

SCRIPTED_EVENTS = (
    STTEvent(type=STTEventType.PARTIAL, text="set a timer for fifteen minutes"),
    STTEvent(type=STTEventType.PARTIAL, text="set a timer for fifty minutes"),
    STTEvent(type=STTEventType.FINAL, text="set a timer for fifty minutes"),
)


def compare_policies(events: Iterable[STTEvent]) -> dict[str, list[str]]:
    """Return observable outcomes for unsafe and revision-aware consumers."""
    unsafe_actions: list[str] = []
    ui_updates: list[str] = []
    speculations_started: list[str] = []
    speculations_cancelled: list[str] = []
    safe_commits: list[str] = []
    active_speculation: str | None = None

    for event in events:
        ui_updates.append(event.text)

        # Unsafe: every hypothesis is treated as permission for a side effect.
        unsafe_actions.append(event.text)

        if event.type is STTEventType.PARTIAL:
            if active_speculation is not None and active_speculation != event.text:
                speculations_cancelled.append(active_speculation)
            if active_speculation != event.text:
                speculations_started.append(event.text)
            active_speculation = event.text
            continue

        # Safe: a FINAL may promote matching speculative work, but it is the
        # first event allowed to commit a timer/tool/database/audio side effect.
        if active_speculation is not None and active_speculation != event.text:
            speculations_cancelled.append(active_speculation)
        safe_commits.append(event.text)
        active_speculation = None

    return {
        "unsafe_irreversible_actions": unsafe_actions,
        "ui_updates": ui_updates,
        "speculations_started": speculations_started,
        "speculations_cancelled": speculations_cancelled,
        "safe_commits": safe_commits,
    }


if __name__ == "__main__":
    print(json.dumps(compare_policies(SCRIPTED_EVENTS), indent=2, sort_keys=True))
