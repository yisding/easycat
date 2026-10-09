"""Unit tests for session helper functions.

Tests for has_unclosed_markdown_delimiters.
"""

from __future__ import annotations

import pytest

from easycat.session.text import has_unclosed_markdown_delimiters, markdown_open_state


class TestMarkdownDelimiters:
    """Tests for has_unclosed_markdown_delimiters edge cases."""

    def test_empty_string(self) -> None:
        assert not has_unclosed_markdown_delimiters("")

    def test_no_markdown(self) -> None:
        assert not has_unclosed_markdown_delimiters("Hello world")

    def test_unclosed_backtick(self) -> None:
        assert has_unclosed_markdown_delimiters("Hello `world")

    def test_closed_backtick(self) -> None:
        assert not has_unclosed_markdown_delimiters("Hello `world`")

    def test_unclosed_triple_backtick(self) -> None:
        assert has_unclosed_markdown_delimiters("```python\nprint('hi')")

    def test_closed_triple_backtick(self) -> None:
        assert not has_unclosed_markdown_delimiters("```python\nprint('hi')\n```")

    def test_unclosed_bold(self) -> None:
        assert has_unclosed_markdown_delimiters("Hello **world")

    def test_closed_bold(self) -> None:
        assert not has_unclosed_markdown_delimiters("Hello **world**")

    def test_unclosed_link(self) -> None:
        assert has_unclosed_markdown_delimiters("Click [here")

    def test_closed_link(self) -> None:
        assert not has_unclosed_markdown_delimiters("Click [here](http://example.com)")

    def test_unclosed_strikethrough(self) -> None:
        assert has_unclosed_markdown_delimiters("Hello ~~world")

    def test_nested_backticks_in_fenced(self) -> None:
        text = "```\nHello `world`\n```"
        assert not has_unclosed_markdown_delimiters(text)

    def test_unclosed_image(self) -> None:
        assert has_unclosed_markdown_delimiters("![alt text")


class TestInlineCodeRunPairing:
    """Inline code spans pair backtick runs of equal length (CommonMark).

    A run of N backticks closes only at the next run of exactly N; runs of
    other lengths inside are content.  Odd/even backtick parity got both
    directions wrong for multi-backtick spans.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "`a",
            "Use ``obj.",
            # The single backtick is content, not the closer of the ``.
            "Use ``obj.`",
            # ``a has no closer yet; a later `` would swallow the `b` span.
            "``a `b` c",
            "Use ``obj.\nmore",
            # Fence handling is unchanged.
            "```",
        ],
    )
    def test_open(self, text: str) -> None:
        assert markdown_open_state(text) == (True, False)

    @pytest.mark.parametrize(
        "text",
        [
            "Use ``obj.`method()``.",
            "``co`de``",
            "`a` and ``b`` and ```c```",
            "Use `wrapped\ncode` here.",
            # A blank line ends the paragraph, so the opener is literal for
            # good and cannot hold the window open.
            "Use `x here.\n\nThen more.",
            "Use ``x here.\r\n\r\nThen more.",
        ],
    )
    def test_closed(self, text: str) -> None:
        assert markdown_open_state(text) == (False, False)

    def test_emphasis_inside_multi_backtick_span_is_ignored(self) -> None:
        assert not has_unclosed_markdown_delimiters("Call ``a`**`` now.")
        assert has_unclosed_markdown_delimiters("Call ``a`b`` and **still open")


class TestEscapedBacktickPairing:
    """An escaped backtick (``\\` ``) is literal text, matching ``strip_markdown``.

    It must neither open a run that holds the streaming window open nor close
    or pair with a real run, while a backslash inside a code span stays literal
    and never escapes that span's closing backtick.
    """

    @pytest.mark.parametrize(
        "text",
        [
            r"Type \`ls",
            r"Type \`ls\` to list.",
            r"x \`*a*",
            # The backslash is code content, so its backtick closes the span.
            r"Type `ls\`",
            # An escaped backslash leaves the following backtick unescaped.
            r"\\`code`",
        ],
    )
    def test_closed(self, text: str) -> None:
        assert markdown_open_state(text) == (False, False)

    @pytest.mark.parametrize(
        "text",
        [
            # The escaped backtick cannot pair with the real opener after it.
            r"Use \` c `a b",
            r"\\`still open",
            # An escaped backtick hides the emphasis that follows it no more
            # than any other literal text does.
            r"\` and **still open",
        ],
    )
    def test_open(self, text: str) -> None:
        assert markdown_open_state(text) == (True, False)


class TestEscapedBacktickFences:
    """An escaped backtick never opens or closes a fence, as in ``strip_markdown``.

    The fence scan consumes escapes in document order, so the backtick run
    after ``\\` `` is judged on its own: ``\\``` `` is a literal tick then a
    two-backtick inline run, never a fence delimiter.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "```a```",
            # An escaped backslash leaves the fence after it unescaped.
            r"\\```a```",
            # An escaped tick before a real fence pair: the pair still closes.
            r"\` then ```a``` ok.",
            r"\``` then ```a``` and ``.",
            # An escaped tick, then a two-backtick run closed by a later ``.
            r"See \``` here``.",
            r"Hello there. See \```x`` now.",
            # The fence after the escaped tick closes and the `` run pairs.
            r"\```x```. Next ``` ``",
        ],
    )
    def test_closed(self, text: str) -> None:
        assert markdown_open_state(text) == (False, False)

    @pytest.mark.parametrize(
        "text",
        [
            "```a",
            # strip_markdown leaves an unpaired `` run (and a lone ```) here,
            # so a later delta could still rewrite the text after it.
            r"\```x```.",
            # The `` run pairs, but the lone ``` inside it could still open a
            # fence with a later ```, which strip_markdown matches first.
            r"\```x```. Next ``",
            # No fence here: what is left after the escape is an unpaired ``.
            "Hello there.\n\\```x",
            r"See \``` here.",
        ],
    )
    def test_open(self, text: str) -> None:
        assert markdown_open_state(text) == (True, False)
