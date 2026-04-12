"""WS2C: RemoteResponsesAPIBridge acceptance criteria tests.

Covers:
- AC2C.2:  Bridge implements ExternalAgentBridge protocol
- AC2C.3:  Turn execution produces correct journal records
- AC2C.4:  N-1 chain interruption
- AC2C.5:  drain_current_unit on SSE stream
- AC2C.6:  Capability discovery via metadata
- AC2C.7:  Graceful degradation on server error
- AC2C.8:  EasyCatConfig URL detection
- AC2C.9:  API key not in journal
- AC2C.10: COMMITTABLE_BOUNDARIES correct
- AC2C.11: No WebSocket protocol
- AC2C.12: All tests pass
- AC2C.13: Integration test (gated)
"""

from __future__ import annotations

import ast
import inspect
import json
import os
from typing import Any

import httpx
import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents._responses_api_events import (
    parse_sse_line,
    translate_sse_event,
)
from easycat.integrations.agents.base import (
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExternalAgentBridge,
    InterruptionPlan,
    MutationInjectedError,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge
from easycat.runtime.journal import InMemoryRingBuffer

from .mock_responses_server import MockResponsesServer

# ── Helpers ─────────────────────────────────────────────────────


def _recorder(journal=None):
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


def _make_bridge(
    mock_server: MockResponsesServer,
    *,
    model: str = "test-model",
    api_key: str = "test-key",
    metadata: dict[str, Any] | None = None,
) -> RemoteResponsesAPIBridge:
    """Create a RemoteResponsesAPIBridge wired to the mock ASGI server."""
    transport = httpx.ASGITransport(app=mock_server)
    bridge = RemoteResponsesAPIBridge(
        base_url="http://testserver",
        model=model,
        api_key=api_key,
        metadata=metadata,
    )
    # Replace the internal client with one using the ASGI transport.
    bridge._client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return bridge


# ── AC2C.2: Protocol conformance ────────────────────────────────


class TestProtocolConformance:
    """AC2C.2 -- RemoteResponsesAPIBridge implements ExternalAgentBridge."""

    def test_is_runtime_checkable_bridge(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        assert isinstance(bridge, ExternalAgentBridge)

    def test_has_committable_boundaries(self):
        assert hasattr(RemoteResponsesAPIBridge, "COMMITTABLE_BOUNDARIES")

    def test_has_invoke(self):
        assert hasattr(RemoteResponsesAPIBridge, "invoke")

    def test_has_snapshot_state(self):
        assert hasattr(RemoteResponsesAPIBridge, "snapshot_state")

    def test_has_apply_interruption(self):
        assert hasattr(RemoteResponsesAPIBridge, "apply_interruption")

    def test_has_reset(self):
        assert hasattr(RemoteResponsesAPIBridge, "reset")


# ── AC2C.3: Turn execution journal records ──────────────────────


class TestTurnExecutionJournal:
    """AC2C.3 -- invoke() produces correct journal records."""

    @pytest.mark.asyncio
    async def test_basic_turn_produces_enter_exit_records(self):
        server = MockResponsesServer()
        server.response_text = "Test response"
        bridge = _make_bridge(server)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            events.append(ev)

        # Should have cursor_entered, text deltas, and done.
        kinds = [e.kind for e in events]
        assert "cursor_entered" in kinds
        assert "text_delta" in kinds
        assert "done" in kinds

        # Journal should have unit_entered and unit_exited.
        records = journal.read()
        names = [r.name for r in records]
        assert "unit_entered" in names
        assert "unit_exited" in names

    @pytest.mark.asyncio
    async def test_accumulated_text_in_done_event(self):
        server = MockResponsesServer()
        server.response_text = "Hello world"
        bridge = _make_bridge(server)
        rec = _recorder()

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        done_events = [e for e in events if e.kind == "done"]
        assert len(done_events) == 1
        assert done_events[0].text == "Hello world"

    @pytest.mark.asyncio
    async def test_tool_calls_produce_journal_records(self):
        server = MockResponsesServer()
        server.tool_calls = [("get_weather", '{"city":"SF"}', '{"temp":72}')]
        server.response_text = "The weather is nice."
        bridge = _make_bridge(server)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("weather?"), rec):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert "tool_started" in kinds
        assert "tool_result" in kinds

        records = journal.read()
        tool_records = [r for r in records if r.name == "tool_phase_changed"]
        assert len(tool_records) >= 2  # start and result


# ── AC2C.4: N-1 chain interruption ──────────────────────────────


class TestN1ChainInterruption:
    """AC2C.4 -- N-1 chain interruption with response_id chaining."""

    @pytest.mark.asyncio
    async def test_response_id_updated_on_completion(self):
        server = MockResponsesServer()
        server.response_text = "First response"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("turn 1"), rec):
            pass

        assert bridge._last_completed_response_id is not None
        assert bridge._response_count == 1

    @pytest.mark.asyncio
    async def test_previous_response_id_sent_on_second_turn(self):
        server = MockResponsesServer()
        server.response_text = "First"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("turn 1"), rec):
            pass

        first_id = bridge._last_completed_response_id

        server.response_text = "Second"
        async for _ in bridge.invoke(AgentTurnInput.from_text("turn 2"), rec):
            pass

        # Second request should have included previous_response_id.
        assert len(server.received_requests) == 2
        second_req = server.received_requests[1]
        assert second_req.get("previous_response_id") == first_id

    @pytest.mark.asyncio
    async def test_interruption_does_not_update_response_id(self):
        server = MockResponsesServer()
        server.response_text = "Complete response"
        bridge = _make_bridge(server)
        rec = _recorder()

        # Complete first turn.
        async for _ in bridge.invoke(AgentTurnInput.from_text("turn 1"), rec):
            pass

        first_id = bridge._last_completed_response_id
        assert first_id is not None

        # Complete second turn.
        server.response_text = "Second response that will be interrupted"
        async for _ in bridge.invoke(AgentTurnInput.from_text("turn 2"), rec):
            pass

        second_id = bridge._last_completed_response_id

        # Apply interruption -- should NOT change response_id.
        bridge.apply_interruption("Second resp", CancellationMode.IMMEDIATE_STOP)

        assert bridge._last_completed_response_id == second_id

    @pytest.mark.asyncio
    async def test_interruption_stashes_replay_items(self):
        server = MockResponsesServer()
        server.response_text = "Full response text"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            pass

        bridge.apply_interruption("Full resp", CancellationMode.IMMEDIATE_STOP)

        assert bridge._replay_items is not None
        # Should have truncated assistant text.
        assistant_items = [
            item for item in bridge._replay_items if item.get("role") == "assistant"
        ]
        assert len(assistant_items) == 1
        assert assistant_items[0]["content"] == "Full resp..."

    @pytest.mark.asyncio
    async def test_replay_items_sent_on_next_invoke(self):
        server = MockResponsesServer()
        server.response_text = "Full response text"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            pass

        bridge.apply_interruption("Full resp", CancellationMode.IMMEDIATE_STOP)

        # Next invoke should include replay items.
        server.response_text = "Next response"
        async for _ in bridge.invoke(AgentTurnInput.from_text("continue"), rec):
            pass

        last_req = server.received_requests[-1]
        input_items = last_req["input"]

        # Should have: assistant truncation, developer note, user msg.
        roles = [item.get("role", item.get("type", "")) for item in input_items]
        assert "assistant" in roles
        assert "developer" in roles
        assert "user" in roles

    @pytest.mark.asyncio
    async def test_interruption_with_tool_calls_preserved(self):
        server = MockResponsesServer()
        server.tool_calls = [("search", '{"q":"test"}', '{"results":["a"]}')]
        server.response_text = "Found results"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("search"), rec):
            pass

        bridge.apply_interruption("Found", CancellationMode.IMMEDIATE_STOP)

        assert bridge._replay_items is not None
        # Should have function_call and function_call_output items.
        types = [item.get("type", "") for item in bridge._replay_items]
        assert "function_call" in types
        assert "function_call_output" in types


# ── AC2C.5: drain_current_unit on SSE stream ────────────────────


class TestDrainCurrentUnit:
    """AC2C.5 -- cancel_token with drain_current_unit behavior."""

    @pytest.mark.asyncio
    async def test_cancel_stops_stream(self):
        server = MockResponsesServer()
        server.response_text = "A long response with many words"
        bridge = _make_bridge(server)
        rec = _recorder()

        from easycat.cancel import CancelToken

        token = CancelToken()

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec, cancel_token=token):
            events.append(ev)
            if ev.kind == "text_delta":
                # Cancel after first text delta.
                token.cancel()

        # Should have some events but may not have all.
        kinds = [e.kind for e in events]
        assert "cursor_entered" in kinds
        assert "done" in kinds


# ── AC2C.6: Capability discovery via metadata ───────────────────


class TestCapabilityDiscovery:
    """AC2C.6 -- easycat.* metadata in responses."""

    @pytest.mark.asyncio
    async def test_metadata_sent_in_request(self):
        server = MockResponsesServer()
        server.response_text = "Ok"
        bridge = _make_bridge(server, metadata={"easycat.version": "1.0"})
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        req = server.received_requests[0]
        assert req.get("metadata", {}).get("easycat.version") == "1.0"

    @pytest.mark.asyncio
    async def test_server_metadata_in_response(self):
        server = MockResponsesServer()
        server.response_text = "Ok"
        server.easycat_metadata = {"easycat.capabilities": "streaming,tools"}
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        # The mock server returns metadata in the response object --
        # verify the bridge doesn't crash on it.
        assert bridge._response_count == 1


# ── AC2C.7: Graceful degradation ────────────────────────────────


class TestGracefulDegradation:
    """AC2C.7 -- bridge handles server errors gracefully."""

    @pytest.mark.asyncio
    async def test_server_failure_event_raises(self):
        server = MockResponsesServer()
        server.fail_on_next = "Internal server error"
        bridge = _make_bridge(server)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        with pytest.raises(RuntimeError, match="Responses API failed"):
            async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
                pass

        # Journal should have a framework error.
        records = journal.read()
        error_records = [r for r in records if r.name == "framework_error"]
        assert len(error_records) >= 1

    @pytest.mark.asyncio
    async def test_http_error_raises_and_records(self):
        server = MockResponsesServer()
        server.status_code_override = 500
        bridge = _make_bridge(server)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        with pytest.raises(httpx.HTTPStatusError):
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
                pass

        records = journal.read()
        error_records = [r for r in records if r.name == "framework_error"]
        assert len(error_records) >= 1


# ── AC2C.8: EasyCatConfig URL detection ─────────────────────────


class TestURLDetection:
    """AC2C.8 -- auto_adapt_agent detects HTTP URLs."""

    def test_http_url_detected(self):
        from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
        from easycat.integrations.agents._factory import auto_adapt_agent

        adapted = auto_adapt_agent("https://api.example.com/v1", model="gpt-4o")
        assert isinstance(adapted, BridgeAdapterShim)
        assert isinstance(adapted.bridge, RemoteResponsesAPIBridge)

    def test_http_url_without_model_raises(self):
        from easycat.integrations.agents._factory import auto_adapt_agent
        from easycat.integrations.agents.base import BridgeInputError

        with pytest.raises(BridgeInputError, match="requires model="):
            auto_adapt_agent("https://api.example.com/v1")

    def test_non_url_string_passthrough(self):
        from easycat.integrations.agents._factory import auto_adapt_agent

        # A plain string (not a URL) should pass through unchanged.
        result = auto_adapt_agent("just-a-string")
        assert result == "just-a-string"

    def test_http_url_with_path(self):
        from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
        from easycat.integrations.agents._factory import auto_adapt_agent

        adapted = auto_adapt_agent("http://localhost:8080", model="gpt-4o")
        assert isinstance(adapted, BridgeAdapterShim)
        assert isinstance(adapted.bridge, RemoteResponsesAPIBridge)


class TestEasyCatConfigURLValidation:
    """AC2C.8 -- EasyCatConfig validates agent_model when agent is URL."""

    def test_url_agent_without_model_raises(self):
        try:
            from easycat.config import EasyCatConfig, EasyCatConfigError
            from easycat.stt.openai_provider import OpenAISTTConfig
            from easycat.tts.openai_tts import OpenAITTSConfig

            with pytest.raises(EasyCatConfigError, match="agent_model"):
                EasyCatConfig(
                    stt=OpenAISTTConfig(api_key="test"),
                    tts=OpenAITTSConfig(api_key="test"),
                    agent="https://api.example.com",
                    agent_model=None,
                )
        except ImportError:
            pytest.skip("config dependencies not importable")

    def test_url_agent_with_model_accepted(self):
        try:
            from easycat.config import EasyCatConfig
            from easycat.stt.openai_provider import OpenAISTTConfig
            from easycat.tts.openai_tts import OpenAITTSConfig

            config = EasyCatConfig(
                stt=OpenAISTTConfig(api_key="test"),
                tts=OpenAITTSConfig(api_key="test"),
                agent="https://api.example.com",
                agent_model="gpt-4o",
            )
            assert config.agent_model == "gpt-4o"
        except ImportError:
            pytest.skip("config dependencies not importable")


# ── AC2C.9: API key not in journal ───────────────────────────────


class TestAPIKeyNotInJournal:
    """AC2C.9 -- API key must not appear in journal records or snapshots."""

    def test_snapshot_excludes_api_key(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server, api_key="sk-secret-12345")
        snap = bridge.snapshot_state()

        serialized = json.dumps(snap.fields)
        assert "sk-secret" not in serialized
        assert "api_key" not in serialized.lower()

    @pytest.mark.asyncio
    async def test_journal_records_exclude_api_key(self):
        server = MockResponsesServer()
        server.response_text = "Safe response"
        bridge = _make_bridge(server, api_key="sk-secret-12345")
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        records = journal.read()
        for record in records:
            data_str = json.dumps(record.data) if record.data else ""
            assert "sk-secret" not in data_str
            assert "api_key" not in data_str.lower() or "base_url" in data_str


# ── AC2C.10: COMMITTABLE_BOUNDARIES correct ─────────────────────


class TestCommittableBoundaries:
    """AC2C.10 -- COMMITTABLE_BOUNDARIES mapping is correct."""

    def test_mapping_present(self):
        assert hasattr(RemoteResponsesAPIBridge, "COMMITTABLE_BOUNDARIES")

    def test_mapping_non_empty(self):
        assert len(RemoteResponsesAPIBridge.COMMITTABLE_BOUNDARIES) > 0

    def test_agent_is_between_turns(self):
        boundaries = RemoteResponsesAPIBridge.COMMITTABLE_BOUNDARIES
        assert boundaries[UnitKind.AGENT] == CommitRule.BETWEEN_TURNS

    def test_values_are_commit_rules(self):
        for rule in RemoteResponsesAPIBridge.COMMITTABLE_BOUNDARIES.values():
            assert isinstance(rule, CommitRule)

    def test_keys_are_unit_kinds(self):
        for kind in RemoteResponsesAPIBridge.COMMITTABLE_BOUNDARIES:
            assert isinstance(kind, UnitKind)


# ── AC2C.11: No WebSocket protocol ──────────────────────────────


class TestNoWebSocketProtocol:
    """AC2C.11 -- bridge uses HTTP+SSE, not WebSocket."""

    def test_no_websocket_imports(self):
        import easycat.integrations.agents.responses_api as mod

        source = inspect.getsource(mod)
        assert "websocket" not in source.lower()
        assert "ws://" not in source
        assert "wss://" not in source

    def test_uses_httpx_stream(self):
        import easycat.integrations.agents.responses_api as mod

        source = inspect.getsource(mod)
        assert "self._client.stream" in source


# ── Four-step atomic ordering ────────────────────────────────────


class TestApplyInterruptionFourStep:
    """Four-step atomic write ordering for RemoteResponsesAPIBridge."""

    def test_four_step_method_calls_present(self):
        assert hasattr(RemoteResponsesAPIBridge, "_plan_interruption")
        assert hasattr(RemoteResponsesAPIBridge, "_apply_planned_mutation")
        assert hasattr(RemoteResponsesAPIBridge, "apply_interruption")

        import textwrap

        source = textwrap.dedent(inspect.getsource(RemoteResponsesAPIBridge.apply_interruption))
        tree = ast.parse(source)

        call_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)

        assert "_plan_interruption" in call_names
        assert "_apply_planned_mutation" in call_names

    def test_record_state_committed_before_apply(self):
        import textwrap

        source = textwrap.dedent(inspect.getsource(RemoteResponsesAPIBridge.apply_interruption))
        tree = ast.parse(source)

        calls_with_line: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls_with_line.append((node.lineno, node.func.attr))
        calls_with_line.sort(key=lambda x: x[0])
        call_order = [name for _, name in calls_with_line]

        if "record_state_committed" in call_order and "_apply_planned_mutation" in call_order:
            committed_idx = call_order.index("record_state_committed")
            apply_idx = call_order.index("_apply_planned_mutation")
            assert committed_idx < apply_idx

    def test_no_direct_mutation_outside_apply_planned(self):
        import textwrap

        source = textwrap.dedent(inspect.getsource(RemoteResponsesAPIBridge.apply_interruption))
        assert "_replay_items" not in source
        assert "_last_completed_response_id" not in source


class TestInterruptionApplyFailed:
    """Mutation failure emits InterruptionApplyFailed."""

    def test_mutation_failure_writes_paired_records(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        def _raise(_plan):
            raise MutationInjectedError("injected")

        bridge._apply_planned_mutation = _raise

        with pytest.raises(MutationInjectedError):
            bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP, recorder=rec)

        records = journal.read()
        names = [r.name for r in records]
        assert "state_committed" in names
        assert "interruption_apply_failed" in names

    def test_commit_write_failure_skips_mutation(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)

        original_apply = bridge._apply_planned_mutation
        apply_called = []

        def _tracking_apply(plan):
            apply_called.append(True)
            return original_apply(plan)

        bridge._apply_planned_mutation = _tracking_apply

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        def _raise_on_commit(*args, **kwargs):
            raise RuntimeError("journal degraded")

        rec.record_state_committed = _raise_on_commit

        bridge.apply_interruption("heard", CancellationMode.IMMEDIATE_STOP, recorder=rec)

        assert len(apply_called) == 0


class TestCancellationModeMatrix:
    """All three cancellation modes work on RemoteResponsesAPIBridge."""

    @pytest.mark.parametrize(
        "mode",
        [
            CancellationMode.IMMEDIATE_STOP,
            CancellationMode.DRAIN_CURRENT_UNIT,
            CancellationMode.DRAIN_TO_COMMIT_POINT,
        ],
    )
    def test_mode_produces_correct_journal_records(self, mode):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        # Seed accumulated items so interruption has something to act on.
        bridge._last_accumulated_items = []
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        bridge.apply_interruption(
            "heard text",
            mode,
            recorder=rec,
            caused_by_signal_id="sig-42",
        )

        records = journal.read()
        names = [r.name for r in records]

        assert "state_committed" in names
        assert "cancellation_boundary" in names

        committed_idx = names.index("state_committed")
        boundary_idx = names.index("cancellation_boundary")
        assert committed_idx < boundary_idx

        assert "interruption_apply_failed" not in names

        boundary_rec = records[boundary_idx]
        assert boundary_rec.data["caused_by_signal_id"] == "sig-42"
        assert boundary_rec.data["cancellation_mode"] == mode.value


class TestBackwardCompatibility:
    """apply_interruption works without recorder (legacy path)."""

    def test_no_recorder(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        bridge._last_accumulated_items = []
        # Should not raise.
        bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP)


# ── SSE parser unit tests ───────────────────────────────────────


class TestSSEParser:
    """Unit tests for parse_sse_line()."""

    def test_valid_data_line(self):
        result = parse_sse_line('data: {"type": "response.output_text.delta", "delta": "hi"}')
        assert result is not None
        event_type, data = result
        assert event_type == "response.output_text.delta"
        assert data["delta"] == "hi"

    def test_blank_line_returns_none(self):
        assert parse_sse_line("") is None
        assert parse_sse_line("  ") is None

    def test_comment_line_returns_none(self):
        assert parse_sse_line(": keep-alive") is None

    def test_event_line_returns_none(self):
        assert parse_sse_line("event: message") is None

    def test_invalid_json_returns_none(self):
        assert parse_sse_line("data: not json") is None

    def test_missing_type_returns_none(self):
        assert parse_sse_line('data: {"delta": "hi"}') is None


class TestSSETranslator:
    """Unit tests for translate_sse_event()."""

    def test_text_delta(self):
        rec = _recorder()
        ev = translate_sse_event(
            "response.output_text.delta",
            {"delta": "hello"},
            rec,
        )
        assert ev is not None
        assert ev.kind == "text_delta"
        assert ev.text == "hello"

    def test_tool_delta(self):
        rec = _recorder()
        ev = translate_sse_event(
            "response.function_call_arguments.delta",
            {"delta": '{"x":', "call_id": "c1"},
            rec,
        )
        assert ev is not None
        assert ev.kind == "tool_delta"
        assert ev.call_id == "c1"

    def test_function_call_done(self):
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        ev = translate_sse_event(
            "response.output_item.done",
            {
                "item": {
                    "type": "function_call",
                    "name": "get_weather",
                    "call_id": "c1",
                }
            },
            rec,
        )
        assert ev is not None
        assert ev.kind == "tool_started"
        assert ev.tool_name == "get_weather"

        records = journal.read()
        tool_records = [r for r in records if r.name == "tool_phase_changed"]
        assert len(tool_records) == 1
        assert tool_records[0].data["phase"] == "start"

    def test_function_call_output_done(self):
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        ev = translate_sse_event(
            "response.output_item.done",
            {
                "item": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "result",
                }
            },
            rec,
        )
        assert ev is not None
        assert ev.kind == "tool_result"
        assert ev.result == "result"

    def test_response_completed_returns_none(self):
        rec = _recorder()
        ev = translate_sse_event("response.completed", {}, rec)
        assert ev is None

    def test_response_failed_returns_none(self):
        rec = _recorder()
        ev = translate_sse_event("response.failed", {}, rec)
        assert ev is None

    def test_unknown_event_returns_none(self):
        rec = _recorder()
        ev = translate_sse_event("response.some_unknown_event", {}, rec)
        assert ev is None


# ── Snapshot tests ──────────────────────────────────────────────


class TestSnapshotState:
    """snapshot_state() returns correct FrameworkStateSnapshot."""

    def test_snapshot_kind(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        snap = bridge.snapshot_state()
        assert snap.kind == "remote_responses_api"

    def test_snapshot_fields(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        snap = bridge.snapshot_state()
        assert "response_count" in snap.fields
        assert "last_completed_response_id" in snap.fields
        assert "base_url_host" in snap.fields
        assert "model" in snap.fields

    @pytest.mark.asyncio
    async def test_snapshot_updates_after_turn(self):
        server = MockResponsesServer()
        server.response_text = "Hello"
        bridge = _make_bridge(server)
        rec = _recorder()

        snap_before = bridge.snapshot_state()
        assert snap_before.fields["response_count"] == 0

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        snap_after = bridge.snapshot_state()
        assert snap_after.fields["response_count"] == 1
        assert snap_after.fields["last_completed_response_id"] is not None


# ── Reset tests ─────────────────────────────────────────────────


class TestReset:
    """reset() clears all state."""

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        server = MockResponsesServer()
        server.response_text = "Hello"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        assert bridge._response_count > 0

        bridge.reset()

        assert bridge._last_completed_response_id is None
        assert bridge._response_count == 0
        assert bridge._replay_items is None
        assert bridge._pending_interruption_note is None


# ── Request body construction ───────────────────────────────────


class TestRequestBody:
    """Verify request body construction."""

    @pytest.mark.asyncio
    async def test_model_in_request(self):
        server = MockResponsesServer()
        server.response_text = "Ok"
        bridge = _make_bridge(server, model="gpt-4o-mini")
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        req = server.received_requests[0]
        assert req["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_stream_flag_in_request(self):
        server = MockResponsesServer()
        server.response_text = "Ok"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        req = server.received_requests[0]
        assert req["stream"] is True

    @pytest.mark.asyncio
    async def test_user_message_in_input(self):
        server = MockResponsesServer()
        server.response_text = "Ok"
        bridge = _make_bridge(server)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("Tell me a joke"), rec):
            pass

        req = server.received_requests[0]
        user_msgs = [item for item in req["input"] if item.get("role") == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "Tell me a joke"


# ── Import/export tests ────────────────────────────────────────


class TestExports:
    """RemoteResponsesAPIBridge is exported from the package."""

    def test_importable_from_package(self):
        from easycat.integrations.agents import RemoteResponsesAPIBridge as Imported

        assert Imported is RemoteResponsesAPIBridge


# ── InterruptionPlan tests ──────────────────────────────────────


class TestInterruptionPlan:
    """Verify _plan_interruption produces valid InterruptionPlan."""

    def test_plan_returns_valid_plan(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        bridge._last_accumulated_items = []
        plan = bridge._plan_interruption("text", CancellationMode.IMMEDIATE_STOP)
        assert isinstance(plan, InterruptionPlan)
        assert plan.mutation_kind == "interrupt_n1_chain"
        assert plan.pre_state_ref != ""
        assert plan.post_state_ref != ""

    def test_plan_includes_truncated_text(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        bridge._last_accumulated_items = []
        plan = bridge._plan_interruption("hello world", CancellationMode.IMMEDIATE_STOP)
        assert plan.framework_instructions["truncated_text"] == "hello world..."

    def test_plan_empty_delivered_text(self):
        server = MockResponsesServer()
        bridge = _make_bridge(server)
        bridge._last_accumulated_items = []
        plan = bridge._plan_interruption("", CancellationMode.IMMEDIATE_STOP)
        assert plan.framework_instructions["truncated_text"] == ""


# ── AC2C.13: Integration test (gated) ──────────────────────────


class TestIntegration:
    """AC2C.13 -- gated integration test against a live endpoint."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_live_responses_api(self):
        base_url = os.environ.get("RESPONSES_API_BASE_URL")
        api_key = os.environ.get("RESPONSES_API_KEY") or os.environ.get(
            "EASYCAT_REMOTE_AGENT_API_KEY"
        )
        model = os.environ.get("RESPONSES_API_MODEL", "gpt-4o-mini")

        if not base_url or not api_key:
            pytest.skip(
                "RESPONSES_API_BASE_URL and RESPONSES_API_KEY not set "
                "-- skipping live integration test"
            )

        bridge = RemoteResponsesAPIBridge(
            base_url=base_url,
            model=model,
            api_key=api_key,
        )
        rec = _recorder()

        events = []
        async for ev in bridge.invoke(
            AgentTurnInput.from_text("Say hello in exactly three words."),
            rec,
        ):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert "text_delta" in kinds
        assert "done" in kinds
        assert bridge._response_count == 1
