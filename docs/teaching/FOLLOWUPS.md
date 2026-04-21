# Teaching Ladder — Follow-ups

Items the finished 14-chapter ladder does **not** cover, collected
from two retrospective reviews. None are chapter-internal bugs;
they are scope decisions worth revisiting before declaring the
ladder 1.0.

## High priority

1. **Agent-bridge chapter (or sub-chapter in ch 7).** The project
   advertises "plugs into OpenAI Agents SDK or PydanticAI" in the
   top-level README, but the ladder never once introduces
   `ExternalAgentBridge`, `OpenAIAgentsBridge`,
   `PydanticAIBridge`, `GenericWorkflowBridge`,
   `RemoteResponsesAPIBridge`, or `BridgeAdapterShim`. Ch 13 uses
   `agents.Agent` directly without explaining the adapter.
   Proposal: a dedicated "Bringing your own agent framework"
   chapter, wedged between ch 6 and ch 7, that walks through one
   bridge end-to-end.

2. **Session-action wiring worked example.** Ch 7's README honestly
   concedes that `SessionAction`s are named but never wired.
   Ch 13 exercise 3 asks the reader to run `SendDTMFAction`
   without any reference implementation. The gap closes with a
   10-20 line example using `CoreSessionActionExecutor` or
   `TwilioSessionActionExecutor`. Chapter 7 or a ch 7.5 sidebar
   is the right home.

3. **Operate / lifecycle / multi-session chapter.**
   `SessionManager`, `session.stop()` vs `session.shutdown()` vs
   `session.close()` vs `session.destroy()`, the debugger UI
   (`easycat.debugger`), and the CLI (`easycat doctor`,
   `easycat scaffold`) are all absent from the ladder. A reader
   who finishes chapter 13 cannot deploy a multi-session server.
   Proposal: a post-ch-13 operations chapter that walks through
   `SessionManager` + the debugger UI + the CLI on a concrete
   two-call setup.

## Medium priority

4. **Ch 10 field recordings.** The ch 10 plan called for five
   real `.wav` files checked in. We ship a synthetic generator
   and a replay harness that exercise the lockstep
   `feed_reference` path, but the fixtures are deterministic
   toy signals (sine + noise + simulated echo), not speech.
   Swap in a real voice set (a single 30-second headset record
   + speakerphone record would be enough) to make the AEC demo
   feel real.

5. **Ch 13 ↔ ch 12 bundle compatibility.** Ch 13 uses the
   production `create_session` path, whose journal emits paired
   `stage_start`/`stage_complete` records. Ch 12's eval scripts
   key on the teaching-shape `stage.tts.execute` composites.
   Either:
   - Add a 20-line translator script that collapses production
     pairs into teaching composites.
   - Rewrite ch 12's scripts to consume the paired shape.
   - Accept the gap and document it (currently done).

6. **Tool-bearing fixture bundle.** Ch 12 exercise 2 says "run on
   the chapter-7 tool-bearing bundles"; none are checked in.
   Ship at least one `tools_*.bundle` alongside the five existing
   ch 12 fixtures.

## Lower priority

7. **Pronunciation pipeline surface.** `LLMOutputProcessor`,
   `MarkdownStripProcessor`, `PauseProcessor`,
   `PhoneticReplacementProcessor` exist as first-class public
   exports. Ch 6's sidebar mentions `strip_markdown` but not the
   broader processor chain. One paragraph in ch 6, or a small
   sidebar chapter alongside ch 7.

8. **`ReconnectingWebSocket` + reconnect events in a real run.**
   Ch 11's bug 2 is the first place a reader sees these — but
   they are synthetic. A live demo of ch 6 or ch 8 with a
   deliberately killed WebSocket would close the loop. Probably
   too flaky for the ladder.

9. **Replay / determinism primitives** (`runtime/replay.py`,
   WS-4 deliverable). Would deepen ch 10's replay story from
   "read the mic WAV into the pipeline" to "re-run the exact
   same LLM + STT + TTS transcripts offline." Requires live
   provider version pinning to be useful.

10. **MCP integration.** The word "MCP" appears once, in
    ch 13's "suggested next reading." `plan/workstream-2b`
    treats MCP as essential. A short MCP chapter after ch 7
    would probably land nicely.

11. **Telephony deep-cuts.** `DTMFAggregator`,
    `VoicemailDetector`, the `ivr`/`screening`/`compliance`
    modules — ~12 public modules, one bullet in ch 13. A
    "building for the phone" chapter (post-ch-13 or
    peripheral) is the natural home.

## Small nits worth fixing when convenient

- Ch 2's `stt.*` records gained a `t_ms` field (fix from the
  final review); other bundle readers may still look for
  `offset_ms` only. Keep both for now.
- The two large diagrams in ch 10 technically violate the
  one-diagram rule (merged into one layered diagram by the
  final review).
- Ch 11's bug-1 investigation now keeps its spoiler in
  `solutions.md` only; bug-2/3 already follow that pattern.
