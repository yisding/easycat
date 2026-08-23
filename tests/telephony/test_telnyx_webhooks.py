"""Telnyx webhook parsing, signature verification, and payload builder tests."""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    TransportDegraded,
    VoicemailDetected,
)
from easycat.telephony.telnyx import (
    TELNYX_DEFAULT_REPLAY_WINDOW_S,
    build_answer_payload,
    build_dial_payload,
    build_stream_parameters,
    decode_client_state,
    encode_client_state,
    parse_telnyx_call_event,
    parse_telnyx_webhook,
    telnyx_webhook_idempotency_key,
    verify_telnyx_webhook_signature,
)

# ── Helpers ───────────────────────────────────────────────────────


def envelope(event_type: str, payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": "evt-1", "event_type": event_type, "payload": payload, **extra}


def _ed25519_pair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    return key, public_b64


def sign(key: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    return base64.b64encode(key.sign(f"{timestamp}|".encode() + body)).decode("ascii")


# ── parse_telnyx_webhook ──────────────────────────────────────────


class TestParseTelnyxWebhook:
    def test_valid_envelope_passes(self) -> None:
        raw = b'{"id":"evt-1","event_type":"call.initiated","payload":{"call_control_id":"CC1"}}'

        parsed = parse_telnyx_webhook(raw)

        assert parsed == {
            "id": "evt-1",
            "event_type": "call.initiated",
            "payload": {"call_control_id": "CC1"},
        }

    @pytest.mark.parametrize("raw", [b"not json", b'{"event_type": ', ""])
    def test_non_json_returns_none(self, raw: bytes | str) -> None:
        assert parse_telnyx_webhook(raw) is None

    @pytest.mark.parametrize("raw", [b"[1,2]", b'"a string"', b"42", b"null"])
    def test_non_object_envelope_returns_none(self, raw: bytes) -> None:
        assert parse_telnyx_webhook(raw) is None

    @pytest.mark.parametrize("envelope_body", [{}, {"event_type": ""}, {"event_type": 7}])
    def test_missing_event_type_returns_none(self, envelope_body: dict[str, Any]) -> None:
        assert parse_telnyx_webhook(envelope_body) is None


# ── Idempotency key ───────────────────────────────────────────────


class TestTelnyxWebhookIdempotencyKey:
    def test_uses_delivery_id_when_present(self) -> None:
        assert telnyx_webhook_idempotency_key(envelope("call.answered", {})) == "evt-1"

    def test_fallback_digest_is_stable_without_id(self) -> None:
        first = {"event_type": "call.initiated", "occurred_at": "2026-01-01T00:00:00Z"}
        second = {"event_type": "call.initiated", "occurred_at": "2026-01-01T00:00:00Z"}

        key = telnyx_webhook_idempotency_key(first)

        assert key == telnyx_webhook_idempotency_key(second)
        assert len(key) == 64

    def test_distinct_events_get_distinct_fallback_keys(self) -> None:
        first = {"event_type": "call.initiated", "occurred_at": "2026-01-01T00:00:00Z"}
        second = {"event_type": "call.answered", "occurred_at": "2026-01-01T00:00:00Z"}

        assert telnyx_webhook_idempotency_key(first) != telnyx_webhook_idempotency_key(second)


# ── client_state round-trip ───────────────────────────────────────


class TestClientStateRoundTrip:
    def test_encode_decode_round_trip(self) -> None:
        state = {"call_sid": "CA123", "attempt": 2}

        decoded = decode_client_state(encode_client_state(state))

        assert decoded == state

    def test_encoded_state_is_plain_base64_json(self) -> None:
        encoded = encode_client_state({"k": "v"})

        assert base64.b64decode(encoded) == b'{"k": "v"}'

    @pytest.mark.parametrize("raw", ["!!!not-base64!!!", "", "YWJj", None, 123])
    def test_decode_of_garbage_returns_empty_dict(self, raw: Any) -> None:
        assert decode_client_state(raw) == {}


# ── Payload builders ──────────────────────────────────────────────


class TestBuildStreamParameters:
    def test_l16_defaults_match_internal_bus(self) -> None:
        parameters = build_stream_parameters(stream_url="wss://example.com/stream")

        assert parameters == {
            "stream_url": "wss://example.com/stream",
            "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "L16",
            "stream_bidirectional_sampling_rate": 16000,
            "send_silence_when_idle": True,
        }

    def test_pcmu_forces_8k_rate(self) -> None:
        parameters = build_stream_parameters(
            stream_url="wss://example.com/stream",
            codec="PCMU",
            sampling_rate=16000,
        )

        assert parameters["stream_bidirectional_codec"] == "PCMU"
        assert parameters["stream_bidirectional_sampling_rate"] == 8000

    def test_invalid_codec_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="L16"):
            build_stream_parameters(stream_url="wss://example.com/stream", codec="OPUS")

    def test_client_state_is_encoded_when_provided(self) -> None:
        parameters = build_stream_parameters(
            stream_url="wss://example.com/stream",
            client_state={"sid": "CA1"},
        )

        assert decode_client_state(parameters["client_state"]) == {"sid": "CA1"}


class TestBuildAnswerPayload:
    def test_stream_defaults_and_command_id_passthrough(self) -> None:
        payload = build_answer_payload(
            stream_url="wss://example.com/stream",
            command_id="cmd-1",
        )

        assert payload["stream_bidirectional_codec"] == "L16"
        assert payload["stream_bidirectional_sampling_rate"] == 16000
        assert payload["command_id"] == "cmd-1"

    def test_no_command_id_by_default(self) -> None:
        payload = build_answer_payload(stream_url="wss://example.com/stream")

        assert "command_id" not in payload


class TestBuildDialPayload:
    def test_dial_includes_routing_and_amd(self) -> None:
        payload = build_dial_payload(
            to="+15550001111",
            from_="+15550002222",
            connection_id="conn-1",
            stream_url="wss://example.com/stream",
            answering_machine_detection="premium",
        )

        assert payload["to"] == "+15550001111"
        assert payload["from"] == "+15550002222"
        assert payload["connection_id"] == "conn-1"
        assert payload["answering_machine_detection"] == "premium"
        assert payload["stream_bidirectional_codec"] == "L16"

    def test_dial_without_amd_omits_field(self) -> None:
        payload = build_dial_payload(
            to="+15550001111",
            from_="+15550002222",
            connection_id="conn-1",
            stream_url="wss://example.com/stream",
        )

        assert "answering_machine_detection" not in payload

    def test_empty_connection_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="connection_id"):
            build_dial_payload(
                to="+15550001111",
                from_="+15550002222",
                connection_id="",
                stream_url="wss://example.com/stream",
            )


# ── Signature verification ────────────────────────────────────────


class TestVerifyTelnyxWebhookSignature:
    TIMESTAMP = "1000000000"
    BODY = b'{"event_type":"call.initiated"}'

    def _verify(
        self,
        key: Ed25519PrivateKey,
        public_key_b64: str,
        *,
        body: bytes | None = None,
        signature: str | None = None,
        timestamp: str | None = None,
        now: Any | None = None,
    ) -> bool:
        return verify_telnyx_webhook_signature(
            payload=self.BODY if body is None else body,
            signature=sign(key, self.TIMESTAMP, self.BODY) if signature is None else signature,
            timestamp=self.TIMESTAMP if timestamp is None else timestamp,
            public_key=public_key_b64,
            now=now,
        )

    def test_valid_signature_verifies_true(self) -> None:
        key, public_key_b64 = _ed25519_pair()

        verified = self._verify(key, public_key_b64, now=lambda: float(self.TIMESTAMP))

        assert verified is True

    def test_non_canonical_base64_signature_rejects_false(self) -> None:
        """Strict b64 validation rejects signatures with embedded padding."""
        import base64

        key, public_key_b64 = _ed25519_pair()
        real_sig = sign(key, self.TIMESTAMP, self.BODY)
        tampered_sig = base64.b64encode(base64.b64decode(real_sig) + b"\x00extra").decode()

        verified = self._verify(
            key,
            public_key_b64,
            signature=tampered_sig,
            now=lambda: float(self.TIMESTAMP),
        )

        assert verified is False

    def test_bytes_and_str_payload_are_equivalent(self) -> None:
        key, public_key_b64 = _ed25519_pair()

        assert verify_telnyx_webhook_signature(
            payload=self.BODY.decode(),
            signature=sign(key, self.TIMESTAMP, self.BODY),
            timestamp=self.TIMESTAMP,
            public_key=public_key_b64,
            now=lambda: float(self.TIMESTAMP),
        )
        assert verify_telnyx_webhook_signature(
            payload=self.BODY,
            signature=sign(key, self.TIMESTAMP, self.BODY),
            timestamp=self.TIMESTAMP,
            public_key=public_key_b64,
            now=lambda: float(self.TIMESTAMP),
        )

    def test_wrong_signature_rejects_false(self) -> None:
        key, public_key_b64 = _ed25519_pair()
        other_key, _ = _ed25519_pair()

        verified = self._verify(
            key,
            public_key_b64,
            signature=sign(other_key, self.TIMESTAMP, self.BODY),
            now=lambda: float(self.TIMESTAMP),
        )

        assert verified is False

    def test_tampered_body_rejects_false(self) -> None:
        key, public_key_b64 = _ed25519_pair()

        verified = self._verify(
            key,
            public_key_b64,
            body=b'{"event_type":"call.initiated","payload":{}}',
            now=lambda: float(self.TIMESTAMP),
        )

        assert verified is False

    def test_stale_timestamp_beyond_tolerance_rejects_false(self) -> None:
        key, public_key_b64 = _ed25519_pair()
        stale_now = float(self.TIMESTAMP) + TELNYX_DEFAULT_REPLAY_WINDOW_S + 1

        verified = self._verify(key, public_key_b64, now=lambda: stale_now)

        assert verified is False

    def test_malformed_timestamp_rejects_false(self) -> None:
        key, public_key_b64 = _ed25519_pair()

        verified = self._verify(
            key,
            public_key_b64,
            timestamp="not-a-number",
            now=lambda: float(self.TIMESTAMP),
        )

        assert verified is False

    @pytest.mark.parametrize(("sig", "ts"), [(None, "1"), ("abc", None)])
    def test_missing_header_values_reject_false(self, sig: str | None, ts: str | None) -> None:
        _key, public_key_b64 = _ed25519_pair()

        verified = verify_telnyx_webhook_signature(
            payload=self.BODY,
            signature=sig,
            timestamp=ts,
            public_key=public_key_b64,
            now=lambda: float(self.TIMESTAMP),
        )

        assert verified is False

    def test_signature_exactly_at_tolerance_passes(self) -> None:
        key, public_key_b64 = _ed25519_pair()
        boundary_now = float(self.TIMESTAMP) + TELNYX_DEFAULT_REPLAY_WINDOW_S

        assert self._verify(key, public_key_b64, now=lambda: boundary_now) is True

    def test_signature_one_second_past_tolerance_fails(self) -> None:
        key, public_key_b64 = _ed25519_pair()
        past_now = float(self.TIMESTAMP) + TELNYX_DEFAULT_REPLAY_WINDOW_S + 1

        assert self._verify(key, public_key_b64, now=lambda: past_now) is False

    def test_default_clock_accepted_without_injection(self) -> None:
        key, public_key_b64 = _ed25519_pair()
        current_ts = str(int(dt.datetime.now(dt.UTC).timestamp()))

        verified = verify_telnyx_webhook_signature(
            payload=self.BODY,
            signature=base64.b64encode(key.sign(f"{current_ts}|".encode() + self.BODY)).decode(
                "ascii"
            ),
            timestamp=current_ts,
            public_key=public_key_b64,
        )

        assert verified is True


# ── Neutral event mapping ─────────────────────────────────────────


class TestParseTelnyxCallEvent:
    def test_call_initiated_maps_control_id_and_directions(self) -> None:
        event = parse_telnyx_call_event(
            envelope(
                "call.initiated",
                {"call_control_id": "CC9", "to": "+15550001111", "from": "+15550002222"},
            ),
            session_id="sess-1",
        )

        assert isinstance(event, CallInitiated)
        assert event.call_sid == "CC9"
        assert event.to == "+15550001111"
        assert event.from_ == "+15550002222"
        assert event.session_id == "sess-1"

    def test_call_ringing_maps_to_neutral_event(self) -> None:
        event = parse_telnyx_call_event(envelope("call.ringing", {"call_control_id": "CC9"}))

        assert isinstance(event, CallRinging)
        assert event.call_sid == "CC9"

    def test_call_answered_maps_to_neutral_event(self) -> None:
        event = parse_telnyx_call_event(envelope("call.answered", {"call_control_id": "CC9"}))

        assert isinstance(event, CallAnswered)
        assert event.call_sid == "CC9"

    @pytest.mark.parametrize(
        ("sip_code_raw", "expected"),
        [("486", 486), (486, 486), ("not-a-code", None), (None, None)],
    )
    def test_busy_hangup_maps_to_call_failed_with_sip_code(
        self, sip_code_raw: Any, expected: int | None
    ) -> None:
        payload: dict[str, Any] = {"call_control_id": "CC9", "hangup_cause": "busy"}
        if sip_code_raw is not None:
            payload["sip_code"] = sip_code_raw

        event = parse_telnyx_call_event(envelope("call.hangup", payload))

        assert isinstance(event, CallFailed)
        assert event.reason == "busy"
        assert event.sip_code == expected
        assert event.number == ""

    def test_no_answer_hangup_maps_to_call_failed(self) -> None:
        event = parse_telnyx_call_event(
            envelope(
                "call.hangup",
                {"call_control_id": "CC9", "hangup_cause": "no_answer", "sip_code": 408},
            )
        )

        assert isinstance(event, CallFailed)
        assert event.reason == "no_answer"
        assert event.sip_code == 408

    def test_normal_hangup_maps_to_call_ended_with_duration(self) -> None:
        event = parse_telnyx_call_event(
            envelope(
                "call.hangup",
                {
                    "call_control_id": "CC9",
                    "hangup_cause": "goodbye",
                    "call_duration": 42,
                },
            )
        )

        assert isinstance(event, CallEnded)
        assert event.duration_s == 42.0
        assert event.disposition == "goodbye"

    @pytest.mark.parametrize(
        ("result", "expected"),
        [("human", "human"), ("machine", "machine"), ("something_else", "unknown")],
    )
    def test_amd_detection_result_mapping(self, result: str, expected: str) -> None:
        event = parse_telnyx_call_event(
            envelope(
                "call.machine.detection.ended",
                {"call_control_id": "CC9", "result": result},
            )
        )

        assert isinstance(event, VoicemailDetected)
        assert event.result == expected
        assert event.source == "detector"
        assert event.call_sid == "CC9"

    def test_premium_greeting_ended_maps_to_machine(self) -> None:
        event = parse_telnyx_call_event(
            envelope(
                "call.machine.premium.greeting.ended",
                {"call_control_id": "CC9"},
            )
        )

        assert isinstance(event, VoicemailDetected)
        assert event.result == "machine"

    def test_streaming_failed_maps_to_fatal_degradation(self) -> None:
        event = parse_telnyx_call_event(
            envelope(
                "streaming.failed",
                {"call_control_id": "CC9", "failure_code": "100002", "failure_reason": "dropped"},
            )
        )

        assert isinstance(event, TransportDegraded)
        assert event.provider == "telnyx"
        assert event.fatal is True

    def test_unsupported_event_type_returns_none(self) -> None:
        assert parse_telnyx_call_event(envelope("call.foo.unsupported", {})) is None

    def test_hangup_without_call_control_id_returns_none(self) -> None:
        assert parse_telnyx_call_event(envelope("call.hangup", {"hangup_cause": "busy"})) is None
