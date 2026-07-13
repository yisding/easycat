# Chapter 2 Exercises

## Change one speech axis at a time

1. Run `openai --voice alloy`, then `openai --voice nova`. Keep your spoken
   prompt identical and describe only the audible TTS difference.
2. Run `deepgram-stt --voice nova` with the same prompt. The output voice is
   held constant; compare partial/final transcription timing and words instead.
3. Run `elevenlabs-voice`, then pass another ElevenLabs voice ID with
   `--voice`. Keep the model fixed so the voice ID is the only changed field.

Self-check:

- A voice change belongs to the typed TTS config, not the agent instructions.
- The Deepgram profile changes STT while preserving OpenAI TTS, proving that
  the roles are independent.
- ElevenLabs calls its field `voice_id`; OpenAI calls its field `voice`.
- The `list` profile reports registered names, while `doctor --provider ...`
  checks credentials/reachability. Neither predicts subjective quality.

For a failure-mode stretch, misspell `deepgram` in a local copy of the script
and read EasyCat's provider suggestion. Restore the valid name before moving
on; do not turn a spelling exercise into a permanent profile.
