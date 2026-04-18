"""AC2.4, AC2.11: BridgeAdapterShim event translation and dual-interface."""

from __future__ import annotations

import pytest

from easycat.integrations.agents._bridge_adapter_shim import (
    BridgeAdapterShim,
    _translate_bridge_to_stream,
)
from easycat.integrations.agents._legacy_types import AgentStreamEventType
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    CancellationMode,
    FrameworkStateSnapshot,
    UnitKind,
)


class TestEventTranslation:
    def test_text_delta(self):
        e = _translate_bridge_to_stream(AgentBridgeEvent(kind="text_delta", text="hi"))
        assert e is not None
        assert e.type == AgentStreamEventType.TEXT_DELTA
        assert e.text == "hi"

    def test_tool_started(self):
        e = _translate_bridge_to_stream(
            AgentBridgeEvent(kind="tool_started", tool_name="get_weather", call_id="c1")
        )
        assert e is not None
        assert e.type == AgentStreamEventType.TOOL_STARTED
        assert e.tool_name == "get_weather"

    def test_tool_result(self):
        e = _translate_bridge_to_stream(
            AgentBridgeEvent(kind="tool_result", call_id="c1", result="sunny")
        )
        assert e is not None
        assert e.type == AgentStreamEventType.TOOL_RESULT

    def test_done(self):
        e = _translate_bridge_to_stream(AgentBridgeEvent(kind="done", text="full text"))
        assert e is not None
        assert e.type == AgentStreamEventType.DONE

    def test_cursor_events_filtered(self):
        e = _translate_bridge_to_stream(AgentBridgeEvent(kind="cursor_entered"))
        assert e is None

    def test_handoff_events_filtered(self):
        e = _translate_bridge_to_stream(AgentBridgeEvent(kind="handoff"))
        assert e is None


class _StubBridge:
    """Minimal bridge for shim testing."""

    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CancellationMode.IMMEDIATE_STOP}

    def __init__(self, events: list[AgentBridgeEvent] | None = None):
        self._events = events or [
            AgentBridgeEvent(kind="text_delta", text="hello "),
            AgentBridgeEvent(kind="text_delta", text="world"),
            AgentBridgeEvent(kind="done", text="hello world"),
        ]
        self._reset_called = False
        self._apply_called = False
        self._replaced_text: str | None = None
        self._appended_note: str | None = None

    async def invoke(self, turn_input, recorder, cancel_token=None):
        for ev in self._events:
            yield ev

    def snapshot_state(self):
        return FrameworkStateSnapshot(fields={"test": True}, kind="stub")

    def apply_interruption(self, delivered_text, mode, recorder=None, caused_by_signal_id=None):
        self._apply_called = True

    def replace_last_assistant_text(self, text: str) -> None:
        self._replaced_text = text

    def append_interruption_note(self, note: str) -> None:
        self._appended_note = note

    def reset(self):
        self._reset_called = True


class TestBridgeAdapterShim:
    @pytest.mark.asyncio
    async def test_run_streaming_yields_stream_events(self):
        shim = BridgeAdapterShim(_StubBridge())
        events = []
        async for ev in shim.run_streaming("test"):
            events.append(ev)

        types = [e.type for e in events]
        assert AgentStreamEventType.TEXT_DELTA in types
        assert AgentStreamEventType.DONE in types

    @pytest.mark.asyncio
    async def test_run_returns_accumulated_text(self):
        shim = BridgeAdapterShim(_StubBridge())
        result = await shim.run("test")
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_history_maintained(self):
        shim = BridgeAdapterShim(_StubBridge())
        await shim.run("test")
        assert len(shim.message_history) == 2
        assert shim.message_history[0]["role"] == "user"
        assert shim.message_history[1]["role"] == "assistant"

    def test_clear_history_delegates_reset(self):
        bridge = _StubBridge()
        shim = BridgeAdapterShim(bridge)
        shim._message_history = [{"role": "user", "content": "hi"}]
        shim.clear_history()
        assert bridge._reset_called
        assert shim.message_history == []

    def test_notify_interruption_delegates(self):
        bridge = _StubBridge()
        shim = BridgeAdapterShim(bridge)
        shim._message_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello world"},
        ]
        shim.notify_interruption("hello", mode="truncate")
        assert bridge._apply_called

    def test_bridge_property(self):
        bridge = _StubBridge()
        shim = BridgeAdapterShim(bridge)
        assert shim.bridge is bridge

    def test_replace_last_assistant_text_delegates(self):
        """Shim must forward markdown-stripping writes to the bridge so
        bridges that keep their own message history stay in sync."""
        bridge = _StubBridge()
        shim = BridgeAdapterShim(bridge)
        shim._message_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "**hello**"},
        ]
        shim.replace_last_assistant_text("hello")
        assert shim._message_history[-1]["content"] == "hello"
        assert bridge._replaced_text == "hello"

    def test_append_interruption_note_delegates(self):
        """Shim must forward message-mode interruption notes to the
        bridge so the bridge's own history reflects the interruption."""
        bridge = _StubBridge()
        shim = BridgeAdapterShim(bridge)
        shim._append_interruption_note()
        assert shim._message_history[-1]["role"] == "system"
        assert bridge._appended_note is not None
        assert "interrupted" in bridge._appended_note.lower()

    def test_shim_tolerates_bridge_without_postprocessing_methods(self):
        """A minimal bridge missing the optional post-processing methods
        must still work — the shim should fall back to shadow-only updates."""

        class _BareBridge:
            COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CancellationMode.IMMEDIATE_STOP}

            async def invoke(self, turn_input, recorder, cancel_token=None):
                if False:
                    yield  # pragma: no cover

            def snapshot_state(self):
                return FrameworkStateSnapshot(fields={}, kind="bare")

            def apply_interruption(self, *args, **kwargs):
                pass

            def reset(self):
                pass

        shim = BridgeAdapterShim(_BareBridge())
        shim._message_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "raw"},
        ]
        shim.replace_last_assistant_text("clean")
        shim._append_interruption_note()
        assert shim._message_history[1]["content"] == "clean"
        assert shim._message_history[-1]["role"] == "system"
