import pytest

from easycat.audio_format import (
    PCM16_MONO_8K,
    PCM16_MONO_16K,
    PCM16_MONO_24K,
    PCM16_MONO_48K,
    AudioChunk,
    AudioFormat,
)


def test_audio_format_frame_size():
    fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
    assert fmt.frame_size == 2

    stereo = AudioFormat(sample_rate=16000, channels=2, sample_width=2)
    assert stereo.frame_size == 4


def test_audio_format_bytes_per_second():
    assert PCM16_MONO_8K.bytes_per_second == 16000
    assert PCM16_MONO_16K.bytes_per_second == 32000
    assert PCM16_MONO_24K.bytes_per_second == 48000
    assert PCM16_MONO_48K.bytes_per_second == 96000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sample_rate", 0),
        ("sample_rate", -1),
        ("sample_rate", True),
        ("sample_rate", 16_000.0),
        ("channels", 0),
        ("channels", False),
        ("sample_width", 0),
        ("sample_width", 2.0),
    ],
)
def test_audio_format_rejects_invalid_geometry(field: str, value: object) -> None:
    values: dict[str, object] = {
        "sample_rate": 16_000,
        "channels": 1,
        "sample_width": 2,
    }
    values[field] = value
    with pytest.raises(ValueError, match=field):
        AudioFormat(**values)  # type: ignore[arg-type]


def test_pcm16_mono_constants():
    assert PCM16_MONO_8K.sample_rate == 8000
    assert PCM16_MONO_8K.channels == 1
    assert PCM16_MONO_8K.sample_width == 2
    assert PCM16_MONO_8K.encoding == "pcm"

    assert PCM16_MONO_16K.sample_rate == 16000
    assert PCM16_MONO_16K.channels == 1
    assert PCM16_MONO_16K.sample_width == 2

    assert PCM16_MONO_24K.sample_rate == 24000
    assert PCM16_MONO_24K.channels == 1
    assert PCM16_MONO_24K.sample_width == 2

    assert PCM16_MONO_48K.sample_rate == 48000
    assert PCM16_MONO_48K.channels == 1
    assert PCM16_MONO_48K.sample_width == 2


def test_audio_chunk_num_samples():
    # 320 bytes at 16kHz mono 16-bit = 160 samples = 10ms
    data = bytes(320)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    assert chunk.num_samples == 160


def test_audio_chunk_duration_ms():
    # 160 samples at 16kHz = 10ms
    data = bytes(320)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    assert chunk.duration_ms == 10.0

    # 160 samples at 8kHz = 20ms
    data = bytes(320)
    chunk = AudioChunk(data=data, format=PCM16_MONO_8K)
    assert chunk.duration_ms == 20.0


def test_audio_chunk_has_timestamp():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    assert isinstance(chunk.timestamp, float)
    assert chunk.timestamp > 0


def test_audio_chunk_routing_metadata_defaults_unset():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    assert chunk._easycat_replay_chunk is False
    assert chunk._easycat_session_id is None
    assert chunk._easycat_turn_id is None
    assert chunk._easycat_turn_ref is None


def test_audio_chunk_routing_metadata_excluded_from_eq_and_repr():
    # Two chunks with identical audio must compare equal regardless of the
    # outbound routing metadata stamped onto one of them, and that metadata
    # must not appear in repr (preserving the prior side-channel behavior).
    data = b"\x01\x02\x03\x04"
    a = AudioChunk(data=data, format=PCM16_MONO_16K, timestamp=1.0)
    b = AudioChunk(data=data, format=PCM16_MONO_16K, timestamp=1.0)
    b._easycat_replay_chunk = True
    b._easycat_session_id = "sess"
    b._easycat_turn_id = "turn"
    b._easycat_turn_ref = object()
    assert a == b
    assert "_easycat" not in repr(b)
