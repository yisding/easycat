"""EasyCat — a voice bot in three lines.

Start here (requires ``uv add 'easycat[quickstart]'``)::

    from agents import Agent
    from easycat import EasyConfig, run
    run(EasyConfig.mic(agent=Agent(name="assistant", instructions="Be helpful.")))

Before the first run, set ``OPENAI_API_KEY`` and verify the environment with
``uv run easycat doctor``. If keys live in ``.env``, use
``uv run easycat doctor --env-file .env`` and
``uv run --env-file .env ...``.

``EasyConfig`` + ``run`` is the synchronous entry path; use ``await arun(...)``
when your application already owns an event loop. Drop to
``Session.from_providers(...)`` only when you need to hand-build provider instances.

The top-level package intentionally exposes the app-facing surface only;
providers, stage internals, and telephony/debug helpers stay importable
from their own modules. Exports load lazily via PEP 562 so cold starts stay
cheap.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

from easycat._public_api import LAZY_EXPORTS

# Library hygiene: stay silent unless the embedding application configures logging.
# https://docs.python.org/3/howto/logging.html#configuring-logging-for-a-library
logging.getLogger("easycat").addHandler(logging.NullHandler())

_LAZY_ATTR = LAZY_EXPORTS
_VERSION_ATTR = "__version__"


if TYPE_CHECKING:
    __version__: str

    from easycat._logging import set_easycat_log_level
    from easycat.audio_format import (
        PCM16_MONO_8K,
        PCM16_MONO_16K,
        PCM16_MONO_24K,
        PCM16_MONO_48K,
        AudioChunk,
        AudioFormat,
    )
    from easycat.cancel import CancelToken
    from easycat.config import (
        EasyConfig,
        OutboundCallConfig,
        TelephonyConfig,
        VoicemailDetectionConfig,
        create_session,
        create_text_session,
    )
    from easycat.debug.bundle import RunBundle
    from easycat.debug.export import export_debug_bundle
    from easycat.errors import EasyCatError, EasyConfigError, ErrorEntry
    from easycat.events import (
        DTMF,
        AgentDelta,
        AgentFinal,
        AgentRequestStarted,
        AudioIn,
        AudioOut,
        BotStartedSpeaking,
        BotStoppedSpeaking,
        CallAnswered,
        CallEnded,
        CallFailed,
        CallInitiated,
        CallRinging,
        CallScreening,
        CallStateChanged,
        DTMFAggregated,
        Error,
        ErrorStage,
        Event,
        EventBus,
        Interruption,
        IVRAction,
        PlaybackMarkAck,
        ReconnectAttempt,
        ReconnectFailure,
        ReconnectSuccess,
        ScreeningResponse,
        ScreeningTimedOut,
        SessionActionCompleted,
        SessionActionFailed,
        SessionActionRequested,
        SessionActionStarted,
        STTFinal,
        STTPartial,
        SupervisorListenerAttached,
        SupervisorListenerDetached,
        ToolCallDelta,
        ToolCallResult,
        ToolCallStarted,
        TransportAudioDelivered,
        TransportDegraded,
        TTSAudio,
        TTSMarkers,
        TurnEnded,
        TurnStarted,
        VADStartSpeaking,
        VADStopSpeaking,
        VoicemailDetected,
    )
    from easycat.helpers import (
        arun,
        attach_runtime_feedback,
        require_env,
        run,
        wait_for_shutdown_signal,
    )
    from easycat.integrations.agents import auto_adapt_agent
    from easycat.llm_output_processing import (
        MarkdownStripProcessor,
        PauseProcessor,
        PhoneticReplacementProcessor,
        default_pronunciation_processors,
    )
    from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
    from easycat.providers import (
        EchoCanceller,
        EventBusBindable,
        NoiseReducer,
        STTProvider,
        Transport,
        TTSProvider,
        VADProvider,
    )
    from easycat.runtime import JournalRecordKind
    from easycat.server.webrtc_routes import (
        run_webrtc_config_server,
        serve_webrtc_config_sessions,
    )
    from easycat.session._session import Session
    from easycat.session._types import CallIdentity, SessionConfig
    from easycat.session.actions import SessionActions
    from easycat.session_manager import SessionManager
    from easycat.smart_turn import SmartTurnConfig
    from easycat.stt.factory import (
        STTProviderConfig,
        available_stt_providers,
        create_stt_provider,
        register_stt_provider,
    )
    from easycat.supervisor import SessionAudioBroadcaster
    from easycat.telephony.session_actions import TwilioSessionActionConfig
    from easycat.transports._webrtc_config import (
        ICEServer,
        WebRTCTransportConfig,
    )
    from easycat.transports.local import LocalTransportConfig
    from easycat.transports.telnyx_media import TelnyxConnectionTransport
    from easycat.transports.twilio_media import TwilioConnectionTransport
    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransportConfig,
    )
    from easycat.transports.webtransport import (
        WebTransportConnectionTransport,
        WebTransportServer,
        WebTransportTransportConfig,
    )
    from easycat.tts.factory import (
        TTSProviderConfig,
        available_tts_providers,
        create_tts_provider,
        register_tts_provider,
    )
    from easycat.turn_manager import TurnManagerConfig, TurnMode
    from easycat.vad import (
        VADConfig,
        available_vad_providers,
        create_vad,
        register_vad_provider,
    )
    from easycat.voice_app import VoiceApp


def __getattr__(name: str):  # PEP 562
    """Lazy re-export dispatcher. Runs once per attribute per session."""
    if name == _VERSION_ATTR:
        from importlib.metadata import PackageNotFoundError, version

        try:
            value = version("easycat")
        except PackageNotFoundError:
            # A source checkout imported without installation has no package
            # metadata. Published wheels and normal editable installs always do.
            value = "0+unknown"
        globals()[name] = value
        return value

    try:
        module_path, attr = _LAZY_ATTR[name]
    except KeyError:
        raise AttributeError(f"module 'easycat' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY_ATTR) + [_VERSION_ATTR]))


__all__ = sorted(_LAZY_ATTR)  # noqa: PLE0605 exports are generated from the public registry
