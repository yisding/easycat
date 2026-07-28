"""Lazy NumPy-compatible Whisper feature extraction for Smart Turn."""

from __future__ import annotations

from typing import Any

# These helpers and ``_WhisperFeatureExtractorNP`` are NumPy ports derived
# from HuggingFace Transformers (audio_utils.py and Whisper's
# feature_extraction_whisper.py), Apache 2.0 licensed. Modified to drop
# PyTorch and keep only the single-waveform, 16 kHz code path Smart Turn
# needs. See LICENSE.transformers in this package.


def _hertz_to_mel(freq: Any, *, np: Any, mel_scale: str = "slaney") -> Any:
    if mel_scale != "slaney":
        raise ValueError(f"Unsupported mel scale: {mel_scale}")

    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = 27.0 / np.log(6.4)
    mels = 3.0 * freq / 200.0

    if isinstance(freq, np.ndarray):
        log_region = freq >= min_log_hertz
        mels[log_region] = min_log_mel + np.log(freq[log_region] / min_log_hertz) * logstep
    elif freq >= min_log_hertz:
        mels = min_log_mel + np.log(freq / min_log_hertz) * logstep

    return mels


def _mel_to_hertz(mels: Any, *, np: Any, mel_scale: str = "slaney") -> Any:
    if mel_scale != "slaney":
        raise ValueError(f"Unsupported mel scale: {mel_scale}")

    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = np.log(6.4) / 27.0
    freq = 200.0 * mels / 3.0

    if isinstance(mels, np.ndarray):
        log_region = mels >= min_log_mel
        freq[log_region] = min_log_hertz * np.exp(logstep * (mels[log_region] - min_log_mel))
    elif mels >= min_log_mel:
        freq = min_log_hertz * np.exp(logstep * (mels - min_log_mel))

    return freq


def _create_triangular_filter_bank(fft_freqs: Any, filter_freqs: Any, *, np: Any) -> Any:
    filter_diff = np.diff(filter_freqs)
    slopes = np.expand_dims(filter_freqs, 0) - np.expand_dims(fft_freqs, 1)
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    return np.maximum(np.zeros(1), np.minimum(down_slopes, up_slopes))


def _mel_filter_bank(
    *,
    np: Any,
    num_frequency_bins: int,
    num_mel_filters: int,
    min_frequency: float,
    max_frequency: float,
    sampling_rate: int,
) -> Any:
    mel_min = _hertz_to_mel(min_frequency, np=np)
    mel_max = _hertz_to_mel(max_frequency, np=np)
    mel_freqs = np.linspace(mel_min, mel_max, num_mel_filters + 2)
    filter_freqs = _mel_to_hertz(mel_freqs, np=np)
    fft_freqs = np.linspace(0, sampling_rate // 2, num_frequency_bins)
    mel_filters = _create_triangular_filter_bank(fft_freqs, filter_freqs, np=np)

    enorm = 2.0 / (filter_freqs[2 : num_mel_filters + 2] - filter_freqs[:num_mel_filters])
    mel_filters *= np.expand_dims(enorm, 0)
    return mel_filters


def _window_function(window_length: int, *, np: Any) -> Any:
    return np.hanning(window_length + 1)[:-1]


def _spectrogram(
    waveform: Any,
    *,
    np: Any,
    window: Any,
    frame_length: int,
    hop_length: int,
    mel_filters: Any,
) -> Any:
    if waveform.size < 2:
        pad_mode = "edge"
    else:
        pad_mode = "reflect"
    waveform = np.pad(waveform, (frame_length // 2, frame_length // 2), mode=pad_mode)
    waveform = waveform.astype(np.float64)
    window = window.astype(np.float64)

    num_frames = int(1 + np.floor((waveform.size - frame_length) / hop_length))
    frames = np.lib.stride_tricks.sliding_window_view(waveform, frame_length)[::hop_length]
    frames = frames[:num_frames]
    # Batch all ~800 windows into one NumPy FFT call.  Keep the explicit
    # complex64 cast from the former per-frame output buffer so this is a
    # scheduling optimization, not a feature-precision/model-input change.
    spec = np.fft.rfft(frames * window, axis=1).astype(np.complex64, copy=False)

    spec = (np.abs(spec, dtype=np.float64) ** 2.0).T
    # This is a small, fixed-shape contraction (80 x 201 x ~800).  ``np.dot``
    # delegates it to the process-wide BLAS pool, whose auto-sized worker team
    # can contend with the ONNX pool that runs immediately afterward.  The
    # explicit non-optimizing einsum stays single-threaded, is numerically
    # equivalent at float32 precision, and removes the endpoint-latency spikes
    # without mutating host-wide BLAS environment settings.
    spec = np.maximum(
        1e-10,
        np.einsum("ij,jk->ik", mel_filters.T, spec, optimize=False),
    )
    spec = np.log10(spec)
    return spec.astype(np.float32)


class _WhisperFeatureExtractorNP:
    """Whisper-compatible log-mel frontend for the bundled ONNX model.

    This is a narrow, torch-free subset of Hugging Face's Whisper feature
    extraction logic. It only implements the path Smart Turn needs:
    single-waveform, CPU, NumPy output.
    """

    def __init__(
        self,
        *,
        np: Any,
        feature_size: int = 80,
        sampling_rate: int = 16000,
        hop_length: int = 160,
        chunk_length: int = 8,
        n_fft: int = 400,
        padding_value: float = 0.0,
    ) -> None:
        self._np = np
        self.feature_size = feature_size
        self.sampling_rate = sampling_rate
        self.hop_length = hop_length
        self.chunk_length = chunk_length
        self.n_fft = n_fft
        self.padding_value = padding_value
        self.n_samples = chunk_length * sampling_rate
        self.window = _window_function(n_fft, np=np)
        self.mel_filters = _mel_filter_bank(
            np=np,
            num_frequency_bins=1 + n_fft // 2,
            num_mel_filters=feature_size,
            min_frequency=0.0,
            max_frequency=8000.0,
            sampling_rate=sampling_rate,
        )

    def __call__(
        self,
        raw_speech: Any,
        *,
        sampling_rate: int,
        do_normalize: bool = True,
    ) -> Any:
        np = self._np
        if sampling_rate != self.sampling_rate:
            raise ValueError(f"expected sampling_rate={self.sampling_rate}, got {sampling_rate}")

        audio = np.asarray(raw_speech, dtype=np.float32)
        if audio.ndim != 1:
            raise ValueError(f"expected mono waveform, got shape={audio.shape}")
        if audio.size == 0:
            return np.zeros(
                (1, self.feature_size, self.n_samples // self.hop_length), dtype=np.float32
            )

        audio = audio[-self.n_samples :]
        valid_length = audio.shape[0]

        if do_normalize:
            audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)

        if valid_length < self.n_samples:
            padded = np.full((self.n_samples,), self.padding_value, dtype=np.float32)
            padded[:valid_length] = audio
            audio = padded

        log_spec = _spectrogram(
            audio,
            np=np,
            window=self.window,
            frame_length=self.n_fft,
            hop_length=self.hop_length,
            mel_filters=self.mel_filters,
        )
        log_spec = log_spec[:, :-1]
        log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
        log_spec = (log_spec + 4.0) / 4.0
        return np.expand_dims(log_spec.astype(np.float32), axis=0)
