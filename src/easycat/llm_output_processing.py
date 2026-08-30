"""LLM output processors used to prepare speech-friendly TTS input."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from easycat.strip_markdown import strip_markdown
from easycat.tts.input import TTSInput, strip_ssml_tags

logger = logging.getLogger(__name__)

PauseStyle = Literal["ssml", "ellipsis", "emdash"]
MAX_SSML_BREAK_MS = 5_000
_PAUSE_STYLES: frozenset[str] = frozenset({"ssml", "ellipsis", "emdash"})


@dataclass(frozen=True)
class _SSMLBreak:
    pause_ms: int


def _to_ssml_payload(parts: list[str | _SSMLBreak]) -> TTSInput:
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, _SSMLBreak):
            pause_ms = max(0, min(part.pause_ms, MAX_SSML_BREAK_MS))
            rendered.append(f'<break time="{pause_ms}ms"/>')
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

    The default ``style="ellipsis"`` remains audible with the bundled
    plain-text TTS providers. Use ``style="ssml"`` for exact ``pause_ms``
    breaks only when the provider advertises native SSML support. For
    ellipsis style, ``ellipsis_count`` controls single vs double cues
    (``1`` => ``...``, ``2`` => ``... ...``).
    """

    pattern: str
    pause_ms: int = 120
    unit_pattern: str = r"\S"
    minimum_units: int = 2
    flags: int = 0
    style: PauseStyle = "ellipsis"
    ellipsis_count: int = 1
    _pattern_re: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _unit_re: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.style not in _PAUSE_STYLES:
            raise ValueError(f"unsupported pause style: {self.style!r}")
        if (
            isinstance(self.minimum_units, bool)
            or not isinstance(self.minimum_units, int)
            or self.minimum_units < 1
        ):
            raise ValueError("minimum_units must be a positive integer")
        if self.style == "ellipsis" and (
            isinstance(self.ellipsis_count, bool)
            or not isinstance(self.ellipsis_count, int)
            or self.ellipsis_count < 1
        ):
            raise ValueError("ellipsis_count must be a positive integer")
        object.__setattr__(
            self, "_pattern_re", _compile_regex(self.pattern, self.flags, "pattern")
        )
        object.__setattr__(
            self,
            "_unit_re",
            _compile_regex(self.unit_pattern, 0, "unit_pattern"),
        )

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
        source = payload.text if payload.format == "plain" else strip_ssml_tags(payload.text)
        if self.style == "ssml":
            return self._process_ssml(payload, source)
        return self._process_plain(payload, source)

    def _matched_units(self, match_text: str) -> list[str] | None:
        units = [match.group(0) for match in self._unit_re.finditer(match_text)]
        return units if len(units) >= self.minimum_units else None

    def _process_ssml(self, payload: TTSInput, source: str) -> TTSInput:
        parts: list[str | _SSMLBreak] = []
        cursor = 0

        for match in self._pattern_re.finditer(source):
            units = self._matched_units(match.group(0))
            if units is None:
                continue
            parts.append(source[cursor : match.start()])
            self._append_ssml_units(parts, units)
            cursor = match.end()

        if not parts:
            return payload
        parts.append(source[cursor:])
        return _to_ssml_payload(parts)

    def _append_ssml_units(self, parts: list[str | _SSMLBreak], units: list[str]) -> None:
        for index, unit in enumerate(units):
            if index:
                parts.extend((" ", _SSMLBreak(self.pause_ms), " "))
            parts.append(unit)

    def _process_plain(self, payload: TTSInput, source: str) -> TTSInput:
        transformed = self._pattern_re.sub(self._replace_plain_match, source)
        if transformed == source:
            return payload
        return TTSInput(text=transformed, format="plain")

    def _replace_plain_match(self, match: re.Match[str]) -> str:
        units = self._matched_units(match.group(0))
        if units is None:
            return match.group(0)
        pause = " ".join(["..."] * self.ellipsis_count) if self.style == "ellipsis" else "—"
        return f" {pause} ".join(units)


def _compile_regex(pattern: str, flags: int, name: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid {name}: {exc}") from exc


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
            # Retain word boundaries for all terms, including punctuation like "C++" (gh 1004).
            pattern = re.compile(rf"(?<!\w){re.escape(source_term)}(?!\w)", flags=re.IGNORECASE)
            transformed = pattern.sub(spoken_term, transformed)

        if transformed == source:
            return payload
        return TTSInput(text=transformed, format="plain")


def default_pronunciation_processors(
    *,
    name_pronunciations: dict[str, str] | None = None,
    phone_pause_ms: int = 120,
    phone_pause_style: PauseStyle = "ellipsis",
    phone_ellipsis_count: int = 1,
) -> list[LLMOutputProcessor]:
    """Build the common stack for pronunciations + provider-compatible phone pauses."""
    processors: list[LLMOutputProcessor] = []
    if name_pronunciations:
        processors.append(PhoneticReplacementProcessor(name_pronunciations))
    processors.append(
        PauseProcessor(
            pattern=r"\+?\d[\d\s().-]{5,}\d",
            pause_ms=phone_pause_ms,
            unit_pattern=r"\d",
            minimum_units=7,
            style=phone_pause_style,
            ellipsis_count=phone_ellipsis_count,
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
