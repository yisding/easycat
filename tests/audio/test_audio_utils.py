"""Tests for audio utilities: resampling, mono downmix, chunk sizing."""

import math
import struct

import pytest

from easycat._audio_utils import (
    AudioFrameAligner,
    PCM16StreamResampler,
    chunk_frames,
    resample,
    resample_chunk,
    to_mono,
    to_mono_chunk,
)
from easycat.audio_format import (
    PCM16_MONO_8K,
    PCM16_MONO_16K,
    PCM16_MONO_24K,
    PCM16_MONO_48K,
    AudioChunk,
    AudioFormat,
)

# ── Resampling tests ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_resample_backend_state():
    """Reset the module-global resample backend cache and per-backend
    log-once tracking before each test so resample tests are
    order-independent (no cross-test coupling via process-wide globals)."""
    import easycat._audio_utils as au

    saved_backend = au._resolved_backend
    saved_logged = set(au._logged_runtime_failure)
    au._resolved_backend = None
    au._logged_runtime_failure.clear()
    try:
        yield
    finally:
        au._resolved_backend = saved_backend
        au._logged_runtime_failure.clear()
        au._logged_runtime_failure.update(saved_logged)


def test_resample_same_rate_noop():
    data = struct.pack("<4h", 100, 200, 300, 400)
    result = resample(data, 16000, 16000)
    assert result == data


def test_resample_8k_to_16k_doubles_samples():
    data = struct.pack("<4h", 0, 1000, 2000, 3000)
    result = resample(data, 8000, 16000)
    num_out = len(result) // 2
    assert num_out == 8


def test_resample_16k_to_8k_halves_samples():
    data = struct.pack("<8h", 0, 500, 1000, 1500, 2000, 2500, 3000, 3500)
    result = resample(data, 16000, 8000)
    num_out = len(result) // 2
    assert num_out == 4


def test_resample_preserves_dc_signal():
    # FIR-based resamplers (soxr, scipy.resample_poly) ring at the
    # boundaries; only the steady-state body should preserve DC to
    # ±1 LSB.  Use a long input and trim the settling region from
    # each end before comparing.
    value = 1234
    n_input = 2048
    data = struct.pack(f"<{n_input}h", *([value] * n_input))
    result = resample(data, 8000, 16000)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    trim = (len(samples) * 15) // 100
    body = samples[trim : len(samples) - trim]
    for s in body:
        assert abs(s - value) <= 1


def test_resample_chunk_updates_format():
    data = struct.pack("<4h", 100, 200, 300, 400)
    chunk = AudioChunk(data=data, format=PCM16_MONO_8K)
    result = resample_chunk(chunk, 16000)
    assert result.format.sample_rate == 16000
    assert result.format.channels == 1
    assert len(result.data) > len(chunk.data)


def test_resample_chunk_same_rate_returns_same():
    chunk = AudioChunk(data=b"\x00\x00\x00\x00", format=PCM16_MONO_16K)
    result = resample_chunk(chunk, 16000)
    assert result is chunk


def test_resample_chunk_resamples_stereo_channels_independently(monkeypatch):
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    stereo = AudioFormat(sample_rate=8000, channels=2, sample_width=2)
    data = struct.pack("<8h", *([10_000, -10_000] * 4))

    result = resample_chunk(AudioChunk(data=data, format=stereo), 16_000)

    samples = struct.unpack(f"<{len(result.data) // 2}h", result.data)
    assert samples[::2] == (10_000,) * 8
    assert samples[1::2] == (-10_000,) * 8
    assert result.format == AudioFormat(sample_rate=16_000, channels=2, sample_width=2)


def test_resample_chunk_drops_incomplete_multichannel_frame(monkeypatch):
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    stereo = AudioFormat(sample_rate=8000, channels=2, sample_width=2)
    # One complete stereo frame followed by an orphaned left-channel sample.
    data = struct.pack("<3h", 100, -100, 999)

    result = resample_chunk(AudioChunk(data=data, format=stereo), 16_000)

    assert struct.unpack("<4h", result.data) == (100, -100, 100, -100)
    assert len(result.data) % result.format.frame_size == 0


def test_resample_chunk_preserves_timestamp_and_routing_metadata(monkeypatch):
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    turn_ref = object()
    chunk = AudioChunk(data=struct.pack("<2h", 100, 200), format=PCM16_MONO_8K, timestamp=1.5)
    chunk._easycat_replay_chunk = True
    chunk._easycat_session_id = "session"
    chunk._easycat_turn_id = "turn"
    chunk._easycat_turn_ref = turn_ref
    chunk._easycat_capture_allowed = False

    result = resample_chunk(chunk, 16_000)

    assert result.timestamp == 1.5
    assert result._easycat_replay_chunk is True
    assert result._easycat_session_id == "session"
    assert result._easycat_turn_id == "turn"
    assert result._easycat_turn_ref is turn_ref
    assert result._easycat_capture_allowed is False


@pytest.mark.parametrize(
    "fmt",
    [
        AudioFormat(sample_rate=8000, channels=1, sample_width=1, encoding="pcm"),
        AudioFormat(sample_rate=8000, channels=1, sample_width=2, encoding="mulaw"),
    ],
)
def test_resample_chunk_rejects_non_pcm16_formats(fmt):
    with pytest.raises(ValueError, match="PCM16"):
        resample_chunk(AudioChunk(data=b"\x00\x00", format=fmt), 16_000)


@pytest.mark.parametrize(
    ("from_rate", "to_rate"),
    [
        (0, 16000),
        (16000, 0),
        (-8000, 16000),
        (16000, -8000),
    ],
)
def test_resample_rejects_non_positive_rates(from_rate: int, to_rate: int):
    with pytest.raises(ValueError, match="positive"):
        resample(b"\x00\x00", from_rate, to_rate)


@pytest.mark.parametrize(
    ("from_rate", "to_rate"),
    [
        (True, 16000),
        (16000, False),
        (16000.0, 16000),
        (16000, "8000"),
    ],
)
def test_resample_rejects_non_integer_rates(from_rate, to_rate):
    with pytest.raises(TypeError, match="integer"):
        resample(b"\x00\x00", from_rate, to_rate)


# ── Extended resampling: 24k and 48k rates ────────────────────────


RATE_PAIRS = [
    (8000, 24000),
    (8000, 48000),
    (16000, 24000),
    (16000, 48000),
    (24000, 8000),
    (24000, 16000),
    (24000, 48000),
    (48000, 8000),
    (48000, 16000),
    (48000, 24000),
    (24000, 44100),
    (44100, 16000),
    (44100, 48000),
    (48000, 44100),
    (24000, 22050),
    (48000, 22050),
]

# 44.1 kHz-family pairs whose float ratio is not exactly representable. The
# linear fallback used to divide by that rounded ratio and drop one sample.
FRACTIONAL_RATE_PAIRS = [
    (48000, 44100),
    (24000, 44100),
    (24000, 22050),
    (48000, 22050),
    (24000, 11025),
    (48000, 11025),
    (44100, 48000),
    (44100, 16000),
]


@pytest.mark.parametrize("from_rate,to_rate", RATE_PAIRS)
def test_resample_rate_pairs_sample_count(from_rate: int, to_rate: int):
    """Verify output sample count is correct for all supported rate pairs."""
    n_input = 480  # enough for any rate
    data = struct.pack(f"<{n_input}h", *([500] * n_input))
    result = resample(data, from_rate, to_rate)
    n_output = len(result) // 2
    expected = int(n_input * to_rate / from_rate)
    assert n_output == expected


@pytest.mark.parametrize("n_input", [240, 480, 960])
@pytest.mark.parametrize(
    "from_rate,to_rate", list(dict.fromkeys(RATE_PAIRS + FRACTIONAL_RATE_PAIRS))
)
def test_linear_resample_sample_count_is_exact_for_fractional_ratios(
    monkeypatch: pytest.MonkeyPatch, from_rate: int, to_rate: int, n_input: int
) -> None:
    """The linear fallback must emit floor(n * to_rate / from_rate) samples.

    It used to compute ``int(n / (from_rate / to_rate))``. For 48 kHz -> 44.1 kHz
    the rounded float ratio made 480 / ratio land at 440.999..., so every
    10 ms frame that fell back to linear was one sample short.
    """
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    data = struct.pack(f"<{n_input}h", *([500] * n_input))
    n_output = len(au.resample(data, from_rate, to_rate)) // 2
    assert n_output == (n_input * to_rate) // from_rate


def test_linear_resample_48k_to_44k1_10ms_frame_yields_441_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 10 ms 48 kHz frame (480 samples) is exactly 441 samples at 44.1 kHz."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    data = struct.pack("<480h", *([500] * 480))
    assert len(au._resample_linear(data, 48000, 44100)) // 2 == 441
    data = struct.pack("<240h", *([500] * 240))
    assert len(au._resample_linear(data, 24000, 44100)) // 2 == 441


@pytest.mark.parametrize("from_rate,to_rate", RATE_PAIRS)
def test_resample_rate_pairs_dc_preservation(from_rate: int, to_rate: int):
    """DC signal should be preserved across all rate pairs in the
    steady-state body.  Boundary samples ring (FIR edge artifact)
    and are excluded by trimming 15% from each end."""
    value = 2000
    n_input = 2048
    data = struct.pack(f"<{n_input}h", *([value] * n_input))
    result = resample(data, from_rate, to_rate)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    trim = (len(samples) * 15) // 100
    body = samples[trim : len(samples) - trim]
    for s in body:
        assert abs(s - value) <= 1


def _dc_body(samples: tuple[int, ...]) -> tuple[int, ...]:
    trim = (len(samples) * 15) // 100
    return samples[trim : len(samples) - trim]


@pytest.mark.parametrize("value", [1000, -1000, 12345, -12345, 32767, -32768])
@pytest.mark.parametrize("from_rate,to_rate", [(16000, 48000), (48000, 16000), (24000, 16000)])
def test_batch_soxr_resample_rounds_dc_exactly(value: int, from_rate: int, to_rate: int):
    """Batch soxr must round float output to nearest, not truncate toward zero.

    Truncating via ``astype(np.int16)`` turned a filter output of 999.9999
    into 999, so a DC 1000 came back as 999 in ~40% of steady-state samples
    (and -1000 as -999): a sign-dependent bias toward zero. The streaming
    soxr path rounds, so the batch path must match it exactly.
    """
    import easycat._audio_utils as au

    assert au.resample_backend() == "soxr"
    n_input = 1600
    data = struct.pack(f"<{n_input}h", *([value] * n_input))
    result = resample(data, from_rate, to_rate)
    body = _dc_body(struct.unpack(f"<{len(result) // 2}h", result))
    assert body
    assert set(body) == {value}


@pytest.mark.parametrize("from_rate,to_rate", [(16000, 48000), (48000, 16000), (24000, 16000)])
def test_batch_scipy_resample_rounds_to_nearest(from_rate: int, to_rate: int):
    """Batch scipy must round float output to nearest, like the soxr paths.

    ``resample_poly`` does not reproduce DC exactly for every ratio, so compare
    against its own float output: rounding stays within 0.5 LSB, whereas the
    old truncation toward zero was off by up to 1 LSB on both signs.
    """
    pytest.importorskip("scipy.signal")
    np = pytest.importorskip("numpy")
    from scipy.signal import resample_poly

    import easycat._audio_utils as au

    n_input = 1600
    source = [
        int(12000 * math.sin(2 * math.pi * 440 * index / from_rate)) for index in range(n_input)
    ]
    data = struct.pack(f"<{n_input}h", *source)
    result = au._resample_scipy_impl(data, from_rate, to_rate)
    out = np.frombuffer(result, dtype=np.int16).astype(np.float64)
    g = math.gcd(from_rate, to_rate)
    samples = np.asarray(source, dtype=np.float32) / 32768.0
    expected = resample_poly(samples, to_rate // g, from_rate // g) * 32768.0
    assert out.shape == expected.shape
    assert float(np.max(np.abs(out - expected))) <= 0.5 + 1e-6


# ── Odd-length chunk handling (split 16-bit sample) ───────────────


def test_resample_odd_length_does_not_crash():
    """An odd byte count (a sample split across a streaming chunk) must not
    raise struct.error; the trailing byte is dropped instead."""
    result = resample(b"\x01\x02\x03", 16000, 24000)
    assert isinstance(result, bytes)


def test_resample_single_byte_returns_empty():
    """A lone byte carries no complete sample and resamples to nothing."""
    assert resample(b"\x01", 16000, 24000) == b""


def test_resample_odd_length_linear_fallback_does_not_crash(monkeypatch):
    """The pure-Python (no numpy/soxr/scipy) path must also tolerate an odd
    trailing byte rather than raising struct.error."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    result = au.resample(b"\x01\x02\x03\x04\x05", 16000, 8000)
    assert isinstance(result, bytes)


def test_resample_runtime_failure_retries_high_quality_backend(monkeypatch, caplog):
    """A transient soxr failure must fall back to linear for that chunk only
    and retry soxr on the next chunk, rather than permanently pinning the whole
    process to the linear resampler (the original one-strike-out bug)."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "soxr")

    calls: list[bytes] = []

    def flaky_soxr(data, from_rate, to_rate):
        calls.append(data)
        if len(calls) == 1:
            raise RuntimeError("transient native-lib hiccup")
        return b"\x00\x00" * 4

    monkeypatch.setattr(au, "_resample_soxr_impl", flaky_soxr, raising=False)

    payload = struct.pack("<4h", 0, 1000, 2000, 3000)

    # First call: soxr raises -> falls back to linear, but backend stays "soxr".
    with caplog.at_level("WARNING"):
        first = au.resample(payload, 8000, 16000)
    assert isinstance(first, bytes)
    assert au._resolved_backend == "soxr"
    assert len(calls) == 1
    assert sum("soxr resampling failed" in r.message for r in caplog.records) == 1

    # Second call: soxr is retried (not permanently downgraded to linear).
    second = au.resample(payload, 8000, 16000)
    assert isinstance(second, bytes)
    assert len(calls) == 2


def test_resample_runtime_failure_logs_once(monkeypatch, caplog):
    """Repeated soxr failures should warn only once to avoid per-chunk log
    spam, while still attempting the high-quality backend each time."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "soxr")

    def always_fail(data, from_rate, to_rate):
        raise RuntimeError("boom")

    monkeypatch.setattr(au, "_resample_soxr_impl", always_fail, raising=False)

    payload = struct.pack("<4h", 0, 1000, 2000, 3000)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            au.resample(payload, 8000, 16000)

    warnings = [r for r in caplog.records if "soxr resampling failed" in r.message]
    assert len(warnings) == 1


def _ten_khz_alias_attenuation_db() -> float:
    from_rate = 48_000
    to_rate = 16_000
    sample_count = from_rate // 2
    amplitude = 20_000
    source = [
        int(amplitude * math.sin(2 * math.pi * 10_000 * index / from_rate))
        for index in range(sample_count)
    ]
    output = resample(
        struct.pack(f"<{len(source)}h", *source),
        from_rate,
        to_rate,
    )
    samples = struct.unpack(f"<{len(output) // 2}h", output)
    # Ignore the FIR's short boundary region.
    body = samples[len(samples) // 10 :]
    alias_radians = 2 * math.pi * 6_000 / to_rate
    alias_amplitude = abs(
        sum(
            sample * complex(math.cos(alias_radians * index), -math.sin(alias_radians * index))
            for index, sample in enumerate(body)
        )
    )
    alias_amplitude *= 2 / len(body)
    return 20 * math.log10(max(alias_amplitude, 1e-12) / amplitude)


def test_default_resampler_suppresses_downsampling_alias():
    """Every install must use the native anti-aliased path by default."""
    import easycat._audio_utils as au

    assert au.resample_backend() == "soxr"
    assert _ten_khz_alias_attenuation_db() < -60


def test_default_stream_resampler_suppresses_downsampling_alias():
    """The low-latency streaming mode must remain band-limited."""
    from_rate = 48_000
    to_rate = 16_000
    amplitude = 12_000
    samples = [
        int(amplitude * math.sin(2 * math.pi * 10_000 * index / from_rate))
        for index in range(from_rate)
    ]
    payload = struct.pack(f"<{len(samples)}h", *samples)
    stream = PCM16StreamResampler(to_rate)
    chunks = [
        stream.process(payload[offset : offset + 1_920], from_rate)
        for offset in range(0, len(payload), 1_920)
    ]
    output = b"".join([*chunks, stream.finish()])
    output_samples = struct.unpack(f"<{len(output) // 2}h", output)
    body = output_samples[1_000:-1_000]
    alias_radians = 2 * math.pi * 6_000 / to_rate
    alias_amplitude = abs(
        sum(
            sample * complex(math.cos(alias_radians * index), -math.sin(alias_radians * index))
            for index, sample in enumerate(body)
        )
    )
    alias_amplitude *= 2 / len(body)
    attenuation_db = 20 * math.log10(max(alias_amplitude, 1e-12) / amplitude)

    assert attenuation_db < -60


@pytest.mark.parametrize(
    ("from_rate", "to_rate", "frame_samples", "max_frames"),
    [
        (48_000, 16_000, 960, 2),
        (8_000, 16_000, 160, 1),
    ],
)
def test_default_stream_resampler_bounds_voice_startup_delay(
    from_rate: int,
    to_rate: int,
    frame_samples: int,
    max_frames: int,
) -> None:
    stream = PCM16StreamResampler(to_rate)
    frame = bytes(frame_samples * 2)

    outputs = [stream.process(frame, from_rate) for _ in range(max_frames)]

    assert any(outputs)


def test_linear_fallback_suppresses_downsampling_alias(monkeypatch):
    """The dependency-free recovery path must not fold a 10 kHz tone into speech."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    assert _ten_khz_alias_attenuation_db() < -40


@pytest.mark.parametrize(("from_rate", "to_rate"), [(24_000, 16_000), (48_000, 16_000)])
def test_stream_resampler_matches_whole_signal_across_chunk_boundaries(
    from_rate: int,
    to_rate: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    samples = [
        int(14_000 * math.sin(2 * math.pi * 1_100 * index / from_rate))
        for index in range(from_rate // 5)
    ]
    payload = struct.pack(f"<{len(samples)}h", *samples)
    whole = PCM16StreamResampler(to_rate)
    expected = whole.process(payload, from_rate) + whole.finish()
    stream = PCM16StreamResampler(to_rate)
    parts: list[bytes] = []
    cursor = 0
    for chunk_samples in (137, 509, 43, 997, 251):
        if cursor >= len(payload):
            break
        end = min(len(payload), cursor + chunk_samples * 2)
        parts.append(stream.process(payload[cursor:end], from_rate))
        cursor = end
    if cursor < len(payload):
        parts.append(stream.process(payload[cursor:], from_rate))
    parts.append(stream.finish())

    actual = b"".join(parts)
    assert len(actual) == len(expected)
    actual_samples = struct.unpack(f"<{len(actual) // 2}h", actual)
    expected_samples = struct.unpack(f"<{len(expected) // 2}h", expected)
    assert (
        max(
            abs(left - right) for left, right in zip(actual_samples, expected_samples, strict=True)
        )
        <= 1
    )


@pytest.mark.parametrize(
    ("backend", "state_name"),
    [
        ("soxr", "_StreamingSoxrState"),
        ("scipy", "_StreamingScipyState"),
    ],
)
def test_stream_resampler_uses_selected_quality_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    state_name: str,
) -> None:
    import easycat._audio_utils as au

    class _State:
        pending_output_bytes = 0

        def process(self, _samples, *, final):
            _ = final
            return b""

    sentinel = _State()
    monkeypatch.setattr(au, "_resolved_backend", backend)
    monkeypatch.setattr(au, state_name, lambda _from, _to: sentinel)

    stream = PCM16StreamResampler(16_000)
    stream.process(b"\x00\x00", 48_000)

    assert stream._state is sentinel


def test_stream_resampler_carries_split_sample_byte() -> None:
    stream = PCM16StreamResampler(16_000)

    first = stream.process(b"\x01\x02\x03", 16_000)
    second = stream.process(b"\x04", 16_000)

    assert first == b"\x01\x02"
    assert second == b"\x03\x04"
    assert stream.finish() == b""


@pytest.mark.parametrize(
    ("from_rate", "to_rate"),
    [(48_000, 16_000), (16_000, 24_000), *FRACTIONAL_RATE_PAIRS],
)
@pytest.mark.parametrize("sample_count", [*range(1, 12), 240, 480, 960])
def test_linear_stream_resampler_short_input_count_matches_batch(
    monkeypatch: pytest.MonkeyPatch,
    from_rate: int,
    to_rate: int,
    sample_count: int,
) -> None:
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    data = struct.pack(f"<{sample_count}h", *range(sample_count))
    stream = PCM16StreamResampler(to_rate)

    output = b"".join(
        stream.process(data[index : index + 2], from_rate) for index in range(0, len(data), 2)
    )
    output += stream.finish()

    assert len(output) == len(au._resample_linear(data, from_rate, to_rate))


@pytest.mark.parametrize(("from_rate", "to_rate"), [(24_000, 44_100), (44_100, 48_000)])
@pytest.mark.parametrize("sample_count", [240, 480, 960])
def test_linear_upsample_stream_matches_batch_for_fractional_ratios(
    monkeypatch: pytest.MonkeyPatch,
    from_rate: int,
    to_rate: int,
    sample_count: int,
) -> None:
    """Batch and streaming linear upsampling interpolate at identical positions.

    Both compute each source position as ``divmod(i * from_rate, to_rate)``,
    so with no anti-alias filter in play the output bytes must be equal.
    """
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    data = struct.pack(f"<{sample_count}h", *range(sample_count))
    stream = PCM16StreamResampler(to_rate)

    output = stream.process(data, from_rate) + stream.finish()

    assert output == au._resample_linear(data, from_rate, to_rate)


def test_stream_resampler_reports_output_retained_until_finish() -> None:
    import easycat._audio_utils as au

    data = struct.pack("<1600h", *range(1600))
    stream = PCM16StreamResampler(24_000)

    emitted = stream.process(data, 16_000)
    pending = stream.pending_output_bytes

    assert pending == len(au._resample_linear(data, 16_000, 24_000)) - len(emitted)
    assert len(stream.finish()) == pending
    assert stream.pending_output_bytes == 0


@pytest.mark.parametrize(("from_rate", "to_rate"), [(48_000, 16_000), (16_000, 24_000)])
@pytest.mark.parametrize("sample_count", range(1, 12))
def test_soxr_stream_pending_bytes_match_finish(
    from_rate: int,
    to_rate: int,
    sample_count: int,
) -> None:
    stream = PCM16StreamResampler(to_rate)
    data = struct.pack(f"<{sample_count}h", *range(sample_count))

    stream.process(data, from_rate)
    pending = stream.pending_output_bytes
    tail = stream.finish()

    assert len(tail) == pending


def test_stream_resampler_finishes_before_source_rate_change() -> None:
    stream = PCM16StreamResampler(16_000)
    first_source = struct.pack("<5h", 0, 1000, 2000, 3000, 4000)
    second_source = struct.pack("<4h", 5000, 6000, 7000, 8000)

    output = stream.process(first_source, 8_000)
    output += stream.process(second_source, 24_000)
    output += stream.finish()

    first_segment = PCM16StreamResampler(16_000)
    expected = first_segment.process(first_source, 8_000) + first_segment.finish()
    second_segment = PCM16StreamResampler(16_000)
    expected += second_segment.process(second_source, 24_000) + second_segment.finish()
    assert output == expected


def test_linear_fallback_does_not_restart_from_zero_for_stream_frames(monkeypatch):
    """Every constant frame should begin at its signal level, not FIR zero padding."""
    import easycat._audio_utils as au

    monkeypatch.setattr(au, "_resolved_backend", "linear")
    frame = struct.pack("<960h", *([12_000] * 960))

    first_samples = [
        struct.unpack_from("<h", au.resample(frame, 48_000, 16_000))[0] for _ in range(4)
    ]

    assert min(first_samples) > 11_900


def test_resample_chunk_to_48k():
    data = struct.pack("<100h", *([1000] * 100))
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    result = resample_chunk(chunk, 48000)
    assert result.format.sample_rate == 48000
    assert result.format == PCM16_MONO_48K


def test_resample_chunk_to_24k():
    data = struct.pack("<100h", *([1000] * 100))
    chunk = AudioChunk(data=data, format=PCM16_MONO_8K)
    result = resample_chunk(chunk, 24000)
    assert result.format.sample_rate == 24000
    assert result.format == PCM16_MONO_24K


# ── Mono downmix tests ────────────────────────────────────────────


def test_to_mono_already_mono():
    data = struct.pack("<4h", 100, 200, 300, 400)
    result = to_mono(data, channels=1)
    assert result == data


def test_to_mono_stereo():
    data = struct.pack("<4h", 100, 300, 200, 400)
    result = to_mono(data, channels=2)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    assert samples == (200, 300)


def test_to_mono_stereo_symmetric():
    data = struct.pack("<4h", 500, 500, -1000, -1000)
    result = to_mono(data, channels=2)
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    assert samples == (500, -1000)


@pytest.mark.parametrize("channels", [0, -1])
def test_to_mono_rejects_non_positive_channels(channels: int):
    with pytest.raises(ValueError, match="positive"):
        to_mono(b"\x00\x00", channels=channels)


@pytest.mark.parametrize("channels", [True, 2.0, "2"])
def test_to_mono_rejects_non_integer_channels(channels):
    with pytest.raises(TypeError, match="integer"):
        to_mono(b"\x00\x00", channels=channels)


def test_to_mono_chunk_returns_mono_format():
    stereo_fmt = AudioFormat(sample_rate=16000, channels=2, sample_width=2)
    data = struct.pack("<4h", 100, 200, 300, 400)
    chunk = AudioChunk(data=data, format=stereo_fmt)
    result = to_mono_chunk(chunk)
    assert result.format.channels == 1
    assert result.format.sample_rate == 16000


def test_to_mono_chunk_already_mono():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    result = to_mono_chunk(chunk)
    assert result is chunk


def test_audio_frame_aligner_preserves_split_stereo_frames():
    fmt = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
    data = struct.pack("<6h", 100, 300, 1_000, 2_000, 3_000, 4_000)
    aligner = AudioFrameAligner()

    first = aligner.align(AudioChunk(data=data[:6], format=fmt))
    second = aligner.align(AudioChunk(data=data[6:], format=fmt))

    assert to_mono_chunk(first).data == struct.pack("<h", 200)
    assert to_mono_chunk(second).data == struct.pack("<2h", 1_500, 3_500)


def test_audio_frame_aligner_discards_partial_frame_on_format_change():
    stereo = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
    mono = PCM16_MONO_16K
    aligner = AudioFrameAligner()

    assert aligner.align(AudioChunk(data=b"\x00\x00", format=stereo)).data == b""
    assert aligner.align(AudioChunk(data=b"\x01\x00", format=mono)).data == b"\x01\x00"


# ── Chunk sizing tests ────────────────────────────────────────────


def test_chunk_frames_10ms_at_16k():
    audio = bytes(640)
    frames = list(chunk_frames(audio, frame_duration_ms=10, sample_rate=16000))
    assert len(frames) == 2
    assert all(len(f) == 320 for f in frames)


def test_chunk_frames_20ms_at_16k():
    audio = bytes(1280)
    frames = list(chunk_frames(audio, frame_duration_ms=20, sample_rate=16000))
    assert len(frames) == 2
    assert all(len(f) == 640 for f in frames)


def test_chunk_frames_30ms_at_16k():
    audio = bytes(960)
    frames = list(chunk_frames(audio, frame_duration_ms=30, sample_rate=16000))
    assert len(frames) == 1
    assert len(frames[0]) == 960


def test_chunk_frames_partial_final_frame():
    audio = bytes(500)
    frames = list(chunk_frames(audio, frame_duration_ms=10, sample_rate=16000))
    assert len(frames) == 2
    assert len(frames[0]) == 320
    assert len(frames[1]) == 180


def test_chunk_frames_8k_10ms():
    audio = bytes(480)
    frames = list(chunk_frames(audio, frame_duration_ms=10, sample_rate=8000))
    assert len(frames) == 3
    assert all(len(f) == 160 for f in frames)


def test_chunk_frames_empty_audio():
    frames = list(chunk_frames(b"", frame_duration_ms=10, sample_rate=16000))
    assert frames == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_duration_ms": 0, "sample_rate": 16000},
        {"frame_duration_ms": -10, "sample_rate": 16000},
        {"frame_duration_ms": 10, "sample_rate": 0},
        {"frame_duration_ms": 10, "sample_rate": -16000},
        {"frame_duration_ms": 10, "sample_rate": 16000, "sample_width": 0},
        {"frame_duration_ms": 10, "sample_rate": 16000, "channels": 0},
        {"frame_duration_ms": 1, "sample_rate": 999},
    ],
)
def test_chunk_frames_rejects_non_positive_or_zero_byte_frames(kwargs):
    with pytest.raises(ValueError):
        list(chunk_frames(b"\x00" * 10, **kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frame_duration_ms": True, "sample_rate": 16000},
        {"frame_duration_ms": 10.0, "sample_rate": 16000},
        {"frame_duration_ms": 10, "sample_rate": True},
        {"frame_duration_ms": 10, "sample_rate": "16000"},
        {"frame_duration_ms": 10, "sample_rate": 16000, "sample_width": 2.0},
        {"frame_duration_ms": 10, "sample_rate": 16000, "channels": False},
    ],
)
def test_chunk_frames_rejects_non_integer_parameters(kwargs):
    with pytest.raises(TypeError):
        list(chunk_frames(b"\x00" * 10, **kwargs))
