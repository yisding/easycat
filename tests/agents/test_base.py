"""Tests for BaseAgentAdapter shared functionality."""

from __future__ import annotations

import json

import pytest

from easycat.integrations.agents._base_adapter import BaseAgentAdapter, serialize_output


class ConcreteAdapter(BaseAgentAdapter):
    """Minimal subclass for testing base behaviour."""

    async def run(self, text: str) -> str:
        return f"echo: {text}"


# ── History management ────────────────────────────────────────────


def test_initial_history_is_empty():
    adapter = ConcreteAdapter()
    assert adapter.message_history == []


def test_clear_history():
    adapter = ConcreteAdapter()
    adapter._message_history = [{"role": "user", "content": "hi"}]
    assert len(adapter.message_history) == 1

    adapter.clear_history()
    assert adapter.message_history == []


def test_message_history_returns_copy():
    adapter = ConcreteAdapter()
    adapter._message_history = [{"role": "user", "content": "hi"}]
    copy = adapter.message_history
    copy.append({"role": "assistant", "content": "bye"})
    assert len(adapter.message_history) == 1  # original unchanged


# ── Subclass contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_not_implemented():
    adapter = BaseAgentAdapter()
    with pytest.raises(NotImplementedError):
        await adapter.run("test")


@pytest.mark.asyncio
async def test_run_streaming_not_implemented():
    adapter = BaseAgentAdapter()
    with pytest.raises(NotImplementedError):
        async for _ in adapter.run_streaming("test"):
            pass


# ── serialize_output tests ───────────────────────────────────────


class TestSerializeOutput:
    def test_string_passthrough(self):
        assert serialize_output("hello world") == "hello world"

    def test_empty_string(self):
        assert serialize_output("") == ""

    def test_pydantic_v2_model(self):
        """Objects with model_dump_json() should use it."""

        class FakeModelV2:
            def model_dump_json(self):
                return '{"name":"Alice","age":30}'

        result = serialize_output(FakeModelV2())
        assert result == '{"name":"Alice","age":30}'

    def test_pydantic_v1_model(self):
        """Objects with json() method should use it."""

        class FakeModelV1:
            def json(self):
                return '{"name":"Bob","age":25}'

        result = serialize_output(FakeModelV1())
        assert result == '{"name":"Bob","age":25}'

    def test_pydantic_v2_takes_precedence(self):
        """When both model_dump_json and json exist, prefer v2."""

        class FakeModelBoth:
            def model_dump_json(self):
                return '{"version":"v2"}'

            def json(self):
                return '{"version":"v1"}'

        result = serialize_output(FakeModelBoth())
        assert result == '{"version":"v2"}'

    def test_dict_to_json(self):
        result = serialize_output({"key": "value", "num": 42})
        parsed = json.loads(result)
        assert parsed == {"key": "value", "num": 42}

    def test_list_to_json(self):
        result = serialize_output([1, "two", 3])
        parsed = json.loads(result)
        assert parsed == [1, "two", 3]

    def test_integer_fallback(self):
        assert serialize_output(42) == "42"

    def test_none_fallback(self):
        assert serialize_output(None) == "None"


# ── output_type property tests ───────────────────────────────────


class TestOutputType:
    def test_no_agent_returns_none(self):
        adapter = BaseAgentAdapter()
        assert adapter.output_type is None

    def test_always_returns_none_for_stub(self):
        """After legacy removal, output_type is always None on the stub."""
        adapter = ConcreteAdapter()
        adapter._agent = object()
        assert adapter.output_type is None


# ── last_output property tests ───────────────────────────────────


# ── done_structured_output helper tests ───────────────────────────


class TestDoneStructuredOutput:
    def test_plain_string_without_output_type_returns_none(self):
        adapter = ConcreteAdapter()
        assert adapter.done_structured_output("hello") is None

    def test_non_string_without_output_type_is_preserved(self):
        adapter = ConcreteAdapter()
        value = {"ok": True}
        assert adapter.done_structured_output(value) == value

    def test_string_without_output_type_returns_none(self):
        """With stub adapter, output_type is always None, so string returns None."""
        adapter = ConcreteAdapter()
        assert adapter.done_structured_output("raw") is None


class TestLastOutput:
    def test_initially_none(self):
        adapter = ConcreteAdapter()
        assert adapter.last_output is None

    def test_set_and_read(self):
        adapter = ConcreteAdapter()
        adapter._last_output = {"key": "value"}
        assert adapter.last_output == {"key": "value"}

    def test_cleared_on_clear_history(self):
        adapter = ConcreteAdapter()
        adapter._last_output = "some output"
        adapter.clear_history()
        assert adapter.last_output is None
