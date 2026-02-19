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
    result = re.sub(r"```([\s\S]*?)```", _extract_fenced_code, result)

    # 2. Inline code → keep content
    result = re.sub(r"`(.+?)`", r"\1", result)

    # 3. Images → remove entirely
    result = re.sub(r"!\[.*?\]\(.+?\)", "", result)

    # 4. Links → keep link text
    result = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", result)

    # 5. Bold (before italic so ** is matched before *)
    result = re.sub(r"\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*", r"\1", result)
    result = re.sub(r"__(?=\S)([\s\S]+?)(?<=\S)__", r"\1", result)

    # 6. Italic
    result = re.sub(r"(?<!\w)\*(?=\S)(.+?)(?<=\S)\*(?!\w)", r"\1", result)
    result = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", r"\1", result)

    # 7. Strikethrough
    result = re.sub(r"~~(.+?)~~", r"\1", result)

    # 8. Headings
    result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)

    # 9. Blockquotes
    result = re.sub(r"^(?:>\s*)+", "", result, flags=re.MULTILINE)

    # 10. Unordered list markers (preserve indentation)
    result = re.sub(r"^(\s*)[-*+]\s+", r"\1", result, flags=re.MULTILINE)

    # 11. Ordered list markers (preserve indentation)
    result = re.sub(r"^(\s*)\d+\.\s+", r"\1", result, flags=re.MULTILINE)

    # 12. Horizontal rules (---, ***, ___)
    result = re.sub(r"^-{3,}\s*$", "", result, flags=re.MULTILINE)
    result = re.sub(r"^\*{3,}\s*$", "", result, flags=re.MULTILINE)
    result = re.sub(r"^_{3,}\s*$", "", result, flags=re.MULTILINE)

    # 13. Collapse runs of blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result.strip()
