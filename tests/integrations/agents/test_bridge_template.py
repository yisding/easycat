"""BridgeTemplate starter base class and register_agent_detector hook."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.integrations.agents import (
    BridgeTemplate,
    auto_adapt_agent,
    clear_agent_detectors,
    register_agent_detector,
)
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    InterruptionPlan,
    MutationInjectedError,
    RecorderContext,
    UnitKind,
)
from easycat.runtime import InMemoryRingBuffer


def _recorder(journal=None):
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class _MinimalBridge(BridgeTemplate):
    """Smallest possible author implementation: the three hooks."""

    def __init__(self) -> None:
        super().__init__(display_name="Minimal")
        self.applied: list[InterruptionPlan] = []
        self.api_key = "sk-secret"  # exercised by scrub tests

    async def stream_events(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token,
    ) -> AsyncIterator[AgentBridgeEvent]:
        for word in turn_input.text.split():
            yield AgentBridgeEvent(kind="text_delta", text=word + " ")

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={"history_len": 2, "api_key": self.api_key},
            kind="minimal",
        )

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref="min-pre",
            post_state_ref="min-post",
            framework_instructions={"delivered_text": delivered_text},
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        self.applied.append(plan)


class _FailingBridge(_MinimalBridge):
    async def stream_events(self, turn_input, recorder, cancel_token):
        yield AgentBridgeEvent(kind="text_delta", text="partial")
        raise RuntimeError("framework exploded")


# ── invoke() lifecycle ───────────────────────────────────────────


class TestInvokeLifecycle:
    async def test_invoke_yields_deltas_and_done(self):
        bridge = _MinimalBridge()
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hello world"), _recorder()):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert kinds == ["text_delta", "text_delta", "done"]
        assert events[-1].text == "hello world "

    async def test_invoke_records_unit_enter_and_committable_exit(self):
        journal = InMemoryRingBuffer(capacity=1000)
        bridge = _MinimalBridge()
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal)):
            pass

        names = [r.name for r in journal.read()]
        assert "unit_entered" in names
        assert "unit_exited" in names

    async def test_invoke_error_records_framework_error_and_exit(self):
        journal = InMemoryRingBuffer(capacity=1000)
        bridge = _FailingBridge()

        with pytest.raises(RuntimeError, match="framework exploded"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal)):
                pass

        names = [r.name for r in journal.read()]
        assert "framework_error" in names
        assert "unit_exited" in names

    async def test_invoke_cancellation_closes_cursor_without_framework_error(self):
        """GeneratorExit (AgentRunner timeout path) closes the cursor via
        safe_exit_cursor instead of leaving it dangling or recording a
        framework error."""
        journal = InMemoryRingBuffer(capacity=1000)
        bridge = _MinimalBridge()

        gen = bridge.invoke(AgentTurnInput.from_text("hello world"), _recorder(journal))
        first = await gen.__anext__()
        assert first.kind == "text_delta"
        await gen.aclose()

        names = [r.name for r in journal.read()]
        assert "unit_entered" in names
        assert "unit_exited" in names
        assert "framework_error" not in names

    async def test_structured_output_flows_to_done_event(self):
        class _StructuredBridge(_MinimalBridge):
            async def stream_events(self, turn_input, recorder, cancel_token):
                self._last_output = {"answer": 42}
                yield AgentBridgeEvent(kind="text_delta", text="42")

        bridge = _StructuredBridge()
        events = [e async for e in bridge.invoke(AgentTurnInput.from_text("q"), _recorder())]
        assert events[-1].kind == "done"
        assert events[-1].structured_output == {"answer": 42}

    async def test_unimplemented_stream_events_raises_not_implemented(self):
        class _Empty(BridgeTemplate):
            pass

        bridge = _Empty()
        with pytest.raises(NotImplementedError, match="stream_events"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
                pass


# ── Defaults: boundaries, protocol conformance, no-op mutators ───


class TestTemplateDefaults:
    def test_default_committable_boundaries(self):
        assert BridgeTemplate.COMMITTABLE_BOUNDARIES == {
            UnitKind.WORKFLOW_NODE: CommitRule.BETWEEN_TURNS,
        }

    def test_minimal_subclass_satisfies_bridge_protocol(self):
        assert isinstance(_MinimalBridge(), ExternalAgentBridge)

    def test_safe_noop_mutation_methods(self):
        bridge = _MinimalBridge()
        bridge.replace_last_assistant_text("cleaned")
        bridge.append_interruption_note("note")
        bridge._last_output = "stale"
        bridge.reset()
        assert bridge._last_output is None

    def test_serialized_state_scrubs_secret_fields(self):
        payload = _MinimalBridge()._serialize_framework_state().decode()
        assert "history_len" in payload
        assert "sk-secret" not in payload
        assert "api_key" not in payload

    def test_serialized_state_degrades_to_empty_on_snapshot_failure(self):
        class _Broken(_MinimalBridge):
            def snapshot_state(self):
                raise RuntimeError("no snapshot")

        assert _Broken()._serialize_framework_state() == b"{}"


# ── apply_interruption: four-step protocol delegation ────────────


class TestApplyInterruption:
    def test_success_writes_committed_then_boundary(self):
        journal = InMemoryRingBuffer(capacity=1000)
        bridge = _MinimalBridge()

        bridge.apply_interruption(
            "heard text",
            CancellationMode.IMMEDIATE_STOP,
            recorder=_recorder(journal),
            caused_by_signal_id="sig-1",
        )

        assert [p.framework_instructions["delivered_text"] for p in bridge.applied] == [
            "heard text"
        ]
        names = [r.name for r in journal.read()]
        assert "state_committed" in names
        assert "cancellation_boundary" in names
        assert names.index("state_committed") < names.index("cancellation_boundary")

    def test_failure_writes_interruption_apply_failed(self):
        journal = InMemoryRingBuffer(capacity=1000)
        bridge = _MinimalBridge()

        def _raise(_plan):
            raise MutationInjectedError("injected")

        bridge._apply_planned_mutation = _raise

        with pytest.raises(MutationInjectedError):
            bridge.apply_interruption(
                "partial", CancellationMode.IMMEDIATE_STOP, recorder=_recorder(journal)
            )

        names = [r.name for r in journal.read()]
        assert "state_committed" in names
        assert "interruption_apply_failed" in names

    def test_check_interruption_supported_short_circuits(self):
        class _Refusing(_MinimalBridge):
            def check_interruption_supported(self) -> None:
                raise RuntimeError("not supported here")

        journal = InMemoryRingBuffer(capacity=1000)
        bridge = _Refusing()
        with pytest.raises(RuntimeError, match="not supported here"):
            bridge.apply_interruption(
                "text", CancellationMode.IMMEDIATE_STOP, recorder=_recorder(journal)
            )
        assert bridge.applied == []
        assert journal.read() == []


# ── register_agent_detector ──────────────────────────────────────


class _MyFrameworkAgent:
    """Stand-in for a third-party framework object."""


class _MyFrameworkBridge(_MinimalBridge):
    def __init__(self, agent: Any) -> None:
        super().__init__()
        self.agent = agent


@pytest.fixture(autouse=True)
def _clean_detectors():
    clear_agent_detectors()
    yield
    clear_agent_detectors()


class TestRegisterAgentDetector:
    def test_detector_routes_matching_agent_to_factory(self):
        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            lambda obj: _MyFrameworkBridge(obj),
        )

        agent = _MyFrameworkAgent()
        adapted = auto_adapt_agent(agent)
        assert isinstance(adapted, _MyFrameworkBridge)
        assert adapted.agent is agent

    def test_non_matching_agent_falls_through(self):
        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            lambda obj: _MyFrameworkBridge(obj),
        )

        class _PlainAgent:
            async def run(self, text: str) -> str:
                return text

        plain = _PlainAgent()
        assert auto_adapt_agent(plain) is plain

    def test_bridge_passthrough_wins_over_detector(self):
        """A registered detector never re-wraps an existing bridge."""
        register_agent_detector(lambda obj: True, lambda obj: _MyFrameworkBridge(obj))

        bridge = _MinimalBridge()
        assert auto_adapt_agent(bridge) is bridge

    def test_agent_runner_unwrap_happens_before_detectors(self):
        """An AgentRunner-wrapped framework agent gets its inner agent
        adapted via the registered detector."""
        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            lambda obj: _MyFrameworkBridge(obj),
        )

        runner = AgentRunner(_MyFrameworkAgent())
        adapted = auto_adapt_agent(runner)
        assert adapted is runner
        assert isinstance(runner._agent, _MyFrameworkBridge)

    def test_detector_wins_over_builtin_workflow_branch(self):
        """Detectors run before the built-in on_user_turn branch."""

        class _WorkflowLike(_MyFrameworkAgent):
            async def on_user_turn(self, text: str) -> str:
                return text

        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            lambda obj: _MyFrameworkBridge(obj),
        )

        adapted = auto_adapt_agent(_WorkflowLike())
        assert isinstance(adapted, _MyFrameworkBridge)

    def test_detectors_consulted_in_registration_order(self):
        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            lambda obj: ("first", obj),
        )
        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            lambda obj: ("second", obj),
        )

        adapted = auto_adapt_agent(_MyFrameworkAgent())
        assert adapted[0] == "first"

    def test_clear_agent_detectors_removes_registrations(self):
        register_agent_detector(lambda obj: True, lambda obj: _MyFrameworkBridge(obj))
        clear_agent_detectors()

        class _PlainAgent:
            async def run(self, text: str) -> str:
                return text

        plain = _PlainAgent()
        assert auto_adapt_agent(plain) is plain
