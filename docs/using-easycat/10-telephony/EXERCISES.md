# Chapter 10 Exercises

Exercises 1–7 are offline. Do not place a live call unless you deliberately
complete the final credentialed exercise.

## 1. Separate the two trust hops

Run:

```bash
uv run python docs/using-easycat/10-telephony/main.py
```

Identify which secret validates the HTTP webhook and which secret signs the
one-use stream token. Change the webhook signature to `wrong` and confirm
validation fails before a token should be issued.

Then consume one issued stream token twice. Explain why the second result is
`False` and why multiple server replicas need shared routing or a compatible
external validator.

## 2. Break public URL reconstruction

Compute a signature for `https://voice.example.test/twiml`, then validate it
against `http://internal:8000/twiml`. Confirm it fails.

Sketch the trusted proxy headers required to reconstruct the original URL.
Name which ingress component must overwrite those headers so a client cannot
spoof them.

## 3. Inspect call and stream identity

Add `CallerName` and `FromCountry` to the form, rebuild the stream parameters,
and inspect the generated XML. Verify there are no `{{From}}` placeholders.

Classify each value as safe operational metadata, PII, or secret. Decide which
may appear in logs, journal records, debug bundles, and agent context.

## 4. Exercise DTMF validation

Change the Gather callback digits to `12X#`. The invalid `X` should not produce
a `DTMF` event. Then try to construct a `SendDTMFAction` with an unsupported
character and read the construction error.

Do not bypass the typed action by concatenating the value into XML.

## 5. Compare classification signals

Add samples for:

- a short human greeting;
- an iOS screening prompt;
- a voicemail greeting;
- an IVR menu;
- a hold message that should not look like an IVR decision point.

Record which helper owns each classification and where ambiguous/late results
should flow in the outbound state machine.

## 6. Inspect provider action payloads

Change the transfer preamble, target, and post-dial digits. Inspect the fake
client's TwiML update and confirm values are escaped/sanitized.

Add a `SendSMSAction` by extending the fake client with a `messages.create`
method and configuring `sms_from_number`. Verify the destination and message
SID metadata without a network call.

## 7. Design an outbound policy

Before `place_call`, specify:

- caller authorization and tenant isolation;
- consent and durable DNC lookup;
- destination normalization and calling hours;
- per-tenant/provider concurrency and rate limits;
- retryable versus permanent failure dispositions;
- recording, disclosure, retention, and deletion rules;
- cost and incident kill switches.

Explain which checks must be atomic when several workers can place calls.

## 8. Enter the live lane deliberately

Only with a Twilio test project and approved destination, install and preflight:

```bash
uv sync --extra openai --extra telephony --extra telephony-fastapi --extra openai-agents --group dev
uv run easycat doctor
uv run easycat doctor --json
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
```

Run the reference app through TLS/WSS and verify, in order: signed `/twiml`,
one-use media authorization, `CallSid`/`StreamSid` correlation, bidirectional
audio, interruption clear, status callback, and clean teardown. Stop at the
first failed safety boundary.

## Done when

You can explain:

- the HTTP-to-media trust handoff;
- why `CallSid` and `StreamSid` have different roles;
- how status, DTMF, screening, voicemail, and IVR reach EasyCat events;
- why call control is a typed provider adapter rather than model-generated XML;
- why placing an outbound call requires product policy beyond API credentials.
