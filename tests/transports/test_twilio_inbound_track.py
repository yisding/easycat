"""Twilio transports declare an inbound-only STT track label.

``_accepted_twilio_media`` drops every ``outbound``/``outbound_track`` frame at
ingest, so audio reaching STT from a Twilio transport is the callee's speech.
Both transport classes therefore expose ``inbound_stt_track = "inbound"`` so the
Session wiring can stamp the label onto otherwise-unlabeled STT events.  These
are plain attribute checks — no socket bind — so they run outside the
``integration_socket`` gate.
"""

from __future__ import annotations

from easycat.transports.twilio_media import TwilioTransport


def test_twilio_transport_declares_inbound_stt_track() -> None:
    assert TwilioTransport.inbound_stt_track == "inbound"
    # Instance access resolves the class attribute too (no socket bind on init).
    assert TwilioTransport().inbound_stt_track == "inbound"


def test_twilio_connection_transport_declares_inbound_stt_track() -> None:
    # Imported lazily because the per-connection class lives alongside the
    # websockets server transport; the class attribute is what matters here.
    from easycat.transports.twilio_media import TwilioConnectionTransport

    assert TwilioConnectionTransport.inbound_stt_track == "inbound"
