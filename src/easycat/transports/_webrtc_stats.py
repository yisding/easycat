"""Sanitized WebRTC client stats persistence primitives.

This module is intentionally independent of the WebRTC transport and server
layers.  Both signaling surfaces use the same sanitizer, append operation, and
per-server quota state without importing peer-connection lifecycle code.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_WEBRTC_STATS_TOP_LEVEL_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "sample_id",
        "sequence",
        "label",
        "captured_at",
        "connection_state",
        "ice_connection_state",
        "ice_gathering_state",
        "signaling_state",
    }
)
_WEBRTC_STATS_NESTED_FIELDS: dict[str, frozenset[str]] = {
    "candidate_pair": frozenset(
        {
            "available_incoming_bitrate",
            "available_outgoing_bitrate",
            "bytes_received",
            "bytes_sent",
            "consent_requests_sent",
            "current_round_trip_time_ms",
            "nominated",
            "packets_received",
            "packets_sent",
            "requests_received",
            "requests_sent",
            "responses_received",
            "responses_sent",
            "state",
        }
    ),
    "inbound_audio": frozenset(
        {
            "bytes_received",
            "concealed_samples",
            "concealment_events",
            "jitter_buffer_delay_ms",
            "jitter_ms",
            "packets_lost",
            "packets_received",
            "total_samples_received",
        }
    ),
    "outbound_audio": frozenset(
        {
            "bytes_sent",
            "packets_sent",
            "retransmitted_bytes_sent",
            "retransmitted_packets_sent",
            "target_bitrate",
            "total_packet_send_delay_ms",
        }
    ),
}


def default_webrtc_stats_path() -> str | None:
    """Return the configured default stats artifact path, if any."""
    return os.environ.get("EASYCAT_WEBRTC_STATS_PATH") or None


def _safe_stats_scalar(value: object) -> object | None:
    if isinstance(value, bool | int) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value.replace("\r", " ").replace("\n", " ")[:200]
    return None


def sanitize_webrtc_stats_snapshot(payload: object) -> dict[str, object]:
    """Keep only non-identifying browser WebRTC stats fields.

    Raw ``RTCPeerConnection.getStats()`` reports can include local/remote
    candidate addresses and implementation-specific IDs. The bundled browser
    client already summarizes safe fields, and this server-side filter keeps
    the validation artifact constrained even if a custom client posts more.
    """
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")  # noqa: TRY004 domain-specific validation error

    snapshot: dict[str, object] = {}
    for field_name in _WEBRTC_STATS_TOP_LEVEL_FIELDS:
        safe_value = _safe_stats_scalar(payload.get(field_name))
        if safe_value is not None:
            snapshot[field_name] = safe_value

    for group_name, allowed_fields in _WEBRTC_STATS_NESTED_FIELDS.items():
        group = payload.get(group_name)
        if not isinstance(group, dict):
            continue
        safe_group: dict[str, object] = {}
        for field_name in allowed_fields:
            safe_value = _safe_stats_scalar(group.get(field_name))
            if safe_value is not None:
                safe_group[field_name] = safe_value
        if safe_group:
            snapshot[group_name] = safe_group

    snapshot.setdefault("kind", "webrtc_client_stats")
    snapshot.setdefault("schema_version", 1)
    return snapshot


def append_webrtc_stats_record(stats_path: Path, snapshot: dict[str, object]) -> None:
    """Append one sanitized stats record to ``stats_path`` using blocking I/O."""
    record = json.dumps(snapshot, sort_keys=True, allow_nan=False) + "\n"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_path.open("a", encoding="utf-8") as handle:
        handle.write(record)


@dataclass
class WebRTCStatsState:
    """Per-server stats rate-limit window and record counter."""

    request_times: deque[float] = field(default_factory=deque)
    record_count: int | None = None
    # The handler offloads appends to a thread. Serialize quota check + append
    # + counter update so concurrent posts cannot observe the same counters.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
