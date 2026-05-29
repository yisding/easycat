"""Smart Turn runtime loading tests."""

from types import SimpleNamespace

from easycat.smart_turn import SmartTurnONNX


def test_smart_turn_ensure_loaded_uses_numpy_and_onnxruntime_only(
    monkeypatch,
    tmp_path,
) -> None:
    """Smart Turn should load through NumPy + ONNX Runtime without transformers."""

    requested_modules: list[str] = []
    fake_np = object()

    def make_session_options() -> SimpleNamespace:
        return SimpleNamespace()

    fake_ort = SimpleNamespace(
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL="all"),
        SessionOptions=make_session_options,
        InferenceSession=lambda model_path, sess_options=None: (
            "fake-session",
            model_path,
            sess_options,
        ),
    )

    def fake_require_module(name: str, *, extra: str, purpose: str):
        assert extra == "smart-turn"
        assert purpose == "Smart-turn endpoint detection"
        requested_modules.append(name)
        if name == "numpy":
            return fake_np
        if name == "onnxruntime":
            return fake_ort
        raise AssertionError(f"unexpected module request: {name}")

    created_feature_extractors: list[tuple[object, int]] = []

    def fake_feature_extractor(*, np, chunk_length: int):
        created_feature_extractors.append((np, chunk_length))
        return "fake-feature-extractor"

    monkeypatch.setattr("easycat.smart_turn.require_module", fake_require_module)
    monkeypatch.setattr("easycat.smart_turn._WhisperFeatureExtractorNP", fake_feature_extractor)

    provider = SmartTurnONNX(model_path=str(tmp_path / "smart-turn.onnx"))
    provider._ensure_loaded()

    assert requested_modules == ["numpy", "onnxruntime"]
    assert created_feature_extractors == [(fake_np, 8)]
    assert provider._feature_extractor == "fake-feature-extractor"
    assert provider._session[0] == "fake-session"


def _make_provider_with_probability(probability: float, threshold: float) -> SmartTurnONNX:
    """Build a SmartTurnONNX whose inference returns a fixed probability."""

    provider = SmartTurnONNX(model_path="unused.onnx", threshold=threshold)
    # numpy is only used for length checks / padding in _predict_sync; a slice
    # of length >= max_samples skips padding entirely.
    fake_np = SimpleNamespace(pad=lambda *a, **k: a[0])
    provider._np = fake_np
    provider._feature_extractor = lambda *a, **k: "features"

    fake_output = SimpleNamespace(item=lambda: probability)
    provider._session = SimpleNamespace(run=lambda *a, **k: [[fake_output]])
    return provider


def test_predict_boundary_equal_threshold_is_incomplete() -> None:
    """probability == threshold must classify as incomplete (strict-greater)."""

    provider = _make_provider_with_probability(0.5, threshold=0.5)
    audio = [0.0] * (8 * 16000)

    result = provider._predict_sync(audio)

    assert result.probability == 0.5
    assert result.prediction == 0


def test_predict_above_threshold_is_complete() -> None:
    """probability strictly above threshold classifies as complete."""

    provider = _make_provider_with_probability(0.51, threshold=0.5)
    audio = [0.0] * (8 * 16000)

    result = provider._predict_sync(audio)

    assert result.prediction == 1
