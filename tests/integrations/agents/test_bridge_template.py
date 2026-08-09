"""BridgeTemplate starter base class and register_agent_detector hook."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from collections.abc import Set as AbstractSet
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
from easycat.integrations.agents._state_serialization import serialize_framework_state
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    BridgeInputError,
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

    async def test_invoke_works_with_contract_kit_recording_recorder(self):
        """The contract kit's ``RecordingAgentRecorder`` must satisfy the
        ``turn_cursor`` surface ``BridgeTemplate.invoke()`` depends on, with
        the same clean-commit / error ordering as the journal recorder."""
        from easycat.testing import RecordingAgentRecorder

        recorder = RecordingAgentRecorder()
        bridge = _MinimalBridge()
        kinds = [
            ev.kind
            async for ev in bridge.invoke(AgentTurnInput.from_text("hello world"), recorder)
        ]
        assert kinds == ["text_delta", "text_delta", "done"]
        assert recorder.kinds() == ["unit_entered", "unit_exited"]
        exited_cursor = recorder.records[-1][1][0]
        assert exited_cursor.committable is True

        recorder = RecordingAgentRecorder()
        with pytest.raises(RuntimeError, match="framework exploded"):
            async for _ in _FailingBridge().invoke(AgentTurnInput.from_text("hi"), recorder):
                pass
        assert recorder.kinds() == ["unit_entered", "framework_error", "unit_exited"]
        assert recorder.records[-1][2]["reason"] == "error"

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

    def test_serialized_state_recursively_scrubs_nested_secret_fields(self):
        class _NestedSecretBridge(_MinimalBridge):
            def snapshot_state(self):
                return FrameworkStateSnapshot(
                    fields={
                        "history_len": 2,
                        "author": "Ada",
                        "authoredAt": "2026-08-06",
                        "monkey": "banana",
                        "keyboard_layout": "qwerty",
                        "keyboardLayout": "dvorak",
                        "authConfig": {"mode": "oauth"},
                        "api_version": "v2",
                        "apiVersion": "v1",
                        "api_key": "TOP_LEVEL_DROPPED",
                        "config": {"api_key": "NESTED_KEY_LEAK", "safe": "ok"},
                        "auth_config": {"auth": "NESTED_AUTH_FIELD_LEAK"},
                        "camel_credentials": {
                            "authToken": "AUTH_TOKEN_CAMEL_LEAK",
                            "apiToken": "API_TOKEN_CAMEL_LEAK",
                            "apiSecret": "API_SECRET_CAMEL_LEAK",
                            "tokenValue": "TOKEN_VALUE_CAMEL_LEAK",
                            "authHeader": "AUTH_HEADER_CAMEL_LEAK",
                            "apiHeader": "API_HEADER_CAMEL_LEAK",
                            "authValue": "AUTH_VALUE_CAMEL_LEAK",
                            "apiValue": "API_VALUE_CAMEL_LEAK",
                        },
                        "headers": {"authorization": "Bearer NESTED_AUTH_LEAK"},
                        "metadata": {"label": "sk-abcdefghijklmnop"},
                        "lookup": {"sk-abcdefghijklmnop": "safe value"},
                        "opaque": {("api_key", "OPAQUE_SECRET_LEAK")},
                        "association_list": [("api_key", "LIST_SECRET_LEAK")],
                        "association_tuple": ("token", "TUPLE_SECRET_LEAK"),
                        "auth_association": ("auth", "AUTH_ASSOCIATION_LEAK"),
                        "camel_associations": [
                            ("authToken", "AUTH_TOKEN_ASSOCIATION_LEAK"),
                            ("apiToken", "API_TOKEN_ASSOCIATION_LEAK"),
                            ("apiSecret", "API_SECRET_ASSOCIATION_LEAK"),
                            ("tokenValue", "TOKEN_VALUE_ASSOCIATION_LEAK"),
                            ("authHeader", "AUTH_HEADER_ASSOCIATION_LEAK"),
                            ("apiHeader", "API_HEADER_ASSOCIATION_LEAK"),
                            ("authValue", "AUTH_VALUE_ASSOCIATION_LEAK"),
                            ("apiValue", "API_VALUE_ASSOCIATION_LEAK"),
                        ],
                        "ordinary_pair": ("role", "assistant"),
                        "ordinary_pairs": [("monkey", "banana")],
                        "messages": [{"role": "system", "token": "NESTED_TOKEN_LEAK"}],
                    },
                    kind="minimal",
                )

        payload = _NestedSecretBridge()._serialize_framework_state().decode()
        state = json.loads(payload)

        assert "api_key" not in state
        assert state["author"] == "Ada"
        assert state["authoredAt"] == "2026-08-06"
        assert state["monkey"] == "banana"
        assert state["keyboard_layout"] == "qwerty"
        assert state["keyboardLayout"] == "dvorak"
        assert state["authConfig"] == {"mode": "oauth"}
        assert state["api_version"] == "v2"
        assert state["apiVersion"] == "v1"
        assert state["config"] == {"api_key": "[REDACTED_SECRET]", "safe": "ok"}
        assert state["auth_config"] == {"auth": "[REDACTED_SECRET]"}
        assert state["camel_credentials"] == {
            "authToken": "[REDACTED_SECRET]",
            "apiToken": "[REDACTED_SECRET]",
            "apiSecret": "[REDACTED_SECRET]",
            "tokenValue": "[REDACTED_SECRET]",
            "authHeader": "[REDACTED_SECRET]",
            "apiHeader": "[REDACTED_SECRET]",
            "authValue": "[REDACTED_SECRET]",
            "apiValue": "[REDACTED_SECRET]",
        }
        assert state["headers"] == {"authorization": "[REDACTED_SECRET]"}
        assert state["metadata"] == {"label": "[REDACTED_SECRET]"}
        assert state["lookup"] == {}
        assert state["opaque"] == [{"api_key": "[REDACTED_SECRET]"}]
        assert state["association_list"] == [{"api_key": "[REDACTED_SECRET]"}]
        assert state["association_tuple"] == {"token": "[REDACTED_SECRET]"}
        assert state["auth_association"] == {"auth": "[REDACTED_SECRET]"}
        assert state["camel_associations"] == [
            {"authToken": "[REDACTED_SECRET]"},
            {"apiToken": "[REDACTED_SECRET]"},
            {"apiSecret": "[REDACTED_SECRET]"},
            {"tokenValue": "[REDACTED_SECRET]"},
            {"authHeader": "[REDACTED_SECRET]"},
            {"apiHeader": "[REDACTED_SECRET]"},
            {"authValue": "[REDACTED_SECRET]"},
            {"apiValue": "[REDACTED_SECRET]"},
        ]
        assert state["ordinary_pair"] == ["role", "assistant"]
        assert state["ordinary_pairs"] == [["monkey", "banana"]]
        assert state["messages"] == [{"role": "system", "token": "[REDACTED_SECRET]"}]
        for secret in (
            "TOP_LEVEL_DROPPED",
            "NESTED_KEY_LEAK",
            "NESTED_AUTH_LEAK",
            "sk-abcdefghijklmnop",
            "NESTED_TOKEN_LEAK",
            "OPAQUE_SECRET_LEAK",
            "LIST_SECRET_LEAK",
            "TUPLE_SECRET_LEAK",
            "NESTED_AUTH_FIELD_LEAK",
            "AUTH_ASSOCIATION_LEAK",
            "AUTH_TOKEN_CAMEL_LEAK",
            "API_TOKEN_CAMEL_LEAK",
            "API_SECRET_CAMEL_LEAK",
            "TOKEN_VALUE_CAMEL_LEAK",
            "AUTH_HEADER_CAMEL_LEAK",
            "API_HEADER_CAMEL_LEAK",
            "AUTH_VALUE_CAMEL_LEAK",
            "API_VALUE_CAMEL_LEAK",
            "AUTH_TOKEN_ASSOCIATION_LEAK",
            "API_TOKEN_ASSOCIATION_LEAK",
            "API_SECRET_ASSOCIATION_LEAK",
            "TOKEN_VALUE_ASSOCIATION_LEAK",
            "AUTH_HEADER_ASSOCIATION_LEAK",
            "API_HEADER_ASSOCIATION_LEAK",
            "AUTH_VALUE_ASSOCIATION_LEAK",
            "API_VALUE_ASSOCIATION_LEAK",
        ):
            assert secret not in payload

    def test_serialized_state_drops_opaque_mapping_keys_without_stringifying_them(self):
        class _OpaqueKey:
            str_called = False

            def __hash__(self) -> int:
                return 1

            def __str__(self) -> str:
                type(self).str_called = True
                return "OPAQUE_MAPPING_KEY_SECRET_LEAK"

        payload = serialize_framework_state({"state": {_OpaqueKey(): "safe"}}).decode()

        assert json.loads(payload) == {"state": {}}
        assert _OpaqueKey.str_called is False
        assert "OPAQUE_MAPPING_KEY_SECRET_LEAK" not in payload

    def test_serialized_state_canonicalizes_mapping_and_set_order(self):
        class _OrderedSet(AbstractSet[str]):
            def __init__(self, items: list[str]) -> None:
                self._items = items

            def __contains__(self, item: object) -> bool:
                return item in self._items

            def __iter__(self) -> Iterator[str]:
                return iter(self._items)

            def __len__(self) -> int:
                return len(self._items)

        first = {
            "state": {"z": 2, "a": 1},
            "members": _OrderedSet(["charlie", "alpha", "bravo"]),
        }
        second = {
            "members": _OrderedSet(["bravo", "charlie", "alpha"]),
            "state": {"a": 1, "z": 2},
        }

        assert serialize_framework_state(first) == serialize_framework_state(second)

    def test_serialized_state_drops_primitive_subclass_keys_without_stringifying_them(self):
        class _LeakyStringKey(str):
            str_called = False

            def __str__(self) -> str:
                type(self).str_called = True
                return "STRING_KEY_SECRET_LEAK"

        class _LeakyIntegerKey(int):
            str_called = False

            def __str__(self) -> str:
                type(self).str_called = True
                return "INTEGER_KEY_SECRET_LEAK"

        payload = serialize_framework_state(
            {"state": {_LeakyStringKey("safe"): 1, _LeakyIntegerKey(1): 2}}
        ).decode()

        assert json.loads(payload) == {"state": {}}
        assert _LeakyStringKey.str_called is False
        assert _LeakyIntegerKey.str_called is False
        assert "STRING_KEY_SECRET_LEAK" not in payload
        assert "INTEGER_KEY_SECRET_LEAK" not in payload

    def test_serialized_state_fails_closed_for_primitive_subclass_association_key(self):
        class _DisguisedSecretKey(str):
            str_called = False

            def __str__(self) -> str:
                type(self).str_called = True
                return "role"

        payload = serialize_framework_state(
            {"pairs": [(_DisguisedSecretKey("api_key"), "ASSOCIATION_SECRET_LEAK")]}
        ).decode()

        assert json.loads(payload) == {"pairs": ["[UNSERIALIZABLE]"]}
        assert _DisguisedSecretKey.str_called is False
        assert "ASSOCIATION_SECRET_LEAK" not in payload

    def test_serialized_state_does_not_use_opaque_object_repr(self):
        class _LeakyOpaque:
            def __str__(self) -> str:
                return "api_key=UNPATTERNED_SECRET_LEAK"

        class _OpaqueBridge(_MinimalBridge):
            def snapshot_state(self):
                return FrameworkStateSnapshot(
                    fields={"opaque": _LeakyOpaque()},
                    kind="minimal",
                )

        payload = _OpaqueBridge()._serialize_framework_state().decode()

        assert json.loads(payload) == {"opaque": "[UNSERIALIZABLE]"}
        assert "UNPATTERNED_SECRET_LEAK" not in payload

    def test_serialized_state_never_emits_nonstandard_json_numbers(self):
        class _NonFiniteBridge(_MinimalBridge):
            def snapshot_state(self):
                return FrameworkStateSnapshot(
                    fields={"latency": float("nan"), "limit": float("inf")},
                    kind="minimal",
                )

        assert _NonFiniteBridge()._serialize_framework_state() == b"{}"

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
            lambda obj: _MyFrameworkBridge(obj),
        )
        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            lambda obj: _MinimalBridge(),
        )

        adapted = auto_adapt_agent(_MyFrameworkAgent())
        assert isinstance(adapted, _MyFrameworkBridge)

    def test_detector_rejects_non_bridge_factory_result(self):
        def invalid_factory(obj: Any) -> ExternalAgentBridge:
            return ("invalid", obj)  # type: ignore[return-value]

        register_agent_detector(
            lambda obj: isinstance(obj, _MyFrameworkAgent),
            invalid_factory,
        )

        with pytest.raises(
            BridgeInputError,
            match="bridge_factory must return an ExternalAgentBridge; got tuple",
        ):
            auto_adapt_agent(_MyFrameworkAgent())

    def test_clear_agent_detectors_removes_registrations(self):
        register_agent_detector(lambda obj: True, lambda obj: _MyFrameworkBridge(obj))
        clear_agent_detectors()

        class _PlainAgent:
            async def run(self, text: str) -> str:
                return text

        plain = _PlainAgent()
        assert auto_adapt_agent(plain) is plain
