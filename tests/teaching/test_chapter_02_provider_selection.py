"""Keep Chapter 2's documented STT swap executable and self-describing."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from easycat.debug.testing import load_bundle
from easycat.events import STTEvent, STTEventType

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "02-transcribe"


def _load_streaming():
    path = CHAPTER / "streaming.py"
    spec = importlib.util.spec_from_file_location("teaching_ch02_streaming", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    def __init__(self, _config) -> None:
        self.events: list[str] = []

    async def connect(self) -> None:
        self.events.append("connect")

    async def receive_audio(self):
        yield object()

    async def disconnect(self) -> None:
        self.events.append("disconnect")


class FakeSTT:
    def __init__(self) -> None:
        self.events_seen: list[str] = []
        self.ended = False
        self.end_event = asyncio.Event()

    async def start_stream(self) -> None:
        self.events_seen.append("start")

    async def send_audio(self, _chunk) -> None:
        self.events_seen.append("send")

    async def end_stream(self) -> None:
        if not self.ended:
            self.events_seen.append("end")
            self.ended = True
            self.end_event.set()

    async def events(self):
        await self.end_event.wait()
        yield STTEvent(type=STTEventType.FINAL, text="provider-neutral transcript")

    async def close(self) -> None:
        self.events_seen.append("close")


@pytest.mark.parametrize(
    ("provider", "env_var", "timing", "target_rate"),
    [
        ("openai", "OPENAI_API_KEY", "after_stream_end", None),
        ("deepgram", "DEEPGRAM_API_KEY", "during_audio", 24_000),
    ],
)
@pytest.mark.asyncio
async def test_streaming_selector_drives_factory_and_bundle(
    provider: str,
    env_var: str,
    timing: str,
    target_rate: int | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chapter = _load_streaming()
    secret = f"secret-for-{provider}"
    monkeypatch.setenv(env_var, secret)
    chapter.RUNS_DIR = tmp_path / "runs"

    configs = []
    fake_stt = FakeSTT()

    def create_stt(config):
        configs.append(config)
        return fake_stt

    transports: list[FakeTransport] = []

    def create_transport(config):
        transport = FakeTransport(config)
        transports.append(transport)
        return transport

    monkeypatch.setattr(chapter, "create_stt_provider", create_stt)
    monkeypatch.setattr(chapter, "LocalTransport", create_transport)

    await chapter.main(provider)

    assert len(configs) == 1
    config = configs[0]
    assert config.provider == provider
    assert config.api_key == secret
    if provider == "openai":
        assert config.params is None
    else:
        assert config.params["sample_rate"] == 24_000
        assert config.params["event_bus"] is not None

    assert transports[0].events == ["connect", "disconnect"]
    assert fake_stt.events_seen == ["start", "send", "end", "close"]

    bundles = list(chapter.RUNS_DIR.glob(f"ch02-streaming-{provider}-*.bundle"))
    assert len(bundles) == 1
    bundle = bundles[0]
    records = list(load_bundle(bundle).records())
    selected = next(record for record in records if record["name"] == "stt.provider.selected")
    assert selected["data"] == {
        "credential_env": env_var,
        "event_timing": timing,
        "input_sample_rate_hz": 24_000,
        "provider": provider,
        "provider_target_sample_rate_hz": target_rate,
    }
    assert any(record["name"] == "stt.final" for record in records)
    assert secret not in json.dumps(records)


@pytest.mark.parametrize(
    ("provider", "env_var"),
    [("openai", "OPENAI_API_KEY"), ("deepgram", "DEEPGRAM_API_KEY")],
)
def test_selector_names_the_missing_provider_credential(
    provider: str,
    env_var: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter = _load_streaming()
    monkeypatch.delenv(env_var, raising=False)

    with pytest.raises(SystemExit, match=env_var):
        chapter.build_stt_config(provider)


def test_streaming_cli_exposes_both_providers() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "streaming.py"), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--provider {openai,deepgram}" in completed.stdout
