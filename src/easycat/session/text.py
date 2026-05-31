"""Text-processing helpers used by the Session pipeline.

Combines sentence-boundary detection, markdown delimiter tracking, speech
energy heuristics, and TTS payload text normalisation in one internal
module.  Everything here is a pure function / dataclass — no I/O, no
provider dependencies, safe to import from tests.

Naming convention for this module:

- Names in ``__all__`` (``split_at_sentence_boundaries``,
  ``has_unclosed_markdown_delimiters``) are the supported public surface,
  re-exported from ``easycat.session`` / consumed by ``_streaming``.
- The leading-underscore helpers (``_truncate_partial_text_to_boundary``,
  ``_text_for_estimation_timeline``, ``_cleanup_estimation_text``,
  ``_chunk_has_speech_energy``) are package-internal cross-module API: they
  are imported by sibling ``session/`` modules (``interruption``,
  ``_turn_runner``, ``_audio_router``) but are *not* part of the supported
  public surface, so they keep the underscore. The remaining underscore
  helpers (``_is_word_char``, ``_has_unclosed_single_emphasis``,
  ``_has_unclosed_markdown_link_or_image``) are file-local.
"""

from __future__ import annotations

import re
import struct

import sentencesplit

__all__ = [
    "split_at_sentence_boundaries",
    "has_unclosed_markdown_delimiters",
]

from easycat.audio_format import AudioChunk
from easycat.tts.input import TTSInput, strip_ssml_tags

# ── Sentence splitting ──────────────────────────────────────────────
#
# sentencesplit's ``segment_with_lookahead`` probes tiny suffixes to
# detect whether the final boundary could still shift once more streaming
# text arrives (e.g. "GPT 3." might become "GPT 3.5").  ``char_span=True``
# asks it for TextSpan objects with start/end offsets so we can slice
# without a second segmentation pass.
_SENTENCE_SEGMENTER = sentencesplit.Segmenter(language="en", clean=False, char_span=True)


def split_at_sentence_boundaries(text: str) -> tuple[str, str]:
    """Split text at the last stable sentence boundary.

    Returns ``(ready_text, remaining_buffer)``.  ``ready_text`` contains
    complete sentences safe to send to TTS; ``remaining_buffer`` holds
    trailing text whose sentence boundary could still shift as more
    streaming text arrives.  Callers must flush the buffer when the
    stream ends.
    """
    result = _SENTENCE_SEGMENTER.segment_with_lookahead(text)
    if not result.segments:
        return "", text
    if not result.should_wait_for_more:
        return text, ""
    if len(result.segments) == 1:
        return "", text
    last_start = result.segments[-1].start
    return text[:last_start], text[last_start:]


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _truncate_partial_text_to_boundary(text: str, chars: int) -> str:
    """Trim a partial text estimate to a safer boundary.

    If the proportional cut lands in the middle of a word, trim back to
    the nearest non-word boundary so interruption context looks less
    noisy.  ``_is_word_char`` treats every alphanumeric character
    (including CJK) as word, so this is effectively a no-op for
    languages without whitespace word boundaries.
    """
    if chars <= 0:
        return ""
    if chars >= len(text):
        return text

    prefix = text[:chars]
    next_char = text[chars]

    # If the cut already lands on a boundary, keep the proportional
    # estimate as-is.
    if (not _is_word_char(prefix[-1])) or (not _is_word_char(next_char)):
        return prefix

    # Otherwise trim back to the nearest safe boundary in the prefix.
    for i in range(len(prefix) - 1, -1, -1):
        if not _is_word_char(prefix[i]):
            return prefix[: i + 1]

    # Single long token with no internal boundary; keep the proportional
    # cut.
    return prefix


# ── Speech energy heuristic ─────────────────────────────────────────


def _chunk_has_speech_energy(chunk: AudioChunk, *, threshold: int = 500) -> bool:
    """Heuristic speech gate for STT-driven turns when VAD is disabled.

    Computes the peak absolute PCM sample value for mono 16-bit chunks
    and compares it to ``threshold``.  Filters continuous silent /
    background frames (e.g. telephony keepalive silence) so they don't
    spuriously start turns.

    Runs on the audio ingress loop for every received frame while IDLE,
    so the peak is computed with a single batch decode (numpy when
    available, else ``struct.unpack``) rather than a per-sample Python
    loop, matching the rest of the audio pipeline in ``_audio_utils``.
    """
    if chunk.format.sample_width != 2:
        return bool(chunk.data)

    # Drop an odd trailing byte that would otherwise split a 16-bit sample.
    data = chunk.data
    data = data[: len(data) // 2 * 2]
    if not data:
        return False

    try:
        import numpy as np  # type: ignore[import-untyped]

        # Widen to int32 before abs so abs(-32768) does not overflow int16.
        samples = np.frombuffer(data, dtype="<i2").astype(np.int32)
        return bool(np.abs(samples).max() >= threshold)
    except ImportError:
        decoded = struct.unpack(f"<{len(data) // 2}h", data)
        return max(abs(s) for s in decoded) >= threshold


# ── Markdown delimiter tracking ─────────────────────────────────────


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

        # Ignore repeated runs (** / __ / *** / ___); those are handled
        # elsewhere.
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

    Intentionally conservative for streaming safety: any trailing
    ``[label]`` without a resolved destination is treated as still-open
    until a non-link continuation is observed.
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
            # Closed bracket not followed by destination: not a markdown
            # link.
            awaiting_destination = False
            continue

        if ch == "[":
            label_depth = 1
        i += 1

    return label_depth > 0 or awaiting_destination or destination_depth > 0


def has_unclosed_markdown_delimiters(text: str) -> bool:
    """Best-effort check for unfinished markdown spans in a rolling buffer.

    The streaming path defers sentence emission while markdown delimiters
    are still open so later deltas cannot rewrite already-emitted text.
    """
    fenced_count = text.count("```")
    if fenced_count % 2 == 1:
        return True

    # Remove fenced blocks so inline delimiter counts are not distorted.
    normalized = re.sub(r"```[\s\S]*?```", "", text)

    # Inline backticks only (exclude fenced markers already handled
    # above).
    inline_tick_count = normalized.count("`")
    if inline_tick_count % 2 == 1:
        return True

    # Remove closed inline-code spans so markdown chars inside code do
    # not affect emphasis/link-state tracking.
    normalized = re.sub(r"`[^`]*`", "", normalized)

    for delimiter in ("**", "__", "~~"):
        if normalized.count(delimiter) % 2 == 1:
            return True

    if _has_unclosed_markdown_link_or_image(normalized):
        return True

    return _has_unclosed_single_emphasis(normalized, "*") or _has_unclosed_single_emphasis(
        normalized, "_"
    )


# ── TTS payload text normalisation for interruption estimation ──────

_PAUSE_MARKER = ""
_PAUSE_CHARS_PER_SECOND = 14.0


def _text_for_estimation_timeline(payload: TTSInput) -> str:
    """Return text used for interruption timeline estimation.

    For SSML payloads, explicit ``<break .../>`` pauses are expanded
    into synthetic marker characters so byte→text interpolation accounts
    for non-spoken silence regions.
    """
    if payload.format != "ssml":
        return payload.text

    def _break_repl(match: re.Match[str]) -> str:
        attrs = match.group(1)
        ms_match = re.search(
            r"""time\s*=\s*(['"])\s*(\d+)\s*ms\s*\1""",
            attrs,
            flags=re.IGNORECASE,
        )
        ms = int(ms_match.group(2)) if ms_match else 0
        count = max(1, round((ms / 1000.0) * _PAUSE_CHARS_PER_SECOND)) if ms > 0 else 1
        return _PAUSE_MARKER * count

    with_markers = re.sub(r"<break\b([^>]*)/>", _break_repl, payload.text, flags=re.IGNORECASE)
    return strip_ssml_tags(with_markers)


def _cleanup_estimation_text(text: str) -> str:
    """Remove synthetic pause markers from estimated spoken text."""
    return text.replace(_PAUSE_MARKER, "")
