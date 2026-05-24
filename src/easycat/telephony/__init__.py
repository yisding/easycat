"""EasyCat telephony features: DTMF, voicemail, outbound calls, screening, IVR.

The package root exposes the helpers an application typically wires up
directly — TwiML helpers, the DTMF aggregator, the outbound call
manager, voicemail policy, compliance/DNC helpers, and IVR navigation.
Internal classifier types (state machines, fusion classifiers, screening
pattern sets, etc.) live in their submodules; reach for them via the
explicit module path when extending the pipeline.
"""

from easycat.telephony.compliance import (
    OPT_OUT_PHRASES,
    AIDisclosureConfig,
    CallBlocked,
    DNCList,
    check_calling_hours,
    detect_opt_out,
    lookup_timezone,
    match_opt_out_phrase,
)
from easycat.telephony.dtmf import (
    DTMFAggregator,
    DTMFAggregatorConfig,
    parse_twilio_dtmf_message,
)
from easycat.telephony.ivr import (
    DTMFDelivery,
    IVRAction,
    IVRActionType,
    IVRNavigator,
    IVRNavigatorConfig,
)
from easycat.telephony.number_health import (
    CallDispositionTracker,
    NumberHealthMonitor,
    NumberHealthWarning,
)
from easycat.telephony.outbound import (
    OutboundCallManager,
    OutboundCallManagerState,
    emit_call_status,
    parse_call_status_callback,
)
from easycat.telephony.retry import (
    RetryDecision,
    RetryStrategy,
    RetryStrategyConfig,
)
from easycat.telephony.screening import (
    CallScreeningDetector,
    ScreeningResponse,
)
from easycat.telephony.session_actions import (
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
)
from easycat.telephony.twiml import (
    compute_twilio_webhook_signature,
    parse_gather_webhook,
    twiml_dial_number,
    twiml_dial_send_digits,
    twiml_gather,
    twiml_hangup,
    twiml_play_digits,
    twiml_say_and_hangup,
    validate_twilio_webhook_signature,
)
from easycat.telephony.voicemail import (
    VoicemailDetector,
    VoicemailDetectorConfig,
    VoicemailPolicy,
    VoicemailPolicyConfig,
    VoicemailPolicyHandler,
)

__all__ = [
    # DTMF
    "DTMFAggregator",
    "DTMFAggregatorConfig",
    "parse_twilio_dtmf_message",
    # TwiML helpers
    "compute_twilio_webhook_signature",
    "parse_gather_webhook",
    "twiml_dial_number",
    "twiml_dial_send_digits",
    "twiml_gather",
    "twiml_hangup",
    "twiml_play_digits",
    "twiml_say_and_hangup",
    "validate_twilio_webhook_signature",
    # Voicemail
    "VoicemailDetector",
    "VoicemailDetectorConfig",
    "VoicemailPolicy",
    "VoicemailPolicyConfig",
    "VoicemailPolicyHandler",
    # Outbound calls
    "OutboundCallManager",
    "OutboundCallManagerState",
    "emit_call_status",
    "parse_call_status_callback",
    # Session actions
    "TwilioSessionActionConfig",
    "TwilioSessionActionExecutor",
    # Call screening
    "CallScreeningDetector",
    "ScreeningResponse",
    # IVR navigator
    "DTMFDelivery",
    "IVRAction",
    "IVRActionType",
    "IVRNavigator",
    "IVRNavigatorConfig",
    # Compliance / DNC
    "AIDisclosureConfig",
    "CallBlocked",
    "DNCList",
    "OPT_OUT_PHRASES",
    "check_calling_hours",
    "detect_opt_out",
    "lookup_timezone",
    "match_opt_out_phrase",
    # Number health + retries
    "CallDispositionTracker",
    "NumberHealthMonitor",
    "NumberHealthWarning",
    "RetryDecision",
    "RetryStrategy",
    "RetryStrategyConfig",
]
