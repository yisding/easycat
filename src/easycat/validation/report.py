from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
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
    redaction_version: int = 1
    kind: str = "validation_run"

    def to_dict(self) -> dict[str, Any]:
        return _serialize_dataclass(self, include_none={"latency"})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def redact_text(value: str) -> str:
    return _redact_string(value)


def redact_runtime_secrets(value: str, secrets: Sequence[str] | None = None) -> str:
    redacted = _redact_string(value)
    for secret in sorted({secret for secret in secrets or () if secret}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED_SECRET]")
    return redacted


_URL_RE = re.compile(r"https?://[^\s\"')\]}]+")
_SECRET_RE = re.compile(r"\b(?:sk|sess|key|tok)-[A-Za-z0-9_-]{12,}\b")
_BEARER_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s;,]+")
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)((?:--(?:api[-_]?key|token|secret|password)\s+)|"
    r"(?:(?:--)?(?:api[-_]?key|token|secret|password)=)|"
    r"(?:(?:api[-_]?key|token|secret|password):\s*))[^\s;,]+"
)
_REQUEST_ID_RE = re.compile(r"\b(?:req|request|resp)_[A-Za-z0-9_-]{6,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_HOME_PATH_RE = re.compile(r"(?P<prefix>^|[\s=:])(?:/home|/Users)/[^/\s:]+")

_UNSAFE_TEXT_FIELDS = {
    "generated_provider_text": "[REDACTED_PROVIDER_TEXT]",
    "generated_text": "[REDACTED_PROVIDER_TEXT]",
    "phone_number": "[REDACTED_PHONE]",
    "prompt": "[REDACTED_PROMPT]",
    "provider_output": "[REDACTED_PROVIDER_TEXT]",
    "provider_request_id": "[REDACTED_REQUEST_ID]",
    "provider_text": "[REDACTED_PROVIDER_TEXT]",
    "request_id": "[REDACTED_REQUEST_ID]",
    "transcript": "[REDACTED_TRANSCRIPT]",
}


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def _serialize_dataclass(value: Any, include_none: set[str] | None = None) -> dict[str, Any]:
    include_none = include_none or set()
    payload: dict[str, Any] = {}
    for item in fields(value):
        field_value = getattr(value, item.name)
        if field_value is None and item.name not in include_none:
            continue
        payload[item.name] = _serialize_value(field_value, item.name)
    return payload


def _serialize_value(value: Any, key: str | None = None) -> Any:
    if key in _UNSAFE_TEXT_FIELDS and value:
        return _UNSAFE_TEXT_FIELDS[key]

    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if is_dataclass(value):
        if isinstance(value, ValidationEnvironment):
            return _serialize_environment(value)
        return {
            field_name: field_value
            for field_name, field_value in _serialize_dataclass(value).items()
            if not _is_empty_optional(field_value)
        }
    if isinstance(value, Mapping):
        return {
            str(item_key): _serialize_value(item_value, str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
            if not _is_empty_optional(item_value)
        }
    if isinstance(value, Sequence):
        return [_serialize_value(item, key) for item in value]
    return value


def _serialize_environment(value: ValidationEnvironment) -> dict[str, Any]:
    return {
        "python": _redact_string(value.python),
        "platform": _redact_string(value.platform),
        "ci": value.ci,
        "env_vars": {
            str(name): bool(present)
            for name, present in sorted(value.env_vars.items(), key=lambda item: str(item[0]))
        },
    }


def _is_empty_optional(value: Any) -> bool:
    return value is None or value == {} or value == ()


def _redact_string(value: str) -> str:
    redacted = _URL_RE.sub("[REDACTED_URL]", value)
    redacted = _BEARER_RE.sub(r"\1[REDACTED_SECRET]", redacted)
    redacted = _KEY_VALUE_SECRET_RE.sub(r"\1[REDACTED_SECRET]", redacted)
    redacted = _SECRET_RE.sub("[REDACTED_SECRET]", redacted)
    redacted = _REQUEST_ID_RE.sub("[REDACTED_REQUEST_ID]", redacted)
    redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)
    redacted = _HOME_PATH_RE.sub(lambda match: f"{match.group('prefix')}~", redacted)
    return redacted
