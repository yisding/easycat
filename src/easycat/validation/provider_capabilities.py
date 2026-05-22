"""Provider capability report models.

V4.1 keeps providers protocol-free: live wrappers may derive capability values
from config and adapter metadata, then serialize them through this stable shape.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from easycat.validation.report import redact_text

ProviderCapabilityStatus = Literal[
    "pass",
    "expected_skip",
    "auth_failure",
    "quota_failure",
    "provider_drift",
    "failure",
]
ProviderContractStatus = Literal["pass", "fail", "expected_skip", "unknown"]
ProviderSchemaStatus = Literal[
    "unchanged",
    "additive_warning",
    "breaking_failure",
    "unknown",
]

_REDACTED_PROVIDER_IDENTIFIER = "[REDACTED_PROVIDER_IDENTIFIER]"


@dataclass(frozen=True)
class ProviderIdentifier:
    """Provider model or voice identifier with explicit low-cardinality safety."""

    value: str
    safe: bool = False

    def to_json_value(self) -> str:
        if self.safe:
            return redact_text(self.value)
        return _REDACTED_PROVIDER_IDENTIFIER


@dataclass(frozen=True)
class ProviderCapabilities:
    input_audio_formats: Sequence[str] = field(default_factory=tuple)
    output_audio_formats: Sequence[str] = field(default_factory=tuple)
    streaming: bool | None = None
    streaming_behavior: str | None = None
    finalization_behavior: str | None = None
    markers: bool | None = None
    alignment: bool | None = None
    ssml: bool | None = None
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self, *, api_version_header_behavior: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "input_audio_formats": _redact_value(list(self.input_audio_formats)),
            "output_audio_formats": _redact_value(list(self.output_audio_formats)),
        }

        optional_fields: dict[str, Any] = {
            "streaming": self.streaming,
            "streaming_behavior": self.streaming_behavior,
            "finalization_behavior": self.finalization_behavior,
            "markers": self.markers,
            "alignment": self.alignment,
            "ssml": self.ssml,
            "api_version_header_behavior": api_version_header_behavior,
        }
        for key, value in optional_fields.items():
            if value is not None:
                payload[key] = _redact_value(value)

        if self.provider_options:
            payload["provider_options"] = _redact_value(self.provider_options)

        return payload


@dataclass(frozen=True)
class ProviderCapabilityReport:
    provider: str
    surface: str
    adapter: str
    protocol: str
    mode: str
    adapter_version: str
    required_extra: str
    credential_env_var: str
    credential_env_var_present: bool
    api_version: str
    api_version_header_behavior: str
    capabilities: ProviderCapabilities
    contract_status: ProviderContractStatus | str
    schema_status: ProviderSchemaStatus | str
    status: ProviderCapabilityStatus | str
    live_checked_at: datetime | str | None = None
    models: Sequence[ProviderIdentifier] = field(default_factory=tuple)
    voices: Sequence[ProviderIdentifier] = field(default_factory=tuple)
    latency: Mapping[str, Any] | None = None
    failure_class: str | None = None
    schema_version: int = 1
    redaction_version: int = 1
    kind: str = "provider_capability_report"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "redaction_version": self.redaction_version,
            "provider": redact_text(self.provider),
            "surface": redact_text(self.surface),
            "adapter": redact_text(self.adapter),
            "protocol": redact_text(self.protocol),
            "mode": redact_text(self.mode),
            "adapter_version": redact_text(self.adapter_version),
            "required_extra": redact_text(self.required_extra),
            "live_checked_at": _serialize_datetime_or_none(self.live_checked_at),
            "api_version": redact_text(self.api_version),
            "auth": {
                "credential_env_var": redact_text(self.credential_env_var),
                "credential_env_var_present": bool(self.credential_env_var_present),
            },
            "capabilities": self.capabilities.to_dict(
                api_version_header_behavior=self.api_version_header_behavior
            ),
            "models": [model.to_json_value() for model in self.models],
            "voices": [voice.to_json_value() for voice in self.voices],
            "contract_status": redact_text(str(self.contract_status)),
            "schema_status": redact_text(str(self.schema_status)),
            "latency": _redact_value(self.latency) if self.latency is not None else None,
            "failure_class": redact_text(self.failure_class) if self.failure_class else None,
            "status": redact_text(str(self.status)),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


def _serialize_datetime_or_none(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return redact_text(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence):
        return [_redact_value(item) for item in value]
    return value
