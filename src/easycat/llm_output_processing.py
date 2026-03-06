"""LLM output processors used to prepare speech-friendly TTS input."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from easycat.strip_markdown import strip_markdown
from easycat.tts.input import TTSInput, strip_ssml_tags

logger = logging.getLogger(__name__)

PauseStyle = Literal["ssml", "ellipsis", "emdash"]


@dataclass(frozen=True)
class _SSMLBreak:
    pause_ms: int


def _to_ssml_payload(parts: list[str | _SSMLBreak]) -> TTSInput:
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, _SSMLBreak):
            rendered.append(f'<break time="{max(0, part.pause_ms)}ms"/>')
        else:
            rendered.append(html.escape(part))
    return TTSInput(text=f"<speak>{''.join(rendered)}</speak>", format="ssml")


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
class PauseProcessor:
    """Apply pause-insertion to text spans matched by a user regex.

    ``pattern`` finds spans to transform. Within each match, ``unit_pattern``
    selects the units that should be separated by pauses (defaults to non-space
    characters).

    For ``style="ellipsis"``, use ``ellipsis_count`` to control single vs
    double ellipsis pauses (``1`` => ``...``, ``2`` => ``... ...``).
    """

    pattern: str
    pause_ms: int = 120
    unit_pattern: str = r"\S"
    minimum_units: int = 2
    flags: int = 0
    style: PauseStyle = "ssml"
    ellipsis_count: int = 1

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
        source = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)
        compiled = re.compile(self.pattern, self.flags)

        def matched_units(match_text: str) -> list[str] | None:
            units = re.findall(self.unit_pattern, match_text)
            if len(units) < self.minimum_units:
                return None
            return units

        if self.style == "ssml":
            parts: list[str | _SSMLBreak] = []
            cursor = 0
            changed = False

            for match in compiled.finditer(source):
                units = matched_units(match.group(0))
                if units is None:
                    continue

                changed = True
                parts.append(source[cursor : match.start()])
                for index, unit in enumerate(units):
                    if index:
                        parts.extend((" ", _SSMLBreak(self.pause_ms), " "))
                    parts.append(unit)
                cursor = match.end()

            if not changed:
                return payload

            parts.append(source[cursor:])
            return _to_ssml_payload(parts)

        def repl(match: re.Match[str]) -> str:
            units = matched_units(match.group(0))
            if units is None:
                return match.group(0)
            if self.style == "ellipsis":
                count = max(1, self.ellipsis_count)
                pause = " ".join(["..."] * count)
            else:
                pause = "—"
            return f" {pause} ".join(units)

        transformed = compiled.sub(repl, source)
        if transformed == source:
            return payload
        return TTSInput(text=transformed, format="plain")


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
    """Build the common stack for pronunciations + regex-based phone pauses."""
    processors: list[LLMOutputProcessor] = []
    if name_pronunciations:
        processors.append(PhoneticReplacementProcessor(name_pronunciations))
    processors.append(
        PauseProcessor(
            pattern=r"\+?\d[\d\s().-]{5,}\d",
            pause_ms=phone_pause_ms,
            unit_pattern=r"\d",
            minimum_units=7,
        )
    )
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
