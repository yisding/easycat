"""Twilio media stream transport, TwiML, and audio conversion tests."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import threading
import time

import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Request

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk
from easycat.events import (
    DTMF,
    CallAnswered,
    CallEnded,
    Event,
    EventBus,
    PlaybackMarkAck,
    TransportDegraded,
)
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
from easycat.telephony import compute_twilio_webhook_signature
from easycat.transports._base import ServerTransportBase
from easycat.transports.twilio_media import (
    _DEGRADED_TWILIO_SEQUENCE_GAP,
    _DEGRADED_TWILIO_TIMESTAMP_GAP,
    TWILIO_STREAM_TOKEN_PARAMETER,
    StreamTokenContext,
    TwilioConnectionTransport,
    TwilioStreamTokenStore,
    TwilioTransport,
    TwilioTransportConfig,
    _TwilioProtocolMixin,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    twilio_websocket_signature_process_request,
    twiml_connect_stream,
    twiml_stream,
)

from ._webrtc_fakes import _UsesPytestTcpPortFactory
from .conftest import make_chunk

TwilioContextAlias = StreamTokenContext

_make_chunk = make_chunk


def _make_sine_pcm16(freq: int = 440, duration_ms: int = 20, sample_rate: int = 16000) -> bytes:
    """Generate a short PCM16 sine wave for conversion tests."""
    import math

    n_samples = (sample_rate * duration_ms) // 1000
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        value = int(16000 * math.sin(2 * math.pi * freq * t))
        samples.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{n_samples}h", *samples)


def _twilio_connected_msg() -> str:
    return json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})


def _twilio_start_msg(
    stream_sid: str = "MZ123",
    call_sid: str = "CA456",
    *,
    custom_parameters: dict[str, str] | None = None,
) -> str:
    start = {
        "streamSid": stream_sid,
        "accountSid": "AC789",
        "callSid": call_sid,
        "tracks": ["inbound"],
        "mediaFormat": {
            "encoding": "audio/x-mulaw",
            "sampleRate": 8000,
            "channels": 1,
        },
    }
    if custom_parameters is not None:
        start["customParameters"] = custom_parameters
    return json.dumps(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": start,
        }
    )


def _twilio_media_msg(
    mulaw_data: bytes,
    stream_sid: str = "MZ123",
    *,
    sequence_number: str = "2",
    timestamp: str = "0",
) -> str:
    payload = base64.b64encode(mulaw_data).decode("ascii")
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": sequence_number,
            "streamSid": stream_sid,
            "media": {
                "track": "inbound",
                "chunk": "1",
                "timestamp": timestamp,
                "payload": payload,
            },
        }
    )


def _twilio_media_msg_with_track(
    mulaw_data: bytes,
    *,
    stream_sid: str = "MZ123",
    track: str = "inbound",
) -> str:
    payload = base64.b64encode(mulaw_data).decode("ascii")
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": "2",
            "streamSid": stream_sid,
            "media": {
                "track": track,
                "chunk": "1",
                "timestamp": "0",
                "payload": payload,
            },
        }
    )


def _twilio_dtmf_msg(digit: str, stream_sid: str = "MZ123") -> str:
    return json.dumps(
        {
            "event": "dtmf",
            "streamSid": stream_sid,
            "dtmf": {"digit": digit, "track": "inbound_track"},
        }
    )


def _twilio_stop_msg(stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "stop", "streamSid": stream_sid})


def _twilio_mark_msg(name: str, stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


async def _drain_transport_diagnostics(
    transport: TwilioTransport | TwilioConnectionTransport,
) -> None:
    await transport._drain_emit_tasks()


class _DummyTwilioWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed_with: tuple[object, ...] | None = None

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *args: object) -> None:
        self.closed_with = args


class _ScriptedTwilioWebSocket:
    def __init__(self, *messages: str) -> None:
        self._messages = list(messages)
        self.sent: list[str] = []
        self.closed_with: tuple[object, ...] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __aiter__(self) -> _ScriptedTwilioWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        self.entered.set()
        await self.release.wait()
        raise StopAsyncIteration

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *args: object) -> None:
        self.closed_with = args
        self.release.set()


class _MalformedTrickleWebSocket(_ScriptedTwilioWebSocket):
    async def __anext__(self) -> str:
        await asyncio.sleep(0.002)
        return "{not-json"


class TestTwilioStreamTokenStore:
    @pytest.mark.parametrize("ttl_s", [True, 0, -1, float("nan"), float("inf")])
    def test_rejects_invalid_ttl(self, ttl_s: float) -> None:
        with pytest.raises(ValueError, match="ttl_s must be a finite positive number"):
            TwilioStreamTokenStore("secret", ttl_s=ttl_s)

    def test_consumes_token_once(self) -> None:
        store = TwilioStreamTokenStore("secret")
        token = store.issue()

        assert store.consume(token)
        assert not store.consume(token)
        assert not store.consume(f"{token}x")
        assert not store.consume("nonce.123.é")

    def test_consumes_fractional_ttl_without_rounding_down(self) -> None:
        current = 100.25
        store = TwilioStreamTokenStore("secret", ttl_s=0.1, now=lambda: current)

        assert store.consume(store.issue())

    def test_rejects_expired_tokens(self) -> None:
        current = 1000.0

        def now() -> float:
            return current

        store = TwilioStreamTokenStore("secret", ttl_s=1, now=now)
        token = store.issue()
        current = 1002.0

        assert not store.consume(token)

    def test_idempotent_claimed_grant_does_not_mint_on_webhook_retry(self) -> None:
        store = TwilioStreamTokenStore("secret")
        token = store.issue(idempotency_key="signed-request", claims={"CallSid": "CA1"})
        context = StreamTokenContext(
            token=token,
            call_sid="CA1",
            stream_sid="MZ1",
            parameters={},
        )

        assert store.issue(idempotency_key="signed-request", claims={"CallSid": "CA1"}) == token
        assert store.consume_start(context)
        assert store.issue(idempotency_key="signed-request", claims={"CallSid": "CA1"}) == token
        assert not store.consume_start(context)

    def test_claimed_grant_rejects_different_call_sid(self) -> None:
        store = TwilioStreamTokenStore("secret")
        token = store.issue(claims={"CallSid": "CA1", "Direction": "inbound"})

        assert not store.consume_start(
            StreamTokenContext(
                token=token,
                call_sid="CA1",
                stream_sid="MZ1",
                parameters={"Direction": "outbound"},
            )
        )


def test_twilio_transport_defaults_to_loopback() -> None:
    assert TwilioTransportConfig().host == "127.0.0.1"


@pytest.mark.asyncio
async def test_twilio_transport_rejects_unauthenticated_public_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = False

    async def _connect(_self: ServerTransportBase) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(ServerTransportBase, "connect", _connect)
    transport = TwilioTransport(TwilioTransportConfig(host="0.0.0.0"))

    with pytest.raises(ValueError, match="stream_token_validator"):
        await transport.connect()
    assert connected is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        TwilioTransportConfig(
            host="0.0.0.0",
            stream_token_validator=lambda _token: True,
        ),
        TwilioTransportConfig(host="0.0.0.0", unsafe_allow_no_auth=True),
    ],
)
async def test_twilio_transport_allows_explicitly_guarded_public_bind(
    config: TwilioTransportConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = False

    async def _connect(_self: ServerTransportBase) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(ServerTransportBase, "connect", _connect)

    await TwilioTransport(config).connect()
    assert connected is True


def test_twilio_media_handshake_validates_signature_against_public_url() -> None:
    auth_token = "twilio-auth"
    websocket_url = "wss://voice.example.com/media"
    signature = compute_twilio_webhook_signature(
        auth_token=auth_token,
        url=websocket_url,
        params=[],
    )
    process_request = twilio_websocket_signature_process_request(
        auth_token,
        websocket_url,
    )

    authorized = Request(
        "/media",
        Headers([("X-Twilio-Signature", signature)]),
    )
    rejected = Request(
        "/media",
        Headers([("X-Twilio-Signature", "forged")]),
    )
    duplicated = Request(
        "/media",
        Headers(
            [
                ("X-Twilio-Signature", signature),
                ("X-Twilio-Signature", signature),
            ]
        ),
    )

    assert process_request(None, authorized) is None  # type: ignore[arg-type]
    response = process_request(None, rejected)  # type: ignore[arg-type]
    assert response is not None
    assert response.status_code == 401
    duplicated_response = process_request(None, duplicated)  # type: ignore[arg-type]
    assert duplicated_response is not None
    assert duplicated_response.status_code == 401


class TestTwilioStreamTokenValidation:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"stream_token_parameter": ""}, "stream_token_parameter must be non-empty"),
            *[
                (
                    {"stream_token_validation_timeout_s": value},
                    "stream_token_validation_timeout_s must be a positive finite number",
                )
                for value in (0, float("nan"), float("inf"), float("-inf"), True)
            ],
        ],
    )
    def test_config_rejects_invalid_token_validation_settings(
        self,
        kwargs: dict[str, object],
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            TwilioTransportConfig(**kwargs)  # type: ignore[arg-type]

    def test_audio_byte_limit_preserves_existing_positional_timeout(self) -> None:
        config = TwilioTransportConfig(
            "127.0.0.1",
            8766,
            PCM16_MONO_16K,
            200,
            None,
            TWILIO_STREAM_TOKEN_PARAMETER,
            0.25,
        )

        assert config.stream_token_validation_timeout_s == 0.25
        assert config.max_pending_bytes == TwilioTransportConfig().max_pending_bytes

    def test_audio_byte_limit_preserves_existing_positional_auth_escape_hatch(self) -> None:
        config = TwilioTransportConfig(
            "127.0.0.1",
            8766,
            PCM16_MONO_16K,
            200,
            None,
            TWILIO_STREAM_TOKEN_PARAMETER,
            0.25,
            True,
        )

        assert config.unsafe_allow_no_auth is True
        assert config.max_pending_bytes == TwilioTransportConfig().max_pending_bytes

    @pytest.mark.asyncio
    async def test_server_transport_consumes_token_and_hides_parameter(self) -> None:
        store = TwilioStreamTokenStore("secret")
        token = store.issue()
        config = TwilioTransportConfig(stream_token_validator=store.consume)
        transport = TwilioTransport(config)

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={
                    TWILIO_STREAM_TOKEN_PARAMETER: token,
                    "crm_account_id": "ACC-42",
                },
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"
        assert transport.call_identity.custom_fields == {"crm_account_id": "ACC-42"}

        replay = TwilioTransport(config)
        await replay._handle_message(
            _twilio_start_msg(
                "STREAM2",
                "CALL2",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: token},
            )
        )
        assert replay.stream_sid is None
        assert replay.call_sid is None

    @pytest.mark.asyncio
    async def test_connection_transport_rejects_missing_token(self) -> None:
        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=lambda _token: True),
        )

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))

        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ws.closed_with == (4003, "Missing or invalid stream token")

    @pytest.mark.asyncio
    async def test_connection_transport_accepts_valid_token(self) -> None:
        store = TwilioStreamTokenStore("secret")
        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=store.consume),
        )

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: store.issue()},
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"
        assert ws.closed_with is None

    @pytest.mark.asyncio
    async def test_connection_transport_rejects_start_without_stream_sid(self) -> None:
        ws = _DummyTwilioWebSocket()
        validation_called = False

        def validator(_token: str) -> bool:
            nonlocal validation_called
            validation_called = True
            return True

        message = json.loads(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )
        message.pop("streamSid")
        message["start"].pop("streamSid")
        answered: list[CallAnswered] = []
        bus = EventBus()
        bus.subscribe(CallAnswered, answered.append)
        transport = TwilioConnectionTransport(
            ws,
            event_bus=bus,
            config=TwilioTransportConfig(stream_token_validator=validator),
        )

        await transport._handle_message(json.dumps(message))

        assert not validation_called
        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert answered == []
        assert ws.closed_with == (4003, "Missing streamSid")

    @pytest.mark.asyncio
    async def test_connection_transport_preflight_accepts_token_once(self) -> None:
        store = TwilioStreamTokenStore("secret")
        token = store.issue()
        ws = _ScriptedTwilioWebSocket(
            _twilio_connected_msg(),
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: token},
            ),
        )
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=store.consume),
        )

        assert await transport.wait_for_start()
        assert not store.consume(token)
        assert transport.stream_sid is None

        await transport.connect()

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"
        assert ws.closed_with is None
        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_connection_transport_preflight_rejects_start_without_stream_sid(
        self,
    ) -> None:
        validation_called = False

        def validator(_token: str) -> bool:
            nonlocal validation_called
            validation_called = True
            return True

        message = json.loads(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )
        message.pop("streamSid")
        message["start"].pop("streamSid")
        ws = _ScriptedTwilioWebSocket(json.dumps(message))
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=validator),
        )

        assert not await transport.wait_for_start()

        assert not validation_called
        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ws.closed_with == (4003, "Missing streamSid")

    @pytest.mark.asyncio
    async def test_connection_transport_preflight_rejects_invalid_token(self) -> None:
        ws = _ScriptedTwilioWebSocket(_twilio_start_msg("STREAM1", "CALL1"))
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=lambda _token: False),
        )

        assert not await transport.wait_for_start()

        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ws.closed_with == (4003, "Missing or invalid stream token")

    @pytest.mark.asyncio
    async def test_connection_transport_preflight_times_out_and_closes(self) -> None:
        ws = _ScriptedTwilioWebSocket()
        transport = TwilioConnectionTransport(ws)

        assert not await transport.wait_for_start(timeout_s=0.01)

        assert ws.closed_with == (1008, "Timed out waiting for Twilio start")
        assert not transport.is_connected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timeout_s", [float("nan"), float("inf")])
    async def test_connection_transport_preflight_rejects_nonfinite_timeout(
        self, timeout_s: float
    ) -> None:
        transport = TwilioConnectionTransport(_ScriptedTwilioWebSocket())

        with pytest.raises(ValueError, match="timeout_s must be positive"):
            await transport.wait_for_start(timeout_s=timeout_s)

    @pytest.mark.asyncio
    async def test_preflight_deadline_is_not_reset_by_malformed_trickle(self) -> None:
        ws = _MalformedTrickleWebSocket()
        transport = TwilioConnectionTransport(ws)

        assert not await transport.wait_for_start(timeout_s=0.01)

        assert ws.closed_with == (1008, "Timed out waiting for Twilio start")

    @pytest.mark.asyncio
    async def test_deferred_start_handler_failure_rolls_back_connection(self) -> None:
        store = TwilioStreamTokenStore("secret")
        ws = _ScriptedTwilioWebSocket(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: store.issue()},
            )
        )
        bus = EventBus(handler_error_policy="raise")

        async def fail_start(_event: CallAnswered) -> None:
            raise RuntimeError("strict handler failed")

        bus.subscribe(CallAnswered, fail_start)
        transport = TwilioConnectionTransport(
            ws,
            event_bus=bus,
            config=TwilioTransportConfig(stream_token_validator=store.consume),
        )
        assert await transport.wait_for_start()

        with pytest.raises(RuntimeError, match="strict handler failed"):
            await transport.connect()

        assert not transport.is_connected
        assert transport._receive_task is None
        assert ws.closed_with == ()

    @pytest.mark.asyncio
    async def test_cancelled_deferred_start_closes_and_rolls_back(self) -> None:
        store = TwilioStreamTokenStore("secret")
        ws = _ScriptedTwilioWebSocket(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: store.issue()},
            )
        )
        bus = EventBus()
        entered = asyncio.Event()

        async def block_start(_event: CallAnswered) -> None:
            entered.set()
            await asyncio.Event().wait()

        bus.subscribe(CallAnswered, block_start)
        transport = TwilioConnectionTransport(
            ws,
            event_bus=bus,
            config=TwilioTransportConfig(stream_token_validator=store.consume),
        )
        assert await transport.wait_for_start()
        connecting = asyncio.create_task(transport.connect())
        await entered.wait()

        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting

        assert not transport.is_connected
        assert transport._receive_task is None
        assert ws.closed_with == ()

    @pytest.mark.asyncio
    async def test_disconnect_preserves_caller_cancellation_while_reaping_receiver(
        self,
    ) -> None:
        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(ws)
        transport._connected = True
        child_cancelled = asyncio.Event()
        release_child = asyncio.Event()

        async def cancellation_resistant_receiver() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                await release_child.wait()

        transport._receive_task = asyncio.create_task(cancellation_resistant_receiver())
        await asyncio.sleep(0)
        disconnecting = asyncio.create_task(transport.disconnect())
        await child_cancelled.wait()
        disconnecting.cancel()
        release_child.set()

        with pytest.raises(asyncio.CancelledError):
            await disconnecting

        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)
        assert transport._socket_close_pending is True
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await transport.connect()

        await transport.disconnect()
        assert ws.closed_with == ()
        assert transport._socket_close_pending is False
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_cancelled_socket_close_is_retained_for_disconnect_retry(self) -> None:
        close_started = asyncio.Event()

        class _CancelOnceCloseWebSocket(_DummyTwilioWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.close_calls = 0

            async def close(self, *args: object) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    close_started.set()
                    await asyncio.Event().wait()
                await super().close(*args)

        ws = _CancelOnceCloseWebSocket()
        transport = TwilioConnectionTransport(ws)
        transport._connected = True
        disconnecting = asyncio.create_task(transport.disconnect())
        await close_started.wait()
        disconnecting.cancel()

        with pytest.raises(asyncio.CancelledError):
            await disconnecting

        assert transport._socket_close_pending is True
        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)

        await transport.disconnect()
        assert ws.close_calls == 2
        assert transport._socket_close_pending is False
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_failed_socket_close_is_retained_for_disconnect_retry(self) -> None:
        class _FailOnceCloseWebSocket(_DummyTwilioWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.close_calls = 0

            async def close(self, *args: object) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("socket close failed")
                await super().close(*args)

        ws = _FailOnceCloseWebSocket()
        transport = TwilioConnectionTransport(ws)
        transport._connected = True

        with pytest.raises(RuntimeError, match="socket close failed"):
            await transport.disconnect()

        assert transport._socket_close_pending is True
        assert isinstance(transport._disconnect_cleanup_error, RuntimeError)
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await transport.connect()

        await transport.disconnect()
        assert ws.close_calls == 2
        assert transport._socket_close_pending is False
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_connect_queued_behind_disconnect_cannot_republish_closed_socket(
        self,
    ) -> None:
        class _BlockingCloseWebSocket(_DummyTwilioWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.close_entered = asyncio.Event()
                self.release_close = asyncio.Event()

            async def close(self, *args: object) -> None:
                self.close_entered.set()
                await self.release_close.wait()
                await super().close(*args)

        ws = _BlockingCloseWebSocket()
        transport = TwilioConnectionTransport(ws)
        transport._connected = True

        disconnecting = asyncio.create_task(transport.disconnect())
        await ws.close_entered.wait()
        connecting = asyncio.create_task(transport.connect())
        await asyncio.sleep(0)

        assert not connecting.done()

        ws.release_close.set()
        await disconnecting
        with pytest.raises(RuntimeError, match="accepted connection is already closed"):
            await connecting

        assert not transport.is_connected
        assert transport._receive_task is None
        assert transport._socket_close_pending is False
        assert transport._disconnect_cleanup_error is None

    @pytest.mark.asyncio
    async def test_concurrent_connect_waits_for_and_shares_tentative_start_failure(
        self,
    ) -> None:
        class _DeferredFailureTransport(TwilioConnectionTransport):
            def __init__(self, ws: _DummyTwilioWebSocket) -> None:
                super().__init__(ws)
                self.start_entered = asyncio.Event()
                self.release_start = asyncio.Event()
                self._pending_start_message = {"event": "start"}

            async def _accept_start(
                self,
                *args: object,
                **kwargs: object,
            ) -> bool:
                self.start_entered.set()
                await self.release_start.wait()
                raise RuntimeError("deferred start failed")

        ws = _DummyTwilioWebSocket()
        transport = _DeferredFailureTransport(ws)
        first = asyncio.create_task(transport.connect())
        await transport.start_entered.wait()
        second = asyncio.create_task(transport.connect())
        await asyncio.sleep(0)

        assert not second.done()
        assert transport._receive_task is None
        assert transport._lifecycle_tasks.active("twilio-connection-connect")

        transport.release_start.set()
        with pytest.raises(RuntimeError, match="deferred start failed"):
            await first
        with pytest.raises(RuntimeError, match="deferred start failed"):
            await second

        assert not transport._lifecycle_tasks.active("twilio-connection-connect")
        assert not transport.is_connected
        assert transport._receive_task is None
        assert ws.closed_with == ()

    @pytest.mark.asyncio
    async def test_remote_eof_consumes_accepted_socket_until_disconnect_cleanup(
        self,
    ) -> None:
        class _EOFWebSocket(_DummyTwilioWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.receive_started = asyncio.Event()
                self.release_receive = asyncio.Event()

            def __aiter__(self) -> _EOFWebSocket:
                return self

            async def __anext__(self) -> str:
                self.receive_started.set()
                await self.release_receive.wait()
                raise StopAsyncIteration

        ws = _EOFWebSocket()
        transport = TwilioConnectionTransport(ws)
        await transport.connect()
        receive_task = transport._receive_task
        assert receive_task is not None
        await ws.receive_started.wait()
        connection = transport._connection_epoch.capture()
        assert connection.guard()
        assert connection.value is ws
        ws.release_receive.set()
        await receive_task

        assert not transport.is_connected
        assert not connection.guard()
        assert transport._connection_epoch.capture().value is None
        assert transport._socket_close_pending is True
        with pytest.raises(RuntimeError, match="has ended; call disconnect"):
            await transport.connect()

        assert transport._receive_task is receive_task
        await transport.disconnect()
        assert transport._socket_close_pending is False
        assert ws.closed_with == ()

    @pytest.mark.asyncio
    async def test_receive_task_uses_attached_transport_scope(self) -> None:
        ws = _ScriptedTwilioWebSocket()
        transport = TwilioConnectionTransport(ws)
        root = RuntimeScope.create_root(
            name="session",
            root_id="test-root:twilio-receive",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        transport.set_runtime_scope(root, name="transport-runtime")

        await transport.connect()
        await ws.entered.wait()
        connection = transport._connection_epoch.capture()

        assert root.tasks("twilio_receive") == (transport._receive_task,)
        assert "transport-receive" in root.cohorts(force=False)
        assert connection.guard()
        assert connection.value is ws

        await transport.disconnect()

        assert not connection.guard()
        assert transport._connection_epoch.capture().value is None
        assert not root.tasks("twilio_receive")
        assert ws.closed_with == ()

    @pytest.mark.asyncio
    async def test_disconnect_during_deferred_start_does_not_spawn_receiver(
        self,
    ) -> None:
        store = TwilioStreamTokenStore("secret")
        ws = _ScriptedTwilioWebSocket(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: store.issue()},
            )
        )
        bus = EventBus()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def block_start(_event: CallAnswered) -> None:
            entered.set()
            await release.wait()

        bus.subscribe(CallAnswered, block_start)
        transport = TwilioConnectionTransport(
            ws,
            event_bus=bus,
            config=TwilioTransportConfig(stream_token_validator=store.consume),
        )
        assert await transport.wait_for_start()
        connecting = asyncio.create_task(transport.connect())
        await entered.wait()

        await transport.disconnect()
        release.set()

        with pytest.raises(ConnectionError, match="disconnected during connect"):
            await connecting
        assert not transport.is_connected
        assert transport._receive_task is None

    @pytest.mark.asyncio
    async def test_context_validator_receives_start_fields_and_merges_claims(self) -> None:
        seen: list[StreamTokenContext] = []

        def validator(context: StreamTokenContext) -> dict[str, object]:
            seen.append(context)
            return {"tenant_id": "verified-tenant", "attempt": 3}

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={
                    TWILIO_STREAM_TOKEN_PARAMETER: "token-1",
                    "tenant_id": "spoofed-tenant",
                    "crm_account_id": "ACC-42",
                },
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"
        assert len(seen) == 1
        assert seen[0].token == "token-1"
        assert seen[0].stream_sid == "STREAM1"
        assert seen[0].call_sid == "CALL1"
        assert seen[0].parameters[TWILIO_STREAM_TOKEN_PARAMETER] == "token-1"
        assert transport.call_identity.custom_fields == {
            "tenant_id": "verified-tenant",
            "crm_account_id": "ACC-42",
            "attempt": "3",
        }

    @pytest.mark.asyncio
    async def test_async_context_validator_is_awaited(self) -> None:
        async def validator(ctx: StreamTokenContext) -> bool:
            await asyncio.sleep(0)
            return ctx.call_sid == "CALL1"

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

    @pytest.mark.asyncio
    async def test_keyword_only_context_validator_is_called_by_keyword(self) -> None:
        seen: list[StreamTokenContext] = []

        def validator(*, context: StreamTokenContext) -> bool:
            seen.append(context)
            return context.token == "token-1"

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert [context.token for context in seen] == ["token-1"]
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

    @pytest.mark.asyncio
    async def test_str_annotation_overrides_context_like_parameter_name(self) -> None:
        seen: list[str] = []

        def validator(context: str) -> bool:
            seen.append(context)
            return context == "token-1"

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert seen == ["token-1"]
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

    @pytest.mark.asyncio
    async def test_non_context_annotation_overrides_context_like_parameter_name(self) -> None:
        seen: list[object] = []

        def validator(ctx: object) -> bool:
            seen.append(ctx)
            return ctx == "token-1"

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert seen == ["token-1"]
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

    @pytest.mark.asyncio
    async def test_unannotated_legacy_validator_still_receives_raw_token(self) -> None:
        seen: list[object] = []

        def validator(candidate):  # type: ignore[no-untyped-def]
            seen.append(candidate)
            return candidate == "token-1"

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert seen == ["token-1"]
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

    @pytest.mark.asyncio
    async def test_unannotated_context_named_validator_receives_raw_token(self) -> None:
        seen: list[object] = []

        def validator(context):  # type: ignore[no-untyped-def]
            seen.append(context)
            return context == "token-1"

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))
        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert seen == ["token-1"]
        assert transport.stream_sid == "STREAM1"

    @pytest.mark.asyncio
    async def test_aliased_context_annotation_opts_into_context(self) -> None:
        seen: list[StreamTokenContext] = []

        def validator(value: TwilioContextAlias) -> bool:
            seen.append(value)
            return True

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))
        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert seen[0].stream_sid == "STREAM1"

    @pytest.mark.asyncio
    async def test_required_context_parameter_follows_optional_parameter(self) -> None:
        seen: list[StreamTokenContext] = []

        def validator(
            verbose: bool = False,
            *,
            context: StreamTokenContext,
        ) -> bool:
            assert not verbose
            seen.append(context)
            return True

        transport = TwilioTransport(
            TwilioTransportConfig(stream_token_validator=validator)  # type: ignore[arg-type]
        )
        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert seen[0].token == "token-1"
        assert transport.stream_sid == "STREAM1"

    @pytest.mark.asyncio
    async def test_rejects_conflicting_stream_sid_before_validation(self) -> None:
        ws = _DummyTwilioWebSocket()
        called = False

        def validator(_token: str) -> bool:
            nonlocal called
            called = True
            return True

        message = json.loads(
            _twilio_start_msg(
                "NESTED",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )
        message["streamSid"] = "TOP"
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=validator),
        )

        await transport._handle_message(json.dumps(message))

        assert not called
        assert transport.stream_sid is None
        assert ws.closed_with == (4003, "Conflicting streamSid")

    @pytest.mark.asyncio
    async def test_validator_claims_cannot_restore_reserved_token(self) -> None:
        def validator(context: StreamTokenContext) -> dict[str, str]:
            return dict(context.parameters)

        transport = TwilioTransport(TwilioTransportConfig(stream_token_validator=validator))
        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert TWILIO_STREAM_TOKEN_PARAMETER not in transport.call_identity.custom_fields

    @pytest.mark.asyncio
    async def test_async_validator_timeout_rejects_stream(self) -> None:
        async def validator(_token: str) -> bool:
            await asyncio.Event().wait()
            return True

        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(
                stream_token_validator=validator,
                stream_token_validation_timeout_s=0.01,
            ),
        )

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert transport.stream_sid is None
        assert ws.closed_with == (4003, "Missing or invalid stream token")

    @pytest.mark.asyncio
    async def test_sync_validator_timeout_rejects_without_blocking_event_loop(self) -> None:
        release = threading.Event()

        def validator(_token: str) -> bool:
            release.wait(timeout=0.2)
            return True

        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(
                stream_token_validator=validator,
                stream_token_validation_timeout_s=0.01,
            ),
        )
        started = time.monotonic()
        try:
            await transport._handle_message(
                _twilio_start_msg(
                    "STREAM1",
                    "CALL1",
                    custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
                )
            )
        finally:
            release.set()

        assert time.monotonic() - started < 0.15
        assert transport.stream_sid is None
        assert ws.closed_with == (4003, "Missing or invalid stream token")

    @pytest.mark.asyncio
    async def test_raising_validator_rejects_stream(self) -> None:
        def validator(_token: str) -> bool:
            raise RuntimeError("validator unavailable")

        ws = _DummyTwilioWebSocket()
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=validator),
        )

        await transport._handle_message(
            _twilio_start_msg(
                "STREAM1",
                "CALL1",
                custom_parameters={TWILIO_STREAM_TOKEN_PARAMETER: "token-1"},
            )
        )

        assert transport.stream_sid is None
        assert ws.closed_with == (4003, "Missing or invalid stream token")


class TestTwilioDtmfParsingInTransports:
    @pytest.mark.asyncio
    async def test_server_transport_uses_shared_dtmf_parser(self) -> None:
        event_bus = EventBus()
        digits: list[str] = []
        event_bus.subscribe(DTMF, lambda event: digits.append(event.digit))
        transport = TwilioTransport(event_bus=event_bus)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_dtmf_msg("a", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_dtmf_msg("X", stream_sid="STREAM1"))
        await transport._handle_message(
            json.dumps(
                {
                    "event": "dtmf",
                    "streamSid": "STREAM1",
                    "dtmf": {"digit": "12"},
                }
            )
        )

        assert digits == ["A"]

    @pytest.mark.asyncio
    async def test_connection_transport_uses_shared_dtmf_parser(self) -> None:
        event_bus = EventBus()
        digits: list[str] = []
        event_bus.subscribe(DTMF, lambda event: digits.append(event.digit))
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_dtmf_msg("b", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_dtmf_msg("", stream_sid="STREAM1"))
        await transport._handle_message(
            json.dumps({"event": "dtmf", "streamSid": "STREAM1", "dtmf": "5"})
        )

        assert digits == ["B"]


class TestTwilioSharedBusSessionCorrelation:
    @pytest.mark.asyncio
    async def test_builtin_events_stay_with_their_owning_session(self) -> None:
        event_bus = EventBus()
        left_events: list[Event] = []
        right_events: list[Event] = []

        def collect_left(event: Event) -> None:
            if event.session_id == "session-left":
                left_events.append(event)

        def collect_right(event: Event) -> None:
            if event.session_id == "session-right":
                right_events.append(event)

        event_bus.subscribe_all(collect_left)
        event_bus.subscribe_all(collect_right)
        left = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)
        right = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)
        left.set_session_id("session-left")
        right.set_session_id("session-right")

        async def emit_lifecycle(
            transport: TwilioConnectionTransport,
            *,
            stream_sid: str,
            call_sid: str,
        ) -> None:
            await transport._handle_message(_twilio_start_msg(stream_sid, call_sid))
            await transport._handle_message(_twilio_mark_msg("mark-1", stream_sid))
            await transport._handle_message(_twilio_dtmf_msg("5", stream_sid))
            transport._emit_degraded("test-session-correlation")
            await _drain_transport_diagnostics(transport)
            await transport._handle_message(_twilio_stop_msg(stream_sid))

        await emit_lifecycle(left, stream_sid="STREAM-LEFT", call_sid="CALL-LEFT")
        await emit_lifecycle(right, stream_sid="STREAM-RIGHT", call_sid="CALL-RIGHT")

        expected_types = {
            CallAnswered,
            PlaybackMarkAck,
            DTMF,
            TransportDegraded,
            CallEnded,
        }
        assert {type(event) for event in left_events} == expected_types
        assert {type(event) for event in right_events} == expected_types
        assert len(left_events) == len(expected_types)
        assert len(right_events) == len(expected_types)
        assert all(event.session_id == "session-left" for event in left_events)
        assert all(event.session_id == "session-right" for event in right_events)


class TestTwilioStreamGapDiagnostics:
    @pytest.mark.asyncio
    async def test_server_transport_emits_sequence_and_timestamp_gap_diagnostics(
        self,
    ) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="3",
                timestamp="0",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="4",
                timestamp="60",
            )
        )
        await _drain_transport_diagnostics(transport)

        assert [event.reason for event in degraded] == [
            _DEGRADED_TWILIO_SEQUENCE_GAP,
            _DEGRADED_TWILIO_TIMESTAMP_GAP,
        ]
        assert degraded[0].provider == "telephony"
        assert "expected sequenceNumber 2, got 3" in degraded[0].detail
        assert "expected media timestamp 20ms, got 60ms" in degraded[1].detail

    @pytest.mark.asyncio
    async def test_connection_transport_emits_sequence_gap_for_active_controls(
        self,
    ) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            json.dumps(
                {
                    "event": "dtmf",
                    "sequenceNumber": "4",
                    "streamSid": "STREAM1",
                    "dtmf": {"digit": "5"},
                }
            )
        )
        await _drain_transport_diagnostics(transport)

        assert [event.reason for event in degraded] == [_DEGRADED_TWILIO_SEQUENCE_GAP]
        assert "expected sequenceNumber 2, got 4" in degraded[0].detail

    @pytest.mark.asyncio
    async def test_malformed_media_metadata_breaks_gap_tracking_continuity(
        self,
    ) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="2",
                timestamp="0",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="",
                timestamp="",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="4",
                timestamp="40",
            )
        )
        await _drain_transport_diagnostics(transport)

        assert degraded == []

    @pytest.mark.asyncio
    async def test_boolean_media_metadata_breaks_gap_tracking_continuity(self) -> None:
        event_bus = EventBus()
        degraded: list[TransportDegraded] = []
        event_bus.subscribe(TransportDegraded, lambda event: degraded.append(event))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="2",
                timestamp="0",
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number=True,  # type: ignore[arg-type]
                timestamp=True,  # type: ignore[arg-type]
            )
        )
        await transport._handle_message(
            _twilio_media_msg(
                mulaw_data,
                "STREAM1",
                sequence_number="4",
                timestamp="40",
            )
        )
        await _drain_transport_diagnostics(transport)

        assert degraded == []


class _BlockingTwilioWebSocket:
    """Async-iterable fake ws that blocks in ``__anext__`` until released.

    Mirrors ``_RaceServerWS`` in ``test_degraded_events`` but is async-iterable
    so it can suspend inside ``TwilioTransport._handle_connection``'s inline
    ``async for raw in ws`` receive loop.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_sends = False
        self.closed_with: tuple[object, ...] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __aiter__(self) -> _BlockingTwilioWebSocket:
        return self

    async def __anext__(self) -> str:
        self.entered.set()
        await self.release.wait()
        raise StopAsyncIteration

    async def send(self, message: str) -> None:
        if self.fail_sends:
            close_frame = websockets.frames.Close(1006, "abnormal")
            raise websockets.exceptions.ConnectionClosed(close_frame, None)
        self.sent.append(message)

    async def close(self, *args: object) -> None:
        self.closed_with = args


class TestTwilioStreamLifecycleRaces:
    @pytest.mark.asyncio
    async def test_server_transport_stale_finally_does_not_tear_down_replacement(
        self,
    ) -> None:
        bus = EventBus()
        ended: list[str] = []
        bus.subscribe(CallEnded, lambda event: ended.append(event.call_sid))
        transport = TwilioTransport(event_bus=bus)

        old_ws = _BlockingTwilioWebSocket()
        new_ws = _BlockingTwilioWebSocket()

        old_task = asyncio.create_task(transport._handle_connection(old_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(old_ws.entered.wait(), timeout=1.0)

        # Establish an active stream so ``send_audio`` actually attempts a send.
        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))

        # ``send_audio`` notices the closed socket, emits the dead call's
        # ``CallEnded`` while it still owns the slot, then clears it, letting a
        # replacement connection be accepted before the stale ``finally`` runs.
        old_ws.fail_sends = True
        assert await transport.send_audio(make_chunk()) is False
        assert transport._ws is None
        assert ended == ["CALL1"]

        new_task = asyncio.create_task(transport._handle_connection(new_ws))  # type: ignore[arg-type]
        await asyncio.wait_for(new_ws.entered.wait(), timeout=1.0)
        assert transport._ws is new_ws
        await transport._handle_message(_twilio_start_msg("STREAM2", "CALL2"))

        # Now let the stale old handler's ``finally`` run.
        old_ws.release.set()
        await asyncio.wait_for(old_task, timeout=1.0)
        await transport._drain_emit_tasks()

        # The replacement must survive: the stale finally must not null ``_ws``,
        # wipe the new stream, emit a spurious CallEnded for the replacement
        # call, or poison the queue.
        assert transport._ws is new_ws
        assert transport.stream_sid == "STREAM2"
        assert transport.call_sid == "CALL2"
        assert ended == ["CALL1"]
        assert transport._in_queue.empty()

        new_ws.release.set()
        await asyncio.wait_for(new_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_server_transport_ignores_stale_stop_and_media_after_new_start(
        self,
    ) -> None:
        event_bus = EventBus()
        ended: list[str] = []
        event_bus.subscribe(CallEnded, lambda event: ended.append(event.call_sid))
        transport = TwilioTransport(event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_start_msg("STREAM2", "CALL2"))
        await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM1"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM1"))

        assert transport.stream_sid == "STREAM2"
        assert transport.call_sid == "CALL2"
        assert transport._in_queue.empty()
        assert ended == []

        for _ in range(4):
            await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM2"))
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM2"))

        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ended == ["CALL2"]

    @pytest.mark.asyncio
    async def test_connection_transport_ignores_stale_stop_and_media_after_new_start(
        self,
    ) -> None:
        event_bus = EventBus()
        ended: list[str] = []
        event_bus.subscribe(CallEnded, lambda event: ended.append(event.call_sid))
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=event_bus)
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_start_msg("STREAM2", "CALL2"))
        await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM1"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM1"))

        assert transport.stream_sid == "STREAM2"
        assert transport.call_sid == "CALL2"
        assert transport._in_queue.empty()
        assert ended == []

        for _ in range(4):
            await transport._handle_message(_twilio_media_msg(mulaw_data, "STREAM2"))
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM2"))

        assert transport.stream_sid is None
        assert transport.call_sid is None
        assert ended == ["CALL2"]


class TestTwilioProtocolConsolidation:
    """Lock the shared Twilio protocol mixin and the drift it fixed."""

    def test_both_transports_share_protocol_mixin(self) -> None:
        # Structural lock: prevents the two classes from re-forking their own
        # verbatim copy of the Media Streams wire protocol.
        assert issubclass(TwilioTransport, _TwilioProtocolMixin)
        assert issubclass(TwilioConnectionTransport, _TwilioProtocolMixin)

    def test_router_registers_every_twilio_inbound_event(self) -> None:
        assert set(_TwilioProtocolMixin._MESSAGE_HANDLERS) == {
            "connected",
            "start",
            "media",
            "dtmf",
            "stop",
            "mark",
        }

    @pytest.mark.parametrize(
        "raw",
        ["{", "[]", json.dumps({"event": []}), '{"event":' + "9" * 5000 + "}"],
    )
    @pytest.mark.asyncio
    async def test_router_ignores_malformed_messages(self, raw: str) -> None:
        transport = TwilioTransport(event_bus=EventBus())

        await transport._handle_message(raw)

        assert transport.stream_sid is None
        assert transport.call_sid is None

    @pytest.mark.asyncio
    async def test_router_ignores_mark_with_non_string_name(self) -> None:
        event_bus = EventBus()
        marks: list[str] = []
        event_bus.subscribe(PlaybackMarkAck, lambda event: marks.append(event.mark_name))
        transport = TwilioTransport(event_bus=event_bus)
        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))

        await transport._handle_message(
            json.dumps(
                {
                    "event": "mark",
                    "streamSid": "STREAM1",
                    "mark": {"name": ["invalid"]},
                }
            )
        )

        assert marks == []

    @pytest.mark.asyncio
    async def test_connection_transport_logs_connected_and_unknown_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The connection variant previously dropped the ``connected`` branch and
        # the unknown-event log; the shared copy restores both.
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket(), event_bus=EventBus())
        with caplog.at_level(logging.DEBUG, logger="easycat.transports.twilio_media"):
            await transport._handle_message(_twilio_connected_msg())
            await transport._handle_message(json.dumps({"event": "bogus"}))
        assert "Twilio connected event" in caplog.text
        assert "Unknown Twilio event" in caplog.text

    @pytest.mark.asyncio
    async def test_server_transport_logs_connected_and_unknown_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = TwilioTransport(event_bus=EventBus())
        with caplog.at_level(logging.DEBUG, logger="easycat.transports.twilio_media"):
            await transport._handle_message(_twilio_connected_msg())
            await transport._handle_message(json.dumps({"event": "bogus"}))
        assert "Twilio connected event" in caplog.text
        assert "Unknown Twilio event" in caplog.text


@pytest.mark.integration_socket
class TestTwilioTransport(_UsesPytestTcpPortFactory):
    """Tests for TwilioTransport with mocked Twilio messages."""

    def test_audio_contract_declares_distinct_tts_preference(self):
        transport = TwilioTransport(TwilioTransportConfig())

        assert transport.audio_format == PCM16_MONO_16K
        assert transport.preferred_tts_output_format == PCM16_MONO_8K

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)

        await transport.connect()
        assert transport.is_connected
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_receive_audio_from_twilio(self):
        """Twilio media messages produce PCM16 audio chunks."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        received: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                received.append(chunk)
                if len(received) >= 1:
                    break

        collect_task = asyncio.create_task(collect())

        # Simulate Twilio client.
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())

            # Create some mulaw audio (160 samples = 20ms at 8kHz).
            pcm_silence = bytes(320)  # 160 samples * 2 bytes
            mulaw_data = pcm16_to_mulaw(pcm_silence, source_rate=8000)
            for _ in range(4):
                await ws.send(_twilio_media_msg(mulaw_data))

            await asyncio.wait_for(collect_task, timeout=2.0)

        await transport.disconnect()
        assert len(received) == 1
        assert received[0].format.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_media_frame_guard_filters_prestart_wrong_stream_and_outbound_tracks(
        self,
    ):
        """Server transport only accepts inbound media for the active streamSid."""
        transport = TwilioTransport(TwilioTransportConfig())
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_media_msg_with_track(mulaw_data))
        assert transport._in_queue.empty()

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="WRONG", track="inbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="outbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(
                mulaw_data,
                stream_sid="STREAM1",
                track="outbound_track",
            )
        )
        assert transport._in_queue.empty()

        for _ in range(4):
            await transport._handle_message(
                _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="inbound")
            )
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_connection_media_frame_guard_filters_prestart_wrong_stream_and_outbound_tracks(
        self,
    ):
        """Connection transport uses the same Twilio inbound media guard."""
        transport = TwilioConnectionTransport(_DummyTwilioWebSocket())
        mulaw_data = pcm16_to_mulaw(bytes(320), source_rate=8000)

        await transport._handle_message(_twilio_media_msg_with_track(mulaw_data))
        assert transport._in_queue.empty()

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="WRONG", track="inbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="outbound")
        )
        await transport._handle_message(
            _twilio_media_msg_with_track(
                mulaw_data,
                stream_sid="STREAM1",
                track="outbound_track",
            )
        )
        assert transport._in_queue.empty()

        for _ in range(4):
            await transport._handle_message(
                _twilio_media_msg_with_track(mulaw_data, stream_sid="STREAM1", track="inbound")
            )
        chunk = transport._in_queue.get_nowait()
        assert chunk is not None
        assert chunk.format.sample_rate == 16000

    @pytest.mark.asyncio
    async def test_send_audio_to_twilio(self):
        """Audio sent via send_audio is received by Twilio as a base64 media message."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg("STREAM1"))
            await asyncio.sleep(0.1)

            assert transport.stream_sid == "STREAM1"

            # Send PCM16 audio chunk.
            chunk = _make_chunk(640, sample_rate=16000)
            await transport.send_audio(chunk)

            # Receive the media message from server.
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "media"
            assert msg["streamSid"] == "STREAM1"
            # Verify the payload is valid base64 mulaw.
            payload = base64.b64decode(msg["media"]["payload"])
            assert len(payload) > 0

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_playback_mark_to_twilio(self):
        """Playback marks are sent as Twilio mark messages."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg("STREAM1"))
            await asyncio.sleep(0.1)

            mark_name = await transport.send_playback_mark("unit_mark")
            assert mark_name == "unit_mark"

            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["event"] == "mark"
            assert msg["streamSid"] == "STREAM1"
            assert msg["mark"]["name"] == "unit_mark"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_playback_mark_without_stream_raises(self):
        """Playback marks without an active stream must not report success."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        with pytest.raises(RuntimeError, match="active stream"):
            await transport.send_playback_mark("unit_mark")

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_dtmf_emitted_to_event_bus(self):
        """DTMF messages from Twilio are emitted as DTMF events."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        event_bus = EventBus()
        transport = TwilioTransport(config, event_bus=event_bus)

        digits_received: list[str] = []
        event_bus.subscribe(DTMF, lambda e: digits_received.append(e.digit))

        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_dtmf_msg("5"))
            await ws.send(_twilio_dtmf_msg("#"))
            await asyncio.sleep(0.1)

        await transport.disconnect()
        assert digits_received == ["5", "#"]

    @pytest.mark.asyncio
    async def test_mark_ack_emitted_to_event_bus(self):
        """Twilio mark messages are emitted as PlaybackMarkAck events."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        event_bus = EventBus()
        transport = TwilioTransport(config, event_bus=event_bus)

        marks_received: list[str] = []
        event_bus.subscribe(PlaybackMarkAck, lambda e: marks_received.append(e.mark_name))

        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_mark_msg("mark_1"))
            await ws.send(_twilio_mark_msg("mark_2"))
            await asyncio.sleep(0.1)

        await transport.disconnect()
        assert marks_received == ["mark_1", "mark_2"]

    @pytest.mark.asyncio
    async def test_control_events_ignore_wrong_stream_sid(self):
        """stop/mark/dtmf are scoped to the active Twilio streamSid."""
        event_bus = EventBus()
        transport = TwilioTransport(event_bus=event_bus)
        digits_received: list[str] = []
        marks_received: list[str] = []
        event_bus.subscribe(DTMF, lambda e: digits_received.append(e.digit))
        event_bus.subscribe(PlaybackMarkAck, lambda e: marks_received.append(e.mark_name))

        await transport._handle_message(_twilio_start_msg("STREAM1", "CALL1"))
        await transport._handle_message(_twilio_dtmf_msg("5", stream_sid="WRONG"))
        await transport._handle_message(_twilio_mark_msg("mark_1", stream_sid="WRONG"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="WRONG"))

        assert digits_received == []
        assert marks_received == []
        assert transport.stream_sid == "STREAM1"
        assert transport.call_sid == "CALL1"

        await transport._handle_message(_twilio_dtmf_msg("6", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_mark_msg("mark_2", stream_sid="STREAM1"))
        await transport._handle_message(_twilio_stop_msg(stream_sid="STREAM1"))

        assert digits_received == ["6"]
        assert marks_received == ["mark_2"]
        assert transport.stream_sid is None
        assert transport.call_sid is None

    @pytest.mark.asyncio
    async def test_stream_metadata(self):
        """stream_sid and call_sid are set from the start message."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg("MY_STREAM", "MY_CALL"))
            await asyncio.sleep(0.1)

            assert transport.stream_sid == "MY_STREAM"
            assert transport.call_sid == "MY_CALL"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_stop_message(self):
        """Twilio stop message ends the receive_audio iterator."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                chunks.append(chunk)

        collect_task = asyncio.create_task(collect())

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await ws.send(_twilio_start_msg())
            await ws.send(_twilio_stop_msg())

        # Client disconnected — collect should end.
        await asyncio.wait_for(collect_task, timeout=2.0)

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_wait_for_client_waits_for_new_twilio_connection_after_disconnect(
        self,
    ):
        """wait_for_client should clear after Twilio socket disconnects."""
        port = self._unused_port()
        config = TwilioTransportConfig(host="127.0.0.1", port=port)
        transport = TwilioTransport(config)
        await transport.connect()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(_twilio_connected_msg())
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await asyncio.sleep(0.05)
        assert not transport.has_client

        with pytest.raises(asyncio.TimeoutError):
            await transport.wait_for_client(timeout=0.1)

        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
            await ws2.send(_twilio_connected_msg())
            await transport.wait_for_client(timeout=1.0)
            assert transport.has_client

        await transport.disconnect()


class TestTwilioSendAudioConnectionClosed:
    """Twilio transports clear live-client state when send_audio hits a drop.

    No real socket needed: a fake ws whose ``send`` raises
    ``ConnectionClosed`` drives the error path directly.
    """

    class _ClosedWS:
        async def send(self, _message):
            raise websockets.exceptions.ConnectionClosed(None, None)

        async def close(self):
            return None

    @pytest.mark.asyncio
    async def test_server_variant_clears_state_on_connection_closed(self):
        config = TwilioTransportConfig(host="127.0.0.1", port=0)
        transport = TwilioTransport(config)
        transport._ws = self._ClosedWS()
        transport._stream_sid = "STREAM1"
        transport._client_connected.set()

        delivered = await transport.send_audio(_make_chunk(640, sample_rate=16000))

        assert delivered is False
        # Stale live-client state is cleared so has_client/is_connected stop
        # reporting a live peer after the socket drops.
        assert transport._ws is None
        assert transport.stream_sid is None
        assert not transport.has_client
        assert not transport._client_connected.is_set()

    @pytest.mark.asyncio
    async def test_server_variant_mark_raises_and_clears_state_on_connection_closed(
        self,
    ):
        config = TwilioTransportConfig(host="127.0.0.1", port=0)
        transport = TwilioTransport(config)
        transport._ws = self._ClosedWS()
        transport._stream_sid = "STREAM1"
        transport._client_connected.set()

        with pytest.raises(RuntimeError, match="disconnected"):
            await transport.send_playback_mark("unit_mark")

        assert transport._ws is None
        assert transport.stream_sid is None
        assert not transport.has_client
        assert not transport._client_connected.is_set()

    @pytest.mark.asyncio
    async def test_connection_variant_clears_state_on_connection_closed(self):
        transport = TwilioConnectionTransport(self._ClosedWS())
        transport._stream_sid = "STREAM1"
        transport._connected = True
        transport._client_connected.set()

        delivered = await transport.send_audio(_make_chunk(640, sample_rate=16000))

        assert delivered is False
        assert transport.stream_sid is None
        assert not transport.is_connected
        assert not transport._client_connected.is_set()

    @pytest.mark.asyncio
    async def test_connection_variant_mark_raises_and_clears_state_on_connection_closed(
        self,
    ):
        transport = TwilioConnectionTransport(self._ClosedWS())
        transport._stream_sid = "STREAM1"
        transport._connected = True
        transport._client_connected.set()

        with pytest.raises(RuntimeError, match="disconnected"):
            await transport.send_playback_mark("unit_mark")

        assert transport.stream_sid is None
        assert not transport.is_connected
        assert not transport._client_connected.is_set()


class TestAudioConversion:
    """Tests for mulaw <-> PCM16 conversion helpers."""

    def test_mulaw_to_pcm16_silence(self):
        """Silent mulaw converts to (near) silent PCM16."""
        # mulaw silence is 0xFF.
        mulaw_silence = bytes([0xFF] * 160)
        pcm = mulaw_to_pcm16(mulaw_silence, target_rate=8000)
        # Should produce PCM16 samples.
        assert len(pcm) == 320  # 160 samples * 2 bytes

    def test_pcm16_to_mulaw_roundtrip(self):
        """PCM16 -> mulaw -> PCM16 round-trip preserves signal shape."""
        pcm_original = _make_sine_pcm16(freq=440, duration_ms=20, sample_rate=8000)
        mulaw = pcm16_to_mulaw(pcm_original, source_rate=8000)
        pcm_back = mulaw_to_pcm16(mulaw, target_rate=8000)

        # Lengths should match.
        assert len(pcm_back) == len(pcm_original)

        # Decode both and check correlation (mulaw is lossy, so values won't match exactly).
        n = len(pcm_original) // 2
        orig_samples = struct.unpack(f"<{n}h", pcm_original)
        back_samples = struct.unpack(f"<{n}h", pcm_back)

        # Correlation check: most samples should be within ~200 of original.
        diffs = [abs(a - b) for a, b in zip(orig_samples, back_samples)]
        avg_diff = sum(diffs) / len(diffs)
        assert avg_diff < 500, f"Average sample difference too high: {avg_diff}"

    def test_pcm16_to_mulaw_with_resampling(self):
        """PCM16 at 16kHz -> mulaw 8kHz produces the expected number of samples."""
        pcm_16k = _make_sine_pcm16(freq=440, duration_ms=20, sample_rate=16000)
        mulaw = pcm16_to_mulaw(pcm_16k, source_rate=16000)
        # 20ms at 8kHz = 160 samples; mulaw is 1 byte per sample.
        assert len(mulaw) == 160

    def test_mulaw_to_pcm16_with_upsampling(self):
        """mulaw 8kHz -> PCM16 16kHz produces the expected number of samples."""
        mulaw_data = bytes([0xFF] * 160)  # 20ms at 8kHz
        pcm = mulaw_to_pcm16(mulaw_data, target_rate=16000)
        # 20ms at 16kHz = 320 samples * 2 bytes = 640 bytes.
        assert len(pcm) == 640


class TestTwiML:
    """Tests for TwiML generation helpers."""

    def test_twiml_connect_stream(self):
        xml = twiml_connect_stream("wss://example.com/stream")
        assert '<?xml version="1.0"' in xml
        assert "<Connect>" in xml
        assert '<Stream url="wss://example.com/stream"' in xml
        assert 'track="both"' in xml
        assert "</Response>" in xml
        assert "<Parameter" not in xml

    def test_twiml_connect_stream_with_callback(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            status_callback_url="https://example.com/status",
        )
        assert 'statusCallback="https://example.com/status"' in xml

    def test_twiml_connect_stream_custom_track(self):
        xml = twiml_connect_stream("wss://example.com/stream", track="inbound")
        assert 'track="inbound"' in xml

    def test_twiml_connect_stream_disable_caller_id(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            forward_caller_id=False,
        )
        assert "<Parameter" not in xml
        assert "<Stream" in xml and "/>" in xml

    def test_twiml_connect_stream_custom_parameters(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            parameters={"crm_account_id": "ACC-42"},
        )
        assert '<Parameter name="crm_account_id"' in xml
        assert 'value="ACC-42"' in xml
        assert '<Parameter name="From"' not in xml

    def test_twiml_connect_stream_with_stream_token(self):
        xml = twiml_connect_stream("wss://example.com/stream", stream_token="token-1")
        assert f'<Parameter name="{TWILIO_STREAM_TOKEN_PARAMETER}" value="token-1"/>' in xml

    def test_twiml_connect_stream_explicit_caller_id_parameters(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            parameters={
                "Direction": "inbound",
                "From": "+15551234567",
                "To": "+15557654321",
            },
            forward_caller_id=True,
        )
        assert '<Parameter name="Direction" value="inbound"/>' in xml
        assert '<Parameter name="From" value="+15551234567"/>' in xml
        assert '<Parameter name="To" value="+15557654321"/>' in xml
        assert "{{From}}" not in xml

    def test_twiml_connect_stream_forward_caller_id_requires_values(self):
        with pytest.raises(ValueError, match="explicit caller-ID values"):
            twiml_connect_stream("wss://example.com/stream", forward_caller_id=True)
        with pytest.raises(ValueError, match="explicit caller-ID values"):
            twiml_connect_stream(
                "wss://example.com/stream",
                parameters={"From": "{{From}}"},
                forward_caller_id=True,
            )

    def test_twiml_connect_stream_escapes_parameter_values_once(self):
        xml = twiml_connect_stream(
            "wss://example.com/stream",
            parameters={"company": "AT&T <Gold>"},
            forward_caller_id=False,
        )
        assert '<Parameter name="company" value="AT&amp;T &lt;Gold&gt;"/>' in xml
        assert "amp;amp" not in xml

    def test_twiml_stream(self):
        xml = twiml_stream("wss://example.com/stream")
        assert "<Start>" in xml
        assert '<Stream url="wss://example.com/stream"' in xml
        assert 'track="inbound_track"' in xml
        assert "<Pause" in xml

    def test_twiml_stream_with_parameters_and_stream_token(self):
        xml = twiml_stream(
            "wss://example.com/stream",
            parameters={"crm_account_id": "ACC-42"},
            stream_token="token-1",
        )
        assert '<Parameter name="crm_account_id" value="ACC-42"/>' in xml
        assert f'<Parameter name="{TWILIO_STREAM_TOKEN_PARAMETER}" value="token-1"/>' in xml
        assert "</Stream>" in xml
