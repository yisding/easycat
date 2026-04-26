from __future__ import annotations

import easycat

PUBLIC_API_SNAPSHOT = (
    "AgentRunner",
    "AgentRunnerConfig",
    "AudioChunk",
    "AudioFormat",
    "AudioIn",
    "AudioOut",
    "BotStartedSpeaking",
    "BotStoppedSpeaking",
    "CallAnswered",
    "CallEnded",
    "CallFailed",
    "CancelToken",
    "EasyCatConfig",
    "EasyCatError",
    "EchoCanceller",
    "Error",
    "ErrorEntry",
    "ErrorStage",
    "Event",
    "EventBus",
    "ICEServer",
    "Interruption",
    "JournalRecordKind",
    "LocalTransportConfig",
    "MarkdownStripProcessor",
    "NoiseReducer",
    "OutboundCallConfig",
    "PCM16_MONO_16K",
    "PCM16_MONO_24K",
    "PCM16_MONO_48K",
    "PCM16_MONO_8K",
    "PauseProcessor",
    "PhoneticReplacementProcessor",
    "RunBundle",
    "STTFinal",
    "STTPartial",
    "STTProvider",
    "Session",
    "SessionActions",
    "SessionAudioBroadcaster",
    "SessionConfig",
    "SessionManager",
    "SmartTurnConfig",
    "TTSAudio",
    "TTSMarkers",
    "TTSProvider",
    "TelephonyConfig",
    "Transport",
    "TurnEnded",
    "TurnManagerConfig",
    "TurnMode",
    "TurnStarted",
    "TwilioConnectionTransport",
    "TwilioSessionActionConfig",
    "VADProvider",
    "VADStartSpeaking",
    "VADStopSpeaking",
    "VoicemailDetectionConfig",
    "WebRTCTransportConfig",
    "WebSocketConnectionTransport",
    "WebSocketTransportConfig",
    "attach_runtime_feedback",
    "create_session",
    "create_text_session",
    "default_pronunciation_processors",
    "export_debug_bundle",
    "require_env",
    "run",
    "wait_for_shutdown_signal",
)


def test_public_api_snapshot() -> None:
    assert tuple(easycat.__all__) == PUBLIC_API_SNAPSHOT
    assert len(easycat.__all__) <= 70


def test_curated_public_api_lazy_imports() -> None:
    from easycat import EasyCatConfig, MarkdownStripProcessor, create_session

    assert EasyCatConfig.__name__ == "EasyCatConfig"
    assert MarkdownStripProcessor.__name__ == "MarkdownStripProcessor"
    assert create_session.__name__ == "create_session"


def test_public_api_symbols_resolve() -> None:
    for name in easycat.__all__:
        assert getattr(easycat, name) is not None


def test_culled_symbols_remain_available_from_modules() -> None:
    from easycat.debug.testing import load_bundle
    from easycat.quick import speak, transcribe_file
    from easycat.session import split_at_sentence_boundaries
    from easycat.session.actions import CoreSessionActionExecutor
    from easycat.stt.factory import STTProviderConfig, create_stt_provider

    assert "CoreSessionActionExecutor" not in easycat.__all__
    assert "STTProviderConfig" not in easycat.__all__
    assert "load_bundle" not in easycat.__all__
    assert "speak" not in easycat.__all__
    assert "transcribe_file" not in easycat.__all__

    assert CoreSessionActionExecutor.__name__ == "CoreSessionActionExecutor"
    assert STTProviderConfig.__name__ == "STTProviderConfig"
    assert create_stt_provider.__name__ == "create_stt_provider"
    assert load_bundle.__name__ == "load_bundle"
    assert speak.__name__ == "speak"
    assert transcribe_file.__name__ == "transcribe_file"
    assert split_at_sentence_boundaries("Hello world. ") == ("Hello world. ", "")
