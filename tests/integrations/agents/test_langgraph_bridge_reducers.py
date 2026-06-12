"""LangGraph bridge reducer and formatted-message tests."""

from __future__ import annotations

from ._langgraph_bridge_support import (
    AgentTurnInput,
    LangGraphBridge,
    LastValue,
    _content,
    _FormattedAddMessagesChannel,
    _GenericReducerChannel,
    _id_of,
    _MockCompiledGraph,
    _MockMessage,
    _node_end,
    _node_start,
    _recorder,
    _ReducerChannel,
    pytest,
)


class TestLangGraphBridgeReducerGuard:
    """``RemoveMessage`` purges and id-keyed rewrites only *merge* into
    an ``add_messages`` reducer channel.  On a plain ``LastValue``
    channel ``update_state`` *replaces* the whole list, so the
    transient-context purge there would wipe the checkpointed
    conversation — the bridge must skip the machinery."""

    def test_messages_key_add_messages_detection(self):
        graph = _MockCompiledGraph()
        bridge = LangGraphBridge(graph)
        # No introspectable channels → assume add_messages (preserve
        # behaviour).
        assert bridge._messages_key_uses_add_messages() is True

        graph.channels = {"messages": _ReducerChannel()}
        assert bridge._messages_key_uses_add_messages() is True

        # A generic (non-add_messages) reducer only appends, so the
        # RemoveMessage / id-keyed-replace machinery must stay off.
        graph.channels = {"messages": _GenericReducerChannel()}
        assert bridge._messages_key_uses_add_messages() is False

        graph.channels = {"messages": LastValue()}
        assert bridge._messages_key_uses_add_messages() is False

    @pytest.mark.asyncio
    async def test_plain_list_channel_skips_destructive_purge(self):
        graph = _MockCompiledGraph([_node_start("p", "n1"), _node_end("p", "n1")])
        graph.channels = {"messages": LastValue()}
        graph._state.values["messages"] = [_MockMessage("assistant", "kept")]
        bridge = LangGraphBridge(graph)
        turn = AgentTurnInput.from_text(
            "hi", context=[{"role": "system", "content": "Caller id: +1"}]
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass

        # No RemoveMessage update_state was issued (it would have
        # replaced — wiped — the whole messages list).
        assert graph.update_state_calls == []
        assert [_content(m) for m in graph._state.values["messages"]] == ["kept"]
        # Context was still forwarded for the turn, just untracked.
        assert bridge._transient_context_ids == []

    @pytest.mark.asyncio
    async def test_reducer_channel_still_purges(self):
        graph = _MockCompiledGraph([_node_start("p", "n1"), _node_end("p", "n1")])
        graph.channels = {"messages": _ReducerChannel()}
        bridge = LangGraphBridge(graph)
        turn = AgentTurnInput.from_text(
            "hi", context=[{"role": "system", "content": "Caller id: +1"}]
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass

        assert graph.update_state_calls
        _cfg, values = graph.update_state_calls[-1]
        # The forwarded context carried a tracked id; the purge issued a
        # removal marker for it.
        assert values["messages"]
        assert all(_id_of(m) for m in values["messages"])

    @pytest.mark.asyncio
    async def test_generic_reducer_channel_skips_destructive_purge(self):
        # ``Annotated[list, operator.add]`` only appends, so a
        # ``RemoveMessage`` marker would be appended as a fresh tail
        # (polluting checkpointed history / emptying ``done.text``)
        # rather than removing the injected context — the bridge must
        # treat it like a no-reducer channel and skip the purge.
        graph = _MockCompiledGraph([_node_start("p", "n1"), _node_end("p", "n1")])
        graph.channels = {"messages": _GenericReducerChannel()}
        graph._state.values["messages"] = [_MockMessage("assistant", "kept")]
        bridge = LangGraphBridge(graph)
        turn = AgentTurnInput.from_text(
            "hi", context=[{"role": "system", "content": "Caller id: +1"}]
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass

        assert graph.update_state_calls == []
        assert [_content(m) for m in graph._state.values["messages"]] == ["kept"]
        assert bridge._transient_context_ids == []


class TestLangGraphBridgeFormattedAddMessages:
    """``add_messages(format=...)`` is the documented way to request a
    message format; it compiles to a ``functools.partial`` the bridge
    must still recognise as ``add_messages`` so the transient-context
    purge and interruption/markdown rewrites stay enabled."""

    def test_partial_add_messages_is_recognised(self):
        graph = _MockCompiledGraph()
        bridge = LangGraphBridge(graph)
        graph.channels = {"messages": _FormattedAddMessagesChannel()}
        assert bridge._messages_key_uses_add_messages() is True
