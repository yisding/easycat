"""Shared redaction policy for validation reports and contract artifacts."""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from easycat._provider_catalog import sensitive_api_domains

REDACTION_VERSION = 1

REDACTED_PHONE = "[REDACTED_PHONE]"
REDACTED_PROMPT = "[REDACTED_PROMPT]"
REDACTED_PROVIDER_TEXT = "[REDACTED_PROVIDER_TEXT]"
REDACTED_REQUEST_ID = "[REDACTED_REQUEST_ID]"
REDACTED_SECRET = "[REDACTED_SECRET]"
REDACTED_TRANSCRIPT = "[REDACTED_TRANSCRIPT]"
REDACTED_URL = "[REDACTED_URL]"

UNSAFE_TEXT_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "generated_provider_text": REDACTED_PROVIDER_TEXT,
        "generated_text": REDACTED_PROVIDER_TEXT,
        "phone_number": REDACTED_PHONE,
        "prompt": REDACTED_PROMPT,
        "provider_output": REDACTED_PROVIDER_TEXT,
        "provider_request_id": REDACTED_REQUEST_ID,
        "provider_text": REDACTED_PROVIDER_TEXT,
        "request_id": REDACTED_REQUEST_ID,
        "transcript": REDACTED_TRANSCRIPT,
    }
)

_SAFE_SECRET_NAME_FIELDS = frozenset(
    {
        "credential_env",
        "credential_env_present",
        "credential_env_var",
        "credential_env_var_present",
        "env_vars",
    }
)

_SECRET_KEY_RE = re.compile(
    r"(?i)(^|[_\-.])("
    r"api[-_]?key|"
    r"x[-_]?api[-_]?key|"
    r"xi[-_]?api[-_]?key|"
    r"access[-_]?token|"
    r"refresh[-_]?token|"
    r"client[-_]?secret|"
    r"signed[-_]?url|"
    r"signature|"
    r"authorization|"
    r"bearer|"
    r"credential|"
    r"password|"
    r"secret|"
    r"token|"
    r"key"
    r")($|[_\-.])"
)

_URL_RE = re.compile(r"https?://[^\s\"')\]}]+")
# Provider API domains come from the STT/TTS provider catalogs, so a
# newly registered provider's URLs are flagged without touching this file.
_SENSITIVE_URL_RE = re.compile(
    r"https?://(?:[^/\s:@]+:[^/\s:@]+@)?[^\s\"')\]}]*(?:"
    + "|".join(re.escape(domain) for domain in sensitive_api_domains())
    + r")[^\s\"')\]}]*",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(r"\b(?:sk|sess|key|tok)-[A-Za-z0-9_-]{12,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_HEADER_SECRET_RE = re.compile(
    r"(?i)((?:authorization|x-api-key|xi-api-key|openai-organization|openai-project)"
    r"\s*[:=]\s*)(?:bearer\s+)?[^\s;,]+"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[^\s;,]+")
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)((?:--(?:api[-_]?key|token|secret|password|access[-_]?token|client[-_]?secret)"
    r"\s+)|"
    r"(?:(?:--)?(?:api[-_]?key|token|secret|password|access[-_]?token|client[-_]?secret)="
    r")|"
    r"(?:(?:api[-_]?key|x[-_]?api[-_]?key|xi[-_]?api[-_]?key|token|secret|password|"
    r"access[-_]?token|refresh[-_]?token|client[-_]?secret|signed[-_]?url|signature):"
    r"\s*))[^\s;,]+"
)
_REQUEST_ID_RE = re.compile(r"\b(?:req|request|resp|response)_[A-Za-z0-9_-]{6,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_HOME_PATH_RE = re.compile(r"(?P<prefix>^|[\s=:\"'])(?:/home|/Users)/[^/\s:]+")


def _secret_after_prefix(match: re.Match[str]) -> str:
    return f"{match.group(1)}{REDACTED_SECRET}"


def _redacted_home_path(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}~"


_TextReplacement = str | Callable[[re.Match[str]], str]
_TEXT_REDACTIONS: tuple[tuple[re.Pattern[str], _TextReplacement], ...] = (
    (_URL_RE, REDACTED_URL),
    (_HEADER_SECRET_RE, _secret_after_prefix),
    (_BEARER_RE, _secret_after_prefix),
    (_KEY_VALUE_SECRET_RE, _secret_after_prefix),
    (_JWT_RE, REDACTED_SECRET),
    (_SECRET_RE, REDACTED_SECRET),
    (_REQUEST_ID_RE, REDACTED_REQUEST_ID),
    (_PHONE_RE, REDACTED_PHONE),
    (_HOME_PATH_RE, _redacted_home_path),
)
# Every text-redaction pattern above requires at least one of these trigger
# characters, except the standalone ``Bearer <value>`` and phone-number forms.
# The latter is included with its complete shape so an isolated digit does not
# force numbered transcripts through all nine substitution regexes.  Checking
# this superset first keeps that work off common hot-path values.  False
# positives are harmless: they merely fall through to the complete policy.
_TEXT_REDACTION_TRIGGER_RE = re.compile(
    rf"[:/=_\-.]|bearer\s|{_PHONE_RE.pattern}",
    re.IGNORECASE,
)
_SENSITIVE_COMMAND_FLAGS = frozenset(
    {
        "--access-token",
        "--api-key",
        "--client-secret",
        "--password",
        "--secret",
        "--token",
    }
)


def redact_text(value: str) -> str:
    """Redact sensitive substrings from free-form validation text."""
    if _TEXT_REDACTION_TRIGGER_RE.search(value) is None:
        return value
    redacted = value
    for pattern, replacement in _TEXT_REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_runtime_secrets(value: str, secrets: Sequence[str] | None = None) -> str:
    """Redact policy-detected text plus exact runtime secret values."""
    redacted = redact_text(value)
    for secret in sorted({secret for secret in secrets or () if secret}, key=len, reverse=True):
        redacted = redacted.replace(secret, REDACTED_SECRET)
    return redacted


def redact_value(value: Any, key: str | None = None) -> Any:
    """Redact a JSON-compatible value using both field-name and value policy."""
    normalized_key = str(key).lower() if key is not None else None
    if normalized_key == "command":
        return redact_command(value)
    if normalized_key is not None and normalized_key in UNSAFE_TEXT_FIELDS and _has_value(value):
        return UNSAFE_TEXT_FIELDS[normalized_key]
    if _is_secret_value_key(normalized_key) and not isinstance(value, bool | type(None)):
        return REDACTED_SECRET

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item_value, str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_value(item, key) for item in value]
    return value


def contains_unredacted_sensitive_text(value: str) -> bool:
    """Return True when contract artifacts still contain sensitive patterns."""
    if any(pattern.search(value) for pattern in (_SENSITIVE_URL_RE, _SECRET_RE, _JWT_RE)):
        return True
    for pattern in (_HEADER_SECRET_RE, _KEY_VALUE_SECRET_RE, _REQUEST_ID_RE):
        for match in pattern.finditer(value):
            if _is_placeholder_match(match.group(0)):
                continue
            return True
    return False


def redact_command(value: Any) -> Any:
    """Redact command strings and argv-style command sequences."""
    if isinstance(value, str):
        return redact_text(value)
    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
        return redact_value(value)

    redacted: list[Any] = []
    redact_next = False
    for item in value:
        if redact_next:
            redacted.append(REDACTED_SECRET if isinstance(item, str) else redact_value(item))
            redact_next = False
            continue
        if isinstance(item, str):
            redacted_item = redact_text(item)
            flag_name = item.split("=", 1)[0].lower()
            if flag_name in _SENSITIVE_COMMAND_FLAGS and "=" not in item:
                redact_next = True
            redacted.append(redacted_item)
            continue
        redacted.append(redact_value(item))
    return redacted


def should_redact_key(key: str | None) -> bool:
    normalized_key = str(key).lower() if key is not None else None
    return normalized_key in UNSAFE_TEXT_FIELDS or _is_secret_value_key(normalized_key)


@functools.lru_cache(maxsize=256)
def _is_secret_value_key(key: str | None) -> bool:
    """Classify repeated schema keys without rerunning the policy regex.

    Journal and validation records repeatedly use a small vocabulary of field
    names.  The bound prevents arbitrary input keys from growing process memory
    while keeping those common names on the constant-time path.
    """
    if not key or key in _SAFE_SECRET_NAME_FIELDS:
        return False
    return bool(_SECRET_KEY_RE.search(key))


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != {} and value != () and value != []


def _is_placeholder_match(value: str) -> bool:
    return any(
        placeholder in value
        for placeholder in (
            REDACTED_PHONE,
            REDACTED_PROMPT,
            REDACTED_PROVIDER_TEXT,
            REDACTED_REQUEST_ID,
            REDACTED_SECRET,
            REDACTED_TRANSCRIPT,
            REDACTED_URL,
        )
    )


__all__ = [
    "REDACTION_VERSION",
    "REDACTED_SECRET",
    "UNSAFE_TEXT_FIELDS",
    "contains_unredacted_sensitive_text",
    "redact_command",
    "redact_runtime_secrets",
    "redact_text",
    "redact_value",
    "should_redact_key",
]
