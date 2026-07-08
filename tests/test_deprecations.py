"""Guard that the compatibility aliases emit machine-visible deprecation signals.

QW8: ``Session.shutdown``/``close``/``destroy`` carry PEP 702 ``@deprecated`` and
the provider config ``settings=`` alias raises ``DeprecationWarning`` when folded
into ``params``. These are behavior-preserving warnings only.
"""

from __future__ import annotations

import pytest

from easycat.session._session import Session
from easycat.stt.factory import STTProviderConfig
from easycat.tts.factory import TTSProviderConfig
from tests.session._session_core_helpers import FakeTransport, _full_config


@pytest.mark.asyncio
async def test_session_shutdown_is_deprecated() -> None:
    session = Session(_full_config(transport=FakeTransport()))
    await session.start()
    with pytest.warns(DeprecationWarning):
        await session.shutdown()


def test_session_close_is_deprecated() -> None:
    session = Session(_full_config(session_id="sess"))
    with pytest.warns(DeprecationWarning):
        session.close()


def test_session_destroy_is_deprecated() -> None:
    session = Session(_full_config(session_id="sess"))
    with pytest.warns(DeprecationWarning):
        session.destroy()


def test_stt_provider_config_settings_alias_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning):
        STTProviderConfig(provider="openai", settings={"model": "whisper-1"})


def test_tts_provider_config_settings_alias_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning):
        TTSProviderConfig(provider="openai", settings={"voice": "alloy"})
