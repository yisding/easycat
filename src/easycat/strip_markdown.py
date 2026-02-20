"""Markdown detection and stripping for voice output.

LLMs sometimes produce Markdown-formatted text even when the output is
destined for TTS. Markdown artefacts (``**``, ``#``, backticks, etc.)
cause TTS engines to literally speak the formatting characters, degrading
voice quality.

This module provides lightweight, regex-based utilities to detect and
strip common Markdown formatting while preserving the readable text
content.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ── Detection patterns ─────────────────────────────────────────────

_MD_DETECT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\*\*(?=\S).+?(?<=\S)\*\*"),  # bold **text**
    re.compile(r"__(?=\S).+?(?<=\S)__"),  # bold __text__
    re.compile(r"(?<!\w)\*(?=\S)(.+?)(?<=\S)\*(?!\w)"),  # italic *text*
    re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"),  # italic _text_
    re.compile(r"~~.+?~~"),  # strikethrough
    re.compile(r"`.+?`"),  # inline code
    re.compile(r"^#{1,6}\s+", re.MULTILINE),  # headings
    re.compile(r"^\s*[-*+]\s+", re.MULTILINE),  # unordered lists
    # Ordered lists: intentionally cap to 1–3 digits to avoid stripping
    # leading year-like numeric sentences (e.g. "2026. We launched").
    re.compile(r"^\s*\d{1,3}\.\s+", re.MULTILINE),
    re.compile(r"\[.+?\]\(.+?\)"),  # links
    re.compile(r"!\[.*?\]\(.+?\)"),  # images
    re.compile(r"^>\s+", re.MULTILINE),  # blockquotes
    re.compile(r"^---{1,}\s*$", re.MULTILINE),  # horizontal rules (dashes)
    re.compile(r"^```", re.MULTILINE),  # fenced code blocks
]


def has_markdown(text: str) -> bool:
    """Return ``True`` if *text* contains recognisable Markdown formatting."""
    return any(p.search(text) for p in _MD_DETECT_PATTERNS)


# ── Stripping ──────────────────────────────────────────────────────


def _extract_fenced_code(match: re.Match[str]) -> str:
    """Extract the body of a fenced code block, discarding the fence markers."""
    body = match.group(1)
    # Strip the optional language identifier on the first line
    lines = body.split("\n", 1)
    if len(lines) == 2:
        return lines[1].strip()
    return body.strip()


_FENCED_CODE_RE = re.compile(r"```([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`(.+?)`")
_IMAGE_RE = re.compile(r"!\[.*?\]\(.+?\)")
_LINK_RE = re.compile(r"\[(.+?)\]\(.+?\)")
_BOLD_ASTERISK_RE = re.compile(r"\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"__(?=\S)([\s\S]+?)(?<=\S)__")
_ITALIC_ASTERISK_RE = re.compile(r"(?<!\w)\*(?=\S)(.+?)(?<=\S)\*(?!\w)")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^(?:>\s*)+", re.MULTILINE)
_UNORDERED_LIST_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_ORDERED_LIST_RE = re.compile(r"^(\s*)\d+\.\s+", re.MULTILINE)
_HR_DASH_RE = re.compile(r"^-{3,}\s*$", re.MULTILINE)
_HR_ASTERISK_RE = re.compile(r"^\*{3,}\s*$", re.MULTILINE)
_HR_UNDERSCORE_RE = re.compile(r"^_{3,}\s*$", re.MULTILINE)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_WS_RE = re.compile(r"\s+")
_DUNDER_NAME_RE = re.compile(r"^__([A-Za-z][A-Za-z0-9_]*)__$")

_SHORT_CODE_MAX_CHARS = 24

_MULTI_CHAR_CODE_SPEECH: tuple[tuple[str, str], ...] = (
    ("===", "triple equals"),
    ("==", "equals equals"),
    ("!=", "not equals"),
    (">=", "greater than or equal to"),
    ("<=", "less than or equal to"),
    ("=>", "arrow"),
    ("->", "arrow"),
    ("::", "double colon"),
    ("&&", "and and"),
    ("||", "or or"),
    ("**", "star star"),
)

_SINGLE_CHAR_CODE_SPEECH: dict[str, str] = {
    "(": "open paren",
    ")": "close paren",
    "[": "open bracket",
    "]": "close bracket",
    "{": "open brace",
    "}": "close brace",
    "<": "less than",
    ">": "greater than",
    "_": "underscore",
    "*": "star",
    "/": "slash",
    "\\": "backslash",
    "|": "pipe",
    "&": "ampersand",
    "+": "plus",
    "-": "minus",
    "=": "equals",
    ".": "dot",
    ",": "comma",
    ":": "colon",
}


def _stash_code_span(
    code_spans: list[str], extractor: Callable[[re.Match[str]], str]
) -> Callable[[re.Match[str]], str]:
    """Protect code text from markdown passes, restoring it at the end."""

    def _replace(match: re.Match[str]) -> str:
        code_spans.append(extractor(match))
        return f"EASYCATCODETOKEN{len(code_spans) - 1}X"

    return _replace


def _extract_inline_code(match: re.Match[str]) -> str:
    return match.group(1)


def _normalize_short_code_for_tts(code: str) -> str:
    """Convert short code snippets to speech-friendly text."""
    snippet = code.strip()
    if not snippet:
        return snippet
    if "\n" in snippet or "\r" in snippet or len(snippet) > _SHORT_CODE_MAX_CHARS:
        return code

    dunder_match = _DUNDER_NAME_RE.fullmatch(snippet)
    if dunder_match:
        dunder_name = dunder_match.group(1).replace("_", " ")
        return f"dunder {dunder_name}".strip()

    normalized = snippet
    for pattern, spoken in _MULTI_CHAR_CODE_SPEECH:
        normalized = normalized.replace(pattern, f" {spoken} ")

    normalized_chars: list[str] = []
    for ch in normalized:
        spoken = _SINGLE_CHAR_CODE_SPEECH.get(ch)
        if spoken is None:
            normalized_chars.append(ch)
            continue
        normalized_chars.append(f" {spoken} ")

    normalized = "".join(normalized_chars)
    normalized = _WS_RE.sub(" ", normalized).strip()
    return normalized if normalized else code


def _extract_fenced_code_for_tts(match: re.Match[str]) -> str:
    return _normalize_short_code_for_tts(_extract_fenced_code(match))


def _extract_inline_code_for_tts(match: re.Match[str]) -> str:
    return _normalize_short_code_for_tts(_extract_inline_code(match))


def strip_markdown(
    text: str, *, trim: bool = True, normalize_code_spans: bool = False
) -> str:
    """Remove Markdown formatting from *text*, preserving readable content.

    Handles fenced code blocks, inline code, images, links, bold, italic,
    strikethrough, headings, blockquotes, lists, and horizontal rules.

    Returns the cleaned text with extra blank lines collapsed.

    Parameters
    ----------
    trim:
        When ``True`` (default), trims leading/trailing whitespace on the
        final result. Set to ``False`` for incremental/streaming use cases
        that must preserve chunk-boundary spaces.
    normalize_code_spans:
        When ``True``, converts short inline/fenced code snippets to
        speech-friendly text (e.g. ``print()`` -> ``print open paren close
        paren``), while leaving longer code unchanged.
    """
    if not text:
        return text

    result = text
    code_spans: list[str] = []

    # 1. Fenced and inline code: remove markdown wrappers, then protect
    # extracted text from later markdown regex passes.
    fenced_extractor: Callable[[re.Match[str]], str] = _extract_fenced_code
    inline_extractor: Callable[[re.Match[str]], str] = _extract_inline_code
    if normalize_code_spans:
        fenced_extractor = _extract_fenced_code_for_tts
        inline_extractor = _extract_inline_code_for_tts
    result = _FENCED_CODE_RE.sub(_stash_code_span(code_spans, fenced_extractor), result)
    result = _INLINE_CODE_RE.sub(_stash_code_span(code_spans, inline_extractor), result)

    # 3. Images → remove entirely
    result = _IMAGE_RE.sub("", result)

    # 4. Links → keep link text
    result = _LINK_RE.sub(r"\1", result)

    # 5. Bold (before italic so ** is matched before *)
    result = _BOLD_ASTERISK_RE.sub(r"\1", result)
    result = _BOLD_UNDERSCORE_RE.sub(r"\1", result)

    # 6. Italic
    result = _ITALIC_ASTERISK_RE.sub(r"\1", result)
    result = _ITALIC_UNDERSCORE_RE.sub(r"\1", result)

    # 7. Strikethrough
    result = _STRIKETHROUGH_RE.sub(r"\1", result)

    # 8. Headings
    result = _HEADING_RE.sub("", result)

    # 9. Blockquotes
    result = _BLOCKQUOTE_RE.sub("", result)

    # 10. Unordered list markers (preserve indentation)
    result = _UNORDERED_LIST_RE.sub(r"\1", result)

    # 11. Ordered list markers (preserve indentation)
    result = re.sub(r"^(\s*)\d{1,3}\.\s+", r"\1", result, flags=re.MULTILINE)

    # 12. Horizontal rules (---, ***, ___)
    result = _HR_DASH_RE.sub("", result)
    result = _HR_ASTERISK_RE.sub("", result)
    result = _HR_UNDERSCORE_RE.sub("", result)

    # 13. Restore protected code spans.
    for idx, code in enumerate(code_spans):
        result = result.replace(f"EASYCATCODETOKEN{idx}X", code)

    # 14. Collapse runs of blank lines
    result = _EXCESS_BLANK_LINES_RE.sub("\n\n", result)

    return result.strip() if trim else result
