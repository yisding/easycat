"""Shared fixtures for agent bridge integration tests."""

from __future__ import annotations

import sys
import types

import pytest

from ._llama_agents_bridge_support import (
    _FakeWorkflowBase,
    _HumanResponseEvent,
    _InputRequiredEvent,
    _StartEvent,
    _StopEvent,
)


@pytest.fixture
def fake_workflows_modules(monkeypatch):
    workflows = types.ModuleType("workflows")
    workflows.Workflow = _FakeWorkflowBase
    workflows.StartEvent = _StartEvent
    workflows.StopEvent = _StopEvent
    workflows.InputRequiredEvent = _InputRequiredEvent
    workflows.HumanResponseEvent = _HumanResponseEvent
    events = types.ModuleType("workflows.events")
    events.StartEvent = _StartEvent
    events.StopEvent = _StopEvent
    events.InputRequiredEvent = _InputRequiredEvent
    events.HumanResponseEvent = _HumanResponseEvent
    monkeypatch.setitem(sys.modules, "workflows", workflows)
    monkeypatch.setitem(sys.modules, "workflows.events", events)
