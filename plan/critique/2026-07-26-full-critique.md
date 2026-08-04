# EasyCat: full critique

> **Status: historical record.** This audit is dated 2026-07-26 and is kept
> for its findings, not for its status. Verified on 2026-08-04: 19 of the 20
> HIGH findings are closed. The exception is #14, which not only remains open
> but regressed —
> `ruff --isolated --select C901,PLR0912,PLR0915 src/easycat/session/_session.py`
> now reports C901 40 against a limit of 10, PLR0912 44 against 12, and
> PLR0915 156 against 50. Every finding here that was still live on that date
> was relocated to `plan/roadmap/open-backlog.md`; work the backlog, not this
> file. The body below is unchanged and is cited by finding number and by
> heading anchor from other documents, so it is not compressed, renumbered, or
> moved.

**Date:** 2026-07-26 · **Commit:** `adc346e7` · **Scope:** the whole repository

## What this is

An adversarial multi-agent audit of EasyCat, run to answer one question: *what could be improved?* Twelve independent lenses read the source; every finding each one raised was then handed to a separate verifier whose default stance was rejection — open the cited lines, grep for a mitigation elsewhere in the tree, kill anything generic, and re-grade severity downward unless the evidence forced otherwise. Two further agents looked for root causes across the confirmed set and for what all twelve lenses had missed.

**129 findings were raised. 115 survived verification; 21 were rejected.** The rejected ones are listed in the appendix — they record what was checked and found to be already handled, which is worth as much as the confirmed list.

Severity after verification: **20 high · 61 medium · 34 low.** No finding was graded critical. Every claim below cites a file and line that an agent actually opened; where a verifier corrected the original claim, the correction is recorded in the *Verified* line.

Findings are numbered #1–#115 in a fixed order. The themes, priority list, and gap analysis cross-reference those numbers, so the numbering is stable throughout the document. There are no hyperlinks; search for `#### 44.` to jump to a finding.

---

## Executive assessment

Six months, one maintainer, 3,225 commits, 87k lines of source, 141k of tests, 256k words of docs and planning, zero users, zero tags, unregistered on PyPI. That ratio is the whole story. This is not a sloppy codebase — the per-line quality is genuinely high, the comments cite real incidents rather than restating code, and the concurrency reasoning (`Task.cancelling()`, the CancelToken timestamp-under-lock, the drain-then-force escalation) is better than most funded projects ship. It is an *over-extended* codebase, and that distinction determines the advice: the answer is not "slow down and be more careful," it is "cut scope until the care you already apply covers the whole surface."

The current path is not sustainable, and the evidence is not opinion. Six agent SDKs are wrapped and not one is installed anywhere — not in the dev venv, not in CI. The single resampler every transport depends on has silently run its worst branch since inception. The journal that CLAUDE.md declares the single source of truth truncates at 40 seconds by default and shreds transcripts at write time. These are not obscure corners; they are the default configuration of the three things the framework is *for*. They survived because the verification apparatus was pointed at prose (15.6k LOC of docs-guard tests, 274 tests asserting literal Markdown) and at internal self-consistency (four guarded representations of the docs map — while the fifth, the one that renders the site, drifted 27 pages out and nobody noticed).

**Differentiation.** Pipecat owns pipeline-composition mindshare and the widest provider matrix. LiveKit Agents owns WebRTC and is backed by a company with a hosted product and an infra moat you cannot out-build solo. Vocode is effectively dormant. EasyCat's real candidate differentiators are three: (a) the journal/replay debug-first runtime — nobody else ships an exportable per-turn record designed to hand to a coding agent; (b) an agent-bridge protocol that genuinely holds at the seam (I grepped `session/` and `stages/` and confirmed zero framework branches — that is the hard part and it was done right); (c) the graduated config ladder from `VoiceApp` to `SessionConfig`.

(a) is the strongest and most defensible — and it is the one that does not survive inspection. The differentiator is unshipped. Meanwhile effort flows to breadth where competitors are structurally stronger: a sixth agent bridge, 5,700 LOC of WebTransport (auth-less, three private aioquic attributes, a transport the deployment guide itself calls niche), two tutorial ladders over the same surface, a validation CLI shipped in the wheel that hard-codes this repo's pytest node IDs.

**Where effort does not compound:** anything whose cost scales with docs volume rather than with users or features. Prose-guard tests, the duplicated tutorial ladder, the docs route registry occupying 63% of `cli/_app.py`, `validation/` inside `src/`. None of it gets cheaper as the project grows; all of it gets more expensive.

The correction is arithmetic. Delete WebTransport, one ladder, and the prose guards; move `validation/` out of `src/`; demote three agent bridges to out-of-tree (the extension seam already exists and is good). Spend a fraction of the recovered budget on: `soxr` in base deps, a spectral test, a journal that holds a whole call, and one CI line that makes 25 already-written SDK gates fire. That is a week's work and it converts a plausible framework into a verified one — which is the only version that survives contact with its first ten users.

---

## Root-cause themes

Seven systemic forces generate most of the 115 findings. Fixing these fixes classes of defect rather than instances.

### T1 — Breadth bought on credit: integration surface grew past any path that can execute it

**Mechanism.** The library integrates six agent SDKs, five+ speech vendors, five transports, telephony, and three journal backends. `uv sync --group dev` installs *none* of them — I verified `langchain_core`, `langgraph`, `pydantic_ai`, `agents`, `llama_index`, and even `openai` are all absent from `.venv`. CI never installs one alongside the tests that gate on it: the 25 `pytest.importorskip` gates live in `tests/integrations/` (verified: 25 hits), while the one nightly job with an SDK installed runs `pytest tests/contracts` — which contains **zero** importorskip gates (verified). So ~9,900 LOC of agent bridges plus the provider layer are coupled to six pre-1.0 SDKs with literally no automated compatibility signal, and because nearly every probe is a soft `getattr`/`except ImportError: pass`, upstream drift degrades silently rather than crashing. The shipped `ProviderContractSuite` compounds this: it is run only against fakes defined in the same test file, so a "provider contract" tier tests nothing about any real provider.

**Explains:** 33, 34, 35, 36, 39, 40, 41, 42, 43, 81, 89, 110, 111, 112, 113, 114, 115.

**Structural fix.** Pick the integration set you can execute and cut the rest to community/out-of-tree status (the extension seam — `register_agent_detector`, `BridgeTemplate`, published contract kits — already exists and works). Then make one nightly job install every SDK and run `tests/integrations`. Until an SDK executes in CI, adding a bridge is adding liability, not capability.

---

### T2 — The DSP substrate was never measured, so the realtime core rests on unverified assumptions

**Mechanism.** `_audio_utils.resample()` is the single chokepoint for every sample-rate conversion in the framework. It prefers `soxr`, then `scipy`, then falls back to pure-Python linear interpolation — and I confirmed **neither `soxr` nor `scipy` appears anywhere in `pyproject.toml` or `uv.lock`**. Every install therefore runs the fallback, which for integer down-ratios degenerates to raw sample dropping with no anti-alias filter (measured: 10 kHz → 6 kHz at 0.00 dB on the documented 48k→16k browser path). The same absence of measurement produces the VAD gating on `time.monotonic()` instead of audio position, the AEC far-end reference being re-stamped with the mic's format on WebRTC, smart-turn burning 122 ms in that same resampler to save a 500 ms timer, and `pre_roll_ms` being uncoupled from `min_speech_duration_ms`. Thirteen perf benchmarks exist and not one touches the audio path; no test asserts spectral content, so the whole defect class is invisible to CI.

**Explains:** 29, 44, 45, 46, 47, 49, 50, 52, 53, 70.

**Structural fix.** Declare `soxr` as a **base** dependency (it is small and it is the whole ballgame). Add one spectral-assertion test (inject a tone, DFT the output, assert the alias floor) and one audio-path benchmark to the perf lane. Convert every duration gate in `vad/_base.py` from wall-clock to accumulated sample counts. The audio path is the product; it needs the same measurement discipline the import-weight tests already get.

---

### T3 — The journal is load-bearing by declaration but not by construction

**Mechanism.** CLAUDE.md and `docs/observability.md` state the journal is "the single source of truth for all observability," and the bundle/replay/context-pack/validation tooling is all built on that premise. None of the three write paths honors it. The default `debug="light"` backend is a 10,000-record ring (verified in `journal_factory.py:27`) fed ~200–300 records/second by the audio stages, so it wraps in ~40 seconds — and the single `BufferOverflow` marker is itself evicted because `_overflow_pending` is never reset (verified at `journal_memory.py:212`). The `debug="full"` backend that production docs recommend COMMITs per record on the asyncio loop. And the write filter destroys the payload: I ran it and confirmed `"the meeting is on 2026-07-26"` → `"the meeting is on [REDACTED_PHONE]"`, `"your order number is 4419 8823 1"` → `[REDACTED_PHONE]`, URLs → `[REDACTED_URL]` — twelve lines above a docstring that says *"Normal `data["text"]` stays intact for replay/debuggability."* Then `easycat replay` doesn't re-execute anything, `export_debug_bundle` materializes the session in RAM before checking its cap, and `debug/export.py` reads private attributes of two concrete stores so a custom `ArtifactStore` exports zero artifacts silently.

**Explains:** 19, 48, 54, 55, 56, 58, 59, 60, 61, 62, 63.

**Structural fix.** Decide what the journal *is*. If it is a debug ring, stop claiming completeness, stop paying SQLite per frame, and size the ring in wall-clock seconds rather than records. If it is a record, batch commits to turn boundaries and move redaction from write time to read/export time — where it belongs, since the export path already has a redaction pass with a post-write assertion.

---

### T4 — Decomposition by delegation: the collaborator split moved code without moving authority

**Mechanism.** `session/` was split into named collaborators, but `SessionWiringContext` (`_wiring.py:74-127`) hands all five of them ~24 live closures over the Session — including `emit`, `stop`, `cancel_turn`, `clear_turn`, `reset_turn_state`, `set_running` (verified). Nobody's authority was narrowed; the god object just acquired six masks. Consequences follow mechanically: the turn pointer is written from eight sites across two modules, forcing six hand-written generation guards; teardown ordering is re-listed four times; `session/` grew 55% since the "decomposition" while `_session.py` fell only 1761→1511. Twenty-two files sit on the project's own complexity-gate grandfather list in `pyproject.toml` (verified), eleven of them in `session/`, `stages/`, and `integrations/agents/` — i.e. the gate is waived for exactly the code that most needs it. The `_wiring` closures are also the reason `Session.agent` has a resyncing setter and `stt`/`tts`/`vad`/`transport` do not: nothing owns the invariant.

**Explains:** 14, 15, 16, 17, 18, 20, 21, 22, 23, 26, 27, 28, 115.

**Structural fix.** One owner for turn state (a small `TurnStore` that collaborators read and only `_turn_runner` writes), one teardown sequence parameterized by `force`, and replace the closure bundle with narrow per-collaborator interfaces. Then take files off the grandfather list one at a time; a permanent waiver list is a gate that has been turned off.

---

### T5 — The meta-layer became a second product competing for the same maintenance budget

**Mechanism.** Verified counts: 117,677 words of `docs/` + 138,526 words of `plan/` = ~256k words; 12,957 LOC of tutorial Python across 83 files; two overlapping tutorial ladders (teaching 77,934 words, using-easycat 19,426); ~15.6k LOC of prose-guard tests; a 41-entry docs route registry occupying 63% of `cli/_app.py` (1,256 lines); and `src/easycat/validation/` — 4,996 LOC of repo-CI orchestration **shipped in the wheel**, hard-coding this repo's pytest node IDs (`provider_reports.py:92` names `tests/stt/test_stt_openai.py::test_live_openai_stt`) and shelling out to `uv run pytest` in an installed user's cwd. The irony that makes this a theme rather than a taste complaint: all this machinery guards four internal representations of the docs map (route registry, llms.txt, llms-full.txt, command hints) while the **fifth — `mkdocs.yml`, the one that renders the public site — is unguarded and has drifted**. I diffed it: 27 of 80 pages are missing from nav, including the entire 25-file `using-easycat` ladder and `deployment/production-servers.md`.

**Explains:** 37, 83, 85, 86, 91, 92, 93, 94, 95, 96.

**Structural fix.** Move `validation/` out of `src/` into `scripts/`. Delete one tutorial ladder. Replace prose assertions with value assertions (the `docs/latency.md` table test is the model — it parses Markdown and asserts against live dataclass fields; that one earns its keep completely). Guard `mkdocs.yml` nav coverage with a five-line test. Every representation of the docs map that is not rendered to a user is overhead.

---

### T6 — Security rigor applied per-module rather than per-boundary

**Mechanism.** `enforce_bind_guard` is a genuinely well-argued piece of work, and `grep -rln` finds it in exactly five files — all under `server/` (verified). The transports that bind their own sockets never pass through it. `TwilioTransportConfig` defaults `host="0.0.0.0"`, `port=8766`, `stream_token_validator=None` — and `_twilio_stream_token_valid` returns `True` when the validator is `None`, i.e. accept-all (verified at `twilio_media.py:142,225`). `webtransport.py` contains zero occurrences of `token` or `auth` (verified) and defaults to `0.0.0.0:4433`. Meanwhile `docs/deployment/production-servers.md:88-93` tells operators non-loopback binds "fail closed." Same pattern in the small: `EasyConfig`/`OpenAISTTConfig` repr the API key in plaintext (verified: `OpenAISTTConfig(api_key='sk-SECRET123', ...)`) while the Twilio token *in the same file* is `repr=False`. The guard exists at the layer the maintainer was thinking about, not at the layer where sockets actually open.

**Explains:** 36, 65, 69, 72, 75, 76, 77, 78, 79, 80.

**Structural fix.** Push the guard down into a `bind()` primitive every transport must call — so a new transport cannot open a socket without passing the check — rather than into the server layer that only some transports traverse. Add one test asserting no config dataclass reprs a field whose name matches `key|token|secret`.

---

### T7 — Defaults chosen for constructibility, not for the common case, and never tested for effect

**Mechanism.** A recurring shape: the shipped default is the inert or unsafe branch, and tests assert the default's *value* rather than its *behavior*. `PauseProcessor` defaults to `style="ssml"` — the one style that does nothing with all four bundled TTS providers. `resample` defaults to `linear` because nobody declared the good backend. `debug="light"` defaults to a ring that silently truncates. `WebRTCTransportConfig` omits `default_echo_cancellation_enabled` so a config-built session gets AEC off while an instance-built one gets it on (verified: present at `webrtc.py:128`, absent from `_webrtc_config.py`). `stream_token_validator=None` means accept-all. `EasyConfig.mic()` is byte-identical to `EasyConfig()`. Constructing an `EasyConfig` mutates process-global logging as a *side effect of the default*. In each case a test asserting "default is X" passes; no test asserts "with the default, a pause is audible / a tone is not aliased / a 5-minute call is fully journaled."

**Explains:** 1, 2, 6, 9, 30, 38, 44, 55, 78.

**Structural fix.** For every default that selects a behavior, add a test that asserts the *effect*, not the value. Where the safe branch and the constructible branch differ, make the safe one the default and the loose one explicit.

---

## The ten that matter most

| # | Finding(s) | Why it ranks here |
|---|---|---|
| 1 | **#44 / #70** — resampler always runs the unfiltered linear fallback | Highest ratio in the entire report: one line in `pyproject.toml` (`soxr>=0.5`) fixes measured 0 dB in-band aliasing on every browser mic path and every Twilio TTS leg, for every install that exists. Also removes ~21 ms/call/sec of CPU and the 122 ms smart-turn tax (#49). |
| 2 | **#97** — `required-version = "==0.11.29"` | Blocks every `just`/`uv` command on any machine whose uv differs — reproduced on this box today. Deleting one line costs nothing; `uv sync --locked` already enforces the property it was added for. |
| 3 | **#56** — write filter mangles transcripts into `[REDACTED_*]` | Default config, silent, and it destroys precisely the payload the journal exists to hold. I confirmed dates and order numbers become `[REDACTED_PHONE]` twelve lines below a docstring promising the opposite. Fix is a key allowlist in `safe_defaults.py:349`. |
| 4 | **#55 / #48** — default journal ring wraps at ~40 s with a self-evicting marker | Default config; every bundle from a call longer than a minute is a truncated tail indistinguishable from a complete one, while docs assert completeness twice. Two small fixes (reset `_overflow_pending`, size by seconds) restore the framework's flagship claim. |
| 5 | **#78 / #76 / #65** — telephony ingress binds `0.0.0.0` with accept-all token validation; the "production starting point" example makes signature validation optional | Real money and real PII: unauthenticated billable sessions, attacker-chosen `CallIdentity` flowing into agent context, forgeable `POST /status` call termination. The hardened path already exists in `telephony/server.py` — the defaults and the example just don't use it. |
| 6 | **#110 / #89** — no CI job runs a bridge against its SDK; the one job with SDKs points at the wrong directory | A one-line workflow change (`tests/contracts` → `tests/contracts tests/integrations`) makes 25 already-written compatibility gates fire for the first time. Cheapest real coverage available anywhere in the repo, and it is the mechanism by which #107, #111, #112, #114 went unnoticed. |
| 7 | **#66** — `max_call_duration_s` never ends a call | Verified: `_max_duration_coro` transitions the state enum and emits `CallEnded`, then returns — the phone leg stays up. The single documented cost circuit-breaker on outbound calling is a no-op, and operators will believe it works. |
| 8 | **#24 / #25 / #68** — barge-in runs inline on the ingress task, waits for playback drain before clearing, and is a literal no-op on WebSocket | Barge-in is the headline behavior of a voice framework. Measured 1.5–5.5 s of the bot talking over the caller *with mic capture stalled*, and on WebSocket it never stops at all — behind a docstring (verified at `websocket.py:167`) that tells the next maintainer there's nothing to fix. |
| 9 | **#27 / #26** — `stop(force=True)` is a no-op when a graceful stop is hung; `try_send_first_audio_inline` is permanently uncancellable | Verified: `stop()` returns at the `_stopping` check before `force` is ever consulted. The documented escape hatch for a wedged provider doesn't work in exactly the case its docstring names, and a half-open socket makes the session unstoppable short of SIGKILL. |
| 10 | **#107** — PydanticAI bridge overwrites the *previous* turn's assistant message on a raced barge-in | Silent conversation-history corruption; every later turn conditions on a fabricated transcript and users will blame the model. The correct guard already exists in `langchain.py` and `langgraph.py` — it just wasn't copied (theme T4/#115). |

*Narrowly missed:* **#91** (27/80 docs off the site nav — moved to quick wins, it's a 5-line test), **#98** (PortAudio; 12 nights of red CI whose signal is now noise), **#33/#34** (the documented `isinstance(x, Protocol)` conformance check — I confirmed a class whose members are all integers passes it).

---

## Quick wins

Small changes with outsized payoff.

1. **Add `soxr>=0.5.0` to base `dependencies`** — `pyproject.toml:33-45`. Fixes #44 and #70 for every install; removes the aliasing, the boundary discontinuity, and most of the smart-turn resampler tax (#49). Highest-leverage single line in the report.
2. **Delete `required-version = "==0.11.29"`** — `pyproject.toml:207`. Unblocks every `just`/`uv` command on any machine (#97). `uv sync --locked` in CI already enforces lock integrity.
3. **Point the nightly extras job at the tests that have gates** — `.github/workflows/nightly-validation.yml:194`: `pytest tests/contracts` → `pytest tests/contracts tests/integrations`. Makes 25 dormant real-SDK gates execute (#89, #110).
4. **Exempt transcript keys from the write filter** — `src/easycat/runtime/safe_defaults.py:349` (`apply_write_filter`): skip `text`/`transcript`/`partial` keys, and rely on the export-time redaction that already exists in `cli/debug/export.py`. Fixes #56.
5. **Reset the overflow marker and size the ring by time** — `src/easycat/runtime/journal_memory.py:212` (`_overflow_pending = False` after append) and `src/easycat/runtime/journal_factory.py:27` (raise light-mode capacity, or drop per-frame audio records from `light`). Fixes #55/#48.
6. **`repr=False` on every secret field** — `src/easycat/config/easy.py:460`, `src/easycat/stt/openai_provider.py:48` and the sibling provider configs; add one test asserting no config repr contains a field matching `key|token|secret`. Fixes #77 and prevents regression.
7. **Guard mkdocs nav coverage** — new 5-line test in `tests/docs/`: every `docs/**/*.md` appears in `mkdocs.yml`. Then add the 27 missing pages. Fixes #91 and stops the class.
8. **Honor `force` when a graceful stop is in flight** — `src/easycat/session/_session.py:1109`: `if self._stopping and not force: return`. Fixes #27, the documented-but-nonexistent escape hatch.
9. **Safe telephony/transport defaults** — `src/easycat/transports/twilio_media.py:142` (`host="127.0.0.1"`), plus add `default_echo_cancellation_enabled = True` to `src/easycat/transports/_webrtc_config.py:32`. Fixes #78 and #1.
10. **Flip the pause default and make it real** — `src/easycat/llm_output_processing.py:75`: `style="ellipsis"`. Fixes #38 (`pause_ms` is currently inert with all four bundled TTS providers, including in the README's own example).
11. **Actually end the call on `max_call_duration_s`** — `src/easycat/telephony/call_state.py:757`: hang up the leg, don't just transition the enum and emit an event. Fixes #66.

---

## Finding index

| # | Sev | Area | Finding |
|---|-----|------|---------|
| 1 | MEDIUM | api-dx | WebRTCTransportConfig omits `default_echo_cancellation_enabled`, so a config-built WebRTC session gets AEC off while an instance-built one gets it on |
| 2 | MEDIUM | api-dx | Constructing an `EasyConfig` mutates process-global logging: `debug="light"` (the default) attaches a stderr handler and sets `propagate = False` on the `easycat` logger |
| 3 | MEDIUM | api-dx | `VoiceApp`, the README's headline entry point, forwards only 5 of `EasyConfig`'s 36 fields and takes them as untyped `**kwargs` |
| 4 | MEDIUM | api-dx | `session_id`, `interruption_mode`, `interruption_latency_compensation_ms`, and `outbound_queue` have no `EasyConfig` counterpart, so reaching them costs every EasyConfig-only capability |
| 5 | MEDIUM | api-dx | A missing `tts=` in a mixed-provider setup is reported as 'Missing API key: OPENAI_API_KEY' |
| 6 | MEDIUM | api-dx | An explicitly-supplied `vad=` is silently discarded when the STT does native endpointing |
| 7 | MEDIUM | api-dx | Top-level allowlist omits types needed to fill `EasyConfig`'s own fields, and `EasyConfigError` is neither exported nor a subclass of the exported `EasyCatError` |
| 8 | LOW | api-dx | `create_session` and the provider catalog produce bare, uninformative runtime errors for type misuse (all three cases are caught by mypy) |
| 9 | LOW | api-dx | `EasyConfig.mic()` is byte-for-byte identical to `EasyConfig()`, yet the README, docs, and every scaffold teach it as the canonical shape |
| 10 | LOW | api-dx | `from easycat import Error` returns an event dataclass, not an exception |
| 11 | LOW | api-dx | `run_session` is the documented path for long-running apps but is not in the top-level allowlist, while `run` is |
| 12 | LOW | api-dx | `create_text_session` carries a config-or-12-loose-kwargs dual calling convention with a hand-maintained default table |
| 13 | LOW | api-dx | `require_env` raises `SystemExit` from a top-level-exported library helper |
| 14 | HIGH | architecture | Session.stop() is a 148-line, cc-19 method with two divergent teardown branches, grandfathered out of the project's own complexity gate |
| 15 | MEDIUM | architecture | Reassigning session.stt/tts/vad/transport half-rewires the pipeline; only `agent` has a resyncing setter |
| 16 | MEDIUM | architecture | cancel_turn, reset_state, and cancel_tts_playback re-list the same teardown sequence three times; reset_state is literally cancel_turn plus two lines |
| 17 | MEDIUM | architecture | stages/agent.py::execute_streaming is a 209-line, cc-24 async generator on the hot path for every agent integration |
| 18 | MEDIUM | architecture | The turn pointer is written from eight call sites in two modules, forcing six hand-written generation guards to compensate |
| 19 | MEDIUM | architecture | debug/export.py collects artifacts by reading private attributes of two concrete stores, so any custom ArtifactStore silently exports zero artifacts |
| 20 | LOW | architecture | session/ imports easycat.config through a deferred import that masks a real module cycle, and config writes four private fields onto Session after construction |
| 21 | LOW | architecture | A NotImplementedError placeholder pinned by its own test, and four late-binding getters that exist for a single test's benefit |
| 22 | LOW | architecture | docs/architecture.md's description of Session.__init__ is verifiably false in both of its claims |
| 23 | LOW | architecture | session/text.py mixes PCM energy detection and interruption-estimation helpers into a sentence-and-markdown module |
| 24 | HIGH | async-concurrency | Barge-in teardown runs inline on the audio-ingress task, so mic capture stops for its whole duration |
| 25 | HIGH | async-concurrency | Barge-in waits for queued/playing bot audio to drain before it clears playback, instead of the other way round |
| 26 | HIGH | async-concurrency | try_send_first_audio_inline makes an untimed transport send permanently uncancellable |
| 27 | MEDIUM | async-concurrency | Session.stop(force=True) is a no-op when a graceful stop is already hung — the exact case its docstring promises to handle |
| 28 | MEDIUM | async-concurrency | STTCommitter.cancel() calls end_stream() with no timeout, while the same file's normal path wraps the identical call in wait_for |
| 29 | MEDIUM | async-concurrency | STTBase holds its lifecycle lock across the provider network write, so end_stream() queues behind a 5 s reconnect window |
| 30 | LOW | async-concurrency | BoundedAudioQueue's BLOCK policy drops a chunk on a lost race instead of re-waiting |
| 31 | LOW | async-concurrency | Three background tasks bypass the repo's own done-callback idiom, so a raising handler dies with no journal record |
| 32 | LOW | async-concurrency | TTSSynthesizer.cancel() swallows provider exceptions with a bare pass, unlike every comparable site |
| 33 | MEDIUM | provider-extensibility | Docs and the generated scaffold advertise `isinstance(x, Protocol)` as the provider conformance check, and it accepts objects that cannot work |
| 34 | MEDIUM | provider-extensibility | `events()` must return a fresh iterator per turn and `commit_segment()->True` obligates a FINAL — neither obligation is written down, and the contract kit tests neither |
| 35 | MEDIUM | provider-extensibility | `EasyConfig`'s native-endpointing detection is a closed isinstance chain over three bundled STT configs; a registered third-party provider silently gets the wrong pipeline |
| 36 | MEDIUM | provider-extensibility | A third-party provider registered with an extra name that is not an importable module pins `/health/ready` to not-ready forever |
| 37 | MEDIUM | provider-extensibility | `model_api_version` in the validation surface tables is hand-maintained and 5 of 9 rows disagree with the config defaults they describe |
| 38 | MEDIUM | provider-extensibility | `PauseProcessor` defaults to `style="ssml"`, so `pause_ms` is inert with all four shipped TTS providers |
| 39 | MEDIUM | provider-extensibility | Providers never map failures into the EasyCat error taxonomy; `EASYCAT_E304`/`E305` are registered, documented, and never raised |
| 40 | MEDIUM | provider-extensibility | The shipped `easycat.testing` contract kit is referenced by zero user-facing docs; extending pages point at in-repo test files a pip user does not have |
| 41 | MEDIUM | provider-extensibility | The provider registry hard-requires a per-provider API-key env var, locking out the local/self-hosted providers the README advertises |
| 42 | MEDIUM | provider-extensibility | VAD, noise reduction, and echo cancellation have no registry at all — and the only provider scaffold EasyCat generates targets VAD |
| 43 | MEDIUM | provider-extensibility | EventBus delivery to injected provider instances depends on undocumented private attribute names, and the documented rule is stale |
| 44 | HIGH | audio-latency | resample() always falls back to unfiltered linear interpolation because neither soxr nor scipy is declared anywhere; integer down-ratios degenerate to raw sample dropping (measured 0 dB in-band aliasing) |
| 45 | MEDIUM | audio-latency | WebRTC AEC far-end reference is re-stamped with the microphone's format, discarding the true reference format the transport already recorded and defeating LiveKitAEC's rate-mismatch guard |
| 46 | MEDIUM | audio-latency | WebSocket and WebTransport default AEC on, but feed the far-end reference at socket-write time with no silence keep-alive — the 1:1 render/capture invariant the local and WebRTC transports go out of their way to maintain |
| 47 | MEDIUM | audio-latency | VAD speech/silence gates measure wall-clock time instead of audio position, so a burst delivery of speech frames emits zero VAD events |
| 48 | MEDIUM | audio-latency | Default debug="light" journal holds only ~30-50 seconds of a call because audio stages emit ~200-300 records/second, and the single eviction marker is itself evicted |
| 49 | MEDIUM | audio-latency | Smart-turn burns ~122 ms per endpoint decision in the pure-Python resampler, on the path whose entire purpose is saving the 500 ms silence timer |
| 50 | MEDIUM | audio-latency | pre_roll_ms (300) is not coupled to VADConfig.min_speech_duration_ms (250); raising the VAD gate as the docs advise silently truncates the start of every utterance |
| 51 | MEDIUM | audio-latency | Barge-in playback cutoff is the last step in cancel_turn, behind an unbounded application event handler and a TTS provider socket teardown |
| 52 | LOW | audio-latency | LocalTransport's output jitter pre-roll is a hardcoded module constant, re-armed after every barge-in and absent from the documented latency table |
| 53 | LOW | audio-latency | Dead _split_frames helper implements the exact zero-padding the AEC class documents as harmful, and the module docstring states the opposite pipeline order from the code |
| 54 | HIGH | errors-observability | SqliteJournal COMMITs on every append from the asyncio audio loop, ~4-6× per 20 ms frame, with ~17× WAL write amplification and no mid-session checkpoint |
| 55 | HIGH | errors-observability | The default journal (debug="light") silently discards the session after ~40 seconds with no marker, counter, or flag |
| 56 | HIGH | errors-observability | Journal write-filter silently mangles ordinary transcripts — dates, order numbers, and URLs become [REDACTED_*] at write time with no opt-out |
| 57 | MEDIUM | errors-observability | Five registered error codes have zero raise sites, and runtime timeout errors sit outside the EasyCatError hierarchy |
| 58 | MEDIUM | errors-observability | `easycat replay` walks records without re-executing anything: `--timing wall` is inert, `Stage.replay` has no shipped caller, and EASYCAT_E403 is unreachable |
| 59 | MEDIUM | errors-observability | A crashed journal whose PID has been reused is permanently classified "live", and the docstring documents a backstop the code short-circuits |
| 60 | MEDIUM | errors-observability | `export_debug_bundle` materializes the entire session in RAM before writing, then fails the 500 MB cap it only checks afterwards |
| 61 | LOW | errors-observability | docs/reference/events.md claims to list "every public EasyCat event type" but omits 23 of 45, including all tool-call, reconnect, and telephony events |
| 62 | LOW | errors-observability | EventBus's slow-handler warning is off by default and unreachable from EasyConfig, the path the docs point at it from |
| 63 | LOW | errors-observability | The coding-agent context pack drops the error message, traceback, and machine-generated notes, leaving only the exception type |
| 64 | LOW | errors-observability | Vendored FunASR VAD writes six anomaly messages to stdout, bypassing the easycat logger, in a file exempted from lint |
| 65 | HIGH | transports-telephony | Twilio media WebSocket builds and starts a billable session before the stream token is validated, and the documented production example has no concurrency cap |
| 66 | HIGH | transports-telephony | max_call_duration_s never ends a call — the timer flips a state enum and emits an event no subscriber acts on |
| 67 | HIGH | transports-telephony | Servers close client WebSockets before the drain window, so a deploy cuts live calls mid-sentence; the Twilio server has no drain window at all |
| 68 | HIGH | transports-telephony | Barge-in is a no-op on both WebSocket transports, justified by a false docstring, and the bundled WS client cannot cancel scheduled audio |
| 69 | MEDIUM | transports-telephony | enforce_bind_guard is documented as covering every transport but WebTransport has zero auth code and defaults to 0.0.0.0 |
| 70 | MEDIUM | transports-telephony | The shipped resampler is always the pure-Python linear fallback: soxr and scipy are declared nowhere, and resample() carries no state across chunks |
| 71 | MEDIUM | transports-telephony | A slow WebSocket client stalls the whole session: browser-event sends are awaited inline in serial EventBus dispatch with no timeout |
| 72 | MEDIUM | transports-telephony | Inbound audio is bounded by frame count, not bytes, and no websockets.serve call sets max_size |
| 73 | LOW | transports-telephony | WebTransport is ~3,400 LOC of protocol plumbing resting on three private aioquic attributes, for a transport the deployment guide already calls niche |
| 74 | LOW | transports-telephony | WebSocketTransport and WebSocketConnectionTransport duplicate the same wire protocol in one file and have already diverged on the disconnect race |
| 75 | HIGH | security | WebTransport server has no authentication mechanism, defaults to 0.0.0.0, and its serve helper skips the library's own bind guard |
| 76 | HIGH | security | examples/twilio_app.py — the file the deployment guide calls the "production starting point" — makes Twilio signature validation optional, leaving POST /twiml a public stream-token minter and POST /status forgeable |
| 77 | MEDIUM | security | repr() of EasyConfig and every provider config prints the API key in plaintext, while the Twilio token in the same dataclass is repr-protected |
| 78 | HIGH | security | TwilioTransportConfig defaults to 0.0.0.0:8766 with stream-token validation off, and EasyConfig.phone() inherits that default |
| 79 | LOW | security | supervisor_message_authorized raises TypeError on a non-ASCII token instead of returning False, bypassing the intended 4401 close |
| 80 | LOW | security | telephony/compliance.py logs raw E.164 phone numbers at WARNING on its most-executed path |
| 81 | MEDIUM | testing | The provider contract tier is a self-test: the shipped ProviderContractSuite kit is never applied to any EasyCat provider, and the CI comment claiming otherwise is false |
| 82 | MEDIUM | testing | Two barge-in tests assert tautologies behind a conditional that silently no-ops, and the idiom is copied 9 times in the file |
| 83 | MEDIUM | testing | tests/teaching is 9,034 LOC / 311 tests / 71s guarding tutorial code, and 832 LOC of it duplicates a check the docs workflow already runs |
| 84 | MEDIUM | testing | ~47s of the default pytest run is pure waste: 15s sleeping out a production timeout and 32s provisioning competitor-framework environments |
| 85 | LOW | testing | 1,016 LOC of docs-command-hint validators and tests-of-validators, plus 176 literal-prose assertions that turn doc edits into CI failures |
| 86 | MEDIUM | testing | The shipped wheel contains 5k LOC of repo-CI orchestration that hard-codes this repo's test node IDs and shells out to `uv run pytest` in the user's cwd |
| 87 | MEDIUM | testing | 57 hand-rolled provider fakes, two contradictory FakeTransport contracts in one directory, and a production compat shim whose drop path no test can reach |
| 88 | MEDIUM | testing | Session tests reach into private collaborators 354 times and assign them directly, freezing the internal decomposition CLAUDE.md documents as the architecture |
| 89 | MEDIUM | testing | 25 real-SDK importorskip gates exist in tests/integrations but no CI job ever installs an SDK alongside them — the one extras job points at a tree with zero gates |
| 90 | MEDIUM | docs-and-meta | README's headline `app.run("browser")` snippet needs the `webrtc` extra the README's own install command omits |
| 91 | MEDIUM | docs-and-meta | mkdocs nav omits 27 of 80 docs pages, including the whole 25-file `using-easycat` ladder, with no test guarding nav coverage |
| 92 | MEDIUM | docs-and-meta | `easycat docs` prints 616 lines by default and ships 91 repo-only commands — including seven raw internal pytest guard invocations compiled into the wheel |
| 93 | LOW | docs-and-meta | plan/roadmap/current-code-status.md, the designated currency gate for the plan tree, is wrong on three inventory claims |
| 94 | LOW | docs-and-meta | Teaching ladder and shipped `easycat console` pin gpt-4o-mini while examples/ was migrated to gpt-5.x |
| 95 | LOW | docs-and-meta | Two overlapping tutorial ladders (97k words, 83 tutorial .py files, 12,957 LOC) maintained in parallel over the same product surface |
| 96 | LOW | docs-and-meta | A committed cross-framework latency harness exists but no results and no comparative positioning are published |
| 97 | HIGH | packaging-deps | `required-version = "==0.11.29"` blocks every uv entry point on any other uv release — reproduced on this machine today |
| 98 | HIGH | packaging-deps | The three extras that install `sounddevice` (`local`, `quickstart`, `all`) have failed the nightly extras matrix for 12+ consecutive nights on undeclared PortAudio, and every error path points the user back at the extra they already installed |
| 99 | MEDIUM | packaging-deps | `pyrnnoise` in the `quickstart` extra drags matplotlib, PyAV, Pillow, fonttools and Jinja2 into the golden-path install for two C-binding symbols |
| 100 | MEDIUM | packaging-deps | Dependency floors track whatever was newest when written, not what the code needs, and no CI job ever resolves against them |
| 101 | MEDIUM | packaging-deps | Release path has never executed: no tags, zero release.yml runs, no CHANGELOG or `__version__`, and the PyPI name is unregistered |
| 102 | LOW | packaging-deps | The `telephony` extra forces FastAPI, uvicorn and python-multipart on library consumers although `src/easycat/telephony/` imports none of them |
| 103 | LOW | packaging-deps | 91% of the base wheel is ONNX model weights that ship regardless of which extras are installed |
| 104 | LOW | packaging-deps | The pytest11 entry point eagerly imports the bundle/journal stack into every pytest process in any environment where easycat is installed |
| 105 | LOW | packaging-deps | Docker base images ride mutable tags and are outside Dependabot, while every GitHub Action is SHA-pinned |
| 106 | LOW | packaging-deps | `easycat[all]` excludes PydanticAI and the exclusion is documented only in a pyproject comment |
| 107 | HIGH | agent-bridges | PydanticAI bridge can skip its history commit on a torn-down turn, so the next apply_interruption() rewrites the *previous* turn's answer |
| 108 | HIGH | agent-bridges | LangGraphBridge uses only LangGraph's synchronous checkpointer API, so async-only savers silently no-op and sync savers block the audio event loop |
| 109 | MEDIUM | agent-bridges | A barge-in on a local LlamaAgents workflow silently discards the workflow Context, and nothing tells the workflow it was cut off |
| 110 | HIGH | agent-bridges | No CI job ever executes a bridge against the SDK it wraps — the shipped contract kit runs only against fakes |
| 111 | MEDIUM | agent-bridges | Bridges differ on tool visibility, structured output, and tool-call identity, with no capability matrix documenting it |
| 112 | MEDIUM | agent-bridges | LlamaAgentsBridge speaks any workflow event carrying one of thirteen generic field names |
| 113 | MEDIUM | agent-bridges | EasyConfig(mcp_servers=...) is silently ignored by the LangChain, LangGraph, and LlamaAgents bridges |
| 114 | MEDIUM | agent-bridges | The LangGraph example speaks the model's intermediate research draft to the caller and streams nothing |
| 115 | LOW | agent-bridges | Bridge boilerplate is duplicated across files rather than shared, and the complexity gate is waived for the whole layer |

---

## Findings in full

### Public API and developer ergonomics

*13 findings — 7 medium · 6 low*

**Assessment.** EasyCat's public surface is unusually well *maintained* — the 92-name allowlist is pinned by tests, the `TYPE_CHECKING` block is verified in sync with the lazy-export registry, `docs/reference/easyconfig.md` is checked field-by-field against the live dataclass, and `import easycat` costs 40 ms with zero provider SDKs loaded. What it is not is *coherent*. The single biggest problem is that there are now four overlapping front doors (`VoiceApp`, `EasyConfig`+`run`, `create_session`+`run_session`, `Session.from_providers`/`SessionConfig`) and each one is missing something the others have: `VoiceApp` — the README's headline entry point and the one with no static typing — forwards only 5 of `EasyConfig`'s 36 fields, so adding a `greeting` forces a rewrite; `EasyConfig` cannot express `session_id`, `interruption_mode`, or `outbound_queue`, so those force a drop to `SessionConfig` and the loss of every string shortcut, credential resolution, and format-alignment behavior; and `SessionConfig` is not a superset — the two configs share only 18 of their 36/39 field names, and those 18 have different types on each side. The package's own docstring names `EasyConfig`+`run` as 'the entry path' while `docs/public-api.md` and the README name `VoiceApp`, so a newcomer gets two different 'the' answers before writing a line. Underneath that, several traced misuse paths fail badly: a missing `tts=` in a mixed-provider setup is reported as 'Missing API key: OPENAI_API_KEY', `create_session(SessionConfig())` yields a raw AttributeError, and passing the top-level-exported `TTSProviderConfig` to `EasyConfig(tts=...)` dies with 'Unsupported TTS configuration type.' naming neither the value nor the fix. Two defaults are outright wrong: constructing an `EasyConfig` mutates process-global logging (`propagate=False`), and `WebRTCTransportConfig` silently defaults echo cancellation off while `WebRTCTransport` defaults it on. Genuinely good: the error catalog with codes and remediations, the fail-loud validation on `Session`/`SessionConfig` and on `VoiceApp`'s shared-live-provider hazard, and the docs-as-tests discipline.

**Done well here:**

- Lazy PEP 562 exports genuinely deliver on their promise: `import easycat` measured at 40 ms of import time / 231 ms wall with no provider, transport, or telephony SDK touched, while still exposing 92 names. `src/easycat/_public_api.py` is a pure data registry with a duplicate-name guard, and `tests/test_public_api.py:136` verifies the `__init__.py` TYPE_CHECKING block matches it exactly in both directions — so IDE autocomplete can never silently drift from the runtime surface. This is a hard problem most frameworks get wrong.
- Docs are enforced as contracts, not prose. `docs/public-api.md`'s 'Top-Level Allowlist' bullets are CI-parsed against `easycat.__all__`, and `docs/reference/easyconfig.md` is checked field-by-field against the live `EasyConfig` dataclass by `tests/docs/test_route_contracts.py::test_easyconfig_reference_tracks_config_fields`. Both pages were verified accurate against the code during this audit — a rarity at this scale.
- Fail-loud construction validation is consistently good where it exists. `Session(SessionConfig())` raises `ValueError: SessionConfig must provide non-noop implementations for: stt, tts, vad, transport, agent` — naming every missing piece at once. `VoiceApp._reject_unknown_mode_kwargs` (voice_app.py:399) turns a `serve_tokn=` typo into an explicit error rather than a silently unauthenticated bind, and `_resolve_serve_token` (voice_app.py:710) refuses non-loopback binds without a token with `unsafe_allow_no_auth` as the only named escape hatch. Dataclass `__post_init__` validators name the offending field and the valid set (`Invalid debug='ful'. Must be one of ['full', 'light', 'off']`).
- `VoiceApp._is_shareable_spec` (voice_app.py:102) catches a genuinely subtle concurrency hazard — reusing a built provider or agent bridge across per-connection sessions — at construction time, with an error that explains both why it is unsafe and the exact remedy (`config_factory`). It correctly excludes `bool`/`int` from the scalar-spec shortcut so a stray `agent=True` fails loudly rather than propagating.
- The `EASYCAT_Exxx` error catalog (`src/easycat/errors.py`) with stable codes, `related` cross-references, remediation strings, and an `easycat explain` CLI is materially better than what comparable frameworks ship. `ProviderCatalog.validate_name` layers `difflib.get_close_matches` on top so `stt="deepram"` suggests `'deepgram'`.
- Provider extensibility is a genuinely small, honest surface: one `ProviderSpec` per backend drives the config-type map, credential env vars, install extras, API-domain redaction, doctor checks, and `provider/model` shortcut parsing — plus an entry-point group so third-party providers are first-class without an import. `docs/extending/tts.md` documents exactly what each metadata field feeds.
- Non-obvious runtime hazards are handled rather than ignored: `create_session` wraps assembly in an `ExitStack` so a partially-built session closes its journal; `install_emergency_export` maintains one process-wide excepthook with a per-session registry instead of un-restorable per-session chaining; `SessionManager.add` releases its key reservation on a failed `start()` without clobbering a replacement. These are the kinds of details that only get written after someone got burned.

#### 1. WebRTCTransportConfig omits `default_echo_cancellation_enabled`, so a config-built WebRTC session gets AEC off while an instance-built one gets it on

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `EasyConfig` derives its echo-cancellation default from `getattr(self.transport, "default_echo_cancellation_enabled", False)`. Every transport config class except `WebRTCTransportConfig` declares that ClassVar, and the WebRTC *transport instance* does declare it. Verified at runtime: `EasyConfig(transport=WebRTCTransportConfig(), openai_api_key='k').echo_cancellation` is `EchoCancellationConfig(enabled=False)` while `EasyConfig(transport=WebRTCTransport(), ...)` is `enabled=True`, and `EasyConfig.browser()` is `True` only because that preset hard-codes `kwargs.setdefault("enable_echo_cancellation", True)` (easy.py:710). Three ways to express one WebRTC deployment, two different answers.

**Impact.** A developer who writes `EasyConfig(agent=a, transport=WebRTCTransportConfig(port=8080))` instead of `EasyConfig.browser(...)` or `VoiceApp(...).run('browser')` gets EasyCat-side AEC off with no signal beyond a `(auto)` suffix in the TTY banner. Practical audio damage is limited because the bundled browser client already asks getUserMedia for echo cancellation (and webrtc.py:120-127 warns that double-processing can itself degrade audio), so the concrete cost is a config surface that contradicts itself and a workaround comment in the planner registry rather than a broken bot for most users. Custom (non-bundled) browser clients that do not set `echoCancellation` do get the loopback.

**Fix.** Add `default_echo_cancellation_enabled: ClassVar[bool] = True` to `WebRTCTransportConfig` in `src/easycat/transports/_webrtc_config.py` (next to the existing `ClassVar` imports), delete the now-redundant `kwargs.setdefault("enable_echo_cancellation", True)` in `EasyConfig.browser` (src/easycat/config/easy.py:710), rename/retarget `test_easycat_config_echo_cancellation_defaults_off_for_other_transports` (tests/config/test_easyconfig_defaults.py:250) to assert Twilio-only, and drop the workaround comment at src/easycat/planning/transport_registry.py:76-85. If the divergence is instead intentional (browser stacks self-cancel), make it explicit: declare `= False` on the config *and* on `WebRTCTransport`, so both paths agree.

**Evidence**

- `src/easycat/transports/_webrtc_config.py:32` — `class WebRTCTransportConfig` declares no `default_echo_cancellation_enabled` ClassVar (verified: grep over the whole transports package finds it on local.py:46, websocket.py:58, webtransport.py:237 — never on the WebRTC config)
- `src/easycat/transports/webrtc.py:128` — `default_echo_cancellation_enabled = True` on the WebRTCTransport *instance*, with a comment calling it a 'Deliberate flip from the prior implicit False default'; the flip was applied to the transport class only
- `src/easycat/config/easy.py:626` — `enable_aec = default_echo_cancellation_enabled(self.transport)`; `runtime/capabilities.py:158` implements it as `getattr(provider_or_config, "default_echo_cancellation_enabled", False)`, so a missing ClassVar silently means False
- `tests/config/test_easyconfig_defaults.py:250` — `test_easycat_config_echo_cancellation_defaults_off_for_other_transports` (line 250, not 248 as originally cited) asserts `EasyConfig(transport=WebRTCTransportConfig()).echo_cancellation == EchoCancellationConfig(enabled=False)`, pinning the divergence in CI
- `src/easycat/planning/transport_registry.py:78` — Maintainer comment already acknowledges the divergence: '``WebRTCTransportConfig``'s own ClassVar is False, but a manifest ``webrtc`` profile routes through ``EasyConfig.browser`` which forces AEC on' — the planner works around it rather than fixing it
- `src/easycat/transports/static/webrtc_client.html:506` — MITIGATION found: the bundled browser client requests `echoCancellation: true` in getUserMedia, so the browser applies its own AEC — this is why the original finding's 'bot self-barges mid-sentence' impact is overstated

*Verified:* Confirmed by reading all four transport config classes and by running `EasyConfig(transport=WebRTCTransportConfig())` vs `EasyConfig(transport=WebRTCTransport())` — False vs True. Corrected the test line number (250, not 248). Downgraded high -> medium: the original impact claim ('bot hears its own TTS, transcript fills with the bot's own words') does not survive `src/easycat/transports/static/webrtc_client.html:506`, which sets `echoCancellation: true`, and the documented paths (`EasyConfig.browser`, `VoiceApp('browser')` -> `_per_connection_factory` -> `EasyConfig.browser`) both force AEC on. The residual defect is a real self-contradicting default surface, not a production audio outage.

#### 2. Constructing an `EasyConfig` mutates process-global logging: `debug="light"` (the default) attaches a stderr handler and sets `propagate = False` on the `easycat` logger

`MEDIUM` · `api-ergonomics` · `effort: small`

**Problem.** `EasyConfig` reads as a pure data object but its `__post_init__` reconfigures the process logger. Verified at runtime: with `logging.basicConfig()` already called by the host, the `easycat` logger goes from `handlers=[NullHandler] propagate=True level=0` to `handlers=[NullHandler, StreamHandler<stderr>] propagate=False level=20` the moment `EasyConfig(openai_api_key='k')` is constructed — nothing started, no session built. Two concerns share one field: `debug` gates both the execution journal (`config/_factory.py:407`) and console-handler installation, so the only way to keep the host's log routing is `debug="off"`, which also drops the journal the architecture docs call the single source of truth.

**Impact.** An embedding application that owns logging (structlog, uvicorn dictConfig, a Sentry handler, JSON-to-stdout) stops receiving `easycat.*` records in its own pipeline as soon as it builds a config, and gets text/Rich on stderr instead. Framework warnings ('LiveKit AEC not available', artifact-store limits, 'record_to requested but debug journaling is disabled') never reach the aggregator. This is documented in docs/observability.md, so it is discoverable rather than silent — but the escape hatch offered there ('do not enable console logging') is only reachable via `debug="off"`, which costs the journal.

**Fix.** Split the coupled concern in `src/easycat/config/easy.py`: add an explicit `console_logging: bool | None = None` field (None = leave process logging untouched) and change `_apply_debug_defaults` (easy.py:640-645) to call `enable_console_logging()` only when it is explicitly True. Leave the process-owning entry points — `easycat.helpers.run` (helpers.py:205), `run_session` (helpers.py:170), `push_to_talk.py:209`, and the CLI — as the callers that opt in via `_enable_console_logging_from_env`. If the field split is too big a change, at minimum stop setting `logger.propagate = False` in `_logging.py:56` when the handler was installed implicitly by a config constructor rather than requested by a process owner.

**Evidence**

- `src/easycat/config/easy.py:471` — `debug: Literal["off", "light", "full"] = "light"` — the debug default is on
- `src/easycat/config/easy.py:616` — `if self.debug in ("light", "full"):` inside `__post_init__`, calling `_apply_debug_defaults()`
- `src/easycat/config/easy.py:643` — `enable_console_logging()` — a global side effect from a dataclass constructor
- `src/easycat/_logging.py:56` — `logger.propagate = False` — host root handlers stop receiving `easycat.*` records
- `src/easycat/config/_factory.py:407` — `if config.debug == "off": return _DebugResources(artifact_store=artifact_store, journal=None)` — confirms the coupling: the only opt-out of console logging also removes the journal entirely
- `docs/observability.md:34` — PARTIAL MITIGATION: the behavior is documented — 'Enabling it also sets propagate=False ... those handlers stop receiving easycat records once console logging is enabled. If you want easycat records in your own root pipeline, do not enable console logging'

*Verified:* Reproduced the exact global-state transition at runtime and confirmed the journal coupling at config/_factory.py:407. Downgraded high -> medium: the original finding calls the failure 'invisible', but docs/observability.md:29-39 documents the `propagate=False` consequence verbatim and names the workaround, and `EASYCAT_LOG_FORMAT=json` (_logging.py:159) still yields machine-parseable output on the installed handler. The design smell (constructor mutates process state; one field owns two concerns) is real and unmitigated.

#### 3. `VoiceApp`, the README's headline entry point, forwards only 5 of `EasyConfig`'s 36 fields and takes them as untyped `**kwargs`

`MEDIUM` · `api-ergonomics` · `effort: medium`

**Problem.** Verified at runtime: `VoiceApp(agent=a, greeting="Hi there")` raises `ValueError: Unknown VoiceApp field(s): ['greeting']`; so do `record_to`, `smart_turn`, `session_actions`, `telephony`, `strip_markdown`, `turn_taking`, `timeouts`, `output_processors`. For browser/websocket/twilio a static `config=` is also rejected (voice_app.py:652), so adding a greeting means rewriting to `VoiceApp(config_factory=lambda t: EasyConfig.browser(transport=t, agent=a, greeting="Hi"))`. Separately, verified with mypy: `VoiceApp(agent=None, prt=8080, debug=123, stt=3.5)` produces **zero** type errors, while `EasyConfig(debug="ful")` on the adjacent line is caught as an `arg-type` error.

**Impact.** The entry point the README leads with is the only one in the package with no IDE autocomplete and no type checking, so the beginner audience gets the least static safety: `VoiceApp(port='9000')` type-checks and passes a `str` into `WebRTCTransportConfig(port=...)`, surfacing as an aiohttp bind error far from the call site. The field cliff is loud rather than silent — the ValueError names the allowed set and the `config=`/`config_factory=` escape hatch — but for server modes it still forces a construction-style rewrite the first time a user wants a greeting or a recording directory.

**Fix.** In `src/easycat/voice_app.py`, replace `**config_kwargs: Any` on `__init__` (line 183) and `**kwargs: Any` on `run`/`serve`/`session` (lines 250, 265, 287) with explicit keyword-only parameters carrying real annotations (`stt: STTConfig | STTProvider | str | None = None`, `tts: ...`, `vad: VADConfig | VADProvider | None = None`, `debug: Literal["off","light","full"] = "light"`, `host: str = "127.0.0.1"`, `port: int = 8080`, `serve_token: str | None = None`, `max_sessions: int | None = None`). Then widen `_FORWARDED_CONFIG_FIELDS` (line 51) to every EasyConfig field that is an immutable spec rather than a live collaborator — `greeting`, `record_to`, `smart_turn`, `strip_markdown`, `turn_taking`, `timeouts` are all safe to re-evaluate per connection — leaving `_LIVE_CAPABLE_FIELDS` (line 99) as the guard for the stateful ones.

**Evidence**

- `src/easycat/voice_app.py:51` — `_FORWARDED_CONFIG_FIELDS = frozenset({"agent", "stt", "tts", "vad", "debug"})` — the complete set forwarded into an EasyConfig preset
- `src/easycat/voice_app.py:183` — `**config_kwargs: Any` on `__init__` — no static types for stt/tts/vad/debug/host/port/serve_token/max_sessions
- `src/easycat/voice_app.py:192` — `unknown = set(config_kwargs) - _ALLOWED_CONFIG_FIELDS` -> ValueError; verified: `VoiceApp(agent=None, greeting='hi')` raises 'Unknown VoiceApp field(s): [\'greeting\']. Allowed high-level fields: [...] For anything else, pass a full `config=` or `config_factory=`.'
- `src/easycat/voice_app.py:287` — `def run(self, mode=None, **kwargs: Any)` — every server field (port, host, serve_token, stream_url, ...) is untyped at the call site too
- `src/easycat/voice_app.py:652` — `_per_connection_factory` rejects a static `config=` for browser/websocket/twilio, so for server modes the only escape from the 5-field cliff is `config_factory`
- `src/easycat/voice_app.py:344` — MITIGATION: `_local_config` DOES accept a static `config=` for local mode, so the cliff applies to the three server modes only

*Verified:* Confirmed the forward set, the ValueError text, and the `config=` rejection for server modes by reading voice_app.py and running the constructor; confirmed the mypy asymmetry by running mypy on a two-line file (VoiceApp: 0 errors, EasyConfig: 1 arg-type error). Corrected two overstatements: local mode does accept a static `config=` (voice_app.py:344-352), and the error message already names the escape hatch. Downgraded high -> medium because every failure here is loud at construction with a remediation string, not silent. Dropped the original recommendation's option (c) 'delete VoiceApp' — it is not an actionable diff and contradicts docs/public-api.md:17, which designates VoiceApp the app-first entry point.

#### 4. `session_id`, `interruption_mode`, `interruption_latency_compensation_ms`, and `outbound_queue` have no `EasyConfig` counterpart, so reaching them costs every EasyConfig-only capability

`MEDIUM` · `api-ergonomics` · `effort: small`

**Problem.** `docs/from-easyconfig-to-session.md` presents `SessionConfig` as the escape hatch for raw pipeline fields, but SessionConfig is not a superset of EasyConfig. Measured against the live dataclasses: EasyConfig has 36 fields, SessionConfig 39, and they share only 18 names. Dropping to `Session(SessionConfig(...))` to set `interruption_mode="message"` costs `stt="deepgram/flux"` string shortcuts, `openai_api_key` env resolution, `auto_align_tts_output_to_transport`, smart-turn normalization, telephony wiring, journal creation, `record_to`, and the debugger — 18 EasyConfig-exclusive fields in total. `session_id` is the sharpest case: `TextSessionConfig` has it (config/easy.py:749) and `create_text_session` accepts it, while an audio session cannot set it at any rung short of hand-building `SessionConfig`.

**Impact.** A team that measures a 250 ms browser playback lag and wants `interruption_latency_compensation_ms=250` must reimplement `_resolve_audio_pipeline` (~100 lines of config/_factory.py) and silently loses TTS/transport format alignment in the process. A telephony operator who wants journal rows correlated with a Twilio CallSid has no supported path — `create_session` hard-codes a random id before the journal is created — though `export_debug_bundle(session, path)` does let them choose the bundle filename, and `session.session_id` can be reassigned before `start()` as an unsupported workaround. This is the 'graduate one rung' promise failing where users need it.

**Fix.** Add `session_id: str | None = None`, `interruption_mode: Literal["truncate", "message"] = "truncate"`, `interruption_latency_compensation_ms: int = 0`, and `outbound_queue: BoundedAudioQueue | None = None` to `EasyConfig` in `src/easycat/config/easy.py:480-528`, thread them through `_make_session_config` in `src/easycat/config/_factory.py:539-570`, and change `create_session` (config/_factory.py:711) to `session_id = config.session_id or f"session-{uuid4().hex[:12]}"` so the journal is created with the caller's id. `session_id` already has a path-traversal validator (`_validate_common`, easy.py:159) to reuse. Pin the rule with a test asserting that every non-collaborator SessionConfig field has an EasyConfig counterpart.

**Evidence**

- `src/easycat/config/_factory.py:711` — `session_id = f"session-{uuid4().hex[:12]}"` — unconditional; `create_session(config: EasyConfig)` has no session_id parameter and EasyConfig has no such field
- `src/easycat/config/_factory.py:539` — `_make_session_config` (lines 539-570) builds the SessionConfig; `interruption_mode`, `interruption_latency_compensation_ms`, and `outbound_queue` are simply never passed, so they always take their SessionConfig defaults
- `src/easycat/session/_types.py:180` — `interruption_mode: Literal["truncate", "message"] = "truncate"` with a 6-line docstring explaining the model-compatibility tradeoff — SessionConfig-only
- `src/easycat/session/_types.py:184` — `interruption_latency_compensation_ms: int = 0` — the network/playback latency budget; SessionConfig-only
- `src/easycat/session/_types.py:155` — `outbound_queue: BoundedAudioQueue | None = None` — the documented way to get BLOCK backpressure instead of the default DROP_NEWEST; SessionConfig-only
- `src/easycat/session/_session.py:276` — PARTIAL MITIGATION: `self.session_id = cfg.session_id or f"session-{uuid4().hex[:12]}"` is a plain public attribute, and line 693 reads it at record_to export time — so post-`create_session` assignment renames auto-exported bundles, though journal rows keep the id baked in at `create_journal(...)` time

*Verified:* Verified the field counts by reflection (36 / 39 / 18 shared, matching the finding exactly), read `_make_session_config` to confirm the four fields are never forwarded, and confirmed `create_session` hard-codes the id. Downgraded high -> medium after finding two mitigations the original missed: `session.session_id` is a public mutable attribute read at record_to export time (session/_session.py:276, 693), and `export_debug_bundle(session, path)` takes a caller-chosen path (debug/export.py:58), so bundle filenames are not in fact uncorrelatable. The `interruption_*` and `outbound_queue` half has no workaround short of hand-building SessionConfig and stands as described.

#### 5. A missing `tts=` in a mixed-provider setup is reported as 'Missing API key: OPENAI_API_KEY'

`MEDIUM` · `api-ergonomics` · `effort: small`

**Problem.** Verified with `OPENAI_API_KEY` unset: `EasyConfig(stt=DeepgramSTTConfig(api_key='dg'))` raises `EasyCatError: EASYCAT_E203: Missing API key: OPENAI_API_KEY` with the remediation 'Set the env var: export OPENAI_API_KEY=...'. The actual mistake is a missing `tts=`. The message names a credential the user deliberately does not have and prescribes a fix that will not resolve their situation (they want a non-OpenAI TTS, not an OpenAI key). The second case is narrower than originally described: `EasyConfig(stt=DeepgramSTTConfig(), openai_api_key='k')` raises the bare `ValueError: deepgram STT requires an API key.` — but that path is only reached for hand-constructed provider configs; the string-shortcut path already raises the coded error naming `DEEPGRAM_API_KEY`.

**Impact.** Mixed providers ('Deepgram STT with ElevenLabs TTS') is a headline capability, and this is the exact first-run error on that path. The user follows the remediation, sets an OpenAI key they did not want, then hits the real problem — or reads the source. The error is loud and recoverable, so this is a message-quality defect rather than a broken behavior, but it lands on the first two minutes of the mixed-provider experience and it undercuts the credibility the coded error catalog buys elsewhere.

**Fix.** In `EasyConfig._validate` (src/easycat/config/easy.py:648-670), split the first check: when `self.tts is None` (resp. `self.stt is None`) and no OpenAI key is available, raise an error that names the unfilled field first — e.g. 'no `tts=` configured; either pass tts=... (string shortcut, config dataclass, or provider instance) or set OPENAI_API_KEY so the default OpenAI TTS can be wired'. For the per-provider branch at line 663, replace the bare `ValueError` with `EASYCAT_E203(var=catalog.env_vars[name])`, reusing the catalog lookup that `_provider_display_name` (easy.py:297) already performs — it iterates `catalog.providers` and has the provider name in hand.

**Evidence**

- `src/easycat/config/easy.py:652` — `if (self.stt is None or self.tts is None) and not self.openai_api_key: raise EASYCAT_E203(var="OPENAI_API_KEY")` — an unfilled *provider slot* is reported as a missing *credential*
- `src/easycat/config/easy.py:663` — `if hasattr(cfg, "api_key") and not cfg.api_key: raise ValueError(f"{name} requires an API key.")` — no env-var name, no error code, no remediation
- `src/easycat/config/easy.py:665` — In-code comment concedes 'there is no (cfg, kind) -> env-var helper today'
- `src/easycat/_provider_catalog.py:65` — `env_vars` maps every registered provider name to its credential env var; `_provider_display_name` (easy.py:297-315) already walks the same catalog to get the display name, so the lookup is one dict access away
- `src/easycat/_provider_catalog.py:214` — MITIGATION: the shortcut-string path already does it right — `parse_string` raises `EASYCAT_E203(var=env_var)`; verified, `EasyConfig(stt='deepgram/flux')` with no key raises 'Missing API key: DEEPGRAM_API_KEY'. Only explicitly-constructed config dataclasses with an empty api_key hit the bare ValueError.

*Verified:* Reproduced both errors verbatim with `OPENAI_API_KEY` unset. Downgraded high -> medium: it is a confusing-but-loud construction-time error, not a correctness or data-loss bug, and the original finding's second case is narrower than claimed — `_provider_catalog.py:214` already raises the correctly-named `EASYCAT_E203` for the shortcut-string path (verified: `stt='deepgram/flux'` -> 'Missing API key: DEEPGRAM_API_KEY'), so only hand-built config dataclasses hit the bare ValueError.

#### 6. An explicitly-supplied `vad=` is silently discarded when the STT does native endpointing

`MEDIUM` · `correctness` · `effort: small`

**Problem.** Verified: `EasyConfig(stt='deepgram/flux', vad=VADConfig(backend='silero', min_speech_duration_ms=300))` keeps the caller's VADConfig on the config object, but `_should_auto_turn_from_stt_final(config)` returns True, so `_resolve_audio_pipeline` builds `vad=None, enable_vad=False`. Nothing is logged during resolution. The decision is right in intent (avoiding double endpointing and duplicate FINALs), but a value the user explicitly typed is discarded with zero acknowledgement, and the reasoning lives only in a private function's docstring.

**Impact.** A developer tuning barge-in sensitivity edits `min_speech_duration_ms`, observes no change, and has no discoverable way to learn VAD is not running: the config object still reports their VADConfig after construction, `create_session` prints nothing, the `run()` banner has no VAD field, and no maintained doc mentions that native-endpointing STTs disable the VAD stage. Diagnosis requires reading `_should_auto_turn_from_stt_final` in a private module.

**Fix.** In `_resolve_audio_pipeline` (src/easycat/config/_factory.py:421-426), log a WARNING when `auto_turn_from_stt_final` is True and `config.vad != VADConfig()`, naming the STT that triggered it and the three ways to force EasyCat VAD back on that `_should_auto_turn_from_stt_final` already checks (push-to-talk `turn_taking.mode`, `smart_turn=True`, or a non-endpointing STT). Add a VAD field to `_wired_summary` (src/easycat/helpers.py:236-271) reading the same predicate, e.g. `vad=off (deepgram/flux native endpointing)`. Add the caveat to the `vad` bullet in docs/reference/easyconfig.md:72.

**Evidence**

- `src/easycat/config/_factory.py:424` — `auto_turn_from_stt_final = _should_auto_turn_from_stt_final(config)`
- `src/easycat/config/_factory.py:426` — `vad = _create_vad(config.vad) if enable_vad else None` — the user's VADConfig is dropped with no log line and no record on the config
- `src/easycat/config/easy.py:176` — `_stt_uses_native_endpointing` covers Deepgram Flux, Cartesia ink-2, and ElevenLabs realtime with the VAD commit strategy
- `src/easycat/helpers.py:236` — `_wired_summary` — the `run()` TTY banner reports stt/tts/transport/noise-reduction/echo-cancel and has no VAD line at all, so the resolved decision is invisible even on the interactive path
- `docs/reference/easyconfig.md:72` — Documents `vad` as a real field ('VADConfig or a live VADProvider; backend auto-resolves Silero -> FunASR -> TEN -> Krisp') with no note that some STT choices void it; grepping all of docs/ and README.md for `enable_vad` / `auto_turn_from_stt_final` returns nothing

*Verified:* Reproduced: the config retains the caller's VADConfig while `_should_auto_turn_from_stt_final` returns True, and reading `_wired_summary` confirms there is no VAD line in the banner. Grepped all of docs/ and README.md for `enable_vad`, `auto_turn_from_stt_final`, and 'flux' — no doc anywhere states that a native-endpointing STT disables the VAD stage. Severity kept at medium: bounded, no wrong output, but genuinely undiscoverable.

#### 7. Top-level allowlist omits types needed to fill `EasyConfig`'s own fields, and `EasyConfigError` is neither exported nor a subclass of the exported `EasyCatError`

`MEDIUM` · `api-ergonomics` · `effort: medium`

**Problem.** Verified against the live dataclass and `easycat.__all__` (92 names): of the types named in `EasyConfig` field annotations, `EchoCanceller, NoiseReducer, NoiseReducerConfig, STTProvider, TTSProvider, VADConfig, VADProvider, SessionActions, SmartTurnConfig, TelephonyConfig, TurnManagerConfig` are exported while `EchoCancellationConfig, TimeoutConfig, AgentRunnerConfig, SessionActionExecutor, LLMOutputProcessor, DNCStore, TransportConfig` are not — so `EasyConfig(vad=VADConfig(...))` is a one-line top-level import and `EasyConfig(timeouts=TimeoutConfig(...))` requires `from easycat.timeouts import TimeoutConfig`, with no stated principle separating them. docs/public-api.md:15-40 lists seven rules and none of them decides these cases. The exception surface is split three ways with no common base: `EasyConfig.__post_init__` alone can raise plain `ValueError` (easy.py:655), the unexported `EasyConfigError` (easy.py:107), and the exported `EasyCatError` via `EASYCAT_E203` (easy.py:653).

**Impact.** There is no single `except` clause that catches EasyCat configuration failures from a top-level import: `except EasyCatError` misses `EasyConfigError` and the plain `ValueError`s, and `EasyConfigError` is not importable from the package root at all, so callers must write `except (ValueError, EasyCatError)` and swallow unrelated ValueErrors. The export asymmetries are a lesser cost: users cannot predict what `from easycat import X` covers, so they either guess wrong or defensively import from submodules, defeating the allowlist's purpose.

**Fix.** Make `EasyConfigError` subclass both bases — `class EasyConfigError(EasyCatError, ValueError)` in src/easycat/config/easy.py:107 — so one `except EasyCatError` covers configuration failures while existing `except ValueError` callers keep working, and convert the two remaining plain `ValueError` raises in `_validate` (easy.py:655, 657, 668) to it. Then add `EasyConfigError`, `TextSessionConfig`, `EchoCancellationConfig`, `TimeoutConfig`, `AgentRunnerConfig`, and `TwilioTransportConfig` to `src/easycat/_public_api.py`, update the `Top-Level Allowlist` bullets in docs/public-api.md (CI parses that section) and `PUBLIC_API_SNAPSHOT` in tests/test_public_api.py, and state the governing rule in the docs/public-api.md Rules section: every type named in an `EasyConfig` field annotation is top-level exported. Export the websocket/webtransport/twilio `run_*`/`serve_*` helpers alongside the WebRTC pair or drop the WebRTC pair.

**Evidence**

- `src/easycat/config/easy.py:107` — `class EasyConfigError(ValueError)` — verified MRO is (EasyConfigError, ValueError, Exception, BaseException); it does NOT subclass `EasyCatError`, whose MRO is (EasyCatError, Exception, BaseException)
- `src/easycat/config/__init__.py:45` — `EasyConfigError` and `TextSessionConfig` are both in `easycat.config.__all__`; verified neither is in `easycat.__all__` (92 names)
- `src/easycat/config/easy.py:511` — `echo_cancellation: EchoCancellationConfig | EchoCanceller | None` — `EchoCanceller` is exported, `EchoCancellationConfig` is not
- `src/easycat/config/easy.py:518` — `timeouts: TimeoutConfig` — `TimeoutConfig` is not exported, while the sibling `turn_taking: TurnManagerConfig` is
- `src/easycat/config/easy.py:461` — `agent_runner: AgentRunnerConfig | None = None` — `AgentRunnerConfig` is not exported (line 461, not 464 as originally cited)
- `src/easycat/_public_api.py:129` — Transport block exports LocalTransportConfig, WebRTCTransportConfig, WebSocketTransportConfig, WebTransportTransportConfig — verified `TwilioTransportConfig` (the fifth member of the same union) is absent, while `TwilioConnectionTransport` is present
- `src/easycat/_public_api.py:134` — `run_webrtc_config_server` / `serve_webrtc_config_sessions` are exported; the identical websocket / webtransport / twilio server helpers are not

*Verified:* Verified every export claim by reflection against `easycat.__all__` and the MRO claim by `issubclass(EasyConfigError, EasyCatError) == False`. Corrected the `agent_runner` line number (461, not 464) and the `config/__init__.py` line (45, the `__all__` list, not 42, a comment). Kept at medium: the exception-hierarchy half is a concrete, actionable defect; the allowlist-curation half is a consistency critique with no functional failure, so I led the recommendation with the exception fix. Note the finding's original claim that `run_webrtc_config_server` is exported 'while niche internals like WebTransportServer are' checks out but is presentation, not evidence of harm.

#### 8. `create_session` and the provider catalog produce bare, uninformative runtime errors for type misuse (all three cases are caught by mypy)

`LOW` · `api-ergonomics` · `effort: small`

**Problem.** Three misuse paths traced end to end at runtime: (1) `create_session(SessionConfig())` -> `AttributeError: 'SessionConfig' object has no attribute 'debug'`; (2) `EasyConfig(tts=TTSProviderConfig(provider='openai', api_key='k'))` constructs cleanly, passes `_validate`, then dies in `create_session` with `ValueError: Unsupported TTS configuration type.` — no type name, no accepted set, no pointer to `OpenAITTSConfig`; (3) `EasyConfig(stt=3.5)` constructs successfully and fails later with `ValueError: Unsupported STT configuration type.`. However, mypy flags all three at the call site (verified: `arg-type` errors naming `SessionConfig` vs `EasyConfig`, `float` vs the STT union, and `TTSProviderConfig` vs the TTS union), so in this typing-first repo the static checker is the primary line of defense and it holds.

**Impact.** For users who do not run a type checker, each error lands one layer removed from the line they wrote, with a message containing none of the three things needed to fix it (what was passed, what is accepted, what to pass instead). Since the repo ships a coded error catalog with remediations (`easycat explain EASYCAT_E203`), these bare ValueErrors read as an unfinished corner. Bounded: loud, deterministic, at construction/build time, and caught statically.

**Fix.** Change `ProviderCatalog.provider_for_config` (src/easycat/_provider_catalog.py:144-150) to `raise ValueError(f"Unsupported {self.kind} configuration type {config_type.__name__}. Accepted: {sorted(c.__name__ for c in self.config_to_provider)}.")` — the catalog already holds `config_to_provider`, so this is a one-line change that fixes cases (2) and (3). Add a guard at the top of `create_session` (src/easycat/config/_factory.py:711): `if not isinstance(config, EasyConfig): raise EasyConfigError(...)` naming the received type and redirecting `SessionConfig` callers to `Session(SessionConfig(...))` / `Session.from_providers(...)`.

**Evidence**

- `src/easycat/config/_factory.py:690` — `def create_session(config: EasyConfig) -> Session:` — annotation only, no runtime guard; first contact is `config.debug` inside `_create_debug_resources` (line 712)
- `src/easycat/_provider_catalog.py:149` — `raise ValueError(f"Unsupported {self.kind} configuration type.")` — does not name the type it received or the accepted set
- `src/easycat/_public_api.py:42` — `SessionConfig` is a top-level export sitting next to `create_session`, and `TTSProviderConfig`/`STTProviderConfig` (line 56) are exported while the typed configs they shadow (OpenAITTSConfig, DeepgramSTTConfig, ...) are not
- `src/easycat/config/easy.py:648` — `_validate` checks credential presence but never validates the *shape* of `stt`/`tts`, so `EasyConfig(stt=3.5)` constructs cleanly and fails one layer later

*Verified:* Reproduced all three runtime errors verbatim. Downgraded medium -> low after finding the mitigation the original missed: running mypy on the exact three misuse lines produces three `arg-type` errors, so a repo that describes itself as typing-first (CLAUDE.md Style) already catches every case statically. What survives is runtime message quality for untyped callers, which is a real but minor gap — and the catalog fix is a genuine one-liner worth taking.

#### 9. `EasyConfig.mic()` is byte-for-byte identical to `EasyConfig()`, yet the README, docs, and every scaffold teach it as the canonical shape

`LOW` · `over-engineering` · `effort: small`

**Problem.** Verified by field-by-field comparison of the constructed dataclasses: `EasyConfig.mic(openai_api_key='k')` and `EasyConfig(openai_api_key='k')` differ in zero field values, including the smart-turn normalization (which keys off `isinstance(transport, LocalTransportConfig)` — true in both) and the AEC tri-state. Its siblings do carry meaning (`browser()` forces `enable_echo_cancellation=True`, `phone()` swaps in `TwilioTransportConfig`), which makes `mic()`'s emptiness harder to notice.

**Impact.** A new reader must decide whether `EasyConfig.mic(...)` differs from `EasyConfig(...)` and cannot tell from the docstring, which promises 'sensible transport defaults'. That is one avoidable concept in the hello-world path. No behavioral cost, and the preset does complete a three-surface family (mic/browser/phone) that reads consistently and gives the local surface a seam if it ever needs its own defaults.

**Fix.** Do not delete it — 148 call sites across README, docs, examples, scaffold templates, and `VoiceApp._local_config` (voice_app.py:361) make removal high-churn for zero functional gain. Instead, change the `EasyConfig.mic` docstring (src/easycat/config/easy.py:680-689) to say plainly that it is an explicit-intent alias for the constructor default (`EasyConfig()` already uses the local mic transport), naming the contrast with `browser()`, which flips AEC, and `phone()`, which swaps the transport. Optionally drop the redundant `kwargs.setdefault("transport", LocalTransportConfig())` at line 691 so the body is just `return cls(**kwargs)`.

**Evidence**

- `src/easycat/config/easy.py:516` — `transport: TransportConfig = field(default_factory=LocalTransportConfig)` — the constructor default is already the local mic transport
- `src/easycat/config/easy.py:691` — `EasyConfig.mic` body is `kwargs.setdefault("transport", LocalTransportConfig()); return cls(**kwargs)` — a no-op setdefault against the same default (classmethod decorator at 679, body at 690-692)
- `README.md:26` — `run(EasyConfig.mic(agent=...))` is presented as 'the one canonical shape'
- `src/easycat/cli/scaffold/templates/openai-agents/agent.py:16` — `run(EasyConfig.mic(agent=agent, **__EASYCAT_CONFIG_EXTRA__))` — `easycat init` writes it into every new project; 148 occurrences of `EasyConfig.mic` across the repo

*Verified:* Confirmed by reflection: comparing every field of `EasyConfig.mic(openai_api_key='k')` against `EasyConfig(openai_api_key='k')` yields an empty diff list. Downgraded medium -> low and rewrote the recommendation: the original called for deleting `mic()` and rewriting the README, the ladder doc, examples, and two scaffold templates, but `grep -c` shows 148 references and there is no functional defect to fix — an intent-revealing alias completing a preset family is a defensible design, and the actionable residue is a docstring that currently oversells it.

#### 10. `from easycat import Error` returns an event dataclass, not an exception

`LOW` · `naming` · `effort: small`

**Problem.** The bare name `Error` at a package's top level reads as an exception type. Here it is a frozen dataclass event. Writing `try: ... except easycat.Error:` raises `TypeError: catching classes that do not inherit from BaseException is not allowed` at runtime (reproduced). The package simultaneously exports `EasyCatError` (the real exception base) and `ErrorEntry` (a journal record), so three similarly-named top-level symbols mean three unrelated things.

**Impact.** A user reaching for the name that looks like the exception writes an `except` clause that raises a TypeError about their error handling rather than about their bot. Bounded: mypy flags it as 'Exception type must be derived from BaseException' (verified), so anyone type-checking catches it before running, and the confusion costs minutes, not correctness.

**Fix.** Keep the class where it is but stop exporting the bare noun at the package root: remove `"Error"` from the `easycat.events` block in `src/easycat/_public_api.py:90` and export `ErrorEvent` instead, defined in `src/easycat/events.py:437` as the primary name with `Error = ErrorEvent` retained as a module-level alias for `easycat.events` importers during 0.x. Update the `Top-Level Allowlist` bullet in docs/public-api.md:180 and `PUBLIC_API_SNAPSHOT` in tests/test_public_api.py, and add a one-line disambiguation grouping `EasyCatError` / `ErrorEntry` / `ErrorEvent` in docs/public-api.md.

**Evidence**

- `src/easycat/events.py:437` — `class Error(Event)` — a `@dataclass(frozen=True)` wrapping an exception, not a BaseException subclass (verified: `issubclass(easycat.Error, BaseException)` is False)
- `src/easycat/_public_api.py:90` — `"Error"` is registered as a top-level export from `easycat.events`
- `src/easycat/_public_api.py:76` — `_register("easycat.errors", "EasyCatError", "ErrorEntry")` — three top-level names starting with 'Error'/'EasyCatError' with unrelated meanings (event, exception base, journal record)
- `docs/public-api.md:180` — `Error` is listed under 'Events'; `ErrorEntry` / `EasyCatError` appear under 'Debugging, Journals, And Errors' further down

*Verified:* Confirmed the class definition, the export registration, and reproduced the `TypeError` at runtime. Downgraded medium -> low: mypy reports 'Exception type must be derived from BaseException' on `except easycat.Error:` (verified on a 5-line file), so the mistake is statically detectable, and no behavior is wrong — only a name is ambiguous. Kept because the fix is cheap and the three-way 'Error*' collision at the package root is real.

#### 11. `run_session` is the documented path for long-running apps but is not in the top-level allowlist, while `run` is

`LOW` · `api-ergonomics` · `effort: small`

**Problem.** `run` (for a config) is a top-level export; `run_session` (for a prebuilt session) is not, though `attach_runtime_feedback` and `wait_for_shutdown_signal` live in the same module and are exported. The allowlist's stated purpose is 'what `from easycat import ...` means for application code' (docs/public-api.md:3), and its own most production-relevant recipe is the one that breaks the pattern with a two-path import.

**Impact.** Users typing `easycat.` for autocomplete see `run` but not `run_session`, and can reasonably conclude `run(config)` is the only supported entry, then hand-roll signal handling and teardown. Minor: the documented workaround is a one-line submodule import that every example already demonstrates.

**Fix.** Add `"run_session"` to the helpers `_register(...)` call in `src/easycat/_public_api.py:30`, add it to the `TYPE_CHECKING` block in `src/easycat/__init__.py`, add the bullet to the 'App Construction' section of the `Top-Level Allowlist` in docs/public-api.md (CI parses that section), and add it to `PUBLIC_API_SNAPSHOT` in tests/test_public_api.py. Then collapse the mixed import in docs/public-api.md:58-61, docs/from-easyconfig-to-session.md:40-42, docs/reference/session-lifecycle.md:17, and the six examples to one `from easycat import ...` line.

**Evidence**

- `src/easycat/helpers.py:160` — `def run_session(session: Session, *, feedback: FeedbackMode = "auto") -> None:` — public, documented, no leading underscore
- `src/easycat/_public_api.py:30` — The helpers `_register` exports `attach_runtime_feedback`, `require_env`, `run`, `wait_for_shutdown_signal`; verified `run_session` and `create_shutdown_event` are absent from `easycat.__all__`
- `docs/public-api.md:20` — 'Long-running applications should use `create_session` plus `easycat.helpers.run_session`' — the recommended production path requires a submodule import
- `docs/public-api.md:60` — The 'Preferred Imports' block mixes `from easycat import EasyConfig, STTFinal, create_session` with `from easycat.helpers import run_session` in one snippet
- `examples/ws_browser_example.py:40` — One of six examples plus four docs pages that reach into `easycat.helpers` for `run_session` (verified by grep across examples/ and docs/)

*Verified:* Confirmed `run_session` and `create_shutdown_event` are absent from the 92-name `easycat.__all__` while `run`, `attach_runtime_feedback`, `require_env`, and `wait_for_shutdown_signal` are present. Verified the docs/examples import split by grep (10 files). Severity low is correct as filed; no change.

#### 12. `create_text_session` carries a config-or-12-loose-kwargs dual calling convention with a hand-maintained default table

`LOW` · `over-engineering` · `effort: small`

**Problem.** The project is at version 0.1.0 (pyproject.toml:3) and unpublished (README.md:65: 'EasyCat is not published to PyPI yet'), yet `create_text_session` carries a 'legacy loose keyword arguments' back-compat path with a hand-maintained default table, a mutual-exclusion validator, and an 11-line re-unpack — roughly 60 lines across two files supporting a form no external user can have depended on. Its sibling `create_session` accepts only a config object.

**Impact.** Every future `TextSessionConfig` field must be added in four places (dataclass field, `from_kwargs` signature, `from_kwargs` `loose` dict, `create_text_session` signature and unpack). Miss the `loose` dict and the mutual-exclusion check silently stops covering that field; miss the unpack and the value is dropped. That drift risk buys nothing, and the asymmetry with `create_session` costs the reader one extra fact.

**Fix.** Collapse to one form: change the signature to `create_text_session(config: TextSessionConfig | None = None)` in src/easycat/config/_factory.py:879, delete `TextSessionConfig.from_kwargs` (src/easycat/config/easy.py:765-829) and the local re-unpack (_factory.py:931-941), and update the loose-form example at docs/from-easyconfig-to-session.md:113. This is only safe once `TextSessionConfig` is exported at top level (it is in `easycat.config.__all__` but not `easycat.__all__` — verified), otherwise the sole remaining calling form requires a submodule import; sequence it with the export change from the public-API finding.

**Evidence**

- `src/easycat/config/_factory.py:879` — `create_text_session(config=None, *, agent, session_id, debug, journal_backend, journal_retention, warmup, wrap_agent, agent_runner, agent_model, remote_agent_api_key, mcp_servers, record_to)` — 12 loose kwargs duplicating the dataclass fields
- `src/easycat/config/_factory.py:931` — Eleven lines (931-941) of `agent = config.agent; session_id = config.session_id; ...` unpacking the resolved config back into locals
- `src/easycat/config/easy.py:793` — `TextSessionConfig.from_kwargs` maintains a hand-written `loose` dict of (value, default) pairs (lines 793-806) that must track the dataclass fields by hand
- `src/easycat/config/easy.py:810` — Runtime `ValueError` when both forms are supplied — a check unnecessary with a single form
- `src/easycat/config/_factory.py:690` — `create_session(config: EasyConfig)` takes only the config form — the two sibling factories the docs present as a pair disagree on calling convention

*Verified:* Verified the signature, the `loose` dict, the mutual-exclusion raise, and the re-unpack by reading both files; corrected the `from_kwargs` `loose`-dict line number (793, not 765 — 765 is the `@classmethod` decorator). Confirmed version 0.1.0 and the README's not-published-to-PyPI statement. Added the sequencing constraint the original recommendation glossed: `TextSessionConfig` is not currently top-level exported (verified against `easycat.__all__`), so deleting the loose path without exporting it would make the factory unreachable from a package-root import. Severity low is correct.

#### 13. `require_env` raises `SystemExit` from a top-level-exported library helper

`LOW` · `api-ergonomics` · `effort: small`

**Problem.** `SystemExit` inherits from `BaseException`, so it bypasses `except Exception` handlers. An exported helper whose name suggests a lookup ('require_env') instead terminates the process, and nothing in its signature (`-> str`) or its export placement next to `run` signals that. Inside an asyncio connection handler — a per-connection `config_factory`, an event callback, a session helper — a raised `SystemExit` is either swallowed by task machinery (connection hangs, no diagnostic) or tears down the whole server for one bad connection.

**Impact.** A helper presented at the package root as general credential plumbing is safe only at startup. Misplacing it yields either a silently dropped connection or a whole-process exit, both hard to attribute back to a credential lookup. Note this is a latent API-shape hazard, not an observed one: every use in this repo places it correctly at startup.

**Fix.** Change `require_env` in src/easycat/helpers.py:24-34 to raise `EASYCAT_E203(var=name)` — the error catalog's existing missing-credential error, which already carries the same remediation string this helper hand-rolls (verified identical wording: 'Set the env var: export ... ; verify with easycat doctor') — and move the `SystemExit` behavior into a separate CLI-only helper if the exit-code semantics are wanted there. If the exiting behavior must stay on the exported name, rename it `require_env_or_exit` in `_public_api.py:30`, docs/public-api.md:144, and the ten doc/example call sites so the process-killing behavior is in the name.

**Evidence**

- `src/easycat/helpers.py:24` — `def require_env(name: str) -> str:` ... `raise SystemExit(...)` on a missing variable; the docstring does say 'or exit with a clear message'
- `src/easycat/_public_api.py:30` — `require_env` is a top-level export alongside `run`, with no name or annotation signalling that it terminates the process
- `examples/webrtc_server.py:52` — CONTRARY EVIDENCE: `require_env("OPENAI_API_KEY")` is called at the top of `main()`, BEFORE the nested `def config(transport)` factory — i.e. correctly, at startup
- `docs/deployment/production-servers.md:34` — CONTRARY EVIDENCE: `require_env("OPENAI_API_KEY")` at module scope, above `def config(transport)`; same pattern repeats at line 120

*Verified:* Confirmed the `SystemExit` raise and the top-level export. CORRECTED the original evidence: the claim that docs 'teach require_env as the credential idiom ... including inside per-connection config factories' is not supported — I grepped every `require_env` call site in docs/ and examples/ (14 sites) and all of them are at module scope or at the top of `main()`/`build_app()`, above the nested factory, including the two the finding cited (examples/webrtc_server.py:52 and docs/using-easycat/02-providers-and-voices/README.md). The hazard scenario is therefore hypothetical, and I rewrote the problem statement to say so. Kept at low because the name/behavior mismatch on an exported helper is real and the catalog-error fix is a genuine improvement.

---

### Architecture, module boundaries, and coupling

*10 findings — 1 high · 5 medium · 4 low*

**Assessment.** The import graph is genuinely acyclic at module level and the `Session → Stages → Providers` direction is real: `stages/` never imports `session/`, `session/actions.py` is a true leaf, and the `_turn_context.py`-at-the-root move is a correct fix for a real cycle. But the much-advertised `session/` decomposition did not decompose anything. Between the first collaborator extraction (c1ce2ff1, 2026-05-11) and today, `_session.py` fell only 1761→1511 lines while the `session/` package grew 5506→8528 (+55%); `_turn_runner.py` nearly doubled (686→1282) and `_audio_router.py` more than doubled (475→1004). The single biggest problem is `SessionWiringContext` (`session/_wiring.py:74-127`): instead of giving each collaborator a narrow interface, it hands all five of them 24 live closures over the Session — including `emit`, `stop`, `cancel_turn`, `reset_turn_state`, `clear_turn`, `set_running` — so every "collaborator" retains full read/write access to Session and no component owns turn state. The result is a god object wearing six masks: 65 instance attributes, 35 public members, three divergent teardown sequences, and `_session.py`/`_audio_router.py`/`_stt_committer.py`/`stages/agent.py` all sitting on the project's own complexity-gate grandfather list. Genuinely well done: `session/_streaming.py` is a clean, Session-free translation layer; `runtime/capabilities.py` centralizes optional duck-typed hooks in one auditable file; the pipeline core is essentially free of vendor names; and the inline "why" comments around AEC ordering, drop policy, and cancellation are unusually good.

**Done well here:**

- Module-level import graph is fully acyclic. An AST-based SCC analysis over all 263 source modules (resolving relative imports, excluding TYPE_CHECKING and function-body imports) found zero strongly-connected components. Only 6 deferred imports in the whole library mask a real cycle, and 4 of those are package-`__init__` re-export artifacts (`easycat.server.routes:261`, `easycat.server.voice_server:1011`, `easycat._provider_catalog:226-227`). For an 87k-LOC library this is unusually disciplined.
- The `Session → Stages → Providers` layering is actually enforced, not just documented. `grep -rn 'easycat.session' src/easycat/stages/` returns nothing: no stage imports the session package. The `_turn_context.py` relocation to the package root (documented at `_turn_context.py:13-20`) is a real, correct fix for the cycle it describes, not a cosmetic move.
- The pipeline core is essentially vendor-free. Grepping `session/`, `stages/`, `providers.py`, `turn_manager.py`, `events.py` for twilio/deepgram/elevenlabs/cartesia/livekit/krisp/silero/webrtc yields only comments plus one fallback heuristic (`_session.py:779`), and that heuristic is explicitly a fallback behind the first-class `transport.transport_kind` property (`_session.py:771-773`).
- `session/_streaming.py` is a model of what the rest of the package should look like: 547 lines, one concern (agent stream events -> sentence-boundary TTS payloads), no Session reference, no wiring context, dependencies passed as explicit callables to `consume_agent_stream` (`_streaming.py:307`). It is the only large session module that could be unit-tested without constructing a Session.
- `runtime/capabilities.py` is the right shape for optional provider hooks: every duck-typed `getattr` probe for `send_playback_mark`, `pending_playout_ms`, `drain_aec_reference_frames`, `clear_audio`, `health_check`, `warmup`, `aclose` lives in one 159-line file with named helpers, rather than scattering bare `getattr` across the pipeline. `providers.py:286-305` documents exactly why those hooks are deliberately kept off the `runtime_checkable` Transport protocol.
- `session/actions.py` is a genuine leaf — zero `easycat.*` module-level imports (only a deferred `easycat.telephony.compliance` import at line 410 for DNC). That is what let `events.py` reference `SessionActionResult` behind a single late-bound factory (`events.py:26-30`) instead of a cycle.
- The `Stage` protocol is uniformly implemented. All seven stages provide `execute` / `snapshot_state` / `replay` / `handle_upstream`, and the two detector-specific extras (`VADStage.replay_decision`, `TurnStage.replay_decision`) are explicitly documented as off-protocol in `stages/base.py:137-141` rather than silently widening the contract.
- Inline rationale comments are consistently load-bearing rather than restating the code: `_audio_router.py:911-927` (why AEC reference feed is isolated from send failures), `_builder.py:140-146` (why outbound audio must be DROP_NEWEST not DROP_OLDEST), `_turn_runner.py:786-792` (why `playback_cut_short` is decided inside the consumer task). This is the kind of documentation that survives refactors.

#### 14. Session.stop() is a 148-line, cc-19 method with two divergent teardown branches, grandfathered out of the project's own complexity gate

`HIGH` · `maintenance-burden` · `effort: large`

**Problem.** `stop()` is the single public teardown verb and the highest-risk method in the library: every leaked task, unclosed socket, and hung provider handle routes through it. It is 148 lines with 20 branches split across a force path and a graceful path that share only a tail, and it is explicitly exempted from the complexity gate the project itself declares should only shrink. Session carries 66 instance attributes and 40 public members spanning provider ownership, agent adaptation, event-bus attachment (including writing provider private fields), journal/artifact lifecycle, outbound-queue lifecycle, health checkers, heartbeats, telephony delegation, DNC list, session-action executor registry, three cancellation verbs, six event-subscription conveniences, and debug-bundle export.

**Impact.** The two branches must be reasoned about independently every time a resource is added. Today this is safe rather than broken: each collaborator drains its own named scope tasks (`_stt_committer.py:441-447`, `_audio_router.py:314/324`, `_greeting.py:104`), so the graceful path is covered even without the scope-wide drain. But that safety is a convention, not a structure — a new `RuntimeScope` task added tomorrow without a matching collaborator-level drain will be cleaned up only on `stop(force=True)` and will leak on the default graceful path, with no test or type to catch it.

**Fix.** Extract a `SessionTeardown` collaborator holding one ordered resource list plus a `force: bool`, so the two branches in `stop()` become one sequence with force-specific steps flagged inline; this is what makes the 'is this resource drained on both paths?' question answerable by reading one list. Separately, move the six event-subscription conveniences off Session (`_session.py:547-652`: `subscribe_event`, `unsubscribe_event`, `subscribe_agent_events`, `on`, `unsubscribe_handlers`) — they are pure `EventBus` sugar with no Session state. Then delete the `"src/easycat/session/_session.py"` line from `pyproject.toml`'s per-file-ignores rather than letting it sit indefinitely.

**Evidence**

- `src/easycat/session/_session.py:1095` — `stop()` spans 1095-1242 (148 lines). Verified with `.venv/bin/ruff check --isolated --select C901,PLR0912,PLR0915`: C901 19>10, PLR0912 20>12, PLR0915 85>50. Force branch 1138-1178, graceful branch 1179-1202, shared tail 1204-1242.
- `src/easycat/session/_session.py:154` — `__init__` spans 154-293 (140 lines); ruff reports PLR0915 62>50. Verified at runtime: `len(vars(Session(SessionConfig(runtime_mode='text_session'))))` == 66 instance attributes; `len([k for k in dir(s) if not k.startswith('_')])` == 40 public members (finding said 35 — corrected).
- `pyproject.toml:283` — `"src/easycat/session/_session.py" = ["C901", "PLR0912", "PLR0915"]`. The header comment at lines 236-239 says the grandfather list should 'shrink, never grow'. `_audio_router.py`, `_stt_committer.py`, `session/text.py`, `stages/agent.py`, `stages/tts.py` are also on it — the whole pipeline core. Isolated ruff over `src/easycat/session/` + `src/easycat/stages/` reports 17 violations.
- `src/easycat/session/_session.py:1175` — Force path calls `self._runtime_scope.cancel_and_drain()` (all tasks); the graceful branch does not. The shared tail at 1216 calls `cancel_and_drain("pipeline_heartbeat")`, which `runtime/scope.py:70-91` shows drains only that one named task.
- `src/easycat/session/_session.py:1458` — `_maybe_attach_event_bus` (1458-1481) writes `provider._config.event_bus` and `provider._event_bus` — private attributes on third-party provider objects — from inside Session.

*Verified:* Ran ruff isolated myself: every complexity number in the finding matched exactly (stop C901 19/PLR0912 20/PLR0915 85; __init__ PLR0915 62; 17 violations across session/ + stages/). Counted instance attributes at runtime: 66 confirmed, but public members are 40 not 35 — corrected. I REMOVED the finding's claim that `_rollback_interrupted_start` (1078-1093) is a third sequence that has 'drifted' because it 'stops helpers but does not close providers': not closing providers is correct there, since a rolled-back start is retryable and the caller still owns the providers. I also softened the 'each new resource must be remembered in three places' impact — I checked `_stt_committer.cancel`, `_audio_router.stop_ingress/stop_outbound`, and `_greeting.cancel` and each drains its own named scope tasks, so the branch divergence is currently a reasoning burden, not a live leak.

#### 15. Reassigning session.stt/tts/vad/transport half-rewires the pipeline; only `agent` has a resyncing setter

`MEDIUM` · `correctness` · `effort: medium`

**Problem.** `SessionWiringContext` advertises late-bound provider getters (`_wiring.py:103-105`), and roughly half the pipeline honors them: `TurnRunner.on_turn_started` opens the stream on the new STT and `STTCommitter` consumes its events, while `STTStage.execute` — the audio delivery path — still sends to the provider captured at build time. `TTSScheduler` collapses the late binding entirely by calling `wiring.tts()` once. `AudioRouter` caches `self._transport` plus four derived capability flags at `_audio_router.py:160-243`, while `Session.stop()` (1218) and `cancel_turn` (1302) act on the live `self.transport`.

**Impact.** A caller who reassigns any of these five attributes gets a session in a silently inconsistent state — audio captured from one object, streamed to another, torn down against a third — with nothing raised. The concrete in-repo cost today is that `tests/integration/test_failure_paths.py:128` documents a supported-looking recovery pattern that does not actually work as written, so the codebase has an assertion that would keep passing if the STT swap path broke entirely. The user-facing risk is latent rather than active: no doc or example reassigns these attributes mid-session (grep over `docs/`, `examples/`, and `src/` finds only `config.transport = transport` in `server/voice_server.py:764`, which is pre-construction).

**Fix.** Make `stt`, `tts`, `vad`, `noise_reducer`, `echo_canceller`, and `transport` read-only properties on Session backed by `_stt`/`_tts`/… that raise `AttributeError` on assignment after construction, and document `create_session` / `SessionConfig` as the only wiring point. Then fix the two in-repo assignments: `tests/session/test_session_audio_pipeline.py:130-131` already rebuilds the stage and can construct the session with `_RaceSTT` instead, and `tests/integration/test_failure_paths.py:128` should be rewritten to assert the failure behavior rather than swap providers. This is cheaper and safer than the alternative of adding `set_provider` to four stages plus push-down into `AudioRouter._transport`, its three derived flags, and `TTSSynthesizer._tts`.

**Evidence**

- `src/easycat/session/_session.py:159` — `self.stt`, `self.tts`, `self.vad`, `self.noise_reducer`, `self.echo_canceller`, `self.transport` (159-164) are plain public mutable attributes with no property setter.
- `src/easycat/session/_session.py:714` — `@agent.setter` (714-740) is the only resyncing setter; line 740 calls `stage.set_provider(self._agent)`. Confirmed by grep: `set_provider` exists only on `stages/agent.py:117`. `stages/stt.py:42`, `stages/tts.py:44`, `stages/vad.py:62`, `stages/transport.py:40` all do a bare `self._provider = provider` at construction with no update path.
- `src/easycat/session/_tts_scheduler.py:93` — `TTSSynthesizer(tts=wiring.tts(), ...)` — the late-binding getter is invoked once at construction and the value stored, so `session.tts = X` never reaches the synthesizer that actually calls the provider.
- `src/easycat/session/_turn_runner.py:260` — `stt = self._stt_provider()` then `await stt.start_stream()` uses the late-bound provider, while line 272 `await self._stt_stage.execute(chunk, ...)` — the only path that delivers audio — sends to the eagerly captured one. `_stt_committer.py:86` (`self._stt_getter = wiring.stt`) likewise consumes `events()` from the new provider.
- `tests/session/test_session_audio_pipeline.py:130` — `session.stt = _RaceSTT()` on line 130 followed immediately by `session._stt_stage = type(session._stt_stage)(session.stt, journal=session._journal)` on line 131 — in-repo proof that the assignment alone does not rewire the pipeline.
- `tests/integration/test_failure_paths.py:128` — `session.stt = working_stt` with no stage rebuild. Passes because `ScriptedSTT.events()` yields the transcript independently of `send_audio` — so the desync (audio still going to the failed provider) is invisible to the assertion.
- `src/easycat/session/_session.py:1436` — `_close_audio_providers` (1436-1454) closes the *current* `self.stt`/`self.tts`/etc., so after a swap it closes the new instances and leaves the old ones — still referenced by the stages — unclosed.

*Verified:* Every citation checked and correct, including both test line numbers verbatim. I confirmed `set_provider` exists only on `AgentStage` and that `TTSSynthesizer` snapshots the getter. DOWNGRADED from high to medium: the finding's impact section asserts users hit this when swapping transports on reconnect or voices mid-call, but grep across docs/, examples/, teaching chapter 13, and src/ found zero documented or in-library mid-session provider reassignment — chapter 13 swaps providers through config, not attributes. So the demonstrated harm is confined to one misleading in-repo test, and the finding is an API trap rather than an active bug.

#### 16. cancel_turn, reset_state, and cancel_tts_playback re-list the same teardown sequence three times; reset_state is literally cancel_turn plus two lines

`MEDIUM` · `duplication` · `effort: small`

**Problem.** Four lifecycle methods hand-roll overlapping subsets of one cancel sequence. There is no 'quiesce the turn' primitive, so the ordering constraints — STT commit cancelled before TTS, outbound flush before transport `clear_audio`, replay-chunk reset before transport clear — are re-encoded in four places and can drift independently. `reset_state`'s body is statement-for-statement identical to `cancel_turn`'s non-barge-in path.

**Impact.** Any change to teardown ordering — adding a queue, or reordering because a provider needs `clear_audio` before `flush` — must be applied in four places with no compiler or test to catch a missed one. Because the four methods are reachable from user code (`cancel_turn`, `reset_state`, `cancel_tts_playback` are all public) and from barge-in, a partial edit produces an ordering bug that only manifests under interruption timing.

**Fix.** Add `async def _quiesce_turn(self, turn: TurnContext | None) -> None` to `_session.py` containing the seven shared statements (1300-1306 in `cancel_turn`). Rewrite `reset_state` (1336-1355) as `await self.cancel_turn()` then `self.agent.reset()` / `self._agent_stage.reset_history()` — it is already exactly that, so the body shrinks to three lines. Have `cancel_tts_playback` call the primitive with a flag selecting `synthesizer.cancel()` instead of `_tts_scheduler.cancel()`.

**Evidence**

- `src/easycat/session/_session.py:1270` — `cancel_turn` (1270-1315): `turn.cancel_token.cancel()` → `_turn_runner.cancel_preemptive_generation()` → `_stt_committer.cancel(turn)` → `_tts_scheduler.cancel()` → `_outbound_queue.flush_for_new_turn()` → `_audio_router.reset_replay_chunks()` → `clear_audio_if_supported(self.transport)` → conditional `_reset_turn_state()`.
- `src/easycat/session/_session.py:1336` — `reset_state` (1336-1355) executes those same seven statements in the same order, then adds `self.agent.reset()`, `self._agent_stage.reset_history()`, `self._reset_turn_state()`. Read side by side, it is exactly `await self.cancel_turn()` plus two lines.
- `src/easycat/session/_session.py:1317` — `cancel_tts_playback` (1317-1334) repeats four of the same steps — `synthesizer.cancel()` (1329), `flush_for_new_turn()`, `reset_replay_chunks()`, `clear_audio_if_supported()` — with a different first step and a state-conditional `_reset_turn_state()`.
- `src/easycat/session/_session.py:1136` — `stop()` re-lists the fragment a fourth time: `cancel_preemptive_generation()` at 1136, `_stt_committer.cancel(turn)` at 1169 (force) / 1201 (graceful), `_tts_scheduler.cancel()` at 1202.

*Verified:* Read all four methods in full and diffed them by hand. Every line number is exact (`cancel_turn` 1270, `cancel_tts_playback` 1317, `reset_state` 1336, `stop` 1095-1242 with the fragment at 1136/1169/1201/1202). The claim that `reset_state` is `cancel_turn(barge_in=False)` plus two lines is literally true — the only extra code in `cancel_turn`'s non-barge-in path is a `cutoff_started = None` guard that short-circuits. Best-evidenced finding in the set; kept at medium with no corrections.

#### 17. stages/agent.py::execute_streaming is a 209-line, cc-24 async generator on the hot path for every agent integration

`MEDIUM` · `maintenance-burden` · `effort: medium`

**Problem.** One async generator handles journal-ctx resolution, recorder construction, three history-context assembly strategies (stage shadow history / AgentRunner-owned history / transient system prefix), prepared-response validation and dispatch, an OTel span, per-event-kind journaling for four kinds, GeneratorExit forwarding into the wrapped bridge, stage-failure recording, a latency histogram, and post-hoc shadow-history commit. Every agent-framework bridge flows through it.

**Impact.** This is the least reviewable function in the core and the one all seven agent integrations share. The history-commit guard at 374-379 makes the correctness of the last assistant turn depend on identity and epoch values captured 170 lines earlier — exactly the state a mid-stream agent swap mutates, since the `Session.agent` setter calls `AgentStage.set_provider` which resets `_provider` and bumps `_history_epoch`. Any change to journaling or event kinds requires re-verifying the yield/return/aclose interaction by hand, because the GeneratorExit contract is enforced only by a comment.

**Fix.** Split out three helpers on `AgentStage` that already exist implicitly: `_build_turn_input(input_text, system_prefix, bridge)` for lines 222-242, `_journal_bridge_event(event, kind, ctx, turn_id)` collapsing the four near-identical `journal_append_event` calls at 276-325 into one call with a per-kind `data_extra` mapping, and `_commit_shadow_history(bridge, history_epoch, input_text, final_text)` for 371-385. The generator body then reduces to the stream loop plus its three yields, and the GeneratorExit `finally` at 331-340 becomes visible. Remove the `stages/agent.py` line from `pyproject.toml` per-file-ignores once the numbers are under the gate.

**Evidence**

- `src/easycat/stages/agent.py:188` — `execute_streaming` spans 188-396 (209 lines). Verified with isolated ruff: C901 24>10, PLR0912 26>12, PLR0915 77>50 — the highest C901 and PLR0912 in `src/easycat/`.
- `src/easycat/stages/agent.py:266` — The `async for event in stream:` block (266-330) has four `kind ==` branches, each wrapping a `journal_append_event` under `if journal_enabled`. The four calls differ only in `data_extra`. Verified exactly three yield sites (287, 328, 330) and a `return` inside the loop at 329.
- `src/easycat/stages/agent.py:331` — `finally: await aclose_quietly(stream)` (331-340) nested inside `with span:` inside `try/except/finally`. The GeneratorExit-forwarding contract is documented in the comment at 332-339 and is correct, but is invisible from the control flow. Max indentation in the function reaches column 41 (line 293) — 8 nesting levels inside the method body.
- `src/easycat/stages/agent.py:374` — The outer `finally` (357-396) commits shadow history under a four-condition guard at 374-379 (`self._tracks_history and self._provider is bridge and self._history_epoch == history_epoch and final_text`) reading `bridge` captured at 205 and `history_epoch` at 206 — correctness spans 190 lines.
- `pyproject.toml:287` — `"src/easycat/stages/agent.py" = ["C901", "PLR0912", "PLR0915"]` — exempted from the gate.

*Verified:* Ran ruff isolated: C901 24 / PLR0912 26 / PLR0915 77 confirmed exactly, and the yield/return line numbers (287, 328, 329, 330) matched verbatim. CORRECTED two claims: nesting reaches 8 levels inside the method body, not 9 (max indent column 41 at line 293); and it is not uniquely 'the worst numbers in src/easycat' — `integrations/agents/_agent_runner.py:246 invoke` is C901 23 / PLR0912 24 / PLR0915 80, i.e. more statements. DOWNGRADED from high to medium: no defect is demonstrated, only complexity, and a sibling function is comparably bad, so this is one instance of a repo-wide pattern rather than a singular trap. The recommendation is concrete and correct as written.

#### 18. The turn pointer is written from eight call sites in two modules, forcing six hand-written generation guards to compensate

`MEDIUM` · `coupling` · `effort: large`

**Problem.** No single component owns the turn pointer. `Session._turn` is cleared or reset from eight places across `_session.py` and `_turn_runner.py`, plus `TTSScheduler.finalize_speaking_turn` via `wiring.clear_turn`. Because any of these can move the pointer while another is mid-flight, each consumer carries its own hand-written `current is X and generation == Y` re-check — six in `_turn_runner.py` and two more in `_tts_scheduler.py`. The `TurnHandle` protocol at `_turn_context.py` was introduced to be that owner but is used only as a pass-through adapter (`_SessionTurnHandle` in `_wiring.py:42-71`), not as an authority.

**Impact.** Turn-state bugs are unlocalizable: when `_turn` is cleared at the wrong moment there is no single place to look, and every new guard added at one call site must be replicated at the others by hand. This is already visible as maintenance debt — `_tts_scheduler.py:227-232` applies the same guard twice in a row around one call because the pointer can move in between, and `_turn_runner.py:890` re-derives the identical predicate independently.

**Fix.** Promote `_SessionTurnHandle` (`_wiring.py:42-71`) from adapter to owner: give it `clear_if_current(turn, generation)` and `reset()` methods encapsulating the identity-plus-generation predicate, remove `clear_turn` and `reset_turn_state` from `SessionWiringContext` (lines 121-122), and route TurnRunner's four `_reset_turn_state()` calls and TTSScheduler's `_clear_turn()` through the handle. That collapses the eight scattered guards into one implementation. Separately, replace `self._tts._record_markdown_strip` (`_turn_runner.py:1020`) with a public method on TTSScheduler, and drop `wiring.stop` — `TurnRunner` already tracks `_StreamingTtsState.should_stop` internally and can return it instead of terminating the session from inside a collaborator.

**Evidence**

- `src/easycat/session/_wiring.py:74` — `SessionWiringContext` (74-127): 24 fields, of which `set_running`, `clear_turn`, `reset_turn_state`, `cancel_turn`, `stop`, and `emit` are mutation verbs on Session rather than reads. `build_wiring` (140-185) closes 16 lambdas plus 2 nested defs over the live Session, including `stop=lambda: session.stop()`.
- `src/easycat/session/_session.py:528` — `_reset_turn_state` has eight call sites: `_session.py:1090` (rollback), `1315` (cancel_turn), `1334` (cancel_tts_playback), `1355` (reset_state), and `_turn_runner.py:287, 452, 765, 910` — reached from TurnRunner via `wiring.reset_turn_state`. `clear_turn` is additionally called by `TTSScheduler.finalize_speaking_turn` (`_tts_scheduler.py:233`).
- `src/easycat/session/_turn_runner.py:890` — Six identity-plus-generation re-checks exist in `_turn_runner.py` alone to defend against concurrent pointer moves: lines 286, 412, 451, 527-528, 763, and 890 (`if self._turn.current is not st.turn or self._turn.generation != st.turn_gen: return`). `_tts_scheduler.py:223-233` carries its own copy of the same check, applied twice around `flush_trailing_playback_mark`.
- `src/easycat/session/_cancel_orchestrator.py:166` — `for_barge_in` calls `self._cancel_turn_impl(barge_in=True)` == `Session.cancel_turn`, which at `_session.py:1292` calls back into `self._cancel.propagate_signal(...)`. TurnManager → CancelOrchestrator → Session → CancelOrchestrator re-entrancy routed through the wiring context.
- `src/easycat/session/_turn_runner.py:1020` — `self._tts._record_markdown_strip(...)` — TurnRunner calls a private method on TTSScheduler. Session likewise reaches two levels at `_session.py:1329` (`self._tts_scheduler.synthesizer.cancel()`), as does TurnRunner at `_turn_runner.py:850` (`self._tts.synthesizer.synthesize(...)`).

*Verified:* REWRITTEN — the original framing does not hold. It claimed the wiring context is a 'friend-class backdoor' giving 'every collaborator write access', but grepping `wiring.` across all five collaborators shows each mutator has exactly one consumer: `set_running` → AudioRouter only, `clear_turn` → TTSScheduler only, `reset_turn_state` → TurnRunner only, `cancel_turn` → CancelOrchestrator only, `stop` → TurnRunner only. The mesh the finding describes is not realized. I also corrected the lambda count (16 lambdas + 2 nested defs, not 22) and three wrong line numbers (the guards are at 286/412/451/527-528/763/890, not 469/517-529/763/890). What survives verification is the diffuse turn-pointer ownership and the guard duplication it forces, which is real and worth fixing — so I kept the finding and rewrote it around that. DOWNGRADED high → medium accordingly.

#### 19. debug/export.py collects artifacts by reading private attributes of two concrete stores, so any custom ArtifactStore silently exports zero artifacts

`MEDIUM` · `coupling` · `effort: medium`

**Problem.** `debug/export.py` types its parameter as `session: object` and rediscovers Session's shape through `getattr` chains on private names, then bypasses the `ArtifactStore` Protocol entirely to read `_store` / `_dir` off two concrete classes. A store that is neither `InMemoryArtifactStore` nor `FilesystemArtifactStore` — the exact case `stages/base.py:220-233` documents support for — falls through to `return {}`. The same private reach is repeated in `debugger/_sources.py:377` (`session._artifact_store`).

**Impact.** A user who supplies an S3- or NFS-backed `ArtifactStore` via `SessionConfig.artifact_store` gets a debug bundle containing a valid manifest and full journal but zero artifacts — no exception, no warning, no field in the manifest indicating the omission. Since the bundle is the primary support artifact ('the journal is the single source of truth for all observability'), the failure surfaces only when someone tries to diagnose a call from a bundle that quietly lacks its audio and payload blobs.

**Fix.** Add an enumeration member to the `ArtifactStore` Protocol in `src/easycat/runtime/artifacts.py:29` — `def snapshot(self) -> dict[str, bytes]` (or `iter_all()`) — implement it on `InMemoryArtifactStore` and `FilesystemArtifactStore`, and rewrite `_collect_artifacts` (`debug/export.py:128-145`) to call it, falling back to a logged warning rather than a silent `{}` when a store does not implement it. While there, delete the dead `_debug`/`debug` probe at `export.py:93` and derive `debug_mode` from the config snapshot instead.

**Evidence**

- `src/easycat/debug/export.py:128` — `_collect_artifacts(session: object)` reads `getattr(session, "_artifact_store", None)`, then at 133 iterates `artifact_store._store.items()` for `InMemoryArtifactStore` and at 140 reads `artifact_store._dir` for `FilesystemArtifactStore`. Line 137: `if not isinstance(artifact_store, _FilesystemArtifactStore): return {}` — every other implementation yields an empty dict with no log or warning.
- `src/easycat/runtime/artifacts.py:29` — The `ArtifactStore` Protocol declares `put`, `get`, `get_head_tail`, `has`, `delete`, `close` — no iteration or snapshot member, so `_collect_artifacts` has no interface-level way to enumerate a store.
- `src/easycat/session/_types.py:141` — `artifact_store: ArtifactStore | None = None` — the public `SessionConfig` accepts any Protocol implementation, so a custom store is a first-class configuration option, not an internal detail.
- `src/easycat/stages/base.py:220` — `_writes_block` docstring explicitly anticipates third-party stores: 'the escape hatch for custom ``ArtifactStore`` implementations (S3/NFS-backed, wrappers around the filesystem store) whose ``put`` does I/O' — confirming custom stores are a designed-for case.
- `src/easycat/debug/export.py:93` — Dead probe: `debug_mode = getattr(session, "_debug", None) or getattr(session, "debug", None)`. Grep confirms Session defines neither attribute, so the fallback on 94-95 runs 100% of the time.

*Verified:* All citations verified. NARROWED substantially: the original finding claimed the dead `_debug` probe means `_require_debug_capture` 'cannot distinguish debug="full" from debug="light"' and that its error message 'can be wrong'. I traced this — `config/_factory.py:404-407` returns `journal=None` exactly when `config.debug == "off"`, so the fallback at `export.py:94-95` produces the correct verdict in every case and the function only ever tests for `"off"` anyway. The probe is dead code with zero behavioral impact, so I demoted it from the headline to a cleanup note. The artifact-store gap is the real defect and I confirmed it end to end (Protocol has no iteration member, SessionConfig is Protocol-typed, `stages/base.py` documents custom stores). Kept at medium: real and silent, but the blast radius is a degraded diagnostic bundle for a minority configuration, not data loss.

#### 20. session/ imports easycat.config through a deferred import that masks a real module cycle, and config writes four private fields onto Session after construction

`LOW` · `layering` · `effort: medium`

**Problem.** The dependency runs both ways and neither direction is expressed in a type or signature. Session needs `_inject_agent_runtime` from `config/easy.py:251` for mid-session agent swaps, so it defers the import; config needs to stash four fields that `Session.__init__` does not accept, so it assigns them post-construction. The result is that `Session.__init__` cannot produce a fully-formed Session, and downstream code (`debug/_serialize.py:80`, `_session.py:1233`) probes for the config-injected fields with `getattr(..., default)`.

**Impact.** `Session(SessionConfig(...))` and `Session.from_providers(...)` — both documented public entry points (`_session.py:296-316`) — produce objects whose debug-bundle config snapshot comes from a different source than sessions built via `create_session`: the raw `SessionConfig` with live provider instances rather than the sanitized `EasyConfig` namespace, which `_serialize.py:71-73` notes renders as `<object at 0x…>` repr strings. Bundles from the two construction paths are therefore not comparable. The deferred import is also load-bearing structure held in place by a comment.

**Fix.** Move `_inject_agent_runtime` out of `config/easy.py:251` into `easycat/integrations/agents/` — it only unwraps `AgentRunner` and calls `configure_runtime` on the inner bridge, so it belongs next to the bridges. That deletes the deferred import at `_session.py:751` and the cycle with it. Then add `config_snapshot`, `agent_model`, and `remote_agent_api_key` as real `SessionConfig` fields so `_factory` passes them through the constructor, and delete the two phantom annotations at `_session.py:151-152`.

**Evidence**

- `src/easycat/session/_session.py:751` — `from easycat.config import _inject_agent_runtime` inside `_inject_agent_runtime_config` — one of only four function-level imports in the file (the others are at 396, 675, 715).
- `src/easycat/config/_factory.py:40` — `from easycat.session._session import Session` at module level, and `config/__init__.py:16` imports `._factory` at module level — so the cycle is `easycat.config` → `easycat.config._factory` → `easycat.session._session` → (deferred) `easycat.config`.
- `src/easycat/config/_factory.py:679` — `_finalize_audio_session` writes `session._easycat_config`, `session._agent_model`, `session._remote_agent_api_key` (679-681); `install_emergency_export` writes `session._emergency_export_unregister` at 875. None of the four is a `SessionConfig` field or `__init__` parameter.
- `src/easycat/session/_session.py:151` — `_easycat_config: Any` and `_emergency_export_unregister: Callable[[], None]` declared as unassigned class annotations with the comment 'Set dynamically by ``easycat.config._factory``' — the class documents its own layering violation so `getattr(..., default)` probes keep working.
- `src/easycat/config/_factory.py:576` — `session._caller_id.private_identity` — config reaches two levels into a Session collaborator's private state.

*Verified:* Cycle verified by reading `config/__init__.py:16`, `_factory.py:40`, and `_session.py:751`; the post-construction writes and phantom annotations are all exactly as cited. CORRECTED the impact: the finding claimed `export_debug_bundle` 'silently emits an empty config snapshot' for directly-constructed sessions, but `debug/_serialize.py:80` reads `getattr(session, "_easycat_config", None) or getattr(session, "_config", None)` — the `_config` fallback (set at `_session.py:156`) covers exactly that case, so the snapshot is different, not empty. I also dropped the claim that 'nothing in the test suite guards' the deferred import: promoting it to module scope would fail on import of `create_session`, which hundreds of tests do, so CI would catch it immediately. DOWNGRADED medium → low: real self-documented layering violation with a clean fix, but nothing is broken today.

#### 21. A NotImplementedError placeholder pinned by its own test, and four late-binding getters that exist for a single test's benefit

`LOW` · `over-engineering` · `effort: small`

**Problem.** Two abstractions in `session/` exist for futures that have not arrived. The `_synthesize_sentences` placeholder is dead code whose accompanying test converts it from 'removable' to 'protected'. The four `enable_*` getters carry a full apparatus — a wiring field, a lambda in `build_wiring`, a stored attribute in AudioRouter/STTCommitter, and a per-frame call — so that one integration test can flip a flag on an already-constructed session.

**Impact.** Small individually. The `NotImplementedError` plus its guard test is the worse pattern: it makes the dead code permanently un-deletable by an automated or reviewer-driven cleanup, because removing it breaks a green test — and it advertises an unimplemented feature in the module docstring that a reader may mistake for a hook they can use.

**Fix.** Delete `TTSScheduler._synthesize_sentences` (`_tts_scheduler.py:293-307`), the docstring bullet at line 12, and `tests/session/test_tts_scheduler.py:686-693`; reintroduce the hook when the pipelining change actually lands. Replace the four `enable_*` fields in `SessionWiringContext` (`_wiring.py:97-100`) and their lambdas in `build_wiring` (167-170) with plain `bool` constructor arguments to `AudioRouter` and `STTCommitter`, and change `tests/integration/test_session_pipeline.py:1551-1552` to construct its session with `enable_vad=False, auto_turn_from_stt_final=True` instead of mutating afterward.

**Evidence**

- `src/easycat/session/_tts_scheduler.py:293` — `_synthesize_sentences` (293-307) raises `NotImplementedError("sentence-level TTS pipelining is not implemented yet")`. Grep across src/ finds no caller; the only other references are the module docstring bullet at line 12 and the test below.
- `tests/session/test_tts_scheduler.py:687` — `test_synthesize_sentences_raises_not_implemented` calls the placeholder and asserts `pytest.raises(NotImplementedError)` — a test whose only function is to make the dead code undeletable by any cleanup pass.
- `src/easycat/session/_wiring.py:97` — `enable_noise_reduction`, `enable_aec`, `enable_vad`, `auto_turn_from_stt_final` are `Callable[[], bool]` late-binding getters. Grep confirms the backing fields (`_session.py:197-204`) are assigned only in `__init__` and mutated nowhere in src/ — the sole mutation site in the whole repo is `tests/integration/test_session_pipeline.py:1551-1552`.
- `src/easycat/session/_audio_router.py:768` — `if self._enable_noise_reduction() or self._enable_aec():`, `if self._enable_vad():` at 773, `self._auto_turn_from_stt_final()` at 785 — closure invocations per audio frame (~50/s) to read values that are constant for the session's lifetime.

*Verified:* The dead placeholder, its pinning test, and the flag-mutation-only-in-one-test claims all verified by grep. REJECTED two of the finding's four sub-claims and removed them. (1) `Session._config` is NOT 'read only by tests' — `debug/_serialize.py:80` reads it as the config-snapshot fallback for directly-constructed sessions, so the recommendation to drop it would break debug bundles on that path. (2) `SessionDebugBackends.close()` is not a vestigial phase: it has independent test coverage exercising retry-after-finalize-failure (`tests/session/test_debug_backends.py:28`), and the `_flushed` latch is what makes that retry work. DOWNGRADED medium → low: what remains is genuinely small cleanup, and I dropped the finding's claim that these are 'why session/ grew 55%' since that causal story does not survive scrutiny.

#### 22. docs/architecture.md's description of Session.__init__ is verifiably false in both of its claims

`LOW` · `documentation-drift` · `effort: small`

**Problem.** The architecture page is the onboarding contract CLAUDE.md designates as 'the maintained home for architecture prose', and its description of `Session.__init__` is the specific claim a new maintainer or coding agent will check first. It will not survive thirty seconds of contact with the file. The page is actively maintained and guarded by `just guard-docs`, so this is a review gap rather than neglect: the guard tests in `tests/docs/` verify link and anchor validity but not a single load-bearing structural claim.

**Impact.** A contributor who trusts the page will plan a change assuming Session's constructor is trivial, then discover mid-PR that it is 140 lines doing validation, adaptation, and flag derivation. The `_tts_scheduler.py` entry compounds this by hiding where turn-pointer clearing lives, which is precisely the state a turn-lifecycle change must reason about.

**Fix.** Rewrite `docs/architecture.md:33-37` to state what `__init__` actually does (resolves provider fallbacks, adapts the agent, attaches the event bus, validates noop providers, derives pipeline flags, then calls `build_session` and constructs `SessionDebugBackends`), and extend line 71 to mention `finalize_speaking_turn` as the owner of the bot-speaking stop and turn-clear decision. Optionally add one guard test in `tests/docs/` asserting a stated line budget for `_session.py` that matches a number written into the page, turning the doc into a ratchet.

**Evidence**

- `docs/architecture.md:34` — '`Session.__init__` is a short field-assignment shell that ends with a single `build_session(self, cfg)` call — a newcomer can scan the constructor in under a minute.' Both halves are false: `_session.py:154-293` is 140 lines with ruff PLR0915 62>50, and it does not end with `build_session` — that call is at line 287, followed by the `SessionDebugBackends(...)` construction at 288-293.
- `src/easycat/session/_session.py:154` — The constructor performs provider fallback resolution (159-164), agent adaptation and runtime injection (173-183), event-bus attachment to three providers (186-189), noop validation (192), pipeline enable-flag derivation (197-205), turn-manager construction and journal binding (208-212), and `record_to` validation with a warning branch (223-231) — none of which is field assignment.
- `docs/architecture.md:71` — '`session/_tts_scheduler.py` — `TTSScheduler.prepare()` builds and normalizes TTS payload text before scheduling synthesis/playback.' Omits `finalize_speaking_turn` (`_tts_scheduler.py:184-234`), which its own docstring calls 'the single owner of the bot-speaking stop / turn-clear decision' — it drains session actions, transitions the turn manager, and clears the turn pointer.

*Verified:* NARROWED from five evidence items to three. I DISPROVED the finding's second claim: it asserted the doc's '~40 inline lambdas' figure 'inflates the benefit of the wiring context by ~2x'. I checked out the parent of the commit that introduced `_wiring.py` (91c5cf4d) and counted `lambda` in `_session.py` at that revision — 41. The doc figure is accurate. I also DISPROVED the `session/text.py` item: `docs/architecture.md:76-80` accurately describes `speech energy detection` and explicitly notes the `interruption.py` split, so that is a cohesion complaint about the code (covered separately), not documentation drift. The `_turn_runner.py:66` item is a terse one-line orientation summary in a file map and is not wrong, so I dropped it too. DOWNGRADED medium → low: one false sentence plus one material omission, with a cheap fix.

#### 23. session/text.py mixes PCM energy detection and interruption-estimation helpers into a sentence-and-markdown module

`LOW` · `cohesion` · `effort: small`

**Problem.** `session/text.py` (494 lines) holds four unrelated concerns: sentence and clause segmentation for streaming TTS, markdown open-state scanning, PCM speech-energy detection, and TTS-payload timeline normalization for interruption estimation. It is also on the complexity grandfather list (`pyproject.toml:285`), where the actual offender is `_scan_markdown_link_or_image` (C901 14, PLR0912 13) — a markdown concern that has nothing to do with the other three.

**Impact.** Low direct cost, but it obscures the audio path: someone reading `AudioRouter._process_chunk` to find why a turn started spuriously has to open a module named `text` to find the 500-sample threshold. It also blocks obvious reuse — `_chunk_has_speech_energy` is generic DSP that a future VAD fallback or diagnostics path would look for in `_audio_utils.py`, next to the other PCM helpers.

**Fix.** Move `_chunk_has_speech_energy` (`session/text.py:246-280`) to `src/easycat/_audio_utils.py` alongside `to_mono` and `chunk_frames`, updating the import at `_audio_router.py:52` and the three tests in `tests/session/test_text_property.py:59-93`. Move `_text_for_estimation_timeline` and `_cleanup_estimation_text` (463-494) into `session/interruption.py` so barge-in estimation is one module. That leaves `session/text.py` as a coherent ~350-line sentence-and-markdown module and lets the docstring drop its ownership explanation.

**Evidence**

- `src/easycat/session/text.py:246` — `_chunk_has_speech_energy(chunk: AudioChunk, *, threshold: int = 500)` — scans 16-bit PCM samples to gate auto-turn start, in a module named `text`. Requires `import struct` (line 28) and `from easycat.audio_format import AudioChunk` (line 39) at the top of a text-processing module.
- `src/easycat/session/_audio_router.py:52` — `from easycat.session.text import _chunk_has_speech_energy`, used in the ingress hot path at line 787 — the audio loop imports its DSP gate from the text module.
- `src/easycat/_audio_utils.py:249` — The natural home already exists and already holds the sibling PCM helpers: `to_mono`, `to_mono_chunk` (270), `chunk_frames` (284), `resample_chunk` (235), `pcm_to_wav` (36).
- `src/easycat/session/text.py:463` — `_text_for_estimation_timeline(payload: TTSInput)` and `_cleanup_estimation_text` (492) are barge-in estimation helpers, while `docs/architecture.md:78-80` documents the estimation itself (`_estimate_text_spoken`) as living in `session/interruption.py` — so the logic is split across two modules.
- `src/easycat/session/text.py:8` — The module docstring spends lines 8-22 explaining which underscore helpers are package-internal cross-module API and which are file-local — a fourteen-line ownership explanation the file needs only because it serves four unrelated consumers.

*Verified:* Every citation verified, including that `_audio_utils.py` exists with the sibling PCM helpers the recommendation names, and that the only in-src consumer of `_chunk_has_speech_energy` is `_audio_router.py:787`. I checked the grandfather-list entry and identified the actual C901 offender in the file (`_scan_markdown_link_or_image` at line 317) so the split has a concrete complexity payoff. Kept at low with no severity change; the finding was accurate as written.

---

### Async correctness, cancellation, and backpressure

*9 findings — 3 high · 3 medium · 3 low*

**Assessment.** The async architecture is unusually literate for a pre-1.0 library — bounded queues everywhere, correct `Task.cancelling()` handling for 3.11+ cancellation semantics, a thread-safe CancelToken, and a journal that degrades instead of raising. But the teardown and barge-in paths have real liveness bugs. The single biggest problem is that there is no bounded timeout anywhere in the shutdown path, and `stop(force=True)` — documented as the escape hatch for "a graceful stop hung on a misbehaving provider" — returns immediately as a no-op in exactly that case (verified empirically: `wait_closed()` then hangs forever and SIGINT cannot break it because the handler only sets an already-set Event). Second: the entire barge-in cancellation runs inline on the audio ingress task, and a cancelled TTS consumer swallows its `CancelledError` and then waits for the outbound queue to fully drain into the transport — I measured `cancel_turn(barge_in=True)` taking 1.495 s against a transport whose `send_audio` blocks for 1.5 s, during which no mic audio is read at all. What is genuinely well done: the agent-stream cancellation path (per-event token checks, in-flight tool-call draining, deterministic `aclose`), the per-chunk error isolation with a consecutive-failure threshold, and the fact that both audio queues are bounded with an explicit policy and a journal drop hook rather than the unbounded `asyncio.Queue` most voice frameworks ship.

**Done well here:**

- CancelToken backs `is_cancelled` with a monotonic timestamp set under a threading.Lock rather than with the asyncio.Event (src/easycat/cancel.py:37-42), so the flag flips synchronously even when the `Event.set()` is deferred onto the loop via `call_soon_threadsafe`. This is the correct fix for a race most codebases get wrong, and the docstring explains why.
- The agent-stream cancellation path is genuinely careful: the token is checked per event, in-flight tool calls are drained before bailing so a `tool_result` is never orphaned, a trailing `done` payload is still captured, and the stream is deterministically `aclose()`d rather than left to GC (src/easycat/session/_streaming.py:394-430, 500-510).
- Both audio queues are bounded with an explicit, documented drop policy and a journal hook on every drop (src/easycat/_bounded_queue.py, src/easycat/session/_session.py:504-526). The inbound queue overflow also emits a transport-degraded event (src/easycat/transports/_base.py:47-95). Most voice frameworks ship an unbounded `asyncio.Queue` here.
- Journal appends are fully guarded and degrade instead of raising (src/easycat/runtime/journal_sql.py:88-119): any sqlite failure flips `_degraded` and returns -1. A debug facility that can never kill the live pipeline is the right call, and the WAL + `synchronous=NORMAL` + open-transaction batching keeps the hot-path append off fsync.
- 3.11+ cancellation semantics are handled properly throughout: `asyncio.current_task().cancelling()` is consulted before deciding whether a caught CancelledError belongs to the drained child or to the caller (src/easycat/runtime/scope.py:281-283, src/easycat/session/_turn_runner.py:549-551, 583-586, 937-941). This is subtle and almost always gotten wrong.
- Per-chunk pipeline errors are isolated with a consecutive-failure threshold rather than an all-or-nothing policy (src/easycat/session/_audio_router.py:594-641), including a sentinel exception specifically so the fatal frame does not emit a duplicate `Error`. One bad frame cannot drop a live call; a sustained run still tears down.
- `_finish_interrupted_start` (src/easycat/session/_session.py:1062-1077) survives repeated caller cancellation by shielding an independent rollback task in a loop, so a double Ctrl-C during startup cannot strand a connected transport. The partial-start rollback releases exactly the resources start() acquired.
- Warmup deliberately runs before `transport.connect()` with a comment explaining the concrete bug that motivated it (inbound queue overflowing during multi-second ONNX model loads, src/easycat/session/_session.py:983-1002). The codebase is full of comments like this that cite the actual incident rather than restating the code.
- Smart-turn ONNX inference is correctly offloaded via `run_in_executor` (src/easycat/smart_turn.py:590), and the decision to run Silero inline is justified with a measured number (~100us at ~31 fps vs a ~40us thread-hop, src/easycat/vad/silero.py:228-232) plus an `await asyncio.sleep(0)` between frames when a transport delivers a batch. That is the right analysis, not a guess.

#### 24. Barge-in teardown runs inline on the audio-ingress task, so mic capture stops for its whole duration

`HIGH` · `correctness` · `effort: medium`

**Problem.** `TurnManager`'s barge-in callback is `CancelOrchestrator.for_barge_in`, which awaits the whole of `Session.cancel_turn(barge_in=True)`. That await sits inside `AudioRouter._process_chunk`, which is the body of the transport receive loop. While cancel_turn runs, nothing is pulling from `transport.receive_audio()`, so the transport's bounded inbound queue backs up and then drops frames. I measured `cancel_turn(barge_in=True)` at 5.505 s against a stub transport reporting 5 s of `pending_playout_ms` (LocalTransport can report up to 10 s at its 500-frame `max_pending_out_chunks` default), and at 2.003 s with a 290-chunk outbound backlog. See the companion finding for why cancel_turn is slow.

**Impact.** Exactly when the caller speaks over the bot, the pipeline stops reading their microphone. With the default 200-frame inbound queue (~4 s at 20 ms frames) a barge-in teardown longer than that discards the start of the interrupting utterance, so the bot transcribes nothing or transcribes it truncated. The drop is not silent — it logs a warning and emits a degraded event — but the audio is gone.

**Fix.** Split the barge-in path in `session/_cancel_orchestrator.py::for_barge_in`: do the non-blocking part inline (cancel the turn token, `self._outbound_queue.flush_for_new_turn()`, `clear_audio_if_supported(transport)`) and hand the slow provider teardown (`_stt_committer.cancel`, `_tts_scheduler.cancel`) to a `RuntimeScope` task so `_process_chunk` returns to `receive_audio()` immediately. `TurnManager._handle_barge_in` (turn_manager.py:671) only needs the suppression verdict, not teardown completion, so the callback can return as soon as the verdict is known.

**Evidence**

- `src/easycat/session/_audio_router.py:778` — `await self._turn_manager.on_vad_event(vad_event)` inside `_process_chunk`, which is the body of the `async for chunk in self._transport.receive_audio()` loop at line 596/608 (the `_run_pipeline` ingress task)
- `src/easycat/turn_manager.py:671` — `result = await self._cancel_turn_callback()` in `_handle_barge_in` — the full teardown is awaited inline before `_begin_turn` runs
- `src/easycat/session/_cancel_orchestrator.py:166` — `await self._cancel_turn_impl(barge_in=True)` in `for_barge_in`, the callback wired into TurnManager
- `src/easycat/session/_session.py:1297` — `Session.cancel_turn` then runs 6 sequential awaits: cancel_preemptive_generation, `_stt_committer.cancel`, `_tts_scheduler.cancel`, flush, reset_replay_chunks, `clear_audio_if_supported` (lines 1297-1302)
- `src/easycat/transports/_base.py:74` — `except asyncio.QueueFull:` -> `logger.warning("Inbound %s audio queue full — dropping frame")` + `emit_degraded(...)`; the newest mic frame (the barge-in speech) is what gets dropped
- `src/easycat/transports/websocket.py:63` — `max_pending_chunks: int = 200` sets the inbound `asyncio.Queue` maxsize (`_base.py:125-130`); LocalTransport uses `max_pending_in_chunks: int = 200` (`transports/local.py:50`)

*Verified:* Call chain verified line by line; `_process_chunk` is definitively on the ingress task (`_run_pipeline`, _audio_router.py:589-608). Reproduced empirically with a stub session: 5.505 s for `cancel_turn(barge_in=True)` against a transport reporting `pending_playout_ms()==5000`, and 2.003 s with a 290-chunk outbound backlog. Corrected the original's claim that overflow 'silently discards' audio — `transports/_base.py:74-79` logs a warning AND emits a degraded event. Also corrected the original's cited 1.495 s figure, which I traced to `_await_non_cancellable_send`, not to this path. Kept at high.

#### 25. Barge-in waits for queued/playing bot audio to drain before it clears playback, instead of the other way round

`HIGH` · `correctness` · `effort: medium`

**Problem.** `_consume_tts_payloads` swallows its own `CancelledError` and falls through to `_settle_turn_after_tts`. On a barge-in neither guard stops it: `Session.cancel_turn(barge_in=True)` does not clear the turn pointer and `TurnManager` is still BOT_SPEAKING, so the already-cancelled consumer calls `finalize_speaking_turn`, which drains session actions and then blocks in `await_drain()` waiting for the *pre-barge-in* audio to reach the transport and finish playing out. `TTSScheduler.cancel()` awaits that task, so `cancel_turn` does not return until the drain finishes — and only then does it flush the queue and call `clear_audio()`.

**Impact.** Barge-in latency becomes 'however long the queued bot audio takes to play', measured at 5.505 s against a transport reporting 5 s of `pending_playout_ms` and 2.003 s (the `await_drain` default timeout) against a 290-chunk outbound backlog. For the whole of that window the bot keeps talking over the interrupting user, and — because the callback runs on the ingress task — mic capture is stalled too. The LocalTransport speaker buffer is sized for up to 10 s, so the worst case on the desktop/dev transport is roughly a 10 s barge-in.

**Fix.** Two independent fixes. (1) In `session/_turn_runner.py::_consume_tts_payloads`, do not fall through to `_settle_turn_after_tts` when the task is being cancelled — re-raise after the bookkeeping at lines 785-798, or guard the line 799 call on `asyncio.current_task().cancelling() == 0` (the same test `_settle_turn_after_tts` already uses at line 885). (2) In `session/_session.py::cancel_turn`, move `self._outbound_queue.flush_for_new_turn()` (line 1300) and `await clear_audio_if_supported(self.transport)` (line 1302) to *before* `await self._tts_scheduler.cancel()` (line 1299) so playback stops first and bookkeeping reconciles second; the drain then sees an empty queue and zero pending playout and returns immediately.

**Evidence**

- `src/easycat/session/_turn_runner.py:773` — `except asyncio.CancelledError: pass` in `_consume_tts_payloads` — the consumer absorbs its own cancellation
- `src/easycat/session/_turn_runner.py:799` — `await self._settle_turn_after_tts(st)` runs unconditionally after the swallow
- `src/easycat/session/_turn_runner.py:888` — generation guard `if self._turn.current is not st.turn or self._turn.generation != st.turn_gen: return` — passes on barge-in because `cancel_turn` skips `_reset_turn_state()` when `barge_in=True` (_session.py:1313)
- `src/easycat/session/_turn_runner.py:892` — `if st.synth_started and self._turn_manager.state == TurnManagerState.BOT_SPEAKING:` -> `finalize_speaking_turn`; still BOT_SPEAKING because `_handle_barge_in` awaits the callback before `_begin_turn`
- `src/easycat/session/_tts_scheduler.py:218` — `await self._audio_router.await_drain()` inside `finalize_speaking_turn`
- `src/easycat/session/_audio_router.py:360` — `asyncio.wait_for(self._outbound_idle.wait(), timeout=timeout)` — 2.0 s default (signature at line 327)
- `src/easycat/session/_audio_router.py:371` — `playout_deadline = max(deadline, loop.time() + remaining_ms / 1000.0 + 0.5)` — for a transport exposing `pending_playout_ms` this can be ~10.5 s (LocalTransport `max_pending_out_chunks=500` at 20 ms/frame)
- `src/easycat/session/_session.py:1300` — `self._outbound_queue.flush_for_new_turn()` and `clear_audio_if_supported` (line 1302) run only AFTER `await self._tts_scheduler.cancel()` (line 1299) returns

*Verified:* Confirmed the mechanism by tracing a live stub session: on `cancel_turn(barge_in=True)` the cancelled consumer does reach `finalize_speaking_turn` -> `await_drain`. Corrected the original's evidence attribution: its 1.495 s measurement was NOT `await_drain` (which returned in 0.000 s in that scenario) but the uncancellable inline send — I re-measured the real cost of this path at 2.003 s (queue backlog) and 5.505 s (playout-reporting transport). Also corrected the claimed guard lines (888/892, not 890/892) and confirmed `await_drain` is bounded, not unbounded. Kept at high on the strength of the 5.5 s measurement.

#### 26. try_send_first_audio_inline makes an untimed transport send permanently uncancellable

`HIGH` · `correctness` · `effort: small`

**Problem.** `_await_non_cancellable_send` deliberately defers caller cancellation until the owned transport send finishes, looping on `asyncio.shield(task)` and swallowing each `CancelledError`. There is no timeout on the wrapped `_send_outbound_chunk`, so if `transport.send_audio` wedges the loop spins absorbing cancellations forever. This runs on the TTS synthesis path, which is the task `Session.stop(force=True)` cancels and awaits, and which `Session.cancel_turn` cancels on barge-in.

**Impact.** A first-frame send to a half-open socket makes the TTS task genuinely uninterruptible. I confirmed in isolation that the waiter absorbs a first cancel, absorbs a second cancel, and is still running afterwards, and confirmed end to end that it delayed a barge-in by 1.494 s against a 1.5 s send (unbounded if the send never returns). With a permanently wedged send, `stop(force=True)` never returns — a full pytest run of that scenario hung past 120 s. This is the one place in the codebase that opts out of cancellation entirely, and it does so around an operation with no independent deadline.

**Fix.** In `session/_audio_router.py::_await_non_cancellable_send`, bound the wait: replace `await asyncio.shield(task)` with `await asyncio.wait_for(asyncio.shield(task), timeout=<one frame budget, e.g. 2x frame_duration_ms>)`, and on expiry cancel `send_task`, await it, and let the stored cancellation propagate. The 'do not interrupt a send mid-frame' invariant only needs to hold for a bounded window, not indefinitely.

**Evidence**

- `src/easycat/session/_audio_router.py:442` — `while not task.done(): await asyncio.shield(task)` in `_await_non_cancellable_send` — loops forever, re-absorbing every cancellation
- `src/easycat/session/_audio_router.py:447` — `if current is None or not current.cancelling(): raise` — a cancellation with `cancelling()` set is stored and swallowed, not propagated
- `src/easycat/session/_audio_router.py:429` — `send_task = asyncio.create_task(self._send_outbound_chunk(chunk, turn), name=self._INLINE_SEND_TASK_NAME)` — the wrapped send has no deadline of its own
- `src/easycat/session/_session.py:1151` — `current_tts_task = self._tts_scheduler.active_turn_task`; stop(force=True) cancels it and `await`s it at lines 1163-1167 — the await never completes if that task is inside this loop

*Verified:* Citations verified (line 442, not 441). Reproduced twice: (a) in isolation, `AudioRouter._await_non_cancellable_send` over a `sleep(3600)` task survived two `.cancel()` calls and kept running; (b) end to end, it absorbed the barge-in cancellation for the full 1.5 s duration of a slow transport send, and a wedged-send `stop(force=True)` never returned. This is the strongest-evidenced finding in the set; kept at high.

#### 27. Session.stop(force=True) is a no-op when a graceful stop is already hung — the exact case its docstring promises to handle

`MEDIUM` · `correctness` · `effort: medium`

**Problem.** `Session.stop()` guards re-entry with a bare `_stopping` boolean that does not consider `force`. If a graceful `stop(force=False)` blocks on any teardown await, a subsequent `stop(force=True)` hits line 1109 and returns immediately having done nothing; `_stopping` stays True because it is only cleared in the blocked call's `finally`, `_mark_closed()` is never reached, and `wait_closed()` never returns. The docstring at line 1099 promises precisely the behaviour the guard prevents.

**Impact.** For an application driving a `Session` directly (the documented public teardown verb) or via `SessionManager`, a wedged provider makes the session unstoppable without process-level force. I reproduced this: with a transport whose `disconnect()` never returns, `stop(force=True)` returned in 0.000045 s, `session._closed` was still False, `session._stopping` was still True, and `wait_closed()` still hung.

**Fix.** In `session/_session.py::stop`, replace the `_stopping` bool with a stored in-flight stop task plus the requested mode. On `stop(force=True)` while a *graceful* stop is in flight, cancel that task and run the force path (this is exactly what `server/transports.py::_escalate_graceful_stop` already does externally); on `stop(force=True)` while a *force* stop is in flight, await the existing task rather than returning. At minimum, update the line 1097-1105 docstring to state the actual contract so callers know they must cancel the graceful task themselves.

**Evidence**

- `src/easycat/session/_session.py:1109` — `if self._stopping: return` — the re-entry guard ignores the `force` argument entirely
- `src/easycat/session/_session.py:1099` — docstring: `force=True` 'aggressively cancels the pipeline / TTS / outbound tasks first, for when a graceful stop is hung on a misbehaving provider'
- `src/easycat/session/_session.py:1239` — `self._stopping = False` is in the `finally` of the hung call, so it never runs while the graceful stop is blocked
- `src/easycat/session/_session.py:1227` — `self._mark_closed()` is never reached, so `_closed` stays False and `wait_closed()` (line 902) never returns
- `src/easycat/session_manager.py:76` — `stop_all` does an unbounded `asyncio.gather(*(session.stop() ...))` with no timeout, so one wedged session pins the whole sweep for direct SessionManager users

*Verified:* Confirmed by reading and reproduced empirically (0.000045 s no-op return, `_closed=False`, `wait_closed()` hangs). DOWNGRADED critical -> medium: the shipped multi-session server does not hit the stated impact — `server/voice_server.py:340-348` and `server/transports.py:157-186` document this exact `_stopping` limitation and work around it by having the drain own the graceful stop and cancel it on escalation, and `voice_server.py:435-446` bounds `stop_all` with `force_shutdown_timeout_s`. I also rejected the original's `easycat.run()` + repeated-Ctrl-C impact story: `helpers.py:126-140` only ever calls `stop(force=True)` once, so `_stopping` is not the blocker there — that hang is the uncancellable-inline-send finding, not this one. Remaining real exposure is direct `Session.stop`/`SessionManager.stop_all` users and the false docstring promise.

#### 28. STTCommitter.cancel() calls end_stream() with no timeout, while the same file's normal path wraps the identical call in wait_for

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `STTCommitter.cancel()` is on both the barge-in path (`Session.cancel_turn` line 1298) and the stop path, and it invokes the provider's `end_stream()` with no deadline — even though `STTCommitter.end_stream` twelve methods earlier applies `asyncio.wait_for(..., timeout=stt_timeout)` to the identical call and emits a typed `STTTimeoutError`. The same absence of a deadline runs through `RuntimeScope.drain` and every provider teardown await in `Session.stop`.

**Impact.** A stalled STT provider `end_stream()` — e.g. one blocked behind the 5 s reconnect-window wait described in the STTBase-lock finding — adds its full stall to every barge-in and to `Session.stop()`. Since barge-in runs on the ingress task, that stall also stops mic capture. The timeout the operator configured via `stt_timeout` silently does not apply on the cancel path.

**Fix.** In `session/_stt_committer.py::cancel`, wrap line 451 the same way line 346 already does: `await asyncio.wait_for(self._stt_getter().end_stream(), timeout=self._timeout_config.stt_timeout)` when a timeout is configured, logging (and continuing the rest of `cancel`) on expiry. Separately, give `RuntimeScope.drain`/`cancel_and_drain` an optional `timeout` parameter and pass it from `Session.stop`.

**Evidence**

- `src/easycat/session/_stt_committer.py:451` — `await self._stt_getter().end_stream()` inside `STTCommitter.cancel()` — no timeout; only the exception is caught (line 452)
- `src/easycat/session/_stt_committer.py:346` — `await asyncio.wait_for(self._stt_getter().end_stream(), timeout=timeout)` in `STTCommitter.end_stream` using `self._timeout_config.stt_timeout`, with an `STTTimeoutError` Error event on expiry — the two call sites are inconsistent
- `src/easycat/runtime/scope.py:280` — `await asyncio.shield(task)` in `RuntimeScope.drain` with no deadline; `cancel_and_drain` (line 293) inherits that, so a task that swallows its CancelledError pins every drain
- `src/easycat/session/_session.py:1218` — `await self.transport.disconnect()` in the teardown body — untimed, like `aclose_if_supported(self.agent)` (1221) and `_close_audio_providers()` (1224, which per-provider `close_if_supported` at 1436-1455 is also untimed)

*Verified:* Both `_stt_committer.py` call sites verified verbatim (346 vs 451) — the asymmetry is real. `RuntimeScope.drain` at scope.py:263-291 confirmed to have no deadline. NARROWED and DOWNGRADED high -> medium: the original's headline recommendation (add a `SessionConfig.shutdown_timeout` and wrap every teardown await) is generic advice, and its stated impact (a wedged session leaking for the process lifetime in a multi-session server) is bounded in the shipped server by `voice_server.py:394/412/440` (`force_shutdown_timeout_s`, default 10 s per `server/config.py:50`). I rewrote the finding around the one concrete, verifiable inconsistency.

#### 29. STTBase holds its lifecycle lock across the provider network write, so end_stream() queues behind a 5 s reconnect window

`MEDIUM` · `correctness` · `effort: medium`

**Problem.** `STTBase.send_audio` acquires `_lifecycle_lock` and holds it across `await self._on_audio(chunk)`. For the WebSocket providers that reaches `ReconnectingWebSocket.send` -> `_await_connected()`, which waits up to `_send_wait_timeout` (5 s by default) while a `recv_iter`-driven reconnect is in flight. `end_stream`, `start_stream` and `commit_segment` all contend for the same lock, so during a reconnect `STTCommitter.cancel()` blocks on the lock — and, per the companion finding, applies no timeout of its own to bound that wait. Batch providers are worse: their `_on_audio` can await a whole cap-triggered HTTP transcription under the same lock.

**Impact.** An STT socket blip during a live turn can stall a single audio frame's `send_audio` for up to 5 s on the ingress task (250 frames of backlog at 20 ms), and turns any barge-in during that window into a 5 s freeze that exceeds the ~4 s inbound buffer, so mic audio is dropped. `Session.stop()` inherits the same wait per pending send.

**Fix.** In `src/easycat/stt/base.py::send_audio`, stop holding `_lifecycle_lock` across the provider call: take the lock only to check `_running` and run `_validate_audio`, release it, then `await self._on_audio(chunk)` under a separate send-ordering lock if per-socket write ordering matters (the class comment at lines 63-66 says the lock exists to protect queue replacement/closure, which does not require covering the network write). Independently, consider lowering `reconnecting_ws.py:152`'s floor for audio sends — dropping a frame beats a multi-second pipeline stall.

**Evidence**

- `src/easycat/stt/base.py:85` — `async with self._lifecycle_lock:` in `STTBase.send_audio`, held across `await self._on_audio(chunk)` at line 89
- `src/easycat/stt/base.py:102` — `end_stream` acquires the same `_lifecycle_lock` (as do `start_stream` line 70 and `commit_segment` line 96), so they queue behind an in-flight send
- `src/easycat/stt/deepgram_provider.py:247` — `await self._send_ws(chunk.data)` -> `websocket_base.py:99 self._ws.send(...)` -> `ReconnectingWebSocket.send` — a real network write under the lock
- `src/easycat/reconnecting_ws.py:152` — `self._send_wait_timeout = min(self._config.max_delay, max(self._config.base_delay, 5.0))` — 5.0 s with the shipped defaults (base_delay=1.0, max_delay=30.0)
- `src/easycat/reconnecting_ws.py:256` — `await asyncio.wait_for(self._connected.wait(), timeout=self._send_wait_timeout)` in `_await_connected` — a send blocks here for the whole reconnect window
- `src/easycat/session/_audio_router.py:803` — `await self._stt_stage.execute(chunk, ...)` in `_process_chunk` — the STT send is awaited inline on the ingress task with no timeout

*Verified:* Every citation verified, including the concrete `_on_audio` -> `_send_ws` -> `ReconnectingWebSocket.send` chain for Deepgram (the default STT WS provider). Corrected the class name: it is `STTBase`, not `BaseSTTProvider`. Not reproduced end to end (would need a forced mid-turn reconnect), so this is structural rather than measured — DOWNGRADED high -> medium accordingly, and because the 5 s bound is itself a deliberate cap (see the comment at reconnecting_ws.py:148-152) rather than an unbounded hang.

#### 30. BoundedAudioQueue's BLOCK policy drops a chunk on a lost race instead of re-waiting

`LOW` · `correctness` · `effort: small`

**Problem.** `_put_after_wait` waits once on `_not_full`, then takes `_put_lock` and re-checks. `get()` wakes every waiting producer, so a producer that loses the re-check is rejected outright rather than looping back to the wait until `block_timeout` expires. The outbound queue does have concurrent producers (`TTSSynthesizer._send_or_queue_audio` at `_tts_synthesizer.py:127`, `AudioRouter.queue_outbound` at line 391, and `AudioRouter.gated_replay` at line 502 all share one queue), so the race is reachable.

**Impact.** A caller who deliberately injects a `DropPolicy.BLOCK` queue via `SessionConfig.outbound_queue` specifically to avoid drops still gets drops — audible gaps mid-utterance — even though `block_timeout` (default 5 s) had time left.

**Fix.** In `src/easycat/_bounded_queue.py::_put_after_wait`, loop against a deadline: compute `deadline = loop.time() + self._block_timeout` once, then `while True:` wait on `_not_full` for the remaining budget, re-check under `_put_lock`, and `_append` on success; only `_reject("block_timeout", ...)` when the deadline actually expires. Also fix the inaccurate `_builder.py:144` comment.

**Evidence**

- `src/easycat/_bounded_queue.py:171` — `return self._reject("block_lost_race", "lost race after BLOCK wait, dropping")` — after a single `wait_for(self._not_full.wait())` at line 161, a producer that loses the re-check drops the chunk instead of waiting again
- `src/easycat/session/_types.py:151` — docstring tells callers to `Inject ... DropPolicy.BLOCK here when you want real backpressure on the synthesizer` — the policy this bug defeats
- `src/easycat/session/_builder.py:144` — comment claims `DROP_NEWEST trims only the tail when the transport falls behind` — under transient backpressure it drops whatever arrives while full, which mid-utterance is a gap, not a tail

*Verified:* HEAVILY NARROWED. The one-shot `_put_after_wait` bug is real and verified at line 159-172. I REJECTED the finding's other two claims: (a) drops are not invisible — `_note_drop` (lines 68-81) bumps the `easycat.queue.dropped.total` OTel counter and fires the `on_drop` hook, which `Session._on_queue_drop` (_session.py:504-526) turns into an `audio_queue_drop` journal record wired in at `_builder.py:151`; (b) the `_replay_chunks_pending` inflation is already reconciled — `_audio_router.py:878-883` zeroes the tally when the queue empties with replay chunks still pending, and the comment there says so. DOWNGRADED medium -> low: `_OUTBOUND_QUEUE_POLICY` is `DROP_NEWEST` (_builder.py:59), so BLOCK is opt-in only and the race additionally needs two producers hitting a full queue in the same wakeup.

#### 31. Three background tasks bypass the repo's own done-callback idiom, so a raising handler dies with no journal record

`LOW` · `correctness` · `effort: small`

**Problem.** The codebase applies `RuntimeScope.create_journaled_task` + `add_done_callback(log_task_exception)` consistently, but `TurnManager`'s silence timer and `PeriodicHealthChecker`'s loop use a bare `asyncio.create_task` and never consume the terminal result. If either coroutine raises anything other than `CancelledError`, the task dies with no log line, no journal record, and no `Error` event — only a 'Task exception was never retrieved' warning at GC time.

**Impact.** Under an opt-in strict bus (`EventBus(handler_error_policy="raise")`, which events.py:660-662 recommends for 'tests or strict app code'), a buggy `TurnEnded` handler kills the silence timer after `_transition(PROCESSING)` has already run, so the turn is stuck in PROCESSING and never dispatches to the agent; a buggy `Error` handler stops health checking permanently. Both are diagnosable only from a stray stderr warning.

**Fix.** Attach `RuntimeScope.log_task_exception` (or route through the existing `BackgroundTaskScope` at `runtime/scope.py:15`, which already consumes terminal results) to `turn_manager.py:497` and `_health_check.py:85`. Cheapest equivalent: add `except Exception: logger.exception(...)` alongside the existing `except asyncio.CancelledError` at `turn_manager.py:604` and `_health_check.py:117`.

**Evidence**

- `src/easycat/turn_manager.py:497` — `self._silence_timer_task = asyncio.create_task(self._silence_timeout())` — no `add_done_callback`
- `src/easycat/turn_manager.py:604` — `_silence_timeout` catches only `asyncio.CancelledError`; an exception from `await self._event_bus.emit(TurnEnded(...))` (line 600) escapes unretrieved
- `src/easycat/_health_check.py:85` — `self._task = asyncio.create_task(self._run())` — no done callback
- `src/easycat/_health_check.py:110` — `_run` catches only CancelledError, and `check_once`'s `except Exception` (line 103) covers only `provider.health_check()` — a raise out of `_record_failure` -> `_emit_error` -> `event_bus.emit` (line 156) kills the loop
- `src/easycat/runtime/scope.py:214` — `RuntimeScope.log_task_exception` is the house idiom, attached to every other background task (e.g. _audio_router.py:294, _stt_committer.py:429, _turn_runner.py:400)

*Verified:* Both surviving sites verified. REJECTED the finding's third site: `_provider_helpers.py:76`'s emit tasks ARE consumed — `_drain_emit_tasks` (lines 93-105) gathers them with `return_exceptions=True`, so there is no unretrieved-exception warning there. CORRECTED the impact mechanism: `_silence_timeout` calls `_transition(PROCESSING)` at line 595 *before* the emit at line 600, so a dead task strands the FSM in PROCESSING, not USER_PAUSED as claimed. DOWNGRADED medium -> low: the default `handler_error_policy` is `"continue"` (events.py:671), under which the bus swallows handler exceptions and neither task can die this way — the trigger is opt-in.

#### 32. TTSSynthesizer.cancel() swallows provider exceptions with a bare pass, unlike every comparable site

`LOW` · `correctness` · `effort: small`

**Problem.** `TTSSynthesizer.cancel` is the path that tells the TTS provider to stop generating on barge-in, and it discards every exception without even a `logger.debug`. Every other 'best-effort teardown' swallow in this codebase logs with `exc_info=True`.

**Impact.** If a custom or out-of-tree TTS provider raises out of its `cancel()`, the failure is invisible in the logs, the journal, and the event bus, so an operator investigating a barge-in that did not stop the provider has nothing to correlate.

**Fix.** In `src/easycat/_tts_synthesizer.py`, replace the bare `pass` at line 238 with `logger.warning("TTS provider cancel failed", exc_info=True)`, matching `_stt_committer.py:452`.

**Evidence**

- `src/easycat/_tts_synthesizer.py:236` — `try: await self._tts.cancel() / except Exception: pass` (lines 235-238) — no log, no event, no journal record
- `src/easycat/session/_tts_scheduler.py:312` — `await self._synth.cancel()` is the first thing `TTSScheduler.cancel()` does, i.e. the barge-in and stop paths
- `src/easycat/session/_stt_committer.py:452` — the comparable swallow logs: `logger.debug("STT end_stream during cancel raised", exc_info=True)`; same at _session.py:1222 and _audio_router.py:1000

*Verified:* Code verified verbatim (lines 235-238; the original cited 235 for the try/except, the actual call is 236 and the `pass` is 238). SEVERITY CUT medium -> low because I disproved the impact story for the bundled providers: `tts/base.py:154-157` sets `_cancelled`/`_active` before any network work so local playback stops regardless, and the persistent-socket path already bounds and logs its own failures — `tts/_multi_context_ws.py:231-242` wraps the cancel frame in `wait_for(_CANCEL_SEND_TIMEOUT)`, logs with `exc_info=True`, and drops the socket on failure. The 'provider keeps streaming and keeps billing with zero signal' scenario is therefore not reachable through ElevenLabs/Cartesia; what remains is a genuine but cosmetic inconsistency.

---

### Provider abstraction and third-party extensibility

*11 findings — 11 medium*

**Assessment.** The provider layer has a well-shaped skeleton — one `ProviderSpec` per backend feeding a `ProviderCatalog` whose derived views really do drive doctor, redaction, and scaffolding; entry-point discovery with per-plugin failure isolation; and an installable pytest contract kit. But the abstraction is honest about metadata and dishonest about behavior: every path that decides *how the pipeline runs* — whether an STT does its own endpointing, what PCM rate a TTS emits, whether a TTS accepts SSML — is a closed `isinstance` chain over the four bundled vendors, so a third-party provider is structurally second-class no matter how well it conforms. The single biggest problem is that the documented conformance check (`isinstance(x, STTProvider)`) is worthless — it passes for a class whose members are integers — while the real contracts the Session depends on (`events()` is re-invoked per turn and must return a fresh terminating iterator; `commit_segment() -> True` obligates a subsequent FINAL) are written nowhere and tested nowhere, so a "conformant" third-party provider fails on turn two or stalls the turn for `stt_timeout`. Genuinely well done: the WebSocket lifecycle work (Deepgram's bare-finalize-ack containment, close-before-drain), the `ProviderErrorEmitter` strong-task-reference mixin, and the batch-buffer cap taxonomy in `STTBase` are careful, real-world-scarred code.

**Done well here:**

- `ProviderSpec`/`ProviderCatalog` (src/easycat/_provider_catalog.py:34-77) derives env-var, install-extra, and API-domain views from a single declaration, and those views genuinely drive `easycat doctor` credential checks, bundle URL redaction (`sensitive_api_domains`), and `easycat init` extras — that part of the registry earns its keep.
- Entry-point discovery (src/easycat/_provider_catalog.py:116-132) sets the `_discovered` flag *before* the loop and wraps each `entry_point.load()` in its own try/except, so a broken third-party plugin logs a warning instead of breaking every other provider — correct failure isolation, and documented in docs/extending/stt.md:136-170.
- `TransportLike` (src/easycat/providers.py:230-256) exists specifically so the pre-built-instance discrimination in `_create_transport` does not reject third-party transports that predate `version_info()`. That is the right instinct about protocol width, and the reasoning is written down at the call site (src/easycat/config/_factory.py:131-136).
- `ProviderErrorEmitter` (src/easycat/_provider_helpers.py:17-104) centralizes a genuinely subtle async bug — keeping a strong reference to a fire-and-forget `bus.emit` task so the loop cannot GC it mid-emit — and adds `_drain_emit_tasks()` so teardown does not leak pending tasks. One copy, well explained.
- `STTBase._extend_limited_audio_buffer` / `_buffer_batch_audio_or_finalize` (src/easycat/stt/base.py:204-319) draws a real distinction: an oversized single chunk is a hard `ValueError`, a cumulative cap is `AudioBufferLimitExceeded` that gracefully finalizes the utterance so a long-talking caller does not tear down a live call.
- Every bundled streaming STT sets `expected_sample_rate=None` and resamples inbound audio to its own target in `_on_audio` (src/easycat/stt/deepgram_provider.py:111-119, src/easycat/stt/cartesia_provider.py:104-113), so users can swap STT providers without a format crash — and the convention is documented at each construction site.
- The contract kit's `collect_events` (src/easycat/testing/contracts.py:130-143) wraps stream drains in `asyncio.timeout` and fails with an explanatory message instead of hanging the suite — the right ergonomics for a kit third parties will run against real backends.
- Deepgram's persistent-socket turn-boundary handling (src/easycat/stt/deepgram_provider.py:320-345, 392-416) correctly contains the case where the provider acks `Finalize` with no transcript body so a late FINAL cannot bleed across a turn boundary — unusually careful realtime-protocol work.

#### 33. Docs and the generated scaffold advertise `isinstance(x, Protocol)` as the provider conformance check, and it accepts objects that cannot work

`MEDIUM` · `api-ergonomics` · `effort: small`

**Problem.** `@runtime_checkable` Protocols check attribute presence only — never signature, arity, sync-vs-async, or return type. Four extending docs pages and the generated `easycat init --template provider` scaffold present `isinstance(..., SomeProvider)` as the headline conformance test for a third-party provider. A provider whose `events()` is synchronous, or whose `commit_segment()` returns `None` instead of `bool`, passes it.

**Impact.** A third-party author copies the four-line test from docs/extending/stt.md, sees it pass, and ships. On the first turn `_stt_committer.py:366` does `async for stt_event in self._stt_getter().events()` and raises `TypeError: 'list' object is not async iterable` inside a background task; or `commit_segment()` returns `None` (falsy), so `_stt_committer.py:284` treats the segment as uncommitted and resolves the transcript future with `""` — an empty user turn with no exception. The check that was supposed to catch this is why they stopped looking.

**Fix.** Rewrite the "## Verifying conformance" section of docs/extending/README.md (line 41) and the four per-stage pages to lead with subclassing the shipped kit (`class TestAcmeSTT(STTProviderContractSuite): provider_factory = AcmeSTT`), demoting `isinstance` to a one-line footnote stating that it checks names only. Change src/easycat/cli/scaffold/templates/provider/test_custom_vad.py:27-28 to subclass `easycat.testing.VADProviderContractSuite` instead of asserting `isinstance`. The pattern to cite is already in-tree at src/easycat/config/_factory.py:257-269, where `_validate_agent_shape` explicitly tightens a Protocol check with `inspect.iscoroutinefunction` because "`@runtime_checkable` only checks method-name presence".

**Evidence**

- `docs/extending/README.md:43` — "Each protocol in `easycat.providers` is `@runtime_checkable`, so the cheapest conformance check is structural", with `assert isinstance(MySTT(), STTProvider)` at line 49. Verified verbatim.
- `docs/extending/stt.md:78` — `assert isinstance(FixedSTT(), STTProvider)` is the first test under "## Verifying conformance" (line 71). Same pattern verified at tts.md:73, vad.md:84, transport.md:103.
- `src/easycat/cli/scaffold/templates/provider/test_custom_vad.py:27` — `def test_conforms_to_vad_provider_protocol()` is a bare `assert isinstance(EnergyVAD(), VADProvider)` (line 28). Verified — the finding cited line 28, the def is at 27.
- `src/easycat/providers.py:24` — `@runtime_checkable` on `VersionedProvider` and every provider Protocol. Verified empirically in the project venv (Python 3.14.6): a class whose `start_stream`/`send_audio`/`commit_segment`/`end_stream`/`events`/`version_info` are the integers 1-6 returns `True` from `isinstance(x, STTProvider)`; so does an all-synchronous class whose `events()` returns a `list`.

*Verified:* Empirically reproduced both isinstance passes in /home/yi/Code/easycat-2/.venv. CORRECTIONS: (1) The finding claims the shipped kit "is name-only" — that is false as a characterization of the suite. `src/easycat/testing/contracts.py:210` already asserts `isinstance(committed, bool)` and `collect_events` at :206-220 would fail on a synchronous `events()`. The kit catches exactly the two failures the finding describes; the docs do not point at it. I therefore dropped the finding's recommendation to rewrite the kit's `test_satisfies_*_protocol` tests and refocused on docs + scaffold. (2) I dropped the `src/easycat/config/_factory.py:137` (TransportLike) evidence: the code comment at :131-136 documents that narrowing as deliberate, and the "dataclass with str fields named connect/disconnect/receive_audio/send_audio" case is contrived (I confirmed it returns True, but no real transport config has those field names). (3) Downgraded high -> medium: the harm is a docs on-ramp defect, not a runtime bug, and the fix is a docs edit.

#### 34. `events()` must return a fresh iterator per turn and `commit_segment()->True` obligates a FINAL — neither obligation is written down, and the contract kit tests neither

`MEDIUM` · `correctness` · `effort: medium`

**Problem.** The Session's two implicit STT contracts are undocumented. (1) `events()` is invoked once per turn and must return a fresh iterator that terminates after `end_stream()` — `STTBase.events()` (src/easycat/stt/base.py:114) happens to be an async generator function so built-ins satisfy this by construction, but nothing states it. (2) `commit_segment() -> True` is treated as a promise that exactly one FINAL will follow, and the turn blocks on a future until it arrives. Neither appears in `providers.py`, in docs/extending/stt.md, or in `STTProviderContractSuite`.

**Impact.** A provider that caches one async generator (`def events(self): return self._iter`) passes the entire shipped contract kit and then yields nothing from turn two onward: the consumer task exits immediately on the exhausted generator, the segment future resolves with `""`, and the bot answers empty user turns with no error. A provider that acks commits optimistically without guaranteeing a FINAL stalls every turn for the full 10s `stt_timeout` before `await_pending` emits `STTTimeoutError`. Separately, docs/extending/stt.md:93 sends readers to look for an `aclose` teardown case in tests/contracts/ that does not exist.

**Fix.** Write both obligations into the `STTProvider` docstrings in src/easycat/providers.py: at :98, that `events()` must return a NEW iterator on every call and must terminate after `end_stream()`; at :86, that returning True is a promise to emit exactly one FINAL and providers that cannot guarantee it must return False. Add two cases to `STTProviderContractSuite` in src/easycat/testing/contracts.py: a two-cycle test (start->send->commit->end->drain, repeated, asserting the second cycle also yields a FINAL) and a commit->FINAL assertion. Either add the `aclose` teardown case or delete the claim at docs/extending/stt.md:93.

**Evidence**

- `src/easycat/session/_turn_runner.py:261` — `await stt.start_stream()` at the top of every turn; `self._stt.start_event_loop(turn)` at line 273. Verified.
- `src/easycat/session/_stt_committer.py:366` — `async for stt_event in self._stt_getter().events():` inside `_consume()`, which `start_event_loop` spawns — so `events()` is called anew once per turn for the session's lifetime. Verified.
- `src/easycat/providers.py:98` — `def events(self) -> AsyncIterator[STTEvent]` docstring is one line: "Return an async iterator of provider-scoped STT events." No statement that repeated calls must yield a fresh iterator, or that it must terminate after `end_stream()`. Verified.
- `src/easycat/providers.py:86` — `commit_segment` docstring: "Returns ``True`` when the provider accepted a segment commit request. Providers that do not support segmented commits should return ``False``." Never states that True obligates exactly one subsequent FINAL. Verified.
- `src/easycat/session/_stt_committer.py:284` — `if not committed:` is the only recovery path — it removes the future and resolves it with `""`. A provider returning True but never emitting a FINAL leaves the enqueued future unresolved until `await_pending` times out at :301 under `stt_timeout` (default 10.0s, src/easycat/timeouts.py:93). Verified.
- `src/easycat/testing/contracts.py:206` — `test_stream_lifecycle_yields_normalized_events` runs exactly one start->send->commit->end->drain cycle; `test_end_stream_is_idempotent` at :223 never calls `start_stream` twice. No kit test exercises a second turn or asserts commit->FINAL. Verified.
- `docs/extending/stt.md:93` — Claims the in-tree contract covers "teardown via `aclose`". Verified false: `grep -rn aclose tests/contracts/` exits 1 (no match).

*Verified:* Every cited line number verified exact. Confirmed `STTBase.events()` (src/easycat/stt/base.py:114-120) is an `async def ... yield` generator function, so calling it returns a new generator each time — the framework's reliance is real but silently satisfied by every built-in. The `aclose` docs claim is verified false (grep exit 1). CORRECTION / DOWNGRADE high -> medium: the cached-iterator failure requires a specific and somewhat unnatural implementation choice, and the docs' own `FixedSTT` example at docs/extending/stt.md:53-55 uses the correct async-generator form, so a reader copying the example is safe. The gap is a missing written contract plus missing kit coverage, not a live bug.

#### 35. `EasyConfig`'s native-endpointing detection is a closed isinstance chain over three bundled STT configs; a registered third-party provider silently gets the wrong pipeline

`MEDIUM` · `provider-abstraction` · `effort: medium`

**Problem.** Whether an STT provider does its own endpointing — the decision that switches EasyCat's Silero VAD stage and smart-turn off entirely — is resolved by `isinstance` against three bundled config classes. A provider registered through the public `register_stt_provider` API reaches `_stt_uses_native_endpointing` and falls through to `return False`. There is no capability attribute, optional Protocol, or `ProviderSpec` field to declare it, and `ProviderSelection.capabilities` (the existing capability container) is hardcoded empty for stt/tts.

**Impact.** A third-party STT with native endpointing (the modern default — Deepgram Flux, Cartesia ink-2, and ElevenLabs realtime all have it) configured through `EasyConfig` runs with EasyCat's Silero VAD and manual segment commits layered on top of its own turn detection: double endpointing, duplicate FINALs, truncated turns — the exact failure the docstring at config/easy.py:180-184 says the chain exists to prevent. `easycat plan` and `/plan` also report an empty capability set for every stt/tts role, so the misconfiguration is invisible to the diagnostic surface.

**Fix.** Add a declarative field to `ProviderSpec` (src/easycat/_provider_catalog.py:34) — e.g. `provides_native_endpointing: bool = False` — plumbed through `register_stt_provider`, and replace the isinstance chain at src/easycat/config/easy.py:195-201 with a catalog lookup that keeps the three built-in predicates as spec-supplied callables. Populate `ProviderSelection.capabilities` from the same source at src/easycat/planning/provider_plan.py:200 and :471. At minimum, document in docs/extending/stt.md that a natively-endpointing third-party STT must be wired through `SessionConfig` with `auto_turn_from_stt_final=True, enable_vad=False` rather than `EasyConfig`.

**Evidence**

- `src/easycat/config/easy.py:195` — `_stt_uses_native_endpointing` (def at :176) is `isinstance(stt, DeepgramSTTConfig)` / `CartesiaSTTConfig` / `ElevenLabsSTTConfig` with `return False` at :201. Verified — no attribute, protocol, or ProviderSpec field lets a provider declare it.
- `src/easycat/config/_factory.py:246` — `_should_auto_turn_from_stt_final` (def at :227) ends in `return _stt_uses_native_endpointing(config.stt)`; line 425 does `enable_vad = not auto_turn_from_stt_final`. Verified: this one chain decides whether the Silero VAD stage and smart-turn run at all.
- `src/easycat/config/_tts_alignment.py:76` — Docstring states it directly: "Any other catalog-registered config (e.g. a third-party provider added via ``register_tts_provider``) reaches this function too ... and falls through unchanged." Dispatch at :84-105 is isinstance over the four built-in configs. Verified.
- `src/easycat/planning/provider_plan.py:200` — `capabilities=frozenset()` for the catalog-role selection path (also at :471), so `ProviderSelection.capabilities` is always empty for stt and tts — the one structure shaped like a capability model carries nothing for the two extensible surfaces. Verified.

*Verified:* All isinstance chains and the empty `capabilities=frozenset()` verified exact. MITIGATION FOUND, so I downgraded high -> medium and rewrote the scope: `SessionConfig` (src/easycat/session/_types.py) exposes `enable_vad: bool = True` (line 162) and `auto_turn_from_stt_final: bool = False` (line 163) as direct public fields, so a third party CAN opt out — just not through `EasyConfig`'s auto-detection. The finding's "there is no way" is wrong. I also REMOVED the TTS output-rate half of this finding as largely mitigated: `WebSocketTransport.send_audio` sends an `audio_format` control message when the outbound rate changes (src/easycat/transports/websocket.py:154-157, and again at :352-355), `TwilioMediaTransport` resamples outbound to 8k (src/easycat/transports/twilio_media.py:943), and `LocalTransportConfig.audio_format` defaults to `PCM16_MONO_24K` (src/easycat/transports/local.py:48), which matches the framework's TTS default — so the chipmunk scenario needs both a third-party TTS at a non-24k rate AND a LocalTransport, a much narrower case than claimed.

#### 36. A third-party provider registered with an extra name that is not an importable module pins `/health/ready` to not-ready forever

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `ProviderCatalog.register(..., extra="acme-speech")` stores a pyproject extra name. The planner has no per-provider probe module, so `probe_module_for_extra` falls back to probing the extra name itself. PyPI extras routinely use hyphens; Python module names cannot. `find_spec` returns None, the extra is reported missing, and it is a blocking readiness gap. Built-in extras dodge this only via the hand-maintained `EXTRA_PROBE_MODULE` table at transport_registry.py:40-59, which third parties cannot extend.

**Impact.** A team shipping a provider plugin and deploying behind `easycat.server` gets `/health/ready` stuck at not-ready with reason `missing_extra:<their-extra>`, naming an extra that IS installed. Readiness probes never pass and the deployment never receives traffic. The only workarounds are naming the extra exactly like an importable module or passing `extra=None`, which forfeits `easycat init` scaffold integration.

**Fix.** Add `probe_module: str | None = None` to `ProviderSpec` (src/easycat/_provider_catalog.py:34-42) and to `register_stt_provider` / `register_tts_provider`, and have `probe_module_for_extra` (src/easycat/planning/transport_registry.py:260-280) consult the catalog before its name-as-module fallback. Document the constraint in the metadata table at docs/extending/stt.md:132-136 and its TTS counterpart either way.

**Evidence**

- `src/easycat/planning/transport_registry.py:280` — `return EXTRA_PROBE_MODULE.get(extra, extra)` — an unmapped third-party extra has its NAME probed as a Python module. The docstring at :269-275 acknowledges the tradeoff explicitly. Verified.
- `src/easycat/planning/provider_plan.py:136` — `_extra_is_missing` (:136-141) -> `_module_available` (:130-135) -> `importlib.util.find_spec`. Verified empirically in the project venv: `find_spec("acme-speech")` returns `None` (it does not raise), so `_module_available` returns False and the extra reads as missing.
- `src/easycat/planning/provider_plan.py:426` — `if _extra_is_missing(choice.extra): ... missing_extras.add(choice.extra)` — blocking unless the role carries `degrades_to_passthrough`, which catalog stt/tts roles never do (`capabilities=frozenset()` at :200/:471). Verified.
- `src/easycat/server/voice_server.py:545` — `return True, plan.blocking_errors()` from `_manifest_readiness` (:519), feeding the `/health/ready` payload built at :509-517. `blocking_errors` composes `missing_extra:{extra}` reasons at planning/provider_plan.py:120. Verified.
- `docs/extending/stt.md:115` — The registration example passes `extra="yours"` with no hint the string must be an importable module name; the metadata table at :132-136 says only that `extra` feeds the `easycat init` scaffold. Verified.

*Verified:* CORRECTED MECHANISM: the finding says `find_spec` "raises" on a hyphenated name. It does not — I ran it in the project venv and `find_spec("acme-speech")` returns `None`. The outcome (extra reported missing, blocking) is identical, and the same failure applies to any third-party extra whose name is not an importable module, not just hyphenated ones. DOWNGRADED high -> medium: the fallback is a deliberate, documented tradeoff (the docstring at transport_registry.py:269-275 weighs it against returning None) and it only bites third-party registered providers behind the manifest-driven server; a factory-only server returns `(None, None)` from `_manifest_readiness` (voice_server.py:534-538) and is unaffected. The missing escape hatch is the genuine gap. Cited line numbers corrected: voice_server.py:545 not 547; docs/extending/stt.md:115 not 108.

#### 37. `model_api_version` in the validation surface tables is hand-maintained and 5 of 9 rows disagree with the config defaults they describe

`MEDIUM` · `maintenance-burden` · `effort: medium`

**Problem.** `ProviderSurfaceSpec.model_api_version` is typed by hand in `LIVE_PROVIDER_SURFACES` and again in `tests/contracts/provider_surface_matrix.py`. Checking each row against the actual config default, 5 of 9 are wrong (openai STT, deepgram STT, deepgram TTS, elevenlabs TTS, cartesia TTS). Two more (elevenlabs STT, cartesia STT) point at `model = None` lazily-resolved defaults. No test asserts correspondence, so the drift is invisible.

**Impact.** `easycat validate` emits `provider_capability_report` artifacts whose `api_version` and `models` fields name models the configured provider does not use — reporting `whisper-1` when the default is `gpt-4o-transcribe`, `nova-3` when it is `nova-2`, `sonic-2` when it is `sonic-3`. An operator diagnosing a transcription- or voice-quality regression from a validation report is reading stale strings. Every provider version bump silently re-breaks it because nothing connects the two tables to the config classes.

**Fix.** Derive `model_api_version` at report time instead of typing it: in `build_provider_capability_report` (src/easycat/validation/provider_reports.py:235), resolve it from `config_cls()` using the existing `MODEL_FIELD` convention already implemented at src/easycat/_provider_catalog.py:224 (`getattr(cfg, getattr(cfg_cls, "MODEL_FIELD", "model"))`), preferring a `resolved_model` property when the config exposes one (CartesiaSTTConfig.resolved_model at stt/cartesia_provider.py:62). Then delete the field from `ProviderSurfaceSpec` (:35) and from `ProviderSurfaceContract` (tests/contracts/provider_surface_matrix.py:23). If the field must stay hand-typed, add a test asserting each row matches its config default.

**Evidence**

- `src/easycat/validation/provider_reports.py:88` — `model_api_version="whisper-1"` for openai STT vs `OpenAISTTConfig.model = "gpt-4o-transcribe"` (src/easycat/stt/openai_provider.py:49). Verified both.
- `src/easycat/validation/provider_reports.py:114` — `model_api_version="nova-3"` for deepgram STT vs `DeepgramSTTConfig.model = "nova-2"` (src/easycat/stt/deepgram_provider.py:38). Verified both.
- `src/easycat/validation/provider_reports.py:159` — deepgram TTS `"aura-2"` vs `DeepgramTTSConfig.model = "aura-asteria-en"` (tts/deepgram_tts.py:36); elevenlabs TTS `"eleven_v3"` (:172) vs `model_id = "eleven_flash_v2_5"` (tts/elevenlabs_tts.py:64); cartesia TTS `"sonic-2"` (:186) vs `model_id = "sonic-3"` (tts/cartesia_tts.py:48). All six values verified.
- `src/easycat/validation/provider_reports.py:253` — `api_version=spec.model_api_version` and `models=(ProviderIdentifier(spec.model_api_version, safe=True),)` at :261 — the stale string is what lands in the emitted `ProviderCapabilityReport`. Verified.
- `tests/contracts/provider_surface_matrix.py:46` — `model_api_version="whisper-1"` duplicated in a second hand-typed table. Verified; no test compares the two tables or checks either against the config defaults.
- `src/easycat/validation/provider_reports.py:74` — Comment: "The remaining fields (protocol/mode/model_api_version/...) have no registry source and are intentionally held here as validation-only metadata" — the drift is by design, and unguarded. Verified.

*Verified:* All five stale values independently verified by reading both the table row and the config dataclass default. CORRECTIONS, both narrowing the finding: (1) The impact claim that "anyone debugging from a debug bundle is reading fiction" is FALSE. `build_provider_capability_report` is called only from src/easycat/validation/_live_runner.py:474 — the validation lane. A session debug bundle records `provider_versions` from each provider's own `version_info()` (e.g. stt/deepgram_provider.py:483 returns `self._config.model`), which is accurate. The blast radius is validation artifacts only. (2) The claim that tests/contracts/provider_surface_matrix.py's "rows are a verbatim subset" is FALSE — it is a superset covering vad and transport surfaces too and adds contract_path/cassette_path/cassette_status fields, so it cannot simply be collapsed into provider_reports.py. I dropped that recommendation. (3) I dropped the "CLAUDE.md one-spec rule is off by 8x" half: both `_PROVIDER_PROBE_URL` (src/easycat/cli/diagnose/doctor.py:263-267 `continue`s for unmapped providers) and `EXTRA_PROBE_MODULE` have graceful third-party fallbacks, so adding a THIRD-PARTY provider really is one registration call; only adding a BUILT-IN touches many files, which is a maintainer cost, not an extensibility defect. DOWNGRADED high -> medium.

#### 38. `PauseProcessor` defaults to `style="ssml"`, so `pause_ms` is inert with all four shipped TTS providers

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `PauseProcessor` emits `<speak>...<break time="140ms"/>...</speak>` with `format="ssml"` by default. The TTS scheduler asks the provider's `input_policy` whether it accepts SSML; no bundled provider does, so it strips the tags. Running the full path in the project venv, `default_pronunciation_processors(phone_pause_ms=140)` on "Call me at 415 555 0142 today." reaches `synthesize()` as `'Call me at 4 1 5 5 5 5 0 1 4 2 today.'` — digit spacing survives, every requested `<break>` is gone. The `pause_ms` / `phone_pause_ms` arguments have no effect with any provider EasyCat ships.

**Impact.** A developer following the README's phone-number-pacing example, or calling `default_pronunciation_processors(phone_pause_ms=140)`, gets no timed pauses and believes 140 ms between digits is configured. The one shipped default (`"ssml"`) is the one that does nothing; `style="ellipsis"` works. README.md:559-560, the primary discovery path, describes the strip as a graceful fallback rather than the loss of the feature.

**Fix.** Change the default at src/easycat/llm_output_processing.py:75 to `style: PauseStyle = "ellipsis"` and pass `style="ellipsis"` explicitly in `default_pronunciation_processors` (:189-195), so the shipped convenience stack actually produces pauses. Update README.md:559-560 to state plainly that SSML `<break>` tags are dropped for every provider EasyCat currently ships and that `style="ellipsis"` is the working option — the accurate wording already exists at docs/using-easycat/04-tools-actions/README.md:144-148 and docs/teaching/14-bring-your-own-agent/README.md:776-781.

**Evidence**

- `src/easycat/llm_output_processing.py:75` — `style: PauseStyle = "ssml"` — the default. Verified.
- `src/easycat/llm_output_processing.py:190` — `default_pronunciation_processors` constructs `PauseProcessor(pattern=..., pause_ms=phone_pause_ms, unit_pattern=r"\d", minimum_units=7)` with no `style=`, so the advertised convenience helper inherits the SSML default. Verified.
- `src/easycat/tts/base.py:136` — `def input_policy(self) -> TTSInputPolicy: return TTSInputPolicy.plain_text()` — verified to be the only concrete `input_policy` in src/ (the other is the Protocol declaration at providers.py:163). No bundled provider overrides it; `TTSInputPolicy.plain_text()` sets `accepted_formats=("plain",)` (tts/input.py:69-73), and I confirmed `OpenAITTS(...).input_policy.accepts("ssml")` is False.
- `src/easycat/session/_tts_scheduler.py:169` — `ssml_downgraded = processed_payload.format == "ssml" and not input_policy.accepts("ssml")` -> line 171 replaces the payload with `strip_ssml_tags(...)`. Verified.
- `README.md:515` — The headline output-processor example uses `PauseProcessor(pattern=..., pause_ms=140)` with no `style=`; the convenience-helper example at :528-534 does the same. Line 559 frames the strip as a benign "Providers that do not support SSML automatically fall back to plain text" and :560 says "Pause length is adjustable via `pause_ms` for SSML" without saying that no shipped provider consumes it.

*Verified:* Reproduced end-to-end in the project venv: the processed payload is `format="ssml"` with nine `<break time="140ms"/>` tags, and `strip_ssml_tags` reduces it to spaced digits. MITIGATION FOUND, so I downgraded high -> medium and corrected two claims. (1) "The only signal is an `ssml_downgraded: true` field buried in a journal record" is FALSE — the behavior is documented in at least four places: docs/using-easycat/04-tools-actions/README.md:144-148 ("`PauseProcessor` defaults to SSML breaks. The default OpenAI TTS path accepts plain text, so EasyCat would strip unsupported SSML tags"), docs/teaching/06-streaming-agent/README.md:725-733, docs/teaching/14-bring-your-own-agent/README.md:776-781, and .../EXERCISES.md:199 ("None of the four bundled TTS providers currently accepts SSML"). Only README.md is misleading. (2) "every requested pause deleted" / "zero pauses" overstates it: digit spacing survives the strip, so there is some pacing effect — the exact break duration is what is lost, as the teaching doc already says. I dropped the finding's third recommendation (a one-time warning in `TTSScheduler.prepare`) as unnecessary once the default is fixed.

#### 39. Providers never map failures into the EasyCat error taxonomy; `EASYCAT_E304`/`E305` are registered, documented, and never raised

`MEDIUM` · `error-handling` · `effort: medium`

**Problem.** There is a first-class error taxonomy (`EasyCatError` with stable codes, an `easycat explain` surface, registry-supplied fix text) that no provider uses — `grep -rn 'easycat.errors' src/easycat/{stt,tts,vad,transports}/` returns nothing. Three STT providers behave three different ways on failure: Deepgram emits a bus Error, ElevenLabs does so in realtime but leaks `httpx.HTTPStatusError` in batch, and OpenAI leaks vendor exceptions and structurally cannot emit because its config lacks the `event_bus` field every sibling declares. Meanwhile E304/E305 are registered for exactly the mid-call-disconnect case that websocket_base.py:199 handles with an untyped `ConnectionError`.

**Impact.** An application cannot write one `except` clause or one `Error` subscriber covering provider failure — the exception type depends on which provider is configured, and swapping `stt="openai/..."` for `stt="deepgram/..."` changes both the type and whether the failure is journaled at all. An operator following `easycat explain EASYCAT_E304` to diagnose a mid-call drop finds documentation for a code that appears in no bundle. Third-party authors have no in-tree example of correct error mapping, so they will leak vendor exceptions too.

**Fix.** Add `event_bus: Any = field(default=None, repr=False)` to `OpenAISTTConfig` (src/easycat/stt/openai_provider.py:47-65) so it matches every sibling and `inject_event_bus` wires it. Wrap the mid-call death at src/easycat/stt/websocket_base.py:199 in `EASYCAT_E304(provider=..., detail=...)` and the reconnect-exhaustion path in `EASYCAT_E305`, or delete both codes from src/easycat/errors.py:402-436 and from the `__all__` at :625-626 if the intent changed. Add a "map failures into the EasyCat error taxonomy" bullet to the "Ground rules for every provider" list in docs/extending/README.md (starts :85), which today covers async-first, `version_info()`, cancellation, and teardown but not errors.

**Evidence**

- `src/easycat/errors.py:402` — `EASYCAT_E304 = register("EASYCAT_E304", "Provider {provider!r} became unreachable mid-call: {detail}", ...)`; `EASYCAT_E305` at :421 for reconnect exhaustion. Verified dead: `grep -rn 'E304|E305' src/ tests/ docs/` hits only errors.py itself (:402/:403/:413/:418/:421/:422/:435/:625/:626) and tests/cli/test_errors.py:137-138, which merely enumerates registered codes.
- `src/easycat/stt/websocket_base.py:199` — The actual mid-call-death path is `self._emit_provider_error(ConnectionError(f"{label} STT WebSocket died mid-stream"))` — a bus Error carrying an untyped `ConnectionError` with no EasyCat code, so `easycat explain EASYCAT_E304` documents a code no bundle will ever contain. Verified.
- `src/easycat/stt/openai_provider.py:29` — `OpenAISTTConfig` has no `event_bus` field (fields verified at :47-65), unlike cartesia_provider.py:59, openai_realtime_provider.py:89, tts/openai_tts.py:79, and the Deepgram/ElevenLabs configs. `inject_event_bus` (_provider_catalog.py:29) only fills a declared field, so `OpenAISTT` can never emit a journal-visible provider Error.
- `src/easycat/stt/openai_provider.py:200` — `response.raise_for_status()` inside the streaming transcription attempt — a raw `httpx.HTTPStatusError` propagates out with no provider attribution and no bus record. Verified.
- `src/easycat/stt/elevenlabs_provider.py:596` — `_transcribe_batch`'s `response.raise_for_status()` escapes raw, while the same provider's realtime path emits bus errors via `_emit_provider_error_from_message` — the provider is inconsistent with itself. Verified.
- `src/easycat/stt/deepgram_provider.py:380` — By contrast Deepgram maps provider Error frames through `_emit_provider_error_from_message` with provider/description context. Verified.

*Verified:* E304/E305 confirmed dead (only errors.py plus a test that enumerates codes). `OpenAISTTConfig` field list read in full — no `event_bus`. CORRECTION: the finding implies the mid-call-death path is entirely unobservable; it is not. `websocket_base.py:199` does call `_emit_provider_error(...)`, so WebSocket-based STT providers DO put a bus `Error` in the journal — it just carries a bare `ConnectionError` with no stable code. The real defect is the missing code mapping plus the OpenAI batch path's total absence of bus emission. Severity confirmed at medium, not raised: this is a consistency and diagnosability gap, not data loss or an outage.

#### 40. The shipped `easycat.testing` contract kit is referenced by zero user-facing docs; extending pages point at in-repo test files a pip user does not have

`MEDIUM` · `discoverability` · `effort: small`

**Problem.** EasyCat ships a factory-parametrized pytest contract kit (`STTProviderContractSuite`, `TTSProviderContractSuite`, `VADProviderContractSuite`, `TransportContractSuite`, `AgentBridgeContractSuite`) explicitly designed for out-of-tree authors. A grep across `docs/`, `README.md`, `examples/`, and the scaffold templates finds zero references. Every extending page instead tells readers to "mirror" test files that exist only in the git checkout, and the module is not listed in docs/public-api.md or pinned by tests/test_public_api.py.

**Impact.** The one artifact that would give a third-party provider author real behavioral coverage is invisible to them; they copy the four-line `isinstance` test from the docs instead. The kit meanwhile accrues maintenance cost (guarded by `just guard-contracts`, imported by five test files) with no external users, and because it is undocumented and unpinned its API can be broken by any refactor without a failing test.

**Fix.** Replace the "Verifying conformance" section of each docs/extending/*.md page (README.md:41, stt.md:71, tts.md, vad.md, transport.md) with a subclass-the-suite example, and change the generated scaffold test at src/easycat/cli/scaffold/templates/provider/test_custom_vad.py to subclass `easycat.testing.VADProviderContractSuite`. Add `easycat.testing` to the extension surface documented in docs/public-api.md alongside the `easycat.transports` block at :97-110, and pin its exported names in tests/test_public_api.py next to the existing transports guard at :188-208. If the docs will not point at it, delete src/easycat/testing/contracts.py and inline the assertions in tests/contracts/.

**Evidence**

- `src/easycat/testing/contracts.py:1` — 436 lines shipped inside the wheel, with a docstring written for third parties ("Subclass the suite for your surface, point ``provider_factory`` at a zero-argument callable ... and pytest collects the protocol-semantics tests against your implementation"). Verified.
- `docs/extending/stt.md:90` — "The in-tree behavioral contract lives in [`tests/contracts/test_stt_provider_contracts.py`] ... mirror its cases" — a git-checkout path, not the installable suite. Same pattern verified at tts.md:83, vad.md:96, transport.md:117.
- `docs/extending/README.md:52` — "For behavior, mirror the offline protocol contract tests under `tests/contracts/`"; the Commands block at :82 likewise says `uv run pytest tests/contracts`. A pip-installed user has no `tests/` directory. Verified.
- `tests/contracts/README.md:15` — The only prose in the repo explaining the kit exists is a test README: "each per-surface contract file here subclasses the corresponding `easycat.testing` suite". Verified `grep -rn 'easycat.testing' docs/ README.md examples/ src/easycat/cli/scaffold/` exits 1 — zero hits.
- `tests/test_public_api.py:188` — The public-surface guard pins `easycat.transports` (and its `__all__` at :208) but nothing pins `easycat.testing`; docs/public-api.md:97-110 documents only `easycat.transports` as the out-of-tree extension surface. Verified.

*Verified:* Grep verified: `easycat.testing` appears only in src/easycat/testing/*, six files under tests/, tests/contracts/README.md, and plan/ documents — nowhere in docs/, README.md, examples/, or the scaffold templates. docs/public-api.md and tests/test_public_api.py confirmed to cover `easycat.transports` only. Severity confirmed at medium: no runtime harm on its own, but it is the direct cause of the docs on-ramp defect in the isinstance finding, and it is the cheapest fix of the set. Added the tests/test_public_api.py evidence with a verified line number, which the original finding asserted without one.

#### 41. The provider registry hard-requires a per-provider API-key env var, locking out the local/self-hosted providers the README advertises

`MEDIUM` · `provider-abstraction` · `effort: medium`

**Problem.** `ProviderCatalog` models every provider as a hosted API authenticated by exactly one env var. `register()` rejects an empty `env_var`, `create_provider()` rejects an empty `api_key`, and `parse_string()` raises `EASYCAT_E203` before the provider is constructed. There is no way to register a provider that needs no credential (on-prem Whisper, a local Piper/Kokoro TTS, a shared gateway) or one authenticating by mTLS, a service account, or a URL.

**Impact.** The README's "Local/open-source speech pipeline" section is reachable only by instance injection, forfeiting everything the registry provides: `stt="local-whisper/base"` shortcuts, `easycat doctor` env checks, `easycat init` extras, `easycat plan`/`/plan` visibility, and bundle URL redaction for the provider's own host. A self-hosted user must set a dummy env var to a dummy value to participate at all — and self-hosting is among the most likely reasons someone writes an out-of-tree provider in the first place.

**Fix.** Make `env_var` optional on `ProviderSpec` (src/easycat/_provider_catalog.py:34-42) and on `register_stt_provider` / `register_tts_provider` (`env_var: str | None = None`). When None: skip the `register()` guard at :93, skip the api_key check at :173, and skip the `EASYCAT_E203` raise at :214 (resolving the config with no key). Have `_catalog_selection` leave `required_env=None` (src/easycat/planning/provider_plan.py:192) so `/health/ready` does not block on a credential that does not exist. Document the credential-free path in docs/extending/stt.md alongside the hosted one, replacing the unconditional claim at :119.

**Evidence**

- `src/easycat/_provider_catalog.py:93` — `if not env_var: raise ValueError(f"{self.kind} provider {normalized!r} requires an env_var naming its API key.")` in `register()`. Verified.
- `src/easycat/_provider_catalog.py:173` — `if not getattr(config, "api_key", None): raise ValueError(f"API key is required for {self.kind} provider '{provider}'")` in `create_provider`. Verified — the finding cited :172, the raise is at :173.
- `src/easycat/_provider_catalog.py:214` — `if not api_key: raise EASYCAT_E203(var=env_var)` in `parse_string` — the `"provider/model"` shortcut is unusable without a populated credential env var. Verified; finding cited :213.
- `README.md:567` — "### Local/open-source speech pipeline ... To run fully local speech, plug in your own STT/TTS implementations and use the same `EasyConfig` surface", followed by an example that injects a provider as an instance — the registry path is silently unavailable for exactly this use case. Verified; finding cited :566.
- `docs/extending/stt.md:119` — "`YourSTTConfig` also needs an `api_key` field." — stated unconditionally, with no credential-free alternative documented.

*Verified:* All three raise sites read and confirmed; line numbers corrected (173 and 214, not 172 and 213; README 567, not 566). Searched for an escape hatch and found none — no code path in _provider_catalog.py accepts an empty env_var or api_key, and `register_stt_provider`/`register_tts_provider` forward straight to `ProviderCatalog.register`. Severity confirmed at medium: instance injection is a real, documented workaround (README.md:567-575 and docs/extending/README.md's "baseline: plain instance injection"), so nothing is impossible — the cost is losing every registry-derived surface.

#### 42. VAD, noise reduction, and echo cancellation have no registry at all — and the only provider scaffold EasyCat generates targets VAD

`MEDIUM` · `provider-abstraction` · `effort: medium`

**Problem.** STT and TTS get `register_*_provider` plus an entry-point group; VAD, noise reduction, and echo cancellation get a closed `Literal` and a hardcoded backend dict. A third-party VAD can only be injected as an instance, so it cannot be selected by name from a manifest (`VoiceProfile.vad` is a string), cannot participate in the `auto` fallback chain, and is reported to the planner as backend `"auto"`. The `easycat init --template provider` scaffold — the advertised on-ramp for external provider packages — demonstrates exactly this non-extensible stage.

**Impact.** The asymmetry is invisible until someone hits it: a user reads docs/extending/README.md, runs the advertised scaffold, builds a VAD, then finds there is no `register_vad_provider` to make it selectable the way the STT/TTS pages describe. Manifest-driven deployments can never name a third-party VAD, and `/plan` misreports the VAD backend as `"auto"` for any injected instance.

**Fix.** Pick one and make it explicit. Either (a) generalize `ProviderCatalog` to VAD/noise-reduction/echo-cancellation — it handles a credential-free case once `env_var` is optional — and add an `easycat.vad_providers` entry-point group; or (b) state plainly in docs/extending/vad.md that VAD is instance-injection-only and never name-registrable, and note that `/plan` will report it as `"auto"`. Either way, change `easycat init --template provider` (src/easycat/cli/scaffold/templates/provider/) to scaffold an STT or TTS provider with a `register_stt_provider` call and a `[project.entry-points."easycat.stt_providers"]` block, so the on-ramp demonstrates the extensible stage.

**Evidence**

- `src/easycat/vad/_base.py:12` — `VADBackend: TypeAlias = Literal["auto", "silero", "funasr", "ten", "krisp"]` with `_VALID_VAD_BACKENDS` at :13 and `_validate_vad_backend` raising at :18-22 — a closed set with no registration hook. Verified; the finding cited :16.
- `src/easycat/vad/factory.py:101` — `_AUTO_BACKENDS` (:101-106) and `_BACKEND_BY_NAME` (:107-109) are module-level literals. Verified `grep -rn register_vad_provider src/` returns nothing, in contrast to the `register_stt_provider` / `register_tts_provider` entry points and the `easycat.stt_providers` / `easycat.tts_providers` entry-point groups.
- `src/easycat/cli/scaffold/templates/provider/custom_vad.py:1` — The `easycat init --template provider` skeleton generates an `EnergyVAD` — the one pluggable stage with no registry, no entry-point group, and no shortcut-string path. Verified (the template directory contains custom_vad.py, test_custom_vad.py, agent.py, pyproject.toml).
- `src/easycat/planning/provider_plan.py:252` — `backend_name = DEFAULT_VAD` unless `vad` is a str or has a `.backend` attribute (:252-257), so an injected third-party VAD instance is reported to the planner and to `/plan` as backend `"auto"`. Verified.
- `docs/extending/README.md:60` — "## Scaffolding an external provider package — `easycat init` ships a `provider` template that generates a standalone package skeleton", pitched as the route to an external provider package, but able to demonstrate only the non-registrable stage. Verified; the finding cited :67.

*Verified:* Registry asymmetry, closed Literal, scaffold target, and the planner's `"auto"` fallback all verified; line numbers corrected (vad/_base.py:12, docs/extending/README.md:60). MAJOR CORRECTION, and I removed the finding's second half: the claim that `VADProvider.configure`'s three keywords are "mandatory, not advisory" because "the factory calls them unconditionally" is FALSE for third-party providers. `src/easycat/config/_factory.py:182-185` shows `_create_vad` returns an injected instance untouched and never calls `configure()` on it; the unconditional call at src/easycat/vad/factory.py:140-144 is inside `_create_backend`, reached only for the four BUILT-IN backends created through `create_vad`. A third-party VAD instance is never forced through the Silero-shaped signature by the runtime (only `easycat.testing.VADProviderContractSuite.test_configure_accepts_threshold_keywords` at contracts.py:287 exercises it). I dropped the `configure`-relaxation recommendation accordingly. Severity confirmed at medium.

#### 43. EventBus delivery to injected provider instances depends on undocumented private attribute names, and the documented rule is stale

`MEDIUM` · `api-ergonomics` · `effort: small`

**Problem.** Two independent injection paths exist. The factory path (`create_*_provider_from_config`) uses `inject_event_bus`, reading the config dataclass's declared `event_bus` field — clean and documented. The instance path drops the bus in `_create_stt`/`_create_tts` and is rescued later by `Session._maybe_attach_event_bus`, which reaches into `provider._config.event_bus` or `provider._event_bus` by name. A third-party provider storing its config as `self.config` or `self._settings` silently gets no bus. Neither private name appears in any extending doc, and the maintainer-facing summary of the rule in CLAUDE.md and docs/architecture.md no longer matches any provider in the tree.

**Impact.** A third-party provider passed as an instance — the case docs/extending/README.md's baseline tells everyone to start with — loses all provider-scoped observability with no warning: no reconnect events, no journal-visible provider `Error` records, and the failure is a silent absence rather than an exception. Separately, anyone reasoning from CLAUDE.md:112 or docs/architecture.md:199-201 draws wrong conclusions about which configs need the field, since three more configs now declare it and none requires it.

**Fix.** Give `_maybe_attach_event_bus` (src/easycat/session/_session.py:1458) a public escape hatch: check for a `set_event_bus(bus)` method first, keep the `_config`/`_event_bus` probes as legacy fallbacks, and document `set_event_bus` as THE instance-path contract in docs/extending/stt.md (near :117), tts.md, and transport.md. Rewrite CLAUDE.md:112 and docs/architecture.md:199-201 to the accurate rule: a provider config that declares an optional `event_bus` field gets the session bus injected; none require it. Note in the docs that vad/noise_reducer/echo_canceller are not covered by `_maybe_attach_event_bus` at all (_session.py:187-189).

**Evidence**

- `src/easycat/config/_factory.py:159` — `def _create_stt(config, event_bus): if _is_stt_provider_instance(config): return config` (:159-162) — a provider instance is returned untouched and the `event_bus` argument is dropped. Same for TTS at :171-174. Verified; the finding cited :159 and :171.
- `src/easycat/session/_session.py:1458` — `_maybe_attach_event_bus` (:1458-1481) probes `getattr(provider, "_config", None)` then falls back to `provider._event_bus` at :1472. Both are private names a third party must guess. Called only for stt, tts, and transport (:187-189) — never for vad, noise_reducer, or echo_canceller. Verified.
- `docs/extending/stt.md:117` — "To receive the session `EventBus`, declare `event_bus: EventBus | None = None` on `YourSTTConfig`; the factory injects the bus into that optional config field" — true only on the registered-config path; nothing states that the instance path requires storing the config on `self._config` or exposing `self._event_bus`. Verified.
- `CLAUDE.md:112` — "Deepgram and ElevenLabs providers require an `EventBus` injected at construction (they emit provider-scoped events). OpenAI providers do not." Verified stale: `OpenAIRealtimeSTTConfig.event_bus` (stt/openai_realtime_provider.py:89), `OpenAITTSConfig.event_bus` (tts/openai_tts.py:79), and `CartesiaSTTConfig.event_bus` (stt/cartesia_provider.py:59) all declare the field, and none REQUIRES it — `inject_event_bus` (_provider_catalog.py:29-31) only fills it when unset.
- `docs/architecture.md:199` — The same stale claim duplicated near-verbatim at :199-201. Verified.

*Verified:* Every claim verified against source, including the three configs that falsify the CLAUDE.md/architecture.md rule. Found one detail the original finding missed and added it: `_maybe_attach_event_bus` is invoked only for `self.stt`, `self.tts`, and `self.transport` (src/easycat/session/_session.py:187-189), so injected VAD, noise-reducer, and echo-canceller instances get no rescue path at all. Severity confirmed at medium: the observability loss is real and silent, but it affects only instance-injected third-party providers that emit provider-scoped events, and the config-path (registered provider) route works and is documented.

---

### Audio pipeline correctness and latency

*10 findings — 1 high · 7 medium · 2 low*

**Assessment.** The pipeline's *structure* is genuinely well thought through — AEC really does run before NR, frame-boundary buffering is stateful and correct in the AEC/RNNoise/VAD accumulators, the local transport pushes its AEC reference at real playback time with silence keep-alive, and the code is unusually well commented about why each choice was made. The DSP *substrate* underneath that structure is where it falls apart: `_audio_utils.resample` is the single chokepoint for every rate conversion in the framework, and because neither `soxr` nor `scipy` is declared in any extra or the lock file, every install runs the pure-Python `_resample_linear`, which for integer down-ratios degenerates to raw sample-dropping with zero anti-alias filtering — I measured a 10 kHz tone folding to 6 kHz at 0 dB on the documented browser 48k→16k path. Second-worst is that the AEC far-end reference is mislabelled or rate-mismatched on every non-local transport, so on WebRTC AEC silently receives a 1.5x-fast reference and on WebSocket/WebTransport (where AEC is on by default) it provably self-disables on the first TTS frame. The VAD's speech/silence gates are keyed to `time.monotonic()` rather than audio time, so a buffered delivery or a post-CPU-spike backlog drain produces zero VAD events. Finally, none of this is measured: 13 perf benchmarks exist and not one touches the audio path, the two committed `perf/*.json` baselines are byte-identical stale journal results, the planned `perf-ws3-regression` CI gate does not exist, and the only latency budgets (8 s p95 total, 2.5 s LLM TTFT) are too loose to catch anything.

**Done well here:**

- Frame-boundary handling is correct and deliberately stateful everywhere it matters: LiveKitAEC (echo_cancellation.py:102-103, 145-149), RNNoiseReducer (noise_reduction.py:109, 165-180), SileroVAD (silero.py:216-223), TenVAD and FunASR all accumulate a sub-frame remainder across calls instead of zero-padding mid-stream, and each carries a comment explaining why zero-padding would corrupt the recurrent/adaptive filter state. This is the class of bug most realtime audio code gets wrong.
- AEC ordering matches the documented contract: AudioStage.execute (stages/audio.py:82-87) runs echo cancellation on the raw mic signal before the noise reducer, exactly as CLAUDE.md specifies, with the AEC3-convergence rationale inline.
- The local-transport AEC reference path is genuinely correct: the sounddevice output callback pushes one reference frame per callback at actual playback time, including silence during pre-roll and underrun, to keep far/near 1:1 (transports/local.py:260-300), and clear_audio deliberately retains already-played references so residual echo during barge-in is still cancellable (local.py:436-451).
- Odd-trailing-byte and dtype hygiene is consistently handled: resample drops a split 16-bit sample (_audio_utils.py:80-83), TTSBase carries a `_sample_carry` remainder so nothing is lost (tts/base.py:99-106), the local input callback clips before the int16 cast because numpy wraps rather than saturates (transports/local.py:167-170), and `_chunk_has_speech_energy` widens to int32 before abs so abs(-32768) does not overflow (session/text.py:271-273).
- The artifact store is properly bounded and refuses rather than evicts still-referenced blobs (runtime/artifacts.py:80-125), and put_artifact_async correctly distinguishes blocking filesystem stores (offloaded to a thread) from in-memory stores (inline) so the audio loop never eats an fsync (stages/base.py:187-232).
- SmartTurnONNX's concurrency handling is careful and correct: a semaphore bounds to one in-flight inference, and the semaphore is released from the executor future's done-callback rather than the coroutine on timeout/cancel, with the retrieved exception clearing asyncio's never-retrieved warning (smart_turn.py:561-605). The trailing detector window is bounded so detection latency stays constant regardless of turn length (turn_manager.py:499-519).

#### 44. resample() always falls back to unfiltered linear interpolation because neither soxr nor scipy is declared anywhere; integer down-ratios degenerate to raw sample dropping (measured 0 dB in-band aliasing)

`HIGH` · `correctness` · `effort: medium`

**Problem.** `resample()` advertises high-quality soxr/scipy backends, but neither package is declared in any optional extra, in `all`, in the dev group, or in `uv.lock`. `_resolve_resample_backend()` therefore returns "linear" in every shipped install and `_resample_linear` is the only code path that ever executes. Linear interpolation is a weak low-pass for fractional ratios and *no* filter at all for integer down-ratios: when `from_rate/to_rate` is an exact integer, `frac` is always 0.0 and the function reduces to picking every Nth sample.

**Impact.** I ran the repo's own `resample()` and measured the alias with a pure-Python DFT: a 10 kHz tone at 48 kHz resampled to 16 kHz produces a 6 kHz component at 0.00 dB relative to the input (full amplitude); 6 kHz at 24 kHz produces 2 kHz at 0.00 dB after conversion to 8 kHz; the non-integer 24k->16k case folds 10 kHz to 6 kHz at -4.02 dB. Two user-visible consequences: (1) on WebRTC and browser-WebSocket transports the 48 kHz mic signal has everything from 8-24 kHz mirrored into the 0-8 kHz speech band before it reaches Silero and the STT provider; (2) on Twilio, the bot's own 24 kHz TTS has its 4-12 kHz sibilant energy folded into the caller's 0-4 kHz band as roughness on every call. No test asserts spectral content, so the whole class of defect is invisible to CI.

**Fix.** Note that EasyCat's core dependency list (pyproject.toml:33-40) is httpx/rich/sentencesplit/typing-extensions/typer/websockets — it has no numpy, so `soxr` cannot simply go in core without pulling numpy into every install. Instead: add `soxr>=0.5` to the extras that actually carry an audio path (`local`, `webrtc`, `webtransport`, `telephony`, `quickstart`, and therefore `all`), keeping `_resample_linear` as the genuinely-dependency-free fallback; and give `_resample_linear` a cheap fixed-tap FIR low-pass before any down-ratio so the no-extras path is merely low-quality rather than aliased. Add a test in tests/audio/test_audio_utils.py that resamples a 10 kHz tone 48k->16k and asserts the 6 kHz bin sits at least 40 dB below the input — that single test catches this and would have caught it originally.

**Evidence**

- `src/easycat/_audio_utils.py:129` — _resolve_resample_backend() (def at :106) returns "linear" when either soxr or scipy is missing; the result is cached in the module-global _resolved_backend
- `src/easycat/_audio_utils.py:103` — return _resample_linear(...) — the only reachable path in a stock install
- `src/easycat/_audio_utils.py:226` — value = samples[idx] * (1 - frac) + samples[idx + 1] * frac — for an integer down-ratio (48k->16k, 24k->8k) src_pos = i*3.0 is exact, frac is always 0.0, so the function returns every Nth sample with no anti-alias filter
- `pyproject.toml:116` — VERIFIED BY GREP: the strings 'soxr' and 'scipy' appear nowhere in pyproject.toml or uv.lock. The `all` extra (lines 116-139) and `quickstart`/`local`/`silero-vad`/`smart-turn` declare numpy+onnxruntime only. `uv sync --group dev` produces a venv with neither.
- `src/easycat/transports/webrtc.py:709` — raw = resample(raw, frame_rate, target_rate) — aiortc decodes Opus at 48 kHz, WebRTCTransportConfig.audio_format defaults to PCM16_MONO_16K (_webrtc_config.py:83), so every browser mic frame is decimated 3:1 unfiltered
- `src/easycat/tts/base.py:131` — data = resample(data, source_format.sample_rate, self._output_format.sample_rate) — OpenAI's speech endpoint always returns 24 kHz PCM (openai_tts.py:86), and EasyConfig aligns the Twilio output format to PCM16_MONO_8K (twilio_media.py:41 + config/_tts_alignment.py:80-87), so this line performs the 24k->8k 3:1 decimation on every phone call
- `tests/audio/test_audio_utils.py:45` — the resampler tests assert output length, DC preservation, odd-byte tolerance and backend-failure fallback only — no spectral, THD, or alias assertion anywhere in tests/audio/

*Verified:* Confirmed by execution, not just reading: `_resolve_resample_backend()` returns "linear" in this repo's .venv, and I measured the alias amplitudes above by calling the repo's `resample()` directly. Two corrections to the original finding. (a) The Twilio citation (twilio_media.py:941, `pcm16_to_mulaw`) is wrong for the default path: EasyConfig's `align_tts_config_to_transport` re-targets OpenAI TTS to PCM16_MONO_8K for Twilio, so `pcm16_to_mulaw` sees `source_rate == 8000` and skips its resample entirely; the 24k->8k decimation actually happens one layer earlier at tts/base.py:131. Same defect, different file. (b) 'Every sample-rate conversion is unfiltered decimation' is overstated — upsampling (8k->16k) and non-integer ratios (24k->16k) do get partial interpolation filtering; only exact integer down-ratios are raw sample dropping. Downgraded critical -> high: this is silent audio-quality degradation on every call, but it is not data loss, a security hole, or an outage.

#### 45. WebRTC AEC far-end reference is re-stamped with the microphone's format, discarding the true reference format the transport already recorded and defeating LiveKitAEC's rate-mismatch guard

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `OutboundAudioSource` queues AEC reference bytes at the original TTS chunk's rate and records that rate in `_ref_format`, but `drain_aec_reference_frames()` returns bare `bytes`. `AudioRouter._feed_transport_aec_reference` then fabricates a format from the *mic* chunk. When the TTS output rate and the transport/mic rate differ, `LiveKitAEC.feed_reference` receives PCM at one rate labelled as another: `_frame_samples_for_rate` slices it into wrong-length frames and `process_reverse_stream` advances the render stream at the wrong speed relative to capture, so AEC3's delay estimate never converges and its residual suppressor gates on a reference uncorrelated with the real echo. Critically, the fabricated label also makes `_check_stream_rate` agree, so the one guard designed to catch exactly this condition never fires.

**Impact.** Bounded but silent. The default `EasyConfig` path is safe (see verification note), so this bites configurations that bypass TTS alignment: `SessionConfig` (the advanced API, which never calls `align_tts_config_to_transport`), `EasyConfig(auto_align_tts_output_to_transport=False)`, or any TTS config whose `output_format` is not the 24 kHz default that alignment keys on. In all of those, WebRTC AEC (on by default, webrtc.py:128) runs with a time-scaled reference and produces no exception, no log line, and no journal record — whereas the same mismatch on the WebSocket path is caught and reported with an actionable warning. No test asserts the reference format; tests/transports/test_webrtc_outbound_audio.py only checks byte identity with same-rate fixtures.

**Fix.** Change `OutboundAudioSource.drain_aec_reference_frames()` (transports/_webrtc_audio.py:318) to also expose the stored `_ref_format` — either return `(bytes, AudioFormat)` pairs or add an `aec_reference_format` property — and use that format at session/_audio_router.py:723 instead of `mic_chunk.format`. The mismatch then reaches `_check_stream_rate`, which produces the existing actionable warning instead of silent corruption. Add a router test with a 24 kHz reference and a 16 kHz mic asserting either the correct format reaches `feed_reference` or `_aec_reference_failed` latches.

**Evidence**

- `src/easycat/session/_audio_router.py:723` — AudioChunk(data=ref_data, format=mic_chunk.format) — the drained reference bytes are stamped with the near-end mic format, whatever rate they were actually produced at
- `src/easycat/transports/_webrtc_audio.py:250` — self._aec_ref_queue.append(delivered_data) where delivered_data is sliced from queued.original_chunk.data — the pre-48k-resample TTS chunk (webrtc.py:369-371 passes original_chunk=chunk, the chunk handed to send_audio)
- `src/easycat/transports/_webrtc_audio.py:251` — self._ref_format = queued.original_chunk.format — the transport stores the true reference format but `drain_aec_reference_frames()` (:318) returns only list[bytes], so the router can never see it
- `src/easycat/echo_cancellation.py:105` — _check_stream_rate() latches the first rate seen on either stream and raises ValueError on a later mismatch — the guard that exists precisely to catch this, and that the fabricated format renders unreachable
- `src/easycat/session/_audio_router.py:681` — _feed_reference_or_disable catches that ValueError, logs an actionable one-time warning and latches _aec_reference_failed — the graceful-degradation path that a correct format would reach

*Verified:* Code citations all verified at the stated lines (_audio_router.py:723, _webrtc_audio.py:250-251). The original finding's severity and framing were wrong and I corrected them: it claimed AEC3 is fed a 1.5x-fast reference 'on the browser-demo path' by default, but `EasyConfig.__post_init__` calls `align_tts_config_to_transport` (config/easy.py:595-598, on by default via `auto_align_tts_output_to_transport: bool = True` at easy.py:521), which rewrites `OpenAITTSConfig.output_format` from PCM16_MONO_24K to the transport's `audio_format` — PCM16_MONO_16K for WebRTC (_webrtc_config.py:83). In the default path reference and mic rates therefore match and there is no bug. Downgraded high -> medium and rewritten around the defect that is actually real: fabricating the format silently defeats the existing `_check_stream_rate` safety net for every non-aligned configuration.

#### 46. WebSocket and WebTransport default AEC on, but feed the far-end reference at socket-write time with no silence keep-alive — the 1:1 render/capture invariant the local and WebRTC transports go out of their way to maintain

`MEDIUM` · `correctness` · `effort: medium`

**Problem.** Two transports that enable echo cancellation by default cannot supply the reference stream AEC3 needs. The reference is fed only when a chunk is written to the socket, so (a) the render stream stalls completely between TTS chunks and during all silence while the capture stream keeps advancing, and (b) the reference timestamp is socket-write time, not playback time — the browser's playback buffer offsets it by an unknown, drifting amount. The two transports that own their playout clock (local, via the sounddevice callback; WebRTC, via RTP pacing) both go out of their way to push silence and padding to keep the two streams 1:1, and both carry comments explaining why it is necessary.

**Impact.** Every browser/WebSocket deployment that installs `easycat[aec]` or `easycat[all]` gets AEC enabled automatically and pays the full per-frame WebRTC APM cost on the near-end path (`LiveKitAEC.process` runs on every mic frame regardless of reference state) for a canceller whose adaptive filter is fed a gapped, misaligned render stream. There is no journal record and no metric indicating the reference is degraded, so an operator debugging echo on the browser path has nothing to look at.

**Fix.** Either set `default_echo_cancellation_enabled = False` on `WebSocketTransport`/`WebTransportTransport` (websocket.py:58/114/298, webtransport.py:237/1115/1475) and document that these transports should rely on the browser's own `echoCancellation: true` getUserMedia constraint, or give them a playout-timed reference: track outbound bytes against a wall-clock playout estimate and push silence frames into the reference at the pipeline frame rate while nothing is being sent, mirroring `LocalTransport._output_callback`. Journal an `aec_disabled`/`aec_reference_degraded` record so a bundle reader can see which case applies.

**Evidence**

- `src/easycat/transports/websocket.py:114` — WebSocketTransport.default_echo_cancellation_enabled = True (also websocket.py:58 on the config, :298 on the client transport)
- `src/easycat/transports/webtransport.py:1115` — WebTransport transport also defaults AEC on (also :237, :1475)
- `src/easycat/session/_audio_router.py:926` — skip_feed = self._aec_reference_failed or self._transport_has_aec_drain — websocket/webtransport lack a drain hook, so they take the send-time feed path in _handle_audio_delivery and get a reference frame only when a chunk is actually written to the socket
- `src/easycat/transports/local.py:262` — LocalTransport pushes a full frame of silence into the reference on every underrun and pre-roll callback, with the comment 'silence keeps far/near 1:1'; local.py:249-253 states the invariant explicitly: 'Every return path pushes exactly one full-frame far-end reference ... so the AEC reference stream stays 1:1 with the mic stream and AEC3 keeps converging'
- `src/easycat/transports/_webrtc_audio.py:263` — _record_silence_reference mirrors playout padding into the reference queue because otherwise 'the reference stream permanently lags real playout' — WebRTC pays the same cost that websocket/webtransport skip
- `src/easycat/transports/websocket.py:158` — send_audio writes the chunk to the socket and returns; the reference is fed at this moment, but the browser's Web Audio buffer plays it an unknown amount of time later. clear_audio (websocket.py:167) is also a no-op, so nothing re-syncs the reference after a barge-in.

*Verified:* I rejected this finding's headline claim and kept the part that survives. The claim that AEC 'provably self-disables on the first TTS frame' because 24 kHz TTS hits a 16 kHz-latched near-end stream is FALSE in the default path: EasyConfig calls `align_tts_config_to_transport` (config/easy.py:595-598, default-on at easy.py:521), which re-targets OpenAI TTS to the transport's PCM16_MONO_16K (websocket.py:62), so the rates match and `_check_stream_rate` never raises. And when a mismatch IS constructed (SessionConfig, or alignment disabled), it is handled: _audio_router.py:679-690 catches it, latches `_aec_reference_failed`, and logs a warning naming the exact cause and fix — so the 'completely silent' framing does not hold either. What I verified as real is the secondary claim: websocket/webtransport implement no `drain_aec_reference_frames` hook (grep confirms only local.py:405 and webrtc.py:390 do) and push no silence, while local.py:249-253 and _webrtc_audio.py:258-262 both document the 1:1 invariant as a hard requirement. Downgraded high -> medium and retitled.

#### 47. VAD speech/silence gates measure wall-clock time instead of audio position, so a burst delivery of speech frames emits zero VAD events

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `min_speech_duration_ms` and `min_silence_duration_ms` gate on elapsed *processing* time rather than elapsed *audio*. In steady-state realtime the two coincide, so the bug is invisible; they diverge whenever frames arrive faster than realtime.

**Impact.** I reproduced this against the repo's real `_VADBase`: feeding 31 consecutive frames at probability 0.9 (0.99 s of continuous speech) in one tight loop emits zero events. The failure mode is inverted from what you want — when the event loop falls behind (GC pause, CPU spike, transport reconnect flushing a jitter buffer, or a client that batches audio into >250 ms messages) and the 200-chunk inbound queue drains as a burst, start-of-speech stops firing exactly when the system is stressed, so turns do not start and barge-in does not trigger. If the burst contains a full utterance bracketed by silence, the trailing sub-threshold frames reset `_speech_start_time` and the utterance is dropped outright. It also makes VAD decisions non-deterministic under faster-than-realtime replay, which undercuts the journal-replay story. Nothing in tests/vad exercises a burst delivery.

**Fix.** Carry audio position instead of wall clock. Each backend already knows its frame duration (512/16000 s for Silero, hop_size/16000 for TEN, chunk_size_ms for FunASR), so accumulate `self._audio_time_s += frame_duration` and pass that as `now` to `_evaluate_speech` — roughly five lines per backend in silero.py:233, ten.py:89, krisp.py:58, plus dropping the `time.monotonic()` calls. Add a test that feeds 1 s of supra-threshold frames in a single tight loop and asserts `VADStartSpeaking` fires.

**Evidence**

- `src/easycat/vad/_base.py:100` — (now - self._speech_start_time) * 1000 >= self._min_speech_duration_ms — `now` is a wall-clock stamp supplied by the caller, not an audio position
- `src/easycat/vad/silero.py:233` — now = time.monotonic() sampled per frame inside the drain loop, so every frame in a buffered chunk gets essentially the same timestamp
- `src/easycat/vad/ten.py:89` — same wall-clock stamp in TenVAD's frame loop
- `src/easycat/vad/krisp.py:58` — same in KrispVAD
- `src/easycat/vad/_base.py:107` — self._speech_start_time = None on any sub-threshold frame, so a burst containing speech bracketed by silence discards the accumulated start entirely
- `src/easycat/transports/_base.py:126` — _init_audio_queue builds an asyncio.Queue with maxsize=max_pending_chunks (200, ~4 s at 20 ms frames) which the pipeline task drains as fast as it can once it catches up

*Verified:* Reproduced directly: `_VADBase.configure(min_speech_duration_ms=250)` then 31 back-to-back `_evaluate_speech(0.9, time.monotonic())` calls returns an empty event list. All five code citations verified at the stated lines (transports/_base.py cited as :128, actually :125-129). Downgraded high -> medium: the shipped browser client posts 128-sample (~2.7 ms) blocks from its AudioWorklet (examples/ws_browser_client.html:119-135) and the local mic delivers one 20 ms frame per callback, so the common paths stay in lockstep with the wall clock; this needs a backlog or a batching client to bite. Real design defect with a clean repro and a cheap fix, but it will not hit most users in steady state.

#### 48. Default debug="light" journal holds only ~30-50 seconds of a call because audio stages emit ~200-300 records/second, and the single eviction marker is itself evicted

`MEDIUM` · `observability` · `effort: medium`

**Problem.** With the default `debug="light"`, every inbound audio frame writes two journal records in each of AudioStage, VADStage and STTStage. At the 20 ms frame cadence of LocalTransport that is ~200 records/second idle and ~300/second during an active turn, against a fixed 10 000-record ring with no configuration path. The buffer therefore retains roughly 30-50 seconds of a call, of which ~99% is per-frame audio bookkeeping rather than turn/agent/TTS records.

**Impact.** CLAUDE.md states the journal is the single source of truth for all observability, and docs/latency.md's triage workflow is built on `easycat bundles show ... | jq '.turns'`. After a two-minute call, `session.journal.read()` and `export_debug_bundle()` return only the last ~40 seconds — the opening turns, which are usually the ones you want, are gone. The ring does append a `BufferOverflow` marker on first eviction, but `_overflow_pending` is never reset so only one is ever written, and that single marker scrolls out of the deque after another ~10k records; a long call therefore ends with a truncated journal carrying no evidence of truncation. Grep confirms nothing under src/easycat/cli/, src/easycat/debug/ or src/easycat/validation/ reads `buffer_overflow`, so `easycat bundles show` and the latency percentile rollups operate on a truncated tail without flagging it.

**Fix.** Give per-frame stage records their own retention class: gate the AudioStage/VADStage/STTStage `stage_start`/`stage_complete` pair on `debug == "full"` (or sample them 1-in-N, mirroring `_AEC_REFERENCE_CAPTURE_EVERY_N_FRAMES`) so `light` keeps turn-scoped and control records for the whole call. Independently, either reset `_overflow_pending` periodically in journal_memory.py so eviction stays visible, or track a monotonic `dropped_count` on the ring that `easycat bundles show` prints — a truncated bundle must never look complete.

**Evidence**

- `src/easycat/runtime/journal_factory.py:27` — capacity: int = 10_000 — the in-memory ring used for debug="light"
- `src/easycat/config/_factory.py:409` — _create_debug_resources calls create_journal(...) without `capacity`; EasyConfig exposes no journal_capacity knob (grep confirms no such field in config/easy.py)
- `src/easycat/config/easy.py:471` — debug: Literal["off", "light", "full"] = "light" — the ring buffer is the default journal
- `src/easycat/stages/audio.py:65` — stage_start per inbound frame; stage_complete at :125 — 2 unconditional records per frame from AudioStage alone, plus two artifact puts
- `src/easycat/stages/vad.py:86` — VADStage stage_start (:86) / stage_complete (:133) add 2 more per frame
- `src/easycat/stages/stt.py:62` — STTStage stage_start (:62) / stage_complete (:97) add 2 more per frame while a turn is active
- `src/easycat/runtime/journal_memory.py:212` — if was_full and not self._overflow_pending — _overflow_pending is set at :213 and never reset anywhere in the file, so exactly one BufferOverflow marker is ever appended; after ~10k further records that marker is itself evicted from the deque
- `src/easycat/session/_audio_router.py:71` — _AEC_REFERENCE_CAPTURE_EVERY_N_FRAMES = 50, with a comment justifying the decimation by 'the ~50 writes/sec/session fsync + journal pressure' — the optional track is throttled 50x while the mandatory per-frame path next to it writes 4-6x that rate unthrottled

*Verified:* All citations verified; journal_factory.py capacity is at :27 not :25, and stages/audio.py stage_start is at :65 not :66. Record-rate arithmetic checked against LocalTransportConfig.frame_duration_ms = 20 (local.py:49): 50 frames/s x 6 records = 300/s with a turn active, 200/s idle, so 33-50 s of retention. Partially corrected the original claim that 'nothing indicates eviction occurred': journal_memory.py:212-227 does append a BufferOverflow marker — but I verified `_overflow_pending` has no reset anywhere in the file, so the marker fires once and is subsequently evicted, and no CLI surface consumes it. Downgraded high -> medium: the blast radius is debug tooling only (debug="full" uses SQLite with no ring cap), with no runtime or user-facing effect.

#### 49. Smart-turn burns ~122 ms per endpoint decision in the pure-Python resampler, on the path whose entire purpose is saving the 500 ms silence timer

`MEDIUM` · `performance` · `effort: small`

**Problem.** `_chunks_to_float32_16k` calls `resample()` once per captured chunk instead of once per window, and `resample()` is the pure-Python `_resample_linear` in every shipped install (see the resampler finding). The 8-second window at the local transport's 20 ms cadence is ~400 chunks of 480 samples each, every one of which goes through a Python-level per-output-sample loop.

**Impact.** I measured the exact default workload against the repo's own `resample()`: 400 chunks of 480 samples at 24k->16k takes 122 ms; the 8 kHz telephony variant takes 112 ms. Smart-turn's whole value proposition is skipping the 500 ms `end_of_turn_silence_ms` timer, so ~122 ms of Python interpolation consumes a quarter of the saving on every endpoint decision. On a multi-session server the executor thread is also occupied for 122 ms per endpoint, competing with every other session's ONNX work.

**Fix.** Do the rate conversion in NumPy inside smart_turn.py rather than calling the dependency-free `resample()`. Smart-turn already requires numpy (pyproject.toml:91, `smart-turn = ["numpy>=1.24.0", "onnxruntime>=1.27.0"]`) and already holds it as `self._np`, so `_chunks_to_float32_16k` can convert each chunk to float32 with `np.frombuffer` first and do the rate change with a single vectorized `np.interp` (or `soxr.resample`, if the resampler finding is fixed) — sub-millisecond either way. Do NOT simply concatenate and call `resample()` once: I measured that at 174 ms, i.e. slower than the per-chunk loop.

**Evidence**

- `src/easycat/smart_turn.py:506` — data = resample(data, source_rate, 16000) — called once per captured chunk inside the reversed-chunk loop of _chunks_to_float32_16k
- `src/easycat/smart_turn.py:409` — _MAX_AUDIO_SAMPLES = 8 s at 16 kHz; the whole trailing window (~400 chunks at 20 ms) is re-preprocessed on every decision
- `src/easycat/transports/local.py:48` — LocalTransportConfig.audio_format defaults to PCM16_MONO_24K and nothing in the pipeline resamples mic audio, so turn_audio chunks reach smart-turn at 24 kHz and always need conversion
- `src/easycat/smart_turn.py:590` — _detect_sync (which calls _chunks_to_float32_16k at :554) runs via loop.run_in_executor, so the cost is off the event loop but squarely on the endpoint-decision path
- `src/easycat/turn_manager.py:549` — await detector.detect(self._detector_audio_window()) on the endpoint path; the fallback grace timer is not reduced by however long detect() took
- `docs/latency.md:119` — smart-turn is documented as the default for EasyCat.mic(); the row accounts only for model inference ('classifies in tens of milliseconds on CPU')

*Verified:* Timings measured by running the repo's own `resample()` in this checkout: 122.3 ms for 400x 20 ms@24k->16k, 112.0 ms for the 8k variant — matching the original finding's numbers. Two corrections. (1) The recommendation to 'concatenate first and resample once' is wrong: I measured the single concatenated call at 174.6 ms, *slower* than the per-chunk loop, because `_resample_linear` is O(output samples) in a Python loop and batching only adds struct.pack/list overhead. The fix has to be vectorization, not batching. (2) The claim that 'the documented latency figure is wrong' overreaches — docs/latency.md:119 says the *model* classifies in tens of ms, which is true, and the 2.0 s `timeout_s` ceiling still comfortably covers 122 ms + inference, so nothing in that row is falsified. Severity medium confirmed.

#### 50. pre_roll_ms (300) is not coupled to VADConfig.min_speech_duration_ms (250); raising the VAD gate as the docs advise silently truncates the start of every utterance

`MEDIUM` · `correctness` · `effort: small`

**Problem.** The pre-roll buffer is the only thing preventing the start of an utterance from being lost — STT receives no audio at all until `_begin_turn` flushes it. It is a fixed 300 ms window trimmed by duration, while `VADStartSpeaking` fires 250 ms plus one 32 ms Silero frame after the first frame whose probability crosses threshold (and Silero's probability typically crosses one to two frames after true acoustic onset). At defaults the buffer holds ~282-350 ms of already-elapsed speech against a 300 ms capacity, so the true pre-onset margin ranges from roughly +18 ms to slightly negative. Nothing validates, derives, or documents the relationship.

**Impact.** An operator following docs/latency.md's own tuning advice and raising `min_speech_duration_ms` to 400 ms to stop echo and coughs from triggering barge-in silently loses ~150 ms off the front of every utterance handed to STT — dropped first words and mis-transcribed openings, with no error, warning, or journal signal. Even at defaults the margin is thin enough that a slow-attack VAD or a 40 ms transport frame can clip the leading consonant, which is precisely the failure pre-roll exists to prevent.

**Fix.** Validate the coupling where both configs are visible — the EasyConfig wiring that builds `TurnManagerConfig` and `VADConfig` — and log a warning when `pre_roll_ms < min_speech_duration_ms + 150`. Better, derive it: when the caller leaves `pre_roll_ms` at its default, set it to `min_speech_duration_ms + 200`. Add a `TurnManagerConfig.pre_roll_ms` row to the docs/latency.md defaults table (which the guard test in tests/observability/test_docs.py will then pin to the code) and cross-reference it from the `min_speech_duration_ms` row's tuning guidance.

**Evidence**

- `src/easycat/turn_manager.py:93` — pre_roll_ms: int = 300
- `src/easycat/vad/factory.py:50` — min_speech_duration_ms: int = 250 — VADStartSpeaking is emitted 250 ms plus one frame after the first supra-threshold frame
- `src/easycat/turn_manager.py:358` — _trim_pre_roll_buffer pops until _pre_roll_duration_ms <= pre_roll_ms, so at most 300 ms of audio is ever retained
- `src/easycat/turn_manager.py:471` — _begin_turn calls _flush_pre_roll_into_turn_audio (:445), and _turn_runner.py:270-272 replays exactly that buffer into STT as the entire pre-speech context — no other audio reaches STT before turn start
- `src/easycat/turn_manager.py:135` — __post_init__ validates only pre_roll_ms >= 0; grep confirms no code anywhere relates pre_roll_ms to min_speech_duration_ms
- `docs/latency.md:118` — the min_speech_duration_ms row tells operators to raise it to reject 'echo, coughs, and background noise' with no mention of pre-roll; pre_roll_ms appears nowhere in docs/latency.md

*Verified:* All citations verified (__post_init__ pre_roll check is at :135-136, not :125; _trim_pre_roll_buffer at :358). Confirmed by grep that `pre_roll` appears in only turn_manager.py and two docs/using-easycat examples — there is no validation, no derivation, and no mention in docs/latency.md. Confirmed via _turn_runner.py:270-272 that the pre-roll buffer really is the sole pre-turn audio source for STT, so the truncation is genuine and not backstopped elsewhere. Severity medium confirmed: real and silent, but it degrades the first word rather than breaking the turn.

#### 51. Barge-in playback cutoff is the last step in cancel_turn, behind an unbounded application event handler and a TTS provider socket teardown

`MEDIUM` · `performance` · `effort: small`

**Problem.** `cancel_turn(barge_in=True)` performs, in order: cancel token, emit `Interruption` to every application subscriber, propagate a control signal through the stages with a journal write each, cancel preemptive generation, cancel the STT committer, cancel the TTS scheduler (awaiting provider cancel plus the cancelled task's `aclose`, potentially a network round trip), flush the outbound queue — and only then clears the transport's playback buffer. The one operation that actually stops the user hearing the bot sits behind two unbounded awaits.

**Impact.** On top of the unavoidable ~282 ms of VAD confirmation, a slow `Interruption` handler or a TTS provider whose WebSocket close blocks adds directly to how long the caller keeps hearing the bot talk over them — the most conversationally damaging latency in a voice agent. The cutoff is instrumented as `easycat.interruption.cutoff_latency` but has no budget in DEFAULT_BUDGETS, no benchmark in perf/, and tests/session/test_interruption_cutoff_latency.py asserts only that the histogram is recorded, never that the value is bounded.

**Fix.** Hoist the cutoff, but suppress playback first — the codebase's own `cancel_tts_playback` (_session.py:1329-1332) shows the required order: `set_playback_suppressed(True)`, then `_outbound_queue.flush_for_new_turn()`, then `await clear_audio_if_supported(self.transport)`. Do those three immediately after the cancel token at _session.py:1282, then run the existing emit/propagate/cancel sequence, and move the histogram record to right after the clear. Add an `interruption_cutoff_ms` entry to DEFAULT_BUDGETS (validation/_latency_budgets.py:44, e.g. p95 <= 400 ms) so a regression is caught by the latency lane.

**Evidence**

- `src/easycat/session/_session.py:1291` — await self._emit(Interruption()) — EventBus.emit (events.py:758-763) awaits every matching handler serially with no timeout, so one slow application subscriber blocks everything after it
- `src/easycat/session/_session.py:1292` — await self._cancel.propagate_signal(...) walks the stages, each awaiting its handler and writing a ControlSignalRecord
- `src/easycat/session/_tts_scheduler.py:311` — TTSScheduler.cancel awaits self._synth.cancel() (which awaits the provider's cancel, _tts_synthesizer.py:233-238) and then awaits the cancelled synthesis task, unwinding contextlib.aclosing on the provider stream (_tts_synthesizer.py:187) — a WebSocket close for ElevenLabs/Cartesia/Deepgram
- `src/easycat/session/_session.py:1302` — await clear_audio_if_supported(self.transport) — the cheapest and most latency-critical action, executed last
- `src/easycat/session/_session.py:1307` — the cutoff latency IS measured into easycat.interruption.cutoff_latency, but nothing gates on the value
- `src/easycat/validation/_latency_budgets.py:44` — DEFAULT_BUDGETS covers only total_ms, tts_ttfb_ms and llm_ttft_ms; there is no interruption-cutoff budget

*Verified:* All six citations verified at the stated lines, including that EventBus.emit awaits handlers serially with no timeout (events.py:758-763) and that TTSSynthesizer.cancel awaits the provider (_tts_synthesizer.py:233-238) inside an aclosing scope (:187). I corrected the recommendation, which as written was unsafe: simply moving `clear_audio_if_supported` and `flush_for_new_turn` to the top would let the still-running synthesis task push fresh chunks into the transport after the flush, so playback suppression has to precede it — exactly what `cancel_tts_playback` already does. Severity medium confirmed (not raised): the latency is real and unbounded in principle, but it only materializes with a slow subscriber or a blocking provider close.

#### 52. LocalTransport's output jitter pre-roll is a hardcoded module constant, re-armed after every barge-in and absent from the documented latency table

`LOW` · `api-ergonomics` · `effort: small`

**Problem.** The local-microphone transport (the `EasyConfig.mic()` default) delays the first sample of every utterance until three frames have queued, and re-arms the delay on every `clear_audio()` so the post-barge-in reply pays it again. The depth is a module constant with no config field, and it does not appear in the docs/latency.md table that otherwise claims to enumerate the latency-adding defaults.

**Impact.** Bounded and usually small: `send_audio` splits each chunk into frame-sized slices (local.py:367-369), so a typical TTS chunk of 100+ ms fills the 3-frame threshold in a single call and playback starts on the next callback (~20 ms). The full 60 ms is only paid when audio dribbles in below 60 ms at a time. The concrete cost is documentation and tunability: a developer profiling `vad_endpoint_to_tts_first_byte_ms` against the documented table has no row to attribute the delay to, and cannot lower it without editing the module.

**Fix.** Promote the depth to `LocalTransportConfig.output_preroll_frames` (default 3) at transports/local.py:49 and read it at :261, and add a corresponding row to the docs/latency.md defaults table so the tests/observability/test_docs.py guard pins it to the code.

**Evidence**

- `src/easycat/transports/local.py:31` — _OUTPUT_PREROLL_FRAMES = 3 — a module-level constant, not a LocalTransportConfig field
- `src/easycat/transports/local.py:261` — if self._out_queue.qsize() < _OUTPUT_PREROLL_FRAMES: the callback emits silence and returns, so playback waits for 3 queued frames (60 ms at the default 20 ms frame_duration_ms)
- `src/easycat/transports/local.py:451` — clear_audio sets self._primed = False, so the pre-roll is re-armed after every barge-in
- `src/easycat/transports/local.py:49` — LocalTransportConfig exposes frame_duration_ms and both queue caps but not the pre-roll depth
- `docs/latency.md:106` — the 'Latency-adding defaults' section promises 'the defaults that *add waiting time* on the response path' and is guard-tested by tests/observability/test_docs.py — but that guard only checks that listed rows match the code, so an omitted default is invisible to it

*Verified:* All line numbers verified and corrected (callback check is at :261, not :260). I corrected the impact, which was materially overstated: the original claimed a flat 60 ms on every utterance and framed it as '12% of the 500 ms end-of-turn timer', but I verified at local.py:367-369 that outbound chunks are pre-split into 20 ms frames, so a normal TTS chunk (OpenAI streams tens of KB, i.e. 100+ ms) satisfies the 3-frame threshold immediately and the realistic cost is one callback period. Kept at low: the surviving substance is an undocumented, non-configurable constant in a page that promises completeness.

#### 53. Dead _split_frames helper implements the exact zero-padding the AEC class documents as harmful, and the module docstring states the opposite pipeline order from the code

`LOW` · `over-engineering` · `effort: small`

**Problem.** `_split_frames` is production-dead: `process` and `feed_reference` both use the stateful per-direction buffers, and the only importers are three tests that exist solely to exercise the dead function. Retaining it is worse than neutral — it is a ready-made helper implementing precisely the zero-padding behaviour the class documents as breaking AEC convergence. Independently, the module docstring says AEC sits *between* noise reduction and VAD, the opposite of what `AudioStage` does and of what CLAUDE.md documents.

**Impact.** Small but real maintenance cost in the file that governs the pipeline's trickiest ordering constraint. The next person to touch AEC reads a docstring telling them noise reduction runs first, and finds a helper suggesting zero-padding partial frames is sanctioned — both pointing back at the convergence bug the current code was written to avoid.

**Fix.** Delete `_split_frames` (echo_cancellation.py:67-77) and the three tests that cover it (tests/audio/test_echo_cancellation.py:21, :30, :42, plus the import at :14). Rewrite the module docstring at echo_cancellation.py:3-5 to say AEC runs on the raw microphone signal *before* noise reduction, mirroring the rationale already written at stages/audio.py:79-85.

**Evidence**

- `src/easycat/echo_cancellation.py:67` — _split_frames zero-pads the last frame; grep across src/ and tests/ shows the only importers are tests/audio/test_echo_cancellation.py:14 and its three tests at :21, :30, :42 — no production call site
- `src/easycat/echo_cancellation.py:95` — LiveKitAEC.__init__'s own comment: chunks that are not exact frame multiples 'must not be zero-padded mid-stream (that injects silence into the filter and desyncs near-end/far-end alignment)' — process and feed_reference use the stateful _near_buffer/_far_buffer accumulators instead
- `src/easycat/echo_cancellation.py:3` — module docstring: 'Provides an optional AEC pipeline stage that sits between noise reduction and VAD.'
- `src/easycat/stages/audio.py:79` — the implementation runs AEC first, on the raw mic signal, then the noise reducer at :85 — with an inline comment giving the reason, matching CLAUDE.md's architecture section

*Verified:* Both claims verified exactly as stated. Grep confirms `_split_frames` has zero production references; the LiveKitAEC comment at :92-100 does say zero-padding mid-stream desyncs alignment; and stages/audio.py:79-85 runs the echo canceller before the noise reducer, contradicting the docstring. Severity low confirmed — documentation and dead-code hygiene, no runtime effect.

---

### Errors, journal, and observability

*11 findings — 3 high · 4 medium · 4 low*

**Assessment.** The observability subsystem is unusually well-*designed* and unusually poorly *bounded*. Logging hygiene, the OpenTelemetry facade with its enforced low-cardinality attribute allowlist, the context-pack projection with its post-write verification, and the crash-sweep classification logic are all better than what most pre-1.0 libraries ship. But the journal — declared "the single source of truth, complete where logs are lossy" — does not hold that guarantee on either backend: the default in-memory backend wraps after ~33 seconds and emits at most one overflow marker that is itself evicted, and the persistent backend pays a synchronous SQLite COMMIT (measured 497 µs) plus 18 regex substitutions (measured 140 µs) per record, 4–6 times per 20 ms audio frame, directly on the asyncio event loop, projecting to ~19.6 GB/hour of WAL. The single biggest problem is that the write path was optimized for a durability contract nobody asked for (per-frame crash durability) at the cost of the realtime budget the whole library exists to protect; batching commits to turn boundaries would fix both the latency and the write amplification with no meaningful loss of debuggability. Secondarily, three surfaces promise more than they deliver: `errors.py` is a documentation registry with no programmatic axis and five codes that nothing raises; `easycat replay` walks records and prints a count while shipping a fidelity enum, a tool policy, and a timing mode that the CLI path ignores; and `docs/reference/events.md` claims to list every public event while omitting 23 of 45, guarded by a test that structurally cannot notice. What is genuinely excellent: the correlation-context logging, the artifact/journal separation, the degraded-mode design where a journal failure never propagates into the pipeline, and the post-finalize SAVEPOINT trick that keeps a crash-after-finalize database looking cleanly closed.

**Done well here:**

- Logging is textbook-correct for a library: `logging.getLogger("easycat").addHandler(logging.NullHandler())` at __init__.py:33, zero calls to `basicConfig`, zero touches of the root logger, exactly one tagged handler installed by `enable_console_logging` (_logging.py:45-56), `propagate=False` to avoid double-logging, and an automatic JSON formatter under `EASYCAT_ENV=prod`. I grepped for every anti-pattern and found none in the framework code.
- Session/turn correlation via `contextvars` + a handler-level `CorrelationFilter` (_log_context.py:67-73) that never drops a record, with `reset_session`/`reset_turn` explicitly hardened against the cross-task `ContextVar.reset` ValueError that bites almost everyone who tries this.
- The OpenTelemetry facade is real, not aspirational: 21 metrics and 9 spans are actually emitted from the pipeline, it no-ops cleanly without the SDK, and `_validate_attribute_key` (_observability.py:234) enforces both an explicit forbidden set (session_id, turn_id, transcript, prompt, api_key, …) and a substring heuristic rejecting anything containing transcript/prompt/content/text/body/secret/token. This is a stronger cardinality and PII guard than most instrumented libraries have.
- `cli/debug/_context_projection.py` is a proper allowlist sharing boundary — it enumerates the 40 data keys permitted to cross, counts (rather than silently drops) omissions, and `_assert_context_pack_redacted` (export.py:188) re-scans the written files for sensitive patterns before the command succeeds. Defense in depth that is actually implemented, not just documented.
- The crash-sweep classification (`crash_sweep.py:79-163`) is careful engineering: read-only-first so a cleanly-closed journal is never opened for writing (which would rewrite its WAL sidecar), WAL checkpoint folded in before the byte copy, sidecar copy as a fallback when the checkpoint is incomplete, and best-effort throughout so it can never raise into journal startup.
- Journal degraded mode is well thought out: `append()` catches everything and returns -1 rather than propagating into the audio pipeline (journal_sql.py:112-114), and the marker is persisted both as a `session_state` key and a `sequence=-1` row so a bundle loaded fresh from disk can tell that records were dropped — with an explicit comment about why the -1 marker is deliberately excluded from the normal `read()`/`follow()` stream.
- The post-`finalize()` SAVEPOINT machinery (journal_sql.py:544-613, 638-651) so that a crash after a clean finalize leaves the durable database looking cleanly closed — including the deliberate decision *not* to persist a degraded marker in that window — is a subtle invariant that most implementations would get wrong, and it is correctly reasoned about in the comments.
- PEP 678 exception notes are threaded consistently: `annotate_stage_exception` (stages/base.py:249) attaches stage, provider, elapsed_ms, sequence and a `record_key` checkpoint id, so a raw traceback in a user's logs points directly at the journal record that captured the failing input. That is a genuinely good debugging affordance.
- `put_artifact_async` (stages/base.py:187-233) correctly distinguishes blocking from non-blocking artifact stores and offloads only the former to a thread, with an explicit `writes_block` escape hatch for out-of-tree stores — the reasoning in the docstring about why a thread hop is pure overhead for in-memory stores at 50 fps is exactly right.

#### 54. SqliteJournal COMMITs on every append from the asyncio audio loop, ~4-6× per 20 ms frame, with ~17× WAL write amplification and no mid-session checkpoint

`HIGH` · `performance` · `effort: medium`

**Problem.** At `debug="full"` each pipeline stage appends two journal rows per audio frame, and `SqliteJournal._do_append` COMMITs each row individually on the event loop thread. I reproduced this against the real class (SqliteJournal, 5000 appends, `JournalRecordKind.EVENT`, a realistic stage-metadata payload) on this machine: **597 µs per append** and **17.6 KB of WAL per record**. A controlled A/B on an equivalent schema with identical PRAGMAs isolates the cause: COMMIT-per-row costs 352 µs/insert and 4.4 KB WAL/record, versus 8.2 µs/insert and 0.26 KB WAL/record when the same inserts are committed in batches of 100 — a 43× CPU and 17× WAL difference. Combined with `wal_autocheckpoint=0` and no mid-session checkpoint, the WAL never shrinks.

**Impact.** Bounded to `debug="full"` (the `EasyConfig` default is `"light"`, in-memory), but `docs/deployment/production-servers.md:190-205` recommends exactly that setting for production and warns only about volume persistence, never about cost. With AEC+VAD+STT active that is 4-6 commits per 20 ms frame: on this box 2.4-3.6 ms of blocking work inside a 20 ms realtime budget before any DSP runs, and the cost is attributed to `easycat.stage.latency`, not to journaling, so it is invisible in the metrics a user would check. Disk is the machine-independent half: 17.6 KB/record × ~250 rec/s is ~4.4 MB/s, ~16 GB per hour of call, on a WAL that is never truncated until close.

**Fix.** In `src/easycat/runtime/journal_sql.py`, replace the unconditional per-append COMMIT at :631 with a bounded batch commit — hold the open transaction and COMMIT on elapsed time (50-200 ms) or every N records, plus an unconditional COMMIT at turn boundaries and on the existing `flush()`/`finalize()`/`close()` paths. That keeps the DURABILITY.md loss window at one turn instead of one frame, which is what debugging needs. Set `PRAGMA wal_autocheckpoint` at :309 to a bounded page count (or call `wal_checkpoint(PASSIVE)` from the same batch timer) so the WAL stops growing monotonically. Then move the append off the loop: give `journal_append_event` (stages/base.py:295) the same treatment `put_artifact_async` (stages/base.py:187) already has for blocking stores — push onto an `asyncio.Queue` drained by a writer task.

**Evidence**

- `src/easycat/runtime/journal_sql.py:631` — `self._conn.execute("COMMIT")` then `BEGIN` at :632, inside `_do_append`, on the calling thread. The block comment at :615-629 shows this is deliberate (DURABILITY.md contract), so any fix must preserve bounded durability rather than just remove the COMMIT.
- `src/easycat/runtime/journal_sql.py:309` — `conn.execute("PRAGMA wal_autocheckpoint=0")`. Verified the only `wal_checkpoint(TRUNCATE)` call sites are close() :464, finalize() :516, and the retention/crash-sweep paths — none run during a live session, and `flush()` (:477) has exactly one caller (the Litestream wrapper at :790). The -wal file therefore grows monotonically for the whole call.
- `src/easycat/stages/audio.py:65` — `journal_append_event(... name="stage_start")` per mic frame; `stage_complete` at :121. Verified `journal_append_event` (stages/base.py:295) calls `ctx.journal.append(...)` synchronously with no offload — I grepped runtime/ and session/_journal_sink.py for `to_thread`/`asyncio.Queue`/`run_in_executor` and found none.
- `src/easycat/stages/vad.py:86` — stage_start per frame; stage_complete at :133. STTStage does the same at stt.py:62 / :97 while listening.
- `src/easycat/session/_audio_router.py:770` — `chunk = await self._audio_stage.execute(...)`, VAD at :774, STT at :803 — all awaited inline in the single per-chunk coroutine at 50 fps.
- `src/easycat/stages/base.py:187` — `put_artifact_async` already offloads blocking filesystem artifact writes via `asyncio.to_thread`, with a docstring explaining that inline fsync-class I/O "would stall the asyncio event loop". The journal write next to it does exactly that and is not offloaded — the inconsistency is the clearest argument for the fix.

*Verified:* Citations all check out (line numbers exact). I re-ran the measurement myself rather than trusting the reported one: I got 597 µs/append and 17.6 KB WAL/record against the real SqliteJournal, versus the finding's claimed 1078 µs and 18.2 KB — same order, so the claim stands, but I substituted my own numbers and labelled the CPU figure as machine-specific (this is an rk3588 ARM board; an x86 server will be faster). My first benchmark attempt silently produced 1.8 µs/append because passing `kind` as a plain string puts the journal into degraded mode — worth knowing if anyone re-measures. I downgraded critical → high: the default `debug="light"` path is entirely unaffected (in-memory ring buffer), so this requires an explicit opt-in, and the per-append COMMIT is a documented deliberate durability tradeoff rather than an oversight. I also verified there is no hidden batching or thread offload anywhere in runtime/.

#### 55. The default journal (debug="light") silently discards the session after ~40 seconds with no marker, counter, or flag

`HIGH` · `correctness` · `effort: small`

**Problem.** `debug="light"` is the default and gives a 10,000-record ring buffer. The stages journal at the same rate regardless of debug level (verified: `journal_append_event` only checks `ctx.journal is None`), so at 4-6 rows per 20 ms frame the buffer wraps in roughly 40 seconds. I reproduced the loss: 3,000 appends into `InMemoryRingBuffer(capacity=1000)` returns exactly 1,000 records (sequences 2002-3001), **zero** `BufferOverflow` markers, and `degraded == False`. Nothing in the returned data says anything was dropped except the fact that the first sequence is not 1 — which no caller checks.

**Impact.** After any call longer than about a minute, `session.journal.read()` and `export_debug_bundle()` return a truncated tail that is indistinguishable from a complete recording. A user debugging "why did the bot hang up at 0:30" on a five-minute call gets a journal that starts at 4:20. This happens on the default configuration, and docs/observability.md asserts the opposite guarantee in two places.

**Fix.** In `src/easycat/runtime/journal_memory.py`: drop the `_overflow_pending` latch (:51, :212-213) and replace it with a monotonic `dropped_records` counter incremented on every eviction, exposed as a property and stamped into the bundle manifest by `debug/export.py::_capture_session_bundle`. Thread a `journal_capacity` field from `EasyConfig`/`create_text_session` (config/easy.py:472) through `_create_debug_resources` (config/_factory.py:405-418) into the existing `create_journal(capacity=)` parameter. Fix docs/observability.md:20 and :72 to state the light-backend bound instead of claiming completeness.

**Evidence**

- `src/easycat/runtime/journal_memory.py:46` — `collections.deque(maxlen=capacity)` with `capacity: int = 10_000` at :41 — oldest records are evicted, not archived.
- `src/easycat/runtime/journal_memory.py:212` — `if was_full and not self._overflow_pending:` / `self._overflow_pending = True` at :213. Grepped the whole repo: `_overflow_pending` appears at exactly three lines (:51 init, :212, :213) and is never reset, so at most one `BufferOverflow` marker is ever produced — and that marker is itself a ring-buffer record, so it is evicted 10,000 appends later.
- `src/easycat/config/_factory.py:409` — `create_journal(session_id, debug=..., backend=..., artifact_store=..., retention_mode=...)` — no `capacity=`, so `journal_factory.create_journal`'s 10_000 default (journal_factory.py:26) applies to every EasyConfig session. Same at :946 for `create_text_session`.
- `src/easycat/config/easy.py:472` — EasyConfig exposes `journal_backend` and `journal_retention` (:472-473) but no capacity field. `SessionConfig.journal` (session/_types.py:140) does accept an injected `ExecutionJournal`, so an advanced user can pass `InMemoryRingBuffer(capacity=N)` — the knob exists one rung down, but not on the documented path.
- `docs/observability.md:20` — Layer C guarantee column reads "Complete single source of truth." — no mention of a bound; :72 repeats "single source of truth ... complete where logs are lossy."

*Verified:* Reproduced end-to-end; every cited line is accurate (capacity default is at :41 not :42, otherwise exact). Two mitigations I looked for and did not find: no `dropped` counter anywhere on the class, and `degraded` stays False. One partial mitigation the finding missed and I have added: `SessionConfig.journal` (session/_types.py:140) accepts an injected journal, so the capacity is reachable from the advanced rung — that narrows the "no knob" claim to the EasyConfig path but does not touch the silent-loss claim, which is the severe half. Kept at high: silent truncation of the self-declared single source of truth, on the default config.

#### 56. Journal write-filter silently mangles ordinary transcripts — dates, order numbers, and URLs become [REDACTED_*] at write time with no opt-out

`HIGH` · `correctness` · `effort: medium`

**Problem.** I ran `redact_text` on realistic utterances against the shipped code: `"my account number is 8 6 7 5 3 0 9"` → `"my account number is [REDACTED_PHONE]"`; `"my order number is 1234567890"` → `"[REDACTED_PHONE]"`; **`"the date is 2024-01-15"` → `"the date is [REDACTED_PHONE]"`**; `"go to https://acme.example.com/orders"` → `"go to [REDACTED_URL]"`; `"the file is at /home/alice/report.pdf"` → `"the file is at ~/report.pdf"`. `redact_value({"text": ...})` shows the same result through the real key path. Because the filter runs at write time in `_journal_record_for_append`, the original text is never stored, so this is unrecoverable, and there is no way to turn it off.

**Impact.** A voice bot whose job is order numbers, dates, account references, or reading back URLs writes a journal in which the interesting part of every transcript is a placeholder. "The bot misheard the account number" cannot be diagnosed from the source of truth, and neither can any date-bearing utterance — an ISO date is nine digits with separators and matches the phone heuristic. Both the module docstring and the observability doc promise transcripts are preserved.

**Fix.** Split secret scrubbing from PII heuristics in `src/easycat/validation/redaction.py`. Keep `_HEADER_SECRET_RE`, `_BEARER_RE`, `_KEY_VALUE_SECRET_RE`, `_JWT_RE`, `_SECRET_RE` always on. Move `_PHONE_RE` (:93), `_URL_RE` (:67) and `_HOME_PATH_RE` (:94) behind a policy argument, and add a `journal_redaction: Literal["secrets", "pii"] = "secrets"` field on `EasyConfig` (config/easy.py:472) threaded through `_create_debug_resources` to `apply_write_filter`. At minimum, anchor `_PHONE_RE` so it does not fire on space- or hyphen-separated digit runs that lack a phone-number shape. Fix the safe_defaults.py:5 docstring and docs/observability.md:75 either way — both currently document behaviour that does not exist.

**Evidence**

- `src/easycat/runtime/_journal_codec.py:382` — `return apply_write_filter(JournalRecord(...))` inside `_journal_record_for_append`, which is the single record-construction path for BOTH the SQLite and in-memory backends. Filtering happens before the record is stored, so the original is never persisted anywhere.
- `src/easycat/runtime/safe_defaults.py:349` — `apply_write_filter` calls `redact_value(record.data)` unconditionally. Grepped the repo: the only non-test references are this definition and the single call site above — there is no config flag, env var, or constructor argument that disables it.
- `src/easycat/validation/redaction.py:93` — `_PHONE_RE = r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)"` — matches any run of 9+ digits including digits separated by spaces, dots, or hyphens. Applied to every string value via `_TEXT_REDACTIONS` (:106).
- `src/easycat/runtime/safe_defaults.py:5` — Module docstring: "it deliberately preserves normal transcript, agent-output, and tool-result text so replay remains useful" — contradicted by the measured behaviour below.
- `docs/observability.md:75` — "It is **PII-bearing by design**: it records transcripts, agent output, and tool arguments so a session can be faithfully replayed and debugged." — the same contradiction in the reader-facing doc.
- `src/easycat/session/_journal_sink.py:58` — `_JOURNAL_ATTRS` begins with `"text"`, so STTPartial/STTFinal/AgentFinal transcripts land in `data["text"]` and go through the filter.

*Verified:* The correctness half is confirmed and reproduced verbatim (I added the ISO-date case, which is the most damaging example and was not in the original). The **performance half of this finding is wrong and I removed it**: the finding claims 18 `re.sub` calls and 140 µs per record, but it missed `_TEXT_REDACTION_TRIGGER_RE` at redaction.py:132/139, a cheap superset pre-check that short-circuits `redact_text` for strings with no trigger character. I measured `apply_write_filter` directly: 13 µs/record for pure stage metadata, 44 µs when a `state_before` repr is present, 35 µs for a text record — versus a 98 µs total `InMemoryRingBuffer.append`, so ~45% of the light-backend append cost, not the claimed 100%. That is not enough to justify the per-key policy the original recommendation led with, so I rewrote the recommendation around the correctness fix only. Also corrected: `"transcript"` (not `"text"`) is in `UNSAFE_TEXT_FIELDS`, so a `data["transcript"]` key is replaced wholesale — a separate, intentional behaviour.

#### 57. Five registered error codes have zero raise sites, and runtime timeout errors sit outside the EasyCatError hierarchy

`MEDIUM` · `api-ergonomics` · `effort: small`

**Problem.** `easycat explain` presents 26 codes as the runtime error vocabulary, but five of them (E304, E305, E401, E402, E403) are never produced by any code path — the sites their `cause` text describes raise uncoded `RuntimeError` or emit code-less events. Separately, `EasyCatError` has no subclasses anywhere in the tree and the three runtime timeout exceptions are plain `Exception` subclasses that merely carry a matching `code` string, so a caller cannot write `except EasyCatError` and catch anything the pipeline actually raises.

**Impact.** A user who reads `easycat explain EASYCAT_E305` and writes handling for it is writing dead code; the same for E304, E401, E402, E403. And a caller who follows the natural inference from a package-level `EasyCatError` export (`easycat/__init__.py:59`) and writes `except EasyCatError` around a session gets a handler that never fires, because STT/agent/TTS timeouts and every vendor exception fall outside it.

**Fix.** Two independent, cheap changes in `src/easycat/errors.py` and `src/easycat/timeouts.py`. (1) Either raise the five codes at the sites that document them — E304 from `_health_check.py:158`, E305 from `reconnecting_ws.py:422` (which also has the `attempts` value E305's template wants), E401/E402 from `debug/export.py` and `debug/_bundle_loader.py`, E403 from wherever divergence detection lands — or delete those five `register(...)` blocks and the corresponding names from `tests/cli/test_errors.py:131`. (2) Make `STTTimeoutError`/`AgentTimeoutError`/`TTSTimeoutError` (timeouts.py:42/57/71) subclass `EasyCatError`, passing their existing `code` through the constructor, so the documented base class catches the errors the runtime actually raises.

**Evidence**

- `src/easycat/errors.py:402` — EASYCAT_E304 registered. Grepping `EASYCAT_E304|E305|E401|E402|E403` across all of `src/` outside errors.py returns **nothing** — zero raise sites for all five (E305:421, E401:443, E402:458, E403:475). `easycat explain EASYCAT_E305` therefore documents an error no user can ever encounter.
- `src/easycat/errors.py:3` — Module docstring: "Every code is both a runtime factory (``EASYCAT_E101(target=...)`` produces a tagged :class:`EasyCatError`) and a documentation entry" — the five above are documentation entries only.
- `src/easycat/_health_check.py:158` — The exact site E304 documents ("provider became unreachable mid-call") emits `Error(exception=RuntimeError(f"Health check failed for {self._provider_name}: {reason}"), ...)` — a bare RuntimeError, no code.
- `src/easycat/reconnecting_ws.py:422` — The exact site E305 documents ("reconnect exhausted") emits `ReconnectFailure(provider=..., error=error)` — no code, and no attempt count despite E305's headline template taking `{attempts}`.
- `src/easycat/timeouts.py:42` — `class STTTimeoutError(Exception)` with `code = "EASYCAT_E301"` at :49; same shape for `AgentTimeoutError` (:57) and `TTSTimeoutError` (:71). None inherit `EasyCatError`, so `except EasyCatError` catches no runtime timeout. Grepping `(EasyCatError)` across src/ returns zero subclasses.
- `tests/cli/test_errors.py:131` — `test_runtime_and_bundle_ranges_are_registered` asserts all eight E3xx/E4xx codes are in `REGISTRY` — a registration check that passes regardless of whether anything raises them, so it locks the five dead entries in place.

*Verified:* Every factual claim verified: zero raise sites for the five codes (grep confirmed), zero `EasyCatError` subclasses, timeout classes derive from `Exception`, `httpx.HTTPStatusError` propagates raw from `tts/openai_tts.py:167` after `_emit_provider_error`, and `_add_exception_notes` stashes `http_status` as a PEP 678 note string (`_provider_helpers.py:74`). I downgraded high → medium and rewrote the framing: the original's headline recommendation — add `retryable`/`category`/`http_status` to `EasyCatError` and wrap every vendor exception at the provider boundary — is a speculative redesign, not a verified defect, and the finding presents no evidence any user hit it. What is verifiable and actionable is the dead-code registry entries and the broken `except EasyCatError` contract, so I narrowed the finding and the recommendation to those. Line numbers corrected: the test is at :131, not :137.

#### 58. `easycat replay` walks records without re-executing anything: `--timing wall` is inert, `Stage.replay` has no shipped caller, and EASYCAT_E403 is unreachable

`MEDIUM` · `over-engineering` · `effort: large`

**Problem.** `easycat replay` iterates journal records, attaches artifact blobs, applies a field mask, and prints a frame count. `ReplaySpec` carries `fidelity`, `timing`, `tool_policy`, `force` and `stage_filter`, but on the CLI path `fidelity` only decides whether a version mismatch raises or logs a warning, `timing` only toggles timestamp masking, and `tool_policy` only decides whether encountering a tool record raises `ReplaySideEffectBlocked`. The per-stage `Stage.replay()` implementations — eight of them, plus `ReplayCassette` and `replay_decision` helpers — exist solely for tests.

**Impact.** The user-facing vocabulary (ARTIFACT/SIMULATED/LIVE fidelity, DENY/STUB/ALLOW tool policy, fast/wall timing, committable checkpoints, provider version pinning) promises re-execution that never happens. Concretely: `--timing wall` is a flag whose help text describes a behaviour it does not have; `--tool-policy allow` is documented as "explicitly side-effecting" when it cannot produce a side effect; and `easycat explain EASYCAT_E403` documents a divergence error that is structurally unreachable. The maintenance cost is eight `replay()` implementations with no production caller.

**Fix.** Pick one and close the gap. (a) Wire the walker to the stages: in `src/easycat/cli/debug/replay.py`, after `bundle.replay(spec)`, instantiate each stage in `spec.stage_filter`, call the existing `Stage.replay(spec, cassette)`, diff against the recorded `output_ref`, and raise `EASYCAT_E403` on mismatch. Or (b) make the surface honest: delete `ReplaySpec.timing` and the `--timing` option (cli/debug/replay.py:121), delete `ReplayFidelity.SIMULATED`, delete the EASYCAT_E403 registration (errors.py:475), drop `Stage.replay` from the protocol (stages/base.py:126) and its eight implementations, and reword docs/using-easycat/07-observability/README.md:128 so `--tool-policy allow` is not described as side-effecting.

**Evidence**

- `src/easycat/runtime/replay.py:443` — ReplayRunner docstring: "The runner does **not** instantiate stages or call their ``execute()`` methods." `run()` at :462 walks records and builds ReplayFrame objects. This is honest documentation — the gap is between it and the CLI's vocabulary.
- `src/easycat/runtime/replay.py:473` — `mask_fields = REPLAY_IGNORE_FIELDS if self._spec.timing == "fast" else frozenset()` — the only consumer of `timing`. Grepping `sleep` in replay.py returns nothing, so `--timing wall` paces nothing; its sole effect is to *disable* timestamp masking.
- `src/easycat/cli/debug/replay.py:121` — `--timing` help text is "Timing mode: fast or wall." with no indication that `wall` neither paces nor replays.
- `src/easycat/stages/base.py:126` — `Stage.replay(self, spec, cassette)` protocol method, implemented by all eight stages (audio.py:190, vad.py:153, stt.py:114, tts.py:237, agent.py:478, turn.py:121, transport.py:149). Grepping for callers: only `tests/runtime/test_replay.py`, `tests/stages/test_stages.py` and `tests/debug/test_replay_and_bundle.py` — no shipped entry point calls any of them. The CLI (cli/debug/replay.py:188) and debugger (debugger/_sources.py:229) both call `bundle.replay(spec)`, which is the record walker.
- `src/easycat/errors.py:475` — EASYCAT_E403 "Replay diverged from recorded bundle" — unreachable, because no shipped path re-runs a stage and compares its output against `output_ref`.
- `docs/using-easycat/07-observability/README.md:128` — "Treat `allow` as explicitly side-effecting." — with `ToolReplayPolicy.ALLOW` the runner only appends the descriptor to a list and logs a warning (replay.py:625-630); nothing executes, so nothing can have a side effect.

*Verified:* All six citations verified; ReplayRunner's docstring is at :443 not :444, otherwise exact. I confirmed by grep that no shipped module calls `Stage.replay` — only three test files do. I downgraded high → medium: this is an API-honesty and dead-weight problem, not a correctness or data-loss one, and the reader-facing teaching doc (docs/using-easycat/07-observability/README.md:113-121) does describe the command accurately as "Replay the captured record stream" and reports frame counts, which undercuts the original's "single largest credibility risk" framing. What survives sharply is the three concrete artifacts: an inert flag with misleading help, an unreachable error code, and a protocol method with no production caller. I added the `--tool-policy allow` documentation contradiction, which the original missed and which is the most misleading of the set.

#### 59. A crashed journal whose PID has been reused is permanently classified "live", and the docstring documents a backstop the code short-circuits

`MEDIUM` · `correctness` · `effort: small`

**Problem.** Both liveness checks consult `live_pid` first and return early if the PID names any running process. I reproduced it: created a `SqliteJournal`, appended a record, flushed, rewrote `live_pid` to `1`, and abandoned the handle without `close()`. `is_journal_live()` returned True, `sweep_crashed_journals()` promoted 0 files, `crash-dumps/` was never created, and the file stayed in `journals/` — permanently, since nothing ever re-evaluates it. The documented write-lock backstop only runs when the marker is absent, which is the case that does not need it.

**Impact.** The failure direction is conservative — a live journal is never deleted — so this is not corruption or data loss. The harm is that a crashed session's journal is stuck forever: `easycat bundles list` reports it as live, it is never promoted to `crash-dumps/`, and retention refuses to archive or prune it, so a shared `journals/` directory on a long-lived host or a restarting container grows without bound with files the tooling will not touch. That defeats the crash-recovery story DURABILITY.md sells as the reason to choose the SQLite backend. PID reuse is realistic in containers restarted against a persistent `EASYCAT_DATA_DIR` and on long-lived hosts after PID wraparound.

**Fix.** In `src/easycat/runtime/crash_sweep.py`, invert the order in both `_read_only_state` (:137) and `is_journal_live` (:203) so the `BEGIN IMMEDIATE` probe runs first and `live_pid` is consulted only when the write lock is free — that implements the two-signal logic the docstrings at :96-101 and :178-183 already describe. Alternatively store a PID identity rather than a bare PID at journal_sql.py:279 (`f"{os.getpid()}:{start_time}"`, reading `/proc/<pid>/stat` field 22 on Linux) and compare both in `_pid_alive`. Add a regression test that sets `live_pid` to a live-but-unrelated PID and asserts the journal is promoted to `crash-dumps/`.

**Evidence**

- `src/easycat/runtime/crash_sweep.py:137` — `if _has_live_pid(conn): return "skip"` inside `_read_only_state`, which `_crashed_state` (:104) consults first — so the BEGIN IMMEDIATE probe at :117 is never reached when the marker names any running process.
- `src/easycat/runtime/crash_sweep.py:203` — `if _has_live_pid(conn): return True  # Marker names a running process -> live.` in `is_journal_live`, before the probe at :218-224. Same short-circuit.
- `src/easycat/runtime/crash_sweep.py:100` — Docstring claims "2. A ``BEGIN IMMEDIATE`` write-lock probe on a would-be crash, as a backstop for an actively-writing session whose ``live_pid`` marker might be stale (PID reuse)" — repeated at :181-183. The probe is structurally unreachable in exactly the stale-PID case it names.
- `src/easycat/runtime/journal_sql.py:279` — `INSERT OR REPLACE INTO session_state ... ('live_pid', ?)` stores a bare `str(os.getpid())` with no boot id, process start time, or container identity, so a recycled PID is indistinguishable from the original owner.
- `src/easycat/runtime/journal_retention.py:126` — `return is_journal_live(db_path)` — retention inherits the misclassification, so the orphan is never archived or deleted either.

*Verified:* Reproduced exactly as described; all five citations are accurate to the line. I searched for a compensating path — a TTL on the marker, an mtime check, a manual promote command — and found none. I kept the severity at medium but rewrote the impact: the original implied crash-recovery breakage, whereas the failure is fail-safe (preserve, never delete), so the concrete harm is unbounded accumulation of unreachable journal files plus permanent misclassification in `bundles list`, not lost data. I also reordered the recommendation to lead with the probe-first reordering, which is a three-line change, over the PID-identity scheme, which needs a per-platform implementation.

#### 60. `export_debug_bundle` materializes the entire session in RAM before writing, then fails the 500 MB cap it only checks afterwards

`MEDIUM` · `performance` · `effort: medium`

**Problem.** Bundle export is O(entire session) in memory on every axis at once. `_serialize_journal` reads the full journal with no limit and holds every record twice (as dataclass, then as a JSON string). `_collect_artifacts` reads every `.bin` in the session directory into a dict. Then `_ArtifactAccumulator` re-hashes each blob and only at that point discovers the 500 MB cap — so on a long session the export does not degrade gracefully, it spikes memory and then raises `BundleValidationError`. Because artifacts are content-addressed one-file-per-frame in a flat directory, that directory holds hundreds of thousands of ~640-byte files.

**Impact.** `session.export_debug_bundle(...)` — the documented way to capture a bug — is slowest and most likely to fail on exactly the long sessions users most want to export, and it fails after the memory spike rather than before. Operators hit inode pressure and slow `iterdir()` on the artifact directory well before the byte cap trips, and the cap under-counts real disk usage by roughly 6× because it counts payload bytes rather than allocated blocks.

**Fix.** Stream the export in `src/easycat/debug/export.py`: open the `ZipFile` in `_write_bundle_archive` first, then write `journal.ndjson` incrementally by paging `journal.read(start=cursor, limit=N)`, and write each artifact with `zf.open(name, "w")` + `shutil.copyfileobj` instead of building the `_CapturedSessionBundle.artifacts` dict — checking `_ARTIFACT_SIZE_CAP` as bytes are streamed rather than after they are all resident. In `src/easycat/runtime/artifacts.py`, shard `_ref_path` (:299) into `<ref[:2]>/<ref>.bin` subdirectories, and seed `_current_bytes` (:223) by scanning the directory on construction so a restart does not reset the budget.

**Evidence**

- `src/easycat/debug/export.py:124` — `lines = [json.dumps(record_to_dict(record), default=str) for record in journal.read()]` — `read()` with no `limit`, whole journal decoded into JournalRecord objects and re-serialized into a list held entirely in memory, then joined into one bytes object at :125.
- `src/easycat/debug/export.py:143` — `{artifact_file.stem: artifact_file.read_bytes() for artifact_file in artifact_dir.iterdir() if ...}` — every artifact blob on disk read into one dict before anything is written.
- `src/easycat/debug/_bundle_loader.py:59` — `_ArtifactAccumulator.ensure_capacity` raises `BundleValidationError("Total artifact size exceeds 500MB cap")` — but it is only reached from `add()` (:66), which `_capture_session_bundle` (export.py:105-107) calls *after* `_collect_artifacts` has already read everything into memory. `add()` also re-sha256s every blob to validate the ref, a second full pass.
- `src/easycat/runtime/artifacts.py:299` — `return self._dir / f"{ref}.bin"` — one flat directory per session, with AudioStage/VADStage/STTStage each calling `put_artifact_async` per frame (stages/audio.py:60 and :117).
- `src/easycat/runtime/artifacts.py:223` — `self._current_bytes = 0` in `FilesystemArtifactStore.__init__` — the byte budget resets on every construction and never reflects what is already on disk; `max_bytes` (:217) is 512 MB of payload only, and at ~640 B per 20 ms PCM frame that is ~800,000 files, each occupying a full 4 KB ext4 block.

*Verified:* All citations verified (`_ref_path` is :299 not :298, `_current_bytes = 0` is :223 not :217, `max_bytes` default is at :217). I checked the `debug="light"` path and it is bounded on both axes — `InMemoryArtifactStore` caps at 50 MB (artifacts.py:90) and the ring buffer at 10,000 records — so this is a `debug="full"` problem only, which is why medium rather than high. I strengthened the finding with something the original missed: the 500 MB cap in `_ArtifactAccumulator.ensure_capacity` is enforced *after* full materialization, so the outcome on a long session is a hard `BundleValidationError` following the memory spike, not merely a slow export — that makes the case for streaming stronger, not weaker.

#### 61. docs/reference/events.md claims to list "every public EasyCat event type" but omits 23 of 45, including all tool-call, reconnect, and telephony events

`LOW` · `documentation` · `effort: small`

**Problem.** I enumerated the concrete `Event` subclasses in `easycat.events`: 45 total, 21 in `easycat.__all__`, 22 named in the events reference. The other 23 — `ToolCallStarted`/`Delta`/`Result`, `ReconnectAttempt`/`Success`/`Failure`, `TransportDegraded`, `TransportAudioDelivered`, `AgentRequestStarted`, `PlaybackMarkAck`, all four `SessionAction*`, and the ten telephony events (CallInitiated, CallRinging, CallStateChanged, CallScreening, ScreeningResponse, ScreeningTimedOut, DTMFAggregated, VoicemailDetected, IVRAction) — are emitted on the same public `EventBus`, journaled by `SessionJournalSink`, and subscribable via `session.subscribe_event(...)`. The guard test's `not extra` assertion means the catalog cannot be extended to cover them without changing the test.

**Impact.** A user looking for the event surface for tool calls or provider reconnects reads the page that promises "every public EasyCat event type" and finds nothing, and would have to already know to look in the telephony or tools tutorial chapters. Bounded, because most of the missing events do get a passing mention elsewhere (`ToolCallStarted`/`Result` in docs/using-easycat/04-tools-actions/README.md:77, the telephony set in docs/using-easycat/10-telephony/README.md:136-151, `Reconnect*` in docs/teaching/13-swap-providers-and-transports/README.md:273, `TransportDegraded` in docs/extending/transport.md) — but none of those give when-emitted semantics, and none is discoverable from the reference page.

**Fix.** Smallest honest fix: change docs/reference/events.md:3 from "every public EasyCat event type" to "every event type exported from `easycat`", and add a short pointer section listing the bus-emitted-but-unexported classes with links to the pages that cover them. Better fix: add the 23 to `easycat.__all__` (they are public in practice — importable from `easycat.events`, emitted on the session bus, journaled), document them in the catalog, and the existing guard at tests/docs/test_route_contracts.py:103 will then cover them with no test change needed.

**Evidence**

- `docs/reference/events.md:3` — "This page lists every public EasyCat event type with its when-emitted semantics." — the overclaim. Line 6 does qualify it ("compares it against the event classes exported from `easycat`"), so the guard's scope is disclosed even though the headline sentence is not accurate.
- `tests/docs/test_route_contracts.py:103` — `test_events_reference_tracks_public_event_types` builds `exported` from `easycat.__all__` (:108-114) and asserts both `not missing` and `not extra` — the `not extra` assertion means documenting the other 23 would actively fail the test, so the guard does not merely miss them, it forbids them.
- `src/easycat/events.py:217` — `ToolCallStarted` (ToolCallDelta:225, ToolCallResult:232) — subscribed and journaled by `session/_journal_sink.py:122-124`, absent from events.md.
- `src/easycat/session/_journal_sink.py:109` — `_SIMPLE_EVENT_RECORDS` subscribes AgentRequestStarted, ToolCall*, SessionAction*, Reconnect* and Supervisor* on the session bus — all public traffic, none in the events catalog.

*Verified:* I re-ran the enumeration myself: 45 concrete `Event` subclasses, 21 in `easycat.__all__` (the finding said 22), 22 named in events.md, 23 missing — list confirmed. I downgraded medium → low and corrected two framings. First, "the guard structurally cannot catch them ... which is worse than having no guard: it advertises coverage it does not provide" is overstated — events.md:6 states plainly that the guard compares against classes exported from `easycat`, so the scope is disclosed one line below the overclaim. Second, the guard's real obstacle is the `not extra` assertion (which forbids documenting non-exported events), not the `exported` set construction the finding pointed at; I corrected the line number to :103 and the mechanism. I also grepped for coverage elsewhere and found the missing events are mentioned across five other docs pages, which the finding's "finds nothing, concludes the capability does not exist" impact claim did not account for.

#### 62. EventBus's slow-handler warning is off by default and unreachable from EasyConfig, the path the docs point at it from

`LOW` · `api-ergonomics` · `effort: small`

**Problem.** Dispatch is inline and awaited in the emitter's coroutine, and `AudioIn`/`TTSAudio`/`AudioOut` are emitted from the per-frame audio path — this is documented and deliberate. The gap is that the one diagnostic the docs point at is off by default and set only through a constructor argument, while both `EventBus()` constructions on the `EasyConfig`/`create_session` path are bare. A user on the path the README and quickstart teach cannot enable it without dropping to `SessionConfig` and building the bus themselves.

**Impact.** The most likely first mistake a new user makes — subscribing to `STTFinal` or `AudioIn` and doing blocking I/O in the handler — degrades audio and produces no warning at any log level, and the fix requires knowing that `EventBus` has a parameter that `EasyConfig` does not expose. Bounded because the workaround exists (`SessionConfig(event_bus=EventBus(slow_handler_threshold_s=...))`, session/_types.py:136) and because the check itself is already implemented.

**Fix.** Add `slow_handler_threshold_s: float | None = 0.005` and `handler_error_policy: Literal["continue","raise"] = "continue"` to `EasyConfig` (config/easy.py:471, alongside `journal_backend`) and pass them into both `EventBus()` constructions in `config/_factory.py` (:587 and :959). Default the threshold on — the check at events.py:779-787 is a `time.perf_counter()` subtraction that costs nothing when nothing is slow, and it is already written.

**Evidence**

- `src/easycat/events.py:670` — `slow_handler_threshold_s: float | None = None` — defaults to off, so the `logger.warning("Slow handler ...")` at :781-787 never fires unless explicitly configured. It is constructor-only: there is no setter or property, only the read of `self._slow_handler_threshold_s` at :780.
- `src/easycat/config/_factory.py:587` — `event_bus = EventBus()` in `_build_audio_session` — bare construction, no threshold. Same at :959 in the `create_text_session` path. Those are the only two `EventBus(` call sites in the factory.
- `src/easycat/config/easy.py:471` — EasyConfig has no `slow_handler_threshold_s` or `handler_error_policy` field, so neither is reachable from `create_session(EasyConfig(...))`. `SessionConfig.event_bus` (session/_types.py:136) does accept a pre-built bus, so the knob is reachable one rung down.
- `docs/observability.md:59` — "Configure `EventBus(slow_handler_threshold_s=...)` when you need warnings for callbacks that might stall audio-critical paths." — advice a reader on the documented simple path cannot act on.
- `src/easycat/session/_audio_router.py:758` — `await self._emit(AudioIn(chunk=chunk))` inside `_process_chunk`, and dispatch at events.py:759-763 awaits each handler inline — so a slow AudioIn subscriber directly stalls mic ingestion at 50 fps.

*Verified:* All citations verified to the line; I confirmed by reading the class that `_slow_handler_threshold_s` is written only in `__init__` and has no setter, and that the factory has exactly two `EventBus(` call sites, both bare. I downgraded medium → low: the underlying inline dispatch is documented behaviour the finding itself concedes is not a bug, the mitigation is genuinely reachable one rung down via `SessionConfig.event_bus` (which the original did not mention), and what remains is a missing pass-through field plus a doc that points at it from the wrong rung — a small, contained ergonomics gap rather than a correctness or performance defect.

#### 63. The coding-agent context pack drops the error message, traceback, and machine-generated notes, leaving only the exception type

`LOW` · `api-ergonomics` · `effort: small`

**Problem.** `easycat bundles export` exists to hand a failing session to a coding agent, but the error projection keeps only `type`, `code` and `status`. An agent debugging a failure receives `{"error": {"type": "HTTPStatusError"}, "omitted_error_fields": 3}`. The `notes` field is the clearest loss: `annotate_stage_exception` deliberately attaches stage/provider/elapsed_ms/sequence/record_key so a traceback can be traced back to the journal record that captured the failing input, and the projection discards exactly that.

**Impact.** The "share this with a coding agent" workflow produces a pack in which every failure is reduced to an exception class name with no stage, no provider, no elapsed time, and no journal sequence to look up in the source bundle. A user who needs to actually diagnose the failure has to fall back to shipping the raw bundle, which contains unfiltered PCM audio and transcripts.

**Fix.** Add `"notes"` to `_CONTEXT_ERROR_KEYS` in `src/easycat/cli/debug/_context_projection.py:57`. The notes payload is generated entirely by `annotate_stage_exception` (stages/base.py:249) from stage/provider/timing/sequence values and carries no user or provider text, so it is safe under the production boundary and restores the correlation the pack needs. Update the pack README at cli/debug/export.py:104 to state that error messages and tracebacks are stripped, since they are not currently listed.

**Evidence**

- `src/easycat/cli/debug/_context_projection.py:57` — `_CONTEXT_ERROR_KEYS = frozenset(("type", "code", "status"))`, consumed by `_project_error` (:80-87) which counts everything else as `omitted_error_fields`. So `message`, `traceback` and `notes` are all discarded.
- `src/easycat/stages/base.py:249` — `annotate_stage_exception` attaches `stage`, `provider`, `elapsed_ms`, `sequence`, `record_key` as PEP 678 notes — entirely machine-generated correlation data, with no user or provider text in it. It lands in `ErrorInfo.notes` and is then dropped by the projection above.
- `src/easycat/cli/debug/export.py:104` — Pack README: "This pack intentionally omits raw journal payload fields such as transcripts, prompts, generated text, tool arguments, tool results, and provider responses." — error messages and notes are not on that list but are omitted anyway, so the pack's own documentation understates what it strips.

*Verified:* The allowlist and the drop are confirmed exactly as described. But **two of the finding's four supporting claims are wrong and I removed them, which changes the recommendation.** (1) It cites `_assert_context_pack_redacted` (export.py:189) as "a second, independent safety net that would have caught a leaking message" — I read `contains_unredacted_sensitive_text` (validation/redaction.py:180) and it only scans for sensitive URLs, `sk-`-style secrets, JWTs, header secrets and request ids. It would not catch a transcript or prompt echoed back inside a provider error body, so it is not a net for the thing at issue. (2) It claims `--redaction` is "accepted but currently ignored" at export.py:212 — the code at :222-225 does force `production`, but :354 emits an explicit `warn("Context-pack export currently applies the conservative production boundary.")` when a non-production policy is requested, so it is disclosed, not silently ignored. Separately, `_redact_error` (safe_defaults.py:365) applies only `redact_text`, not the `UNSAFE_TEXT_FIELDS` key policy, so an error `message` genuinely can carry raw prompt or transcript text a provider echoed back — meaning dropping `message` is a defensible conservative choice, not the "belt-and-braces over-redaction" the finding asserts. I therefore downgraded medium → low and narrowed the recommendation from "add message + notes + traceback" to "add notes", which is unambiguously safe and recovers most of the diagnostic value.

#### 64. Vendored FunASR VAD writes six anomaly messages to stdout, bypassing the easycat logger, in a file exempted from lint

`LOW` · `maintenance-burden` · `effort: small`

**Problem.** Six `print()` calls sit in the vendored FunASR VAD state machine. They write to stdout unconditionally, are not routed through the `easycat` logger, cannot be silenced by `EASYCAT_LOG_LEVEL`, and are exempted from lint by the file-level `# ruff: noqa` at line 1. `_logging.py` and docs/observability.md both promise the opposite guarantee for library code.

**Impact.** When the FunASR backend is selected — explicitly, or via the `auto` fallback chain when silero is missing — an anomalous VAD state emits unformatted, uncorrelated lines to stdout during a live call: no session id, no turn id, no level, and no way to turn them off. Bounded, because the prints are guarded by anomaly conditions rather than firing on every frame, so a healthy session is silent. The blanket `# ruff: noqa` is the part with lasting cost: it guarantees the next vendor refresh reintroduces this without any signal.

**Fix.** In `src/easycat/vad/_funasr_runtime/e2e_vad.py`, replace the six `print()` calls at :361, :371, :381, :393, :416 and :432 with `logger.debug`/`logger.warning` on a module-level `logging.getLogger(__name__)` — a ~10-line patch that is trivially re-applicable on vendor updates. Narrow the `# ruff: noqa` at :1 to the specific rules the vendored style actually violates (E501, E741, N802, etc.) so `T201` still fires and a reintroduced print is caught at lint time rather than in a user's terminal.

**Evidence**

- `src/easycat/vad/_funasr_runtime/e2e_vad.py:361` — `print("error in calling pop data_buf\n")` in `PopDataToOutputBuf`. Full set, confirmed by grep — exactly six: :361, :371 (`print("warning\n")`), :381, :393 (`print("Something wrong with the VAD algorithm\n")`), :416 and :432 (both `print("not reset vad properly\n")`, in `OnVoiceStart`/`OnVoiceEnd`).
- `src/easycat/vad/_funasr_runtime/e2e_vad.py:1` — `# ruff: noqa` — a blanket file-level suppression, so no lint rule (including T201) will ever flag these, and a future vendored-code refresh reintroduces them silently.
- `src/easycat/_logging.py:3` — "Library code stays silent by default ... attaches exactly ONE tagged handler to the ``easycat`` logger (never the root logger) so applications that already own logging are never clobbered." — the same guarantee is made to readers at docs/observability.md:28-30. Raw `print()` is outside that contract entirely: `EASYCAT_LOG_LEVEL` cannot silence it.
- `src/easycat/vad/factory.py:103` — `_VADBackendSpec("funasr", "FunASR", _build_funasr)` — a shipped backend, and the `auto` chain at :33-34 falls through to it when silero is unavailable, so a user can reach this code without naming it.

*Verified:* All six print lines confirmed by grep at exactly the cited numbers, `# ruff: noqa` confirmed at line 1, and `_logging.py`'s silence guarantee confirmed at :3-8. I checked reachability and added it: funasr is a registered backend in vad/factory.py:103 and sits in the `auto` fallback chain, which the original did not establish. I trimmed two overstated pieces from the impact: the prints are guarded by error conditions inside the state machine rather than firing per frame, and I found no path where the FunASR VAD runs under a `--json` CLI command, so the "corrupts `--json` stdout envelopes" claim has no support here. I also dropped the recommendation's "add a repo-wide test asserting no `print(` outside cli/, server/ and debugger/" — there are legitimate prints in validation/_release_runner.py:52,75, validation/runner.py:54 and helpers.py:218 that such a test would have to special-case, and narrowing the noqa achieves the same guard for free.

---

### Transports, telephony, and server runtime

*10 findings — 4 high · 4 medium · 2 low*

**Assessment.** Under real network conditions this codebase is better than average at the *mechanics* of teardown — the reconnect-race guards in the Twilio mixin, the bounded force-escalation in `CapacityGate._escalate_graceful_stop`, WebTransport's inline sample-rate header, and the debugger's loopback hardening are all genuinely well-reasoned, and dead-peer detection exists on every transport (websockets keepalive, aioice consent freshness, QUIC idle timeout). The single biggest problem is that the telephony ingress — the path that carries real, billable phone calls — is the least protected surface in the repo: `examples/twilio_app.py` (the documented production starting point) binds `0.0.0.0:8766` with no auth and no concurrency cap, and every accepted socket builds and *starts* a full Session (STT/TTS network handshakes) before the one-time stream token is ever checked. Two more systemic gaps compound it: the "unified bind guard" that the docs present as covering every transport actually covers only WebSocket and WebRTC — WebTransport has literally zero auth code and defaults to `0.0.0.0`, and `EasyConfig.phone()` does too. Third, the shutdown story is inverted from what the deployment guide promises: all four server implementations close the listener (which closes every client connection with code 1001) *before* the drain window, so `drain_timeout_s=30` bounds the teardown of an already-severed socket, never the conversation. Beyond security, two quiet quality defects bite every user: the shipped resampler falls back to pure-Python linear interpolation in every declared install (soxr/scipy appear nowhere in `pyproject.toml`), costing ~21 ms of CPU per concurrent call per wall-second and ~33 dB SNR from stateless per-frame resampling; and barge-in is simply broken on the WebSocket transport, whose `clear_audio()` is a no-op justified by a false claim.

**Done well here:**

- The Twilio reconnect-race handling is genuinely subtle and correct: `_TwilioProtocolMixin._finalize_after_receive` (twilio_media.py:522-541) only tears down when the handler still owns the slot or the slot is unclaimed, and `send_audio`/`send_mark` emit `CallEnded` *before* releasing `self._ws` (twilio_media.py:818-836, 863-872) so a replacement connection can't swallow the previous call's end event. The reasoning is written down in the code, not just implied.
- `CapacityGate.drain` / `_escalate_graceful_stop` (server/transports.py:137-289) is unusually rigorous teardown code: the drain owns the single graceful stop precisely because `Session.stop` has a `_stopping` idempotency guard that would make a later `force=True` a no-op, both the forced call and the follow-on cancel-await are bounded by `force_timeout_s`, and `_safe_await` correctly re-raises only the drain's *own* cancellation via `current_task().cancelling()`.
- WebTransport's decision to carry the sample rate inline on each audio stream in *both* directions (webtransport.py:33-46, 579-619, 840-857) genuinely eliminates a real cross-stream race that a separate `config` control frame would have; QUIC gives no cross-stream ordering and the design acknowledges that instead of assuming it.
- The debugger server is hardened well past what a debug tool usually gets: loopback-only default with `_check_host` refusing non-loopback without `allow_remote` (server.py:1541-1559), and a four-layer `origin_guard` middleware (server.py:539-587) covering DNS-rebinding via exact `Host` match, `Origin`, `Sec-Fetch-Site`, and a simple-form-POST CSRF block requiring `application/json` plus a present Origin on state-changing methods.
- The G.711 mu-law codec is correct. I verified the 65536-entry encode LUT and 256-entry decode LUT (twilio_media.py:1005-1008) are byte-identical to the reference per-sample functions, decode matches `audioop.ulaw2lin` exactly, and encode differs from `audioop.lin2ulaw` on only 381 of 65536 inputs, all at quantization boundaries on negative samples (±1 step). This is a correct Sun-reference implementation, not a hand-rolled approximation.
- PEP 562 lazy exports in `transports/__init__.py:86-93` keep the 41 ms mu-law LUT build, aioquic, and aiortc off the base import path — verified `import easycat` costs 60 ms cumulative while `import easycat.transports.twilio_media` alone costs 431 ms.
- `WebRTCSignalingHandlers.stats_write_permitted` (server/_webrtc_handlers.py:166-181) correctly refuses to expose an unauthenticated disk-append sink: with no auth policy it requires both a loopback bind and a same-origin request, and the quota check holds `_stats.write_lock` across check+append+counter so concurrent posts can't race past the limits.
- `AudioQueueMixin._emit_degraded` (transports/_base.py:169-223) coalesces per-reason, caps pending emit tasks, and truncates the attacker-controllable detail *before* appending the suppression count — the comment at :202-205 explains that appending first would let a padded detail evict the suppression count. That is a real, correctly-reasoned hardening detail.

#### 65. Twilio media WebSocket builds and starts a billable session before the stream token is validated, and the documented production example has no concurrency cap

`HIGH` · `security` · `effort: medium`

**Problem.** The Twilio Media Streams listener accepts an anonymous WebSocket, constructs an `EasyConfig`, calls `create_session()` and `Session.start()` — which runs provider warmup including a live OpenAI Realtime WebSocket handshake — and only validates the one-time `EasyCatStreamToken` when the first Twilio `start` frame arrives. `telephony/server.py` bounds the blast radius with a 64-slot semaphore and the scaffold template bounds it with `TWILIO_MAX_SESSIONS` (default 8, templates/twilio-phone/server.py:33-42), but `examples/twilio_app.py` — the file the deployment guide names as the production starting point — has no cap at all.

**Impact.** Anyone who can reach the media port (which must be publicly reachable for Twilio to dial back into it) can loop connect/disconnect and force provider sessions to be opened. Under `examples/twilio_app.py` there is no ceiling: N concurrent sockets means N concurrent OpenAI Realtime handshakes and N in-flight sessions until the account rate-limits. Under the in-tree helper the cost is bounded at 64 but real phone calls are still blocked by squatted slots.

**Fix.** Two separate changes. (1) Add a `session_slots = asyncio.Semaphore(...)` to `examples/twilio_app.py:66-110` mirroring `telephony/server.py:178-190` and the scaffold at templates/twilio-phone/server.py:35-42 — the documented example should not be the only uncapped one. (2) Move the token check to the handshake: pass a `process_request` hook (same shape as `server/websocket.py:70-77`) to the three `websockets.serve` calls at examples/twilio_app.py:128, telephony/server.py:225 and templates/twilio-phone/server.py:64, validating a token carried in the `wss://` URL query that `twiml_connect_stream` already mints, and return HTTP 401 before the handler runs. Keep the `start`-frame check at twilio_media.py:592 as defense in depth.

**Evidence**

- `examples/twilio_app.py:128` — `twilio_server = await websockets.serve(handle_twilio_connection, "0.0.0.0", 8766)` — no `process_request` hook, no semaphore. Verified: grep of the file shows the only bearer check (line 176) guards `POST /calls`, not the media socket.
- `examples/twilio_app.py:81` — `session = create_session(EasyConfig(...))` then line 108 `async with manager.connection(key, session, ...)`. Verified SessionManager.add calls `await session.start()` at session_manager.py:52 for every accepted socket.
- `src/easycat/session/_session.py:992` — `await self._warmup.run(select=lambda name: name != "transport")`, gated by `EasyConfig.warmup` which defaults True (config/easy.py:474). Verified the hooks are real network work: stt/openai_realtime_provider.py:142 opens and keeps a persistent Realtime WebSocket; tts/openai_tts.py:107 issues `GET /models`.
- `src/easycat/transports/twilio_media.py:592` — `if not _twilio_stream_token_valid(start, self._config):` inside `_handle_start` — the one-time token is checked only when the first `start` JSON frame arrives, after the session and its provider connections are live. The validator IS wired in the example (twilio_app.py:72), so the defect is ordering plus the missing cap, not a missing token.
- `src/easycat/telephony/server.py:189` — `async with session_slots:` (semaphore built at :178 from `max_sessions: int = 64`, server.py:88) with the same build-then-auth ordering; the class docstring at :70-77 states the session is built "*before* the first ``start`` frame's one-time stream token is validated".
- `src/easycat/server/websocket.py:70` — `def process_request(_ws, request)` passed to `websockets.serve` at :89-96 — handshake-time auth already exists in this codebase for the plain-WebSocket server, so the fix is a known pattern here, not new design.
- `docs/deployment/production-servers.md:175` — "Use `examples/twilio_app.py:create_app` as the production starting point for phone calls." — verified verbatim.

*Verified:* Downgraded critical -> high. Ordering and warmup claims all verified (warmup default True, Realtime opens a persistent socket). Two evidence claims were corrected: the scaffold template DOES cap concurrency (templates/twilio-phone/server.py:33-42, default 8), and `examples/twilio_app.py` DOES wire the token validator (line 72) — so the residual defects are the pre-auth session build and the single uncapped example, not a missing token. Not critical: no data loss or silent wrong output, and the two library-owned paths are capped; the harm is bounded resource/cost abuse on one example file.

#### 66. max_call_duration_s never ends a call — the timer flips a state enum and emits an event no subscriber acts on

`HIGH` · `correctness` · `effort: small`

**Problem.** `OutboundCallStateMachine._max_duration_coro` is the only consumer of `max_call_duration_s`. When it fires it sets the internal state to ENDED and emits `CallEnded(disposition="max_duration")`. No subscriber hangs up the Twilio call, closes the media stream, or stops the session — verified by enumerating every `CallEnded` subscriber in the tree. `OutboundCallManager.hangup_call` is the method that would do it and has no in-tree caller. The PSTN leg, the STT stream, the TTS stream and the agent all keep running past the configured limit.

**Impact.** The one documented cost circuit-breaker on outbound calling silently does nothing. A stuck IVR, hold music, or an agent loop keeps a live phone leg plus streaming STT/TTS/LLM billing running indefinitely, and the only signal is a journal record saying the call 'ended'. Operators who set `max_call_duration_s=300` will believe calls are capped at five minutes.

**Fix.** In `src/easycat/telephony/call_state.py:756-762`, make the timer terminate rather than annotate: give `OutboundCallStateMachine` an `on_max_duration` async callback and have `config/_outbound_helpers.py:_add_state_machine` (line 137-152) bind it to the built `OutboundCallManager.hangup_call`, or emit an `EndCallAction` which `TwilioSessionActionExecutor.execute` already turns into a hangup + `stop_session=True` (telephony/session_actions.py:55-57). Then change tests/telephony/test_outbound_integration.py:607 to assert the Twilio REST `calls(sid).update(status="completed")` was issued, since the current test passes on a call that never hangs up.

**Evidence**

- `src/easycat/telephony/call_state.py:756` — `_max_duration_coro`: `await asyncio.sleep(self._max_call_duration_s)` then `_transition(ENDED)` and `emit(CallEnded(disposition="max_duration"))`. That is the entire body — verified lines 756-762.
- `src/easycat/telephony/outbound.py:366` — `async def _on_call_ended(self, event): self._clear_active_call(event.call_sid)` — the only `CallEnded` subscriber in `OutboundCallManager`; it resets bookkeeping and returns.
- `src/easycat/telephony/outbound.py:280` — `async def hangup_call(self, call_sid=None)` issues `calls(sid).update(status="completed")`. Verified with `grep -rn hangup_call src/ tests/ examples/ docs/`: the only references are the definition and one unit test (tests/telephony/test_outbound.py:601). Nothing in the library ever calls it.
- `src/easycat/telephony/call_state.py:443` — The other `CallEnded` subscribers are call_state.py:604 (`_on_ended`), screening.py:819 and number_health.py:294 — all bookkeeping. Verified: no `CallEnded` subscriber exists anywhere under `src/easycat/session/`, `src/easycat/transports/` or `src/easycat/config/`.
- `tests/telephony/test_outbound_integration.py:607` — `test_max_call_duration_terminates_call` asserts only `sm.state == OutboundCallState.ENDED`. The test name claims termination; the assertion covers an enum. No test asserts a hangup.
- `src/easycat/config/easy.py:395` — `max_call_duration_s: int = 300` on `OutboundCallConfig`, validated positive at :419, threaded to the state machine at config/_outbound_helpers.py:142.

*Verified:* Confirmed at claimed severity. Every cited line verified; I additionally enumerated all four `CallEnded` subscribers across the repo to rule out a non-obvious handler, and read the existing test, which only asserts the enum. The original finding cited call_state.py:757 for the sleep; the coroutine starts at 756.

#### 67. Servers close client WebSockets before the drain window, so a deploy cuts live calls mid-sentence; the Twilio server has no drain window at all

`HIGH` · `correctness` · `effort: medium`

**Problem.** Every raw-`websockets` audio listener is closed with the library default `close_connections=True`, which immediately closes established client connections with code 1001. In `VoiceServer.stop()` the drain window that follows therefore applies to sessions whose transport is already dead — `drain_timeout_s` only bounds teardown. `serve_websocket_sessions`, `serve_twilio_voice_app` and `examples/twilio_app.py` do not even have a drain phase: they go straight from `close()` to `stop_all()`. The WebRTC helper is the only server where the drain window actually protects a live conversation, and that is incidental to WebRTC media living outside the HTTP listener.

**Impact.** A rolling deploy or pod eviction drops every in-progress phone call and browser WebSocket session mid-sentence — the caller hears a click. Operators sizing `terminationGracePeriodSeconds` around `drain_timeout_s` per the deployment guide get no benefit from it on the audio path.

**Fix.** In `src/easycat/server/voice_server.py:371-374`, call `ws_server.close(close_connections=False)` so the listener stops accepting while established connections survive, then close survivors after the drain at :388 returns (a second `ws_server.close()` is idempotent). Add the same draining flag plus a bounded drain to `serve_twilio_voice_app` (`src/easycat/telephony/server.py:243-250`) and `serve_websocket_sessions` (`src/easycat/server/websocket.py:100-106`) by reusing `CapacityGate.start_draining`/`drain` as `webrtc_routes.py:561-569` already does. Then correct `docs/deployment/production-servers.md:95-107` to say which phase keeps calls alive.

**Evidence**

- `src/easycat/server/voice_server.py:373` — `ws_server.close()` in step (3) of `stop()`, executed before the drain at :388. Verified against the installed library: websockets 15.0.1 `Server.close(close_connections: bool = True)` (.venv/.../websockets/asyncio/server.py:408) documents "Close open WebSocket connections with close code 1001 (going away)" as the default.
- `src/easycat/server/voice_server.py:388` — `await self._gate.drain(self._active_session_pairs, drain_timeout_s=drain_timeout, force_after=True, ...)` — runs after the sockets are already closed, so `drain_timeout_s` (config.py:49, default 30.0) bounds only `session.stop()` teardown, never the conversation.
- `src/easycat/server/websocket.py:104` — `finally: server.close(); await server.wait_closed(); await manager.stop_all()` — `serve_websocket_sessions` has no draining phase at all.
- `src/easycat/telephony/server.py:247` — `finally: media_server.close(); await media_server.wait_closed(); ...; await manager.stop_all()` — no draining flag, no drain window, no force-escalation bound. Every in-flight phone call is severed the instant the shutdown event fires.
- `examples/twilio_app.py:134` — `twilio_server.close(); await twilio_server.wait_closed(); await manager.stop_all()` — same immediate-cut shape in the documented production example.
- `docs/deployment/production-servers.md:103` — "3. Wait for active sessions up to `drain_timeout_s` (graceful `session.stop()`)." and :252-253 "give live calls a bounded drain window" — describes protection the audio path does not get.
- `src/easycat/server/webrtc_routes.py:561` — The WebRTC helper does `gate.start_draining()` then `await site.stop()` then `gate.drain(...)`; because WebRTC media rides a separate peer connection, stopping the aiohttp signaling site does NOT cut live audio. That path is unaffected — the defect is specific to the raw-`websockets` audio transports.
- `src/easycat/server/voice_server.py:366` — Verified with `grep -rn close_connections src/ tests/ docs/`: zero hits. No call site anywhere opts out of closing established connections.

*Verified:* Confirmed at claimed severity. I read the installed websockets 15.0.1 source to verify `close_connections=True` is the default and closes with 1001, and grepped the whole repo for `close_connections` (zero hits). Added the correction that the WebRTC helper is NOT affected — its drain works because media does not ride the closed listener — which narrows the finding to the raw-`websockets` transports. examples/twilio_app.py line is 134, not 133.

#### 68. Barge-in is a no-op on both WebSocket transports, justified by a false docstring, and the bundled WS client cannot cancel scheduled audio

`HIGH` · `correctness` · `effort: medium`

**Problem.** Interruption flushes the session's bounded outbound queue, but on the WebSocket transports everything already handed to `ws.send()` is unrecoverable: 32 KiB of websockets write buffer, the kernel socket buffer, bytes in flight, and decisively everything the browser already received and scheduled. There is no `clear` control message in the WebSocket wire protocol (docs/browser-playground.md:60-67 lists only `ready` and `audio_format` outbound), the bundled client ignores the `interruption` event, and the outbound loop applies no realtime pacing so the client is typically seconds ahead of the playhead.

**Impact.** On the WebSocket path a user who barges in is talked over for the full length of already-transmitted TTS. The default `easycat serve` mode is WebRTC, which is unaffected — so this bites `--mode websocket`, `examples/ws_browser_client.html`, and every bring-your-own-server integration built on `WebSocketConnectionTransport`. The false docstring is the compounding problem: it tells the next maintainer there is nothing to fix, and no test would catch it.

**Fix.** Delete the false docstrings at `src/easycat/transports/websocket.py:167` and `:364` and implement the hook: add `{"type": "clear"}` to the outbound WebSocket wire protocol (document it in docs/browser-playground.md:60-67), send it from both `clear_audio()` implementations, and in `examples/ws_browser_client.html:160-183` keep the scheduled `AudioBufferSourceNode`s in an array, `.stop()` each on receipt and reset `nextPlayTime = playCtx.currentTime`. Optionally pace outbound WebSocket writes to realtime in `session/_audio_router.py` so the client is never more than ~200 ms ahead.

**Evidence**

- `src/easycat/transports/websocket.py:167` — `async def clear_audio(self) -> None:` with body `"""No-op — WebSocket sends frames immediately without buffering."""` — the premise is wrong at three layers: websockets sets `write_limit = 2**15` (32 KiB) and `Connection.send` awaits `self.drain()` (.venv/.../websockets/asyncio/connection.py:60, 915, 1054), plus the kernel socket buffer, plus everything the browser already received.
- `src/easycat/transports/websocket.py:364` — Identical no-op body in `WebSocketConnectionTransport` — the class `VoiceServer`'s `/ws` route builds (voice_server.py:910) and `serve_websocket_config_sessions` builds (server/websocket.py:124).
- `src/easycat/session/_session.py:1302` — `await clear_audio_if_supported(self.transport)` (also :1332, :1350) — the session does invoke the hook on interruption; the hook itself does nothing on WebSocket. WebRTC (webrtc.py:386 `self._outbound.clear()`), Twilio (twilio_media.py:879, 1108) and WebTransport (webtransport.py:1233) all implement it for real.
- `src/easycat/session/_audio_router.py:388` — The outbound drain loop has no realtime pacing — it pushes chunks as fast as `transport.send_audio` accepts them, so a streaming TTS provider hands the browser seconds of speech ahead of the playhead. Contrast the WebRTC path, which produces paced 20 ms frames.
- `examples/ws_browser_client.html:181` — `src.start(nextPlayTime); nextPlayTime += buffer.duration;` — each chunk is scheduled at an absolute future time and the `AudioBufferSourceNode` handle is dropped, so nothing can `.stop()` it.
- `examples/ws_browser_client.html:255` — The `ws.onmessage` text branch handles only `audio_format` (:255) and `session` (:258). There is no `interruption` case and no playback flush, even though `_browser_events.py:127` emits one.
- `docs/extending/transport.md:125` — "`clear_audio()` is invoked only when present; implement it if your transport buffers outbound audio, so barge-in feels instant." The shipped WebSocket transport violates the guidance the extension guide gives third-party authors.
- `src/easycat/testing/contracts.py:349` — `test_clear_audio_is_idempotent` is the only contract test on this hook — it asserts calling it twice does not raise. Nothing anywhere asserts that audio actually stops.

*Verified:* Confirmed at claimed severity. Verified the no-op bodies, the session's clear-audio call sites, that all three other transports implement the hook for real, that the outbound router applies no pacing, and that the bundled WS client neither handles `interruption` nor retains node handles. Corrected the scope: the default browser playground uses WebRTC (docs/browser-playground.md:3-7) where barge-in works, so this is confined to the WebSocket transports rather than 'browser sessions' generally. Kept at high because the false docstring is an active maintenance trap and no test covers the behavior.

#### 69. enforce_bind_guard is documented as covering every transport but WebTransport has zero auth code and defaults to 0.0.0.0

`MEDIUM` · `security` · `effort: medium`

**Problem.** `enforce_bind_guard` is called from exactly three places: `VoiceServer.start`, `serve_websocket_sessions` and `serve_webrtc_config_sessions`. `WebTransportServer.start` calls it nowhere, `serve_webtransport_config_sessions` calls it nowhere, and the WebTransport module contains no token, auth, or loopback concept whatsoever — while defaulting `host` to `0.0.0.0`. The deployment guide tells operators the guard is universal.

**Impact.** `run_webtransport_config_server()` opens an unauthenticated audio ingest on every interface over QUIC, and an operator who read docs/deployment/production-servers.md:88-93 will believe a non-loopback bind without a token cannot happen. WebTransport is worse than a missing guard call because there is no configuration knob that would satisfy the guard — the transport has no token concept to enable. Practical exposure is limited: WebTransport requires the optional `webtransport` extra plus explicit `certfile`/`keyfile` (webtransport.py:255-262), so it is never on by default.

**Fix.** Either (a) add an `auth_token` / `unsafe_allow_no_auth` pair to `WebTransportTransportConfig` (src/easycat/transports/webtransport.py:231-251), validate it on the H3 CONNECT request in the session handler, and call `enforce_bind_guard` at the top of `serve_webtransport_config_sessions` (src/easycat/server/webtransport.py:29-37); or (b) until there is an auth story, have `WebTransportServer.start` raise on a non-loopback bind outright and flip the `host` default to `127.0.0.1` to match websocket.py:60 and _webrtc_config.py:78. Either way, correct docs/deployment/production-servers.md:88-93 to name the three helpers that actually enforce the guard.

**Evidence**

- `docs/deployment/production-servers.md:90` — "this is the single structured guard applied to every transport, and it closes a previously unauthenticated `0.0.0.0` WebSocket voice endpoint" — verified verbatim. `grep -rn enforce_bind_guard src/easycat/` returns exactly three call sites: server/voice_server.py:187, server/websocket.py:62, server/webrtc_routes.py:524.
- `src/easycat/transports/webtransport.py:239` — `host: str = "0.0.0.0"` in `WebTransportTransportConfig`. Verified with `grep -icE 'auth|token|bind_guard|loopback' src/easycat/transports/webtransport.py` → 0 across all 1577 lines. There is no authentication concept on the WebTransport path at all.
- `src/easycat/server/webtransport.py:36` — `server = WebTransportServer(config, handle_connection); await server.start()` in `serve_webtransport_config_sessions` — never calls `enforce_bind_guard`, unlike its WebSocket and WebRTC siblings.
- `src/easycat/transports/webrtc.py:236` — `if not is_loopback_host(self._config.host) and auth_token is None: raise ValueError(...)`, with a `127.0.0.1` default at _webrtc_config.py:78 — the correct posture. `WebSocketTransportConfig` also defaults to `127.0.0.1` (websocket.py:60, :70). WebTransport is the outlier.
- `src/easycat/server/auth.py:243` — Docstring: "Both the WebSocket and WebRTC serve helpers call this so the behavior — and the ``0.0.0.0`` gap it closes — is identical across transports." The module docstring is accurate about which helpers call it; the deployment doc generalizes it to 'every transport'.

*Verified:* Downgraded high -> medium and narrowed to WebTransport. The WebTransport half is fully verified (zero auth tokens in 1577 lines, no guard call, 0.0.0.0 default) and the doc overclaim at line 90 is verbatim. I dropped the Twilio half of the original finding: `TwilioTransportConfig.host = "0.0.0.0"` is intentional (Twilio must dial in from the public internet) and the pre-auth-session problem it describes is already covered by the first finding — keeping it here would double-count. Medium rather than high because WebTransport needs an optional extra plus explicit TLS cert paths, so no default configuration is exposed.

#### 70. The shipped resampler is always the pure-Python linear fallback: soxr and scipy are declared nowhere, and resample() carries no state across chunks

`MEDIUM` · `performance` · `effort: medium`

**Problem.** `resample()` advertises soxr/scipy preference, but neither library is declared in any of pyproject.toml's extras, mentioned in the docs, or checked by `easycat doctor` — so every real install runs the pure-Python linear path (confirmed by running the resolver in this repo's venv). That path compounds two problems: it is a per-sample Python loop on the audio hot path, and it is stateless across chunks, so each 20 ms frame is resampled in isolation with no carried phase or filter memory. The outbound telephony leg additionally decimates 24k→8k with no anti-alias filter.

**Impact.** Chronic, test-invisible audio quality tax on every telephony call — aliasing on the 24k→8k TTS leg and a boundary discontinuity 50 times per second on both legs — plus a CPU tax that makes the documented `max_sessions`/`max_concurrent_sessions` default of 64 optimistic, since resampling runs inline in the receive loop and adds latency jitter to the turn path.

**Fix.** Add `soxr` and `numpy` to the `telephony`, `webrtc`, `webtransport` and `quickstart` extras in pyproject.toml (soxr is a small wheel and the right tool), and have `easycat doctor` report the resolved backend from `_resolve_resample_backend()`. Independently, make resampling stateful on the streaming paths: hold a per-stream `soxr.ResampleStream` (or carry the linear path's fractional phase and tail sample) in `transports/twilio_media.py` and `tts/base.py` instead of calling the stateless `resample()` per chunk. If soxr will not be shipped, delete `_resample_soxr`/`_resample_scipy` (src/easycat/_audio_utils.py:131-182) and drop the preference claim at line 66.

**Evidence**

- `src/easycat/_audio_utils.py:66` — "Prefers high-quality backends (soxr, scipy) when available and falls back to linear interpolation if not." Verified `grep -n 'soxr\|scipy' pyproject.toml` → no hits; `grep -rn 'soxr\|scipy' docs/ README.md src/easycat/cli/` → no hits. Neither backend is in any extra, and `easycat doctor` never reports which backend resolved.
- `src/easycat/_audio_utils.py:105` — `_resolve_resample_backend` needs numpy+soxr or numpy+scipy and otherwise returns "linear". Executed in this repo's own venv: prints `linear`. `_resample_soxr`/`_resample_scipy` and their `_impl` helpers (lines 131-182) are dead code in every declared install.
- `src/easycat/_audio_utils.py:203` — `_resample_linear` — a per-sample Python loop with no anti-imaging/anti-aliasing filter. Measured in this venv: 304 µs per 20 ms frame for 8k→16k and 154 µs for 16k→8k, i.e. ~23 ms of single-threaded CPU per concurrent call per wall-second across both legs. (Measured on this Rockchip RK3588 ARM board; a typical x86 server will be several times faster, but still 1-2 orders of magnitude slower than soxr.)
- `src/easycat/_audio_utils.py:60` — `resample()` is stateless — no filter memory, no fractional-phase carry across calls. Measured: resampling a 1 s 440 Hz tone in 20 ms blocks versus whole gives 40.5 dB SNR with peak sample error 2032 on a 12000-amplitude signal, i.e. an edge transient at every one of the 50 frame boundaries per second.
- `src/easycat/transports/twilio_media.py:646` — `pcm_data = mulaw_to_pcm16(mulaw_data, self._audio_format.sample_rate)` on every inbound 20 ms Twilio frame (50 Hz) → `resample(pcm_8k, 8000, target_rate)` at twilio_media.py:937.
- `src/easycat/tts/base.py:131` — `data = resample(data, source_format.sample_rate, self._output_format.sample_rate)` — the outbound telephony leg. `EasyConfig` aligns TTS output to `PCM16_MONO_8K` for Twilio (config/_tts_alignment.py:84-87, twilio_media.py:41), so a 24 kHz OpenAI TTS stream is decimated 24k→8k here, chunk by chunk, by linear interpolation with no low-pass filter — content in 4-12 kHz aliases straight back into the voice band.
- `src/easycat/server/config.py:48` — `max_sessions: int = 64` — at ~23 ms/call/wall-second of resampling alone, 64 concurrent calls needs ~1.5 s of CPU per wall-second on one asyncio loop before STT, VAD, AEC or TTS is counted.

*Verified:* Downgraded high -> medium. I re-ran the numbers rather than trusting them: backend resolves to `linear` in this venv, 304 µs / 154 µs per 20 ms frame, and blockwise-vs-whole SNR is 40.5 dB with peak error 2032 (the original claimed 32.6 dB / 3527 — corrected). I also corrected the outbound mechanism: `pcm16_to_mulaw` usually sees 8 kHz already because `_tts_alignment.py` retargets TTS output, so the outbound resample actually happens in `tts/base.py:131` (24k→8k, which is the aliasing-prone direction) — a stronger point than the one originally made. Medium not high: it is a quality and capacity tax, not a failure, and the CPU numbers come from a slow ARM SBC.

#### 71. A slow WebSocket client stalls the whole session: browser-event sends are awaited inline in serial EventBus dispatch with no timeout

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `BrowserEventForwarder` subscribes to `STTPartial`, `STTFinal`, `AgentDelta`, `AgentFinal`, `TurnStarted`, `Interruption` and `BotStartedSpeaking` (_browser_events.py:88-96) and awaits a WebSocket send inside each handler. `EventBus.emit` awaits handlers one at a time. A client that stops reading — backgrounded tab, congested uplink, half-open TCP — fills the 32 KiB write buffer, `ws.send()` parks on the drain future, and the emitting coroutine (on the STT/agent hot path) blocks until websockets' keepalive gives up, roughly 20-40 s at the default `ping_interval=20`/`ping_timeout=20`. A merely slow client that still answers pings can block indefinitely.

**Impact.** One slow browser can freeze its own session for tens of seconds per event: transcripts, agent progress and TTS scheduling all stop. Because emission is serial and blocking, no *subsequent* event is emitted either, so the journal — the documented single source of truth for observability — records nothing for the duration of the incident. The WebRTC forwarder is unaffected (aiortc data-channel `send` does not await a drain); this is specific to the WebSocket transports.

**Fix.** Bound the send in `src/easycat/transports/_browser_events.py:150-154`: wrap `self._send_json(payload)` in `asyncio.wait_for(..., timeout≈0.25)` and drop on `TimeoutError`, or push payloads onto a small bounded per-connection queue drained by a dedicated writer task — the pattern `_WebTransportSession._outbound_writer` (webtransport.py:824) already uses. The class docstring at :76-78 already asserts delivery is never load-bearing; make the implementation honor it.

**Evidence**

- `src/easycat/events.py:758` — `for handler in handlers:` → `result = handler(event)` / `if asyncio.iscoroutine(result): await result` (lines 758-762). Dispatch is strictly serial and each handler is awaited, so one slow handler blocks every later handler and the emitting coroutine.
- `src/easycat/transports/_browser_events.py:152` — `await self._send_json(payload)` inside `_send` — no `asyncio.wait_for`, no shielding, no fire-and-forget. The `except Exception` at :153 catches failures but cannot bound a hang.
- `src/easycat/transports/websocket.py:369` — `await self._ws.send(json.dumps(payload))` in `WebSocketConnectionTransport._send_client_event`. Verified in the installed library: websockets 15.0.1 sets `write_limit = 2**15` (asyncio/connection.py:60) and `send` awaits `self.drain()` (:915, :1054), which parks on a future until the transport resumes writing or the connection dies.
- `src/easycat/transports/websocket.py:324` — `self._ensure_browser_event_forwarder()` in `connect()` — attached unconditionally on every `WebSocketConnectionTransport` session, so this path is always live, not opt-in.
- `src/easycat/transports/_browser_events.py:76` — Class docstring: "Delivery is observability, never load-bearing: send failures are logged at debug level and dropped." docs/browser-playground.md:99 repeats it: "a slow or closed channel never blocks the audio pipeline." Both are true for failures and false for slowness.

*Verified:* Confirmed at claimed severity. Verified serial awaited dispatch in EventBus.emit, the unbounded await in `_send`, and the websockets 15.0.1 write-limit/drain behavior in the installed package. Corrected one impact claim: the journal sink subscribes before the forwarder (session/_journal_sink.py:330 runs at construction, the forwarder at transport connect), so it is not blocked *behind* the forwarder for the same event — the real mechanism is that no further events are emitted at all while the send is parked. Line for `_send_client_event` is 367-370, not 370 exactly.

#### 72. Inbound audio is bounded by frame count, not bytes, and no websockets.serve call sets max_size

`MEDIUM` · `security` · `effort: small`

**Problem.** The WebSocket-family transports bound the inbound queue at 200 entries but place no limit on entry size, and no `websockets.serve` call in the repo passes `max_size`, leaving the library's 1 MiB default. One connection can therefore pin roughly 200 MiB of queued audio (about 400 MiB if it negotiates an upsampled rate) before the `_enqueue_inbound_chunk` drop path engages, because that path triggers on frame *count* and the count stays at 200.

**Impact.** With `VoiceServerConfig.max_sessions = 64` the theoretical peak is multiple GiB of resident audio from clients that have passed auth (a buggy or compromised browser, or any authenticated tenant); on the Twilio media port no auth is required to reach the receive loop at all. The process OOMs rather than degrading, and the per-connection drop path designed to protect it never fires. The secondary effect is worse in practice: an oversized frame that needs resampling blocks the receive loop for a large fraction of a second per frame.

**Fix.** Pass an explicit `max_size` sized to a sane audio frame (e.g. `max_size=64 * 1024`) to all six `websockets.serve` call sites: transports/_base.py:385, server/voice_server.py:801, server/websocket.py:89, telephony/server.py:225, examples/twilio_app.py:128, cli/scaffold/templates/twilio-phone/server.py:64. Then extend `_init_audio_queue`/`_enqueue_inbound_chunk` in `src/easycat/transports/_base.py:125-135` and :47-90 to track queued bytes alongside frame count with a configurable ceiling, reusing the existing `inbound_queue_full` degraded code.

**Evidence**

- `src/easycat/transports/_base.py:385` — `self._server = await websockets.serve(self._handle_connection, self._host, self._port, compression=None)` — no `max_size`. Verified `grep -rn max_size src/easycat/ tests/` → zero hits anywhere in the repo, and websockets 15.0.1 defaults `max_size: int | None = 2**20` (.venv/.../websockets/asyncio/server.py:722). Same omission at server/voice_server.py:801, server/websocket.py:89, telephony/server.py:225, examples/twilio_app.py:128 and cli/scaffold/templates/twilio-phone/server.py:64.
- `src/easycat/transports/_base.py:128` — `asyncio.Queue(maxsize=max_pending_chunks)` in `_init_audio_queue` — the only inbound bound is a count of chunks (200 by default, websocket.py:63), with no accounting of the bytes each chunk carries.
- `src/easycat/transports/websocket.py:231` — `chunk = AudioChunk(data=message, format=self._audio_format)` — an arbitrary-length binary frame becomes one queue entry; the receive loop has no length check before `_enqueue_chunk`.
- `src/easycat/transports/websocket.py:45` — `_valid_config_sample_rate` accepts 8000..384000, so a client can negotiate 8 kHz against a 16 kHz pipeline and have every frame doubled in size by `resample_chunk` (websocket.py:241) before it is queued. Worse, a 1 MiB frame through the pure-Python linear resampler is roughly half a second of blocking CPU inside the receive loop.
- `src/easycat/transports/webtransport.py:134` — `_MAX_STREAM_DATA = 64 * 1024` — WebTransport does bound a single delivery through the QUIC flow-control window. The WebSocket family has no equivalent.

*Verified:* Confirmed at claimed severity. Verified zero `max_size` occurrences repo-wide and the 1 MiB library default in the installed package, plus the count-only queue bound and the absence of any length check in either receive loop. Added a verified consequence the original missed: an oversized frame that triggers `resample_chunk` costs roughly half a second of blocking pure-Python CPU in the receive loop, which is a cheaper attack than the memory one.

#### 73. WebTransport is ~3,400 LOC of protocol plumbing resting on three private aioquic attributes, for a transport the deployment guide already calls niche

`LOW` · `over-engineering` · `effort: medium`

**Problem.** A third browser transport carries ~3,445 lines, has no authentication concept at all (see the bind-guard finding), requires TLS certificates, HTTP/3, QUIC and a QUIC-aware load balancer, and rests its outbound memory bound on three undocumented aioquic internals. The control codec, the tag-byte stream demultiplexer, the private-attribute backpressure probe and the lazily-constructed protocol class are QUIC/H3 plumbing driven by wire-format choices, not by anything specific to voice.

**Impact.** For a pre-1.0 project the cost is paid on every refactor and every aioquic release. The private-attribute dependency is the concrete risk: an aioquic rename turns the outbound memory bound into a no-op, and the only signal is a journal record that, by this codebase's own account, nothing is watching.

**Fix.** The scope decision (keep, extract to `easycat-webtransport`, or delete) is the maintainer's. The defect-shaped part is actionable now: file an aioquic issue for a public per-stream buffered-bytes accessor and, until it exists, add a startup assertion in `src/easycat/transports/webtransport.py` that the three private attributes resolve — so a rename fails loudly at connect rather than degrading to an unbounded-memory no-op mid-call. Replacing the tag-byte demultiplexer with distinct unidirectional streams would let `_dispatch_untagged_stream`, `_reject_stream` and both DoS caps be deleted.

**Evidence**

- `src/easycat/transports/webtransport.py:784` — `streams = getattr(quic, "_streams", None)` then :792-793 `sender = getattr(stream, "sender", None); raw_buffer = getattr(sender, "_buffer", None)` — the outbound backpressure gate reads three private aioquic attributes (`_quic`, `_streams`, `sender._buffer`) with no public equivalent.
- `src/easycat/transports/webtransport.py:199` — Comment: "If a future aioquic release renames any of these, the probe can no longer measure buffered bytes and the gate silently degrades to a no-op — re-exposing the unbounded-memory failure `_OUTBOUND_SEND_BUFFER_HIGH_WATER` exists to prevent." The module ships a dedicated degraded code (`_DEGRADED_OUTBOUND_BACKPRESSURE_BLIND`, :206) and a 22-line reporter (:801-822) whose only job is to announce that this design broke.
- `src/easycat/transports/webtransport.py:885` — `def _get_protocol_class() -> type:` — a `QuicConnectionProtocol` subclass defined inside a function and cached in the module global `_PROTOCOL_CLASS_CACHE` (:882), so it cannot be statically typed, subclassed, or referenced by out-of-tree code.
- `src/easycat/transports/webtransport.py:479` — `_dispatch_untagged_stream` (:479) plus `_reject_stream` (:549) — a hand-rolled stream demultiplexer with two bespoke DoS caps (`_MAX_PENDING_TAG_STREAMS` :170, `_MAX_REJECTED_STREAMS` :181) required only because the wire format tags streams with a leading byte.
- `docs/deployment/production-servers.md:168` — "Keep WebTransport behind the optional `webtransport` extra and deploy it only where certificate, HTTP/3, QUIC, and load-balancer support are explicit." The project's own operator guidance treats it as niche.
- `src/easycat/transports/webtransport.py:1` — Measured: 1577 lines in transports/webtransport.py + 68 in server/webtransport.py + 1800 across six test files under tests/transports/ = ~3,445 lines total.

*Verified:* Downgraded medium -> low. Every fact checks out: the three private attribute reads, the blind-probe code and reporter, the in-function protocol class, both DoS caps, the deployment-guide line, and the LOC counts (1577 + 68 source, 1800 test). But this is a scope/product judgment rather than a defect — the code works today — so it is a low-severity maintenance observation. I rewrote the recommendation to separate the actionable hardening (fail loudly on an aioquic rename) from the 'delete the package' opinion, and removed the 'closing an unauthenticated surface' framing since that is already its own finding.

#### 74. WebSocketTransport and WebSocketConnectionTransport duplicate the same wire protocol in one file and have already diverged on the disconnect race

`LOW` · `maintenance-burden` · `effort: small`

**Problem.** The two WebSocket transports implement the same documented wire protocol twice in one file. `_TwilioProtocolMixin` was written to eliminate exactly this duplication for the Twilio pair and `transports/_base.py` already exists as the shared home, but the WebSocket pair was left duplicated — and has drifted: the server variant guards the `ready` send against a disconnect race and handles `start`/`stop` control messages; the connection variant does neither.

**Impact.** Every WebSocket protocol change must be made twice with nothing to catch divergence. The concrete cost today is an error-level traceback logged by websockets on an ordinary client-vanishes-during-handshake race in the `/ws` path, and a control-message vocabulary that silently differs between two transports the docs describe as one protocol.

**Fix.** Wrap the `ready` send at `src/easycat/transports/websocket.py:325` in the same `try/except websockets.exceptions.ConnectionClosed` guard used at :196-201 — that is a two-line fix for the noisy-traceback race. Then extract a `_WebSocketProtocolMixin` in the same file owning `_receive_loop`, `_handle_control_message`, `send_audio`, `clear_audio` and `_send_client_event`, mirroring `_TwilioProtocolMixin`'s split of shared routing versus per-class lifecycle hooks, so the `start`/`stop` handling exists once.

**Evidence**

- `src/easycat/transports/websocket.py:290` — `class WebSocketConnectionTransport(AudioQueueMixin)` (290-434) versus `class WebSocketTransport(ServerTransportBase)` (88-289). `_receive_loop` (218 vs 372), `_handle_control_message` (246 vs 402), `send_audio`, `clear_audio` (167 vs 364) and `_send_client_event` (137 vs 367) are near-verbatim copies.
- `src/easycat/transports/websocket.py:196` — `await ws.send(json.dumps({"type": "ready"}))` inside a `try` whose `except websockets.exceptions.ConnectionClosed` at :198 handles a client that vanishes between accept and ready.
- `src/easycat/transports/websocket.py:325` — `await self._ws.send(json.dumps({"type": "ready"}))` in `WebSocketConnectionTransport.connect()` — unguarded. Verified the propagation path: `Session.start()` re-raises, `SessionManager.add` (session_manager.py:52) re-raises, and `VoiceServer._handle_websocket_connection` (voice_server.py:911-916) has no `except` around `self._manager.add`, so the exception escapes the handler and websockets logs `"connection handler failed"` with a traceback (.venv/.../websockets/asyncio/server.py:378).
- `src/easycat/transports/websocket.py:402` — `if msg.get("type") == "config":` — the connection variant silently ignores `start`/`stop`/unknown control types that the server variant handles and logs at :277-284, even though docs/browser-playground.md:50-58 documents them as one protocol.
- `src/easycat/transports/twilio_media.py:439` — `class _TwilioProtocolMixin` — the identical server/connection pair problem, already solved once for Twilio with a shared mixin owning routing, handlers and accessors. The pattern exists in this codebase and was not applied to the WebSocket pair.

*Verified:* Confirmed at claimed severity. All line numbers verified exactly (classes at 88 and 290; clear_audio at 167 and 364; control handlers at 246 and 402). I traced the unguarded-`ready` claim end to end rather than assuming it: the exception really does escape to websockets' `"connection handler failed"` error log because voice_server.py:911-916 wraps `self._manager.add` in no `except`. I did not re-run the 52%-duplication difflib measurement; the duplication is obvious from reading both classes, so I dropped the specific percentage from the write-up.

---

### Security posture

*6 findings — 3 high · 1 medium · 2 low*

**Assessment.** EasyCat's security posture is genuinely strong in the places the maintainer has deliberately worked — the unified `easycat.server.auth` bind guard, the bundle loader (path-traversal rejection + SHA-256 content verification + size caps), the redaction engine with a post-write assertion on context packs, 0600/0700 journal and artifact permissions, bundled ONNX models with `torch.hub` explicitly disabled for supply-chain reasons, and a debugger with a four-layer DNS-rebinding/CSRF origin guard. There is no pickle, no eval/exec, no shell=True, no yaml.load, no `verify=False`, and no runtime model downloads anywhere in the tree. The single biggest problem is that this rigor is applied unevenly: `enforce_bind_guard` is called from exactly three modules, so the WebTransport server (a top-level public export documented under "Production multi-client servers") binds `0.0.0.0:4433` with zero authentication, zero bind guard, and zero Origin check on the CONNECT handshake — and `TwilioTransport`/`EasyConfig.phone()` do the same on `0.0.0.0:8766` with `stream_token_validator=None`, meaning every stream start is accepted. The docs claim non-loopback binds "fail closed" while listing WebTransport in the same surface table. Compounding this, the file the deployment guide names "the production starting point for phone calls" (`examples/twilio_app.py`) makes Twilio webhook signature validation optional, leaves `POST /status` forgeable, and compares its outbound-call bearer token with `!=`. Separately, `repr()` on every provider config and on `EasyConfig` prints the OpenAI/Deepgram/ElevenLabs API key in plaintext, even though the same file uses `repr=False` for the Twilio token.

**Done well here:**

- The bundle loader (`src/easycat/debug/_bundle_loader.py`) is exemplary untrusted-archive handling: `_reject_traversal` normalizes backslashes and rejects absolute/`..` members before any read (lines 83-91), every artifact ref must match `_SHA256_REF` AND its content must hash to that ref (lines 66-80), and there are independent caps on manifest size, per-member size, aggregate artifact bytes, inline artifact count, and per-record metadata. `zipfile.extractall` is never used anywhere in the repo.
- No unsafe deserialization or command-execution primitives exist in the source tree at all. A repo-wide grep for `pickle`, `yaml.load`, `eval(`, `exec(`, `os.system`, `shell=True`, `extractall`, `verify=False`, `ssl._create_unverified`, and `CERT_NONE` returns exactly one hit: a safe `ast.literal_eval` on a bundled FunASR config list (`src/easycat/vad/_funasr_runtime/online.py:226`). The hand-rolled mini-YAML parser next to it exists specifically to avoid a PyYAML dependency.
- Zero runtime model downloads. Silero, smart-turn, and FunASR ONNX models ship inside the wheel (`src/easycat/models/`), and `SileroVAD._load_torch_model` (`src/easycat/vad/silero.py:174-179`) raises with an explicit supply-chain rationale: 'torch.hub loads remote Python code without repository pinning or hash verification.' That is the right call, documented at the point of refusal.
- `src/easycat/server/auth.py` is a well-argued auth layer: constant-time comparison, blank-token normalization so `Authorization: Bearer ` cannot match (lines 175-176), and an explicit non-ASCII guard on `hmac.compare_digest` (lines 199-203) with a docstring explaining that an unguarded `TypeError` would become a 500 DoS. `enforce_bind_guard` honors the escape hatch from both the parameter and the policy field and stays fail-safe with neither set.
- The debugger's `origin_guard` (`src/easycat/debugger/server.py:540-588`) layers exact-loopback `Host` matching (blocking `localhost.attacker.example` rebinding), `Origin` validation, `Sec-Fetch-Site` checking, and a JSON-content-type + present-Origin requirement on state-changing methods. `_probe_bind` deliberately binds a concrete literal loopback address rather than the caller's string so static analysis can prove it (lines 1524-1536).
- `VoiceServer._handle_websocket_connection` (`src/easycat/server/voice_server.py:876-907`) checks draining, then auth, then capacity — auth before slot acquisition, which is the correct ordering. Every read-only metadata route (`/plan`, `/metrics`, `/manifest`, `/capabilities`) goes through `_authorized_readonly_request` (`src/easycat/server/routes.py:107-119`); only `/health*` is open.
- Journals and artifacts are written with restrictive permissions by construction (`src/easycat/runtime/_private_files.py:9-41`, `src/easycat/runtime/artifacts.py:252-256`), and exported bundles inherit 0600 because they are written through `tempfile.NamedTemporaryFile` then renamed (`src/easycat/debug/bundle.py:243-251`).
- Scaffolded projects get a real `.gitignore` covering `.env`, `.env.*`, `*.pem`, `*.key`, and `.easycat/` before `git init` runs, and `_template_sources` correctly picks up dotfiles via `pathlib.rglob` (verified empirically). The scaffold also strips `*.key`/`*.pem` from template sources on copy (`src/easycat/cli/scaffold/init.py:261`).
- `easycat bundles export` writes a minimized context pack and then re-reads every output file through `contains_unredacted_sensitive_text` before returning (`src/easycat/cli/debug/export.py:190-200, 275`) — a belt-and-braces check that catches redaction-policy regressions rather than trusting the policy.
- Inbound DTMF digits (the classic PIN/card-entry channel) are not journaled: `digit`/`sequence` are absent from `_JOURNAL_ATTRS`, and outbound `SendDTMFAction` digits are explicitly redacted via `_SESSION_ACTION_SENSITIVE_KEYS` (`src/easycat/session/_journal_sink.py:85-99, 224-245`).
- The `pytest11` entry point is appropriately minimal — `easycat.debug._pytest_plugin` pulls 246 modules, all stdlib plus pytest itself (no httpx, no numpy, no onnxruntime), registers one factory fixture, and performs no import-time side effects. `easycat/__init__.py` uses PEP 562 lazy exports and only installs a `NullHandler`. The blast radius of the auto-load is small and I found nothing to fault here.

#### 75. WebTransport server has no authentication mechanism, defaults to 0.0.0.0, and its serve helper skips the library's own bind guard

`HIGH` · `security` · `effort: medium`

**Problem.** `WebTransportServer` is a public export (`docs/public-api.md:214`) that terminates untrusted network audio and constructs a full EasyCat session per accepted client. Its config has no authentication field of any kind, `_handle_headers` never inspects `authorization` or `origin`, `host` defaults to `0.0.0.0`, and `serve_webtransport_config_sessions` never calls `enforce_bind_guard`. Every other network-serving surface in the library either self-guards in `connect()` (WebRTC), defaults to loopback (WebSocket transport), or is gated by `enforce_bind_guard` in the server layer.

**Impact.** Anyone who can reach the UDP port on a deployed WebTransport server gets an unauthenticated voice session billed to the operator's OpenAI/Deepgram/ElevenLabs accounts, plus whatever tools the operator's agent exposes, bounded only by `max_concurrent_sessions=64`. Blast radius is narrower than the finding originally implied — WebTransport is an optional extra requiring TLS certs and end-to-end HTTP/3 ingress, so few deployments exist — but for anyone who does deploy it there is no auth story at all, and no doc telling them to add one at the ingress.

**Fix.** Either (a) add `auth_token: str | None = None` to `WebTransportTransportConfig` (src/easycat/transports/webtransport.py:231-251), call `enforce_bind_guard(config.host, auth=...)` at the top of `serve_webtransport_config_sessions` (src/easycat/server/webtransport.py:19), and reject with `:status 401` in `_handle_headers` (webtransport.py:1001) when a token is configured and the `authorization` header does not match — routed through the ASCII-guarded comparison in `BearerTokenAuth._token_matches` (server/auth.py:188-203); or (b) if that is not worth doing for a Chrome/Edge-only transport, add an explicit paragraph to `docs/deployment/production-servers.md:166-171` stating that WebTransport has no built-in authentication and MUST sit behind an authenticating ingress, and change `examples/webtransport_server.py:56` to default `--host 127.0.0.1`.

**Evidence**

- `src/easycat/transports/webtransport.py:239` — `host: str = "0.0.0.0"` on WebTransportTransportConfig (port 4433 on line 240). Verified: the dataclass (lines 231-251) has no auth_token, token, or allowed_origins field. A repo grep of `auth|token|origin` over the whole 1577-line file returns exactly one hit — the word "original" in a comment at line 558.
- `src/easycat/transports/webtransport.py:1001` — `_handle_headers` decodes only `:method`, `:protocol`, `:path` (lines 1004-1006), then checks path (1013), END_STREAM (1018), duplicate session (1032), and the concurrency cap (1039), and sends `:status 200` at line 1054. Verified: no `authorization` header and no `origin` header is read anywhere in the handler.
- `src/easycat/server/webtransport.py:36` — Verified full file: `serve_webtransport_config_sessions` builds `WebTransportServer(config, handle_connection)` and calls `await server.start()`. No enforce_bind_guard call, no credential check. `handle_connection` (line 32) calls `create_session(config_factory(transport))` per accepted client — a full STT/TTS/LLM session per anonymous peer.
- `src/easycat/server/auth.py:233` — `enforce_bind_guard` raises ValueError for a non-loopback host with no token-bearing AuthPolicy. Verified by repo-wide grep that its only production call sites are voice_server.py:187, webrtc_routes.py:524, websocket.py:62. WebTransport is not among them.
- `src/easycat/transports/webrtc.py:236` — The comparable transport self-guards inside `connect()`: `if not is_loopback_host(self._config.host) and auth_token is None: raise ValueError(...)`. `WebSocketTransportConfig.host` also defaults to `127.0.0.1` (transports/websocket.py:60). WebTransport is the outlier.
- `docs/deployment/production-servers.md:166` — The `## WebTransport servers` section (lines 166-171) covers certificates, HTTP/3, QUIC, and load balancers and never mentions authentication, in contrast to line 49 for WebSocket: "Set `EASYCAT_WS_TOKEN` before exposing the server beyond loopback."
- `examples/webtransport_server.py:56` — `parser.add_argument("--host", default="0.0.0.0")` — the shipped example inherits the all-interfaces default.

*Verified:* Opened every cited line. All code claims hold: host default, absent auth fields, header handler contents, missing enforce_bind_guard call. CORRECTIONS: (1) the original claim that "the docs actively assert the opposite guarantee" is wrong — docs/using-easycat/09-multi-caller/README.md:101-105 says "Both WebSocket and WebRTC serve paths apply the same bind guard", which is accurate and correctly scoped; the surface table at line 213 merely lists WebTransport as a transport option and asserts nothing about auth. I dropped that evidence item. (2) The Origin/CORS argument is directionally right (WebTransport has no CORS preflight) but weaker in practice than stated, since a browser needs a publicly-trusted cert or a pinned `serverCertificateHashes` to connect at all; I removed it from the impact. (3) Added the missing counter-evidence that `WebSocketTransportConfig.host` already defaults to loopback, which strengthens the "outlier" framing. Held at high: it is a genuine unauthenticated network surface with no mitigation anywhere in the repo, but I widened the recommendation to accept a docs-only fix since the transport is niche.

#### 76. examples/twilio_app.py — the file the deployment guide calls the "production starting point" — makes Twilio signature validation optional, leaving POST /twiml a public stream-token minter and POST /status forgeable

`HIGH` · `security` · `effort: small`

**Problem.** The library helper and the scaffold template both require a Twilio auth token before minting media-stream tokens; `examples/twilio_app.py` silently degrades to zero signature validation when `TWILIO_AUTH_TOKEN` is unset, and the deployment guide points operators at that example as the production starting point. `POST /status`, which mutates live call state via the session event bus, is protected by nothing else.

**Impact.** An operator following docs/deployment/production-servers.md:175 deploys a public `POST /twiml` that issues a valid media-stream token to any caller, defeating the stream-token gate on port 8766 and yielding free voice sessions on their provider accounts with attacker-chosen `From`/`To`/`CallerName` parameters that flow into `session.call_identity`. Independently, an unauthenticated `POST /status` with a guessed or observed CallSid can terminate a live call. The mitigating factor is that the two authoritative paths (`easycat.telephony.server.serve_twilio_voice_app` and the `twilio-phone` scaffold) are already hardened, so this bites only operators who follow the deployment doc's pointer to the example.

**Fix.** Two changes, either of which closes it. (1) Minimal: in `examples/twilio_app.py`, replace `settings.auth_token or None` at line 121 with a token obtained via `require_env("TWILIO_AUTH_TOKEN")` at `create_app` entry (matching src/easycat/cli/scaffold/templates/twilio-phone/server.py:32), swap line 176 for `hmac.compare_digest`, and move `TWILIO_AUTH_TOKEN` out of the "optional" list in examples/README.md:109. (2) Better: change docs/deployment/production-servers.md:175 to point at `easycat.telephony.server.serve_twilio_voice_app` (which already enforces all of this) and demote the example to what examples/README.md:109 already calls it — a "lower-level reference". Note tests/examples/test_deploy_and_browser_docs.py asserts on these docs, so update it alongside.

**Evidence**

- `docs/deployment/production-servers.md:175` — Verified verbatim: "Use `examples/twilio_app.py:create_app` as the production starting point for phone calls." The checklist at line 183 lists "Validate Twilio webhook signatures" as a manual operator to-do, not something the referenced template does.
- `examples/twilio_app.py:121` — `auth_token=settings.auth_token or None` inside `twilio_form`. `settings.auth_token` is `TWILIO_AUTH_TOKEN` read via `twilio_app_settings_from_env` (telephony/twilio_app.py:97), which does not require it and does not warn when it is absent.
- `src/easycat/telephony/twiml.py:180` — `if auth_token and not validate_twilio_webhook_signature(...)` — verified: with `auth_token=None` the X-Twilio-Signature check is skipped entirely and the form is returned unvalidated.
- `examples/twilio_app.py:146` — `stream_token=stream_tokens.issue()` — every accepted `POST /twiml` mints a media-stream token. The media WebSocket IS token-gated (line 72 wires `stream_token_validator=stream_tokens.consume`), so the hole is not an open socket: it is that an unauthenticated /twiml hands out valid tokens on demand.
- `examples/twilio_app.py:150` — `POST /status` parses through the same optionally-validating `twilio_form`, then calls `emit_call_status(form, session.event_bus)` for the session looked up by `CallSid` (lines 152-158). With no auth token configured, anyone who learns a CallSid can inject CallEnded/CallFailed/voicemail events into a live call's bus.
- `examples/twilio_app.py:176` — `if request.headers.get("authorization") != f"Bearer {settings.call_api_token}"` — non-constant-time comparison on the outbound-calling endpoint. Note this path is only reachable when `outbound_calling_enabled` is true (telephony/twilio_app.py:42), which itself requires `auth_token`, so it is the least severe part of this finding.
- `src/easycat/telephony/server.py:153` — Verified: the library's own `serve_twilio_voice_app` raises ValueError unless `twilio_auth_token` is set or `unsafe_allow_unsigned_webhooks=True`, with a docstring (lines 140-145) explaining exactly this attack: "an unauthenticated public listener would let anyone obtain a token the media WebSocket accepts." The example was never brought in line.
- `src/easycat/cli/scaffold/templates/twilio-phone/server.py:32` — `twilio_auth_token = require_env("TWILIO_AUTH_TOKEN")` — the scaffold template already does the right thing, so the example is the only laggard.

*Verified:* Read examples/twilio_app.py in full and confirmed every cited line, plus twiml.py:180 and telephony/server.py:153. CORRECTIONS to the original: (1) it implied the media WebSocket is unauthenticated — it is not; line 72 wires `stream_token_validator=stream_tokens.consume`, so the actual defect is that /twiml mints those tokens for anyone. I rewrote the impact accordingly. (2) The `!=` comparison at line 176 is unreachable unless TWILIO_AUTH_TOKEN is already set (telephony/twilio_app.py:42 gates outbound_calling_enabled on it) and requires a network-timing oracle against a FastAPI handler — real but minor; I demoted it to a secondary point. (3) examples/README.md:109 already describes the file as a "lower-level ... reference" and points at `easycat.telephony.server`, which partially mitigates; the deployment doc is the one making the wrong recommendation. Held at high because the doc pointer plus the silent no-validation default is a live path to an open token minter on real phone infrastructure.

#### 78. TwilioTransportConfig defaults to 0.0.0.0:8766 with stream-token validation off, and EasyConfig.phone() inherits that default

`HIGH` · `security` · `effort: small`

**Problem.** Transport configs in this package diverge on safe defaults. WebRTC and WebSocket default to loopback and WebRTC self-enforces a token for non-loopback binds; `TwilioTransportConfig` defaults to all-interfaces with token validation disabled, `ServerTransportBase.connect` applies no guard, and both `EasyConfig.phone()` and the manifest `phone` preset inherit that default. The library already knows this is dangerous — `telephony/server.py:153` refuses to serve TwiML without an auth token for exactly this reason — but the transport-level default undoes it for anyone not using that helper.

**Impact.** `create_session(EasyConfig.phone(agent=...))` followed by `session.start()` opens port 8766 on every interface and accepts any WebSocket client as a Twilio media stream. The attacker controls `start.customParameters`, which `_parse_twilio_start_identity` (twilio_media.py:195) converts into `CallIdentity.caller_number` / `display_name` / `custom_fields` that flow into `session.call_identity` and typically into agent context — so this is attacker-chosen caller identity, not just unmetered provider spend. Nothing caps concurrent sessions on the standalone transport path either; the `max_sessions` semaphore lives in telephony/server.py:180, not in the transport.

**Fix.** Mirror webrtc.py:235-238 in the Twilio transport: change `TwilioTransportConfig.host` (twilio_media.py:142) to `"127.0.0.1"`, and in `TwilioTransport.connect` raise unless the host is loopback, a `stream_token_validator` is set, or an explicit `unsafe_allow_no_auth=True` is passed. Putting the guard in `ServerTransportBase.connect` (transports/_base.py:378) instead would cover WebSocketTransport too at the cost of touching more call sites. Separately, make `project/manifest.py:169` raise or warn when a `phone` spec omits `token`. Both `telephony/server.py:192` and the scaffold template already pass a validator, so the change should not break the maintained paths.

**Evidence**

- `src/easycat/transports/twilio_media.py:142` — Verified: `host: str = "0.0.0.0"` (142), `port: int = 8766` (143), `stream_token_validator: Callable[[str], bool] | None = None` (146).
- `src/easycat/transports/twilio_media.py:227` — `_twilio_stream_token_valid`: `validator = config.stream_token_validator` then `if validator is None: return True`. With the default config every Twilio `start` frame is accepted with no credential.
- `src/easycat/config/easy.py:728` — `kwargs.setdefault("transport", TwilioTransportConfig())` in `EasyConfig.phone()`. Verified via config/_factory.py:122 that a bare `TwilioTransportConfig` is materialized into a `TwilioTransport` — a `ServerTransportBase` subclass (twilio_media.py:720) that opens a listening socket.
- `src/easycat/transports/_base.py:385` — `ServerTransportBase.connect` calls `websockets.serve(self._handle_connection, self._host, self._port, compression=None)` with no bind guard and no auth hook. Verified there is no guard in `TwilioTransport.__init__` (twilio_media.py:755-782) either — it only forwards host/port to super().
- `src/easycat/transports/webrtc.py:236` — The counterexample: WebRTC raises in `connect()` when the host is non-loopback and `auth_token` is None, and `WebRTCTransportConfig` defaults to `127.0.0.1`. `WebSocketTransportConfig.host` also defaults to `127.0.0.1` (transports/websocket.py:60). Twilio is the only transport config in the package that defaults to all-interfaces AND ships with its credential check disabled.
- `src/easycat/project/manifest.py:169` — `if spec.token is not None:` wires `stream_token_validator`; otherwise line 176 falls through to bare `EasyConfig.phone(**kwargs)`. A manifest phone deployment that omits `token` is silently unauthenticated with no warning.

*Verified:* Opened every cited line and confirmed all of them, including that `TwilioTransport.__init__` adds no guard beyond forwarding host/port. CORRECTION to the original: it framed `_base.py:385` as the shared root cause for both TwilioTransport and WebSocketTransport, implying both are exposed — but `WebSocketTransportConfig.host` already defaults to `127.0.0.1` (transports/websocket.py:60), so Twilio is the sole outlier among transport configs. That actually sharpens the finding rather than weakening it. Held at high: unlike the WebTransport case this is the default of a documented preset (`EasyConfig.phone`) reachable with two lines of user code, and the attacker-controlled CallIdentity path is a real second-order effect I traced to twilio_media.py:195-215.

#### 77. repr() of EasyConfig and every provider config prints the API key in plaintext, while the Twilio token in the same dataclass is repr-protected

`MEDIUM` · `security` · `effort: small`

**Problem.** Provider API keys live in plain dataclass string fields with default repr behavior, while the Twilio credentials in the same file are explicitly `repr=False`. The journal/bundle export path is protected by `safe_config_snapshot`, so the codebase's own redaction tests all pass and the gap is invisible from inside the repo — it only escapes through the user's own tooling.

**Impact.** Any sink that stringifies a config object writes the live key: `print(config)` while debugging, `pytest --showlocals`, a Sentry/Rollbar frame-locals capture, or a config embedded in an exception message. The exposure is not automatic — a bare Python traceback does not print locals — so this is bounded rather than certain, but the fix is one line per dataclass and the maintainer already applied it to the adjacent Twilio fields. There is no test anywhere in tests/ asserting that a config repr omits secrets, so a regression would not be caught either.

**Fix.** Change the nine provider dataclass fields listed above plus `EasyConfig.openai_api_key` (config/easy.py:506) and `_AgentSessionConfig.remote_agent_api_key` (config/easy.py:460) to `field(default=..., repr=False)`. Then add a parametrized test in tests/config/ that walks the `ProviderCatalog` spec list from `_provider_catalog.py`, constructs each config with a sentinel key, and asserts the sentinel does not appear in `repr()` — that covers future providers automatically. Reusing `_is_secret_name` from runtime/safe_defaults.py as the field-name oracle keeps the two redaction policies in sync.

**Evidence**

- `src/easycat/config/easy.py:506` — `openai_api_key: str | None = None` — no `field(repr=False)`. Reproduced live with the project venv: `repr(EasyConfig(openai_api_key='sk-SUPERSECRET123456789'))` emits `openai_api_key='sk-SUPERSECRET123456789'` AND `stt=OpenAIRealtimeSTTConfig(api_key='sk-SUPERSECRET123456789', ...)` because __post_init__ propagates the key into the auto-wired provider configs.
- `src/easycat/config/easy.py:402` — `twilio_auth_token: str = field(default="", repr=False)` (and `twilio_account_sid` at 401) — 104 lines above the unprotected `openai_api_key`, proving the pattern is known and deliberate here. Same at telephony/session_actions.py:31.
- `src/easycat/config/easy.py:460` — `remote_agent_api_key: str | None = None` on `_AgentSessionConfig` — also unprotected, and inherited by both `EasyConfig` and `TextSessionConfig`.
- `src/easycat/stt/openai_provider.py:48` — `api_key: str = ""`. Repo grep finds 25 `api_key: str` declarations; the dataclass fields among them (stt/openai_provider.py:48, stt/openai_realtime_provider.py:66, stt/deepgram_provider.py:37, stt/elevenlabs_provider.py:52, stt/cartesia_provider.py:34, tts/openai_tts.py:70, tts/deepgram_tts.py:35, tts/elevenlabs_tts.py:55, tts/cartesia_tts.py:43) carry no repr=False, even though sibling fields in the same dataclasses do (e.g. stt/deepgram_provider.py:66 `ws_connect: Any = field(default=None, repr=False)`).
- `src/easycat/runtime/safe_defaults.py:179` — `_SafeRenderer._render_dataclass_field` (line 176) does `if _is_secret_name(name): return f"{rendered_name}='***'"` — verified. But that renderer is reached only from `safe_config_snapshot` (safe_defaults.py:295), used by debug/export.py:111 and debugger/_sources.py:360. Nothing touches the plain `__repr__`.

*Verified:* Reproduced the leak live with ./.venv/bin/python: the sentinel key appears three times in `repr(EasyConfig(...))`. Confirmed repr=False exists on twilio_auth_token/twilio_account_sid and on non-secret Any fields in the same provider dataclasses, so the omission is inconsistency rather than an unknown pattern. Confirmed safe_config_snapshot's redaction never touches __repr__. Confirmed by grep that tests/ contains no repr-vs-secret assertion. DOWNGRADED high -> medium: exposure requires a tool that stringifies the config (Sentry frame locals, --showlocals, explicit print) — CPython tracebacks do not print locals by default — so the original framing of "the single most likely real-world credential leak" is unsupported. Corrected the safe_defaults line reference from 168 (which is `parts = [...]` in _render_dataclass) to 176/179, the actual redaction branch.

#### 79. supervisor_message_authorized raises TypeError on a non-ASCII token instead of returning False, bypassing the intended 4401 close

`LOW` · `correctness` · `effort: small`

**Problem.** The supervisor listen-in WebSocket streams live caller and assistant audio. Its token check calls `hmac.compare_digest` on a raw attacker-supplied string without the ASCII guard the library's own auth module applies and documents as necessary, so a single non-ASCII byte in the `token` field turns a clean 4401 rejection into an unhandled exception in the handler task.

**Impact.** Bounded. A peer that can already reach the supervisor socket can force a traceback into the operator's logs on each attempt, and the documented 4401 close contract is broken (the client sees an abnormal 1011). The more realistic trigger is operator misconfiguration: a supervisor token containing an accented character makes every legitimate connection crash with a confusing 500-equivalent instead of a clear auth denial. Not a bypass — a non-ASCII token can never equal an ASCII one — and no amplification beyond one exception per connection.

**Fix.** Extract the guarded comparison from `BearerTokenAuth._token_matches` (src/easycat/server/auth.py:188-203) into a shared helper in `src/easycat/_net.py`, which already owns `normalize_auth_token` as the leaf module for this concern, and call it from `supervisor_message_authorized` (src/easycat/supervisor.py:87) and `BearerTokenAuth`. Add a test next to tests/session/test_supervisor.py:299-310, which already covers the wrong-token and non-str-token cases but not the non-ASCII one.

**Evidence**

- `src/easycat/supervisor.py:86` — `supplied_token = message.get("token")` then `return isinstance(supplied_token, str) and hmac.compare_digest(supplied_token, expected_token)` (87-90). `message` comes from `_decode_supervisor_subscribe` (supervisor.py:206), i.e. straight from attacker-supplied JSON. Reproduced live: `supervisor_message_authorized({'token':'café'}, 'secret')` raises `TypeError: comparing strings with non-ASCII characters is not supported`.
- `src/easycat/server/auth.py:199` — `BearerTokenAuth._token_matches` guards exactly this: `credential.isascii() and self.token.isascii() and compare_digest(...)`, with a docstring at 189-198 spelling out the consequence of not doing so.
- `src/easycat/supervisor.py:181` — `if not supervisor_message_authorized(message, expected_token, allow_unauthenticated=...)` — no try/except, so the TypeError escapes the handler coroutine before the 4401 close at line 185 is sent; the websockets server logs a traceback and closes 1011 instead.
- `src/easycat/transports/twilio_media.py:112` — By contrast `TwilioStreamTokenStore.consume` wraps its compare_digest in `try/except TypeError: return False` (110-114). The supervisor is the only one of the library's constant-time comparisons with no guard at all.

*Verified:* Reproduced the TypeError live. Confirmed the missing try/except at the call site and the existing guard in auth.py. DOWNGRADED medium -> low: no auth bypass, no amplification, and the endpoint is opt-in (examples/ws_supervisor_server.py is the only caller). REJECTED two items from the original recommendation: `project/manifest.py:174`'s bare `compare_digest` is already safe because every `stream_token_validator` call is wrapped in `try/except Exception` at transports/twilio_media.py:236-239, and `TwilioStreamTokenStore.consume` already has its own `except TypeError` at twilio_media.py:113. So the "three call sites, three levels of hardening" framing is wrong — two of three are protected, and only the supervisor needs fixing.

#### 80. telephony/compliance.py logs raw E.164 phone numbers at WARNING on its most-executed path

`LOW` · `security` · `effort: small`

**Problem.** The three raw-number log lines in compliance.py sit on the module's default execution path: with only 12 area codes mapped, `lookup_timezone` returns None for almost every US number, so a real outbound campaign writes every recipient's E.164 number into the application log at WARNING level. The module's own docstring (lines 3-9) already discloses the coverage limitation, so the fail-closed behavior itself is documented and intentional — the logging is the part nobody signed off on.

**Impact.** An operator wiring `check_calling_hours` as the `compliance_check` gate on `OutboundCallManager` (the documented integration at telephony/outbound.py:294-307) gets one WARNING per attempted call containing the callee's phone number, in a module whose entire premise is regulatory care. That is a GDPR/CCPA-relevant PII sink in logs that are typically shipped to a third-party aggregator. Secondary and less severe: the near-total blocking behavior pushes operators toward passing a blanket `timezone_override` to make it stop, which defeats the check.

**Fix.** Log the area code instead of the full number at compliance.py:148, 185, and 196 (e.g. `"Cannot determine timezone for area code %s, blocking call", _extract_area_code(phone)`), or route the value through `easycat.validation.redaction.redact_text`. Do NOT follow the original suggestion of deriving timezones from the existing dependency: the project depends on `phonenumberslite` (pyproject.toml:74,125), which ships without the geocoder/carrier/timezone metadata, so `phonenumbers.timezone` is unavailable and `_AREA_CODE_TZ` cannot be replaced without upgrading to full `phonenumbers`. If the table stays, consider making `timezone_override` or `current_hour` a required argument to `check_calling_hours` so the failure mode is a wiring-time TypeError rather than silent blanket blocking in production.

**Evidence**

- `src/easycat/telephony/compliance.py:185` — `logger.warning("Cannot determine timezone for %s, blocking call", phone)` — raw E.164 number into the application log.
- `src/easycat/telephony/compliance.py:196` — `logger.warning("Invalid or unknown timezone %r for %s, blocking call", tz_name, phone)` — same.
- `src/easycat/telephony/compliance.py:148` — A third site the original finding missed: `lookup_timezone` logs `"Area code %s not in timezone mapping for %s ...", area_code, phone`, also at WARNING. This one fires first for any number whose area code is outside the table, so it is the highest-volume of the three.
- `src/easycat/telephony/compliance.py:34` — `_AREA_CODE_TZ` holds exactly 12 entries (lines 34-47) out of ~350 assigned NANP area codes, so lines 148 and 185 are the default path for almost every real US number, not an edge case.
- `src/easycat/validation/redaction.py:93` — `_PHONE_RE` -> `REDACTED_PHONE` exists and is applied to journal/report text, but `redact_text` is never wired into any `logging` call in the package.

*Verified:* Confirmed all three log lines, the 12-entry table, the fail-closed branch, and the existence of `_PHONE_RE`. DOWNGRADED medium -> low and recategorized from over-engineering to a concrete logging defect, because two of the original supporting claims do not hold: (1) the module docstring at compliance.py:3-9 already states the coverage limitation and tells callers to pass `timezone_override`, so "shipping under a regulatory name invites reliance the docstring disclaims" is largely self-answered; (2) the "well-built redactor sitting unused two packages away" framing is misleading — `redact_text` is a journal/report redactor, and the codebase logs identifiers elsewhere too (telephony/voicemail.py:477 logs `transfer_number` at INFO, session/actions.py:416 logs a DNC number at INFO), so this is a package-wide logging-policy gap rather than a compliance.py-specific sin. I also corrected the recommendation, which was factually wrong about phonenumberslite's capabilities, and added the third log site at line 148 that the original missed.

---

### Test suite quality and cost

*9 findings — 8 medium · 1 low*

**Assessment.** The behavioral core of this suite is genuinely good: session/pipeline tests drive a real Session, real TurnManager and real AudioRouter with fakes only at the provider boundary, the hard cases the brief asked about (reconnect, concurrent sessions, ring-buffer overflow, mid-stream provider failure, RSS growth under 50 turns) all have real tests, and the autouse asyncio-task-leak detector plus import-weight subprocess guards are unusually high value per line. The suite also runs green in 592s serial / 151s parallel with zero flakes across a full run and zero tests in flaky quarantine. The single biggest problem is that a large, load-bearing-looking fraction of the investment verifies nothing about the product: tests/contracts + src/easycat/testing/contracts.py (2.7k LOC) runs the \"provider contract\" suites exclusively against fakes defined in the same test files; tests/teaching (9k LOC, 13% of runtime) validates 11.5k LOC of tutorial code that ships nothing; ~5.7k LOC across 274 tests asserts on literal Markdown prose; and src/easycat/validation (5k LOC, shipped in the wheel with hard-coded test node IDs) is backed by another 4.6k LOC of tests. Compounding this, the two barge-in tests named after the framework's hardest behavior are structurally incapable of failing. Cutting the suite in half is easy and would improve confidence, not reduce it: delete the contract fake-loop, move tests/teaching to the docs workflow, drop the prose assertions and the benchmark-arithmetic tests, and relocate the validation subsystem out of src — then spend a tenth of the recovered effort on real provider cassettes and a one-line CI change that makes the existing agent-SDK importorskip gates actually fire.

**Done well here:**

- Pipeline tests exercise real wiring, not mock theater: tests/session/test_session_streaming_behavior.py:62-97 constructs a real Session with a real SessionConfig and only substitutes providers at the Protocol boundary, then asserts on the real event bus. Noop stubs appear in only 7 files (39 references) — the suite is not asserting against stubs.
- tests/conftest.py:104-140 `fail_on_leaked_asyncio_tasks` is an autouse fixture that fails any async test leaving new pending tasks on the loop, with an `allow_task_leak` escape hatch. For an async-first framework with cooperative cancellation this catches a bug class most codebases ship to production.
- tests/conftest.py:83-102 `_restore_easycat_logger_state` documents and fixes a real cross-test-pollution bug (enable_console_logging flipping `propagate=False` and blinding caplog for every later test in a serial run). The docstring shows the failure was diagnosed properly rather than papered over with test reordering.
- tests/planning/test_boundary.py:13-42 and tests/project/test_boundary.py run import-weight assertions in *fresh subprocesses* so leftover sys.modules state cannot mask a regression — high value per line, and they protect real cold-start latency for a library with ~25 extras.
- Marker discipline is enforced at collection time: tests/conftest.py:142-158 raises `pytest.UsageError` on bad provider/surface/flaky marker metadata via tests/_marker_lint.py, and tests/conftest.py:160-170 auto-applies the `guard` marker from a maintained directory/file list. This is what makes the lane/slice system trustworthy rather than decorative.
- The hard concurrency behaviors are actually tested, not just claimed: tests/websocket/test_reconnecting_ws.py has 12 reconnect tests including reconnect-budget exhaustion and send-waits-for-in-progress-reconnect; tests/core/test_bounded_queue.py:96-131 covers drop-oldest/drop-newest/flush-resets-counter; tests/e2e/test_plan_2_sustained_stress.py:224 covers concurrent-session journal isolation; tests/integration/test_failure_paths.py covers 10 distinct stage-failure paths.
- Property-based tests are placed where they earn their keep, not sprinkled: tests/audio/test_audio_utils_property.py asserts same-rate identity, int16 alignment and never-crash-on-odd-trailing-byte for the resampler, and tests/session/test_interruption_property.py:33-58 asserts the byte→text estimator never invents characters — the exact invariant whose violation would corrupt agent history on barge-in.
- tests/_hypothesis_profiles.py:55-68 falls back to the `dev` profile with a warning when HYPOTHESIS_PROFILE is unrecognized, specifically so a typo cannot abort collection of the entire suite. That is the kind of failure-mode reasoning that usually only appears after an outage.
- Mutation testing exists at all: pyproject.toml:455-469 configures mutmut over four focused pure-logic modules with a curated fast test selection, plus an `also_copy` list so the mutants tree does not miscount import errors. Rare in a pre-1.0 project and correctly scoped.
- Fixtures are lean and non-magical: 553 LOC of conftest across the entire 482-file tree, with no heavyweight autouse session state. The one expensive fixture (tests/e2e/conftest.py:64 `voice_fixtures`, real OpenAI TTS) is session-scoped, disk-cached, and skips cleanly without credentials.
- Assertion coupling to log strings is minimal — only 11 `in caplog.text` assertions repo-wide — so the common refactoring tax of log-message-shaped tests is essentially absent here.

#### 81. The provider contract tier is a self-test: the shipped ProviderContractSuite kit is never applied to any EasyCat provider, and the CI comment claiming otherwise is false

`MEDIUM` · `test-effectiveness` · `effort: medium`

**Problem.** `src/easycat/testing/contracts.py` (436 LOC) is a genuine, well-written protocol-semantics kit exported as public API for out-of-tree provider authors. In-repo, however, every subclass of it is a fake written in the same test module — `_ContractSTT` yields PARTIAL then FINAL because the author wrote it to, and the suite then asserts PARTIAL then FINAL arrived. Not one of EasyCat's own shipped providers is ever run through the kit, even though every provider already has a socket-injection seam that would make it trivial (ElevenLabsSTTConfig.ws_connect at tests/stt/test_stt_elevenlabs.py:75, MockWebSocket in tests/stt/helpers.py). Separately, .github/workflows/nightly-validation.yml:189-192 justifies the extras-install matrix job by claiming the contract tests' importorskip gates fire with each SDK present; tests/contracts contains zero importorskip calls, so that job's only real check is scripts/extras_smoke.py.

**Impact.** Two concrete costs. (1) A free win is left on the table: the kit would catch a real class of regression — a provider that stops returning bool from commit_segment, emits a non-STTEvent, leaks a key through version_info, or breaks end_stream idempotency — for roughly 30 lines of subclassing per provider. Today those invariants hold only for fakes. (2) The false CI comment misleads the next maintainer into believing the extras matrix exercises SDK-gated tests, which makes the genuinely broken gate in the agent-bridge finding harder to notice.

**Fix.** Do NOT delete tests/contracts or src/easycat/testing/contracts.py — the kit is shipped public API, and tests/contracts/test_provider_session_matrix.py is real coverage (it runs the actual create_stt_provider_from_config / create_tts_provider_from_config dispatch plus a Session lifecycle over every registered STT x TTS pair). Instead: (a) in tests/contracts/test_stt_provider_contracts.py, add subclasses whose `provider_factory` builds the real ElevenLabsSTT / OpenAIRealtimeSTT / DeepgramSTT with the existing mock-socket seams (`ws_connect=`, tests/stt/helpers.py MockWebSocket) instead of only `_ContractSTT`; same for TTS. (b) Fix or delete the false comment at .github/workflows/nightly-validation.yml:189-192, and either point that step at a tree that actually has importorskip gates (see the agent-bridge finding) or state plainly that scripts/extras_smoke.py is the whole check.

**Evidence**

- `tests/contracts/test_stt_provider_contracts.py:70` — VERIFIED: `provider_factory = _ContractSTT`, the fake defined at line 16 of the same file. Same shape in test_tts_provider_contracts.py:49, test_vad_provider_contracts.py:52.
- `tests/contracts/test_stt_provider_contracts.py:72` — VERIFIED tautology: `test_fake_observes_lifecycle_calls_and_payloads` drives the fake and asserts the fake's own counters (`provider.started == 1`, etc.).
- `src/easycat/testing/contracts.py:188` — VERIFIED 436 LOC. Repo-wide grep for `ProviderContractSuite` subclasses returns only tests/contracts/* fakes and tests/testing/test_contract_kit.py fakes. OpenAISTT/DeepgramSTT/ElevenLabsSTT/CartesiaSTT are never run through it.
- `.github/workflows/nightly-validation.yml:189` — VERIFIED FALSE COMMENT: 'the offline contract tests below, whose importorskip gates now run for real with this cell's SDK installed'. `grep -c importorskip tests/contracts/` = 0.
- `tests/contracts/provider_surface_matrix.py:50` — VERIFIED: 25 rows declare cassette_path; only 3 files exist under tests/cassettes/ (http/openai-stt.json, ws/openai-realtime-stt.json, sse/remote-responses-api.json). test_provider_surface_matrix.py:34 only enforces existence when cassette_status == 'required'.
- `src/easycat/testing/contracts.py:210` — CORRECTION CONTEXT: the suite itself is substantive and correct (protocol isinstance checks, event-type normalization, idempotent end_stream, version_info redaction). It is a shipped public API re-exported from easycat.testing for out-of-tree provider authors — not dead weight.

*Verified:* Citations all check out, but the severity and impact claims do not. Downgraded high -> medium and rewrote the impact. Three corrections: (1) 'nothing is replayed into a provider' is false — tests/contracts/test_sse_cassette_replay.py:31 feeds the SSE cassette through the real `translate_sse_event` from easycat.integrations.agents._responses_api_events. (2) 'a Deepgram field rename produces zero offline signal' is false — tests/stt/ is 10,279 LOC and drives realistic protocol JSON straight into the real providers (tests/stt/test_stt_deepgram.py:133 builds `{"type": "Results", ...}` frames; tests/stt/test_stt_openai_realtime.py:743 and tests/stt/test_stt_elevenlabs.py:403 call the real `_handle_json_message`). (3) The impact's premise is unsound regardless: a recorded cassette pins our parser against a snapshot of the old API and can no more detect an upstream schema change than a fake can — only live canaries can. Recommendation (b) 'delete ~1,800 LOC' would remove shipped public API (easycat.testing) and the genuinely valuable provider_session_matrix; replaced with the cheap real-provider subclassing win.

#### 82. Two barge-in tests assert tautologies behind a conditional that silently no-ops, and the idiom is copied 9 times in the file

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `test_session_streaming_barge_in_cancellation` starts a session, sleeps 0.1s, and cancels only `if session.cancel_token` is truthy. If the turn has not started, the cancel is skipped and the final assertion `len(deltas) < 7` passes against an empty list. `test_session_barge_in_without_tool_calls_stops_immediately` is worse: its agent can emit at most 2 deltas by construction, so `<= 2` is a tautology — the test's own comment concedes 'it may finish before cancel arrives'. Neither test ever asserts that cancellation was observed by the agent.

**Impact.** Bounded but real: these are the two tests named after barge-in in tests/session/, and neither can fail for the right reason. The sharper problem is directional — because the guard is `if session.cancel_token:`, a regression that delays turn start (a slower STT commit, an extra await in session/_wiring.py) converts these into vacuous passes rather than failures, so the suite gets quieter as the bug gets worse. Actual barge-in behaviour is covered elsewhere, so this is false confidence rather than an open hole.

**Fix.** In tests/session/test_session_streaming_barge_in.py, replace the `sleep(0.1)` + `if session.cancel_token:` idiom (lines 107, 193, 337, 367, 402, 484, 791, 846, 896) with an event-driven wait: subscribe an asyncio.Event to the first AgentDelta, `await` it, then assert `session.cancel_token is not None` unconditionally before cancelling. Change line 114 from `assert len(deltas) < 7` to a specific truncation bound plus an assertion that the agent saw the cancel (InterruptibleAgent already checks `cancel_token.is_cancelled` at line 62 — have it record that it broke). Either delete test_session_barge_in_without_tool_calls_stops_immediately (lines 381-412) or extend SlowStreamingAgent's word list at tests/session/_session_streaming_helpers.py:230 so `<= 2` becomes a real bound.

**Evidence**

- `tests/session/test_session_streaming_barge_in.py:107` — VERIFIED: `if session.cancel_token:` then `session.cancel_token.cancel()`. Session.cancel_token returns None with no active turn (src/easycat/session/_session.py:866: `return self._turn.cancel_token if self._turn else None`), so a slow turn start silently skips the cancel entirely.
- `tests/session/test_session_streaming_barge_in.py:114` — VERIFIED: `assert len(deltas) < 7` — vacuously true with 0 deltas, i.e. passes when the turn never started and no cancel ever happened.
- `tests/session/test_session_streaming_barge_in.py:412` — VERIFIED: `assert len(deltas) <= 2` in test_session_barge_in_without_tool_calls_stops_immediately.
- `tests/session/_session_streaming_helpers.py:230` — VERIFIED: SlowStreamingAgent.invoke loops `for word in ["slow ", "response"]` yielding exactly 2 text_delta events (the trailing `done` event is not a delta), so `<= 2` at line 412 is unconditionally true whether or not cancellation works.
- `tests/session/test_session_streaming_barge_in.py:68` — VERIFIED: the local `BargeInVAD` emits the same start-then-stop as FakeVAD; its own comment reads '(In a real scenario, barge-in would trigger cancel_turn)'. The test pokes the cancel token by hand instead.
- `tests/session/test_session_streaming_barge_in.py:402` — VERIFIED: `await asyncio.sleep(0.1)` is the only synchronization for 'a turn is in flight'. `grep -c 'if session.cancel_token' ` = 9, at lines 107, 193, 337, 367, 402, 484, 791, 846, 896 exactly as claimed.

*Verified:* Every line citation verified exact, including the `grep -c` of 9 occurrences and the Session.cancel_token implementation at src/easycat/session/_session.py:866. The finding is honest about the mitigation it found: tests/integration/test_session_pipeline.py:394 (test_barge_in_during_bot_speaking, real ScriptedVAD start/stop/start/stop driving an actual interruption) and tests/turns/test_turn_manager.py:630 do provide genuine barge-in coverage. Because that coverage exists, this is a test-quality defect, not a hole in barge-in verification — downgraded high -> medium on that basis.

#### 83. tests/teaching is 9,034 LOC / 311 tests / 71s guarding tutorial code, and 832 LOC of it duplicates a check the docs workflow already runs

`MEDIUM` · `over-engineering` · `effort: large`

**Problem.** docs/teaching/ is 11,507 LOC of Python across 70 files outside src/easycat, plus docs/using-easycat/ at 1,450 LOC; tests/teaching/ is 9,034 LOC / 311 tests keeping them green. I measured the run directly: 311 passed, 4 skipped in 70.69s. Roughly 2,651 LOC of that (test_feature_ladder.py 902, test_regen_teaching_chapters.py 832, test_ladder_index.py 714, test_diagrams.py 203) is prose and generated-block scanning, and 40 files spawn subprocesses. The 832-LOC regen test is straight duplication: .github/workflows/docs.yml:53 already runs the `--check` script it re-implements.

**Impact.** 71s of the ~590s serial run and 9k LOC of test surface, of which the clearest waste is the 832-LOC duplicate of an existing CI step. The prose assertions convert documentation edits into CI failures with no product defect involved — rewording docs/using-easycat/README.md breaks test_feature_ladder.py, and because that file carries no `guard` marker it fails in the fast dev loop too. The executable per-chapter probes are legitimate (docs that do not run rot), so the burden is concentrated in the prose layer, not the whole tree.

**Fix.** Three targeted cuts, in order of payoff. (1) Delete tests/teaching/test_regen_teaching_chapters.py entirely (832 LOC) — .github/workflows/docs.yml:53 already runs `scripts/regen_teaching_chapters.py --check`. (2) Delete tests/teaching/test_feature_ladder.py:63-84 (the 15 literal marketing-phrase assertions) and add test_feature_ladder.py to GUARD_FILES in tests/conftest.py:37 so the rest of it leaves the fast loop, matching how test_ladder_index.py and test_diagrams.py are already treated. (3) Collapse the 64 subprocess.run call sites into one parametrized test that imports each probe via importlib and captures stdout in-process; the per-chapter probes are worth keeping, the 64 interpreter spawns are not.

**Evidence**

- `tests/teaching/test_regen_teaching_chapters.py:83` — VERIFIED 832 LOC. .github/workflows/docs.yml:53 ALREADY runs `python scripts/regen_teaching_chapters.py --check` as its own step, so the auto-block freshness this file re-implements is gated twice.
- `.github/workflows/docs.yml:56` — VERIFIED: docs.yml also has an 'Import-check teaching scripts not covered by pytest' step — the workflow the finding proposes moving into already exists and already covers this tree.
- `tests/teaching/test_feature_ladder.py:63` — VERIFIED: test_feature_ladder_declares_complete_feature_journey asserts 15 literal phrases including 'how voice AI works' and 'what EasyCat can do and how to use it'. CORRECTION: FEATURE_LADDER is defined at line 20 as docs/using-easycat, NOT docs/teaching — this file guards a different, smaller (1,450 LOC) doc tree.
- `tests/teaching/test_ladder_index.py:67` — VERIFIED 714 LOC including a hand-rolled Markdown fence stripper (`_without_fenced_code`) so it can regex-scan chapter headings.
- `tests/teaching/test_chapter_13_matrix_probe.py:13` — VERIFIED: subprocess.run(sys.executable, docs/teaching/13-*/matrix_probe.py) + exact-dict compare on stdout. 40 files in tests/teaching use subprocess.run, 64 call sites total — each paying a full interpreter start and `import easycat`.
- `tests/conftest.py:44` — VERIFIED: GUARD_FILES lists only test_regen_teaching_chapters.py, test_ladder_index.py and test_diagrams.py. test_feature_ladder.py (902 LOC, the most prose-heavy) is not marked guard and so runs in the fast dev loop.

*Verified:* Measured directly rather than trusting the numbers: `pytest tests/teaching -q` reports 311 passed, 4 skipped in 70.69s (finding said 76s/311 — close). LOC counts confirmed exactly (9,034 test / 11,507 docs/teaching). Downgraded high -> medium: executable documentation tests are a defensible design that catches real doc rot, and the finding overstates by calling this 'the single largest maintenance-burden multiplier in the repo' without comparison. Two corrections made: test_feature_ladder.py targets docs/using-easycat (1,450 LOC), not docs/teaching, so the '9,034 LOC testing 11,507 LOC' framing conflates two trees; and subprocess call sites are 64 across 40 files, not '~50 tests'. I also strengthened the finding — it missed that docs.yml:53 already runs the exact --check the 832-LOC test duplicates, which is the single clearest deletion available.

#### 84. ~47s of the default pytest run is pure waste: 15s sleeping out a production timeout and 32s provisioning competitor-framework environments

`MEDIUM` · `developer-velocity` · `effort: small`

**Problem.** The default `uv run pytest` — what `just test` and `just check` invoke — applies no marker deselection (pyproject.toml:333), so it runs the slow/stress/integration_external tests every other lane excludes. Two items are pure waste that I measured directly: 15.03s of the run is three ElevenLabs tests each waiting out the real 5-second production `final_transcript_timeout_s`, and 32.6s is two tests provisioning pipecat and livekit environments to benchmark competitor frameworks.

**Impact.** About 47 seconds of every full local run and every CI run buys nothing — the ElevenLabs sleeps test a timeout the file already knows how to shorten in five other tests, and the competitor benchmarks are a marketing comparison, not a regression gate. For a single-maintainer repo this is the margin between running the gauntlet per change and running it once before pushing.

**Fix.** Two one-line fixes plus one scoping decision. (1) In tests/stt/test_stt_elevenlabs.py:75, add `final_transcript_timeout_s=0.05` to the ElevenLabsSTTConfig built by `_make_el_stt_realtime` — the dataclass uses default_factory so this works, and it removes 15s. (2) Add `-m "not integration_external"` to the default addopts in pyproject.toml:333 (or move test_external_framework_worker_smoke into perf/ outside testpaths) — removes 32.6s. (3) For the remaining serial cost, add `-n auto --dist loadscope` to the `test` recipe in justfile:24 WITHOUT changing its marker scope, and validate that the integration_socket tests survive parallelism before committing to it.

**Evidence**

- `tests/stt/test_stt_elevenlabs.py:75` — VERIFIED AND MEASURED: `_make_el_stt_realtime` (defined line 65) builds ElevenLabsSTTConfig without overriding final_transcript_timeout_s. `pytest tests/stt/test_stt_elevenlabs.py --durations` shows exactly three 5.01s tests (sends_audio_as_base64, connects_with_query_params, sends_stop) — 15.03s of dead sleep in a 16.07s file.
- `src/easycat/stt/elevenlabs_provider.py:73` — VERIFIED the fix is one line: `final_transcript_timeout_s: float = field(default_factory=lambda: _FINAL_TRANSCRIPT_TIMEOUT_S)` — the default is read per-instance, and other tests in the same file already override it (lines 481, 635, 658, 700, 962).
- `tests/perf/test_framework_latency_benchmark.py:254` — VERIFIED AND MEASURED: `test_external_framework_worker_smoke[pipecat]` 16.43s + `[livekit]` 15.89s = 32.6s wall clock. Neither SDK is installed in .venv, so the time is spent provisioning isolated `uv run --with pipecat-ai==1.4.0` environments.
- `pyproject.toml:333` — VERIFIED: `addopts = "--durations=15 --durations-min=1.0"` — no marker deselection at all, so the default `uv run pytest` collects slow, stress and integration_external tests that every other lane excludes.
- `justfile:30` — CORRECTION: `test-fast` is NOT the same body of tests. Measured by collection: default collects 6744 tests, the test-fast marker expression collects 6129 (615 deselected) — it drops all integration_socket, integration_live, integration_external, contract, slow, stress, flaky and guard tests.
- `tests/e2e/test_plan_2_sustained_stress.py:117` — VERIFIED markers: test_fifty_turns_single_session_scripted carries @integration_socket + @slow + @stress and is still collected by the bare default `pytest`.

*Verified:* Kept at medium but rewrote the title and problem. The headline claim 'the same tests run in 151s in parallel' is wrong and I disproved it by collection count: default collects 6744, the test-fast marker expression collects 6129 — 615 tests deselected, precisely the socket/contract/slow/stress/guard tests most likely to catch integration regressions. The finding's own numbers (6495 vs 5904 passed) already showed this; it drew the wrong conclusion from them. So the '4x tax' conflates parallelism with a smaller scope, and the recommendation to give `check:` the fast lane's marker expression would silently shrink the release gate — I replaced it with parallelism-only. I independently measured the two concrete waste items rather than trusting them: 15.03s ElevenLabs (finding said 15s, correct; helper is at line 65, config built at line 75, not line 76) and 32.6s external frameworks (finding said 39s, slightly high). I did not re-run the full suite, so the 592s figure is unverified.

#### 86. The shipped wheel contains 5k LOC of repo-CI orchestration that hard-codes this repo's test node IDs and shells out to `uv run pytest` in the user's cwd

`MEDIUM` · `over-engineering` · `effort: large`

**Problem.** src/easycat/validation is 4,996 LOC of test-lane orchestration (slice/latency/live/release runners, report models, reliability policy, redaction) that exists to drive this repository's own pytest lanes, and it ships inside the installed package along with the `easycat validate` Typer group (src/easycat/cli/validate.py). It hard-codes repo-relative test node IDs, and its default pytest resolver is literally `["uv", "run", "pytest"]` with no repo detection.

**Impact.** The coupling runs the wrong way: renaming tests/stt/test_stt_openai.py or tests/e2e/test_plan_7_latency_benchmark.py breaks a shipped module, and mypy, ruff and import-linter police 5k LOC of CI glue as product code — with 4,633 LOC of tests existing largely to keep that coupling honest. For an installed user, `easycat validate quick` runs `uv run pytest` against their cwd, which is a confusing failure mode rather than a graceful 'this command only works inside the EasyCat repo'.

**Fix.** Move src/easycat/validation/ out of the distribution into a top-level `validation/` package alongside the existing perf/ and scripts/ trees, and move the `easycat validate` Typer group out of src/easycat/cli/. Keep src/easycat/validation/redaction.py in the package (rename to src/easycat/redaction.py) — it is imported by src/easycat/integrations/agents/template.py:193 and src/easycat/testing/contracts.py and is genuinely product logic. If a full move is too large, the cheap interim fix is to make src/easycat/validation/_runner_support.py:55-58 fail with a clear 'validation lanes require the EasyCat source checkout' error when no repo tests/ tree is found, instead of silently invoking `uv run pytest` in the user's directory.

**Evidence**

- `src/easycat/validation/_runner_support.py:58` — VERIFIED, strongest evidence: `return shlex.split(raw) if raw else ["uv", "run", "pytest"]` — a shipped library module whose default behaviour is to shell out to `uv run pytest` in whatever directory the installed user happens to be in.
- `src/easycat/validation/provider_reports.py:92` — VERIFIED: `live_pytest_target="tests/stt/test_stt_openai.py::test_live_openai_stt"`, with ten more repo-relative node IDs following through line ~204.
- `src/easycat/validation/_latency_selectors.py:9` — VERIFIED: `LATENCY_TEST_FILE = "tests/e2e/test_plan_7_latency_benchmark.py"` baked into the package, joined with test function names at lines 17-19.
- `src/easycat/validation/_release_runner.py:190` — VERIFIED: `"EASYCAT_VALIDATION_TEST_PATHS": str(source_root / "tests")` — the wheel shells out to pytest against a tests/ tree the end user does not have.
- `src/easycat/validation/redaction.py:1` — MITIGATION CONFIRMED: redaction.py IS product code — imported by src/easycat/integrations/agents/template.py:193 (`from easycat.validation.redaction import redact_value`) and by the contract kit at src/easycat/testing/contracts.py. src/easycat/_observability.py:46 also registers an `easycat.validation.failures.total` counter. Any move must keep redaction.py in the package.
- `tests/validation/test_slice_runner.py:40` — VERIFIED scale: tests/validation is 1,448 LOC and tests/cli/test_validate_*.py + test_latency_*.py is 3,185 LOC = 4,633 LOC, against src/easycat/validation at 4,996 LOC.

*Verified:* All LOC figures and hard-coded paths verified exactly. Kept at medium. I strengthened the evidence: the finding missed the sharpest item, `_runner_support.py:58` defaulting to `["uv", "run", "pytest"]`, which is what actually makes the installed-wheel behaviour bad. I trimmed the impact — 5k LOC of pure-Python adds negligible wheel weight and no dependencies, so 'users get 5k LOC of dead code' overstates; the real cost is the shipped-module-to-test-path coupling. Confirmed the finding's own caveat about redaction.py and made it a hard constraint on the recommendation, adding the two additional in-package consumers (integrations/agents/template.py:193, _observability.py:46) it did not cite.

#### 87. 57 hand-rolled provider fakes, two contradictory FakeTransport contracts in one directory, and a production compat shim whose drop path no test can reach

`MEDIUM` · `maintenance-burden` · `effort: medium`

**Problem.** Fifty-seven hand-rolled provider fakes exist across tests/, including two FakeTransport classes in the same tests/session/ directory with contradictory send_audio return contracts. The Transport protocol at src/easycat/providers.py:277 specifies `-> bool` where False means the frame was silently dropped; most fakes return None. Production absorbs the drift with a `None -> True` normalization at src/easycat/stages/transport.py:95, and the consequence is measurable: `result_attr = "drop"` at line 101 is the only occurrence of that string anywhere in src/ or tests/, so the drop telemetry branch has zero coverage and no existing fake could reach it.

**Impact.** Two concrete costs. The drop path is untestable with the current fakes — a regression that stopped tagging dropped frames, or that inverted the falsy check, would not fail anything. And because the fakes duck-type, a change to a provider Protocol that should break 17 transport implementations breaks none of them, so the CLAUDE.md-stated 'Protocol over inheritance' extension mechanism has no automated blast-radius signal; adding an optional method means auditing 57 classes by hand.

**Fix.** Start with the smallest high-value step: change tests/session/_session_streaming_helpers.py:45 to `-> bool` returning True so the two FakeTransports in tests/session/ agree, then add one test with a fake returning False that asserts the easycat.stage.latency sample carries `result=drop` — that is the first coverage the branch at src/easycat/stages/transport.py:101 has ever had. Then consolidate into a single tests/_fakes.py with configurable FakeTransport/FakeSTT/FakeTTS/FakeVAD and an import-time `assert isinstance(FakeTransport(), Transport)` against the runtime_checkable protocols. Only once no fake returns None should the `delivered is None` shim at stages/transport.py:95 be deleted.

**Evidence**

- `tests/session/_session_core_helpers.py:52` — VERIFIED: `async def send_audio(self, chunk: AudioChunk) -> bool:` ... `return True`.
- `tests/session/_session_streaming_helpers.py:45` — VERIFIED: `async def send_audio(self, chunk: AudioChunk) -> None:` — the FakeTransport in the same directory returns None.
- `src/easycat/providers.py:277` — VERIFIED: `async def send_audio(self, chunk: AudioChunk) -> bool:` with docstring 'Returns True when the chunk was accepted for delivery and False when it was silently dropped'.
- `src/easycat/stages/transport.py:95` — VERIFIED: `result = True if delivered is None else bool(delivered)`, with a comment calling None returns 'Legacy implicit'. The only producers of None I found are test fakes.
- `src/easycat/stages/transport.py:101` — VERIFIED DEAD PATH: `result_attr = "drop"` is the ONLY occurrence of the string "drop" in src/ or tests/ (`grep -rn '\"drop\"'` returns exactly one hit). No test asserts an easycat.stage.latency sample tagged drop, and no fake returns False.
- `tests/session/_session_streaming_helpers.py:31` — CORRECTED COUNT (higher than claimed): 17 `class FakeTransport`, 15 `class FakeSTT`, 16 `class FakeTTS`, 9 `class FakeVAD` = 57 definitions across tests/, not 46.
- `src/easycat/stubs.py:1` — CORRECTED: NoopSTT/NoopTTS/NoopVAD/NoopTransport are referenced in only 3 test files (tests/runtime/test_logging.py, tests/telephony/test_session_telephony_hooks.py, tests/session/test_tts_scheduler.py), 33 references repo-wide including 4 src files — narrower than the finding's '7 test files'.

*Verified:* All citations verified; my counts came out higher than claimed (57 fake definitions, not 46) so I corrected upward, and lower for stub adoption (3 test files, not 7) so I corrected downward. Kept at medium. The strongest verified item is one the finding stated but under-weighted: `grep -rn '\"drop\"'` across src/ and tests/ returns exactly one hit, the assignment at stages/transport.py:101 — the branch is provably unexercised. I reordered the recommendation to put the two-line fix and the missing drop test first, since 'consolidate 57 fakes' is the kind of advice that never gets acted on.

#### 88. Session tests reach into private collaborators 354 times and assign them directly, freezing the internal decomposition CLAUDE.md documents as the architecture

`MEDIUM` · `maintenance-burden` · `effort: large`

**Problem.** Counted across test_*.py: 150 `session._turn`, 101 `session._turn_runner`, 40 `session._stt_committer`, 32 `session._audio_router`, 31 `session._turn_manager` accesses — every figure matching the original claim exactly. Tests do not merely read these; they assign them (`session._turn = TurnContext(...)`), mutate their internal dicts, toggle `session._is_running`, and call private methods on them (`_audio_router._drain_outbound_audio()`). The `_turn_runner` / `_stt_committer` / `_audio_router` / `_cancel_orchestrator` split is a Session refactor the test suite has now welded in place.

**Impact.** Renaming `_turn_runner`, merging `_stt_committer` into it, or changing when `_turn` is populated becomes a several-hundred-line test edit with zero behavioural change — precisely the cost that stops maintainers from refactoring the package CLAUDE.md calls the core orchestrator. The assignment cases are additionally unsound: tests that build a TurnContext by hand bypass session/_builder.py and session/_wiring.py entirely, so they can pass against a Session state the real construction path would never produce.

**Fix.** Do not attempt all 354 sites. Add the one seam the worst offenders need — a test-visible `Session.begin_turn(turn_id)` (or expose the existing TurnHandle protocol from src/easycat/_turn_context.py as a read/write accessor) — and rewrite the three hand-assignment sites against it: tests/session/test_session_event_bus_playback.py:174-184, tests/integration/test_playback_marks.py:179, and tests/integration/test_replay_round_trip.py:135. For new tests, drive turns through `session.start()` with scripted VAD/STT the way tests/session/test_session_streaming_behavior.py already does, and treat a new `# noqa: SLF001` in a test as a review flag rather than a convention.

**Evidence**

- `tests/session/test_turn_runner.py:1` — VERIFIED exactly: `grep -c 'session\._turn_runner|session\._turn\.'` returns 69 in this file.
- `tests/integration/test_replay_round_trip.py:135` — VERIFIED: `await session._turn_runner.run_streaming_agent("hello", token=None)` — an integration test driving a private collaborator instead of the public turn path.
- `tests/session/test_session_event_bus_playback.py:174` — VERIFIED, the worst case: `session._turn = TurnContext("turn-first", CancelToken())` at 174, then `await session._audio_router._drain_outbound_audio()` at 176 (a private method on a private object), `patch.object(session._stt_committer, "start_event_loop")` at 182, `await session._turn_runner.on_turn_started(TurnStarted())` at 183, and `session._is_running` toggled by hand at 181/184.
- `tests/integration/test_playback_marks.py:179` — VERIFIED: `session._turn.playback_mark_to_bytes["test_mark"] = 1000` — mutating a private dict on a private object.
- `tests/e2e/test_plan_2_sustained_stress.py:209` — VERIFIED: `queue_depth=session._outbound_queue.qsize(),  # noqa: SLF001 - stress telemetry`.
- `src/easycat/_turn_context.py:1` — MITIGATION AVAILABLE: the TurnHandle protocol already exists here per CLAUDE.md, so the public seam the recommendation needs is partly built already.

*Verified:* Every count in this finding reproduces exactly — I ran each grep independently and got 150/101/40/32/31 and 69, matching the claims to the digit, and all five code citations are verbatim correct. Kept at medium: this is a genuine maintenance trap with no correctness consequence today. I narrowed the recommendation to the three hand-assignment sites (which are the unsound ones, not merely coupled) because 'rewrite 354 accesses' is not actionable; I also verified that src/easycat/_turn_context.py exists and exports TurnHandle, so the proposed seam is a small addition rather than a new abstraction.

#### 89. 25 real-SDK importorskip gates exist in tests/integrations but no CI job ever installs an SDK alongside them — the one extras job points at a tree with zero gates

`MEDIUM` · `test-effectiveness` · `effort: small`

**Problem.** src/easycat/integrations is 9,868 LOC (the largest package) and tests/integrations is 16,514 LOC / 602 tests, essentially all running against duck types authored alongside the bridges (`_MockRunnable`, `_MockAIMessageChunk`, `fake_workflows_modules`). The suite does contain 25 genuine real-SDK smoke gates behind `pytest.importorskip`, which is the right design — but they never fire. I confirmed none of langchain_core, langgraph, llama_index, pydantic_ai or agents is importable in the checked-out .venv, the `dev` group at pyproject.toml:216 lists no SDKs, ci.yml syncs only `--group dev`, and the one job that does install an extra (nightly-validation.yml:182) then runs `pytest tests/contracts`, which has zero importorskip calls.

**Impact.** The maintainer wrote real-SDK compatibility gates and they are dead in every environment: locally they skip, in PR CI they skip, and in the nightly extras matrix the job that has the SDK installed runs a different directory. A langchain-core 1.x `astream_events` schema change, a langgraph `update_state` signature change, or a pydantic-ai breaking release is invisible everywhere, while the marker vocabulary (`agent_bridge`, `surface_agent`) reads as if the surface is covered. This is the cheapest real coverage available in the repo.

**Fix.** One-line fix: change .github/workflows/nightly-validation.yml:194 from `pytest tests/contracts -q -m "not integration_live"` to `pytest tests/contracts tests/integrations -q -m "not integration_live"`, so the 25 existing importorskip gates fire in whichever extras cell has that SDK installed. Confirm the extras matrix from scripts/extras_matrix.py actually emits cells for langchain/langgraph/pydantic-ai/openai-agents; if it does not, add them. Only after that lands is it worth growing tests/integrations/agents/test_factory_reusable_spec.py with one end-to-end `invoke()` per bridge against a real SDK object and a stub LLM. Do not add more mock-based tests — 16.5k LOC against 9.8k LOC of source is already past diminishing returns.

**Evidence**

- `.github/workflows/nightly-validation.yml:194` — VERIFIED, the actionable defect: the only job that installs each extra (`uv sync --locked --extra "${{ matrix.extra }}"` at line 182) runs `pytest tests/contracts -q -m "not integration_live"` — and tests/contracts contains 0 importorskip calls across all 17 files.
- `tests/integrations/agents/test_factory_reusable_spec.py:78` — VERIFIED: real-SDK gates DO exist — `pytest.importorskip("agents")` at 78, `pydantic_ai` at 83, `langchain_core.runnables` at 91 and 100, `langgraph` at 108. 25 importorskip calls total across tests/integrations, spanning test_factory.py, test_pydantic_ai_v2.py, test_facade.py and test_openai_agents_bridge_options.py.
- `pyproject.toml:216` — CORRECTION, and it makes the finding worse: the `dev` dependency group contains ONLY tooling (pytest, mypy, ruff, hypothesis, xdist...) and no agent SDKs. The finding cited line 120, which is inside the `all` optional-dependency extra (defined at line 113), not the dev group. Every CI job in ci.yml uses `uv sync --locked --group dev` with no extras, so no job anywhere installs langchain_core/langgraph/pydantic_ai/agents alongside tests/integrations.
- `tests/integrations/agents/_langchain_bridge_support.py:65` — VERIFIED: `class _MockAIMessageChunk` with docstring 'Duck-types as langchain_core.messages.AIMessageChunk.'
- `tests/integrations/agents/test_langchain_bridge_invoke.py:5` — VERIFIED: 1,341 LOC importing through the _langchain_bridge_support barrel with no real langchain import.

*Verified:* Verified and, unusually, the finding understated its case. Its evidence claimed 'the dev group does list the SDKs' at pyproject.toml:120 — that line is inside the `all` optional-dependency extra (line 113), while the actual dev group at line 216 contains only tooling. I confirmed by import that all five SDKs are absent from .venv and that ci.yml uses `uv sync --locked --group dev` with no extras in every job, so the gates are dead in all environments, not merely locally. The 25 importorskip count (finding cited only the 5 in test_factory_reusable_spec.py) and the tests/contracts = 0 count both reproduce. Kept at medium and downgraded effort from medium to small, since the buy-real-coverage step is a single-line workflow change.

#### 85. 1,016 LOC of docs-command-hint validators and tests-of-validators, plus 176 literal-prose assertions that turn doc edits into CI failures

`LOW` · `over-engineering` · `effort: medium`

**Problem.** A three-layer stack sits on the docs route registry: `easycat.cli._app._DOCS_LINKS` is the source of truth, tests/_command_hints.py (661 LOC) and tests/docs/_docs_index_helpers.py validate documents against it, and tests/docs/test_command_hint_validator.py (318 LOC) plus tests/test_command_hints.py (37 LOC) test those validators. On top of that, 176 assertions across the suite compare literal sentences and phrases against Markdown files. The value delivered — no stale `uv run easycat ...` line in a doc — is real, but it costs ~1,016 LOC of validator infrastructure plus a large literal-prose surface.

**Impact.** Bounded and mostly a maintainer-taste cost, but with one concrete edge: rewording a sentence, renaming a heading in CLAUDE.md, or reordering a template README bullet produces a red suite with no product defect involved, which discourages exactly the doc improvement the machinery exists to protect. tests/docs/test_command_hint_validator.py is explicitly GUARD_EXEMPT (tests/conftest.py:56), so 318 LOC of meta-tests run in the fast dev loop.

**Fix.** Delete tests/docs/test_command_hint_validator.py (318 LOC) and tests/test_command_hints.py (37 LOC) — validators whose only consumer is other tests do not need their own test tier; a bug in them shows up as a false failure in the guard tests they feed. Keep tests/_command_hints.py and reduce tests/docs/test_route_contracts.py:66 from a heading-split-plus-literal-comparison to a single parametrized check that every command string in `_DOCS_LINKS` is a valid CLI invocation, dropping the CLAUDE.md `## Commands` / `## Architecture` structural coupling. Delete the assertion at tests/cli/test_json_schema.py:359 that compares the CLI envelope to its own in-process source.

**Evidence**

- `tests/_command_hints.py:1` — VERIFIED 661 LOC of Markdown command extraction/validation helpers.
- `tests/docs/test_command_hint_validator.py:12` — VERIFIED 318 LOC testing `_cli_docs_command_hint_problems` — tests of the validator that validates the docs. Also listed in tests/conftest.py:56 GUARD_EXEMPT, so it deliberately runs in the fast dev loop.
- `tests/test_command_hints.py:6` — VERIFIED 37 LOC testing `documented_commands()` against a hand-written markdown literal.
- `tests/docs/test_route_contracts.py:66` — VERIFIED: `test_maintainer_guide_docs_route_matches_guide_command_hints` does `guide.split("## Commands", 1)[1].split("## Architecture", 1)[0]` — renaming either heading in CLAUDE.md breaks the test suite.
- `tests/cli/test_templates.py:723` — VERIFIED: `assert "EasyConfig.mic(agent=agent)" in readme`. I reproduced the finding's count exactly — `grep -rn 'assert "..." in (readme|guide|text|content|doc)'` over tests/ returns 176.
- `tests/cli/test_json_schema.py:359` — VERIFIED near-tautology: `assert payload["entries"] == [{**entry, "commands": list(entry.get("commands", ()))} for entry in _docs_entries()]` — the CLI output is compared to the exact in-process source the CLI reads from, so it can only fail if serialization mutates data.

*Verified:* Every cited line verified, and I reproduced the 176-literal-prose-assertion count exactly with the finding's own grep shape. Downgraded medium -> low: this is a taste and friction cost with no correctness consequence, and it overlaps substantially with the tests/teaching finding. I could not reproduce the '274 test functions / 5,746 LOC' AST measurement, so I dropped it from the problem statement and kept only what I confirmed. One correction to the recommendation: the finding proposes excluding surviving guard tests 'from the default run, not just from test-fast' — that contradicts the documented contract at pyproject.toml:356, which states guard tests are 'excluded from the fast dev loop but always run in just test/check'. Removing them from the default run would leave them ungated outside the guard-* lanes, so I dropped that suggestion.

---

### Documentation and meta-tooling

*7 findings — 3 medium · 4 low*

**Assessment.** EasyCat's documentation is unusually high-craft where it is guard-tested against source — docs/latency.md, docs/reference/easyconfig.md, and the teaching ladder's pedagogy are better than almost any comparable OSS project — but the meta-layer has outgrown the library. The docs tree is 117,677 words across 80 files, the plan tree a further 138,526 words across 62 files, prose-guard tests ~15,600 LOC (11% of a 141k-line suite), three generator scripts 1,612 LOC, and a 41-entry docs route registry occupies 63% of the shipped cli/_app.py — all for a 0.1.0 library not on PyPI with no users. The single biggest problem is that this self-consistency apparatus guards four internal representations of the docs map while leaving the fifth — mkdocs.yml, the one that renders the public site — unguarded and drifted: 27 of 80 pages are missing from the site, including the entire 25-file "EasyCat feature ladder" that the README and llms.txt both promote. Compounding it, the README's very first code block (app.run("browser")) does not run after the README's own install command, the plan tree's designated currency gate is wrong by 24–48% on its own file counts, and the whole teaching ladder is pinned to a model generation the examples tree already migrated off. What is genuinely done well: guard tests that assert values rather than prose (latency defaults, EasyConfig fields, public-API snapshot) earn their keep completely, and the plan folder's operating model with explicit status labels and stale banners is more disciplined than most commercial repos. The correction is not more documentation — it is deleting one of the two tutorial ladders, collapsing five route-map representations to two, and spending the freed effort on the one document that does not exist: why a developer should pick EasyCat over Pipecat or LiveKit Agents, backed by the benchmark harness the repo already owns.

**Done well here:**

- docs/latency.md is the best page in the repo and possibly the best latency doc in this problem domain: it enumerates every latency-adding default with its exact value, where it waits, and tuning guidance — and tests/observability/test_docs.py:140-184 parses that Markdown table and asserts each documented value against the live dataclass field, so the table provably cannot drift.
- docs/reference/easyconfig.md documents all ~35 EasyConfig fields with accurate defaults, and tests/docs/test_route_contracts.py::test_easyconfig_reference_tracks_config_fields compares each section against the live dataclass. Spot-checking every field against src/easycat/config/easy.py:459-534 found zero drift — including the non-obvious ones (debug='light' default, caller_id_exposure='tools_only', auto_align_tts_output_to_transport=True).
- The teaching ladder's pedagogical design is genuinely first-rate and not merely long: wrong-version-first chapters (3, 5, 9) that ship deliberately broken code to motivate the fix; a predict-then-run protocol with the prediction preserved as evidence; a hardware-free offline spine (docs/teaching/offline_spine.py) giving every chapter a credential-free checkpoint that strips *_API_KEY from child processes; closed-book self-checks with N/N gates and phase reviews. The staged-hint disclosure in EXERCISES.md is better instructional design than most paid courses.
- llms.txt and llms-full.txt are actually generated from a single source (scripts/regen_llms_txt.py) and guarded byte-for-byte by tests/test_llms_txt.py — verified current, no drift. Generation is the right call for these two files specifically, because hand-maintaining 41 routes × command hints across two formats would be hopeless.
- plan/operating-model.md is real governance, not theater: four document types with explicit upkeep rules, four status labels, a promotion flow, and an instruction to say what drift is known rather than silently rewriting history. plan/neo/README.md:5-16 carries an honest stale banner declaring that the runtime cost/latency-budget implementation the whole packet was built on was removed as 'undercooked and duplicative with the journal' — the kind of self-correction most repos never write down.
- .github/workflows/docs.yml goes beyond `mkdocs build --strict`: it runs regen_teaching_chapters.py --check so auto-blocks cannot silently drift, compileall's the whole teaching tree, exec-imports the three wrong-version-first scripts to catch symbol moves, and asserts specific site/ paths exist to catch a silently-broken URL-rewriting hook. The inline comments explain why each check exists.
- CONTRIBUTING.md's flaky-quarantine policy is the strongest anti-rot rule in the repo: @pytest.mark.flaky requires issue, owner, and an ISO review_by date, and a past date fails collection — quarantine debt cannot rot because the suite forces re-triage on a deadline.
- docs/architecture.md carries the architecture prose that CLAUDE.md deliberately does not duplicate, and its collaborator-by-collaborator map of session/ (_builder, _wiring, _turn_runner, _stt_committer, _tts_scheduler, _audio_router, _greeting, _caller_id, _telephony_facade) checked out accurately against the actual files — a maintainer arriving cold can navigate an 8,528-line package from this one page.

#### 90. README's headline `app.run("browser")` snippet needs the `webrtc` extra the README's own install command omits

`MEDIUM` · `correctness` · `effort: small`

**Problem.** The README's first code block calls `app.run("browser")`, which is WebRTC-backed and requires the `webrtc` extra (aiortc + aiohttp). The Install section 65 lines later prescribes only `uv sync --extra quickstart --group dev`. `examples/voice_app.py:10` and the optional-extras bullet at README.md:148 both get it right, so this is a README-local inconsistency, not a code defect.

**Impact.** A reader who copies the top snippet and follows the README's own Install command hits `ImportError: WebRTC signaling requires the aiohttp.web package. Install with: uv add 'easycat[webrtc]'. From the EasyCat repo, use: uv sync --extra webrtc --group dev.` on first run. Bounded rather than fatal — `_extras.require_module` (src/easycat/_extras.py:20-30) prints the exact fix — but it is a first-impression stumble on the single most-read block in the project.

**Fix.** In README.md, change the Install block at line 82 to `uv sync --extra quickstart --extra webrtc --group dev`, or annotate line 17 with `# needs --extra webrtc; use "local" for mic/speakers`. Then extend `tests/test_quickstart_e2e.py::test_readme_quickstart_leads_and_install_block_uses_env_convention` (line 272) — which already asserts the exact string `uv sync --extra quickstart --group dev` appears (line 284) — to assert the VoiceApp snippet's mode and the install command's extras agree.

**Evidence**

- `README.md:17` — Verified: first code block in the repo ends `app.run("browser")  # or "local", "websocket", "twilio"`.
- `README.md:82` — Verified: the Install section's four-command block starts `uv sync --extra quickstart --group dev`. No `--extra webrtc` anywhere in Install or Quickstart.
- `pyproject.toml:98` — Verified: `quickstart = [sounddevice, openai, openai-agents, pyrnnoise, requests, numpy, onnxruntime, livekit]` — no aiortc, no aiohttp. `webrtc = [aiortc, aiohttp]` is a separate extra (pyproject.toml:97).
- `src/easycat/voice_app.py:294` — Verified: `run()` resolves `mode or "browser"`; browser dispatches to `_run_browser` (line 302).
- `src/easycat/server/webrtc_routes.py:529` — Actual failure site (the audit cited voice_app.py:447 instead): `web = require_module("aiohttp.web", extra="webrtc", purpose="WebRTC signaling")` inside `run_webrtc_config_server` (defined line 583).
- `examples/voice_app.py:10` — Verified: the example documents the correct install — `uv sync --extra quickstart --extra webrtc --group dev`. README does not.
- `README.md:148` — Only nearby mention of the extra is a bullet 130 lines below the snippet: `aiortc + aiohttp (WebRTCTransport): uv sync --extra webrtc --group dev`.

*Verified:* Confirmed. Corrections made: (a) downgraded high→medium — the failure is immediate and the error message names the exact remedy, so it is friction rather than a dead end; (b) the audit's evidence line `src/easycat/voice_app.py:447` points at `_run_browser`'s import of `easycat.server.webrtc_routes`, but that module imports aiohttp lazily (see its docstring line 46) — the real raise site is webrtc_routes.py:529; (c) also confirmed the README's own 4-command flow ends on `examples/openai_agents_voice.py` (local mic), so a reader who follows Install literally never exercises browser mode — which further bounds the impact.

#### 91. mkdocs nav omits 27 of 80 docs pages, including the whole 25-file `using-easycat` ladder, with no test guarding nav coverage

`MEDIUM` · `correctness` · `effort: small`

**Problem.** 27 of 80 docs pages are absent from `mkdocs.yml`'s nav: the entire docs/using-easycat/ ladder (README + 12 chapter READMEs + 12 EXERCISES), docs/browser-playground.md, and docs/deployment/production-servers.md. Because mkdocs' omitted-files check is INFO-level and mkdocs.yml sets no `validation:` overrides, `mkdocs build --strict` in .github/workflows/docs.yml:55 passes. Nothing in tests/ reads mkdocs.yml.

**Impact.** On the published site these pages build and are reachable from the home page and search, but they get no sidebar/tab entry and no nav context — a reader browsing the Learn tab sees only the 16-chapter teaching ladder and cannot discover the feature ladder, the browser playground, or the production-servers guide by navigation. Any future docs subtree will silently repeat this, since no guard exists.

**Fix.** Add `using-easycat/*`, `browser-playground.md` and `deployment/production-servers.md` entries to the nav block in mkdocs.yml (lines 77-153). Then either set `validation: nav: omitted_files: error` in mkdocs.yml so `mkdocs build --strict` fails on the next omission, or add `tests/docs/test_route_contracts.py::test_mkdocs_nav_covers_every_docs_page` asserting `{p.relative_to('docs') for p in Path('docs').rglob('*.md')}` equals the set of .md paths parsed out of the nav block.

**Evidence**

- `mkdocs.yml:77` — Verified by script: nav (lines 77-153) names 53 .md paths; `docs/**/*.md` on disk is 80. Diff = exactly 27 missing, 0 nav entries pointing at nonexistent files.
- `mkdocs.yml:70` — Missing set = all 25 files of docs/using-easycat/, plus docs/browser-playground.md and docs/deployment/production-servers.md. No `validation:` block is configured, so mkdocs' `nav.omitted_files` stays at INFO and `--strict` (.github/workflows/docs.yml:55) cannot catch it.
- `docs/README.md:40` — Mitigation found: the site home page (README.md renders at `/` per .mkdocs/hooks.py:on_files) links all 12 using-easycat chapters, browser-playground.md (line 115) and deployment/production-servers.md (line 148). MkDocs builds every file under docs_dir regardless of nav, so these pages ARE published and searchable — they are only absent from the nav sidebar.
- `README.md:121` — Verified: README's chooser row 'Learn EasyCat feature by feature' points at docs/using-easycat/.
- `tests/docs/test_route_contracts.py:1` — Verified: `grep -rln mkdocs tests/` returns only tests/teaching/test_diagrams.py, which does not read nav. No test asserts nav coverage.

*Verified:* Confirmed with a scripted diff (exactly 27, matching the audit's count and file list). Downgraded high→medium and corrected the central overstatement: the audit claims the pages 'do not exist on the published site' and are 'invisible'. MkDocs builds every markdown file under docs_dir; nav only controls the navigation tree. I verified docs/README.md:40-49,115,148 links all of them from the site home page and .mkdocs/hooks.py rewrites those folder links to the built pages, so they are published and reachable — the defect is loss of nav discoverability plus the missing guard, not missing content. The `--strict` analysis in the audit is correct.

#### 92. `easycat docs` prints 616 lines by default and ships 91 repo-only commands — including seven raw internal pytest guard invocations compiled into the wheel

`MEDIUM` · `api-ergonomics` · `effort: medium`

**Problem.** `easycat docs` with no filter emits 616 lines (measured) covering 41 routes and 400 command hints, 91 of which cannot run outside a repo checkout because tests/, examples/, scripts/, docs/ and perf/ are not in the wheel. Seven of those are the repo's own guard test-lane invocations, compiled into shipped code by scripts/regen_guard_commands.py specifically so the CLI can print them.

**Impact.** The CLI's advertised discovery entry point dumps 616 unpaged lines on an installed user, with maintainer-only routes (CLAUDE.md, AGENTS.md, tests/contracts/README.md, plan/validation/reference.md) mixed into the same flat list and internal pytest lane strings distributed in the package. Not breaking — every route prints a working GitHub URL and a footer explains the `uv run` prefix — but it is the wrong default for the command README points new users at.

**Fix.** Make bare `easycat docs` print the audience menu plus route labels only, and require `--audience X` or a new `--verbose` to expand command hints (the audience machinery already exists — `easycat docs --audience learners` works today). Separately, drop the `_DOCS_ONBOARDING_RAW_GUARD_COMMANDS` splices at src/easycat/cli/_app.py:643, 673, 781, 829, 960 and keep only the `just guard-*` names, so scripts/regen_guard_commands.py no longer needs to emit raw pytest strings into the wheel.

**Evidence**

- `src/easycat/cli/_app.py:198` — Verified by introspection: `_DOCS_LINKS` (lines 198-989) holds 41 routes and 400 command hints; 91 match `uv run pytest|python (examples|scripts|docs|perf)`. 10 of 41 route paths are outside docs/: README.md#choose-your-path, README.md#install, README.md#cli, examples/README.md, CLAUDE.md, AGENTS.md, tests/contracts/README.md, CONTRIBUTING.md, src/easycat/runtime/DURABILITY.md, plan/validation/reference.md.
- `src/easycat/cli/_guard_commands.py:22` — Verified: a generated module inside the installed package whose `DOCS_ONBOARDING_RAW_GUARD_COMMANDS` tuple is seven verbatim `uv run pytest tests/...` strings lifted from the repo justfile.
- `src/easycat/cli/_app.py:642` — Verified splice sites: 642-643, 672-673, 781, 828-829, 959-960 inject those guard strings into route command hints.
- `src/easycat/cli/_app.py:990` — Mitigation: `_DOCS_SOURCE_URL` makes every route print a working https://github.com/yisding/easycat/blob/main/... link, so even non-shipped paths like plan/validation/reference.md resolve for an installed user.
- `src/easycat/cli/_app.py:991` — Mitigation: `_DOCS_COMMAND_NOTE` trailing footer states 'Commands already starting with uv run are repo-local and should run from the repository root.'

*Verified:* Confirmed by loading `_DOCS_LINKS` directly and by running the CLI: 616 output lines (audit said 616), 400 hints (audit said 398), 91 repo-local (exact match), 41 routes and 10 non-docs paths (exact match). Held at medium rather than promoting: I found two mitigations the audit understated — every route emits a resolvable github.com blob URL, so 'maintainer-only paths point at files not in the wheel' does not produce a broken link; and the trailing note already flags repo-local commands. Narrowed the recommendation: the audit's proposal to move CLAUDE.md/AGENTS.md/CONTRIBUTING.md out of `_DOCS_LINKS` would break `easycat docs --audience maintainers` and `--audience coding-agents`, which CONTRIBUTING.md:16-22 and README.md:103 both advertise, so I replaced it with a default-verbosity change.

#### 93. plan/roadmap/current-code-status.md, the designated currency gate for the plan tree, is wrong on three inventory claims

`LOW` · `maintenance-burden` · `effort: small`

**Problem.** The plan tree's governance routes every reader through one document whose sole job is accurate inventory. It undercounts src/easycat Python files by 24% (217 vs 287), undercounts tests by 48% (216 vs 415), and lists a LICENSE committed three weeks ago as outstanding release-bar work.

**Impact.** Bounded to contributors and coding agents working from plan/ — no user-visible or runtime effect. A contributor following plan/operating-model.md:67 concludes the codebase is a third smaller than it is and that LICENSE work remains; plan/operating-model.md:69-70 already instructs them to re-verify named files, which limits the damage.

**Fix.** Fix the LICENSE bullet at plan/roadmap/current-code-status.md:94 and update the snapshot date at line 5. Replace the hand-typed counts at lines 13-14 with a generated block (the repo already runs three regen scripts via `just`), or delete the Inventory section entirely and keep only the narrative status — the counts add nothing that `git ls-files` cannot answer on demand.

**Evidence**

- `plan/roadmap/current-code-status.md:13` — Verified: '`src/easycat/` contains 217 tracked Python files.' Actual: `git ls-files src/easycat | grep -c '\.py$'` = 287.
- `plan/roadmap/current-code-status.md:14` — Verified: '`tests/` contains 216 tracked `test_*.py` files.' Actual: `git ls-files tests | grep -c '/test_.*\.py$'` = 415.
- `plan/roadmap/current-code-status.md:94` — Verified: '- A root `LICENSE` remains active release-bar work.' A tracked BSD 2-Clause LICENSE was committed on 2026-07-03 in 6592b72a ('packaging: add BSD-2-Clause LICENSE and PEP 639 metadata').
- `plan/roadmap/current-code-status.md:5` — Verified: 'Snapshot date: 2026-06-12.' — six weeks stale as of 2026-07-26, and predates the LICENSE commit.
- `plan/README.md:13` — Verified: designates this file 'latest static code inspection snapshot used to judge which plans are still current'.
- `plan/operating-model.md:67` — Corrected citation (audit said line 78): 'At the start of a larger work session, read `plan/README.md` and `roadmap/current-code-status.md`.' Partial mitigation at lines 69-70: 'Before implementing from any old checklist, verify the named files/classes still exist with `rg` or `find`.'

*Verified:* All three factual claims confirmed independently; the LICENSE commit date (2026-07-03, after the 2026-06-12 snapshot) explains it. Downgraded medium→low: plan/ is an internal planning tree (docs/README.md:3 explicitly separates it from reader-facing docs), the blast radius is contributor confusion only, and plan/operating-model.md:69-70 already prescribes verifying stale checklists. Corrected two audit numbers: the operating-model citation is line 67, not 78; and plan/ is 129,122 words across 45 *tracked* files, not '138,526 words across 62 files' (the audit counted the untracked plan/v2/ directory).

#### 94. Teaching ladder and shipped `easycat console` pin gpt-4o-mini while examples/ was migrated to gpt-5.x

`LOW` · `maintenance-burden` · `effort: medium`

**Problem.** The 16-chapter teaching ladder and two shipped CLI entry points (`console.py:46`, `serve.py:32`) pin gpt-4o-mini, while examples/ was migrated to gpt-5.2/gpt-5.5. Chapter 5's exercise and its three generated hints are built entirely on the observable latency delta between gpt-4o-mini and gpt-4o, and chapter 6 quotes millisecond figures measured against that generation.

**Impact.** No breakage — gpt-4o-mini still resolves. The cost is internal inconsistency (a learner sees one model family in the tutorial and another in the examples for the same product) and a slow-decaying exercise whose pedagogical payoff shrinks as those names age out. None of the seven guard lanes checks semantic currency, so nothing would flag a withdrawal.

**Fix.** Update the `MODEL` constants under docs/teaching/**/main.py, plus src/easycat/cli/console.py:46 and src/easycat/cli/serve.py:32, to the family examples/ uses; re-measure the chapter 6 prose at docs/teaching/06-streaming-agent/README.md:707; and rewrite the chapter 5 exercise table at EXERCISES.md:28 around a currently-available fast/slow pair. Optionally add a check in tests/teaching/ asserting model literals in docs/teaching/**/*.py are a subset of those in examples/**/*.py.

**Evidence**

- `docs/teaching/05-blocking-agent/main.py:48` — Verified: `MODEL = "gpt-4o-mini"`. Same constant in 06-streaming-agent/main.py:54, 07-tools/main.py:58, 07-tools/blocking_tool.py:60, 08-smart-turn/main.py:60, 09-interruption/{cancel,estimate,ignore}.py:59/60/54, 10-cleaning-signal/main.py:78 and wrong_order.py:72, 12-evals-and-latency/llm_judge.py:33, 14-bring-your-own-agent/main.py:59.
- `src/easycat/cli/console.py:46` — Verified: `_LIVE_AGENT_MODEL = "gpt-4o-mini"` in shipped code behind `easycat console`. Not cited by the audit but identical: src/easycat/cli/serve.py:32 `_DEFAULT_AGENT_MODEL = "gpt-4o-mini"`.
- `docs/teaching/05-blocking-agent/EXERCISES.md:28` — Verified: exercise table row `| MODEL = "gpt-4o-mini" → "gpt-4o" | ? | ? |`, with a generated hint at line 41 asserting 'Switching to gpt-4o mostly affects the agent_ms span'.
- `docs/teaching/06-streaming-agent/README.md:707` — Verified: 'On a typical 3-sentence reply with `gpt-4o-mini`, expect first-audio to drop from ~3000 ms (blocking) to ~800-1200 ms (streaming)'.
- `examples/pydantic_ai_voice.py:27` — Verified contrast: examples/ uses `openai:gpt-5.2` (also function_tools_pydantic.py:30, session_actions_pydantic.py:39) and `gpt-5.5` (langchain_voice.py:38, langgraph_voice.py:41, function_tools_lang*.py, session_actions_lang*.py).

*Verified:* Every cited line confirmed verbatim. Downgraded medium→low: this is a consistency and freshness issue with no functional failure, no user-visible breakage and no correctness risk. Added src/easycat/cli/serve.py:32, which the audit missed and which has the same constant. Also checked that the audit did NOT wrongly flag src/easycat/stt/openai_provider.py:49 (gpt-4o-transcribe) or src/easycat/tts/openai_tts.py:71 (gpt-4o-mini-tts) — those are current OpenAI audio model names with no gpt-5 equivalent and are correctly left alone.

#### 95. Two overlapping tutorial ladders (97k words, 83 tutorial .py files, 12,957 LOC) maintained in parallel over the same product surface

`LOW` · `over-engineering` · `effort: large`

**Problem.** docs/teaching/ and docs/using-easycat/ are two full curricula over the same product, each with per-chapter EXERCISES, generated navigation blocks and dedicated guard tests. Every public-API change now has three places to update — code, ladder A, ladder B — and three test suites to re-green.

**Impact.** Pure maintenance surface, not a defect: 97k words and ~13k LOC of tutorial code kept in sync by hand and by guard tests, for a 0.1.0 library not yet on PyPI. The feature ladder is simultaneously the thinner of the two (19k vs 78k words) and the one missing from the site nav, which is weak evidence it is not carrying its weight.

**Fix.** A scope decision for the maintainer, not a fix. If consolidating: fold docs/using-easycat/ into 3-4 how-to pages under docs/ (runtime modes, providers and voices, telephony, production ops) and retire tests/teaching/test_feature_ladder.py with it. If keeping both, at minimum add it to the mkdocs nav (see the nav finding) so the investment is visible.

**Evidence**

- `docs/teaching/README.md:3` — Verified: 16-chapter ground-up ladder; 35 .md files, 77,934 words, 70 .py files.
- `docs/using-easycat/README.md:1` — Verified: second 12-chapter ladder (runtime modes, providers, conversation controls, tools, bridges, sessions, observability, evals, multi-caller, telephony, prod ops); 25 .md files, 19,426 words, 13 .py files. Combined the two are 97,360 of docs/'s 117,677 words and 12,957 LOC of tutorial Python.
- `README.md:128` — Verified: README has to explain which ladder to pick — 'Use the ground-up ladder above when you want to build the underlying voice pipeline from PCM onward; use the feature ladder when you want to build an app with EasyCat first.'
- `tests/teaching/test_feature_ladder.py:1` — Verified: 902 lines guarding the second ladder's structure alone.
- `docs/teaching/README.md:104` — Mitigation the audit missed: 'The spine reaches every chapter without credentials' — docs/teaching/offline_spine.py runs one credential-free checkpoint per chapter with every *_API_KEY stripped from child environments, so the DEEPGRAM_API_KEY requirement at README.md:107 applies to the live path only.

*Verified:* All counts re-measured and confirmed (83 .py / 12,957 LOC across both; 35 and 25 .md files; 77,934 and 19,426 words). Downgraded medium→low and softened two claims. First, the audit's '8 of 16 chapters require a second paid provider account' is materially mitigated: docs/teaching/offline_spine.py provides a documented credential-free checkpoint reaching every chapter, advertised at docs/teaching/README.md:104 and README.md:98. Second, 'pick one ladder' is a product-scope judgment I cannot validate as a defect, so I reframed the recommendation as a decision rather than a fix and stripped the prescriptive deletion.

#### 96. A committed cross-framework latency harness exists but no results and no comparative positioning are published

`LOW` · `adoption` · `effort: medium`

**Problem.** The repo has paid for the hard part of a defensible comparison — a normalized, reproducible, randomized-order cross-framework latency harness with committed dependency locks and recorded environment hashes — and publishes no output from it and makes no comparative claim anywhere reader-facing.

**Impact.** Not a defect; an unclaimed asset. A developer evaluating voice frameworks sees a tagline and a feature list at README.md:275 with no way to tell what EasyCat does that Pipecat or LiveKit Agents do not. Low severity because deferring public benchmark claims until release is a legitimate choice — benchmark numbers published against competitors carry their own maintenance and credibility cost.

**Fix.** If the maintainer wants this before release: add docs/comparison.md carrying (a) P50/P95 from `uv run python perf/bench_framework_latency.py` with docs/latency.md's own caveats verbatim, (b) a capability matrix vs Pipecat and LiveKit Agents, (c) explicit non-goals, and put a 3-5 line 'Why EasyCat' block under README.md:5. Wire the harness into a scheduled workflow so any published number carries a date and commit SHA.

**Evidence**

- `docs/latency.md:63` — Verified: 'The deterministic cross-framework harness compares EasyCat, LiveKit Agents, and Pipecat at one external boundary…' with full methodology through line 105 — randomized round order, committed uv.lock graphs, seed and SHA-256 env-lock hashes recorded in the artifact.
- `perf/bench_framework_latency.py:1` — Verified: harness is committed, with perf/framework_environments/{livekit,pipecat}/{pyproject.toml,uv.lock} resolved lock graphs (22 tracked files under perf/).
- `docs/latency.md:98` — Verified: '--require-easycat-fastest when a comparison should return a nonzero status unless EasyCat wins raw latency at both P50 and P95.'
- `perf/baseline.json:1` — Verified: the only committed benchmark artifacts (baseline.json and ws3-final.json, byte-identical prefixes) contain journal append latencies from 2026-04-10, not framework comparisons.
- `README.md:3` — Verified: positioning is the tagline only. `grep -riE '(pipecat|livekit agents|vocode)'` over README/docs/CONTRIBUTING/AGENTS/CLAUDE returns 5 hits, all in docs/latency.md's methodology plus one .dockerignore note in docs/deployment/docker.md:87.

*Verified:* Every citation confirmed, including that perf/baseline.json holds journal append latencies rather than framework results. Downgraded medium→low and removed the audit's 'highest-leverage missing document in the repo' framing: this is a product-marketing gap, not a defect, and none of the severity criteria (data loss, security, outage, wrong output, maintenance trap) apply. I also searched plan/ for a deliberate decision to withhold comparisons and found none — competitors are discussed extensively in plan/v2/ and plan/peripherals/peripheral-provider-ecosystem.md:38 — so the omission looks like unfinished work rather than policy.

---

### Packaging, dependencies, and release readiness

*10 findings — 2 high · 3 medium · 5 low*

**Assessment.** The release *infrastructure* here is genuinely above average for a pre-1.0 project — SHA-pinned Actions, zizmor/actionlint gating, Trusted Publishing over OIDC with a reviewer-gated environment, a committed lock enforced with `--locked` everywhere, a nightly extras matrix generated from pyproject, hash-pinned docs requirements, and a Dockerfile that is multi-stage, non-root, and healthchecked. The dependency *declarations* it protects are the weak half. The single biggest problem is that the version constraints are unresearched: every floor equals the version that happened to be in uv.lock when it was written, three of them (fastapi, uvicorn, python-multipart) pin libraries that appear nowhere in `src/`, one (`openai`) pins an SDK the OpenAI providers do not use, and I demonstrated the CLI running fine on rich/typer/httpx releases roughly two years below their declared floors — with no `--resolution lowest-direct` job anywhere to catch it. Compounding that, the release path those floors would ship through has never executed: zero tags, zero release.yml runs, 40/40 failed release-validation runs, an unclaimed PyPI name, no CHANGELOG. Two live defects deserve immediate attention regardless of the 1.0 question: `required-version = "==0.11.29"` currently blocks every `uv`/`just` command on this machine, and the `local`/`quickstart`/`all` extras have been red in nightly CI for eight straight nights on an undeclared PortAudio system dependency whose error message misdirects the user. What is done genuinely well is optional-dependency discipline inside the code — 39 guarded `require_module` sites, zero unguarded module-level optional imports outside the vendored FunASR runtime, no import-time side effects, and a lazy top-level package that imports in ~10 ms over interpreter baseline.

**Done well here:**

- Optional-dependency guarding is close to exemplary. An AST sweep of all 287 source files for module-level imports of the 22 optional distributions found exactly three hits, all inside `easycat/vad/_funasr_runtime/` which is itself reached only through guarded loaders (`src/easycat/vad/funasr.py:93`, `_funasr_runtime/online.py:106-107`). 39 `require_module` call sites, 32 of which name the exact extra, produce messages of the form "Install with: uv add 'easycat[webrtc]'".
- Zero import-time side effects. An AST scan for module-level `os.environ`/`os.getenv` reads, filesystem calls, and `logging.basicConfig` across the package returned nothing; `easycat/__init__.py:33` correctly attaches only a `NullHandler`, and PEP 562 lazy exports keep `import easycat` at ~10 ms over the interpreter's own site baseline (40 ms vs 30 ms measured).
- The nightly extras install matrix is generated from `[project.optional-dependencies]` by `scripts/extras_matrix.py`, so adding an extra adds a CI cell with no workflow edit, and the documented exclusion set is itself guarded by `tests/test_dependency_policy.py:64-80` against going stale. `scripts/extras_smoke.py` derives its provider import targets from the existing `required_extra` column of the provider surface matrix rather than a parallel hand-maintained list.
- The release workflow's security design is careful and correct: build and publish are split so `id-token: write` is scoped to a single job (`release.yml:84-85`), secrets are passed explicitly rather than via `secrets: inherit` with the reasoning recorded inline (`release.yml:28-34`), caching is deliberately disabled in the publishing build to close a cache-poisoning vector (`release.yml:55-56`), and `pypa/gh-action-pypi-publish` is pinned to a 40-char SHA rather than the mutable `@release/v1` tag.
- The Python 3.11-3.14 support claim is actually exercised, not merely asserted: `ci.yml:77` runs the quick validation lane on 3.11 and 3.14 per PR, and `nightly-validation.yml:26` runs the full local suite on 3.11/3.12/3.13/3.14 — all four of which are currently green.
- The Docker image is production-shaped: multi-stage with the venv resolved in the builder, a dedicated non-root `easycat` user at uid 1000 with `nologin`, `HEALTHCHECK` wired to a real probe script, `libgomp1` installed for onnxruntime with the reason recorded, a named volume for journal durability, and a `.dockerignore` that explicitly excludes `.env*`, `*.pem`, and `*.key` from the build context.
- The Dependabot configuration is more thoughtful than most: a 7-day cooldown so freshly published (possibly compromised) versions soak before a PR opens, groups split by risk so majors never block the safe minor/patch batch, and a documented `ignore` on pip proposing pydantic-ai major bumps with the closed-issue reference explaining why.
- `requirements-docs.txt` is fully hash-pinned (375 `--hash` entries across 29 packages) with the exact regeneration command recorded in `requirements-docs.in:6`, and the pygments/pymdown-extensions pins carry the specific crash they work around.

#### 97. `required-version = "==0.11.29"` blocks every uv entry point on any other uv release — reproduced on this machine today

`HIGH` · `tooling-fragility` · `effort: small`

**Problem.** `[tool.uv].required-version` uses `==`, not a range. The uv on this machine is 0.11.30 (Homebrew, 2026-07-20), so every uv entry point hard-fails. I reproduced it directly: `uv run python -c 'print(1)'` in the repo root prints `error: Required uv version ==0.11.29 does not match the running version 0.11.30` and produces no output. That takes down `uv sync`, `uv build`, and therefore every `just` recipe (`just check`, `just test`, `just typecheck`, all seven `guard-*` lanes), since the justfile shells everything through uv. uv self-updates routinely via Homebrew or `uv self update`, so a tool upgrade unrelated to this repo bricks the repo with an error that names uv, not EasyCat, and offers no fix.

**Impact.** The working copy on this machine cannot run a single project command right now — I had to work around it for the rest of this audit. Any contributor whose uv is not byte-exactly 0.11.29 is blocked at the first `just sync`. The stated justification (lock churn) is already independently enforced: every CI job runs `uv sync --locked` (ci.yml:37 and equivalents in nightly-validation.yml, release-validation.yml), which fails on any lockfile drift regardless of the uv version that produced it.

**Fix.** Relax pyproject.toml:207 to `required-version = ">=0.11.29,<0.12"`. That keeps the major-version guard while unblocking local dev, and `--locked` in CI still catches lock churn. The one thing a range loses is the setup-uv optimization noted in the comment at pyproject.toml:204 (a range makes setup-uv resolve 'latest matching' over the network); if that matters, add an explicit `version: "0.11.29"` input to the `astral-sh/setup-uv` steps in the workflows and keep pyproject on the range, so the exact pin lives where it is needed instead of gating every local command.

**Evidence**

- `pyproject.toml:207` — `required-version = "==0.11.29"` — exact equality specifier under `[tool.uv]`
- `pyproject.toml:204` — the rationale comment: setup-uv reads it in CI to skip network 'latest' resolution, and local uv enforces it so 'mismatched versions can't churn uv.lock'
- `justfile:16` — `uv sync --group dev` — every recipe shells through `uv run`/`uv sync`, so all of them inherit the block
- `pyproject.toml:138` — `requires = ["uv_build>=0.11.28,<0.12.0"]` — the build backend already uses a range, so the `==` pin is internally inconsistent

*Verified:* Confirmed by execution, not just by reading: `uv --version` reports 0.11.30 and `uv run python -c 'print(1)'` errors out exactly as described. pyproject.toml:207 verified. Corrected one evidence claim: the original said bumping the pin is a 'coupled 2-file edit' with ci.yml:37 — it is not. No workflow pins a uv version; setup-uv reads pyproject, so bumping is a one-file edit. Downgraded critical -> high: this is a hard developer-workflow block, but it is not data loss, a security hole, a production outage, or silent wrong output, and CI itself is green because setup-uv installs the pinned version.

#### 98. The three extras that install `sounddevice` (`local`, `quickstart`, `all`) have failed the nightly extras matrix for 12+ consecutive nights on undeclared PortAudio, and every error path points the user back at the extra they already installed

`HIGH` · `correctness` · `effort: small`

**Problem.** `sounddevice` wraps the system libportaudio2 shared object. Declaring `sounddevice>=0.4.6` in an extra guarantees nothing on Linux: pip installs fine and `import sounddevice` raises `OSError('PortAudio library not found')`. I pulled the live CI evidence. `gh run view 30199058214 --json jobs` for the 2026-07-26 nightly shows 32 jobs, of which exactly three fail — `Extras Install (local)`, `Extras Install (quickstart)`, `Extras Install (all)` — while all 19 other extras cells pass. `--log-failed` shows the identical cause in all three: `extras-smoke[local]: import failures: - sounddevice: OSError('PortAudio library not found')`. `gh run list --workflow=nightly-validation.yml --limit 12` returns `conclusion: failure` for all 12 retained runs (2026-07-15 through 2026-07-26), so this is at least twelve consecutive red nights, not eight. On the error-message side, `_extras.require_module`'s OSError branch appends the extra-install hint, so the user is told to install `easycat[local]` when the actual fix is an OS package; `doctor.check_microphone` downgrades the same OSError to a `skip` carrying only the exception class name. Only docs/teaching/00-hello-audio/README.md:36 mentions libportaudio2 anywhere in the repo; the README install section does not.

**Impact.** A Linux user follows the README's quickstart, gets a clean install, and `LocalTransport` dies at construction with a message that loops them back to the install they just did. `easycat doctor` — the documented first-run check whose entire job is diagnosing this class of problem — reports it as a skip with a bare `OSError`, so the diagnostic explicitly designed for the case fails to diagnose it. And the one CI job that install-tests the extras has been red every night for two weeks, so its signal is now noise.

**Fix.** Three small fixes: (1) in `src/easycat/_extras.py`, split the OSError branch (lines 41-44) so the message names the missing shared library and the OS package rather than the Python extra, e.g. append "On Debian/Ubuntu: apt-get install libportaudio2; on macOS: brew install portaudio" when the underlying error mentions a library load failure; (2) in `doctor.check_microphone` (src/easycat/cli/diagnose/doctor.py:321-326), return `status="fail"` with an `EASYCAT_E2xx` code and that same fix string instead of `skip`; (3) add `sudo apt-get install -y libportaudio2` as a step in the `extras-install` job of .github/workflows/nightly-validation.yml (before the smoke step) and document the system package in the README install section, so local/quickstart/all go green and the matrix regains signal.

**Evidence**

- `pyproject.toml:80` — `local = ["sounddevice>=0.4.6", "numpy>=1.24.0"]` — sounddevice is a CFFI binding to the system libportaudio2, which pip cannot supply on Linux
- `src/easycat/transports/local.py:146` — `require_module("sounddevice", extra="local", ...)` — the LocalTransport entry point into the failing import
- `src/easycat/_extras.py:41` — the `except OSError` branch (lines 41-44) raises `ImportError(f"{label} could not load {module_name}: {exc}.{hint}")` where `hint` is `_extra_install_hint(extra)` -> "Install with: uv add 'easycat[local]'" — i.e. the extra the user already has
- `src/easycat/cli/diagnose/doctor.py:321` — `except OSError` returns `status="skip"` with `detail=f"sounddevice unavailable: {type(exc).__name__}"` — the class name only, no mention of libportaudio2
- `.github/workflows/nightly-validation.yml:187` — the extras-install matrix cell (`uv sync --locked --extra "${{ matrix.extra }}"` + `scripts/extras_smoke.py`) that is red for local/quickstart/all

*Verified:* Every claim independently reproduced. Live CI checked with gh: run 30199058214 has exactly three failing jobs (local, quickstart, all) with the PortAudio OSError in the log; 12/12 retained nightly runs are failures, so the finding's '8 nights' understates it. Corrected two citations: the OSError branch in _extras.py is lines 41-44, not 45; the Dockerfile evidence pointed at line 29 but the runtime stage's apt-get install is lines 36-39 and the runtime `FROM` is line 31 — I dropped the Dockerfile item entirely because the default `ARG EXTRAS` (Dockerfile:23) does not include `local`, making that a hypothetical rather than a shipped break. Held at high: not critical (no data loss/security/silent wrong output), but it breaks a documented golden path on Linux and has destroyed the signal from the only CI job that install-tests extras.

#### 99. `pyrnnoise` in the `quickstart` extra drags matplotlib, PyAV, Pillow, fonttools and Jinja2 into the golden-path install for two C-binding symbols

`MEDIUM` · `dependency-bloat` · `effort: medium`

**Problem.** `quickstart` — the extra the README recommends for the first-run experience — pulls `pyrnnoise`, whose unconditional install_requires chain is `pyrnnoise -> audiolab -> av + matplotlib + soundfile + smart_open + jinja2 + humanize`, plus matplotlib's own Pillow/fonttools/contourpy/kiwisolver/cycler/pyparsing tail. I measured the wheel sizes straight out of uv.lock for linux-aarch64: av 41.98 MB, onnxruntime 18.67 MB, numpy 16.92 MB, pyrnnoise 13.27 MB, livekit 11.37 MB, matplotlib 11.14 MB, Pillow 7.00 MB, fonttools 5.15 MB. The audiolab/matplotlib branch alone is roughly 65 MB of wheels that exists only to serve pyrnnoise's CLI tooling. EasyCat touches exactly two symbols from the distribution — `FRAME_SIZE` and `create()` (noise_reduction.py:123-124) — and never imports audiolab, matplotlib, av, or soundfile.

**Impact.** The advertised first-run install downloads a scientific plotting stack and a video-codec library to denoise 10 ms audio frames, and drags Pillow and PyAV — both frequent CVE subjects — into the dependency-audit surface of every quickstart user for zero functional benefit. Because `EasyConfig.enable_noise_reduction` defaults to `False` and `create_noise_reducer`'s auto mode degrades to a logged passthrough when no backend is installed (noise_reduction.py:340-346), most quickstart users pay this cost and never execute a single line of RNNoise.

**Fix.** Drop `pyrnnoise>=0.3.0` and `requests>=2.33.0` from the `quickstart` extra (pyproject.toml:98-107) and update the README's `quickstart` description (README.md:134-138) to stop listing 'RNNoise dependencies'. Keep them in the opt-in `rnnoise` extra. This is safe today because noise reduction is off by default and auto mode already degrades to passthrough. If RNNoise is wanted on the golden path, replace the dependency with a leaf binding (or a vendored ~200-line ctypes wrapper for the two symbols used) that does not carry the audiolab/matplotlib chain — that would also shrink `rnnoise` and `all`.

**Evidence**

- `pyproject.toml:98` — `quickstart` extra (lines 98-107) lists `pyrnnoise>=0.3.0` alongside `requests>=2.33.0`
- `uv.lock:4983` — `pyrnnoise 0.4.3` dependencies: audiolab, click, matplotlib, numpy, tqdm — all unconditional, no extras to opt out of
- `uv.lock:370` — `audiolab 0.5.1` dependencies: av, click, humanize, jinja2, requests, smart-open, soundfile
- `src/easycat/noise_reduction.py:115` — `require_module("pyrnnoise.rnnoise")` — the raw binding is the only thing loaded
- `src/easycat/noise_reduction.py:123` — `getattr(self._rnnoise, "FRAME_SIZE", ...)` and `self._rnnoise.create()` on line 124 — the complete API surface EasyCat uses
- `src/easycat/config/easy.py:512` — `enable_noise_reduction: bool = False` — noise reduction is off by default, so removing pyrnnoise from quickstart does not change the default golden path

*Verified:* Dependency chain confirmed from uv.lock (pyrnnoise 0.4.3 -> audiolab/matplotlib/click/numpy/tqdm; audiolab 0.5.1 -> av/jinja2/soundfile/smart-open/humanize/requests) and the two-symbol usage confirmed at noise_reduction.py:115-127. Corrected the size numbers: I measured from the lockfile rather than a pip dry-run and got av at 41.98 MB on linux-aarch64, not the 22.5 MB claimed (platform-dependent; the original figure was likely x86_64). I did not verify the '60 packages / 118 MB' total and dropped it from the writeup. Downgraded high -> medium: real and every quickstart user pays it, but the harm is download size and audit surface, not breakage — bounded. Added the enable_noise_reduction=False evidence myself; it makes the fix strictly safer than the original finding claimed.

#### 100. Dependency floors track whatever was newest when written, not what the code needs, and no CI job ever resolves against them

`MEDIUM` · `api-ergonomics` · `effort: medium`

**Problem.** Every floor matches the version uv.lock currently resolves, which is the signature of `>=whatever-was-latest` rather than a researched minimum. I inventoried the actual API surface: rich is used only for `Console`, `Table`, `Panel`, `Prompt` and `markup.escape` (stable since rich 10.x, 2021); typer only for `Typer`/`Option`/`Argument`/`Context`/`Exit` (stable since typer 0.4); langchain-core only for the three message classes in `langchain_core.messages` (stable since 0.1); websockets only for `websockets.asyncio.client/server`, `datastructures.Headers` and `http11.Request/Response` (the new asyncio implementation, shipped in 13.0 and stable in 14.x). None of these justify their floors. There is also no mechanism that would ever catch an over-tight floor: `grep -rn resolution .github/ justfile scripts/ pyproject.toml` finds no `--resolution lowest-direct` job, every CI job runs `uv sync --locked` against the pinned lockfile, and tests/test_dependency_policy.py guards the `all` union and a vulnerable-onnx floor but says nothing about minimums.

**Impact.** For an application-embedded library this is a co-installability tax. A team with an existing service on rich 13, typer 0.12, or langchain-core 0.3 cannot add EasyCat without a coordinated upgrade of unrelated infrastructure, and gets pip backtracking rather than a clear message. rich and typer are two of the most common CLI dependencies in the ecosystem, so pinning them to their newest releases narrows the set of environments EasyCat can join for reasons that do not exist in the code. The `<17` cap on core `websockets` and `<1.0.0` on `llama-agents-client` compound it from the other side.

**Fix.** Lower each floor to the oldest release whose API the code actually uses — `rich>=13.0`, `typer>=0.12`, `httpx>=0.27`, `langchain-core>=0.3`, `websockets>=14.0,<17`, `aiohttp>=3.9` are supported by the usage inventory above — and then make the claim testable: add one CI job to .github/workflows/ci.yml running `uv sync --resolution lowest-direct --group dev` followed by `uv run easycat validate quick`. Without that job any floor number in pyproject.toml is an unverified assertion in either direction.

**Evidence**

- `pyproject.toml:36` — `rich>=15.0.0` — the only rich API used repo-wide is Console, Table, Panel, Prompt and markup.escape
- `pyproject.toml:39` — `typer>=0.26.8` — the only typer API used is Typer, Option, Argument, Context, Exit (plus typer.testing in tests)
- `pyproject.toml:35` — `httpx>=0.28.1` — AsyncClient/Timeout/Response and the standard exception tree
- `pyproject.toml:58` — `langchain-core>=1.4.9` — the only import is `langchain_core.messages` (AIMessage/HumanMessage/SystemMessage)
- `pyproject.toml:40` — `websockets>=15.0.1,<17` on a core dependency; the code only needs `websockets.asyncio.*`, `websockets.datastructures`, `websockets.http11`, available since websockets 13/14
- `pyproject.toml:72` — `aiohttp>=3.14.0` in the telephony extra — only `aiohttp.web` and `WSMsgType` are used

*Verified:* Line numbers 35/36/39/40/58/72 all verified exactly. I independently inventoried the rich, typer, and websockets API surface across src/easycat and confirmed the floors far exceed what the code uses, and confirmed no `--resolution lowest-direct` job exists anywhere. I did NOT reproduce the original's claim of having installed rich 13.7.1 / typer 0.12.5 / httpx 0.27.2 into a clean venv and run the CLI — uv is blocked on this machine (see the required-version finding), so that experiment is unverified and I removed it from the writeup; the argument now rests on the API inventory alone. Downgraded high -> medium: the package is not yet on PyPI, nothing breaks at runtime, and the harm is a bounded, easily-fixed co-installation constraint rather than something that bites most users.

#### 101. Release path has never executed: no tags, zero release.yml runs, no CHANGELOG or `__version__`, and the PyPI name is unregistered

`MEDIUM` · `release-readiness` · `effort: medium`

**Problem.** There is a carefully built three-job release pipeline — validate -> build -> publish, Trusted Publishing over OIDC, a `pypi` environment reviewer gate, SHA-pinned `pypa/gh-action-pypi-publish` — that has never run. `gh run list --workflow=release.yml` returns an empty list, no annotated tag exists locally or on the remote, and the reusable gate release-validation.yml has never completed a run either: all 45 retained runs are from `event: push` on the `fix/library-review` branch around 2026-05-29 and GitHub reports them as "likely failed because of a workflow file issue" with no retained logs, i.e. they died at workflow startup before executing a step. The current file has no `push` trigger at all, so those runs are stale artifacts of an older trigger config, not evidence about the gate's steps. Separately there is no CHANGELOG, no `easycat.__version__` (only the CLI reports a version via installed metadata), and no documented version-bump procedure; the only stability statement is the three-sentence pre-release note at docs/public-api.md:231-236.

**Impact.** The first real `git tag v0.1.0` will be the first execution of the entire release path — a reusable gate that has never run a step, and the `release-validation` and `pypi` environments whose secrets and reviewer configuration have never been exercised — against a live PyPI publish. That is the worst possible moment to find out something is misconfigured. Meanwhile the `easycat` name is unregistered on PyPI for a public repo whose README announces the intent to use it.

**Fix.** Reserve the `easycat` name on PyPI now (a 0.0.0 placeholder or a pending Trusted Publisher configuration). Then exercise the gate before it matters: run release-validation.yml via `workflow_dispatch` (it supports it — release-validation.yml:4) until it is green, and dry-run release.yml against TestPyPI with a `v0.1.0rc1` tag. Add CHANGELOG.md and export `__version__` from src/easycat/__init__.py via `importlib.metadata.version("easycat")` so downstreams can feature-detect, and add a sentence to docs/public-api.md stating the SemVer contract that takes effect at 1.0.

**Evidence**

- `.github/workflows/release.yml:9` — triggers only on `push: tags: v*`; `git tag` returns empty, `git ls-remote --tags origin` returns empty, and `gh run list --workflow=release.yml` returns `[]`
- `.github/workflows/release-validation.yml:3` — `on: workflow_dispatch` + `workflow_call` only — all 45 retained runs have `event: push`, meaning they never reached the gate's steps
- `README.md:65` — "EasyCat is not published to PyPI yet" — I confirmed https://pypi.org/pypi/easycat/json returns HTTP 404, so the name is unregistered
- `pyproject.toml:3` — `version = "0.1.0"`; no CHANGELOG.md at repo root and no `__version__` in src/easycat/__init__.py

*Verified:* Verified every factual claim: `git tag` and `git ls-remote --tags origin` are both empty, `gh run list --workflow=release.yml` returns `[]`, `curl https://pypi.org/pypi/easycat/json` returns 404, no CHANGELOG.md, no `__version__` in __init__.py. Made one significant correction: the original framed the release-validation failure rate as evidence that "the gate is currently decorative" and "a 40/40 failure rate means the gate is broken." That is wrong. I checked run 26622070548 — GitHub reports "This run likely failed because of a workflow file issue", logs are not retained, and all 45 runs carry `event: push` while the current workflow declares only `workflow_dispatch`/`workflow_call`. These are startup failures from a stale trigger config on a feature branch two months ago, not the gate failing on its merits. The real finding is that the gate has never run at all, which I rewrote accordingly. Held at medium.

#### 102. The `telephony` extra forces FastAPI, uvicorn and python-multipart on library consumers although `src/easycat/telephony/` imports none of them

`LOW` · `over-engineering` · `effort: small`

**Problem.** `easycat[telephony]` pins three web-serving dependencies at aggressive floors for code that is not in the installed package. `grep -rn fastapi src/easycat --include=*.py` returns exactly one hit, in `cli/scaffold/templates/twilio-phone/server.py`, which is template text copied into a user's project. uvicorn appears only inside CLI hint strings (cli/_app.py:557, cli/_app.py:878, cli/scaffold/init.py:196). python-multipart appears nowhere at all — EasyCat's own Twilio webhook helper parses the form body itself with `parse_qsl` (telephony/twiml.py:173-178), so even the FastAPI-based example never needs Starlette's multipart parser. What `src/easycat/telephony/` actually imports is `aiohttp.web`, `websockets.asyncio.server`, and `twilio` (lazily, per telephony/outbound.py:184).

**Impact.** A team adding Twilio telephony to an existing Litestar, Starlette, Django or bare-aiohttp service is forced onto `fastapi>=0.139` and `uvicorn>=0.51` — and onto python-multipart, which nothing in the repo uses — because the extra bundles the dependencies of the repo's example and scaffold template with the dependencies of the library itself. It is a small tax, but it is applied to the one extra most likely to land inside somebody else's production web app.

**Fix.** Remove `python-multipart>=0.0.29` from the `telephony` extra outright (pyproject.toml:71-78 and the mirrored entry in `all` at pyproject.toml:113+, which tests/test_dependency_policy.py:64 will require you to keep in sync). Then either move `fastapi`/`uvicorn` into a separate `telephony-example` extra that examples/twilio_app.py and the twilio-phone scaffold depend on, or declare them in the scaffold template's own pyproject.toml, leaving `telephony` pinning only what `src/easycat/telephony/` imports (aiohttp, twilio, phonenumberslite). Update the README.md:167 line to match.

**Evidence**

- `pyproject.toml:71` — `telephony` extra (lines 71-78) pins `fastapi>=0.139.0`, `uvicorn>=0.51.0`, `python-multipart>=0.0.29` alongside aiohttp/phonenumberslite/twilio
- `src/easycat/telephony/twiml.py:173` — the webhook helper reads `await request.body()` and calls `parse_qsl` on the raw text — it never calls `request.form()`, so python-multipart is not needed by any code path in the repo
- `examples/twilio_app.py:115` — the only non-template FastAPI import in the repo, inside `create_app()` in an example — not in the installed package's telephony code
- `src/easycat/cli/scaffold/templates/twilio-phone/server.py:12` — the other FastAPI import, in a template that is copied into the *user's* project rather than imported by EasyCat
- `README.md:167` — "FastAPI + Twilio SDK (Twilio Media Streams / outbound calls): `uv sync --extra telephony --group dev`" — the extra is documented as the way to get FastAPI

*Verified:* Heavily narrowed from the original, which bundled five sub-claims across nine extras. REJECTED sub-claims: (a) the three empty marker extras are documented at pyproject.toml:62-67 as deliberate doctor-enumeration markers and are the standard way to keep `easycat[deepgram]` from erroring; (b) `silero-vad` and `smart-turn` being byte-identical is not duplication — they are separate features that happen to share numpy+onnxruntime, and aliasing them would be worse for users; (c) the `openai` extra is not unused — src/easycat/debug/testing.py:489 imports `AsyncOpenAI` for the default LLM judge, and openai-agents pulls the SDK anyway; (d) the original's evidence that `telephony` is 'decorative' overlooked examples/twilio_app.py:115, which does import FastAPI and is the documented way to run the Twilio example. What survives is the narrower and better-supported claim, which I strengthened myself by confirming at telephony/twiml.py:173-178 that python-multipart has no consumer anywhere in the repo. Downgraded medium -> low.

#### 103. 91% of the base wheel is ONNX model weights that ship regardless of which extras are installed

`LOW` · `packaging` · `effort: medium`

**Problem.** I measured the packaged tree directly: deflate-compressing `src/easycat/models/` yields 11.08 MB, while deflate-compressing everything else under `src/easycat` (excluding __pycache__) yields 1.13 MB. So the base wheel is roughly 12.2 MB of which about 91% is neural network weights. Those weights ship in the base distribution, outside the extras system: `silero-vad`, `smart-turn` and `funasr-vad` gate the *runtime* (numpy + onnxruntime) behind an extra, but the weights arrive with `pip install easycat` even for a user running server-side VAD via the OpenAI Realtime API or Deepgram endpointing who will never open them.

**Impact.** Every install and every Docker layer rebuild carries about 11 MB of weights that most deployments never load. It is a one-time download rather than a runtime problem, and it is dwarfed by onnxruntime (18.7 MB) and numpy (16.9 MB) for anyone who does use the ONNX backends — but it is inconsistent with the project's own extras design, and nothing prevents the number from growing on the next model bump.

**Fix.** The narrow, safe fix is a guard rather than a redesign: add a wheel-size assertion to the `build-smoke` job in .github/workflows/ci.yml (e.g. fail if the built wheel exceeds ~13 MB) so the figure is a tracked budget instead of an accident. If the size is worth attacking, the highest-yield single change is moving smart-turn's 8.3 MB out — it is the only one of the three that is opt-in per session rather than a default-path VAD — either into a separate `easycat-models-smart-turn` data package required by the `smart-turn` extra, or fetched on first use into a cache dir with a checksum. Note that lazy fetching trades away the offline-first property the README advertises at README.md:161 ("runs the bundled ONNX model"), so weigh it accordingly.

**Evidence**

- `src/easycat/smart_turn.py:28` — `_BUNDLED_MODEL = .../models/smart-turn-v3.2-cpu.onnx` — 8.3 MB on disk
- `src/easycat/vad/silero.py:30` — `_SILERO_ONNX_MODEL = .../models/silero_vad.onnx` — 2.3 MB on disk
- `src/easycat/vad/funasr.py:24` — `_FUNASR_BUNDLED_MODEL_DIR = .../models/funasr_fsmn_vad` — 1.7 MB on disk
- `pyproject.toml:142` — `source-exclude` (lines 142-208) prunes caches, docs, plans and the root test suite but never scopes `src/easycat/models/`

*Verified:* Measurements independently reproduced and they match the original almost exactly: models compress to 11.08 MB, the rest of src/easycat to 1.13 MB, giving ~91%. All three model-path citations verified at the stated lines, and `source-exclude` confirmed not to scope the models directory. Downgraded medium -> low: the harm is a bounded one-time download that is smaller than the onnxruntime/numpy wheels any ONNX user installs anyway, and bundled offline models are a deliberate, documented product feature (README.md:161, 'no torch required'), so option (b) in the original recommendation would trade a real capability for the saving. I reframed the recommendation around the wheel-size guard, which is the part that is unambiguously worth doing.

#### 104. The pytest11 entry point eagerly imports the bundle/journal stack into every pytest process in any environment where easycat is installed

`LOW` · `packaging` · `effort: small`

**Problem.** The pytest11 entry point registers unconditionally, and the plugin module imports the bundle stack at module scope. I reproduced this in a throwaway directory with one trivial test file, using the repo's own venv interpreter: a `pytest_configure` probe reports eight easycat modules already resident — `easycat`, `easycat._public_api`, `easycat.debug`, `easycat.debug._bundle_loader`, `easycat.debug._bundle_models`, `easycat.debug._pytest_plugin`, `easycat.debug.bundle`, `easycat.debug.testing` — plus `sqlite3`. Wall-clock cost is negligible; the issue is coupling, not speed.

**Impact.** Any import-time regression in `easycat.debug.testing` or its transitive imports — a broken optional dependency, a bad refactor — breaks collection for every pytest suite in that environment, including suites with no connection to EasyCat, with a traceback pointing at a plugin the user never enabled. Low probability, wide blast radius, and the fix is one line.

**Fix.** Move `from easycat.debug.testing import load_bundle` from module scope (src/easycat/debug/_pytest_plugin.py:20) into the `_load` closure at line 32, so plugin registration imports nothing beyond pytest itself and the bundle stack loads only when a test actually requests the `easycat_bundle` fixture.

**Evidence**

- `pyproject.toml:51` — `[project.entry-points.pytest11]` / `easycat = "easycat.debug._pytest_plugin"` — unconditional autoload with no opt-in
- `src/easycat/debug/_pytest_plugin.py:20` — `from easycat.debug.testing import load_bundle` at module scope, executed at plugin-registration time
- `src/easycat/debug/_pytest_plugin.py:32` — `def _load(path)` — the closure is the only consumer of `load_bundle`, so the import can move inside it

*Verified:* Reproduced the module-residency claim exactly: running pytest on a one-line test file with the repo venv's interpreter reports precisely the eight easycat modules listed plus sqlite3. Both cited lines verified. REJECTED the second half of the original finding, about `import pytest` at src/easycat/testing/contracts.py:35 (line number is correct): that module is a provider contract kit whose documented usage is subclassing its suites inside a pytest test file, so every real importer has pytest by definition, and the 'pkgutil.walk_packages sweep fails' scenario is synthetic. Adding a `testing` extra or excluding the module from the wheel would break the out-of-tree provider workflow described in its own docstring and exercised by tests/contracts/. Kept at low.

#### 105. Docker base images ride mutable tags and are outside Dependabot, while every GitHub Action is SHA-pinned

`LOW` · `supply-chain` · `effort: small`

**Problem.** The workflows demonstrate a deliberate supply-chain posture: every GitHub Action is pinned to a 40-character commit SHA with a trailing version comment, Dependabot is configured to bump those pins weekly with a 7-day cooldown, zizmor and actionlint run in pre-commit, and the publish job uses tokenless OIDC. The Dockerfile does not follow the same standard — both `FROM` lines use floating tags, so two builds of the same commit can resolve different base layers, and .github/dependabot.yml has no `docker` ecosystem entry, so the base images are never audited or updated on the same cadence as everything else.

**Impact.** Non-reproducible image builds and an unaudited base-layer supply chain in the one artifact users are told to run in production (docs/deployment/docker.md). It is a small gap only because the rest of the posture is strong enough to make it conspicuous.

**Fix.** Pin both `FROM` lines in docker/Dockerfile (lines 16 and 31) to `image@sha256:...` digests with a trailing `# tag` comment, mirroring the Actions convention, and add a `package-ecosystem: "docker"` entry with `directory: "/docker"` and the same weekly schedule and 7-day cooldown to .github/dependabot.yml so the digests get bumped alongside everything else.

**Evidence**

- `docker/Dockerfile:16` — `FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder` — mutable tag
- `docker/Dockerfile:31` — `FROM python:3.11-slim-bookworm AS runtime` — mutable tag
- `.github/workflows/ci.yml:29` — `actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0  # v7.0.0` — the convention every Action in the repo follows
- `.github/dependabot.yml:7` — ecosystems configured are `uv`, `github-actions` and `pip`; there is no `docker` entry, so neither base image is ever bumped

*Verified:* Both `FROM` lines and the checkout SHA pin verified. Corrected one line number: the runtime stage `FROM` is Dockerfile:31, not 29 (line 29 does not exist in a meaningful sense — the stage comment is line 30). I also independently confirmed the Dependabot gap by reading .github/dependabot.yml in full: only uv, github-actions and pip ecosystems are configured. Kept at low; the claim is accurate and the fix is mechanical.

#### 106. `easycat[all]` excludes PydanticAI and the exclusion is documented only in a pyproject comment

`LOW` · `api-ergonomics` · `effort: small`

**Problem.** `all` is a union of every extra except ten-vad and the two pydantic-ai variants. The exclusion is necessary — `[tool.uv].conflicts` makes the two pydantic-ai extras mutually exclusive, so including either would make `easycat[all,pydantic-ai]` unresolvable — and it is guarded by a test. The gap is purely on the documentation side: `grep -rn '\[all\]|--extra all' README.md docs/` returns nothing, so an extra named `all` exists in the published metadata, is discoverable from PyPI, and is described to users nowhere. A user who reaches for it as the obvious evaluation install gets no PydanticAI support and no signal about why. Separately, the `pydantic-ai` extra is frozen at `>=1.99.0,<2.0.0` while the ecosystem has moved to the 2.x line, with no stated sunset for the v1 branch.

**Impact.** A bounded discoverability papercut: someone installing `easycat[all]` to evaluate the framework finds PydanticAI missing and has to read pyproject.toml to learn it was intentional. Nothing breaks silently at runtime.

**Fix.** Add two lines to the README extras section (near README.md:165): document that `easycat[all]` exists, and state that it deliberately omits `ten-vad` (license) and both pydantic-ai extras (mutually exclusive — pick one explicitly). Separately, state an exit plan for the v1 line in the pyproject comment at line 55: a date after which `pydantic-ai` points at the 2.x range and `pydantic-ai-v2` is deleted, collapsing the `[tool.uv].conflicts` block.

**Evidence**

- `pyproject.toml:108` — the comment above `all` (lines 108-112) documents the three exclusions: ten-vad, pydantic-ai, pydantic-ai-v2
- `pyproject.toml:208` — `[tool.uv].conflicts` declares pydantic-ai and pydantic-ai-v2 mutually exclusive, which is why neither can join `all`
- `tests/test_dependency_policy.py:64` — `test_all_extra_is_union_of_non_conflicting_extras` mechanically enforces the exclusion set, so it is deliberate and guarded
- `README.md:165` — the extras list documents every per-feature extra individually but never mentions that an `all` extra exists, nor what it omits

*Verified:* The exclusion, the conflicts block and the enforcing test are all real and verified at the cited lines, and I confirmed by grep that `all` appears in no README or docs file. REJECTED the finding's central claim and its entire impact story: it argued that `PydanticAIBridge.__init__` needs a `require_module("pydantic_ai", ...)` guard because the bridge 'constructs successfully without it before failing mid-turn.' That scenario is not reachable. The bridge takes a caller-constructed `pydantic_ai.Agent` (src/easycat/integrations/agents/pydantic_ai.py:65), so any user with a real agent has already executed `from pydantic_ai import Agent` in their own code and would fail there first; the original's own evidence says it constructed the bridge with a *stub*, which is not a user path. That is also why this bridge correctly differs from OpenAIAgentsBridge, which needs `Runner` from the SDK inside EasyCat itself and therefore does carry a guard (verified by tests/integrations/agents/test_agent_bridge_install_hints.py:21). Downgraded medium -> low: what remains is a documentation gap, not a runtime trap.

---

### Agent-framework bridges

*9 findings — 3 high · 5 medium · 1 low*

**Assessment.** The `ExternalAgentBridge` protocol itself is a genuine success: I grepped `session/` and `stages/` and found zero framework-specific branches — the only dispatch anywhere in the runtime is `isinstance(provider, AgentRunner)`, so the abstraction really does hold at the seam. Everything below that seam is the problem. The six bridges re-implement the same three concerns (cursor lifecycle with three exception arms, the plan/apply/serialize interruption triple, the sub-generator `aclose` dance) five to seven times each, even though `BridgeTemplate` — a correct shared base for exactly this boilerplate — already ships in the same package and is used only by `GenericWorkflowBridge`. That duplication has already produced divergent correctness: the partial-turn-on-cancel fix and the stop-at-user-message guard exist in LangChain and LangGraph but not in PydanticAI, where a barge-in silently overwrites the *previous* turn's answer in history. The single biggest problem, though, is that none of this is verifiable: `uv sync --group dev` installs no agent SDK, CI never installs one, and the one nightly job that does runs only `tests/contracts`, which exercises a hand-written fake. Nearly 10k lines of reverse-engineered coupling to six pre-1.0 SDKs — 192 `hasattr`/`getattr` probes, private-class-name string matching, four-layer import fallbacks — carry zero automated compatibility signal, and because almost every probe fails soft into `logger.debug`, upstream drift degrades silently rather than crashing. For a solo-maintained pre-1.0 library this breadth is not affordable; two provably correct bridges would serve users better than six plausible ones.

**Done well here:**

- The `ExternalAgentBridge` protocol genuinely abstracts. Grepping all of `src/easycat` outside `integrations/agents/` for framework names turns up only comments, CLI help strings, and scaffold templates — no behavioral branches. `AgentStage` (src/easycat/stages/agent.py:71-110) dispatches solely on `AgentRunner` vs. bridge, and `session/_streaming.py:109-140` maps the five event kinds uniformly. This is the hard part of a multi-framework adapter layer and it was done right.
- `OpenAIAgentsBridge` handles cancellation correctly and expensively-correctly: `result.cancel(mode="after_turn" if pending_tool_calls else "immediate")` (openai_agents.py:246) with a documented explanation that abandoning `stream_events()` would let the SDK run the turn to completion, fire post-cancel tool side-effects, and bill tokens. It then keeps draining the stream so cancellation settles before snapshotting state. This is the only bridge that provably stops the in-flight LLM call, and the reasoning is captured in the code.
- `run_interruption_journal_protocol` (base.py:159-231) is properly factored: the four-step ordering (pre-snapshot → `FrameworkStateCommitted` → apply → paired success/failure) lives in one place, with correct asymmetric degraded-journal handling — a step-2 journal failure skips the mutation, a step-4b failure logs and continues because the mutation already stands. `tests/contracts/test_agent_bridge_contracts.py:257-285` pins both halves.
- `_drop_dangling_function_calls` (openai_agents.py:57-79) is exactly the kind of detail that separates a real integration from a demo: a hard-cancelled run can snapshot `to_input_list()` mid-tool-call, and the Responses API rejects a `function_call` with no matching output, poisoning every later turn. Someone found this the hard way and fixed it at the right layer.
- `_unwrap_compiled_state_graph` (_factory.py:557-578) raises `BridgeInputError` rather than silently dropping a `.bind(**kwargs)` / `.with_listeners(...)` wrapper it cannot preserve through a `RunnableRetry`. Choosing a loud failure over a silent semantic change, and documenting why, is unusually disciplined.
- `_langchain_events.py` picks the right default for TTS safety: `_custom_event_text` (line 50-65) speaks a custom event only when it carries an explicit `text` / `speak` / `say` key, so chain instrumentation and progress telemetry cannot leak into audio. The per-event-type dispatch registry (line 646-659) also makes adding an `astream_events` type a two-line change.
- The out-of-tree extension path is real and complete: `register_agent_detector` (_factory.py:32-57), `BridgeTemplate` (template.py), and a published `AgentBridgeContractSuite` under `easycat.testing`. A third-party bridge author has everything they need — which is precisely what makes demoting some first-party bridges a low-cost option.
- `is_reusable_agent_spec` (_factory.py:334-417) reasons carefully about the per-connection-server concurrency question and correctly rejects a `Runnable` that pins `thread_id` / `checkpoint_id` / `session_id` via `with_config`, reusing the bridge's own `_bound_config` walk so detection cannot drift from resolution.

#### 107. PydanticAI bridge can skip its history commit on a torn-down turn, so the next apply_interruption() rewrites the *previous* turn's answer

`HIGH` · `correctness` · `effort: medium`

**Problem.** `PydanticAIBridge._stream_via_iter` / `_stream_via_run_stream` commit the turn's messages only on the normal fall-through path. When the turn is torn down by an `aclose()` (GeneratorExit) or a hard task cancel rather than by the cooperative `cancel_token` check, that line is skipped and the bridge's message history still ends at the previous completed turn. Session then calls `apply_interruption(delivered_text)`, and `_apply_planned_mutation` walks backwards for the last `ModelResponse` with no stop-at-user guard — hitting the previous, fully-delivered answer and replacing it with a fragment of a different reply.

**Impact.** When the race is lost, conversation history is silently corrupted: turn N-1's assistant message is overwritten with what the caller heard of turn N, and turn N disappears entirely. Every subsequent turn conditions on a fabricated transcript. Two concrete triggers exist: (a) a tool-calling agent — after cancellation the bridge keeps yielding tool events from a `CallToolsNode` and `_consume_cancelled` acloses on the first one; (b) a hard task cancel via `_turn_runner._cancel_and_drain` (agent timeout, or the turn task being cancelled), which raises CancelledError inside the `async with agent.iter(...)` body. The failure is silent, so users will blame the model.

**Fix.** In `src/easycat/integrations/agents/pydantic_ai.py`, wrap the `async with agent.iter(...)` body of `_stream_via_iter` (and the `stream_text()` loop of `_stream_via_run_stream`) in `try: ... except BaseException: self._set_history_for_key(history_key, await _run_new_messages(agent_run)); raise` — `AgentRun.new_messages()` is valid mid-run (pydantic_ai/run.py:174 slices `all_messages()[ctx.deps.new_message_index:]`), so it returns the user prompt plus whatever response parts exist. Independently and more cheaply, harden `_apply_planned_mutation` (line 306) and `replace_last_assistant_text` (line 345) to abort the backward walk when they reach a `ModelRequest` carrying a `UserPromptPart` before finding a `ModelResponse`, so a missing current turn is a no-op instead of a clobber. Add a test mirroring `tests/integrations/agents/test_langgraph_bridge_interruption.py` that acloses `invoke()` mid-stream and asserts the previous turn's `ModelResponse` is untouched.

**Evidence**

- `src/easycat/integrations/agents/pydantic_ai.py:500` — `self._set_history_for_key(history_key, await _run_new_messages(agent_run))` sits after the `async for node in agent_run` loop and inside `async with agent.iter(...)`. There is no `except BaseException` / `finally` arm, so a `GeneratorExit` injected at the `yield mapped` on line 498 unwinds both context managers and the turn is never recorded. Same shape at line 524 in `_stream_via_run_stream`.
- `src/easycat/integrations/agents/pydantic_ai.py:306` — `_apply_planned_mutation` (def at 292) walks history backwards for the last `ModelResponse` and overwrites its first `TextPart.content` with `delivered_text + '...'` (line 268). No stop-at-user guard, no 'this turn produced no assistant output' flag. `replace_last_assistant_text` (line 345) has the same unguarded backward walk.
- `src/easycat/session/_streaming.py:499` — `_AgentStreamConsumer._close_stream` unconditionally `aclose()`s the agent stream in `run()`'s `finally` (line 388). Combined with the deliberate aclose-forwarding in `stages/agent.py:339` and `pydantic_ai.py:189-196`, GeneratorExit reaches `_stream_via_iter`'s suspended `yield`.
- `src/easycat/session/_streaming.py:409` — `_consume_cancelled` returns False (→ `break` → aclose) the first time it sees an event while cancelled with `_pending_tool_calls <= 0`. This is deterministic for tool-using agents: pydantic_ai.py:489-492 keeps yielding tool events after cancellation when `is_tool_node`, so the first such event kills the generator mid-yield.
- `src/easycat/integrations/agents/langchain.py:366` — LangChain solves exactly this with an `except BaseException` arm that calls `_append_to_history(...)` + `_mirror_partial_turn_to_store(...)` (lines 898-906) before re-raising; `_rewrite_last_ai_in` (lines 900-906 of the module-level helper, docstring at 880-898) additionally returns False at the most recent user message. langgraph.py has both guards (`_commit_partial_assistant` at 866, `_turn_produced_no_assistant` checked at 1231). pydantic_ai.py has neither.

*Verified:* Mechanism confirmed empirically with a fake pydantic agent driving the real bridge (scratch repro): closing the consumer immediately after cancelling leaves the active history key `[]`, while letting the consumer keep pulling produces `['USER-N','ASSISTANT-N-partial']`. Two corrections to the original finding: (1) the claim that the bridge's cooperative break 'loses this race in the common case' is WRONG — pydantic_ai.py:489-492 checks `cancel_token.is_cancelled` *before* each `yield`, and `_streaming._consume` only checks after receiving an event, so in the plain text-streaming case the bridge wins and commits correctly. I downgraded critical → high and rewrote the impact to name the two paths where the race is actually lost. (2) Cited line numbers corrected: langchain BaseException arm is 366 (not 389); `_apply_planned_mutation` walk is 306 (not 310); `_consume_cancelled` is 409 (not 401).

#### 108. LangGraphBridge uses only LangGraph's synchronous checkpointer API, so async-only savers silently no-op and sync savers block the audio event loop

`HIGH` · `correctness` · `effort: large`

**Problem.** Every checkpointer interaction in the LangGraph bridge uses LangGraph's synchronous API. With a sync saver on disk or a remote backend, each turn performs three or more blocking round-trips directly on the asyncio loop that also runs the audio router, VAD and TTS pump. With an async-only saver, the sync methods are not implemented, and because every call site is wrapped in a bare `except Exception: logger.debug(...)` the bridge degrades to a silent no-op rather than failing loudly.

**Impact.** A user who moves to production persistence with `AsyncPostgresSaver` gets a bridge that constructs fine and then silently loses interruption truncation, the partial-turn commit on cancel, the transient-context purge, checkpoint journaling, `structured_output`, and the final-AIMessage fallback that makes non-streaming nodes audible at all — with nothing above DEBUG to explain it. With a sync remote saver, each turn stalls the event loop for the checkpointer's latency inside a 20 ms-frame realtime pipeline.

**Fix.** In `src/easycat/integrations/agents/langgraph.py`, prefer `aget_state` / `aupdate_state` / `aget_state_history` wherever the compiled graph exposes them and await them from `invoke()`; route the remaining sync calls through `asyncio.to_thread`. `apply_interruption` / `replace_last_assistant_text` are sync protocol methods, so queue the mutation and flush it at the top of the next `invoke()` rather than widening the protocol. Separately, at the constructor check (line 195) probe the saver — e.g. `type(checkpointer).get_tuple is BaseCheckpointSaver.get_tuple` — and raise or `logger.warning` naming the saver, instead of discovering it via a swallowed exception on turn one. At minimum, upgrade the five `logger.debug` swallows to `recorder.record_framework_error` so the journal shows the degradation.

**Evidence**

- `src/easycat/integrations/agents/langgraph.py:461` — `final_state = self._graph.get_state(config)` — sync, once per turn on the asyncio event loop. `grep -n 'aget_state|aupdate_state|aget_state_history|to_thread'` over the 1588-line file returns zero hits; all 10 checkpointer calls are sync (461, 706, 760, 768, 862, 893, 1144, 1174, 1241, 1281).
- `src/easycat/integrations/agents/langgraph.py:476` — `except Exception: logger.debug('Failed to fetch final LangGraph state', exc_info=True)` — the failure that costs `structured_output`, the final-AIMessage `done.text` fallback (used at 676) and the checkpoint trail is logged at DEBUG and never surfaced.
- `src/easycat/integrations/agents/langgraph.py:1241` — `_rewrite_last_ai_message` reads via sync `get_state` and writes via sync `update_state` (1281), both inside `except Exception: logger.debug(...)` (1242-1244) — a checkpointer that refuses sync calls makes interruption truncation a silent no-op. Same swallow at 862-864 (`_purge_transient_context`), 893-895 (`_commit_partial_assistant`), 1144-1147 (`get_state_history`).
- `src/easycat/integrations/agents/langgraph.py:195` — `if not checkpointer or checkpointer is True: raise BridgeInputError(...)` is the only checkpointer validation. An `AsyncSqliteSaver` / `AsyncPostgresSaver` is truthy, so it passes construction and fails every state call at runtime.
- `/home/yi/.cache/uv/archive-v0/p3nsf84tyv2muaIv/langgraph/checkpoint/base/__init__.py:251` — `BaseCheckpointSaver.get_tuple` is `raise NotImplementedError`; `Pregel.get_state` calls it directly (langgraph/pregel/main.py:1391-1401, langgraph 1.2.7). An async-only saver that does not implement the sync method therefore raises inside every one of the bridge's swallowed try blocks.

*Verified:* Verified there is not a single async state call or `to_thread` in the file, and confirmed all 10 sync call sites and all five DEBUG swallows at the exact lines cited. Confirmed against the cached langgraph 1.2.7 / langgraph-checkpoint 4.1.1 sources that `Pregel.get_state` invokes `checkpointer.get_tuple` synchronously and that the base implementation raises NotImplementedError. Corrected two line numbers (the swallow is 476, not 475; `_purge_transient_context`'s update_state is 862, not 'the tail of every turn' at 861). Kept at high: the async-saver path is silent feature loss in production, and the blocking-I/O path affects every sync remote saver. Note the docs only ever mention `InMemorySaver`, so the blast radius is users going to production, not the quickstart.

#### 110. No CI job ever executes a bridge against the SDK it wraps — the shipped contract kit runs only against fakes

`HIGH` · `maintenance-burden` · `effort: medium`

**Problem.** The bridge layer is ~9,900 lines of duck-typed coupling to six fast-moving SDKs, and no automated job ever executes any of it with the real SDK loaded. The nightly extras matrix installs each SDK and then runs only an import smoke plus `tests/contracts`, which contains no real-SDK bridge exercise. The shipped `AgentBridgeContractSuite` — the event-grammar and interruption-journal conformance kit published under `easycat.testing` for bridge authors — is never pointed at a first-party bridge.

**Impact.** An upstream change (LangGraph renaming a channel class, LlamaIndex reshaping its event envelope, PydanticAI changing `AgentRun`) is undetectable until a user reports a bot that went silent. Because the probes are defensive (`getattr(..., None)`, `except ImportError: pass`, `except Exception: logger.debug(...)`), the failure mode is silent degradation rather than a crash. This is also the mechanism by which the divergences in findings 1, 2 and 6 went unnoticed.

**Fix.** Change `.github/workflows/nightly-validation.yml:194` to `pytest tests/contracts tests/integrations/agents -q -m 'not integration_live'` so the `importorskip`-gated real-SDK tests actually execute in the cell that installed the SDK, and fix the misleading comment at line 189-193. Then add one `AgentBridgeContractSuite` subclass per real bridge (each setting `provider_factory` to build the bridge over a stub model — `FakeListChatModel` for LangChain/LangGraph, `TestModel` for PydanticAI, `agents` SDK fake model for OpenAI Agents), so the event-grammar and interruption-journal assertions run against real SDK objects at roughly 30 lines per bridge.

**Evidence**

- `.github/workflows/ci.yml:89` — `uv sync --locked --group dev --python ${{ matrix.python-version }}`. The dev group (pyproject.toml:216-229) is diff-cover, hypothesis, import-linter, mutmut, mypy, pre-commit, pytest*, ruff — no agent framework. I confirmed the local .venv (175 packages) contains no langgraph, langchain_core, pydantic_ai, agents, or workflows, so every bridge test in the standard suite runs against fakes.
- `.github/workflows/nightly-validation.yml:194` — The only job that installs a framework extra runs `pytest tests/contracts -q -m 'not integration_live'`, preceded by `scripts/extras_smoke.py` (import-only). `tests/contracts/` contains zero `importorskip`; its agent-bridge coverage is `test_agent_bridge_contracts.py:138-150`, which only `importlib.import_module`s the adapter module.
- `.github/workflows/nightly-validation.yml:189` — The step comment asserts 'the offline contract tests below, whose importorskip gates now run for real with this cell's SDK installed'. That claim is false: there are no importorskip gates in tests/contracts. The `importorskip`-gated real-SDK tests live in tests/integrations/agents/ and tests/e2e/, and no job runs those with an extra installed (the nightly full-local job at line 45 syncs `--group dev` only).
- `src/easycat/testing/contracts.py:357` — `AgentBridgeContractSuite` is subclassed exactly three times in the repo — `tests/contracts/test_agent_bridge_contracts.py:143` (over the hand-written `_ContractBridge` fake) and twice in `tests/testing/test_contract_kit.py:371,382`. None of LangGraphBridge, LangChainBridge, PydanticAIBridge, LlamaAgentsBridge, OpenAIAgentsBridge, RemoteResponsesAPIBridge is run through it.
- `src/easycat/integrations/agents/langgraph.py:1328` — `_is_add_messages_reducer` unwraps `functools.partial` layers, identity-compares against `langgraph.graph.message.add_messages`, then matches `__module__`, then substring-matches the name — pure reverse-engineering of LangGraph internals, with `type(channel).__name__ == 'LastValue'` (line 358) a string comparison against a private class name. Nothing executes this against a real graph in CI.

*Verified:* Every claim checked directly. Confirmed the dev group's exact contents, zero `importorskip` in tests/contracts, the three AgentBridgeContractSuite subclasses (all fakes), and that tests/integrations/agents and tests/e2e do carry importorskip gates that no extras-installed job runs. Added one item the original finding missed: the workflow's own comment at line 189-193 asserts the opposite of what the code does, which is worth fixing alongside. Severity kept at high — this is the meta-cause behind several of the other findings, and the fix is a one-line workflow change plus small suites.

#### 109. A barge-in on a local LlamaAgents workflow silently discards the workflow Context, and nothing tells the workflow it was cut off

`MEDIUM` · `correctness` · `effort: medium`

**Problem.** On any interrupted local Llama workflow turn the bridge nulls `self._ctx`. The code comment justifies this technically (reusing a still-running Context raises `ContextStateError` or replays the cancelled response's buffered deltas), but its claim that continuity is preserved via `apply_interruption` / `append_interruption_note` is false for the default configuration: `_apply_planned_mutation` writes only a snapshot field unless the workflow implements `apply_interruption` itself, and `append_interruption_note` is never invoked under the default `interruption_mode='truncate'`. So the workflow's own state is dropped with no record and no signal.

**Impact.** A LlamaIndex workflow that keeps retrieved documents, a running summary, or any `ctx.store` value loses all of it the moment the caller barges in, and never learns it was cut off — so it will happily regenerate the interrupted answer. There is no journal record and no warning; the only description of the behaviour is a code comment. `docs/using-easycat/05-agent-bridges/README.md:97` advertises this bridge for 'Local/remote workflows and human-in-the-loop resumption' with no caveat.

**Fix.** In `src/easycat/integrations/agents/llama_agents.py`, at the `finally` at line 512-519: gate the drop on the handler actually being non-terminal (`handler.is_done()`) so a Context that is safely reusable survives; and when it must be dropped, make the loss explicit — call `recorder.record_framework_error(...)` (the pattern `GenericWorkflowBridge.on_turn_start` already uses at generic_workflow.py:104-112) and set `self._pending_interruption_note = INTERRUPTION_NOTE` from `_apply_planned_mutation` so the note reaches the next start event via `_build_start_payload` (line 889) regardless of `interruption_mode`. Document the barge-in state semantics in the class docstring and in the agent-bridges chapter.

**Evidence**

- `src/easycat/integrations/agents/llama_agents.py:515` — `self._ctx = None` in `_invoke_local`'s `finally` whenever `cancelled or failed or cancel_token.is_cancelled` — i.e. on every barge-in. Under the default `preserve_context=True` (line 87) this is the only carrier of workflow-internal state between turns.
- `src/easycat/integrations/agents/llama_agents.py:511` — The justifying comment claims 'assistant-text continuity is carried by apply_interruption() / append_interruption_note(), not the workflow Context'. Neither delivers anything to a stock workflow — see the next two citations.
- `src/easycat/integrations/agents/llama_agents.py:870` — `_apply_planned_mutation` sets `self._last_output_text` (read only by `_serialize_framework_state`) and calls `target.apply_interruption` only if the user's workflow happens to define that method. For a stock LlamaIndex workflow it is a complete no-op.
- `src/easycat/session/_types.py:180` — `interruption_mode: Literal['truncate','message'] = 'truncate'` — the default routes to `apply_interruption`, so `append_interruption_note` (llama_agents.py:245, the only writer of `_pending_interruption_note`, which is the only source of the `easycat_interruption_note` start-event field at line 889-891) is never called.

*Verified:* All four citations verified at the exact lines. Downgraded high → medium and rewrote the impact: the original claim that 'mid-conversation the bot forgets who it is talking to and what was asked' is WRONG — `_build_start_payload` (line 884-885) passes the session's own conversation history into every start event via `context_key='context'` (default), so cross-turn conversational continuity does not depend on the workflow Context. What is genuinely lost is workflow-internal Context state and the interruption signal. The code comment also gives a real technical reason for dropping the Context, which the original finding treated as merely a false claim; I preserved the valid half of it.

#### 111. Bridges differ on tool visibility, structured output, and tool-call identity, with no capability matrix documenting it

`MEDIUM` · `api-ergonomics` · `effort: medium`

**Problem.** The `ExternalAgentBridge` protocol and the shipped `AgentBridgeContractSuite` imply a uniform event grammar, but the implementations differ on three user-visible axes with no documentation. Tool visibility: OpenAI Agents, LangChain, LangGraph, PydanticAI and Responses API emit tool events; LlamaAgents and GenericWorkflow emit none. Structured output: every bridge but Responses API populates `done.structured_output`. Tool-call identity: LangChain back-fills provider call ids onto argument deltas; PydanticAI ships them empty.

**Impact.** A user who builds a tool UI on `session.on(ToolCallStarted)` gets a working demo on OpenAI Agents and then silently nothing after switching to a LlamaIndex workflow, with no error and nothing in the journal to explain it. A user relying on `AgentFinal.structured_output` for typed results gets None from the remote bridge with no signal. A PydanticAI user cannot attribute argument deltas to a call. These are exactly the cross-framework portability promises the bridge layer exists to make.

**Fix.** Add a capability matrix to `docs/using-easycat/05-agent-bridges/README.md` next to the selection table (line 89) with one row per bridge and columns for token streaming, tool events, structured output, interruption fidelity, and history ownership. Close the two cheap gaps: pass a structured payload on the `done` event at `responses_api.py:229`, and thread `tool_name`/`call_id` through `_pydantic_ai_events._translate_delta` (line 22-35) by caching the last `FunctionToolCallEvent`'s ids the way `_langchain_events.py:398-411` does. For LlamaAgents, either emit tool events (LlamaIndex `AgentWorkflow` exposes `ToolCall`/`ToolCallResult` events) or state plainly in the class docstring that it does not.

**Evidence**

- `src/easycat/integrations/agents/llama_agents.py:459` — `_invoke_local` yields only `text_delta`. A grep for `tool_started|tool_delta|tool_result|record_tool_call` across this 1362-line file returns zero hits — a LlamaIndex workflow that calls tools produces no `ToolCallStarted` events and no tool-phase journal records. Same zero-hit result for generic_workflow.py.
- `src/easycat/integrations/agents/responses_api.py:229` — `yield AgentBridgeEvent(kind='done', text=accumulated)` with no `structured_output=`. Every other bridge passes it (langchain.py:459, langgraph.py:696, llama_agents.py:204, openai_agents.py:357, pydantic_ai.py:459 and 772), so `AgentFinal.structured_output` is permanently None for the remote bridge.
- `src/easycat/integrations/agents/_pydantic_ai_events.py:34` — `return AgentBridgeEvent(kind='tool_delta', text=text)` — no `tool_name`, no `call_id` — and `recorder.record_tool_call(phase='delta', name='')` on line 32. `_streaming.py:121-129` builds `ToolCallDelta(call_id=event.call_id, ...)`, so PydanticAI tool-argument deltas reach the user with an empty call_id and cannot be correlated with their `tool_started`.
- `src/easycat/integrations/agents/_langchain_events.py:405` — The LangChain translator caches `(run_id, index) -> (id, name)` and back-fills them onto args-only chunks precisely 'so tool_delta events and the journal delta phase stay associated with the originating tool_started instead of getting empty id/name'. The same problem was solved in one bridge and left open in the other.
- `docs/using-easycat/05-agent-bridges/README.md:89` — The bridge selection table gives one 'Use it for' blurb per bridge. Line 85 says 'The concrete bridges do not pretend every framework has identical features' but no page in docs/ states which features each bridge actually has (`grep -rl 'capability matrix' docs/` returns nothing).

*Verified:* All five citations verified; the zero-hit greps for tool events in llama_agents.py and generic_workflow.py, the missing `structured_output=` at responses_api.py:229 against the six bridges that pass it, the empty ids in `_translate_delta`, the LangChain back-fill cache with its rationale comment, and the absence of any capability matrix in docs/. Line numbers adjusted (docs table row is line 89; `_streaming.py` emission is 121-129). Severity medium is correct: real and user-visible, but bounded and cheap to fix.

#### 112. LlamaAgentsBridge speaks any workflow event carrying one of thirteen generic field names

`MEDIUM` · `correctness` · `effort: small`

**Problem.** LlamaIndex workflows use `ctx.write_event_to_stream(...)` for intermediate progress, retrieval traces and tool telemetry as much as for user-visible text, and the bridge treats every streamed event as potentially speakable, grabbing the first attribute named `prompt`, `question`, `content`, `response`, `output` or `result`. There is no opt-in marker and no suppression path short of supplying a custom `event_text_extractor=`.

**Impact.** A workflow emitting a routine progress or retrieval event — e.g. one whose `content` holds a matched document chunk, or whose `prompt`/`question` holds the internal query — has that text read aloud to the caller. The `str(text)` coercion at line 1191 makes it worse: a non-string field (a ChatMessage, a Pydantic model) is stringified and spoken. The same repository already established the safe default for LangChain and applied the unsafe one here.

**Fix.** In `src/easycat/integrations/agents/llama_agents.py`, split the field list: keep a narrow streaming set (`delta`, `text_delta`, `chunk`, `msg`, `text`) for `_extract_event_text` (line 895-898) and leave the full `_TEXT_FIELDS` scan for `_extract_output_text` on the terminal StopEvent result, where the question is 'what did the workflow return' rather than 'should this be spoken'. Also drop the `str(text)` coercion at line 1191 from the streaming path so a non-string field is skipped rather than stringified. Ship with a note that `event_text_extractor=` is the escape hatch for workflows that relied on the old behavior.

**Evidence**

- `src/easycat/integrations/agents/llama_agents.py:37` — `_TEXT_FIELDS = ('delta','text_delta','chunk','msg','prefix','prompt','question','text','content','message','response','output','result')` — thirteen names, seven of which ('prefix','prompt','question','content','message','response','output','result') have nothing to do with speakable output.
- `src/easycat/integrations/agents/llama_agents.py:1180` — `_extract_text_field` returns the first matching key from `_event_mapping(event)` (which unpacks `model_dump()`), recurses into nested dicts, and stringifies any non-collection scalar (`return str(text)`, line 1191) — so a non-string field is coerced to text and spoken.
- `src/easycat/integrations/agents/llama_agents.py:456` — `delta = self._extract_event_text(workflow_event)` is applied to every non-terminal, non-input-required workflow event in the local stream and yielded straight to TTS as a `text_delta`. The default `include_internal_events=False` (line 95) only passes `expose_internal=False` to LlamaIndex's `stream_events` (line 532), which filters the *framework's* internal events — not the workflow's own `ctx.write_event_to_stream(...)` progress events.
- `src/easycat/integrations/agents/_langchain_events.py:50` — `_custom_event_text` takes the opposite default for LangChain/LangGraph: only `text` / `speak` / `say` are spoken, with the explicit rationale 'so we don't accidentally narrate progress strings, debug logs, or state diffs'. The two bridges disagree on the same question.

*Verified:* All citations verified at the exact lines. Two corrections. (1) The original recommendation to drop `msg` is WRONG and would break the canonical LlamaIndex streaming pattern — the documented way a workflow streams tokens is `ctx.write_event_to_stream(Event(msg=chunk.delta))`, so `msg` (like `delta`) must stay in the speakable set; I narrowed the recommendation to the genuinely generic tail. (2) The finding missed the partial mitigation at line 95/532: `include_internal_events=False` already filters LlamaIndex's own internal events via `stream_events(expose_internal=False)` — it just does not filter the workflow's own custom events, which is where the problem actually lives. Kept at medium; 'data-exposure path' is overstated since the workflow author controls what is written to the stream, but unintended text reaching the caller is a real, user-visible defect.

#### 113. EasyConfig(mcp_servers=...) is silently ignored by the LangChain, LangGraph, and LlamaAgents bridges

`MEDIUM` · `api-ergonomics` · `effort: small`

**Problem.** `mcp_servers` is a documented, validated EasyConfig field, but `_inject_agent_runtime` only delivers it to bridges implementing the undeclared `configure_runtime` extension surface or exposing a private `_mcp_servers` attribute. For LangChain, LangGraph and LlamaAgents the setting evaporates: the URIs are validated, frozen into SessionConfig, threaded into `RecorderContext.mcp_servers`, and then never read.

**Impact.** A user writes `EasyConfig.mic(agent=my_graph, mcp_servers=['stdio://my-server'])`, sees the URIs validated at construction, and gets an agent with no MCP tools and no diagnostic anywhere. Debugging requires reading `_inject_agent_runtime` and discovering the delivery mechanism is a `hasattr` on a private attribute. Out-of-tree bridge authors hit the same trap, since the `configure_runtime` contract lives only in prose at docs/teaching/14-bring-your-own-agent/README.md:889.

**Fix.** Give `_inject_agent_runtime` (src/easycat/config/easy.py:250-293) a loud else-branch: when `mcp_servers` is non-empty and the resolved bridge implements neither `configure_runtime` nor `_mcp_servers`, raise at session construction naming the bridge class and the setting. Update `docs/reference/easyconfig.md:43` to name the bridges that honor `mcp_servers` (OpenAI Agents, PydanticAI, Responses API) and say the rest ignore it.

**Evidence**

- `src/easycat/config/easy.py:274` — `configure = getattr(inner, 'configure_runtime', None); if callable(configure): configure(...); return`. Only three bridges define `configure_runtime`: openai_agents.py:438, responses_api.py:388, pydantic_ai.py:332.
- `src/easycat/config/easy.py:285` — The fallback is `if hasattr(inner, '_mcp_servers'): inner._mcp_servers = list(mcp_servers)`. `grep -n '_mcp_servers' langchain.py langgraph.py llama_agents.py` returns zero hits, so for those three bridges `_inject_agent_runtime` returns having done nothing — no warning, no error, no journal record.
- `docs/reference/easyconfig.md:43` — '`mcp_servers` — optional list of MCP server URIs (`stdio://`, `sse://`, `http://`, `https://`) passed through to agent bridges; frozen per session.' No caveat about which bridges consume it; no other docs page names the supported set.
- `src/easycat/integrations/agents/generic_workflow.py:104` — `GenericWorkflowBridge.on_turn_start` does the right thing — it records a framework-error warning when `recorder.context.mcp_servers` is non-empty in shallow mode. The three framework bridges that also cannot consume MCP servers have no equivalent.

*Verified:* All four citations verified at the exact lines, including the zero-hit grep for `_mcp_servers` in the three framework bridges and the absence of any docs caveat (`grep -rn mcp_servers docs/` returns only easyconfig.md:43 and the teaching chapter). Severity medium is right: silent misconfiguration, bounded to one feature, small fix. I dropped the original sub-recommendation to add a no-op `configure_runtime` to BridgeTemplate — BridgeTemplate is not on the delivery path for these three bridges, so it would not help.

#### 114. The LangGraph example speaks the model's intermediate research draft to the caller and streams nothing

`MEDIUM` · `correctness` · `effort: small`

**Problem.** `examples/langgraph_voice.py` is the LangGraph reference wiring listed in examples/README.md:73. Both nodes call the non-streaming `model.invoke(...)`, so the bridge falls back to `_on_chat_model_end`, which emits each node's complete raw model output as a `text_delta`. The research node's whole draft is spoken aloud before the write node's answer, the `Research: ` prefix the node actually stores in graph state is not what the caller heard, and time-to-first-audio is the sum of two full LLM generations.

**Impact.** A user copying the reference example gets a voice bot that reads its own scratchpad to the caller and waits two full model round-trips before the first word of audio — in a framework whose value proposition is low-latency streamed voice. The recorded `done.text` disagrees with what was spoken, so postmortem journals mislead about what the caller actually heard.

**Fix.** Rewrite `examples/langgraph_voice.py` as a single streaming node (`async def` node with `await model.astream(...)`, or `ChatOpenAI(..., streaming=True)` with `ainvoke`) so tokens reach TTS as they arrive. If a multi-node graph is worth demonstrating, give the internal node a model instance excluded from the event stream (or tag its run and teach the bridge to suppress tagged runs) and say so in the example docstring. `examples/function_tools_langgraph.py` has the same non-streaming shape via `create_react_agent(ChatOpenAI(...))` — that one does not speak a draft, but it does pay the latency, so give it `streaming=True` too.

**Evidence**

- `examples/langgraph_voice.py:45` — `reply = model.invoke(state['messages'])` in the `research` node, wrapped as `AIMessage(content=f'Research: {reply.content}')` on line 46; a second `model.invoke` in the `write` node on line 50. `ChatOpenAI` defaults to non-streaming, so no `on_chat_model_stream` fires for either.
- `src/easycat/integrations/agents/_langchain_events.py:479` — `_on_chat_model_end` exists to emit a `text_delta` for models that never streamed, skipping only runs already in `chat_streamed_run_ids` — which a `.invoke()` call never is. It therefore fires for BOTH nodes, so the caller hears the raw research draft followed by the final answer.
- `src/easycat/integrations/agents/langgraph.py:593` — `acc.model_text_streamed = True` is set for any `text_delta` from `translate_stream_event`, including the `_on_chat_model_end` fallback, so `acc.accumulated` becomes draft+answer. `_finalize_done` (line 669-673) then sets `done.text` to the final AI message only — the journal and `AgentFinal` record just the `write` node's reply while TTS actually spoke both. The docstring at 649-655 acknowledges this 'speculative-streaming gap'.
- `docs/extending/agent-bridge.md:99` — 'Bridges yield `text_delta` events as text arrives; Session splits them into sentences for TTS. Buffering a whole reply before yielding adds first-audio latency.' The example violates the project's own stated rule twice over — two sequential non-streaming model calls before any audio.

*Verified:* Traced the full chain and confirmed each link: two `model.invoke` calls in the example, `_on_chat_model_end`'s streamed-run guard at 478-481 which a `.invoke()` run never satisfies, `acc.model_text_streamed` set by the fallback at langgraph.py:591-593, and `_finalize_done`'s `if acc.model_text_streamed:` branch at 669-673 preferring the final AI message for `done.text`. Corrected two claims: the docstring acknowledging the gap is at langgraph.py:649-655 (cited as 650-655), and `examples/function_tools_langgraph.py` is single-node so it suffers only the latency, not the spoken-draft bug — I narrowed that sentence. Also dropped the original recommendation to add an 'assert nodes are async def' smoke test, which would not have caught this bug (an async node calling `ainvoke` on a non-streaming model behaves identically).

#### 115. Bridge boilerplate is duplicated across files rather than shared, and the complexity gate is waived for the whole layer

`LOW` · `over-engineering` · `effort: small`

**Problem.** Three concerns are re-implemented per bridge instead of shared: the message content accessors (duplicated verbatim between langchain.py and langgraph.py), the interruption truncation convention (seven copies of the same expression), and the per-turn cursor lifecycle (three bridges use `recorder.turn_cursor`, three hand-roll it). Ruff's complexity rules are waived for every file in the layer, so the growth is unpoliced.

**Impact.** A policy change to the truncation convention, or a bug in the message accessors, has to be made in up to seven places by hand. This is the mechanism behind the divergences documented in findings 1 and 6: the stop-at-user guard in the interruption rewrite exists in langchain.py (_rewrite_last_ai_in, 900-906) and langgraph.py (1231) but not in openai_agents.py:398-424 or pydantic_ai.py:306-322.

**Fix.** Move `_content_of` / `_set_content` / `_role_of` / `_message_is_ai` / `_message_is_human` into one `src/easycat/integrations/agents/_messages.py` imported by langchain.py and langgraph.py. Add a `truncated_replacement(delivered_text)` helper in `base.py` next to `apply_standard_interruption` and call it from all seven sites. Treat the `[tool.ruff.lint.per-file-ignores]` entries at pyproject.toml:275-281 as debt with an owner rather than a permanent waiver.

**Evidence**

- `src/easycat/integrations/agents/langchain.py:938` — `_content_of` (938-941) and `_set_content` (944-951) are byte-identical to langgraph.py:1537-1551 — the same two helpers maintained in two files.
- `src/easycat/integrations/agents/langgraph.py:1186` — One of seven copies of `delivered_text + '...' if delivered_text else ''` (also openai_agents.py:387, responses_api.py:289, llama_agents.py:865, pydantic_ai.py:268, langchain.py:794, _agent_runner.py:412). The truncation convention is a policy decision replicated seven times with no single owner.
- `src/easycat/integrations/agents/template.py:100` — `BridgeTemplate` owns the invoke cursor lifecycle, the BaseException cleanup arm, `apply_interruption` + the four-step journal protocol, scrubbed `_serialize_framework_state`, and safe no-op mutation defaults. `GenericWorkflowBridge` (generic_workflow.py:39) is its only subclass in src/.
- `pyproject.toml:275` — `[tool.ruff.lint.per-file-ignores]` waives C901/PLR0912/PLR0915 for all seven files in the bridge layer (lines 275-281) — the project's own complexity gate is switched off for exactly this directory.

*Verified:* Downgraded high → low and rewrote the framing, which was wrong. The original claim that 'the repository already contains the correct abstraction' and that the six bridges should migrate onto `BridgeTemplate` does not survive reading template.py:1-3, which describes itself as a 'starter base class for NEW agent bridges' — an out-of-tree authoring aid, not a retrofit target — and openai_agents.py:200-209 carries a specific documented reason its dual-cursor handoff lifecycle cannot use a single `turn_cursor` context manager. `BridgeTemplate.invoke` wraps one `stream_events` generator and cannot express langgraph's multi-cursor node lifecycle. So the 'migrate six bridges, one per PR' recommendation is speculative and I removed it. What survives verification is concrete and small: verbatim helper duplication across two files, seven copies of one expression, and the ruff waiver — roughly a 20-line dedup, which is a low-severity cleanup, not a high-severity maintenance trap. The drift argument the original used as impact is already captured as an actionable fix in finding 1.

---

## What the twelve lenses missed

A separate agent audited the seams between the lenses above and the directories nobody was assigned. Its report follows verbatim.

I audited the unowned seams myself. Findings below, by area, with severity and concrete remediation.

---

### 1. `src/easycat/stages/` — the layer no lens owned

The architecture lens looked at `stages/agent.py` only (finding #17). The other six stages contain a systematic defect none of the twelve lenses would have found.

#### 1.1 `debug="off"` does not turn off per-frame journaling work — and only 3 of 7 stages gate it (medium)

Three stages guard their capture work behind a runtime check:

- `src/easycat/stages/tts.py:49` — `capture_enabled = ctx.journal is not None or ctx.artifact_store is not None`, then `state_before = self.snapshot_state() if capture_enabled else None` (`tts.py:51`)
- `src/easycat/stages/transport.py:53`, `transport.py:56` — identical
- `src/easycat/stages/agent.py:207`, `agent.py:209` — `journal_enabled = ctx.journal is not None`

Four do not. `src/easycat/stages/audio.py:58`, `src/easycat/stages/vad.py:72`, `src/easycat/stages/stt.py:49` and `src/easycat/stages/turn.py:53` all call `state_before = self.snapshot_state()` unconditionally, then build `start_extra` dicts, then `await put_artifact_async(...)`, then call `journal_append_event(...)` which early-returns at `src/easycat/stages/base.py:318` (`if ctx.journal is None: return None`). The two ungated stages are precisely the two that run **per audio frame** (50 fps): `AudioStage` (NR + AEC) and `VADStage`.

`grep -c "capture_enabled\|journal_enabled" src/easycat/stages/*.py` returns 8/5/4 for agent/tts/transport and **0** for audio, vad, stt, turn.

I measured the cost against the real classes with `journal=None, artifact_store=None` (i.e. `debug="off"`):

```
AudioStage.execute   7.37 us/frame   (bare provider.process: 0.17 us) -> 7.20 us overhead
VADStage.execute     8.30 us/frame   (bare provider.process: 0.98 us) -> 7.32 us overhead
combined            14.52 us/frame  = 0.73 ms/s/session
                                    = 4.6% of one core at the documented max_sessions=64
```

`TurnStage` is worse in kind than in frequency: `src/easycat/stages/turn.py:54` calls `_concat_chunks(input)` — a full `b"".join` copy of the pre-roll buffer plus the whole utterance (`turn.py:174-181`) — unconditionally at every endpoint decision, then hands it to `put_artifact_async`, which discards it at `base.py:189` because there is no store.

**Do:** hoist `capture_enabled = ctx.journal is not None or ctx.artifact_store is not None` into `stages/base.py` as a shared helper and apply it in all seven `execute` methods; guard `_concat_chunks` in `turn.py:54` behind it. Add a test asserting `snapshot_state` is not called when `RunContext` has neither journal nor store — nothing currently pins this, which is why three stages drifted apart from four.

#### 1.2 The `Stage` protocol's `replay` half is dead weight, compounding finding #58 (low)

Finding #58 established `easycat replay` never re-executes. What it did not note: `Stage.replay` is a **required** protocol member (`src/easycat/stages/base.py:126`) with a full implementation in every one of the seven stages (`audio.py:191`, `vad.py:153`, `turn.py:121`, etc.), plus `ReplayCassette`/`ReplaySpec`/`ReplayFidelity` plumbing, plus the "detector-specific extension" `replay_decision` carve-out documented at `base.py:138-146`. That is several hundred lines of an abstraction whose only consumers are `src/easycat/debugger/_sources.py:229` and `src/easycat/cli/debug/replay.py:188`, both of which go through `bundle.replay(spec)`, which #58 showed does not re-execute. It is also the reason `snapshot_state` exists at all on the hot path (§1.1).

**Do:** either wire one real `LIVE` replay path end to end, or demote `replay` from the `Stage` protocol to an optional capability probe and delete the six unreachable implementations. Doing neither keeps a per-frame cost in service of an unreachable feature.

---

### 2. `src/easycat/audio_format.py` — the framework's most-constructed type has zero validation

Rated **medium**. `AudioFormat` (`src/easycat/audio_format.py:13-27`) is a frozen dataclass with no `__post_init__` and no `raise` anywhere in the file — I checked: `grep -n "__post_init__\|raise ValueError" src/easycat/audio_format.py` returns nothing. This is the only configuration-shaped type in the package that is unvalidated; contrast `VADConfig.__post_init__` (`src/easycat/vad/factory.py:55-61`), `ReconnectConfig` (`src/easycat/reconnecting_ws.py:49-51`), `PauseProcessor.__post_init__`, and the `_validate_positive_int` / `_validate_non_negative_ms` helpers used throughout `config/`.

Verified reachable, running the real classes:

```python
f = AudioFormat(sample_rate=0, channels=0, sample_width=0)  # constructs fine
AudioChunk(data=b"ab", format=f).num_samples  # ZeroDivisionError  (audio_format.py:66-68)
AudioChunk(
    data=b"abcd", format=AudioFormat(0, 1, 2)
).duration_ms  # ZeroDivisionError (audio_format.py:70-72)
```

This is exactly the failure a third-party transport hits: it parses a sample rate out of a malformed SDP / Twilio `start` payload / WAV header, stamps it onto an `AudioFormat`, and the error surfaces as an untraceable `ZeroDivisionError` inside `duration_ms` on a background ingress task rather than at the line that built the bad format. `docs/extending/transport.md` tells authors to construct `AudioChunk`s and never mentions any constraint.

**Do:** add `__post_init__` to `AudioFormat` validating `sample_rate > 0`, `channels >= 1`, `sample_width in (1, 2, 4)`, and a non-empty `encoding`. It is five lines, matches the codebase's own convention everywhere else, and converts a class of downstream mystery into a construction-site error.

---

### 3. The CLI as a product surface

**Ratio.** Developer tooling in `src/`: `cli/` 8,881 + `validation/` 4,996 + `debugger/` 4,517 + `debug/` 3,823 + `planning/` 913 + `project/` 756 = **23,886 of 87,433 LOC = 27%**. On the test side, `tests/cli` (12,243) + `tests/debugger` (5,609) + `tests/debug` (3,230) + `tests/validation` (1,448) + `tests/teaching` (9,034) + `tests/docs` (1,730) + `tests/examples` (2,352) = **35,646 of 141,024 = 25%** of the suite guards tooling and prose rather than the runtime.

My judgment: the *debug-first* half of this (bundles, journal, debugger UI, replay) is defensible — it is the project's stated thesis and it is genuinely well built. The `validation/` half is not, and finding #86 is right. But two things nobody flagged:

#### 3.1 Twelve of the sixteen top-level commands are bundle-inspection variants, and two are literal duplicates (medium)

`easycat --help` shows 16 top-level commands. Eight of them (`inspect`, `replay`, `latency`, `diff`, `tail`, `bundles`, `debugger`, `journal`) all operate on the same two inputs — a `.zip` bundle or a `.sqlite` journal — and they overlap:

- `src/easycat/cli/_app.py:1236` registers `tail` → `follow_journal`. `src/easycat/cli/debug/bundles.py:501` registers `journal follow` → **the same function object**, with different help text (`"Live-tail a SQLite journal as it grows."` vs `"…, redacting every line."` — the same function, two contradictory descriptions of whether it redacts).
- `src/easycat/cli/debug/bundles.py:482-497` defines `inspect_bundle` as a verbatim copy-paste of `show_bundle` (`bundles.py:460-477`) — identical argument list, identical `--issues`/`--json` options, identical single-line body `_show_bundle_summary(bundle_path, json_output=json_output, issues=issues)`. Its docstring admits it: *"Friendly alias for `easycat bundles show`"*. Registered at `_app.py:1232` and `bundles.py:520`.
- `debugger` is a Typer sub-app (`_app.py:1238`) containing exactly one command, `serve` (`bundles.py:394`).

**Do:** collapse to `easycat bundle {show,list,export,replay,diff,latency,grep,follow,promote,ui}` and delete `inspect_bundle` entirely (it is duplicated code, not a Typer alias). Keep top-level slots for `console`, `init`, `doctor`, `serve`, `plan`, `docs`, `explain`. This removes ~40 lines of copy-paste and roughly halves the discovery surface.

#### 3.2 Repo-only commands write into the user's project directory and report `fail` with no explanation (medium)

Finding #86 named the coupling. The concrete behavior, which I reproduced from an empty directory outside the repo using the installed entry point:

```
$ easycat validate quick
quick: fail; report:
.easycat/validation/runs/20260726T170701Z-quick-1613293-1a2523d2/report.json
```

It created `.easycat/validation/runs/…` inside my cwd, exited non-zero, and gave no indication that the command is meaningless outside the EasyCat checkout. **Do:** have `validate` detect the absence of the repo's `tests/` tree and exit with `EASYCAT_E6xx`-style guidance ("`easycat validate` runs this repository's own test lanes and only works inside an EasyCat checkout") *before* creating any directory.

#### 3.3 `easycat console` records the user's microphone to disk by default, and the banner does not say so (medium — privacy)

`src/easycat/cli/console.py:44` sets `_DEFAULT_RECORD_DIR = ".easycat/recordings"`; `console.py:246-250` makes `--record-to` a `Path` option defaulting to it with **no sentinel to disable it**; `console.py:217` builds `EasyConfig.mic(agent=agent, debug="light", record_to=record_dir)`. The live-voice banner (`console.py:56-59`) says *"OPENAI_API_KEY and a microphone detected — starting a live voice session. Speak into your microphone; press Ctrl-C to end the session."* — nothing about recording. The bundle path is printed only *after* the session, at `console.py:268`. Per finding #63, that bundle contains unfiltered PCM audio and transcripts.

This is the command README leads with for first-run. It is mitigated by the `easycat init` scaffold shipping `.easycat/` in its `.gitignore` (verified in `src/easycat/cli/scaffold/templates/openai-agents/.gitignore`), but the user is not told at the moment of capture.

**Do:** put "This session will be recorded to `<path>` (audio + transcript)" in the live-voice banner before `session.start()`, and accept `--record-to none` / `--no-record`.

---

### 4. Cost and spend accounting — the largest genuinely-absent cross-cutting concern

Rated **high** as a product gap (not a defect).

`grep -rniE "cost_usd|token_usage|prompt_tokens|completion_tokens|billing|spend"` across `src/easycat` returns **zero** runtime hits. The OpenTelemetry metric catalog at `src/easycat/_observability.py:36-52` is otherwise thorough — `easycat.turn.latency`, `easycat.queue.depth`, `easycat.queue.dropped.total`, `easycat.event_loop.lag`, `easycat.interruption.cutoff_latency`, `easycat.transport.disconnects.total` — and contains no cost, token, character, or provider-seconds metric. No journal record carries usage. No event carries usage.

This was a deliberate removal, not an oversight: `plan/peripherals/peripheral-observability-and-cost.md:3-10` records that `CostRecord`, `max_session_cost_usd`, `cost_budget_*` records and a `stop(force=True)` kill switch **had partially landed and were removed** as *"undercooked and duplicative with the journal."*

I think the decision is wrong on its own terms, and the twelve lenses independently supply the evidence:

- The journal cannot serve as the cost ledger. Finding #55 (which I re-verified against `src/easycat/runtime/journal_memory.py:46,212`) shows the **default** journal is an in-memory ring that discards the session after ~40 seconds with no marker. A cost record written there is gone before the call ends.
- Uncapped spend is already the harm in two independent high-severity findings. #66: `max_call_duration_s` never ends a call, so "a stuck IVR… keeps a live phone leg plus streaming STT/TTS/LLM billing running indefinitely." #65: an unauthenticated Twilio media port lets anyone "force provider sessions to be opened… N concurrent OpenAI Realtime handshakes." Both were written as if the missing piece were a timer or a token. The missing piece is also that **nothing in the framework can observe or bound spend**, so neither failure is detectable until the provider invoice arrives.
- `session_manager.py` and `VoiceServerConfig.max_sessions` bound *concurrency*, which is a proxy for cost only if every session is short.

**Do:** the minimum viable version is not the removed design. It is (a) a `usage` field on `AgentFinal` / `STTFinal` / `TTSEvent` populated from what every provider SDK already returns, (b) two counters in `_observability.py` (`easycat.provider.requests.total`, `easycat.provider.units.total` labelled by surface and unit), and (c) one paragraph in `docs/observability.md` showing the `session.on(...)` subscriber that turns those into a spend budget. Pricing tables and a `easycat cost` command are correctly out of scope; *emitting the units* is not.

---

### 5. Internationalization — no language story at all in the core, next to a meticulously localized telephony corner

Rated **medium**, and the internal asymmetry is what makes it a finding rather than a scope choice.

`EasyConfig` has **no** `language` field. The only `language` on it is `callee_language: str = "en"` at `src/easycat/config/easy.py:400`, buried in `TelephonyConfig` and used solely to pick screening patterns (`src/easycat/config/_outbound_helpers.py:134`). Language lives only on individual provider configs, each with its own default and type: `src/easycat/stt/deepgram_provider.py:39` (`language: str = "en"`), `src/easycat/stt/cartesia_provider.py:41` (`"en"`), `src/easycat/tts/cartesia_tts.py:53` (`"en"`), `src/easycat/stt/openai_provider.py:50` (`str | None = None`), `src/easycat/stt/elevenlabs_provider.py:54` (`None`), `src/easycat/stt/openai_realtime_provider.py:73` (`None`).

Nothing coordinates them. `grep -rn "language" src/easycat/config/_factory.py src/easycat/config/_tts_alignment.py src/easycat/cli/diagnose/doctor.py src/easycat/planning/*.py` returns **nothing** — the factory that goes to real trouble aligning TTS *sample rate* to the transport (`_tts_alignment.py`) has no equivalent for language. So `EasyConfig(stt=DeepgramSTTConfig(language="es"), tts=CartesiaTTSConfig())` gives you Spanish transcription and English speech, with no warning from `create_session`, `easycat doctor`, or `easycat plan`. `grep -rniE "multiling|language|i18n|locale"` over `README.md` and `docs/reference/easyconfig.md` returns **zero hits** — the field reference does not mention language exists.

Meanwhile `src/easycat/telephony/screening.py:82-188` carries hand-curated Google Call Screen / iOS / carrier prompt patterns in **eight languages** (en/es/fr/de/pt/ja/ko/zh), including CJK. Someone put real care into localizing the narrowest feature in the package while the headline config surface has no language concept.

Two more consequences nobody traced: the bundled Silero VAD and the smart-turn v3.2 endpoint classifier (`src/easycat/models/smart-turn-v3.2-cpu.onnx`) have no documented language coverage anywhere in `docs/`, so a non-English user has no way to know whether the endpointing model was trained on their language — and finding #50's pre-roll/`min_speech_duration_ms` tuning advice in `docs/latency.md:118` is implicitly English-timed.

**Do:** add `language: str | None = None` to `EasyConfig`, fan it out to whichever provider configs the user did not set explicitly (the same mechanism `openai_api_key` already uses), and raise/warn when a user-set STT language and TTS language disagree. Add one row to `docs/reference/easyconfig.md` and one sentence to the smart-turn docs stating the model's language scope.

---

### 6. Graceful degradation — detection without remediation, and no failover

Rated **medium**.

`grep -rniE "failover|fallback_provider|secondary_stt|backup" src/easycat` returns exactly **one** hit — a comment. There is no mechanism to fail over from one STT/TTS/agent provider to another when one is down. That is a legitimate scope choice for 0.1.0, but two things make it worth raising:

1. **The health checker's remediation hook is wired to a log line.** `src/easycat/_health_check.py` is a well-built piece of code — consecutive-failure streak, threshold-gated single `Error` emission, `on_unhealthy`/`on_recovered` transitions. The only in-tree consumer wires it at `src/easycat/session/_session.py:1021-1027`, and `_on_provider_unhealthy` (`_session.py:936-950`) is a `logger.warning` whose docstring says *"recovery is delegated to provider reconnect / Error subscribers."* `grep -rn "PeriodicHealthChecker\|on_unhealthy" docs examples README.md` returns **nothing** — there is no documented example of the `Error` subscriber the code delegates to. So the framework detects staleness and then does nothing, and does not show the user how to do something.

2. **429 handling exists on exactly one code path.** `STTBase._run_with_bounded_retry` (`src/easycat/stt/base.py:153-195`) is a careful implementation — retries only 429 among HTTP status errors, exponential backoff, clamped attempt count. Its only two callers are `src/easycat/stt/openai_provider.py:164` and `src/easycat/stt/elevenlabs_provider.py:601` — both **batch HTTP** transcription. `grep -n "429" src/easycat/stt/websocket_base.py src/easycat/tts/*.py` returns nothing. Every streaming provider — the ones actually used in a live call — has no rate-limit awareness at all: a 429 on WebSocket connect goes into `ReconnectingWebSocket`'s generic exponential backoff (`src/easycat/reconnecting_ws.py:186-222`), which does not read `Retry-After` and will hammer a rate-limited account through its full retry budget on every turn.

**Do:** (a) ship one worked `Error`-subscriber recipe in `docs/observability.md` showing teardown-on-unhealthy, since the code explicitly delegates to a pattern that is nowhere written down; (b) surface HTTP status / `Retry-After` from the WebSocket handshake into `ReconnectingWebSocket` so a 429 backs off on the server's terms rather than the client's; (c) state in `docs/` that cross-provider failover is out of scope, so nobody assumes the dual-backend VAD fallback chain implies one for STT/TTS.

---

### 7. Multi-tenancy and abuse — the codebase has zero IP awareness

Rated **medium**, and it sharpens findings #65, #72 and #78 with an angle none of them named.

`grep -rniE "remote_addr|per.?ip|client_ip|X-Forwarded-For|throttl" src/easycat --include='*.py'` returns eight hits, **all** of which are unrelated (`peripheral-*.md` doc references, Deepgram's flush throttle, the playback-mark throttle). There is no client IP anywhere in the package: not in the journal, not in logs, not in a rate limiter, not in `X-Forwarded-For` parsing behind the reverse proxy `docs/deployment/production-servers.md:243` tells operators to deploy.

Consequences, on top of the auth findings already logged:

- The only capacity control in the whole product is a **global** counter — `VoiceServerConfig.max_sessions = 64` (`src/easycat/server/config.py:48`), `WEBRTC_MAX_SESSIONS`, WebTransport's `max_concurrent_sessions`. One client that opens 64 connections denies service to every other tenant, and #65 confirms a Twilio media socket does not even need to authenticate to consume a slot.
- An operator who *notices* abuse has nothing to block on. The journal — "the single source of truth for all observability" per CLAUDE.md — records no peer address, so a debug bundle cannot answer "who did this."
- The operations checklist at `docs/deployment/production-servers.md:242-262` covers ingress, worker affinity, session caps, shutdown, persistence, health probes and observability, and never mentions per-client limits or abuse response.

**Do:** thread the peer address (with `X-Forwarded-For` handling gated on a `trusted_proxies` setting) into the session's journal metadata and into `SessionAcceptor`, add an optional `max_sessions_per_client` alongside `max_sessions`, and add an "Abuse and per-client limits" bullet to the operations checklist. Even logging the address without enforcing anything would be a large improvement over the current state.

---

### 8. Warm start and horizontal scaling

#### 8.1 Every new connection loads a fresh ONNX graph synchronously on the event loop (medium)

`SileroVAD.__init__` calls `self._load_model()` eagerly (`src/easycat/vad/silero.py:150`), which constructs a new `onnxruntime.InferenceSession` per instance (`silero.py:181-186` → `_SileroOnnxModel.__init__`, `silero.py:54-73`). There is **no module-level cache** — `grep -n "^_\|lru_cache\|cache" src/easycat/vad/silero.py` shows only constants and the model path.

`create_session` is synchronous (`src/easycat/config/_factory.py:690`) and builds the VAD inline at `_factory.py:426` (`vad = _create_vad(config.vad) if enable_vad else None`). The server calls it from inside an async handler: `src/easycat/server/websocket.py:84` does `session = session_factory(ws)`, and `websocket.py:122-126` defines that factory as `create_session(config_factory(transport))`. So **every incoming WebSocket/WebRTC connection blocks the event loop — and therefore every other live call's audio — for the duration of an ONNX graph load and allocation**, and a 64-session process holds 64 independent copies of the Silero graph.

`Session.start` goes to considerable trouble about exactly this class of problem — the comment at `src/easycat/session/_session.py:983-991` explains that warmup was deliberately ordered before `transport.connect()` because slow warmup was overflowing the inbound queue. But that reasoning stops at `Session`; the model load happens *earlier*, in `create_session`, outside any of it.

**Do:** cache the `_SileroOnnxModel` (and the smart-turn `InferenceSession`, `src/easycat/smart_turn.py:476`) at module level keyed by model path, sharing the read-only graph across sessions while keeping the per-session recurrent state (`reset_states`, `silero.py:76-81`) separate — that is what the state/graph split already permits. Add an `async def create_session_async` (or move the model construction into the existing `warmup` hook) so the graph load never runs on the loop for server deployments.

#### 8.2 State persistence across restarts is genuinely absent and genuinely undocumented (low)

`docs/deployment/production-servers.md:245-247` does cover worker affinity honestly ("in-memory session registries are per process. Use sticky routing or an external control plane"). What no doc covers: conversation history does not survive a process restart for any bridge except LangGraph-with-a-user-supplied-checkpointer (and finding #108 shows that path is broken for async savers). `grep -rniE "persist.*conversation|resume a conversation|history.*(restart|persist)" docs/` returns nothing. **Do:** one sentence in the deployment guide stating that conversation state is per-process and per-session, and that resumption is the application's responsibility.

---

### 9. Internal planning vocabulary shipped in the wheel's public docstrings (low)

`grep -rnE "\bM[0-9]+[a-z]?\b" src/easycat --include='*.py'` returns **111** hits. These are not comments in private helpers — they are in the docstrings `help()` renders for public types:

- `src/easycat/server/config.py:27-43` — `VoiceServerConfig`'s class docstring reads *"M4 reads host / port / max_sessions… M5 makes auth / unsafe_allow_no_auth / allow_query_token LIVE… enable_metrics — metric emission/registration is M8."*
- `src/easycat/server/voice_server.py:1` — *"`VoiceServer` — the M5 production process layer."*
- `src/easycat/planning/__init__.py:1`, `src/easycat/cli/plan.py:1`, `src/easycat/transports/webtransport.py`, `src/easycat/server/transports.py:1`, and 100+ more.

Two problems. First, "M5"/"M6b"/"M8" appear nowhere in `docs/` (one stray hit at `docs/observability.md:205`), so a user reading `help(VoiceServer)` sees a vocabulary with no glossary. Second, the milestone claims **go stale**: `server/config.py:41` says `enable_metrics` is future work ("M8"), but `src/easycat/server/routes.py:274` reads `if server.config.enable_metrics:` today — the flag is live and its own docstring says it is not.

Comparable class: `src/easycat/cli/debug/bundles.py:15`, `src/easycat/tts/cartesia_tts.py:100`, `src/easycat/debugger/__init__.py:3` and `src/easycat/config/easy.py:677` cite `peripheral-cli.md`, `peripheral-telephony-tts-output.md`, `peripheral-eval-and-debugger-ui.md` and `peripheral-dx-onboarding.md` — files that live in `plan/peripherals/`, are not shipped in the wheel, and are not linked from anywhere in `docs/`.

**Do:** strip milestone codes and `plan/` filenames from every docstring in `src/`; keep them in `plan/` where they belong. Add a lint rule (`grep`-based, in the existing guard lanes) so they cannot come back — this is exactly the kind of drift the seven guard recipes were built for and none of them checks.

---

### 10. Areas I checked and found genuinely fine

- **Repo hygiene is excellent** and I want to be explicit about it, since I was asked to look: `git ls-files | grep -c __pycache__` → **0** (the untracked `src/easycat/__pycache__` is covered by `.gitignore:1`); TODO/FIXME/XXX/HACK across all of `src/easycat` → **2**, both intentional (`src/easycat/cli/debug/promote.py:70,78`, describing a `TODO` the tool *emits* into generated test files); bare `except:` → **0**; no commented-out code (all 34 regex hits are prose comments wrapping onto a line starting with `for`/`if`); no dead modules (my import-graph scan's only candidate, `easycat.validation._release_runner`, is imported at `src/easycat/validation/runner.py:9`). 58 silent-swallow sites (`except Exception: pass` + `suppress(Exception)`) across 87k LOC is unremarkable.
- **Licensing and third-party attribution.** `LICENSE` (BSD-2), `license-files` declared at `pyproject.toml:9`, and each vendored model carries its own file naming upstream repo and vendored tag: `src/easycat/models/LICENSE.silero-vad` ("Vendored from tag: v6.2.1"), `LICENSE.smart-turn`, `LICENSE.funasr-fsmn-vad`. The vendored FunASR runtime carries its copyright header at `src/easycat/vad/_funasr_runtime/e2e_vad.py:3-4`. `pyproject.toml:82` even documents *why* TEN VAD is not vendored. Better than most projects at this stage.
- **The debugger's security posture is the strongest in the package** and deserves to be cited as the internal standard the transports fall short of. `src/easycat/debugger/server.py:540-588` layers four defenses (exact-loopback `Host` against DNS rebinding, `Sec-Fetch-Site`, `Origin`, and a JSON-content-type + present-`Origin` requirement on state-changing methods); `server.py:1546-1553` refuses a non-loopback bind without `allow_remote=True` and says so ("The debugger has no auth"); `src/easycat/debugger/_records.py:144-197` contains a hand-written ReDoS guard on user search regexes. `src/easycat/debugger/dev.py:22-25,95-105` gates auto-launch on TTY + `!CI`. Contrast finding #75 (WebTransport, no auth, `0.0.0.0`).
- **`easycat.project/` (756 LOC).** Sound. `discover_manifest_path` (`src/easycat/project/loader.py:46-71`) does **not** walk parent directories, so the `python:module:attribute` agent resolver (`src/easycat/project/manifest.py:53-56`) is not a drive-by code-execution vector; `easycat plan` explicitly avoids resolving the agent (`src/easycat/cli/plan.py:10`); unknown-key strictness and the `bearer-env:NAME` secret contract are enforced at parse time.
- **`session_manager.py`.** The class docstring (`src/easycat/session_manager.py:19-37`) states its concurrency contract precisely, including the `connection`/`stop_all` overlap hazard it does *not* handle. The `add()` failure path (`session_manager.py:53-62`) correctly avoids erasing a replacement that claimed the key while `start()` was in flight. Good code.
- **`debugger/session_registry.py`.** Uses `weakref.finalize` with a strong-closure fallback and a prune-on-read backstop (`session_registry.py:115-151`) — no session leak from dev-mode registration.
- **`push_to_talk.py`, `recipes.py`, `stubs.py`, `strip_markdown.py`, `_health_check.py`** (design, as distinct from its unwired hook in §6): all small, single-purpose, and referenced from docs/examples. `Noop*` stubs are only reachable via explicit text-session construction (`src/easycat/config/_factory.py:977-984`) and `SessionConfig` field defaults (`src/easycat/session/_session.py:159-164`) — no auto-fallback path can silently substitute one for a real provider.
- **`_observability.py` metric catalog** (`_observability.py:36-65`) is comprehensive on latency, queues, errors, loop lag and server state. My only complaint is the cost gap in §4.
- **Accessibility** of the shipped browser clients is thinner than ideal but not broken: `src/easycat/transports/static/webrtc_client.html:142` correctly marks the transcript `aria-live="polite"`, and `<html lang>` is set on all three pages. The connection-status element (`webrtc_client.html:136`) lacks `role="status"`, so state changes are not announced, and `examples/ws_browser_client.html` has zero ARIA in 309 lines. Low severity; a two-attribute fix.

---

## What to delete

| Target | LOC saved | Risk |
|---|---|---|
| **WebTransport entirely** — `src/easycat/transports/webtransport.py` (1,577), `src/easycat/server/webtransport.py` (68), `examples/webtransport_server.py`, `tests/**webtransport**` (4,070), the `webtransport` extra | **~5,750** | **Low.** Optional extra, requires certs + HTTP/3 ingress, has zero auth code, rests on three private aioquic attributes, and the deployment guide already calls it niche. Deletes #73 and #75 outright and removes half of #69. The clearest pure-cost item in the repo. |
| **Move `src/easycat/validation/` out of `src/`** to `scripts/validation/` (4,996) — and drop `easycat validate` from the installed CLI | **~5,000 from the wheel** | **Medium.** Requires updating the `justfile`, seven guard lanes, and `tests/validation` (1,448) + the `tests/cli/test_validate_*` set. Payoff: mypy/ruff/import-linter stop policing CI glue as product code, and installed users stop getting `uv run pytest` fired at their cwd (#86). Also breaks the `runtime → validation.redaction → _provider_catalog → httpx` import chain I hit — the journal hot path currently drags the provider catalog in. |
| **One of the two tutorial ladders** — `docs/using-easycat/` (25 files, 19,426 words) + its guard tests | **~2,500 test LOC + 25 docs files** | **Low-medium.** It is simultaneously the thinner ladder (19k vs 78k words) and the one entirely missing from site nav. Verify the README/llms.txt pointers first (#95, #91). |
| **Prose-assertion guard tests** — `tests/teaching/test_feature_ladder.py` (902), `tests/teaching/test_ladder_index.py` (714), `tests/teaching/test_regen_teaching_chapters.py` (832, duplicates `.github/workflows/docs.yml:56`) | **~2,450** + ~40 s of runtime | **Low.** Keep the executable per-chapter probes — docs that don't run do rot. Delete only the layer that asserts on literal prose (#83). |
| **Command-hint validator machinery** — `tests/_command_hints.py` (661) + `tests/docs/test_command_hint_validator.py` (318, meta-tests of the validator, GUARD_EXEMPT so they run in the fast loop) | **~980** | **Low.** #85. |
| **Competitor benchmark tests** — `tests/perf/test_framework_latency_benchmark.py` (267) | **267** + 32 s | **Low.** Keep `perf/bench_framework_latency.py` — it is the raw material for the positioning doc that does not exist (#96). Delete only the CI-gated arithmetic assertions (#84). |
| **The 15 s ElevenLabs timeout sleep** — `tests/stt/test_stt_elevenlabs.py:75` | ~10 | **None.** The same file already shortens the timeout in five other tests (#84). |
| **Fake-only provider contract loop** — the fakes inside `tests/contracts/test_stt_provider_contracts.py` that exist only to be tested, plus the false CI comment at `.github/workflows/nightly-validation.yml:189` | ~300 | **Low, but *repoint* rather than delete.** Subclass `ProviderContractSuite` against the four real bundled providers (~30 LOC each). Keeps the kit and makes it mean something (#81). |
| **Five unraised error codes** — `EASYCAT_E304/E305/E401/E402/E403` in `src/easycat/errors.py:402-475` (verified: zero raise sites outside `errors.py`) | ~80 | **Low.** Either raise them from the provider/timeout paths (better — fixes #39/#57) or delete them. Documenting a code that can never appear is worse than not documenting it. |
| **Replay's unimplemented vocabulary** — the fidelity/tool-policy/`--timing wall` surface in `src/easycat/runtime/replay.py` (818) and `src/easycat/cli/debug/replay.py` (230), plus the eight `Stage.replay` implementations with no production caller | **~600 of the 1,048** | **Medium.** Reduce the CLI to what it does (a record walker) rather than deleting wholesale — the *idea* is part of the differentiation. But shipping flags that describe behavior that does not exist is worse than shipping fewer flags (#58). |
| **Dead code with test pins** — `_tts_scheduler.py:293` `NotImplementedError` placeholder + `tests/session/test_tts_scheduler.py:687`; `_split_frames` in `echo_cancellation.py:67` | ~40 | **None.** #21, #53. The test-pinned placeholder is the worse pattern — it makes dead code un-deletable by any automated cleanup. |
| **Docs route registry in the shipped CLI** — the maintainer-only routes and seven raw pytest guard strings in `src/easycat/cli/_app.py:642-991` (registry is 63% of a 1,256-line file) | ~400 | **Low.** Split maintainer routes behind `--audience maintainers` (already a flag) and stop compiling internal pytest lane strings into the wheel (#92). |

**Rough total: ~18,000 LOC removable at low risk**, plus ~2 minutes off the serial test run.

---

## Strengths worth protecting

This list is not consolation. It is the part of the codebase that a cleanup must not break, and several items are the reason the recommended deletions are cheap.

**The agent-bridge seam is the real asset.** I independently confirmed it: grepping all of `src/easycat` outside `integrations/agents/` for framework names turns up only comments, CLI help, and scaffold templates. `AgentStage` dispatches solely on `AgentRunner` vs. bridge. This is the hard part of a multi-framework adapter layer and it was done correctly. **Demoting bridges does not damage this — it is exactly what makes demotion cheap.** `register_agent_detector`, `BridgeTemplate`, and the published `AgentBridgeContractSuite` mean an out-of-tree bridge is a first-class citizen.

**Lazy PEP 562 exports and import-weight discipline.** `import easycat` at ~10 ms over interpreter baseline with 92 names exposed and zero provider SDKs touched, with `tests/test_public_api.py` verifying the `TYPE_CHECKING` block matches the lazy registry in both directions, and `tests/planning/test_boundary.py` asserting import weight in *fresh subprocesses*. Most frameworks get this wrong. Do not let a cleanup collapse the registry back into eager imports.

**Docs-as-value-contracts (not docs-as-prose-contracts).** `docs/latency.md` is the best page in the repo and its guard test parses the Markdown table and asserts each value against the live dataclass — that pattern is worth *expanding*, not cutting. Same for `docs/reference/easyconfig.md` (I spot-checked every field; zero drift) and the `llms.txt` generator. The deletion list above targets prose assertions specifically; keep every guard that asserts a *value*.

**Cancellation and teardown reasoning, where it is correct.** The `CancelToken` timestamp-under-`threading.Lock` (so the flag flips synchronously ahead of the deferred `Event.set()`), the consistent `asyncio.current_task().cancelling()` checks, the agent-stream drain that finishes in-flight tool calls before bailing, `_finish_interrupted_start`'s shielded rollback loop, and `CapacityGate._escalate_graceful_stop`'s bounded force escalation. The barge-in bugs (#24–#28) are *placement* errors — work on the wrong task, guards in the wrong order — not reasoning errors. Fix placement without touching the primitives.

**Bounded queues everywhere with explicit drop policy and a journal hook.** Both audio queues bounded, `DROP_NEWEST` for outbound with the rationale written at the call site, drops emit a degraded event. Most voice frameworks ship an unbounded `asyncio.Queue` here.

**Untrusted-input handling.** The bundle loader (traversal rejection + SHA-256 content verification + five independent caps, never `extractall`), the context-pack allowlist with a post-write re-scan, the debugger's four-layer DNS-rebinding/CSRF guard, `server/auth.py`'s non-ASCII `compare_digest` guard, 0600/0700 journal and artifact permissions, bundled ONNX with `torch.hub` explicitly refused for supply-chain reasons, and zero `pickle`/`eval`/`shell=True`/`verify=False` in the entire tree. Theme T6 is that this rigor is applied unevenly — the answer is to **extend** it to the socket-binding boundary, never to relax it.

**Frame-boundary statefulness in the DSP stages.** LiveKitAEC, RNNoise, Silero, TenVAD, and FunASR all carry sub-frame remainders across calls instead of zero-padding, each with a comment explaining why zero-padding corrupts the adaptive/recurrent state. The `resample` bug is in the *substrate*, not in these — do not let a resampler fix disturb the accumulators.

**Test-infrastructure fixtures worth their weight.** `fail_on_leaked_asyncio_tasks` (autouse, with an explicit escape hatch), `_restore_easycat_logger_state` (fixes a real documented cross-test pollution bug), collection-time marker linting, the flaky-quarantine policy with a mandatory `review_by` date that fails collection when stale, and the property tests placed exactly where they earn their keep (resampler invariants, the byte→text interruption estimator). The suite has *no* flaky quarantine and 11 total `in caplog.text` assertions repo-wide. When cutting 40% of the test tree, keep all of this — it is the part that makes the remaining 60% trustworthy.

**Release and supply-chain infrastructure.** SHA-pinned Actions, `id-token: write` scoped to a single job, caching disabled in the publishing build with the cache-poisoning reason recorded inline, hash-pinned docs requirements, Dependabot with a 7-day soak and risk-split groups, a non-root healthchecked multi-stage Dockerfile, and Python 3.11–3.14 actually exercised. This is better than most funded projects. The gap (#101) is that it has never *run* — cut a `v0.0.1a0` tag against TestPyPI and find out now rather than on release day.

---

## Appendix: findings that were rejected

These were raised by a lens and killed by its verifier. They are recorded because a rejected finding documents a place where the codebase already does the right thing — and because a reader who suspects one of these should know it was checked.

### Architecture, module boundaries, and coupling

- **The session/ collaborator split relocated code without reducing it: package grew 55% while Session shrank 14%** — The central impact claim is disproven by `tests/session/_wiring_helpers.py:32`, a `make_wiring()` helper whose docstring states it exists so that 'unit tests that construct a collaborator directly (without a host Session)' can do so. It is used by `tests/session/test_audio_router.py`, `test_stt_committer.py`, `test_cancel_orchestrator.py`, and `test_tts_scheduler.py` — four of the five collaborators ARE constructed and unit-tested with no Session at all. The finding asserts the exact opposite ('no collaborator can be constructed or unit-tested without a live Session'), and its recommendation asks for isolation that already exists. The causal attribution also fails: `git log c1ce2ff1..HEAD -- src/easycat/session/` returns 216 commits, overwhelmingly feature work (preemptive generation, TTS/agent overlap, latency ordering, telephony, journal records), and 583 of the added lines are new feature modules (`_warmup.py` 157, `_caller_id.py` 137, `_greeting.py` 112, `_debug_backends.py` 99, `_telephony_facade.py` 78) that have nothing to do with the extraction sequence. Attributing +3022 lines to 'ceremony' is unsupported; the actual refactoring artifacts are `_builder.py` (332) and `_wiring.py` (188), most of which is relocated construction code. The verified residue — `build_session` is 235 lines and `SessionComponents` is unpacked onto 17 fields — is a linear, branch-free assembly function that ruff does not flag at all, i.e. trivia.
- **Turn cancellation is hand-rolled per call site instead of scope-owned: 11 of 15 asyncio.shield uses and all 5 Task.cancelling() probes live in session/** — Three of the four supporting facts are wrong. (1) `.cancelling()` probes are NOT 'the only such probes in the entire library' — grep finds 9 in src/, with `runtime/scope.py:282`, `server/transports.py:286`, and `config/_telephony_wiring.py:133` outside `session/`. (2) `TaskGroup` is not 'used exactly once in the whole codebase' — it appears zero times in `src/` (only in a teaching example asserted as a source string by `tests/teaching/test_chapter_02_stream_lifecycle.py:86`), so the premise that the project already adopted it is false. (3) 'Roughly 350 of _turn_runner.py's 1282 lines are cancellation plumbing' — the seven named helpers span lines 870-1017, about 148 lines, so the figure is inflated ~2.4x. The recommendation is also already partly implemented: `runtime/scope.py:263-294` `RuntimeScope.drain` is exactly the shield-plus-`cancelling()` primitive the finding asks to be created there. Finally, the core recommendation to route `agent_task`/`tts_task` (`_turn_runner.py:705-706`) through `RuntimeScope` targets tasks that are already owned and awaited by their parent `_run_streaming_agent` in a `try/finally`, so the claimed orphaned-task risk is speculative. What is left is the accurate but non-diagnostic observation that 11 of 15 `asyncio.shield` uses are in the most concurrency-sensitive package, which is expected rather than defective.
- **The underscore-module convention costs navigability without buying encapsulation: Session.__module__ is easycat.session._session** — The facts check out (`easycat.Session.__module__ == 'easycat.session._session'` verified at runtime; 103 of 263 non-__init__ modules are underscore-prefixed; `_wiring.py:188` does export `_SessionTurnHandle` in `__all__`), but no concrete harm is demonstrated — the impact reduces to tracebacks 'reading as internals' and hypothetical pickle/Sphinx paths, with no in-repo instance of either causing a problem. The recommendation is also misgraded: it calls the rename 'small — two files, `git mv` plus the `_public_api.py` and `__init__.py` registrations', but grep shows 41 files import `easycat.session._session` and 32 import `easycat.session._types`, so it is a 73-site churn. The other half of the recommendation, `Session.__module__ = "easycat"`, changes runtime introspection and pickle-by-reference resolution for a cosmetic gain and is not something to do on this evidence. The one genuinely actionable item — renaming `_SessionTurnHandle` since it sits in `__all__` — is a one-line nit that does not carry a finding.

### Async correctness, cancellation, and backpressure

- **The per-stream reconnect budget never replenishes, so a long call dies after N lifetime blips** — The mechanism is real (`reconnecting_ws.py:315` initialises `remaining_reconnects` once per `recv_iter` and decrements at 327 without replenishment) but the stated harm — 'the STT stream terminates permanently, mid-call' — is disproven by the provider restart paths. For non-persistent providers, every `start_stream()` builds a brand-new `ReconnectingWebSocket` and a new `recv_iter` (`stt/base.py:70-79` -> `stt/deepgram_provider.py:154-163` -> `websocket_base.py:72-97`), so the budget resets each turn and the 45-minute-call scenario cannot occur. For the persistent path (Deepgram Nova default), an exhausted budget nulls `_ws` (`reconnecting_ws.py:353-355`) and the very next `start_stream()` hits `_ensure_persistent_connection` (`deepgram_provider.py:175-193`), which sees the dead socket, discards it, and reconnects — so the worst case is one turn's transcript lost plus a surfaced `ConnectionError` provider event (`websocket_base.py:198-201`), not a dead call. The lifetime budget is also a documented deliberate tradeoff: the comment at `reconnecting_ws.py:308-314` explains it exists to stop 'unbounded successful reconnect churn' from a peer that accepts and immediately drops.

### Audio pipeline correctness and latency

- **The audio pipeline has no benchmark and no CI regression gate; the two committed perf baselines are byte-identical stale journal results** — The strongest piece of evidence collapses on inspection. plan/workstreams/workstream-3-stage-refactor.md:3 opens with '> **Status: partially stale historical record.**', so citing AC3.15 (:511, :574) as an unmet contract is citing a document that explicitly disclaims itself — an untracked-in-CI perf gate described in a self-declared stale plan doc is not a defect. Of what remains: `diff perf/baseline.json perf/ws3-final.json` is indeed empty (verified) and `grep -rn perf .github/workflows/*.yml` finds nothing (verified), but a duplicated 40-line JSON file is a housekeeping nit, not a finding. The rest — 'add perf/bench_audio_pipeline.py, add a baseline, wire a +5%/+10% gate' — is process advice that would apply verbatim to any Python project, with no demonstration that a specific regression slipped through. The concrete defects this finding claims a benchmark would have caught are already reported individually above; restating 'and we should have had a benchmark' adds no actionable information.
- **FunASR VAD synthesizes future timestamps and runs ONNX inline without yielding, so an atomically-reported segment emits no VAD events at all** — The headline is disproven by running the code. I instantiated the real FunASROnnxVAD state machine and called `_evaluate_funasr_segments([[0, 1000]], now)`: it emits ['VADStartSpeaking', 'VADStopSpeaking'], not nothing. The `boundary_now = now + (end_ms - beg_ms)/1000` line at funasr.py:188 is not a bug — it is the mechanism that makes it work. `_evaluate_speech(1.0, now)` latches `_speech_start_time = now`, then the second call at the future timestamp presents an elapsed time equal to the segment's true audio duration, which is exactly what the 250 ms speech gate needs; deleting the line as recommended would break the backend, not fix it. The trailing `_evaluate_speech(0.0, boundary_now)` fires the stop immediately because funasr.py:133-135 deliberately pins `self._min_silence_duration_ms = 0` (FunASR applies the silence gate internally via `max_end_sil`), with a comment explaining exactly that. The 'three different values for one knob' sub-claim is also wrong: there are two (150 in `_VADBase.__init__`/`configure`'s kwarg default, 50 in `VADConfig`), and `create_vad` always calls `configure`, so the 150 is only reachable by hand-constructing a backend and never calling configure. The one true residual — funasr.py:162-176 lacks the `await asyncio.sleep(0)` that silero.py:242 has — is a low-value nit on an opt-in backend and does not carry a 'turns are silently dropped' finding.

### Transports, telephony, and server runtime

- **OutboundCallStateMachine is not a state machine: transitions have no table, transition() is unguarded public API, and classification correctness depends on helper subscription order** — The load-bearing claim is false. The finding says the helper-ordering contract "exists only as a code comment with no test pinning it" — but tests/telephony/test_outbound_helper_builder.py:25-44 is titled `test_builder_preserves_default_helper_order_and_shared_patterns`, docstringed "Ordering-sensitive listeners and classifiers remain deterministic", and asserts the exact 7-tuple of helper class names plus `built.state_machine is built.helpers[3]` and `built.screening_detector is built.helpers[4]`; tests/telephony/test_outbound_helper_builder.py:47-65 pins the reduced order too. Reordering two `self._helpers.append(...)` lines in config/_outbound_helpers.py fails those tests immediately. The remaining claims are accurate but are design preference with no demonstrated failure: `_transition` (call_state.py:521) does more than assign — it emits `CallStateChanged`, updates SmartTurn suppression and releases the classification gate — and the `_on_call_initiated` reset scenario at :482 requires reaching HUMAN with an empty `_call_sid`, which cannot happen because `CallAnswered` sets it at :584/:591. No concrete illegal transition or misclassification is shown.
- **TransportDegraded is documented as a public transport concept but has exactly one subscriber and nothing acts on it — including fatal=True** — The docs already say exactly what the finding says they fail to say. The event's own docstring at src/easycat/events.py:268-272 reads "Emitted on the session EventBus so SessionJournalSink can record drop / poison / abort conditions that would otherwise only reach the debug log — keeping the journal the single source of truth for observability", and docs/extending/transport.md:122-124 frames it identically: "Emit TransportDegraded ... instead of logging when you drop frames — the journal is the single source of truth for observability." Both state plainly that this is journal observability, not a control-plane contract, so the claimed harm (extension authors misled into expecting the framework to react) is not supported by the cited text. The single-subscriber fact is verified (session/_journal_sink.py:330, :455) but matches the documented design. What remains — "have the session react to fatal=True" and "surface inbound_queue_full to the STT stage" — is a feature request, not a defect, and outside the mandate to verify rather than propose.

### Security posture

- **No prompt-injection boundary or guidance anywhere, while the CLI actively suggests wiring a filesystem MCP server behind a phone-callable agent** — The framing that the CLI "actively suggests" a filesystem MCP server does not survive inspection. The `@modelcontextprotocol/server-filesystem` string at src/easycat/cli/scaffold/init.py:613 appears only inside the body of an `EASYCAT_E102` validation error that fires when a user has ALREADY passed `mcp_servers` and typed a URI with an invalid scheme (init.py:610-616). A grep of src/easycat/cli/scaffold/templates/ returns zero `mcp` hits — no scaffold template wires an MCP server, by default or otherwise, so no scaffolded project is phone-callable-with-filesystem-tools unless the user explicitly builds that. Stripped of that hook, the remainder is the observation that the docs contain no prompt-injection page, with a recommendation to write one — boilerplate advice that applies identically to every agent framework, and which this library is poorly positioned to act on since it delegates prompt construction entirely to the OpenAI Agents SDK / PydanticAI / LangChain bridges in src/easycat/integrations/agents/. The scheme-prefix check at config/easy.py:154 is correctly described but is a URI validator, not a claimed security boundary. No concrete defect, no failure scenario with specific inputs.

### Test suite quality and cost

- **tests/perf (882 LOC) tests the benchmark scripts' arithmetic, not the system's latency, and none of it gates anything** — The central claim is factually wrong. 'The only numeric gate in the whole latency story is an 8000ms sanity bound at tests/e2e/test_plan_7_latency_benchmark.py:983' is disproved by the eight SLO thresholds defined at tests/e2e/test_plan_7_latency_benchmark.py:709-716 and enforced at lines 995-1061: baseline p50 5000ms, baseline p90 6500ms, full-stack p50 4500ms, and — the meaningful ones — relative overhead budgets for debug=full journal (800ms), debug=light journal (400ms), noise reduction (500ms), echo cancellation (500ms) and smart turn (800ms), all collected into one combined AssertionError. src/easycat/validation/_latency_budgets.py:45-47 adds p95 budgets for total_ms, tts_ttfb_ms and llm_ttft_ms. The relative-overhead gates are exactly the regression signal the finding says does not exist. The real (much narrower) issue is that tests/e2e/test_plan_7_latency_benchmark.py:108-118 marks the file integration_live and it skips without OPENAI_API_KEY, so the gates are live-only — but that is a different, far weaker finding than the one written. The remaining verified content is duplicative: the 32.6s I measured for test_external_framework_worker_smoke and its presence in the default collection are already covered by the developer-velocity finding, and 'delete ~215 LOC of arithmetic tests in tests/perf' is a low-value nit with no demonstrated harm.

### Documentation and meta-tooling

- **The tutorial teaches learners to import private symbols, contradicting the project's own public-API rule** — Three separate disproofs. (1) The claimed rule does not say what the finding claims: docs/public-api.md:33-36 governs the top-level allowlist — 'must use this allowlist when they write `from easycat import ...`; import implementation-specific names from their submodules instead'. All three scripts import from submodules, which is exactly what the rule prescribes; it says nothing about underscore-prefixed submodule constants. (2) The pedagogical-harm claim is contradicted by the source: docs/teaching/00-hello-audio/format_boundaries.py:20-22 reads 'This repo-local drift probe intentionally reads their runtime constants; application code should configure transport formats instead', and tts_alignment_probe.py:28-30 reads 'application code should not import underscore-prefixed names'. The scripts explicitly teach the opposite of what the finding says they teach. (3) The mechanical-harm claim ('renaming silently breaks the tutorial; only compileall covers it') is false — all three scripts are executed under `subprocess.run(..., check=True)` by dedicated pytest tests: tests/teaching/test_chapter_00_format_boundaries.py:15-22, tests/teaching/test_chapter_00_tts_alignment.py:16-23, and tests/teaching/test_chapter_13_event_bus_contract.py:14-23. Renaming `_REALTIME_SAMPLE_RATE`, `_OPENAI_PCM_FORMAT` or `_PROVIDER_TO_CONFIG` fails those tests in `just check` and in `just guard-teaching`.
- **A first-time contributor must satisfy 7 guard lanes, 8 validation lanes, 23 strict markers, 3 generators and 9 pre-commit hooks before a one-line PR** — The central claim is false and the recommendation is already implemented. CONTRIBUTING.md:7-13 is the document's first section, '## Quick start', and is exactly the 'Small change? Do this' block the finding asks for: `uv sync --group dev` / `just` / `just check  # fmt-check + lint + tests (the pre-PR gauntlet)`. justfile:134 defines `check: fmt-check lint test`, and pyproject.toml:356 documents that guard tests are 'excluded from the fast dev loop but always run in `just test`/`check` and in the guard-* lanes' — so `just check` alone covers all seven guard lanes. The guard-routing matrix the finding cites is introduced at CONTRIBUTING.md:99-100 as a narrowing shortcut ('run the narrow guard that owns that surface *before* the broader validation lane'), i.e. a way to do less, not an added obligation. The remaining claims are accurate counts (9 pre-commit hooks, 24 markers in pyproject.toml:344-368) but describe maintainer-facing reference material, not a gate on a one-line PR.
- **The docs-about-docs machinery is larger and more brittle than the docs it guards, and none of it guards the published site** — Rejected as duplicative and miscounted. Its one verified defect — 'the mkdocs nav is the only unguarded representation and it has drifted' — is the nav finding, already confirmed above; nothing is added by restating it. The distinctive over-engineering claim carries a materially wrong number driving its main deletion recommendation: `grep -rho 'BEGIN auto:[a-z-]*'` shows exercise-protocol(16) + self-check-protocol(16) + exercise-completion(16) + practice-handoff(16) = 64 markers, not the '120 of the 223' the finding claims it would delete. Its test-LOC figure is also inflated (tests/teaching is 9,034 lines, not 10,764 — the finding appears to have double-counted tests/docs' 1,730). The remaining claims are accurate size measurements (_DOCS_LINKS spans src/easycat/cli/_app.py:198-989, 63% of the module; scripts total 1,612 LOC) but describe a working generator-plus-staleness-check design, which is standard practice, with no demonstrated concrete harm beyond 'you must re-run the generator', which is the design's intended behavior.

### Packaging, dependencies, and release readiness

- **PydanticAIBridge needs a construction-time require_module guard (sub-claim of the `easycat[all]` finding)** — Not reachable. src/easycat/integrations/agents/pydantic_ai.py:65 takes a caller-supplied `agent=` object, so any user with a real PydanticAI agent has already run `from pydantic_ai import Agent` in their own module and fails there — never inside a voice turn. The bridge's lazy internal imports (pydantic_ai.py:305, 353, 379) are only reached on that same code path. The original's evidence explicitly constructed the bridge with a stub object, which no real user does. Contrast OpenAIAgentsBridge, which imports `Runner` from the SDK inside EasyCat itself and therefore does carry a guard (tests/integrations/agents/test_agent_bridge_install_hints.py:21). The surviving documentation gap is retained as a separate low finding.
- **Three extras (deepgram, elevenlabs, cartesia) install nothing and are decorative** — Already justified and load-bearing. pyproject.toml:62-67 documents them as install markers so downstream tooling enumerates the provider surface uniformly, and src/easycat/stt/factory.py:45-53 confirms the providers genuinely need no vendor SDK (they use httpx/websockets). Declaring the extra is what keeps `pip install easycat[deepgram]` from erroring, and README.md:170-173 explains the design to users. Removing them would break a documented, tested surface to save nothing.
- **`silero-vad` and `smart-turn` are byte-identical duplicates that should be merged or aliased** — Not a defect. They are distinct user-facing features (VAD backend vs. endpoint detector) that happen to share numpy+onnxruntime; tests/test_dependency_policy.py:47 deliberately asserts they share the *same* onnxruntime constraint rather than diverging. Merging them into one `onnx` extra would force users who want only Silero VAD to reason about smart-turn, which is worse ergonomics, not better.
- **The `openai` extra pins an SDK the library never imports and should become an empty marker** — Wrong. src/easycat/debug/testing.py:489 does `from openai import AsyncOpenAI` for the default LLM-judge in `assert_llm_judge()`, and openai-agents pulls the SDK transitively anyway. It is true that the OpenAI STT/realtime/TTS providers use httpx (verified at stt/openai_provider.py:10, tts/openai_tts.py:9, stt/openai_realtime_provider.py), but emptying the extra would strip the judge's dependency with no declared home.
- **The FastAPI/uvicorn pins in `telephony` exist only for scaffold template text** — Overstated: examples/twilio_app.py:115 imports FastAPI in `create_app()`, and README.md:167 documents `uv sync --extra telephony` as the way to run it, so the pins do serve a real in-repo path. The narrower and correct version — that these belong with the example/scaffold rather than in a library extra, and that python-multipart has no consumer at all (twiml.py:173-178 parses the body with parse_qsl) — is retained as a separate low finding.
- **release-validation.yml's 40/40 failure rate proves the release gate is broken and decorative** — Misreads the CI history. All 45 retained runs carry `event: push` from ~2026-05-29 on the `fix/library-review` branch, GitHub reports run 26622070548 as "likely failed because of a workflow file issue" with no retained logs, and the current .github/workflows/release-validation.yml:3-8 declares only `workflow_dispatch` and `workflow_call` — no push trigger. These are workflow-startup failures from a stale trigger config, not the gate failing on its steps. The correct statement is that the gate has never executed at all, which is retained in the rewritten release-readiness finding.
- **`easycat/testing/contracts.py` ships with an unguarded `import pytest` and should be gated behind a `testing` extra or excluded from the wheel** — Generic and counterproductive. The module's own docstring (contracts.py:1-24) markets it as a subclassable contract kit for out-of-tree provider authors, whose usage is writing pytest test files — every real importer has pytest by construction, and tests/contracts/README.md:15-16 documents EasyCat's own suites subclassing it. The failure scenario cited is a synthetic `pkgutil.walk_packages` sweep. Excluding it from the wheel would break the documented extension workflow.

### Agent-framework bridges

- **Five first-party framework bridges is more surface than one maintainer with zero real-SDK CI can carry** — Not a defect — a product-strategy recommendation (delete bridges, move them to companion packages, delete the HITL and time-travel machinery) whose evidence is entirely line counts plus a restatement of findings 1, 2, 3 and 5. Its load-bearing factual claim is also wrong: OpenAIAgentsBridge is not 'the only bridge that provably cancels the in-flight LLM call' — llama_agents.py:371 and :294 drive `_cancel_local_handler` (which calls the handler's `cancel_run()`) and `_cancel_remote_handler` on every teardown path, and langgraph.py:850 / langchain.py:264 cancel by closing `astream_events`. The 'real-world demand is unclear' judgements about `_SuspendableSource` (llama_agents.py:935-1045) and the LangGraph resume cursor (langgraph.py:218-228) are assertions with no evidence in the repo either way; llama_agents.py:433-441 documents a concrete bug that `_SuspendableSource` was written to fix. The actionable residue — 'gate every first-party bridge behind a real-SDK AgentBridgeContractSuite run in the nightly extras matrix' — is already the recommendation of the confirmed finding on CI coverage, so keeping this entry would double-count it.

---

## Method

26 agents, 4.4M tokens, 2,085 tool calls, 74 minutes wall clock. Phase 1: twelve lenses (public API/DX, architecture, async correctness, provider extensibility, audio pipeline, errors and observability, transports and telephony, security, test suite, docs and meta-tooling, packaging, agent bridges), each instructed to cite file:line for every claim, to grep for existing mitigation before asserting absence, and to treat over-engineering as a first-class defect. Phase 2: one adversarial verifier per lens, pipelined so verification began as soon as a lens finished. Phase 3: a synthesis agent for themes and strategy, and a completeness critic for the unowned seams.
