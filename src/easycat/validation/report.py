from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from easycat.validation._report_models import (
    ArtifactRef,
    GitMetadata,
    ProviderCheck,
    ProviderCheckState,
    ValidationCheck,
    ValidationEnvironment,
    ValidationFailure,
    ValidationSkip,
    ValidationStatus,
)
from easycat.validation._report_serialization import serialize_dataclass
from easycat.validation.redaction import REDACTION_VERSION
from easycat.validation.redaction import (
    redact_runtime_secrets as _redact_runtime_secrets,
)
from easycat.validation.redaction import (
    redact_text as _redact_text,
)

__all__ = [
    "ArtifactRef",
    "GitMetadata",
    "ProviderCheck",
    "ProviderCheckState",
    "ValidationCheck",
    "ValidationEnvironment",
    "ValidationFailure",
    "ValidationRun",
    "ValidationSkip",
    "ValidationStatus",
    "redact_runtime_secrets",
    "redact_text",
]


@dataclass(frozen=True)
class ValidationRun:
    run_id: str
    command: Sequence[str] | str
    started_at: datetime | str
    finished_at: datetime | str
    duration_s: float
    status: ValidationStatus
    exit_code: int
    tool_exit_codes: Mapping[str, int] = field(default_factory=dict)
    git: GitMetadata = field(default_factory=GitMetadata)
    environment: ValidationEnvironment | None = None
    checks: Sequence[ValidationCheck] = field(default_factory=list)
    skips: Sequence[ValidationSkip] = field(default_factory=list)
    failures: Sequence[ValidationFailure] = field(default_factory=list)
    latency: Mapping[str, Any] | None = None
    reliability: Mapping[str, Any] | None = None
    providers: Sequence[ProviderCheck] = field(default_factory=list)
    provider_reports: Sequence[Mapping[str, Any]] = field(default_factory=list)
    extras: Sequence[str] = field(default_factory=list)
    artifacts: Mapping[str, ArtifactRef] = field(default_factory=dict)
    schema_version: int = 1
    redaction_version: int = REDACTION_VERSION
    kind: str = "validation_run"

    def to_dict(self) -> dict[str, Any]:
        return serialize_dataclass(
            self,
            include_none=frozenset({"latency"}),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def redact_text(value: str) -> str:
    return _redact_text(value)


def redact_runtime_secrets(value: str, secrets: Sequence[str] | None = None) -> str:
    return _redact_runtime_secrets(value, secrets)
