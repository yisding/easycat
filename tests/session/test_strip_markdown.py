"""Session-adjacent AgentRunner history assertions split from markdown utilities."""

from __future__ import annotations


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
