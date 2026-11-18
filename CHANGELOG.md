# Changelog

All notable changes to EasyCat are recorded here. The project uses
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure; the
versioning contract that begins at 1.0.0 is documented in
[docs/public-api.md](docs/public-api.md#deprecation-removal-policy).

## Unreleased

### Added

- Release-readiness checks, including installed version metadata and strict
  release-tag validation.
- In-band DTMF output. `Session.play_dtmf_tones(digits, ...)` renders ITU-T
  Q.23 key tones (`easycat.telephony.DTMFToneConfig`, `render_dtmf_pcm16`,
  `render_dtmf_mulaw`) and plays them on the live media stream after any queued
  bot speech, returning a `DTMFTonePlayback` handle whose `wait()` reports
  `played`, `timeout`, `disconnected` or `estimated`. The session keeps
  running. The journal records `dtmf_tones_sent` / `dtmf_tones_completed`
  without the digits, and tone audio is masked under `"pii"` redaction.
- The optional `ProtectedPlayback` transport capability
  (`easycat.runtime.capabilities.protected_playback`). Twilio Media Streams
  implement it: a playout fence mark before the tones, a barge-in `clear`
  deferred until the tones are acknowledged (at most the tone length plus
  5 s), and inbound `dtmf` events from the bot's own tones suppressed
  (`TwilioTransportConfig.suppress_inbound_dtmf_during_tones`,
  `dtmf_echo_suppress_tail_ms`).
- Opt-in in-band delivery for Twilio `SendDTMFAction`:
  `TwilioSessionActionConfig(dtmf_mode="in_band")` (or `"auto"`, or the
  `TWILIO_DTMF_MODE` env var for `TwilioAppSettings`) plays the digits on the
  media stream and leaves the session running, with `delivery: "in_band"` and
  the playback outcome in the result metadata. The default stays `"rest"`.
- `InBandDTMFDelivery` and the `IVRDTMFSender` protocol for IVR navigation,
  plus `OutboundCallConfig.ivr_dtmf_mode` (`"auto"`, `"in_band"`, `"rest"`)
  and `ivr_dtmf_tones`.
- A billable live loopback test,
  `tests/telephony/test_twilio_inband_dtmf_live.py`, that checks a Twilio
  `<Gather>` hears EasyCat's in-band tones and that the stream survives them.
  In-band DTMF has not yet been verified on live carrier routes; run this test
  on your own numbers before relying on it.

### Fixed

- `TwilioSessionActionExecutor` now requests session stop after a
  `SendDTMFAction`. Twilio plays outbound DTMF by replacing the call's TwiML,
  which ends the session's `<Connect><Stream>` media stream and then the call,
  so the result no longer claims the session can keep talking. The result
  metadata carries `media_stream_replaced: true`. Telnyx DTMF is unchanged.
  This REST path stays the default; set `dtmf_mode="in_band"` or `"auto"` to
  keep the session running instead.
- Twilio IVR navigation no longer ends the media stream on the first key
  press, so multi-level menus work. With the default
  `OutboundCallConfig.ivr_dtmf_mode="auto"`, a stock Twilio `DTMFDelivery` is
  replaced by a per-session `InBandDTMFDelivery` that plays the tones and the
  speak fallback on the live stream instead of replacing the call's TwiML. Set
  `ivr_dtmf_mode="rest"` to keep the old behaviour. A custom sender is used
  as given, and Telnyx is unchanged.
- `EasyConfig(mcp_servers=...)` raises `EasyConfigError` for a bare string,
  a non-iterable, or a non-string item, instead of a raw `AttributeError` or a
  misleading per-character URI error.
- `with_agent_timeout` and `with_tts_timeout` close the wrapped coroutine or
  stream when they reject an invalid `timeout`, instead of leaking it.
- `LiveKitAEC` drops a buffered sub-frame remainder when a stream's channel
  count or sample width changes, instead of swapping stereo channels for the
  rest of the stream.
- `easycat journal grep --regex` and the debugger's `/api/records?regex=1`
  now reject patterns that chain variable-width repeats with nothing mandatory
  between them (such as `a+a+a+!`), which could backtrack for minutes. Repeats
  separated by a required token, such as `\d+\.\d+`, are still accepted.
- `VoiceServerConfig` rejects non-integer and out-of-range ports, and port
  65535 while the raw WebSocket listener (which binds `port + 1`) is enabled,
  instead of failing at startup with an `OverflowError`.
- The debugger's waveform PNG route reduces mono PCM peaks at C speed instead
  of a per-sample Python loop that could block the debugger for seconds.
- Plain-mode agent streaming no longer re-runs the sentence segmenter over the
  whole pending text on every delta when no sentence boundary is possible.
- `SendDTMFAction` and `TransferPlan.post_dial_digits` now reject characters
  outside the DTMF alphabet instead of silently playing pauses on Twilio.
- Interruption estimation caps each SSML `<break>` at 10 s, so one huge `time`
  value can no longer allocate an unbounded pause timeline.
- `OpenAISTTConfig` and `ElevenLabsSTTConfig` reject NaN, infinite and
  non-positive timeouts, and ElevenLabs STT rejects unsupported
  `realtime_sample_rate` values when the config is built.
- `OpenAITTSConfig.speed` must be in [0.25, 4.0] and ElevenLabs TTS
  `stability`/`similarity_boost` in [0.0, 1.0]; bad values now fail when the
  config is built instead of on the first synthesis.
- `BoundedAudioQueue` rejects NaN, infinite, negative and non-numeric
  `block_timeout` values at construction, so a `BLOCK` queue can no longer
  wait forever or fail on its first full `put()`.
- Telnyx start frames negotiating L16 at a sample rate other than 8000, 16000,
  24000 or 48000 Hz are now rejected with close code 4003 instead of breaking
  or amplifying inbound audio later.
- `LocalTransport` packs outbound audio across chunk boundaries, so TTS chunks
  that are not a whole number of speaker frames no longer play with a burst of
  silence after each one.
- `ReadonlySqliteJournal` closes its SQLite connection after every query instead
  of leaving it to garbage collection, which warned on Python 3.13+.
- `run_text_turn` and `run_text_turns` find each turn's journal records by
  sequence number, so results stay correct after the in-memory journal wraps.
- `run_retention`, `SqliteJournal` and `create_journal` reject unknown retention
  modes, debug levels, backends and negative limits with `ValueError`, so a
  typo such as `mode="Archive"` no longer deletes journals instead of
  archiving them.

## 0.1.0 - Unreleased

### Added

- Initial pre-release implementation of the EasyCat voice-agent framework.
