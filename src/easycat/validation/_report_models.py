"""Leaf models used by validation reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

ValidationStatus = Literal["pass", "fail", "skip", "error"]


class ProviderCheckState(StrEnum):
    NOT_REQUESTED = "not_requested"
    SKIPPED_MISSING_SECRET = "skipped_missing_secret"
    FAILED_MISSING_REQUIRED_SECRET = "failed_missing_required_secret"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    path: str


@dataclass(frozen=True)
class GitMetadata:
    sha: str | None = None
    branch: str | None = None
    dirty: bool | None = None


@dataclass(frozen=True)
class ValidationEnvironment:
    python: str
    platform: str
    ci: bool
    env_vars: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    status: ValidationStatus
    duration_s: float
    command: Sequence[str] | str | None = None
    artifacts: Mapping[str, ArtifactRef] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationSkip:
    name: str
    reason: str
    expected: bool = True


@dataclass(frozen=True)
class ValidationFailure:
    name: str
    message: str
    failure_class: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCheck:
    provider: str
    surface: str
    state: ProviderCheckState | str
    credential_env: str | None = None
    required: bool = False
    failure_class: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
