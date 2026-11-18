"""Tests for in-band DTMF tone synthesis (``easycat._dtmf_tones``)."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

import easycat._dtmf_tones as dtmf_tones
from easycat import telephony
from easycat._audio_utils import resample
from easycat._dtmf_tones import (
    DTMF_FREQUENCIES,
    DTMF_PAUSES_MS,
    DTMF_SAMPLE_RATE,
    MAX_DTMF_DIGITS,
    MAX_DTMF_INTER_DIGIT_DELAY_MS,
    DTMFPlaybackOutcome,
    DTMFToneConfig,
    DTMFTonePlayback,
    count_dtmf_keys,
    dtmf_sequence_duration_ms,
    render_dtmf_mulaw,
    render_dtmf_pcm16,
    render_dtmf_pcm16_async,
)
from easycat.session import actions as session_actions
from easycat.telephony.dtmf import VALID_DTMF_DIGITS, VALID_DTMF_OUTPUT_CHARS
from easycat.transports._g711 import mulaw_to_pcm16, pcm16_to_mulaw
from tests.telephony._dtmf_decoder import decode_dtmf, decode_segments

ALL_KEYS = "123A456B789C*0#D"
SPS = DTMF_SAMPLE_RATE // 1000  # samples per millisecond
BARE = {"lead_in_ms": 0, "tail_ms": 0}


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64)


def _pcm(samples: np.ndarray) -> bytes:
    return np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes()


def _component_amplitude(samples: np.ndarray, freq_hz: float) -> float:
    n = np.arange(len(samples))
    projection = np.sum(samples * np.exp(-2j * np.pi * freq_hz * n / DTMF_SAMPLE_RATE))
    return float(2.0 * abs(projection) / len(samples))


# ── Decoding round trips ─────────────────────────────────────────


class TestDecodeRoundTrip:
    @pytest.mark.parametrize("key", list(ALL_KEYS))
    def test_each_key_decodes(self, key: str) -> None:
        assert decode_dtmf(render_dtmf_pcm16(key)) == key

    def test_full_keypad_sequence_decodes_in_order(self) -> None:
        assert decode_dtmf(render_dtmf_pcm16(ALL_KEYS)) == ALL_KEYS

    def test_repeated_key_decodes_as_two_presses(self) -> None:
        assert decode_dtmf(render_dtmf_pcm16("11")) == "11"
        assert decode_dtmf(render_dtmf_pcm16("##00")) == "##00"

    def test_minimum_timing_still_decodes(self) -> None:
        config = DTMFToneConfig(tone_ms=40, gap_ms=40)
        assert decode_dtmf(render_dtmf_pcm16("1122", config=config)) == "1122"

    def test_pauses_are_not_keys(self) -> None:
        assert decode_dtmf(render_dtmf_pcm16("1w2W3")) == "123"

    def test_decodes_after_mulaw_round_trip(self) -> None:
        mulaw = render_dtmf_mulaw(ALL_KEYS)
        assert decode_dtmf(mulaw_to_pcm16(mulaw, target_rate=DTMF_SAMPLE_RATE)) == ALL_KEYS

    def test_decodes_after_resample_round_trip(self) -> None:
        pcm = render_dtmf_pcm16(ALL_KEYS)
        upsampled = resample(pcm, DTMF_SAMPLE_RATE, 16000)
        round_trip = resample(upsampled, 16000, DTMF_SAMPLE_RATE)
        assert decode_dtmf(round_trip) == ALL_KEYS

    def test_decodes_with_noise_at_20_db_snr(self) -> None:
        config = DTMFToneConfig()
        samples = _samples(render_dtmf_pcm16(ALL_KEYS, config=config))
        tone_rms = math.sqrt((config.low_amplitude**2 + config.high_amplitude**2) / 2)
        noise = np.random.default_rng(1234).normal(0.0, tone_rms / 10.0, len(samples))
        assert decode_dtmf(_pcm(samples + noise)) == ALL_KEYS

    def test_quiet_levels_decode(self) -> None:
        config = DTMFToneConfig(low_level_dbm0=-20.0, high_level_dbm0=-20.0)
        assert decode_dtmf(render_dtmf_pcm16(ALL_KEYS, config=config)) == ALL_KEYS

    def test_decoder_ignores_silence_and_single_tone(self) -> None:
        assert decode_dtmf(bytes(16000)) == ""
        n = np.arange(800)
        single = 10000.0 * np.sin(2 * np.pi * 697 * n / DTMF_SAMPLE_RATE)
        assert decode_dtmf(_pcm(single)) == ""

    def test_decoded_tone_lengths_track_tone_ms(self) -> None:
        config = DTMFToneConfig(tone_ms=200)
        segments = decode_segments(render_dtmf_pcm16("5", config=config))
        assert [segment.key for segment in segments] == ["5"]
        assert 190 <= segments[0].duration_ms <= 240


# ── Layout and sample-exact durations ────────────────────────────


class TestLayout:
    def test_single_key_is_exactly_tone_ms(self) -> None:
        config = DTMFToneConfig(tone_ms=120, **BARE)
        assert len(render_dtmf_pcm16("7", config=config)) == 120 * SPS * 2

    def test_lead_in_and_tail_are_exact_silence(self) -> None:
        config = DTMFToneConfig(lead_in_ms=60, tail_ms=100)
        bare = render_dtmf_pcm16("7", config=DTMFToneConfig(**BARE))
        pcm = render_dtmf_pcm16("7", config=config)
        lead, tail = 60 * SPS * 2, 100 * SPS * 2
        assert len(pcm) == lead + len(bare) + tail
        assert pcm[:lead] == bytes(lead)
        assert pcm[lead : lead + len(bare)] == bare
        assert pcm[lead + len(bare) :] == bytes(tail)

    def test_gap_between_keys_is_exact_silence(self) -> None:
        config = DTMFToneConfig(tone_ms=100, gap_ms=70, **BARE)
        one = render_dtmf_pcm16("1", config=config)
        two = render_dtmf_pcm16("2", config=config)
        pcm = render_dtmf_pcm16("12", config=config)
        gap = 70 * SPS * 2
        assert pcm == one + bytes(gap) + two

    def test_inter_digit_delay_widens_but_never_narrows_the_gap(self) -> None:
        config = DTMFToneConfig(tone_ms=100, gap_ms=100, **BARE)
        tone = 100 * SPS * 2
        wide = render_dtmf_pcm16("12", inter_digit_delay_ms=250, config=config)
        assert len(wide) == 2 * tone + 250 * SPS * 2
        narrow = render_dtmf_pcm16("12", inter_digit_delay_ms=10, config=config)
        assert len(narrow) == 2 * tone + 100 * SPS * 2

    def test_inter_digit_delay_is_clamped(self) -> None:
        config = DTMFToneConfig(max_total_ms=600_000, **BARE)
        duration = dtmf_sequence_duration_ms(
            "12", inter_digit_delay_ms=MAX_DTMF_INTER_DIGIT_DELAY_MS * 3, config=config
        )
        assert duration == 2 * config.tone_ms + MAX_DTMF_INTER_DIGIT_DELAY_MS

    @pytest.mark.parametrize(("marker", "samples"), [("w", 4000), ("W", 8000)])
    def test_pause_markers_add_exact_silence_on_top_of_gap(
        self, marker: str, samples: int
    ) -> None:
        config = DTMFToneConfig(tone_ms=100, gap_ms=100, **BARE)
        plain = render_dtmf_pcm16("12", config=config)
        paused = render_dtmf_pcm16(f"1{marker}2", config=config)
        assert len(paused) - len(plain) == samples * 2
        tone = 100 * SPS * 2
        silence = paused[tone : len(paused) - tone]
        assert silence == bytes(len(silence))

    def test_leading_and_trailing_pauses(self) -> None:
        config = DTMFToneConfig(tone_ms=100, **BARE)
        assert len(render_dtmf_pcm16("w1", config=config)) == (500 + 100) * SPS * 2
        assert len(render_dtmf_pcm16("1W", config=config)) == (100 + 1000) * SPS * 2
        assert render_dtmf_pcm16("ww", config=config) == bytes(1000 * SPS * 2)

    @pytest.mark.parametrize(
        ("digits", "delay"),
        [("1", 0), ("123#", 0), ("1w2W3", 0), ("99", 400), ("wA", 0), ("*", 3000)],
    )
    def test_analytic_duration_matches_rendered_length(self, digits: str, delay: int) -> None:
        config = DTMFToneConfig(tone_ms=90, gap_ms=60, lead_in_ms=30, tail_ms=50)
        pcm = render_dtmf_pcm16(digits, inter_digit_delay_ms=delay, config=config)
        duration = dtmf_sequence_duration_ms(digits, inter_digit_delay_ms=delay, config=config)
        assert len(pcm) == duration * SPS * 2

    def test_default_duration(self) -> None:
        # 60 lead-in + 3 x 100 tone + 2 x 100 gap + 100 tail
        assert dtmf_sequence_duration_ms("123") == 660

    def test_mulaw_is_the_encoded_pcm(self) -> None:
        pcm = render_dtmf_pcm16("1w#")
        mulaw = render_dtmf_mulaw("1w#")
        assert mulaw == pcm16_to_mulaw(pcm, source_rate=DTMF_SAMPLE_RATE)
        assert len(mulaw) == len(pcm) // 2

    def test_rendering_is_deterministic(self) -> None:
        assert render_dtmf_pcm16("159D") == render_dtmf_pcm16("159D")

    def test_count_dtmf_keys_skips_pauses(self) -> None:
        assert count_dtmf_keys("1w2W#") == 3
        assert count_dtmf_keys("wW") == 0


# ── Levels ───────────────────────────────────────────────────────


class TestLevels:
    @pytest.mark.parametrize(
        ("low_dbm0", "high_dbm0"),
        [(-7.0, -5.0), (-12.0, -10.0), (-20.0, -20.0), (-3.0, -3.0), (-10.0, -4.0)],
    )
    @pytest.mark.parametrize("key", ["1", "5", "9", "D"])
    def test_component_levels_within_half_a_db(
        self, key: str, low_dbm0: float, high_dbm0: float
    ) -> None:
        config = DTMFToneConfig(
            tone_ms=500, low_level_dbm0=low_dbm0, high_level_dbm0=high_dbm0, **BARE
        )
        samples = _samples(render_dtmf_pcm16(key, config=config))
        steady = samples[400:-400]  # skip the ramps
        low_hz, high_hz = DTMF_FREQUENCIES[key]
        for freq, level in ((low_hz, low_dbm0), (high_hz, high_dbm0)):
            expected = 22662.0 * 10 ** (level / 20)
            measured = _component_amplitude(steady, freq)
            assert abs(20 * math.log10(measured / expected)) <= 0.5

    def test_default_twist_is_two_db(self) -> None:
        config = DTMFToneConfig()
        twist = 20 * math.log10(config.high_amplitude / config.low_amplitude)
        assert twist == pytest.approx(2.0)

    def test_peak_stays_below_mulaw_clip(self) -> None:
        config = DTMFToneConfig(low_level_dbm0=-3.0, high_level_dbm0=-3.0)
        samples = _samples(render_dtmf_pcm16(ALL_KEYS, config=config))
        assert np.max(np.abs(samples)) < 32635

    def test_ramps_start_and_end_near_zero(self) -> None:
        config = DTMFToneConfig(ramp_ms=5.0, **BARE)
        samples = _samples(render_dtmf_pcm16("D", config=config))
        assert abs(samples[0]) < 200
        assert abs(samples[-1]) < 200
        assert np.max(np.abs(samples[:40])) < np.max(np.abs(samples[200:400]))


# ── Validation ───────────────────────────────────────────────────


class TestRejections:
    @pytest.mark.parametrize(
        "digits",
        [
            '1"/><Say>',
            "١٢٣",  # Arabic-Indic one, two, three
            "a",
            "abcd",
            "12a",
            "1 2",
            "1,2",
            "",
            "1" * (MAX_DTMF_DIGITS + 1),
        ],
    )
    def test_invalid_digits_raise_without_echoing(self, digits: str) -> None:
        for render in (render_dtmf_pcm16, render_dtmf_mulaw, dtmf_sequence_duration_ms):
            with pytest.raises(ValueError) as exc_info:
                render(digits)
            message = str(exc_info.value)
            if len(digits) > 1:  # a lone letter can appear in the help text
                assert digits not in message
            assert "<Say>" not in message
            assert "١" not in message

    def test_max_length_is_accepted(self) -> None:
        config = DTMFToneConfig(tone_ms=40, gap_ms=40, **BARE)
        assert decode_dtmf(render_dtmf_pcm16("1" * MAX_DTMF_DIGITS, config=config)) == (
            "1" * MAX_DTMF_DIGITS
        )

    @pytest.mark.parametrize("digits", [None, 1, b"123", ["1"]])
    def test_non_string_digits_raise_type_error(self, digits: Any) -> None:
        with pytest.raises(TypeError):
            render_dtmf_pcm16(digits)

    def test_max_total_ms_overflow_raises(self) -> None:
        config = DTMFToneConfig(max_total_ms=1000)
        with pytest.raises(ValueError, match="max_total_ms"):
            render_dtmf_pcm16("12345678", config=config)
        with pytest.raises(ValueError, match="max_total_ms"):
            render_dtmf_mulaw("12345678", config=config)
        # The analytic helper reports the length without enforcing the cap.
        assert dtmf_sequence_duration_ms("12345678", config=config) == 1660

    def test_default_cap_rejects_huge_delay_sequences(self) -> None:
        with pytest.raises(ValueError, match="max_total_ms"):
            render_dtmf_pcm16("1" * 10, inter_digit_delay_ms=MAX_DTMF_INTER_DIGIT_DELAY_MS)

    def test_negative_inter_digit_delay_raises(self) -> None:
        with pytest.raises(ValueError, match="inter_digit_delay_ms"):
            render_dtmf_pcm16("1", inter_digit_delay_ms=-1)

    @pytest.mark.parametrize("delay", [True, 1.5, "100", None])
    def test_non_integer_inter_digit_delay_raises(self, delay: Any) -> None:
        with pytest.raises(TypeError, match="inter_digit_delay_ms"):
            render_dtmf_pcm16("1", inter_digit_delay_ms=delay)

    def test_wrong_config_type_raises(self) -> None:
        with pytest.raises(TypeError):
            render_dtmf_pcm16("1", config={"tone_ms": 100})  # type: ignore[arg-type]


class TestToneConfig:
    def test_defaults(self) -> None:
        config = DTMFToneConfig()
        assert (config.tone_ms, config.gap_ms, config.lead_in_ms, config.tail_ms) == (
            100,
            100,
            60,
            100,
        )
        assert (config.low_level_dbm0, config.high_level_dbm0) == (-7.0, -5.0)
        assert config.ramp_ms == 2.0
        assert config.max_total_ms == 60_000

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"tone_ms": 40},
            {"tone_ms": 500},
            {"gap_ms": 40},
            {"gap_ms": 10_000},
            {"lead_in_ms": 0},
            {"lead_in_ms": 1000},
            {"tail_ms": 0},
            {"tail_ms": 1000},
            {"low_level_dbm0": -20},
            {"high_level_dbm0": -3.0, "low_level_dbm0": -3.0},
            {"ramp_ms": 0},
            {"ramp_ms": 10.0},
            {"ramp_ms": 10, "tone_ms": 40},
            {"max_total_ms": 100},
            {"max_total_ms": 600_000},
        ],
    )
    def test_accepts_bounds(self, kwargs: dict[str, Any]) -> None:
        DTMFToneConfig(**kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"tone_ms": 39},
            {"tone_ms": 501},
            {"gap_ms": 39},
            {"gap_ms": 10_001},
            {"lead_in_ms": -1},
            {"lead_in_ms": 1001},
            {"tail_ms": -1},
            {"tail_ms": 1001},
            {"low_level_dbm0": -2.9},
            {"low_level_dbm0": -20.1},
            {"high_level_dbm0": 0.0},
            {"high_level_dbm0": float("nan")},
            {"low_level_dbm0": float("-inf")},
            {"ramp_ms": -0.1},
            {"ramp_ms": 10.1},
            {"ramp_ms": float("inf")},
            {"ramp_ms": 10.0, "tone_ms": 40, "gap_ms": 39},
            {"max_total_ms": 99},
            {"max_total_ms": 600_001},
        ],
    )
    def test_rejects_out_of_bounds(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            DTMFToneConfig(**kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"tone_ms": 100.0},
            {"tone_ms": True},
            {"gap_ms": "100"},
            {"max_total_ms": None},
            {"high_level_dbm0": "loud"},
            {"ramp_ms": False},
        ],
    )
    def test_rejects_wrong_types(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(TypeError):
            DTMFToneConfig(**kwargs)

    def test_is_frozen_and_hashable(self) -> None:
        config = DTMFToneConfig()
        with pytest.raises(AttributeError):
            config.tone_ms = 200  # type: ignore[misc]
        assert hash(config) == hash(DTMFToneConfig())


# ── Drift guards ─────────────────────────────────────────────────


class TestDrift:
    def test_keys_match_telephony_alphabet(self) -> None:
        assert set(DTMF_FREQUENCIES) == VALID_DTMF_DIGITS
        assert set(DTMF_FREQUENCIES) | set(DTMF_PAUSES_MS) == VALID_DTMF_OUTPUT_CHARS

    def test_pause_markers(self) -> None:
        assert dict(DTMF_PAUSES_MS) == {"w": 500, "W": 1000}

    def test_limits_match_session_actions(self) -> None:
        assert MAX_DTMF_DIGITS == session_actions.MAX_DTMF_DIGITS
        assert MAX_DTMF_INTER_DIGIT_DELAY_MS == session_actions.MAX_DTMF_INTER_DIGIT_DELAY_MS
        assert set(DTMF_FREQUENCIES) | set(DTMF_PAUSES_MS) == (
            session_actions._VALID_DTMF_OUTPUT_CHARS
        )

    def test_q23_frequency_table(self) -> None:
        assert DTMF_FREQUENCIES["1"] == (697, 1209)
        assert DTMF_FREQUENCIES["5"] == (770, 1336)
        assert DTMF_FREQUENCIES["C"] == (852, 1633)
        assert DTMF_FREQUENCIES["#"] == (941, 1477)
        assert DTMF_FREQUENCIES["0"] == (941, 1336)
        assert DTMF_FREQUENCIES["*"] == (941, 1209)

    def test_telephony_package_re_exports(self) -> None:
        assert telephony.DTMFToneConfig is DTMFToneConfig
        assert telephony.DTMFTonePlayback is DTMFTonePlayback
        assert telephony.render_dtmf_pcm16 is render_dtmf_pcm16
        assert telephony.render_dtmf_mulaw is render_dtmf_mulaw


# ── Property tests ───────────────────────────────────────────────

_OUTPUT_ALPHABET = "".join(sorted(VALID_DTMF_OUTPUT_CHARS))


@given(
    digits=st.text(alphabet=_OUTPUT_ALPHABET, min_size=1, max_size=12),
    delay=st.integers(min_value=0, max_value=300),
    tone_ms=st.sampled_from([40, 70, 100, 150]),
    gap_ms=st.sampled_from([40, 60, 100]),
)
def test_any_valid_sequence_decodes_to_its_keys(
    digits: str, delay: int, tone_ms: int, gap_ms: int
) -> None:
    config = DTMFToneConfig(tone_ms=tone_ms, gap_ms=gap_ms)
    pcm = render_dtmf_pcm16(digits, inter_digit_delay_ms=delay, config=config)
    keys = "".join(char for char in digits if char in DTMF_FREQUENCIES)
    assert decode_dtmf(pcm) == keys
    duration = dtmf_sequence_duration_ms(digits, inter_digit_delay_ms=delay, config=config)
    assert len(pcm) == duration * SPS * 2
    assert count_dtmf_keys(digits) == len(keys)


@given(digits=st.text(min_size=1, max_size=8).filter(lambda s: set(s) - VALID_DTMF_OUTPUT_CHARS))
def test_any_invalid_sequence_is_rejected(digits: str) -> None:
    with pytest.raises(ValueError):
        render_dtmf_pcm16(digits)


# ── Playback handle ──────────────────────────────────────────────


def _playback(future: asyncio.Future[DTMFPlaybackOutcome]) -> DTMFTonePlayback:
    return DTMFTonePlayback(key_count=3, duration_ms=660, mark_name="easycat_dtmf_1", _done=future)


class TestTonePlayback:
    async def test_wait_returns_resolved_outcome(self) -> None:
        future: asyncio.Future[DTMFPlaybackOutcome] = asyncio.get_running_loop().create_future()
        playback = _playback(future)
        assert playback.delivery == "in_band"
        assert playback.done() is False
        asyncio.get_running_loop().call_soon(future.set_result, "played")
        assert await playback.wait(1.0) == "played"
        assert playback.done() is True
        assert await playback.wait() == "played"

    async def test_wait_times_out_without_cancelling_delivery(self) -> None:
        future: asyncio.Future[DTMFPlaybackOutcome] = asyncio.get_running_loop().create_future()
        playback = _playback(future)
        assert await playback.wait(0.01) == "timeout"
        assert not future.cancelled()
        future.set_result("played")
        assert await playback.wait(0) == "played"

    async def test_caller_cancellation_does_not_cancel_delivery(self) -> None:
        future: asyncio.Future[DTMFPlaybackOutcome] = asyncio.get_running_loop().create_future()
        playback = _playback(future)
        waiter = asyncio.create_task(playback.wait(None))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not future.done()
        future.set_result("estimated")
        assert await playback.wait(None) == "estimated"

    async def test_cancelled_or_failed_delivery_reports_disconnected(self) -> None:
        loop = asyncio.get_running_loop()
        cancelled: asyncio.Future[DTMFPlaybackOutcome] = loop.create_future()
        cancelled.cancel()
        assert await _playback(cancelled).wait(1.0) == "disconnected"
        failed: asyncio.Future[DTMFPlaybackOutcome] = loop.create_future()
        failed.set_exception(ConnectionError("gone"))
        assert await _playback(failed).wait(1.0) == "disconnected"

    async def test_invalid_timeout_raises(self) -> None:
        future: asyncio.Future[DTMFPlaybackOutcome] = asyncio.get_running_loop().create_future()
        with pytest.raises(ValueError):
            await _playback(future).wait(-1.0)
        future.cancel()

    async def test_repr_and_equality_ignore_future(self) -> None:
        loop = asyncio.get_running_loop()
        first: asyncio.Future[DTMFPlaybackOutcome] = loop.create_future()
        second: asyncio.Future[DTMFPlaybackOutcome] = loop.create_future()
        assert _playback(first) == _playback(second)
        assert "_done" not in repr(_playback(first))
        first.cancel()
        second.cancel()


class TestAsyncRender:
    async def test_short_sequence_renders_inline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fail(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("short sequences must not use a thread")

        monkeypatch.setattr(dtmf_tones.asyncio, "to_thread", _fail)
        assert await render_dtmf_pcm16_async("123") == render_dtmf_pcm16("123")

    async def test_long_sequence_renders_in_a_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        real_to_thread = asyncio.to_thread

        async def _record(func: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(func)
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(dtmf_tones.asyncio, "to_thread", _record)
        digits = "1W2W3W4W5W6"
        assert await render_dtmf_pcm16_async(digits) == render_dtmf_pcm16(digits)
        assert len(calls) == 1

    async def test_async_render_validates_before_rendering(self) -> None:
        with pytest.raises(ValueError):
            await render_dtmf_pcm16_async("1<")
