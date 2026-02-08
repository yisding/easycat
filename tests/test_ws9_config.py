from __future__ import annotations

import pytest

from easycat import EasyCatConfig, create_session
from easycat.agent_runner import AgentRunner
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.tts.openai_tts import OpenAITTSConfig


def test_easycat_config_requires_stt_tts():
    with pytest.raises(ValueError):
        EasyCatConfig()


def test_easycat_config_openai_defaults():
    config = EasyCatConfig(openai_api_key="test-key")
    assert isinstance(config.stt, OpenAISTTConfig)
    assert isinstance(config.tts, OpenAITTSConfig)


def test_easycat_config_wraps_agent():
    class DummyAgent:
        async def run(self, text: str) -> str:
            return text

    config = EasyCatConfig(openai_api_key="test-key", agent=DummyAgent())
    session = create_session(config)
    assert isinstance(session.agent, AgentRunner)
