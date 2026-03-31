"""Text processing utilities for the session pipeline.

Sentence boundary detection, markdown delimiter checking, speech energy
detection, and other pure helper functions used by the Session class.
"""

from __future__ import annotations

import re
from typing import Any

import pysbd

from easycat.audio_format import AudioChunk

# Sentence boundary detection via pySBD.
_SENTENCE_SEGMENTER = pysbd.Segmenter(language="en", clean=False, char_span=True)


def _span_bounds(span: object) -> tuple[int, int]:
    if isinstance(span, tuple) and len(span) == 2:
        return span
    start = getattr(span, "start", None)
    end = getattr(span, "end", None)
    if start is None or end is None:
        raise TypeError(f"Unexpected span type from pySBD: {span!r}")
    return int(start), int(end)


def _split_at_sentence_boundaries(text: str) -> tuple[str, str]:
    """Split text at the last sentence boundary.

    Returns (ready_text, remaining_buffer). ``ready_text`` contains complete
    sentences to send to TTS; ``remaining_buffer`` holds any trailing text
    that hasn't reached a sentence boundary yet.

    Only splits when pySBD detects multiple sentences — all but the last are
    returned as ready.  Single-span text is always buffered; the caller is
    responsible for flushing the final buffer when the LLM stream finishes.
    """
    spans = _SENTENCE_SEGMENTER.segment(text)
    if len(spans) <= 1:
        return "", text
    last_start, _ = _span_bounds(spans[-1])
    return text[:last_start], text[last_start:]


def _truncate_partial_text_to_boundary(text: str, chars: int) -> str:
    """Trim a partial text estimate to a safer boundary.

    If the proportional cut lands in the middle of a word, trim back to the
    nearest non-word boundary so interruption context looks less noisy.

    Note: ``_is_word_char`` treats all alphanumeric characters (including CJK
    and other non-Latin scripts) as word characters, so this function is
    effectively a no-op for languages without whitespace word boundaries.
    """
    if chars <= 0:
        return ""
    if chars >= len(text):
        return text

    prefix = text[:chars]
    next_char = text[chars]

    # If the cut already lands on a boundary (e.g. whitespace/punctuation),
    # keep the proportional estimate as-is.
    if (not _is_word_char(prefix[-1])) or (not _is_word_char(next_char)):
        return prefix

    # Otherwise trim back to the nearest safe boundary in the prefix.
    for i in range(len(prefix) - 1, -1, -1):
        if not _is_word_char(prefix[i]):
            return prefix[: i + 1]

    # Single long token with no internal boundary; keep the proportional cut.
    return prefix


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _chunk_has_speech_energy(chunk: AudioChunk, *, threshold: int = 500) -> bool:
    """Heuristic speech gate for STT-driven turns when VAD is disabled.

    Computes the peak absolute PCM sample value for mono 16-bit chunks and
    compares it to ``threshold``. This filters continuous silent/background
    frames (e.g. telephony keepalive silence) so they don't spuriously start
    turns.
    """
    if chunk.format.sample_width != 2:
        return bool(chunk.data)

    data = chunk.data
    if len(data) < 2:
        return False

    peak = 0
    for i in range(0, len(data) - 1, 2):
        sample = int.from_bytes(data[i : i + 2], "little", signed=True)
        mag = abs(sample)
        if mag > peak:
            peak = mag
            if peak >= threshold:
                return True
    return False


def _has_unclosed_single_emphasis(text: str, delimiter: str) -> bool:
    """Detect unclosed single-char emphasis delimiters (* / _) in text."""
    open_count = 0
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\\":
            i += 2  # Skip escaped character (if any).
            continue
        if ch != delimiter:
            i += 1
            continue

        prev_char = text[i - 1] if i > 0 else ""
        next_char = text[i + 1] if i + 1 < length else ""

        # Ignore repeated runs (** / __ / *** / ___); those are handled elsewhere.
        if prev_char == delimiter or next_char == delimiter:
            i += 1
            continue

        is_open = (not _is_word_char(prev_char)) and bool(next_char) and not next_char.isspace()
        is_close = bool(prev_char) and not prev_char.isspace() and (not _is_word_char(next_char))

        if is_close and open_count > 0:
            open_count -= 1
        elif is_open:
            open_count += 1
        i += 1

    return open_count > 0


def _has_unclosed_markdown_link_or_image(text: str) -> bool:
    """Detect incomplete markdown link/image spans in ``text``.

    The check is intentionally conservative for streaming safety: any trailing
    ``[label]`` without a resolved destination is treated as still-open until a
    non-link continuation is observed.
    """

    label_depth = 0
    awaiting_destination = False
    destination_depth = 0
    i = 0
    length = len(text)

    while i < length:
        ch = text[i]

        if ch == "\\":
            i += 2
            continue

        if destination_depth > 0:
            if ch == "(":
                destination_depth += 1
            elif ch == ")":
                destination_depth -= 1
            i += 1
            continue

        if label_depth > 0:
            if ch == "[":
                label_depth += 1
            elif ch == "]":
                label_depth -= 1
                if label_depth == 0:
                    awaiting_destination = True
            i += 1
            continue

        if awaiting_destination:
            if ch.isspace():
                i += 1
                continue
            if ch == "(":
                awaiting_destination = False
                destination_depth = 1
                i += 1
                continue
            # Closed bracket not followed by destination: not a markdown link.
            awaiting_destination = False
            continue

        if ch == "[":
            label_depth = 1
        i += 1

    return label_depth > 0 or awaiting_destination or destination_depth > 0


def _has_unclosed_markdown_delimiters(text: str) -> bool:
    """Best-effort check for unfinished markdown spans in a rolling buffer.

    The streaming path defers sentence emission while markdown delimiters are
    still open so later deltas cannot rewrite already-emitted text.
    """

    fenced_count = text.count("```")
    if fenced_count % 2 == 1:
        return True

    # Remove fenced blocks so inline delimiter counts are not distorted.
    normalized = re.sub(r"```[\s\S]*?```", "", text)

    # Inline backticks only (exclude fenced markers already handled above).
    inline_tick_count = normalized.count("`")
    if inline_tick_count % 2 == 1:
        return True

    # Remove closed inline-code spans so markdown chars inside code do not
    # affect emphasis/link-state tracking.
    normalized = re.sub(r"`[^`]*`", "", normalized)

    for delimiter in ("**", "__", "~~"):
        if normalized.count(delimiter) % 2 == 1:
            return True

    if _has_unclosed_markdown_link_or_image(normalized):
        return True

    return _has_unclosed_single_emphasis(normalized, "*") or _has_unclosed_single_emphasis(
        normalized, "_"
    )


def _replace_last_assistant_text(agent: Any, text: str) -> None:
    """Update the last assistant message in the agent's chat history.

    Works with :class:`AgentRunner` and :class:`BaseAgentAdapter` (or any
    object that exposes ``replace_last_assistant_text``).  Silently does
    nothing when the method is unavailable.
    """
    fn = getattr(agent, "replace_last_assistant_text", None)
    if callable(fn):
        fn(text)
