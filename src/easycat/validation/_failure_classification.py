"""Shared provider failure classification."""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "FailureCategory",
    "classify_failure_category",
    "classify_latency_failure",
]


class FailureCategory(StrEnum):
    """Canonical failure buckets shared by latency and live validation."""

    AUTH = "auth"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    NETWORK = "network"
    DRIFT = "drift"
    REGRESSION = "regression"
    OTHER = "other"


# Ordered by actionable precedence. A rate limit wins over a co-occurring auth
# token, and schema drift wins over incidental connection vocabulary.
_FAILURE_CATEGORY_TOKENS: tuple[tuple[FailureCategory, tuple[str, ...]], ...] = (
    (
        FailureCategory.QUOTA,
        ("rate limit", "ratelimit", "429", "quota", "too many requests"),
    ),
    (
        FailureCategory.AUTH,
        (
            "api key",
            "auth",
            "unauthorized",
            "forbidden",
            "permission denied",
            "401",
            "403",
        ),
    ),
    (FailureCategory.TIMEOUT, ("timeout", "timed out", "deadline")),
    (FailureCategory.DRIFT, ("schema", "unknown event", "drift")),
    (FailureCategory.NETWORK, ("dns", "network", "connection")),
    (FailureCategory.REGRESSION, ("assert", "failed", "traceback")),
)


def classify_failure_category(message: str) -> FailureCategory:
    """Map an error message to its canonical category."""
    normalized = message.lower().replace("_", " ").replace("-", " ")
    for category, tokens in _FAILURE_CATEGORY_TOKENS:
        if any(token in normalized for token in tokens):
            return category
    return FailureCategory.OTHER


_LATENCY_FAILURE_CLASSES: dict[FailureCategory, str] = {
    FailureCategory.AUTH: "provider_auth",
    FailureCategory.QUOTA: "provider_rate_limit",
    FailureCategory.TIMEOUT: "provider_timeout",
    FailureCategory.NETWORK: "provider_timeout",
    FailureCategory.DRIFT: "easycat_latency_regression",
    FailureCategory.REGRESSION: "easycat_latency_regression",
    FailureCategory.OTHER: "easycat_latency_regression",
}


def classify_latency_failure(message: str) -> str:
    """Map a message to the latency artifact's failure vocabulary."""
    return _LATENCY_FAILURE_CLASSES[classify_failure_category(message)]
