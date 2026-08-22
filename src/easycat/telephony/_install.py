"""Shared telephony optional-extra install guidance."""

TELEPHONY_INSTALL_HINT = (
    "Install with: uv add 'easycat[telephony]'. From the EasyCat repo, use: "
    "uv sync --extra telephony --group dev."
)

TELNYX_INSTALL_HINT = (
    "The 'cryptography' package is required for Telnyx webhook signature "
    "verification. Install with: uv add 'easycat[telnyx]'. From the EasyCat "
    "repo, use: uv sync --extra telnyx --group dev."
)
