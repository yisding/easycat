"""Live Twilio loopback check for in-band DTMF (billable; never runs by default).

The offline suites prove that EasyCat renders Q.23 tones, sends them on the
Twilio Media Stream without ending it, and that the bytes decode back to the
digits. They cannot prove that a real carrier route carries those tones to a
far end that detects them. This test is that oracle:

1. A local server listens on ``EASYCAT_DTMF_LOOPBACK_PORT`` (default 8766) and
   must be reachable from Twilio at ``EASYCAT_DTMF_LOOPBACK_PUBLIC_URL``
   (an ``https://`` tunnel or ingress). It serves the media WebSocket and the
   two ``GET`` TwiML webhooks on that one port; every request must carry a
   valid ``X-Twilio-Signature``.
2. For the duration of the test, the voice URL of the dedicated loopback
   number ``EASYCAT_DTMF_LOOPBACK_TO`` (number B) points at this server, which
   answers with ``<Gather input="dtmf" finishOnKey="">`` sized to the burst, so
   ``#`` is collected as a digit instead of ending the gather. The previous
   voice URL and method are restored afterwards, also when the update itself
   fails; if restoring fails too, the error says so and the number must be
   restored by hand. Use a dedicated, non-production
   number that serves nothing else and routes voice through its webhook URL:
   the test refuses a number with a TwiML App (``voice_application_sid``) or a
   SIP trunk (``trunk_sid``), because Twilio ignores ``voice_url`` on those.
3. Twilio places a call from ``EASYCAT_DTMF_LOOPBACK_FROM`` (number A) to
   number B whose leg runs ``<Connect><Stream>`` into a real EasyCat
   :class:`~easycat.session.Session`. The session plays ``1234#`` in-band;
   B's ``<Gather>`` must report ``Digits=1234#``. Then the same stream plays
   ``56#`` and B must report ``Digits=56#``, which proves the media stream
   survived the first burst (multi-level IVR navigation). B answers the last
   gather with a bounded ``<Pause>``, so the call stays up until the second
   playback mark arrives; the test then ends the call.

Run it explicitly (it places one short call per tone variant)::

    TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... \\
    EASYCAT_DTMF_LOOPBACK_FROM=+1555... EASYCAT_DTMF_LOOPBACK_TO=+1555... \\
    EASYCAT_DTMF_LOOPBACK_PUBLIC_URL=https://example.ngrok.app \\
    uv run pytest tests/telephony/test_twilio_inband_dtmf_live.py \\
        -m "integration_live and provider and surface_transport" -v

The live test is skipped when any variable is missing or the ``twilio`` SDK
is not installed. The offline checks at the end of this module (no marks, no
network) keep the loopback session config and routing guard honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import quoteattr

import pytest
import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from easycat.providers import Transport
from easycat.session import Session, SessionConfig
from easycat.stubs import NoopAgent
from easycat.telephony import DTMFToneConfig
from easycat.telephony.twiml import validate_twilio_webhook_signature
from easycat.transports import TwilioStreamTokenStore, TwilioTransportConfig
from easycat.transports.twilio_media import (
    TwilioConnectionTransport,
    twiml_connect_stream,
)
from tests._fakes import FakeSTT, FakeTransport, FakeTTS

_REQUIRED_ENV = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "EASYCAT_DTMF_LOOPBACK_FROM",
    "EASYCAT_DTMF_LOOPBACK_TO",
    "EASYCAT_DTMF_LOOPBACK_PUBLIC_URL",
)
_ANSWER_PATH = "/loopback/answer"
_GATHERED_PATH = "/loopback/gathered"
_MEDIA_PATH = "/loopback/media"
_CALL_SETUP_TIMEOUT_S = 60.0
_GATHER_TIMEOUT_S = 30.0
# B's <Gather> starts listening once its answer TwiML runs; give the far end a
# moment after the media stream opens before the first key press.
_GATHER_SETTLE_S = 1.5
# The bursts B's <Gather> verbs expect, in order. ``#`` is a digit, not a
# finish key, so a dropped ``#`` fails the comparison instead of passing
# after the gather timeout.
_BURSTS = ("1234#", "56#")
# B holds the call this long after the last gather so the second playback
# mark can reach EasyCat; the test hangs up once it has checked both.
_FINAL_PAUSE_S = 15


@dataclass(frozen=True)
class _LoopbackEnv:
    account_sid: str
    auth_token: str
    from_number: str
    to_number: str
    public_url: str
    port: int

    @property
    def host(self) -> str:
        return urlsplit(self.public_url).netloc

    def https(self, path: str) -> str:
        return f"https://{self.host}{path}"

    def wss(self, path: str) -> str:
        return f"wss://{self.host}{path}"


@dataclass
class _LoopbackState:
    """What the loopback server observed, shared with the test body."""

    gathers: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    sessions: asyncio.Queue[Session] = field(default_factory=asyncio.Queue)
    gathers_done: int = 0
    rejected_requests: int = 0


def _loopback_env() -> _LoopbackEnv:
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip("in-band DTMF loopback needs " + ", ".join(missing))
    pytest.importorskip("twilio", reason="twilio SDK not installed (telephony extra)")
    public_url = os.environ["EASYCAT_DTMF_LOOPBACK_PUBLIC_URL"].rstrip("/")
    if urlsplit(public_url).scheme != "https":
        pytest.fail("EASYCAT_DTMF_LOOPBACK_PUBLIC_URL must be an https:// URL")
    return _LoopbackEnv(
        account_sid=os.environ["TWILIO_ACCOUNT_SID"],
        auth_token=os.environ["TWILIO_AUTH_TOKEN"],
        from_number=os.environ["EASYCAT_DTMF_LOOPBACK_FROM"],
        to_number=os.environ["EASYCAT_DTMF_LOOPBACK_TO"],
        public_url=public_url,
        port=int(os.environ.get("EASYCAT_DTMF_LOOPBACK_PORT", "8766")),
    )


def _loopback_session_config(transport: Transport) -> SessionConfig:
    """An audio-mode session that only plays tones.

    ``Session`` rejects passthrough STT/TTS (and VAD when enabled) in audio
    mode, so this uses the shared non-passthrough fakes and turns VAD off: the
    loopback never needs a caller turn, only the outbound media path.
    """
    return SessionConfig(
        transport=transport,
        stt=FakeSTT(transcript=""),
        tts=FakeTTS(),
        agent=NoopAgent(),
        enable_vad=False,
        enable_noise_reduction=False,
    )


def _voice_routing_problem(number: Any) -> str | None:
    """Why Twilio would not route calls on *number* to its ``voice_url``."""
    if getattr(number, "voice_application_sid", None):
        return "a TwiML App (voice_application_sid)"
    if getattr(number, "trunk_sid", None):
        return "a SIP trunk (trunk_sid)"
    return None


def _gather_twiml(env: _LoopbackEnv, burst: str) -> str:
    """Collect exactly ``len(burst)`` keys, ``#`` included."""
    action = quoteattr(env.https(_GATHERED_PATH))
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="dtmf" finishOnKey="" numDigits="{len(burst)}" timeout="20" '
        f'method="GET" action={action}/>'
        "<Hangup/></Response>"
    )


def _gathered_twiml(env: _LoopbackEnv, state: _LoopbackState) -> str:
    """The next gather, or a bounded pause after the last one."""
    state.gathers_done += 1
    if state.gathers_done < len(_BURSTS):
        return _gather_twiml(env, _BURSTS[state.gathers_done])
    return f'<Response><Pause length="{_FINAL_PAUSE_S}"/><Hangup/></Response>'


def _xml_response(connection: ServerConnection, body: str) -> Response:
    response = connection.respond(HTTPStatus.OK, body)
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = "text/xml; charset=utf-8"
    return response


def _unauthorized() -> Response:
    body = b"Missing or invalid Twilio signature.\n"
    return Response(
        HTTPStatus.UNAUTHORIZED.value,
        HTTPStatus.UNAUTHORIZED.phrase,
        Headers([("Content-Type", "text/plain"), ("Content-Length", str(len(body)))]),
        body,
    )


def _process_request(
    env: _LoopbackEnv, state: _LoopbackState
) -> Callable[[ServerConnection, Request], Response | None]:
    """Serve B's GET TwiML webhooks and authenticate the media upgrade."""

    def process_request(connection: ServerConnection, request: Request) -> Response | None:
        path = urlsplit(request.path).path
        signatures = request.headers.get_all("X-Twilio-Signature")
        signature = signatures[0] if len(signatures) == 1 else None
        public = env.wss(request.path) if path == _MEDIA_PATH else env.https(request.path)
        if not validate_twilio_webhook_signature(
            auth_token=env.auth_token, url=public, params=[], signature=signature
        ):
            state.rejected_requests += 1
            return _unauthorized()
        if path == _MEDIA_PATH:
            return None
        if path == _ANSWER_PATH:
            state.answered.set()
            return _xml_response(connection, _gather_twiml(env, _BURSTS[0]))
        if path == _GATHERED_PATH:
            digits = parse_qs(urlsplit(request.path).query).get("Digits", [""])[0]
            state.gathers.put_nowait(digits)
            return _xml_response(connection, _gathered_twiml(env, state))
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")

    return process_request


@contextlib.asynccontextmanager
async def _loopback_server(env: _LoopbackEnv, state: _LoopbackState) -> AsyncIterator[Any]:
    tokens = TwilioStreamTokenStore()

    async def handle(ws: ServerConnection) -> None:
        transport = TwilioConnectionTransport(
            ws, config=TwilioTransportConfig(stream_token_validator=tokens.consume_start)
        )
        if not await transport.wait_for_start(timeout_s=_CALL_SETUP_TIMEOUT_S):
            return
        session = Session(_loopback_session_config(transport))
        await session.start()
        state.sessions.put_nowait(session)
        # Keep the socket open until the test has stopped the session.
        await state.finished.wait()

    server = await websockets.serve(
        handle,
        # The tunnel or ingress forwards the public URL to this port.
        "0.0.0.0",
        env.port,
        process_request=_process_request(env, state),
        compression=None,
    )
    try:
        yield tokens
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def _loopback_voice_url(client: Any, env: _LoopbackEnv) -> AsyncIterator[None]:
    """Point number B at the loopback server, restoring its settings after."""
    numbers = await asyncio.to_thread(
        client.incoming_phone_numbers.list, phone_number=env.to_number, limit=1
    )
    if not numbers:
        pytest.fail("EASYCAT_DTMF_LOOPBACK_TO is not a number on this Twilio account")
    number = numbers[0]
    problem = _voice_routing_problem(number)
    if problem is not None:
        pytest.fail(
            "EASYCAT_DTMF_LOOPBACK_TO routes voice through "
            f"{problem}, which overrides voice_url; use a dedicated number with "
            "webhook voice routing"
        )
    previous_url, previous_method = number.voice_url, number.voice_method

    async def restore() -> None:
        await asyncio.to_thread(
            client.incoming_phone_numbers(number.sid).update,
            voice_url=previous_url or "",
            voice_method=previous_method or "POST",
        )

    # The override is inside the restore scope: Twilio may apply it even when
    # the client never sees the response.
    try:
        await asyncio.to_thread(
            client.incoming_phone_numbers(number.sid).update,
            voice_url=env.https(_ANSWER_PATH),
            voice_method="GET",
        )
        yield
    except BaseException as exc:
        try:
            await restore()
        except Exception as restore_exc:  # noqa: BLE001 keep the original error primary
            exc.add_note(
                f"Restoring the voice URL of {number.sid} also failed "
                f"({type(restore_exc).__name__}); restore its voice URL and method by hand."
            )
        raise
    await restore()


@pytest.mark.integration_live
@pytest.mark.provider("twilio")
@pytest.mark.surface_transport
# One real PSTN loopback call per variant: ringing, answer and two gathers take
# far longer than the suite's 60 s default.
@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    "tone_config",
    [
        pytest.param(None, id="defaults"),
        pytest.param(DTMFToneConfig(tone_ms=60, gap_ms=60), id="short-60ms"),
        pytest.param(
            DTMFToneConfig(low_level_dbm0=-12.0, high_level_dbm0=-10.0), id="quiet-12dbm0"
        ),
    ],
)
async def test_inband_dtmf_reaches_twilio_gather_and_stream_survives(
    tone_config: DTMFToneConfig | None,
) -> None:
    env = _loopback_env()
    # After the skip check: the SDK is an optional extra.
    from twilio.rest import Client

    client = Client(env.account_sid, env.auth_token)
    state = _LoopbackState()
    call_sid: str | None = None
    session: Session | None = None

    async with _loopback_server(env, state) as tokens, _loopback_voice_url(client, env):
        try:
            twiml = twiml_connect_stream(env.wss(_MEDIA_PATH), stream_token=tokens.issue())
            call = await asyncio.to_thread(
                client.calls.create,
                to=env.to_number,
                from_=env.from_number,
                twiml=twiml,
                time_limit=120,
            )
            call_sid = call.sid
            session = await asyncio.wait_for(state.sessions.get(), _CALL_SETUP_TIMEOUT_S)
            await asyncio.wait_for(state.answered.wait(), _CALL_SETUP_TIMEOUT_S)
            await asyncio.sleep(_GATHER_SETTLE_S)

            first = await session.play_dtmf_tones(_BURSTS[0], tone_config=tone_config)
            assert first.mark_name is not None
            assert await first.wait(first.duration_ms / 1000 + 10.0) == "played"
            assert await asyncio.wait_for(state.gathers.get(), _GATHER_TIMEOUT_S) == _BURSTS[0]
            assert session.is_running, "the media stream must survive in-band DTMF"

            await asyncio.sleep(_GATHER_SETTLE_S)
            second = await session.play_dtmf_tones(_BURSTS[1], tone_config=tone_config)
            assert await second.wait(second.duration_ms / 1000 + 10.0) == "played"
            assert await asyncio.wait_for(state.gathers.get(), _GATHER_TIMEOUT_S) == _BURSTS[1]
        finally:
            try:
                if session is not None:
                    await session.stop(force=True)
            finally:
                # Release the socket handler and end the call even if stop()
                # raised or was cancelled; server.wait_closed() waits on it.
                state.finished.set()
                if call_sid is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(client.calls(call_sid).update, status="completed")


# ── Offline checks (no marks, no network) ─────────────────────────


def test_loopback_session_config_passes_audio_provider_validation() -> None:
    config = _loopback_session_config(FakeTransport())
    assert config.runtime_mode != "text_session"
    Session(config)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"voice_application_sid": "AP123", "trunk_sid": None}, "TwiML App"),
        ({"voice_application_sid": None, "trunk_sid": "TK123"}, "SIP trunk"),
        ({"voice_application_sid": "", "trunk_sid": ""}, None),
        ({}, None),
    ],
)
def test_voice_routing_problem_rejects_app_and_trunk_routing(
    fields: dict[str, str | None], expected: str | None
) -> None:
    problem = _voice_routing_problem(SimpleNamespace(**fields))
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem


def _offline_env() -> _LoopbackEnv:
    return _LoopbackEnv(
        account_sid="AC0",
        auth_token="token",
        from_number="+15550000001",
        to_number="+15550000002",
        public_url="https://loopback.example",
        port=0,
    )


def test_gathers_collect_the_whole_burst_including_hash() -> None:
    env = _offline_env()
    state = _LoopbackState()

    first = _gather_twiml(env, _BURSTS[0])
    second = _gathered_twiml(env, state)
    final = _gathered_twiml(env, state)

    assert 'finishOnKey=""' in first and 'numDigits="5"' in first
    assert 'finishOnKey=""' in second and 'numDigits="3"' in second
    # The last gather holds the call open for the second playback mark.
    assert "<Pause" in final and "<Gather" not in final


class _FakeIncomingNumbers:
    """``client.incoming_phone_numbers`` recording updates, with injected failures."""

    def __init__(self, *, fail_override: bool = False, fail_restore: bool = False) -> None:
        self.fail_override = fail_override
        self.fail_restore = fail_restore
        self.updates: list[dict[str, str]] = []
        self.number = SimpleNamespace(
            sid="PN0",
            voice_url="https://prod.example/voice",
            voice_method="POST",
            voice_application_sid=None,
            trunk_sid=None,
        )

    def list(self, **_: object) -> list[SimpleNamespace]:
        return [self.number]

    def __call__(self, sid: str) -> SimpleNamespace:
        assert sid == self.number.sid
        return SimpleNamespace(update=self._update)

    def _update(self, **fields: str) -> None:
        self.updates.append(fields)
        if fields["voice_method"] == "GET" and self.fail_override:
            raise ConnectionError("response lost after Twilio applied the update")
        if fields["voice_method"] == "POST" and self.fail_restore:
            raise RuntimeError("restore failed")


_RESTORED = {"voice_url": "https://prod.example/voice", "voice_method": "POST"}


async def test_voice_url_is_restored_when_the_override_request_fails() -> None:
    numbers = _FakeIncomingNumbers(fail_override=True)
    client = SimpleNamespace(incoming_phone_numbers=numbers)

    with pytest.raises(ConnectionError):
        async with _loopback_voice_url(client, _offline_env()):
            pytest.fail("the body must not run when the override failed")

    assert numbers.updates[-1] == _RESTORED
    assert len(numbers.updates) == 2


async def test_voice_url_restore_failure_keeps_the_original_error() -> None:
    numbers = _FakeIncomingNumbers(fail_restore=True)
    client = SimpleNamespace(incoming_phone_numbers=numbers)

    with pytest.raises(AssertionError) as excinfo:
        async with _loopback_voice_url(client, _offline_env()):
            raise AssertionError("gather mismatch")

    assert str(excinfo.value) == "gather mismatch"
    assert any("restore its voice URL" in note for note in excinfo.value.__notes__)


async def test_voice_url_is_restored_after_a_clean_run() -> None:
    numbers = _FakeIncomingNumbers()
    client = SimpleNamespace(incoming_phone_numbers=numbers)

    async with _loopback_voice_url(client, _offline_env()):
        assert numbers.updates[-1]["voice_method"] == "GET"

    assert numbers.updates[-1] == _RESTORED
