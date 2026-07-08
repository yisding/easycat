"""Typed input payload for TTS synthesis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

TTSInputFormat = Literal["plain", "ssml"]
TTSStreamingBoundary = Literal["sentence", "provider_native", "unknown"]
TTSSSMLFallback = Literal["strip", "reject"]
TTSPauseSupport = Literal["none", "ssml_break", "provider_native"]
TTSPronunciationSupport = Literal["none", "ssml_phoneme", "provider_native"]
TTSMarkerSupport = Literal["none", "provider_native"]


@dataclass(frozen=True)
class TTSInput:
    """Input payload for TTS providers.

    Attributes:
        text: Text (or SSML markup) to synthesize.
        format: Input format indicator. ``plain`` is raw text;
            ``ssml`` indicates XML SSML markup.
    """

    text: str
    format: TTSInputFormat = "plain"


@dataclass(frozen=True)
class TTSInputPolicy:
    """Provider-facing TTS input contract.

    ``accepted_formats`` describes what the provider can receive natively.
    EasyCat currently falls back by stripping unsupported SSML before calling
    provider ``synthesize`` methods, which is represented by
    ``unsupported_ssml="strip"``.
    """

    accepted_formats: Sequence[TTSInputFormat] = ("plain",)
    unsupported_ssml: TTSSSMLFallback = "strip"
    streaming_boundary: TTSStreamingBoundary = "sentence"
    pause_support: TTSPauseSupport = "none"
    pronunciation_support: TTSPronunciationSupport = "none"
    marker_support: TTSMarkerSupport = "none"
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        accepted_formats = tuple(dict.fromkeys(self.accepted_formats))
        invalid_formats = sorted(set(accepted_formats) - {"plain", "ssml"})
        if invalid_formats:
            raise ValueError(f"unsupported TTS input formats: {invalid_formats!r}")
        if "plain" not in accepted_formats:
            raise ValueError("TTS input policy must accept plain text")

        object.__setattr__(self, "accepted_formats", accepted_formats)
        object.__setattr__(self, "provider_options", MappingProxyType(dict(self.provider_options)))

    @classmethod
    def plain_text(
        cls,
        *,
        streaming_boundary: TTSStreamingBoundary = "sentence",
        provider_options: Mapping[str, Any] | None = None,
    ) -> TTSInputPolicy:
        """Return the default policy for providers that accept plain text only."""
        return cls(
            accepted_formats=("plain",),
            streaming_boundary=streaming_boundary,
            provider_options=provider_options or {},
        )

    @classmethod
    def native_ssml(
        cls,
        *,
        streaming_boundary: TTSStreamingBoundary = "sentence",
        pause_support: TTSPauseSupport = "ssml_break",
        pronunciation_support: TTSPronunciationSupport = "ssml_phoneme",
        marker_support: TTSMarkerSupport = "none",
        provider_options: Mapping[str, Any] | None = None,
    ) -> TTSInputPolicy:
        """Return a policy for providers that accept SSML natively."""
        return cls(
            accepted_formats=("plain", "ssml"),
            streaming_boundary=streaming_boundary,
            pause_support=pause_support,
            pronunciation_support=pronunciation_support,
            marker_support=marker_support,
            provider_options=provider_options or {},
        )

    @property
    def supports_ssml(self) -> bool:
        """Whether the provider accepts SSML without EasyCat stripping it."""
        return "ssml" in self.accepted_formats

    def accepts(self, format: TTSInputFormat) -> bool:
        """Return whether ``format`` can be sent to the provider unchanged."""
        return format in self.accepted_formats

    def to_dict(self) -> dict[str, Any]:
        """Serialize the policy using report-friendly primitive values."""
        payload: dict[str, Any] = {
            "accepted_formats": list(self.accepted_formats),
            "supports_ssml": self.supports_ssml,
            "unsupported_ssml": self.unsupported_ssml,
            "streaming_boundary": self.streaming_boundary,
            "pause_support": self.pause_support,
            "pronunciation_support": self.pronunciation_support,
            "marker_support": self.marker_support,
        }
        if self.provider_options:
            payload["provider_options"] = dict(self.provider_options)
        return payload


def strip_ssml_tags(text: str) -> str:
    """Best-effort conversion from SSML markup to plain text."""
    import html
    import re

    without_tags = re.sub(r"<[^>]+>", " ", text)
    collapsed = re.sub(r"\s+", " ", without_tags).strip()
    return html.unescape(collapsed)


def coerce_tts_input(payload: TTSInput | str) -> TTSInput:
    """Accept legacy string input and normalize to ``TTSInput``."""
    if isinstance(payload, TTSInput):
        return payload
    return TTSInput(text=payload, format="plain")


def resolve_tts_input_policy(provider: object) -> TTSInputPolicy:
    """Return a provider's typed input policy, with legacy SSML fallback.

    Providers that expose ``input_policy`` must return :class:`TTSInputPolicy`.
    Older providers can keep exposing only ``supports_ssml`` and receive an
    equivalent policy.
    """
    try:
        policy = provider.input_policy
    except AttributeError:
        policy = None
    if policy is not None:
        if not isinstance(policy, TTSInputPolicy):
            raise TypeError(
                "provider input_policy must be an easycat.tts.input.TTSInputPolicy instance"
            )
        return policy

    supports_ssml = bool(getattr(provider, "supports_ssml", False))
    if supports_ssml:
        return TTSInputPolicy.native_ssml()
    return TTSInputPolicy.plain_text()
