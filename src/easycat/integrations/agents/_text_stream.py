"""Canonical accumulation for flat and indexed agent text events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from easycat.integrations.agents.base import AgentBridgeEvent

TEXT_EVENT_KINDS = frozenset({"text_delta", "text_replace"})


@dataclass(frozen=True)
class AgentTextUpdate:
    """Result of applying one bridge text event to a response."""

    previous_text: str
    text: str
    appended_text: str | None
    operation: Literal["append", "replace"]
    part_index: int | None


class AgentTextStream:
    """Accumulate either append-only text or indexed replaceable text parts.

    A bridge stream uses one representation for a turn: unindexed
    ``text_delta`` events, or indexed ``text_delta`` / ``text_replace``
    events. Mixing the two would leave the relative ordering undefined and is
    rejected at the first consumer boundary.
    """

    def __init__(self) -> None:
        self._mode: Literal["flat", "indexed"] | None = None
        self._flat_text = ""
        self._parts: dict[int, str] = {}

    @property
    def text(self) -> str:
        if self._mode == "indexed":
            return "".join(self._parts[index] for index in sorted(self._parts))
        return self._flat_text

    def apply(self, event: AgentBridgeEvent | Any) -> AgentTextUpdate | None:
        """Apply a bridge text event and return its canonical response update."""
        kind = getattr(event, "kind", None)
        if kind not in TEXT_EVENT_KINDS:
            return None

        previous = self.text
        value = getattr(event, "text", "") or ""
        part_index = getattr(event, "part_index", None)
        if part_index is None:
            if kind == "text_replace":
                raise ValueError("text_replace events require part_index")
            self._select_mode("flat")
            self._flat_text += value
            operation: Literal["append", "replace"] = "append"
        else:
            if not isinstance(part_index, int) or isinstance(part_index, bool) or part_index < 0:
                raise ValueError("indexed text events require a non-negative part_index")
            self._select_mode("indexed")
            if kind == "text_replace":
                self._parts[part_index] = value
                operation = "replace"
            else:
                self._parts[part_index] = self._parts.get(part_index, "") + value
                operation = "append"

        current = self.text
        appended = current[len(previous) :] if current.startswith(previous) else None
        return AgentTextUpdate(
            previous_text=previous,
            text=current,
            appended_text=appended,
            operation=operation,
            part_index=part_index,
        )

    def replace_final(self, text: str) -> None:
        """Adopt an authoritative terminal response."""
        self._mode = "flat"
        self._flat_text = text
        self._parts.clear()

    def _select_mode(self, mode: Literal["flat", "indexed"]) -> None:
        if self._mode is None:
            self._mode = mode
        elif self._mode != mode:
            raise ValueError("agent text streams cannot mix indexed and unindexed events")


def is_text_event(event: AgentBridgeEvent | Any) -> bool:
    """Return whether *event* carries agent response text."""
    return getattr(event, "kind", None) in TEXT_EVENT_KINDS
