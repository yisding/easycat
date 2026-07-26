"""Regression tests for Smart Turn's NumPy Whisper feature port."""

from __future__ import annotations

import hashlib

import pytest

from easycat._smart_turn_features import (
    _hertz_to_mel,
    _mel_filter_bank,
    _spectrogram,
    _WhisperFeatureExtractorNP,
    _window_function,
)

np = pytest.importorskip("numpy")


@pytest.mark.parametrize(
    ("frequency", "expected_mels"),
    [
        (0.0, 0.0),
        (500.0, 7.5),
        (1000.0, 15.0),
        (6400.0, 42.0),
    ],
)
def test_hertz_to_mel_scalar_branches(frequency: float, expected_mels: float) -> None:
    assert _hertz_to_mel(frequency, np=np) == pytest.approx(expected_mels)


def test_hertz_to_mel_array_branches() -> None:
    frequencies = np.array([0.0, 500.0, 1000.0, 6400.0], dtype=np.float64)

    actual = _hertz_to_mel(frequencies, np=np)

    np.testing.assert_allclose(actual, [0.0, 7.5, 15.0, 42.0], rtol=1e-12, atol=1e-12)


def test_mel_filter_bank_matches_transformers_reference() -> None:
    actual = _mel_filter_bank(
        np=np,
        num_frequency_bins=9,
        num_mel_filters=4,
        min_frequency=0.0,
        max_frequency=8000.0,
        sampling_rate=16000,
    )

    # Generated with transformers 5.13.1 audio_utils.mel_filter_bank using
    # norm="slaney" and mel_scale="slaney".
    expected = np.array(
        [
            [0.0, 0.0, 0.0, 0.0],
            [0.00060509576390204, 0.0007352135534891175, 0.0, 0.0],
            [0.0, 0.0003358614972034312, 0.00046726403337509203, 0.0],
            [0.0, 0.0, 0.00042571519645100484, 0.00012267497989052392],
            [0.0, 0.0, 0.00009680999238109699, 0.000299228830973115],
            [0.0, 0.0, 0.0, 0.00028431835628968985],
            [0.0, 0.0, 0.0, 0.0001895455708597932],
            [0.0, 0.0, 0.0, 0.00009477278542989652],
            [0.0, 0.0, 0.0, 0.0],
        ],
        dtype=np.float64,
    )

    assert actual.shape == (9, 4)
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize(
    ("waveform", "expected"),
    [
        pytest.param(
            [1.0],
            [[0.60206002], [0.0], [-10.0]],
            id="one-sample-edge-padding",
        ),
        pytest.param(
            [0.0, 1.0, 0.0, -1.0],
            [[0.0, -10.0, -10.0], [-10.0, 0.0, 0.0], [0.0, -10.0, -10.0]],
            id="normal-reflect-padding",
        ),
    ],
)
def test_spectrogram_padding_modes(
    waveform: list[float],
    expected: list[list[float]],
) -> None:
    actual = _spectrogram(
        np.asarray(waveform, dtype=np.float32),
        np=np,
        window=_window_function(4, np=np),
        frame_length=4,
        hop_length=2,
        mel_filters=np.eye(3, dtype=np.float64),
    )

    assert actual.dtype == np.float32
    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-7)


def test_whisper_feature_extractor_matches_transformers_reference() -> None:
    sampling_rate = 16000
    time = np.arange(8 * sampling_rate, dtype=np.float64) / sampling_rate
    waveform = (
        0.37 * np.sin(2 * np.pi * (180 * time + 90 * time**2))
        + 0.13 * np.cos(2 * np.pi * 53 * time)
    ).astype(np.float32)

    actual = _WhisperFeatureExtractorNP(np=np)(
        waveform,
        sampling_rate=sampling_rate,
        do_normalize=True,
    )

    # Full production shape generated with transformers 5.13.1
    # WhisperFeatureExtractor using the same waveform and default 80-filter,
    # eight-second configuration. Quantization keeps the digest stable across
    # equivalent BLAS/FFT implementations while covering every tensor value.
    quantized = np.round(actual, decimals=5).astype("<f4")
    digest = hashlib.sha256(quantized.tobytes()).hexdigest()

    assert actual.shape == (1, 80, 800)
    assert actual.dtype == np.float32
    assert np.isfinite(actual).all()
    assert digest == "22f9490892ec2009dba6649ec4ddf727d7b4fcf0282cf4d582ba1817ac901d5f"
