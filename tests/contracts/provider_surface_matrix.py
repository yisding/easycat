from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from easycat.config import _TRANSPORT_FACTORIES
from easycat.stt.factory import _PROVIDER_TO_CONFIG as _STT_REGISTRY
from easycat.tts.factory import _PROVIDERS as _TTS_REGISTRY
from easycat.vad._base import _VALID_VAD_BACKENDS, VADBackend

Surface = Literal["stt", "tts", "vad", "transport", "agent_bridge"]
CassetteStatus = Literal["required", "deferred", "not_applicable"]
LiveCanaryStatus = Literal["required", "deferred", "not_applicable"]


@dataclass(frozen=True)
class ProviderSurfaceContract:
    provider: str
    surface: Surface
    adapter: str
    protocol: str
    mode: str
    model_api_version: str
    required_extra: str
    credential_env_var: str
    contract_path: str
    cassette_path: str
    cassette_status: CassetteStatus
    live_canary_status: LiveCanaryStatus
    expected_skip_reason: str = (
        "offline contract run defers live/cassette and optional-extra checks"
    )

    @property
    def key(self) -> tuple[str, Surface, str, str]:
        return (self.provider, self.surface, self.protocol, self.mode)


PROVIDER_SURFACE_CONTRACTS: tuple[ProviderSurfaceContract, ...] = (
    ProviderSurfaceContract(
        provider="openai",
        surface="stt",
        adapter="easycat.stt.openai_provider.OpenAISTT",
        protocol="http",
        mode="batch",
        model_api_version="whisper-1",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        contract_path="tests/contracts/test_stt_provider_contracts.py",
        cassette_path="tests/cassettes/http/openai-stt.json",
        cassette_status="required",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="openai-realtime",
        surface="stt",
        adapter="easycat.stt.openai_realtime_provider.OpenAIRealtimeSTT",
        protocol="websocket",
        mode="realtime",
        model_api_version="gpt-4o-transcribe",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        contract_path="tests/contracts/test_stt_provider_contracts.py",
        cassette_path="tests/cassettes/ws/openai-realtime-stt.json",
        cassette_status="required",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="deepgram",
        surface="stt",
        adapter="easycat.stt.deepgram_provider.DeepgramSTT",
        protocol="websocket",
        mode="realtime",
        model_api_version="nova-3",
        required_extra="deepgram",
        credential_env_var="DEEPGRAM_API_KEY",
        contract_path="tests/contracts/test_stt_provider_contracts.py",
        cassette_path="tests/cassettes/ws/deepgram-stt.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="elevenlabs",
        surface="stt",
        adapter="easycat.stt.elevenlabs_provider.ElevenLabsSTT",
        protocol="http/websocket",
        mode="batch+realtime",
        model_api_version="scribe_v1",
        required_extra="elevenlabs",
        credential_env_var="ELEVENLABS_API_KEY",
        contract_path="tests/contracts/test_stt_provider_contracts.py",
        cassette_path="tests/cassettes/http/elevenlabs-stt.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="cartesia",
        surface="stt",
        adapter="easycat.stt.cartesia_provider.CartesiaSTT",
        protocol="websocket",
        mode="realtime",
        model_api_version="ink-whisper",
        required_extra="cartesia",
        credential_env_var="CARTESIA_API_KEY",
        contract_path="tests/contracts/test_stt_provider_contracts.py",
        cassette_path="tests/cassettes/ws/cartesia-stt.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="openai",
        surface="tts",
        adapter="easycat.tts.openai_tts.OpenAITTS",
        protocol="http",
        mode="streaming",
        model_api_version="gpt-4o-mini-tts",
        required_extra="openai",
        credential_env_var="OPENAI_API_KEY",
        contract_path="tests/contracts/test_tts_provider_contracts.py",
        cassette_path="tests/cassettes/http/openai-tts.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="deepgram",
        surface="tts",
        adapter="easycat.tts.deepgram_tts.DeepgramTTS",
        protocol="websocket",
        mode="streaming",
        model_api_version="aura-2",
        required_extra="deepgram",
        credential_env_var="DEEPGRAM_API_KEY",
        contract_path="tests/contracts/test_tts_provider_contracts.py",
        cassette_path="tests/cassettes/ws/deepgram-tts.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="elevenlabs",
        surface="tts",
        adapter="easycat.tts.elevenlabs_tts.ElevenLabsTTS",
        protocol="http/websocket",
        mode="streaming",
        model_api_version="eleven_v3",
        required_extra="elevenlabs",
        credential_env_var="ELEVENLABS_API_KEY",
        contract_path="tests/contracts/test_tts_provider_contracts.py",
        cassette_path="tests/cassettes/http/elevenlabs-tts.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="cartesia",
        surface="tts",
        adapter="easycat.tts.cartesia_tts.CartesiaTTS",
        protocol="websocket",
        mode="streaming",
        model_api_version="sonic-2",
        required_extra="cartesia",
        credential_env_var="CARTESIA_API_KEY",
        contract_path="tests/contracts/test_tts_provider_contracts.py",
        cassette_path="tests/cassettes/ws/cartesia-tts.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="openai-agents",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.openai_agents.OpenAIAgentsBridge",
        protocol="python-sdk",
        mode="streaming",
        model_api_version="openai-agents",
        required_extra="openai-agents",
        credential_env_var="OPENAI_API_KEY",
        contract_path="tests/contracts/test_agent_bridge_contracts.py",
        cassette_path="tests/cassettes/sse/openai-agents.json",
        cassette_status="deferred",
        live_canary_status="required",
    ),
    ProviderSurfaceContract(
        provider="pydantic-ai",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.pydantic_ai.PydanticAIBridge",
        protocol="python-sdk",
        mode="agent+graph",
        model_api_version="pydantic-ai",
        required_extra="pydantic-ai",
        credential_env_var="",
        contract_path="tests/contracts/test_agent_bridge_contracts.py",
        cassette_path="tests/cassettes/sse/pydantic-ai.json",
        cassette_status="deferred",
        live_canary_status="deferred",
    ),
    ProviderSurfaceContract(
        provider="generic-workflow",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.generic_workflow.GenericWorkflowBridge",
        protocol="python-callback",
        mode="deep+shallow",
        model_api_version="easycat-generic-workflow-v1",
        required_extra="",
        credential_env_var="",
        contract_path="tests/contracts/test_agent_bridge_contracts.py",
        cassette_path="tests/cassettes/sse/generic-workflow.json",
        cassette_status="deferred",
        live_canary_status="not_applicable",
    ),
    ProviderSurfaceContract(
        provider="remote-responses-api",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.responses_api.RemoteResponsesAPIBridge",
        protocol="http/sse",
        mode="streaming",
        model_api_version="responses-api",
        required_extra="",
        credential_env_var="",
        contract_path="tests/contracts/test_agent_bridge_contracts.py",
        cassette_path="tests/cassettes/sse/remote-responses-api.json",
        cassette_status="required",
        live_canary_status="deferred",
    ),
    ProviderSurfaceContract(
        provider="langchain",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.langchain.LangChainBridge",
        protocol="python-runnable",
        mode="streaming",
        model_api_version="langchain-core",
        required_extra="langchain",
        credential_env_var="",
        contract_path="tests/contracts/test_agent_bridge_contracts.py",
        cassette_path="tests/cassettes/sse/langchain.json",
        cassette_status="deferred",
        live_canary_status="deferred",
    ),
    ProviderSurfaceContract(
        provider="langgraph",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.langgraph.LangGraphBridge",
        protocol="python-graph",
        mode="streaming",
        model_api_version="langgraph",
        required_extra="langgraph",
        credential_env_var="",
        contract_path="tests/contracts/test_agent_bridge_contracts.py",
        cassette_path="tests/cassettes/sse/langgraph.json",
        cassette_status="deferred",
        live_canary_status="deferred",
    ),
    ProviderSurfaceContract(
        provider="llama-agents",
        surface="agent_bridge",
        adapter="easycat.integrations.agents.llama_agents.LlamaAgentsBridge",
        protocol="python-workflow",
        mode="streaming",
        model_api_version="llama-index-workflows",
        required_extra="llama-agents",
        credential_env_var="",
        contract_path="tests/contracts/test_agent_bridge_contracts.py",
        cassette_path="tests/cassettes/sse/llama-agents.json",
        cassette_status="deferred",
        live_canary_status="deferred",
    ),
    ProviderSurfaceContract(
        provider="silero",
        surface="vad",
        adapter="easycat.vad.silero.SileroVAD",
        protocol="python-onnx",
        mode="streaming",
        model_api_version="silero-vad-onnx",
        required_extra="silero-vad",
        credential_env_var="",
        contract_path="tests/contracts/test_vad_provider_contracts.py",
        cassette_path="tests/cassettes/vad/silero.json",
        cassette_status="deferred",
        live_canary_status="not_applicable",
    ),
    ProviderSurfaceContract(
        provider="funasr",
        surface="vad",
        adapter="easycat.vad.funasr.FunASROnnxVAD",
        protocol="python-onnx",
        mode="streaming",
        model_api_version="funasr-onnx",
        required_extra="funasr-vad",
        credential_env_var="",
        contract_path="tests/contracts/test_vad_provider_contracts.py",
        cassette_path="tests/cassettes/vad/funasr.json",
        cassette_status="deferred",
        live_canary_status="not_applicable",
    ),
    ProviderSurfaceContract(
        provider="ten",
        surface="vad",
        adapter="easycat.vad.ten.TenVAD",
        protocol="python-native",
        mode="streaming",
        model_api_version="ten-vad",
        required_extra="ten-vad",
        credential_env_var="",
        contract_path="tests/contracts/test_vad_provider_contracts.py",
        cassette_path="tests/cassettes/vad/ten.json",
        cassette_status="deferred",
        live_canary_status="not_applicable",
    ),
    ProviderSurfaceContract(
        provider="krisp",
        surface="vad",
        adapter="easycat.vad.krisp.KrispVAD",
        protocol="python-sdk",
        mode="streaming",
        model_api_version="krisp-audio",
        required_extra="krisp",
        credential_env_var="",
        contract_path="tests/contracts/test_vad_provider_contracts.py",
        cassette_path="tests/cassettes/vad/krisp.json",
        cassette_status="deferred",
        live_canary_status="not_applicable",
    ),
    ProviderSurfaceContract(
        provider="local",
        surface="transport",
        adapter="easycat.transports.local.LocalTransport",
        protocol="local-audio-device",
        mode="duplex",
        model_api_version="easycat-transport-v1",
        required_extra="local",
        credential_env_var="",
        contract_path="tests/contracts/test_transport_contracts.py",
        cassette_path="tests/cassettes/transport/local.json",
        cassette_status="deferred",
        live_canary_status="not_applicable",
    ),
    ProviderSurfaceContract(
        provider="websocket",
        surface="transport",
        adapter="easycat.transports.websocket.WebSocketTransport",
        protocol="websocket",
        mode="duplex",
        model_api_version="easycat-websocket-v1",
        required_extra="",
        credential_env_var="",
        contract_path="tests/contracts/test_transport_contracts.py",
        cassette_path="tests/cassettes/transport/websocket.json",
        cassette_status="deferred",
        live_canary_status="not_applicable",
    ),
    ProviderSurfaceContract(
        provider="twilio",
        surface="transport",
        adapter="easycat.transports.twilio_media.TwilioTransport",
        protocol="twilio-media-streams",
        mode="duplex",
        model_api_version="twilio-media-streams-v1",
        required_extra="telephony",
        credential_env_var="TWILIO_STREAM_URL",
        contract_path="tests/contracts/test_transport_contracts.py",
        cassette_path="tests/cassettes/transport/twilio.json",
        cassette_status="deferred",
        live_canary_status="deferred",
    ),
    ProviderSurfaceContract(
        provider="webrtc",
        surface="transport",
        adapter="easycat.transports.webrtc.WebRTCTransport",
        protocol="webrtc",
        mode="duplex",
        model_api_version="easycat-webrtc-v1",
        required_extra="webrtc",
        credential_env_var="",
        contract_path="tests/contracts/test_transport_contracts.py",
        cassette_path="tests/cassettes/transport/webrtc.json",
        cassette_status="deferred",
        live_canary_status="deferred",
    ),
    ProviderSurfaceContract(
        provider="webtransport",
        surface="transport",
        adapter="easycat.transports.webtransport.WebTransportTransport",
        protocol="webtransport",
        mode="duplex",
        model_api_version="easycat-webtransport-v1",
        required_extra="webtransport",
        credential_env_var="",
        contract_path="tests/contracts/test_transport_contracts.py",
        cassette_path="tests/cassettes/transport/webtransport.json",
        cassette_status="deferred",
        live_canary_status="deferred",
    ),
)

EXPLICIT_PROVIDER_SURFACE_EXCLUSIONS: dict[tuple[str, Surface], str] = {}


def missing_registered_provider_surfaces() -> list[tuple[str, Surface]]:
    registered = {
        *((provider, "stt") for provider in _STT_REGISTRY),
        *((provider, "tts") for provider in _TTS_REGISTRY),
        *((provider, "vad") for provider in _registered_vad_backends()),
        *((provider, "transport") for provider in _registered_transport_names()),
    }
    covered = {(row.provider, row.surface) for row in PROVIDER_SURFACE_CONTRACTS}
    excluded = set(EXPLICIT_PROVIDER_SURFACE_EXCLUSIONS)
    return sorted(registered - covered - excluded)


def _registered_vad_backends() -> tuple[VADBackend, ...]:
    return tuple(backend for backend in _VALID_VAD_BACKENDS if backend != "auto")


def _registered_transport_names() -> tuple[str, ...]:
    names = []
    for config_type in _TRANSPORT_FACTORIES:
        raw = config_type.__name__.removesuffix("TransportConfig").lower()
        names.append(raw.removesuffix("connection"))
    return tuple(sorted(names))
