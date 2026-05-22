from __future__ import annotations

from datetime import date

VALIDATION_SCOPE_MARKERS = frozenset({"contract", "integration_live", "latency"})

PROVIDER_MARKERS = frozenset(
    {
        "provider_cartesia",
        "provider_deepgram",
        "provider_elevenlabs",
        "provider_openai",
    }
)

SURFACE_MARKERS = frozenset(
    {
        "surface_agent",
        "surface_stt",
        "surface_transport",
        "surface_tts",
        "surface_vad",
    }
)

# Accepted quarantine metadata:
# @pytest.mark.flaky(issue="...", owner="...", review_by="YYYY-MM-DD")
REQUIRED_FLAKY_METADATA = ("issue", "owner", "review_by")


def validate_provider_surface_markers(nodeid: str, marker_names: set[str]) -> list[str]:
    """Require provider/surface pairs once validation tests declare either side."""
    if not marker_names.intersection(VALIDATION_SCOPE_MARKERS):
        return []

    errors: list[str] = []
    has_provider = bool(marker_names.intersection(PROVIDER_MARKERS)) or "provider" in marker_names
    has_surface = bool(marker_names.intersection(SURFACE_MARKERS))

    if has_provider and not has_surface:
        surface_names = ", ".join(sorted(SURFACE_MARKERS))
        errors.append(
            f"{nodeid} is provider-scoped but missing surface metadata; add one of: "
            f"{surface_names}"
        )

    if has_surface and not has_provider:
        provider_names = ", ".join(sorted(PROVIDER_MARKERS))
        errors.append(
            f"{nodeid} is surface-scoped but missing provider metadata; add "
            f"provider(NAME) or one of: {provider_names}"
        )

    return errors


def validate_flaky_marker(
    nodeid: str,
    marker_names: set[str],
    marker_kwargs: dict[str, object],
    *,
    today: date | None = None,
) -> list[str]:
    if "flaky" not in marker_names:
        return []

    today = today or date.today()
    errors: list[str] = []
    missing = [
        name for name in REQUIRED_FLAKY_METADATA if not str(marker_kwargs.get(name, "")).strip()
    ]
    if missing:
        errors.append(f"{nodeid} has @pytest.mark.flaky missing metadata: {', '.join(missing)}")
        return errors

    review_by_value = str(marker_kwargs["review_by"])
    try:
        review_by = date.fromisoformat(review_by_value)
    except ValueError:
        errors.append(
            f"{nodeid} has invalid flaky review_by date {review_by_value!r}; expected YYYY-MM-DD"
        )
        return errors

    if review_by < today:
        errors.append(f"{nodeid} has stale flaky review_by date {review_by_value}")

    if "release" in marker_names:
        errors.append(f"{nodeid} is release-scoped but still quarantined with @pytest.mark.flaky")

    return errors
