"""Tests for easycat.strip_markdown — detection and stripping utilities."""

from __future__ import annotations

import time

from easycat.strip_markdown import has_markdown, strip_markdown

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

    def test_malformed_link_fragments_do_not_rescan_repeatedly(self) -> None:
        payload = ("[x](" * 1000) + ")"

        start = time.perf_counter()
        result = has_markdown(payload)
        elapsed = time.perf_counter() - start

        assert result is False
        assert elapsed < 0.5


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

    def test_long_inline_code_not_tts_normalized(self) -> None:
        text = "Use `very_long_identifier_name_for_internal_config`."
        assert (
            strip_markdown(text, normalize_code_spans=True)
            == "Use very_long_identifier_name_for_internal_config."
        )

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

    def test_malformed_link_fragments_do_not_rescan_repeatedly(self) -> None:
        payload = ("[x](" * 1000) + ")"

        start = time.perf_counter()
        result = strip_markdown(payload)
        elapsed = time.perf_counter() - start

        assert result == payload
        assert elapsed < 0.5

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


# ── Agent history integration ──────────────────────────────────────


class TestAgentRunnerHistoryUpdate:
    """Test that AgentRunner.replace_last_assistant_text updates history."""

    def test_replace_updates_last_assistant_entry(self) -> None:
        from easycat.integrations.agents._agent_runner import AgentRunner

        class DummyAgent:
            async def run(self, text: str) -> str:
                return f"Echo: {text}"

        runner = AgentRunner(DummyAgent())
        # Simulate history from a completed turn
        runner._history.append({"role": "user", "content": "hello"})
        runner._history.append({"role": "assistant", "content": "**Hello!**"})

        runner.replace_last_assistant_text("Hello!")

        assert runner._history[-1]["content"] == "Hello!"
        # User message unchanged
        assert runner._history[0]["content"] == "hello"

    def test_replace_with_no_history_is_noop(self) -> None:
        from easycat.integrations.agents._agent_runner import AgentRunner

        class DummyAgent:
            async def run(self, text: str) -> str:
                return text

        runner = AgentRunner(DummyAgent())
        # Should not raise
        runner.replace_last_assistant_text("cleaned")
        assert runner._history == []
