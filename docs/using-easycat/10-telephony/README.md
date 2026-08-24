# Chapter 10: Answer the Phone

Telephony is not merely another audio transport. A Twilio application has an
HTTP control plane, a Media Streams WebSocket, provider status callbacks, and
possibly REST call-control requests. EasyCat connects those surfaces while
keeping one `Session` per call.

This chapter starts offline. It validates the trust handoff and runs callback,
classification, IVR, and call-control helpers without Twilio credentials or a
network connection.

## Prerequisites

- Complete [chapter 9](../09-multi-caller/) for per-connection factories,
  admission control, and lifecycle ownership.
- Run `uv sync --group dev` from the repository root.
- No API keys, phone number, optional Twilio SDK, or network are needed for the
  chapter checkpoint; it injects a fake REST client.
- The live application later needs the `telephony`, provider, and agent extras.

## Run the offline checkpoint

```bash
uv run python docs/using-easycat/10-telephony/main.py
```

Expected output:

```text
PASS handoff: signed webhook minted one-use media authorization
PASS callbacks: DTMF, status, screening, and IVR inputs classified
PASS actions: DTMF, transfer, and hangup mapped to Twilio updates
```

The checkpoint is intentionally a boundary test. It does not pretend to place
a phone call: live Twilio delivery, credentials, provider AMD, and real audio
belong in a separate integration lane.

## A Twilio call crosses two planes

The inbound sequence is:

1. Twilio sends `POST /twiml` with call metadata and
   `X-Twilio-Signature`.
2. The app reconstructs the exact public URL, validates that signature, and
   only then returns TwiML.
3. The TwiML contains `<Connect><Stream>` pointing to a public `wss://` media
   endpoint plus a signed, one-use stream token.
4. Twilio opens the Media Streams WebSocket and sends a `start` message with
   `CallSid`, `StreamSid`, audio format, and custom parameters.
5. EasyCat consumes the one-use token, creates one
   `TwilioConnectionTransport`, and starts one session for that socket.
6. Twilio sends inbound μ-law 8 kHz media and lifecycle/DTMF messages; EasyCat
   converts them to the session's normal audio and event vocabulary.
7. On Twilio `stop`, socket closure, or process shutdown, the session stops and
   its call mapping is removed.

Webhook signature validation and the stream token solve different problems.
The signature authenticates Twilio's HTTP request. The one-time token prevents
an arbitrary WebSocket client from bypassing the webhook and attaching to the
media listener. The checkpoint proves a token succeeds once and replay fails.
For multi-tenant or shared-worker media listeners, `stream_token_validator` can
accept a `StreamTokenContext` parameter instead of a raw token string; EasyCat
passes the token, `CallSid`, `StreamSid`, and stream custom parameters, and any
mapping returned by the validator is merged into `session.call_identity.custom_fields`.
An explicit `StreamTokenContext` annotation opts in regardless of the parameter
name (including aliases of that type). Every other case — other explicit
annotations and unannotated parameters — retains the raw-token contract,
whatever the parameter is named. The reserved stream-token parameter is
stripped from returned claims and never lands in `custom_fields`.

## Validate the public URL Twilio signed

Twilio signs the public URL and form values, not the internal URL visible after
a TLS-terminating proxy rewrites scheme or host. `twilio_form_items_from_request`
uses `twilio_public_url_from_request` and raises
`TwilioWebhookSignatureError` when validation fails.

Trust `X-Forwarded-Proto` and `X-Forwarded-Host` only when requests arrive
through a proxy that overwrites client-supplied values. Otherwise those headers
let an attacker influence signature reconstruction.

Validate every Twilio webhook, including `/twiml`, `/status`, and Gather
callbacks. Keep your own bearer auth on application-owned control routes such
as `POST /calls`; a valid Twilio webhook signature is not authorization for a
user to place an outbound call.

## Preserve call identity across callbacks and media

`CallSid` identifies the Twilio call. `StreamSid` identifies one Media Stream
attached to it. Preserve both in structured correlation fields, but never use
these high-cardinality identifiers as metric labels. Use `CallSid` for call
lifecycle and status routing.

`twilio_stream_parameters_from_form` copies reviewed caller fields such as
`From`, `To`, and locale metadata into `<Parameter>` children. Twilio forwards
literal parameter values; it does not substitute Python-generated
`{{From}}` placeholders.

Caller data is untrusted personal data. Choose `caller_id_exposure="off"`,
`"system_message"`, or `"tools_only"` deliberately, and redact it from bundles
and application logs according to your policy.

## Inbound media is one session per call

The app-first live path uses `VoiceApp.run("twilio")` with a config factory:

```python
from easycat import EasyConfig, TelephonyConfig, VoiceApp


def config_for(transport):
    return EasyConfig.phone(
        transport=transport,
        agent=build_agent(),
        telephony=TelephonyConfig(
            enable_dtmf_aggregator=True,
            enable_voicemail_detector=True,
        ),
    )


app = VoiceApp(config_factory=config_for)
app.run(
    "twilio",
    stream_url=stream_url,
    twilio_auth_token=twilio_auth_token,
)
```

The reusable helper runs two listeners: HTTP `/twiml` and the raw media
WebSocket. For outbound routes, status callbacks, SMS, and explicit
`CallSid -> Session` routing, start from `examples/twilio_app.py`.

Do not expose a `ws://` URL to Twilio; use `wss://`. Put listener shutdown and
`SessionManager.stop_all()` in the framework lifespan, as chapter 9 did for
browser callers.

## Status callbacks drive call lifecycle

`parse_call_status_callback` turns Twilio form fields into EasyCat events:

| Twilio status | EasyCat event |
|---|---|
| `initiated` | `CallInitiated` |
| `ringing` | `CallRinging` |
| `in-progress` | `CallAnswered` |
| `completed` | `CallEnded` |
| `busy`, `no-answer`, `failed`, `canceled` | `CallFailed` |

`emit_call_status` also maps supported async `AnsweredBy` values into
`VoicemailDetected`. Status callbacks can be retried or arrive out of order;
make handlers idempotent and correlate them with `CallSid`. Removing a call
from an active registry is not the same as deleting its durable audit record.

## DTMF has input and output paths

Incoming Media Stream DTMF messages and `<Gather>` webhooks become `DTMF`
events. `DTMFAggregator` can combine individual digits until a terminator,
length, or idle timeout emits `DTMFAggregated`.

Outgoing DTMF is call control. Twilio does not send outbound DTMF through a
bidirectional Media Stream; EasyCat updates the call with TwiML `<Play
digits="...">`. `SendDTMFAction` applies a bounded inter-digit delay and
sanitizes the allowed `0-9`, `*`, `#`, `A-D`, and pause characters before XML
rendering.

Never concatenate model output into TwiML. Use the typed action and TwiML
helpers, which validate or escape values.

## Screening, voicemail, and IVR are different states

- Provider AMD (`AnsweredBy`) is one signal for human versus machine.
- `VoicemailDetector` observes audio timing/transcripts as another signal.
- `CallScreeningDetector` is for outbound calls: it recognizes iOS, Android,
  carrier, and third-party screening prompts that intercept a call EasyCat
  placed, then can provide a bounded response.
- `IVRNavigator` recognizes menu prompts and asks an injected agent callback
  for a validated `dtmf`, `speak`, `wait`, or `hangup` decision.

These classifiers are fallible and may resolve late. The outbound call state
machine gates the opener while classification is unresolved so a greeting is
not played to voicemail, a screening bot, or an IVR menu. Bound the gate with
`classification_gate_timeout_s`; an unavailable classifier must not hold a
call forever.

`IVRNavigator` enforces maximum menu depth, prompt/agent timeouts, retry bounds,
and a DTMF whitelist. The callback result is untrusted even when produced by
your model. EasyCat parses it into a constrained decision before acting.

Inbound spam or routing policy belongs in your `/twiml` webhook, before a media
stream token is minted. Use `twiml_reject()` to decline an inbound call or
`twiml_redirect()` to hand it to another TwiML URL without opening an EasyCat
session.

## Call control stays provider-neutral at the session boundary

Agent tools enqueue provider-neutral session actions:

- `SendDTMFAction` updates the call with safe TwiML tones;
- `TransferCallAction` uses a `TransferPlan` for preamble, caller ID, and
  post-dial digits;
- `EndCallAction` completes the active call and stops its session;
- `SendSMSAction` sends from the configured Twilio SMS number.

`TwilioSessionActionExecutor` is the provider adapter. It requires an active
`session.transport.call_sid`, then calls the Twilio REST client. The checkpoint
injects a fake client and proves the exact update payloads without installing
the Twilio SDK.

Transfer and hangup results request session stop. Treat tool success as the
provider request being accepted, not proof that a human received the transfer;
observe later status callbacks for the final disposition.

## Outbound calling is a policy boundary

`OutboundCallManager.place_call()` is only one step in a safe outbound product.
Before it, enforce consent, do-not-call state, calling hours/timezone, tenant
authorization, rate and concurrency limits, and number-health policy. Protect
the HTTP call-creation route with application auth; never expose it because the
Twilio credentials themselves are secret.

Useful EasyCat helpers include `SQLiteDNCList`, `check_calling_hours`,
`NumberHealthMonitor`, `CallDispositionTracker`, and `RetryStrategy`. Keep retry
decisions disposition-aware: blocked/DNC/invalid destinations are not transient
failures.

Configure outbound behavior per session with `TelephonyConfig(outbound=...)`.
The account SID, auth token, source number, TwiML URL, and status callback URL
come from secrets/configuration, never model arguments.

## Interruptions must clear provider playback

Twilio can already have outbound audio buffered after EasyCat decides to
interrupt. `TwilioConnectionTransport` sends a `clear` message when the
interruption policy requires it, then uses `mark` acknowledgements to track
playback progress. Stopping only local TTS generation is insufficient: the
caller would still hear provider-buffered audio.

## Telnyx on the same ladder

Telnyx Call Control v2 support mirrors the Twilio structure with three
provider differences:

1. **Webhook auth is Ed25519, not an HMAC over form values.** Telnyx signs
   `{timestamp}|{raw_body}`; verify `telnyx-signature-ed25519` against your
   portal public key with a five-minute replay window.
2. **The media WebSocket handshake is not signed at all.** The one-time
   stream token embedded in the answer/dial `stream_url` is the entire
   transport-auth boundary — treat token TTL and one-time consumption as
   load-bearing.
3. **Media defaults to L16 @ 16 kHz**, which matches EasyCat's internal bus
   exactly (no μ-law companding); PCMU @ 8 kHz negotiates from the
   authoritative `start.media_format`.

The app-first path is identical in shape:

```python
app.run(
    "telnyx",
    stream_url=stream_url,
    api_key=api_key,
    public_key=public_key,
)
```

Session actions use native Call Control commands (`transfer`, `send_dtmf`,
`hangup`) instead of TwiML redirects, outbound calls flow through
`OutboundCallConfig(provider="telnyx")`, and status callbacks map
`call.initiated` / `call.answered` / `call.hangup` onto the same neutral
`CallInitiated` / `CallAnswered` / `CallEnded` / `CallFailed` events. Start
from `examples/telnyx_app.py`; required environment variables are
`TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, and `TELNYX_STREAM_URL` (which must be
`wss://`).

## Graduate to a live phone last

Install and preflight the live example deliberately:

```bash
uv sync --extra openai --extra telephony --extra telephony-fastapi --extra openai-agents --group dev
uv run easycat doctor
uv run easycat doctor --json
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
```

Then run the reference app behind a public TLS/WSS ingress:

```bash
uv run --env-file .env uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0
```

For Telnyx, run the Telnyx reference app instead:

```bash
uv run --env-file .env uvicorn examples.telnyx_app:create_app --factory --host 0.0.0.0
```

Use a Twilio test project or tightly controlled destination first. Verify
signature validation through the real proxy URL and media WebSocket handshake,
stream-token consumption, the `TWILIO_MAX_SESSIONS` cap, status callbacks,
teardown, recording/consent policy, and cost limits before enabling general
outbound calls. Tune `TWILIO_DRAIN_TIMEOUT_S` and
`TWILIO_FORCE_SHUTDOWN_TIMEOUT_S` so rolling deploys leave enough time for live
sessions to flush before surviving media sockets are closed.

Continue with [the exercises](./EXERCISES.md) to break each boundary safely and
design a production call policy.

## What you should be able to answer now

> Why are both webhook signatures and stream tokens needed?

They establish different properties. The signature authenticates Twilio on
both HTTP control requests and the WebSocket handshake. The one-time stream
token proves that the connection came from TwiML this app accepted and blocks
replay when the `start` frame arrives.

> Can outbound DTMF be written into the Media Stream?

No. EasyCat performs a REST call update with sanitized TwiML.

> Is a successful transfer action the final call disposition?

No. Status callbacks report what happened after the provider accepted it.

## What's next

Chapter 11 closes the ladder by combining validation, deployment, durable
journals, metrics, health, and bounded teardown into an operating contract.
