"""Shared redaction policy for validation reports and contract artifacts."""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast
from xml.sax.saxutils import escape as xml_escape

from easycat._provider_domains import sensitive_api_domains

REDACTION_VERSION = 1

REDACTED_PHONE = "[REDACTED_PHONE]"
REDACTED_PROMPT = "[REDACTED_PROMPT]"
REDACTED_PROVIDER_TEXT = "[REDACTED_PROVIDER_TEXT]"
REDACTED_REQUEST_ID = "[REDACTED_REQUEST_ID]"
REDACTED_SECRET = "[REDACTED_SECRET]"
REDACTED_TRANSCRIPT = "[REDACTED_TRANSCRIPT]"
REDACTED_URL = "[REDACTED_URL]"

RedactionPolicy: TypeAlias = Literal["secrets", "pii"]
TextArtifactFormat: TypeAlias = Literal["json", "jsonl", "text"]
ArtifactRedactionOperation: TypeAlias = Literal["parse", "scrub", "serialize"]


class ArtifactRedactionError(Exception):
    """A validation artifact could not be safely transformed for publication."""

    def __init__(
        self,
        artifact_format: TextArtifactFormat,
        operation: ArtifactRedactionOperation,
        cause: Exception,
    ) -> None:
        self.artifact_format = artifact_format
        self.operation = operation
        self.cause_type = type(cause).__name__
        super().__init__(f"{artifact_format} artifact {operation} failed with {self.cause_type}")


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

_BARE_SECRET_VALUE_KEYS = frozenset({"api", "auth"})

_SECRET_KEY_RE = re.compile(
    r"(?i)(^|[_\-.])("
    r"api[-_]?key|"
    r"x[-_]?api[-_]?key|"
    r"xi[-_]?api[-_]?key|"
    r"(?:api|auth)[-_.]?(?:header|value)|"
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
_CAMEL_ACRONYM_BOUNDARY_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_URL_RE = re.compile(r"(?:https?|wss?|ws)://[^\s\"')\]}]+", re.IGNORECASE)
_URL_USERINFO_SECRET_RE = re.compile(r"(?i)((?:https?|wss?|ws)://[^/\s:@]+:)[^/\s@]+(@)")
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:"
    r"[^&=#\s]*(?:api[-_]?key|access[-_]?token|refresh[-_]?token|token|secret|"
    r"password|signature|credential)[^&=#\s]*|key|sig"
    r")=)[^&#\s]+"
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


def _redacted_url_secret(match: re.Match[str]) -> str:
    suffix = match.group(2) if match.lastindex == 2 else ""
    return f"{match.group(1)}{REDACTED_SECRET}{suffix}"


_TextReplacement = str | Callable[[re.Match[str]], str]
_SECRET_TEXT_REDACTIONS: tuple[tuple[re.Pattern[str], _TextReplacement], ...] = (
    (_URL_USERINFO_SECRET_RE, _redacted_url_secret),
    (_URL_QUERY_SECRET_RE, _redacted_url_secret),
    (_HEADER_SECRET_RE, _secret_after_prefix),
    (_BEARER_RE, _secret_after_prefix),
    (_KEY_VALUE_SECRET_RE, _secret_after_prefix),
    (_JWT_RE, REDACTED_SECRET),
    (_SECRET_RE, REDACTED_SECRET),
)
_PII_TEXT_REDACTIONS: tuple[tuple[re.Pattern[str], _TextReplacement], ...] = (
    (_URL_RE, REDACTED_URL),
    *_SECRET_TEXT_REDACTIONS,
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
_PII_TEXT_REDACTION_TRIGGER_RE = re.compile(
    rf"[:/=_\-.]|bearer\s|{_PHONE_RE.pattern}",
    re.IGNORECASE,
)
_SECRET_TEXT_REDACTION_TRIGGER_RE = re.compile(r"[:=_\-.]|bearer\s", re.IGNORECASE)
# Backwards-compatible private aliases used by the validation policy tests.
_TEXT_REDACTIONS = _PII_TEXT_REDACTIONS
_TEXT_REDACTION_TRIGGER_RE = _PII_TEXT_REDACTION_TRIGGER_RE
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


def validate_redaction_policy(policy: str) -> RedactionPolicy:
    """Validate and narrow a journal/validation redaction policy."""
    if policy not in ("secrets", "pii"):
        raise ValueError(f"Unknown redaction policy: {policy!r}")
    return cast(RedactionPolicy, policy)


def redact_text(value: str, *, policy: RedactionPolicy = "pii") -> str:
    """Redact sensitive substrings from free-form text.

    ``policy="secrets"`` preserves replay-relevant customer content such as
    phone numbers, URLs, request IDs, and filesystem paths while still
    scrubbing credentials. ``policy="pii"`` applies the complete
    validation/export policy and remains the default for existing callers.
    """
    if policy == "secrets":
        redactions = _SECRET_TEXT_REDACTIONS
        trigger = _SECRET_TEXT_REDACTION_TRIGGER_RE
    elif policy == "pii":
        redactions = _PII_TEXT_REDACTIONS
        trigger = _PII_TEXT_REDACTION_TRIGGER_RE
    else:
        raise ValueError(f"Unknown redaction policy: {policy!r}")
    if trigger.search(value) is None:
        return value
    redacted = value
    for pattern, replacement in redactions:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _runtime_secret_text_variants(secrets: Sequence[str] | None) -> tuple[str, ...]:
    """Return raw, JSON-escaped, and XML-escaped configured secret spellings."""
    variants: set[str] = set()
    for secret in secrets or ():
        if not secret:
            continue
        variants.add(secret)
        # Structured artifacts normally decode before redaction. These escaped
        # spellings cover malformed JSON/JSONL fallback paths where only the
        # serialized source text remains available.
        variants.add(json.dumps(secret, ensure_ascii=True)[1:-1])
        variants.add(json.dumps(secret, ensure_ascii=False)[1:-1])
        # JUnit is XML but is treated as an unstructured validation artifact.
        # Match its entity-escaped secret spelling before general text policy
        # runs, or values such as ``token&value`` leak as ``token&amp;value``.
        variants.add(xml_escape(secret, {'"': "&quot;", "'": "&apos;"}))
    return tuple(sorted(variants, key=len, reverse=True))


def redact_runtime_secrets(value: str, secrets: Sequence[str] | None = None) -> str:
    """Redact policy-detected text plus exact runtime secret values."""
    redacted = value
    for variant in _runtime_secret_text_variants(secrets):
        redacted = redacted.replace(variant, REDACTED_SECRET)
    return redact_text(redacted)


def _redact_runtime_secret_value(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_runtime_secrets(value, secrets)
    if isinstance(value, Mapping):
        return {
            redact_runtime_secrets(str(item_key), secrets): _redact_runtime_secret_value(
                item_value,
                secrets,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_runtime_secret_value(item, secrets) for item in value]
    return value


def _parse_json(value: str, artifact_format: TextArtifactFormat) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise
    except Exception as exc:
        raise ArtifactRedactionError(artifact_format, "parse", exc) from exc


def _scrub_json_value(
    payload: Any,
    secrets: Sequence[str],
    artifact_format: TextArtifactFormat,
) -> Any:
    try:
        return _redact_runtime_secret_value(payload, secrets)
    except Exception as exc:
        raise ArtifactRedactionError(artifact_format, "scrub", exc) from exc


def _serialize_json(
    payload: Any,
    artifact_format: TextArtifactFormat,
    **kwargs: Any,
) -> str:
    try:
        return json.dumps(payload, **kwargs)
    except Exception as exc:
        raise ArtifactRedactionError(artifact_format, "serialize", exc) from exc


def _redact_json_text(value: str, secrets: Sequence[str]) -> str:
    payload = _parse_json(value, "json")
    suffix = "\n" if value.endswith(("\n", "\r")) else ""
    return (
        _serialize_json(
            _scrub_json_value(payload, secrets, "json"),
            "json",
            indent=2,
            sort_keys=True,
        )
        + suffix
    )


def _redact_jsonl_text(value: str, secrets: Sequence[str]) -> str:
    lines: list[str] = []
    for line in value.splitlines():
        if not line.strip():
            lines.append("")
            continue
        payload = _parse_json(line, "jsonl")
        lines.append(
            _serialize_json(
                _scrub_json_value(payload, secrets, "jsonl"),
                "jsonl",
                sort_keys=True,
            )
        )
    suffix = "\n" if value.endswith(("\n", "\r")) else ""
    return "\n".join(lines) + suffix


def _scrub_binary_runtime_secret_bytes(path: Path, secrets: Sequence[str]) -> None:
    raw_value = path.read_bytes()
    redacted = raw_value
    byte_variants: set[bytes] = set()
    for variant in _runtime_secret_text_variants(secrets):
        try:
            byte_variants.add(variant.encode("utf-8", errors="surrogateescape"))
        except UnicodeEncodeError:
            # JSON's ensure_ascii spelling above remains encodable even when an
            # environment value contains an otherwise unencodable surrogate.
            continue
    replacement = REDACTED_SECRET.encode("utf-8")
    for byte_variant in sorted(byte_variants, key=len, reverse=True):
        redacted = redacted.replace(byte_variant, replacement)
    if redacted != raw_value:
        path.write_bytes(redacted)


def _file_redaction_io_failure(exc: OSError, *, raise_on_error: bool) -> bool:
    if raise_on_error:
        raise exc
    return False


def _scrub_unstructured_artifact(
    value: str,
    secrets: Sequence[str],
    artifact_format: TextArtifactFormat,
) -> str:
    try:
        return redact_runtime_secrets(value, secrets)
    except Exception as exc:
        raise ArtifactRedactionError(artifact_format, "scrub", exc) from exc


def _redact_artifact_text(
    value: str,
    secrets: Sequence[str],
    artifact_format: TextArtifactFormat,
) -> str:
    if artifact_format == "text":
        return _scrub_unstructured_artifact(value, secrets, artifact_format)
    try:
        if artifact_format == "json":
            return _redact_json_text(value, secrets)
        return _redact_jsonl_text(value, secrets)
    except json.JSONDecodeError:
        # Keep malformed evidence malformed so the normal artifact parser can
        # report it, but still remove exact secrets from its source text.
        return _scrub_unstructured_artifact(value, secrets, artifact_format)


def redact_runtime_secrets_in_file(
    path: Path,
    secrets: Sequence[str],
    *,
    artifact_format: TextArtifactFormat = "text",
    raise_on_error: bool = False,
) -> bool:
    """Scrub one validation-owned text artifact without damaging binary files.

    JSON and JSONL are decoded before redaction so secrets containing quotes or
    backslashes are removed from their logical string values, then serialized
    back into valid structured text. Non-UTF-8 artifacts receive exact
    byte-level secret replacement before being reported as malformed; missing
    or unreadable paths are left untouched and reported as ``False``. Set
    ``raise_on_error`` when callers must distinguish an I/O failure from a
    missing or malformed artifact before publishing it.
    """
    try:
        if not path.is_file():
            return False
        value = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # The artifact is corrupt as text and its normal parser will report
        # that, but it still crosses the validation artifact boundary. Remove
        # exact UTF-8 secret bytes before callers publish the path.
        try:
            _scrub_binary_runtime_secret_bytes(path, secrets)
        except OSError as exc:
            return _file_redaction_io_failure(exc, raise_on_error=raise_on_error)
        return False
    except OSError as exc:
        return _file_redaction_io_failure(exc, raise_on_error=raise_on_error)

    redacted = _redact_artifact_text(value, secrets, artifact_format)

    if redacted != value:
        try:
            path.write_text(redacted, encoding="utf-8")
        except OSError as exc:
            return _file_redaction_io_failure(exc, raise_on_error=raise_on_error)
    return True


def redact_value(
    value: Any,
    key: str | None = None,
    *,
    policy: RedactionPolicy = "pii",
) -> Any:
    """Redact a JSON-compatible value using both field-name and value policy."""
    normalized_key = _normalize_key_name(key)
    if normalized_key == "command":
        return redact_command(value, policy=policy)
    if (
        policy == "pii"
        and normalized_key is not None
        and normalized_key in UNSAFE_TEXT_FIELDS
        and _has_value(value)
    ):
        return UNSAFE_TEXT_FIELDS[normalized_key]
    if _is_secret_value_key(normalized_key) and not isinstance(value, bool | type(None)):
        return REDACTED_SECRET

    if isinstance(value, str):
        return redact_text(value, policy=policy)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item_value, str(item_key), policy=policy)
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_value(item, key, policy=policy) for item in value]
    return value


def contains_unredacted_sensitive_text(value: str) -> bool:
    """Return True when contract artifacts still contain sensitive patterns."""
    if any(pattern.search(value) for pattern in (_sensitive_url_re(), _SECRET_RE, _JWT_RE)):
        return True
    for pattern in (_HEADER_SECRET_RE, _KEY_VALUE_SECRET_RE, _REQUEST_ID_RE):
        for match in pattern.finditer(value):
            if _is_placeholder_match(match.group(0)):
                continue
            return True
    return False


@functools.lru_cache(maxsize=32)
def _compile_sensitive_url_re(domains: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(domain) for domain in domains)
    if not alternatives:
        return re.compile(r"(?!x)x")
    return re.compile(
        r"(?:https?|wss?|ws)://(?:[^/\s:@]+:[^/\s:@]+@)?[^\s\"')\]}]*(?:"
        + alternatives
        + r")[^\s\"')\]}]*",
        re.IGNORECASE,
    )


def _sensitive_url_re() -> re.Pattern[str]:
    """Compile against the current domain snapshot, including plugin registrations."""
    return _compile_sensitive_url_re(sensitive_api_domains())


def redact_command(value: Any, *, policy: RedactionPolicy = "pii") -> Any:
    """Redact command strings and argv-style command sequences."""
    if isinstance(value, str):
        return redact_text(value, policy=policy)
    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
        return redact_value(value, policy=policy)

    redacted: list[Any] = []
    redact_next = False
    for item in value:
        if redact_next:
            redacted.append(
                REDACTED_SECRET if isinstance(item, str) else redact_value(item, policy=policy)
            )
            redact_next = False
            continue
        if isinstance(item, str):
            redacted_item = redact_text(item, policy=policy)
            flag_name = item.split("=", 1)[0].lower()
            if flag_name in _SENSITIVE_COMMAND_FLAGS and "=" not in item:
                redact_next = True
            redacted.append(redacted_item)
            continue
        redacted.append(redact_value(item, policy=policy))
    return redacted


def should_redact_key(key: str | None) -> bool:
    normalized_key = _normalize_key_name(key)
    return normalized_key in UNSAFE_TEXT_FIELDS or should_redact_secret_key(normalized_key)


def should_redact_secret_key(key: str | None) -> bool:
    """Return whether *key* names credential material rather than PII content."""
    normalized_key = _normalize_key_name(key)
    return normalized_key in _BARE_SECRET_VALUE_KEYS or _is_secret_value_key(normalized_key)


def _normalize_key_name(key: str | None) -> str | None:
    """Normalize case boundaries before credential-key classification."""
    if key is None:
        return None
    value = str(key)
    value = _CAMEL_ACRONYM_BOUNDARY_RE.sub("_", value)
    return _CAMEL_WORD_BOUNDARY_RE.sub("_", value).lower()


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
    "REDACTED_SECRET",
    "REDACTION_VERSION",
    "UNSAFE_TEXT_FIELDS",
    "ArtifactRedactionError",
    "RedactionPolicy",
    "contains_unredacted_sensitive_text",
    "redact_command",
    "redact_runtime_secrets",
    "redact_runtime_secrets_in_file",
    "redact_text",
    "redact_value",
    "should_redact_key",
    "validate_redaction_policy",
]
