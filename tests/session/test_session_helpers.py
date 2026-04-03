"""Unit tests for session helper functions.

Tests for _estimate_text_spoken, _split_at_sentence_boundaries,
and _has_unclosed_markdown_delimiters.
"""

from __future__ import annotations

from easycat.session import (
    _estimate_text_spoken,
    _has_unclosed_markdown_delimiters,
    _split_at_sentence_boundaries,
)


class TestEstimateTextSpoken:
    """Tests for _estimate_text_spoken edge cases."""

    def test_empty_chunks(self) -> None:
        assert _estimate_text_spoken([], 100) == ""

    def test_zero_bytes_sent(self) -> None:
        assert _estimate_text_spoken([("hello", 100)], 0) == ""

    def test_negative_bytes_sent(self) -> None:
        assert _estimate_text_spoken([("hello", 100)], -10) == ""

    def test_full_delivery(self) -> None:
        assert _estimate_text_spoken([("hello", 100)], 100) == "hello"

    def test_over_delivery(self) -> None:
        assert _estimate_text_spoken([("hello", 100)], 200) == "hello"

    def test_partial_single_chunk(self) -> None:
        result = _estimate_text_spoken([("hello world", 100)], 50)
        assert len(result) > 0
        assert len(result) < len("hello world")

    def test_multiple_chunks_partial(self) -> None:
        chunks = [("First. ", 100), ("Second. ", 100), ("Third.", 100)]
        result = _estimate_text_spoken(chunks, 150)
        assert "First. " in result
        assert "Third" not in result

    def test_zero_audio_chunk_skipped(self) -> None:
        chunks = [("skipped", 0), ("hello", 100)]
        result = _estimate_text_spoken(chunks, 100)
        assert result == "hello"

    def test_all_zero_audio_chunks(self) -> None:
        chunks = [("a", 0), ("b", 0)]
        result = _estimate_text_spoken(chunks, 100)
        assert result == ""


class TestSentenceSplitting:
    """Tests for _split_at_sentence_boundaries edge cases."""

    def test_empty_string(self) -> None:
        ready, remaining = _split_at_sentence_boundaries("")
        assert ready == ""
        assert remaining == ""

    def test_single_sentence(self) -> None:
        ready, remaining = _split_at_sentence_boundaries("Hello world.")
        assert ready == ""
        assert remaining == "Hello world."

    def test_two_sentences(self) -> None:
        ready, remaining = _split_at_sentence_boundaries("First sentence. Second sentence.")
        assert "First sentence." in ready
        assert "Second" in remaining

    def test_no_punctuation(self) -> None:
        ready, remaining = _split_at_sentence_boundaries("Hello world")
        assert ready == ""
        assert remaining == "Hello world"

    def test_only_whitespace(self) -> None:
        ready, remaining = _split_at_sentence_boundaries("   ")
        assert ready == ""
        assert remaining == "   "


class TestMarkdownDelimiters:
    """Tests for _has_unclosed_markdown_delimiters edge cases."""

    def test_empty_string(self) -> None:
        assert not _has_unclosed_markdown_delimiters("")

    def test_no_markdown(self) -> None:
        assert not _has_unclosed_markdown_delimiters("Hello world")

    def test_unclosed_backtick(self) -> None:
        assert _has_unclosed_markdown_delimiters("Hello `world")

    def test_closed_backtick(self) -> None:
        assert not _has_unclosed_markdown_delimiters("Hello `world`")

    def test_unclosed_triple_backtick(self) -> None:
        assert _has_unclosed_markdown_delimiters("```python\nprint('hi')")

    def test_closed_triple_backtick(self) -> None:
        assert not _has_unclosed_markdown_delimiters("```python\nprint('hi')\n```")

    def test_unclosed_bold(self) -> None:
        assert _has_unclosed_markdown_delimiters("Hello **world")

    def test_closed_bold(self) -> None:
        assert not _has_unclosed_markdown_delimiters("Hello **world**")

    def test_unclosed_link(self) -> None:
        assert _has_unclosed_markdown_delimiters("Click [here")

    def test_closed_link(self) -> None:
        assert not _has_unclosed_markdown_delimiters("Click [here](http://example.com)")

    def test_unclosed_strikethrough(self) -> None:
        assert _has_unclosed_markdown_delimiters("Hello ~~world")

    def test_nested_backticks_in_fenced(self) -> None:
        text = "```\nHello `world`\n```"
        assert not _has_unclosed_markdown_delimiters(text)

    def test_unclosed_image(self) -> None:
        assert _has_unclosed_markdown_delimiters("![alt text")
