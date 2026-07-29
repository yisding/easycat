"""Value types and errors shared by bundle readers, writers, and replay."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FORMAT_VERSION = 1
_ARTIFACT_SIZE_CAP = 500_000_000
# Bound the JSON object overhead independently from decoded bytes. At 80 bytes
# per entry (64-byte SHA ref plus JSON punctuation/indentation), this cap keeps
# entry metadata within 8 MB while still allowing far more artifacts than a
# practical debug session produces.
_INLINE_ARTIFACT_COUNT_CAP = 100_000
_INLINE_ARTIFACT_ENTRY_OVERHEAD = 80
_SHA256_REF = re.compile(r"^[a-f0-9]{64}$")


class BundleError(RuntimeError): ...


class BundleExists(BundleError): ...


class BundleVersionError(BundleError): ...


class BundleValidationError(BundleError):
    def __init__(self, message: str, *, reason_code: str = "UNKNOWN") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class BundleInUseError(BundleError): ...


class BundleRecoveryError(BundleError): ...


class DebugCaptureDisabledError(BundleError): ...


@dataclass(frozen=True)
class ArtifactEntry:
    ref: str
    size_bytes: int = 0


_CHECKPOINT_ID_PREFIX = "cp_"


def checkpoint_id(sequence: int) -> str:
    """Convert a monotonic journal sequence to its public checkpoint id.

    The ``cp_<sequence>`` vocabulary is shared by the debugger UI, replay
    commands, and coding-agent prompts while the journal retains the integer
    sequence used for ordering and indexing.
    """
    if sequence < 0:
        raise ValueError(f"checkpoint sequence must be non-negative, got {sequence}")
    return f"{_CHECKPOINT_ID_PREFIX}{sequence}"


def parse_checkpoint_id(value: str) -> int:
    """Inverse of :func:`checkpoint_id`. Raise ``ValueError`` on a bad id."""
    if not isinstance(value, str) or not value.startswith(_CHECKPOINT_ID_PREFIX):
        raise ValueError(f"Invalid checkpoint id {value!r}: expected 'cp_<int>'")
    raw = value[len(_CHECKPOINT_ID_PREFIX) :]
    try:
        sequence = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid checkpoint id {value!r}: not an integer") from exc
    if sequence < 0:
        raise ValueError(f"Invalid checkpoint id {value!r}: negative sequence")
    return sequence


@dataclass(frozen=True)
class CommittableCheckpoint:
    sequence: int
    stage: str
    unit_id: str = ""

    @property
    def checkpoint_id(self) -> str:
        """Return the ``cp_<sequence>`` public id for this checkpoint."""
        return checkpoint_id(self.sequence)


@dataclass(frozen=True)
class Manifest:
    format_version: int = FORMAT_VERSION
    provider_versions: dict[str, Any] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    env_metadata: dict[str, str] = field(default_factory=dict)
    journal_dropped_records: int = 0
    sharing_banner: str = ""
