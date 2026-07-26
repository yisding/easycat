"""Regression tests for Smart Turn's NumPy Whisper feature port."""

from __future__ import annotations

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
    time = np.arange(16000, dtype=np.float64) / 16000.0
    waveform = (
        0.37 * np.sin(2 * np.pi * (180 * time + 720 * time**2))
        + 0.13 * np.cos(2 * np.pi * 53 * time)
    ).astype(np.float32)

    extractor = _WhisperFeatureExtractorNP(np=np, feature_size=8, chunk_length=1)
    actual = extractor(waveform, sampling_rate=16000, do_normalize=True)

    # These fixtures come from transformers 5.13.1 WhisperFeatureExtractor's
    # CPU NumPy path with the same constructor arguments and waveform.
    expected_leading = np.array(
        [
            [1.4091717, 1.4193919, 1.4272149, 1.4341657, 1.4408454, 1.4470471],
            [0.90528876, 0.3960842, 0.06258935, 0.20557272, 0.10829037, 0.32059222],
            [0.66646576, 0.15725875, -0.52977824, -0.45915353, -0.52977824, -0.49393713],
            [0.504266, -0.0051204, -0.52977824, -0.52977824, -0.52977824, -0.52977824],
        ],
        dtype=np.float32,
    )
    probe_rows = np.array([0, 0, 1, 1, 2, 2, 3, 4, 5, 6, 7, 7, 7])
    probe_columns = np.array([1, 50, 0, 99, 2, 73, 41, 9, 88, 37, 0, 50, 99])
    expected_probes = np.array(
        [
            1.4193919,
            1.0466441,
            0.90528876,
            0.4020959,
            -0.52977824,
            1.3778621,
            -0.26805425,
            -0.52977824,
            -0.52977824,
            -0.52977824,
            -0.01369202,
            -0.52977824,
            -0.06866372,
        ],
        dtype=np.float32,
    )

    assert actual.shape == (1, 8, 100)
    assert actual.dtype == np.float32
    np.testing.assert_allclose(actual[0, :4, :6], expected_leading, rtol=1e-5, atol=1e-5)
    np.testing.assert_allclose(
        actual[0, probe_rows, probe_columns],
        expected_probes,
        rtol=1e-5,
        atol=1e-5,
    )
