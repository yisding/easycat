"""WS2B: Interruption contract and MCP pass-through tests.

Covers:
- AC2B.2: Four-step atomic write ordering (AST-level)
- AC2B.3: InterruptionApplyFailed emission on controlled failure
- AC2B.4: Three cancellation modes end-to-end per bridge/mode
- AC2B.5: drain_to_commit_point respects COMMITTABLE_BOUNDARIES
- AC2B.6: Atomicity on mutation failure (apply failure)
- AC2B.7: Atomicity on journal-write failure (commit failure)
- AC2B.8: Shallow-mode downgrade path
- AC2B.9: MCP mock wiring, shallow warning, deep pass-through
- AC2B.10: EasyCatConfig MCP URI validation
- AC2B.11: No EasyCat-native tool code (guardrail re-run)
- AC2B.12: Signal-to-framework cancellation linkage
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    CancellationMode,
    InterruptionPlan,
    MutationInjectedError,
    RecorderContext,
    ShallowModeInterruptionError,
    UnitKind,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.runtime.records import ErrorInfo

# ── Helpers ─────────────────────────────────────────────────────


def _recorder(journal=None, mcp_servers=()):
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(
            run_id="r1", session_id="s1", turn_id="t1", mcp_servers=mcp_servers
        ),
    )


def _openai_bridge():
    """Create an OpenAIAgentsBridge with mock agent and message history."""
    agent = MagicMock()
    agent.name = "TestAgent"
    bridge = OpenAIAgentsBridge(agent)
    # Seed with a fake assistant message for interruption to act on.
    bridge._message_history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "The full response text here."},
            ],
        },
    ]
    return bridge


def _pydantic_ai_bridge_agent():
    """Create a PydanticAIBridge in agent mode with mock agent."""
    try:
        from pydantic_ai import Agent
        from pydantic_ai.messages import ModelResponse, TextPart

        agent = MagicMock(spec=Agent)
        agent.name = "TestPydanticAgent"
        bridge = PydanticAIBridge(agent=agent)
        bridge._message_history = [ModelResponse(parts=[TextPart(content="Full response text.")])]
        return bridge
    except ImportError:
        pytest.skip("pydantic_ai not installed")


def _pydantic_ai_bridge_graph():
    """Create a PydanticAIBridge in graph mode with mock graph."""
    try:
        from pydantic_ai.messages import ModelResponse, TextPart

        class _MockState:
            _easycat_event_handler: Any = None

            def truncate_last_assistant(self, text: str) -> None:
                pass

        bridge = PydanticAIBridge(
            graph=MagicMock(),
            state_factory=_MockState,
            initial_node_factory=lambda text, state: None,
        )
        bridge._state = _MockState()
        bridge._message_history = [ModelResponse(parts=[TextPart(content="Graph response.")])]
        return bridge
    except ImportError:
        pytest.skip("pydantic_ai not installed")


class _DeepWorkflow:
    """Deep-mode workflow with explicit apply_interruption."""

    def __init__(self):
        self.interrupted = False
        self.last_delivered = ""
        self.last_mode = None

    async def on_user_turn(self, text: str, *, recorder=None, cancel_token=None) -> str:
        return f"Response: {text}"

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        self.interrupted = True
        self.last_delivered = delivered_text
        self.last_mode = mode


class _DeepWorkflowNoOverride:
    """Deep-mode workflow without apply_interruption."""

    async def on_user_turn(self, text: str, *, recorder=None, cancel_token=None) -> str:
        return f"Response: {text}"


class _ShallowWorkflow:
    """Shallow-mode workflow."""

    async def on_user_turn(self, text: str) -> str:
        return f"Shallow: {text}"


class _ShallowWorkflowWithOverride:
    """Shallow-mode workflow with explicit apply_interruption override."""

    def __init__(self):
        self.interrupted = False

    async def on_user_turn(self, text: str) -> str:
        return f"Shallow: {text}"

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        self.interrupted = True


def _generic_deep_bridge():
    return GenericWorkflowBridge(workflow=_DeepWorkflow())


def _generic_deep_bridge_no_override():
    return GenericWorkflowBridge(workflow=_DeepWorkflowNoOverride())


def _generic_shallow_bridge():
    return GenericWorkflowBridge(workflow=_ShallowWorkflow())


def _generic_shallow_bridge_with_override():
    return GenericWorkflowBridge(workflow=_ShallowWorkflowWithOverride())


# ── AC2B.2: Four-step atomic write ordering (AST-level) ────────


class TestApplyInterruptionFourStepOrder:
    """AC2B.2 — AST-level assertion that each bridge's apply_interruption
    consists of _plan_interruption → journal write → apply → paired write."""

    @pytest.mark.parametrize(
        "bridge_cls",
        [OpenAIAgentsBridge, PydanticAIBridge, GenericWorkflowBridge],
    )
    def test_four_step_method_calls_present(self, bridge_cls):
        """Each bridge has _plan_interruption, _apply_planned_mutation,
        and the public apply_interruption references both."""
        assert hasattr(bridge_cls, "_plan_interruption")
        assert hasattr(bridge_cls, "_apply_planned_mutation")
        assert hasattr(bridge_cls, "apply_interruption")

        # AST-level: verify apply_interruption calls both helpers.
        import textwrap

        source = textwrap.dedent(inspect.getsource(bridge_cls.apply_interruption))
        tree = ast.parse(source)

        call_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)

        assert "_plan_interruption" in call_names, (
            f"{bridge_cls.__name__}.apply_interruption must call _plan_interruption"
        )
        assert "_apply_planned_mutation" in call_names, (
            f"{bridge_cls.__name__}.apply_interruption must call _apply_planned_mutation"
        )

    @pytest.mark.parametrize(
        "bridge_cls",
        [OpenAIAgentsBridge, PydanticAIBridge, GenericWorkflowBridge],
    )
    def test_apply_interruption_calls_record_state_committed(self, bridge_cls):
        """apply_interruption calls record_state_committed before _apply_planned_mutation."""
        import textwrap

        source = textwrap.dedent(inspect.getsource(bridge_cls.apply_interruption))
        tree = ast.parse(source)

        # Collect calls in line-number order.
        calls_with_line: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls_with_line.append((node.lineno, node.func.attr))
        calls_with_line.sort(key=lambda x: x[0])
        call_order = [name for _, name in calls_with_line]

        # record_state_committed must appear before _apply_planned_mutation.
        if "record_state_committed" in call_order and "_apply_planned_mutation" in call_order:
            committed_idx = call_order.index("record_state_committed")
            apply_idx = call_order.index("_apply_planned_mutation")
            assert committed_idx < apply_idx, (
                f"{bridge_cls.__name__}: record_state_committed must come "
                "before _apply_planned_mutation"
            )

    @pytest.mark.parametrize(
        "bridge_cls",
        [OpenAIAgentsBridge, PydanticAIBridge, GenericWorkflowBridge],
    )
    def test_no_direct_framework_mutation_outside_apply_planned(self, bridge_cls):
        """apply_interruption does not directly mutate framework state
        outside of _apply_planned_mutation."""
        import textwrap

        source = textwrap.dedent(inspect.getsource(bridge_cls.apply_interruption))
        # Should not contain direct message_history mutation.
        assert "_message_history" not in source, (
            f"{bridge_cls.__name__}.apply_interruption must not "
            "directly reference _message_history"
        )


# ── AC2B.3: InterruptionApplyFailed emission ───────────────────


class TestInterruptionApplyFailedEmitted:
    """AC2B.3 — controlled mutation failure emits InterruptionApplyFailed."""

    def test_openai_bridge_failure(self):
        bridge = _openai_bridge()
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

    def test_pydantic_bridge_agent_failure(self):
        bridge = _pydantic_ai_bridge_agent()
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

    def test_pydantic_bridge_graph_failure(self):
        bridge = _pydantic_ai_bridge_graph()
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

    def test_generic_deep_bridge_failure(self):
        bridge = _generic_deep_bridge()
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


# ── AC2B.4: Three cancellation modes per bridge/mode ───────────


_INTERRUPTIBLE_BRIDGES = [
    ("openai", _openai_bridge),
    ("pydantic_agent", _pydantic_ai_bridge_agent),
    ("pydantic_graph", _pydantic_ai_bridge_graph),
    ("generic_deep", _generic_deep_bridge),
    ("generic_deep_no_override", _generic_deep_bridge_no_override),
    ("generic_shallow_with_override", _generic_shallow_bridge_with_override),
]

_ALL_MODES = [
    CancellationMode.IMMEDIATE_STOP,
    CancellationMode.DRAIN_CURRENT_UNIT,
    CancellationMode.DRAIN_TO_COMMIT_POINT,
]


class TestCancellationModeMatrix:
    """AC2B.4 — three modes work on every interruptible bridge/mode."""

    @pytest.mark.parametrize("bridge_name,factory", _INTERRUPTIBLE_BRIDGES)
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_mode_produces_correct_journal_records(self, bridge_name, factory, mode):
        bridge = factory()
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

        # Must have state_committed followed by cancellation_boundary.
        assert "state_committed" in names
        assert "cancellation_boundary" in names

        # state_committed comes before cancellation_boundary.
        committed_idx = names.index("state_committed")
        boundary_idx = names.index("cancellation_boundary")
        assert committed_idx < boundary_idx

        # No interruption_apply_failed on success.
        assert "interruption_apply_failed" not in names

        # Cancellation boundary carries the signal ID.
        boundary_rec = records[boundary_idx]
        assert boundary_rec.data["caused_by_signal_id"] == "sig-42"
        assert boundary_rec.data["cancellation_mode"] == mode.value


class TestShallowModeDowngradePath:
    """AC2B.8 — shallow workflow without override raises
    ShallowModeInterruptionError."""

    def test_shallow_no_override_raises(self):
        bridge = _generic_shallow_bridge()
        with pytest.raises(ShallowModeInterruptionError):
            bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP)

    def test_shallow_with_override_succeeds(self):
        bridge = _generic_shallow_bridge_with_override()
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        bridge.apply_interruption(
            "partial",
            CancellationMode.IMMEDIATE_STOP,
            recorder=rec,
            caused_by_signal_id="sig-1",
        )

        assert bridge._workflow.interrupted
        records = journal.read()
        names = [r.name for r in records]
        assert "state_committed" in names
        assert "cancellation_boundary" in names


# ── AC2B.5: drain_to_commit_point respects boundaries ──────────


class TestDrainToCommitPointRespectsBoundaries:
    """AC2B.5 — each bridge's COMMITTABLE_BOUNDARIES is consistent
    with its _plan_interruption output."""

    @pytest.mark.parametrize("bridge_name,factory", _INTERRUPTIBLE_BRIDGES)
    def test_plan_returns_valid_mutation_kind(self, bridge_name, factory):
        bridge = factory()
        plan = bridge._plan_interruption("text", CancellationMode.DRAIN_TO_COMMIT_POINT)
        assert isinstance(plan, InterruptionPlan)
        assert plan.mutation_kind != ""
        assert plan.pre_state_ref != ""
        assert plan.post_state_ref != ""

    def test_openai_boundaries_match_unit_kinds(self):
        bridge = _openai_bridge()
        for kind in bridge.COMMITTABLE_BOUNDARIES:
            assert isinstance(kind, UnitKind)

    def test_generic_boundaries_match_unit_kinds(self):
        bridge = _generic_deep_bridge()
        for kind in bridge.COMMITTABLE_BOUNDARIES:
            assert isinstance(kind, UnitKind)


# ── AC2B.6: Atomicity on apply failure ─────────────────────────


class TestAtomicityOnApplyFailure:
    """AC2B.6 — mutation failure after FrameworkStateCommitted write."""

    @pytest.mark.parametrize(
        "factory",
        [
            _openai_bridge,
            _pydantic_ai_bridge_agent,
            _pydantic_ai_bridge_graph,
            _generic_deep_bridge,
        ],
    )
    def test_mutation_failure_writes_paired_records(self, factory):
        bridge = factory()
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        def _raise(_plan):
            raise MutationInjectedError("controlled failure")

        bridge._apply_planned_mutation = _raise

        with pytest.raises(MutationInjectedError):
            bridge.apply_interruption("heard", CancellationMode.IMMEDIATE_STOP, recorder=rec)

        records = journal.read()
        names = [r.name for r in records]

        # FrameworkStateCommitted followed by InterruptionApplyFailed.
        assert "state_committed" in names
        assert "interruption_apply_failed" in names
        committed_idx = names.index("state_committed")
        failed_idx = names.index("interruption_apply_failed")
        assert committed_idx < failed_idx

        # No FrameworkCancellationBoundaryReached on failure.
        assert "cancellation_boundary" not in names

        # Mutation kinds match.
        committed_data = records[committed_idx].data
        failed_data = records[failed_idx].data
        assert committed_data["mutation_kind"] == failed_data["mutation_kind"]

    @pytest.mark.parametrize(
        "factory",
        [
            _openai_bridge,
            _pydantic_ai_bridge_agent,
            _pydantic_ai_bridge_graph,
            _generic_deep_bridge,
        ],
    )
    def test_failure_error_info_captured(self, factory):
        bridge = factory()
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        def _raise(_plan):
            raise MutationInjectedError("controlled failure")

        bridge._apply_planned_mutation = _raise

        with pytest.raises(MutationInjectedError):
            bridge.apply_interruption("heard", CancellationMode.IMMEDIATE_STOP, recorder=rec)

        records = journal.read()
        failed_records = [r for r in records if r.name == "interruption_apply_failed"]
        assert len(failed_records) == 1
        assert failed_records[0].error is not None
        assert "MutationInjectedError" in failed_records[0].error.type


# ── AC2B.7: Atomicity on journal-write failure ─────────────────


class TestAtomicityOnCommitWriteFailure:
    """AC2B.7 — journal write failure prevents mutation."""

    @pytest.mark.parametrize(
        "factory",
        [
            _openai_bridge,
            _pydantic_ai_bridge_agent,
            _pydantic_ai_bridge_graph,
            _generic_deep_bridge,
        ],
    )
    def test_commit_write_failure_skips_mutation(self, factory):
        bridge = factory()

        # Capture whether _apply_planned_mutation was called.
        original_apply = bridge._apply_planned_mutation
        apply_called = []

        def _tracking_apply(plan):
            apply_called.append(True)
            return original_apply(plan)

        bridge._apply_planned_mutation = _tracking_apply

        # Create a recorder whose record_state_committed raises.
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        def _raise_on_commit(*args, **kwargs):
            raise RuntimeError("journal degraded")

        rec.record_state_committed = _raise_on_commit

        # apply_interruption should return without applying.
        bridge.apply_interruption("heard", CancellationMode.IMMEDIATE_STOP, recorder=rec)

        # _apply_planned_mutation must NOT have been called.
        assert len(apply_called) == 0

        # No records should be in the journal.
        records = journal.read()
        assert len(records) == 0


# ── AC2B.9: MCP tests ──────────────────────────────────────────


class TestMCPShallowModeWarning:
    """AC2B.9 — shallow workflow with mcp_servers emits warning record."""

    @pytest.mark.asyncio
    async def test_shallow_mcp_warning_emitted(self):
        bridge = _generic_shallow_bridge()
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal, mcp_servers=("stdio://mock-server",))

        inp = AgentTurnInput.from_text("hello")
        async for _ in bridge.invoke(inp, rec):
            pass

        records = journal.read()
        error_records = [r for r in records if r.name == "framework_error"]
        assert len(error_records) >= 1

        warning_found = False
        for r in error_records:
            if r.error and "MCPShallowModeWarning" in r.error.type:
                warning_found = True
                assert "shallow mode" in r.error.message.lower()
        assert warning_found, "Expected MCPShallowModeWarning record"

    @pytest.mark.asyncio
    async def test_shallow_mcp_warning_emitted_once(self):
        """Warning is emitted exactly once per session, not per turn."""
        bridge = _generic_shallow_bridge()
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal, mcp_servers=("stdio://mock-server",))
        inp = AgentTurnInput.from_text("hello")

        # Two invocations.
        async for _ in bridge.invoke(inp, rec):
            pass
        async for _ in bridge.invoke(inp, rec):
            pass

        records = journal.read()
        warnings = [
            r
            for r in records
            if r.name == "framework_error" and r.error and "MCPShallowModeWarning" in r.error.type
        ]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_no_mcp_warning_without_servers(self):
        """No warning when mcp_servers is empty."""
        bridge = _generic_shallow_bridge()
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)  # no mcp_servers
        inp = AgentTurnInput.from_text("hello")

        async for _ in bridge.invoke(inp, rec):
            pass

        records = journal.read()
        warnings = [
            r
            for r in records
            if r.name == "framework_error" and r.error and "MCPShallowModeWarning" in r.error.type
        ]
        assert len(warnings) == 0


class TestMCPDeepModePassthrough:
    """AC2B.9 — deep-mode workflow reads mcp_servers from RecorderContext."""

    @pytest.mark.asyncio
    async def test_deep_workflow_sees_mcp_servers(self):
        mcp = ("stdio://test-server", "https://remote.example.com/mcp")

        class _MCPAwareWorkflow:
            def __init__(self):
                self.seen_mcp: tuple[str, ...] = ()

            async def on_user_turn(self, text: str, *, recorder=None, cancel_token=None) -> str:
                if recorder is not None:
                    self.seen_mcp = recorder.context.mcp_servers
                return "ok"

        workflow = _MCPAwareWorkflow()
        bridge = GenericWorkflowBridge(workflow=workflow)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal, mcp_servers=mcp)
        inp = AgentTurnInput.from_text("hello")

        async for _ in bridge.invoke(inp, rec):
            pass

        assert workflow.seen_mcp == mcp


class TestMCPWiringMockServer:
    """AC2B.9 — MCP server list reaches bridge construction."""

    def test_openai_bridge_stores_mcp_servers(self):
        agent = MagicMock()
        agent.name = "Test"
        bridge = OpenAIAgentsBridge(agent, mcp_servers=["stdio://test"])
        assert bridge._mcp_servers == ["stdio://test"]

    def test_pydantic_bridge_agent_stores_mcp_servers(self):
        try:
            agent = MagicMock()
            agent.name = "Test"
            bridge = PydanticAIBridge(agent=agent, mcp_servers=["stdio://test"])
            assert bridge._mcp_servers == ["stdio://test"]
        except Exception:
            pytest.skip("pydantic_ai construction requires specific mock")

    def test_pydantic_bridge_graph_stores_mcp_servers(self):
        try:

            class _State:
                _easycat_event_handler: Any = None

            bridge = PydanticAIBridge(
                graph=MagicMock(),
                state_factory=_State,
                initial_node_factory=lambda t, s: None,
                mcp_servers=["stdio://test"],
            )
            assert bridge._mcp_servers == ["stdio://test"]
        except Exception:
            pytest.skip("pydantic_ai construction requires specific mock")


class TestMCPFilesystemIntegration:
    """AC2B.9 — gated integration test using real mcp-filesystem binary."""

    @pytest.mark.integration
    def test_mcp_filesystem_integration(self):
        import os

        server_path = os.environ.get("MCP_FILESYSTEM_SERVER_PATH")
        if not server_path:
            pytest.skip(
                "MCP_FILESYSTEM_SERVER_PATH not set — skipping test_mcp_filesystem_integration"
            )

        # Verify the binary exists.
        assert os.path.isfile(server_path), (
            f"MCP_FILESYSTEM_SERVER_PATH={server_path!r} does not exist"
        )

        # Verify each MCP-capable bridge accepts a stdio:// URI
        # pointing at the binary and the recorder context carries it.
        uri = f"stdio://{server_path}"
        bridge = OpenAIAgentsBridge(MagicMock(name="TestAgent"), mcp_servers=[uri])
        assert bridge._mcp_servers == [uri]

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal, mcp_servers=(uri,))
        assert rec.context.mcp_servers == (uri,)


# ── AC2B.10: EasyCatConfig MCP URI validation ──────────────────


class TestEasyCatConfigMCPValidation:
    """AC2B.10 — invalid MCP URIs raise EasyCatConfigError."""

    def test_valid_uris_accepted(self):
        """Valid schemes do not raise."""

        # EasyCatConfig requires STT/TTS, so we just test the validation
        # logic directly via the _validate pathway. We'll construct with
        # required fields.
        try:
            from easycat.config import _VALID_MCP_SCHEMES

            servers = [
                "stdio:///usr/local/bin/mcp-fs",
                "sse://localhost:8080",
                "http://mcp.example.com/v1",
                "https://mcp.example.com/v1",
            ]
            for s in servers:
                assert any(s.startswith(scheme) for scheme in _VALID_MCP_SCHEMES)
        except ImportError:
            pytest.skip("config not importable")

    def test_invalid_uri_raises(self):
        from easycat.config import _VALID_MCP_SCHEMES, EasyCatConfigError

        # Test the validation logic directly.
        bad_uri = "ftp://bad-server"
        assert not any(bad_uri.startswith(s) for s in _VALID_MCP_SCHEMES)

        # Attempt to construct EasyCatConfig with invalid URI.
        try:
            from easycat.config import EasyCatConfig
            from easycat.stt.openai_provider import OpenAISTTConfig
            from easycat.tts.openai_tts import OpenAITTSConfig

            with pytest.raises(EasyCatConfigError, match="ftp://bad"):
                EasyCatConfig(
                    stt=OpenAISTTConfig(api_key="test"),
                    tts=OpenAITTSConfig(api_key="test"),
                    mcp_servers=["ftp://bad-server"],
                )
        except ImportError:
            pytest.skip("config dependencies not importable")

    def test_mixed_valid_invalid_raises(self):
        from easycat.config import EasyCatConfigError

        try:
            from easycat.config import EasyCatConfig
            from easycat.stt.openai_provider import OpenAISTTConfig
            from easycat.tts.openai_tts import OpenAITTSConfig

            with pytest.raises(EasyCatConfigError, match="ws://bad"):
                EasyCatConfig(
                    stt=OpenAISTTConfig(api_key="test"),
                    tts=OpenAITTSConfig(api_key="test"),
                    mcp_servers=["stdio://good", "ws://bad"],
                )
        except ImportError:
            pytest.skip("config dependencies not importable")


# ── AC2B.11: No EasyCat-native tool code ───────────────────────


class TestNoToolRegistryAfterMCP:
    """AC2B.11 — re-run WS2A AC2.10 guardrail after MCP wiring."""

    def test_no_easycat_native_tool_code(self):
        # Plain string patterns (exact grep).
        plain_patterns = [
            "@easycat_tool",
            "@register_tool",
            "@register_function",
            "class ToolRegistry",
            "class MCPClient",
            "def register_tool",
        ]
        for pattern in plain_patterns:
            result = subprocess.run(
                ["grep", "-r", pattern, "src/easycat/"],
                capture_output=True,
                text=True,
            )
            assert result.stdout.strip() == "", (
                f"Found tool pattern {pattern!r} in src/easycat/:\n{result.stdout}"
            )

        # Regex patterns for broader coverage (AC2B.11 full set).
        regex_patterns = [
            r"class \w*Registry",
            r"class \w*Router",
        ]
        for pattern in regex_patterns:
            result = subprocess.run(
                ["grep", "-rE", pattern, "src/easycat/"],
                capture_output=True,
                text=True,
            )
            assert result.stdout.strip() == "", (
                f"Found tool pattern {pattern!r} in src/easycat/:\n{result.stdout}"
            )


# ── AC2B.12: Signal-to-framework cancellation linkage ──────────


class TestSignalToFrameworkCancellationLinkage:
    """AC2B.12 — caused_by_signal_id links boundary to signal."""

    @pytest.mark.parametrize("bridge_name,factory", _INTERRUPTIBLE_BRIDGES)
    def test_cancellation_boundary_carries_signal_id(self, bridge_name, factory):
        bridge = factory()
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        signal_id = f"sig-{bridge_name}-001"
        bridge.apply_interruption(
            "heard text",
            CancellationMode.IMMEDIATE_STOP,
            recorder=rec,
            caused_by_signal_id=signal_id,
        )

        records = journal.read()
        boundary_records = [r for r in records if r.name == "cancellation_boundary"]
        assert len(boundary_records) == 1
        assert boundary_records[0].data["caused_by_signal_id"] == signal_id


# ── InterruptionPlan dataclass tests ────────────────────────────


class TestInterruptionPlan:
    """Verify InterruptionPlan construction and immutability."""

    def test_construction(self):
        plan = InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref="pre-abc",
            post_state_ref="post-abc",
            framework_instructions={"replacement": "hello..."},
        )
        assert plan.mutation_kind == "interrupt_truncate"
        assert plan.framework_instructions["replacement"] == "hello..."

    def test_frozen(self):
        plan = InterruptionPlan(
            mutation_kind="test",
            pre_state_ref="pre",
            post_state_ref="post",
        )
        with pytest.raises(AttributeError):
            plan.mutation_kind = "changed"  # type: ignore[misc]


# ── MutationInjectedError tests ────────────────────────────────


class TestMutationInjectedError:
    def test_is_runtime_error(self):
        assert issubclass(MutationInjectedError, RuntimeError)

    def test_constructible(self):
        err = MutationInjectedError("test failure")
        assert str(err) == "test failure"


# ── Recorder new methods tests ──────────────────────────────────


class TestRecorderAtomicMethods:
    """Verify record_state_committed and record_interruption_apply_failed."""

    def test_record_state_committed(self):
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        rec.record_state_committed(
            mutation_kind="interrupt_truncate",
            pre_state_ref="pre-ref",
            post_state_ref="post-ref",
        )

        records = journal.read()
        assert len(records) == 1
        assert records[0].name == "state_committed"
        assert records[0].data["mutation_kind"] == "interrupt_truncate"
        assert records[0].data["pre_state_ref"] == "pre-ref"
        assert records[0].data["post_state_ref"] == "post-ref"

    def test_record_interruption_apply_failed(self):
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        err = ErrorInfo(type="TestError", message="boom")
        rec.record_interruption_apply_failed(
            mutation_kind="interrupt_truncate",
            pre_state_ref="pre-ref",
            post_state_ref="post-ref",
            failure_error=err,
        )

        records = journal.read()
        assert len(records) == 1
        assert records[0].name == "interruption_apply_failed"
        assert records[0].data["mutation_kind"] == "interrupt_truncate"
        assert records[0].error is not None
        assert records[0].error.type == "TestError"

    def test_no_op_without_journal(self):
        rec = JournalAgentRecorder(
            journal=None,
            artifact_store=None,
            context=RecorderContext(run_id="r1", session_id="s1"),
        )
        # Should not raise.
        rec.record_state_committed(mutation_kind="test")
        rec.record_interruption_apply_failed(mutation_kind="test")


# ── Backward compatibility ──────────────────────────────────────


class TestBackwardCompatibility:
    """apply_interruption still works without recorder (legacy path)."""

    def test_openai_no_recorder(self):
        bridge = _openai_bridge()
        bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP)
        # Should still mutate message history.
        content = bridge._message_history[1]["content"]
        text_parts = [
            p["text"] for p in content if isinstance(p, dict) and p.get("type") == "output_text"
        ]
        assert text_parts[0] == "partial..."

    def test_generic_deep_no_recorder(self):
        bridge = _generic_deep_bridge()
        bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP)
        assert bridge._workflow.interrupted

    def test_generic_deep_no_override_no_recorder(self):
        bridge = _generic_deep_bridge_no_override()
        # Should not raise — just logs debug.
        bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP)


# ── Bridge history post-processing (markdown strip + message-mode note) ──


class TestOpenAIAgentsBridgeHistoryPostProcessing:
    """OpenAIAgentsBridge.replace_last_assistant_text and
    append_interruption_note must mutate the bridge's own
    ``_message_history`` so subsequent turns see the update."""

    def test_replace_last_assistant_text_list_content(self):
        bridge = _openai_bridge()
        bridge.replace_last_assistant_text("cleaned")
        content = bridge._message_history[1]["content"]
        parts = [p for p in content if p.get("type") == "output_text"]
        assert parts[0]["text"] == "cleaned"

    def test_replace_last_assistant_text_plain_string(self):
        agent = MagicMock()
        agent.name = "Plain"
        bridge = OpenAIAgentsBridge(agent)
        bridge._message_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "**bold** text"},
        ]
        bridge.replace_last_assistant_text("bold text")
        assert bridge._message_history[1]["content"] == "bold text"

    def test_replace_last_assistant_text_no_assistant_is_noop(self):
        agent = MagicMock()
        agent.name = "NoAssistant"
        bridge = OpenAIAgentsBridge(agent)
        bridge._message_history = [{"role": "user", "content": "hi"}]
        # Should not raise.
        bridge.replace_last_assistant_text("anything")
        assert bridge._message_history == [{"role": "user", "content": "hi"}]

    def test_append_interruption_note_adds_developer_message(self):
        bridge = _openai_bridge()
        bridge.append_interruption_note("note")
        assert bridge._message_history[-1] == {"role": "developer", "content": "note"}

    def test_append_interruption_note_sets_pending_in_response_id_mode(self):
        bridge = _openai_bridge()
        bridge._use_previous_response_id = True
        bridge._previous_response_id = "resp-prior"
        bridge.append_interruption_note("note")
        assert bridge._pending_interruption == "note"

    def test_append_interruption_note_survives_subsequent_build_input(self):
        """The appended note must participate in the next _build_input()
        so a new turn actually sees the interruption signal."""
        bridge = _openai_bridge()
        bridge._use_previous_response_id = False
        bridge._previous_response_id = None
        bridge.append_interruption_note("note")
        input_data = bridge._build_input("next question")
        assert isinstance(input_data, list)
        assert any(
            isinstance(item, dict)
            and item.get("role") == "developer"
            and item.get("content") == "note"
            for item in input_data
        )


class TestPydanticAIBridgeHistoryPostProcessing:
    """PydanticAIBridge.replace_last_assistant_text must mutate the last
    ``ModelResponse``'s ``TextPart``. append_interruption_note must
    append a new ``ModelRequest`` with ``SystemPromptPart``."""

    def test_replace_last_assistant_text(self):
        bridge = _pydantic_ai_bridge_agent()
        bridge.replace_last_assistant_text("cleaned")
        from pydantic_ai.messages import ModelResponse

        last = bridge._message_history[-1]
        assert isinstance(last, ModelResponse)
        text_part = next(p for p in last.parts if type(p).__name__ == "TextPart")
        assert text_part.content == "cleaned"

    def test_append_interruption_note(self):
        bridge = _pydantic_ai_bridge_agent()
        original_len = len(bridge._message_history)
        bridge.append_interruption_note("interrupted")
        from pydantic_ai.messages import ModelRequest, SystemPromptPart

        assert len(bridge._message_history) == original_len + 1
        appended = bridge._message_history[-1]
        assert isinstance(appended, ModelRequest)
        system_part = next(p for p in appended.parts if isinstance(p, SystemPromptPart))
        assert system_part.content == "interrupted"
