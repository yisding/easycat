# Changelog

All notable changes to EasyCat are recorded here. The project uses
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure; the
versioning contract that begins at 1.0.0 is documented in
[docs/public-api.md](docs/public-api.md#deprecation-removal-policy).

## Unreleased

### Added

- Release-readiness checks, including installed version metadata and strict
  release-tag validation.

### Fixed

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
  leaving it to garbage collection, which warned on Python 3.13+.

## 0.1.0 - Unreleased

### Added

- Initial pre-release implementation of the EasyCat voice-agent framework.
