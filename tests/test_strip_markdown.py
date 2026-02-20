"""Tests for easycat.strip_markdown — detection and stripping utilities."""

from __future__ import annotations

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

    def test_strikethrough(self) -> None:
        assert has_markdown("~~deleted~~")

    def test_snake_case_not_detected(self) -> None:
        """Underscores in snake_case identifiers should not trigger detection."""
        assert not has_markdown("The variable my_variable_name is defined")

    def test_empty_string(self) -> None:
        assert not has_markdown("")


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
            == "Visit Google for search"
        )

    def test_image_removed(self) -> None:
        assert strip_markdown("Look at this: ![photo](image.jpg)") == "Look at this:"

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
        assert result == "Welcome\n\nThis is bold and italic with a link."

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


# ── Agent history integration ──────────────────────────────────────


class TestAgentRunnerHistoryUpdate:
    """Test that AgentRunner.replace_last_assistant_text updates history."""

    def test_replace_updates_last_assistant_entry(self) -> None:
        from easycat.agent_runner import AgentRunner

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
        from easycat.agent_runner import AgentRunner

        class DummyAgent:
            async def run(self, text: str) -> str:
                return text

        runner = AgentRunner(DummyAgent())
        # Should not raise
        runner.replace_last_assistant_text("cleaned")
        assert runner._history == []


class TestBaseAdapterHistoryUpdate:
    """Test BaseAgentAdapter.replace_last_assistant_text (default no-op)."""

    def test_default_is_noop(self) -> None:
        from easycat.agents.base import BaseAgentAdapter

        adapter = BaseAgentAdapter()
        adapter._message_history = [{"role": "assistant", "content": "**hi**"}]
        # Default implementation does nothing
        adapter.replace_last_assistant_text("hi")
        assert adapter._message_history[0]["content"] == "**hi**"


class TestOpenAIAdapterHistoryUpdate:
    """Test OpenAIAgentsAdapter.replace_last_assistant_text."""

    def test_replaces_output_text_part(self) -> None:
        from easycat.agents.openai_agents import OpenAIAgentsAdapter

        class FakeAgent:
            pass

        adapter = OpenAIAgentsAdapter(FakeAgent())
        adapter._message_history = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "**Hello!**"}],
            },
        ]

        adapter.replace_last_assistant_text("Hello!")

        part = adapter._message_history[1]["content"][0]
        assert part["text"] == "Hello!"

    def test_preserves_output_text_part_granularity(self) -> None:
        from easycat.agents.openai_agents import OpenAIAgentsAdapter

        class FakeAgent:
            pass

        adapter = OpenAIAgentsAdapter(FakeAgent())
        adapter._message_history = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "**Hello** "},
                    {"type": "other_type", "value": 1},
                    {"type": "output_text", "text": "*world*"},
                ],
            },
        ]

        adapter.replace_last_assistant_text("Hello world")

        parts = adapter._message_history[1]["content"]
        assert parts[0]["text"] == "Hello "
        assert parts[2]["text"] == "world"
        assert parts[0]["text"] + parts[2]["text"] == "Hello world"

    def test_replaces_string_content(self) -> None:
        from easycat.agents.openai_agents import OpenAIAgentsAdapter

        class FakeAgent:
            pass

        adapter = OpenAIAgentsAdapter(FakeAgent())
        adapter._message_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "**Hello!**"},
        ]

        adapter.replace_last_assistant_text("Hello!")
        assert adapter._message_history[1]["content"] == "Hello!"

    def test_empty_history_is_noop(self) -> None:
        from easycat.agents.openai_agents import OpenAIAgentsAdapter

        class FakeAgent:
            pass

        adapter = OpenAIAgentsAdapter(FakeAgent())
        adapter.replace_last_assistant_text("Hello!")
        assert adapter._message_history == []


class TestPydanticAIAdapterHistoryUpdate:
    """Test PydanticAIAdapter.replace_last_assistant_text."""

    def test_replaces_text_part_content(self) -> None:
        from easycat.agents.pydantic_ai import PydanticAIAdapter

        # Simulate PydanticAI ModelResponse / TextPart without importing them
        class TextPart:
            def __init__(self, content: str) -> None:
                self.content = content

        class ModelResponse:
            def __init__(self, parts: list) -> None:
                self.parts = parts

        class FakeAgent:
            pass

        adapter = PydanticAIAdapter(FakeAgent())
        text_part = TextPart("**Hello!**")
        adapter._message_history = [ModelResponse(parts=[text_part])]

        adapter.replace_last_assistant_text("Hello!")
        assert text_part.content == "Hello!"

    def test_preserves_text_part_granularity(self) -> None:
        from easycat.agents.pydantic_ai import PydanticAIAdapter

        class TextPart:
            def __init__(self, content: str) -> None:
                self.content = content

        class ToolPart:
            pass

        class ModelResponse:
            def __init__(self, parts: list) -> None:
                self.parts = parts

        class FakeAgent:
            pass

        adapter = PydanticAIAdapter(FakeAgent())
        first = TextPart("**Hello** ")
        second = TextPart("*world*")
        adapter._message_history = [ModelResponse(parts=[first, ToolPart(), second])]

        adapter.replace_last_assistant_text("Hello world")
        assert first.content == "Hello "
        assert second.content == "world"
        assert first.content + second.content == "Hello world"

    def test_empty_history_is_noop(self) -> None:
        from easycat.agents.pydantic_ai import PydanticAIAdapter

        class FakeAgent:
            pass

        adapter = PydanticAIAdapter(FakeAgent())
        adapter.replace_last_assistant_text("Hello!")
        assert adapter._message_history == []
