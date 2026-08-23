"""EasyCat telephony features: DTMF, voicemail, outbound calls, screening, IVR.

The package root exposes the helpers an application typically wires up
directly — TwiML helpers, the DTMF aggregator, the outbound call
manager, voicemail policy, compliance/DNC helpers, and IVR navigation.
Internal classifier types (state machines, fusion classifiers, screening
pattern sets, etc.) live in their submodules; reach for them via the
explicit module path when extending the pipeline.

Exports load lazily via PEP 562 so importing a single telephony submodule
(e.g. ``from easycat.telephony.dtmf import DTMFAggregatorConfig``) does not
drag in the whole telephony stack — keeping non-telephony cold starts cheap.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY_ATTR: dict[str, str] = {
    # DTMF
    "DTMFAggregator": "easycat.telephony.dtmf",
    "DTMFAggregatorConfig": "easycat.telephony.dtmf",
    "parse_twilio_dtmf_message": "easycat.telephony.dtmf",
    # TwiML helpers
    "compute_twilio_webhook_signature": "easycat.telephony.twiml",
    "parse_gather_webhook": "easycat.telephony.twiml",
    "reconstruct_public_url": "easycat.telephony.twiml",
    "twilio_form_items_from_request": "easycat.telephony.twiml",
    "twilio_public_url_from_request": "easycat.telephony.twiml",
    "twilio_stream_parameters_from_form": "easycat.telephony.twiml",
    "twilio_webhook_idempotency_key": "easycat.telephony.twiml",
    "TwilioWebhookSignatureError": "easycat.telephony.twiml",
    "twiml_dial_number": "easycat.telephony.twiml",
    "twiml_dial_send_digits": "easycat.telephony.twiml",
    "twiml_gather": "easycat.telephony.twiml",
    "twiml_hangup": "easycat.telephony.twiml",
    "twiml_play_digits": "easycat.telephony.twiml",
    "twiml_redirect": "easycat.telephony.twiml",
    "twiml_reject": "easycat.telephony.twiml",
    "twiml_say_and_hangup": "easycat.telephony.twiml",
    "validate_twilio_webhook_signature": "easycat.telephony.twiml",
    # Twilio app settings
    "TwilioAppSettings": "easycat.telephony.twilio_app",
    "TwilioCallSessionIndex": "easycat.telephony.twilio_app",
    "bearer_token_matches": "easycat.telephony.twilio_app",
    "twilio_app_settings_from_env": "easycat.telephony.twilio_app",
    # Telnyx protocol helpers
    "build_answer_payload": "easycat.telephony.telnyx",
    "build_dial_payload": "easycat.telephony.telnyx",
    "build_stream_parameters": "easycat.telephony.telnyx",
    "decode_client_state": "easycat.telephony.telnyx",
    "encode_client_state": "easycat.telephony.telnyx",
    "parse_telnyx_call_event": "easycat.telephony.telnyx",
    "parse_telnyx_webhook": "easycat.telephony.telnyx",
    "telnyx_webhook_idempotency_key": "easycat.telephony.telnyx",
    "verify_telnyx_webhook_signature": "easycat.telephony.telnyx",
    # Telnyx app settings
    "TelnyxAppSettings": "easycat.telephony.telnyx_app",
    "telnyx_app_settings_from_env": "easycat.telephony.telnyx_app",
    # Voicemail
    "VoicemailDetector": "easycat.telephony.voicemail",
    "VoicemailDetectorConfig": "easycat.telephony.voicemail",
    "VoicemailPolicy": "easycat.telephony.voicemail",
    "VoicemailPolicyConfig": "easycat.telephony.voicemail",
    "VoicemailPolicyHandler": "easycat.telephony.voicemail",
    # Outbound calls
    "OutboundCallManager": "easycat.telephony.outbound",
    "OutboundCallManagerState": "easycat.telephony.outbound",
    "emit_call_status": "easycat.telephony.outbound",
    "parse_call_status_callback": "easycat.telephony.outbound",
    # Session actions
    "TwilioSessionActionConfig": "easycat.telephony.session_actions",
    "TwilioSessionActionExecutor": "easycat.telephony.session_actions",
    # Call screening
    "CallScreeningDetector": "easycat.telephony.screening",
    "ScreeningResponse": "easycat.telephony.screening",
    # IVR navigator
    "DTMFDelivery": "easycat.telephony.ivr",
    "IVRAction": "easycat.telephony.ivr",
    "IVRActionType": "easycat.telephony.ivr",
    "IVRNavigator": "easycat.telephony.ivr",
    "IVRNavigatorConfig": "easycat.telephony.ivr",
    # Compliance / DNC
    "AIDisclosureConfig": "easycat.telephony.compliance",
    "AsyncDNCStore": "easycat.telephony.compliance",
    "CallBlocked": "easycat.telephony.compliance",
    "DNCList": "easycat.telephony.compliance",
    "DNCStore": "easycat.telephony.compliance",
    "SQLiteDNCList": "easycat.telephony.compliance",
    "check_calling_hours": "easycat.telephony.compliance",
    "lookup_timezone": "easycat.telephony.compliance",
    # Number health + retries
    "CallDispositionTracker": "easycat.telephony.number_health",
    "NumberHealthMonitor": "easycat.telephony.number_health",
    "RetryDecision": "easycat.telephony.retry",
    "RetryStrategy": "easycat.telephony.retry",
    "RetryStrategyConfig": "easycat.telephony.retry",
}

__all__ = sorted(_LAZY_ATTR)  # noqa: PLE0605 exports are generated from the public registry


if TYPE_CHECKING:
    from easycat.telephony.compliance import (
        AIDisclosureConfig,
        AsyncDNCStore,
        CallBlocked,
        DNCList,
        DNCStore,
        SQLiteDNCList,
        check_calling_hours,
        lookup_timezone,
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
    from easycat.telephony.telnyx import (
        build_answer_payload,
        build_dial_payload,
        build_stream_parameters,
        decode_client_state,
        encode_client_state,
        parse_telnyx_call_event,
        parse_telnyx_webhook,
        telnyx_webhook_idempotency_key,
        verify_telnyx_webhook_signature,
    )
    from easycat.telephony.telnyx_app import (
        TelnyxAppSettings,
        telnyx_app_settings_from_env,
    )
    from easycat.telephony.twilio_app import (
        TwilioAppSettings,
        TwilioCallSessionIndex,
        bearer_token_matches,
        twilio_app_settings_from_env,
    )
    from easycat.telephony.twiml import (
        TwilioWebhookSignatureError,
        compute_twilio_webhook_signature,
        parse_gather_webhook,
        reconstruct_public_url,
        twilio_form_items_from_request,
        twilio_public_url_from_request,
        twilio_stream_parameters_from_form,
        twilio_webhook_idempotency_key,
        twiml_dial_number,
        twiml_dial_send_digits,
        twiml_gather,
        twiml_hangup,
        twiml_play_digits,
        twiml_redirect,
        twiml_reject,
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


def __getattr__(name: str):  # PEP 562
    """Lazy re-export dispatcher. Runs once per attribute per process."""
    try:
        module_path = _LAZY_ATTR[name]
    except KeyError:
        raise AttributeError(f"module 'easycat.telephony' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY_ATTR)))
