"""Tests for legacy removal: __all__ hygiene and removed symbols."""

from __future__ import annotations

# ── Removed symbols not present in narrowed form ────────────


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


# ── __all__ doesn't expose removed types ─────────────────────


class TestAllDoesNotExposeRemoved:
    """Verify that easycat.__all__ does not expose types scheduled for removal."""

    def test_no_event_trace_logger_in_all(self):
        import easycat

        assert "EventTraceLogger" not in easycat.__all__

    def test_no_span_manager_in_all(self):
        import easycat

        assert "SpanManager" not in easycat.__all__


# ── __all__ contains expected allowlist ──────────────────────


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
        # Agent types
        "AgentRunner",
        "AgentStreamEvent",
        "AgentStreamEventType",
        "StreamingAgent",
        "BaseAgentAdapter",
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


# ── New canonical locations are importable ───────────────────


class TestNewLocations:
    """Verify that the new canonical import paths work."""

    def test_agent_runner_importable(self):
        from easycat.integrations.agents._agent_runner import AgentRunner

        assert AgentRunner is not None

    def test_legacy_types_importable(self):
        from easycat.integrations.agents._legacy_types import (
            AgentStreamEvent,
            AgentStreamEventType,
        )

        assert AgentStreamEvent is not None
        assert AgentStreamEventType is not None

    def test_base_adapter_importable(self):
        from easycat.integrations.agents._base_adapter import BaseAgentAdapter

        assert BaseAgentAdapter is not None

    def test_factory_importable(self):
        from easycat.integrations.agents._factory import auto_adapt_agent

        assert auto_adapt_agent is not None
