# Explicitly Rejected — Do Not Re-Litigate

Both audits ran every candidate through an adversarial verifier whose default stance was
"this is wrong / already done / not worth it." The items below were **killed on purpose**,
with concrete repo evidence. They are recorded here so they are not re-proposed in a future
sweep. If you think one should be revisited, the burden is to refute the stated reasoning
with new evidence — not to re-raise it fresh.

Source syntheses: [`reports/maintainability-plan.md`](reports/maintainability-plan.md) ·
[`reports/code-quality-report.md`](reports/code-quality-report.md).

---

## A. Maintainability proposals that were rejected (8)

1. **Shared `tests/_fakes/` package with Protocol-conformance assertions + migrate the 21 `FakeSession`s.**
   The conformance mechanism already ships (`src/easycat/testing/contracts.py`, guarded by
   `just guard-contracts`). `Session` is a concrete class, not a `runtime_checkable` Protocol,
   so the proposed canonical-fake `isinstance` test cannot exist. The 21 `_FakeSession`
   classes fake **disjoint** role slices; one canonical fake would be a god-object or an empty
   base with no drift protection. Only real duplication: five ~4-line `_pcm16_bytes` helpers —
   a 30-minute fold, not a package.

2. **Extract shared LangChain message accessors into `_lc_messages.py`.**
   The real part is ~12 identical lines and a shared home already exists (`_helpers.py`). The
   headline "shared `rewrite_last_ai_message`" is infeasible: the two rewrite functions operate
   on fundamentally different persistence models (in-memory shadow list vs. LangGraph checkpoint
   state) and share only an innermost loop.

3. **Extract the 532-line `_DOCS_LINKS` map out of `cli/_app.py` into a named module.**
   `_app.py` (~996 lines) isn't among the large modules; the block is a single grep-able list
   literal whose location is already documented (`scripts/regen_llms_txt.py`). A pure move
   delivers no correctness benefit and invites merge friction against constant concurrent
   `codex/*` merges.

4. **Full/maximalist deprecation machinery across *all* aliases + a written removal policy.**
   PEP 702 can't decorate the module-level `_PROVIDERS` dict or the `settings` dataclass field,
   so an "every alias carries the decorator" guard is unsatisfiable; an invented "removal 0.3"
   policy contradicts CLAUDE.md's deliberate "kept alias" wording. *(The scoped, feasible
   subset — decorate the three `Session` methods + settings-fold warnings — is **accepted as QW8**.)*

5. **`griffe check` signature-level API-break detection in CI.**
   `git tag -l` returns zero tags and `griffe check` diffs against the last release tag —
   a permanent no-op until a first release, and it fights the deliberate pre-1.0 API churn the
   test suite already documents (`test_culled_symbols_remain_available`).

6. **towncrier changelog with a PR-fragment gate.**
   No tags, no publish step, no adopters, no existing CHANGELOG to conflict — the "avoid
   CHANGELOG merge conflicts" benefit is moot. High per-PR friction on a stream dominated by
   tiny agent-authored hardening PRs, for near-zero reader value at 0.1.0.

7. **Convert the frozen advisory gates into enforced ratchets (mypy error-count baseline,
   diff-cover `fail-under`).**
   The mypy count is currently **non-deterministic** (CI command dies on a numpy stub;
   `python_version` 3.11 config vs 3.12 stubs), so a count baseline can't be pinned without first
   fixing the interpreter/stub mismatch. The repo already has a **superior** enforced ratchet
   (gated clean-core `mypy_gated_paths`). diff-cover `--fail-under` would false-block
   provider/transport PRs whose tests are integration-marked and excluded from the coverage slice.
   *(Only the trivial cleanup — remove the phantom `--fail-under` reference — is justified.)*

8. **Import Linter *layers* + *independence(all)* + *forbidden* as originally specified.**
   Two of three contracts contradict repo reality: `forbidden server/cli→webrtc` fails on the
   legitimate module-level import at `server/webrtc_routes.py`; `independence` including
   transports/telephony fails on real bidirectional coupling (`twilio_media.py`↔`telephony.dtmf`).
   *(The feasible subset — independence of `stt`/`tts`/`vad` + forbidden-with-ignores + deferred
   layers — is **accepted as QS4**.)*

---

## B. Code-quality findings that were refuted (12)

These were surfaced by hunters but **killed by the verifier** — the failure scenario was
unreachable or the premise was false. Do not file bugs for them.

1. **`STTCommitter.commit_now` finally clobbers a successor commit task** (`_stt_committer.py`) —
   Unreachable: asyncio doesn't defer cancellation during I/O suspension; single-flight guard +
   `cancel_scheduled()`-first callers + provider `_lifecycle_lock`/`_COMMIT_MIN_BYTES` all
   independently prevent it.
2. **Telephony racy `Semaphore.locked()` capacity idiom** (`telephony/server.py`) — Applies a
   threading race to single-threaded asyncio; `locked()` is sync and the following `acquire()`
   never suspends when a slot is free.
3. **HTTP TTS cancel-race: `httpx.StreamClosed` escapes the `HTTPError` swallow**
   (`tts/openai_tts.py`) — Verified against httpx 0.28.1: mid-stream close raises `ReadError`
   (an `HTTPError`, already swallowed); `StreamClosed`'s check isn't re-hit here.
4. **`DeepgramTTS.stop()` sends Flush then closes, dropping audio** (`tts/deepgram_tts.py`) —
   No runtime path calls TTS `.stop()` mid-synthesis (teardown uses `cancel()`).
5. **`LangGraphBridge` reads checkpointed state after cancel-break while run executes**
   (`langgraph.py`) — Cancel-detection branch + `get_state` are fully synchronous (no await),
   so the abandoned producer task can't interleave.
6. **debugger `_make_app` 880-line closure makes handlers untestable** (`debugger/server.py`) —
   Every heavy/pure concern is *already* module-level and importable; the closure holds only
   thin HTTP wiring. (Premise largely false; note the maintainability plan's **QS3** still splits
   `server.py` for size/churn reasons, which is a different and valid argument.)
7. **`play_hold_audio` fires a background task with no strong ref / no prior-cancel**
   (`_telephony_wiring.py`) — Task *is* stored (`self._hold_audio_task`); `gate.close()` is
   single-flight per answer.
8. **Classification/max-duration timers reassign without cancelling** (`telephony/call_state.py`) —
   Sole caller `_on_answered` is state-guarded and transitions before starting timers.
9. **TTS synthesizer `cancel()` swallows all exceptions with no logging** (`_tts_synthesizer.py`) —
   Bare-pass is an established coexisting teardown pattern (~26 sites); observability nicety only.
10. **config ↔ transports import cycle masked by function-local import** (`config/_factory.py`) —
    No actual import-time cycle; the deferred edge is a deliberate, documented PEP-562 convention.
11. **Force-path teardown self-awaits the pipeline task and deadlocks** (`_session.py`) —
    Force branch `cancel()`s the task before awaiting, so the self-await raises `CancelledError`
    immediately (swallowed), not a hang.
12. **Teardown await loops swallow cancellation of `stop()` itself** (`_session.py`) —
    Intentional: every awaited task is pre-cancelled, and running to completion is required by
    the documented teardown contract.

---

### Note on the distinction between B.6 here and QS3

The verifier refuted the *specific claim* that `_make_app`'s handlers are "untestable" (they
delegate to already-importable helpers). That is separate from the maintainability plan's **QS3**,
which splits `debugger/server.py` because it is the largest (3006-line) and highest-churn module
in the repo — a size/merge-conflict argument the verifier did **not** reject. Both can be true:
the handlers are testable *and* the file should still be split.
