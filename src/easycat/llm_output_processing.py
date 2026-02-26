"""LLM output processors used to prepare speech-friendly TTS input."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from easycat.strip_markdown import strip_markdown
from easycat.tts.input import TTSInput, strip_ssml_tags

logger = logging.getLogger(__name__)


class LLMOutputProcessor(Protocol):
    """Processor that can transform text before TTS synthesis."""

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput: ...


@dataclass(frozen=True)
class MarkdownStripProcessor:
    """Strip markdown formatting before TTS."""

    normalize_code_spans: bool = True

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
        if payload.format == "ssml":
            return payload
        return TTSInput(
            text=strip_markdown(payload.text, normalize_code_spans=self.normalize_code_spans),
            format="plain",
        )


@dataclass(frozen=True)
class PhoneNumberSSMLProcessor:
    """Convert phone-number-like text spans into SSML with pauses."""

    pause_ms: int = 120

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
        source = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)

        def repl(match: re.Match[str]) -> str:
            digits = [ch for ch in match.group(0) if ch.isdigit()]
            if len(digits) < 7:
                return match.group(0)
            pause = f'<break time="{self.pause_ms}ms"/>'
            return f" {pause} ".join(digits)

        transformed = re.sub(r"\+?\d[\d\s().-]{5,}\d", repl, source)
        if transformed == source:
            return payload

        escaped = html.escape(transformed)
        escaped = escaped.replace("&lt;break time=&quot;", '<break time="')
        escaped = escaped.replace("ms&quot;/&gt;", 'ms"/>')
        return TTSInput(text=f"<speak>{escaped}</speak>", format="ssml")


@dataclass(frozen=True)
class PhoneticReplacementProcessor:
    """Replace names/terms with pronunciation-friendly text before TTS.

    The mapping is applied case-insensitively with whole-word boundaries to
    avoid replacing partial substrings inside larger words.
    """

    replacements: dict[str, str]

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
        source = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)
        transformed = source
        for source_term, spoken_term in self.replacements.items():
            pattern = re.compile(rf"\b{re.escape(source_term)}\b", flags=re.IGNORECASE)
            transformed = pattern.sub(spoken_term, transformed)

        if transformed == source:
            return payload
        return TTSInput(text=transformed, format="plain")


def default_pronunciation_processors(
    *,
    name_pronunciations: dict[str, str] | None = None,
    phone_pause_ms: int = 120,
) -> list[LLMOutputProcessor]:
    """Build the common processor stack for pronunciations + phone numbers."""
    processors: list[LLMOutputProcessor] = []
    if name_pronunciations:
        processors.append(PhoneticReplacementProcessor(name_pronunciations))
    processors.append(PhoneNumberSSMLProcessor(pause_ms=phone_pause_ms))
    return processors


def apply_output_processors(
    payload: TTSInput,
    processors: list[LLMOutputProcessor],
    *,
    is_final: bool,
    is_streaming: bool,
) -> TTSInput:
    """Run processors in sequence with fail-open behavior."""
    current = payload
    for processor in processors:
        try:
            current = processor.process(current, is_final=is_final, is_streaming=is_streaming)
        except Exception:
            logger.warning("Output processor failed: %s", type(processor).__name__, exc_info=True)
    return current
