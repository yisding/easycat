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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial

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
    re.compile(r"^>\s+", re.MULTILINE),  # blockquotes
    re.compile(r"^---{1,}\s*$", re.MULTILINE),  # horizontal rules (dashes)
    re.compile(r"^```", re.MULTILINE),  # fenced code blocks
]


def has_markdown(text: str) -> bool:
    """Return ``True`` if *text* contains recognisable Markdown formatting."""
    return _has_markdown_link_or_image(text) or any(p.search(text) for p in _MD_DETECT_PATTERNS)


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
# Inline code span (CommonMark): a run of N backticks closed by the next run of
# exactly N backticks, so a double-backtick span can carry a literal backtick.
# The lookarounds pin both delimiter runs to their full length; an opener with
# no matching closer is left as literal text.  A span may wrap across line
# endings but not across a blank line: a blank line ends the paragraph, and
# pairing stray backticks from different paragraphs would swallow the prose
# between them.
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)((?:[^\n]|\n(?![ \t]*\r?\n))+?)(?<!`)\1(?!`)")
_CODE_SPAN_LINE_ENDING_RE = re.compile(r"\r\n|\r|\n")
_BOLD_ASTERISK_RE = re.compile(r"\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"__(?=\S)([\s\S]+?)(?<=\S)__")
_ITALIC_ASTERISK_RE = re.compile(r"(?<!\w)\*(?=\S)(.+?)(?<=\S)\*(?!\w)")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
# A whole ATX heading line, split into its opener and the rest of the line, so
# the optional closing ``#`` run (``# Title #``) can be dropped before the
# opener pass.  The closer is located with ``str.rstrip`` in the callback
# rather than a backtracking pattern, keeping the pass linear on long lines.
_HEADING_LINE_RE = re.compile(r"^(#{1,6}[ \t])([^\r\n]*)", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^(?:>[ \t]*)+", re.MULTILINE)
_UNORDERED_LIST_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
# Ordered lists: cap to 1–3 digits (mirrors the detect pattern) to avoid
# stripping leading year-like numeric sentences (e.g. "2026. We launched").
_ORDERED_LIST_RE = re.compile(r"^(\s*)\d{1,3}\.\s+", re.MULTILINE)
# Horizontal rules: three or more of one of ``-``, ``*`` or ``_`` alone on a
# line, optionally spaced (``* * *``) and indented up to three spaces.
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}\s*$", re.MULTILINE)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_WS_RE = re.compile(r"\s+")
_DUNDER_NAME_RE = re.compile(r"^__([A-Za-z][A-Za-z0-9_]*)__$")
# Code spans are stashed behind a sentinel while the markdown passes run.  The
# delimiters are private-use code points chosen so a placeholder cannot collide
# with the caller's own text: no markdown pass matches them, and any occurrence
# already present in the input is dropped before stashing.  A plaintext
# sentinel ("EASYCATCODETOKEN<n>X") could collide, and a literal one in model
# output was silently replaced with an unrelated stashed code span (gh 1069).
_CODE_TOKEN_OPEN = "\ue002"
_CODE_TOKEN_CLOSE = "\ue003"
_CODE_TOKEN_RE = re.compile(rf"{_CODE_TOKEN_OPEN}(\d+){_CODE_TOKEN_CLOSE}")
# Backslash-escaped emphasis markers (``\*`` / ``\_``) are literal text, not
# delimiters.  They are swapped for private-use stand-ins before the emphasis
# passes run and restored as the bare character afterwards, so ``\*x\*``
# reads as ``*x*`` instead of the italic regex eating the ``*`` and leaving a
# spoken "backslash".  A ``\\`` pair is consumed as a unit so the character
# after an escaped backslash is still treated as markdown.
_ESCAPED_STAR = "\ue004"
_ESCAPED_UNDERSCORE = "\ue005"
_ESCAPED_EMPHASIS_RE = re.compile(r"\\([\\*_])")
_ESCAPED_EMPHASIS_STANDINS = {"*": _ESCAPED_STAR, "_": _ESCAPED_UNDERSCORE}
_RESTORE_ESCAPED_EMPHASIS = str.maketrans({_ESCAPED_STAR: "*", _ESCAPED_UNDERSCORE: "_"})
_SENTINEL_CHARS_RE = re.compile(
    f"[{_CODE_TOKEN_OPEN}{_CODE_TOKEN_CLOSE}{_ESCAPED_STAR}{_ESCAPED_UNDERSCORE}]"
)

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
_CODE_SPEECH_CHARACTERS = frozenset(_SINGLE_CHAR_CODE_SPEECH)


def _stash_code_span(
    code_spans: list[str], extractor: Callable[[re.Match[str]], str]
) -> Callable[[re.Match[str]], str]:
    """Protect code text from markdown passes, restoring it at the end."""

    def _replace(match: re.Match[str]) -> str:
        code_spans.append(extractor(match))
        return f"{_CODE_TOKEN_OPEN}{len(code_spans) - 1}{_CODE_TOKEN_CLOSE}"

    return _replace


def _extract_inline_code(match: re.Match[str]) -> str:
    # CommonMark turns each line ending inside a code span into a space before
    # the padding rule below, so a wrapped span reads as one line.
    code = _CODE_SPAN_LINE_ENDING_RE.sub(" ", match.group(2))
    # CommonMark strips one space from each side when both are present and the
    # span is not all spaces; the padding lets a span start or end with a
    # backtick (a double-backtick span around " `a` " reads as "`a`").
    if code.startswith(" ") and code.endswith(" ") and code.strip(" "):
        return code[1:-1]
    return code


def _strip_heading_closing_sequence(match: re.Match[str], *, allow_at_text_end: bool) -> str:
    """Drop the optional closing ``#`` run of an ATX heading line.

    The run only counts as a closer when it is preceded by a space or tab and
    followed by nothing but spaces/tabs to the end of the line, so
    ``# I love C#``, ``# Title#`` and an escaped ``\\#`` keep their ``#``.
    When *allow_at_text_end* is ``False`` (streaming windows), a line that
    runs to the end of the text may still be continued by the next chunk, so
    it is left alone until its newline arrives.
    """
    if not allow_at_text_end and match.end() == len(match.string):
        return match.group(0)
    opener, rest = match.group(1), match.group(2)
    body = rest.rstrip(" \t")
    content = body.rstrip("#")
    if len(content) == len(body) or (content and content[-1] not in " \t"):
        return match.group(0)
    content = content.rstrip(" \t")
    # ``# #`` is an empty heading: nothing in it is speakable.
    return f"{opener}{content}" if content else ""


def _protect_escaped_emphasis(match: re.Match[str]) -> str:
    char = match.group(1)
    return _ESCAPED_EMPHASIS_STANDINS.get(char, match.group(0))


def _restore_code_spans(text: str, code_spans: list[str]) -> str:
    """Restore stashed code spans in a single pass over *text*."""
    if not code_spans:
        return text

    # A real placeholder index never has more digits than the largest stashed
    # index. Token-shaped substrings with an oversized digit run are left
    # unchanged, which also avoids parsing arbitrarily long ints (Python caps
    # int(str) at sys.set_int_max_str_digits and raises ValueError otherwise).
    max_width = len(str(len(code_spans) - 1))

    def _replace(match: re.Match[str]) -> str:
        digits = match.group(1)
        if len(digits) > max_width:
            return match.group(0)
        idx = int(digits)
        if idx >= len(code_spans):
            return match.group(0)
        return code_spans[idx]

    return _CODE_TOKEN_RE.sub(_replace, text)


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
    if _CODE_SPEECH_CHARACTERS.isdisjoint(snippet):
        return snippet if _WS_RE.search(snippet) is None else _WS_RE.sub(" ", snippet).strip()

    normalized = snippet
    for pattern, spoken in _MULTI_CHAR_CODE_SPEECH:
        normalized = normalized.replace(pattern, f" {spoken} ")

    normalized_chars: list[str] = []
    for ch in normalized:
        char_spoken = _SINGLE_CHAR_CODE_SPEECH.get(ch)
        if char_spoken is None:
            normalized_chars.append(ch)
            continue
        normalized_chars.append(f" {char_spoken} ")

    normalized = "".join(normalized_chars)
    normalized = _WS_RE.sub(" ", normalized).strip()
    return normalized if normalized else code


def _extract_fenced_code_for_tts(match: re.Match[str]) -> str:
    return _normalize_short_code_for_tts(_extract_fenced_code(match))


def _extract_inline_code_for_tts(match: re.Match[str]) -> str:
    return _normalize_short_code_for_tts(_extract_inline_code(match))


def _is_escaped(text: str, idx: int) -> bool:
    """Return True when character at *idx* is escaped by an odd '\' run."""
    backslashes = 0
    i = idx - 1
    while i >= 0 and text[i] == "\\":
        backslashes += 1
        i -= 1
    return backslashes % 2 == 1


class _DelimiterScanner:
    """Index balanced delimiters and malformed-input recovery in linear time."""

    def __init__(
        self,
        text: str,
        opener: str,
        closer: str,
        *,
        track_recovery: bool = False,
    ) -> None:
        self._matches: dict[int, int | None] = {}
        self._next_closes: dict[int, int | None] | None = None
        stack: list[int] = []
        i = 0
        length = len(text)
        while i < length:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == opener:
                stack.append(i)
            elif ch == closer and stack:
                self._matches[stack.pop()] = i
            i += 1

        for unmatched in stack:
            self._matches[unmatched] = None

        if track_recovery:
            self._next_closes = self._index_next_closes(text, closer)

    def _index_next_closes(self, text: str, closer: str) -> dict[int, int | None]:
        """Index literal recovery closers for malformed references."""
        next_closes: dict[int, int | None] = {}
        next_close: int | None = None
        for i in range(len(text) - 1, -1, -1):
            # Malformed-reference recovery historically advanced to the next
            # literal closer, including an escaped one. Keep that policy while
            # balanced matching above continues to honor escapes.
            if text[i] == closer:
                next_close = i
            if i in self._matches:
                next_closes[i] = next_close
        return next_closes

    def find_close(self, start: int) -> int | None:
        """Return the balanced close for the opener at *start*, if any."""
        return self._matches.get(start)

    def find_next_close(self, start: int) -> int | None:
        """Return the first literal closer after the opener at *start*."""
        if self._next_closes is None:
            raise RuntimeError("recovery indexing is disabled")
        return self._next_closes.get(start)


def _extract_markdown_destination_url(destination: str) -> str:
    """Extract URL token from markdown destination, dropping optional titles."""
    token = destination.strip()
    if not token:
        return ""

    if token.startswith("<"):
        end = token.find(">")
        if end > 1:
            return token[1:end].strip()

    i = 0
    while i < len(token):
        ch = token[i]
        if ch == "\\":
            i += 2
            continue
        if ch.isspace():
            break
        i += 1
    return token[:i].strip()


@dataclass(frozen=True, slots=True)
class _MarkdownReference:
    start: int
    end: int
    label: str
    destination_url: str
    is_image: bool


class _MarkdownReferenceScanner:
    """Yield balanced inline links/images and own malformed-input recovery."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._length = len(text)
        self._labels = _DelimiterScanner(text, "[", "]")
        self._destinations = _DelimiterScanner(text, "(", ")", track_recovery=True)

    def __iter__(self) -> Iterator[_MarkdownReference]:
        index = 0
        while index < self._length:
            candidate = self._candidate_at(index)
            if candidate is None:
                index += 1
                continue

            label_start, is_image = candidate
            reference, next_index = self._parse_candidate(
                start=index,
                label_start=label_start,
                is_image=is_image,
            )
            if reference is not None:
                yield reference
            if next_index is None:
                return
            index = next_index

    def _candidate_at(self, index: int) -> tuple[int, bool] | None:
        char = self._text[index]
        if (
            char == "!"
            and index + 1 < self._length
            and self._text[index + 1] == "["
            and not _is_escaped(self._text, index)
        ):
            return index + 1, True
        if char == "[" and not _is_escaped(self._text, index):
            return index, False
        return None

    def _parse_candidate(
        self,
        *,
        start: int,
        label_start: int,
        is_image: bool,
    ) -> tuple[_MarkdownReference | None, int | None]:
        label_end = self._labels.find_close(label_start)
        if label_end is None:
            return None, start + 1

        destination_start = self._destination_start(label_end + 1)
        if destination_start is None:
            return None, start + 1

        destination_end = self._destinations.find_close(destination_start)
        if destination_end is None:
            recovery_close = self._destinations.find_next_close(destination_start)
            return None, recovery_close + 1 if recovery_close is not None else None

        label = self._text[label_start + 1 : label_end].strip()
        destination = self._text[destination_start + 1 : destination_end]
        return (
            _MarkdownReference(
                start=start,
                end=destination_end + 1,
                label=label,
                destination_url=_extract_markdown_destination_url(destination),
                is_image=is_image,
            ),
            destination_end + 1,
        )

    def _destination_start(self, index: int) -> int | None:
        while index < self._length and self._text[index].isspace():
            index += 1
        if index >= self._length or self._text[index] != "(":
            return None
        return index


def _replace_markdown_links_and_images(
    text: str, protect_destination: Callable[[str], str] | None = None
) -> str:
    """Render links as label+URL and images as alt text for voice output.

    *protect_destination*, when given, maps a rendered link URL to the text
    spliced in its place, so ``strip_markdown`` can stash the URL behind a
    placeholder and keep the later emphasis passes away from it.
    """
    if "[" not in text:
        return text

    out: list[str] = []
    cursor = 0
    changed = False
    for reference in _MarkdownReferenceScanner(text):
        out.append(text[cursor : reference.start])
        out.append(_render_markdown_reference(reference, protect_destination))
        cursor = reference.end
        changed = True
    if not changed:
        return text
    out.append(text[cursor:])
    return "".join(out)


def _render_markdown_reference(
    reference: _MarkdownReference, protect_destination: Callable[[str], str] | None = None
) -> str:
    if reference.is_image:
        return reference.label
    url = reference.destination_url
    if url and protect_destination is not None:
        url = protect_destination(url)
    return " ".join(part for part in (reference.label, url) if part)


def _stash_link_destination(code_spans: list[str]) -> Callable[[str], str]:
    """Protect a rendered link URL from markdown passes, restoring it at the end.

    The URL is spoken verbatim, so ``_`` / ``*`` / ``~~`` inside it (Next.js
    ``/_next_/`` paths) must not be read as emphasis.  It shares the code-span
    stash; because the final restore is a single pass, the URL is resolved to
    its final text here: escaped-emphasis stand-ins become their characters
    and any code-span placeholder already inside it is expanded.
    """

    def _replace(url: str) -> str:
        url = _restore_code_spans(url.translate(_RESTORE_ESCAPED_EMPHASIS), code_spans)
        code_spans.append(url)
        return f"{_CODE_TOKEN_OPEN}{len(code_spans) - 1}{_CODE_TOKEN_CLOSE}"

    return _replace


def _has_markdown_link_or_image(text: str) -> bool:
    """Return True when *text* contains a balanced markdown link or image.

    This avoids the formerly regex-based ``[label](destination)`` detection,
    which could repeatedly rescan malformed fragments such as ``[x](``. Label
    matching is resolved once in linear time via :class:`_DelimiterScanner`,
    keeping detection O(n) on adversarial inputs such as ``"[" * n + ")"``.
    """
    return "[" in text and next(iter(_MarkdownReferenceScanner(text)), None) is not None


def strip_markdown(text: str, *, trim: bool = True, normalize_code_spans: bool = False) -> str:
    """Remove Markdown formatting from *text*, preserving readable content.

    Handles fenced code blocks, inline code, images, links, bold, italic,
    strikethrough, headings, blockquotes, lists, and horizontal rules.

    Link handling preserves both label and URL (for example ``[Docs](https://x)``
    becomes ``Docs https://x``). Image handling preserves alt text and removes
    destination URLs (for example ``![diagram](https://img)`` becomes
    ``diagram``).

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

    # Drop any sentinel character the input already carries, so a stashed
    # placeholder cannot be confused with the caller's own text.  These are
    # unassigned private-use code points with no spoken form, so removing them
    # costs nothing downstream (gh 1069).
    result = _SENTINEL_CHARS_RE.sub("", text)
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

    # 2a. Backslash-escaped emphasis markers are literal text: hide them from
    # the bold/italic/list/rule passes (code spans keep their escapes).
    result = _ESCAPED_EMPHASIS_RE.sub(_protect_escaped_emphasis, result)

    # 2b. Horizontal rules (---, ***, ___) before the emphasis and list passes,
    # which would otherwise eat part of a ``___`` / ``****`` / ``* * *`` rule
    # and leave stray characters for TTS to speak.
    result = _HR_RE.sub("", result)

    # 2c. ATX heading closers (``# Title #``) while the heading line is still
    # intact: a ``#`` that only ends the line once emphasis, strikethrough or
    # link markup has been stripped (``# **C #**``) is content, not a closer.
    result = _HEADING_LINE_RE.sub(
        partial(_strip_heading_closing_sequence, allow_at_text_end=trim), result
    )

    # 3/4. Links/images with balanced destination parsing.  A rendered link
    # URL is stashed like a code span so the emphasis, strikethrough, heading
    # and list passes below never rewrite it (``/_next_/`` keeps its ``_``).
    # Labels and image alt text stay in the text: they are prose.
    result = _replace_markdown_links_and_images(result, _stash_link_destination(code_spans))

    # 5. Bold (before italic so ** is matched before *)
    result = _BOLD_ASTERISK_RE.sub(r"\1", result)
    result = _BOLD_UNDERSCORE_RE.sub(r"\1", result)

    # 6. Italic
    result = _ITALIC_ASTERISK_RE.sub(r"\1", result)
    result = _ITALIC_UNDERSCORE_RE.sub(r"\1", result)

    # 7. Strikethrough
    result = _STRIKETHROUGH_RE.sub(r"\1", result)

    # 8. Headings: the opener (the closing ``#`` run went in step 2c).
    result = _HEADING_RE.sub("", result)

    # 9. Blockquotes
    result = _BLOCKQUOTE_RE.sub("", result)

    # 10. Unordered list markers (preserve indentation)
    result = _UNORDERED_LIST_RE.sub(r"\1", result)

    # 11. Ordered list markers (preserve indentation)
    result = _ORDERED_LIST_RE.sub(r"\1", result)

    # 12. Horizontal rules exposed by stripping blockquote/list markers
    # (e.g. ``> ---``).
    result = _HR_RE.sub("", result)

    # 13. Restore escaped emphasis markers as literal characters, then the
    # protected code spans and link URLs in one substitution pass.
    result = result.translate(_RESTORE_ESCAPED_EMPHASIS)
    result = _restore_code_spans(result, code_spans)

    # 14. Collapse runs of blank lines
    result = _EXCESS_BLANK_LINES_RE.sub("\n\n", result)

    return result.strip() if trim else result
