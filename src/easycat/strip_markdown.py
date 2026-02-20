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
    re.compile(r"^\s*\d+\.\s+", re.MULTILINE),  # ordered lists
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


def strip_markdown(text: str) -> str:
    """Remove Markdown formatting from *text*, preserving readable content.

    Handles fenced code blocks, inline code, images, links, bold, italic,
    strikethrough, headings, blockquotes, lists, and horizontal rules.

    Returns the cleaned text with extra blank lines collapsed.
    """
    if not text:
        return text

    result = text

    # 1. Fenced code blocks (```lang\n...\n```) → keep body only
    result = _FENCED_CODE_RE.sub(_extract_fenced_code, result)

    # 2. Inline code → keep content
    result = _INLINE_CODE_RE.sub(r"\1", result)

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
    result = _ORDERED_LIST_RE.sub(r"\1", result)

    # 12. Horizontal rules (---, ***, ___)
    result = _HR_DASH_RE.sub("", result)
    result = _HR_ASTERISK_RE.sub("", result)
    result = _HR_UNDERSCORE_RE.sub("", result)

    # 13. Collapse runs of blank lines
    result = _EXCESS_BLANK_LINES_RE.sub("\n\n", result)

    return result.strip()
