# Chapter 3 Exercises

## Compare a complete turn, not a single number

1. Ask the same two-clause question with `vad-only` and `fast`, pausing briefly
   between clauses. Note whether either profile cuts off the second clause.
2. Run `balanced`, ask for a long answer, and interrupt it aloud. Repeat with
   `raw` while wearing headphones; distinguish barge-in policy from acoustic
   echo effects.
3. Add steady background noise, compare `balanced` and `clean`, and record
   whether VAD false-starts or transcript errors actually improve.
4. Run `push_to_talk.py` and deliberately pause mid-sentence. Confirm the turn
   stays open until the second Enter press.

Self-check:

- Faster endpointing is only better when the complete intended utterance
  survives.
- Barge-in is triggered by a speech-start during bot playback; it is not a
  separate turn mode.
- AEC targets playback echo; NR targets background noise. They solve different
  problems.
- Push-to-talk ignores pauses because the UI owns `start_turn()` / `end_turn()`.

For a tuning stretch, change only `smart_turn_sensitivity` in the `fast`
profile. Test several multi-clause utterances and keep notes; do not declare a
winner from one sentence.
