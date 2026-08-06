"""LlamaAgents cancellation helper tests."""

from __future__ import annotations

from ._llama_agents_bridge_support import (
    Any,
    AsyncIterator,
    CancelToken,
    _TrackingSource,
    asyncio,
    contextlib,
    pytest,
)


@pytest.mark.asyncio
async def test_bounded_cancel_survivor_remains_scope_owned(monkeypatch):
    from easycat.integrations.agents import llama_agents as llama_module
    from easycat.integrations.agents.llama_agents import LlamaAgentsBridge

    monkeypatch.setattr(llama_module, "_POST_CANCEL_AWAIT_TIMEOUT", 0.01)
    bridge = LlamaAgentsBridge(workflow=object(), display_name="test")
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()

    await bridge._best_effort_cancel(
        cleanup(),
        task_name="easycat-llama-test-cancel",
    )

    assert started.is_set()
    owned = bridge._cancel_cleanup_task_scope.tasks()
    assert len(owned) == 1
    assert owned[0].get_name() == "easycat-llama-test-cancel"

    release.set()
    await asyncio.gather(*owned)
    await asyncio.sleep(0)
    assert bridge._cancel_cleanup_task_scope.tasks() == ()


class TestAiterWithCancellation:
    """Regression coverage for the event-stream leak: a consumer that
    closes the wrapper mid-stream (the text-session interruption path
    aclose()s invoke(), raising GeneratorExit) must propagate that close
    into the wrapped ``stream_events()`` iterator so its cleanup runs."""

    @pytest.mark.asyncio
    async def test_generator_close_closes_source_with_cancel_token(self):
        from easycat.integrations.agents.llama_agents import _aiter_with_cancellation

        source = _TrackingSource()
        token = CancelToken()  # present but never fired -- the reviewed path
        agen = _aiter_with_cancellation(source, token)

        assert await agen.__anext__() == "first"
        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert source.closed is True
        assert source.exhausted is False

    @pytest.mark.asyncio
    async def test_generator_close_closes_source_without_cancel_token(self):
        from easycat.integrations.agents.llama_agents import _aiter_with_cancellation

        source = _TrackingSource()
        agen = _aiter_with_cancellation(source, None)

        assert await agen.__anext__() == "first"
        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert source.closed is True
        assert source.exhausted is False

    @pytest.mark.asyncio
    async def test_cancel_token_win_still_closes_source(self):
        from easycat.integrations.agents.llama_agents import _aiter_with_cancellation

        source = _TrackingSource()
        token = CancelToken()
        agen = _aiter_with_cancellation(source, token)

        assert await agen.__anext__() == "first"
        token.cancel()
        with contextlib.suppress(StopAsyncIteration):
            await asyncio.wait_for(agen.__anext__(), timeout=2.0)
        await agen.aclose()

        assert source.closed is True
        assert source.exhausted is False

    @pytest.mark.asyncio
    async def test_full_drain_yields_all_items(self):
        from easycat.integrations.agents.llama_agents import _aiter_with_cancellation

        async def _source() -> AsyncIterator[Any]:
            yield "a"
            yield "b"

        token = CancelToken()
        collected = [item async for item in _aiter_with_cancellation(_source(), token)]

        assert collected == ["a", "b"]
