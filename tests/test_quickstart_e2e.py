"""Documented happy-path smoke test.

Walks the canonical quickstart flow advertised in README:

    EasyConfig(...) -> create_session -> start -> one turn -> stop -> export bundle

If this test breaks, the README's quickstart is also broken — that is
the entire point of having it. It uses the existing scripted-provider
harness so it stays hermetic (no API keys, no network).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from easycat import (
    AgentFinal,
    EasyConfig,
    STTFinal,
    TTSAudio,
    create_session,
)

from .integration.harness import (
    EventCollector,
    QueueTransport,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    make_chunk,
    make_test_config,
    patch_provider_factories,
)


class EchoAgent:
    async def run(self, text: str) -> str:
        return f"You said: {text}"


@pytest.mark.integration_local
async def test_quickstart_happy_path_one_turn_with_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: build session, run one turn, export a debug bundle.

    Asserts the events the README leads users to expect (STTFinal,
    AgentFinal, TTSAudio) all fire in order, and that
    ``export_debug_bundle`` produces a readable zip.
    """
    transport = QueueTransport()
    stt = ScriptedSTT(["hello world"])
    tts = RecordingTTS(chunk_sizes=(640, 640))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = make_test_config(transport=transport, agent=EchoAgent())
    # Override debug=off default so export_debug_bundle has a journal.
    config = EasyConfig(
        stt=config.stt,
        tts=config.tts,
        transport=transport,
        agent=EchoAgent(),
        turn_taking=config.turn_taking,
        debug="light",
    )

    session = create_session(config)

    collector = EventCollector(session.event_bus)
    collector.subscribe(STTFinal, AgentFinal, TTSAudio)

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk(), make_chunk())
        stt_final = await collector.wait_for(STTFinal, timeout=3.0)
        agent_final = await collector.wait_for(AgentFinal, timeout=3.0)
        tts_audio = await collector.wait_for(TTSAudio, timeout=3.0)

        assert stt_final.text == "hello world"
        assert agent_final.text == "You said: hello world"
        assert tts_audio.chunk.data  # non-empty payload
    finally:
        await session.stop()

    bundle_path = tmp_path / "quickstart.zip"
    session.export_debug_bundle(str(bundle_path))

    assert bundle_path.exists(), "export_debug_bundle did not write a file"
    with zipfile.ZipFile(bundle_path) as zf:
        names = zf.namelist()
        assert "manifest.json" in names, f"bundle missing manifest: {names}"
        assert any(name.startswith("journal") for name in names), (
            f"bundle missing journal: {names}"
        )


async def test_quickstart_public_api_imports_resolve() -> None:
    """The literal imports the README quickstart uses must resolve.

    Catches drift between ``easycat.__init__`` lazy registrations and
    the symbols documented in README.md.
    """
    from easycat import (  # noqa: F401
        EasyConfig,
        Session,
        SessionConfig,
        create_session,
        create_text_session,
    )
    from easycat.integrations.agents import (  # noqa: F401
        AgentRunner,
        AgentRunnerConfig,
        OpenAIAgentsBridge,
        PydanticAIBridge,
        auto_adapt_agent,
    )
