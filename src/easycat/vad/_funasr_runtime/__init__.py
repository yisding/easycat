"""Vendored runtime pieces for the bundled FunASR FSMN-VAD model."""

from easycat.vad._funasr_runtime.online import FunASROnlineRuntime, ONNXRuntimeError

__all__ = ["FunASROnlineRuntime", "ONNXRuntimeError"]
