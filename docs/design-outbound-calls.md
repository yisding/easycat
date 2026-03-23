# Outbound Calls: Voicemail, IVR, and Call Screening — Design Document

> **Scope:** Design only. This document captures architecture, corner cases, and implementation plan for future development. No code changes in this iteration.
>
> **Decisions made:**
> - **Twilio SDK** (`twilio` Python package) as an optional dependency for REST API calls
> - **Screening response:** both configurable string (default, fast) and agent-generated (opt-in, contextual)

## Context

EasyCat currently handles **inbound** calls well — audio comes in via Twilio Media Streams, flows through VAD → STT → Agent → TTS, and goes back out. There's already foundational telephony support: DTMF parsing/aggregation, TwiML helpers, heuristic voicemail detection (monologue + beep), and Twilio AMD webhook parsing.

**What's missing** is the orchestration layer for **outbound** calls, where the bot initiates calls and must handle three increasingly common scenarios:
1. **Voicemail** — the call goes to an answering machine
2. **IVR** — an automated phone tree answers before reaching a human
3. **Call screening** — iOS 26 / Android / carrier-level screening intercepts the call

Each scenario requires detecting *what* answered, deciding *what to do*, and executing that decision — all while the audio pipeline is running.

---

## The Problem Space

### Scenario 1: Voicemail / Answering Machine

**What happens:** Bot dials → ringing → voicemail greeting plays → beep → silence.

**Detection approaches (layered):**
- **Twilio Async AMD** (fastest, ~4s) — use `MachineDetection=DetectMessageEnd` + `AsyncAmd=true` so the bot starts interacting immediately while AMD runs in background. Already parsed by `parse_twilio_amd_webhook()`.
- **Heuristic monologue detection** (fallback) — already implemented in `VoicemailDetector`: continuous speech >8s suggests recorded greeting. Good supplement to Twilio AMD.
- **Beep detection** (already implemented) — zero-crossing frequency analysis in `VoicemailDetector.process_audio()`. Confirms voicemail and signals when to start leaving a message.
- **STT transcript analysis** — use the transcribed greeting text to classify (phrases like "leave a message after the tone", "not available right now"). This is NOT yet implemented and would be a valuable addition.

**Corner cases:**
- Short voicemail greetings (<3s) that AMD may classify as human
- Carrier-level voicemail (Google Voice, YouMail) with non-standard greetings
- Voicemail greetings that ask questions ("press 1 to leave a message")
- No beep — some systems use silence instead of beep (`machine_end_silence`)
- Dual-greeting systems (carrier greeting → personal greeting)
- Voicemail-to-text services that transcribe your message in real-time

### Scenario 2: IVR / Phone Tree

**What happens:** Bot dials → automated system answers → "Press 1 for sales, 2 for support..." → expects DTMF or speech input → may have multiple menu levels.

**Detection:** IVR is hard to distinguish from a human receptionist. Indicators:
- Structured prompts with numbered options
- DTMF tone expectations
- Repeating prompts after timeout
- Standardized phrases ("press or say")

**Navigation approaches:**
- **Agent-driven** — the LLM agent receives the transcribed IVR prompt and decides which DTMF to send or what to say. Most flexible, handles novel IVR layouts.
- **Scripted** — pre-configured DTMF sequences with delays (already supported via `twiml_dial_send_digits()` with `w` pauses). Works for known, static IVR trees.
- **Hybrid** — scripted for known trees, agent fallback for unexpected prompts.

**Corner cases:**
- Multi-level IVR (3+ menu depths)
- Speech-based IVR ("say 'billing'") — bot needs to respond with TTS, not DTMF
- Hold music / hold queues between IVR levels (extended silence + music)
- IVR timeout and re-prompt ("I didn't get that, please try again")
- IVR that requires account numbers or PINs
- Transfer to human after IVR navigation — bot must detect the transition
- IVR systems that use speech recognition and mishear the bot's TTS

### Scenario 3: Call Screening (the new challenge)

**iOS 26 Call Screening** (released June 2025):
- User enables "Ask Reason for Calling" in Settings
- Outbound call → iPhone plays: *"Please state your name and reason for calling"*
- Bot's response is transcribed on-device and shown to the user
- User can: pick up, decline, or let it go to voicemail
- **Not interactive** — Apple's system doesn't ask follow-ups (unlike Google)
- The bot hears a pre-recorded prompt, NOT a live human

**Google/Android Call Screen** (Pixel devices):
- Google Assistant answers: *"Hi, the person you're calling is using a screening service. Go ahead and say your name and why you're calling"*
- **Interactive** — Assistant may ask follow-up questions
- Can auto-reject spam based on responses

**Carrier-level screening** (T-Mobile Scam Shield, AT&T Call Protect, etc.):
- May play "The person you're calling has caller ID screening. Please state your name"
- Similar pattern but less sophisticated

**Detection approach (per Twilio's guidance):**
1. Enable **Async AMD** + **Real-Time Transcription** on the outbound call
2. Use transcript pattern matching to identify screening prompts:
   - iOS: "record your name and reason for calling", "see if this person is available"
   - Android: "using a screening service", "say your name and why you're calling"
   - Carrier: "caller ID screening", "state your name"
3. When screening detected: speak an identification message (name + purpose)
4. Monitor for three outcomes:
   - **Person picks up** — transition to normal conversation
   - **Goes to voicemail** — leave message per voicemail policy
   - **Call declined** — handle gracefully (retry later, different channel, etc.)

**Corner cases:**
- Bot must respond quickly to screening prompts (~3-5s window)
- Screening → brief silence → human picks up (must not mistake for voicemail)
- Screening → voicemail (must leave appropriate message)
- Multiple screening layers (carrier screening → iOS screening)
- Non-English screening prompts (international calls)
- Custom screening messages (some apps let users record their own)
- Bot's identification response may itself be screened/rejected

---

## Proposed Implementation

### Phase 1: Call Lifecycle Events & Outbound Call Manager

**New events** in `events.py`:
```python
CallInitiated     # Bot placed an outbound call (call_sid, to, from_)
CallRinging       # Remote phone is ringing
CallAnswered      # Call was answered (by human, machine, or screener)
CallScreening     # Call screening detected (platform: ios|android|carrier)
CallFailed        # Call failed (busy, no answer, rejected, error)
CallEnded         # Call terminated (duration, disposition)
```

**New module** `telephony/outbound.py`:
- `OutboundCallManager` — orchestrates placing calls via Twilio REST API
- Manages call state (initiating → ringing → answered → in_progress → ended)
- Handles Twilio status callbacks (`initiated`, `ringing`, `answered`, `completed`, `busy`, `no-answer`, `failed`, `canceled`)
- Configures AMD and real-time transcription on outbound calls

**New config** in `config.py`:
```python
@dataclass
class OutboundCallConfig:
    from_number: str                    # Caller ID (E.164)
    amd_mode: str = "DetectMessageEnd"  # "Enable" | "DetectMessageEnd"
    async_amd: bool = True              # Always async for best UX
    amd_timeout: int = 30               # MachineDetectionTimeout (3-59s)
    speech_threshold: int = 2400        # MachineDetectionSpeechThreshold (1000-6000ms)
    speech_end_threshold: int = 1200    # MachineDetectionSpeechEndThreshold (500-5000ms)
    silence_timeout: int = 5000         # MachineDetectionSilenceTimeout (2000-10000ms)
    enable_screening_detection: bool = True
    screening_response: str = ""        # Static response when screened (fast path)
    screening_use_agent: bool = False   # If True, agent generates screening response
    enable_realtime_transcription: bool = True
    twilio_account_sid: str = ""        # Twilio credentials (or from env)
    twilio_auth_token: str = ""
```

**Dependency:** `twilio` Python SDK added as optional extra (`pip install easycat[twilio]`). The `OutboundCallManager` imports it at runtime and raises `ImportError` with install instructions if missing.

### Phase 2: Call Screening Detector

**New module** `telephony/screening.py`:
- `CallScreeningDetector` — subscribes to `STTPartial`/`STTFinal` events
- Pattern-matches transcribed audio against known screening prompts
- Classifies platform: `ios`, `android`, `carrier`, `unknown`
- Emits `CallScreening` event with detected platform
- Configurable response: bot speaks its identification (name + reason)

**Screening response strategy (both options):**
- **Fast path (default):** `screening_response` config string is sent immediately via TTS. Example: *"Hi, this is Sarah from Acme Corp calling about your upcoming appointment."* Latency: ~200ms (just TTS synthesis).
- **Agent path (opt-in):** When `screening_use_agent=True`, the `CallScreeningDetector` invokes the agent with context (callee name, call purpose, etc.) to generate a natural response. Latency: ~1-2s (LLM + TTS). Better for personalized outreach.
- **Fallback:** If agent path is enabled but agent doesn't respond within 3s, fall back to the static string.

**Screening state machine:**
```
WAITING → SCREENING_DETECTED → RESPONDING → OUTCOME
                                              ├→ HUMAN_ANSWERED
                                              ├→ VOICEMAIL
                                              └→ DECLINED
```

**Key patterns to match** (configurable, extensible):
```python
IOS_PATTERNS = [
    "record your name",
    "reason for calling",
    "see if this person is available",
    "state your name",          # iOS variant
]
ANDROID_PATTERNS = [
    "using a screening service",
    "say your name and why",
    "Google call screen",
    "screening service from Google",
]
CARRIER_PATTERNS = [
    "caller ID screening",
    "state your name",
    "identify yourself",
    "scam likely",              # carrier spam label
]
```

**Important timing note:** iOS 26 screening gives ~10s for the caller to respond before routing to voicemail. The bot must detect the screening prompt via STT partial transcripts and begin responding within 3-5s to be effective. Using `STTPartial` events (not waiting for `STTFinal`) is critical here.

### Phase 3: Outbound Call State Machine (Coordinator)

**New module** `telephony/call_state.py`:
- `OutboundCallStateMachine` — coordinates AMD, screening, voicemail, and IVR detection
- Consumes events from all detectors and produces high-level call disposition

**States:**
```
INITIATING → RINGING → ANSWERED → CLASSIFYING
                                    ├→ HUMAN (normal conversation)
                                    ├→ SCREENING (identification flow)
                                    │    ├→ HUMAN (person picked up)
                                    │    ├→ VOICEMAIL (went to VM after screening)
                                    │    └→ DECLINED (call rejected)
                                    ├→ VOICEMAIL (leave message / hang up)
                                    ├→ IVR (navigate menus)
                                    │    └→ HUMAN (reached a person)
                                    └→ UNKNOWN (fallback to agent)
```

- Integrates with existing `VoicemailDetector`, `VoicemailPolicyHandler`, `DTMFAggregator`
- Time-bounded classification: if no determination in N seconds, default to `UNKNOWN` and let agent handle it

### Phase 4: IVR Navigator

**New module** `telephony/ivr.py`:
- `IVRNavigator` — optional module for agent-driven IVR traversal
- Subscribes to `STTFinal` events during IVR state
- Passes transcribed prompts to the agent with IVR-specific context
- Agent returns structured output: `{action: "dtmf", digits: "1"}` or `{action: "speak", text: "billing"}`
- Sends DTMF via Twilio REST API or TTS response
- Tracks menu depth and navigation history
- Detects transfer to human (long silence after IVR, greeting change)

### Phase 5: Enhanced Voicemail Handling

**Enhancements to existing `telephony/voicemail.py`:**
- Add **STT-based greeting classification** — analyze transcribed greeting text for voicemail indicators
- Add **post-screening voicemail detection** — when screening → voicemail, the greeting comes after the screening prompt
- Integrate with `OutboundCallStateMachine` for coordinated detection

---

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/easycat/events.py` | Modify | Add `CallInitiated`, `CallRinging`, `CallAnswered`, `CallScreening`, `CallFailed`, `CallEnded` events |
| `src/easycat/telephony/outbound.py` | Create | `OutboundCallManager` — Twilio REST API integration for placing calls |
| `src/easycat/telephony/screening.py` | Create | `CallScreeningDetector` — transcript-based screening detection |
| `src/easycat/telephony/call_state.py` | Create | `OutboundCallStateMachine` — coordinates all detection into call disposition |
| `src/easycat/telephony/ivr.py` | Create | `IVRNavigator` — agent-driven IVR menu traversal |
| `src/easycat/telephony/voicemail.py` | Modify | Add STT-based greeting classification |
| `src/easycat/telephony/__init__.py` | Modify | Export new modules |
| `src/easycat/config.py` | Modify | Add `OutboundCallConfig`, screening config |
| `src/easycat/session.py` | Modify | Wire outbound call manager + screening detector into session lifecycle |
| `tests/telephony/test_outbound.py` | Create | Tests for outbound call manager |
| `tests/telephony/test_screening.py` | Create | Tests for screening detection |
| `tests/telephony/test_call_state.py` | Create | Tests for call state machine |
| `tests/telephony/test_ivr.py` | Create | Tests for IVR navigator |

---

## Existing Code to Reuse

- `telephony/voicemail.py` — `VoicemailDetector` (monologue + beep), `VoicemailPolicyHandler`, `parse_twilio_amd_webhook()`
- `telephony/dtmf.py` — `DTMFAggregator`, `parse_twilio_dtmf_message()`
- `telephony/twiml.py` — `twiml_play_digits()`, `twiml_dial_send_digits()`, `twiml_hangup()`
- `events.py` — `EventBus`, `VoicemailDetected`, `DTMF`, `DTMFAggregated`, `STTPartial`, `STTFinal`
- `cancel.py` — `CancelToken` for cooperative cancellation of IVR/screening flows

---

## Verification Plan

1. **Unit tests** — each new module gets isolated tests with `NoopTransport`/mock `EventBus`
2. **Screening detection tests** — feed known iOS/Android/carrier screening transcripts through `CallScreeningDetector`, verify correct classification
3. **State machine tests** — simulate full call flows (ringing → screening → human, ringing → voicemail, ringing → IVR → human) through `OutboundCallStateMachine`
4. **IVR navigation tests** — mock agent responses and verify correct DTMF/speech actions
5. **Integration test** (marked `@pytest.mark.integration`) — place a real outbound call via Twilio to a test number
6. Run full suite: `uv run pytest` + `uv run ruff check .`

---

## Open Questions for Future Implementation

1. **Retry strategy** — when a call is declined via screening, should we retry with a different approach (SMS fallback, different time of day, different caller ID)?
2. **Concurrency** — for campaign-style outbound calling (many calls at once), do we need a dialer/queue manager, or is that out of scope?
3. **Recording** — should outbound calls be recorded for compliance/QA? Twilio supports call recording, but there are legal requirements (consent, notification).
4. **Analytics** — should we track call disposition rates (human answer rate, voicemail rate, screening rate) for tuning AMD parameters?
5. **Non-Twilio providers** — should `OutboundCallManager` be a Protocol so users can plug in Vonage, Bandwidth, etc.?

---

## References

- [Twilio AMD Documentation](https://www.twilio.com/docs/voice/answering-machine-detection)
- [Twilio AMD FAQ & Best Practices](https://www.twilio.com/docs/voice/answering-machine-detection-faq-best-practices)
- [Twilio Async AMD Tutorial](https://www.twilio.com/en-us/blog/async-answering-machine-detection-tutorial)
- [Twilio: Detecting iOS 26 Call Screening with AMD + Real-Time Transcriptions](https://www.twilio.com/en-us/blog/developers/tutorials/product/detect-ios-call-screening-amd-transcriptions)
- [Twilio: Fine-Tune AMD for Accurate Voice Automation](https://www.twilio.com/en-us/blog/developers/best-practices/automated-amd-tests-voice)
- [Apple iOS 26 Call Screening](https://www.apple.com/cm/newsroom/2025/06/apple-elevates-the-iphone-experience-with-ios-26/)
- [Nooks: iOS Call Screening Will Not Kill Parallel Dialing](https://www.nooks.ai/blog-posts/no-ios-call-screening-will-not-kill-parallel-dialing-heres-what-you-need-to-know)
- [Bland AI: Building a Robust Voicemail Detection System](https://www.bland.ai/blogs/building-a-robust-voicemail-detection-system-at-bland) (open-sourced Wave2Vec + CNN models on HuggingFace)
- [Pipecat: Open Source Voice AI Framework](https://github.com/pipecat-ai/pipecat)
