"""Session-adjacent AgentRunner history assertions split from markdown utilities."""

from __future__ import annotations

import pytest


class TestAgentRunnerHistoryUpdate:
    """Test that AgentRunner.replace_last_assistant_text updates history."""

    @pytest.mark.asyncio
    async def test_replace_updates_last_assistant_entry(self) -> None:
        """A completed turn's assistant entry is rewritten with the stripped text.

        The turn is driven through ``invoke()`` rather than by appending to
        ``_history`` directly: the rewrite is scoped to the turn currently in
        flight, so hand-built history would not be recognized as one (gh 1100).
        Production only reaches here on ``stream_succeeded``, i.e. after the
        turn has been mirrored.
        """
        from easycat.integrations.agents._agent_runner import AgentRunner
        from easycat.integrations.agents.base import AgentTurnInput, NullAgentRecorder

        class DummyAgent:
            async def run(self, text: str) -> str:
                return "**Hello!**"

        runner = AgentRunner(DummyAgent())
        async for _event in runner.invoke(AgentTurnInput.from_text("hello"), NullAgentRecorder()):
            pass

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
