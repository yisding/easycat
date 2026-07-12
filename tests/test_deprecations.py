"""Guard that retained compatibility aliases emit deprecation signals.

The provider config ``settings=`` alias raises ``DeprecationWarning`` when
folded into ``params``. Session's obsolete lifecycle aliases were removed
during the pre-release API cleanup rather than retained here.
"""

from __future__ import annotations

import pytest

from easycat.stt.factory import STTProviderConfig
from easycat.tts.factory import TTSProviderConfig


def test_stt_provider_config_settings_alias_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning):
        STTProviderConfig(provider="openai", settings={"model": "whisper-1"})


def test_tts_provider_config_settings_alias_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning):
        TTSProviderConfig(provider="openai", settings={"voice": "alloy"})
