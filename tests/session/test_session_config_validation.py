"""Construction-time validation for low-level Session policy fields."""

from __future__ import annotations

import pytest

from easycat.errors import EasyCatError, EasyConfigError
from easycat.session._types import CallIdentity, SessionConfig


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("journal_detail", "verbose"),
        ("interruption_mode", "append"),
        ("runtime_mode", "realtime"),
        ("caller_id_exposure", "offf"),
        ("journal_redaction", "everything"),
    ],
)
def test_session_config_rejects_invalid_runtime_policy(
    field_name: str,
    invalid_value: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        SessionConfig(**{field_name: invalid_value})  # type: ignore[arg-type]


def test_call_identity_rejects_invalid_direction() -> None:
    with pytest.raises(ValueError, match="direction"):
        CallIdentity(direction="sideways")  # type: ignore[arg-type]


def test_session_config_rejects_missing_collaborators_before_session_wiring() -> None:
    with pytest.raises(EasyConfigError, match="requires: agent, stt, tts, transport, vad") as exc:
        SessionConfig()

    assert isinstance(exc.value, EasyCatError)
    assert isinstance(exc.value, ValueError)
    assert exc.value.code == "EASYCAT_E105"
    assert "EasyConfig + create_session/run" in str(exc.value)


def test_text_session_config_allows_noop_agent_for_recording_only_uses() -> None:
    config = SessionConfig(runtime_mode="text_session")

    assert config.runtime_mode == "text_session"
