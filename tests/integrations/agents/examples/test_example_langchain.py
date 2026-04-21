"""Example L1: LangChainBridge wrapping an LCEL chain.

Mirrors plan appendix Example L1 — a LangChain ``Runnable`` composed
with LCEL, wrapped via :class:`LangChainBridge`.  Uses duck-typed mocks
so the real ``langchain-core`` SDK is not required at test time.

This fixture runs end-to-end using the mock Runnable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.runtime.journal import InMemoryRingBuffer


class _MockAIMessageChunk:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.tool_call_chunks: list[Any] = []


class _MockLCELChain:
    """Duck-types a LangChain ``Runnable``.

    Emits a representative sequence for an LCEL chain: prompt start →
    chat model start → streaming tokens → chat model end → chain end.
    """

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens

    async def astream_events(self, input: Any, *, version: str) -> AsyncIterator[dict[str, Any]]:
        yield {
            "event": "on_chain_start",
            "name": "RunnableSequence",
            "run_id": "c",
            "parent_ids": [],
            "data": {},
        }
        yield {
            "event": "on_prompt_start",
            "name": "ChatPromptTemplate",
            "run_id": "p",
            "parent_ids": ["c"],
            "data": {},
        }
        yield {
            "event": "on_prompt_end",
            "name": "ChatPromptTemplate",
            "run_id": "p",
            "parent_ids": ["c"],
            "data": {},
        }
        yield {
            "event": "on_chat_model_start",
            "name": "ChatOpenAI",
            "run_id": "m",
            "parent_ids": ["c"],
            "data": {},
        }
        for tok in self._tokens:
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "run_id": "m",
                "parent_ids": ["c"],
                "data": {"chunk": _MockAIMessageChunk(content=tok)},
            }
        yield {
            "event": "on_chat_model_end",
            "name": "ChatOpenAI",
            "run_id": "m",
            "parent_ids": ["c"],
            "data": {"output": _MockAIMessageChunk(content="".join(self._tokens))},
        }
        yield {
            "event": "on_chain_end",
            "name": "RunnableSequence",
            "run_id": "c",
            "parent_ids": [],
            "data": {"output": "".join(self._tokens)},
        }


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestLangChainExample:
    @pytest.mark.asyncio
    async def test_invoke_streams_and_captures_output(self):
        chain = _MockLCELChain(tokens=["Hello ", "voice ", "world."])
        bridge = LangChainBridge(chain, display_name="LCELChain")

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("say hi"), rec):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert text == "Hello voice world."
        assert done[0].structured_output == "Hello voice world."

        records = journal.read()
        names = [r.name for r in records]
        # Outer agent cursor plus prompt + chat + chain nested cursors.
        assert names.count("unit_entered") == names.count("unit_exited")
        assert names.count("unit_entered") >= 4

    def test_committable_boundaries_published(self):
        assert LangChainBridge.COMMITTABLE_BOUNDARIES
