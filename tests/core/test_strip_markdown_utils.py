"""Tests for easycat.strip_markdown — detection and stripping utilities."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable

import pytest

from easycat.strip_markdown import _MarkdownReferenceScanner, has_markdown, strip_markdown

# ── Adversarial DoS payloads ───────────────────────────────────────
#
# These exercise the bracket-scanning paths that were previously quadratic
# (O(n^2)) on unbalanced input. ``"[" * n`` and ``"[" * n + "]"`` forced
# ``strip_markdown`` to rescan every opener to end-of-string, and
# ``"[" * n + ")"`` did the same for ``has_markdown``. The builders keep
# functional coverage for those malformed shapes without wall-clock assertions.
_ADVERSARIAL_PAYLOADS: tuple[tuple[str, Callable[[int], str]], ...] = (
    ("open_brackets", lambda n: "[" * n),
    ("open_brackets_then_paren", lambda n: "[" * n + ")"),
    ("open_brackets_then_close", lambda n: "[" * n + "]"),
)


def _malformed_destinations(count: int) -> str:
    return " ".join("[label](()" for _ in range(count))


# ── has_markdown detection ─────────────────────────────────────────


class TestHasMarkdown:
    def test_plain_text(self) -> None:
        assert not has_markdown("Hello, how can I help you today?")

    def test_bold(self) -> None:
        assert has_markdown("This is **bold** text")

    def test_italic_asterisk(self) -> None:
        assert has_markdown("This is *italic* text")

    def test_italic_underscore(self) -> None:
        assert has_markdown("This is _italic_ text")

    def test_bold_underscore(self) -> None:
        assert has_markdown("This is __bold__ text")

    def test_heading(self) -> None:
        assert has_markdown("# Heading")

    def test_heading_h3(self) -> None:
        assert has_markdown("### Sub-heading")

    def test_link(self) -> None:
        assert has_markdown("Click [here](https://example.com)")

    def test_link_with_parenthesized_url(self) -> None:
        assert has_markdown("See [Function](https://en.wikipedia.org/wiki/Function_(math))")

    def test_inline_code(self) -> None:
        assert has_markdown("Use `print()` to debug")

    def test_fenced_code_block(self) -> None:
        assert has_markdown("```\nprint('hello')\n```")

    def test_unordered_list(self) -> None:
        assert has_markdown("- item one\n- item two")

    def test_ordered_list(self) -> None:
        assert has_markdown("1. first\n2. second")

    def test_blockquote(self) -> None:
        assert has_markdown("> This is a quote")

    def test_horizontal_rule(self) -> None:
        assert has_markdown("---")

    def test_image(self) -> None:
        assert has_markdown("![alt text](image.png)")

    def test_image_with_parenthesized_url(self) -> None:
        assert has_markdown("![alt text](https://example.com/a(b))")

    def test_strikethrough(self) -> None:
        assert has_markdown("~~deleted~~")

    def test_snake_case_not_detected(self) -> None:
        """Underscores in snake_case identifiers should not trigger detection."""
        assert not has_markdown("The variable my_variable_name is defined")

    def test_empty_string(self) -> None:
        assert not has_markdown("")

    @pytest.mark.parametrize(
        "build", [b for _, b in _ADVERSARIAL_PAYLOADS], ids=[n for n, _ in _ADVERSARIAL_PAYLOADS]
    )
    def test_adversarial_brackets_not_detected(self, build: Callable[[int], str]) -> None:
        assert has_markdown(build(2000)) is False


class TestMarkdownReferenceScanner:
    @pytest.mark.parametrize(
        ("text", "detected", "expected"),
        [
            (
                "broken [x](a(b) then [ok](url)",
                True,
                "broken [x](a(b) then ok url",
            ),
            (
                "broken [x](a(b then [ok](url)",
                False,
                "broken [x](a(b then [ok](url)",
            ),
            (
                r"broken [x](a(b\) then [ok](url)",
                True,
                r"broken [x](a(b\) then ok url",
            ),
            ("![one](img) and [two](url)", True, "one and two url"),
            ("[label]   (url)", True, "label url"),
            ("[outer [inner]](url)", True, "outer [inner] url"),
            ('[Docs](https://example.test "title")', True, "Docs https://example.test"),
        ],
    )
    def test_detection_and_rendering_share_reference_policy(
        self,
        text: str,
        detected: bool,
        expected: str,
    ) -> None:
        assert has_markdown(text) is detected
        assert strip_markdown(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Press [Enter] (the big key on the right) to continue.",
            "Choose [yes] (or no) now.",
            "Press [Enter](the big key) now.",
            "Item [1] (see above).",
            "![chart] (shown below)",
            '[Docs](https://example.test "title" extra)',
            "[Enter]\n(Return)",
            "[Enter]\r\n(Return)",
            "[Enter]\v(Return)",
            "[Enter]\f(Return)",
            "[Enter]\x85(Return)",
            "[Enter]\u2028(Return)",
            "[Enter]\u2029(Return)",
            "[a](<b\u2028c>)",
        ],
    )
    def test_bracketed_prose_before_parenthetical_is_kept_verbatim(self, text: str) -> None:
        """A parenthetical that is not a valid link destination is prose.

        The scanner used to treat any ``[x] (...)`` as a link and keep only the
        first word inside the parentheses as its "URL", so ``Press [Enter]
        (the big key on the right)`` was spoken as ``Press Enter the``. A line
        break between ``]`` and ``(`` also made one.
        """
        assert has_markdown(text) is False
        assert strip_markdown(text) == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[Enter] (Return)", "Enter Return"),
            ("[Docs](https://example.test 'title')", "Docs https://example.test"),
            ("[Docs](https://example.test (title))", "Docs https://example.test"),
            ('[Docs](https://example.test "a \\" b")', "Docs https://example.test"),
            ("[Docs](<https://example.test/a b> 'title')", "Docs https://example.test/a b"),
            ("[Docs]( https://example.test )", "Docs https://example.test"),
            ("[Docs]()", "Docs"),
            ("![alt](img.png 'caption')", "alt"),
            ("[x](not a url) then [ok](url)", "[x](not a url) then ok url"),
            ("[x]([ok](url) more)", "[x](ok url more)"),
            ("[x](foo\\ 'title')", "x foo\\"),
        ],
    )
    def test_valid_destinations_with_titles_still_render(self, text: str, expected: str) -> None:
        assert has_markdown(text) is True
        assert strip_markdown(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[x](<foo\\>bar>)", "x foo\\>bar"),
            ("[x](<foo\\>bar> 'title')", "x foo\\>bar"),
        ],
    )
    def test_escaped_angle_close_stays_inside_destination(self, text: str, expected: str) -> None:
        """A backslash-escaped ``>`` does not close an ``<...>`` destination,
        and the rendered URL runs to the unescaped closing ``>``."""
        assert has_markdown(text) is True
        assert strip_markdown(text) == expected

    def test_backslash_does_not_escape_whitespace_in_destination(self) -> None:
        """CommonMark only escapes ASCII punctuation: ``foo\\ bar`` is two
        tokens, so the parenthetical is not a destination and stays prose."""
        text = "Use [x](foo\\ bar) here."
        assert has_markdown(text) is False
        assert strip_markdown(text) == text

    @pytest.mark.parametrize(
        "build",
        [
            lambda n: "[a](" * n + "x y" + ")" * n,
            lambda n: "[a](<" * n + "x y" + ")" * n,
        ],
        ids=["nested_tokens", "nested_angle"],
    )
    def test_nested_invalid_destinations_are_left_verbatim(
        self, build: Callable[[int], str]
    ) -> None:
        text = build(2000)
        assert has_markdown(text) is False
        assert strip_markdown(text) == text

    @pytest.mark.parametrize(
        ("prefix", "inner"),
        [
            ("[a](", 'x "{title}"'),
            ("[a](<", 'x> "{title}"'),
            ("[a](", "x ({title})"),
        ],
        ids=["double_quoted", "angle_double_quoted", "parenthesized"],
    )
    def test_nested_candidates_sharing_one_title_scan_it_once(
        self, monkeypatch: pytest.MonkeyPatch, prefix: str, inner: str
    ) -> None:
        """Every nesting level resolves to the same title start; only the
        innermost level's destination ends with the title, so it alone is a
        link. Each title start must be matched once, not once per level, or
        the scan turns quadratic in the nesting depth times the title length.
        """
        import easycat.strip_markdown as module

        title_pattern = module._LINK_TITLE_RE
        title_starts: list[int] = []

        class _CountingPattern:
            def match(self, text: str, pos: int = 0) -> re.Match[str] | None:
                title_starts.append(pos)
                return title_pattern.match(text, pos)

            def fullmatch(self, text: str, pos: int = 0, endpos: int = sys.maxsize) -> object:
                title_starts.append(pos)
                return title_pattern.fullmatch(text, pos, endpos)

        monkeypatch.setattr(module, "_LINK_TITLE_RE", _CountingPattern())
        n = 2000
        text = prefix * n + inner.format(title="t" * n) + ")" * n
        expected = prefix * (n - 1) + "a x" + ")" * (n - 1)

        assert has_markdown(text) is True
        assert title_starts
        assert len(title_starts) == len(set(title_starts))

        title_starts.clear()
        assert strip_markdown(text) == expected
        assert title_starts
        assert len(title_starts) == len(set(title_starts))

    def test_scanner_yields_consecutive_typed_references(self) -> None:
        references = list(
            _MarkdownReferenceScanner('![diagram](image.png) [Docs](https://example.test "title")')
        )

        assert [reference.is_image for reference in references] == [True, False]
        assert [reference.label for reference in references] == ["diagram", "Docs"]
        assert [reference.destination_url for reference in references] == [
            "image.png",
            "https://example.test",
        ]


# ── strip_markdown ─────────────────────────────────────────────────


class TestStripMarkdown:
    def test_empty_string(self) -> None:
        assert strip_markdown("") == ""

    def test_plain_text_unchanged(self) -> None:
        text = "Hello, how can I help you today?"
        assert strip_markdown(text) == text

    def test_bold_asterisks(self) -> None:
        assert strip_markdown("This is **bold** text") == "This is bold text"

    def test_bold_underscores(self) -> None:
        assert strip_markdown("This is __bold__ text") == "This is bold text"

    def test_italic_asterisk(self) -> None:
        assert strip_markdown("This is *italic* text") == "This is italic text"

    def test_italic_underscore(self) -> None:
        assert strip_markdown("This is _italic_ text") == "This is italic text"

    def test_bold_italic(self) -> None:
        assert strip_markdown("This is ***bold italic*** text") == "This is bold italic text"

    def test_strikethrough(self) -> None:
        assert strip_markdown("This is ~~deleted~~ text") == "This is deleted text"

    def test_inline_code(self) -> None:
        assert strip_markdown("Use `print()` for output") == "Use print() for output"

    def test_inline_code_preserves_literal_markdown_chars(self) -> None:
        text = "Use `__init__` and `*args*` literally"
        assert strip_markdown(text) == "Use __init__ and *args* literally"

    def test_inline_code_tts_normalization(self) -> None:
        text = "Use `print()` and `__init__`."
        assert (
            strip_markdown(text, normalize_code_spans=True)
            == "Use print open paren close paren and dunder init."
        )

    @pytest.mark.parametrize("whitespace", ["  ", "\t", "\v", "\N{NO-BREAK SPACE}"])
    def test_plain_inline_code_still_collapses_whitespace(self, whitespace: str) -> None:
        text = f"say `hello{whitespace}world` now"

        assert strip_markdown(text, normalize_code_spans=True) == "say hello world now"

    def test_long_inline_code_not_tts_normalized(self) -> None:
        text = "Use `very_long_identifier_name_for_internal_config`."
        assert (
            strip_markdown(text, normalize_code_spans=True)
            == "Use very_long_identifier_name_for_internal_config."
        )

    def test_many_inline_code_spans_are_restored(self) -> None:
        text = " ".join("`x`" for _ in range(1_000))
        result = strip_markdown(text, normalize_code_spans=True)

        assert result == " ".join("x" for _ in range(1_000))

    def test_oversized_token_shaped_digits_left_unchanged(self) -> None:
        # A real code span stashes one placeholder (index 0), then the input
        # carries a token-shaped substring with a 5000-digit run. The digit run
        # exceeds the largest stashed index width, so restoration must leave it
        # untouched without raising (int() caps very long digit strings).
        #
        # The sentinel delimiters are stripped from the input before stashing,
        # so this shape can only arise from the restore pass itself; the guard
        # is asserted directly on ``_restore_code_spans`` (gh 1069).
        from easycat.strip_markdown import (
            _CODE_TOKEN_CLOSE,
            _CODE_TOKEN_OPEN,
            _restore_code_spans,
        )

        oversized_token = _CODE_TOKEN_OPEN + ("1" * 5000) + _CODE_TOKEN_CLOSE
        placeholder = f"{_CODE_TOKEN_OPEN}0{_CODE_TOKEN_CLOSE}"

        result = _restore_code_spans(f"{placeholder} {oversized_token}", ["code"])

        assert result == f"code {oversized_token}"

    def test_literal_plaintext_token_text_is_preserved(self) -> None:
        # The sentinel used to be plaintext, so a literal occurrence in model
        # output was replaced with an unrelated stashed code span: this input
        # returned "code literal plus code" (gh 1069).
        text = "EASYCATCODETOKEN0X literal plus `code`"

        assert strip_markdown(text) == "EASYCATCODETOKEN0X literal plus code"

    def test_preexisting_sentinel_delimiters_cannot_collide(self) -> None:
        # A private-use delimiter arriving in the input is dropped before
        # stashing, so it can never be read back as a placeholder index.
        from easycat.strip_markdown import _CODE_TOKEN_CLOSE, _CODE_TOKEN_OPEN

        injected = f"{_CODE_TOKEN_OPEN}0{_CODE_TOKEN_CLOSE}"

        assert strip_markdown(f"{injected} literal plus `code`") == "0 literal plus code"

    def test_link(self) -> None:
        assert (
            strip_markdown("Visit [Google](https://google.com) for search")
            == "Visit Google https://google.com for search"
        )

    def test_image_removed(self) -> None:
        assert strip_markdown("Look at this: ![photo](image.jpg)") == "Look at this: photo"

    def test_link_with_parenthesized_url(self) -> None:
        text = "See [Function](https://en.wikipedia.org/wiki/Function_(mathematics))."
        assert (
            strip_markdown(text)
            == "See Function https://en.wikipedia.org/wiki/Function_(mathematics)."
        )

    def test_image_with_parenthesized_url(self) -> None:
        text = "Diagram: ![plot](https://example.com/a(b))."
        assert strip_markdown(text) == "Diagram: plot."

    @pytest.mark.parametrize(
        "build", [b for _, b in _ADVERSARIAL_PAYLOADS], ids=[n for n, _ in _ADVERSARIAL_PAYLOADS]
    )
    def test_adversarial_brackets_left_intact(self, build: Callable[[int], str]) -> None:
        payload = build(2000)
        assert strip_markdown(payload) == payload

    def test_malformed_destinations_are_left_intact(self) -> None:
        payload = _malformed_destinations(100)
        assert strip_markdown(payload) == payload

    def test_heading_h1(self) -> None:
        assert strip_markdown("# Main Title") == "Main Title"

    def test_heading_h3(self) -> None:
        assert strip_markdown("### Sub Title") == "Sub Title"

    def test_blockquote(self) -> None:
        assert strip_markdown("> Important note") == "Important note"

    def test_nested_blockquote(self) -> None:
        assert strip_markdown(">> Nested quote") == "Nested quote"

    def test_unordered_list_dash(self) -> None:
        text = "- First item\n- Second item"
        expected = "First item\nSecond item"
        assert strip_markdown(text) == expected

    def test_unordered_list_asterisk(self) -> None:
        text = "* First item\n* Second item"
        expected = "First item\nSecond item"
        assert strip_markdown(text) == expected

    def test_ordered_list(self) -> None:
        text = "1. First\n2. Second\n3. Third"
        expected = "First\nSecond\nThird"
        assert strip_markdown(text) == expected

    def test_ordered_list_up_to_three_digits(self) -> None:
        text = "100. First\n101. Second"
        expected = "First\nSecond"
        assert strip_markdown(text) == expected

    def test_numeric_sentence_with_year_preserved(self) -> None:
        text = "2026. We launched globally."
        assert strip_markdown(text) == text

    def test_horizontal_rule_dashes(self) -> None:
        text = "Above\n---\nBelow"
        result = strip_markdown(text)
        assert "---" not in result
        assert "Above" in result
        assert "Below" in result

    def test_multiple_underscore_horizontal_rules_are_removed(self) -> None:
        text = "A\n\n___\n\nB\n\n___\n\nC"
        assert strip_markdown(text) == "A\n\nB\n\nC"

    @pytest.mark.parametrize("rule", ["___", "_____", "****"])
    def test_horizontal_rule_underscores_and_long_asterisks_removed(self, rule: str) -> None:
        # HR lines are stripped before the emphasis passes, which would otherwise
        # eat part of the rule and leave a stray "_" / "**" that TTS would speak.
        result = strip_markdown(f"Above\n{rule}\nBelow")
        assert "_" not in result
        assert "*" not in result
        assert result.split() == ["Above", "Below"]

    def test_fenced_code_block(self) -> None:
        text = "Here is code:\n```python\nprint('hello')\n```"
        result = strip_markdown(text)
        assert "```" not in result
        assert "print('hello')" in result

    def test_fenced_code_block_no_language(self) -> None:
        text = "```\nsome code\n```"
        result = strip_markdown(text)
        assert "```" not in result
        assert "some code" in result

    def test_snake_case_preserved(self) -> None:
        """Underscores inside words (snake_case) should not be stripped."""
        text = "Set my_variable to 5"
        assert strip_markdown(text) == "Set my_variable to 5"

    def test_multiple_formatting_combined(self) -> None:
        text = "# Welcome\n\nThis is **bold** and *italic* with a [link](http://x.com)."
        result = strip_markdown(text)
        assert result == "Welcome\n\nThis is bold and italic with a link http://x.com."

    def test_blank_lines_collapsed(self) -> None:
        text = "Line one\n\n\n\nLine two"
        assert strip_markdown(text) == "Line one\n\nLine two"

    def test_trim_false_preserves_boundary_whitespace(self) -> None:
        text = "Then "
        assert strip_markdown(text, trim=False) == "Then "

    def test_typical_llm_response(self) -> None:
        """Simulate a typical LLM markdown response for voice output."""
        text = (
            "## How to Reset Your Password\n\n"
            "Here are the steps:\n\n"
            "1. Go to the **Settings** page\n"
            "2. Click on *Security*\n"
            "3. Select `Reset Password`\n\n"
            "For more info, visit [our help page](https://example.com/help)."
        )
        result = strip_markdown(text)
        assert "##" not in result
        assert "**" not in result
        assert "*Security*" not in result
        assert "`" not in result
        assert "[our help page]" not in result
        assert "Settings" in result
        assert "Security" in result
        assert "Reset Password" in result
        assert "our help page" in result
        assert "https://example.com/help" in result


# ── Backslash-escaped emphasis markers ─────────────────────────────


@pytest.mark.parametrize(
    ("text", "acceptable"),
    [
        (r"\*literal\*", {r"\*literal\*", "*literal*"}),
        (r"price \*5\* ok", {r"price \*5\* ok", "price *5* ok"}),
        (r"\_literal\_", {r"\_literal\_", "_literal_"}),
    ],
)
def test_strip_markdown_does_not_treat_escaped_asterisks_as_emphasis(
    text: str, acceptable: set[str]
) -> None:
    """``\\*x\\*`` is literal text in Markdown, not italic.

    The italic regex used to consume the ``*`` between the backslash and the
    word, so ``\\*literal\\*`` became ``\\literal\\`` and TTS spoke
    "backslash". The result must be either the untouched escape sequence or
    the unescaped literal marker.
    """
    assert strip_markdown(text) in acceptable


def test_strip_markdown_keeps_paragraph_break_inside_multi_paragraph_blockquote() -> None:
    """A bare ``>`` line separates quoted paragraphs and must stay a paragraph break.

    ``_BLOCKQUOTE_RE`` must only consume horizontal whitespace after ``>``; matching
    the newline would swallow the marker-only line together with the next line's
    marker and run the two quoted paragraphs together.
    """
    assert strip_markdown("> Quote one.\n>\n> Quote two.") == "Quote one.\n\nQuote two."


# ── ATX heading closing sequences ──────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Title #", "Title"),
        ("## Overview ##\n\nBody text.", "Overview\n\nBody text."),
        ("### Steps ###   ", "Steps"),
        ("#  Spaced  ##  ", "Spaced"),
        ("# Title # #", "Title #"),
        ("# **Bold** title #", "Bold title"),
        ("# Title #\r\nBody", "Title\r\nBody"),
        ("Intro.\n\n## Next ##\nMore.", "Intro.\n\nNext\nMore."),
        ("# #", ""),
    ],
)
def test_strip_markdown_removes_atx_heading_closing_sequence(text: str, expected: str) -> None:
    """``# Title #`` is an ATX heading whose trailing ``#`` run is only decoration.

    ``_HEADING_RE`` strips the opening marker but used to leave the closing
    sequence, so TTS spoke a stray "hash"/"pound" after the heading text.
    """
    assert strip_markdown(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# I love C#", "I love C#"),
        ("# Title#", "Title#"),
        ("# Title #x", "Title #x"),
        ("Issue #5 #", "Issue #5 #"),
        ("Issue #5 #\nNext", "Issue #5 #\nNext"),
        ("## Foo \\#", "Foo \\#"),
        ("####### Seven #", "####### Seven #"),
        ("# Use `x #` here", "Use x # here"),
        ("# **C #**", "C #"),
        ("# *x #*", "x #"),
        ("# ~~value #~~", "value #"),
        ("# Title **#**", "Title #"),
    ],
)
def test_strip_markdown_keeps_hash_that_is_not_a_heading_closer(text: str, expected: str) -> None:
    """Only a whitespace-preceded ``#`` run ending a heading line is a closer."""
    assert strip_markdown(text) == expected


def test_strip_markdown_streaming_strips_heading_closer_only_at_end_of_line() -> None:
    """A ``trim=False`` window may end mid-line, so the closer needs its newline."""
    assert strip_markdown("## Overview ##\nBody", trim=False) == "Overview\nBody"
    assert strip_markdown("# Title #", trim=False) == "Title #"


def test_strip_markdown_heading_closer_scan_handles_long_lines() -> None:
    """Closer detection stays linear on a long heading line full of ``#`` runs."""
    body = " a #" * 20_000
    assert strip_markdown(f"#{body}") == body.strip().removesuffix(" #")
    assert strip_markdown(f"#{body}x") == f"{body}x".strip()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("``a``", "a"),
        ("``co`de``", "co`de"),
        ("see ``a`b`` ok", "see a`b ok"),
        ("```a``` and ``b``", "a and b"),
        ("``a`` then `b` then ``c`d``", "a then b then c`d"),
        ("``__init__`` and ``*x*``", "__init__ and *x*"),
    ],
)
def test_strip_markdown_handles_multi_backtick_inline_code_spans(text: str, expected: str) -> None:
    """A CommonMark code span delimited by N backticks must lose its delimiters.

    ``_INLINE_CODE_RE`` only matched single-backtick spans, leaving stray
    backticks in the TTS text. Double-backtick spans are the standard way to
    embed a literal backtick in inline code.
    """
    assert strip_markdown(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # One padding space is stripped from each side so a span can begin or
        # end with a literal backtick.
        ("`` `a` ``", "`a`"),
        ("say `` ` `` now", "say ` now"),
        ("x ` a ` y", "x a y"),
        # Padding on only one side is kept.
        ("x `` a`` y", "x  a y"),
        # An all-space span is not stripped.
        ("x ``  `` y", "x    y"),
    ],
)
def test_strip_markdown_strips_one_padding_space_from_code_spans(text: str, expected: str) -> None:
    assert strip_markdown(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # A closer must be exactly as long as its opener; otherwise the run is
        # literal text.
        "``a```",
        "```a``",
        "`a`` b",
        "a ` b",
        "a `` b",
    ],
)
def test_strip_markdown_leaves_unmatched_backtick_runs_literal(text: str) -> None:
    assert strip_markdown(text) == text


def test_strip_markdown_unmatched_opener_run_does_not_swallow_later_span() -> None:
    # ``a has no closing double run, so it stays literal; the later single
    # backtick pair is still a code span.
    assert strip_markdown("``a `b` c") == "``a b c"


def test_strip_markdown_normalizes_multi_backtick_code_spans_for_tts() -> None:
    text = "Call ``print()`` or ``a`b``."

    assert (
        strip_markdown(text, normalize_code_spans=True)
        == "Call print open paren close paren or a`b."
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # CommonMark: a code span may wrap across line endings, and each line
        # ending inside it reads as a single space.
        ("`foo\nbar`", "foo bar"),
        ("``a\nb``", "a b"),
        ("`foo\r\nbar`", "foo bar"),
        ("``a\r\nb``", "a b"),
        ("Run `pip\ninstall easycat` now.", "Run pip install easycat now."),
        # The line ending becomes a space before the padding rule runs.
        ("``\nfoo\n``", "foo"),
    ],
)
def test_strip_markdown_inline_code_span_crosses_line_endings(text: str, expected: str) -> None:
    assert strip_markdown(text) == expected
    assert strip_markdown(text, trim=False) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Use `x here.\n\nThen y` there.",
        "Use ``x here.\n\nThen y`` there.",
        "Use `x here.\r\n\r\nThen y` there.",
        # A whitespace-only line is still a blank line.
        "Use `x here.\n  \t\nThen y` there.",
    ],
)
def test_strip_markdown_inline_code_span_does_not_cross_blank_line(text: str) -> None:
    """Backticks in different paragraphs must not pair into one code span.

    A blank line ends the paragraph, so the opener and closer are literal
    text; pairing them would hide the prose between them inside a span.
    """
    assert strip_markdown(text) == text


def test_strip_markdown_wrapped_code_span_normalizes_for_tts() -> None:
    assert (
        strip_markdown("Call `print(\n)` now.", normalize_code_spans=True)
        == "Call print open paren close paren now."
    )


def test_strip_markdown_fenced_code_wins_over_wrapped_inline_span() -> None:
    text = "```py\nx = `a\nb`\n```"

    assert strip_markdown(text) == "x = `a\nb`"
