"""Shared fixtures for end-to-end tests.

Voice fixtures: a session-scoped fixture that renders a handful of real
utterances via OpenAI TTS (16 kHz PCM16) and caches them on disk. Tests
that need voice audio depend on this fixture; it skips the whole test
suite if ``OPENAI_API_KEY`` isn't present.

Server helpers: ``start_ws_server`` and ``start_twilio_server`` spin up
a real localhost WebSocket server bound to a free port, wire a
session-factory callback into the handler, and return a handle the test
can connect to. The handler saves the constructed ``Session`` on the
handle so tests can inspect its ``journal`` after the turn, including
after a clean ``session.stop()``.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio

from easycat._audio_utils import resample

# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------


def _require_live() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required for live integration test")


# ---------------------------------------------------------------------------
# Voice fixtures (real TTS audio cached at session scope)
# ---------------------------------------------------------------------------


_FIXTURE_UTTERANCES: dict[str, str] = {
    "question": "What is the capital of France?",
    "greeting": "Hello, how are you today?",
    "short": "Yes.",
    "long": "Please tell me a short story about a robot who learned to paint.",
    "interrupt": "Stop talking right now please.",
    "numbers": "One two three four five six seven eight nine ten.",
}

_VOICE_FIXTURE_RENDER_TIMEOUT_S = float(
    os.environ.get("EASYCAT_E2E_VOICE_FIXTURE_TIMEOUT_S", "45")
)


@pytest.fixture(scope="session")
def voice_fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    return tmp_path_factory.mktemp("e2e_voice")


@pytest.fixture(scope="session")
def voice_fixtures(voice_fixtures_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """Render and cache a dict of utterance-name -> PCM16 16 kHz file path.

    Skips the test if ``OPENAI_API_KEY`` is absent.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required for voice_fixtures")

    from tests.e2e._audio import render_tts_pcm

    async def _render_all() -> dict[str, pathlib.Path]:
        out: dict[str, pathlib.Path] = {}
        for name, text in _FIXTURE_UTTERANCES.items():
            cache_path = voice_fixtures_dir / f"{name}.pcm16.16k"
            if not cache_path.exists():
                pcm24k = await render_tts_pcm(text)
                pcm16k = resample(pcm24k, 24000, 16000)
                cache_path.write_bytes(pcm16k)
            out[name] = cache_path
        return out

    try:
        return asyncio.run(
            asyncio.wait_for(
                _render_all(),
                timeout=_VOICE_FIXTURE_RENDER_TIMEOUT_S,
            )
        )
    except TimeoutError:
        pytest.skip(
            "OpenAI TTS voice fixture rendering timed out after "
            f"{_VOICE_FIXTURE_RENDER_TIMEOUT_S:.0f}s"
        )


# ---------------------------------------------------------------------------
# WebSocket and Twilio server handles
# ---------------------------------------------------------------------------


@dataclass
class WSServerHandle:
    port: int
    url: str
    session: Any = None  # set when the handler creates the Session
    exception: BaseException | None = None
    server: Any = None
    _task: asyncio.Task[None] | None = None


SessionBuilder = Callable[[Any], Awaitable[Any]]


def _bound_server_port(server: Any) -> int:
    sockets = getattr(server, "sockets", None)
    if not sockets:
        raise RuntimeError("websocket test server did not expose a bound socket")
    return int(sockets[0].getsockname()[1])


async def _start_ws_server(session_builder: SessionBuilder) -> WSServerHandle:
    """Start a real localhost ws:// server that invokes ``session_builder``
    for each incoming connection. The builder is given the websocket and
    should return (session, handler_coroutine). This helper runs
    ``session.start()`` and keeps the handler alive until the client
    disconnects."""
    import websockets

    handle = WSServerHandle(port=0, url="")

    sessions_to_stop: list[Any] = []
    handle._sessions_to_stop = sessions_to_stop  # type: ignore[attr-defined]

    async def on_connect(ws: Any) -> None:
        try:
            session = await session_builder(ws)
            handle.session = session
            sessions_to_stop.append(session)
            await session.start()
            # Keep the session running until the client closes so tests can
            # inspect pre-stop state if needed. Post-stop journal reads are
            # also supported; cleanup happens in the fixture teardown below.
            await ws.wait_closed()
        except BaseException as exc:
            handle.exception = exc
            raise

    server = await websockets.serve(on_connect, "127.0.0.1", 0, max_size=None)
    port = _bound_server_port(server)
    handle.port = port
    handle.url = f"ws://127.0.0.1:{port}"
    handle.server = server
    return handle


_TEARDOWN_TIMEOUT_S = 5.0


async def _stop_ws_server(handle: WSServerHandle) -> None:
    # Every wait here is bounded. Teardown runs in a fixture finalizer, so an
    # unbounded await does not fail the test that caused it -- it hangs the
    # worker until the global pytest timeout force-exits the process, which
    # costs the whole run a worker and blames whichever test happened to be
    # holding the fixture.
    sessions = getattr(handle, "_sessions_to_stop", None) or []
    for session in sessions:
        try:
            await asyncio.wait_for(session.stop(), timeout=_TEARDOWN_TIMEOUT_S)
        except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
            pass
    if handle.server is not None:
        handle.server.close()
        try:
            # ``wait_closed()`` waits for every live connection handler to
            # return. A handler parked in ``session.start()`` or
            # ``ws.wait_closed()`` never does, so this must not be unbounded:
            # the sockets are already closed by ``close()`` above, and a
            # leaked handler task is a far cheaper failure than a dead worker.
            await asyncio.wait_for(handle.server.wait_closed(), timeout=_TEARDOWN_TIMEOUT_S)
        except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
            pass


@pytest_asyncio.fixture
async def ws_server_factory():
    """Async factory for spinning up WebSocket test servers.

    Usage::

        async def build(ws):
            transport = WebSocketConnectionTransport(ws)
            return create_session(EasyConfig(..., transport=transport))

        handle = await ws_server_factory(build)
        # handle.url, handle.session
    """
    handles: list[WSServerHandle] = []

    async def _factory(builder: SessionBuilder) -> WSServerHandle:
        handle = await _start_ws_server(builder)
        handles.append(handle)
        return handle

    try:
        yield _factory
    finally:
        for h in handles:
            await _stop_ws_server(h)


# ---------------------------------------------------------------------------
# Minimal live-config builder (OpenAI providers)
# ---------------------------------------------------------------------------


def build_live_session(
    *,
    transport: Any,
    instructions: str = "Answer in one short sentence.",
    debug: str = "full",
    model: str = "gpt-4o-mini",
) -> Any:
    """Build a fully wired live ``Session`` with real OpenAI providers.

    Uses the public ``create_session`` API. Noise reduction is disabled
    via ``enable_noise_reduction=False`` because the open-source
    fallbacks (RNNoise) may not be installed in all test environments;
    Krisp is closed-source. LiveKit AEC runs when the extra is present.
    """
    _require_live()
    from agents import Agent  # type: ignore[import-untyped]

    from easycat import EasyConfig, create_session

    api_key = os.environ["OPENAI_API_KEY"]
    agent = Agent(name="e2e_assistant", instructions=instructions, model=model)
    config = EasyConfig(
        openai_api_key=api_key,
        transport=transport,
        agent=agent,
        debug=debug,  # type: ignore[arg-type]
        # Plans 1/2/4 exercise the voice pipeline without depending on
        # noise reduction or AEC being available in the test env. Plan
        # 6 covers the full-stack case with both enabled.
        enable_noise_reduction=False,
        enable_echo_cancellation=False,
    )
    return create_session(config)


# Back-compat alias for earlier test files
make_live_openai_config = build_live_session


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@dataclass
class CapturedErrors:
    items: list[BaseException] = field(default_factory=list)


@pytest.fixture
def captured_errors() -> CapturedErrors:
    return CapturedErrors()
