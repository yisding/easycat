"""Tests for WS5 legacy removal: deprecation warnings, shim re-exports, and __all__ hygiene."""

from __future__ import annotations

import importlib
import sys
import warnings

import pytest

# ── AC5.2: Deprecation warnings emitted on import ──────────────────


class TestDeprecationWarnings:
    """Legacy modules emit DeprecationWarning at import time."""

    @pytest.fixture(autouse=True)
    def _clear_module_cache(self):
        """Remove cached legacy modules so re-import triggers warnings."""
        modules_to_clear = [
            "easycat.event_logging",
            "easycat.tracing",
            "easycat.metrics",
            "easycat._span_manager",
            "easycat.agent_runner",
            "easycat.agents",
            "easycat.agents.base",
            "easycat.agents.openai_agents",
            "easycat.agents.pydantic_ai",
            "easycat.agents.pydantic_ai_workflow",
            "easycat.agents.factory",
        ]
        saved = {}
        for mod in modules_to_clear:
            if mod in sys.modules:
                saved[mod] = sys.modules.pop(mod)
        yield
        # Restore to avoid polluting other tests.
        for mod, obj in saved.items():
            sys.modules[mod] = obj

    def test_event_logging_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.event_logging")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.event_logging" in str(x.message) for x in deprecation_warnings)

    def test_tracing_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.tracing")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.tracing" in str(x.message) for x in deprecation_warnings)

    def test_metrics_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.metrics")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.metrics" in str(x.message) for x in deprecation_warnings)

    def test_span_manager_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat._span_manager")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat._span_manager" in str(x.message) for x in deprecation_warnings)

    def test_agent_runner_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.agent_runner")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.agent_runner" in str(x.message) for x in deprecation_warnings)

    def test_agents_package_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.agents")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.agents" in str(x.message) for x in deprecation_warnings)

    def test_agents_base_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.agents.base")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.agents.base" in str(x.message) for x in deprecation_warnings)

    def test_agents_openai_agents_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.agents.openai_agents")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.agents.openai_agents" in str(x.message) for x in deprecation_warnings)

    def test_agents_pydantic_ai_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.agents.pydantic_ai")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.agents.pydantic_ai" in str(x.message) for x in deprecation_warnings)

    def test_agents_factory_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.agents.factory")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any("easycat.agents.factory" in str(x.message) for x in deprecation_warnings)

    def test_agents_pydantic_ai_workflow_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            importlib.import_module("easycat.agents.pydantic_ai_workflow")
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert any(
            "easycat.agents.pydantic_ai_workflow" in str(x.message) for x in deprecation_warnings
        )


# ── AC5.5: Removed symbols not present in narrowed form ────────────


class TestRemovedSymbols:
    """Symbols scheduled for removal are not exposed at the top level."""

    def test_no_dual_write_flag_in_source(self):
        """EASYCAT_LEGACY_OBS_DUAL_WRITE references removed from prod code."""
        import pathlib

        src_dir = pathlib.Path(__file__).resolve().parent.parent / "src" / "easycat"
        hits = []
        for py_file in src_dir.rglob("*.py"):
            # Skip plan files, migration docs, and test files.
            rel = str(py_file.relative_to(src_dir))
            if "plan/" in rel or "test" in rel:
                continue
            text = py_file.read_text()
            if "EASYCAT_LEGACY_OBS_DUAL_WRITE" in text:
                hits.append(rel)
        assert hits == [], f"EASYCAT_LEGACY_OBS_DUAL_WRITE still referenced in: {hits}"


# ── AC5.14: easycat.__all__ doesn't expose removed types ───────────


class TestAllDoesNotExposeRemoved:
    """Verify that easycat.__all__ does not expose types scheduled for removal."""

    def test_no_event_trace_logger_in_all(self):
        import easycat

        assert "EventTraceLogger" not in easycat.__all__

    def test_no_span_manager_in_all(self):
        import easycat

        assert "SpanManager" not in easycat.__all__


# ── AC5.15: easycat.__all__ contains expected allowlist ─────────────


class TestAllContainsExpected:
    """Verify that easycat.__all__ contains the expected public API symbols."""

    EXPECTED_SYMBOLS = [
        # Core session
        "Session",
        "SessionConfig",
        "TurnState",
        "TurnMode",
        "CancelToken",
        "EventBus",
        "Event",
        # Agent types (still re-exported for backwards compat)
        "AgentRunner",
        "AgentStreamEvent",
        "AgentStreamEventType",
        "StreamingAgent",
        "BaseAgentAdapter",
        "OpenAIAgentsAdapter",
        "PydanticAIAdapter",
        # Journal runtime
        "ExecutionJournal",
        "JournalRecord",
        "JournalRecordKind",
        "JournalView",
        # Provider protocols
        "STTProvider",
        "TTSProvider",
        "VADProvider",
        "Transport",
        # Audio
        "AudioChunk",
        "AudioFormat",
    ]

    def test_expected_symbols_present(self):
        import easycat

        missing = [s for s in self.EXPECTED_SYMBOLS if s not in easycat.__all__]
        assert missing == [], f"Missing from __all__: {missing}"


# ── Shim re-exports still work ──────────────────────────────────────


class TestShimReExports:
    """Importing from easycat.agents still works via the deprecation shim."""

    def test_base_agent_adapter_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.base import BaseAgentAdapter

        assert BaseAgentAdapter is not None

    def test_openai_agents_adapter_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.openai_agents import OpenAIAgentsAdapter

        assert OpenAIAgentsAdapter is not None

    def test_pydantic_ai_adapter_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.pydantic_ai import PydanticAIAdapter

        assert PydanticAIAdapter is not None

    def test_factory_auto_adapt_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.factory import auto_adapt_agent

        assert auto_adapt_agent is not None

    def test_workflow_adapter_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.pydantic_ai_workflow import (
                PydanticAIWorkflowAdapter,
                WorkflowTurnResult,
            )

        assert PydanticAIWorkflowAdapter is not None
        assert WorkflowTurnResult is not None

    def test_agents_package_all_symbols(self):
        """All symbols from agents.__all__ are importable."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import easycat.agents

        for name in easycat.agents.__all__:
            assert hasattr(easycat.agents, name), f"Missing from easycat.agents: {name}"

    def test_serialize_output_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.base import serialize_output

        assert callable(serialize_output)

    def test_build_openai_agents_adapter_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.openai_agents import build_openai_agents_adapter

        assert callable(build_openai_agents_adapter)

    def test_split_replacement_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agents.base import split_replacement_by_original_parts

        assert callable(split_replacement_by_original_parts)

    def test_agent_stream_event_importable(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from easycat.agent_runner import AgentStreamEvent, AgentStreamEventType

        assert AgentStreamEvent is not None
        assert AgentStreamEventType is not None
