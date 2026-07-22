"""Unit tests for session helper functions.

Tests for has_unclosed_markdown_delimiters.
"""

from __future__ import annotations

from easycat.session.text import has_unclosed_markdown_delimiters


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
