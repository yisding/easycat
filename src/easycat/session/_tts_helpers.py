"""TTS payload text normalization for interruption estimation.

Helpers that extract plain spoken text from TTSInput payloads, expanding
SSML pauses into synthetic markers so byte-to-text interpolation accounts
for non-spoken silence regions.
"""

from __future__ import annotations

import re

from easycat.tts.input import TTSInput, strip_ssml_tags

_PAUSE_MARKER = "\ue000"
_PAUSE_CHARS_PER_SECOND = 14.0


def _text_for_spoken_estimation(payload: TTSInput) -> str:
    """Return plain spoken text for interruption accounting.

    Interruption text estimation compares audio-byte progress against text
    length. SSML markup should not count toward spoken-character estimates,
    so SSML payloads are normalized to plain text here.
    """

    if payload.format == "ssml":
        return strip_ssml_tags(payload.text)
    return payload.text


def _text_for_estimation_timeline(payload: TTSInput) -> str:
    """Return text used for interruption timeline estimation.

    For SSML payloads, explicit ``<break .../>`` pauses are expanded into
    synthetic marker characters so byte->text interpolation accounts for
    non-spoken silence regions.
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
