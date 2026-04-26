"""Helpers for forwarding per-turn bridge context into framework adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_TRANSIENT_CONTEXT_ROLES = {"system", "developer"}


def normalize_context_messages(
    context: Sequence[Mapping[str, Any]] | None,
    *,
    own_history: bool,
) -> list[dict[str, str]]:
    """Return bridge context messages safe to prepend to one framework turn.

    When a framework bridge already owns conversation history, caller-provided
    user/assistant history would duplicate that state.  System/developer
    messages are still retained because they describe transient per-turn
    environment such as caller identity.
    """
    messages: list[dict[str, str]] = []
    for item in context or ():
        if not isinstance(item, Mapping):
            continue
        role = item.get("role")
        content = item.get("content")
        if not isinstance(role, str) or content is None:
            continue
        role = role.strip().lower()
        if not role:
            continue
        if own_history and role not in _TRANSIENT_CONTEXT_ROLES:
            continue
        messages.append({"role": role, "content": str(content)})
    return messages
