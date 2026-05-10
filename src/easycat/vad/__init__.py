"""Voice Activity Detection implementations: Silero, TEN, FunASR, and Krisp.

Each backend implements the ``VADProvider`` protocol from
``easycat.providers``:

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]
    def configure(self, ...) -> None

The factory function :func:`create_vad` selects the best available backend
with automatic fallback from Silero -> FunASR -> TEN -> Krisp.  TEN VAD is
installed via the ``ten-vad`` optional extra; we no longer vendor its
binaries because the upstream license is incompatible with this project's
redistribution terms.
"""

from __future__ import annotations

from easycat.vad._base import VADBackend
from easycat.vad.factory import VADConfig, create_vad
from easycat.vad.funasr import FunASROnnxVAD
from easycat.vad.krisp import KrispVAD
from easycat.vad.silero import SileroVAD
from easycat.vad.ten import TenVAD

__all__ = [
    "FunASROnnxVAD",
    "KrispVAD",
    "SileroVAD",
    "TenVAD",
    "VADBackend",
    "VADConfig",
    "create_vad",
]
