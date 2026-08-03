"""Architecture contracts for debugger HTTP route groups."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.audio_format import AudioFormat
from easycat.debugger._core_routes import register_core_routes
from easycat.debugger._sources import DebuggerSource


class _RecordingRouter:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []

    def add_get(self, path: str, handler: Any) -> None:
        self.routes.append(("GET", path, handler))


def test_core_route_group_owns_read_only_source_endpoints() -> None:
    router = _RecordingRouter()
    app = SimpleNamespace(router=router)
    source = DebuggerSource(
        label="test",
        _records_fn=list,
        _progress_fn=lambda: (0, 0),
        _artifact_fn=lambda _ref: None,
        _manifest_fn=dict,
        _bundle_fn=None,
        _replay_fn=None,
        is_live=False,
    )

    register_core_routes(
        app,
        source,
        static_dir=Path("static"),
        web=SimpleNamespace(),
    )

    assert [(method, path) for method, path, _handler in router.routes] == [
        ("GET", "/"),
        ("GET", "/api/manifest"),
        ("GET", "/api/records"),
        ("GET", "/api/turns"),
        ("GET", "/api/timeline"),
        ("GET", "/api/transcript"),
        ("GET", "/api/issues"),
        ("GET", "/api/artifact/{ref}"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_vad_whatif_preserves_geometry_and_closes_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider_fails: bool,
) -> None:
    from easycat.debugger.server import _vad_whatif_for_turn
    from easycat.vad import factory as vad_factory

    class _Provider:
        def __init__(self) -> None:
            self.formats: list[AudioFormat] = []
            self.closed = False

        async def process(self, chunk):
            self.formats.append(chunk.format)
            if provider_fails:
                raise RuntimeError("VAD failed")
            if False:
                yield None

        def close(self) -> None:
            self.closed = True

    provider = _Provider()
    monkeypatch.setattr(vad_factory, "create_vad", lambda _config: provider)
    source = DebuggerSource(
        label="test",
        _records_fn=lambda: [
            {
                "sequence": 1,
                "name": "stage_start",
                "turn_id": "t1",
                "input_ref": "pcm",
                "data": {
                    "stage": "vad",
                    "sample_rate": 48_000,
                    "channels": 2,
                    "sample_width": 2,
                    "encoding": "pcm",
                },
            }
        ],
        _artifact_fn=lambda ref: b"\x00\x00\x00\x00" * 8 if ref == "pcm" else None,
        _manifest_fn=dict,
    )

    if provider_fails:
        with pytest.raises(RuntimeError, match="VAD failed"):
            await _vad_whatif_for_turn(source, "t1", threshold=0.5)
    else:
        await _vad_whatif_for_turn(source, "t1", threshold=0.5)

    assert provider.formats == [AudioFormat(sample_rate=48_000, channels=1, sample_width=2)]
    assert provider.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "sample_width"),
    [
        ("pcm", 1),
        ("mulaw", 2),
    ],
)
async def test_vad_whatif_rejects_same_non_pcm16_formats_as_live_stage(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    sample_width: int,
) -> None:
    from easycat.debugger.server import _vad_whatif_for_turn
    from easycat.vad import factory as vad_factory

    class _Provider:
        def __init__(self) -> None:
            self.called = False
            self.closed = False

        async def process(self, chunk):
            self.called = True
            if False:
                yield None

        def close(self) -> None:
            self.closed = True

    provider = _Provider()
    monkeypatch.setattr(vad_factory, "create_vad", lambda _config: provider)
    source = DebuggerSource(
        label="test",
        _records_fn=lambda: [
            {
                "sequence": 1,
                "name": "stage_start",
                "turn_id": "t1",
                "input_ref": "audio",
                "data": {
                    "stage": "vad",
                    "sample_rate": 8_000,
                    "channels": 2,
                    "sample_width": sample_width,
                    "encoding": encoding,
                },
            }
        ],
        _artifact_fn=lambda ref: b"\x10\xf0\x20\xe0" if ref == "audio" else None,
        _manifest_fn=dict,
    )

    with pytest.raises(RuntimeError, match="VAD what-if requires captured PCM16 audio"):
        await _vad_whatif_for_turn(source, "t1", threshold=0.5)

    assert provider.called is False
    assert provider.closed is True


@pytest.mark.asyncio
async def test_vad_whatif_owns_async_provider_close_through_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.debugger.server import _vad_whatif_for_turn
    from easycat.vad import factory as vad_factory

    class _AsyncClosingProvider:
        def __init__(self) -> None:
            self.process_started = asyncio.Event()
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_calls = 0
            self.closed = False

        async def process(self, chunk):
            _ = chunk
            self.process_started.set()
            await asyncio.Event().wait()
            if False:
                yield None

        async def aclose(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()
            self.closed = True

    provider = _AsyncClosingProvider()
    monkeypatch.setattr(vad_factory, "create_vad", lambda _config: provider)
    source = DebuggerSource(
        label="test",
        _records_fn=lambda: [
            {
                "sequence": 1,
                "name": "stage_start",
                "turn_id": "t1",
                "input_ref": "audio",
                "data": {
                    "stage": "vad",
                    "sample_rate": 8_000,
                    "channels": 1,
                    "sample_width": 2,
                    "encoding": "pcm",
                },
            }
        ],
        _artifact_fn=lambda ref: b"\x00\x00" if ref == "audio" else None,
        _manifest_fn=dict,
    )

    request_task = asyncio.create_task(_vad_whatif_for_turn(source, "t1", threshold=0.5))
    await provider.process_started.wait()
    request_task.cancel()
    await provider.close_started.wait()

    request_task.cancel()
    request_task.cancel()
    await asyncio.sleep(0)
    assert request_task.cancelling() == 3
    assert request_task.done() is False
    assert provider.close_calls == 1
    assert provider.closed is False

    provider.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert provider.close_calls == 1
    assert provider.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting_cancellation", [False, True])
async def test_vad_whatif_logs_internal_provider_close_cancel_without_cancelling_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    preexisting_cancellation: bool,
) -> None:
    from easycat.debugger.server import _vad_whatif_for_turn
    from easycat.vad import factory as vad_factory

    class _InternallyCancelledProvider:
        def __init__(self) -> None:
            self.close_calls = 0

        async def process(self, chunk):
            _ = chunk
            if False:
                yield None

        async def aclose(self) -> None:
            self.close_calls += 1
            raise asyncio.CancelledError

    provider = _InternallyCancelledProvider()
    monkeypatch.setattr(vad_factory, "create_vad", lambda _config: provider)
    source = DebuggerSource(
        label="test",
        _records_fn=lambda: [
            {
                "sequence": 1,
                "name": "stage_start",
                "turn_id": "t1",
                "input_ref": "audio",
                "data": {
                    "stage": "vad",
                    "sample_rate": 8_000,
                    "channels": 1,
                    "sample_width": 2,
                    "encoding": "pcm",
                },
            }
        ],
        _artifact_fn=lambda ref: b"\x00\x00" if ref == "audio" else None,
        _manifest_fn=dict,
    )

    async def run_request() -> tuple[dict[str, Any], int]:
        caller = asyncio.current_task()
        assert caller is not None
        if preexisting_cancellation:
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.sleep(0)
        result = await _vad_whatif_for_turn(source, "t1", threshold=0.5)
        return result, caller.cancelling()

    with caplog.at_level(logging.DEBUG, logger="easycat.debugger.server"):
        result, cancellation_requests = await asyncio.create_task(run_request())

    assert result["whatif_starts"] == 0
    assert cancellation_requests == int(preexisting_cancellation)
    assert provider.close_calls == 1
    assert "Failed to close debugger VAD what-if provider" in caplog.text
