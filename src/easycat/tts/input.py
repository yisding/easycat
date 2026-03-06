"""Typed input payload for TTS synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TTSInputFormat = Literal["plain", "ssml"]


@dataclass(frozen=True)
class TTSInput:
    """Input payload for TTS providers.

    Attributes:
        text: Text (or SSML markup) to synthesize.
        format: Input format indicator. ``plain`` is raw text;
            ``ssml`` indicates XML SSML markup.
    """

    text: str
    format: TTSInputFormat = "plain"


def strip_ssml_tags(text: str) -> str:
    """Best-effort conversion from SSML markup to plain text."""
    import html
    import re

    without_tags = re.sub(r"<[^>]+>", " ", text)
    collapsed = re.sub(r"\s+", " ", without_tags).strip()
    return html.unescape(collapsed)


def coerce_tts_input(payload: TTSInput | str) -> TTSInput:
    """Accept legacy string input and normalize to ``TTSInput``."""
    if isinstance(payload, TTSInput):
        return payload
    return TTSInput(text=payload, format="plain")
