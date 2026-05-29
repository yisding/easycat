"""Shared helpers for agent bridge implementations.

``split_replacement_by_original_parts`` is used by multiple bridge
backends (OpenAI Agents SDK, PydanticAI, etc.) for post-processing
assistant output and keeping history part granularity when rewriting
text.
"""

from __future__ import annotations

from collections.abc import Sequence

# Shared constant used by bridges when recording an end-of-turn
# interruption in message history.
INTERRUPTION_NOTE = (
    "[The user interrupted the assistant's response and may not have heard all of it.]"
)


def split_replacement_by_original_parts(
    original_parts: Sequence[str],
    replacement: str,
) -> list[str]:
    """Split a replacement string across original part boundaries.

    This keeps history part granularity when post-processing modifies the
    concatenated assistant text (e.g. Markdown stripping). The returned
    parts always concatenate back to ``replacement``.

    Assumes the replacement is derived by *deletion only* — characters
    may be removed but not substituted or inserted. The greedy
    subsequence mapping below can't recover boundaries across
    substitutions; Markdown stripping (the current caller) satisfies
    this.
    """
    if not original_parts:
        return []
    if len(original_parts) == 1:
        return [replacement]

    original_joined = "".join(original_parts)
    if not original_joined:
        return [replacement, *([""] * (len(original_parts) - 1))]

    # Greedy subsequence mapping: markdown stripping primarily removes
    # characters, so map each original index to the consumed index in the
    # replacement text.
    replacement_len = len(replacement)
    original_to_replacement = [0] * (len(original_joined) + 1)
    replacement_idx = 0
    for original_idx, ch in enumerate(original_joined):
        if replacement_idx < replacement_len and ch == replacement[replacement_idx]:
            replacement_idx += 1
        original_to_replacement[original_idx + 1] = replacement_idx

    split_points: list[int] = []
    running = 0
    for part in original_parts[:-1]:
        running += len(part)
        split_points.append(original_to_replacement[running])

    result_parts: list[str] = []
    prev = 0
    for split_at in split_points:
        bounded = max(prev, min(replacement_len, split_at))
        result_parts.append(replacement[prev:bounded])
        prev = bounded
    result_parts.append(replacement[prev:])
    return result_parts
