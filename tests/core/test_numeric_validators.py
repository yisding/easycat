"""Oversized ints must fail validation with ``ValueError``, not ``OverflowError``.

``math.isfinite`` converts an int to a C double and overflows beyond float
range; every public validator goes through ``easycat._numeric`` instead.
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

import pytest

from easycat._health_check import PeriodicHealthChecker
from easycat._numeric import is_finite_number, is_finite_real
from easycat.config import EasyConfig
from easycat.smart_turn import _validate_probability_threshold
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.telephony._server_timeouts import validate_server_timeouts
from easycat.telephony.outbound import _parse_float
from easycat.telephony.screening import _validate_positive_number as screening_positive
from easycat.telephony.voicemail import _validate_non_negative_number as voicemail_non_negative
from easycat.telephony.voicemail import _validate_positive_number as voicemail_positive
from easycat.transports.telnyx_media import TelnyxTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.turn_manager import _validate_non_negative_finite_number
from easycat.vad._base import _validate_non_negative_ms, _validate_vad_sensitivity

HUGE = 10**400


@pytest.mark.parametrize(
    "build",
    [
        lambda: EasyConfig(openai_api_key="k", slow_handler_threshold_s=HUGE),
        lambda: voicemail_positive("x", HUGE),
        lambda: voicemail_non_negative("x", HUGE),
        lambda: screening_positive("x", HUGE),
        lambda: CartesiaTTSConfig(api_key="k", warmup_timeout_s=HUGE),
        lambda: DeepgramTTSConfig(api_key="k", warmup_timeout_s=HUGE),
        lambda: ElevenLabsTTSConfig(api_key="k", warmup_timeout_s=HUGE),
        lambda: DeepgramSTTConfig(api_key="k", warmup_timeout_s=HUGE),
        lambda: TwilioTransportConfig(stream_token_validation_timeout_s=HUGE),
        lambda: TelnyxTransportConfig(stream_token_validation_timeout_s=HUGE),
        lambda: validate_server_timeouts(
            "cfg", start_timeout_s=HUGE, drain_timeout_s=1.0, force_shutdown_timeout_s=1.0
        ),
        lambda: PeriodicHealthChecker(object(), probe_timeout=HUGE),
        lambda: _validate_probability_threshold("x", HUGE),
        lambda: _validate_non_negative_finite_number("x", HUGE),
        lambda: _validate_vad_sensitivity(HUGE),
        lambda: _validate_non_negative_ms("x", HUGE),
    ],
)
def test_oversized_int_is_a_value_error(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        build()


def test_parse_float_treats_oversized_int_as_missing() -> None:
    assert _parse_float(HUGE) is None


def test_is_finite_real_accepts_reals_and_rejects_non_finite() -> None:
    assert is_finite_real(1)
    assert is_finite_real(0.5)
    assert is_finite_real(Fraction(1, 3))
    assert not is_finite_real(True)
    assert not is_finite_real("1")
    assert not is_finite_real(float("inf"))
    assert not is_finite_real(HUGE)
    # ``is_finite_number`` stays limited to built-in int/float.
    assert not is_finite_number(Fraction(1, 3))
