"""Shared helpers for agent bridge implementations.

``split_replacement_by_original_parts`` is used by multiple bridge
backends (OpenAI Agents SDK, PydanticAI, etc.) for post-processing
assistant output and keeping history part granularity when rewriting
text.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from collections.abc import Sequence
from typing import Any

from easycat._provider_helpers import get_package_version
from easycat.integrations.agents.base import AgentRecorder

logger = logging.getLogger(__name__)

# Shared constant used by bridges when recording an end-of-turn
# interruption in message history.
INTERRUPTION_NOTE = (
    "[The user interrupted the assistant's response and may not have heard all of it.]"
)


def resolve_model_name(candidate: Any) -> str | None:
    """Return a model identifier from common string and SDK object shapes."""
    if isinstance(candidate, str):
        model = candidate.strip()
        return model or None
    for attr in ("model", "model_name", "name"):
        value = getattr(candidate, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def bridge_version_info(
    *,
    provider: str,
    model: str | None,
    distribution: str,
) -> dict[str, str]:
    """Build the stable provider metadata shape for an agent bridge."""
    return {
        "provider": provider,
        "model": model or "unknown",
        "api_version": "unknown",
        "sdk_version": get_package_version(distribution),
    }


async def record_usage_from_result(
    recorder: AgentRecorder,
    result: Any,
    *,
    provider: str,
    model: str | None,
) -> None:
    """Best-effort token usage extraction across optional agent SDK versions."""
    try:
        usage = await _resolve_usage(result)
        if usage is None:
            return
        input_tokens = _usage_int(usage, "input_tokens", "request_tokens")
        output_tokens = _usage_int(usage, "output_tokens", "response_tokens")
        cached_input_tokens = _usage_int(
            usage,
            "cached_input_tokens",
            "cache_read_tokens",
        )
        if cached_input_tokens is None:
            details = _usage_value(
                usage,
                "input_tokens_details",
                "request_tokens_details",
                "details",
            )
            cached_input_tokens = _usage_int(
                details,
                "cached_tokens",
                "cache_read_tokens",
                "cached_input_tokens",
            )
        if input_tokens is None and output_tokens is None and cached_input_tokens is None:
            return
        counts = (input_tokens, output_tokens, cached_input_tokens)
        if all(value in (None, 0) for value in counts):
            return
        if (
            cached_input_tokens is not None
            and input_tokens is not None
            and cached_input_tokens > input_tokens
        ):
            cached_input_tokens = None
        record_usage = getattr(recorder, "record_usage", None)
        if record_usage is None:
            return
        record_usage(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
        )
    except Exception:
        logger.debug("Unable to record agent token usage", exc_info=True)


async def _resolve_usage(result: Any) -> Any:
    candidates: list[Any] = []
    for owner, attribute in (
        (result, "usage"),
        (_safe_attr(result, "context_wrapper"), "usage"),
        (_safe_attr(result, "ctx"), "usage"),
    ):
        candidate = _safe_attr(owner, attribute)
        if candidate is not None:
            candidates.append(candidate)
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            if inspect.iscoroutinefunction(candidate):
                continue
            value = candidate() if callable(candidate) else candidate
        except Exception:
            logger.debug("Agent usage accessor raised", exc_info=True)
            continue
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            continue
        if value is not None:
            return value
    return None


def _safe_attr(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except Exception:
        logger.debug("Agent usage attribute raised", exc_info=True)
        return None


def _usage_value(usage: Any, *names: str) -> Any:
    if usage is None:
        return None
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if value is not None:
            return value
    return None


def _usage_int(usage: Any, *names: str) -> int | None:
    value = _usage_value(usage, *names)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def aclose_quietly(agen: Any) -> None:
    """Close an async generator/iterator, swallowing teardown errors.

    ``async for`` does not forward an early consumer ``aclose()``
    (barge-in ``GeneratorExit``) into a delegated sub-generator, so a
    bridge that yields from ``self._drive_stream(...)`` must close it
    explicitly to run its ``BaseException`` cleanup synchronously — before
    a follow-up ``apply_interruption()``.  A no-op once the generator is
    already exhausted/closed.
    """
    aclose = getattr(agen, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception):
            await aclose()


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
