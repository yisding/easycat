"""Shared helpers for session/integration tests.

``_TestBridgeBase`` is a mixin that satisfies the :class:`ExternalAgentBridge`
protocol with minimal boilerplate.  Test classes subclass it and override
``invoke()`` with the event sequence they want Session to consume.
"""

from __future__ import annotations

from typing import Any

from easycat.integrations.agents.base import (
    CancellationMode,
    CommitRule,
    FrameworkStateSnapshot,
    UnitKind,
)


class _TestBridgeBase:
    """Minimal :class:`ExternalAgentBridge` mixin for tests.

    Subclasses define ``async def invoke(self, turn_input, recorder,
    cancel_token=None)`` and are automatically :class:`ExternalAgentBridge`
    instances thanks to Protocol structural typing.
    """

    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}

    def __init__(self) -> None:
        self._history: list[dict[str, str]] = []
        self._last_output: Any = None
        # Interruption observability for tests asserting barge-in behavior.
        self.interruption_notified = False
        self.interruption_text_spoken = ""
        self.interruption_mode = ""

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={"agent": type(self).__name__},
            kind="test_bridge",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: Any = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        replacement = delivered_text + "..." if delivered_text else "..."
        for i in range(len(self._history) - 1, -1, -1):
            if self._history[i].get("role") == "assistant":
                self._history[i] = {"role": "assistant", "content": replacement}
                break
        self.interruption_notified = True
        self.interruption_text_spoken = delivered_text
        self.interruption_mode = "truncate"

    def replace_last_assistant_text(self, text: str) -> None:
        for entry in reversed(self._history):
            if entry.get("role") == "assistant":
                entry["content"] = text
                break

    def append_interruption_note(self, note: str) -> None:
        self._history.append({"role": "system", "content": note})
        self.interruption_notified = True
        self.interruption_mode = "message"

    def reset(self) -> None:
        self._history.clear()
        self._last_output = None

    @property
    def last_output(self) -> Any:
        return self._last_output
