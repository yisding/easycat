"""EasyCat telephony features: DTMF, voicemail, outbound calls, screening, IVR."""

from easycat.telephony.call_state import (
    CallStateChanged,
    OutboundCallState,
    OutboundCallStateMachine,
)
from easycat.telephony.dtmf import (
    DTMFAggregator,
    DTMFAggregatorConfig,
    parse_twilio_dtmf_message,
)
from easycat.telephony.ivr import (
    IVRAction,
    IVRActionType,
    IVRNavigator,
    IVRNavigatorConfig,
    classify_ivr_prompt,
)
from easycat.telephony.outbound import (
    OutboundCallManager,
    OutboundCallManagerState,
    emit_call_status,
    parse_call_status_callback,
)
from easycat.telephony.screening import (
    CallScreeningDetector,
    ScreeningPatternSet,
    ScreeningResponse,
    ScreeningState,
    match_screening_platform,
)
from easycat.telephony.twiml import (
    parse_gather_webhook,
    twiml_dial_send_digits,
    twiml_gather,
    twiml_hangup,
    twiml_play_digits,
)
from easycat.telephony.voicemail import (
    BeepDetectorConfig,
    VoicemailDetector,
    VoicemailDetectorConfig,
    VoicemailPolicy,
    VoicemailPolicyConfig,
    VoicemailPolicyHandler,
    classify_greeting,
    detect_sit_tones,
    is_comfort_noise,
    parse_twilio_amd_webhook,
)

__all__ = [
    # DTMF
    "DTMFAggregator",
    "DTMFAggregatorConfig",
    "parse_twilio_dtmf_message",
    # TwiML helpers
    "parse_gather_webhook",
    "twiml_dial_send_digits",
    "twiml_gather",
    "twiml_hangup",
    "twiml_play_digits",
    # Voicemail
    "BeepDetectorConfig",
    "VoicemailDetector",
    "VoicemailDetectorConfig",
    "VoicemailPolicy",
    "VoicemailPolicyConfig",
    "VoicemailPolicyHandler",
    "parse_twilio_amd_webhook",
    "classify_greeting",
    "detect_sit_tones",
    "is_comfort_noise",
    # Outbound calls
    "OutboundCallManager",
    "OutboundCallManagerState",
    "emit_call_status",
    "parse_call_status_callback",
    # Call screening
    "CallScreeningDetector",
    "ScreeningPatternSet",
    "ScreeningResponse",
    "ScreeningState",
    "match_screening_platform",
    # Call state machine
    "CallStateChanged",
    "OutboundCallState",
    "OutboundCallStateMachine",
    # IVR navigator
    "IVRAction",
    "IVRActionType",
    "IVRNavigator",
    "IVRNavigatorConfig",
    "classify_ivr_prompt",
]
