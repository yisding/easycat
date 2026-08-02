"""WebTransport control codec tests."""

from __future__ import annotations

import struct
from typing import Any

from easycat.transports.webtransport import _ControlCodec


class TestControlCodec:
    def test_encode_round_trip(self) -> None:
        codec = _ControlCodec()
        msg = {"type": "config", "sample_rate": 48000}
        encoded = _ControlCodec.encode(msg)
        (length,) = struct.unpack_from(">I", encoded, 0)
        assert length == len(encoded) - 4
        assert codec.feed(encoded) == [msg]

    def test_feed_partial_frames(self) -> None:
        codec = _ControlCodec()
        msg = {"type": "ready"}
        encoded = _ControlCodec.encode(msg)
        out: list[dict[str, Any]] = []
        for i in range(len(encoded)):
            out.extend(codec.feed(encoded[i : i + 1]))
        assert out == [msg]

    def test_feed_multiple_frames_in_one_chunk(self) -> None:
        codec = _ControlCodec()
        a = _ControlCodec.encode({"type": "start"})
        b = _ControlCodec.encode({"type": "stop"})
        assert codec.feed(a + b) == [{"type": "start"}, {"type": "stop"}]

    def test_malformed_json_is_skipped(self) -> None:
        codec = _ControlCodec()
        bad = struct.pack(">I", 4) + b"\xff\xff\xff\xff"
        good = _ControlCodec.encode({"type": "ready"})
        assert codec.feed(bad + good) == [{"type": "ready"}]

    def test_large_integer_json_is_skipped(self) -> None:
        codec = _ControlCodec()
        body = b'{"type":' + b"9" * 5000 + b"}"
        bad = struct.pack(">I", len(body)) + body
        good = _ControlCodec.encode({"type": "ready"})

        assert codec.feed(bad + good) == [{"type": "ready"}]

    def test_oversized_length_prefix_poisons_codec(self) -> None:
        """A malicious uint32 length prefix must not pin a multi-GB buffer."""
        codec = _ControlCodec()
        # Advertise a frame bigger than the cap; codec should refuse to grow.
        oversized = struct.pack(">I", 1 << 30)  # 1 GiB
        assert codec.feed(oversized + b"X") == []
        assert codec.poisoned is True
        # Subsequent valid frames are now dropped — the stream is considered
        # malicious until the session resets it.
        good = _ControlCodec.encode({"type": "ready"})
        assert codec.feed(good) == []
