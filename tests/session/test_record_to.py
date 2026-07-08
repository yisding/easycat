"""Tests for ``EasyConfig(record_to=...)`` auto-capture."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from easycat.audio_format import AudioChunk
from easycat.config import (
    EasyConfig,
    TextSessionConfig,
    create_session,
    create_text_session,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.session._types import SessionConfig


class _DummyAgent:
    async def run(self, text: str) -> str:
        return text


class _CustomSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return False

    async def end_stream(self) -> None:
        pass

    async def events(self):
        if False:
            yield None

    def version_info(self) -> dict[str, str]:
        return {"provider": "custom-stt"}


class _CustomTTS:
    supports_ssml = False

    async def synthesize(self, payload):
        if False:
            yield None

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {"provider": "custom-tts"}


class _CustomVAD:
    def configure(self, **kwargs) -> None:
        pass

    async def process(self, chunk: AudioChunk):
        if False:
            yield None

    def version_info(self) -> dict[str, str]:
        return {"provider": "custom-vad"}


class _CustomTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self):
        if False:
            yield None

    async def send_audio(self, chunk: AudioChunk) -> bool:
        return True

    def version_info(self) -> dict[str, str]:
        return {"provider": "custom-transport"}


def _bundles(path: Path, session_id: str) -> list[Path]:
    return list(path.glob(f"{session_id}-*.zip"))


@pytest.fixture
def _restore_easycat_logger():
    logger = logging.getLogger("easycat")
    handlers = logger.handlers[:]
    level = logger.level
    propagate = logger.propagate
    try:
        yield
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.mark.asyncio
async def test_record_to_exports_on_stop(tmp_path: Path) -> None:
    session = create_text_session(agent=None, debug="light", record_to=tmp_path)

    await session.stop()

    bundles = _bundles(tmp_path, session.session_id)
    assert len(bundles) == 1
    exported_path = bundles[0]
    assert exported_path.parent == tmp_path
    # Timestamp suffix keeps multiple runs from colliding in one dir.
    assert session.session_id in exported_path.name
    assert exported_path.name.endswith(".zip")


@pytest.mark.asyncio
async def test_record_to_forwards_force_flag(tmp_path: Path) -> None:
    session = create_text_session(agent=None, debug="light", record_to=tmp_path)

    await session.stop(force=True)

    assert len(_bundles(tmp_path, session.session_id)) == 1


@pytest.mark.asyncio
async def test_record_to_exports_on_shutdown(tmp_path: Path) -> None:
    session = create_text_session(agent=None, debug="full", record_to=tmp_path)

    await session.stop(force=True)
    assert len(_bundles(tmp_path, session.session_id)) == 1


@pytest.mark.asyncio
async def test_record_to_exports_via_async_with(tmp_path: Path) -> None:
    """``async with session:`` exits through stop(force=True) and must record."""
    session = create_text_session(agent=None, debug="light", record_to=tmp_path)

    async with session:
        pass

    assert len(_bundles(tmp_path, session.session_id)) == 1


@pytest.mark.asyncio
async def test_record_to_is_noop_when_debug_off(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    _restore_easycat_logger,
) -> None:
    """With debug='off' there is no journal, so recording is skipped."""
    target = tmp_path / "runs"
    logging.getLogger("easycat").propagate = True
    with caplog.at_level(logging.WARNING, logger="easycat.session._session"):
        session = create_text_session(agent=None, debug="off", record_to=target)

    await session.stop()
    assert not target.exists()
    assert "debug journaling is disabled" in caplog.text


@pytest.mark.asyncio
async def test_record_to_creates_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "recordings"
    assert not target.exists()

    session = create_text_session(agent=None, debug="light", record_to=target)
    await session.stop()

    assert target.exists()
    assert len(_bundles(target, session.session_id)) == 1


@pytest.mark.asyncio
async def test_record_to_export_failure_does_not_mask_shutdown(
    tmp_path: Path,
) -> None:
    """If the export raises, teardown must still complete normally."""

    session = create_text_session(agent=None, debug="light", record_to=tmp_path)

    def _fail_export(path: str) -> None:
        raise RuntimeError("bundle write failed")

    session.export_debug_bundle = _fail_export  # type: ignore[method-assign]

    # Should not raise — record_to export failures do not mask teardown.
    await session.stop(force=True)


def test_record_to_keeps_session_teardown_methods_unpatched(tmp_path: Path) -> None:
    session = create_text_session(agent=None, debug="light", record_to=tmp_path)

    assert "stop" not in vars(session)
    assert "shutdown" not in vars(session)


def test_record_to_field_accepts_path_and_str() -> None:
    """Session configs accept both str and Path for record_to."""
    # EasyConfig is construction-only here because create_session needs
    # providers; the text-session path has an integration test below.
    cfg1 = EasyConfig(openai_api_key="sk-stub", record_to="/tmp/a")
    cfg2 = EasyConfig(openai_api_key="sk-stub", record_to=Path("/tmp/b"))
    cfg3 = TextSessionConfig(record_to="/tmp/c")
    cfg4 = TextSessionConfig(record_to=Path("/tmp/d"))
    assert cfg1.record_to == "/tmp/a"
    assert cfg2.record_to == Path("/tmp/b")
    assert cfg3.record_to == "/tmp/c"
    assert cfg4.record_to == Path("/tmp/d")


@pytest.mark.asyncio
async def test_text_session_record_to_exports_on_stop(tmp_path: Path) -> None:
    session = create_text_session(agent=None, debug="light", record_to=tmp_path)

    await session.stop()

    assert len(_bundles(tmp_path, session.session_id)) == 1


@pytest.mark.asyncio
async def test_easyconfig_record_to_exports_on_stop(
    tmp_path: Path,
    _restore_easycat_logger: None,
) -> None:
    session = create_session(
        EasyConfig(
            stt=_CustomSTT(),
            tts=_CustomTTS(),
            vad=_CustomVAD(),
            transport=_CustomTransport(),
            agent=_DummyAgent(),
            debug="light",
            record_to=tmp_path,
        )
    )

    await session.stop()

    assert len(_bundles(tmp_path, session.session_id)) == 1


@pytest.mark.asyncio
async def test_record_to_sanitizes_low_level_session_id_path_components(
    tmp_path: Path,
) -> None:
    target = tmp_path / "intended"
    escape = tmp_path / "escape"
    session = Session(
        SessionConfig(
            journal=InMemoryRingBuffer(),
            record_to=target,
            session_id="../escape/owned",
            runtime_mode="text_session",
        )
    )

    await session.stop()

    bundles = list(target.glob("__-escape-owned-*.zip"))
    assert len(bundles) == 1
    assert bundles[0].parent == target
    assert not escape.exists()


@pytest.mark.asyncio
async def test_record_to_sanitizes_low_level_absolute_session_id(
    tmp_path: Path,
) -> None:
    target = tmp_path / "intended"
    absolute_session_id = str(tmp_path / "escape" / "owned")
    session = Session(
        SessionConfig(
            journal=InMemoryRingBuffer(),
            record_to=target,
            session_id=absolute_session_id,
            runtime_mode="text_session",
        )
    )

    await session.stop()

    bundles = list(target.glob("*-escape-owned-*.zip"))
    assert len(bundles) == 1
    assert bundles[0].parent == target
    assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_record_to_sanitizes_low_level_windows_drive_session_id(
    tmp_path: Path,
) -> None:
    target = tmp_path / "intended"
    session = Session(
        SessionConfig(
            journal=InMemoryRingBuffer(),
            record_to=target,
            session_id=r"C:\escape\owned",
            runtime_mode="text_session",
        )
    )

    await session.stop()

    bundles = list(target.glob("C-escape-owned-*.zip"))
    assert len(bundles) == 1
    assert bundles[0].parent == target
    assert ":" not in bundles[0].name
