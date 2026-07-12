# Chapter 0 Exercises

## Separate agent behavior from voice runtime

1. Change only the `instructions` passed to `Agent` so every answer ends with
   a short question.
2. Run the chapter and have a two-turn conversation.
3. Revert the instruction change, then rename the Python variable `app` to
   `concierge` without changing the `VoiceApp` call.

Self-check:

- The first change affects what the assistant says, because behavior belongs
  to the agent.
- The variable rename affects nothing at runtime, because it does not change
  either the agent specification or the EasyCat configuration.
- Neither change requires you to construct STT, TTS, VAD, a transport, or a
  `Session` by hand.

For a small stretch, add `stt="openai/realtime"` to `VoiceApp`. The app should
behave the same because you have made the current default explicit. Remove the
argument again before the next chapter; provider selection gets its own rung.
