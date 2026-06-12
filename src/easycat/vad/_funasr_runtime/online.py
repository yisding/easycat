"""Minimal in-tree FunASR FSMN-VAD online runtime.

Portions of this module wrap runtime pieces vendored from ``funasr-onnx``
0.4.1.  The upstream package is no longer suitable as a dependency because it
pins ``numpy<=1.26.4``; this wrapper keeps the model execution path local while
preserving the same ``Fsmn_vad_online`` streaming contract EasyCat uses.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path
from typing import Any

from easycat._extras import require_module


class ONNXRuntimeError(Exception):
    """Raised when ONNXRuntime inference fails."""


class _OrtInferSession:
    """Small ONNXRuntime session wrapper matching FunASR input ordering."""

    def __init__(
        self,
        model_file: str | Path,
        *,
        device_id: str | int = "-1",
        intra_op_num_threads: int = 4,
    ) -> None:
        onnxruntime = require_module("onnxruntime", extra="funasr-vad", purpose="FunASR VAD ONNX")

        device_id = str(device_id)
        sess_opt = onnxruntime.SessionOptions()
        sess_opt.intra_op_num_threads = intra_op_num_threads
        sess_opt.log_severity_level = 4
        sess_opt.enable_cpu_mem_arena = False
        sess_opt.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL

        cpu_ep = "CPUExecutionProvider"
        providers: list[Any] = [(cpu_ep, {"arena_extend_strategy": "kSameAsRequested"})]
        cuda_ep = "CUDAExecutionProvider"
        if (
            device_id != "-1"
            and onnxruntime.get_device() == "GPU"
            and cuda_ep in onnxruntime.get_available_providers()
        ):
            providers.insert(
                0,
                (
                    cuda_ep,
                    {
                        "device_id": device_id,
                        "arena_extend_strategy": "kNextPowerOfTwo",
                        "cudnn_conv_algo_search": "EXHAUSTIVE",
                        "do_copy_in_default_stream": "true",
                    },
                ),
            )

        model_path = Path(model_file)
        if not model_path.is_file():
            raise FileNotFoundError(f"{model_path} is not a file")

        self.session = onnxruntime.InferenceSession(
            str(model_path), sess_options=sess_opt, providers=providers
        )
        if device_id != "-1" and cuda_ep not in self.session.get_providers():
            warnings.warn(
                f"{cuda_ep} is not available; FunASR VAD inference is using {cpu_ep}.",
                RuntimeWarning,
                stacklevel=2,
            )

    def __call__(self, input_content: list[Any]) -> list[Any]:
        input_dict = dict(zip(self.get_input_names(), input_content, strict=False))
        try:
            return self.session.run(self.get_output_names(), input_dict)
        except Exception as exc:
            raise ONNXRuntimeError("ONNXRuntime inference failed.") from exc

    def get_input_names(self) -> list[str]:
        return [value.name for value in self.session.get_inputs()]

    def get_output_names(self) -> list[str]:
        return [value.name for value in self.session.get_outputs()]

    def close(self) -> None:
        self.session = None


class FunASROnlineRuntime:
    """Streaming runtime for the bundled FunASR FSMN-VAD ONNX model."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        device_id: str | int = "-1",
        quantize: bool = False,
        intra_op_num_threads: int = 4,
        max_end_sil: int | None = None,
    ) -> None:
        require_module("numpy", extra="funasr-vad", purpose="FunASR VAD")
        require_module("kaldi_native_fbank", extra="funasr-vad", purpose="FunASR VAD frontend")
        from easycat.vad._funasr_runtime.e2e_vad import E2EVadModel
        from easycat.vad._funasr_runtime.frontend import WavFrontendOnline

        model_path = Path(model_dir)
        if not model_path.exists():
            raise FileNotFoundError(f"FunASR model directory does not exist: {model_path}")
        if not model_path.is_dir():
            raise NotADirectoryError(f"FunASR model path is not a directory: {model_path}")

        model_file = model_path / ("model_quant.onnx" if quantize else "model.onnx")
        config_file = model_path / "config.yaml"
        cmvn_file = model_path / "am.mvn"
        missing = [str(path) for path in (model_file, config_file, cmvn_file) if not path.exists()]
        if missing:
            raise FileNotFoundError("FunASR model assets are missing: " + ", ".join(missing))

        config = _read_simple_yaml(config_file)
        self.frontend = WavFrontendOnline(cmvn_file=str(cmvn_file), **config["frontend_conf"])
        self.ort_infer = _OrtInferSession(
            model_file,
            device_id=device_id,
            intra_op_num_threads=intra_op_num_threads,
        )
        self.vad_scorer = E2EVadModel(config["model_conf"])
        self.max_end_sil = (
            max_end_sil
            if max_end_sil is not None
            else config["model_conf"]["max_end_silence_time"]
        )
        self.encoder_conf = config["encoder_conf"]

    def prepare_cache(self, in_cache: list[Any] | None = None) -> list[Any]:
        np = require_module("numpy", extra="funasr-vad", purpose="FunASR VAD")
        cache = in_cache or []
        if cache:
            return cache

        fsmn_layers = self.encoder_conf["fsmn_layers"]
        proj_dim = self.encoder_conf["proj_dim"]
        lorder = self.encoder_conf["lorder"]
        for _ in range(fsmn_layers):
            cache.append(np.zeros((1, proj_dim, lorder - 1, 1), dtype=np.float32))
        return cache

    def __call__(self, audio_in: Any, *, param_dict: dict[str, Any] | None = None) -> list[Any]:
        np = require_module("numpy", extra="funasr-vad", purpose="FunASR VAD")
        params = param_dict if param_dict is not None else {}
        waveforms = np.expand_dims(audio_in, axis=0)
        is_final = bool(params.get("is_final", False))
        frontend = params.get("frontend", self.frontend)

        feats, _feats_len = frontend.extract_fbank(
            waveforms,
            np.asarray([waveforms.shape[-1]], dtype=np.int32),
            is_final,
        )
        segments: list[Any] = []
        if feats.size:
            in_cache = self.prepare_cache(params.get("in_cache", []))
            vad_scorer = params.get("vad_scorer", self.vad_scorer)
            inputs = [feats]
            inputs.extend(in_cache)
            outputs = self.ort_infer(inputs)
            scores, out_caches = outputs[0], outputs[1:]
            params["in_cache"] = out_caches
            segments = vad_scorer(
                scores,
                frontend.get_waveforms(),
                is_final=is_final,
                max_end_sil=self.max_end_sil,
                online=True,
            )

        params["frontend"] = frontend
        params["vad_scorer"] = params.get("vad_scorer", self.vad_scorer)
        return segments

    def reset(self) -> None:
        self.frontend.cache_reset()
        self.vad_scorer.AllResetDetection()

    def close(self) -> None:
        close = getattr(self.ort_infer, "close", None)
        if callable(close):
            close()


def _read_simple_yaml(path: Path) -> dict[str, Any]:
    """Read the simple two-level YAML shape used by FunASR model configs."""
    root: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, sep, raw_value = raw_line.strip().partition(":")
        if not sep:
            continue
        if indent == 0:
            if raw_value.strip():
                root[key] = _parse_yaml_scalar(raw_value.strip())
                current = None
            else:
                current = {}
                root[key] = current
        elif current is not None:
            current[key] = _parse_yaml_scalar(raw_value.strip())
    return root


def _parse_yaml_scalar(value: str) -> Any:
    if value in {"", "null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value.startswith("["):
        return ast.literal_eval(value)
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value.strip("\"'")
