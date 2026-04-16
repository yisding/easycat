"""AC2.16: COMMITTABLE_BOUNDARIES published per bridge."""

from __future__ import annotations

import pytest

from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge


@pytest.mark.parametrize(
    "bridge_cls",
    [OpenAIAgentsBridge, PydanticAIBridge, GenericWorkflowBridge],
    ids=["openai", "pydantic_ai", "generic"],
)
class TestCommittableBoundaries:
    def test_mapping_present(self, bridge_cls):
        assert hasattr(bridge_cls, "COMMITTABLE_BOUNDARIES")

    def test_mapping_non_empty(self, bridge_cls):
        assert len(bridge_cls.COMMITTABLE_BOUNDARIES) > 0

    def test_mapping_values_are_commit_rules(self, bridge_cls):
        from easycat.integrations.agents.base import CommitRule

        for rule in bridge_cls.COMMITTABLE_BOUNDARIES.values():
            assert isinstance(rule, CommitRule)
