# EasyCat Roadmap — Competitive Gap Analysis & Next Steps

_Last updated: 2026-05-31_

This document captures a competitive review of EasyCat against the leading
open-source voice-AI agent frameworks, identifies the gaps that matter, and
proposes a prioritized roadmap. It is the output of a structured exploration of
each project's source tree and **recent commit history** (what they have been
actively building over the last ~6 months), not just their READMEs.

## Frameworks surveyed

| Framework | License | Language(s) | Positioning |
|---|---|---|---|
| **EasyCat** (us) | — | Python | Voice-only, telephony + compliance, journal-first observability |
| **Pipecat** (Daily) | BSD-2 | Python | Frame/processor pipeline; widest service catalog; fastest path to prod |
| **LiveKit Agents** | Apache-2 | Python, Node | WebRTC-native platform + agent runtime; 75+ plugins; production scaling |
| **TEN Framework** | Apache-2 | C/C++, Go, Python, Node | Graph runtime + visual designer; avatars; lowest-level/highest-ceiling |
| **Vocode Core** | MIT | Python | Modular toolkit; Twilio/Vonage; LiveKit/Zoom |
| **Bolna** | MIT | Python | End-to-end telephony platform; graph agents; deep call analytics |

## What everyone is building right now (the recent-commit signal)

Reading the last ~6 months of commits across competitors, four themes dominate
development effort everywhere:

1. **Realtime / speech-to-speech (S2S) as a first-class path.** Pipecat shipped
   a major "realtime service mode" refactor (decoupling context writes from turn
   frames to kill STT latency); LiveKit added OpenAI Realtime v2, Gemini Live,
   AWS Polly/Nova realtime, xAI realtime; TEN added Realtime GPT + Gemini 3 Flash
   defaults. This is the single biggest trend.
2. **Provider catalog expansion at a relentless pace.** TEN added ~56 STT/TTS
   commits in 6 months; LiveKit and Pipecat each carry 19–28 STT and 24–28 TTS
   integrations and add new ones monthly (Soniox, Speechmatics, Gradium, Rime,
   Sarvam, Smallest, Hume, xAI, Fish, Inworld, local Whisper/Piper/Kokoro).
3. **Avatars + vision/multimodal.** LiveKit now maintains **14** avatar
   providers; Pipecat has HeyGen/Tavus/Simli/LemonSlice + Moondream vision; TEN
   has Live2D lip-sync, HeyGen, Anam, Trulience, Tavus.
4. **MCP (Model Context Protocol) + multi-agent orchestration.** Pipecat,
   LiveKit, and TEN all added native MCP support. Pipecat shipped a multi-worker
   bus (local/Redis/PGMQ) for distributed agent handoff; LiveKit has typed agent
   handoff with shared `userdata`.

## Where EasyCat already leads (protect these)

EasyCat is **not** behind everywhere — it has real, differentiated strengths
that the roadmap should preserve and lean into:

- **Journal-first observability.** The `ExecutionJournal` (in-memory / SQLite /
  Litestream / libSQL backends), debug bundles, deterministic replay, and the
  validation CLI (`validate quick/socket/stress/latency/live`) are *deeper than
  any competitor's debugging story*. Pipecat's Whisker/Tail and LiveKit's
  OTel/Prometheus are good, but nobody has bundle-based record/replay for evals.
- **Compliance-grade outbound telephony.** Built-in TCPA opt-out phrase
  detection, DNC list, calling-hours checks, per-number health, call disposition
  tracking, retry/SMS-fallback strategy, AI-disclosure greeting. *No competitor
  ships this.* Bolna has telephony breadth but not the compliance layer.
- **Broad agent-framework bridge support.** 7 bridges (OpenAI Agents, PydanticAI,
  LangChain, LangGraph, LlamaIndex workflows, Responses API, generic workflow).
  Most competitors assume their own LLM-service abstraction; EasyCat meets
  existing agent code where it lives.
- **Smart-turn ONNX endpointing** bundled and working today.
- **Focused, teachable voice-only surface** with a 16-chapter ladder.

## Gap analysis by theme

Legend for "Who has it": **PC**=Pipecat, **LK**=LiveKit, **TEN**=TEN,
**VOC**=Vocode, **BOL**=Bolna.

### A. Realtime / speech-to-speech (HIGH impact)
- **Gap:** EasyCat uses OpenAI Realtime only as a low-latency *STT*, not as a
  true voice-to-voice loop. There is no S2S path where the model consumes audio
  and emits audio/turns directly.
- **Who has it:** PC, LK, TEN, BOL (OpenAI Realtime; LK/PC also Gemini Live, AWS
  Nova Sonic, xAI realtime).
- **Why it matters:** S2S is the lowest-latency, most natural conversational
  mode and is where the whole field is converging. Today this is EasyCat's most
  conspicuous architectural gap.

### B. Provider catalog breadth (HIGH impact, MEDIUM effort)
- **Gap:** STT 5 / TTS 4 / LLM-via-bridges. Missing the providers competitors
  treat as table stakes:
  - **STT:** Google, Azure, AWS Transcribe, AssemblyAI, Soniox, Speechmatics,
    Gladia, Groq/local Whisper, Sarvam.
  - **TTS:** Google, Azure, AWS Polly, Rime, Hume, Fish, Sarvam, LMNT, Neuphonic,
    plus **local/offline** options (Piper, Kokoro, XTTS).
  - **LLM:** no unified multi-LLM path. Bolna routes 13+ models through LiteLLM.
- **Who has it:** all, led by TEN/LK/PC.
- **Why it matters:** provider lock-in is the #1 adoption objection; "does it
  support X?" decides evals. The registry pattern (`_PROVIDER_TO_CONFIG`,
  `_PROVIDERS`) already makes this cheap to extend.

### C. Avatars, video & vision / multimodal (MEDIUM impact)
- **Gap:** no video, no avatar lip-sync, no vision input. Voice-only by design.
- **Who has it:** LK (14 avatars), PC (HeyGen/Tavus/Simli + Moondream vision),
  TEN (Live2D/HeyGen/Anam/Trulience/Tavus).
- **Why it matters:** avatars drive demos and a real (if narrower) set of use
  cases. This is a deliberate scope decision — recommend a *thin avatar sink
  abstraction* rather than chasing 14 vendors.

### D. MCP (Model Context Protocol) (MEDIUM-HIGH impact, LOW-MEDIUM effort)
- **Gap:** no native MCP client; tools must be wired per-agent-framework.
- **Who has it:** PC, LK, TEN (all native, stdio + HTTP/SSE).
- **Why it matters:** MCP is becoming the standard tool-distribution mechanism;
  a single MCP bridge would light up tools across *all 7* agent bridges at once.

### E. Multi-agent orchestration & handoff (MEDIUM impact)
- **Gap:** workflows are manual; no typed handoff, no shared session state across
  agents, no distributed bus.
- **Who has it:** PC (multi-worker bus: local/Redis/PGMQ), LK (agent handoff +
  shared `userdata`), Bolna/TEN (graph agents).
- **Why it matters:** complex IVR/triage flows increasingly need specialist
  agents that hand off. EasyCat's strong session/turn model is a good base.

### F. Telephony breadth (MEDIUM impact, LOW effort per provider)
- **Gap:** Twilio only.
- **Who has it:** BOL (Twilio/Plivo/Exotel/Vobiz/SIP), PC serializers
  (Twilio/Plivo/Telnyx/Exotel/Genesys/Vonage), LK (SIP + warm transfer), VOC
  (Twilio/Vonage).
- **Why it matters:** EasyCat's compliance layer is its telephony moat — but it
  only reaches Twilio customers. Adding Plivo/Telnyx/SIP multiplies the
  addressable base while keeping the differentiated compliance stack on top.

### G. Advanced turn-taking & interruption (MEDIUM impact)
- **Gap:** single silence-timeout + smart-turn path; interruption is audio-byte
  estimation. No pluggable strategies, no false-positive/resume handling, no
  barge-in cooldown.
- **Who has it:** PC (7+ pluggable start/stop strategies incl. wake-phrase,
  min-words), LK (semantic transformer turn model, barge-in cooldown, false-
  positive detection + resumption), BOL (sequence-gated `InterruptionManager`).
- **Why it matters:** turn quality is the difference between a demo and a
  shippable agent; it's where all three leaders are actively investing.

### H. Language identification & mid-call switching (LOW-MEDIUM impact)
- **Gap:** none.
- **Who has it:** BOL (LID with shadow A/B), LK/AWS (mid-stream language switch).

### I. OpenTelemetry / metrics export (LOW-MEDIUM impact, LOW effort)
- **Gap:** OTel facade is minimal; no Prometheus/OTLP export of the rich data the
  journal already holds.
- **Who has it:** PC, LK, TEN (OTel + Prometheus/Sentry).
- **Why it matters:** the journal already captures everything — exporting it to
  OTLP/Prometheus is mostly plumbing and unlocks prod monitoring.

### J. Context/memory & prompt caching (LOW impact, by design)
- **Gap:** stateless-per-turn; no memory layer, no summarization helper, no
  explicit prompt-cache control.
- **Who has it:** PC (mem0, `LLMContextSummarizer`, prompt caching), LK (Anthropic
  cached_content, OpenAI cache retention).

### K. Horizontal scaling / job scheduling (LOW impact for now)
- **Gap:** single-process examples; no worker pool, IPC, or job dispatch.
- **Who has it:** LK (AgentServer + IPC), PC (worker bus).

---

## Proposed roadmap

Ordered by impact-to-effort. Each tier is independently shippable.

### Now (next 1–2 cycles) — close the latency & catalog gaps

1. **Realtime / speech-to-speech path (Theme A).** Promote OpenAI Realtime from
   STT-only to a true S2S mode: model consumes mic audio and emits audio + turn
   signals, with the journal/turn-manager wrapping it. Design the seam so Gemini
   Live and AWS Nova Sonic can slot in behind the same interface. *Highest-
   leverage single item.*
2. **Provider catalog expansion (Theme B).** Use the existing registries to add,
   in priority order: **Google** + **Azure** STT/TTS, **AWS** Transcribe/Polly,
   **AssemblyAI** STT, **Groq/local Whisper** STT, and at least one **local TTS**
   (Piper or Kokoro) for offline/cost-sensitive deploys. Add a **LiteLLM-style
   unified LLM bridge** so any of 100+ chat models works without a bespoke bridge.
3. **Native MCP bridge (Theme D).** One MCP client (stdio + HTTP/SSE) that
   surfaces MCP tools to every agent bridge. High ROI: one integration, all 7
   bridges benefit.
4. **OpenTelemetry/Prometheus export of journal data (Theme I).** Cheap given the
   journal already holds spans/metrics; makes EasyCat prod-observable in standard
   tooling while keeping the journal as the differentiator.

### Next (following cycles) — depth where we already have a moat

5. **Telephony breadth on top of the compliance stack (Theme F).** Add **Plivo**
   and **Telnyx** transports/serializers, then a generic **SIP** path. Keep
   DNC/opt-out/calling-hours/disposition working across all of them — this turns
   our compliance layer from a Twilio feature into a platform advantage.
6. **Pluggable turn-taking + smarter interruption (Theme G).** Refactor turn
   start/stop into composable strategies (wake-phrase, min-words, transcription,
   external/manual) and add barge-in cooldown + false-positive/resume handling.
   Consider adopting/swapping in a semantic turn model.
7. **Multi-agent handoff (Theme E).** Typed agent handoff with shared session
   `userdata` across turns (LiveKit-style) before any distributed bus. Builds on
   the existing Session/TurnManager rather than a rewrite.

### Later — scope-expanding bets (validate demand first)

8. **Thin avatar/video sink (Theme C).** A minimal `AvatarSink` protocol that
   feeds TTS audio + timing to one provider (Tavus or HeyGen) as a proof point,
   without committing to a 14-vendor matrix. Add **vision input** via the
   realtime path (Gemini Live) if/when S2S lands.
9. **Memory & context helpers (Theme J).** Optional mem0-style memory adapter and
   a context-summarization helper; expose prompt-cache control on bridges that
   support it.
10. **Language identification / mid-call switching (Theme H).**
11. **Horizontal scaling guide + worker pool (Theme K).** Document and provide a
    reference for multi-process/distributed deployment once single-process
    ergonomics are saturated.

## Strategic framing

EasyCat should not try to out-catalog TEN or out-scale LiveKit. The durable
position is **"the voice agent stack you can actually debug and ship to a
regulated phone deployment."** That means:

- **Win on observability + evals** (journal/bundles/replay) — already ahead, keep
  extending (OTLP export, more eval tooling).
- **Win on compliant telephony** — already unique, broaden the carrier reach
  (Plivo/Telnyx/SIP) so the compliance moat covers more customers.
- **Reach parity, not dominance, on latency & catalog** — ship the S2S path and
  the table-stakes providers (Google/Azure/AWS/AssemblyAI/local) + MCP so EasyCat
  stops losing evals on "does it support X?".
- **Treat avatars/video/scaling as optional bets** gated on real demand.
