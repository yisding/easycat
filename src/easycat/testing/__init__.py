"""Contract-test kit for EasyCat provider and bridge authors.

EasyCat ships its testing surface in core (like ``easycat.debug.testing``)
so a provider author can validate a new STT/TTS/VAD/Transport provider or
agent bridge against the exact protocol semantics EasyCat's own contract
tests enforce — subclass a suite, point ``provider_factory`` at your
implementation, and run pytest::

    from easycat.testing import STTProviderContractSuite

    class TestAcmeSTT(STTProviderContractSuite):
        provider_factory = AcmeSTT

Suites run offline by default; set ``live = True`` plus
``credential_env_var`` on a subclass for an optional live mode that skips
when the credential is missing. See :mod:`easycat.testing.contracts` for
the per-surface contracts and override points.

The capability-report models re-exported here come from
:mod:`easycat.validation` (the same redaction-aware shapes EasyCat's
``validate`` lanes emit), so provider authors publish capability metadata
through one shared, secret-safe schema instead of inventing their own.
"""

from easycat.testing.contracts import (
    AGENT_BRIDGE_EVENT_KINDS,
    AgentBridgeContractSuite,
    ContractSuite,
    ProviderContractSuite,
    STTProviderContractSuite,
    TransportContractSuite,
    TTSProviderContractSuite,
    VADProviderContractSuite,
)
from easycat.testing.recorder import RecordingAgentRecorder
from easycat.validation.provider_capabilities import (
    ProviderCapabilities,
    ProviderCapabilityReport,
    ProviderIdentifier,
)
from easycat.validation.redaction import contains_unredacted_sensitive_text

__all__ = [
    "AGENT_BRIDGE_EVENT_KINDS",
    "AgentBridgeContractSuite",
    "ContractSuite",
    "ProviderCapabilities",
    "ProviderCapabilityReport",
    "ProviderContractSuite",
    "ProviderIdentifier",
    "RecordingAgentRecorder",
    "STTProviderContractSuite",
    "TTSProviderContractSuite",
    "TransportContractSuite",
    "VADProviderContractSuite",
    "contains_unredacted_sensitive_text",
]
