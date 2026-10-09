"""Unit tests for session helper functions.

Tests for has_unclosed_markdown_delimiters.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

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
        assert not has_unclosed_markdown_delimiters("Hello `world` now")

    def test_closing_backtick_at_text_end_is_open(self) -> None:
        # The closing run may still grow into a run that no longer closes it.
        assert has_unclosed_markdown_delimiters("Hello `world`")

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
            "``co`de`` here",
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
            r"Type `ls\` now",
            # An escaped backslash leaves the following backtick unescaped.
            r"\\`code` here",
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


class TestFenceRunsInsideInlineSpans:
    """Fences and inline spans share ``strip_markdown``'s left-to-right scan.

    A triple-backtick run inside an already-open inline span is span content,
    so the span's closer, not the inner run, decides whether it is closed.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "``a ```b``` c`` here",
            "`a ```b``` c` done.",
            "`x` ```py\ncode\n``` done.",
            "`x ```y` z ``k`` ok",
        ],
    )
    def test_closed(self, text: str) -> None:
        assert markdown_open_state(text) == (False, False)

    @pytest.mark.parametrize(
        "text",
        [
            "``a ```b",
            "``a ```b``` c",
            "`x` ```py\ncode",
            # The span closes before the trailing ```, which a later ``` could
            # still turn into a fence.
            "`x ```y` z```",
        ],
    )
    def test_open(self, text: str) -> None:
        assert markdown_open_state(text) == (True, False)


class TestBackToBackAndLongFences:
    """Fence openers and four-backtick fence lines judge openness like ``strip_markdown``."""

    @pytest.mark.parametrize(
        "text",
        [
            # The six-backtick run closes the first block and opens the second.
            "```py\nx\n``````js\ny\n```",
            "```py\nx\n``````js\ny\n``` done.",
            # The stray tick cannot pair across the four-backtick block.
            "`a\n````\ncode with `tick\n````\n\nNext.",
        ],
    )
    def test_closed(self, text: str) -> None:
        assert markdown_open_state(text) == (False, False)

    @pytest.mark.parametrize(
        "text",
        [
            "```py\nx\n``````js\ny",
            "`a\n````\ncode with `tick",
            # The ```` line opens a fence with no closer yet, so a later
            # ```` could still close it.  The span cannot cross that line, so
            # the text stays literal once complete (as with a ``` line).
            "``a\n````\nb``",
        ],
    )
    def test_open(self, text: str) -> None:
        assert markdown_open_state(text) == (True, False)


class TestInlineSpanClosingRunAtTextEnd:
    """A span whose closing run ends the text stays open: the run may still grow.

    ``It`s ... ```f()` `` read as one span until the next delta made the
    trailing tick a ```` ``` ```` that closes ```` ```f()``` ```` as a fence,
    so the sentence already split off inside the span was rewritten.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Hello `world`",
            "Sure thing. It`s simple. First call ```f()`",
            "Start here. Wrap ``a. b Call ```f()``",
            "`x` and `y`",
        ],
    )
    def test_open(self, text: str) -> None:
        assert markdown_open_state(text) == (True, False)

    @pytest.mark.parametrize(
        "text",
        [
            "Hello `world` now",
            "Sure thing. It`s simple. First call ```f()` and wait.",
            # A fence closer at the end is settled: growing it keeps the block.
            "First call ```f()```",
        ],
    )
    def test_closed(self, text: str) -> None:
        assert markdown_open_state(text) == (False, False)


def test_unclosed_fence_stops_the_code_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unpaired fence opener ends the scan, so a streaming block rechecks cheaply.

    Nothing after it can change the answer, yet every inline span in the code
    after it used to be matched on each recheck of the growing block.
    """
    from easycat.session import text as session_text

    real = session_text._CODE_SPAN_RE
    matches: list[str] = []

    class _CountingPattern:
        def finditer(self, string: str) -> Iterator[re.Match[str]]:
            for match in real.finditer(string):
                matches.append(match.group(0))
                yield match

    monkeypatch.setattr(session_text, "_CODE_SPAN_RE", _CountingPattern())
    block = "Here.\n```js\n" + "const s = `hello ${a}`;\n" * 500

    assert markdown_open_state(block) == (True, False)
    assert matches == ["```"]


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
            r"\```x```. Next ``` `` ok",
            # The `` run pairs and the ``` inside it is span content: the scan
            # reaches the span first, so a later ``` cannot reopen it as a fence.
            r"\```x```. Next `` ok",
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
            # No fence here: what is left after the escape is an unpaired ``.
            "Hello there.\n\\```x",
            r"See \``` here.",
        ],
    )
    def test_open(self, text: str) -> None:
        assert markdown_open_state(text) == (True, False)
